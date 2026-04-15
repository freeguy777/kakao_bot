from __future__ import annotations

import asyncio
from datetime import datetime

from app.schemas import HanallApiBundle, HanallSourceStatus


class HanallPrefetchService:
    def __init__(
        self,
        *,
        opendart_service: object | None = None,
        clinicaltrials_service: object | None = None,
        sec_edgar_service: object | None = None,
    ) -> None:
        self._opendart_service = opendart_service
        self._clinicaltrials_service = clinicaltrials_service
        self._sec_edgar_service = sec_edgar_service

    async def collect(self, *, window_start: datetime, window_end: datetime) -> HanallApiBundle:
        tasks = [
            self._collect_from_service(self._opendart_service, window_start=window_start, window_end=window_end),
            self._collect_from_service(self._clinicaltrials_service, window_start=window_start, window_end=window_end),
            self._collect_from_service(self._sec_edgar_service, window_start=window_start, window_end=window_end),
        ]
        results = await asyncio.gather(*tasks)
        bundle = HanallApiBundle()
        for facts, status in results:
            bundle.facts.extend(facts)
            if status is not None:
                bundle.source_statuses.append(status)
        return bundle

    async def _collect_from_service(
        self,
        service: object | None,
        *,
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[list[object], HanallSourceStatus | None]:
        if service is None:
            return [], None
        try:
            return await service.collect_company_updates(window_start=window_start, window_end=window_end)
        except Exception as exc:  # noqa: BLE001
            checked_at = datetime.now(window_end.tzinfo)
            source_name = type(service).__name__
            if source_name == "OpenDartService":
                source_name = "OpenDART"
            elif source_name == "ClinicalTrialsService":
                source_name = "ClinicalTrials.gov API"
            elif source_name == "SecEdgarService":
                source_name = "SEC EDGAR API"
            status = HanallSourceStatus(
                source_name=source_name,
                status="unavailable",
                checked_at=checked_at,
                detail=str(exc),
                hard_requirement=True,
            )
            return [], status
