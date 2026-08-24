from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import slaigpus.cdp as cdp  # noqa: E402
from slaigpus.cdp import (  # noqa: E402
    AuthLease,
    BrowserFetchError,
    BrowserFetchTransport,
    CCIAuthorization,
    CDPConnection,
    CDPError,
    CDPTimeout,
    DevToolsEndpoint,
    wait_for_devtools,
)


CCI_URL = cdp.CCI_API_BASE + "/subscriptions/sub/apps"


class FakeChrome:
    def __init__(self, args=None, returncode=None):
        self.args = args or []
        self.returncode = returncode

    def poll(self):
        return self.returncode


def test_wait_for_explicit_devtools_endpoint(monkeypatch):
    calls = []

    def fake_fetch(host, port, timeout):
        calls.append((host, port, timeout))
        return DevToolsEndpoint(port, f"ws://{host}:{port}/devtools/browser/id")

    monkeypatch.setattr(cdp, "_fetch_version", fake_fetch)
    endpoint = wait_for_devtools(9333, timeout=1)
    assert endpoint.port == 9333
    assert endpoint.browser_ws_url.endswith("/devtools/browser/id")
    assert calls and calls[0][0:2] == ("127.0.0.1", 9333)
    assert "devtools/browser" not in repr(endpoint)


def test_wait_for_random_devtools_port_from_profile(tmp_path, monkeypatch):
    (tmp_path / "DevToolsActivePort").write_text(
        "45123\n/devtools/browser/random-id\n", encoding="utf-8"
    )
    chrome = FakeChrome(["chrome", f"--user-data-dir={tmp_path}"])
    monkeypatch.setattr(
        cdp,
        "_fetch_version",
        lambda host, port, timeout: DevToolsEndpoint(
            port, f"ws://{host}:{port}/devtools/browser/random-id"
        ),
    )
    endpoint = wait_for_devtools(0, chrome, timeout=0.1)
    assert endpoint.port == 45123
    assert endpoint.browser_ws_url == (
        "ws://127.0.0.1:45123/devtools/browser/random-id"
    )


def test_random_devtools_endpoint_must_match_profile_target(tmp_path, monkeypatch):
    (tmp_path / "DevToolsActivePort").write_text(
        "45123\n/devtools/browser/owned-id\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        cdp,
        "_fetch_version",
        lambda host, port, timeout: DevToolsEndpoint(
            port, f"ws://{host}:{port}/devtools/browser/different-id"
        ),
    )

    with pytest.raises(CDPTimeout):
        wait_for_devtools(0, profile_dir=tmp_path, timeout=0)


def test_wait_for_devtools_rejects_non_loopback_and_dead_chrome():
    with pytest.raises(CDPError, match="loopback"):
        wait_for_devtools(9222, host="example.com", timeout=0)
    with pytest.raises(CDPError, match="Chrome exited"):
        wait_for_devtools(9222, FakeChrome(returncode=1), timeout=1)


class FakeWebSocket:
    _CLOSED = object()

    def __init__(self):
        self.incoming = queue.Queue()
        self.sent = []
        self.on_send = None
        self.closed = False

    def send(self, encoded):
        message = json.loads(encoded)
        self.sent.append(message)
        if self.on_send is not None:
            self.on_send(message)

    def recv(self):
        value = self.incoming.get(timeout=2)
        if value is self._CLOSED:
            return ""
        return value

    def emit(self, message):
        self.incoming.put(json.dumps(message))

    def close(self):
        self.closed = True
        self.incoming.put(self._CLOSED)


def test_cdp_connection_routes_calls_and_events():
    ws = FakeWebSocket()

    def respond(message):
        ws.emit({"id": message["id"], "result": {"answer": 42}})

    ws.on_send = respond
    connection = CDPConnection(ws)
    observed = []
    ready = threading.Event()

    def listener(method, params, session_id):
        observed.append((method, params, session_id))
        ready.set()

    connection.add_listener(listener)
    assert connection.call("Example.command", {"input": 1}) == {"answer": 42}
    ws.emit(
        {
            "method": "Network.responseReceived",
            "params": {"requestId": "r1"},
            "sessionId": "s1",
        }
    )
    assert ready.wait(1)
    assert observed == [
        ("Network.responseReceived", {"requestId": "r1"}, "s1")
    ]
    connection.close()
    assert ws.closed
    assert connection.is_closed


def test_cdp_connection_errors_never_echo_protocol_data():
    secret = "Bearer should-not-escape"
    ws = FakeWebSocket()

    def respond(message):
        ws.emit(
            {
                "id": message["id"],
                "error": {"code": -32000, "message": secret, "data": secret},
            }
        )

    ws.on_send = respond
    connection = CDPConnection(ws)
    with pytest.raises(CDPError) as caught:
        connection.call("Runtime.callFunctionOn", {"secret": secret})
    assert secret not in str(caught.value)
    assert "-32000" in str(caught.value)
    connection.close()


def test_cdp_websocket_uses_preconnected_loopback_socket_despite_proxy_env(
    monkeypatch,
):
    import websocket

    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.delenv("NO_PROXY", raising=False)
    raw_socket = object()
    observed = {}
    ws = FakeWebSocket()

    def connect_raw(address, timeout):
        observed["raw"] = (address, timeout)
        return raw_socket

    def connect_websocket(url, **kwargs):
        observed["websocket"] = (url, kwargs)
        return ws

    monkeypatch.setattr(cdp.socket, "create_connection", connect_raw)
    monkeypatch.setattr(websocket, "create_connection", connect_websocket)

    connection = CDPConnection.connect(
        "ws://127.0.0.1:45123/devtools/browser/owned", timeout=3
    )

    assert observed["raw"] == (("127.0.0.1", 45123), 3.0)
    options = observed["websocket"][1]
    assert options["socket"] is raw_socket
    assert not any(key.startswith("http_proxy") for key in options)
    connection.close()


def test_reader_exit_always_closes_underlying_websocket():
    ws = FakeWebSocket()
    connection = CDPConnection(ws)
    ws.incoming.put("not valid JSON")

    connection._reader.join(timeout=1)

    assert not connection._reader.is_alive()
    assert ws.closed
    assert connection.is_closed
    connection.close()  # idempotent


def _request_event(request_id, url, token, session="page", method="GET"):
    return (
        "Network.requestWillBeSent",
        {
            "requestId": request_id,
            "request": {
                "url": url,
                "method": method,
                "headers": {"Authorization": token},
            },
        },
        session,
    )


def _response_event(request_id, url, status, session="page"):
    return (
        "Network.responseReceived",
        {"requestId": request_id, "response": {"url": url, "status": status}},
        session,
    )


def _promote(auth, request_id="r", token="Bearer good", status=200, session="page"):
    auth.feed_event(*_request_event(request_id, CCI_URL, token, session))
    auth.feed_event(*_response_event(request_id, CCI_URL, status, session))


def test_authorization_promotes_only_successful_exact_cci_requests():
    auth = CCIAuthorization()
    secret = "Bearer top-secret-token"

    # Look-alike host and path-prefix confusion must never produce a lease.
    auth.feed_event(
        *_request_event("evil-host", "https://cci.cn-sh-01.sensecore.cn.evil.test/compute/cci/data/v2/apps", secret)
    )
    auth.feed_event(
        *_response_event("evil-host", "https://cci.cn-sh-01.sensecore.cn.evil.test/compute/cci/data/v2/apps", 200)
    )
    auth.feed_event(
        *_request_event("evil-path", cdp.CCI_API_BASE + "0/apps", secret)
    )
    auth.feed_event(
        *_response_event("evil-path", cdp.CCI_API_BASE + "0/apps", 200)
    )
    with pytest.raises(CDPTimeout):
        auth.wait(timeout=0)

    # A 401 candidate is explicitly not promoted.
    _promote(auth, "unauthorized", secret, status=401)
    with pytest.raises(CDPTimeout):
        auth.wait(timeout=0)

    _promote(auth, "ok", secret)
    lease = auth.wait(timeout=0)
    assert lease.generation == 1
    assert lease.authorization == secret
    assert secret not in repr(lease)
    assert secret not in repr(auth)


def test_authorization_handles_extra_info_order_and_session_scoping():
    auth = CCIAuthorization()
    token = "Bearer from-extra-info"

    # ExtraInfo can precede requestWillBeSent.  A matching requestId in a
    # different flattened session must not be allowed to complete it.
    auth.feed_event(
        "Network.requestWillBeSentExtraInfo",
        {"requestId": "same", "headers": {"authorization": token}},
        "session-a",
    )
    auth.feed_event(*_request_event("same", CCI_URL, "", "session-b"))
    auth.feed_event(*_response_event("same", CCI_URL, 204, "session-b"))
    with pytest.raises(CDPTimeout):
        auth.wait(timeout=0)

    auth.feed_event(*_request_event("same", CCI_URL, "", "session-a"))
    auth.feed_event(*_response_event("same", CCI_URL, 204, "session-a"))
    assert auth.wait(timeout=0).authorization == token


def test_authorization_uses_effective_200_when_raw_cache_status_arrives_first():
    auth = CCIAuthorization()
    token = "Bearer cached-response-token"

    auth.feed_event(*_request_event("cached", CCI_URL, token))
    auth.feed_event(
        "Network.responseReceivedExtraInfo",
        {"requestId": "cached", "statusCode": 304},
        "page",
    )
    with pytest.raises(CDPTimeout):
        auth.wait(timeout=0)

    auth.feed_event(*_response_event("cached", CCI_URL, 200))

    assert auth.wait(timeout=0).authorization == token
    assert auth.diagnostic_counts == {
        "exact_requests": 1,
        "bearer_requests": 1,
        "effective_2xx": 1,
        "promotions": 1,
    }
    assert token not in repr(auth.diagnostic_counts)


def test_authorization_keeps_successful_candidate_for_late_request_extra_info():
    auth = CCIAuthorization()
    token = "Bearer late-extra-info-token"

    auth.feed_event(*_request_event("late-header", CCI_URL, ""))
    auth.feed_event(*_response_event("late-header", CCI_URL, 200))
    auth.feed_event(
        "Network.loadingFinished",
        {"requestId": "late-header"},
        "page",
    )
    with pytest.raises(CDPTimeout):
        auth.wait(timeout=0)

    auth.feed_event(
        "Network.requestWillBeSentExtraInfo",
        {
            "requestId": "late-header",
            "headers": {"authorization": token},
        },
        "page",
    )

    assert auth.wait(timeout=0).authorization == token
    assert auth.diagnostic_counts == {
        "exact_requests": 1,
        "bearer_requests": 1,
        "effective_2xx": 1,
        "promotions": 1,
    }


def test_authorization_never_promotes_raw_2xx_over_effective_failure():
    auth = CCIAuthorization()
    token = "Bearer failed-effective-response"

    auth.feed_event(*_request_event("failed-effective", CCI_URL, token))
    auth.feed_event(
        "Network.responseReceivedExtraInfo",
        {"requestId": "failed-effective", "statusCode": 200},
        "page",
    )
    auth.feed_event(*_response_event("failed-effective", CCI_URL, 401))

    with pytest.raises(CDPTimeout):
        auth.wait(timeout=0)
    assert auth.diagnostic_counts == {
        "exact_requests": 1,
        "bearer_requests": 1,
        "effective_2xx": 0,
        "promotions": 0,
    }


def test_authorization_generation_prevents_stale_invalidation():
    auth = CCIAuthorization()
    _promote(auth, "one", "Bearer first")
    first = auth.current()
    _promote(auth, "two", "Bearer second")
    second = auth.current()
    assert first is not None and second is not None
    assert second.generation > first.generation

    auth.invalidate(first.generation)
    assert auth.current() == second
    auth.invalidate(second.generation)
    assert auth.current() is None


def test_auth_lease_repr_is_always_redacted():
    lease = AuthLease(7, "Bearer never-print-me")
    assert repr(lease) == "AuthLease(generation=7, authorization=<redacted>)"
    assert "never-print-me" not in str(lease)


def test_auth_candidate_repr_never_contains_bearer():
    secret = "Bearer candidate-must-not-print"
    candidate = cdp._AuthCandidate(touched=1.0, authorization=secret)
    assert secret not in repr(candidate)


class FakeCDP:
    def __init__(self):
        self.calls = []
        self.listeners = []
        self.fetch_results = []
        self.target_infos = []
        self.closed = False

    def add_listener(self, listener):
        self.listeners.append(listener)

    def remove_listener(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)

    def call(self, method, params=None, *, session_id=None, timeout=None):
        self.calls.append((method, params or {}, session_id, timeout))
        if method == "Target.createTarget":
            return {"targetId": "target-1"}
        if method == "Target.getTargets":
            return {"targetInfos": self.target_infos}
        if method == "Target.attachToTarget":
            return {"sessionId": "session-1"}
        if method == "Browser.getWindowForTarget":
            return {"windowId": 17}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame-1"}}}
        if method == "Page.createIsolatedWorld":
            count = sum(1 for call in self.calls if call[0] == method)
            return {"executionContextId": 100 + count}
        if method == "Runtime.callFunctionOn":
            if self.fetch_results:
                return self.fetch_results.pop(0)
            return {
                "result": {
                    "value": {"status": 200, "text": '{"result":"ok"}'}
                }
            }
        return {}

    def close(self):
        self.closed = True


def test_browser_transport_marks_owned_target_or_session_loss_as_broken():
    transport, _auth, fake = _started_transport()

    listener = fake.listeners[0]
    listener(
        "Target.detachedFromTarget",
        {"sessionId": "some-other-session"},
        None,
    )
    assert not transport.broken

    listener(
        "Target.detachedFromTarget",
        {"sessionId": "session-1"},
        None,
    )
    assert transport.broken
    transport.close()

    transport, _auth, fake = _started_transport()
    fake.listeners[0](
        "Target.targetDestroyed", {"targetId": "target-1"}, None
    )
    assert transport.broken
    transport.close()


def test_browser_transport_marks_connection_and_command_failure_as_broken():
    transport, _auth, fake = _started_transport()
    fake.is_closed = True
    assert transport.broken
    transport.close()

    class FailingCDP(FakeCDP):
        def call(self, method, params=None, *, session_id=None, timeout=None):
            if method == "Runtime.callFunctionOn":
                raise CDPError("session disappeared")
            return super().call(
                method, params, session_id=session_id, timeout=timeout
            )

    transport, _auth, _fake = _started_transport(FailingCDP())
    with pytest.raises(BrowserFetchError, match="browser request failed"):
        transport.request("GET", CCI_URL)
    assert transport.broken
    transport.close()


def test_browser_transport_auth_capture_is_limited_to_owned_session():
    fake = FakeCDP()
    auth = CCIAuthorization()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
        auth=auth,
    ).start()
    listener = fake.listeners[0]

    listener(*_request_event("foreign", CCI_URL, "Bearer foreign", "session-2"))
    listener(*_response_event("foreign", CCI_URL, 200, "session-2"))
    with pytest.raises(CDPTimeout):
        auth.wait(timeout=0)

    listener(*_request_event("owned", CCI_URL, "Bearer owned", "session-1"))
    listener(*_response_event("owned", CCI_URL, 200, "session-1"))
    assert auth.wait(timeout=0).authorization == "Bearer owned"
    transport.close()
    auth.close()


def test_browser_transport_custom_auth_capture_accepts_only_owned_exact_iam_request():
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
        auth_capture_base=cdp.SENSECORE_IAM_AUTH_CAPTURE_URL,
        auth_capture_exact_path=True,
        auth_capture_methods=("GET",),
    ).start()
    listener = fake.listeners[0]
    token = "Bearer owned-iam-token"
    capture_url = cdp.SENSECORE_IAM_AUTH_CAPTURE_URL

    rejected = [
        ("foreign-session", capture_url, "session-2"),
        (
            "lookalike-host",
            "https://iam.sensecoreapi.cn.evil.test/iam/idp/v1/myRegionAndAzs",
            "session-1",
        ),
        ("lookalike-path", capture_url + "0", "session-1"),
        ("descendant-path", capture_url + "/child", "session-1"),
        ("encoded-descendant", capture_url + "%2Fchild", "session-1"),
    ]
    for request_id, url, session_id in rejected:
        listener(*_request_event(request_id, url, token, session_id))
        listener(*_response_event(request_id, url, 200, session_id))

    listener(*_request_event("wrong-method", capture_url, token, "session-1", "POST"))
    listener(*_response_event("wrong-method", capture_url, 200, "session-1"))

    with pytest.raises(CDPTimeout):
        transport.auth.wait(timeout=0)

    listener(*_request_event("owned-iam", capture_url, token, "session-1"))
    listener(*_response_event("owned-iam", capture_url, 204, "session-1"))

    lease = transport.auth.wait(timeout=0)
    assert lease.authorization == token
    assert transport.auth_capture_base == capture_url
    assert transport.auth_capture_exact_path is True
    assert transport.auth_capture_methods == ("GET",)
    assert transport.auth.api_base == capture_url
    assert transport.auth.exact_path is True
    assert transport.auth.allowed_methods == frozenset(("GET",))
    assert transport.auth.diagnostic_counts == {
        "exact_requests": 1,
        "bearer_requests": 1,
        "effective_2xx": 1,
        "promotions": 1,
    }
    transport.close()


def _started_transport(fake=None):
    fake = fake or FakeCDP()
    auth = CCIAuthorization()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
        auth=auth,
    ).start()
    _promote(auth, token="Bearer browser-token")
    return transport, auth, fake


def _runtime_value(value):
    return {"result": {"value": value}}


_VALID_LOGIN_CHALLENGE_URL = (
    "https://signin.sensecore.cn/"
    "?login_challenge=opaque-fixture_ABC.123~x%2Fy&platform=console"
)
_VALID_OAUTH_AUTHORIZATION_URL = (
    "https://signin.sensecore.cn/oauth2/auth"
    "?client_id=console-fixture&state=opaque-oauth-fixture"
)
_VALID_RETURN_OAUTH_AUTHORIZATION_URL = (
    "https://signin.sensecore.cn/oauth2/auth"
    "?client_id=console-fixture&state=opaque-oauth-return-fixture"
)
_VALID_SECOND_RETURN_OAUTH_AUTHORIZATION_URL = (
    "https://signin.sensecore.cn/oauth2/auth"
    "?client_id=console-fixture&state=opaque-oauth-second-return-fixture"
)
_VALID_IAM_AUTHORIZATION_URL = (
    "https://iam.sensecoreapi.cn/authorize"
    "?request=opaque-iam-fixture"
)
_VALID_SECOND_IAM_AUTHORIZATION_URL = (
    "https://iam.sensecoreapi.cn/authorize"
    "?request=opaque-iam-second-fixture"
)
_VALID_CONSOLE_LOGIN_TERMINAL_URL = "https://console.sensecore.cn/home"
_VALID_CONSOLE_HOME_OAUTH_RESULT_URL = (
    "https://console.sensecore.cn/home"
    "?code=fixture-code&scope=openid%20profile&state=fixture-state"
)
_VALID_CONSOLE_CALLBACK_URL = (
    "https://console.sensecore.cn/auth/callback?code=fixture"
)
_REGIONAL_CONSOLE_HOME_URL = "https://console.sensecore.cn/cn-sh-01/home"


