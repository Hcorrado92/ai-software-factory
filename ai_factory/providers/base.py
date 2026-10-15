"""Provider contract. The supervisor only depends on this module, never on a concrete vendor.

To add GPT, Gemini, etc.: subclass ``AIProvider``, translate the vendor's
response into ``ProviderResult`` / ``ProviderError`` (always reporting
``Usage``), and register a factory in ``ProviderRegistry``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_factory.models import Usage


@dataclass(frozen=True)
class ProviderRequest:
    task_id: str
    attempt: int
    prompt: str
    workspace: Path
    timeout_s: float
    max_budget_usd: float | None = None


@dataclass(frozen=True)
class ProviderResult:
    output: str
    usage: Usage = field(default_factory=Usage)
    metadata: dict[str, Any] = field(default_factory=dict)


class ProviderError(Exception):
    """The provider could not complete the request.

    ``retryable`` tells the supervisor whether another attempt makes sense
    (timeouts, transient API errors) or not (missing binary, policy violation).
    ``usage`` carries any consumption incurred before the failure so it is
    still accounted for.
    """

    def __init__(self, message: str, *, retryable: bool = True, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.usage = usage or Usage()


class AIProvider(ABC):
    name: str

    @abstractmethod
    def run(self, request: ProviderRequest) -> ProviderResult:
        """Execute the request inside ``request.workspace``. Raise ``ProviderError`` on failure."""
