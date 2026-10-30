"""Tests for the hooks-telemetry module.

Verifies that the hook module:
- Registers handlers for ALL error/failure events across the ecosystem
- Returns None (no cleanup) when telemetry is not enabled
- on_provider_request records start time and returns action=continue
- on_event extracts the right fields for each event type
- Category mapping works correctly (event_type → event_category)
- llm:response with status="success" is NOT reported
- execution:end with status="success" is NOT reported
- execution:end with status="error" IS reported
- provider:retry payload extraction (attempt, max_retries, delay)
- provider:throttle payload extraction (reason, dimension, remaining, limit)
- tool:error payload extraction (tool_name)
- Telemetry errors don't propagate (fire-and-forget)
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock amplifier_core.models.HookResult so handlers can import it
# ---------------------------------------------------------------------------


class _MockHookResult:
    """Stand-in for amplifier_core.models.HookResult in tests."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __eq__(self, other):
        if isinstance(other, _MockHookResult):
            return self.__dict__ == other.__dict__
        return NotImplemented

    def __repr__(self):
        attrs = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"HookResult({attrs})"


# Inject mock into sys.modules so `from amplifier_core.models import HookResult` works
_mock_amp_core = MagicMock()
_mock_amp_models = MagicMock()
_mock_amp_models.HookResult = _MockHookResult
_mock_amp_core.models = _mock_amp_models
sys.modules.setdefault("amplifier_core", _mock_amp_core)
sys.modules.setdefault("amplifier_core.models", _mock_amp_models)

# ---------------------------------------------------------------------------
# Ensure hooks-telemetry module is importable
# ---------------------------------------------------------------------------
_BUNDLE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOOKS_MODULE_PATH = os.path.join(_BUNDLE_ROOT, "modules", "hooks-telemetry")
if _HOOKS_MODULE_PATH not in sys.path:
    sys.path.insert(0, _HOOKS_MODULE_PATH)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class MockHooksRegistry:
    """Mock hooks registry that records registrations."""

    def __init__(self) -> None:
        self.registered: list[dict[str, Any]] = []

    def register(
        self,
        event: str,
        handler: Callable,
        priority: int = 0,
        name: str | None = None,
    ) -> Callable:
        self.registered.append(
            {"event": event, "handler": handler, "priority": priority, "name": name}
        )

        def unregister() -> None:
            pass

        return unregister


class MockCoordinator:
    """Mock coordinator with a hooks registry."""

    def __init__(self, hooks: Any = None) -> None:
        self.hooks = hooks if hooks is not None else MockHooksRegistry()


def _reload_hooks_telemetry():
    """Force-reimport hooks_telemetry to pick up patched state."""
    import hooks_telemetry as mod

    importlib.reload(mod)
    return mod


def _mock_telemetry_modules(mock_report_event=None):
    """Return a patch dict for sys.modules that mocks provider_failover.telemetry."""
    mock_re = mock_report_event or AsyncMock()
    return {
        "provider_failover": MagicMock(),
        "provider_failover.telemetry": MagicMock(
            _ENABLED=True,
            report_event=mock_re,
        ),
    }


async def _mount_and_get_handlers(mock_report_event=None):
    """Mount the module and return (registry, handlers_by_event, mock_report_event)."""
    registry = MockHooksRegistry()
    coordinator = MockCoordinator(hooks=registry)

    mock_re = mock_report_event or AsyncMock()
    with patch.dict("sys.modules", _mock_telemetry_modules(mock_re)):
        mod = _reload_hooks_telemetry()
        cleanup = await mod.mount(coordinator)

    handlers: dict[str, Callable] = {}
    for reg in registry.registered:
        handlers[reg["event"]] = reg["handler"]

    return registry, handlers, mock_re, cleanup


# ---------------------------------------------------------------------------
# Expected events
# ---------------------------------------------------------------------------

_EXPECTED_EVENTS = [
    "provider:request",
    "provider:error",
    "tool:error",
    "policy:violation",
    "approval:denied",
    "provider:retry",
    "provider:throttle",
    "provider:tool_sequence_repaired",
    "llm:response",
    "execution:end",
    "cancel:completed",
    "provider:cloudflare_challenge",
]


# ---------------------------------------------------------------------------
# mount() — registration tests
# ---------------------------------------------------------------------------