def _login_document_request(
    url,
    *,
    request_id,
    loader_id="login-loader",
    frame_id="frame-1",
    redirect_url=None,
    redirect_status=302,
    method="GET",
    initiator=None,
    session_id="session-1",
):
    params = {
        "requestId": request_id,
        "loaderId": loader_id,
        "documentURL": url,
        "frameId": frame_id,
        "type": "Document",
        "request": {"url": url, "method": method, "headers": {}},
    }
    if initiator is not None:
        params["initiator"] = {"type": initiator}
    if redirect_url is not None:
        params["redirectResponse"] = {
            "url": redirect_url,
            "status": redirect_status,
            "headers": {},
        }
    return "Network.requestWillBeSent", params, session_id


def _login_frame_navigated(
    url,
    *,
    loader_id="login-loader",
    frame_id="frame-1",
    parent_id=None,
    session_id="session-1",
):
    frame = {
        "id": frame_id,
        "loaderId": loader_id,
        "url": url,
        "securityOrigin": "https://signin.sensecore.cn",
        "mimeType": "text/html",
    }
    if parent_id is not None:
        frame["parentId"] = parent_id
    return "Page.frameNavigated", {"frame": frame, "type": "Navigation"}, session_id


def _login_frame_scheduled(
    url,
    *,
    frame_id="frame-1",
    reason="scriptInitiated",
    session_id="session-1",
):
    return (
        "Page.frameScheduledNavigation",
        {"frameId": frame_id, "delay": 0, "reason": reason, "url": url},
        session_id,
    )


def _login_frame_requested(
    url,
    *,
    frame_id="frame-1",
    reason="scriptInitiated",
    disposition="currentTab",
    session_id="session-1",
):
    return (
        "Page.frameRequestedNavigation",
        {
            "frameId": frame_id,
            "reason": reason,
            "url": url,
            "disposition": disposition,
        },
        session_id,
    )


def _login_frame_cleared(*, frame_id="frame-1", session_id="session-1"):
    return (
        "Page.frameClearedScheduledNavigation",
        {"frameId": frame_id},
        session_id,
    )


def _emit_renderer_intent(listener, url, **kwargs):
    listener(*_login_frame_scheduled(url, **kwargs))
    listener(*_login_frame_requested(url, **kwargs))


def _emit_real_entry_commit(fake):
    listener = fake.listeners[0]
    listener(
        *_login_document_request(
            cdp.SENSECORE_LOGIN_URL,
            request_id="real-entry",
            loader_id="real-entry-loader",
            initiator="other",
        )
    )
    listener(
        *_login_frame_navigated(
            cdp.SENSECORE_LOGIN_URL,
            loader_id="real-entry-loader",
        )
    )


def _emit_real_console_commit(fake):
    _emit_real_entry_commit(fake)
    listener = fake.listeners[0]
    _emit_renderer_intent(listener, cdp.SENSECORE_CONSOLE_ROOT_URL)
    listener(
        *_login_document_request(
            cdp.SENSECORE_CONSOLE_ROOT_URL,
            request_id="real-console-one",
            loader_id="real-console-loader-one",
            initiator="script",
        )
    )
    listener(
        *_login_frame_navigated(
            cdp.SENSECORE_CONSOLE_ROOT_URL,
            loader_id="real-console-loader-one",
        )
    )


def _emit_regional_console_home_document(fake):
    """Move the owned Console root document to the regional home route."""
    listener = fake.listeners[0]
    _emit_renderer_intent(listener, _REGIONAL_CONSOLE_HOME_URL)
    listener(
        *_login_document_request(
            _REGIONAL_CONSOLE_HOME_URL,
            request_id="regional-console-home",
            loader_id="regional-console-home-loader",
            initiator="script",
        )
    )
    listener(
        *_login_frame_navigated(
            _REGIONAL_CONSOLE_HOME_URL,
            loader_id="regional-console-home-loader",
        )
    )


def _emit_regional_console_home_within_document(fake):
    """Model Console's history-API transition without a new Document loader."""
    fake.listeners[0](
        "Page.navigatedWithinDocument",
        {
            "frameId": "frame-1",
            "url": _REGIONAL_CONSOLE_HOME_URL,
            "navigationType": "historyApi",
        },
        "session-1",
    )


def _emit_console_home_oauth_result_within_document(fake):
    """Model the post-login History API preservation of OAuth result keys."""
    fake.listeners[0](
        "Page.navigatedWithinDocument",
        {
            "frameId": "frame-1",
            "url": _VALID_CONSOLE_HOME_OAUTH_RESULT_URL,
            "navigationType": "historyApi",
        },
        "session-1",
    )


def _emit_console_oauth_iam_challenge(fake, *, prefix="regional-login"):
    """Drive a trusted Console page through OAuth/IAM to the IAM form."""
    listener = fake.listeners[0]
    _emit_renderer_intent(listener, _VALID_OAUTH_AUTHORIZATION_URL)
    listener(
        *_login_document_request(
            _VALID_OAUTH_AUTHORIZATION_URL,
            request_id=f"{prefix}-oauth",
            loader_id=f"{prefix}-loader",
            initiator="script",
        )
    )
    listener(
        *_login_document_request(
            _VALID_IAM_AUTHORIZATION_URL,
            request_id=f"{prefix}-oauth",
            loader_id=f"{prefix}-loader",
            redirect_url=_VALID_OAUTH_AUTHORIZATION_URL,
            redirect_status=302,
        )
    )
    listener(
        *_login_document_request(
            _VALID_LOGIN_CHALLENGE_URL,
            request_id=f"{prefix}-oauth",
            loader_id=f"{prefix}-loader",
            redirect_url=_VALID_IAM_AUTHORIZATION_URL,
            redirect_status=302,
        )
    )
    listener(
        *_login_frame_navigated(
            _VALID_LOGIN_CHALLENGE_URL,
            loader_id=f"{prefix}-loader",
        )
    )


def _emit_direct_terminal_commit(fake, terminal_url):
    """Leave the IAM challenge for one trusted Console terminal candidate."""
    _emit_real_renderer_challenge_flow(fake)
    listener = fake.listeners[0]
    listener(
        *_login_document_request(
            terminal_url,
            request_id="direct-console-terminal",
            loader_id="direct-console-terminal-loader",
            redirect_url=_VALID_LOGIN_CHALLENGE_URL,
            redirect_status=302,
        )
    )
    listener(
        *_login_frame_navigated(
            terminal_url,
            loader_id="direct-console-terminal-loader",
        )
    )


def _emit_real_renderer_challenge_flow(
    fake, *, replace_console_request=False, commit_challenge=True
):
    """Drive the token-free shape observed through the real SSH/browser path."""
    listener = fake.listeners[0]
    _emit_real_entry_commit(fake)

    _emit_renderer_intent(listener, cdp.SENSECORE_CONSOLE_ROOT_URL)
    listener(
        *_login_document_request(
            cdp.SENSECORE_CONSOLE_ROOT_URL,
            request_id="real-console-one",
            loader_id="real-console-loader-one",
            initiator="script",
        )
    )
    if replace_console_request:
        # Real Chromium clears the scheduled timer after the Document request,
        # then may emit the same renderer navigation with a replacement
        # request/loader.  Neither clear means that the network navigation was
        # cancelled; the second loader is the one that must commit.
        listener(*_login_frame_cleared())
        _emit_renderer_intent(listener, cdp.SENSECORE_CONSOLE_ROOT_URL)
        listener(
            *_login_document_request(
                cdp.SENSECORE_CONSOLE_ROOT_URL,
                request_id="real-console-two",
                loader_id="real-console-loader-two",
                initiator="script",
            )
        )
        listener(*_login_frame_cleared())
        console_loader = "real-console-loader-two"
    else:
        console_loader = "real-console-loader-one"
    listener(
        *_login_frame_navigated(
            cdp.SENSECORE_CONSOLE_ROOT_URL,
            loader_id=console_loader,
        )
    )

    _emit_renderer_intent(listener, _VALID_OAUTH_AUTHORIZATION_URL)
    listener(
        *_login_document_request(
            _VALID_OAUTH_AUTHORIZATION_URL,
            request_id="real-oauth-chain",
            loader_id="real-oauth-loader",
            initiator="script",
        )
    )
    listener(
        *_login_document_request(
            _VALID_IAM_AUTHORIZATION_URL,
            request_id="real-oauth-chain",
            loader_id="real-oauth-loader",
            redirect_url=_VALID_OAUTH_AUTHORIZATION_URL,
        )
    )
    listener(
        *_login_document_request(
            _VALID_LOGIN_CHALLENGE_URL,
            request_id="real-oauth-chain",
            loader_id="real-oauth-loader",
            redirect_url=_VALID_IAM_AUTHORIZATION_URL,
        )
    )
    if commit_challenge:
        listener(
            *_login_frame_navigated(
                _VALID_LOGIN_CHALLENGE_URL,
                loader_id="real-oauth-loader",
            )
        )


def _submitted_transport(fake=None):
    fake = fake or FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    _emit_real_renderer_challenge_flow(fake)
    fake.fetch_results.append(_runtime_value("submitted"))
    assert transport.submit_login("fixture-user", "fixture-password") == "submitted"
    return transport, fake


def _begin_oauth_route(fake, *, request_id="route-chain", loader_id="route-loader"):
    listener = fake.listeners[0]
    _emit_renderer_intent(listener, _VALID_OAUTH_AUTHORIZATION_URL)
    listener(
        *_login_document_request(
            _VALID_OAUTH_AUTHORIZATION_URL,
            request_id=request_id,
            loader_id=loader_id,
            initiator="script",
        )
    )


def _emit_oauth_iam_round(
    fake,
    oauth_url,
    iam_url,
    return_oauth_url,
    *,
    request_id="route-chain",
    loader_id="route-loader",
):
    listener = fake.listeners[0]
    listener(
        *_login_document_request(
            iam_url,
            request_id=request_id,
            loader_id=loader_id,
            redirect_url=oauth_url,
            redirect_status=302,
        )
    )
    listener(
        *_login_document_request(
            return_oauth_url,
            request_id=request_id,
            loader_id=loader_id,
            redirect_url=iam_url,
            redirect_status=302,
        )
    )


def _finish_oauth_route(
    fake,
    oauth_url,
    *,
    request_id="route-chain",
    loader_id="route-loader",
    commit=True,
):
    listener = fake.listeners[0]
    listener(
        *_login_document_request(
            _VALID_CONSOLE_LOGIN_TERMINAL_URL,
            request_id=request_id,
            loader_id=loader_id,
            redirect_url=oauth_url,
            redirect_status=303,
        )
    )
    if commit:
        listener(
            *_login_frame_navigated(
                _VALID_CONSOLE_LOGIN_TERMINAL_URL,
                loader_id=loader_id,
            )
        )


def _emit_ready_login_flow(fake):
    """Drive one fake owned target through challenge to provisional Console."""

    listener = fake.listeners[0]
    listener(
        *_login_document_request(
            cdp.SENSECORE_LOGIN_URL,
            request_id="fixture-login-chain",
        )
    )
    listener(
        *_login_document_request(
            _VALID_LOGIN_CHALLENGE_URL,
            request_id="fixture-login-chain",
            redirect_url=cdp.SENSECORE_LOGIN_URL,
        )
    )
    listener(*_login_frame_navigated(_VALID_LOGIN_CHALLENGE_URL))
    terminal_url = "https://console.sensecore.cn/auth/callback?code=fixture"
    listener(
        *_login_document_request(
            terminal_url,
            request_id="fixture-login-terminal",
            redirect_url=_VALID_LOGIN_CHALLENGE_URL,
        )
    )
    listener(*_login_frame_navigated(terminal_url))
    return terminal_url


def _age_console_landing_past_grace(transport):
    """Advance only the fixture's proven landing age, without wall-clock sleep."""
    assert transport.login_diagnostic == "console"
    with transport._login_flow_lock:
        assert transport._login_console_committed_at is not None
        transport._login_console_committed_at = (
            cdp.time.monotonic()
            - cdp._LOGIN_CONSOLE_ROOT_GRACE_SECONDS
            - 0.01
        )


def _promote_stable_console_landing(transport, fake):
    """Make a provisional landing explicitly pass the quiet-window proof."""
    _age_console_landing_past_grace(transport)
    fake.fetch_results.append(_runtime_value("departed"))
    assert transport.inspect_login_page(timeout=0) == "departed"
    assert transport.login_diagnostic == "ready"


@pytest.mark.parametrize(
    "url",
    [
        _VALID_LOGIN_CHALLENGE_URL,
        (
            "https://signin.sensecore.cn/"
            "?platform=console&login_challenge=opaque-fixture_ABC.123~x%2Fy"
        ),
    ],
)
def test_canonical_login_challenge_url_accepts_only_the_two_required_parameters(
    url,
):
    assert cdp._canonical_login_challenge_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://signin.sensecore.cn/?login_challenge=one&platform=console",
        "https://fixture@signin.sensecore.cn/?login_challenge=one&platform=console",
        "https://signin.sensecore.cn:444/?login_challenge=one&platform=console",
        "https://signin.sensecore.cn.evil.test/?login_challenge=one&platform=console",
        "https://evil.signin.sensecore.cn/?login_challenge=one&platform=console",
        "https://signin.sensecore.cn/login?login_challenge=one&platform=console",
        "https://signin.sensecore.cn/?login_challenge=one&platform=console#continue",
        "https://signin.sensecore.cn/?login_challenge=&platform=console",
        "https://signin.sensecore.cn/?login_challenge=one",
        "https://signin.sensecore.cn/?platform=console",
        "https://signin.sensecore.cn/?login_challenge=one&platform=other",
        "https://signin.sensecore.cn/?login_challenge=one&IAM&platform=console",
        "https://signin.sensecore.cn/?login_challenge=one&platform=console&next=x",
        (
            "https://signin.sensecore.cn/"
            "?login_challenge=one&login_challenge=two&platform=console"
        ),
        (
            "https://signin.sensecore.cn/"
            "?login_challenge=one&platform=console&platform=console"
        ),
        "https://signin.sensecore.cn/?%6cogin_challenge=one&platform=console",
        "https://signin.sensecore.cn/?login_challenge=%00&platform=console",
        "https://signin.sensecore.cn/?login_challenge=%20one&platform=console",
        "\nhttps://signin.sensecore.cn/?login_challenge=one&platform=console",
    ],
)
def test_canonical_login_challenge_url_rejects_lookalikes(url):
    assert cdp._canonical_login_challenge_url(url) is None


@pytest.mark.parametrize(
    ("validator", "valid", "invalid"),
    [
        (
            cdp._canonical_oauth_authorization_url,
            _VALID_OAUTH_AUTHORIZATION_URL,
            [
                _VALID_OAUTH_AUTHORIZATION_URL.replace("https://", "http://"),
                _VALID_OAUTH_AUTHORIZATION_URL.replace(
                    "signin.sensecore.cn", "fixture@signin.sensecore.cn"
                ),
                _VALID_OAUTH_AUTHORIZATION_URL.replace(
                    "signin.sensecore.cn", "signin.sensecore.cn:444"
                ),
                _VALID_OAUTH_AUTHORIZATION_URL.replace(
                    "signin.sensecore.cn", "signin.sensecore.cn.evil.test"
                ),
                _VALID_OAUTH_AUTHORIZATION_URL.replace(
                    "/oauth2/auth", "/oauth2/authorize"
                ),
                _VALID_OAUTH_AUTHORIZATION_URL + "#error=access_denied",
            ],
        ),
        (
            cdp._canonical_iam_authorization_url,
            _VALID_IAM_AUTHORIZATION_URL,
            [
                _VALID_IAM_AUTHORIZATION_URL.replace("https://", "http://"),
                _VALID_IAM_AUTHORIZATION_URL.replace(
                    "iam.sensecoreapi.cn", "fixture@iam.sensecoreapi.cn"
                ),
                _VALID_IAM_AUTHORIZATION_URL.replace(
                    "iam.sensecoreapi.cn", "iam.sensecoreapi.cn:444"
                ),
                _VALID_IAM_AUTHORIZATION_URL.replace(
                    "iam.sensecoreapi.cn", "iam.sensecoreapi.cn.evil.test"
                ),
                _VALID_IAM_AUTHORIZATION_URL + "#continue",
            ],
        ),
    ],
)
def test_intermediate_login_urls_are_strict_and_never_canonicalize(
    validator, valid, invalid
):
    assert validator(valid) == valid
    for value in invalid:
        assert validator(value) is None


def test_console_terminal_rejects_fragment_errors_and_origin_lookalikes():
    assert cdp._is_console_login_terminal_url(
        "https://console.sensecore.cn/auth/callback?code=fixture"
    )
    for value in (
        "http://console.sensecore.cn/auth/callback?code=fixture",
        "https://console.sensecore.cn.evil.test/auth/callback?code=fixture",
        "https://fixture@console.sensecore.cn/auth/callback?code=fixture",
        "https://console.sensecore.cn:444/auth/callback?code=fixture",
        "https://console.sensecore.cn/auth/callback#error=access_denied",
        "https://console.sensecore.cn/auth/callback?%65rror=access_denied",
    ):
        assert not cdp._is_console_login_terminal_url(value)

    assert cdp._canonical_console_login_terminal_url(
        _VALID_CONSOLE_LOGIN_TERMINAL_URL
    ) == _VALID_CONSOLE_LOGIN_TERMINAL_URL
    for value in (
        "https://console.sensecore.cn/",
        "https://console.sensecore.cn/home/",
        "https://console.sensecore.cn/home-lookalike",
        "https://console.sensecore.cn.evil.test/home",
        "https://console.sensecore.cn/home#continue",
    ):
        assert cdp._canonical_console_login_terminal_url(value) is None


def test_console_landing_accepts_only_exact_oauth_result_query_shape():
    assert (
        cdp._canonical_console_landing_url(
            _VALID_CONSOLE_HOME_OAUTH_RESULT_URL
        )
        == _VALID_CONSOLE_HOME_OAUTH_RESULT_URL
    )
    regional_result = (
        _REGIONAL_CONSOLE_HOME_URL
        + "?code=fixture-code&scope=openid%20profile&state=fixture-state"
    )
    assert cdp._canonical_console_landing_url(regional_result) == regional_result
    for value in (
        "https://console.sensecore.cn/home?code=x&scope=y",
        "https://console.sensecore.cn/home?code=x&scope=y&state=",
        "https://console.sensecore.cn/home?code=x&scope=y&state=z&extra=1",
        "https://console.sensecore.cn/home?code=x&scope=y&state=z&code=again",
        "https://console.sensecore.cn/home?%63ode=x&scope=y&state=z",
        "https://console.sensecore.cn/home?code=x&scope=y&state=z#continue",
        "https://console.sensecore.cn/cn-sh-02/home?code=x&scope=y&state=z",
    ):
        assert cdp._canonical_console_landing_url(value) is None


