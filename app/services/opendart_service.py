from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from io import BytesIO
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.errors import ExternalAPIError
from app.schemas import HanallSourceStatus, HanallStructuredFact


class OpenDartService:
    CORP_CODE_ARCHIVE_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
    RECEIPT_TIMESTAMP_PATTERNS = (
        re.compile(r"(\d{4}[.\-/]\d{2}[.\-/]\d{2})\s+(\d{2}:\d{2}:\d{2})"),
        re.compile(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*(\d{2}:\d{2}:\d{2})"),
    )
    VIEWER_CALL_PATTERN = re.compile(
        r'viewDoc\(\s*"(?P<rcp_no>\d+)"\s*,\s*"(?P<dcm_no>\d+)"\s*,\s*"(?P<ele_id>[^"]*)"\s*,\s*"(?P<offset>[^"]*)"\s*,\s*"(?P<length>[^"]*)"\s*,\s*"(?P<dtd>[^"]*)"(?:\s*,\s*"(?P<toc_no>[^"]*)")?\s*\)'
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._timezone = ZoneInfo(settings.app_timezone)
        self._corp_code_cache: dict[str, str] | None = None

    async def collect_company_updates(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[list[HanallStructuredFact], HanallSourceStatus]:
        checked_at = datetime.now(self._timezone)
        if not self._settings.opendart_api_key:
            return [], HanallSourceStatus(
                source_name="OpenDART",
                status="skipped",
                checked_at=checked_at,
                detail="OPENDART_API_KEY is not configured",
                hard_requirement=True,
            )

        corp_code = self._settings.hanall_dart_corp_code or await self._resolve_corp_code()
        params = {
            "crtfc_key": self._settings.opendart_api_key,
            "corp_code": corp_code,
            "bgn_de": window_start.strftime("%Y%m%d"),
            "end_de": window_end.strftime("%Y%m%d"),
            "sort": "date",
            "sort_mth": "desc",
            "page_no": 1,
            "page_count": 100,
        }
        async with httpx.AsyncClient(timeout=self._settings.opendart_timeout_seconds) as client:
            response = await client.get(f"{self._settings.opendart_base_url.rstrip('/')}/list.json", params=params)
            response.raise_for_status()
            payload = response.json()

            status_code = str(payload.get("status") or "")
            if status_code and status_code not in {"000", "013"}:
                message = payload.get("message") or f"OpenDART returned status={status_code}"
                raise ExternalAPIError(message)

            filings = payload.get("list") or []
            facts: list[HanallStructuredFact] = []
            for filing in filings:
                fact = await self._build_fact(
                    client=client,
                    filing=filing,
                    window_start=window_start,
                    window_end=window_end,
                )
                if fact is not None:
                    facts.append(fact)

        hard_count = sum(1 for fact in facts if fact.validation_mode == "hard")
        soft_count = len(facts) - hard_count
        detail = "공시 없음" if not facts else f"공시 {len(facts)}건 감지 (hard={hard_count}, soft={soft_count})"
        return facts, HanallSourceStatus(
            source_name="OpenDART",
            status="ok",
            checked_at=checked_at,
            detail=detail,
            hard_requirement=True,
        )

    async def _resolve_corp_code(self) -> str:
        stock_code = self._settings.hanall_dart_stock_code
        if self._corp_code_cache and stock_code in self._corp_code_cache:
            return self._corp_code_cache[stock_code]

        params = {"crtfc_key": self._settings.opendart_api_key}
        async with httpx.AsyncClient(timeout=self._settings.opendart_timeout_seconds) as client:
            response = await client.get(self.CORP_CODE_ARCHIVE_URL, params=params)
            response.raise_for_status()
            content = response.content

        corp_code_map = self._parse_corp_code_archive(content)
        self._corp_code_cache = corp_code_map
        corp_code = corp_code_map.get(stock_code)
        if corp_code is None:
            raise ExternalAPIError(f"OpenDART corp_code not found for stock_code={stock_code}")
        return corp_code

    def _parse_corp_code_archive(self, content: bytes) -> dict[str, str]:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            xml_name = next((name for name in archive.namelist() if name.lower().endswith(".xml")), None)
            if xml_name is None:
                raise ExternalAPIError("OpenDART corpCode archive contained no XML file")
            xml_content = archive.read(xml_name)

        root = ET.fromstring(xml_content)
        mapping: dict[str, str] = {}
        for item in root.findall("list"):
            stock_code = (item.findtext("stock_code") or "").strip()
            corp_code = (item.findtext("corp_code") or "").strip()
            if stock_code and corp_code:
                mapping[stock_code] = corp_code
        return mapping

    async def _build_fact(
        self,
        *,
        client: httpx.AsyncClient,
        filing: dict[str, str],
        window_start: datetime,
        window_end: datetime,
    ) -> HanallStructuredFact | None:
        receipt_no = (filing.get("rcept_no") or "").strip()
        report_name = (filing.get("report_nm") or "").strip()
        receipt_date = self._parse_receipt_date((filing.get("rcept_dt") or "").strip())
        if not receipt_no or not report_name or receipt_date is None:
            return None

        try:
            receipt_timestamp, timestamp_parse_status = await self._fetch_receipt_timestamp(client, receipt_no)
        except Exception:  # noqa: BLE001
            receipt_timestamp = None
            timestamp_parse_status = "fallback_fetch_failed"
        if receipt_timestamp is not None:
            if not (window_start <= receipt_timestamp <= window_end):
                return None
            observed_at = receipt_timestamp
            observed_date = None
            validation_mode = "hard"
            observed_label = observed_at.strftime("%Y-%m-%d %H:%M KST")
        else:
            if not (window_start.date() <= receipt_date.date() <= window_end.date()):
                return None
            observed_at = None
            observed_date = receipt_date.date()
            validation_mode = "soft"
            observed_label = observed_date.isoformat()

        source_url = f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}"
        fact_text = f"{report_name} 공시가 {observed_label} 기준 OpenDART에서 확인됨"
        return HanallStructuredFact(
            source_name="OpenDART",
            source_type="filing",
            entity="HanAll Biopharma",
            category="SEC/DART/KRX",
            title=report_name,
            fact_text=fact_text,
            source_id=receipt_no,
            source_url=source_url,
            observed_at=observed_at,
            observed_date=observed_date,
            validation_mode=validation_mode,
            timestamp_parse_status=timestamp_parse_status,
        )

    async def _fetch_receipt_timestamp(
        self,
        client: httpx.AsyncClient,
        receipt_no: str,
    ) -> tuple[datetime | None, str]:
        response = await client.get(f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={receipt_no}")
        response.raise_for_status()
        receipt_timestamp = self._parse_receipt_timestamp_text(response.text)
        if receipt_timestamp is not None:
            return receipt_timestamp, "parsed_main_page"

        viewer_url = self._extract_viewer_url(response.text, receipt_no)
        if viewer_url is None:
            return None, "fallback_parse_failed"

        viewer_response = await client.get(viewer_url)
        viewer_response.raise_for_status()
        receipt_timestamp = self._parse_receipt_timestamp_text(viewer_response.text)
        if receipt_timestamp is not None:
            return receipt_timestamp, "parsed_viewer_page"
        return None, "fallback_parse_failed"

    def _parse_receipt_date(self, raw_value: str) -> datetime | None:
        if not raw_value:
            return None
        return datetime.strptime(raw_value, "%Y%m%d").replace(tzinfo=self._timezone)

    def _parse_receipt_timestamp_text(self, raw_text: str) -> datetime | None:
        normalized = self._normalize_timestamp_text(raw_text)
        for pattern in self.RECEIPT_TIMESTAMP_PATTERNS:
            match = pattern.search(normalized)
            if match is None:
                continue
            if len(match.groups()) == 2:
                date_text = match.group(1).replace("/", "-").replace(".", "-")
                return datetime.strptime(
                    f"{date_text} {match.group(2)}",
                    "%Y-%m-%d %H:%M:%S",
                ).replace(tzinfo=self._timezone)
            month = int(match.group(2))
            day = int(match.group(3))
            return datetime.strptime(
                f"{match.group(1)}-{month:02d}-{day:02d} {match.group(4)}",
                "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=self._timezone)
        return None

    @staticmethod
    def _normalize_timestamp_text(raw_text: str) -> str:
        normalized = html.unescape(raw_text).replace("\xa0", " ")
        normalized = re.sub(r"<[^>]+>", " ", normalized)
        return re.sub(r"\s+", " ", normalized).strip()

    def _extract_viewer_url(self, main_text: str, receipt_no: str) -> str | None:
        for match in self.VIEWER_CALL_PATTERN.finditer(main_text):
            if match.group("rcp_no") != receipt_no:
                continue
            query = urlencode(
                {
                    "rcpNo": match.group("rcp_no"),
                    "dcmNo": match.group("dcm_no"),
                    "eleId": match.group("ele_id"),
                    "offset": match.group("offset"),
                    "length": match.group("length"),
                    "dtd": match.group("dtd"),
                }
            )
            return f"https://dart.fss.or.kr/report/viewer.do?{query}"
        return None