class TestMount:
    @pytest.mark.asyncio
    async def test_registers_all_events(self):
        """mount() should register handlers for all expected events."""
        registry, handlers, _, _ = await _mount_and_get_handlers()

        registered_events = sorted(r["event"] for r in registry.registered)
        expected_sorted = sorted(_EXPECTED_EVENTS)
        assert registered_events == expected_sorted

    @pytest.mark.asyncio
    async def test_registers_correct_count(self):
        """mount() should register exactly 12 handlers (1 timer + 11 events)."""
        registry, _, _, _ = await _mount_and_get_handlers()
        assert len(registry.registered) == 12

    @pytest.mark.asyncio
    async def test_provider_request_has_dedicated_handler(self):
        """provider:request should have a separate timer handler."""
        registry, handlers, _, _ = await _mount_and_get_handlers()

        timer_reg = next(
            r for r in registry.registered if r["event"] == "provider:request"
        )
        assert timer_reg["name"] == "telemetry-timer"

        # All other handlers should be named telemetry-{event}
        for reg in registry.registered:
            if reg["event"] != "provider:request":
                assert reg["name"] == f"telemetry-{reg['event']}"

    @pytest.mark.asyncio
    async def test_all_handlers_priority_99(self):
        """All handlers should be registered at priority 99."""
        registry, _, _, _ = await _mount_and_get_handlers()
        for reg in registry.registered:
            assert reg["priority"] == 99

    @pytest.mark.asyncio
    async def test_returns_cleanup_callable(self):
        """mount() should return a cleanup function."""
        _, _, _, cleanup = await _mount_and_get_handlers()
        assert callable(cleanup)
        cleanup()  # should not raise

    @pytest.mark.asyncio
    async def test_returns_none_when_telemetry_disabled(self):
        """mount() should return None when Supabase is not configured."""
        registry = MockHooksRegistry()
        coordinator = MockCoordinator(hooks=registry)

        with patch.dict("sys.modules", {
            "provider_failover": MagicMock(),
            "provider_failover.telemetry": MagicMock(
                _ENABLED=False,
                report_event=AsyncMock(),
            ),
        }):
            mod = _reload_hooks_telemetry()
            result = await mod.mount(coordinator)

        assert result is None
        assert len(registry.registered) == 0

    @pytest.mark.asyncio
    async def test_returns_none_when_import_fails(self):
        """mount() should return None when provider_failover is not importable."""
        coordinator = MockCoordinator()

        saved = {}
        for key in list(sys.modules.keys()):
            if key.startswith("provider_failover"):
                saved[key] = sys.modules.pop(key)

        try:
            failover_path = os.path.join(_BUNDLE_ROOT, "modules", "provider-failover")
            path_was_present = failover_path in sys.path
            if path_was_present:
                sys.path.remove(failover_path)
            try:
                mod = _reload_hooks_telemetry()
                result = await mod.mount(coordinator)
                assert result is None
            finally:
                if path_was_present:
                    sys.path.insert(0, failover_path)
        finally:
            sys.modules.update(saved)

    @pytest.mark.asyncio
    async def test_returns_none_when_no_hooks_registry(self):
        """mount() should return None when coordinator has no hooks attr."""
        coordinator = MagicMock(spec=[])
        del coordinator.hooks

        with patch.dict("sys.modules", _mock_telemetry_modules()):
            mod = _reload_hooks_telemetry()
            result = await mod.mount(coordinator)

        assert result is None


# ---------------------------------------------------------------------------
# Category mapping tests
# ---------------------------------------------------------------------------