def test_real_renderer_chain_requires_every_request_and_commit_before_credentials():
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()

    _emit_real_renderer_challenge_flow(fake, commit_challenge=False)
    assert transport.login_diagnostic == "challenge_pending"
    fake.fetch_results.append(_runtime_value("password_form"))
    assert transport.inspect_login_page(timeout=0) == "untrusted"
    precommit_call = [
        item for item in fake.calls if item[0] == "Runtime.callFunctionOn"
    ][-1]
    assert precommit_call[1]["arguments"] == [{"value": ""}]
    assert transport.submit_login("fixture-user", "fixture-password") == "rejected"

    fake.listeners[0](
        *_login_frame_navigated(
            _VALID_LOGIN_CHALLENGE_URL,
            loader_id="real-oauth-loader",
        )
    )
    fake.fetch_results.append(_runtime_value("password_form"))
    assert transport.inspect_login_page(timeout=0) == "password_form"
    postcommit_call = [
        item for item in fake.calls if item[0] == "Runtime.callFunctionOn"
    ][-1]
    assert postcommit_call[1]["arguments"] == [
        {"value": _VALID_LOGIN_CHALLENGE_URL}
    ]
    assert _VALID_OAUTH_AUTHORIZATION_URL not in repr(transport)
    assert _VALID_IAM_AUTHORIZATION_URL not in repr(transport)
    assert _VALID_LOGIN_CHALLENGE_URL not in transport.login_diagnostic
    transport.close()


def test_real_renderer_chain_allows_one_bound_console_request_replacement():
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()

    _emit_real_renderer_challenge_flow(fake, replace_console_request=True)
    assert transport.login_diagnostic == "challenge"
    assert transport._trusted_login_challenge() == _VALID_LOGIN_CHALLENGE_URL
    transport.close()


@pytest.mark.parametrize(
    "case",
    [
        "missing_schedule",
        "wrong_reason",
        "wrong_disposition",
        "wrong_target",
        "foreign_intent",
        "wrong_method",
        "wrong_initiator",
        "reused_request",
        "reused_loader",
        "wrong_commit_loader",
        "second_cancellation",
        "second_replacement",
        "replacement_old_request_replayed",
        "replacement_old_loader_committed",
    ],
)
def test_renderer_bootstrap_rejects_incomplete_or_changed_navigation(case):
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    _emit_real_entry_commit(fake)
    listener = fake.listeners[0]

    if case == "missing_schedule":
        listener(*_login_frame_requested(cdp.SENSECORE_CONSOLE_ROOT_URL))
    elif case == "wrong_reason":
        listener(
            *_login_frame_scheduled(
                cdp.SENSECORE_CONSOLE_ROOT_URL,
                reason="anchorClick",
            )
        )
    elif case == "wrong_disposition":
        listener(*_login_frame_scheduled(cdp.SENSECORE_CONSOLE_ROOT_URL))
        listener(
            *_login_frame_requested(
                cdp.SENSECORE_CONSOLE_ROOT_URL,
                disposition="newWindow",
            )
        )
    elif case == "wrong_target":
        listener(
            *_login_frame_scheduled(
                cdp.SENSECORE_CONSOLE_ROOT_URL + "?next=signin"
            )
        )
    elif case == "foreign_intent":
        _emit_renderer_intent(
            listener,
            cdp.SENSECORE_CONSOLE_ROOT_URL,
            session_id="session-foreign",
        )
        listener(
            *_login_document_request(
                cdp.SENSECORE_CONSOLE_ROOT_URL,
                request_id="console-new",
                loader_id="console-new-loader",
                initiator="script",
            )
        )
    elif case in {
        "wrong_method",
        "wrong_initiator",
        "reused_request",
        "reused_loader",
        "wrong_commit_loader",
    }:
        _emit_renderer_intent(listener, cdp.SENSECORE_CONSOLE_ROOT_URL)
        listener(
            *_login_document_request(
                cdp.SENSECORE_CONSOLE_ROOT_URL,
                request_id=(
                    "real-entry" if case == "reused_request" else "console-new"
                ),
                loader_id=(
                    "real-entry-loader"
                    if case == "reused_loader"
                    else "console-new-loader"
                ),
                method="POST" if case == "wrong_method" else "GET",
                initiator="other" if case == "wrong_initiator" else "script",
            )
        )
        if case == "wrong_commit_loader" and not transport.login_diagnostic.startswith(
            "unsafe"
        ):
            listener(
                *_login_frame_navigated(
                    cdp.SENSECORE_CONSOLE_ROOT_URL,
                    loader_id="different-console-loader",
                )
            )
    elif case == "second_cancellation":
        _emit_renderer_intent(listener, cdp.SENSECORE_CONSOLE_ROOT_URL)
        listener(*_login_frame_cleared())
        _emit_renderer_intent(listener, cdp.SENSECORE_CONSOLE_ROOT_URL)
        listener(*_login_frame_cleared())
    else:
        _emit_renderer_intent(listener, cdp.SENSECORE_CONSOLE_ROOT_URL)
        listener(
            *_login_document_request(
                cdp.SENSECORE_CONSOLE_ROOT_URL,
                request_id="console-one",
                loader_id="console-one-loader",
                initiator="script",
            )
        )
        listener(*_login_frame_cleared())
        _emit_renderer_intent(listener, cdp.SENSECORE_CONSOLE_ROOT_URL)
        listener(
            *_login_document_request(
                cdp.SENSECORE_CONSOLE_ROOT_URL,
                request_id="console-two",
                loader_id="console-two-loader",
                initiator="script",
            )
        )
        listener(*_login_frame_cleared())
        if case == "second_replacement":
            listener(*_login_frame_scheduled(cdp.SENSECORE_CONSOLE_ROOT_URL))
        elif case == "replacement_old_request_replayed":
            listener(
                *_login_document_request(
                    cdp.SENSECORE_CONSOLE_ROOT_URL,
                    request_id="console-one",
                    loader_id="console-one-loader",
                    initiator="script",
                )
            )
        else:
            listener(
                *_login_frame_navigated(
                    cdp.SENSECORE_CONSOLE_ROOT_URL,
                    loader_id="console-one-loader",
                )
            )

    assert transport.login_diagnostic.startswith("unsafe:"), case
    assert transport._trusted_login_challenge() == ""
    assert transport.submit_login("fixture-user", "fixture-password") == "rejected"
    transport.close()


@pytest.mark.parametrize(
    "case",
    [
        "oauth_without_intent",
        "oauth_wrong_path",
        "iam_wrong_source",
        "iam_wrong_status",
        "iam_wrong_request",
        "iam_wrong_loader",
        "iam_wrong_method",
        "iam_wrong_host",
        "challenge_wrong_source",
        "challenge_wrong_status",
        "challenge_wrong_request",
        "challenge_wrong_loader",
        "challenge_noncanonical",
        "challenge_wrong_commit_loader",
    ],
)
def test_renderer_oauth_and_iam_redirect_chain_fails_closed_on_mismatch(case):
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    _emit_real_console_commit(fake)
    listener = fake.listeners[0]

    if case == "oauth_without_intent":
        listener(
            *_login_document_request(
                _VALID_OAUTH_AUTHORIZATION_URL,
                request_id="oauth-chain",
                loader_id="oauth-loader",
                initiator="script",
            )
        )
    elif case == "oauth_wrong_path":
        _emit_renderer_intent(
            listener,
            _VALID_OAUTH_AUTHORIZATION_URL.replace("/oauth2/auth", "/oauth2/login"),
        )
    else:
        _emit_renderer_intent(listener, _VALID_OAUTH_AUTHORIZATION_URL)
        listener(
            *_login_document_request(
                _VALID_OAUTH_AUTHORIZATION_URL,
                request_id="oauth-chain",
                loader_id="oauth-loader",
                initiator="script",
            )
        )
        iam_url = (
            _VALID_IAM_AUTHORIZATION_URL.replace(
                "iam.sensecoreapi.cn", "iam.sensecoreapi.cn.evil.test"
            )
            if case == "iam_wrong_host"
            else _VALID_IAM_AUTHORIZATION_URL
        )
        listener(
            *_login_document_request(
                iam_url,
                request_id="different-request"
                if case == "iam_wrong_request"
                else "oauth-chain",
                loader_id="different-loader"
                if case == "iam_wrong_loader"
                else "oauth-loader",
                redirect_url="https://evil.test/oauth"
                if case == "iam_wrong_source"
                else _VALID_OAUTH_AUTHORIZATION_URL,
                redirect_status=200 if case == "iam_wrong_status" else 302,
                method="POST" if case == "iam_wrong_method" else "GET",
            )
        )
        if case.startswith("challenge_"):
            challenge_url = (
                _VALID_LOGIN_CHALLENGE_URL + "&next=console"
                if case == "challenge_noncanonical"
                else _VALID_LOGIN_CHALLENGE_URL
            )
            listener(
                *_login_document_request(
                    challenge_url,
                    request_id="different-request"
                    if case == "challenge_wrong_request"
                    else "oauth-chain",
                    loader_id="different-loader"
                    if case == "challenge_wrong_loader"
                    else "oauth-loader",
                    redirect_url="https://evil.test/iam"
                    if case == "challenge_wrong_source"
                    else _VALID_IAM_AUTHORIZATION_URL,
                    redirect_status=200
                    if case == "challenge_wrong_status"
                    else 302,
                )
            )
            if case == "challenge_wrong_commit_loader" and not (
                transport.login_diagnostic.startswith("unsafe")
            ):
                listener(
                    *_login_frame_navigated(
                        _VALID_LOGIN_CHALLENGE_URL,
                        loader_id="different-challenge-loader",
                    )
                )

    assert transport.login_diagnostic.startswith("unsafe:"), case
    assert transport._trusted_login_challenge() == ""
    transport.close()


def test_post_submit_oauth_route_reaches_exact_console_home_once():
    transport, fake = _submitted_transport()

    _begin_oauth_route(fake)
    assert transport.login_diagnostic == "submitted_oauth"
    assert transport._trusted_login_challenge() == ""
    assert transport.submit_login("fixture-user", "fixture-password") == "rejected"
    fake.fetch_results.append(_runtime_value("untrusted"))
    assert transport.inspect_login_page(timeout=0) == "loading"
    _emit_oauth_iam_round(
        fake,
        _VALID_OAUTH_AUTHORIZATION_URL,
        _VALID_IAM_AUTHORIZATION_URL,
        _VALID_RETURN_OAUTH_AUTHORIZATION_URL,
    )
    _finish_oauth_route(fake, _VALID_RETURN_OAUTH_AUTHORIZATION_URL)
    _promote_stable_console_landing(transport, fake)

    assert transport.login_diagnostic == "ready"
    sensitive_calls = [
        call
        for call in fake.calls
        if call[0] == "Runtime.callFunctionOn"
        and call[1].get("functionDeclaration") == cdp._LOGIN_SUBMIT_FUNCTION
    ]
    assert len(sensitive_calls) == 1
    transport.close()


def test_persistent_sso_allows_two_bounded_iam_round_trips_to_console_home():
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    _emit_real_console_commit(fake)
    _begin_oauth_route(fake)
    _emit_oauth_iam_round(
        fake,
        _VALID_OAUTH_AUTHORIZATION_URL,
        _VALID_IAM_AUTHORIZATION_URL,
        _VALID_RETURN_OAUTH_AUTHORIZATION_URL,
    )
    _emit_oauth_iam_round(
        fake,
        _VALID_RETURN_OAUTH_AUTHORIZATION_URL,
        _VALID_SECOND_IAM_AUTHORIZATION_URL,
        _VALID_SECOND_RETURN_OAUTH_AUTHORIZATION_URL,
    )
    _finish_oauth_route(fake, _VALID_SECOND_RETURN_OAUTH_AUTHORIZATION_URL)
    _promote_stable_console_landing(transport, fake)

    assert transport.login_diagnostic == "ready"
    assert transport.submit_login("fixture-user", "fixture-password") == "rejected"
    assert not [
        call
        for call in fake.calls
        if call[0] == "Runtime.callFunctionOn"
        and call[1].get("functionDeclaration") == cdp._LOGIN_SUBMIT_FUNCTION
    ]
    transport.close()


def test_stable_console_root_becomes_sso_candidate_only_after_grace(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(cdp.time, "monotonic", lambda: now[0])
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    _emit_real_console_commit(fake)

    fake.fetch_results.append(_runtime_value("departed"))
    assert transport.inspect_login_page(timeout=0) == "loading"
    assert transport.login_diagnostic == "console"

    now[0] += cdp._LOGIN_CONSOLE_ROOT_GRACE_SECONDS + 0.01
    fake.fetch_results.append(_runtime_value("departed"))
    assert transport.inspect_login_page(timeout=0) == "departed"
    assert transport.login_diagnostic == "ready"
    assert transport.submit_login("fixture-user", "fixture-password") == "rejected"
    transport.close()


def test_console_root_oauth_intent_prevents_premature_sso_promotion(monkeypatch):
    now = [200.0]
    monkeypatch.setattr(cdp.time, "monotonic", lambda: now[0])
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    _emit_real_console_commit(fake)
    listener = fake.listeners[0]
    listener(*_login_frame_scheduled(_VALID_OAUTH_AUTHORIZATION_URL))

    now[0] += cdp._LOGIN_CONSOLE_ROOT_GRACE_SECONDS + 1
    fake.fetch_results.append(_runtime_value("departed"))
    assert transport.inspect_login_page(timeout=0) == "loading"
    assert transport.login_diagnostic == "console"

    listener(*_login_frame_requested(_VALID_OAUTH_AUTHORIZATION_URL))
    listener(
        *_login_document_request(
            _VALID_OAUTH_AUTHORIZATION_URL,
            request_id="route-chain",
            loader_id="route-loader",
            initiator="script",
        )
    )
    _emit_oauth_iam_round(
        fake,
        _VALID_OAUTH_AUTHORIZATION_URL,
        _VALID_IAM_AUTHORIZATION_URL,
        _VALID_RETURN_OAUTH_AUTHORIZATION_URL,
    )
    _finish_oauth_route(fake, _VALID_RETURN_OAUTH_AUTHORIZATION_URL)
    _promote_stable_console_landing(transport, fake)
    assert transport.login_diagnostic == "ready"
    transport.close()


def test_cold_console_root_is_not_ready_after_only_one_second(monkeypatch):
    now = [300.0]
    monkeypatch.setattr(cdp.time, "monotonic", lambda: now[0])
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
    ).start()
    _emit_real_console_commit(fake)

    now[0] += 1.0
    fake.fetch_results.append(_runtime_value("departed"))
    assert transport.inspect_login_page(timeout=0) == "loading"
    assert transport.login_diagnostic != "ready"
    with pytest.raises(BrowserFetchError, match="not complete"):
        transport.navigate_console()
    assert [
        call[1]["url"] for call in fake.calls if call[0] == "Page.navigate"
    ] == [cdp.SENSECORE_LOGIN_URL]
    transport.close()


def test_regional_console_home_can_continue_to_trusted_iam_password_form():
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    _emit_real_console_commit(fake)
    _emit_regional_console_home_document(fake)

    assert transport.login_diagnostic != "ready"
    assert not transport.login_diagnostic.startswith("unsafe:")
    _emit_console_oauth_iam_challenge(fake)

    assert transport.login_diagnostic == "challenge"
    fake.fetch_results.append(_runtime_value("password_form"))
    assert transport.inspect_login_page(timeout=0) == "password_form"
    with pytest.raises(BrowserFetchError, match="not complete"):
        transport.navigate_console()
    transport.close()


@pytest.mark.parametrize(
    "terminal_url",
    [_VALID_CONSOLE_LOGIN_TERMINAL_URL, _VALID_CONSOLE_CALLBACK_URL],
)
def test_console_terminal_commit_requires_stable_grace_before_ready(
    monkeypatch, terminal_url
):
    now = [400.0]
    monkeypatch.setattr(cdp.time, "monotonic", lambda: now[0])
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
    ).start()
    _emit_direct_terminal_commit(fake, terminal_url)

    fake.fetch_results.append(_runtime_value("departed"))
    assert transport.inspect_login_page(timeout=0) == "loading"
    assert transport.login_diagnostic != "ready"
    with pytest.raises(BrowserFetchError, match="not complete"):
        transport.navigate_console()

    now[0] += cdp._LOGIN_CONSOLE_ROOT_GRACE_SECONDS + 0.01
    fake.fetch_results.append(_runtime_value("departed"))
    assert transport.inspect_login_page(timeout=0) == "departed"
    assert transport.login_diagnostic == "ready"
    transport.close()


def test_terminal_grace_return_to_signin_never_opens_cci(monkeypatch):
    now = [500.0]
    monkeypatch.setattr(cdp.time, "monotonic", lambda: now[0])
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
    ).start()
    _emit_direct_terminal_commit(fake, _VALID_CONSOLE_CALLBACK_URL)
    listener = fake.listeners[0]

    listener(
        *_login_document_request(
            _VALID_LOGIN_CHALLENGE_URL,
            request_id="terminal-grace-return",
            loader_id="terminal-grace-return-loader",
            redirect_url=_VALID_CONSOLE_CALLBACK_URL,
            redirect_status=302,
        )
    )
    listener(
        *_login_frame_navigated(
            _VALID_LOGIN_CHALLENGE_URL,
            loader_id="terminal-grace-return-loader",
        )
    )

    assert transport.login_diagnostic != "ready"
    with pytest.raises(BrowserFetchError, match="not complete"):
        transport.navigate_console()
    assert [
        call[1]["url"] for call in fake.calls if call[0] == "Page.navigate"
    ] == [cdp.SENSECORE_LOGIN_URL]
    if not transport.login_diagnostic.startswith("unsafe:"):
        fake.fetch_results.append(_runtime_value("password_form"))
        assert transport.inspect_login_page(timeout=0) == "password_form"
    transport.close()


def test_console_home_oauth_result_history_update_remains_provisional(
    monkeypatch,
):
    now = [550.0]
    monkeypatch.setattr(cdp.time, "monotonic", lambda: now[0])
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    _emit_direct_terminal_commit(fake, _VALID_CONSOLE_LOGIN_TERMINAL_URL)
    _emit_console_home_oauth_result_within_document(fake)

    assert transport._login_committed_url == _VALID_CONSOLE_HOME_OAUTH_RESULT_URL
    assert not transport.login_diagnostic.startswith("unsafe:")
    fake.fetch_results.append(_runtime_value("departed"))
    assert transport.inspect_login_page(timeout=0) == "loading"

    now[0] += cdp._LOGIN_CONSOLE_ROOT_GRACE_SECONDS + 0.01
    fake.fetch_results.append(_runtime_value("departed"))
    assert transport.inspect_login_page(timeout=0) == "departed"
    assert transport.login_diagnostic == "ready"
    transport.close()


