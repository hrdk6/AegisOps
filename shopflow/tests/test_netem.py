"""The network fault proxy must behave like a real degraded path: faults are visible to the
caller, apply to established connections, and disappear completely when removed (the reason
it replaced Toxiproxy, which kept connections wedged after resets under load)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest

from shopflow.netem import Proxy, Toxic, create_app

pytestmark = pytest.mark.asyncio


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(1024):
        writer.write(data)
        await writer.drain()
    writer.close()


@pytest.fixture
async def proxy() -> AsyncIterator[Proxy]:
    upstream = await asyncio.start_server(_echo, "127.0.0.1", 0)
    port = upstream.sockets[0].getsockname()[1]
    p = Proxy("order-to-payment", "127.0.0.1:0", f"127.0.0.1:{port}")
    await p.start()
    yield p
    assert p.server is not None
    p.server.close()
    upstream.close()


async def _connect(p: Proxy) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    assert p.server is not None
    return await asyncio.open_connection("127.0.0.1", p.server.sockets[0].getsockname()[1])


async def _roundtrip(r: asyncio.StreamReader, w: asyncio.StreamWriter, payload: bytes = b"ping") -> tuple[bytes, float]:
    t0 = time.monotonic()
    w.write(payload)
    await w.drain()
    data = await asyncio.wait_for(r.readexactly(len(payload)), timeout=2)
    return data, time.monotonic() - t0


async def test_passthrough_without_faults(proxy: Proxy) -> None:
    r, w = await _connect(proxy)
    data, took = await _roundtrip(r, w)
    assert data == b"ping" and took < 0.2
    w.close()


async def test_latency_applies_to_established_connections_and_is_removable(proxy: Proxy) -> None:
    r, w = await _connect(proxy)
    await _roundtrip(r, w)  # connection established before the fault
    proxy.toxics["slow"] = Toxic(name="slow", type="latency", attributes={"latency": 300})
    _, slow = await _roundtrip(r, w)
    assert slow >= 0.28
    del proxy.toxics["slow"]
    _, fast = await _roundtrip(r, w)
    assert fast < 0.2, "removing the fault must restore the same connection immediately"
    w.close()


async def test_reset_peer_tears_down_the_connection(proxy: Proxy) -> None:
    proxy.toxics["rst"] = Toxic(name="rst", type="reset_peer", toxicity=1.0)
    r, w = await _connect(proxy)
    w.write(b"charge")
    await w.drain()
    with pytest.raises((ConnectionError, asyncio.IncompleteReadError)):
        await asyncio.wait_for(r.readexactly(6), timeout=2)


async def test_timeout_black_holes_traffic_without_closing(proxy: Proxy) -> None:
    proxy.toxics["hole"] = Toxic(name="hole", type="timeout")
    r, w = await _connect(proxy)
    w.write(b"get")
    await w.drain()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(r.readexactly(3), timeout=0.5)
    del proxy.toxics["hole"]
    data, _ = await _roundtrip(r, w, b"again")
    assert data == b"again", "the connection stays open and recovers once the fault is removed"
    w.close()


async def test_control_api_adds_lists_and_removes_toxics(proxy: Proxy) -> None:
    app = create_app({proxy.name: proxy})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://netem") as c:
        r = await c.post(f"/proxies/{proxy.name}/toxics", json={"name": "lat", "type": "latency", "attributes": {"latency": 50}})
        assert r.status_code == 200
        listed = (await c.get("/proxies")).json()[proxy.name]["toxics"]
        assert [t["name"] for t in listed] == ["lat"]
        assert (await c.post(f"/proxies/{proxy.name}/toxics", json={"name": "x", "type": "shell"})).status_code == 422
        assert (await c.delete(f"/proxies/{proxy.name}/toxics/lat")).status_code == 204
        assert (await c.delete(f"/proxies/{proxy.name}/toxics/lat")).status_code == 404
        assert (await c.post("/proxies/unknown/toxics", json={"name": "a", "type": "timeout"})).status_code == 404
