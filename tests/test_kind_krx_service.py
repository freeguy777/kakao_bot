from __future__ import annotations

from datetime import datetime

import httpx

from app.services.kind_krx_service import KindKrxService


COMPANY_DISCLOSURE_HTML = """
<section>
  <table>
    <tbody>
      <tr>
        <td>16</td>
        <td class="txc">2026-05-27 20:00</td>
        <td><a title="한올바이오파마">한올바이오파마</a></td>
        <td><a href="#viewer" onclick="openDisclsViewer('20260527000934','')" title="[투자주의]투자경고종목 지정예고">[투자주의]투자경고종목 지정예고</a></td>
        <td>시장감시위원회</td>
      </tr>
    </tbody>
  </table>
</section>
"""

MARKET_ALERT_HTML = """
<section>
  <table>
    <tbody>
      <tr class="first">
        <td>1</td>
        <td title="한올바이오파마"><a title="한올바이오파마">한올바이오파마</a></td>
        <td>투자경고 지정예고</td>
        <td class="txc">2026-05-27</td>
        <td class="txc">2026-05-28</td>
      </tr>
    </tbody>
  </table>
</section>
"""

EMPTY_TABLE_HTML = """
<section>
  <table>
    <tbody>
      <tr class="first">
        <td class="first null" colspan="5">조회된 결과값이 없습니다.</td>
      </tr>
    </tbody>
  </table>
</section>
"""

VIEWER_HTML = """
<select id="mainDoc">
  <option value="">본문선택</option>
  <option value="20260527002162|Y" selected="selected">[투자주의]투자경고종목 지정예고 (2026.05.27)</option>
</select>
"""

CONTENTS_HTML = """
<script>
parent.setPath('', 'https://kind.krx.co.kr/external/2026/05/27/000934/20260527002162/68807.htm', '/external/2026/05/27/000934/20260527002162/68807', '03', '15');
</script>
"""

DETAIL_HTML = """
<html><body>
  <span>[투자주의]투자경고종목 지정예고</span>
  <span>2. 지정예고일 | 2026년 05월 28일</span>
  <span>3. 지정예고사유 | - 2026년 05월 27일의 종가가 5일 전일의 종가보다 60% 이상 상승</span>
</body></html>
"""


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class FakeAsyncClient:
    def __init__(self, *, timeout: int) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url: str, data: dict[str, str]):
        if url.endswith("/disclosure/searchdisclosurebycorp.do"):
            return FakeResponse(COMPANY_DISCLOSURE_HTML)
        if url.endswith("/investwarn/investattentwarnrisky.do"):
            return FakeResponse(EMPTY_TABLE_HTML)
        raise AssertionError(f"unexpected POST {url} {data}")

    async def get(self, url: str, params: dict[str, str] | None = None):
        params = params or {}
        if url.endswith("/common/disclsviewer.do") and params.get("method") == "search":
            return FakeResponse(VIEWER_HTML)
        if url.endswith("/common/disclsviewer.do") and params.get("method") == "searchContents":
            return FakeResponse(CONTENTS_HTML)
        if url == "https://kind.krx.co.kr/external/2026/05/27/000934/20260527002162/68807.htm":
            return FakeResponse(DETAIL_HTML)
        raise AssertionError(f"unexpected GET {url} {params}")


async def test_kind_krx_collects_company_disclosure_with_external_detail(test_settings, monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    service = KindKrxService(test_settings)
    facts, status = await service.collect_company_updates(
        window_start=datetime(2026, 5, 27, 7, 42, tzinfo=service._timezone),
        window_end=datetime(2026, 5, 28, 7, 42, tzinfo=service._timezone),
    )

    assert status.status == "ok"
    assert len(facts) == 1
    assert facts[0].source_name == "KIND/KRX"
    assert facts[0].source_id == "20260527000934"
    assert facts[0].title == "[투자주의]투자경고종목 지정예고"
    assert facts[0].source_url == "https://kind.krx.co.kr/external/2026/05/27/000934/20260527002162/68807.htm"
    assert "지정/예고일: 2026년 05월 28일" in facts[0].fact_text
    assert "5일 전일의 종가보다 60% 이상 상승" in facts[0].fact_text


async def test_kind_krx_collects_market_alert_when_company_disclosure_is_absent(test_settings, monkeypatch) -> None:
    class MarketOnlyClient(FakeAsyncClient):
        async def post(self, url: str, data: dict[str, str]):
            if url.endswith("/disclosure/searchdisclosurebycorp.do"):
                return FakeResponse(EMPTY_TABLE_HTML)
            if url.endswith("/investwarn/investattentwarnrisky.do") and data["menuIndex"] == "1":
                return FakeResponse(MARKET_ALERT_HTML)
            if url.endswith("/investwarn/investattentwarnrisky.do"):
                return FakeResponse(EMPTY_TABLE_HTML)
            raise AssertionError(f"unexpected POST {url} {data}")

    monkeypatch.setattr(httpx, "AsyncClient", MarketOnlyClient)

    service = KindKrxService(test_settings)
    facts, status = await service.collect_company_updates(
        window_start=datetime(2026, 5, 27, 7, 42, tzinfo=service._timezone),
        window_end=datetime(2026, 5, 28, 7, 42, tzinfo=service._timezone),
    )

    assert status.status == "ok"
    assert len(facts) == 1
    assert facts[0].title == "투자주의종목 | 투자경고 지정예고"
    assert facts[0].observed_date.isoformat() == "2026-05-27"
    assert "지정일/예고일 2026-05-28" in facts[0].fact_text


async def test_kind_krx_ignores_window_outside_rows(test_settings, monkeypatch) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    service = KindKrxService(test_settings)
    facts, status = await service.collect_company_updates(
        window_start=datetime(2026, 5, 28, 7, 42, tzinfo=service._timezone),
        window_end=datetime(2026, 5, 29, 7, 42, tzinfo=service._timezone),
    )

    assert status.status == "ok"
    assert facts == []
