"""PostgreSQL pool and Redis cache wrappers with operational metrics."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from redis.exceptions import RedisError

from shopflow.config import Settings
from shopflow.errors import CacheUnavailableError, DependencyError, PoolExhaustedError
from shopflow.telemetry import (
    CACHE_OPS,
    DB_ACQUIRE_SECONDS,
    DB_ACQUIRE_TIMEOUTS,
    DB_POOL_IN_USE,
    DB_POOL_SIZE,
    DB_POOL_WAITING,
    DEP_DURATION,
    DEP_REQUESTS,
    log_fields,
)

log = logging.getLogger("shopflow.datastores")


class Database:
    """asyncpg pool with explicit acquisition accounting.

    The in-use / waiting gauges and acquire timeouts are the signals an
    operator needs to recognise connection-pool exhaustion.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.pool: asyncpg.Pool | None = None
        self.in_use = 0
        self.waiting = 0
        self._leaked: list[asyncpg.Connection] = []
        DB_POOL_SIZE.labels(settings.service).set(settings.db_pool_size)

    async def connect(self, schema: str, attempts: int = 60) -> None:
        last: Exception | None = None
        for _ in range(attempts):
            try:
                self.pool = await asyncpg.create_pool(
                    host=self.settings.db_host, port=self.settings.db_port, user=self.settings.db_user,
                    password=self.settings.db_password, database=self.settings.db_name,
                    min_size=1, max_size=self.settings.db_pool_size, command_timeout=5,
                )
                async with self.pool.acquire() as conn:
                    await conn.execute(schema)
                log.info("database connected", extra=log_fields(pool_size=self.settings.db_pool_size))
                return
            except (TimeoutError, OSError, asyncpg.PostgresError) as exc:
                last = exc
                await asyncio.sleep(1)
        raise DependencyError(f"database unavailable after {attempts} attempts: {last}")

    async def close(self) -> None:
        if self.pool is not None:
            self.pool.terminate()

    def _publish(self) -> None:
        DB_POOL_IN_USE.labels(self.settings.service).set(self.in_use)
        DB_POOL_WAITING.labels(self.settings.service).set(self.waiting)

    @asynccontextmanager
    async def connection(self, *, leak: bool = False) -> AsyncIterator[asyncpg.Connection]:
        if self.pool is None:
            raise DependencyError("database not connected")
        service = self.settings.service
        start = time.perf_counter()
        self.waiting += 1
        self._publish()
        try:
            conn = await self.pool.acquire(timeout=self.settings.db_acquire_timeout)
        except TimeoutError as exc:
            DB_ACQUIRE_TIMEOUTS.labels(service).inc()
            raise PoolExhaustedError(
                f"database pool exhausted: timed out acquiring connection after {self.settings.db_acquire_timeout:.1f}s "
                f"(in_use={self.in_use}/{self.settings.db_pool_size}, waiting={self.waiting})"
            ) from exc
        except (OSError, asyncpg.PostgresError) as exc:
            raise DependencyError(f"database unavailable: {type(exc).__name__}: {str(exc)[:120]}") from exc
        finally:
            self.waiting -= 1
            DB_ACQUIRE_SECONDS.labels(service).observe(time.perf_counter() - start)
            self._publish()
        self.in_use += 1
        self._publish()
        held = time.perf_counter()
        outcome = "ok"
        try:
            yield conn
        except (OSError, asyncpg.PostgresError):
            outcome = "error"
            raise
        finally:
            if not leak:
                DEP_REQUESTS.labels(service, "postgres", outcome).inc()
                DEP_DURATION.labels(service, "postgres").observe(time.perf_counter() - held)
            if leak:
                # The connection is never returned to the pool (bug path).
                self._leaked.append(conn)
            else:
                self.in_use -= 1
                await self.pool.release(conn)
            self._publish()

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        async with self.connection() as conn:
            return await conn.fetch(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        async with self.connection() as conn:
            return await conn.execute(query, *args)


class Cache:
    """Redis wrapper; failures raise CacheUnavailableError and are counted."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = aioredis.from_url(settings.redis_url, socket_timeout=1.0, socket_connect_timeout=1.0,
                                        decode_responses=True)

    async def close(self) -> None:
        await self.client.aclose()

    def _observe(self, start: float, outcome: str) -> None:
        DEP_REQUESTS.labels(self.settings.service, "redis", outcome).inc()
        DEP_DURATION.labels(self.settings.service, "redis").observe(time.perf_counter() - start)

    async def get(self, key: str) -> str | None:
        start = time.perf_counter()
        try:
            value = await self.client.get(key)
        except (RedisError, OSError) as exc:
            CACHE_OPS.labels(self.settings.service, "error").inc()
            self._observe(start, "error")
            raise CacheUnavailableError(f"redis unavailable: {type(exc).__name__}: {str(exc)[:120]}") from exc
        CACHE_OPS.labels(self.settings.service, "hit" if value is not None else "miss").inc()
        self._observe(start, "ok")
        return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        start = time.perf_counter()
        try:
            await self.client.set(key, value, ex=ttl)
            CACHE_OPS.labels(self.settings.service, "write").inc()
            self._observe(start, "ok")
        except (RedisError, OSError) as exc:
            CACHE_OPS.labels(self.settings.service, "error").inc()
            self._observe(start, "error")
            raise CacheUnavailableError(f"redis unavailable: {type(exc).__name__}: {str(exc)[:120]}") from exc
