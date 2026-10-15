from __future__ import annotations

from collections.abc import Callable

from ai_factory.providers.base import AIProvider


class UnknownProviderError(KeyError):
    pass


class ProviderRegistry:
    """Maps provider names (as stored on tasks) to lazily-built provider instances."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], AIProvider]] = {}
        self._instances: dict[str, AIProvider] = {}

    def register(self, name: str, factory: Callable[[], AIProvider]) -> None:
        self._factories[name] = factory
        self._instances.pop(name, None)

    def register_instance(self, provider: AIProvider) -> None:
        self._factories[provider.name] = lambda: provider
        self._instances[provider.name] = provider

    def get(self, name: str) -> AIProvider:
        if name not in self._instances:
            try:
                factory = self._factories[name]
            except KeyError:
                raise UnknownProviderError(name) from None
            self._instances[name] = factory()
        return self._instances[name]

    def names(self) -> list[str]:
        return sorted(self._factories)
