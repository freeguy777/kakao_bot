from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import constants
from app.config import Settings
from app.schemas import OptionsSentimentSnapshot


class SchedulerService:
    ROOM_JOB_PREFIXES = ("hanall_publish::", "family_brief::")

    def __init__(
        self,
        *,
        settings: Settings,
        room_registry: object,
        delivery_service: object,
        admin_notifier: object,
        hanall_research_service: object,
        family_brief_service: object,
        options_sentiment_service: object | None = None,
        options_summary_repository: object | None = None,
        scheduled_job_repository: object,
    ) -> None:
        self._settings = settings
        self._room_registry = room_registry
        self._delivery_service = delivery_service
        self._admin_notifier = admin_notifier
        self._hanall_research_service = hanall_research_service
        self._family_brief_service = family_brief_service
        self._options_sentiment_service = options_sentiment_service
        self._options_summary_repository = options_summary_repository
        self._scheduled_job_repository = scheduled_job_repository
        self._timezone = ZoneInfo(settings.app_timezone)
        self._scheduler = AsyncIOScheduler(timezone=self._timezone)

    def start(self) -> None:
        self._scheduler.add_job(
            self.run_daily_hanall_collect,
            CronTrigger(hour=7, minute=42, timezone=self._timezone),
            id="hanall_collect",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self.refresh_room_jobs_if_needed,
            IntervalTrigger(seconds=self._settings.room_config_reload_interval_seconds, timezone=self._timezone),
            id="room_config_sync",
            replace_existing=True,
        )
        self.refresh_room_jobs(force=True)
        self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def run_daily_hanall_collect(self) -> None:
        artifact_date = datetime.now(self._timezone).date()
        try:
            artifact = await self._hanall_research_service.get_or_create_daily_artifact(artifact_date)
            await self._send_hanall_admin_detail_once(artifact, artifact_date)
        except Exception as exc:  # noqa: BLE001
            await self._admin_notifier.notify_feature_error(
                room_name="system",
                feature_name="hanall_collect",
                error_message=str(exc),
                failure_type=constants.FAILURE_RESEARCH,
            )

    async def refresh_room_jobs_if_needed(self) -> None:
        self.refresh_room_jobs()

    async def publish_hanall_room(self, room_name: str, *, force: bool = False) -> None:
        room = self._room_registry.resolve_room(room_name)
        if room is None or not room.features.get("hanall_briefing"):
            return
        now = datetime.now(self._timezone)
        job_key = self._hanall_publish_job_key(
            publish_date=now.date(),
            room_name=room.name,
            publish_time=room.hanall_publish_time,
            force=force,
        )
        if not force and self._scheduled_job_repository.is_success(job_key):
            return
        try:
            artifact = await self._hanall_research_service.get_existing_daily_artifact(now.date())
            if artifact is None:
                detail = "daily HanAll research artifact is missing; publish reuses only the 07:42 collect result"
                self._scheduled_job_repository.mark_status(job_key, constants.SCHEDULED_STATUS_FAILED, detail)
                await self._admin_notifier.notify_feature_error(
                    room_name=room.name,
                    feature_name="hanall_briefing",
                    error_message=detail,
                    failure_type=constants.FAILURE_RESEARCH,
                )
                return
            options_summary = self._get_options_summary_for_publish(artifact)
            public_results = await self._delivery_service.send_text(
                room.name,
                self._build_hanall_public_message(artifact=artifact, options_summary=options_summary),
                package_name=room.package_name,
                correlation_key=job_key,
                failure_type=constants.FAILURE_RESEARCH,
            )
            if not self._delivery_service.all_delivered(public_results):
                self._scheduled_job_repository.mark_status(
                    job_key,
                    constants.SCHEDULED_STATUS_FAILED,
                    self._delivery_service.summarize_results(public_results),
                )
                return
            await self._publish_hanall_options_warning(room=room, artifact=artifact, summary=options_summary)
            self._scheduled_job_repository.mark_status(job_key, constants.SCHEDULED_STATUS_SUCCESS)
        except Exception as exc:  # noqa: BLE001
            self._scheduled_job_repository.mark_status(job_key, constants.SCHEDULED_STATUS_FAILED, str(exc))
            await self._admin_notifier.notify_feature_error(
                room_name=room.name,
                feature_name="hanall_briefing",
                error_message=str(exc),
                failure_type=constants.FAILURE_RESEARCH,
            )

    async def _send_hanall_admin_detail_once(self, artifact: object, artifact_date: date) -> None:
        job_key = f"hanall_admin_detail:{artifact_date.isoformat()}"
        if self._scheduled_job_repository.is_success(job_key):
            return

        admin_results = await self._delivery_service.send_text(
            self._settings.admin_room_name,
            self._hanall_research_service.render_admin_message(artifact),
            suppress_admin_report=True,
            correlation_key=job_key,
            failure_type=constants.FAILURE_RESEARCH,
        )
        if self._delivery_service.all_delivered(admin_results):
            self._scheduled_job_repository.mark_status(job_key, constants.SCHEDULED_STATUS_SUCCESS)
            return
        self._scheduled_job_repository.mark_status(
            job_key,
            constants.SCHEDULED_STATUS_FAILED,
            self._delivery_service.summarize_results(admin_results),
        )

    def _build_hanall_public_message(self, *, artifact: object, options_summary: object | None) -> str:
        message = self._hanall_research_service.render_public_message(artifact)
        if not self._should_include_options_public_message(options_summary):
            return message
        options_message = self._options_sentiment_service.render_public_message(options_summary).strip()
        if not options_message:
            return message
        return f"{message}\n\n{options_message}"

    async def _publish_hanall_options_warning(self, *, room: object, artifact: object, summary: object | None) -> None:
        if self._options_sentiment_service is None:
            return

        if summary is not None:
            if summary.should_publish_public:
                return
            await self._maybe_send_options_warning_once(
                date_us=summary.date_us.isoformat(),
                symbol=summary.symbol,
                source_environment=summary.source_environment,
                reason=self._options_sentiment_service.get_warning_reason_from_summary(summary),
                text=self._options_sentiment_service.render_admin_warning_from_summary(
                    summary,
                    room_name=room.name,
                    public_message_included=self._should_include_options_public_message(summary),
                ),
            )
            return

        snapshot = self._get_options_snapshot_from_artifact(artifact)
        if snapshot is None:
            return
        await self._maybe_send_options_warning_once(
            date_us=snapshot.date_us.isoformat() if snapshot.date_us is not None else artifact.artifact_date.isoformat(),
            symbol=snapshot.symbol,
            source_environment=snapshot.source_environment,
            reason=self._options_sentiment_service.get_warning_reason_from_snapshot(snapshot),
            text=self._options_sentiment_service.render_admin_warning_from_snapshot(snapshot, room_name=room.name),
        )

    def _get_options_summary_for_publish(self, artifact: object):
        if self._options_summary_repository is None:
            return None
        get_daily_summary = getattr(self._options_summary_repository, "get_daily_summary", None)
        if not callable(get_daily_summary):
            return None
        return get_daily_summary(
            self._options_sentiment_service.DEFAULT_SYMBOL,
            date_kst=artifact.artifact_date,
            source_environment=self._settings.tradier_env,
        )

    @staticmethod
    def _get_options_snapshot_from_artifact(artifact: object) -> OptionsSentimentSnapshot | None:
        raw_response = getattr(artifact, "raw_response", None)
        if not isinstance(raw_response, dict):
            return None
        snapshot_payload = raw_response.get("options_sentiment")
        if not isinstance(snapshot_payload, dict):
            return None
        try:
            return OptionsSentimentSnapshot.model_validate(snapshot_payload)
        except Exception:  # noqa: BLE001
            return None

    def _should_include_options_public_message(self, summary: object | None) -> bool:
        return (
            summary is not None
            and self._options_sentiment_service is not None
            and getattr(summary, "source_environment", "").strip().lower() == "live"
        )

    async def _maybe_send_options_warning_once(
        self,
        *,
        date_us: str,
        symbol: str,
        source_environment: str,
        reason: str | None,
        text: str | None,
    ) -> None:
        if not reason or not text:
            return

        warning_key = f"hanall_options_warning:{date_us}:{symbol}:{source_environment}:{reason}"
        if self._scheduled_job_repository.is_success(warning_key):
            return

        warning_results = await self._delivery_service.send_text(
            self._settings.admin_room_name,
            text,
            suppress_admin_report=True,
            correlation_key=warning_key,
            failure_type=constants.FAILURE_RESEARCH,
        )
        if self._delivery_service.all_delivered(warning_results):
            self._scheduled_job_repository.mark_status(warning_key, constants.SCHEDULED_STATUS_SUCCESS)

    @staticmethod
    def _hanall_publish_job_key(
        *,
        publish_date: date,
        room_name: str,
        publish_time: str | None,
        force: bool,
    ) -> str:
        prefix = "hanall_manual" if force else "hanall"
        return f"{prefix}:{publish_date.isoformat()}:{room_name}:{publish_time}"

    async def publish_family_brief(self, room_name: str) -> None:
        room = self._room_registry.resolve_room(room_name)
        if room is None:
            return
        publish_time = room.weather.publish_time or "08:10"
        now = datetime.now(self._timezone)
        job_key = f"family_weather:{now.date().isoformat()}:{room.name}:{publish_time}"
        if self._scheduled_job_repository.is_success(job_key):
            return
        try:
            message = await self._family_brief_service.build_daily_message(room)
            if message:
                results = await self._delivery_service.send_text(
                    room.name,
                    message,
                    package_name=room.package_name,
                    correlation_key=job_key,
                    failure_type=constants.FAILURE_API,
                )
                if not self._delivery_service.all_delivered(results):
                    self._scheduled_job_repository.mark_status(
                        job_key,
                        constants.SCHEDULED_STATUS_FAILED,
                        self._delivery_service.summarize_results(results),
                    )
                    return
            self._scheduled_job_repository.mark_status(job_key, constants.SCHEDULED_STATUS_SUCCESS)
        except Exception as exc:  # noqa: BLE001
            self._scheduled_job_repository.mark_status(job_key, constants.SCHEDULED_STATUS_FAILED, str(exc))
            await self._admin_notifier.notify_feature_error(
                room_name=room.name,
                feature_name="family_brief",
                error_message=str(exc),
                failure_type=constants.FAILURE_API,
            )

    def refresh_room_jobs(self, *, force: bool = False) -> bool:
        config_changed = False
        if force:
            config_changed = True
        else:
            reload_if_config_changed = getattr(self._room_registry, "reload_if_config_changed", None)
            if callable(reload_if_config_changed):
                config_changed = bool(reload_if_config_changed())
        if not config_changed:
            return False

        desired_jobs = self._build_room_job_specs()
        desired_job_ids = set(desired_jobs)
        existing_job_ids = {job.id for job in self._scheduler.get_jobs() if self._is_room_job(job.id)}

        for job_id in existing_job_ids - desired_job_ids:
            self._scheduler.remove_job(job_id)

        for job_id, spec in desired_jobs.items():
            self._scheduler.add_job(
                spec["func"],
                spec["trigger"],
                id=job_id,
                replace_existing=True,
                kwargs=spec["kwargs"],
            )
        return True

    def _build_room_job_specs(self) -> dict[str, dict[str, object]]:
        jobs: dict[str, dict[str, object]] = {}
        for room in self._room_registry.list_rooms():
            if room.features.get("hanall_briefing") and room.hanall_publish_time:
                hour, minute = map(int, room.hanall_publish_time.split(":"))
                jobs[self._hanall_job_id(room.name)] = {
                    "func": self.publish_hanall_room,
                    "trigger": CronTrigger(hour=hour, minute=minute, timezone=self._timezone),
                    "kwargs": {"room_name": room.name},
                }
            if room.features.get("weather") or room.features.get("child_age"):
                publish_time = room.weather.publish_time or "08:10"
                hour, minute = map(int, publish_time.split(":"))
                jobs[self._family_job_id(room.name)] = {
                    "func": self.publish_family_brief,
                    "trigger": CronTrigger(hour=hour, minute=minute, timezone=self._timezone),
                    "kwargs": {"room_name": room.name},
                }
        return jobs

    def _is_room_job(self, job_id: str) -> bool:
        return job_id.startswith(self.ROOM_JOB_PREFIXES)

    @staticmethod
    def _hanall_job_id(room_name: str) -> str:
        return f"hanall_publish::{room_name}"

    @staticmethod
    def _family_job_id(room_name: str) -> str:
        return f"family_brief::{room_name}"