class TestCategoryMapping:
    def test_static_categories(self):
        """Static event types should map to expected categories."""
        mod = _reload_hooks_telemetry()

        cases = {
            "provider:error": "error",
            "tool:error": "error",
            "policy:violation": "error",
            "provider:retry": "retry",
            "provider:throttle": "throttle",
            "approval:denied": "cancel",
            "cancel:completed": "cancel",
            "provider:tool_sequence_repaired": "repair",
            "provider:cloudflare_challenge": "error",
        }
        for event_type, expected_category in cases.items():
            with patch.dict("sys.modules", _mock_telemetry_modules()):
                mod = _reload_hooks_telemetry()
                result = mod.event_category(event_type, {})
            assert result == expected_category, f"Failed for {event_type}"

    def test_llm_response_error_returns_error(self):
        with patch.dict("sys.modules", _mock_telemetry_modules()):
            mod = _reload_hooks_telemetry()
        assert mod.event_category("llm:response", {"status": "error"}) == "error"

    def test_llm_response_success_returns_none(self):
        with patch.dict("sys.modules", _mock_telemetry_modules()):
            mod = _reload_hooks_telemetry()
        assert mod.event_category("llm:response", {"status": "success"}) is None

    def test_execution_end_error_returns_status(self):
        with patch.dict("sys.modules", _mock_telemetry_modules()):
            mod = _reload_hooks_telemetry()
        assert mod.event_category("execution:end", {"status": "error"}) == "status"

    def test_execution_end_cancelled_returns_status(self):
        with patch.dict("sys.modules", _mock_telemetry_modules()):
            mod = _reload_hooks_telemetry()
        assert mod.event_category("execution:end", {"status": "cancelled"}) == "status"

    def test_execution_end_incomplete_returns_status(self):
        with patch.dict("sys.modules", _mock_telemetry_modules()):
            mod = _reload_hooks_telemetry()
        assert mod.event_category("execution:end", {"status": "incomplete"}) == "status"

    def test_execution_end_success_returns_none(self):
        with patch.dict("sys.modules", _mock_telemetry_modules()):
            mod = _reload_hooks_telemetry()
        assert mod.event_category("execution:end", {"status": "success"}) is None

    def test_unknown_event_returns_none(self):
        with patch.dict("sys.modules", _mock_telemetry_modules()):
            mod = _reload_hooks_telemetry()
        assert mod.event_category("unknown:event", {}) is None


# ---------------------------------------------------------------------------
# on_provider_request — timer recording
# ---------------------------------------------------------------------------


class TestOnProviderRequest:
    @pytest.mark.asyncio
    async def test_records_start_time(self):
        """on_provider_request should record a monotonic timestamp."""
        _, handlers, _, _ = await _mount_and_get_handlers()
        handler = handlers["provider:request"]

        before = time.monotonic()
        result = await handler("provider:request", {
            "session_id": "sess-1",
            "iteration": 3,
            "provider": "anthropic",
        })
        after = time.monotonic()

        assert hasattr(result, "action") and result.action == "continue"

    @pytest.mark.asyncio
    async def test_returns_continue_action(self):
        _, handlers, _, _ = await _mount_and_get_handlers()
        handler = handlers["provider:request"]

        result = await handler("provider:request", {
            "session_id": "test", "iteration": 0,
        })
        assert hasattr(result, "action")
        assert result.action == "continue"


# ---------------------------------------------------------------------------
# provider:error — full extraction
# ---------------------------------------------------------------------------


