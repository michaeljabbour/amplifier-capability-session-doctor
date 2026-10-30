"""Fictional-only telemetry trust-boundary regressions. No HTTP requests."""
from __future__ import annotations
import importlib
import json
from unittest.mock import MagicMock, patch
import urllib.error
import pytest


def configured(**overrides):
    import provider_failover.telemetry as mod
    env = {"SESSION_DOCTOR_TELEMETRY_ENABLED": "true",
           "SUPABASE_URL": "https://fixture.invalid", "SUPABASE_ANON_KEY": "fictional-marker"}
    env.update(overrides)
    with patch.dict("os.environ", env):
        importlib.reload(mod)
    return mod


@pytest.mark.parametrize("flag", ["", "0", "false", "no", "off", "maybe"])
def test_ambient_supabase_config_does_not_opt_in(flag):
    mod = configured(SESSION_DOCTOR_TELEMETRY_ENABLED=flag)
    assert not mod._ENABLED
    with patch.object(mod.urllib.request, "build_opener") as opener:
        mod._post_sync({"provider": "fixture"})
    opener.assert_not_called()


@pytest.mark.parametrize("url", ["http://remote.invalid", "http://localhost.evil.invalid", "file:///tmp/example",
                                 "https://", "https://user:fictional@fixture.invalid", "https://fixture.invalid?key=x",
                                 "https://fixture.invalid/#secret", "https://fixture.invalid:99999"])
def test_unsafe_endpoint_never_enables_or_opens(url):
    mod = configured(SUPABASE_URL=url)
    assert not mod._ENABLED
    with patch.object(mod.urllib.request, "build_opener") as opener:
        mod._post_sync({"provider": "fixture"}, url)
    opener.assert_not_called()


@pytest.mark.parametrize("url", ["https://fixture.invalid", "http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"])
def test_secure_or_loopback_endpoint_is_allowed(url):
    mod = configured(SUPABASE_URL=url)
    assert mod._ENABLED


@pytest.mark.asyncio
async def test_both_public_apis_omit_free_text_before_scheduling_post():
    mod = configured()
    captured = []
    def capture(payload, endpoint=None):
        captured.append(dict(payload))
    with patch.object(mod, "_post_sync", side_effect=capture):
        await mod.report_error(provider="fixture", model="fixture-model", error_type="Failure",
                               error_message="fictional-private-diagnostic-marker", status_code=503)
        await mod.report_event(event_type="approval:denied", event_category="policy",
                               error_message="fictional-private-approval-marker", status_code=403)
    assert len(captured) == 3  # error table, mirrored event, explicit event
    assert all("error_message" not in p for p in captured)
    assert "fictional-private" not in json.dumps(captured)
    assert captured[0]["status_code"] == 503
    assert all(v is not None for p in captured for v in p.values())


def test_transport_rechecks_payload_and_uses_unredirected_auth():
    mod = configured()
    fake_opener = MagicMock()
    with patch.object(mod.urllib.request, "build_opener", return_value=fake_opener) as builder:
        mod._post_sync({"provider": "fixture", "error_message": "fictional-private-marker", "status_code": 503})
    assert isinstance(builder.call_args.args[0], mod._NoRedirect)
    request = fake_opener.open.call_args.args[0]
    assert request.full_url == "https://fixture.invalid/rest/v1/provider_errors"
    assert json.loads(request.data) == {"provider": "fixture", "status_code": 503}
    assert "authorization" not in {k.lower() for k in request.headers}
    assert "apikey" not in {k.lower() for k in request.headers}
    assert {k.lower() for k in request.unredirected_hdrs} >= {"authorization", "apikey"}
    assert fake_opener.open.call_args.kwargs["timeout"] == 2


def test_transport_refuses_unsafe_override_even_if_enabled():
    mod = configured()
    with patch.object(mod.urllib.request, "build_opener") as opener:
        mod._post_sync({"provider": "fixture"}, "http://remote.invalid/rest/v1/provider_errors")
    opener.assert_not_called()


def test_redirect_refused_before_any_second_request():
    mod = configured()
    req = mod.urllib.request.Request("https://fixture.invalid/rest/v1/provider_errors")
    req.add_unredirected_header("Authorization", "Bearer fictional-marker")
    with pytest.raises(urllib.error.HTTPError, match="Telemetry redirect refused"):
        mod._NoRedirect().redirect_request(req, None, 302, "Found", {}, "https://other.invalid/")
