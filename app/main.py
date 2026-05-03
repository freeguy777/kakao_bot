from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.webhook import router as webhook_router
from app.config import Settings
from app.db import configure_sqlite, create_session_factory, create_sqlite_engine, ensure_runtime_schema
from app.logging import configure_logging
from app.models import Base
from app.repositories import (
    ArtifactRepository,
    DeliveryRepository,
    EventRepository,
    FeatureOverrideRepository,
    OptionsSentimentSummaryRepository,
    ScheduledJobRepository,
    sqlite_policy_from_settings,
)
from app.router import MessageRouter, RoomRegistry
from app.scheduler import SchedulerService
from app.services.admin_command_service import AdminCommandService
from app.services.admin_notify import AdminNotifyService
from app.services.delivery_ack_broker import DeliveryAckBroker
from app.services.chat_service import ChatService
from app.services.child_age_service import ChildAgeService
from app.services.delivery_service import DeliveryService
from app.services.family_brief_service import FamilyBriefService
from app.services.clinicaltrials_service import ClinicalTrialsService
from app.services.hanall_research_service import HanallResearchService
from app.services.hanall_prefetch_service import HanallPrefetchService
from app.services.options_sentiment_service import OptionsSentimentService
from app.services.opendart_service import OpenDartService
from app.services.sec_edgar_service import SecEdgarService
from app.services.socket_client import SocketClient
from app.services.weather_service import WeatherService
from app.services.youtube_service import YouTubeService


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_directory_structure()
    configure_logging(settings.log_level)

    engine = create_sqlite_engine(settings.database_url)
    configure_sqlite(engine)
    Base.metadata.create_all(engine)
    ensure_runtime_schema(engine)
    session_factory = create_session_factory(engine)
    sqlite_policy = sqlite_policy_from_settings(settings)

    event_repository = EventRepository(session_factory, sqlite_policy)
    delivery_repository = DeliveryRepository(session_factory, sqlite_policy)
    artifact_repository = ArtifactRepository(session_factory, sqlite_policy)
    override_repository = FeatureOverrideRepository(session_factory, sqlite_policy)
    options_summary_repository = OptionsSentimentSummaryRepository(session_factory, sqlite_policy)
    scheduled_job_repository = ScheduledJobRepository(session_factory, sqlite_policy)

    prompts = settings.load_prompts()
    room_registry = RoomRegistry(settings, override_repository)
    delivery_ack_broker = DeliveryAckBroker()
    socket_client = SocketClient(settings, delivery_ack_broker)
    admin_notifier = AdminNotifyService(settings)
    delivery_service = DeliveryService(
        settings=settings,
        delivery_repository=delivery_repository,
        socket_client=socket_client,
        admin_notifier=admin_notifier,
    )
    admin_notifier.attach_sender(
        lambda room_name, text: delivery_service.send_text(
            room_name,
            text,
            suppress_admin_report=True,
        )
    )
    chat_service = ChatService(
        settings=settings,
        prompts=prompts,
        room_registry=room_registry,
        delivery_service=delivery_service,
        admin_notifier=admin_notifier,
    )
    youtube_service = YouTubeService(
        settings=settings,
        prompts=prompts,
        delivery_service=delivery_service,
        admin_notifier=admin_notifier,
    )
    weather_service = WeatherService(
        settings=settings,
        delivery_service=delivery_service,
        admin_notifier=admin_notifier,
    )
    opendart_service = OpenDartService(settings)
    clinicaltrials_service = ClinicalTrialsService(settings)
    sec_edgar_service = SecEdgarService(settings)
    hanall_prefetch_service = HanallPrefetchService(
        opendart_service=opendart_service,
        clinicaltrials_service=clinicaltrials_service,
        sec_edgar_service=sec_edgar_service,
    )
    child_age_service = ChildAgeService(settings)
    family_brief_service = FamilyBriefService(weather_service=weather_service, child_age_service=child_age_service)
    options_sentiment_service = OptionsSentimentService(
        settings=settings,
        summary_repository=options_summary_repository,
    )
    hanall_research_service = HanallResearchService(
        settings=settings,
        prompts=prompts,
        artifact_repository=artifact_repository,
        hanall_prefetch_service=hanall_prefetch_service,
        options_sentiment_service=options_sentiment_service,
    )
    scheduler_service = SchedulerService(
        settings=settings,
        room_registry=room_registry,
        delivery_service=delivery_service,
        admin_notifier=admin_notifier,
        hanall_research_service=hanall_research_service,
        family_brief_service=family_brief_service,
        options_sentiment_service=options_sentiment_service,
        options_summary_repository=options_summary_repository,
        scheduled_job_repository=scheduled_job_repository,
    )
    admin_command_service = AdminCommandService(
        settings=settings,
        room_registry=room_registry,
        delivery_service=delivery_service,
        scheduler_service=scheduler_service,
    )
    message_router = MessageRouter(
        settings=settings,
        room_registry=room_registry,
        admin_command_service=admin_command_service,
        chat_service=chat_service,
        youtube_service=youtube_service,
        weather_service=weather_service,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.validate_runtime_secrets()
        scheduler_service.start()
        yield
        scheduler_service.shutdown()
        await socket_client.close()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.event_repository = event_repository
    app.state.delivery_repository = delivery_repository
    app.state.message_router = message_router
    app.state.scheduler_service = scheduler_service
    app.state.delivery_service = delivery_service
    app.state.room_registry = room_registry
    app.state.admin_notifier = admin_notifier
    app.state.admin_command_service = admin_command_service
    app.state.weather_service = weather_service
    app.state.hanall_research_service = hanall_research_service
    app.include_router(webhook_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
