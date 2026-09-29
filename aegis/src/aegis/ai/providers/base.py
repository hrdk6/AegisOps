"""LLM provider abstraction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Completion:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool = True, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class Provider(Protocol):
    name: str

    def configured(self) -> bool: ...

    async def complete(self, model: str, system: str, user: str, *, max_tokens: int, temperature: float,
                       timeout: float) -> Completion: ...

    async def aclose(self) -> None: ...
