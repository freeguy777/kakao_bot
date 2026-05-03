from __future__ import annotations

from datetime import date, datetime, timezone

from app.schemas import HanallArtifact, OptionsPCRDailySummary, OptionsSentimentCollectionResult, OptionsSentimentSnapshot
from app.services.hanall_research_service import HanallResearchService


class FakeArtifactRepository:
    def __init__(self) -> None:
        self.saved: list[HanallArtifact] = []

    def get_by_key(self, artifact_key: str) -> HanallArtifact | None:
        return None

    def save(self, artifact: HanallArtifact, artifact_type: str, room_name: str | None = None) -> HanallArtifact:
        self.saved.append(artifact)
        return artifact


class FakeOptionsSentimentService:
    def __init__(self, *, result: OptionsSentimentCollectionResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.saved_summaries: list[OptionsPCRDailySummary] = []

    async def collect_daily_summary(self, *, artifact_date_kst: date):
        if self.error is not None:
            raise self.error
        return self.result

    def save_daily_summary(self, summary: OptionsPCRDailySummary) -> OptionsPCRDailySummary:
        self.saved_summaries.append(summary)
        return summary


def _build_summary() -> OptionsPCRDailySummary:
    return OptionsPCRDailySummary(
        date_us=date(2026, 4, 10),
        date_kst=date(2026, 4, 11),
        symbol="IMVT",
        close=18.5,
        change_1d_pct=2.0,
        pcr_oi_total=0.95,
        pcr_vol_total=1.05,
        put_oi_total=1900,
        call_oi_total=2000,
        put_vol_total=1050,
        call_vol_total=1000,
        total_option_volume=2050,
        total_option_oi=3900,
        short_dte_pcr_oi=0.98,
        short_dte_pcr_vol=1.01,
        data_quality_flag="OK",
        should_publish_public=True,
        no_publish_reason=None,
        source="tradier",
        source_environment="live",
        retrieved_at_utc=datetime(2026, 4, 10, 22, 42, tzinfo=timezone.utc),
        oi_effective_date=None,
        by_expiry_json=[],
        raw_response_json={},
    )


def _build_service(test_settings, artifact_repository: FakeArtifactRepository, options_service: object | None) -> HanallResearchService:
    return HanallResearchService(
        settings=test_settings,
        prompts=test_settings.load_prompts(),
        artifact_repository=artifact_repository,
        options_sentiment_service=options_service,
    )


async def test_hanall_collect_saves_options_snapshot_on_success(test_settings) -> None:
    artifact_repository = FakeArtifactRepository()
    summary = _build_summary()
    options_service = FakeOptionsSentimentService(
        result=OptionsSentimentCollectionResult(
            collect_status="success",
            summary=summary,
            snapshot=OptionsSentimentSnapshot(
                collect_status="success",
                symbol="IMVT",
                date_us=summary.date_us,
                date_kst=summary.date_kst,
                source="tradier",
                source_environment="live",
                retrieved_at_utc=summary.retrieved_at_utc,
                data_quality_flag="OK",
                should_publish_public=True,
                summary={"pcr_oi_total": summary.pcr_oi_total},
            ),
        )
    )
    service = _build_service(test_settings, artifact_repository, options_service)

    async def fake_run_research(artifact_date: date) -> HanallArtifact:
        return HanallArtifact(
            artifact_key=f"hanall:{artifact_date.isoformat()}",
            artifact_date=artifact_date,
            summary_text="summary",
            detail_text="detail",
            model_name="kimi-k2.5",
            raw_response={"completion": {}},
        )

    service._run_research = fake_run_research  # type: ignore[method-assign]

    artifact = await service.get_or_create_daily_artifact(date(2026, 4, 11))

    assert artifact_repository.saved
    assert options_service.saved_summaries == [summary]
    assert artifact.raw_response["options_sentiment"]["collect_status"] == "success"
    assert artifact.raw_response["options_sentiment"]["summary"]["pcr_oi_total"] == 0.95


async def test_hanall_collect_keeps_artifact_when_options_collect_raises(test_settings) -> None:
    artifact_repository = FakeArtifactRepository()
    options_service = FakeOptionsSentimentService(error=RuntimeError("tradier down"))
    service = _build_service(test_settings, artifact_repository, options_service)

    async def fake_run_research(artifact_date: date) -> HanallArtifact:
        return HanallArtifact(
            artifact_key=f"hanall:{artifact_date.isoformat()}",
            artifact_date=artifact_date,
            summary_text="summary",
            detail_text="detail",
            model_name="kimi-k2.5",
            raw_response={"completion": {}},
        )

    service._run_research = fake_run_research  # type: ignore[method-assign]

    artifact = await service.get_or_create_daily_artifact(date(2026, 4, 11))

    assert artifact_repository.saved
    assert options_service.saved_summaries == []
    assert artifact.summary_text == "summary"
    assert artifact.raw_response["options_sentiment"]["collect_status"] == "failed"
    assert "tradier down" in artifact.raw_response["options_sentiment"]["reason"]


async def test_hanall_collect_records_disabled_snapshot_without_failing(test_settings) -> None:
    artifact_repository = FakeArtifactRepository()
    options_service = FakeOptionsSentimentService(
        result=OptionsSentimentCollectionResult(
            collect_status="disabled",
            snapshot=OptionsSentimentSnapshot(
                collect_status="disabled",
                symbol="IMVT",
                date_us=date(2026, 4, 10),
                date_kst=date(2026, 4, 11),
                source="tradier",
                source_environment="live",
                retrieved_at_utc=datetime(2026, 4, 10, 22, 42, tzinfo=timezone.utc),
                should_publish_public=False,
                reason="TRADIER_API is not configured",
            ),
        )
    )
    service = _build_service(test_settings, artifact_repository, options_service)

    async def fake_run_research(artifact_date: date) -> HanallArtifact:
        return HanallArtifact(
            artifact_key=f"hanall:{artifact_date.isoformat()}",
            artifact_date=artifact_date,
            summary_text="summary",
            detail_text="detail",
            model_name="kimi-k2.5",
            raw_response={"completion": {}},
        )

    service._run_research = fake_run_research  # type: ignore[method-assign]

    artifact = await service.get_or_create_daily_artifact(date(2026, 4, 11))

    assert artifact_repository.saved
    assert options_service.saved_summaries == []
    assert artifact.raw_response["options_sentiment"]["collect_status"] == "disabled"
