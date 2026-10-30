"""Shared test fixtures and helpers for provider-failover module tests.

Provides:
- MockProvider: Configurable mock LLM provider with pop-next-or-reuse-last
  response pattern, implementing complete/stream/parse_tool_calls/get_info.
- Helper factories for constructing test tiers, failover instances, etc.
- sys.path configuration for modules/provider-failover.
"""

from __future__ import annotations

import sys
import os
from dataclasses import dataclass, replace
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# sys.path: make modules/provider-failover importable from tests
# ---------------------------------------------------------------------------
_BUNDLE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODULE_PATH = os.path.join(_BUNDLE_ROOT, "modules", "provider-failover")
if _MODULE_PATH not in sys.path:
    sys.path.insert(0, _MODULE_PATH)

from provider_failover.failover import FailoverProvider, TierConfig  # noqa: E402



# ---------------------------------------------------------------------------
# Session-wide telemetry kill-switch
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_telemetry():
    """Prevent tests from POSTing to real Supabase.

    Reset module state for every test: enabled transport tests reload it with
    fictional configuration and mocks, which must not leak into later tests.
    """
    try:
        from provider_failover import telemetry

        telemetry._ENABLED = False
        telemetry._ENDPOINT = ""
        telemetry._EVENTS_ENDPOINT = ""
    except ImportError:
        pass
    yield


# ---------------------------------------------------------------------------
# MockRequest
# ---------------------------------------------------------------------------


@dataclass
class MockRequest:
    """Minimal request object supporting Pydantic-style model_copy()."""

    model: str
    messages: list[dict[str, Any]]

    def model_copy(self, *, update: dict[str, Any]) -> "MockRequest":
        """Return a new MockRequest with the given fields updated."""
        return replace(self, **update)


# ---------------------------------------------------------------------------
# MockProvider
# ---------------------------------------------------------------------------


class MockProvider:
    """Configurable mock LLM provider for testing.

    Responses are consumed in order (pop-next); once the list is exhausted,
    the last response is reused for all subsequent calls.

    If a response entry is an exception, it is raised instead of returned.
    """

    def __init__(self, responses: list[Any]) -> None:
        """Initialize with a list of responses.

        Args:
            responses: Ordered list of response objects (or exceptions) to
                return from complete(). Last entry is reused when exhausted.
        """
        if not responses:
            raise ValueError("responses must be non-empty")
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def _next_response(self) -> Any:
        """Pop the next response, or reuse last if exhausted."""
        if len(self._responses) > 1:
            return self._responses.pop(0)
        return self._responses[0]

    async def complete(self, request: Any) -> Any:
        """Simulate a completion request.

        Args:
            request: The request object (dataclass, Pydantic model, or dict).

        Returns:
            The next configured response, or raises if the response is an exception.
        """
        self.calls.append({"method": "complete", "request": request})
        response = self._next_response()
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, type) and issubclass(response, BaseException):
            raise response()
        return response

    async def stream(self, request: Any):
        """Simulate a streaming completion request.

        Yields chunks from the configured response if it is iterable, otherwise
        yields the whole response as a single chunk.
        """
        self.calls.append({"method": "stream", "request": request})
        response = self._next_response()
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, type) and issubclass(response, BaseException):
            raise response()
        iterable_response: Any = response
        if hasattr(iterable_response, "__iter__") and not isinstance(
            iterable_response, (str, bytes, dict)
        ):
            for chunk in iterable_response:
                yield chunk
        else:
            yield iterable_response

    def parse_tool_calls(self, response: Any) -> list[dict[str, Any]]:
        """Parse tool calls from a response (stub — returns empty list)."""
        return []

    def get_info(self) -> dict[str, Any]:
        """Return provider info (stub)."""
        return {"type": "mock", "calls": len(self.calls)}


# ---------------------------------------------------------------------------
# Response / request factories
# ---------------------------------------------------------------------------


def _make_response(text: str, input_tokens: int = 100) -> dict[str, Any]:
    """Build a minimal response dict for use with MockProvider.

    Args:
        text: The response text content.
        input_tokens: Simulated input token count.

    Returns:
        A dict with content and usage fields.
    """
    return {
        "content": text,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": len(text.split()),
        },
    }


def _make_request(model: str = "test-model") -> dict[str, Any]:
    """Build a minimal request dict.

    # Used by future failover/stream tests that pass raw dicts.

    Args:
        model: The model identifier.

    Returns:
        A dict with messages and model fields.
    """
    return {
        "model": model,
        "messages": [{"role": "user", "content": "Hello"}],
    }


# ---------------------------------------------------------------------------
# Tier / FailoverProvider factories
# ---------------------------------------------------------------------------


def _make_tier(
    name: str,
    responses: list[Any],
    model: str = "test-model",
    label: str | None = None,
    context_window: int = 200_000,
) -> tuple[TierConfig, MockProvider]:
    """Create a (TierConfig, MockProvider) pair for use in tests.

    Args:
        name: Name used as provider label if label is not provided.
        responses: Ordered list of responses for the MockProvider.
        model: Model identifier for the TierConfig.
        label: Human-readable label (defaults to name).
        context_window: Context window size for the TierConfig.

    Returns:
        Tuple of (TierConfig, MockProvider) where TierConfig.provider is the
        MockProvider instance.
    """
    mock = MockProvider(responses=responses)
    tier = TierConfig(
        provider=mock,
        model=model,
        label=label if label is not None else name,
        context_window=context_window,
    )
    return tier, mock


def _make_failover(
    tiers: list[TierConfig],
    probe_interval: int = 5,
) -> FailoverProvider:
    """Create a FailoverProvider for deterministic tests.

    Args:
        tiers: Ordered list of TierConfig instances.
        probe_interval: Successes before probing primary.

    Returns:
        A FailoverProvider instance.
    """
    fp = FailoverProvider(
        tiers=tiers,
        probe_interval=probe_interval,
    )
    return fp
