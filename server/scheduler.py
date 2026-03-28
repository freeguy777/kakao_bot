from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from server.application.delivery import deliver_room_messages, notify_admin_error
from server.application.errors import FeatureExecutionError
from server.application.family import build_family_morning_brief
from server.application.news import build_hanall_news_brief
from server.application.prompting import run_prompt_by_key
from server.config import get_rooms_registry, reload_settings
from server.db import record_job_run
from server.settings import ROOMS_PATH
from server.utils import make_trace_id, now_kst

logger = logging.getLogger(__name__)

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
except ImportError:  # pragma: no cover
    BackgroundScheduler = None
    CronTrigger = None


JobBuilder = Callable[[str], str]


def _build_test_prompt(_: str) -> str:
    return run_prompt_by_key("test_prompt")


JOB_BUILDERS: dict[str, JobBuilder] = {
    "family_morning_brief": lambda room_key: build_family_morning_brief(room_key=room_key),
    "hanall_news_brief": lambda room_key: build_hanall_news_brief(
        room_key=room_key,
        raise_on_error=True,
        send_raw_to_admin=True,
    ),
    "test_prompt": _build_test_prompt,
}
SYSTEM_JOB_IDS = {"system:rooms_config_watch"}
_ROOMS_CONFIG_MTIME_NS: int | None = None
RECENT_MISFIRE_GRACE_SECONDS = 20


@dataclass(frozen=True)
class ScheduledJobSpec:
    room_key: str
    job_name: str
    job_index: int
    builder: JobBuilder
    trigger: dict[str, Any]
    trigger_index: int


def create_scheduler(timezone: str) -> Any:
    if BackgroundScheduler is None:
        logger.warning("APScheduler is not installed. Scheduled jobs are disabled.")
        return None
    return BackgroundScheduler(timezone=timezone)


def _deliver_job_message(job_name: str, room_key: str, builder: JobBuilder) -> None:
    trace_id = make_trace_id()
    dedupe_key = f"schedule:{room_key}:{job_name}:{now_kst().strftime('%Y%m%d%H%M')}"
    try:
        message = builder(room_key)
        result = deliver_room_messages(
            room_key=room_key,
            message=message,
            source_type=f"schedule:{job_name}",
            trace_id=trace_id,
            meta={"job_name": job_name},
            dedupe_key=dedupe_key,
        )
        if result["via"] == "dedupe_skip":
            record_job_run(job_name, "skipped", "duplicate schedule delivery skipped", trace_id)
            return
        if result["via"] == "polling_outbox":
            detail = (
                f"queued via={result['via']} "
                f"trace_id={trace_id} "
                f"outbox_ids={result.get('outbox_ids', [])}"
            )
            status = "queued"
        elif result["ok"]:
            detail = f"delivered via={result['via']} trace_id={trace_id}"
            status = "success"
        else:
            detail = (
                f"queued via={result['via']} "
                f"outbox_ids={result.get('outbox_ids', [])} "
                f"error={result.get('error', '')}"
            )
            status = "queued"
        record_job_run(job_name, status, detail, trace_id)
    except FeatureExecutionError as exc:
        logger.warning(
            "scheduled feature skipped room_key=%s job_name=%s trace_id=%s detail=%s",
            room_key,
            job_name,
            trace_id,
            exc.detail,
        )
        notify_admin_error(
            room_key=room_key,
            feature_key=exc.feature_key,
            trace_id=trace_id,
            detail=exc.detail,
            meta={"job_name": job_name},
        )
        record_job_run(job_name, "skipped", exc.detail, trace_id)
    except Exception as exc:
        logger.exception("scheduled job failed job_name=%s trace_id=%s", job_name, trace_id, exc_info=exc)
        notify_admin_error(
            room_key=room_key,
            feature_key=f"schedule:{job_name}",
            trace_id=trace_id,
            detail=str(exc),
            meta={"job_name": job_name},
        )
        record_job_run(job_name, "failed", str(exc), trace_id)


def _iter_room_job_specs() -> list[ScheduledJobSpec]:
    registry = get_rooms_registry()
    job_specs: list[ScheduledJobSpec] = []
    for room in registry.rooms.values():
        if not room.schedules.enabled:
            continue
        for job_index, room_job in enumerate(room.schedules.jobs, start=1):
            builder_key = room_job.builder
            builder = JOB_BUILDERS.get(builder_key)
            if builder is None:
                logger.warning("scheduled job builder not found room_key=%s job_name=%s builder=%s", room.room_key, room_job.name, builder_key)
                continue
            if not room_job.triggers:
                logger.warning("scheduled job triggers missing room_key=%s job_name=%s", room.room_key, room_job.name)
                continue
            for trigger_index, trigger in enumerate(room_job.triggers, start=1):
                if not isinstance(trigger, dict):
                    logger.warning(
                        "scheduled job trigger is invalid room_key=%s job_name=%s index=%s",
                        room.room_key,
                        room_job.name,
                        trigger_index,
                    )
                    continue
                job_specs.append(
                    ScheduledJobSpec(
                        room_key=room.room_key,
                        job_name=room_job.name,
                        job_index=job_index,
                        builder=builder,
                        trigger=trigger,
                        trigger_index=trigger_index,
                    )
                )
    return job_specs


def _build_scheduler_job_id(room_key: str, job_name: str, job_index: int, trigger: dict[str, Any], trigger_index: int) -> str:
    id_suffix = str(trigger.get("id_suffix", "")).strip()
    if id_suffix:
        return f"{room_key}:{job_name}:{job_index}:{id_suffix}"
    return f"{room_key}:{job_name}:{job_index}:{trigger_index}"


