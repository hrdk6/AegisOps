"""PostgreSQL LISTEN/NOTIFY subscriber with automatic reconnection."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import asyncpg

log = logging.getLogger("aegis.listen")

Handler = Callable[[str], Awaitable[None]]


class Listener:
    """Dispatches NOTIFY payloads on `channel` to an async handler."""

    def __init__(self, dsn: str, channel: str, handler: Handler) -> None:
        self.dsn = dsn
        self.channel = channel
        self.handler = handler
        self._task: asyncio.Task[None] | None = None
        self.connected = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"listen-{self.channel}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(self.dsn)
                lost = asyncio.Event()
                conn.add_termination_listener(lambda _c, _lost=lost: _lost.set())

                def on_notify(_c: object, _pid: int, _ch: str, payload: str) -> None:
                    asyncio.get_running_loop().create_task(self._dispatch(payload))

                await conn.add_listener(self.channel, on_notify)
                self.connected.set()
                backoff = 1.0
                log.info("listening", extra={"fields": {"channel": self.channel}})
                await lost.wait()
            except asyncio.CancelledError:
                if conn is not None:
                    await conn.close()
                raise
            except (OSError, asyncpg.PostgresError) as exc:
                log.warning("listener connection failed; retrying",
                            extra={"fields": {"channel": self.channel, "error": str(exc)}})
            finally:
                self.connected.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _dispatch(self, payload: str) -> None:
        try:
            await self.handler(payload)
        except Exception:
            log.exception("notification handler failed", extra={"fields": {"channel": self.channel}})
