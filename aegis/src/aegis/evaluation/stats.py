"""Uncertainty estimates for benchmark metrics."""

from __future__ import annotations

import math
import random
import statistics


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (robust for small n)."""
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def bootstrap_mean(values: list[float], iterations: int = 2000, seed: int = 7) -> tuple[float, float, float]:
    """Mean with a percentile bootstrap 95% CI (deterministic seed)."""
    if not values:
        return (math.nan, math.nan, math.nan)
    if len(values) == 1:
        return (values[0], values[0], values[0])
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(values, k=len(values))) for _ in range(iterations))
    return (statistics.fmean(values), means[int(0.025 * iterations)], means[int(0.975 * iterations) - 1])


def rate(flags: list[bool]) -> dict[str, float | int]:
    k, n = sum(flags), len(flags)
    lo, hi = wilson(k, n)
    return {"value": round(k / n, 4) if n else None, "k": k, "n": n, "ci95": [round(lo, 4), round(hi, 4)]}  # type: ignore[dict-item]


def duration(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"mean": None, "median": None, "p90": None, "n": 0, "ci95": None}  # type: ignore[dict-item]
    mean, lo, hi = bootstrap_mean(values)
    ordered = sorted(values)
    return {"mean": round(mean, 1), "median": round(statistics.median(values), 1),
            "p90": round(ordered[min(len(ordered) - 1, int(0.9 * len(ordered)))], 1), "n": len(values),
            "ci95": [round(lo, 1), round(hi, 1)]}  # type: ignore[dict-item]


def brier(pairs: list[tuple[float, bool]]) -> float | None:
    """Brier score of confidence vs correctness (lower is better calibrated)."""
    if not pairs:
        return None
    return round(sum((c - float(ok)) ** 2 for c, ok in pairs) / len(pairs), 4)