class TestProviderError:
    @pytest.mark.asyncio
    async def test_extracts_all_fields(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        with patch("hooks_telemetry.time") as mock_time:
            mock_time.monotonic.side_effect = [100.0, 100.250]

            await handlers["provider:request"]("provider:request", {
                "session_id": "sess-1", "iteration": 1,
            })
            result = await handlers["provider:error"]("provider:error", {
                "session_id": "sess-1",
                "iteration": 1,
                "provider": "anthropic",
                "model": "claude-opus-4",
                "error": {"type": "OverloadedError", "msg": "Service overloaded"},
                "status_code": 529,
                "retryable": True,
            })
            await asyncio.sleep(0)

        assert hasattr(result, "action") and result.action == "continue"
        mock_re.assert_called_once()
        kw = mock_re.call_args[1]
        assert kw["event_type"] == "provider:error"
        assert kw["event_category"] == "error"
        assert kw["provider"] == "anthropic"
        assert kw["error_type"] == "OverloadedError"
        assert kw["error_message"] == "Service overloaded"
        assert kw["status_code"] == 529
        assert kw["retryable"] is True
        assert kw["latency_ms"] == 250
        assert kw["source"] == "hook"

    @pytest.mark.asyncio
    async def test_handles_missing_start_time(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["provider:error"]("provider:error", {
            "session_id": "orphan", "iteration": 0,
            "provider": "openai",
            "error": {"type": "RateLimitError", "msg": "Too many requests"},
            "status_code": 429,
        })
        await asyncio.sleep(0)

        kw = mock_re.call_args[1]
        assert kw["latency_ms"] is None

    @pytest.mark.asyncio
    async def test_truncates_long_error_messages(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["provider:error"]("provider:error", {
            "provider": "test",
            "error": {"type": "E", "msg": "x" * 1000},
        })
        await asyncio.sleep(0)

        kw = mock_re.call_args[1]
        assert len(kw["error_message"]) == 500

    @pytest.mark.asyncio
    async def test_handles_empty_data(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        result = await handlers["provider:error"]("provider:error", {})
        await asyncio.sleep(0)

        assert hasattr(result, "action") and result.action == "continue"
        kw = mock_re.call_args[1]
        assert kw["error_type"] == "Unknown"

    @pytest.mark.asyncio
    async def test_cleans_up_timer(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        with patch("hooks_telemetry.time") as mt:
            mt.monotonic.side_effect = [1.0, 1.5]
            await handlers["provider:request"]("provider:request", {
                "session_id": "s", "iteration": 1,
            })
            await handlers["provider:error"]("provider:error", {
                "session_id": "s", "iteration": 1,
                "provider": "t", "error": {"type": "E", "msg": "m"},
            })
            await asyncio.sleep(0)

        # Timer consumed, second call should have latency_ms=None
        mock_re.reset_mock()
        await handlers["provider:error"]("provider:error", {
            "session_id": "s", "iteration": 1,
            "provider": "t", "error": {"type": "E", "msg": "m"},
        })
        await asyncio.sleep(0)
        assert mock_re.call_args[1]["latency_ms"] is None


# ---------------------------------------------------------------------------
# tool:error — extraction
# ---------------------------------------------------------------------------


class TestToolError:
    @pytest.mark.asyncio
    async def test_extracts_tool_fields(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["tool:error"]("tool:error", {
            "tool_name": "bash",
            "tool_call_id": "tc_123",
            "error": {"type": "ExecutionError", "msg": "Command failed"},
            "parallel_group_id": "pg_1",
        })
        await asyncio.sleep(0)

        kw = mock_re.call_args[1]
        assert kw["event_type"] == "tool:error"
        assert kw["event_category"] == "error"
        assert kw["tool_name"] == "bash"
        assert kw["error_type"] == "ExecutionError"
        assert kw["error_message"] == "Command failed"


# ---------------------------------------------------------------------------
# provider:retry — extraction
# ---------------------------------------------------------------------------


class TestProviderRetry:
    @pytest.mark.asyncio
    async def test_extracts_retry_fields(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["provider:retry"]("provider:retry", {
            "provider": "anthropic",
            "model": "claude-opus-4",
            "attempt": 2,
            "max_retries": 5,
            "delay": 1.5,
            "retry_after": 3.0,
            "error_type": "OverloadedError",
            "error_message": "529",
        })
        await asyncio.sleep(0)

        kw = mock_re.call_args[1]
        assert kw["event_type"] == "provider:retry"
        assert kw["event_category"] == "retry"
        assert kw["provider"] == "anthropic"
        assert kw["model"] == "claude-opus-4"
        assert kw["attempt"] == 2
        assert kw["max_retries"] == 5
        assert kw["retry_delay"] == 1.5
        assert kw["retry_after"] == 3.0
        assert kw["error_type"] == "OverloadedError"
        assert kw["error_message"] == "529"


# ---------------------------------------------------------------------------
# provider:throttle — extraction
# ---------------------------------------------------------------------------


class TestProviderThrottle:
    @pytest.mark.asyncio
    async def test_extracts_throttle_fields(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["provider:throttle"]("provider:throttle", {
            "provider": "anthropic",
            "model": "claude-opus-4",
            "reason": "input_tokens_low",
            "dimension": "input_tokens",
            "remaining": 100,
            "limit": 10000,
            "ratio": 0.01,
            "delay": 2.5,
        })
        await asyncio.sleep(0)

        kw = mock_re.call_args[1]
        assert kw["event_type"] == "provider:throttle"
        assert kw["event_category"] == "throttle"
        assert kw["throttle_reason"] == "input_tokens_low"
        assert kw["throttle_dimension"] == "input_tokens"
        assert kw["throttle_remaining"] == 100
        assert kw["throttle_limit"] == 10000
        assert kw["throttle_ratio"] == 0.01


# ---------------------------------------------------------------------------
# provider:tool_sequence_repaired — extraction
# ---------------------------------------------------------------------------


class TestToolSequenceRepaired:
    @pytest.mark.asyncio
    async def test_extracts_repair_fields(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["provider:tool_sequence_repaired"](
            "provider:tool_sequence_repaired", {
                "provider": "anthropic",
                "repair_count": 3,
                "repairs": [
                    {"tool_call_id": "tc1", "tool_name": "bash"},
                    {"tool_call_id": "tc2", "tool_name": "read_file"},
                    {"tool_call_id": "tc3", "tool_name": "write_file"},
                ],
            }
        )
        await asyncio.sleep(0)

        kw = mock_re.call_args[1]
        assert kw["event_type"] == "provider:tool_sequence_repaired"
        assert kw["event_category"] == "repair"
        assert kw["error_message"] == "Repaired 3 tool call(s)"
        assert kw["provider"] == "anthropic"


# ---------------------------------------------------------------------------
# policy:violation — extraction
# ---------------------------------------------------------------------------


class TestPolicyViolation:
    @pytest.mark.asyncio
    async def test_extracts_policy_fields(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["policy:violation"]("policy:violation", {
            "policy": "max_tokens_exceeded",
            "message": "Request exceeds maximum token limit",
        })
        await asyncio.sleep(0)

        kw = mock_re.call_args[1]
        assert kw["event_type"] == "policy:violation"
        assert kw["event_category"] == "error"
        assert kw["error_type"] == "max_tokens_exceeded"
        assert kw["error_message"] == "Request exceeds maximum token limit"


# ---------------------------------------------------------------------------
# approval:denied — extraction
# ---------------------------------------------------------------------------


class TestApprovalDenied:
    @pytest.mark.asyncio
    async def test_extracts_denial_fields(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["approval:denied"]("approval:denied", {
            "reason": "User rejected the proposed change",
        })
        await asyncio.sleep(0)

        kw = mock_re.call_args[1]
        assert kw["event_type"] == "approval:denied"
        assert kw["event_category"] == "cancel"
        assert kw["error_type"] == "approval_denied"
        assert kw["error_message"] == "User rejected the proposed change"


# ---------------------------------------------------------------------------
# llm:response — conditional reporting
# ---------------------------------------------------------------------------


class TestLlmResponse:
    @pytest.mark.asyncio
    async def test_reports_error_status(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["llm:response"]("llm:response", {
            "status": "error",
            "provider": "openai",
            "model": "gpt-5.4",
            "error": {"type": "APIError", "msg": "Internal server error"},
        })
        await asyncio.sleep(0)

        mock_re.assert_called_once()
        kw = mock_re.call_args[1]
        assert kw["event_type"] == "llm:response"
        assert kw["event_category"] == "error"
        assert kw["error_type"] == "APIError"
        assert kw["execution_status"] == "error"

    @pytest.mark.asyncio
    async def test_skips_success_status(self):
        """llm:response with status=success should NOT be reported."""
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        result = await handlers["llm:response"]("llm:response", {
            "status": "success",
            "provider": "anthropic",
            "model": "claude-opus-4",
        })
        await asyncio.sleep(0)

        assert hasattr(result, "action") and result.action == "continue"
        mock_re.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_empty_status(self):
        """llm:response with no status should NOT be reported."""
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["llm:response"]("llm:response", {
            "provider": "test",
        })
        await asyncio.sleep(0)

        mock_re.assert_not_called()


# ---------------------------------------------------------------------------
# execution:end — conditional reporting
# ---------------------------------------------------------------------------


class TestExecutionEnd:
    @pytest.mark.asyncio
    async def test_reports_error_status(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["execution:end"]("execution:end", {
            "status": "error",
            "error": "Provider exhausted all retries",
        })
        await asyncio.sleep(0)

        mock_re.assert_called_once()
        kw = mock_re.call_args[1]
        assert kw["event_type"] == "execution:end"
        assert kw["event_category"] == "status"
        assert kw["execution_status"] == "error"
        assert kw["error_message"] == "Provider exhausted all retries"

    @pytest.mark.asyncio
    async def test_reports_cancelled_status(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["execution:end"]("execution:end", {
            "status": "cancelled",
        })
        await asyncio.sleep(0)

        mock_re.assert_called_once()
        kw = mock_re.call_args[1]
        assert kw["execution_status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_reports_incomplete_status(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["execution:end"]("execution:end", {
            "status": "incomplete",
        })
        await asyncio.sleep(0)

        mock_re.assert_called_once()
        kw = mock_re.call_args[1]
        assert kw["execution_status"] == "incomplete"

    @pytest.mark.asyncio
    async def test_skips_success_status(self):
        """execution:end with status=success should NOT be reported."""
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        result = await handlers["execution:end"]("execution:end", {
            "status": "success",
        })
        await asyncio.sleep(0)

        assert hasattr(result, "action") and result.action == "continue"
        mock_re.assert_not_called()


# ---------------------------------------------------------------------------
# cancel:completed — extraction
# ---------------------------------------------------------------------------


class TestCancelCompleted:
    @pytest.mark.asyncio
    async def test_extracts_cancellation_fields(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["cancel:completed"]("cancel:completed", {
            "reason": "User pressed Ctrl+C",
        })
        await asyncio.sleep(0)

        kw = mock_re.call_args[1]
        assert kw["event_type"] == "cancel:completed"
        assert kw["event_category"] == "cancel"
        assert kw["error_type"] == "session_cancelled"
        assert kw["error_message"] == "User pressed Ctrl+C"


# ---------------------------------------------------------------------------
# provider:cloudflare_challenge — extraction
# ---------------------------------------------------------------------------


class TestCloudflareChallenge:
    @pytest.mark.asyncio
    async def test_extracts_challenge_fields(self):
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        await handlers["provider:cloudflare_challenge"](
            "provider:cloudflare_challenge", {
                "provider": "anthropic",
                "model": "claude-opus-4",
                "message": "Cloudflare bot challenge detected",
                "status_code": 403,
            }
        )
        await asyncio.sleep(0)

        kw = mock_re.call_args[1]
        assert kw["event_type"] == "provider:cloudflare_challenge"
        assert kw["event_category"] == "error"
        assert kw["error_type"] == "cloudflare_challenge"
        assert kw["error_message"] == "Cloudflare bot challenge detected"
        assert kw["status_code"] == 403


# ---------------------------------------------------------------------------
# Fire-and-forget: errors don't propagate
# ---------------------------------------------------------------------------


class TestErrorIsolation:
    @pytest.mark.asyncio
    async def test_report_event_error_doesnt_propagate(self):
        """Exceptions in report_event should be swallowed."""
        mock_re = AsyncMock(side_effect=ConnectionError("Supabase down"))
        _, handlers, _, _ = await _mount_and_get_handlers(mock_re)

        result = await handlers["provider:error"]("provider:error", {
            "provider": "test",
            "error": {"type": "TestError", "msg": "test"},
        })
        await asyncio.sleep(0)

        assert hasattr(result, "action") and result.action == "continue"

    @pytest.mark.asyncio
    async def test_all_handlers_return_continue(self):
        """Every event handler must return action=continue regardless of outcome."""
        _, handlers, mock_re, _ = await _mount_and_get_handlers()

        # Test a variety of events
        test_cases = [
            ("provider:error", {"provider": "t", "error": {"type": "E", "msg": "m"}}),
            ("tool:error", {"tool_name": "t", "error": {"type": "E", "msg": "m"}}),
            ("provider:retry", {"provider": "t", "attempt": 1}),
            ("provider:throttle", {"provider": "t", "reason": "r"}),
            ("policy:violation", {"policy": "p"}),
            ("approval:denied", {"reason": "r"}),
            ("cancel:completed", {"reason": "r"}),
            ("provider:cloudflare_challenge", {"message": "m"}),
            ("provider:tool_sequence_repaired", {"repair_count": 1}),
            ("llm:response", {"status": "success"}),  # should skip but still continue
            ("execution:end", {"status": "success"}),  # should skip but still continue
        ]

        for event, data in test_cases:
            if event == "provider:request":
                result = await handlers[event](event, data)
            else:
                result = await handlers[event](event, data)
            assert hasattr(result, "action") and result.action == "continue", f"Failed for {event}"
            await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# telemetry.py — report_event + report_error backward compat
# ---------------------------------------------------------------------------


class TestTelemetryReportEvent:
    """Verify the new report_event function in telemetry.py."""

    @pytest.mark.asyncio
    async def test_report_event_sends_to_events_endpoint(self):
        from provider_failover import telemetry as mod

        mod = _reload_telemetry_with_env(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_ANON_KEY="test-key",
        )
        captured = {}
        captured_endpoint = {}

        def fake_post(payload, endpoint=None):
            captured.update(payload)
            captured_endpoint["url"] = endpoint

        with patch.object(mod, "_post_sync", side_effect=fake_post):
            await mod.report_event(
                event_type="provider:retry",
                event_category="retry",
                provider="anthropic",
                model="claude-opus-4",
                attempt=2,
                max_retries=5,
                retry_delay=1.5,
            )

        assert captured["event_type"] == "provider:retry"
        assert captured["event_category"] == "retry"
        assert captured["attempt"] == 2
        assert captured["max_retries"] == 5
        assert captured["retry_delay"] == 1.5
        assert "events" in captured_endpoint["url"]

    @pytest.mark.asyncio
    async def test_report_event_strips_none_values(self):
        from provider_failover import telemetry as mod

        mod = _reload_telemetry_with_env(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_ANON_KEY="test-key",
        )
        captured = {}

        def fake_post(payload, endpoint=None):
            captured.update(payload)

        with patch.object(mod, "_post_sync", side_effect=fake_post):
            await mod.report_event(
                event_type="tool:error",
                event_category="error",
                # Leave everything else as None
            )

        assert "provider" not in captured
        assert "model" not in captured
        assert "attempt" not in captured
        assert "throttle_reason" not in captured

    @pytest.mark.asyncio
    async def test_report_event_noop_when_disabled(self):
        from provider_failover import telemetry as mod

        mod = _reload_telemetry_with_env(
            SUPABASE_URL="",
            SUPABASE_ANON_KEY="",
        )

        with patch.object(mod, "_post_sync") as mock_post:
            await mod.report_event(
                event_type="provider:error",
                event_category="error",
            )

        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_report_event_never_raises(self):
        from provider_failover import telemetry as mod

        mod = _reload_telemetry_with_env(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_ANON_KEY="test-key",
        )

        with patch.object(mod, "_post_sync", side_effect=ConnectionError("down")):
            # Should NOT raise
            await mod.report_event(
                event_type="provider:error",
                event_category="error",
            )


class TestTelemetryBackwardCompat:
    """Verify report_error still works and now also mirrors to telemetry_events."""

    @pytest.mark.asyncio
    async def test_report_error_still_works(self):
        from provider_failover import telemetry as mod

        mod = _reload_telemetry_with_env(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_ANON_KEY="test-key",
        )
        calls = []

        def fake_post(payload, endpoint=None):
            calls.append({"payload": payload, "endpoint": endpoint})

        with patch.object(mod, "_post_sync", side_effect=fake_post):
            await mod.report_error(
                provider="anthropic",
                model="claude-opus-4",
                error_type="OverloadedError",
                latency_ms=250,
                source="hook",
            )

        # Should have 2 calls: one to provider_errors, one to telemetry_events
        assert len(calls) == 2
        # First call is to provider_errors (endpoint=None → uses default)
        assert calls[0]["endpoint"] is None
        assert calls[0]["payload"]["provider"] == "anthropic"
        # Second call is to telemetry_events
        assert "telemetry_events" in calls[1]["endpoint"]
        assert calls[1]["payload"]["event_type"] == "provider:error"

    @pytest.mark.asyncio
    async def test_latency_ms_included_in_payload(self):
        from provider_failover import telemetry as mod

        mod = _reload_telemetry_with_env(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_ANON_KEY="test-key",
        )
        captured = {}

        def fake_post(payload, endpoint=None):
            if endpoint is None:  # provider_errors call
                captured.update(payload)

        with patch.object(mod, "_post_sync", side_effect=fake_post):
            await mod.report_error(
                provider="anthropic",
                model="claude-opus-4",
                error_type="OverloadedError",
                latency_ms=250,
                source="hook",
            )

        assert captured["latency_ms"] == 250
        assert captured["source"] == "hook"

    @pytest.mark.asyncio
    async def test_source_defaults_stripped_when_none(self):
        from provider_failover import telemetry as mod

        mod = _reload_telemetry_with_env(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_ANON_KEY="test-key",
        )
        captured = {}

        def fake_post(payload, endpoint=None):
            if endpoint is None:  # provider_errors call
                captured.update(payload)

        with patch.object(mod, "_post_sync", side_effect=fake_post):
            await mod.report_error(
                provider="anthropic",
                model="claude-opus-4",
                error_type="RateLimitError",
            )

        assert "latency_ms" not in captured
        assert "source" not in captured


def _reload_telemetry_with_env(**env_overrides):
    """Re-import the telemetry module with patched env vars."""
    import importlib

    import provider_failover.telemetry as mod

    with patch.dict("os.environ", {"SESSION_DOCTOR_TELEMETRY_ENABLED": "true", **env_overrides}, clear=False):
        importlib.reload(mod)
    return mod
