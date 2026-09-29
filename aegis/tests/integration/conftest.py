"""PostgreSQL-backed integration tests.

Uses AEGIS_TEST_DATABASE_URL when set; otherwise starts a disposable
postgres:16-alpine container on a random localhost port (skipped when Docker
is unavailable) and migrates it with the production Alembic migrations.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
from collections.abc import AsyncIterator, Iterator

import pytest

from aegis.db.migrate import upgrade
from aegis.db.session import Database

pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    dsn = os.environ.get("AEGIS_TEST_DATABASE_URL")
    container = None
    if not dsn:
        if not shutil.which("docker"):
            pytest.skip("integration tests need Docker or AEGIS_TEST_DATABASE_URL")
        port, password = _free_port(), secrets.token_hex(12)
        container = f"aegis-it-{secrets.token_hex(4)}"
        r = subprocess.run(["docker", "run", "-d", "--rm", "--name", container, "-e", f"POSTGRES_PASSWORD={password}",
                            "-e", "POSTGRES_DB=aegis_test", "-p", f"127.0.0.1:{port}:5432", "postgres:16-alpine"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            pytest.skip(f"cannot start PostgreSQL container: {r.stderr.strip()[:200]}")
        dsn = f"postgresql+asyncpg://postgres:{password}@127.0.0.1:{port}/aegis_test"
    try:
        upgrade(dsn, attempts=45)
        yield dsn
    finally:
        if container:
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=60)


@pytest.fixture
async def db(pg_dsn: str) -> AsyncIterator[Database]:
    database = Database(pg_dsn, pool_size=2)
    yield database
    await database.dispose()
