"""Tests for provider_failover.telemetry module.

Verifies that telemetry:
- Never raises, even when Supabase is unreachable
- Correctly hashes machine identity (consistent, 16-char hex)
- Strips None values from payloads
- Is disabled when env vars are missing
- Fires correctly from failover error paths
"""

from __future__ import annotations

import asyncio
import importlib
import os
import pathlib
import re
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — reload telemetry with controlled env
# ---------------------------------------------------------------------------


def _reload_telemetry(**env_overrides):
    """Import (or re-import) the telemetry module with patched env vars."""
    import importlib

    import provider_failover.telemetry as mod

    with patch.dict("os.environ", {"SESSION_DOCTOR_TELEMETRY_ENABLED": "true", **env_overrides}, clear=False):
        importlib.reload(mod)
    return mod


# ---------------------------------------------------------------------------
# Machine hash
# ---------------------------------------------------------------------------


class TestMachineHash:
    def test_returns_16_char_hex(self):
        from provider_failover.telemetry import _machine_hash

        h = _machine_hash()
        assert len(h) == 16
        assert re.fullmatch(r"[0-9a-f]{16}", h)

    def test_is_deterministic(self):
        from provider_failover.telemetry import _machine_hash

        assert _machine_hash() == _machine_hash()

    def test_module_level_constant_matches(self):
        from provider_failover.telemetry import _MACHINE_HASH, _machine_hash

        assert _MACHINE_HASH == _machine_hash()


# ---------------------------------------------------------------------------
# Enabled / disabled logic
# ---------------------------------------------------------------------------


class TestEnabled:
    def test_disabled_when_no_env(self):
        mod = _reload_telemetry(SUPABASE_URL="", SUPABASE_ANON_KEY="")
        assert mod._ENABLED is False

    def test_disabled_when_missing_key(self):
        mod = _reload_telemetry(
            SUPABASE_URL="https://example.supabase.co", SUPABASE_ANON_KEY=""
        )
        assert mod._ENABLED is False

    def test_disabled_when_missing_url(self):
        mod = _reload_telemetry(
            SUPABASE_URL="", SUPABASE_ANON_KEY="some-key"
        )
        assert mod._ENABLED is False

    def test_enabled_when_opted_in_and_both_set(self):
        mod = _reload_telemetry(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_ANON_KEY="some-key",
        )
        assert mod._ENABLED is True
        assert "example.supabase.co" in mod._ENDPOINT


# ---------------------------------------------------------------------------
# report_error — payload construction
# ---------------------------------------------------------------------------


class TestReportError:
    @pytest.mark.asyncio
    async def test_noop_when_disabled(self):
        """report_error should silently return when telemetry is disabled."""
        mod = _reload_telemetry(SUPABASE_URL="", SUPABASE_ANON_KEY="")
        # Should not raise, should not attempt any HTTP
        await mod.report_error(
            provider="anthropic",
            model="claude-opus-4",
            error_type="ProviderUnavailableError",
        )

    @pytest.mark.asyncio
    async def test_payload_strips_none_values(self):
        """Payload sent to Supabase should not contain None values."""
        mod = _reload_telemetry(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_ANON_KEY="test-key",
        )
        captured_payload = {}

        def fake_post(payload):
            captured_payload.update(payload)

        with patch.object(mod, "_post_sync", side_effect=fake_post):
            await mod.report_error(
                provider="anthropic",
                model="claude-opus-4",
                error_type="RateLimitError",
                error_message="Too many requests",
                status_code=429,
                retryable=True,
                # Leave these as None (default):
                # tier_label, tier_index, prompt_tokens, etc.
            )

        # Verify no None values
        for key, value in captured_payload.items():
            assert value is not None, f"Payload key {key!r} should not be None"

        # Verify required fields are present
        assert captured_payload["provider"] == "anthropic"
        assert captured_payload["model"] == "claude-opus-4"
        assert captured_payload["error_type"] == "RateLimitError"
        assert captured_payload["status_code"] == 429
        assert captured_payload["retryable"] is True
        assert captured_payload["machine_hash"]  # non-empty
        assert captured_payload["os_platform"]  # non-empty

    @pytest.mark.asyncio
    async def test_never_raises_on_http_error(self):
        """report_error must swallow all exceptions — telemetry never crashes the session."""
        mod = _reload_telemetry(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_ANON_KEY="test-key",
        )

        def exploding_post(payload):
            raise ConnectionError("Supabase is down")

        with patch.object(mod, "_post_sync", side_effect=exploding_post):
            # Must not raise
            await mod.report_error(
                provider="anthropic",
                model="claude-opus-4",
                error_type="ProviderUnavailableError",
            )

    @pytest.mark.asyncio
    async def test_never_raises_on_timeout(self):
        """report_error must handle timeouts gracefully."""
        import time

        mod = _reload_telemetry(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_ANON_KEY="test-key",
        )

        def blocking_post(payload):
            # Block in a thread for longer than the 2s timeout
            time.sleep(10)

        with patch.object(mod, "_post_sync", side_effect=blocking_post):
            # Must not hang — 2s timeout should kick in
            await asyncio.wait_for(
                mod.report_error(
                    provider="anthropic",
                    model="claude-opus-4",
                    error_type="ProviderUnavailableError",
                ),
                timeout=5.0,  # generous outer timeout
            )

    @pytest.mark.asyncio
    async def test_full_payload_shape(self):
        """When all fields are provided, payload should include everything."""
        mod = _reload_telemetry(
            SUPABASE_URL="https://example.supabase.co",
            SUPABASE_ANON_KEY="test-key",
        )
        captured_payload = {}

        def fake_post(payload):
            captured_payload.update(payload)

        with patch.object(mod, "_post_sync", side_effect=fake_post):
            await mod.report_error(
                provider="anthropic",
                model="claude-opus-4",
                tier_label="primary",
                tier_index=0,
                error_type="ProviderUnavailableError",
                error_message="Service overloaded",
                status_code=529,
                retryable=True,
                prompt_tokens=15000,
                message_count=12,
                tool_count=5,
                has_streaming=False,
                failover_from=None,
                failover_to="secondary",
                tiers_tried=1,
                client="amplifier",
                bundle_name="provider-doctor",
            )

        expected_keys = {
            "machine_hash",
            "provider",
            "model",
            "tier_label",
            "tier_index",
            "error_type",
            "status_code",
            "retryable",
            "prompt_tokens",
            "message_count",
            "tool_count",
            "has_streaming",
            "failover_to",
            "tiers_tried",
            "client",
            "bundle_name",
            "os_platform",
            "os_version",
        }
        # failover_from was None so it should be stripped
        assert "failover_from" not in captured_payload
        assert expected_keys == set(captured_payload.keys())