def test_same_document_regional_home_keeps_oauth_iam_route_trusted():
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    _emit_real_console_commit(fake)
    _emit_regional_console_home_within_document(fake)

    assert transport._login_committed_url == _REGIONAL_CONSOLE_HOME_URL
    assert transport.login_diagnostic != "ready"
    assert not transport.login_diagnostic.startswith("unsafe:")
    _emit_console_oauth_iam_challenge(fake, prefix="same-document-login")

    assert transport.login_diagnostic == "challenge"
    fake.fetch_results.append(_runtime_value("password_form"))
    assert transport.inspect_login_page(timeout=0) == "password_form"
    transport.close()


@pytest.mark.parametrize(
    "case",
    [
        "intent_wrong_host",
        "intent_wrong_path",
        "intent_cancelled",
        "duplicate_initial_oauth",
        "skip_first_iam",
        "iam_wrong_status",
        "iam_wrong_source",
        "iam_wrong_request",
        "iam_wrong_loader",
        "iam_wrong_host",
        "iam_wrong_method",
        "duplicate_iam",
        "skip_oauth_return",
        "oauth_return_wrong_status",
        "oauth_return_wrong_source",
        "oauth_return_wrong_request",
        "oauth_return_wrong_loader",
        "oauth_return_wrong_host",
        "oauth_return_wrong_path",
        "duplicate_oauth_return",
        "terminal_wrong_status",
        "terminal_wrong_source",
        "terminal_wrong_request",
        "terminal_wrong_loader",
        "terminal_wrong_commit_loader",
        "terminal_wrong_host",
        "terminal_wrong_path",
        "terminal_wrong_method",
        "third_iam_hop",
    ],
)
def test_post_submit_oauth_route_fails_closed_on_any_deviation(case):
    transport, fake = _submitted_transport()
    listener = fake.listeners[0]

    if case.startswith("intent_"):
        bad_url = (
            _VALID_OAUTH_AUTHORIZATION_URL.replace(
                "signin.sensecore.cn", "signin.sensecore.cn.evil.test"
            )
            if case == "intent_wrong_host"
            else _VALID_OAUTH_AUTHORIZATION_URL.replace(
                "/oauth2/auth", "/oauth2/login"
            )
            if case == "intent_wrong_path"
            else _VALID_OAUTH_AUTHORIZATION_URL
        )
        _emit_renderer_intent(listener, bad_url)
        if case == "intent_cancelled" and not transport.login_diagnostic.startswith(
            "unsafe:"
        ):
            listener(*_login_frame_cleared())
    else:
        _begin_oauth_route(fake)
        if case == "duplicate_initial_oauth":
            listener(
                *_login_document_request(
                    _VALID_OAUTH_AUTHORIZATION_URL,
                    request_id="route-chain",
                    loader_id="route-loader",
                    initiator="script",
                )
            )
        elif case == "skip_first_iam":
            listener(
                *_login_document_request(
                    _VALID_CONSOLE_LOGIN_TERMINAL_URL,
                    request_id="route-chain",
                    loader_id="route-loader",
                    redirect_url=_VALID_OAUTH_AUTHORIZATION_URL,
                    redirect_status=303,
                )
            )
        else:
            iam_url = (
                _VALID_IAM_AUTHORIZATION_URL.replace(
                    "iam.sensecoreapi.cn", "iam.sensecoreapi.cn.evil.test"
                )
                if case == "iam_wrong_host"
                else _VALID_IAM_AUTHORIZATION_URL
            )
            listener(
                *_login_document_request(
                    iam_url,
                    request_id="different-request"
                    if case == "iam_wrong_request"
                    else "route-chain",
                    loader_id="different-loader"
                    if case == "iam_wrong_loader"
                    else "route-loader",
                    redirect_url="https://evil.test/oauth"
                    if case == "iam_wrong_source"
                    else _VALID_OAUTH_AUTHORIZATION_URL,
                    redirect_status=303 if case == "iam_wrong_status" else 302,
                    method="POST" if case == "iam_wrong_method" else "GET",
                )
            )
            if not transport.login_diagnostic.startswith("unsafe:"):
                if case == "duplicate_iam":
                    listener(
                        *_login_document_request(
                            _VALID_IAM_AUTHORIZATION_URL,
                            request_id="route-chain",
                            loader_id="route-loader",
                            redirect_url=_VALID_OAUTH_AUTHORIZATION_URL,
                        )
                    )
                elif case == "skip_oauth_return":
                    listener(
                        *_login_document_request(
                            _VALID_CONSOLE_LOGIN_TERMINAL_URL,
                            request_id="route-chain",
                            loader_id="route-loader",
                            redirect_url=_VALID_IAM_AUTHORIZATION_URL,
                            redirect_status=303,
                        )
                    )
                else:
                    return_oauth = (
                        _VALID_RETURN_OAUTH_AUTHORIZATION_URL.replace(
                            "signin.sensecore.cn", "signin.sensecore.cn.evil.test"
                        )
                        if case == "oauth_return_wrong_host"
                        else _VALID_RETURN_OAUTH_AUTHORIZATION_URL.replace(
                            "/oauth2/auth", "/oauth2/login"
                        )
                        if case == "oauth_return_wrong_path"
                        else _VALID_RETURN_OAUTH_AUTHORIZATION_URL
                    )
                    listener(
                        *_login_document_request(
                            return_oauth,
                            request_id="different-request"
                            if case == "oauth_return_wrong_request"
                            else "route-chain",
                            loader_id="different-loader"
                            if case == "oauth_return_wrong_loader"
                            else "route-loader",
                            redirect_url="https://evil.test/iam"
                            if case == "oauth_return_wrong_source"
                            else _VALID_IAM_AUTHORIZATION_URL,
                            redirect_status=303
                            if case == "oauth_return_wrong_status"
                            else 302,
                        )
                    )
                    if not transport.login_diagnostic.startswith("unsafe:"):
                        if case == "duplicate_oauth_return":
                            listener(
                                *_login_document_request(
                                    _VALID_RETURN_OAUTH_AUTHORIZATION_URL,
                                    request_id="route-chain",
                                    loader_id="route-loader",
                                    redirect_url=_VALID_IAM_AUTHORIZATION_URL,
                                )
                            )
                        elif case == "third_iam_hop":
                            _emit_oauth_iam_round(
                                fake,
                                _VALID_RETURN_OAUTH_AUTHORIZATION_URL,
                                _VALID_SECOND_IAM_AUTHORIZATION_URL,
                                _VALID_SECOND_RETURN_OAUTH_AUTHORIZATION_URL,
                            )
                            listener(
                                *_login_document_request(
                                    _VALID_IAM_AUTHORIZATION_URL,
                                    request_id="route-chain",
                                    loader_id="route-loader",
                                    redirect_url=(
                                        _VALID_SECOND_RETURN_OAUTH_AUTHORIZATION_URL
                                    ),
                                )
                            )
                        else:
                            terminal_url = (
                                _VALID_CONSOLE_LOGIN_TERMINAL_URL.replace(
                                    "console.sensecore.cn",
                                    "console.sensecore.cn.evil.test",
                                )
                                if case == "terminal_wrong_host"
                                else _VALID_CONSOLE_LOGIN_TERMINAL_URL + "/lookalike"
                                if case == "terminal_wrong_path"
                                else _VALID_CONSOLE_LOGIN_TERMINAL_URL
                            )
                            listener(
                                *_login_document_request(
                                    terminal_url,
                                    request_id="different-request"
                                    if case == "terminal_wrong_request"
                                    else "route-chain",
                                    loader_id="different-loader"
                                    if case == "terminal_wrong_loader"
                                    else "route-loader",
                                    redirect_url="https://evil.test/oauth"
                                    if case == "terminal_wrong_source"
                                    else _VALID_RETURN_OAUTH_AUTHORIZATION_URL,
                                    redirect_status=302
                                    if case == "terminal_wrong_status"
                                    else 303,
                                    method="POST"
                                    if case == "terminal_wrong_method"
                                    else "GET",
                                )
                            )
                            if (
                                case == "terminal_wrong_commit_loader"
                                and not transport.login_diagnostic.startswith(
                                    "unsafe:"
                                )
                            ):
                                listener(
                                    *_login_frame_navigated(
                                        _VALID_CONSOLE_LOGIN_TERMINAL_URL,
                                        loader_id="different-commit-loader",
                                    )
                                )

    assert transport.login_diagnostic.startswith("unsafe:"), case
    assert transport._trusted_login_challenge() == ""
    assert transport.submit_login("fixture-user", "fixture-password") in {
        "rejected",
        "unknown",
    }
    transport.close()


def test_login_page_inspection_uses_exact_origin_and_no_credentials():
    transport, _auth, fake = _started_transport()
    fake.fetch_results.append(_runtime_value("password_form"))

    # The exact enterprise entry establishes tenant provenance, but it no
    # longer authorizes reading credentials.  Only a fully committed shared
    # challenge does.
    assert transport.inspect_login_page(timeout=0) == "untrusted"

    call = [item for item in fake.calls if item[0] == "Runtime.callFunctionOn"][-1]
    params = call[1]
    assert params["arguments"] == [{"value": ""}]
    assert cdp.SENSECORE_LOGIN_URL == "https://zhicheng.signin.sensecore.cn/"
    assert cdp.SENSECORE_LOGIN_ORIGIN in params["functionDeclaration"]
    assert cdp.SENSECORE_LOGIN_PATH == "/"
    assert 'location.href === allowedURL' in params["functionDeclaration"]
    assert 'location.pathname === allowedPath' in params["functionDeclaration"]
    assert 'location.search === ""' in params["functionDeclaration"]
    assert 'location.hash === ""' in params["functionDeclaration"]
    assert "allowedQueryNames" not in params["functionDeclaration"]
    assert "URLSearchParams" not in params["functionDeclaration"]
    assert "*.sensecore.cn" not in params["functionDeclaration"]
    assert "\x08" not in params["functionDeclaration"]
    assert r"\botp\b" in params["functionDeclaration"]
    assert call[2] == "session-1"
    transport.close()


def test_signin_challenge_requires_owned_bootstrap_redirect_provenance():
    """A canonical challenge URL is trusted only as our bootstrap redirect.

    A user, extension, stale tab, or foreign CDP target can navigate directly
    to an otherwise valid-looking challenge URL.  The transport must therefore
    pass the browser-side guard the exact URL only after observing the main
    document redirect from the exact zhicheng bootstrap in its owned session.
    """

    challenge_url = _VALID_LOGIN_CHALLENGE_URL

    class ChallengeProofCDP(FakeCDP):
        def call(self, method, params=None, *, session_id=None, timeout=None):
            if method != "Runtime.callFunctionOn":
                return super().call(
                    method,
                    params,
                    session_id=session_id,
                    timeout=timeout,
                )
            params = params or {}
            self.calls.append((method, params, session_id, timeout))
            values = [item.get("value") for item in params.get("arguments", [])]
            is_submit = params.get("functionDeclaration") == cdp._LOGIN_SUBMIT_FUNCTION
            proof = values[2] if is_submit and len(values) == 3 else (
                values[0] if not is_submit and len(values) == 1 else ""
            )
            return _runtime_value(
                "submitted" if is_submit and proof == challenge_url else
                "rejected" if is_submit else
                "password_form" if proof == challenge_url else
                "untrusted"
            )

    # Merely entering the canonical challenge URL in the owned target is not
    # provenance.  It must not authorize reading the credential store later.
    direct_fake = ChallengeProofCDP()
    direct = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=direct_fake,
    ).start()
    foreign_redirect = _login_document_request(
        challenge_url,
        request_id="foreign-redirect-chain",
        redirect_url=cdp.SENSECORE_LOGIN_URL,
    )
    direct_fake.listeners[0](
        foreign_redirect[0], foreign_redirect[1], "session-foreign"
    )
    direct_fake.listeners[0](
        *_login_document_request(challenge_url, request_id="direct")
    )
    assert direct.inspect_login_page(timeout=0) == "untrusted"
    direct_call = [
        call for call in direct_fake.calls if call[0] == "Runtime.callFunctionOn"
    ][-1]
    assert direct_call[1]["arguments"] in ([], [{"value": ""}])
    direct.close()

    # The same URL becomes eligible only after the exact bootstrap response
    # redirects its main document in this transport's flattened session.
    redirected_fake = ChallengeProofCDP()
    redirected = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=redirected_fake,
    ).start()
    listener = redirected_fake.listeners[0]
    listener(
        *_login_document_request(
            cdp.SENSECORE_LOGIN_URL,
            request_id="redirect-chain",
        )
    )
    listener(
        *_login_document_request(
            challenge_url,
            request_id="redirect-chain",
            redirect_url=cdp.SENSECORE_LOGIN_URL,
        )
    )
    listener(*_login_frame_navigated(challenge_url))

    assert redirected.inspect_login_page(timeout=0) == "password_form"
    assert redirected.submit_login(
        "fixture-user", "fixture-password", timeout=1
    ) == "submitted"
    runtime_calls = [
        call for call in redirected_fake.calls if call[0] == "Runtime.callFunctionOn"
    ]
    assert runtime_calls[-2][1]["arguments"] == [{"value": challenge_url}]
    assert runtime_calls[-1][1]["arguments"] == [
        {"value": "fixture-user"},
        {"value": "fixture-password"},
        {"value": challenge_url},
    ]
    assert all(call[2] == "session-1" for call in runtime_calls)
    redirected.close()

    invalid_challenge_url = challenge_url + "&next=console"
    intermediate_url = "https://identity.sensecore.cn/oauth/continue"
    negative_event_sets = {
        "foreign session": [
            _login_document_request(
                cdp.SENSECORE_LOGIN_URL,
                request_id="foreign",
                session_id="session-foreign",
            ),
            _login_document_request(
                challenge_url,
                request_id="foreign",
                redirect_url=cdp.SENSECORE_LOGIN_URL,
                session_id="session-foreign",
            ),
            _login_frame_navigated(
                challenge_url,
                session_id="session-foreign",
            ),
        ],
        "wrong frame": [
            _login_document_request(
                cdp.SENSECORE_LOGIN_URL,
                request_id="wrong-frame",
            ),
            _login_document_request(
                challenge_url,
                request_id="wrong-frame",
                frame_id="child-frame",
                redirect_url=cdp.SENSECORE_LOGIN_URL,
            ),
            _login_frame_navigated(
                challenge_url,
                frame_id="child-frame",
                parent_id="frame-1",
            ),
        ],
        "wrong request id": [
            _login_document_request(
                cdp.SENSECORE_LOGIN_URL,
                request_id="bootstrap-request",
            ),
            _login_document_request(
                challenge_url,
                request_id="different-request",
                redirect_url=cdp.SENSECORE_LOGIN_URL,
            ),
            _login_frame_navigated(challenge_url),
        ],
        "wrong loader id": [
            _login_document_request(
                cdp.SENSECORE_LOGIN_URL,
                request_id="wrong-loader",
                loader_id="bootstrap-loader",
            ),
            _login_document_request(
                challenge_url,
                request_id="wrong-loader",
                loader_id="different-loader",
                redirect_url=cdp.SENSECORE_LOGIN_URL,
            ),
            _login_frame_navigated(
                challenge_url,
                loader_id="different-loader",
            ),
        ],
        "non redirect status": [
            _login_document_request(
                cdp.SENSECORE_LOGIN_URL,
                request_id="status-200",
            ),
            _login_document_request(
                challenge_url,
                request_id="status-200",
                redirect_url=cdp.SENSECORE_LOGIN_URL,
                redirect_status=200,
            ),
            _login_frame_navigated(challenge_url),
        ],
        "orphan redirect event": [
            _login_document_request(
                challenge_url,
                request_id="orphan",
                redirect_url=cdp.SENSECORE_LOGIN_URL,
            ),
            _login_frame_navigated(challenge_url),
        ],
        "intermediate redirect": [
            _login_document_request(
                cdp.SENSECORE_LOGIN_URL,
                request_id="intermediate",
            ),
            _login_document_request(
                intermediate_url,
                request_id="intermediate",
                redirect_url=cdp.SENSECORE_LOGIN_URL,
            ),
            _login_document_request(
                challenge_url,
                request_id="intermediate",
                redirect_url=intermediate_url,
            ),
            _login_frame_navigated(challenge_url),
        ],
        "illegal challenge url": [
            _login_document_request(
                cdp.SENSECORE_LOGIN_URL,
                request_id="illegal-url",
            ),
            _login_document_request(
                invalid_challenge_url,
                request_id="illegal-url",
                redirect_url=cdp.SENSECORE_LOGIN_URL,
            ),
            _login_frame_navigated(invalid_challenge_url),
        ],
        "challenge token changed after trust": [
            _login_document_request(
                cdp.SENSECORE_LOGIN_URL,
                request_id="token-change",
            ),
            _login_document_request(
                challenge_url,
                request_id="token-change",
                redirect_url=cdp.SENSECORE_LOGIN_URL,
            ),
            _login_frame_navigated(challenge_url),
            _login_document_request(
                (
                    "https://signin.sensecore.cn/"
                    "?login_challenge=different-token&platform=console"
                ),
                request_id="different-token-request",
            ),
            _login_frame_navigated(
                (
                    "https://signin.sensecore.cn/"
                    "?login_challenge=different-token&platform=console"
                )
            ),
        ],
    }

    for case_name, events in negative_event_sets.items():
        fake = ChallengeProofCDP()
        transport = BrowserFetchTransport(
            9222,
            "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
            connection=fake,
        ).start()
        listener = fake.listeners[0]
        for event in events:
            listener(*event)
        assert transport.inspect_login_page(timeout=0) == "untrusted", case_name
        runtime_call = [
            call for call in fake.calls if call[0] == "Runtime.callFunctionOn"
        ][-1]
        assert runtime_call[1]["arguments"] in (
            [],
            [{"value": ""}],
        ), case_name
        transport.close()


