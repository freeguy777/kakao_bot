from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import constants
from app.config import Settings


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
        scheduled_job_repository: object,
    ) -> None:
        self._settings = settings
        self._room_registry = room_registry
        self._delivery_service = delivery_service
        self._admin_notifier = admin_notifier
        self._hanall_research_service = hanall_research_service
        self._family_brief_service = family_brief_service
        self._scheduled_job_repository = scheduled_job_repository
        self._timezone = ZoneInfo(settings.app_timezone)
        self._scheduler = AsyncIOScheduler(timezone=self._timezone)

    def start(self) -> None:
        self._scheduler.add_job(
            self.run_daily_hanall_collect,
            CronTrigger(hour=8, minute=0, timezone=self._timezone),
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
            await self._hanall_research_service.get_or_create_daily_artifact(artifact_date)
        except Exception as exc:  # noqa: BLE001
            await self._admin_notifier.notify_feature_error(
                room_name="system",
                feature_name="hanall_collect",
                error_message=str(exc),
                failure_type=constants.FAILURE_RESEARCH,
            )

    async def refresh_room_jobs_if_needed(self) -> None:
        self.refresh_room_jobs()

    async def publish_hanall_room(self, room_name: str) -> None:
        room = self._room_registry.resolve_room(room_name)
        if room is None or not room.features.get("hanall_briefing"):
            return
        now = datetime.now(self._timezone)
        job_key = f"hanall:{now.date().isoformat()}:{room.name}:{room.hanall_publish_time}"
        if self._scheduled_job_repository.is_success(job_key):
            return
        try:
            artifact = await self._hanall_research_service.get_existing_daily_artifact(now.date())
            if artifact is None:
                detail = "daily HanAll research artifact is missing; publish reuses only the 08:00 collect result"
                self._scheduled_job_repository.mark_status(job_key, constants.SCHEDULED_STATUS_FAILED, detail)
                await self._admin_notifier.notify_feature_error(
                    room_name=room.name,
                    feature_name="hanall_briefing",
                    error_message=detail,
                    failure_type=constants.FAILURE_RESEARCH,
                )
                return
            public_results = await self._delivery_service.send_text(
                room.name,
                self._hanall_research_service.render_public_message(artifact),
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

            admin_results = await self._delivery_service.send_text(
                self._settings.admin_room_name,
                self._hanall_research_service.render_admin_message(artifact),
                suppress_admin_report=True,
                correlation_key=job_key,
            )
            if not self._delivery_service.all_delivered(admin_results):
                self._scheduled_job_repository.mark_status(
                    f"{job_key}:admin",
                    constants.SCHEDULED_STATUS_FAILED,
                    self._delivery_service.summarize_results(admin_results),
                )
            self._scheduled_job_repository.mark_status(job_key, constants.SCHEDULED_STATUS_SUCCESS)
        except Exception as exc:  # noqa: BLE001
            self._scheduled_job_repository.mark_status(job_key, constants.SCHEDULED_STATUS_FAILED, str(exc))
            await self._admin_notifier.notify_feature_error(
                room_name=room.name,
                feature_name="hanall_briefing",
                error_message=str(exc),
                failure_type=constants.FAILURE_RESEARCH,
            )

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
