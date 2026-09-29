"""Model routing with retries, timeouts, validation, fallback and cost tracking.

Routes map a *purpose* (diagnosis, planning, postmortem) to an ordered list of
"provider:model" entries. The router skips unconfigured providers, retries
once with validation feedback on malformed output, falls back to the next
entry on failure, and returns None when every option fails — callers then
continue with the deterministic path. The literal entry "heuristic" means
"no model": it terminates the route explicitly.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from prometheus_client import Counter, Histogram
from pydantic import BaseModel, ValidationError

from aegis.ai.providers.base import Provider, ProviderError
from aegis.ai.providers.gemini import GeminiProvider
from aegis.ai.providers.openai_compat import OpenAICompatibleProvider
from aegis.security.redaction import redact

log = logging.getLogger("aegis.ai.router")
T = TypeVar("T", bound=BaseModel)

LLM_CALLS = Counter("aegis_llm_calls_total", "Model calls", ["purpose", "provider", "model", "status"])
LLM_TOKENS = Counter("aegis_llm_tokens_total", "Model tokens", ["provider", "model", "direction"])
LLM_COST = Counter("aegis_llm_cost_usd_total", "Approximate model cost (USD)", ["provider", "model"])
LLM_LATENCY = Histogram("aegis_llm_latency_seconds", "Model call latency", ["provider"], buckets=(0.5, 1, 2, 5, 10, 20, 40))

DEFAULT_ROUTES: dict[str, list[str]] = {
    "diagnosis": ["openai:gpt-4.1", "gemini:gemini-2.5-pro", "groq:llama-3.3-70b-versatile",
                  "nvidia:meta/llama-3.3-70b-instruct", "ollama:qwen2.5:7b-instruct", "heuristic"],
    "planning": ["openai:gpt-4.1", "gemini:gemini-2.5-pro", "groq:llama-3.3-70b-versatile",
                 "nvidia:meta/llama-3.3-70b-instruct", "ollama:qwen2.5:7b-instruct", "heuristic"],
    "postmortem": ["groq:llama-3.1-8b-instant", "gemini:gemini-2.5-flash", "openai:gpt-4.1-mini",
                   "ollama:qwen2.5:7b-instruct", "heuristic"],
}

# Approximate USD per 1M tokens (input, output). Unknown models cost is not estimated.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.0, 8.0), "gpt-4.1-mini": (0.4, 1.6), "gemini-2.5-pro": (1.25, 10.0), "gemini-2.5-flash": (0.3, 2.5),
    "llama-3.3-70b-versatile": (0.59, 0.79), "llama-3.1-8b-instant": (0.05, 0.08),
}


@dataclass
class CallRecord:
    purpose: str
    provider: str
    model: str
    status: str
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    attempt: int = 1
    fallback: bool = False
    error: str | None = None


@dataclass
class RouterResult(Generic[T]):
    value: T | None
    provider: str | None = None
    model: str | None = None
    calls: list[CallRecord] = field(default_factory=list)
    reason: str | None = None

    @property
    def metadata(self) -> dict[str, Any]:
        return {"provider": self.provider, "model": self.model, "reason": self.reason,
                "calls": [c.__dict__ for c in self.calls],
                "prompt_tokens": sum(c.prompt_tokens for c in self.calls),
                "completion_tokens": sum(c.completion_tokens for c in self.calls),
                "cost_usd": round(sum(c.cost_usd or 0 for c in self.calls), 6)}


def extract_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


class ModelRouter:
    def __init__(self, routes: dict[str, list[str]] | None = None, providers: dict[str, Provider] | None = None,
                 prices: dict[str, tuple[float, float]] | None = None, timeout: float = 30.0, max_retries: int = 1,
                 max_calls_per_incident: int = 8,
                 recorder: Callable[[str | None, CallRecord], Awaitable[None]] | None = None) -> None:
        self.routes = routes or DEFAULT_ROUTES
        self.providers: dict[str, Provider] = providers if providers is not None else {
            "openai": OpenAICompatibleProvider("openai"), "groq": OpenAICompatibleProvider("groq"),
            "nvidia": OpenAICompatibleProvider("nvidia"), "ollama": OpenAICompatibleProvider("ollama"),
            "gemini": GeminiProvider(),
        }
        self.prices = {**DEFAULT_PRICES, **(prices or {})}
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_calls = max_calls_per_incident
        self.recorder = recorder
        self._calls: dict[str, int] = {}

    @classmethod
    def from_settings(cls, routes_json: str, prices_json: str, timeout: float, max_retries: int, max_calls: int,
                      recorder: Callable[[str | None, CallRecord], Awaitable[None]] | None = None) -> ModelRouter:
        routes = json.loads(routes_json) if routes_json else None
        prices = {k: tuple(v) for k, v in json.loads(prices_json).items()} if prices_json else None
        return cls(routes=routes, prices=prices, timeout=timeout, max_retries=max_retries,  # type: ignore[arg-type]
                   max_calls_per_incident=max_calls, recorder=recorder)

    def resolve(self, purpose: str) -> list[tuple[Provider, str]]:
        out: list[tuple[Provider, str]] = []
        for entry in self.routes.get(purpose, []):
            if entry == "heuristic":
                break
            name, _, model = entry.partition(":")
            p = self.providers.get(name)
            if p is not None and p.configured() and model:
                out.append((p, model))
        return out

    def available(self, purpose: str) -> bool:
        return bool(self.resolve(purpose))

    def status(self) -> dict[str, Any]:
        return {purpose: [f"{p.name}:{m}" for p, m in self.resolve(purpose)] or ["heuristic"] for purpose in self.routes}

    def _cost(self, model: str, pt: int, ct: int) -> float | None:
        price = self.prices.get(model) or self.prices.get(model.split("/")[-1])
        if not price:
            return None
        return (pt * price[0] + ct * price[1]) / 1e6

    async def _record(self, incident_id: str | None, rec: CallRecord) -> None:
        LLM_CALLS.labels(rec.purpose, rec.provider, rec.model, rec.status).inc()
        LLM_TOKENS.labels(rec.provider, rec.model, "prompt").inc(rec.prompt_tokens)
        LLM_TOKENS.labels(rec.provider, rec.model, "completion").inc(rec.completion_tokens)
        if rec.cost_usd:
            LLM_COST.labels(rec.provider, rec.model).inc(rec.cost_usd)
        LLM_LATENCY.labels(rec.provider).observe(rec.latency_ms / 1000)
        log.info("model call", extra={"fields": {"purpose": rec.purpose, "provider": rec.provider, "model": rec.model,
                                                 "status": rec.status, "latency_ms": round(rec.latency_ms),
                                                 "prompt_tokens": rec.prompt_tokens, "completion_tokens": rec.completion_tokens,
                                                 "attempt": rec.attempt, "fallback": rec.fallback}})
        if self.recorder is not None:
            try:
                await self.recorder(incident_id, rec)
            except Exception:  # recording must never break reasoning
                log.exception("failed to persist model call")

    async def structured(self, purpose: str, system: str, user: str, schema: type[T], *, incident_id: str | None = None,
                         validate: Callable[[T], list[str]] | None = None, max_tokens: int = 1500,
                         temperature: float = 0.1) -> RouterResult[T]:
        route = self.resolve(purpose)
        result: RouterResult[T] = RouterResult(value=None)
        if not route:
            result.reason = "no model configured for purpose (deterministic path)"
            return result
        key = incident_id or "_global"
        for idx, (provider, model) in enumerate(route):
            prompt = user
            for attempt in range(1, self.max_retries + 2):
                if self._calls.get(key, 0) >= self.max_calls:
                    result.reason = f"model call budget exhausted ({self.max_calls} per incident)"
                    return result
                self._calls[key] = self._calls.get(key, 0) + 1
                rec = CallRecord(purpose=purpose, provider=provider.name, model=model, status="ok", attempt=attempt,
                                 fallback=idx > 0)
                try:
                    comp = await provider.complete(model, system, prompt, max_tokens=max_tokens,
                                                   temperature=temperature, timeout=self.timeout)
                except ProviderError as exc:
                    rec.status, rec.error = "error", redact(str(exc))
                    result.calls.append(rec)
                    await self._record(incident_id, rec)
                    if not exc.retryable:
                        break
                    continue
                rec.latency_ms, rec.prompt_tokens, rec.completion_tokens = comp.latency_ms, comp.prompt_tokens, comp.completion_tokens
                rec.cost_usd = self._cost(model, comp.prompt_tokens, comp.completion_tokens)
                errors: list[str] = []
                parsed: T | None = None
                try:
                    parsed = schema.model_validate(extract_json(comp.text))
                    errors = validate(parsed) if validate else []
                except (ValueError, ValidationError) as exc:
                    errors = [redact(str(exc))[:600]]
                if parsed is not None and not errors:
                    result.calls.append(rec)
                    await self._record(incident_id, rec)
                    result.value, result.provider, result.model = parsed, provider.name, model
                    return result
                rec.status, rec.error = "invalid_output", "; ".join(errors)[:600]
                result.calls.append(rec)
                await self._record(incident_id, rec)
                prompt = (user + "\n\nYour previous answer was rejected by the validator:\n- " + "\n- ".join(errors[:6])
                          + "\nReturn ONLY a corrected JSON object that satisfies the schema and rules.")
        result.reason = "all configured models failed or produced invalid output"
        return result

    async def aclose(self) -> None:
        for p in self.providers.values():
            await p.aclose()