def test_bootstrap_redirect_events_may_arrive_before_page_navigate_returns():
    """CDP's reader thread can deliver Network events during the call."""

    class EarlyRedirectCDP(FakeCDP):
        def call(self, method, params=None, *, session_id=None, timeout=None):
            if method == "Page.navigate" and (params or {}).get("url") == (
                cdp.SENSECORE_LOGIN_URL
            ):
                result = super().call(
                    method,
                    params,
                    session_id=session_id,
                    timeout=timeout,
                )
                listener = self.listeners[0]
                listener(
                    *_login_document_request(
                        cdp.SENSECORE_LOGIN_URL,
                        request_id="early-chain",
                    )
                )
                listener(
                    *_login_document_request(
                        _VALID_LOGIN_CHALLENGE_URL,
                        request_id="early-chain",
                        redirect_url=cdp.SENSECORE_LOGIN_URL,
                    )
                )
                listener(*_login_frame_navigated(_VALID_LOGIN_CHALLENGE_URL))
                return result
            if method == "Runtime.callFunctionOn":
                params = params or {}
                self.calls.append((method, params, session_id, timeout))
                arguments = params.get("arguments", [])
                proof = arguments[0].get("value") if len(arguments) == 1 else ""
                return _runtime_value(
                    "password_form"
                    if proof == _VALID_LOGIN_CHALLENGE_URL
                    else "untrusted"
                )
            return super().call(
                method,
                params,
                session_id=session_id,
                timeout=timeout,
            )

    fake = EarlyRedirectCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()

    assert transport.inspect_login_page(timeout=0) == "password_form"
    runtime_call = [
        call for call in fake.calls if call[0] == "Runtime.callFunctionOn"
    ][-1]
    assert runtime_call[1]["arguments"] == [
        {"value": _VALID_LOGIN_CHALLENGE_URL}
    ]
    transport.close()


def test_login_dom_functions_fail_closed_in_real_javascript_runtime():
    """Execute both declarations against a small DOM, not a mocked result.

    Node is only a test-time syntax/runtime oracle.  The fixture implements the
    narrow DOM surface used by the login guard and verifies that lookalike
    account forms and DOM changes after input never receive a click.
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable for login DOM guard verification")

    script = (
        "const inspectLogin = (" + cdp._LOGIN_INSPECT_FUNCTION + ");\n"
        "const submitLogin = (" + cdp._LOGIN_SUBMIT_FUNCTION + ");\n"
        + r"""
class HTMLElement {
  constructor(tagName, attributes = {}) {
    this.tagName = String(tagName).toLowerCase();
    this.disabled = false;
    this.visible = attributes.visible !== false;
    this.id = attributes.id || "";
    this.className = attributes.class || "";
    this.type = attributes.type || "";
    this.autocomplete = attributes.autocomplete || "";
    this.innerText = attributes.text || "";
    this.isConnected = true;
    this.children = [];
    this.clicks = 0;
    this.submits = 0;
    this._attributes = {};
    for (const [name, value] of Object.entries(attributes)) {
      if (!["visible", "text"].includes(name)) this._attributes[name] = String(value);
    }
  }
  getAttribute(name) {
    if (name === "id") return this.id || null;
    if (name === "class") return this.className || null;
    if (name === "autocomplete") return this.autocomplete || null;
    return Object.prototype.hasOwnProperty.call(this._attributes, name) ?
      this._attributes[name] : null;
  }
  hasAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this._attributes, name);
  }
  setAttribute(name, value) {
    this._attributes[name] = String(value);
    if (name === "id") this.id = String(value);
    if (name === "class") this.className = String(value);
    if (name === "autocomplete") this.autocomplete = String(value);
  }
  getBoundingClientRect() {
    return this.visible ? {width: 100, height: 24} : {width: 0, height: 0};
  }
  querySelectorAll(selector) { return select(this.children, selector); }
  dispatchEvent(event) {
    if (this.tagName === "form" && event.type === "submit") {
      this.submits += 1;
      return this.cancelSubmit === true ? false : true;
    }
    if (typeof globalThis.onInputEvent === "function") globalThis.onInputEvent(this, event);
    return true;
  }
  click() { this.clicks += 1; }
}

class HTMLInputElement extends HTMLElement {
  constructor(attributes = {}) {
    super("input", attributes);
    this.type = attributes.type || "text";
    this._value = attributes.value || "";
  }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
}

function matches(element, selector) {
  if (selector === "input" || selector === "form" || selector === "button" ||
      selector === "a" || selector === "iframe" || selector === "textarea" ||
      selector === "select") return element.tagName === selector;
  if (selector === '[id]') return Boolean(element.id);
  if (selector === '[class]') return Boolean(element.className);
  if (selector === '[role=button]') return element.getAttribute("role") === "button";
  if (selector === 'input[type=submit]') {
    return element.tagName === "input" && element.type === "submit";
  }
  if (selector === 'input[type="password"]') {
    return element.tagName === "input" && element.type === "password";
  }
  throw new Error("unsupported selector: " + selector);
}

function select(elements, expression) {
  const selectors = expression.split(",").map((value) => value.trim());
  return elements.filter((element) => selectors.some((selector) => matches(element, selector)));
}

globalThis.HTMLElement = HTMLElement;
globalThis.HTMLInputElement = HTMLInputElement;
globalThis.Event = class Event { constructor(type, options) { this.type = type; this.options = options; } };
globalThis.getComputedStyle = (element) => ({
  display: element.visible ? "block" : "none",
  visibility: "visible",
  opacity: "1"
});
globalThis.requestAnimationFrame = (callback) => callback();

function build(options = {}) {
  const origin = options.origin || "https://zhicheng.signin.sensecore.cn";
  const pathname = options.pathname || "/";
  const hash = options.hash || "";
  const search = options.search || "";
  globalThis.location = {
    origin, pathname, hash, search,
    href: options.href || origin + pathname + search + hash
  };

  const formAttributes = {id: options.formId || "login-form", class: "login-form"};
  if (options.action !== undefined) formAttributes.action = options.action;
  if (options.method !== undefined) formAttributes.method = options.method;
  const form = new HTMLElement("form", formAttributes);
  form.cancelSubmit = options.hydrated !== false;
  const username = new HTMLInputElement({
    id: "username", name: "username", type: "text", autocomplete: "username"
  });
  const password = new HTMLInputElement({
    id: "password", name: "password", type: "password",
    autocomplete: options.passwordAutocomplete || "current-password"
  });
  const button = new HTMLElement("button", {
    type: "submit",
    text: options.buttonText || "登录",
    class: options.buttonClass || "ant-btn login_submit"
  });
  button.type = "submit";
  let tenant = null;
  if (options.extraField) {
    tenant = new HTMLInputElement({
      id: options.tenantId || "tenant_code",
      name: options.tenantName || "tenant_code",
      type: options.tenantType || "text",
      autocomplete: options.tenantAutocomplete || "",
      placeholder: options.tenantPlaceholder || "请输入企业标识"
    });
  }
  form.children.push(username);
  if (tenant !== null) form.children.push(tenant);
  form.children.push(password, button);

  const elements = [form, username];
  if (tenant !== null) elements.push(tenant);
  elements.push(password, button);
  if (options.additionalField) {
    const additional = new HTMLInputElement({
      id: "recovery_code", name: "recovery_code", type: "text"
    });
    form.children.splice(form.children.length - 1, 0, additional);
    elements.push(additional);
  }
  if (options.duplicateTenant) {
    const duplicate = new HTMLInputElement({
      id: "tenant_code", name: "tenant_code", type: "text",
      autocomplete: "", placeholder: "企业标识"
    });
    form.children.splice(form.children.length - 1, 0, duplicate);
    elements.push(duplicate);
  }
  if (options.extraControl) {
    const extra = new HTMLElement(options.extraControl, {id: "recovery-control"});
    form.children.push(extra);
    elements.push(extra);
  }
  if (options.captcha) {
    elements.push(new HTMLElement("iframe", {id: "hcaptcha-frame", src: "https://captcha.test/"}));
  }
  if (options.passkey) {
    elements.push(new HTMLElement("button", {id: "passkey", text: "Use passkey"}));
  }
  if (options.passkeyInForm) {
    const passkey = new HTMLElement("button", {id: "passkey", text: "Use passkey"});
    form.children.push(passkey);
    elements.push(passkey);
  }
  if (options.qr) {
    elements.push(new HTMLElement("a", {id: "qr-login", text: "扫码登录"}));
  }
  if (options.qrInForm) {
    const qr = new HTMLElement("a", {id: "qr-login", text: "扫码登录"});
    form.children.push(qr);
    elements.push(qr);
  }
  if (options.otp) {
    elements.push(new HTMLInputElement({
      id: "otp", name: "otp", type: "text", autocomplete: "one-time-code"
    }));
  }
  if (options.otpButton) {
    elements.push(new HTMLElement("button", {text: "短信验证码登录"}));
  }
  if (options.mfaWidget) {
    elements.push(new HTMLElement("div", {class: "mfa verification-widget"}));
  }
  globalThis.document = {querySelectorAll: (selector) => select(elements, selector)};
  globalThis.onInputEvent = null;
  if (options.dynamic === "otp") {
    let inserted = false;
    globalThis.onInputEvent = () => {
      if (inserted) return;
      inserted = true;
      const otp = new HTMLInputElement({
        id: "otp", name: "otp", type: "text", autocomplete: "one-time-code"
      });
      form.children.push(otp);
      elements.push(otp);
    };
  } else if (options.dynamic === "action") {
    globalThis.onInputEvent = () => form.setAttribute("action", "https://evil.test/capture");
  } else if (options.dynamic === "tenant-replace") {
    let replaced = false;
    globalThis.onInputEvent = (element) => {
      if (replaced || element !== tenant) return;
      replaced = true;
      const replacement = new HTMLInputElement({
        id: "tenant_code", name: "tenant_code", type: "text",
        autocomplete: "", placeholder: "请输入企业标识"
      });
      const formIndex = form.children.indexOf(tenant);
      const documentIndex = elements.indexOf(tenant);
      form.children.splice(formIndex, 1, replacement);
      elements.splice(documentIndex, 1, replacement);
      tenant.isConnected = false;
    };
  } else if (options.dynamic === "extra-field") {
    let inserted = false;
    globalThis.onInputEvent = (element) => {
      if (inserted || element !== tenant) return;
      inserted = true;
      const additional = new HTMLInputElement({
        id: "unexpected", name: "unexpected", type: "text"
      });
      form.children.push(additional);
      elements.push(additional);
    };
  }
  return {username, tenant, password, button};
}

async function inspectCase(options) {
  build(options);
  return inspectLogin(options.trustedChallengeURL || "");
}
async function submitCase(options) {
  const dom = build(options);
  const state = await submitLogin(
    "fixture-user",
    "fixture-password",
    options.trustedChallengeURL || ""
  );
  return {
    state,
    clicks: dom.button.clicks,
    submits: globalThis.document.querySelectorAll("form")[0].submits,
    username: dom.username.value,
    tenant: dom.tenant === null ? null : dom.tenant.value,
    password: dom.password.value
  };
}