# ---------------------------------------------------------------------------
# Integration: _fire_telemetry in FailoverProvider
# ---------------------------------------------------------------------------


class TestFireTelemetryIntegration:
    """Verify that FailoverProvider._fire_telemetry schedules report_error."""

    def _make_failover(self):
        from provider_failover.errors import LLMError
        from provider_failover.failover import FailoverProvider, TierConfig

        mock_provider = MagicMock()
        mock_provider.name = "anthropic"
        tiers = [
            TierConfig(provider=mock_provider, model="claude-opus-4", label="primary"),
            TierConfig(
                provider=mock_provider, model="claude-sonnet-4-5", label="secondary"
            ),
        ]
        fp = FailoverProvider(tiers=tiers, probe_interval=5)
        return fp, LLMError

    @pytest.mark.asyncio
    async def test_fire_telemetry_schedules_report(self):
        fp, LLMError = self._make_failover()
        exc = LLMError("test error", retryable=True)

        mock_report = AsyncMock()
        with patch("provider_failover.failover.report_error", mock_report):
            fp._fire_telemetry(
                exc, fp._tiers[0], tier_idx=0, has_streaming=False
            )
            # Give the event loop a tick to run the scheduled future
            await asyncio.sleep(0)

            mock_report.assert_called_once()
            call_kwargs = mock_report.call_args[1]
            assert call_kwargs["provider"] == "anthropic"
            assert call_kwargs["model"] == "claude-opus-4"
            assert call_kwargs["error_type"] == "LLMError"
            assert call_kwargs["has_streaming"] is False
            assert call_kwargs["tier_index"] == 0

    @pytest.mark.asyncio
    async def test_fire_telemetry_includes_failover_context(self):
        fp, LLMError = self._make_failover()
        exc = LLMError("overloaded", retryable=True)

        mock_report = AsyncMock()
        with patch("provider_failover.failover.report_error", mock_report):
            # Simulate error on tier 0 — should show failover_to="secondary"
            fp._fire_telemetry(
                exc, fp._tiers[0], tier_idx=0, has_streaming=True
            )
            await asyncio.sleep(0)

            call_kwargs = mock_report.call_args[1]
            assert call_kwargs["failover_to"] == "secondary"
            assert call_kwargs["has_streaming"] is True
            assert call_kwargs["tiers_tried"] == 1

    @pytest.mark.asyncio
    async def test_fire_telemetry_no_failover_to_on_last_tier(self):
        fp, LLMError = self._make_failover()
        exc = LLMError("all exhausted", retryable=False)

        mock_report = AsyncMock()
        with patch("provider_failover.failover.report_error", mock_report):
            # Error on last tier (idx 1) — no next tier to fail to
            fp._fire_telemetry(
                exc, fp._tiers[1], tier_idx=1, has_streaming=False
            )
            await asyncio.sleep(0)

            call_kwargs = mock_report.call_args[1]
            assert call_kwargs["failover_to"] is None
            assert call_kwargs["tiers_tried"] == 2



