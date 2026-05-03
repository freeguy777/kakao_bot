from __future__ import annotations

from datetime import date, datetime, timezone

import httpx

from app.schemas import OptionsPCRDailySummary
from app.services.options_sentiment_service import OptionsSentimentService

DATE_US = date(2026, 4, 10)
DATE_KST = date(2026, 4, 11)
RETRIEVED_AT_UTC = datetime(2026, 4, 10, 22, 42, tzinfo=timezone.utc)


class FakeOptionsSummaryRepository:
    def __init__(self, previous_summary: OptionsPCRDailySummary | None = None) -> None:
        self.previous_summary = previous_summary

    def get_previous_summary(self, symbol: str, *, before_date_us: date, source_environment: str) -> OptionsPCRDailySummary | None:
        return self.previous_summary

    def save_daily_summary(self, summary: OptionsPCRDailySummary) -> OptionsPCRDailySummary:
        return summary


def _chain_payload(entries: list[dict[str, object]]) -> dict[str, object]:
    return {"options": {"option": entries}}


def _quote_payload(*, close: float = 18.5, prevclose: float = 18.0) -> dict[str, object]:
    return {"quotes": {"quote": {"symbol": "IMVT", "close": close, "prevclose": prevclose}}}


def _build_transport(
    *,
    expirations: list[str],
    chains: dict[str, tuple[int, dict[str, object]] | dict[str, object]],
    quote_status: int = 200,
    quote_payload: dict[str, object] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/markets/options/expirations"):
            return httpx.Response(200, json={"expirations": {"date": expirations}})
        if request.url.path.endswith("/markets/options/chains"):
            expiration = request.url.params["expiration"]
            payload = chains[expiration]
            if isinstance(payload, tuple):
                status_code, body = payload
            else:
                status_code, body = 200, payload
            return httpx.Response(status_code, json=body)
        if request.url.path.endswith("/markets/quotes"):
            return httpx.Response(quote_status, json=quote_payload or _quote_payload())
        raise AssertionError(f"unexpected path: {request.url.path}")

    return httpx.MockTransport(handler)


def _build_service(test_settings, transport: httpx.MockTransport | None = None, *, tradier_env: str = "live", previous_summary=None):
    settings = test_settings.model_copy(
        update={
            "tradier_api": "tradier-key",
            "tradier_env": tradier_env,
        }
    )
    service = OptionsSentimentService(settings=settings, summary_repository=FakeOptionsSummaryRepository(previous_summary))
    if transport is not None:
        service._create_client = lambda: httpx.AsyncClient(transport=transport, base_url=service._base_url())  # type: ignore[method-assign]
    return service


async def _collect_summary(service: OptionsSentimentService) -> OptionsPCRDailySummary:
    return await service._collect_summary(
        artifact_date_kst=DATE_KST,
        date_us=DATE_US,
        retrieved_at_utc=RETRIEVED_AT_UTC,
        symbol="IMVT",
    )


def _build_previous_summary() -> OptionsPCRDailySummary:
    return OptionsPCRDailySummary(
        date_us=date(2026, 4, 9),
        date_kst=date(2026, 4, 10),
        symbol="IMVT",
        close=18.0,
        change_1d_pct=1.0,
        pcr_oi_total=0.75,
        pcr_vol_total=0.7,
        put_oi_total=1500,
        call_oi_total=2000,
        put_vol_total=700,
        call_vol_total=1000,
        total_option_volume=1700,
        total_option_oi=3500,
        short_dte_pcr_oi=0.8,
        short_dte_pcr_vol=0.75,
        data_quality_flag="OK",
        should_publish_public=True,
        no_publish_reason=None,
        source="tradier",
        source_environment="live",
        retrieved_at_utc=datetime(2026, 4, 9, 22, 42, tzinfo=timezone.utc),
        oi_effective_date=None,
        by_expiry_json=[],
        raw_response_json={},
    )


async def test_collect_daily_summary_returns_disabled_when_api_missing(test_settings) -> None:
    settings = test_settings.model_copy(update={"tradier_api": None, "tradier_env": "live"})
    service = OptionsSentimentService(settings=settings)

    result = await service.collect_daily_summary(artifact_date_kst=DATE_KST)

    assert result.collect_status == "disabled"
    assert result.summary is None
    assert result.snapshot.collect_status == "disabled"
    assert result.snapshot.reason == "TRADIER_API is not configured"


async def test_collect_summary_calculates_total_and_short_dte_pcrs(test_settings) -> None:
    transport = _build_transport(
        expirations=["2026-04-24", "2026-05-09", "2026-08-21"],
        chains={
            "2026-04-24": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 1200, "volume": 400},
                    {"option_type": "call", "open_interest": 1000, "volume": 500},
                ]
            ),
            "2026-05-09": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 800, "volume": 600},
                    {"option_type": "call", "open_interest": 1000, "volume": 700},
                ]
            ),
            "2026-08-21": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 500, "volume": 200},
                    {"option_type": "call", "open_interest": 1000, "volume": 300},
                ]
            ),
        },
    )
    service = _build_service(test_settings, transport, previous_summary=_build_previous_summary())

    summary = await _collect_summary(service)

    assert summary.pcr_oi_total == 0.8333
    assert summary.pcr_vol_total == 0.8
    assert summary.short_dte_pcr_oi == 1.0
    assert summary.short_dte_pcr_vol == 0.8333
    assert summary.should_publish_public is True
    assert summary.data_quality_flag == OptionsSentimentService.QUALITY_OK
    assert summary.raw_response_json["previous_pcr_oi_total"] == 0.75
    assert summary.raw_response_json["previous_pcr_oi_total_delta"] == 0.0833