(async () => {
  const challengeOrigin = "https://signin.sensecore.cn";
  const challengeQuery =
    "?login_challenge=opaque-fixture_ABC.123~x%2Fy&platform=console";
  const challengeURL = challengeOrigin + "/" + challengeQuery;
  const trustedChallenge = {
    origin: challengeOrigin,
    search: challengeQuery,
    extraField: true,
    trustedChallengeURL: challengeURL
  };
  const reorderedChallengeQuery =
    "?platform=console&login_challenge=opaque-fixture_ABC.123~x%2Fy";
  const trustedReorderedChallenge = {
    origin: challengeOrigin,
    search: reorderedChallengeQuery,
    extraField: true,
    trustedChallengeURL: challengeOrigin + "/" + reorderedChallengeQuery
  };
  const iamChallengeQuery =
    "?login_challenge=opaque-fixture_ABC.123~x%2Fy&IAM&platform=console";
  const trustedIAMChallenge = {
    origin: challengeOrigin,
    search: iamChallengeQuery,
    extraField: true,
    trustedChallengeURL: challengeURL
  };
  const trustedIAMEqualsChallenge = {
    origin: challengeOrigin,
    search:
      "?platform=console&IAM=&login_challenge=opaque-fixture_ABC.123~x%2fy",
    extraField: true,
    trustedChallengeURL: challengeURL
  };
  const rejectedIAMChallengeCases = {
    changedToken: {
      origin: challengeOrigin,
      search: "?login_challenge=opaque-fixture_ABC.123~x%2Fz&IAM&platform=console",
      trustedChallengeURL: challengeURL
    },
    nonemptyIAM: {
      origin: challengeOrigin,
      search: "?login_challenge=opaque-fixture_ABC.123~x%2Fy&IAM=1&platform=console",
      trustedChallengeURL: challengeURL
    },
    duplicateIAM: {
      origin: challengeOrigin,
      search: "?login_challenge=opaque-fixture_ABC.123~x%2Fy&IAM&IAM=&platform=console",
      trustedChallengeURL: challengeURL
    },
    encodedIAMKey: {
      origin: challengeOrigin,
      search: "?login_challenge=opaque-fixture_ABC.123~x%2Fy&%49AM&platform=console",
      trustedChallengeURL: challengeURL
    },
    lowercaseIAMKey: {
      origin: challengeOrigin,
      search: "?login_challenge=opaque-fixture_ABC.123~x%2Fy&iam&platform=console",
      trustedChallengeURL: challengeURL
    },
    partnerInstead: {
      origin: challengeOrigin,
      search: "?login_challenge=opaque-fixture_ABC.123~x%2Fy&partner=&platform=console",
      trustedChallengeURL: challengeURL
    },
    otherParameter: {
      origin: challengeOrigin,
      search: "?login_challenge=opaque-fixture_ABC.123~x%2Fy&IAM&platform=console&next=console",
      trustedChallengeURL: challengeURL
    },
    duplicateChallenge: {
      origin: challengeOrigin,
      search: "?login_challenge=opaque-fixture_ABC.123~x%2Fy&login_challenge=opaque-fixture_ABC.123~x%2Fy&IAM",
      trustedChallengeURL: challengeURL
    },
    encodedPlatformKey: {
      origin: challengeOrigin,
      search: "?login_challenge=opaque-fixture_ABC.123~x%2Fy&IAM&%70latform=console",
      trustedChallengeURL: challengeURL
    },
    nonemptyIAMEncoding: {
      origin: challengeOrigin,
      search: "?login_challenge=opaque-fixture_ABC.123~x%2Fy&IAM=%00&platform=console",
      trustedChallengeURL: challengeURL
    },
    iamHash: {
      origin: challengeOrigin,
      search: iamChallengeQuery,
      hash: "#continue",
      trustedChallengeURL: challengeURL
    }
  };
  const rejectedChallengeCases = {
    unprovenChallenge: {
      origin: challengeOrigin,
      search: challengeQuery
    },
    mismatchedChallengeProof: {
      origin: challengeOrigin,
      search: challengeQuery,
      trustedChallengeURL: challengeURL + "-different"
    },
    insecureChallenge: {
      origin: "http://signin.sensecore.cn",
      search: challengeQuery,
      trustedChallengeURL: "http://signin.sensecore.cn/" + challengeQuery
    },
    nonDefaultPort: {
      origin: "https://signin.sensecore.cn:444",
      search: challengeQuery,
      trustedChallengeURL:
        "https://signin.sensecore.cn:444/" + challengeQuery
    },
    challengeUserinfo: {
      origin: challengeOrigin,
      search: challengeQuery,
      href: "https://fixture-user@signin.sensecore.cn/" + challengeQuery,
      trustedChallengeURL:
        "https://fixture-user@signin.sensecore.cn/" + challengeQuery
    },
    foreignChallengeHost: {
      origin: "https://signin.sensecore.cn.evil.test",
      search: challengeQuery,
      trustedChallengeURL:
        "https://signin.sensecore.cn.evil.test/" + challengeQuery
    },
    challengeSubdomain: {
      origin: "https://evil.signin.sensecore.cn",
      search: challengeQuery,
      trustedChallengeURL:
        "https://evil.signin.sensecore.cn/" + challengeQuery
    },
    emptyChallenge: {
      origin: challengeOrigin,
      search: "?login_challenge=&platform=console",
      trustedChallengeURL:
        challengeOrigin + "/?login_challenge=&platform=console"
    },
    duplicateChallenge: {
      origin: challengeOrigin,
      search: "?login_challenge=one&login_challenge=two&platform=console",
      trustedChallengeURL: challengeOrigin +
        "/?login_challenge=one&login_challenge=two&platform=console"
    },
    duplicatePlatform: {
      origin: challengeOrigin,
      search: "?login_challenge=one&platform=console&platform=console",
      trustedChallengeURL: challengeOrigin +
        "/?login_challenge=one&platform=console&platform=console"
    },
    missingPlatform: {
      origin: challengeOrigin,
      search: "?login_challenge=one",
      trustedChallengeURL: challengeOrigin + "/?login_challenge=one"
    },
    wrongPlatform: {
      origin: challengeOrigin,
      search: "?login_challenge=one&platform=other",
      trustedChallengeURL:
        challengeOrigin + "/?login_challenge=one&platform=other"
    },
    extraChallengeParameter: {
      origin: challengeOrigin,
      search: "?login_challenge=one&platform=console&next=console",
      trustedChallengeURL: challengeOrigin +
        "/?login_challenge=one&platform=console&next=console"
    },
    encodedChallengeKey: {
      origin: challengeOrigin,
      search: "?%6cogin_challenge=one&platform=console",
      trustedChallengeURL:
        challengeOrigin + "/?%6cogin_challenge=one&platform=console"
    },
    challengePath: {
      origin: challengeOrigin,
      pathname: "/login",
      search: "?login_challenge=one&platform=console",
      trustedChallengeURL:
        challengeOrigin + "/login?login_challenge=one&platform=console"
    },
    challengeHash: {
      origin: challengeOrigin,
      search: "?login_challenge=one&platform=console",
      hash: "#continue",
      trustedChallengeURL:
        challengeOrigin + "/?login_challenge=one&platform=console#continue"
    }
  };
  const result = {
    valid: await inspectCase({}),
    loginChallengeQuery: await inspectCase({search: "?login_challenge=fixture"}),
    partnerQuery: await inspectCase({search: "?partner=fixture"}),
    dangerousQuery: await inspectCase({search: "?mode=reset"}),
    duplicateQuery: await inspectCase({search: "?partner=a&partner=b"}),
    resetPath: await inspectCase({pathname: "/reset-password"}),
    hashRoute: await inspectCase({hash: "#/reset-password"}),
    newPassword: await inspectCase({...trustedIAMChallenge, passwordAutocomplete: "new-password"}),
    resetForm: await inspectCase({...trustedIAMChallenge, formId: "reset-password"}),
    foreignAction: await inspectCase({...trustedIAMChallenge, action: "https://evil.test/capture", method: "post"}),
    nonLoginAction: await inspectCase({...trustedIAMChallenge, action: "/register", method: "post"}),
    explicitMethod: await inspectCase({...trustedIAMChallenge, method: "post"}),
    emptyMethodAttribute: await inspectCase({...trustedIAMChallenge, method: ""}),
    emptyActionAttribute: await inspectCase({...trustedIAMChallenge, action: ""}),
    extraField: await inspectCase({...trustedIAMChallenge, extraField: true}),
    missingTenant: await inspectCase({...trustedIAMChallenge, extraField: false}),
    forgedTenantId:
      await inspectCase({...trustedIAMChallenge, tenantId: "tenant-code"}),
    forgedTenantName:
      await inspectCase({...trustedIAMChallenge, tenantName: "tenant"}),
    forgedTenantAutocomplete:
      await inspectCase({...trustedIAMChallenge, tenantAutocomplete: "organization"}),
    forgedTenantPlaceholder:
      await inspectCase({...trustedIAMChallenge, tenantPlaceholder: "请输入标识"}),
    forgedTenantPlaceholderMissingIdentifier:
      await inspectCase({...trustedIAMChallenge, tenantPlaceholder: "请输入企业"}),
    forgedTenantType:
      await inspectCase({...trustedIAMChallenge, tenantType: "email"}),
    additionalField:
      await inspectCase({...trustedIAMChallenge, additionalField: true}),
    duplicateTenant:
      await inspectCase({...trustedIAMChallenge, duplicateTenant: true}),
    extraTextarea: await inspectCase({...trustedIAMChallenge, extraControl: "textarea"}),
    extraSelect: await inspectCase({...trustedIAMChallenge, extraControl: "select"}),
    resetButton: await inspectCase({...trustedIAMChallenge, buttonText: "Reset password"}),
    whitespaceChineseButton:
      await inspectCase({...trustedIAMChallenge, buttonText: "  登\n 录  "}),
    whitespaceEnglishButton:
      await inspectCase({...trustedIAMChallenge, buttonText: " Sign \n In "}),
    nearChineseButton:
      await inspectCase({...trustedIAMChallenge, buttonText: "立即登录"}),
    nearEnglishButton:
      await inspectCase({...trustedIAMChallenge, buttonText: "Login now"}),
    mixedButton:
      await inspectCase({...trustedIAMChallenge, buttonText: "登录 Login"}),
    captcha: await inspectCase({...trustedIAMChallenge, captcha: true}),
    passkeyOutside: await inspectCase({...trustedIAMChallenge, passkey: true}),
    passkeyInside: await inspectCase({...trustedIAMChallenge, passkeyInForm: true}),
    qrOutside: await inspectCase({...trustedIAMChallenge, qr: true}),
    qrInside: await inspectCase({...trustedIAMChallenge, qrInForm: true}),
    passkeyWithoutStrictForm:
      await inspectCase({...trustedIAMChallenge, passkey: true, passwordAutocomplete: "new-password"}),
    qrWithoutStrictForm: await inspectCase({...trustedIAMChallenge, qr: true, extraField: false}),
    otpOutside: await inspectCase({...trustedIAMChallenge, otp: true}),
    otpButtonOutside: await inspectCase({...trustedIAMChallenge, otpButton: true}),
    mfaWidgetOutside: await inspectCase({...trustedIAMChallenge, mfaWidget: true}),
    submitted: await submitCase({}),
    unhydrated: await submitCase({...trustedIAMChallenge, hydrated: false}),
    submitLoginChallengeQuery: await submitCase({search: "?login_challenge=fixture"}),
    submitPartnerQuery: await submitCase({search: "?partner=fixture"}),
    submitWrongPath: await submitCase({pathname: "/change-password"}),
    dynamicOtp: await submitCase({...trustedIAMChallenge, dynamic: "otp"}),
    dynamicAction: await submitCase({...trustedIAMChallenge, dynamic: "action"}),
    dynamicTenantReplace:
      await submitCase({...trustedIAMChallenge, dynamic: "tenant-replace"}),
    dynamicExtraField:
      await submitCase({...trustedIAMChallenge, dynamic: "extra-field"}),
    submittedMissingTenant:
      await submitCase({...trustedIAMChallenge, extraField: false}),
    submittedForgedTenantId:
      await submitCase({...trustedIAMChallenge, tenantId: "tenant-code"}),
    submittedForgedTenantName:
      await submitCase({...trustedIAMChallenge, tenantName: "tenant"}),
    submittedForgedTenantAutocomplete:
      await submitCase({...trustedIAMChallenge, tenantAutocomplete: "organization"}),
    submittedForgedTenantPlaceholder:
      await submitCase({...trustedIAMChallenge, tenantPlaceholder: "企业"}),
    submittedForgedTenantType:
      await submitCase({...trustedIAMChallenge, tenantType: "email"}),
    submittedAdditionalField:
      await submitCase({...trustedIAMChallenge, additionalField: true}),
    submittedDuplicateTenant:
      await submitCase({...trustedIAMChallenge, duplicateTenant: true}),
    submittedWhitespaceChineseButton:
      await submitCase({...trustedIAMChallenge, buttonText: "  登\n 录  "}),
    submittedWhitespaceEnglishButton:
      await submitCase({...trustedIAMChallenge, buttonText: " Log\tIn "}),
    submittedNearChineseButton:
      await submitCase({...trustedIAMChallenge, buttonText: "立即登录"}),
    submittedNearEnglishButton:
      await submitCase({...trustedIAMChallenge, buttonText: "Login now"}),
    submittedMixedButton:
      await submitCase({...trustedIAMChallenge, buttonText: "登录 Login"}),
    submittedOutsideQr: await submitCase({...trustedIAMChallenge, qr: true}),
    submittedInsideQr: await submitCase({...trustedIAMChallenge, qrInForm: true}),
    submittedCaptcha: await submitCase({...trustedIAMChallenge, captcha: true}),
    submittedOtpOutside: await submitCase({...trustedIAMChallenge, otp: true}),
    submittedOtpButtonOutside:
      await submitCase({...trustedIAMChallenge, otpButton: true}),
    submittedMfaWidgetOutside:
      await submitCase({...trustedIAMChallenge, mfaWidget: true}),
    submittedAlternativeWithoutStrictForm:
      await submitCase({...trustedIAMChallenge, qr: true, extraField: false}),
    trustedChallenge: await inspectCase(trustedChallenge),
    submittedTrustedChallenge: await submitCase(trustedChallenge),
    trustedReorderedChallenge: await inspectCase(trustedReorderedChallenge),
    submittedTrustedReorderedChallenge:
      await submitCase(trustedReorderedChallenge),
    trustedIAMChallenge: await inspectCase(trustedIAMChallenge),
    submittedTrustedIAMChallenge: await submitCase(trustedIAMChallenge),
    trustedIAMEqualsChallenge: await inspectCase(trustedIAMEqualsChallenge),
    submittedTrustedIAMEqualsChallenge:
      await submitCase(trustedIAMEqualsChallenge),
    rejectedChallengeInspections: {},
    rejectedChallengeSubmissions: {},
    rejectedIAMChallengeInspections: {},
    rejectedIAMChallengeSubmissions: {}
  };
  for (const [name, options] of Object.entries(rejectedChallengeCases)) {
    result.rejectedChallengeInspections[name] = await inspectCase(options);
    result.rejectedChallengeSubmissions[name] = await submitCase(options);
  }
  for (const [name, options] of Object.entries(rejectedIAMChallengeCases)) {
    result.rejectedIAMChallengeInspections[name] = await inspectCase(options);
    result.rejectedIAMChallengeSubmissions[name] = await submitCase(options);
  }
  process.stdout.write(JSON.stringify(result));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    result = json.loads(completed.stdout)

    assert result["valid"] == "loading"
    for name in (
        "loginChallengeQuery",
        "partnerQuery",
        "dangerousQuery",
        "duplicateQuery",
    ):
        assert result[name] == "untrusted"
    assert result["resetPath"] == result["hashRoute"] == "untrusted"
    for name in (
        "newPassword",
        "resetForm",
        "foreignAction",
        "nonLoginAction",
        "explicitMethod",
        "emptyMethodAttribute",
        "emptyActionAttribute",
        "extraTextarea",
        "extraSelect",
        "resetButton",
        "missingTenant",
        "forgedTenantId",
        "forgedTenantName",
        "forgedTenantAutocomplete",
        "forgedTenantPlaceholder",
        "forgedTenantPlaceholderMissingIdentifier",
        "forgedTenantType",
        "additionalField",
        "duplicateTenant",
    ):
        assert result[name] == "ambiguous"
    assert result["extraField"] == "password_form"
    assert result["captcha"] == result["otpOutside"] == "challenge"
    assert result["otpButtonOutside"] == result["mfaWidgetOutside"] == "challenge"
    assert result["passkeyOutside"] == result["qrOutside"] == "password_form"
    assert result["passkeyInside"] == result["qrInside"] == "challenge"
    assert result["passkeyWithoutStrictForm"] == "challenge"
    assert result["qrWithoutStrictForm"] == "challenge"
    assert result["whitespaceChineseButton"] == "password_form"
    assert result["whitespaceEnglishButton"] == "password_form"
    for name in ("nearChineseButton", "nearEnglishButton", "mixedButton"):
        assert result[name] == "ambiguous"
    assert result["submitted"] == {
        "state": "rejected",
        "clicks": 0,
        "submits": 0,
        "username": "",
        "tenant": None,
        "password": "",
    }
    assert result["unhydrated"]["state"] == "rejected"
    assert result["unhydrated"]["clicks"] == 0
    assert result["unhydrated"]["submits"] == 1
    assert result["unhydrated"]["tenant"] == "zhicheng"
    for name in ("submitLoginChallengeQuery", "submitPartnerQuery"):
        assert result[name]["state"] == "rejected"
        assert result[name]["clicks"] == 0
        assert result[name]["submits"] == 0
        assert result[name]["username"] == ""
        assert result[name]["tenant"] is None
        assert result[name]["password"] == ""
    assert result["submitWrongPath"]["state"] == "rejected"
    assert result["submitWrongPath"]["clicks"] == 0
    assert result["dynamicOtp"]["state"] == "challenge"
    assert result["dynamicOtp"]["clicks"] == 0
    assert result["dynamicOtp"]["username"] == ""
    assert result["dynamicOtp"]["tenant"] == "zhicheng"
    assert result["dynamicOtp"]["password"] == ""
    assert result["dynamicAction"]["state"] == "rejected"
    assert result["dynamicAction"]["clicks"] == 0
    assert result["dynamicAction"]["username"] == ""
    assert result["dynamicAction"]["tenant"] == "zhicheng"
    assert result["dynamicAction"]["password"] == ""
    for name in ("dynamicTenantReplace", "dynamicExtraField"):
        assert result[name]["state"] == "rejected"
        assert result[name]["clicks"] == 0
        assert result[name]["submits"] == 0
        assert result[name]["username"] == ""
        assert result[name]["password"] == ""
    assert result["dynamicTenantReplace"]["tenant"] == "zhicheng"
    assert result["dynamicExtraField"]["tenant"] == "zhicheng"
    assert result["submittedMissingTenant"] == {
        "state": "rejected",
        "clicks": 0,
        "submits": 0,
        "username": "",
        "tenant": None,
        "password": "",
    }
    for name in (
        "submittedForgedTenantId",
        "submittedForgedTenantName",
        "submittedForgedTenantAutocomplete",
        "submittedForgedTenantPlaceholder",
        "submittedForgedTenantType",
    ):
        assert result[name] == {
            "state": "rejected",
            "clicks": 0,
            "submits": 0,
            "username": "",
            "tenant": "",
            "password": "",
        }
    for name in ("submittedAdditionalField", "submittedDuplicateTenant"):
        assert result[name] == {
            "state": "rejected",
            "clicks": 0,
            "submits": 0,
            "username": "",
            "tenant": "",
            "password": "",
        }
    assert result["submittedOutsideQr"] == {
        "state": "submitted",
        "clicks": 0,
        "submits": 1,
        "username": "fixture-user",
        "tenant": "zhicheng",
        "password": "fixture-password",
    }
    for name in (
        "submittedWhitespaceChineseButton",
        "submittedWhitespaceEnglishButton",
    ):
        assert result[name] == {
            "state": "submitted",
            "clicks": 0,
            "submits": 1,
            "username": "fixture-user",
            "tenant": "zhicheng",
            "password": "fixture-password",
        }
    for name in (
        "submittedNearChineseButton",
        "submittedNearEnglishButton",
        "submittedMixedButton",
    ):
        assert result[name] == {
            "state": "rejected",
            "clicks": 0,
            "submits": 0,
            "username": "",
            "tenant": "",
            "password": "",
        }
    for name in (
        "submittedInsideQr",
        "submittedCaptcha",
        "submittedOtpOutside",
        "submittedOtpButtonOutside",
        "submittedMfaWidgetOutside",
        "submittedAlternativeWithoutStrictForm",
    ):
        assert result[name] == {
            "state": "challenge",
            "clicks": 0,
            "submits": 0,
            "username": "",
            "tenant": None if name == "submittedAlternativeWithoutStrictForm" else "",
            "password": "",
        }

    assert result["trustedChallenge"] == "loading"
    assert result["submittedTrustedChallenge"] == {
        "state": "rejected",
        "clicks": 0,
        "submits": 0,
        "username": "",
        "tenant": "",
        "password": "",
    }
    assert result["trustedReorderedChallenge"] == "loading"
    assert result["submittedTrustedReorderedChallenge"]["state"] == "rejected"
    assert result["trustedIAMChallenge"] == "password_form"
    assert result["submittedTrustedIAMChallenge"] == {
        "state": "submitted",
        "clicks": 0,
        "submits": 1,
        "username": "fixture-user",
        "tenant": "zhicheng",
        "password": "fixture-password",
    }
    assert result["trustedIAMEqualsChallenge"] == "password_form"
    assert result["submittedTrustedIAMEqualsChallenge"] == {
        "state": "submitted",
        "clicks": 0,
        "submits": 1,
        "username": "fixture-user",
        "tenant": "zhicheng",
        "password": "fixture-password",
    }
    assert result["rejectedChallengeInspections"]
    assert set(result["rejectedChallengeInspections"].values()) == {"untrusted"}
    for submission in result["rejectedChallengeSubmissions"].values():
        assert submission == {
            "state": "rejected",
            "clicks": 0,
            "submits": 0,
            "username": "",
            "tenant": None,
            "password": "",
        }
    assert result["rejectedIAMChallengeInspections"]
    assert set(result["rejectedIAMChallengeInspections"].values()) == {"untrusted"}
    for submission in result["rejectedIAMChallengeSubmissions"].values():
        assert submission == {
            "state": "rejected",
            "clicks": 0,
            "submits": 0,
            "username": "",
            "tenant": None,
            "password": "",
        }


@pytest.mark.parametrize("state", ["challenge", "ambiguous", "redirecting", "loading"])
def test_login_page_inspection_returns_only_coarse_states(state):
    transport, _auth, fake = _started_transport()
    fake.fetch_results.append(_runtime_value(state))

    assert transport.inspect_login_page(timeout=0) == state
    transport.close()


def test_login_submission_passes_secrets_only_as_cdp_arguments():
    username = "fake-user-source-sentinel"
    password = "fake-password-source-sentinel#$"
    transport, _auth, fake = _started_transport()
    _emit_real_renderer_challenge_flow(fake)
    fake.fetch_results.append(_runtime_value("submitted"))

    assert transport.submit_login(username, password, timeout=1) == "submitted"

    call = [item for item in fake.calls if item[0] == "Runtime.callFunctionOn"][-1]
    params = call[1]
    declaration = params["functionDeclaration"]
    assert username not in declaration
    assert password not in declaration
    assert "internalIAMChallenge(pinnedChallenge, location.href)" in declaration
    assert "location.href === pinnedChallenge" not in declaration
    assert cdp.SENSECORE_LOGIN_URL not in declaration
    assert "URLSearchParams" not in declaration
    assert params["arguments"] == [
        {"value": username},
        {"value": password},
        {"value": _VALID_LOGIN_CHALLENGE_URL},
    ]
    assert call[2] == "session-1"
    assert username not in repr(transport)
    assert password not in repr(transport)
    transport.close()


def test_login_submission_uncertainty_is_not_retried_or_leaked():
    username = "fake-user-error-sentinel"
    password = "fake-password-error-sentinel"

    class FailingLoginCDP(FakeCDP):
        def call(self, method, params=None, *, session_id=None, timeout=None):
            if method == "Runtime.callFunctionOn":
                self.calls.append((method, params or {}, session_id, timeout))
                raise RuntimeError(f"sensitive params: {params!r}")
            return super().call(method, params, session_id=session_id, timeout=timeout)

    fake = FailingLoginCDP()
    transport, _auth, _fake = _started_transport(fake)
    _emit_real_renderer_challenge_flow(fake)

    assert transport.submit_login(username, password, timeout=1) == "unknown"
    calls = [item for item in fake.calls if item[0] == "Runtime.callFunctionOn"]
    assert len(calls) == 1
    transport.close()


def test_login_navigation_discards_stale_isolated_context():
    transport, _auth, fake = _started_transport()
    _emit_real_renderer_challenge_flow(fake)
    fake.fetch_results.extend(
        [_runtime_value("password_form"), _runtime_value("password_form")]
    )

    assert transport.inspect_login_page(timeout=0) == "password_form"
    fake.listeners[0]("Runtime.executionContextsCleared", {}, "session-1")
    assert transport.inspect_login_page(timeout=0) == "password_form"

    worlds = [item for item in fake.calls if item[0] == "Page.createIsolatedWorld"]
    assert len(worlds) == 2
    transport.close()


def test_login_submission_rejects_empty_values_without_cdp_call():
    transport, _auth, fake = _started_transport()

    assert transport.submit_login("", "password") == "rejected"
    assert transport.submit_login("username", "") == "rejected"

    assert not [item for item in fake.calls if item[0] == "Runtime.callFunctionOn"]
    transport.close()


def test_login_submission_never_overwrites_concurrent_unsafe_transition():
    class InvalidatingSubmitCDP(FakeCDP):
        def call(self, method, params=None, *, session_id=None, timeout=None):
            if (
                method == "Runtime.callFunctionOn"
                and (params or {}).get("functionDeclaration")
                == cdp._LOGIN_SUBMIT_FUNCTION
            ):
                self.calls.append((method, params or {}, session_id, timeout))
                self.listeners[0](
                    *_login_document_request(
                        "https://evil.test/leave-login",
                        request_id="unsafe-during-submit",
                        loader_id="unsafe-during-submit-loader",
                    )
                )
                return _runtime_value("submitted")
            return super().call(
                method,
                params,
                session_id=session_id,
                timeout=timeout,
            )

    fake = InvalidatingSubmitCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    _emit_real_renderer_challenge_flow(fake)

    assert transport.submit_login(
        "fixture-user", "fixture-password", timeout=1
    ) == "unknown"
    assert transport.login_diagnostic.startswith("unsafe:")
    assert transport.submit_login(
        "fixture-user", "fixture-password", timeout=1
    ) == "rejected"
    sensitive_calls = [
        call
        for call in fake.calls
        if call[0] == "Runtime.callFunctionOn"
        and call[1].get("functionDeclaration") == cdp._LOGIN_SUBMIT_FUNCTION
    ]
    assert len(sensitive_calls) == 1
    assert "fixture-password" not in repr(transport)
    transport.close()


def test_login_submission_rechecks_generation_before_sensitive_runtime_call():
    class InvalidatingContextCDP(FakeCDP):
        invalidate_on_world = False

        def call(self, method, params=None, *, session_id=None, timeout=None):
            result = super().call(
                method,
                params,
                session_id=session_id,
                timeout=timeout,
            )
            if method == "Page.createIsolatedWorld" and self.invalidate_on_world:
                self.invalidate_on_world = False
                self.listeners[0](
                    *_login_document_request(
                        "https://evil.test/before-sensitive-call",
                        request_id="unsafe-before-submit",
                        loader_id="unsafe-before-submit-loader",
                    )
                )
            return result

    fake = InvalidatingContextCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    _emit_real_renderer_challenge_flow(fake)
    fake.invalidate_on_world = True

    assert transport.submit_login(
        "fixture-user", "fixture-password", timeout=1
    ) == "unknown"
    sensitive_calls = [
        call
        for call in fake.calls
        if call[0] == "Runtime.callFunctionOn"
        and call[1].get("functionDeclaration") == cdp._LOGIN_SUBMIT_FUNCTION
    ]
    assert sensitive_calls == []
    assert transport.login_diagnostic.startswith("unsafe:")
    transport.close()


def test_console_navigation_postchecks_concurrent_login_proof_invalidation():
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"

    class InvalidatingNavigationCDP(FakeCDP):
        def call(self, method, params=None, *, session_id=None, timeout=None):
            result = super().call(
                method,
                params,
                session_id=session_id,
                timeout=timeout,
            )
            if method == "Page.navigate" and (params or {}).get("url") == console_url:
                self.listeners[0](
                    *_login_document_request(
                        "https://evil.test/raced-navigation",
                        request_id="unsafe-during-console",
                        loader_id="unsafe-during-console-loader",
                    )
                )
            return result

    fake = InvalidatingNavigationCDP()
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
    ).start()
    _emit_ready_login_flow(fake)
    _promote_stable_console_landing(transport, fake)

    with pytest.raises(BrowserFetchError, match="proof changed"):
        transport.navigate_console()
    assert transport.broken
    assert transport.login_diagnostic.startswith("unsafe:")
    transport.close()


def test_transport_start_then_console_navigation_use_one_owned_session():
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
    ).start()

    navigations = [call for call in fake.calls if call[0] == "Page.navigate"]
    assert len(navigations) == 1
    assert navigations[0][1] == {
        "url": "https://zhicheng.signin.sensecore.cn/"
    }
    assert navigations[0][2] == "session-1"
    assert "?" not in navigations[0][1]["url"]
    assert "#" not in navigations[0][1]["url"]
    assert all(console_url not in repr(call) for call in fake.calls)

    _emit_real_renderer_challenge_flow(fake)
    fake.fetch_results.append(_runtime_value("password_form"))
    assert transport.inspect_login_page(timeout=0) == "password_form"
    assert transport._execution_context_id is not None

    listener = fake.listeners[0]
    terminal_url = "https://console.sensecore.cn/auth/callback?code=fixture"
    _emit_renderer_intent(listener, terminal_url)
    listener(
        *_login_document_request(
            terminal_url,
            request_id="same-session-terminal",
            loader_id="same-session-terminal-loader",
            initiator="script",
        )
    )
    listener(
        *_login_frame_navigated(
            terminal_url,
            loader_id="same-session-terminal-loader",
        )
    )
    _promote_stable_console_landing(transport, fake)
    assert transport.navigate_console() is transport
    navigations = [call for call in fake.calls if call[0] == "Page.navigate"]
    assert [call[1] for call in navigations] == [
        {"url": "https://zhicheng.signin.sensecore.cn/"},
        {"url": console_url},
    ]
    assert [call[2] for call in navigations] == ["session-1", "session-1"]
    assert transport._execution_context_id is None
    transport.close()


