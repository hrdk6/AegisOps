"""End-to-end tests against the running kind environment (python scripts/devctl.py up).

Opt-in: AEGIS_E2E=1, AEGIS_API_URL (default http://localhost:8000) and AEGIS_TOKEN (admin).
They inject faults into the demo namespace, so never run them against anything else.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import httpx
import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture
async def api() -> AsyncIterator[httpx.AsyncClient]:
    if os.environ.get("AEGIS_E2E") != "1" or not os.environ.get("AEGIS_TOKEN"):
        pytest.skip("set AEGIS_E2E=1 and AEGIS_TOKEN to run end-to-end tests against the kind environment")
    async with httpx.AsyncClient(base_url=os.environ.get("AEGIS_API_URL", "http://localhost:8000"), timeout=30,
                                 headers={"Authorization": f"Bearer {os.environ['AEGIS_TOKEN']}"}) as c:
        yield c
