from __future__ import annotations

from datetime import datetime

import httpx

from app.schemas import HanallApiBundle
from app.services.sec_edgar_service import SecEdgarService


async def test_sec_edgar_skips_without_user_agent(test_settings) -> None:
    test_settings.sec_user_agent = None
    service = SecEdgarService(test_settings)

    facts, status = await service.collect_company_updates(
        window_start=datetime(2026, 4, 13, 7, 42, tzinfo=service._timezone),
        window_end=datetime(2026, 4, 14, 7, 42, tzinfo=service._timezone),
    )

    assert facts == []
    assert status.status == "skipped"
    assert status.detail == "SEC_USER_AGENT is not configured"


async def test_sec_edgar_collects_recent_hard_fact_and_marks_direct_validation_source(test_settings, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "filings": {
                    "recent": {
                        "accessionNumber": ["0001764013-26-000123"],
                        "filingDate": ["2026-04-13"],
                        "acceptanceDateTime": ["2026-04-13T15:54:57.000Z"],
                        "form": ["8-K"],
                        "primaryDocument": ["imvt-8k.htm"],
                        "primaryDocDescription": ["Current report"],
                    }
                }
            }

    class FakeAsyncClient:
        def __init__(self, *, timeout: int, headers: dict[str, str]) -> None:
            captured["timeout"] = timeout
            captured["headers"] = headers

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str):
            captured["url"] = url
            return FakeResponse()

    test_settings.sec_user_agent = "Kakao Bot ops@example.com"
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    service = SecEdgarService(test_settings)
    facts, status = await service.collect_company_updates(
        window_start=datetime(2026, 4, 13, 7, 42, tzinfo=service._timezone),
        window_end=datetime(2026, 4, 14, 7, 42, tzinfo=service._timezone),
    )

    assert status.status == "ok"
    assert len(facts) == 1
    assert facts[0].source_name == "SEC EDGAR API"
    assert facts[0].source_id == "0001764013-26-000123"
    assert facts[0].title == "8-K | Current report"
    assert facts[0].validation_mode == "hard"
    assert facts[0].observed_at == datetime(2026, 4, 14, 0, 54, 57, tzinfo=service._timezone)
    assert facts[0].source_url == "https://www.sec.gov/Archives/edgar/data/1764013/000176401326000123/imvt-8k.htm"
    assert captured["timeout"] == test_settings.sec_timeout_seconds
    assert captured["headers"] == {
        "User-Agent": "Kakao Bot ops@example.com",
        "Accept-Encoding": "gzip, deflate",
    }
    assert str(captured["url"]).endswith("/CIK0001764013.json")

    bundle = HanallApiBundle(facts=facts)
    assert bundle.direct_validation_facts() == facts
