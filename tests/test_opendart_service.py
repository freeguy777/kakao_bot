from __future__ import annotations

from datetime import datetime
from io import BytesIO
import zipfile

import httpx

from app.services.opendart_service import OpenDartService


def _build_corp_code_archive() -> bytes:
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<result>
  <list>
    <corp_code>00126380</corp_code>
    <corp_name>한올바이오파마</corp_name>
    <stock_code>009420</stock_code>
  </list>
</result>
"""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("CORPCODE.xml", xml_content)
    return buffer.getvalue()


async def test_opendart_collects_hard_fact_with_receipt_timestamp(test_settings, monkeypatch) -> None:
    archive_bytes = _build_corp_code_archive()
    captured_requests: list[tuple[str, dict[str, object]]] = []

    class FakeResponse:
        def __init__(self, *, json_payload=None, text: str = "", content: bytes = b"") -> None:
            self._json_payload = json_payload
            self.text = text
            self.content = content

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._json_payload

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            assert timeout == test_settings.opendart_timeout_seconds

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, params: dict[str, object] | None = None):
            captured_requests.append((url, params or {}))
            if url.endswith("corpCode.xml"):
                return FakeResponse(content=archive_bytes)
            if url.endswith("list.json"):
                return FakeResponse(
                    json_payload={
                        "status": "000",
                        "list": [
                            {
                                "corp_name": "한올바이오파마",
                                "report_nm": "기업설명회(IR)개최(안내공시)",
                                "rcept_no": "20260413800541",
                                "rcept_dt": "20260413",
                            }
                        ],
                    }
                )
            if "dsaf001/main.do" in url:
                return FakeResponse(text="2026.04.13 15:54:57 기업설명회(IR)개최(안내공시)")
            raise AssertionError(f"unexpected URL: {url}")

    test_settings.opendart_api_key = "dart-key"
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    service = OpenDartService(test_settings)
    facts, status = await service.collect_company_updates(
        window_start=datetime(2026, 4, 13, 7, 42, tzinfo=service._timezone),
        window_end=datetime(2026, 4, 14, 7, 42, tzinfo=service._timezone),
    )

    assert status.status == "ok"
    assert len(facts) == 1
    assert facts[0].source_id == "20260413800541"
    assert facts[0].title == "기업설명회(IR)개최(안내공시)"
    assert facts[0].validation_mode == "hard"
    assert facts[0].observed_at == datetime(2026, 4, 13, 15, 54, 57, tzinfo=service._timezone)
    assert facts[0].timestamp_parse_status == "parsed_main_page"
    assert facts[0].source_url == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260413800541"
    assert any(url.endswith("list.json") and params["corp_code"] == "00126380" for url, params in captured_requests)


async def test_opendart_collects_hard_fact_from_viewer_when_main_page_lacks_timestamp(test_settings, monkeypatch) -> None:
    archive_bytes = _build_corp_code_archive()

    class FakeResponse:
        def __init__(self, *, json_payload=None, text: str = "", content: bytes = b"") -> None:
            self._json_payload = json_payload
            self.text = text
            self.content = content

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._json_payload

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            assert timeout == test_settings.opendart_timeout_seconds

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, params: dict[str, object] | None = None):
            if url.endswith("corpCode.xml"):
                return FakeResponse(content=archive_bytes)
            if url.endswith("list.json"):
                return FakeResponse(
                    json_payload={
                        "status": "000",
                        "list": [
                            {
                                "corp_name": "한올바이오파마",
                                "report_nm": "기업설명회(IR)개최(안내공시)",
                                "rcept_no": "20260413800541",
                                "rcept_dt": "20260413",
                            }
                        ],
                    }
                )
            if "dsaf001/main.do" in url:
                return FakeResponse(text='viewDoc("20260413800541", "11318074", "0", "0", "0", "HTML", "");')
            if "report/viewer.do" in url:
                return FakeResponse(text="접수일시 2026-04-13 15:54:57")
            raise AssertionError(f"unexpected URL: {url}")

    test_settings.opendart_api_key = "dart-key"
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    service = OpenDartService(test_settings)
    facts, status = await service.collect_company_updates(
        window_start=datetime(2026, 4, 13, 7, 42, tzinfo=service._timezone),
        window_end=datetime(2026, 4, 14, 7, 42, tzinfo=service._timezone),
    )

    assert status.status == "ok"
    assert len(facts) == 1
    assert facts[0].validation_mode == "hard"
    assert facts[0].observed_at == datetime(2026, 4, 13, 15, 54, 57, tzinfo=service._timezone)
    assert facts[0].timestamp_parse_status == "parsed_viewer_page"


async def test_opendart_falls_back_to_soft_when_timestamp_parse_fails(test_settings, monkeypatch) -> None:
    archive_bytes = _build_corp_code_archive()

    class FakeResponse:
        def __init__(self, *, json_payload=None, text: str = "", content: bytes = b"") -> None:
            self._json_payload = json_payload
            self.text = text
            self.content = content

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._json_payload

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            assert timeout == test_settings.opendart_timeout_seconds

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, params: dict[str, object] | None = None):
            if url.endswith("corpCode.xml"):
                return FakeResponse(content=archive_bytes)
            if url.endswith("list.json"):
                return FakeResponse(
                    json_payload={
                        "status": "000",
                        "list": [
                            {
                                "corp_name": "한올바이오파마",
                                "report_nm": "기업설명회(IR)개최(안내공시)",
                                "rcept_no": "20260413800541",
                                "rcept_dt": "20260413",
                            }
                        ],
                    }
                )
            if "dsaf001/main.do" in url:
                return FakeResponse(text='viewDoc("20260413800541", "11318074", "0", "0", "0", "HTML", "");')
            if "report/viewer.do" in url:
                return FakeResponse(text="접수일시 없음")
            raise AssertionError(f"unexpected URL: {url}")

    test_settings.opendart_api_key = "dart-key"
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    service = OpenDartService(test_settings)
    facts, status = await service.collect_company_updates(
        window_start=datetime(2026, 4, 13, 7, 42, tzinfo=service._timezone),
        window_end=datetime(2026, 4, 14, 7, 42, tzinfo=service._timezone),
    )

    assert status.status == "ok"
    assert len(facts) == 1
    assert facts[0].validation_mode == "soft"
    assert facts[0].observed_at is None
    assert facts[0].observed_date.isoformat() == "2026-04-13"
    assert facts[0].timestamp_parse_status == "fallback_parse_failed"


async def test_opendart_falls_back_to_soft_when_timestamp_fetch_fails(test_settings, monkeypatch) -> None:
    archive_bytes = _build_corp_code_archive()

    class FakeResponse:
        def __init__(self, *, json_payload=None, text: str = "", content: bytes = b"") -> None:
            self._json_payload = json_payload
            self.text = text
            self.content = content

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return self._json_payload

    class FakeAsyncClient:
        def __init__(self, *, timeout: int) -> None:
            assert timeout == test_settings.opendart_timeout_seconds

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, params: dict[str, object] | None = None):
            if url.endswith("corpCode.xml"):
                return FakeResponse(content=archive_bytes)
            if url.endswith("list.json"):
                return FakeResponse(
                    json_payload={
                        "status": "000",
                        "list": [
                            {
                                "corp_name": "한올바이오파마",
                                "report_nm": "기업설명회(IR)개최(안내공시)",
                                "rcept_no": "20260413800541",
                                "rcept_dt": "20260413",
                            }
                        ],
                    }
                )
            if "dsaf001/main.do" in url:
                raise httpx.ReadTimeout("timeout", request=httpx.Request("GET", url))
            raise AssertionError(f"unexpected URL: {url}")

    test_settings.opendart_api_key = "dart-key"
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    service = OpenDartService(test_settings)
    facts, status = await service.collect_company_updates(
        window_start=datetime(2026, 4, 13, 7, 42, tzinfo=service._timezone),
        window_end=datetime(2026, 4, 14, 7, 42, tzinfo=service._timezone),
    )

    assert status.status == "ok"
    assert len(facts) == 1
    assert facts[0].validation_mode == "soft"
    assert facts[0].observed_at is None
    assert facts[0].observed_date.isoformat() == "2026-04-13"
    assert facts[0].timestamp_parse_status == "fallback_fetch_failed"
