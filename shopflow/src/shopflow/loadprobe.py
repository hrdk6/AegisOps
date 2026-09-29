"""Sandbox load probe used by AegisOps remediation simulations.

Replays identical synthetic traffic against the baseline and candidate clones
and writes a compact JSON result (Kubernetes termination message, <4 KiB):
{"baseline": {...}, "candidate": {...}}. Errors are 5xx responses, timeouts
and connection failures; 4xx responses are valid application answers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from typing import Any

import httpx

_RSS = re.compile(r"^process_resident_memory_bytes\s+([0-9.e+]+)$", re.M)
_CPU = re.compile(r"^process_cpu_seconds_total\s+([0-9.e+]+)$", re.M)


async def scrape(client: httpx.AsyncClient, base: str) -> tuple[float, float] | None:
    try:
        text = (await client.get(base + "/metrics", timeout=2.0)).text
    except httpx.HTTPError:
        return None
    rss, cpu = _RSS.search(text), _CPU.search(text)
    if not rss or not cpu:
        return None
    return float(rss.group(1)), float(cpu.group(1))


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


async def run_target(base: str, args: argparse.Namespace) -> dict[str, Any]:
    latencies: list[float] = []
    errors = 0
    body = json.loads(args.body) if args.body else None
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=httpx.Timeout(3.0, connect=1.0)) as client:
        # Wait until the target answers (the baseline may be crash-looping).
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                await client.get(base + "/healthz", timeout=1.0)
                break
            except httpx.HTTPError:
                await asyncio.sleep(0.5)
        before = await scrape(client, base)

        async def one() -> None:
            nonlocal errors
            async with sem:
                start = time.perf_counter()
                try:
                    resp = await client.request(args.method, base + args.path, json=body)
                    if resp.status_code >= 500:
                        errors += 1
                except httpx.HTTPError:
                    errors += 1
                latencies.append((time.perf_counter() - start) * 1000)

        interval = 1.0 / args.rps
        tasks: list[asyncio.Task[None]] = []
        started = time.perf_counter()
        n = int(args.rps * args.duration)
        for i in range(n):
            tasks.append(asyncio.create_task(one()))
            await asyncio.sleep(max(0.0, started + (i + 1) * interval - time.perf_counter()))
        await asyncio.gather(*tasks)
        elapsed = time.perf_counter() - started
        after = await scrape(client, base)
    result: dict[str, Any] = {
        "requests": len(latencies), "errors": errors,
        "errorRate": round(errors / len(latencies), 4) if latencies else 1.0,
        "p50Ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        "p95Ms": round(percentile(latencies, 0.95), 1), "p99Ms": round(percentile(latencies, 0.99), 1),
        "throughputRps": round(len(latencies) / elapsed, 2) if elapsed else 0.0,
        "cpuMillicores": 0.0, "memoryMb": 0.0, "memoryGrowthMb": 0.0,
    }
    if before and after:
        result["cpuMillicores"] = round((after[1] - before[1]) / elapsed * 1000, 1)
        result["memoryMb"] = round(after[0] / 1e6, 1)
        result["memoryGrowthMb"] = round((after[0] - before[0]) / 1e6, 1)
    return result


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for spec in args.targets.split(","):
        name, _, url = spec.partition("=")
        out[name] = await run_target(url.rstrip("/"), args)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--targets", required=True, help="name=url,name=url")
    p.add_argument("--path", required=True)
    p.add_argument("--method", default="GET", choices=["GET", "POST"])
    p.add_argument("--body", default="")
    p.add_argument("--rps", type=float, default=20)
    p.add_argument("--duration", type=float, default=15)
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--output", default="-")
    args = p.parse_args()
    result = asyncio.run(main_async(args))
    doc = json.dumps(result, separators=(",", ":"))
    if args.output == "-":
        print(doc)
    else:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(doc)
        print(doc)


if __name__ == "__main__":
    main()
