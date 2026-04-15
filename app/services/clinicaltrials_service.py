from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.schemas import HanallSourceStatus, HanallStructuredFact


class ClinicalTrialsService:
    SEARCH_QUERY = (
        'batoclimab OR imeroprubart OR "IMVT-1401" OR "IMVT-1402" OR HL161 OR HL161ANS '
        'OR tanfanercept OR HL036 OR Immunovant OR "HanAll Biopharma"'
    )
    ENTITY_TOKENS = ("immunovant", "hanall", "batoclimab", "imeroprubart", "imvt-1401", "imvt-1402", "hl161", "hl161ans", "hl036", "tanfanercept")

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
        params = {
            "query.term": self.SEARCH_QUERY,
            "pageSize": 100,
            "format": "json",
        }
        async with httpx.AsyncClient(timeout=self._settings.clinicaltrials_timeout_seconds) as client:
            response = await client.get(f"{self._settings.clinicaltrials_api_base_url.rstrip('/')}/studies", params=params)
            response.raise_for_status()
            payload = response.json()

        studies = payload.get("studies") or []
        facts: list[HanallStructuredFact] = []
        seen_nct_ids: set[str] = set()
        for study in studies:
            fact = self._build_fact(study=study, window_start=window_start.date(), window_end=window_end.date())
            if fact is None:
                continue
            if fact.source_id in seen_nct_ids:
                continue
            seen_nct_ids.add(fact.source_id or "")
            facts.append(fact)

        detail = "임상등록 신규 없음" if not facts else f"임상등록 업데이트 {len(facts)}건 감지"
        return facts, HanallSourceStatus(
            source_name="ClinicalTrials.gov API",
            status="ok",
            checked_at=checked_at,
            detail=detail,
            hard_requirement=True,
        )

    def _build_fact(
        self,
        *,
        study: dict[str, object],
        window_start: date,
        window_end: date,
    ) -> HanallStructuredFact | None:
        protocol = study.get("protocolSection") or {}
        identification = protocol.get("identificationModule") or {}
        status_module = protocol.get("statusModule") or {}
        sponsor_module = protocol.get("sponsorCollaboratorsModule") or {}

        if not self._is_relevant_study(identification=identification, sponsor_module=sponsor_module, study=study):
            return None

        nct_id = (identification.get("nctId") or "").strip()
        brief_title = (identification.get("briefTitle") or "").strip()
        if not nct_id or not brief_title:
            return None

        last_update_raw = ((status_module.get("lastUpdatePostDateStruct") or {}).get("date") or "").strip()
        last_update_date = self._parse_iso_date(last_update_raw)
        if last_update_date is None or not (window_start <= last_update_date <= window_end):
            return None

        overall_status = (status_module.get("overallStatus") or "").strip() or "상태 미상"
        lead_sponsor = ((sponsor_module.get("leadSponsor") or {}).get("name") or "").strip()
        source_url = f"https://clinicaltrials.gov/study/{nct_id}"
        fact_text = f"{brief_title} 임상 레코드가 {last_update_date.isoformat()}에 ClinicalTrials.gov에 업데이트 게시됨 (status: {overall_status})"
        if lead_sponsor:
            fact_text = f"{fact_text}, sponsor: {lead_sponsor}"

        return HanallStructuredFact(
            source_name="ClinicalTrials.gov API",
            source_type="clinical_registry",
            entity="HanAll/Immunovant",
            category="임상",
            title=brief_title,
            fact_text=fact_text,
            source_id=nct_id,
            source_url=source_url,
            observed_date=last_update_date,
            validation_mode="soft",
        )

    def _is_relevant_study(
        self,
        *,
        identification: dict[str, object],
        sponsor_module: dict[str, object],
        study: dict[str, object],
    ) -> bool:
        title_candidates = [
            str(identification.get("briefTitle") or ""),
            str(identification.get("officialTitle") or ""),
            str(((sponsor_module.get("leadSponsor") or {}).get("name") or "")),
            str(study),
        ]
        joined = " ".join(title_candidates).lower()
        return any(token in joined for token in self.ENTITY_TOKENS)

    @staticmethod
    def _parse_iso_date(raw_value: str) -> date | None:
        if not raw_value:
            return None
        return date.fromisoformat(raw_value)
