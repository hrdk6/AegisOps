"""Google Gemini (generateContent) with JSON output mode."""

from __future__ import annotations

import os
import time

import httpx

from aegis.ai.providers.base import Completion, ProviderError


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        self._key = api_key if api_key is not None else (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", ""))
        self.base_url = (base_url or os.environ.get("GEMINI_BASE_URL") or
                         "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))

    def configured(self) -> bool:
        return bool(self._key)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def complete(self, model: str, system: str, user: str, *, max_tokens: int, temperature: float,
                       timeout: float) -> Completion:
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens,
                                 "responseMimeType": "application/json"},
        }
        # The key travels in a header, never in the URL (URLs end up in logs).
        headers = {"x-goog-api-key": self._key, "Content-Type": "application/json"}
        start = time.perf_counter()
        try:
            resp = await self.http.post(f"{self.base_url}/models/{model}:generateContent", json=body,
                                        headers=headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ProviderError("gemini timed out") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"gemini transport error: {type(exc).__name__}") from exc
        latency = (time.perf_counter() - start) * 1000
        if resp.status_code >= 400:
            retryable = resp.status_code in (408, 429) or resp.status_code >= 500
            raise ProviderError(f"gemini HTTP {resp.status_code}", retryable=retryable, status=resp.status_code)
        try:
            data = resp.json()
            text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("gemini returned an unexpected payload") from exc
        usage = data.get("usageMetadata") or {}
        return Completion(text=text, model=model, prompt_tokens=int(usage.get("promptTokenCount") or 0),
                          completion_tokens=int(usage.get("candidatesTokenCount") or 0), latency_ms=latency)