def test_console_exact_owned_get_and_matching_frame_commit_remain_ready():
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
    ).start()
    _emit_ready_login_flow(fake)
    _promote_stable_console_landing(transport, fake)
    listener = fake.listeners[0]

    assert transport.login_diagnostic == "ready"
    assert transport.navigate_console() is transport
    assert transport.cci_auth_diagnostic == {
        "exact_main_frame_commit": False,
        "owned_session_cci_requests": 0,
        "bearer_candidates": 0,
        "effective_2xx": 0,
    }
    with pytest.raises(CDPTimeout, match="navigation did not finish"):
        transport.wait_for_console_commit(timeout=0)

    listener(
        *_login_document_request(
            console_url,
            request_id="cci-console-document",
            loader_id="cci-console-loader",
            method="GET",
        )
    )
    with pytest.raises(CDPTimeout, match="navigation did not finish"):
        transport.wait_for_console_commit(timeout=0)
    assert transport.login_diagnostic == "ready"

    listener(
        *_login_frame_navigated(
            console_url,
            loader_id="cci-console-loader",
        )
    )
    transport.wait_for_console_commit(timeout=0)

    assert transport.login_diagnostic == "ready"
    assert transport.cci_auth_diagnostic["exact_main_frame_commit"] is True
    assert not transport.broken
    transport.close()


def test_console_auth_diagnostic_counts_only_owned_exact_cci_requests():
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
    ).start()
    listener = fake.listeners[0]
    secret = "Bearer diagnostic-token-must-not-escape"

    listener(*_request_event("foreign", CCI_URL, secret, "session-2"))
    listener(*_response_event("foreign", CCI_URL, 200, "session-2"))
    listener(
        *_request_event(
            "lookalike",
            "https://cci.cn-sh-01.sensecore.cn.evil.test/compute/cci/data/v2/apps",
            secret,
            "session-1",
        )
    )
    listener(
        *_response_event(
            "lookalike",
            "https://cci.cn-sh-01.sensecore.cn.evil.test/compute/cci/data/v2/apps",
            200,
            "session-1",
        )
    )

    assert transport.cci_auth_diagnostic == {
        "exact_main_frame_commit": False,
        "owned_session_cci_requests": 0,
        "bearer_candidates": 0,
        "effective_2xx": 0,
    }

    listener(*_request_event("owned", CCI_URL, secret, "session-1"))
    listener(*_response_event("owned", CCI_URL, 200, "session-1"))
    diagnostic = transport.cci_auth_diagnostic

    assert diagnostic == {
        "exact_main_frame_commit": False,
        "owned_session_cci_requests": 1,
        "bearer_candidates": 1,
        "effective_2xx": 1,
    }
    assert secret not in repr(diagnostic)
    transport.close()


@pytest.mark.parametrize(
    ("case", "expected_diagnostic"),
    [
        ("missing_request", "unsafe:"),
        ("wrong_url", "unsafe:"),
        ("wrong_loader", "unsafe:"),
        ("wrong_frame", "ready"),
        ("wrong_session", "ready"),
    ],
)
def test_console_commit_rejects_wrong_url_loader_frame_or_session(
    case, expected_diagnostic
):
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
    ).start()
    _emit_ready_login_flow(fake)
    _promote_stable_console_landing(transport, fake)
    listener = fake.listeners[0]
    transport.navigate_console()

    if case == "missing_request":
        listener(
            *_login_frame_navigated(
                console_url,
                loader_id="unproven-console-loader",
            )
        )
    elif case == "wrong_url":
        wrong_url = "https://console.sensecore.cn/cn-sh-01/cci/other"
        listener(
            *_login_document_request(
                wrong_url,
                request_id="wrong-console-url",
                loader_id="wrong-console-url-loader",
                method="GET",
            )
        )
        listener(
            *_login_frame_navigated(
                wrong_url,
                loader_id="wrong-console-url-loader",
            )
        )
    elif case == "wrong_loader":
        listener(
            *_login_document_request(
                console_url,
                request_id="console-loader-mismatch",
                loader_id="expected-console-loader",
                method="GET",
            )
        )
        listener(
            *_login_frame_navigated(
                console_url,
                loader_id="different-console-loader",
            )
        )
    elif case == "wrong_frame":
        listener(
            *_login_document_request(
                console_url,
                request_id="foreign-frame-console",
                loader_id="foreign-frame-loader",
                frame_id="frame-2",
                method="GET",
            )
        )
        listener(
            *_login_frame_navigated(
                console_url,
                loader_id="foreign-frame-loader",
                frame_id="frame-2",
            )
        )
    else:
        listener(
            *_login_document_request(
                console_url,
                request_id="foreign-session-console",
                loader_id="foreign-session-loader",
                method="GET",
                session_id="session-2",
            )
        )
        listener(
            *_login_frame_navigated(
                console_url,
                loader_id="foreign-session-loader",
                session_id="session-2",
            )
        )

    with pytest.raises(CDPTimeout, match="navigation did not finish"):
        transport.wait_for_console_commit(timeout=0)
    if expected_diagnostic == "ready":
        assert transport.login_diagnostic == expected_diagnostic
    else:
        assert transport.login_diagnostic.startswith(expected_diagnostic)
    transport.close()


def test_console_retry_requires_commit_and_allows_one_fixed_get_only():
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
    ).start()
    _emit_ready_login_flow(fake)
    _promote_stable_console_landing(transport, fake)
    listener = fake.listeners[0]
    transport.navigate_console()

    with pytest.raises(BrowserFetchError, match="bootstrap is not safe"):
        transport.retry_console_navigation_for_auth()

    listener(
        *_login_document_request(
            console_url,
            request_id="first-console-document",
            loader_id="first-console-loader",
            method="GET",
        )
    )
    listener(
        *_login_frame_navigated(
            console_url,
            loader_id="first-console-loader",
        )
    )
    transport.wait_for_console_commit(timeout=0)

    assert transport.retry_console_navigation_for_auth() is transport
    assert transport.cci_auth_diagnostic["exact_main_frame_commit"] is True
    listener(
        *_login_document_request(
            console_url,
            request_id="retry-console-document",
            loader_id="retry-console-loader",
            method="GET",
        )
    )
    listener(
        *_login_frame_navigated(
            console_url,
            loader_id="retry-console-loader",
        )
    )
    transport.wait_for_console_commit(timeout=0)

    with pytest.raises(BrowserFetchError, match="bootstrap is not safe"):
        transport.retry_console_navigation_for_auth()

    navigation_calls = [call for call in fake.calls if call[0] == "Page.navigate"]
    assert [call[1] for call in navigation_calls] == [
        {"url": cdp.SENSECORE_LOGIN_URL},
        {"url": console_url},
        {"url": console_url},
    ]
    assert [call[2] for call in navigation_calls] == [
        "session-1",
        "session-1",
        "session-1",
    ]
    assert all(set(call[1]) == {"url"} for call in navigation_calls)
    assert not any(call[0] == "Page.reload" for call in fake.calls)
    assert not any(call[0] == "Network.replayXHR" for call in fake.calls)
    assert not any(call[0] == "Runtime.evaluate" for call in fake.calls)
    assert all(
        call[1].get("functionDeclaration") == cdp._LOGIN_INSPECT_FUNCTION
        for call in fake.calls
        if call[0] == "Runtime.callFunctionOn"
    )
    assert not any(
        storage_name in repr(fake.calls)
        for storage_name in ("localStorage", "sessionStorage", "cookie")
    )
    assert transport.login_diagnostic == "ready"
    transport.close()


def test_renderer_challenge_completion_requires_terminal_commit_before_console():
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"
    terminal_url = "https://console.sensecore.cn/auth/callback?code=fixture"
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
    ).start()
    _emit_real_renderer_challenge_flow(fake)
    listener = fake.listeners[0]

    _emit_renderer_intent(listener, terminal_url)
    listener(
        *_login_document_request(
            terminal_url,
            request_id="terminal-renderer",
            loader_id="terminal-renderer-loader",
            initiator="script",
        )
    )
    assert transport.login_diagnostic == "terminal_pending"
    with pytest.raises(BrowserFetchError, match="not complete"):
        transport.navigate_console()

    listener(
        *_login_frame_navigated(
            terminal_url,
            loader_id="terminal-renderer-loader",
        )
    )
    assert transport.login_diagnostic == "console"
    with pytest.raises(BrowserFetchError, match="not complete"):
        transport.navigate_console()
    _promote_stable_console_landing(transport, fake)
    assert transport.login_diagnostic == "ready"
    assert transport.navigate_console() is transport
    assert [
        call[1]["url"] for call in fake.calls if call[0] == "Page.navigate"
    ] == [cdp.SENSECORE_LOGIN_URL, console_url]
    transport.close()


@pytest.mark.parametrize(
    "case",
    [
        "missing_intent_and_source",
        "wrong_redirect_source",
        "wrong_redirect_status",
        "wrong_redirect_method",
        "wrong_commit_loader",
        "fragment_error",
    ],
)
def test_challenge_completion_never_readies_on_unproven_terminal(case):
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"
    terminal_url = "https://console.sensecore.cn/auth/callback?code=fixture"
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
    ).start()
    _emit_real_renderer_challenge_flow(fake)
    listener = fake.listeners[0]

    if case == "fragment_error":
        _emit_renderer_intent(listener, terminal_url + "#error=access_denied")
    else:
        listener(
            *_login_document_request(
                terminal_url,
                request_id="terminal-direct",
                loader_id="terminal-direct-loader",
                redirect_url=(
                    None
                    if case == "missing_intent_and_source"
                    else "https://evil.test/challenge"
                    if case == "wrong_redirect_source"
                    else _VALID_LOGIN_CHALLENGE_URL
                ),
                redirect_status=200 if case == "wrong_redirect_status" else 302,
                method="POST" if case == "wrong_redirect_method" else "GET",
            )
        )
        if case == "wrong_commit_loader" and not transport.login_diagnostic.startswith(
            "unsafe"
        ):
            listener(
                *_login_frame_navigated(
                    terminal_url,
                    loader_id="different-terminal-loader",
                )
            )

    assert transport.login_diagnostic.startswith("unsafe:"), case
    with pytest.raises(BrowserFetchError):
        transport.navigate_console()
    assert [
        call[1]["url"] for call in fake.calls if call[0] == "Page.navigate"
    ] == [cdp.SENSECORE_LOGIN_URL]
    transport.close()


def test_console_navigation_is_gated_until_verified_challenge_departure(
    monkeypatch,
):
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
    ).start()

    # This is a transport-level invariant, not merely a CLI calling
    # convention.  A future caller cannot accidentally bypass login phases.
    with pytest.raises(BrowserFetchError):
        transport.navigate_console()
    assert not transport.broken
    assert [
        call[1]["url"] for call in fake.calls if call[0] == "Page.navigate"
    ] == [cdp.SENSECORE_LOGIN_URL]

    listener = fake.listeners[0]
    listener(
        *_login_document_request(
            cdp.SENSECORE_LOGIN_URL,
            request_id="gate-chain",
        )
    )
    listener(
        *_login_document_request(
            _VALID_LOGIN_CHALLENGE_URL,
            request_id="gate-chain",
            redirect_url=cdp.SENSECORE_LOGIN_URL,
        )
    )
    listener(*_login_frame_navigated(_VALID_LOGIN_CHALLENGE_URL))

    # Reaching the trusted challenge form is still not completion.
    with pytest.raises(BrowserFetchError):
        transport.navigate_console()
    assert [
        call[1]["url"] for call in fake.calls if call[0] == "Page.navigate"
    ] == [cdp.SENSECORE_LOGIN_URL]

    login_terminal_url = "https://console.sensecore.cn/auth/callback?code=fixture"
    listener(
        *_login_document_request(
            login_terminal_url,
            request_id="login-terminal",
            redirect_url=_VALID_LOGIN_CHALLENGE_URL,
        )
    )
    listener(*_login_frame_navigated(login_terminal_url))
    _age_console_landing_past_grace(transport)
    fake.fetch_results.extend(
        [_runtime_value("redirecting"), _runtime_value("departed")]
    )
    monkeypatch.setattr(cdp.time, "sleep", lambda _seconds: None)

    assert transport.wait_for_login_departure(1) == "departed"
    assert transport.navigate_console() is transport
    assert [
        call[1]["url"] for call in fake.calls if call[0] == "Page.navigate"
    ] == [cdp.SENSECORE_LOGIN_URL, console_url]
    transport.close()


def test_rejected_enterprise_login_navigation_never_opens_console():
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"

    class RejectedLoginCDP(FakeCDP):
        def call(self, method, params=None, *, session_id=None, timeout=None):
            result = super().call(
                method,
                params,
                session_id=session_id,
                timeout=timeout,
            )
            if method == "Page.navigate" and (params or {}).get("url") == (
                "https://zhicheng.signin.sensecore.cn/"
            ):
                return {"errorText": "network failed"}
            return result

    fake = RejectedLoginCDP()
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
    )

    with pytest.raises(BrowserFetchError, match="login navigation was rejected"):
        transport.start()

    navigations = [call for call in fake.calls if call[0] == "Page.navigate"]
    assert [call[1] for call in navigations] == [
        {"url": "https://zhicheng.signin.sensecore.cn/"}
    ]
    assert all(console_url not in repr(call) for call in fake.calls)
    assert not transport._started
    transport.close()


def test_console_navigation_and_login_departure_require_started_transport():
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    )

    with pytest.raises(BrowserFetchError, match="must be started"):
        transport.navigate_console()
    with pytest.raises(BrowserFetchError, match="must be started"):
        transport.wait_for_login_departure(0)

    assert fake.calls == []
    transport.close()


def test_console_navigation_failure_clears_context_and_breaks_session():
    class RejectedConsoleCDP(FakeCDP):
        def call(self, method, params=None, *, session_id=None, timeout=None):
            result = super().call(
                method,
                params,
                session_id=session_id,
                timeout=timeout,
            )
            if method == "Page.navigate" and (params or {}).get("url", "").startswith(
                "https://console.sensecore.cn/"
            ):
                return {"errorText": "network failed"}
            return result

    fake = RejectedConsoleCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    _emit_ready_login_flow(fake)
    _promote_stable_console_landing(transport, fake)
    transport._execution_context_id = 123

    with pytest.raises(BrowserFetchError, match="navigation was rejected"):
        transport.navigate_console()

    assert transport._execution_context_id is None
    assert transport.broken
    transport.close()


def test_wait_for_login_departure_polls_without_credentials_or_navigation(
    monkeypatch,
):
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    fake.fetch_results.extend(
        [
            _runtime_value("loading"),
            _runtime_value("redirecting"),
            _runtime_value("departed"),
        ]
    )
    _emit_ready_login_flow(fake)
    _age_console_landing_past_grace(transport)
    monkeypatch.setattr(cdp.time, "sleep", lambda _seconds: None)

    assert transport.wait_for_login_departure(1) == "departed"

    runtime_calls = [
        call for call in fake.calls if call[0] == "Runtime.callFunctionOn"
    ]
    assert len(runtime_calls) == 3
    assert all(call[1]["arguments"] == [{"value": ""}] for call in runtime_calls)
    assert all(call[2] == "session-1" for call in runtime_calls)
    navigations = [call for call in fake.calls if call[0] == "Page.navigate"]
    assert [call[1] for call in navigations] == [
        {"url": "https://zhicheng.signin.sensecore.cn/"}
    ]
    transport.close()