async def test_collect_summary_sets_short_dte_fields_to_null_and_omits_message_line(test_settings) -> None:
    transport = _build_transport(
        expirations=["2026-04-13", "2026-06-09"],
        chains={
            "2026-04-13": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 1500, "volume": 600},
                    {"option_type": "call", "open_interest": 1500, "volume": 600},
                ]
            ),
            "2026-06-09": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 1500, "volume": 600},
                    {"option_type": "call", "open_interest": 1500, "volume": 600},
                ]
            ),
        },
    )
    service = _build_service(test_settings, transport)

    summary = await _collect_summary(service)
    message = service.render_public_message(summary)

    assert summary.short_dte_pcr_oi is None
    assert summary.short_dte_pcr_vol is None
    assert "단기 만기권 OI PCR(7~45일 남은 만기 합산)" not in message


async def test_low_volume_message_removes_interpretation_and_keeps_publish_enabled(test_settings) -> None:
    transport = _build_transport(
        expirations=["2026-04-24", "2026-05-09"],
        chains={
            "2026-04-24": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 2000, "volume": 200},
                    {"option_type": "call", "open_interest": 2000, "volume": 200},
                ]
            ),
            "2026-05-09": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 1500, "volume": 150},
                    {"option_type": "call", "open_interest": 1500, "volume": 150},
                ]
            ),
        },
    )
    service = _build_service(test_settings, transport)

    summary = await _collect_summary(service)
    message = service.render_public_message(summary)

    assert summary.data_quality_flag == OptionsSentimentService.QUALITY_LOW_VOLUME
    assert summary.should_publish_public is True
    assert "[해석]" not in message
    assert "오늘 옵션 거래량이 적어 Volume PCR의 방향성 해석은 보류합니다." in message


async def test_low_oi_takes_priority_over_low_volume_and_blocks_public_publish(test_settings) -> None:
    transport = _build_transport(
        expirations=["2026-04-24", "2026-05-09"],
        chains={
            "2026-04-24": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 125, "volume": 5},
                    {"option_type": "call", "open_interest": 125, "volume": 5},
                ]
            ),
            "2026-05-09": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 125, "volume": 5},
                    {"option_type": "call", "open_interest": 125, "volume": 5},
                ]
            ),
        },
    )
    service = _build_service(test_settings, transport)

    summary = await _collect_summary(service)

    assert summary.data_quality_flag == OptionsSentimentService.QUALITY_LOW_OI
    assert summary.should_publish_public is False
    assert summary.no_publish_reason == OptionsSentimentService.NO_PUBLISH_LOW_OI


