from __future__ import annotations

import asyncio

from app.schemas import DeliveryResult


class DeliveryAckBroker:
    def __init__(self, *, completed_limit: int = 500) -> None:
        self._waiters: dict[str, asyncio.Future[DeliveryResult]] = {}
        self._completed: dict[str, DeliveryResult] = {}
        self._completed_order: list[str] = []
        self._completed_limit = completed_limit
        self._lock = asyncio.Lock()

    async def register_waiter(self, message_id: str) -> asyncio.Future[DeliveryResult]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[DeliveryResult] = loop.create_future()
        async with self._lock:
            self._waiters[message_id] = future
            cached = self._completed.pop(message_id, None)
            if cached is not None:
                self._completed_order = [item for item in self._completed_order if item != message_id]
                future.set_result(cached)
        return future

    async def unregister_waiter(self, message_id: str, future: asyncio.Future[DeliveryResult]) -> None:
        async with self._lock:
            if self._waiters.get(message_id) is future:
                self._waiters.pop(message_id, None)

    async def resolve(self, result: DeliveryResult) -> bool:
        async with self._lock:
            future = self._waiters.get(result.message_id)
            if future is not None and not future.done():
                future.set_result(result)
                return True

            self._completed[result.message_id] = result
            self._completed_order = [item for item in self._completed_order if item != result.message_id]
            self._completed_order.append(result.message_id)
            while len(self._completed_order) > self._completed_limit:
                stale = self._completed_order.pop(0)
                self._completed.pop(stale, None)
            return False