def test_wait_for_login_departure_does_not_accept_bootstrap_redirect(
    monkeypatch,
):
    """Only verified completion of signin's challenge is a departure."""

    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    listener = fake.listeners[0]
    listener(
        *_login_document_request(
            cdp.SENSECORE_LOGIN_URL,
            request_id="completion-chain",
        )
    )
    listener(
        *_login_document_request(
            _VALID_LOGIN_CHALLENGE_URL,
            request_id="completion-chain",
            redirect_url=cdp.SENSECORE_LOGIN_URL,
        )
    )
    listener(*_login_frame_navigated(_VALID_LOGIN_CHALLENGE_URL))
    login_terminal_url = "https://console.sensecore.cn/auth/callback?code=fixture"
    listener(
        *_login_document_request(
            login_terminal_url,
            request_id="completion-terminal",
            redirect_url=_VALID_LOGIN_CHALLENGE_URL,
        )
    )
    listener(*_login_frame_navigated(login_terminal_url))
    _age_console_landing_past_grace(transport)
    fake.fetch_results.extend(
        [
            _runtime_value("redirecting"),
            _runtime_value("departed"),
        ]
    )
    monkeypatch.setattr(cdp.time, "sleep", lambda _seconds: None)

    assert transport.wait_for_login_departure(1) == "departed"
    runtime_calls = [
        call for call in fake.calls if call[0] == "Runtime.callFunctionOn"
    ]
    assert len(runtime_calls) == 2
    assert all(len(call[1]["arguments"]) <= 1 for call in runtime_calls)
    assert sum(call[0] == "Page.navigate" for call in fake.calls) == 1
    transport.close()


def test_wait_for_login_departure_returns_challenge_immediately():
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    fake.fetch_results.extend(
        [_runtime_value("challenge"), _runtime_value("redirecting")]
    )

    assert transport.wait_for_login_departure(10) == "challenge"
    runtime_calls = [
        call for call in fake.calls if call[0] == "Runtime.callFunctionOn"
    ]
    assert len(runtime_calls) == 1
    transport.close()


def test_submitted_password_form_remains_pollable_without_second_submission():
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    _emit_real_renderer_challenge_flow(fake)
    fake.fetch_results.append(_runtime_value("submitted"))

    assert transport.submit_login("fixture-user", "fixture-password") == "submitted"
    assert transport.login_diagnostic == "submitted"
    assert transport.submit_login("fixture-user", "fixture-password") == "rejected"

    fake.fetch_results.append(_runtime_value("password_form"))
    assert transport.wait_for_login_completion(0) == "password_form"
    sensitive_calls = [
        call
        for call in fake.calls
        if call[0] == "Runtime.callFunctionOn"
        and call[1].get("functionDeclaration") == cdp._LOGIN_SUBMIT_FUNCTION
    ]
    assert len(sensitive_calls) == 1
    transport.close()


@pytest.mark.parametrize("state", ["password_form", "loading", "ambiguous"])
def test_wait_for_login_departure_timeout_returns_last_polled_state(state):
    fake = FakeCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    ).start()
    if state == "password_form":
        _emit_real_renderer_challenge_flow(fake)
    fake.fetch_results.append(_runtime_value(state))

    assert transport.wait_for_login_departure(0) == state
    assert sum(call[0] == "Runtime.callFunctionOn" for call in fake.calls) == 1
    assert sum(call[0] == "Page.navigate" for call in fake.calls) == 1
    transport.close()


def test_browser_transport_starts_minimized_window_and_fetches_without_expression_token():
    transport, _auth, fake = _started_transport()
    response = transport.request(
        "PATCH",
        CCI_URL,
        params={"client_type": 0},
        json_body={"template": {"containers": []}},
    )
    assert response.status == 200
    assert response.json() == {"result": "ok"}

    methods = [call[0] for call in fake.calls]
    assert fake.calls[0][1] == {
        "url": "about:blank",
        "newWindow": True,
        "background": True,
        "windowState": "minimized",
    }
    assert methods[:7] == [
        "Target.createTarget",
        "Target.attachToTarget",
        "Network.enable",
        "Runtime.enable",
        "Page.enable",
        "Page.getFrameTree",
        "Page.navigate",
    ]
    fetch = next(call for call in fake.calls if call[0] == "Runtime.callFunctionOn")
    call_params = fetch[1]
    request_value = call_params["arguments"][0]["value"]
    assert "browser-token" not in call_params["functionDeclaration"]
    assert 'credentials: "omit"' in call_params["functionDeclaration"]
    assert 'credentials: "include"' not in call_params["functionDeclaration"]
    assert request_value["headers"]["Authorization"] == "Bearer browser-token"
    assert request_value["headers"]["x-ui-valid"] == "x-ui-valid"
    assert request_value["url"].endswith("?client_type=0")
    assert request_value["body"] == '{"template":{"containers":[]}}'

    transport.close()
    assert "Target.closeTarget" in [call[0] for call in fake.calls]
    close_target = next(call for call in fake.calls if call[0] == "Target.closeTarget")
    assert close_target[1] == {"targetId": "target-1"}
    assert not fake.closed  # injected connection remains owned by its caller


def test_browser_fetch_omits_cookies_but_preserves_explicit_authorization():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable for browser fetch verification")
    script = (
        "const browserFetch = (" + cdp._FETCH_FUNCTION + ");\n"
        + r"""
let captured = null;
globalThis.fetch = async (url, options) => {
  captured = {url, options};
  return {status: 200, text: async () => "ok"};
};
(async () => {
  const response = await browserFetch({
    url: "https://example.test/api",
    method: "GET",
    headers: {Authorization: "Bearer fixture-token"},
    body: null
  });
  process.stdout.write(JSON.stringify({captured, response}));
})().catch((error) => { console.error(error); process.exit(1); });
"""
    )
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    result = json.loads(completed.stdout)

    assert result["captured"]["options"]["credentials"] == "omit"
    assert result["captured"]["options"]["redirect"] == "error"
    assert result["captured"]["options"]["headers"] == {
        "Authorization": "Bearer fixture-token"
    }
    assert result["response"] == {"status": 200, "text": "ok"}


def test_browser_transport_falls_back_to_new_window_then_minimizes():
    class OlderWindowCDP(FakeCDP):
        def call(self, method, params=None, *, session_id=None, timeout=None):
            params = params or {}
            if method == "Target.createTarget" and "windowState" in params:
                self.calls.append((method, params, session_id, timeout))
                raise CDPError("unsupported createTarget parameter")
            return super().call(
                method, params, session_id=session_id, timeout=timeout
            )

    transport, _auth, fake = _started_transport(OlderWindowCDP())
    create_calls = [call for call in fake.calls if call[0] == "Target.createTarget"]

    assert len(create_calls) == 3
    assert create_calls[-1][1] == {
        "url": "about:blank",
        "newWindow": True,
        "background": True,
    }
    window_lookup = next(
        call for call in fake.calls if call[0] == "Browser.getWindowForTarget"
    )
    assert window_lookup[1] == {"targetId": "target-1"}
    minimize = next(
        call for call in fake.calls if call[0] == "Browser.setWindowBounds"
    )
    assert minimize[1] == {
        "windowId": 17,
        "bounds": {"windowState": "minimized"},
    }
    transport.close()


def test_browser_transport_falls_back_to_legacy_background_tab():
    class TabOnlyCDP(FakeCDP):
        def call(self, method, params=None, *, session_id=None, timeout=None):
            params = params or {}
            if method == "Target.createTarget" and params.get("newWindow"):
                self.calls.append((method, params, session_id, timeout))
                raise CDPError("newWindow is unsupported")
            return super().call(
                method, params, session_id=session_id, timeout=timeout
            )

    transport, _auth, fake = _started_transport(TabOnlyCDP())
    create_calls = [call for call in fake.calls if call[0] == "Target.createTarget"]

    assert len(create_calls) == 5
    assert create_calls[-1][1] == {"url": "about:blank", "background": True}
    assert "Browser.getWindowForTarget" not in [call[0] for call in fake.calls]
    transport.close()


def test_browser_transport_does_not_retry_ambiguous_create_timeout():
    class TimedOutCreateCDP(FakeCDP):
        def call(self, method, params=None, *, session_id=None, timeout=None):
            params = params or {}
            if method == "Target.createTarget":
                self.calls.append((method, params, session_id, timeout))
                raise CDPTimeout("ambiguous create timeout")
            return super().call(
                method, params, session_id=session_id, timeout=timeout
            )

    fake = TimedOutCreateCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
    )

    with pytest.raises(CDPTimeout, match="ambiguous create timeout"):
        transport.start()

    assert sum(call[0] == "Target.createTarget" for call in fake.calls) == 1
    assert "Target.closeTarget" not in [call[0] for call in fake.calls]
    assert fake.listeners == []
    transport.close()


def test_headless_transport_never_reuses_nonblank_page():
    console_url = "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x"
    fake = FakeCDP()
    fake.target_infos = [
        {"targetId": "worker", "type": "service_worker", "url": console_url},
        {
            "targetId": "incognito",
            "type": "page",
            "url": console_url,
            "browserContextId": "private-context",
        },
        {
            "targetId": "busy",
            "type": "page",
            "url": console_url,
            "attached": True,
        },
        {"targetId": "other-page", "type": "page", "url": "https://other/"},
        {"targetId": "console-page", "type": "page", "url": console_url},
    ]
    transport = BrowserFetchTransport(
        9222,
        console_url,
        connection=fake,
        reuse_existing_page=True,
    ).start()

    assert "Target.getTargets" in [call[0] for call in fake.calls]
    create = next(call for call in fake.calls if call[0] == "Target.createTarget")
    assert create[1] == {"url": "about:blank"}
    attach = next(call for call in fake.calls if call[0] == "Target.attachToTarget")
    assert attach[1]["targetId"] == "target-1"

    transport.close()
    close_target = next(call for call in fake.calls if call[0] == "Target.closeTarget")
    assert close_target[1] == {"targetId": "target-1"}
    assert "Target.detachFromTarget" not in [call[0] for call in fake.calls]


def test_headless_transport_reuses_only_idle_about_blank_page():
    fake = FakeCDP()
    fake.target_infos = [
        {"targetId": "other-page", "type": "page", "url": "https://other/"},
        {"targetId": "blank-page", "type": "page", "url": "about:blank"},
    ]
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
        reuse_existing_page=True,
    ).start()

    assert "Target.createTarget" not in [call[0] for call in fake.calls]
    attach = next(call for call in fake.calls if call[0] == "Target.attachToTarget")
    assert attach[1]["targetId"] == "blank-page"

    transport.close()
    detach = next(call for call in fake.calls if call[0] == "Target.detachFromTarget")
    assert detach[1] == {"sessionId": "session-1"}


def test_headless_transport_creates_only_minimal_page_when_none_is_reusable():
    fake = FakeCDP()
    fake.target_infos = [
        {"targetId": "worker", "type": "service_worker"},
        {
            "targetId": "busy-page",
            "type": "page",
            "url": "about:blank",
            "attached": True,
        },
    ]
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
        reuse_existing_page=True,
    ).start()

    create = next(call for call in fake.calls if call[0] == "Target.createTarget")
    assert create[1] == {"url": "about:blank"}
    transport.close()
    close_target = next(call for call in fake.calls if call[0] == "Target.closeTarget")
    assert close_target[1] == {"targetId": "target-1"}
    assert "Target.detachFromTarget" not in [call[0] for call in fake.calls]


def test_login_required_only_after_wait_and_refresh_both_time_out():
    fake = FakeCDP()
    auth = CCIAuthorization()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
        auth=auth,
        auth_timeout=0,
    ).start()

    assert not transport.login_required
    with pytest.raises(CDPTimeout, match="authorization"):
        transport.wait_for_auth()
    assert transport.login_required
    assert sum(call[0] == "Page.reload" for call in fake.calls) == 1

    _promote(auth, token="Bearer restored")
    assert transport.wait_for_auth().authorization == "Bearer restored"
    assert not transport.login_required

    fake.fetch_results.extend(
        [
            {"exceptionDetails": {"text": "network failed"}},
            {"exceptionDetails": {"text": "network failed"}},
        ]
    )
    with pytest.raises(BrowserFetchError, match="browser request failed"):
        transport.request("GET", CCI_URL)
    assert not transport.login_required
    transport.close()
    auth.close()


def test_login_required_does_not_mask_cdp_reload_failure():
    class ReloadFailureCDP(FakeCDP):
        def call(self, method, params=None, *, session_id=None, timeout=None):
            if method == "Page.reload":
                raise CDPError("session closed")
            return super().call(
                method, params, session_id=session_id, timeout=timeout
            )

    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=ReloadFailureCDP(),
        auth_timeout=0,
    ).start()

    with pytest.raises(BrowserFetchError, match="reload"):
        transport.wait_for_auth()
    assert transport.broken
    assert not transport.login_required
    transport.close()


def test_successful_explicit_refresh_clears_login_required():
    auth = CCIAuthorization()

    class RefreshingCDP(FakeCDP):
        promote_on_reload = False

        def call(self, method, params=None, *, session_id=None, timeout=None):
            result = super().call(
                method, params, session_id=session_id, timeout=timeout
            )
            if method == "Page.reload" and self.promote_on_reload:
                _promote(auth, request_id="refreshed", token="Bearer refreshed")
            return result

    fake = RefreshingCDP()
    transport = BrowserFetchTransport(
        9222,
        "https://console.sensecore.cn/cn-sh-01/cci/app?workspace=x",
        connection=fake,
        auth=auth,
        auth_timeout=0,
    ).start()
    _emit_ready_login_flow(fake)
    _promote_stable_console_landing(transport, fake)
    transport.navigate_console()
    listener = fake.listeners[0]
    console_url = transport.console_url
    listener(
        *_login_document_request(
            console_url,
            request_id="refresh-console-document",
            loader_id="refresh-console-loader",
            method="GET",
        )
    )
    listener(
        *_login_frame_navigated(
            console_url,
            loader_id="refresh-console-loader",
        )
    )
    transport.wait_for_console_commit(timeout=0)
    _promote(auth, request_id="initial", token="Bearer initial")

    with pytest.raises(CDPTimeout):
        transport.refresh_auth()
    assert transport.login_required

    fake.promote_on_reload = True
    assert transport.refresh_auth().authorization == "Bearer refreshed"
    assert not transport.login_required
    transport.close()
    auth.close()


def test_browser_transport_requests_graceful_browser_close():
    transport, _auth, fake = _started_transport()

    assert transport.close_browser(timeout=2.5)
    close_call = next(call for call in fake.calls if call[0] == "Browser.close")
    assert close_call == ("Browser.close", {}, None, 2.5)

    transport.close()


def test_graceful_browser_close_accepts_expected_cdp_disconnect():
    class DisconnectingCDP(FakeCDP):
        is_closed = False

        def call(self, method, params=None, *, session_id=None, timeout=None):
            if method == "Browser.close":
                self.calls.append((method, params or {}, session_id, timeout))
                self.is_closed = True
                raise CDPError("browser websocket closed")
            return super().call(
                method, params, session_id=session_id, timeout=timeout
            )

    transport, _auth, fake = _started_transport(DisconnectingCDP())

    assert transport.close_browser()
    assert fake.calls[-1] == ("Browser.close", {}, None, 5.0)
    transport.close()


def test_graceful_browser_close_returns_false_for_unexpected_failure():
    class FailingCloseCDP(FakeCDP):
        def call(self, method, params=None, *, session_id=None, timeout=None):
            if method == "Browser.close":
                self.calls.append((method, params or {}, session_id, timeout))
                raise CDPError("command rejected")
            return super().call(
                method, params, session_id=session_id, timeout=timeout
            )

    transport, _auth, _fake = _started_transport(FailingCloseCDP())

    assert not transport.close_browser()
    transport.close()
    assert not transport.close_browser()


def test_browser_transport_allows_management_api_but_blocks_exfiltration_headers():
    transport, _auth, _fake = _started_transport()
    response = transport.request(
        "GET", "https://management.sensecoreapi.cn/rmh/v1/resources"
    )
    assert response.ok

    with pytest.raises(BrowserFetchError, match="allow-list"):
        transport.request(
            "GET",
            "https://management.sensecoreapi.cn.evil.test/rmh/v1/resources",
        )
    with pytest.raises(BrowserFetchError, match="authorization"):
        transport.request(
            "GET", CCI_URL, headers={"AUTHORIZATION": "Bearer caller-value"}
        )
    with pytest.raises(BrowserFetchError, match="cookie"):
        transport.request("GET", CCI_URL, headers={"Cookie": "secret=1"})
    transport.close()


def test_browser_transport_401_invalidates_only_used_lease():
    fake = FakeCDP()
    fake.fetch_results.append(
        {"result": {"value": {"status": 401, "text": "unauthorized"}}}
    )
    transport, auth, _fake = _started_transport(fake)
    response = transport.request("POST", CCI_URL, body="{}")
    assert response.status == 401
    assert auth.current() is None
    with pytest.raises(CDPTimeout, match="authorization"):
        transport.refresh_auth(timeout=0)
    assert transport.login_required
    transport.close()


def test_browser_transport_rebuilds_context_and_retries_only_get():
    fake = FakeCDP()
    fake.fetch_results.extend(
        [
            {"exceptionDetails": {"text": "Execution context destroyed"}},
            {"result": {"value": {"status": 200, "text": "after reload"}}},
        ]
    )
    transport, _auth, _fake = _started_transport(fake)
    assert transport.request("GET", CCI_URL).text == "after reload"
    assert sum(call[0] == "Page.createIsolatedWorld" for call in fake.calls) == 2
    get_calls = [call for call in fake.calls if call[0] == "Runtime.callFunctionOn"]
    assert len(get_calls) == 2
    assert all('credentials: "omit"' in call[1]["functionDeclaration"] for call in get_calls)
    assert all(
        call[1]["arguments"][0]["value"]["headers"]["Authorization"]
        == "Bearer browser-token"
        for call in get_calls
    )
    transport.close()

    fake = FakeCDP()
    fake.fetch_results.append(
        {"exceptionDetails": {"text": "Execution context destroyed"}}
    )
    transport, _auth, _fake = _started_transport(fake)
    with pytest.raises(BrowserFetchError, match="browser request failed"):
        transport.request("PATCH", CCI_URL, body="{}")
    assert sum(call[0] == "Runtime.callFunctionOn" for call in fake.calls) == 1
    transport.close()