def test_admin_warning_for_included_public_message_uses_caution_wording(test_settings) -> None:
    service = _build_service(test_settings)
    summary = _build_previous_summary().model_copy(
        update={
            "data_quality_flag": OptionsSentimentService.QUALITY_SINGLE_EXPIRY_DISTORTION,
            "should_publish_public": False,
            "no_publish_reason": OptionsSentimentService.NO_PUBLISH_SINGLE_EXPIRY_DISTORTION,
        }
    )

    warning = service.render_admin_warning_from_summary(
        summary,
        room_name="테스트하는방방방",
        public_message_included=True,
    )

    assert warning is not None
    assert "공개방 발행을 차단했습니다" not in warning
    assert "수치 해석에 주의가 필요합니다." in warning


async def test_partial_chain_blocks_public_publish(test_settings) -> None:
    transport = _build_transport(
        expirations=["2026-04-24", "2026-05-09"],
        chains={
            "2026-04-24": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 3000, "volume": 800},
                    {"option_type": "call", "open_interest": 3000, "volume": 800},
                ]
            ),
            "2026-05-09": (503, {"fault": {"message": "temporary"}}),
        },
    )
    service = _build_service(test_settings, transport)

    summary = await _collect_summary(service)

    assert summary.data_quality_flag == OptionsSentimentService.QUALITY_PARTIAL_CHAIN
    assert summary.should_publish_public is False
    assert summary.no_publish_reason == OptionsSentimentService.NO_PUBLISH_PARTIAL_CHAIN


async def test_single_expiry_distortion_blocks_public_publish(test_settings) -> None:
    transport = _build_transport(
        expirations=["2026-04-24", "2026-05-09"],
        chains={
            "2026-04-24": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 3500, "volume": 1200},
                    {"option_type": "call", "open_interest": 3500, "volume": 1200},
                ]
            ),
            "2026-05-09": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 500, "volume": 400},
                    {"option_type": "call", "open_interest": 500, "volume": 400},
                ]
            ),
        },
    )
    service = _build_service(test_settings, transport)

    summary = await _collect_summary(service)

    assert summary.data_quality_flag == OptionsSentimentService.QUALITY_SINGLE_EXPIRY_DISTORTION
    assert summary.should_publish_public is False
    assert summary.no_publish_reason == OptionsSentimentService.NO_PUBLISH_SINGLE_EXPIRY_DISTORTION


async def test_sandbox_environment_disables_public_publish(test_settings) -> None:
    transport = _build_transport(
        expirations=["2026-04-24", "2026-05-09"],
        chains={
            "2026-04-24": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 2000, "volume": 700},
                    {"option_type": "call", "open_interest": 2000, "volume": 700},
                ]
            ),
            "2026-05-09": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 1800, "volume": 600},
                    {"option_type": "call", "open_interest": 1800, "volume": 600},
                ]
            ),
        },
    )
    service = _build_service(test_settings, transport, tradier_env="sandbox")

    summary = await _collect_summary(service)

    assert summary.should_publish_public is False
    assert summary.no_publish_reason == OptionsSentimentService.NO_PUBLISH_SANDBOX


async def test_quote_failure_keeps_summary_generation_alive(test_settings) -> None:
    transport = _build_transport(
        expirations=["2026-04-24", "2026-05-09"],
        chains={
            "2026-04-24": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 2000, "volume": 700},
                    {"option_type": "call", "open_interest": 2000, "volume": 700},
                ]
            ),
            "2026-05-09": _chain_payload(
                [
                    {"option_type": "put", "open_interest": 1800, "volume": 600},
                    {"option_type": "call", "open_interest": 1800, "volume": 600},
                ]
            ),
        },
        quote_status=503,
        quote_payload={"fault": {"message": "quote unavailable"}},
    )
    service = _build_service(test_settings, transport)

    summary = await _collect_summary(service)

    assert summary.close is None
    assert summary.change_1d_pct is None
    assert summary.pcr_oi_total == 1.0
    assert summary.raw_response_json["quote"]["status"] == "failed"