# ---------------------------------------------------------------------------
# _load_dotenv — .env auto-loading
# ---------------------------------------------------------------------------


class TestLoadDotenv:
    """Verify _load_dotenv reads .env files and doesn't override existing vars."""

    def test_loads_vars_from_env_file(self, tmp_path):
        """_load_dotenv should set env vars from a .env file."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "TEST_DOTENV_VAR_A=hello\n"
            "TEST_DOTENV_VAR_B=world\n"
        )

        import provider_failover.telemetry as mod

        # Clean up any previous test vars
        os.environ.pop("TEST_DOTENV_VAR_A", None)
        os.environ.pop("TEST_DOTENV_VAR_B", None)

        try:
            # Patch __file__ to point inside tmp_path so parent search finds .env
            fake_file = str(tmp_path / "sub" / "pkg" / "telemetry.py")
            os.makedirs(os.path.dirname(fake_file), exist_ok=True)
            pathlib.Path(fake_file).touch()

            with patch.object(mod, "__file__", fake_file):
                mod._load_dotenv()

            assert os.environ.get("TEST_DOTENV_VAR_A") == "hello"
            assert os.environ.get("TEST_DOTENV_VAR_B") == "world"
        finally:
            os.environ.pop("TEST_DOTENV_VAR_A", None)
            os.environ.pop("TEST_DOTENV_VAR_B", None)

    def test_does_not_override_existing_vars(self, tmp_path):
        """_load_dotenv should NOT override vars already in the environment."""
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_DOTENV_EXISTING=from_file\n")

        import provider_failover.telemetry as mod

        os.environ["TEST_DOTENV_EXISTING"] = "from_env"

        try:
            fake_file = str(tmp_path / "sub" / "pkg" / "telemetry.py")
            os.makedirs(os.path.dirname(fake_file), exist_ok=True)
            pathlib.Path(fake_file).touch()

            with patch.object(mod, "__file__", fake_file):
                mod._load_dotenv()

            assert os.environ["TEST_DOTENV_EXISTING"] == "from_env"
        finally:
            os.environ.pop("TEST_DOTENV_EXISTING", None)

    def test_handles_missing_env_file(self, tmp_path):
        """_load_dotenv should not crash when no .env file exists."""
        import provider_failover.telemetry as mod

        # Point to a directory with no .env file at all
        fake_file = str(tmp_path / "sub" / "pkg" / "telemetry.py")
        os.makedirs(os.path.dirname(fake_file), exist_ok=True)
        pathlib.Path(fake_file).touch()

        # Should not raise
        with patch.object(mod, "__file__", fake_file):
            mod._load_dotenv()

    def test_skips_comments_and_blank_lines(self, tmp_path):
        """_load_dotenv should ignore comments and blank lines."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# This is a comment\n"
            "\n"
            "TEST_DOTENV_REAL=value\n"
            "  # Another comment\n"
            "\n"
        )

        import provider_failover.telemetry as mod

        os.environ.pop("TEST_DOTENV_REAL", None)

        try:
            fake_file = str(tmp_path / "sub" / "pkg" / "telemetry.py")
            os.makedirs(os.path.dirname(fake_file), exist_ok=True)
            pathlib.Path(fake_file).touch()

            with patch.object(mod, "__file__", fake_file):
                mod._load_dotenv()

            assert os.environ.get("TEST_DOTENV_REAL") == "value"
        finally:
            os.environ.pop("TEST_DOTENV_REAL", None)

    def test_strips_quotes_from_values(self, tmp_path):
        """_load_dotenv should strip surrounding quotes from values."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            'TEST_DOTENV_DQ="double_quoted"\n'
            "TEST_DOTENV_SQ='single_quoted'\n"
            "TEST_DOTENV_NQ=no_quotes\n"
        )

        import provider_failover.telemetry as mod

        os.environ.pop("TEST_DOTENV_DQ", None)
        os.environ.pop("TEST_DOTENV_SQ", None)
        os.environ.pop("TEST_DOTENV_NQ", None)

        try:
            fake_file = str(tmp_path / "sub" / "pkg" / "telemetry.py")
            os.makedirs(os.path.dirname(fake_file), exist_ok=True)
            pathlib.Path(fake_file).touch()

            with patch.object(mod, "__file__", fake_file):
                mod._load_dotenv()

            assert os.environ.get("TEST_DOTENV_DQ") == "double_quoted"
            assert os.environ.get("TEST_DOTENV_SQ") == "single_quoted"
            assert os.environ.get("TEST_DOTENV_NQ") == "no_quotes"
        finally:
            os.environ.pop("TEST_DOTENV_DQ", None)
            os.environ.pop("TEST_DOTENV_SQ", None)
            os.environ.pop("TEST_DOTENV_NQ", None)
