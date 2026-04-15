from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.schemas import HanallSourceStatus, HanallStructuredFact


class SecEdgarService:
    SOURCE_NAME = "SEC EDGAR API"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._timezone = ZoneInfo(settings.app_timezone)

    async def collect_company_updates(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[list[HanallStructuredFact], HanallSourceStatus]:
        checked_at = datetime.now(self._timezone)
        if not self._settings.sec_user_agent:
            return [], HanallSourceStatus(
                source_name=self.SOURCE_NAME,
                status="skipped",
                checked_at=checked_at,
                detail="SEC_USER_AGENT is not configured",
                hard_requirement=True,
            )

        cik = self._normalize_cik(self._settings.immunovant_sec_cik)
        headers = {
            "User-Agent": self._settings.sec_user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        url = f"{self._settings.sec_submissions_base_url.rstrip('/')}/CIK{cik}.json"
        async with httpx.AsyncClient(timeout=self._settings.sec_timeout_seconds, headers=headers) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()

        facts = self._build_facts(payload=payload, cik=cik, window_start=window_start, window_end=window_end)
        detail = "SEC filing 없음" if not facts else f"SEC filing {len(facts)}건 감지"
        return facts, HanallSourceStatus(
            source_name=self.SOURCE_NAME,
            status="ok",
            checked_at=checked_at,
            detail=detail,
            hard_requirement=True,
        )

    def _build_facts(
        self,
        *,
        payload: dict[str, object],
        cik: str,
        window_start: datetime,
        window_end: datetime,
    ) -> list[HanallStructuredFact]:
        recent = ((payload.get("filings") or {}).get("recent") or {})
        accessions = recent.get("accessionNumber") or []
        facts: list[HanallStructuredFact] = []
        seen_accessions: set[str] = set()
        for index, accession_value in enumerate(accessions):
            accession_number = str(accession_value or "").strip()
            if not accession_number or accession_number in seen_accessions:
                continue
            seen_accessions.add(accession_number)
            fact = self._build_fact(
                cik=cik,
                accession_number=accession_number,
                form_type=self._list_value(recent.get("form"), index),
                filing_date_raw=self._list_value(recent.get("filingDate"), index),
                acceptance_raw=self._list_value(recent.get("acceptanceDateTime"), index),
                primary_document=self._list_value(recent.get("primaryDocument"), index),
                description=self._list_value(recent.get("primaryDocDescription"), index),
                window_start=window_start,
                window_end=window_end,
            )
            if fact is not None:
                facts.append(fact)
        return facts

    def _build_fact(
        self,
        *,
        cik: str,
        accession_number: str,
        form_type: str,
        filing_date_raw: str,
        acceptance_raw: str,
        primary_document: str,
        description: str,
        window_start: datetime,
        window_end: datetime,
    ) -> HanallStructuredFact | None:
        acceptance_timestamp = self._parse_acceptance_timestamp(acceptance_raw)
        filing_date = self._parse_iso_date(filing_date_raw)
        if acceptance_timestamp is not None:
            if not (window_start <= acceptance_timestamp <= window_end):
                return None
            observed_at = acceptance_timestamp
            observed_date = None
            validation_mode = "hard"
            observed_label = observed_at.strftime("%Y-%m-%d %H:%M KST")
        else:
            if filing_date is None or not (window_start.date() <= filing_date <= window_end.date()):
                return None
            observed_at = None
            observed_date = filing_date
            validation_mode = "soft"
            observed_label = observed_date.isoformat()

        normalized_form = form_type or "Form 미상"
        title = normalized_form if not description else f"{normalized_form} | {description}"
        source_url = self._build_source_url(cik=cik, accession_number=accession_number, primary_document=primary_document)
        fact_text = f"Immunovant {normalized_form} filing이 {observed_label} 기준 SEC EDGAR submissions에서 확인됨"
        if description:
            fact_text = f"{fact_text} ({description})"

        return HanallStructuredFact(
            source_name=self.SOURCE_NAME,
            source_type="filing",
            entity="Immunovant",
            category="SEC/DART/KRX",
            title=title,
            fact_text=fact_text,
            source_id=accession_number,
            source_url=source_url,
            observed_at=observed_at,
            observed_date=observed_date,
            validation_mode=validation_mode,
        )

    @staticmethod
    def _normalize_cik(raw_value: str) -> str:
        digits = "".join(character for character in raw_value if character.isdigit())
        if not digits:
            raise ValueError("immunovant_sec_cik must contain digits")
        return digits.zfill(10)

    def _parse_acceptance_timestamp(self, raw_value: str) -> datetime | None:
        if not raw_value:
            return None
        normalized = raw_value.replace("Z", "+00:00")
        timestamp = datetime.fromisoformat(normalized)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(self._timezone)

    @staticmethod
    def _parse_iso_date(raw_value: str) -> date | None:
        if not raw_value:
            return None
        return date.fromisoformat(raw_value)

    @staticmethod
    def _list_value(raw_values: object, index: int) -> str:
        if not isinstance(raw_values, list) or index >= len(raw_values):
            return ""
        return str(raw_values[index] or "").strip()

    @staticmethod
    def _build_source_url(*, cik: str, accession_number: str, primary_document: str) -> str:
        cik_path = str(int(cik))
        accession_path = accession_number.replace("-", "")
        document_path = primary_document or "index.html"
        return f"https://www.sec.gov/Archives/edgar/data/{cik_path}/{accession_path}/{document_path}"