def _build_cron_trigger(trigger: dict[str, Any], default_timezone: str | None = None) -> Any:
    allowed_fields = {
        "year",
        "month",
        "day",
        "week",
        "day_of_week",
        "hour",
        "minute",
        "second",
        "start_date",
        "end_date",
        "timezone",
        "jitter",
    }
    cron_kwargs = {key: value for key, value in trigger.items() if key in allowed_fields}
    if "timezone" not in cron_kwargs and default_timezone:
        cron_kwargs["timezone"] = default_timezone
    if not cron_kwargs:
        raise ValueError("cron trigger fields are empty")
    return CronTrigger(**cron_kwargs)


def _get_rooms_config_mtime_ns(path: Path = ROOMS_PATH) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        logger.warning("rooms config mtime is unavailable path=%s", path)
        return None


def _sync_room_jobs(scheduler: Any, timezone: str) -> list[str]:
    if scheduler is None or CronTrigger is None:
        return []

    desired_job_ids: list[str] = []
    desired_job_id_set: set[str] = set()
    for spec in _iter_room_job_specs():
        job_id = _build_scheduler_job_id(spec.room_key, spec.job_name, spec.job_index, spec.trigger, spec.trigger_index)
        scheduler.add_job(
            lambda job_name=spec.job_name, room_key=spec.room_key, builder=spec.builder: _deliver_job_message(
                job_name,
                room_key,
                builder,
            ),
            trigger=_build_cron_trigger(spec.trigger, timezone),
            id=job_id,
            name=f"scheduled:{spec.room_key}:{spec.job_name}",
            replace_existing=True,
        )
        desired_job_ids.append(job_id)
        desired_job_id_set.add(job_id)
        logger.info("scheduled job registered id=%s room_key=%s job_name=%s", job_id, spec.room_key, spec.job_name)

    for job in scheduler.get_jobs():
        if job.id in SYSTEM_JOB_IDS:
            continue
        if job.id not in desired_job_id_set:
            scheduler.remove_job(job.id)
            logger.info("scheduled job removed id=%s", job.id)
    return desired_job_ids


def _deliver_recently_due_jobs(timezone: str) -> list[str]:
    delivered_job_ids: list[str] = []
    now = datetime.now(ZoneInfo(timezone))
    lookup_from = now - timedelta(minutes=1)

    for spec in _iter_room_job_specs():
        cron_trigger = _build_cron_trigger(spec.trigger, timezone)
        due_time = cron_trigger.get_next_fire_time(None, lookup_from)
        if due_time is None or now <= due_time:
            continue
        delay_seconds = (now - due_time).total_seconds()
        if delay_seconds > RECENT_MISFIRE_GRACE_SECONDS:
            continue
        job_id = _build_scheduler_job_id(spec.room_key, spec.job_name, spec.job_index, spec.trigger, spec.trigger_index)
        logger.info(
            "scheduled job catch-up run id=%s room_key=%s job_name=%s scheduled_for=%s delay_seconds=%.1f",
            job_id,
            spec.room_key,
            spec.job_name,
            due_time,
            delay_seconds,
        )
        _deliver_job_message(spec.job_name, spec.room_key, spec.builder)
        delivered_job_ids.append(job_id)
    return delivered_job_ids


def _reload_schedule_config_job(scheduler: Any) -> None:
    global _ROOMS_CONFIG_MTIME_NS

    current_mtime_ns = _get_rooms_config_mtime_ns()
    if current_mtime_ns is None:
        return
    if _ROOMS_CONFIG_MTIME_NS is None:
        _ROOMS_CONFIG_MTIME_NS = current_mtime_ns
        return
    if current_mtime_ns == _ROOMS_CONFIG_MTIME_NS:
        return

    try:
        settings = reload_settings()
        synced_job_ids = _sync_room_jobs(scheduler, settings.timezone)
        catch_up_job_ids = _deliver_recently_due_jobs(settings.timezone)
        _ROOMS_CONFIG_MTIME_NS = current_mtime_ns
        logger.info(
            "scheduled jobs reloaded count=%s catch_up_count=%s timezone=%s path=%s",
            len(synced_job_ids),
            len(catch_up_job_ids),
            settings.timezone,
            ROOMS_PATH,
        )
    except Exception as exc:
        logger.exception("scheduled jobs reload failed path=%s", ROOMS_PATH, exc_info=exc)


def register_jobs(scheduler: Any, settings: Any) -> None:
    global _ROOMS_CONFIG_MTIME_NS

    if scheduler is None or CronTrigger is None:
        return

    synced_job_ids = _sync_room_jobs(scheduler, settings.timezone)
    catch_up_job_ids = _deliver_recently_due_jobs(settings.timezone)

    if scheduler.get_job("system:rooms_config_watch") is None:
        scheduler.add_job(
            lambda scheduler=scheduler: _reload_schedule_config_job(scheduler),
            trigger="interval",
            seconds=15,
            id="system:rooms_config_watch",
            name="system:rooms_config_watch",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("scheduled job registered id=system:rooms_config_watch")

    _ROOMS_CONFIG_MTIME_NS = _get_rooms_config_mtime_ns()
    scheduler.start()
    logger.info(
        "scheduler started timezone=%s scheduled_job_count=%s catch_up_count=%s",
        settings.timezone,
        len(synced_job_ids),
        len(catch_up_job_ids),
    )


def shutdown_scheduler(scheduler: Any) -> None:
    if scheduler is not None and getattr(scheduler, "running", False):
        scheduler.shutdown(wait=False)
        logger.info("scheduler stopped")
