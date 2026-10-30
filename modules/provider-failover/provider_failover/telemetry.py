"""Fire-and-forget provider error telemetry.

Reports provider errors to a community Supabase database for aggregate
analysis. Raw diagnostic text is omitted; only structured provider/status
metadata and a stable pseudonymous machine identifier are included.

Telemetry is disabled unless SESSION_DOCTOR_TELEMETRY_ENABLED is explicitly
true and SUPABASE_URL / SUPABASE_ANON_KEY configure a valid HTTPS (or loopback)
endpoint. Raw error/reason text is omitted centrally; structured diagnostics
and a stable pseudonymous machine hash remain opt-in.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import platform
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid

logger = logging.getLogger(__name__)

# --- Machine fingerprint (computed once, hashed, no PII) -----------------


def _machine_hash() -> str:
    """Stable pseudonymous machine fingerprint; not an anonymity guarantee."""
    mac = hex(uuid.getnode())
    hostname = socket.gethostname()
    return hashlib.sha256(f"{mac}:{hostname}".encode()).hexdigest()[:16]


_MACHINE_HASH = _machine_hash()
_OS_PLATFORM = platform.system().lower()  # 'darwin', 'linux', 'windows'
_OS_VERSION = platform.platform()  # 'macOS-26.3-arm64-arm-64bit'

# --- Auto-load .env from bundle root (no dependencies) -------------------


def _load_dotenv() -> None:
    """Load .env file from bundle root if it exists. No dependencies needed."""
    import pathlib as _pathlib

    current = _pathlib.Path(__file__).resolve()
    for parent in current.parents:
        env_file = parent / ".env"
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    # Only set if not already in environment (don't override)
                    if key and key not in os.environ:
                        os.environ[key] = value
            break


_load_dotenv()

# --- Supabase config from env --------------------------------------------

def _safe_service_url(url: str) -> bool:
    """Credentials may cross HTTPS, or HTTP to an exact loopback host only."""
    try:
        parts = urllib.parse.urlsplit(url)
        # Accessing .port also rejects malformed/out-of-range ports.
        _ = parts.port
        if not parts.hostname or parts.username is not None or parts.password is not None:
            return False
        if parts.query or parts.fragment:
            return False
        return parts.scheme == "https" or (
            parts.scheme == "http" and parts.hostname.lower() in ("localhost", "127.0.0.1", "::1")
        )
    except (TypeError, ValueError):
        return False


def _safe_payload(payload: dict) -> dict:
    """Do not send arbitrary provider/tool/approval/policy exception text."""
    return {key: value for key, value in payload.items()
            if key != "error_message" and value is not None}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Raising avoids forwarding either ordinary or unredirected headers.
        raise urllib.error.HTTPError(req.full_url, code, "Telemetry redirect refused", headers, fp)


_SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
_SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
_OPTED_IN = os.environ.get("SESSION_DOCTOR_TELEMETRY_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
_ENABLED = bool(_OPTED_IN and _SUPABASE_ANON_KEY and _safe_service_url(_SUPABASE_URL))
_ENDPOINT = f"{_SUPABASE_URL}/rest/v1/provider_errors" if _ENABLED else ""
_EVENTS_ENDPOINT = (
    f"{_SUPABASE_URL}/rest/v1/telemetry_events" if _ENABLED else ""
)


# --- Sync HTTP POST (runs in a thread) -----------------------------------


def _post_sync(
    payload: dict,  # type: ignore[type-arg]
    endpoint: str | None = None,
) -> None:
    """Blocking POST to Supabase.  Runs in a thread via asyncio.to_thread."""
    if not _ENABLED:
        return
    url = endpoint or _ENDPOINT
    if not _safe_service_url(url):
        return
    data = json.dumps(_safe_payload(payload)).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Prefer": "return=minimal"},
        method="POST",
    )
    req.add_unredirected_header("apikey", _SUPABASE_ANON_KEY)
    req.add_unredirected_header("Authorization", f"Bearer {_SUPABASE_ANON_KEY}")
    with urllib.request.build_opener(_NoRedirect()).open(req, timeout=2):
        pass



# --- Public async API: report_error (legacy, provider_errors table) -------


async def report_error(
    *,
    provider: str,
    model: str,
    tier_label: str | None = None,
    tier_index: int | None = None,
    error_type: str,
    error_message: str | None = None,
    status_code: int | None = None,
    retryable: bool | None = None,
    prompt_tokens: int | None = None,
    message_count: int | None = None,
    tool_count: int | None = None,
    has_streaming: bool | None = None,
    failover_from: str | None = None,
    failover_to: str | None = None,
    tiers_tried: int | None = None,
    client: str = "amplifier",
    bundle_name: str | None = None,
    latency_ms: int | None = None,
    source: str | None = None,
) -> None:
    """Fire-and-forget: report a provider error to the telemetry database.

    Never raises. Each POST has a 2-second wait budget (report_error also
    mirrors an event). Silently no-ops unless explicit telemetry opt-in and
    valid Supabase configuration are present.
    """
    if not _ENABLED:
        return

    payload = {
        "machine_hash": _MACHINE_HASH,
        "provider": provider,
        "model": model,
        "tier_label": tier_label,
        "tier_index": tier_index,
        "error_type": error_type,
        "error_message": error_message,
        "status_code": status_code,
        "retryable": retryable,
        "prompt_tokens": prompt_tokens,
        "message_count": message_count,
        "tool_count": tool_count,
        "has_streaming": has_streaming,
        "failover_from": failover_from,
        "failover_to": failover_to,
        "tiers_tried": tiers_tried,
        "client": client,
        "bundle_name": bundle_name,
        "latency_ms": latency_ms,
        "source": source,
        "os_platform": _OS_PLATFORM,
        "os_version": _OS_VERSION,
    }
    # Strip None values so Supabase uses column defaults
    payload = _safe_payload(payload)

    try:
        await asyncio.wait_for(
            asyncio.to_thread(_post_sync, payload),
            timeout=2.0,
        )
        logger.debug(
            "telemetry: reported %s on %s/%s", error_type, provider, model
        )
    except Exception:
        # Silent — telemetry must never impact the session
        logger.debug(
            "telemetry: failed to report (silenced)"
        )

    # Also mirror to telemetry_events table for unified querying
    try:
        await report_event(
            event_type="provider:error",
            event_category="error",
            provider=provider,
            model=model,
            error_type=error_type,
            error_message=error_message,
            status_code=status_code,
            retryable=retryable,
            prompt_tokens=prompt_tokens,
            message_count=message_count,
            tool_count=tool_count,
            latency_ms=latency_ms,
            client=client,
            bundle_name=bundle_name,
            source=source or "failover",
        )
    except Exception:
        pass  # telemetry must never impact the session


# --- Public async API: report_event (new, telemetry_events table) ---------


async def report_event(
    *,
    event_type: str,
    event_category: str,
    provider: str | None = None,
    model: str | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    status_code: int | None = None,
    retryable: bool | None = None,
    attempt: int | None = None,
    max_retries: int | None = None,
    retry_delay: float | None = None,
    retry_after: float | None = None,
    throttle_reason: str | None = None,
    throttle_dimension: str | None = None,
    throttle_remaining: int | None = None,
    throttle_limit: int | None = None,
    throttle_ratio: float | None = None,
    tool_name: str | None = None,
    prompt_tokens: int | None = None,
    message_count: int | None = None,
    tool_count: int | None = None,
    latency_ms: int | None = None,
    execution_status: str | None = None,
    client: str = "amplifier",
    bundle_name: str | None = None,
    source: str = "hook",
) -> None:
    """Fire-and-forget: report any event to the telemetry_events table.

    Never raises. Each POST has a 2-second wait budget (report_error also
    mirrors an event). Silently no-ops unless explicit telemetry opt-in and
    valid Supabase configuration are present.
    """
    if not _ENABLED:
        return

    payload = {
        "machine_hash": _MACHINE_HASH,
        "event_type": event_type,
        "event_category": event_category,
        "provider": provider,
        "model": model,
        "error_type": error_type,
        "error_message": error_message,
        "status_code": status_code,
        "retryable": retryable,
        "attempt": attempt,
        "max_retries": max_retries,
        "retry_delay": retry_delay,
        "retry_after": retry_after,
        "throttle_reason": throttle_reason,
        "throttle_dimension": throttle_dimension,
        "throttle_remaining": throttle_remaining,
        "throttle_limit": throttle_limit,
        "throttle_ratio": throttle_ratio,
        "tool_name": tool_name,
        "prompt_tokens": prompt_tokens,
        "message_count": message_count,
        "tool_count": tool_count,
        "latency_ms": latency_ms,
        "execution_status": execution_status,
        "client": client,
        "bundle_name": bundle_name,
        "source": source,
        "os_platform": _OS_PLATFORM,
        "os_version": _OS_VERSION,
    }
    # Strip None values so Supabase uses column defaults
    payload = _safe_payload(payload)

    try:
        await asyncio.wait_for(
            asyncio.to_thread(_post_sync, payload, _EVENTS_ENDPOINT),
            timeout=2.0,
        )
        logger.debug(
            "telemetry: reported event %s/%s",
            event_type,
            event_category,
        )
    except Exception:
        # Silent — telemetry must never impact the session
        logger.debug(
            "telemetry: failed to report event (silenced)"
        )
