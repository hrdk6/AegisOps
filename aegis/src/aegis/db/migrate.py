"""Run database migrations: `aegis-migrate` (used as an init step)."""

from __future__ import annotations

import time
from pathlib import Path

from alembic import command
from alembic.config import Config

from aegis.config import get_settings


def alembic_config(dsn: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", dsn.replace("%", "%%"))
    return cfg


def upgrade(dsn: str, attempts: int = 30) -> None:
    """Upgrade to head, retrying while the database starts."""
    last: Exception | None = None
    for _ in range(attempts):
        try:
            command.upgrade(alembic_config(dsn), "head")
            return
        except Exception as exc:  # database not ready yet
            last = exc
            time.sleep(2)
    raise RuntimeError(f"migrations failed: {last}")


def run() -> None:
    upgrade(get_settings().dsn)
    print('{"level":"info","msg":"database migrated to head","service":"aegis-migrate"}')
