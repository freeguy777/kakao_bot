from __future__ import annotations

from datetime import datetime

import httpx

from app.services.clinicaltrials_service import ClinicalTrialsService


async def test_clinicaltrials_collects_recent_company_update(test_settings, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "studies": [
                    {
                        "protocolSection": {
                            "identificationModule": {
                                "nctId": "NCT01234567",
                                "briefTitle": "Batoclimab in Generalized Myasthenia Gravis",
                            },
                            "statusModule": {
                                "overallStatus": "RECRUITING",
                                "lastUpdatePostDateStruct": {"date": "2026-04-14"},
                            },
                            "sponsorCollaboratorsModule": {
                                "leadSponsor": {"name": "Immunovant Sciences GmbH"}
                            },
                        }
                    }
                ]
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, params: dict[str, object]):
            captured["url"] = url
            captured["params"] = params
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    service = ClinicalTrialsService(test_settings)
    facts, status = await service.collect_company_updates(
        window_start=datetime(2026, 4, 13, 7, 42, tzinfo=service._timezone),
        window_end=datetime(2026, 4, 14, 7, 42, tzinfo=service._timezone),
    )

    assert status.status == "ok"
    assert len(facts) == 1
    assert facts[0].source_id == "NCT01234567"
    assert facts[0].validation_mode == "soft"
    assert facts[0].title == "Batoclimab in Generalized Myasthenia Gravis"
    assert "RECRUITING" in facts[0].fact_text
    assert captured["url"].endswith("/studies")
    assert "batoclimab" in captured["params"]["query.term"]
