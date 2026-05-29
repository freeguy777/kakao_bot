from __future__ import annotations

import html
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.schemas import HanallSourceStatus, HanallStructuredFact


class KindKrxService:
    SOURCE_NAME = "KIND/KRX"
    COMPANY_NAME = "한올바이오파마"
    ENTITY_NAME = "HanAll Biopharma"
    COMPANY_DISCLOSURE_PATH = "/disclosure/searchdisclosurebycorp.do"
    MARKET_ALERT_PATH = "/investwarn/investattentwarnrisky.do"
    VIEWER_PATH = "/common/disclsviewer.do"
    MARKET_ALERT_TABS = (
        ("1", "투자주의종목", "invstcautnisu_sub"),
        ("2", "투자경고종목", "invstwarnisu_sub"),
        ("3", "투자위험종목", "invstriskisu_sub"),
    )
    ROW_PATTERN = re.compile(r"<tr\b[^>]*>(?P<body>.*?)</tr>", re.DOTALL | re.IGNORECASE)
    CELL_PATTERN = re.compile(r"<td\b[^>]*>(?P<body>.*?)</td>", re.DOTALL | re.IGNORECASE)
    VIEWER_CALL_PATTERN = re.compile(r"openDisclsViewer\('(?P<acpt_no>\d+)'\s*,\s*'(?P<doc_no>[^']*)'\)")
    TITLE_ATTR_PATTERN = re.compile(r"\btitle=(?P<quote>['\"])(?P<title>.*?)(?P=quote)", re.DOTALL | re.IGNORECASE)
    MAIN_DOC_PATTERN = re.compile(r"<option\b[^>]*value=(?P<quote>['\"])(?P<doc_no>\d+)\|Y(?P=quote)", re.IGNORECASE)
    CONTENT_PATH_PATTERN = re.compile(r"parent\.setPath\([^,]*,\s*'(?P<url>https?://[^']+)'", re.IGNORECASE)
    KOREAN_DATE_PATTERN = re.compile(r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일")
    PRICE_MOVE_REASON_PATTERN = re.compile(r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일의\s*종가가[^|+]+?상승")

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._timezone = ZoneInfo(settings.app_timezone)
        self._base_url = settings.kind_krx_base_url.rstrip("/")

    async def collect_company_updates(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[list[HanallStructuredFact], HanallSourceStatus]:
        checked_at = datetime.now(self._timezone)
        async with httpx.AsyncClient(timeout=self._settings.kind_krx_timeout_seconds) as client:
            company_facts = await self._collect_company_disclosures(
                client=client,
                window_start=window_start,
                window_end=window_end,
            )
            market_alert_facts = await self._collect_market_alerts(
                client=client,
                window_start=window_start,
                window_end=window_end,
                existing_company_facts=company_facts,
            )

        facts = self._dedupe_facts([*company_facts, *market_alert_facts])
        detail = (
            "KIND/KRX 공시 없음"
            if not facts
            else f"KIND/KRX {len(facts)}건 감지 (회사별={len(company_facts)}, 시장경보={len(market_alert_facts)})"
        )
        return facts, HanallSourceStatus(
            source_name=self.SOURCE_NAME,
            status="ok",
            checked_at=checked_at,
            detail=detail,
            hard_requirement=True,
        )

    async def _collect_company_disclosures(
        self,
        *,
        client: httpx.AsyncClient,
        window_start: datetime,
        window_end: datetime,
    ) -> list[HanallStructuredFact]:
        payload = {
            "method": "searchDisclosureByCorpSub",
            "forward": "searchdisclosurebycorp_sub",
            "searchCorpName": self.COMPANY_NAME,
            "searchCodeType": "char",
            "repIsuSrtCd": f"A{self._settings.hanall_dart_stock_code}",
            "allRepIsuSrtCd": "",
            "fromDate": window_start.strftime("%Y-%m-%d"),
            "toDate": window_end.strftime("%Y-%m-%d"),
            "reportNm": "",
            "reportCd": "",
            "lastReport": "",
            "currentPageSize": "3000",
            "pageIndex": "1",
            "orderMode": "1",
            "orderStat": "D",
        }
        response = await client.post(f"{self._base_url}{self.COMPANY_DISCLOSURE_PATH}", data=payload)
        response.raise_for_status()

        facts: list[HanallStructuredFact] = []
        for row_html in self._table_rows(response.text):
            fact = await self._build_company_disclosure_fact(
                client=client,
                row_html=row_html,
                window_start=window_start,
                window_end=window_end,
            )
            if fact is not None:
                facts.append(fact)
        return facts

    async def _build_company_disclosure_fact(
        self,
        *,
        client: httpx.AsyncClient,
        row_html: str,
        window_start: datetime,
        window_end: datetime,
    ) -> HanallStructuredFact | None:
        cells = self._cell_html(row_html)
        if len(cells) < 5:
            return None

        observed_at = self._parse_observed_datetime(self._clean_text(cells[1]))
        if observed_at is None or not (window_start <= observed_at <= window_end):
            return None

        company_text = self._clean_text(cells[2])
        if not self._is_hanall_row(company_text):
            return None

        title = self._extract_title(cells[3])
        submitter = self._clean_text(cells[4])
        if not title:
            return None

        source_id = self._extract_accept_no(row_html)
        source_url = self._viewer_url(source_id) if source_id else self._company_search_url()
        detail_text = ""
        timestamp_parse_status = "parsed_company_search_row"
        if source_id:
            try:
                resolved_url = await self._resolve_external_document_url(client, source_id)
            except Exception:  # noqa: BLE001
                resolved_url = None
                timestamp_parse_status = "parsed_company_search_row_detail_fetch_failed"
            if resolved_url:
                source_url = resolved_url
                detail_text = await self._fetch_detail_text(client, resolved_url)
                timestamp_parse_status = "parsed_company_search_row_and_detail"

        fact_text = self._company_fact_text(
            title=title,
            observed_at=observed_at,
            submitter=submitter,
            detail_text=detail_text,
        )
        return HanallStructuredFact(
            source_name=self.SOURCE_NAME,
            source_type="filing",
            entity=self.ENTITY_NAME,
            category="SEC/DART/KRX",
            title=title,
            fact_text=fact_text,
            source_id=source_id,
            source_url=source_url,
            observed_at=observed_at,
            validation_mode="hard",
            timestamp_parse_status=timestamp_parse_status,
        )

    async def _collect_market_alerts(
        self,
        *,
        client: httpx.AsyncClient,
        window_start: datetime,
        window_end: datetime,
        existing_company_facts: list[HanallStructuredFact],
    ) -> list[HanallStructuredFact]:
        facts: list[HanallStructuredFact] = []
        for menu_index, alert_label, forward in self.MARKET_ALERT_TABS:
            payload = {
                "method": "investattentwarnriskySub",
                "forward": forward,
                "searchCorpName": self.COMPANY_NAME,
                "searchCorpNameTmp": self.COMPANY_NAME,
                "searchCodeType": "char",
                "repIsuSrtCd": f"A{self._settings.hanall_dart_stock_code}",
                "menuIndex": menu_index,
                "marketType": "",
                "startDate": window_start.strftime("%Y-%m-%d"),
                "endDate": window_end.strftime("%Y-%m-%d"),
                "currentPageSize": "3000",
                "pageIndex": "1",
            }
            response = await client.post(f"{self._base_url}{self.MARKET_ALERT_PATH}", data=payload)
            response.raise_for_status()
            for row_html in self._table_rows(response.text):
                fact = self._build_market_alert_fact(
                    row_html=row_html,
                    alert_label=alert_label,
                    window_start=window_start.date(),
                    window_end=window_end.date(),
                )
                if fact is None or self._duplicates_company_disclosure(fact, existing_company_facts):
                    continue
                facts.append(fact)
        return facts

    def _build_market_alert_fact(
        self,
        *,
        row_html: str,
        alert_label: str,
        window_start: date,
        window_end: date,
    ) -> HanallStructuredFact | None:
        cells = [self._clean_text(cell) for cell in self._cell_html(row_html)]
        if len(cells) < 5 or any("조회된 결과값이 없습니다" in cell for cell in cells):
            return None
        if not self._is_hanall_row(" ".join(cells)):
            return None

        if alert_label == "투자주의종목":
            alert_type = cells[2]
            disclosure_date = self._parse_date(cells[3])
            designation_date = self._parse_date(cells[4])
        else:
            alert_type = alert_label
            disclosure_date = self._parse_date(cells[2])
            designation_date = self._parse_date(cells[3])
        if disclosure_date is None or not (window_start <= disclosure_date <= window_end):
            return None

        title = f"{alert_label} | {alert_type}"
        source_id = f"{self.SOURCE_NAME}:{alert_label}:{disclosure_date.isoformat()}:{designation_date.isoformat() if designation_date else '날짜미상'}"
        designation_label = designation_date.isoformat() if designation_date else "날짜미상"
        fact_text = (
            f"{self.COMPANY_NAME}가 KIND/KRX {alert_label} 화면에서 {alert_type}로 확인됨 "
            f"(공시일 {disclosure_date.isoformat()}, 지정일/예고일 {designation_label})"
        )
        return HanallStructuredFact(
            source_name=self.SOURCE_NAME,
            source_type="filing",
            entity=self.ENTITY_NAME,
            category="SEC/DART/KRX",
            title=title,
            fact_text=fact_text,
            source_id=source_id,
            source_url=self._market_alert_url(),
            observed_date=disclosure_date,
            validation_mode="hard",
            timestamp_parse_status="parsed_market_alert_table",
        )

    async def _resolve_external_document_url(self, client: httpx.AsyncClient, accept_no: str) -> str | None:
        viewer_response = await client.get(
            f"{self._base_url}{self.VIEWER_PATH}",
            params={"method": "search", "acptno": accept_no, "docno": "", "viewerhost": "", "viewerport": ""},
        )
        viewer_response.raise_for_status()
        doc_match = self.MAIN_DOC_PATTERN.search(viewer_response.text)
        if doc_match is None:
            return None

        contents_response = await client.get(
            f"{self._base_url}{self.VIEWER_PATH}",
            params={"method": "searchContents", "docNo": doc_match.group("doc_no")},
        )
        contents_response.raise_for_status()
        path_match = self.CONTENT_PATH_PATTERN.search(contents_response.text)
        if path_match is None:
            return None
        return html.unescape(path_match.group("url"))

    async def _fetch_detail_text(self, client: httpx.AsyncClient, source_url: str) -> str:
        try:
            response = await client.get(source_url)
            response.raise_for_status()
        except Exception:  # noqa: BLE001
            return ""
        return self._clean_text(response.text)

    def _company_fact_text(
        self,
        *,
        title: str,
        observed_at: datetime,
        submitter: str,
        detail_text: str,
    ) -> str:
        observed_label = observed_at.strftime("%Y-%m-%d %H:%M KST")
        pieces = [f"{title} KIND/KRX 공시가 {observed_label}에 확인됨"]
        if submitter:
            pieces.append(f"제출인: {submitter}")
        detail_pieces = self._extract_detail_pieces(detail_text)
        if detail_pieces:
            pieces.extend(detail_pieces)
        return ", ".join(pieces)

    def _extract_detail_pieces(self, detail_text: str) -> list[str]:
        if not detail_text:
            return []
        pieces: list[str] = []
        if match := self.KOREAN_DATE_PATTERN.search(detail_text):
            if "지정예고일" in detail_text or "지정일" in detail_text:
                pieces.append(f"지정/예고일: {match.group(0)}")
        if match := self.PRICE_MOVE_REASON_PATTERN.search(detail_text):
            pieces.append(f"사유: {match.group(0)}")
        return pieces[:2]

    @classmethod
    def _table_rows(cls, raw_html: str) -> list[str]:
        return [match.group("body") for match in cls.ROW_PATTERN.finditer(raw_html)]

    @classmethod
    def _cell_html(cls, row_html: str) -> list[str]:
        return [match.group("body") for match in cls.CELL_PATTERN.finditer(row_html)]

    @classmethod
    def _extract_title(cls, cell_html: str) -> str:
        title_match = cls.TITLE_ATTR_PATTERN.search(cell_html)
        if title_match is not None:
            return cls._clean_text(title_match.group("title"))
        return cls._clean_text(cell_html)

    @classmethod
    def _extract_accept_no(cls, row_html: str) -> str | None:
        match = cls.VIEWER_CALL_PATTERN.search(row_html)
        if match is None:
            return None
        return match.group("acpt_no")

    @classmethod
    def _clean_text(cls, raw_text: str) -> str:
        unescaped = html.unescape(raw_text).replace("\xa0", " ")
        without_comments = re.sub(r"<!--.*?-->", " ", unescaped, flags=re.DOTALL)
        without_tags = re.sub(r"<br\b[^>]*>", " ", without_comments, flags=re.IGNORECASE)
        without_tags = re.sub(r"<[^>]+>", " ", without_tags)
        without_lines = without_tags.replace("|", " ")
        return re.sub(r"\s+", " ", without_lines).strip()

    def _is_hanall_row(self, text: str) -> bool:
        normalized = text.replace(" ", "")
        stock_code = self._settings.hanall_dart_stock_code
        return self.COMPANY_NAME in normalized or stock_code in normalized or stock_code[:-1] in normalized

    def _parse_observed_datetime(self, raw_value: str) -> datetime | None:
        try:
            return datetime.strptime(raw_value, "%Y-%m-%d %H:%M").replace(tzinfo=self._timezone)
        except ValueError:
            return None

    @staticmethod
    def _parse_date(raw_value: str) -> date | None:
        try:
            return datetime.strptime(raw_value, "%Y-%m-%d").date()
        except ValueError:
            return None

    def _duplicates_company_disclosure(
        self,
        market_alert_fact: HanallStructuredFact,
        company_facts: list[HanallStructuredFact],
    ) -> bool:
        market_text = self._normalize_for_dedupe(f"{market_alert_fact.title} {market_alert_fact.fact_text}")
        for company_fact in company_facts:
            company_text = self._normalize_for_dedupe(f"{company_fact.title} {company_fact.fact_text}")
            if "투자경고" in market_text and "투자경고" in company_text and "투자주의" in company_text:
                return True
        return False

    def _dedupe_facts(self, facts: list[HanallStructuredFact]) -> list[HanallStructuredFact]:
        deduped: list[HanallStructuredFact] = []
        seen: set[str] = set()
        for fact in facts:
            key = "|".join(
                [
                    fact.source_name,
                    fact.source_id or "",
                    self._normalize_for_dedupe(fact.title),
                    fact.observed_at.isoformat() if fact.observed_at else "",
                    fact.observed_date.isoformat() if fact.observed_date else "",
                ]
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(fact)
        return deduped

    @staticmethod
    def _normalize_for_dedupe(value: str) -> str:
        return re.sub(r"\s+", "", value).lower()

    def _viewer_url(self, accept_no: str) -> str:
        return f"{self._base_url}{self.VIEWER_PATH}?method=search&acptno={accept_no}&docno=&viewerhost=&viewerport="

    def _company_search_url(self) -> str:
        return f"{self._base_url}{self.COMPANY_DISCLOSURE_PATH}?method=searchDisclosureByCorpMain"

    def _market_alert_url(self) -> str:
        return f"{self._base_url}{self.MARKET_ALERT_PATH}?method=investattentwarnriskyMain"
