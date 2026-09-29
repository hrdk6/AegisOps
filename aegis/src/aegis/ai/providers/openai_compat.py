"""OpenAI-compatible chat completions (OpenAI, Groq, NVIDIA NIM, Ollama, ...)."""

from __future__ import annotations

import os
import time

import httpx

from aegis.ai.providers.base import Completion, ProviderError

PRESETS: dict[str, tuple[str, str | None]] = {
    # name: (default base URL, API key env var)
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
}


class OpenAICompatibleProvider:
    def __init__(self, name: str, base_url: str | None = None, api_key: str | None = None) -> None:
        if name not in PRESETS:
            raise ValueError(f"unknown OpenAI-compatible provider {name}")
        default_url, key_env = PRESETS[name]
        env_url = os.environ.get(f"{name.upper()}_BASE_URL")
        self.name = name
        self.base_url = (base_url or env_url or default_url).rstrip("/")
        self._key = api_key if api_key is not None else (os.environ.get(key_env, "") if key_env else "")
        self._needs_key = key_env is not None
        self._explicit = env_url is not None or base_url is not None
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0))

    def configured(self) -> bool:
        if self._needs_key:
            return bool(self._key)
        return self._explicit  # ollama: only when a base URL is explicitly configured

    async def aclose(self) -> None:
        await self.http.aclose()

    async def complete(self, model: str, system: str, user: str, *, max_tokens: int, temperature: float,
                       timeout: float) -> Completion:
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        body = {
            "model": model, "temperature": temperature, "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        start = time.perf_counter()
        try:
            resp = await self.http.post(f"{self.base_url}/chat/completions", json=body, headers=headers, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ProviderError(f"{self.name} timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"{self.name} transport error: {type(exc).__name__}", retryable=True) from exc
        latency = (time.perf_counter() - start) * 1000
        if resp.status_code >= 400:
            retryable = resp.status_code in (408, 409, 429) or resp.status_code >= 500
            raise ProviderError(f"{self.name} HTTP {resp.status_code}", retryable=retryable, status=resp.status_code)
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.name} returned an unexpected payload", retryable=True) from exc
        usage = data.get("usage") or {}
        return Completion(text=text, model=data.get("model", model), prompt_tokens=int(usage.get("prompt_tokens") or 0),
                          completion_tokens=int(usage.get("completion_tokens") or 0), latency_ms=latency)
