"""Amplifier hook module: ecosystem-wide telemetry.

Captures ALL error/failure events across the Amplifier ecosystem and
reports them to Supabase for aggregate analysis.  Covers:

  - provider:error          LLM/provider failures
  - tool:error              Tool execution failures
  - policy:violation        Policy gate violations
  - approval:denied         Approval gate denials
  - provider:retry          Every retry attempt
  - provider:throttle       Pre-emptive rate limiting
  - provider:tool_sequence_repaired   Corrupt tool call repairs
  - llm:response            When status="error" (API errors)
  - execution:end           When status != "success"
  - cancel:completed        Session cancellation
  - provider:cloudflare_challenge     Anthropic CF bot challenge

No PII, no prompts, no responses.  Just structured diagnostic data.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event → category mapping
# ---------------------------------------------------------------------------

_CATEGORY_MAP: dict[str, str] = {
    "provider:error": "error",
    "tool:error": "error",
    "policy:violation": "error",
    "provider:retry": "retry",
    "provider:throttle": "throttle",
    "approval:denied": "cancel",
    "cancel:completed": "cancel",
    "provider:tool_sequence_repaired": "repair",
    "provider:cloudflare_challenge": "error",
    # llm:response and execution:end are handled dynamically
}

# All events we register on (provider:request is the timer, not reported)
_HOOK_EVENTS = [
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


def event_category(event_type: str, data: dict[str, Any]) -> str | None:
    """Return the telemetry category for an event, or None to skip.

    Returns None for events that should not be reported (e.g. successful
    llm:response or execution:end with status=success).
    """
    # Static mappings
    if event_type in _CATEGORY_MAP:
        return _CATEGORY_MAP[event_type]

    # Dynamic: llm:response only when status indicates error
    if event_type == "llm:response":
        status = data.get("status", "")
        if status == "error":
            return "error"
        return None  # skip successes

    # Dynamic: execution:end only when non-success
    if event_type == "execution:end":
        status = data.get("status", "")
        if status in ("error", "cancelled", "incomplete"):
            return "status"
        return None  # skip successes

    return None


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> Callable | None:
    """Mount the telemetry hook.  Registers on all error/failure events."""
    # Lazy import to handle cases where the module path might differ
    try:
        from provider_failover.telemetry import _ENABLED, report_event
    except ImportError:
        logger.warning(
            "hooks-telemetry: provider_failover.telemetry not available "
            "— telemetry disabled"
        )
        return None

    if not _ENABLED:
        logger.info(
            "hooks-telemetry: telemetry disabled (explicit opt-in and valid "
            "Supabase configuration are required)"
        )
        return None

    config = config or {}

    # State: track request start times for latency measurement
    # Keyed by "session_id:iteration" — cleaned up on error or timeout
    request_timers: dict[str, float] = {}

    async def on_provider_request(event: str, data: dict[str, Any]) -> Any:
        """Record request start time for latency measurement."""
        from amplifier_core.models import HookResult

        session_id = data.get("session_id", "unknown")
        iteration = data.get("iteration", 0)
        key = f"{session_id}:{iteration}"
        request_timers[key] = time.monotonic()
        return HookResult(action="continue")

    async def on_event(event: str, data: dict[str, Any]) -> Any:
        """Universal event handler for all telemetry events."""
        import asyncio

        category = event_category(event, data)
        if category is None:
            from amplifier_core.models import HookResult
            return HookResult(action="continue")

        # --- Build kwargs for report_event based on event type ---
        kwargs: dict[str, Any] = {
            "event_type": event,
            "event_category": category,
            "source": "hook",
        }

        # Common: provider and model (available on many events)
        kwargs["provider"] = data.get("provider")
        kwargs["model"] = data.get("model")

        # --- provider:error ---
        if event == "provider:error":
            session_id = data.get("session_id", "unknown")
            iteration = data.get("iteration", 0)
            key = f"{session_id}:{iteration}"
            start = request_timers.pop(key, None)
            kwargs["latency_ms"] = (
                int((time.monotonic() - start) * 1000) if start else None
            )

            error_info = data.get("error", {})
            kwargs["error_type"] = error_info.get("type", "Unknown")
            kwargs["error_message"] = (
                error_info.get("msg", "") or ""
            )[:500]
            kwargs["status_code"] = data.get("status_code")
            kwargs["retryable"] = data.get("retryable")

        # --- tool:error ---
        elif event == "tool:error":
            error_info = data.get("error", {})
            kwargs["error_type"] = error_info.get("type", "Unknown")
            kwargs["error_message"] = (
                error_info.get("msg", "") or ""
            )[:500]
            kwargs["tool_name"] = data.get("tool_name")

        # --- provider:retry ---
        elif event == "provider:retry":
            kwargs["error_type"] = data.get("error_type")
            kwargs["error_message"] = (
                data.get("error_message", "") or ""
            )[:500]
            kwargs["attempt"] = data.get("attempt")
            kwargs["max_retries"] = data.get("max_retries")
            kwargs["retry_delay"] = data.get("delay")
            kwargs["retry_after"] = data.get("retry_after")

        # --- provider:throttle ---
        elif event == "provider:throttle":
            kwargs["throttle_reason"] = data.get("reason")
            kwargs["throttle_dimension"] = data.get("dimension")
            kwargs["throttle_remaining"] = data.get("remaining")
            kwargs["throttle_limit"] = data.get("limit")
            kwargs["throttle_ratio"] = data.get("ratio")

        # --- provider:tool_sequence_repaired ---
        elif event == "provider:tool_sequence_repaired":
            repair_count = data.get("repair_count", 0)
            kwargs["error_message"] = (
                f"Repaired {repair_count} tool call(s)"
            )

        # --- policy:violation ---
        elif event == "policy:violation":
            kwargs["error_type"] = data.get("policy", "unknown_policy")
            kwargs["error_message"] = (
                data.get("message", "") or ""
            )[:500]

        # --- approval:denied ---
        elif event == "approval:denied":
            kwargs["error_type"] = "approval_denied"
            kwargs["error_message"] = (
                data.get("reason", "") or ""
            )[:500]

        # --- llm:response (only status=error, filtered above) ---
        elif event == "llm:response":
            error_info = data.get("error", {})
            if isinstance(error_info, dict):
                kwargs["error_type"] = error_info.get("type", "Unknown")
                kwargs["error_message"] = (
                    error_info.get("msg", "") or ""
                )[:500]
            else:
                kwargs["error_type"] = "Unknown"
                kwargs["error_message"] = (str(error_info) or "")[:500]
            kwargs["execution_status"] = data.get("status")

        # --- execution:end (only non-success, filtered above) ---
        elif event == "execution:end":
            kwargs["execution_status"] = data.get("status")
            error_val = data.get("error")
            if isinstance(error_val, str):
                kwargs["error_message"] = (error_val or "")[:500]

        # --- cancel:completed ---
        elif event == "cancel:completed":
            kwargs["error_type"] = "session_cancelled"
            kwargs["error_message"] = (
                data.get("reason", "") or ""
            )[:500]

        # --- provider:cloudflare_challenge ---
        elif event == "provider:cloudflare_challenge":
            kwargs["error_type"] = "cloudflare_challenge"
            kwargs["error_message"] = (
                data.get("message", "") or ""
            )[:500]
            kwargs["status_code"] = data.get("status_code")

        # Fire-and-forget telemetry POST
        try:
            asyncio.ensure_future(report_event(**kwargs))
        except Exception:
            pass  # telemetry must never impact the session

        from amplifier_core.models import HookResult
        return HookResult(action="continue")

    # --- Register hooks ---
    hooks = coordinator.hooks if hasattr(coordinator, "hooks") else None
    if hooks is None:
        logger.warning("hooks-telemetry: no hooks registry available")
        return None

    unregisters: list[Callable] = []

    # provider:request gets its own handler (timer only, no reporting)
    unreg = hooks.register(
        "provider:request",
        on_provider_request,
        priority=99,
        name="telemetry-timer",
    )
    if unreg:
        unregisters.append(unreg)

    # All other events share the universal handler
    for evt in _HOOK_EVENTS:
        if evt == "provider:request":
            continue  # already registered above
        unreg = hooks.register(
            evt, on_event, priority=99, name=f"telemetry-{evt}"
        )
        if unreg:
            unregisters.append(unreg)

    logger.info(
        "hooks-telemetry: registered on %d events: %s",
        len(_HOOK_EVENTS),
        _HOOK_EVENTS,
    )

    def cleanup() -> None:
        for unreg_fn in unregisters:
            unreg_fn()

    return cleanup
