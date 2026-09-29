"""shop-network: a small TCP network-path emulator between ShopFlow services.

Every proxied path (e.g. order-service -> payment-service) can be degraded at
runtime without touching either service, emulating what a real network fault
looks like from both ends: the caller sees latency, resets or silence while
the callee's own metrics stay healthy.

Control API (Toxiproxy-compatible subset, port 8474):
  GET    /proxies
  POST   /proxies/{proxy}/toxics        {"name", "type": latency|reset_peer|timeout, "attributes", "toxicity"}
  DELETE /proxies/{proxy}/toxics/{name}
Faults apply to new *and* established connections.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest
from pydantic import BaseModel, Field

log = logging.getLogger("shop-network")
CONNS = Counter("netem_connections_total", "Proxied connections", ["proxy", "outcome"])


class Toxic(BaseModel):
    name: str = Field(max_length=80)
    type: str = Field(pattern="^(latency|reset_peer|timeout)$")
    stream: str = "downstream"
    toxicity: float = Field(default=1.0, ge=0.0, le=1.0)
    attributes: dict[str, float] = Field(default_factory=dict)


class Proxy:
    def __init__(self, name: str, listen: str, upstream: str) -> None:
        self.name = name
        self.listen_port = int(listen.rsplit(":", 1)[1])
        host, port = upstream.rsplit(":", 1)
        self.upstream = (host, int(port))
        self.upstream_raw = upstream
        self.toxics: dict[str, Toxic] = {}
        self.server: asyncio.Server | None = None

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "listen": f"0.0.0.0:{self.listen_port}", "upstream": self.upstream_raw, "enabled": True,
                "toxics": [t.model_dump() for t in self.toxics.values()]}

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, "0.0.0.0", self.listen_port)  # noqa: S104

    def _active(self, kind: str) -> list[Toxic]:
        return [t for t in self.toxics.values() if t.type == kind]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(*self.upstream), timeout=3)
        except (OSError, TimeoutError):
            CONNS.labels(self.name, "upstream_unreachable").inc()
            writer.close()
            return
        CONNS.labels(self.name, "accepted").inc()
        tasks = [asyncio.create_task(self._pipe(reader, uw, writer, "upstream")),
                 asyncio.create_task(self._pipe(ur, writer, uw, "downstream"))]
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for w in (writer, uw):
            w.close()

    async def _pipe(self, src: asyncio.StreamReader, dst: asyncio.StreamWriter, peer: asyncio.StreamWriter,
                    direction: str) -> None:
        try:
            while data := await src.read(65536):
                if self._active("timeout"):
                    continue  # black hole: bytes are dropped, the connection stays open
                if direction == "upstream":
                    for t in self._active("reset_peer"):
                        if random.random() < t.toxicity:
                            CONNS.labels(self.name, "reset").inc()
                            for w in (dst, peer):
                                w.transport.abort()  # RST, like a lossy path tearing down the flow
                            return
                if direction == "downstream":
                    for t in self._active("latency"):
                        if random.random() < t.toxicity:
                            lat = t.attributes.get("latency", 0) + random.uniform(-1, 1) * t.attributes.get("jitter", 0)
                            await asyncio.sleep(max(0.0, lat) / 1000)
                dst.write(data)
                await dst.drain()
        except (ConnectionError, OSError):
            pass


def create_app(proxies: dict[str, Proxy]) -> FastAPI:
    app = FastAPI(title="shop-network", docs_url=None, redoc_url=None)

    @app.get("/version")
    async def version() -> dict[str, str]:
        return {"version": "shopflow-netem-1"}

    @app.get("/proxies")
    async def list_proxies() -> dict[str, Any]:
        return {n: p.describe() for n, p in proxies.items()}

    @app.post("/proxies/{name}/toxics")
    async def add_toxic(name: str, toxic: Toxic) -> dict[str, Any]:
        if name not in proxies:
            raise HTTPException(404, "unknown proxy")
        proxies[name].toxics[toxic.name] = toxic
        log.info(json.dumps({"msg": "toxic added", "proxy": name, "toxic": toxic.model_dump()}))
        return toxic.model_dump()

    @app.delete("/proxies/{name}/toxics/{toxic}", status_code=204)
    async def delete_toxic(name: str, toxic: str) -> None:
        if name not in proxies or proxies[name].toxics.pop(toxic, None) is None:
            raise HTTPException(404, "unknown toxic")
        log.info(json.dumps({"msg": "toxic removed", "proxy": name, "toxic": toxic}))

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


async def main(spec: list[dict[str, str]]) -> None:
    proxies = {p["name"]: Proxy(p["name"], p["listen"], p["upstream"]) for p in spec}
    for p in proxies.values():
        await p.start()
    server = uvicorn.Server(uvicorn.Config(create_app(proxies), host="0.0.0.0", port=8474, log_level="warning",  # noqa: S104
                                           access_log=False))
    await server.serve()


def create_app_entry() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    with open(os.environ.get("NETEM_CONFIG", "/config/network.json"), encoding="utf-8") as fh:
        spec = json.load(fh)
    asyncio.run(main(spec))
