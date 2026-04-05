from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
from typing import Any, Callable

from server.config import get_settings
from server.core.contracts import build_standard_response
from server.application.use_cases.outbox_polling import ACTIVE_TRANSPORT, get_polling_status_snapshot
from server.infra.sqlite_store import count_outbox_messages, list_polling_heartbeats, list_scheduler_events
from server.utils import make_trace_id, now_kst


@dataclass(slots=True)
class RuntimeHealthUseCase:
    settings_provider: Callable[[], Any] = get_settings
    outbox_counter: Callable[[str | None], int] = count_outbox_messages
    scheduler_event_lister: Callable[[int], list[dict[str, Any]]] = list_scheduler_events
    polling_heartbeat_lister: Callable[[int, str | None], list[dict[str, Any]]] = list_polling_heartbeats
    trace_id_factory: Callable[[], str] = make_trace_id
    now_factory: Callable[[], Any] = now_kst

    def _build_polling_meta(self) -> dict[str, Any]:
        polling_status = get_polling_status_snapshot(outbox_counter=self.outbox_counter)
        recent_polling_heartbeats = self.polling_heartbeat_lister(10, None)
        latest_pull = self.polling_heartbeat_lister(1, "pull")
        last_observed_pull_at = polling_status["last_pull_at"]
        if not last_observed_pull_at and latest_pull:
            last_observed_pull_at = latest_pull[0]["checked_at"]

        last_pull_age_seconds: int | None = None
        parsed_last_pull_at = self._parse_iso_datetime(last_observed_pull_at)
        now_value = self.now_factory()
        if parsed_last_pull_at is not None:
            last_pull_age_seconds = max(0, int((now_value - parsed_last_pull_at).total_seconds()))

        interval_ms = int(polling_status.get("polling_interval_ms") or 0)
        stale_threshold_seconds = max(60, (interval_ms * 3 + 999) // 1000) if interval_ms > 0 else 60
        polling_stale = None if last_pull_age_seconds is None else last_pull_age_seconds > stale_threshold_seconds

        return {
            **polling_status,
            "last_observed_pull_at": last_observed_pull_at,
            "last_pull_age_seconds": last_pull_age_seconds,
            "polling_stale": polling_stale,
            "polling_stale_threshold_seconds": stale_threshold_seconds,
            "recent_polling_heartbeats": recent_polling_heartbeats,
        }

    @staticmethod
    def _parse_iso_datetime(value: str | None) -> datetime | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    def build_health(self, scheduler: Any) -> dict[str, Any]:
        settings = self.settings_provider()
        polling_status = self._build_polling_meta()
        scheduled_jobs = []
        if scheduler is not None:
            for job in scheduler.get_jobs():
                scheduled_jobs.append(
                    {
                        "id": job.id,
                        "name": job.name,
                        "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                        "trigger": str(job.trigger),
                    }
                )

        return build_standard_response(
            ok=True,
            trace_id=self.trace_id_factory(),
            action="health",
            messages=["ok"],
            error=None,
            meta={
                "env": settings.app_env,
                "timezone": settings.timezone,
                "api_base_path": settings.api_base_path,
                "api_base_url": settings.api_base_url,
                "active_transport": ACTIVE_TRANSPORT,
                "delivery_mode": ACTIVE_TRANSPORT,
                "polling_enabled": polling_status["polling_enabled"],
                "polling_interval_ms": polling_status["polling_interval_ms"],
                "last_pull_at": polling_status["last_pull_at"],
                "last_observed_pull_at": polling_status["last_observed_pull_at"],
                "last_pull_age_seconds": polling_status["last_pull_age_seconds"],
                "last_success_at": polling_status["last_success_at"],
                "last_ack_success_count": polling_status["last_ack_success_count"],
                "last_ack_fail_count": polling_status["last_ack_fail_count"],
                "polling_stale": polling_status["polling_stale"],
                "polling_stale_threshold_seconds": polling_status["polling_stale_threshold_seconds"],
                "scheduler_recent_misfire_grace_seconds": getattr(
                    settings,
                    "scheduler_recent_misfire_grace_seconds",
                    None,
                ),
                "scheduler_recent_events": self.scheduler_event_lister(10),
                "recent_polling_heartbeats": polling_status["recent_polling_heartbeats"],
                "pending_outbox_count": polling_status["pending_outbox_count"],
                "inflight_outbox_count": polling_status["inflight_outbox_count"],
                "socket_transport": {
                    "deprecated": True,
                    "status": "inactive",
                    "enabled": bool(getattr(settings.socket, "enabled", False)),
                },
                "scheduled_jobs": scheduled_jobs,
                "now": self.now_factory().isoformat(),
            },
        )

    def build_socket_health(self) -> dict[str, Any]:
        polling_status = self._build_polling_meta()
        return build_standard_response(
            ok=True,
            trace_id=self.trace_id_factory(),
            action="socket.health",
            messages=["socket delivery is inactive; polling_outbox is the only supported delivery path"],
            error=None,
            meta={
                "deprecated": True,
                "status": "inactive",
                "active_transport": ACTIVE_TRANSPORT,
                "delivery_mode": ACTIVE_TRANSPORT,
                "pending_outbox_count": polling_status["pending_outbox_count"],
                "inflight_outbox_count": polling_status["inflight_outbox_count"],
            },
        )

    def build_polling_status(self) -> dict[str, Any]:
        polling_status = self._build_polling_meta()
        return build_standard_response(
            ok=True,
            trace_id=self.trace_id_factory(),
            action="polling.status",
            messages=["ok"],
            error=None,
            meta=polling_status,
        )
