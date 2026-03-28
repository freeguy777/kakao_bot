from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from server.config import get_settings
from server.core.contracts import build_standard_response
from server.infra.sqlite_store import count_outbox_messages
from server.utils import make_trace_id, now_kst


@dataclass(slots=True)
class RuntimeHealthUseCase:
    settings_provider: Callable[[], Any] = get_settings
    outbox_counter: Callable[[str | None], int] = count_outbox_messages
    trace_id_factory: Callable[[], str] = make_trace_id
    now_factory: Callable[[], Any] = now_kst

    def build_health(self, scheduler: Any) -> dict[str, Any]:
        settings = self.settings_provider()
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
                "delivery_mode": "polling_outbox",
                "phone_socket_enabled": settings.socket.enabled,
                "phone_socket_host": settings.socket.host,
                "phone_socket_port": settings.socket.port,
                "phone_socket_flush_interval_seconds": settings.socket.flush_interval_seconds,
                "pending_outbox_count": self.outbox_counter("pending"),
                "inflight_outbox_count": self.outbox_counter("inflight"),
                "scheduled_jobs": scheduled_jobs,
                "now": self.now_factory().isoformat(),
            },
        )

    def build_socket_health(self) -> dict[str, Any]:
        return build_standard_response(
            ok=True,
            trace_id=self.trace_id_factory(),
            action="socket.health",
            messages=["socket push is deprecated; polling/outbox delivery is active"],
            error=None,
            meta={
                "deprecated": True,
                "delivery_mode": "polling_outbox",
                "pending_outbox_count": self.outbox_counter("pending"),
                "inflight_outbox_count": self.outbox_counter("inflight"),
            },
        )
