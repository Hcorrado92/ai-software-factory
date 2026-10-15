"""Scriptable in-process provider for tests and dry runs. Never calls a real model."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from typing import Union

from ai_factory.models import Usage
from ai_factory.providers.base import AIProvider, ProviderRequest, ProviderResult

Response = Union[ProviderResult, Exception, Callable[[ProviderRequest], ProviderResult]]


class MockProvider(AIProvider):
    """Returns scripted responses in order; once exhausted, echoes a zero-cost success.

    A response may be a ``ProviderResult``, an exception instance (raised), or a
    callable receiving the request (useful to simulate edits in the workspace).
    """

    def __init__(self, name: str = "mock", responses: Iterable[Response] = ()) -> None:
        self.name = name
        self._responses: deque[Response] = deque(responses)
        self.calls: list[ProviderRequest] = []

    def run(self, request: ProviderRequest) -> ProviderResult:
        self.calls.append(request)
        if not self._responses:
            return ProviderResult(output=f"[mock] {request.prompt[:200]}", usage=Usage())
        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(request)
        return response
