from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from server.config import get_settings
from server.core.contracts import build_standard_response
from server.application.use_cases.outbox_polling import ACTIVE_TRANSPORT, get_polling_status_snapshot
from server.infra.sqlite_store import count_outbox_messages, list_scheduler_events
from server.utils import make_trace_id, now_kst


@dataclass(slots=True)
class RuntimeHealthUseCase:
    settings_provider: Callable[[], Any] = get_settings
    outbox_counter: Callable[[str | None], int] = count_outbox_messages
    scheduler_event_lister: Callable[[int], list[dict[str, Any]]] = list_scheduler_events
    trace_id_factory: Callable[[], str] = make_trace_id
    now_factory: Callable[[], Any] = now_kst

    def build_health(self, scheduler: Any) -> dict[str, Any]:
        settings = self.settings_provider()
        polling_status = get_polling_status_snapshot(outbox_counter=self.outbox_counter)
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
                "last_success_at": polling_status["last_success_at"],
                "last_ack_success_count": polling_status["last_ack_success_count"],
                "last_ack_fail_count": polling_status["last_ack_fail_count"],
                "scheduler_recent_misfire_grace_seconds": getattr(
                    settings,
                    "scheduler_recent_misfire_grace_seconds",
                    None,
                ),
                "scheduler_recent_events": self.scheduler_event_lister(10),
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
        polling_status = get_polling_status_snapshot(outbox_counter=self.outbox_counter)
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
        polling_status = get_polling_status_snapshot(outbox_counter=self.outbox_counter)
        return build_standard_response(
            ok=True,
            trace_id=self.trace_id_factory(),
            action="polling.status",
            messages=["ok"],
            error=None,
            meta=polling_status,
        )
