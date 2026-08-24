"""Small, synchronous Chrome DevTools Protocol client for SenseCore.

The CCI API is only reachable through the Chrome instance opened by
``slaigpus``.  This module deliberately executes API requests inside that
browser instead of copying its bearer token into a normal HTTP client.  The
token stays in memory, is scoped to an exact allow-list, and is never included
in a repr, exception, or log message.

``CDPConnection`` owns one browser-level websocket and multiplexes commands
and target-session events.  Listener callbacks run on its reader thread and
therefore must not call :meth:`CDPConnection.call` synchronously.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import unquote, unquote_plus, urlencode, urlsplit, urlunsplit
from urllib.request import ProxyHandler, build_opener


CCI_API_ORIGIN = "https://cci.cn-sh-01.sensecore.cn"
CCI_API_PREFIX = "/compute/cci/data/v2/"
CCI_API_BASE = CCI_API_ORIGIN + CCI_API_PREFIX.rstrip("/")
MANAGEMENT_API_BASE = "https://management.sensecoreapi.cn/rmh/v1"


class CDPError(RuntimeError):
    """Chrome DevTools Protocol connection or command failure."""


class CDPTimeout(CDPError):
    """A CDP command or authorization wait exceeded its deadline."""


class BrowserFetchError(CDPError):
    """A browser-side fetch could not be completed safely."""


@dataclass(frozen=True)
class DevToolsEndpoint:
    """Resolved browser websocket endpoint.

    The websocket URL contains Chrome's browser target id.  It is not an API
    credential, but hiding it from reprs avoids publishing a local control
    endpoint accidentally.
    """

    port: int
    browser_ws_url: str = field(repr=False)


def _is_loopback(host: Optional[str]) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _profile_from_chrome(chrome: Any) -> Optional[Path]:
    """Best-effort extraction of ``--user-data-dir`` from a Popen-like object."""
    args = getattr(chrome, "args", None)
    if isinstance(args, str):
        try:
            args = shlex.split(args)
        except ValueError:
            return None
    if not isinstance(args, (list, tuple)):
        return None
    for index, arg in enumerate(args):
        if not isinstance(arg, str):
            continue
        if arg.startswith("--user-data-dir="):
            value = arg.split("=", 1)[1]
            return Path(value).expanduser() if value else None
        if arg == "--user-data-dir" and index + 1 < len(args):
            value = args[index + 1]
            return Path(str(value)).expanduser() if value else None
    return None


def _active_port_endpoint(profile_dir: Path, host: str) -> Optional[DevToolsEndpoint]:
    path = Path(profile_dir).expanduser() / "DevToolsActivePort"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        port = int(lines[0].strip())
        target_path = lines[1].strip()
    except (OSError, ValueError, IndexError):
        return None
    if port <= 0 or port > 65535 or not target_path.startswith("/devtools/browser/"):
        return None
    return DevToolsEndpoint(port, f"ws://{host}:{port}{target_path}")


def _fetch_version(host: str, port: int, timeout: float) -> DevToolsEndpoint:
    # Explicitly disable environment proxies.  A local DevTools endpoint must
    # never be sent through ALL_PROXY/HTTP_PROXY inherited from slaigpus.
    opener = build_opener(ProxyHandler({}))
    url = f"http://{host}:{port}/json/version"
    try:
        with opener.open(url, timeout=max(0.05, timeout)) as response:
            payload = json.loads(response.read().decode("utf-8"))
        ws_url = payload.get("webSocketDebuggerUrl")
    except Exception as exc:  # noqa: BLE001 - converted to a redacted error
        raise CDPError("DevTools endpoint is not ready") from exc
    if not isinstance(ws_url, str):
        raise CDPError("DevTools endpoint returned no browser websocket")
    parsed = urlsplit(ws_url)
    if parsed.scheme not in ("ws", "wss") or not _is_loopback(parsed.hostname):
        raise CDPError("DevTools returned an unsafe browser websocket endpoint")
    if (parsed.port or (443 if parsed.scheme == "wss" else 80)) != port:
        raise CDPError("DevTools returned a websocket on an unexpected port")
    if not parsed.path.startswith("/devtools/browser/"):
        raise CDPError("DevTools returned an invalid browser websocket path")
    return DevToolsEndpoint(port, ws_url)


def wait_for_devtools(
    cdp_port: int,
    chrome: Any = None,
    timeout: float = 10.0,
    *,
    host: str = "127.0.0.1",
    profile_dir: Optional[Path] = None,
) -> DevToolsEndpoint:
    """Wait for Chrome's browser-level DevTools websocket.

    A positive ``cdp_port`` is resolved through ``/json/version``.  Port zero
    is Chrome's random-port mode and is discovered from
    ``PROFILE/DevToolsActivePort``.  In the latter case ``profile_dir`` may be
    omitted when it can be recovered from the Popen-like ``chrome.args``.
    """
    if not _is_loopback(host):
        raise CDPError("DevTools host must be loopback")
    try:
        port = int(cdp_port)
    except (TypeError, ValueError) as exc:
        raise CDPError("invalid DevTools port") from exc
    if port < 0 or port > 65535:
        raise CDPError("invalid DevTools port")

    profile = Path(profile_dir).expanduser() if profile_dir else _profile_from_chrome(chrome)
    if port == 0 and profile is None:
        raise CDPError("random DevTools port requires Chrome's profile directory")

    deadline = time.monotonic() + max(0.0, float(timeout))
    last_error: Optional[BaseException] = None
    while True:
        if chrome is not None:
            try:
                if chrome.poll() is not None:
                    raise CDPError("Chrome exited before DevTools became ready")
            except AttributeError:
                pass

        try:
            if port:
                return _fetch_version(host, port, max(0.05, deadline - time.monotonic()))
            assert profile is not None
            endpoint = _active_port_endpoint(profile, host)
            if endpoint is not None:
                verified = _fetch_version(
                    host,
                    endpoint.port,
                    max(0.05, deadline - time.monotonic()),
                )
                if urlsplit(verified.browser_ws_url).path != urlsplit(
                    endpoint.browser_ws_url
                ).path:
                    raise CDPError(
                        "DevTools endpoint does not belong to the managed Chrome profile"
                    )
                return verified
        except CDPError as exc:
            last_error = exc

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if isinstance(last_error, CDPError):
                raise CDPTimeout("timed out waiting for DevTools") from last_error
            raise CDPTimeout("timed out waiting for DevTools")
        time.sleep(min(0.1, remaining))


@dataclass
class _PendingCall:
    event: threading.Event = field(default_factory=threading.Event)
    result: Optional[Dict[str, Any]] = None
    error_code: Optional[Any] = None
    closed: bool = False


class CDPConnection:
    """Thread-safe synchronous command facade over one browser websocket."""

    def __init__(self, websocket: Any, *, default_timeout: float = 10.0) -> None:
        self._websocket = websocket
        self._default_timeout = float(default_timeout)
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: Dict[int, _PendingCall] = {}
        self._listeners: List[Callable[[str, dict, Optional[str]], None]] = []
        self._closed = False
        self._websocket_close_lock = threading.Lock()
        self._websocket_closed = False
        self._reader = threading.Thread(
            target=self._read_loop,
            name="slaigpus-cdp-reader",
            daemon=True,
        )
        self._reader.start()

    @classmethod
    def connect(cls, ws_url: str, *, timeout: float = 10.0) -> "CDPConnection":
        """Connect with ``websocket-client`` without consulting proxy env vars."""
        parsed = urlsplit(ws_url)
        if parsed.scheme != "ws" or not _is_loopback(parsed.hostname):
            raise CDPError("DevTools websocket must be on loopback")
        try:
            port = parsed.port
        except ValueError as exc:
            raise CDPError("invalid DevTools websocket port") from exc
        if port is None or port <= 0:
            raise CDPError("invalid DevTools websocket port")
        try:
            import websocket as websocket_client
        except ModuleNotFoundError as exc:  # pragma: no cover - packaging path
            raise CDPError(
                "websocket-client is required for CCI automation"
            ) from exc
        raw_socket: Optional[socket.socket] = None
        try:
            # websocket-client falls back to HTTP_PROXY when proxy kwargs are
            # unset, even for loopback in some versions.  Supplying an already
            # connected loopback socket bypasses all proxy discovery by
            # construction while preserving its WebSocket handshake logic.
            raw_socket = socket.create_connection(
                (parsed.hostname or "127.0.0.1", port), timeout=float(timeout)
            )
            ws = websocket_client.create_connection(
                ws_url,
                timeout=float(timeout),
                suppress_origin=True,
                enable_multithread=True,
                socket=raw_socket,
            )
            raw_socket = None  # ownership transferred to websocket-client
            # The reader owns recv() and blocks until data or close.  Command
            # deadlines are managed independently by call().
            try:
                ws.settimeout(None)
            except (AttributeError, OSError):
                pass
        except Exception as exc:  # noqa: BLE001 - never echo endpoint details
            if raw_socket is not None:
                try:
                    raw_socket.close()
                except OSError:
                    pass
            raise CDPError("could not connect to DevTools") from exc
        return cls(ws, default_timeout=timeout)

    def __repr__(self) -> str:
        state = "closed" if self._closed else "connected"
        return f"<CDPConnection {state}>"

    @property
    def is_closed(self) -> bool:
        """Whether the reader or an explicit close ended this connection."""
        with self._state_lock:
            return self._closed

    def add_listener(
        self, callback: Callable[[str, dict, Optional[str]], None]
    ) -> None:
        with self._state_lock:
            if self._closed:
                raise CDPError("DevTools connection is closed")
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(
        self, callback: Callable[[str, dict, Optional[str]], None]
    ) -> None:
        with self._state_lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    def call(
        self,
        method: str,
        params: Optional[dict] = None,
        *,
        session_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        if threading.current_thread() is self._reader:
            raise CDPError("CDP listeners cannot make synchronous CDP calls")
        with self._id_lock:
            self._next_id += 1
            call_id = self._next_id
        pending = _PendingCall()
        with self._state_lock:
            if self._closed:
                raise CDPError("DevTools connection is closed")
            self._pending[call_id] = pending

        message: Dict[str, Any] = {"id": call_id, "method": str(method)}
        if params:
            message["params"] = params
        if session_id:
            message["sessionId"] = session_id
        try:
            encoded = json.dumps(message, separators=(",", ":"))
            with self._send_lock:
                self._websocket.send(encoded)
        except Exception as exc:  # noqa: BLE001 - redact message and params
            with self._state_lock:
                self._pending.pop(call_id, None)
            raise CDPError(f"CDP command {method} could not be sent") from exc

        wait_timeout = self._default_timeout if timeout is None else float(timeout)
        if not pending.event.wait(max(0.0, wait_timeout)):
            with self._state_lock:
                self._pending.pop(call_id, None)
            raise CDPTimeout(f"CDP command {method} timed out")
        if pending.closed:
            raise CDPError(f"CDP connection closed during {method}")
        if pending.error_code is not None:
            # CDP's message/data can reflect evaluated source and arguments, so
            # report only the method and numeric protocol code.
            raise CDPError(f"CDP command {method} failed (code {pending.error_code})")
        return pending.result or {}

    def _read_loop(self) -> None:
        try:
            while True:
                raw = self._websocket.recv()
                if raw in (None, "", b""):
                    break
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError, UnicodeDecodeError):
                    break
                if not isinstance(message, dict):
                    continue
                call_id = message.get("id")
                if isinstance(call_id, int):
                    with self._state_lock:
                        pending = self._pending.pop(call_id, None)
                    if pending is None:
                        continue
                    error = message.get("error")
                    if isinstance(error, dict):
                        code = error.get("code")
                        # Protocol codes are integers.  Never interpolate a
                        # server-provided string into an exception: it could
                        # reflect evaluated source or request arguments.
                        pending.error_code = code if isinstance(code, int) else "unknown"
                    else:
                        result = message.get("result")
                        pending.result = result if isinstance(result, dict) else {}
                    pending.event.set()
                    continue

                method = message.get("method")
                params = message.get("params")
                if not isinstance(method, str) or not isinstance(params, dict):
                    continue
                session_id = message.get("sessionId")
                if not isinstance(session_id, str):
                    session_id = None
                with self._state_lock:
                    listeners = list(self._listeners)
                for callback in listeners:
                    try:
                        callback(method, params, session_id)
                    except Exception:  # noqa: BLE001 - listener isolation, no logging
                        pass
        except Exception:  # noqa: BLE001 - close and wake callers, never log raw data
            pass
        finally:
            self._mark_closed()
            self._close_websocket()

    def _mark_closed(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for item in pending:
            item.closed = True
            item.event.set()

    def close(self) -> None:
        self._mark_closed()
        self._close_websocket()
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=1.0)

    def _close_websocket(self) -> None:
        with self._websocket_close_lock:
            if self._websocket_closed:
                return
            self._websocket_closed = True
            shutdown = getattr(self._websocket, "shutdown", None)
            try:
                if callable(shutdown):
                    # recv() belongs exclusively to the reader thread.  An
                    # immediate socket shutdown wakes it without starting a
                    # competing graceful-close receive loop in this thread.
                    shutdown()
                else:
                    self._websocket.close()
            except Exception:  # noqa: BLE001 - best-effort close
                try:
                    self._websocket.close()
                except Exception:
                    pass

    def __enter__(self) -> "CDPConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.close()
        return False


@dataclass(frozen=True, repr=False)
class AuthLease:
    """A generation-tagged authorization value with a permanently redacted repr."""

    generation: int
    authorization: str = field(repr=False)

    def _header_value(self) -> str:
        return self.authorization

    def __repr__(self) -> str:
        return f"AuthLease(generation={self.generation}, authorization=<redacted>)"


@dataclass(repr=False)
class _AuthCandidate:
    touched: float
    allowed: Optional[bool] = None
    request_seen: bool = False
    authorization: Optional[str] = None
    # ``responseReceived.status`` is the browser-visible/effective status.  Its
    # ExtraInfo companion reports the raw network status and can legitimately
    # disagree for a cache revalidation (effective 200, raw 304).  Keeping them
    # separate prevents whichever event happens to arrive first from deciding
    # the candidate incorrectly.
    effective_status: Optional[int] = None
    raw_status: Optional[int] = None
    diagnostic_request_counted: bool = False
    diagnostic_bearer_counted: bool = False
    diagnostic_effective_2xx_counted: bool = False


_BEARER_RE = re.compile(r"^Bearer[\t ]+[^\s\r\n]+$", re.IGNORECASE)


def _header(headers: Any, name: str) -> Optional[str]:
    if not isinstance(headers, Mapping):
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted and isinstance(value, str):
            return value
    return None


def _bearer_from_headers(headers: Any) -> Optional[str]:
    value = _header(headers, "authorization")
    if value is None:
        return None
    value = value.strip()
    return value if _BEARER_RE.fullmatch(value) else None


def _default_port(scheme: str) -> Optional[int]:
    if scheme == "https":
        return 443
    if scheme == "http":
        return 80
    return None


def _url_matches_base(url: str, base: str) -> bool:
    """Origin + decoded path-boundary match, safe against look-alike hosts."""
    try:
        candidate = urlsplit(url)
        allowed = urlsplit(base)
        candidate_port = candidate.port
        allowed_port = allowed.port
    except (TypeError, ValueError):
        return False
    if candidate.username is not None or candidate.password is not None:
        return False
    candidate_scheme = candidate.scheme.lower()
    allowed_scheme = allowed.scheme.lower()
    if candidate_scheme != allowed_scheme or candidate_scheme != "https":
        return False
    if (candidate.hostname or "").lower() != (allowed.hostname or "").lower():
        return False
    if (candidate_port or _default_port(candidate_scheme)) != (
        allowed_port or _default_port(allowed_scheme)
    ):
        return False

    candidate_path = unquote(candidate.path or "/")
    allowed_path = unquote(allowed.path or "/")
    if "\\" in candidate_path or "\\" in allowed_path:
        return False
    # Dot segments can be normalized by the browser after our check.  Reject
    # them rather than attempting to duplicate every URL parser's rules.
    if any(segment in (".", "..") for segment in candidate_path.split("/")):
        return False
    prefix = allowed_path.rstrip("/")
    if not prefix:
        prefix = "/"
    if prefix == "/":
        return candidate_path.startswith("/")
    return candidate_path == prefix or candidate_path.startswith(prefix + "/")


def _url_matches_exact_path(url: str, endpoint: str) -> bool:
    """Match one HTTPS origin/path while permitting ordinary query values."""
    if not _url_matches_base(url, endpoint):
        return False
    try:
        candidate_path = unquote(urlsplit(url).path or "/")
        endpoint_path = unquote(urlsplit(endpoint).path or "/")
    except (TypeError, ValueError):
        return False
    return candidate_path == endpoint_path


class CCIAuthorization:
    """Promote bearer tokens observed on successful, exact API requests."""

    def __init__(
        self,
        api_base: str = CCI_API_BASE,
        *,
        exact_path: bool = False,
        allowed_methods: Optional[Sequence[str]] = None,
        candidate_ttl: float = 30.0,
        max_candidates: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.exact_path = bool(exact_path)
        self.allowed_methods = (
            None
            if allowed_methods is None
            else frozenset(str(method).upper() for method in allowed_methods)
        )
        self._candidate_ttl = float(candidate_ttl)
        self._max_candidates = max(1, int(max_candidates))
        self._clock = clock
        self._condition = threading.Condition()
        self._candidates: "OrderedDict[Tuple[str, str], _AuthCandidate]" = OrderedDict()
        self._finished: "OrderedDict[Tuple[str, str], float]" = OrderedDict()
        self._generation = 0
        self._current: Optional[AuthLease] = None
        self._closed = False
        self._diagnostic_counts = {
            "exact_requests": 0,
            "bearer_requests": 0,
            "effective_2xx": 0,
            "promotions": 0,
        }

    def __repr__(self) -> str:
        with self._condition:
            state = "ready" if self._current is not None else "waiting"
            return f"<CCIAuthorization generation={self._generation} {state}>"

    def _key(self, params: dict, session_id: Optional[str]) -> Optional[Tuple[str, str]]:
        request_id = params.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            return None
        return session_id or "", request_id

    def _allowed(self, url: str) -> bool:
        matcher = _url_matches_exact_path if self.exact_path else _url_matches_base
        return matcher(url, self.api_base)

    def _request_allowed(self, request: Mapping[str, Any]) -> bool:
        url = request.get("url")
        if not isinstance(url, str) or not self._allowed(url):
            return False
        if self.allowed_methods is None:
            return True
        method = request.get("method")
        return isinstance(method, str) and method.upper() in self.allowed_methods

    def _prune(self, now: float) -> None:
        cutoff = now - self._candidate_ttl
        for key, candidate in list(self._candidates.items()):
            if candidate.touched >= cutoff:
                break
            self._candidates.pop(key, None)
        for key, touched in list(self._finished.items()):
            if touched >= cutoff:
                break
            self._finished.pop(key, None)
        while len(self._candidates) > self._max_candidates:
            self._candidates.popitem(last=False)
        while len(self._finished) > self._max_candidates:
            self._finished.popitem(last=False)

    def _candidate(self, key: Tuple[str, str], now: float) -> _AuthCandidate:
        candidate = self._candidates.get(key)
        if candidate is None:
            candidate = _AuthCandidate(touched=now)
            self._candidates[key] = candidate
        else:
            candidate.touched = now
            self._candidates.move_to_end(key)
        return candidate

    def _finish(self, key: Tuple[str, str], now: float) -> None:
        self._candidates.pop(key, None)
        self._finished[key] = now
        self._finished.move_to_end(key)

    def _update_diagnostic_counts(self, candidate: _AuthCandidate) -> None:
        """Record token-free facts only after the exact API base is proven."""
        if candidate.allowed is not True:
            return
        if not candidate.diagnostic_request_counted:
            candidate.diagnostic_request_counted = True
            self._diagnostic_counts["exact_requests"] += 1
        if (
            candidate.authorization is not None
            and not candidate.diagnostic_bearer_counted
        ):
            candidate.diagnostic_bearer_counted = True
            self._diagnostic_counts["bearer_requests"] += 1
        if (
            candidate.effective_status is not None
            and 200 <= candidate.effective_status < 300
            and not candidate.diagnostic_effective_2xx_counted
        ):
            candidate.diagnostic_effective_2xx_counted = True
            self._diagnostic_counts["effective_2xx"] += 1

    def _try_promote(
        self, key: Tuple[str, str], candidate: _AuthCandidate, now: float
    ) -> None:
        self._update_diagnostic_counts(candidate)
        if candidate.effective_status is None:
            return
        if not 200 <= candidate.effective_status < 300:
            self._finish(key, now)
            return
        if candidate.allowed is not True or candidate.authorization is None:
            return
        self._generation += 1
        self._current = AuthLease(self._generation, candidate.authorization)
        self._diagnostic_counts["promotions"] += 1
        self._finish(key, now)
        self._condition.notify_all()

    def feed_event(
        self, method: str, params: dict, session_id: Optional[str] = None
    ) -> None:
        """Consume Network domain events; safe to call on the CDP reader thread."""
        if not isinstance(params, dict):
            return
        key = self._key(params, session_id)
        if key is None:
            return
        now = self._clock()
        with self._condition:
            if self._closed:
                return
            self._prune(now)

            if method == "Network.requestWillBeSent":
                # A redirect reuses requestId.  Its new URL and headers must be
                # evaluated independently of the previous hop.
                if "redirectResponse" in params:
                    self._candidates.pop(key, None)
                    self._finished.pop(key, None)
                candidate = self._candidate(key, now)
                request = params.get("request")
                if not isinstance(request, dict):
                    return
                candidate.request_seen = True
                candidate.allowed = self._request_allowed(request)
                authorization = _bearer_from_headers(request.get("headers"))
                if authorization is not None:
                    candidate.authorization = authorization
                self._try_promote(key, candidate, now)
                return

            if method == "Network.requestWillBeSentExtraInfo":
                if key in self._finished:
                    return
                authorization = _bearer_from_headers(params.get("headers"))
                if authorization is None:
                    return
                candidate = self._candidate(key, now)
                candidate.authorization = authorization
                self._try_promote(key, candidate, now)
                return

            if method == "Network.responseReceived":
                if key in self._finished:
                    return
                candidate = self._candidate(key, now)
                response = params.get("response")
                if not isinstance(response, dict):
                    return
                url = response.get("url")
                if isinstance(url, str):
                    allowed = self._allowed(url)
                    if candidate.request_seen or self.allowed_methods is None:
                        candidate.allowed = (
                            allowed
                            if candidate.allowed is None
                            else candidate.allowed and allowed
                        )
                    elif not allowed:
                        # With a method allow-list, an otherwise matching
                        # response cannot authorize until requestWillBeSent
                        # proves the request method too.
                        candidate.allowed = False
                try:
                    candidate.effective_status = int(response.get("status"))
                except (TypeError, ValueError):
                    return
                self._try_promote(key, candidate, now)
                return

            if method == "Network.responseReceivedExtraInfo":
                if key in self._finished:
                    return
                candidate = self._candidate(key, now)
                try:
                    candidate.raw_status = int(params.get("statusCode"))
                except (TypeError, ValueError):
                    return
                # Raw status is retained for event correlation only.  In
                # particular, a raw cache-validation 304 must not override the
                # effective 200 delivered by Network.responseReceived.
                self._try_promote(key, candidate, now)
                return

            if method == "Network.loadingFailed":
                candidate = self._candidates.get(key)
                if candidate is not None:
                    self._try_promote(key, candidate, now)
                    if key in self._candidates:
                        self._finish(key, now)
                return

            if method == "Network.loadingFinished":
                candidate = self._candidates.get(key)
                if candidate is None:
                    return
                # Retention below is measured from completion, not from an
                # arbitrarily earlier request/response event.
                candidate = self._candidate(key, now)
                self._try_promote(key, candidate, now)
                if key not in self._candidates:
                    return
                # ExtraInfo event order is not tied to loadingFinished.  Keep a
                # browser-visible successful candidate until its bounded TTL so
                # a late Authorization header can still complete it.  Failed,
                # disallowed, or status-less requests are final immediately.
                if (
                    candidate.effective_status is not None
                    and 200 <= candidate.effective_status < 300
                    and candidate.allowed is not False
                ):
                    return
                self._finish(key, now)

    def wait(
        self, *, after_generation: int = 0, timeout: Optional[float] = 60.0
    ) -> AuthLease:
        deadline = None if timeout is None else self._clock() + max(0.0, float(timeout))
        with self._condition:
            while True:
                if self._closed:
                    raise CDPError("authorization capture is closed")
                if (
                    self._current is not None
                    and self._current.generation > int(after_generation)
                ):
                    return self._current
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise CDPTimeout("timed out waiting for browser authorization")
                self._condition.wait(remaining)

    def current(self) -> Optional[AuthLease]:
        with self._condition:
            return self._current

    @property
    def diagnostic_counts(self) -> Dict[str, int]:
        """Return token-free counters suitable for coarse support diagnostics."""
        with self._condition:
            return dict(self._diagnostic_counts)

    def invalidate(self, generation: int) -> None:
        """Clear only the lease that produced a 401, never a newer rotation."""
        with self._condition:
            if self._current is not None and self._current.generation == generation:
                self._current = None
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._current = None
            self._candidates.clear()
            self._finished.clear()
            self._condition.notify_all()


# The shorter name reads naturally in callers and preserves the interface used
# by the CLI/controller design notes.
CCIAuthCapture = CCIAuthorization


@dataclass(frozen=True)
class BrowserResponse:
    status: int
    text: str = field(repr=False)
    auth_generation: int = 0

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.text)


_FETCH_FUNCTION = """async function(request) {
  const options = {
    method: request.method,
    headers: request.headers,
    credentials: "omit",
    redirect: "error"
  };
  if (request.body !== null) options.body = request.body;
  const response = await fetch(request.url, options);
  return {status: response.status, text: await response.text()};
}"""


# Every login flow is bootstrapped at the parameter-free enterprise URL.  The
# site then routes through Console, OAuth and IAM before producing the shared
# sign-in challenge.  Credentials may be offered only after the CDP transport
# has proved that bounded route in its own target/session; the URL alone is
# never authority.
SENSECORE_LOGIN_ORIGIN = "https://zhicheng.signin.sensecore.cn"
SENSECORE_LOGIN_PATH = "/"
SENSECORE_LOGIN_URL = "https://zhicheng.signin.sensecore.cn/"
SENSECORE_CHALLENGE_ORIGIN = "https://signin.sensecore.cn"
SENSECORE_CONSOLE_ORIGIN = "https://console.sensecore.cn"
SENSECORE_CONSOLE_ROOT_URL = "https://console.sensecore.cn/"
SENSECORE_CONSOLE_LOGIN_TERMINAL_PATH = "/home"
SENSECORE_CONSOLE_REGION_HOME_URL = (
    "https://console.sensecore.cn/cn-sh-01/home"
)
SENSECORE_IAM_HOST = "iam.sensecoreapi.cn"
# The Console calls this exact read-only identity endpoint after a successful
# login and sends the same short-lived Bearer accepted by regional services.
# Capturing it removes the circular dependency where slaigpus previously had
# to wait for the CCI micro-frontend to make a CCI request before it could make
# its own first read-only CCI request.
SENSECORE_IAM_AUTH_CAPTURE_URL = (
    "https://iam.sensecoreapi.cn/iam/idp/v1/myRegionAndAzs"
)
_LOGIN_MAX_IAM_HOPS = 2
_LOGIN_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))
_LOGIN_CONSOLE_ROOT_GRACE_SECONDS = 15.0


def _canonical_login_challenge_url(url: Any) -> Optional[str]:
    """Return a strictly canonical challenge URL, otherwise ``None``.

    The opaque challenge is intentionally not decoded into a stored field and
    never appears in reprs or errors.  Raw parameter names are checked before
    decoding so percent-encoded key aliases cannot broaden the allow-list.
    """
    if (
        not isinstance(url, str)
        or not url
        or len(url) > 16384
        or url.strip() != url
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in url)
    ):
        return None
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or parsed.netloc != "signin.sensecore.cn"
        or parsed.path != "/"
        or parsed.fragment
        or not parsed.query
    ):
        return None
    parts = parsed.query.split("&")
    if len(parts) != 2:
        return None
    raw_values: Dict[str, str] = {}
    for part in parts:
        key, separator, raw_value = part.partition("=")
        if separator != "=" or key not in {"login_challenge", "platform"}:
            return None
        if key in raw_values or not raw_value:
            return None
        raw_values[key] = raw_value
    if set(raw_values) != {"login_challenge", "platform"}:
        return None
    if raw_values["platform"] != "console":
        return None
    try:
        challenge = unquote_plus(raw_values["login_challenge"], errors="strict")
    except (UnicodeDecodeError, ValueError):
        return None
    if (
        not challenge
        or len(challenge) > 4096
        or challenge.strip() != challenge
        or any(ord(char) < 0x20 or ord(char) == 0x7F or char.isspace() for char in challenge)
    ):
        return None
    return url


def _is_console_login_terminal_url(url: Any) -> bool:
    """Whether *url* is a strict post-login console candidate.

    This is not API authentication by itself: a successful Bearer capture from
    the configured exact endpoint in the same owned session remains required.
    CCI uses its read-only IAM identity request and does not navigate its app
    page merely to obtain that credential.
    """
    if (
        not isinstance(url, str)
        or not url
        or len(url) > 16384
        or url.strip() != url
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in url)
    ):
        return False
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return False
    if (
        parsed.scheme != "https"
        or parsed.netloc != "console.sensecore.cn"
        or not parsed.path.startswith("/")
        or parsed.fragment
        or "\\" in parsed.path
        or "%5c" in parsed.path.lower()
    ):
        return False
    for part in parsed.query.split("&") if parsed.query else ():
        raw_key = part.partition("=")[0]
        try:
            key = unquote_plus(raw_key, errors="strict").lower()
        except (UnicodeDecodeError, ValueError):
            return False
        if key in {"error", "error_description"}:
            return False
    return True


def _canonical_console_login_terminal_url(url: Any) -> Optional[str]:
    """Pin the exact console landing path used by the OAuth login flow."""
    if not _is_console_login_terminal_url(url):
        return None
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):  # pragma: no cover - checked above
        return None
    return url if parsed.path == SENSECORE_CONSOLE_LOGIN_TERMINAL_PATH else None


def _canonical_console_landing_url(url: Any) -> Optional[str]:
    """Return one fixed Console landing or exact OAuth-result URL shape."""
    if url in {
        SENSECORE_CONSOLE_ROOT_URL,
        SENSECORE_CONSOLE_ORIGIN + SENSECORE_CONSOLE_LOGIN_TERMINAL_PATH,
        SENSECORE_CONSOLE_REGION_HOME_URL,
    }:
        return str(url)
    # The Console SPA commits ``/home`` first and then preserves the OAuth
    # result with a History API update.  Values are deliberately opaque, but
    # the shape must be exact: no extra/encoded/duplicate/empty keys.  This is
    # still only a provisional landing; a successful exact configured Bearer
    # capture remains the API-authentication proof.
    if not _is_console_login_terminal_url(url):
        return None
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):  # pragma: no cover - checked above
        return None
    if parsed.path not in {
        SENSECORE_CONSOLE_LOGIN_TERMINAL_PATH,
        urlsplit(SENSECORE_CONSOLE_REGION_HOME_URL).path,
    } or not parsed.query:
        return None
    values: Dict[str, str] = {}
    for part in parsed.query.split("&"):
        key, separator, raw_value = part.partition("=")
        if (
            separator != "="
            or key not in {"code", "scope", "state"}
            or key in values
            or not raw_value
        ):
            return None
        values[key] = raw_value
    if set(values) != {"code", "scope", "state"}:
        return None
    return str(url)


def _canonical_oauth_authorization_url(url: Any) -> Optional[str]:
    """Pin one strict shared-signin OAuth authorization URL.

    Query values are deliberately opaque.  The full URL is retained only in
    the private transport state so that the following redirect can be tied to
    this exact navigation without ever placing its parameters in diagnostics.
    """
    if (
        not isinstance(url, str)
        or not url
        or len(url) > 16384
        or url.strip() != url
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in url)
    ):
        return None
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or parsed.netloc != "signin.sensecore.cn"
        or parsed.path != "/oauth2/auth"
        or parsed.fragment
        or not parsed.query
    ):
        return None
    return url


def _canonical_iam_authorization_url(url: Any) -> Optional[str]:
    """Pin one HTTPS IAM authorization endpoint, otherwise return ``None``."""
    if (
        not isinstance(url, str)
        or not url
        or len(url) > 16384
        or url.strip() != url
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in url)
    ):
        return None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname != SENSECORE_IAM_HOST
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or not parsed.path.startswith("/")
        or "\\" in parsed.path
        or "%5c" in parsed.path.lower()
    ):
        return None
    return url


_LOGIN_INSPECT_FUNCTION = """function(trustedChallengeURL) {
  const allowedURL = "https://zhicheng.signin.sensecore.cn/";
  const allowedOrigin = "https://zhicheng.signin.sensecore.cn";
  const allowedPath = "/";
  const canonicalChallenge = (value) => {
    if (typeof value !== "string" || value.length === 0 || value.length > 16384) {
      return null;
    }
    let parsed;
    try { parsed = new URL(value); } catch (_) { return null; }
    if (parsed.protocol !== "https:" || parsed.host !== "signin.sensecore.cn" ||
        parsed.username !== "" || parsed.password !== "" ||
        parsed.pathname !== "/" || parsed.hash !== "" || parsed.search === "") {
      return null;
    }
    const parts = parsed.search.slice(1).split("&");
    if (parts.length !== 2) return null;
    const raw = new Map();
    for (const part of parts) {
      const separator = part.indexOf("=");
      if (separator <= 0) return null;
      const key = part.slice(0, separator);
      const rawValue = part.slice(separator + 1);
      if (!["login_challenge", "platform"].includes(key) ||
          raw.has(key) || rawValue === "") return null;
      raw.set(key, rawValue);
    }
    if (raw.size !== 2 || raw.get("platform") !== "console") return null;
    let challenge;
    try {
      challenge = decodeURIComponent(raw.get("login_challenge").replace(/\\+/g, " "));
    } catch (_) { return null; }
    if (challenge.length === 0 || challenge.length > 4096 ||
        challenge.trim() !== challenge || /[\\u0000-\\u0020\\u007f]/.test(challenge)) {
      return null;
    }
    return parsed.href === value ? value : null;
  };
  const internalIAMChallenge = (pinned, current) => {
    if (pinned === null || typeof current !== "string" ||
        current.length === 0 || current.length > 16384) return false;
    let pinnedParsed;
    let currentParsed;
    try {
      pinnedParsed = new URL(pinned);
      currentParsed = new URL(current);
    } catch (_) { return false; }
    if (currentParsed.protocol !== "https:" ||
        currentParsed.host !== "signin.sensecore.cn" ||
        currentParsed.username !== "" || currentParsed.password !== "" ||
        currentParsed.pathname !== "/" || currentParsed.hash !== "" ||
        currentParsed.href !== current) return false;

    const pinnedRaw = new Map();
    for (const part of pinnedParsed.search.slice(1).split("&")) {
      const separator = part.indexOf("=");
      pinnedRaw.set(part.slice(0, separator), part.slice(separator + 1));
    }
    const parts = currentParsed.search.slice(1).split("&");
    if (parts.length !== 3) return false;
    const currentRaw = new Map();
    for (const part of parts) {
      const separator = part.indexOf("=");
      if (separator < 0) {
        if (part !== "IAM" || currentRaw.has("IAM")) return false;
        currentRaw.set("IAM", "");
        continue;
      }
      const key = part.slice(0, separator);
      const rawValue = part.slice(separator + 1);
      if (!["login_challenge", "platform", "IAM"].includes(key) ||
          currentRaw.has(key) ||
          (key === "IAM" ? rawValue !== "" : rawValue === "")) return false;
      currentRaw.set(key, rawValue);
    }
    if (currentRaw.size !== 3 || currentRaw.get("platform") !== "console" ||
        currentRaw.get("IAM") !== "") return false;
    try {
      const pinnedToken = decodeURIComponent(
        pinnedRaw.get("login_challenge").replace(/\\+/g, " "));
      const currentToken = decodeURIComponent(
        currentRaw.get("login_challenge").replace(/\\+/g, " "));
      return currentToken === pinnedToken;
    } catch (_) { return false; }
  };
  const pinnedChallenge = canonicalChallenge(trustedChallengeURL);
  const credentialPageAllowed = pinnedChallenge !== null &&
    internalIAMChallenge(pinnedChallenge, location.href);
  const pageAllowed = () => (location.href === allowedURL &&
    location.origin === allowedOrigin && location.pathname === allowedPath &&
    location.search === "" && location.hash === "") ||
    (pinnedChallenge !== null && (location.href === pinnedChallenge ||
      internalIAMChallenge(pinnedChallenge, location.href)));
  const consoleTerminal = () => {
    let parsed;
    try { parsed = new URL(location.href); } catch (_) { return false; }
    if (parsed.protocol !== "https:" || parsed.host !== "console.sensecore.cn" ||
        parsed.username !== "" || parsed.password !== "" ||
        parsed.hash !== "" || !parsed.pathname.startsWith("/") ||
        parsed.pathname.includes("\\\\") ||
        /%5c/i.test(parsed.pathname)) return false;
    for (const key of parsed.searchParams.keys()) {
      if (["error", "error_description"].includes(key.toLowerCase())) return false;
    }
    return true;
  };
  if (!pageAllowed()) return consoleTerminal() ? "departed" : "untrusted";
  // The original two-parameter challenge proves network provenance only.
  // SenseCore's password-login view first marks the same document with one
  // empty IAM flag through history state; credentials stay unloaded until
  // that exact internal variant is present.
  if (!credentialPageAllowed) return "loading";

  const visible = (element) => {
    if (!(element instanceof HTMLElement) || element.disabled) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      Number(style.opacity || "1") !== 0 && rect.width > 0 && rect.height > 0;
  };
  const metadata = (element) => [
    element.getAttribute("name") || "",
    element.id || "",
    typeof element.className === "string" ? element.className : "",
    element.getAttribute("autocomplete") || "",
    element.getAttribute("placeholder") || "",
    element.getAttribute("aria-label") || ""
  ].join(" ").toLowerCase();

  const hardChallengePresent = () => {
    const inputs = Array.from(document.querySelectorAll("input")).filter(visible);
    if (inputs.some((element) => element.autocomplete === "one-time-code" ||
      /captcha|\\botp\\b|\\bmfa\\b|verification|verify[_-]?code|sms[_-]?code|验证码|动态验证/.test(metadata(element)))) {
      return true;
    }
    const widgets = Array.from(document.querySelectorAll(
      "iframe, [id], [class], button, a, [role=button]"))
      .filter(visible);
    if (widgets.some((element) => {
      const value = [metadata(element), element.getAttribute("src") || "",
        element.getAttribute("title") || "", element.innerText || ""]
        .join(" ").toLowerCase();
      return /captcha|recaptcha|hcaptcha|geetest|turnstile|\\botp\\b|\\bmfa\\b|verification|verify[_-]?code|sms[_-]?code|验证码|动态验证/.test(value);
    })) return true;
    return false;
  };
  const alternativeLoginPresent = (root) =>
    Array.from(root.querySelectorAll("button, a, [role=button]"))
      .filter(visible)
      .some((element) => /passkey|webauthn|security key|通行密钥|安全密钥|二维码|扫码/.test(
        [metadata(element), element.innerText || ""].join(" ").toLowerCase()
      ));

  const matchLoginForm = () => {
    const forms = Array.from(document.querySelectorAll("form")).filter(visible);
    if (forms.length !== 1) return null;
    const form = forms[0];
    const extraControls = Array.from(form.querySelectorAll("textarea, select"))
      .filter(visible);
    if (extraControls.length !== 0) return null;
    const inputs = Array.from(form.querySelectorAll("input")).filter(visible);
    const passwords = inputs.filter((element) => element.type === "password");
    if (passwords.length !== 1) return null;
    const password = passwords[0];
    const formMetadata = [metadata(form), metadata(password),
      form.getAttribute("action") || ""].join(" ").toLowerCase();
    if (!["", "current-password"].includes(password.autocomplete) ||
      password.id !== "password" || password.getAttribute("name") !== "password" ||
      /(?:new|confirm|repeat)[_-]?(?:password|passwd)|(?:reset|change|forgot|recover|retrieve|register|sign[_ -]?up)[_-]?(?:password|passwd)?|新密码|确认密码|重复密码|重置|修改密码|找回密码|注册/.test(formMetadata)) {
      return null;
    }
    // The verified SenseCore React/Ant Design form has no native action or
    // method.  A synthetic, cancelable submit below is accepted only when its
    // hydrated React listener prevents the browser default; native GET/POST
    // submission is never allowed to carry credentials.
    if (form.hasAttribute("method") || form.hasAttribute("action")) return null;

    const usernames = inputs.filter((element) =>
      ["text", "email"].includes(element.type) && element.id === "username" &&
      element.getAttribute("name") === "username" &&
      ["", "username"].includes(element.autocomplete));
    if (usernames.length !== 1) return null;
    const username = usernames[0];
    const tenants = inputs.filter((element) => {
      const placeholder = element.getAttribute("placeholder") || "";
      return element.type === "text" && element.id === "tenant_code" &&
        element.getAttribute("name") === "tenant_code" &&
        element.autocomplete === "" && placeholder.includes("企业") &&
        placeholder.includes("标识");
    });
    if (tenants.length !== 1) return null;
    const tenant = tenants[0];
    const allowedAuxiliaryTypes = new Set(["checkbox", "radio", "hidden", "submit", "button"]);
    if (inputs.some((element) => element !== username && element !== password &&
        element !== tenant &&
        !allowedAuxiliaryTypes.has(element.type))) {
      return null;
    }

    const buttons = Array.from(form.querySelectorAll("button, input[type=submit]"))
      .filter(visible);
    const selected = buttons.filter((element) => {
      const text = (element.innerText || element.value || "")
        .replace(/\\s+/g, "").toLowerCase();
      const classes = (typeof element.className === "string" ? element.className : "")
        .split(/\\s+/);
      return element.type === "submit" && classes.includes("login_submit") &&
        (text === "登录" || text === "signin" || text === "login") &&
        !/reset|change|forgot|recover|retrieve|register|sign[_ -]?up|重置|修改|找回|注册/.test(
          [metadata(element), text].join(" ")
        );
    });
    return selected.length === 1 ?
      {form, username, tenant, password, button: selected[0]} : null;
  };

  if (hardChallengePresent()) return "challenge";
  const match = matchLoginForm();
  if (match) {
    // A page may offer QR/passkey as a separate optional tab next to its
    // ordinary password form.  Only alternatives inside the matched form are
    // mandatory for this submission path.  With no strict password form, any
    // visible alternative remains a challenge and credentials stay unloaded.
    return alternativeLoginPresent(match.form) ? "challenge" : "password_form";
  }
  if (alternativeLoginPresent(document)) return "challenge";
  const visiblePasswords = Array.from(document.querySelectorAll('input[type="password"]'))
    .filter(visible);
  return visiblePasswords.length === 0 ? "loading" : "ambiguous";
}"""


_LOGIN_SUBMIT_FUNCTION = """async function(username, passwordValue, trustedChallengeURL) {
  const canonicalChallenge = (value) => {
    if (typeof value !== "string" || value.length === 0 || value.length > 16384) {
      return null;
    }
    let parsed;
    try { parsed = new URL(value); } catch (_) { return null; }
    if (parsed.protocol !== "https:" || parsed.host !== "signin.sensecore.cn" ||
        parsed.username !== "" || parsed.password !== "" ||
        parsed.pathname !== "/" || parsed.hash !== "" || parsed.search === "") {
      return null;
    }
    const parts = parsed.search.slice(1).split("&");
    if (parts.length !== 2) return null;
    const raw = new Map();
    for (const part of parts) {
      const separator = part.indexOf("=");
      if (separator <= 0) return null;
      const key = part.slice(0, separator);
      const rawValue = part.slice(separator + 1);
      if (!["login_challenge", "platform"].includes(key) ||
          raw.has(key) || rawValue === "") return null;
      raw.set(key, rawValue);
    }
    if (raw.size !== 2 || raw.get("platform") !== "console") return null;
    let challenge;
    try {
      challenge = decodeURIComponent(raw.get("login_challenge").replace(/\\+/g, " "));
    } catch (_) { return null; }
    if (challenge.length === 0 || challenge.length > 4096 ||
        challenge.trim() !== challenge || /[\\u0000-\\u0020\\u007f]/.test(challenge)) {
      return null;
    }
    return parsed.href === value ? value : null;
  };
  const internalIAMChallenge = (pinned, current) => {
    if (pinned === null || typeof current !== "string" ||
        current.length === 0 || current.length > 16384) return false;
    let pinnedParsed;
    let currentParsed;
    try {
      pinnedParsed = new URL(pinned);
      currentParsed = new URL(current);
    } catch (_) { return false; }
    if (currentParsed.protocol !== "https:" ||
        currentParsed.host !== "signin.sensecore.cn" ||
        currentParsed.username !== "" || currentParsed.password !== "" ||
        currentParsed.pathname !== "/" || currentParsed.hash !== "" ||
        currentParsed.href !== current) return false;

    const pinnedRaw = new Map();
    for (const part of pinnedParsed.search.slice(1).split("&")) {
      const separator = part.indexOf("=");
      pinnedRaw.set(part.slice(0, separator), part.slice(separator + 1));
    }
    const parts = currentParsed.search.slice(1).split("&");
    if (parts.length !== 3) return false;
    const currentRaw = new Map();
    for (const part of parts) {
      const separator = part.indexOf("=");
      if (separator < 0) {
        if (part !== "IAM" || currentRaw.has("IAM")) return false;
        currentRaw.set("IAM", "");
        continue;
      }
      const key = part.slice(0, separator);
      const rawValue = part.slice(separator + 1);
      if (!["login_challenge", "platform", "IAM"].includes(key) ||
          currentRaw.has(key) ||
          (key === "IAM" ? rawValue !== "" : rawValue === "")) return false;
      currentRaw.set(key, rawValue);
    }
    if (currentRaw.size !== 3 || currentRaw.get("platform") !== "console" ||
        currentRaw.get("IAM") !== "") return false;
    try {
      const pinnedToken = decodeURIComponent(
        pinnedRaw.get("login_challenge").replace(/\\+/g, " "));
      const currentToken = decodeURIComponent(
        currentRaw.get("login_challenge").replace(/\\+/g, " "));
      return currentToken === pinnedToken;
    } catch (_) { return false; }
  };
  const pinnedChallenge = canonicalChallenge(trustedChallengeURL);
  const pageAllowed = () => pinnedChallenge !== null &&
    internalIAMChallenge(pinnedChallenge, location.href);
  if (!pageAllowed()) return "rejected";

  const visible = (element) => {
    if (!(element instanceof HTMLElement) || element.disabled) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      Number(style.opacity || "1") !== 0 && rect.width > 0 && rect.height > 0;
  };
  const metadata = (element) => [
    element.getAttribute("name") || "",
    element.id || "",
    typeof element.className === "string" ? element.className : "",
    element.getAttribute("autocomplete") || "",
    element.getAttribute("placeholder") || "",
    element.getAttribute("aria-label") || ""
  ].join(" ").toLowerCase();

  const hardChallengePresent = () => {
    const inputs = Array.from(document.querySelectorAll("input")).filter(visible);
    if (inputs.some((element) => element.autocomplete === "one-time-code" ||
      /captcha|\\botp\\b|\\bmfa\\b|verification|verify[_-]?code|sms[_-]?code|验证码|动态验证/.test(metadata(element)))) {
      return true;
    }
    const widgets = Array.from(document.querySelectorAll(
      "iframe, [id], [class], button, a, [role=button]"))
      .filter(visible);
    if (widgets.some((element) => {
      const value = [metadata(element), element.getAttribute("src") || "",
        element.getAttribute("title") || "", element.innerText || ""]
        .join(" ").toLowerCase();
      return /captcha|recaptcha|hcaptcha|geetest|turnstile|\\botp\\b|\\bmfa\\b|verification|verify[_-]?code|sms[_-]?code|验证码|动态验证/.test(value);
    })) return true;
    return false;
  };
  const alternativeLoginPresent = (root) =>
    Array.from(root.querySelectorAll("button, a, [role=button]"))
      .filter(visible)
      .some((element) => /passkey|webauthn|security key|通行密钥|安全密钥|二维码|扫码/.test(
        [metadata(element), element.innerText || ""].join(" ").toLowerCase()
      ));

  const matchLoginForm = () => {
    const forms = Array.from(document.querySelectorAll("form")).filter(visible);
    if (forms.length !== 1) return null;
    const form = forms[0];
    const extraControls = Array.from(form.querySelectorAll("textarea, select"))
      .filter(visible);
    if (extraControls.length !== 0) return null;
    const inputs = Array.from(form.querySelectorAll("input")).filter(visible);
    const passwords = inputs.filter((element) => element.type === "password");
    if (passwords.length !== 1) return null;
    const password = passwords[0];
    const formMetadata = [metadata(form), metadata(password),
      form.getAttribute("action") || ""].join(" ").toLowerCase();
    if (!["", "current-password"].includes(password.autocomplete) ||
      password.id !== "password" || password.getAttribute("name") !== "password" ||
      /(?:new|confirm|repeat)[_-]?(?:password|passwd)|(?:reset|change|forgot|recover|retrieve|register|sign[_ -]?up)[_-]?(?:password|passwd)?|新密码|确认密码|重复密码|重置|修改密码|找回密码|注册/.test(formMetadata)) {
      return null;
    }
    if (form.hasAttribute("method") || form.hasAttribute("action")) return null;

    const usernames = inputs.filter((element) =>
      ["text", "email"].includes(element.type) && element.id === "username" &&
      element.getAttribute("name") === "username" &&
      ["", "username"].includes(element.autocomplete));
    if (usernames.length !== 1) return null;
    const usernameInput = usernames[0];
    const tenants = inputs.filter((element) => {
      const placeholder = element.getAttribute("placeholder") || "";
      return element.type === "text" && element.id === "tenant_code" &&
        element.getAttribute("name") === "tenant_code" &&
        element.autocomplete === "" && placeholder.includes("企业") &&
        placeholder.includes("标识");
    });
    if (tenants.length !== 1) return null;
    const tenant = tenants[0];
    const allowedAuxiliaryTypes = new Set(["checkbox", "radio", "hidden", "submit", "button"]);
    if (inputs.some((element) => element !== usernameInput && element !== password &&
        element !== tenant &&
        !allowedAuxiliaryTypes.has(element.type))) {
      return null;
    }
    const buttons = Array.from(form.querySelectorAll("button, input[type=submit]"))
      .filter(visible);
    const selected = buttons.filter((element) => {
      const text = (element.innerText || element.value || "")
        .replace(/\\s+/g, "").toLowerCase();
      const classes = (typeof element.className === "string" ? element.className : "")
        .split(/\\s+/);
      return element.type === "submit" && classes.includes("login_submit") &&
        (text === "登录" || text === "signin" || text === "login") &&
        !/reset|change|forgot|recover|retrieve|register|sign[_ -]?up|重置|修改|找回|注册/.test(
          [metadata(element), text].join(" ")
        );
    });
    return selected.length === 1 ?
      {form, username: usernameInput, tenant, password, button: selected[0]} : null;
  };

  if (hardChallengePresent()) return "challenge";
  const match = matchLoginForm();
  if (!match) {
    return alternativeLoginPresent(document) ? "challenge" : "rejected";
  }
  if (alternativeLoginPresent(match.form)) return "challenge";
  const usernameInput = match.username;
  const tenant = match.tenant;
  const password = match.password;

  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  // Establish the fixed enterprise identifier before either credential is
  // written.  React may rerender on this first controlled-input change, so
  // wait and revalidate the exact form and node identities first.
  setter.call(tenant, "zhicheng");
  tenant.dispatchEvent(new Event("input", {bubbles: true}));
  tenant.dispatchEvent(new Event("change", {bubbles: true}));
  tenant.dispatchEvent(new Event("blur", {bubbles: true}));
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));

  if (!pageAllowed()) return "rejected";
  if (hardChallengePresent()) return "challenge";
  const tenantConfirmed = matchLoginForm();
  if (!tenantConfirmed || tenantConfirmed.form !== match.form ||
      tenantConfirmed.username !== usernameInput ||
      tenantConfirmed.tenant !== tenant || tenantConfirmed.password !== password ||
      tenantConfirmed.button !== match.button || !tenant.isConnected ||
      tenant.value !== "zhicheng") {
    return "rejected";
  }

  setter.call(usernameInput, username);
  usernameInput.dispatchEvent(new Event("input", {bubbles: true}));
  usernameInput.dispatchEvent(new Event("change", {bubbles: true}));
  setter.call(password, passwordValue);
  password.dispatchEvent(new Event("input", {bubbles: true}));
  password.dispatchEvent(new Event("change", {bubbles: true}));
  usernameInput.dispatchEvent(new Event("blur", {bubbles: true}));
  password.dispatchEvent(new Event("blur", {bubbles: true}));
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));

  if (!pageAllowed()) return "rejected";
  if (hardChallengePresent()) return "challenge";
  const confirmed = matchLoginForm();
  if (!confirmed) {
    return alternativeLoginPresent(document) ? "challenge" : "rejected";
  }
  if (alternativeLoginPresent(confirmed.form)) return "challenge";
  if (confirmed.form !== match.form ||
      confirmed.username !== usernameInput || confirmed.password !== password ||
      confirmed.tenant !== tenant || tenant.value !== "zhicheng" ||
      confirmed.button !== match.button || !usernameInput.isConnected ||
      !tenant.isConnected || !password.isConnected || !match.button.isConnected ||
      match.button.disabled) {
    return "rejected";
  }
  const Submit = typeof SubmitEvent === "function" ? SubmitEvent : Event;
  const submitEvent = new Submit("submit", {
    bubbles: true, cancelable: true, submitter: match.button
  });
  // React/Ant Design's hydrated handler must cancel the native default.  If
  // it does not, do not invoke click()/requestSubmit(): either could serialize
  // credentials into a default GET URL or an unexpected native endpoint.
  return match.form.dispatchEvent(submitEvent) === false ? "submitted" : "rejected";
}"""


class BrowserFetchTransport:
    """Run allow-listed SenseCore HTTP requests in an authenticated Chrome.

    ``start`` attaches a flattened CDP session, enables Network capture, and
    navigates its page to the fixed, parameter-free SenseCore enterprise login
    URL.  Transports that need a service page can opt into the guarded
    :meth:`navigate_console` transition; CCI instead captures a successful
    read-only IAM request after login and calls its API directly.  The default
    mode creates a separate minimized automation window in the connected Chrome.  A dedicated headless
    automation Chrome can instead pass ``reuse_existing_page=True``; that mode
    reuses an idle default-profile page when possible and otherwise creates a
    minimal headless-safe page.  Tests and embedders can inject an already-
    created ``connection``.
    """

    def __init__(
        self,
        cdp_port: int,
        console_url: str,
        api_base: str = CCI_API_BASE,
        *,
        allowed_request_prefixes: Optional[Sequence[str]] = None,
        host: str = "127.0.0.1",
        profile_dir: Optional[Path] = None,
        connection: Optional[Any] = None,
        connection_factory: Optional[Callable[[str], Any]] = None,
        endpoint_resolver: Optional[Callable[..., DevToolsEndpoint]] = None,
        auth: Optional[CCIAuthorization] = None,
        auth_capture_base: Optional[str] = None,
        auth_capture_exact_path: bool = False,
        auth_capture_methods: Optional[Sequence[str]] = None,
        auth_requires_console_navigation: bool = True,
        discovery_timeout: float = 10.0,
        auth_timeout: float = 60.0,
        reuse_existing_page: bool = False,
    ) -> None:
        self.cdp_port = int(cdp_port)
        self.console_url = str(console_url)
        self.api_base = api_base.rstrip("/")
        prefixes = list(allowed_request_prefixes or (self.api_base, MANAGEMENT_API_BASE))
        if self.api_base not in prefixes:
            prefixes.insert(0, self.api_base)
        self.allowed_request_prefixes = tuple(prefix.rstrip("/") for prefix in prefixes)
        self.host = host
        self.profile_dir = Path(profile_dir).expanduser() if profile_dir else None
        self.auth_capture_base = (
            str(auth_capture_base).rstrip("/")
            if auth_capture_base is not None
            else self.api_base
        )
        self.auth_capture_exact_path = bool(auth_capture_exact_path)
        self.auth_capture_methods = (
            None
            if auth_capture_methods is None
            else tuple(str(method).upper() for method in auth_capture_methods)
        )
        self.auth_requires_console_navigation = bool(
            auth_requires_console_navigation
        )
        self.auth = auth or CCIAuthorization(
            self.auth_capture_base,
            exact_path=self.auth_capture_exact_path,
            allowed_methods=self.auth_capture_methods,
        )
        self._owns_auth = auth is None
        self._connection = connection
        self._owns_connection = connection is None
        self._connection_factory = connection_factory or CDPConnection.connect
        self._endpoint_resolver = endpoint_resolver or wait_for_devtools
        self._discovery_timeout = float(discovery_timeout)
        self._auth_timeout = float(auth_timeout)
        self.reuse_existing_page = bool(reuse_existing_page)
        self._target_id: Optional[str] = None
        self._target_owned = False
        self._session_id: Optional[str] = None
        self._execution_context_id: Optional[int] = None
        self._started = False
        self._closed = False
        self._broken = threading.Event()
        self._login_required = threading.Event()
        self._listener_added = False
        self._lock = threading.RLock()
        # Listener callbacks run on CDPConnection's reader thread while
        # start() may be waiting for a command response under ``_lock``.  Login
        # provenance therefore has its own short-held lock and never performs
        # a CDP call while holding it.
        self._login_flow_lock = threading.Lock()
        self._login_phase = "idle"
        self._login_main_frame_id: Optional[str] = None
        self._login_request_id: Optional[str] = None
        self._login_loader_id: Optional[str] = None
        self._login_committed_loader_id: Optional[str] = None
        self._login_entry_request_seen = False
        self._login_challenge_url: Optional[str] = None
        self._login_oauth_url: Optional[str] = None
        self._login_iam_url: Optional[str] = None
        self._login_terminal_url: Optional[str] = None
        self._login_committed_url: Optional[str] = None
        self._login_intent_stage: Optional[str] = None
        self._login_intent_url: Optional[str] = None
        self._login_intent_scheduled = False
        self._login_intent_requested = False
        self._login_cancel_count = 0
        self._login_console_replacement_count = 0
        self._login_iam_hop_count = 0
        self._login_console_committed_at: Optional[float] = None
        self._login_flow_generation = 0
        self._login_console_navigation_active = False
        self._login_console_retry_used = False
        self._login_console_committed = threading.Event()
        self._login_exact_console_commit_seen = False
        self._login_failure_reason = ""

    def __repr__(self) -> str:
        state = "closed" if self._closed else ("started" if self._started else "new")
        return f"<BrowserFetchTransport {state}>"

    @property
    def broken(self) -> bool:
        """Whether this instance can no longer safely issue CDP commands.

        A transport intentionally stays single-use after its browser websocket
        or flattened target session disappears.  The watcher owns rebuilding a
        fresh transport (and therefore a fresh target/session) instead of
        accidentally retrying forever through a closed object.
        """
        if self._broken.is_set():
            return True
        connection = self._connection
        return bool(connection is not None and getattr(connection, "is_closed", False))

    @property
    def login_required(self) -> bool:
        """Whether the console needs an interactive login before automation."""
        return self._login_required.is_set()

    def _arm_login_flow(self, main_frame_id: str) -> None:
        with self._login_flow_lock:
            self._login_phase = "armed"
            self._login_main_frame_id = main_frame_id
            self._login_request_id = None
            self._login_loader_id = None
            self._login_committed_loader_id = None
            self._login_entry_request_seen = False
            self._login_challenge_url = None
            self._login_oauth_url = None
            self._login_iam_url = None
            self._login_terminal_url = None
            self._login_committed_url = None
            self._clear_login_intent_locked()
            self._login_cancel_count = 0
            self._login_console_replacement_count = 0
            self._login_iam_hop_count = 0
            self._login_console_committed_at = None
            self._login_flow_generation += 1
            self._login_console_navigation_active = False
            self._login_console_retry_used = False
            self._login_console_committed.clear()
            self._login_exact_console_commit_seen = False
            self._login_failure_reason = ""

    def _reset_login_flow(self) -> None:
        with self._login_flow_lock:
            self._login_phase = "idle"
            self._login_main_frame_id = None
            self._login_request_id = None
            self._login_loader_id = None
            self._login_committed_loader_id = None
            self._login_entry_request_seen = False
            self._login_challenge_url = None
            self._login_oauth_url = None
            self._login_iam_url = None
            self._login_terminal_url = None
            self._login_committed_url = None
            self._clear_login_intent_locked()
            self._login_cancel_count = 0
            self._login_console_replacement_count = 0
            self._login_iam_hop_count = 0
            self._login_console_committed_at = None
            self._login_flow_generation += 1
            self._login_console_navigation_active = False
            self._login_console_retry_used = False
            self._login_console_committed.clear()
            self._login_exact_console_commit_seen = False
            self._login_failure_reason = ""

    def _clear_login_intent_locked(self) -> None:
        self._login_intent_stage = None
        self._login_intent_url = None
        self._login_intent_scheduled = False
        self._login_intent_requested = False

    def _set_login_phase_locked(self, phase: str) -> None:
        if self._login_phase != phase:
            self._login_phase = phase
            self._login_flow_generation += 1

    def _fail_login_locked(self, reason: str) -> None:
        """Irreversibly revoke this target's login proof without echoing URLs."""
        self._set_login_phase_locked("unsafe")
        self._login_failure_reason = reason
        self._login_challenge_url = None
        self._login_oauth_url = None
        self._login_iam_url = None
        self._login_terminal_url = None
        self._clear_login_intent_locked()

    @property
    def login_diagnostic(self) -> str:
        """Return a token-free coarse login state for support diagnostics."""
        with self._login_flow_lock:
            return self._login_phase + (
                f":{self._login_failure_reason}"
                if self._login_failure_reason
                else ""
            )

    @property
    def cci_auth_diagnostic(self) -> Dict[str, Any]:
        """Return legacy-named, token-free facts about this auth capture."""
        with self._login_flow_lock:
            exact_commit = bool(self._login_exact_console_commit_seen)
        counts = self.auth.diagnostic_counts
        return {
            "exact_main_frame_commit": exact_commit,
            "owned_session_cci_requests": int(counts["exact_requests"]),
            "bearer_candidates": int(counts["bearer_requests"]),
            "effective_2xx": int(counts["effective_2xx"]),
        }

    def _trusted_login_challenge(self) -> str:
        with self._login_flow_lock:
            if self._login_phase not in {
                "challenge",
                "submitted",
                "terminal_pending",
                "ready",
            }:
                return ""
            return self._login_challenge_url or ""

    def _login_intent_candidate_locked(self, url: Any) -> Tuple[str, str]:
        """Classify a renderer navigation from the currently committed page."""
        phase = self._login_phase
        if (
            phase == "entry_committed"
            and self._login_entry_request_seen
            and self._login_committed_url == SENSECORE_LOGIN_URL
            and url == SENSECORE_CONSOLE_ROOT_URL
        ):
            return "console", SENSECORE_CONSOLE_ROOT_URL
        if (
            phase == "console_pending"
            and self._login_committed_url == SENSECORE_LOGIN_URL
            and url == SENSECORE_CONSOLE_ROOT_URL
            and self._login_console_replacement_count < 1
        ):
            # Chromium can issue the same renderer navigation a second time
            # before the first Document commits (for example after clearing
            # the page's scheduled timer).  Permit one exact-URL replacement;
            # the final frame commit must still match the replacement loader.
            return "console", SENSECORE_CONSOLE_ROOT_URL
        if phase == "console" and _is_console_login_terminal_url(
            self._login_committed_url
        ):
            landing_url = _canonical_console_landing_url(url)
            if (
                landing_url is not None
                and landing_url != self._login_committed_url
            ):
                return "landing", landing_url
            oauth_url = _canonical_oauth_authorization_url(url)
            if oauth_url is not None:
                return "oauth", oauth_url
        if (
            phase == "submitted"
            and self._login_challenge_url is not None
            and self._login_committed_url == self._login_challenge_url
        ):
            oauth_url = _canonical_oauth_authorization_url(url)
            if oauth_url is not None:
                return "submitted_oauth", oauth_url
        if (
            phase == "challenge"
            and self._login_challenge_url is not None
            and self._login_committed_url == self._login_challenge_url
        ):
            terminal_url = url if _is_console_login_terminal_url(url) else None
            if isinstance(terminal_url, str):
                return "terminal", terminal_url
        return "", ""

    def _observe_login_navigation_intent(
        self, method: str, params: Mapping[str, Any]
    ) -> None:
        """Bind a renderer-initiated main-frame navigation to its source page."""
        frame_id = params.get("frameId")
        if not isinstance(frame_id, str):
            return
        with self._login_flow_lock:
            if frame_id != self._login_main_frame_id:
                return
            if self._login_phase in {"idle", "armed", "unsafe"}:
                return

            if self._login_phase == "ready":
                if method == "Page.frameClearedScheduledNavigation":
                    return
                if self._login_console_navigation_active:
                    self._fail_login_locked("console_left_trusted_route")
                    return
                if not _is_console_login_terminal_url(
                    self._login_committed_url
                ):
                    self._fail_login_locked("renderer_intent_not_trusted")
                    return
                # A cold Console SPA can schedule OAuth exactly as the stable
                # landing grace expires.  Revoke the provisional ready state
                # atomically before classifying that renderer navigation.
                self._set_login_phase_locked("console")
                self._login_console_committed_at = time.monotonic()

            if method == "Page.frameClearedScheduledNavigation":
                # Once a Document request has been bound, Chrome may clear the
                # scheduled renderer timer even though that request is still
                # navigating.  Do not mistake this post-request notification
                # for cancellation or discard its request/loader proof.
                if self._login_phase in {"console_pending", "landing_pending"}:
                    if self._login_intent_stage is not None:
                        self._clear_login_intent_locked()
                        self._login_console_replacement_count += 1
                        self._login_flow_generation += 1
                    return
                pending_console = self._login_intent_stage == "console"
                if not pending_console:
                    if self._login_intent_stage is not None:
                        self._fail_login_locked("renderer_navigation_cancelled")
                    return
                if (
                    self._login_committed_url != SENSECORE_LOGIN_URL
                    or self._login_cancel_count >= 1
                ):
                    self._fail_login_locked("renderer_navigation_cancelled")
                    return
                self._login_cancel_count += 1
                self._clear_login_intent_locked()
                self._set_login_phase_locked("entry_committed")
                return

            url = params.get("url")
            reason = params.get("reason")
            if not isinstance(url, str) or reason != "scriptInitiated":
                self._fail_login_locked("renderer_intent_not_trusted")
                return
            stage, canonical_url = self._login_intent_candidate_locked(url)
            if not stage:
                self._fail_login_locked("renderer_intent_not_trusted")
                return

            if self._login_intent_stage is not None:
                if (
                    self._login_intent_stage != stage
                    or self._login_intent_url != canonical_url
                ):
                    self._fail_login_locked("renderer_intent_changed")
                    return
            else:
                self._login_intent_stage = stage
                self._login_intent_url = canonical_url

            if method == "Page.frameScheduledNavigation":
                self._login_intent_scheduled = True
                return
            if method != "Page.frameRequestedNavigation":
                return
            disposition = params.get("disposition")
            if disposition not in (None, "currentTab"):
                self._fail_login_locked("renderer_intent_not_trusted")
                return
            if not self._login_intent_scheduled:
                self._fail_login_locked("renderer_schedule_missing")
                return
            self._login_intent_requested = True

    def _observe_login_document_request(self, params: Mapping[str, Any]) -> None:
        """Advance login trust through a bound renderer navigation/redirect chain."""
        if params.get("type") != "Document":
            return
        frame_id = params.get("frameId")
        request_id = params.get("requestId")
        loader_id = params.get("loaderId")
        request = params.get("request")
        if not isinstance(request, Mapping):
            return
        url = request.get("url")
        method = request.get("method")
        if not all(
            isinstance(value, str) and value
            for value in (frame_id, request_id, loader_id, url)
        ):
            return

        with self._login_flow_lock:
            phase = self._login_phase
            if phase == "idle" or frame_id != self._login_main_frame_id:
                return

            redirect = params.get("redirectResponse")
            redirect_mapping = redirect if isinstance(redirect, Mapping) else None
            status = redirect_mapping.get("status") if redirect_mapping else None
            redirect_url = redirect_mapping.get("url") if redirect_mapping else None
            allowed_redirect = (
                isinstance(status, int)
                and not isinstance(status, bool)
                and status in _LOGIN_REDIRECT_STATUSES
            )
            if (
                url == SENSECORE_LOGIN_URL
                and method == "GET"
                and redirect_mapping is None
                and phase in {"armed", "entry", "entry_committed"}
            ):
                if self._login_entry_request_seen and (
                    self._login_request_id != request_id
                    or self._login_loader_id != loader_id
                ):
                    self._fail_login_locked("bootstrap_request_changed")
                    return
                self._login_entry_request_seen = True
                self._login_request_id = request_id
                self._login_loader_id = loader_id
                if phase != "entry_committed":
                    self._set_login_phase_locked("entry")
                return

            if phase == "armed":
                # The initial about:blank target can finish before the exact
                # Page.navigate request is observed.  It grants no trust, but
                # is not itself evidence of an attack.
                return

            if (
                self._login_intent_stage is not None
                and self._login_intent_scheduled
                and self._login_intent_requested
            ):
                stage = self._login_intent_stage
                intent_url = self._login_intent_url
                initiator = params.get("initiator")
                initiator_type = (
                    initiator.get("type") if isinstance(initiator, Mapping) else None
                )
                valid_renderer_request = (
                    url == intent_url
                    and method == "GET"
                    and redirect_mapping is None
                    and initiator_type == "script"
                    and request_id != self._login_request_id
                    and loader_id != self._login_loader_id
                )
                if not valid_renderer_request:
                    self._fail_login_locked("renderer_request_not_trusted")
                    return
                replacing_console_request = (
                    stage == "console" and phase == "console_pending"
                )
                self._login_request_id = request_id
                self._login_loader_id = loader_id
                self._clear_login_intent_locked()
                if stage == "console":
                    if replacing_console_request:
                        self._login_console_replacement_count += 1
                        self._login_flow_generation += 1
                    self._set_login_phase_locked("console_pending")
                elif stage == "landing":
                    self._login_terminal_url = intent_url
                    self._set_login_phase_locked("landing_pending")
                elif stage == "oauth":
                    self._login_oauth_url = intent_url
                    self._login_iam_hop_count = 0
                    self._set_login_phase_locked("oauth")
                elif stage == "submitted_oauth":
                    self._login_oauth_url = intent_url
                    self._login_iam_hop_count = 0
                    self._set_login_phase_locked("submitted_oauth")
                elif stage == "terminal":
                    self._login_terminal_url = intent_url
                    self._set_login_phase_locked("terminal_pending")
                else:  # pragma: no cover - private state invariant
                    self._fail_login_locked("renderer_request_not_trusted")
                return

            if phase == "entry":
                challenge_url = _canonical_login_challenge_url(url)
                terminal = _is_console_login_terminal_url(url)
                direct_redirect = (
                    self._login_entry_request_seen
                    and request_id == self._login_request_id
                    and loader_id == self._login_loader_id
                    and redirect_url == SENSECORE_LOGIN_URL
                    and allowed_redirect
                    and method == "GET"
                )
                if direct_redirect and challenge_url is not None:
                    self._login_challenge_url = challenge_url
                    self._set_login_phase_locked("challenge_pending")
                    return
                if direct_redirect and terminal:
                    # A persistent SSO session may skip the password challenge.
                    self._login_terminal_url = url
                    self._set_login_phase_locked("terminal_pending")
                    return
                if challenge_url is None and not terminal:
                    reason = "redirect_destination_not_allowed"
                elif redirect_mapping is None:
                    reason = "redirect_proof_missing"
                elif request_id != self._login_request_id:
                    reason = "redirect_request_changed"
                elif loader_id != self._login_loader_id:
                    reason = "redirect_loader_changed"
                elif redirect_url != SENSECORE_LOGIN_URL:
                    reason = "redirect_source_changed"
                elif not allowed_redirect:
                    reason = "redirect_status_not_allowed"
                elif method != "GET":
                    reason = "redirect_method_not_allowed"
                else:
                    reason = "redirect_not_trusted"
                self._fail_login_locked(reason)
                return

            if phase in {"oauth", "submitted_oauth"}:
                iam_url = _canonical_iam_authorization_url(url)
                valid_iam_redirect = (
                    iam_url is not None
                    and self._login_iam_hop_count < _LOGIN_MAX_IAM_HOPS
                    and request_id == self._login_request_id
                    and loader_id == self._login_loader_id
                    and redirect_url == self._login_oauth_url
                    and status == 302
                    and method == "GET"
                )
                if valid_iam_redirect:
                    self._login_iam_url = iam_url
                    self._login_iam_hop_count += 1
                    self._set_login_phase_locked(
                        "iam" if phase == "oauth" else "submitted_iam"
                    )
                    return
                terminal_url = _canonical_console_login_terminal_url(url)
                valid_terminal_redirect = (
                    terminal_url is not None
                    and self._login_iam_hop_count >= 1
                    and request_id == self._login_request_id
                    and loader_id == self._login_loader_id
                    and redirect_url == self._login_oauth_url
                    and status == 303
                    and method == "GET"
                )
                if valid_terminal_redirect:
                    self._login_terminal_url = terminal_url
                    self._set_login_phase_locked("terminal_pending")
                    return
                self._fail_login_locked("oauth_redirect_not_trusted")
                return

            if phase in {"iam", "submitted_iam"}:
                challenge_url = _canonical_login_challenge_url(url)
                valid_challenge_redirect = (
                    phase == "iam"
                    and challenge_url is not None
                    and request_id == self._login_request_id
                    and loader_id == self._login_loader_id
                    and redirect_url == self._login_iam_url
                    and status == 302
                    and method == "GET"
                )
                if valid_challenge_redirect:
                    self._login_challenge_url = challenge_url
                    self._set_login_phase_locked("challenge_pending")
                    return
                oauth_url = _canonical_oauth_authorization_url(url)
                valid_oauth_return = (
                    oauth_url is not None
                    and request_id == self._login_request_id
                    and loader_id == self._login_loader_id
                    and redirect_url == self._login_iam_url
                    and status == 302
                    and method == "GET"
                )
                if valid_oauth_return:
                    self._login_oauth_url = oauth_url
                    self._set_login_phase_locked(
                        "oauth" if phase == "iam" else "submitted_oauth"
                    )
                    return
                self._fail_login_locked("iam_redirect_not_trusted")
                return

            if phase == "challenge":
                if (
                    url == self._login_challenge_url
                    and method == "GET"
                    and redirect_mapping is None
                ):
                    self._login_request_id = request_id
                    self._login_loader_id = loader_id
                    self._set_login_phase_locked("challenge_pending")
                    return
                terminal = _is_console_login_terminal_url(url)
                direct_terminal = (
                    terminal
                    and redirect_url == self._login_challenge_url
                    and allowed_redirect
                    and method == "GET"
                )
                if direct_terminal:
                    self._login_request_id = request_id
                    self._login_loader_id = loader_id
                    self._login_terminal_url = url
                    self._set_login_phase_locked("terminal_pending")
                    return
                self._fail_login_locked("challenge_left_trusted_route")
                return

            if phase == "submitted":
                self._fail_login_locked("challenge_left_trusted_route")
                return

            if phase in {
                "console_pending",
                "landing_pending",
                "challenge_pending",
                "terminal_pending",
            }:
                if (
                    request_id == self._login_request_id
                    and loader_id == self._login_loader_id
                    and url
                    in {
                        SENSECORE_CONSOLE_ROOT_URL,
                        self._login_challenge_url,
                        self._login_terminal_url,
                    }
                ):
                    return
                self._fail_login_locked("pending_navigation_changed")
                return

            if phase == "ready":
                if self._login_console_navigation_active:
                    if url == self.console_url and method == "GET":
                        self._login_request_id = request_id
                        self._login_loader_id = loader_id
                        return
                    self._fail_login_locked("console_left_trusted_route")
                    return
                self._fail_login_locked("console_left_trusted_route")
                return

            self._fail_login_locked("document_route_not_trusted")

    def _observe_login_frame(self, params: Mapping[str, Any]) -> None:
        frame = params.get("frame")
        if not isinstance(frame, Mapping) or frame.get("parentId") not in (None, ""):
            return
        frame_id = frame.get("id")
        url = frame.get("url")
        loader_id = frame.get("loaderId")
        if not all(
            isinstance(value, str) and value
            for value in (frame_id, url, loader_id)
        ):
            return
        with self._login_flow_lock:
            if frame_id != self._login_main_frame_id or self._login_phase == "idle":
                return
            phase = self._login_phase
            expected_url: Optional[str] = None
            next_phase: Optional[str] = None
            if phase in {"armed", "entry"} and url == SENSECORE_LOGIN_URL:
                if (
                    self._login_entry_request_seen
                    and self._login_loader_id != loader_id
                ):
                    self._fail_login_locked("entry_commit_not_trusted")
                    return
                expected_url = SENSECORE_LOGIN_URL
                next_phase = "entry_committed"
            elif phase == "entry_committed" and (
                url == SENSECORE_LOGIN_URL
                and loader_id == self._login_committed_loader_id
            ):
                return
            elif phase == "console_pending":
                expected_url = SENSECORE_CONSOLE_ROOT_URL
                next_phase = "console"
            elif phase == "console" and (
                url == self._login_committed_url
                and _is_console_login_terminal_url(url)
                and loader_id == self._login_committed_loader_id
            ):
                return
            elif phase == "landing_pending":
                expected_url = self._login_terminal_url
                next_phase = "console"
            elif phase == "challenge_pending":
                expected_url = self._login_challenge_url
                next_phase = "challenge"
            elif phase in {"challenge", "submitted"} and (
                url == self._login_challenge_url
                and loader_id == self._login_committed_loader_id
            ):
                return
            elif phase == "terminal_pending":
                expected_url = self._login_terminal_url
                next_phase = "console"
            elif (
                phase == "ready"
                and self._login_console_navigation_active
                and url == self.console_url
                and self._login_loader_id is not None
            ):
                expected_url = self.console_url
                next_phase = "ready"
            elif phase == "armed" and url == "about:blank":
                return
            else:
                self._fail_login_locked("frame_commit_not_trusted")
                return

            if expected_url != url or (
                self._login_loader_id is not None
                and self._login_loader_id != loader_id
            ):
                self._fail_login_locked("frame_commit_not_trusted")
                return
            self._login_committed_url = url
            self._login_committed_loader_id = loader_id
            assert next_phase is not None
            self._set_login_phase_locked(next_phase)
            if next_phase == "console":
                self._login_console_committed_at = time.monotonic()
            elif next_phase == "ready" and url == self.console_url:
                self._login_exact_console_commit_seen = True
                self._login_console_committed.set()

    def _observe_login_same_document_navigation(
        self, params: Mapping[str, Any]
    ) -> None:
        """Track only a strict Console landing History API transition."""
        frame_id = params.get("frameId")
        url = params.get("url")
        navigation_type = params.get("navigationType")
        if not isinstance(frame_id, str) or not isinstance(url, str):
            return
        with self._login_flow_lock:
            if frame_id != self._login_main_frame_id:
                return
            phase = self._login_phase
            # The IAM page adds the one empty ``IAM`` flag through history
            # state.  Its exact DOM validator remains responsible for that
            # pinned challenge variant; never overwrite the canonical source
            # URL used by the OAuth redirect proof.
            if phase in {"challenge", "submitted"}:
                return
            if phase not in {"console", "ready"}:
                return
            if self._login_console_navigation_active:
                self._fail_login_locked("console_left_trusted_route")
                return
            landing_url = _canonical_console_landing_url(url)
            if (
                navigation_type != "historyApi"
                or landing_url is None
                or not _is_console_login_terminal_url(
                    self._login_committed_url
                )
            ):
                self._fail_login_locked("console_left_trusted_route")
                return
            if self._login_intent_stage is not None:
                if (
                    self._login_intent_stage != "landing"
                    or self._login_intent_url != landing_url
                    or not self._login_intent_scheduled
                    or not self._login_intent_requested
                ):
                    self._fail_login_locked("renderer_intent_changed")
                    return
                self._clear_login_intent_locked()
            self._login_committed_url = landing_url
            self._login_console_committed_at = time.monotonic()
            if phase == "ready":
                self._set_login_phase_locked("console")

    def _verified_login_dom_state(self, state: str) -> str:
        with self._login_flow_lock:
            phase = self._login_phase
            if phase == "unsafe":
                return "untrusted"
            if phase in {"submitted_oauth", "submitted_iam"} and state in {
                "untrusted",
                "password_form",
            }:
                return "loading"
            if (
                state == "departed"
                and phase == "console"
                and _is_console_login_terminal_url(self._login_committed_url)
            ):
                committed_at = self._login_console_committed_at
                if (
                    self._login_intent_stage is None
                    and committed_at is not None
                    and time.monotonic() - committed_at
                    >= _LOGIN_CONSOLE_ROOT_GRACE_SECONDS
                ):
                    # A stable, proven Console landing is only an SSO
                    # candidate.  The deliberately long quiet window lets a
                    # cold SPA begin its OAuth/IAM route before we consider
                    # navigating away from it.
                    # The caller still requires a successful configured auth
                    # capture before it may issue an API request.  CCI uses the
                    # read-only IAM identity request already made by this page.
                    self._set_login_phase_locked("ready")
                    return "departed"
                return "loading"
            if state == "departed" and phase != "ready":
                return "untrusted"
            if state == "password_form" and phase not in {"challenge", "submitted"}:
                return "untrusted"
            return state

    def _feed_cdp_event(
        self, method: str, params: dict, session_id: Optional[str]
    ) -> None:
        """Feed auth capture and notice loss of our private target/session.

        This callback runs on CDPConnection's reader thread.  It deliberately
        does not acquire ``self._lock``: start() can be holding that lock while
        waiting for the same reader to deliver a command response.
        """
        owned_session = self._session_id
        try:
            # Network events from any other page in the browser must never be
            # allowed to authorize this transport.  Browser-level Target events
            # remain visible below so target/session loss is still detected.
            if owned_session is not None and session_id == owned_session:
                if method == "Network.requestWillBeSent":
                    self._observe_login_document_request(params)
                elif method == "Page.frameNavigated":
                    self._observe_login_frame(params)
                elif method == "Page.navigatedWithinDocument":
                    self._observe_login_same_document_navigation(params)
                elif method in {
                    "Page.frameRequestedNavigation",
                    "Page.frameScheduledNavigation",
                    "Page.frameClearedScheduledNavigation",
                }:
                    self._observe_login_navigation_intent(method, params)
                if method in {
                    "Page.frameNavigated",
                    "Runtime.executionContextDestroyed",
                    "Runtime.executionContextsCleared",
                }:
                    # A navigation invalidates isolated-world ids.  Login page
                    # discovery is allowed to recover by creating a fresh one;
                    # browser-side CCI fetches will do the same on their next
                    # request instead of using a stale context.
                    self._execution_context_id = None
                self.auth.feed_event(method, params, session_id)
        finally:
            target_id = self._target_id
            detached_session = params.get("sessionId")
            destroyed_target = params.get("targetId")
            if (
                method == "Target.detachedFromTarget"
                and owned_session is not None
                and detached_session == owned_session
            ) or (
                method in {"Target.targetDestroyed", "Target.targetCrashed"}
                and target_id is not None
                and destroyed_target == target_id
            ) or (
                method == "Inspector.detached"
                and owned_session is not None
                and session_id == owned_session
            ):
                self._broken.set()

    @staticmethod
    def _minimize_target_window(connection: Any, target_id: str) -> None:
        """Best-effort fallback for Chrome versions without create-time state."""
        try:
            result = connection.call(
                "Browser.getWindowForTarget", {"targetId": target_id}
            )
            window_id = result.get("windowId")
            if not isinstance(window_id, int) or isinstance(window_id, bool):
                return
            connection.call(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {"windowState": "minimized"},
                },
            )
        except Exception:
            # Creating an independent background window is still preferable to
            # placing the automation page in the user's working window.  Older
            # platforms may not implement Browser window bounds at all.
            pass

    def _create_automation_target(self, connection: Any) -> str:
        """Create the least-visible isolated target supported by this Chrome."""
        # Target.createTarget has evolved across Chrome releases.  Only retry
        # definite protocol rejections: a timeout has an ambiguous outcome and
        # retrying it could leave multiple untracked automation windows behind.
        attempts = (
            (
                {
                    "url": "about:blank",
                    "newWindow": True,
                    "background": True,
                    "windowState": "minimized",
                },
                False,
            ),
            (
                {
                    "url": "about:blank",
                    "newWindow": True,
                    "windowState": "minimized",
                },
                False,
            ),
            (
                {
                    "url": "about:blank",
                    "newWindow": True,
                    "background": True,
                },
                True,
            ),
            ({"url": "about:blank", "newWindow": True}, True),
            ({"url": "about:blank", "background": True}, False),
        )
        last_error: Optional[CDPError] = None
        for params, minimize_after_create in attempts:
            try:
                created = connection.call("Target.createTarget", params)
            except CDPTimeout:
                raise
            except CDPError as exc:
                last_error = exc
                if bool(getattr(connection, "is_closed", False)):
                    raise
                continue
            target_id = created.get("targetId")
            if not isinstance(target_id, str) or not target_id:
                # A successful command with no id may still have created a
                # target.  Do not issue another create and risk an orphan.
                raise BrowserFetchError(
                    "Chrome did not return an automation target id"
                )
            if minimize_after_create:
                self._minimize_target_window(connection, target_id)
            return target_id
        raise BrowserFetchError(
            "Chrome could not create an automation target"
        ) from last_error

    def _existing_or_headless_target(self, connection: Any) -> Tuple[str, bool]:
        """Reuse one idle default-context page, or create a headless-safe page."""
        result = connection.call("Target.getTargets")
        target_infos = result.get("targetInfos")
        if not isinstance(target_infos, list):
            raise BrowserFetchError("Chrome returned an invalid target list")

        candidates: List[Tuple[int, str]] = []
        for index, info in enumerate(target_infos):
            if not isinstance(info, dict) or info.get("type") != "page":
                continue
            target_id = info.get("targetId")
            if not isinstance(target_id, str) or not target_id:
                continue
            # Incognito/other explicit browser contexts do not share the
            # persistent default profile's SSO.  Attached pages may belong to a
            # concurrent transport and must not be navigated out from under it.
            if info.get("browserContextId") not in (None, ""):
                continue
            if bool(info.get("attached")) or info.get("subtype") not in (None, ""):
                continue
            # A non-blank target may already be in flight between the
            # enterprise entry and its shared login challenge.  Attaching
            # after Network.enable would then observe only the destination
            # request and could never prove where it came from.  Reuse only a
            # stable blank target; otherwise create a fresh blank target.
            if info.get("url") == "about:blank":
                candidates.append((index, target_id))

        if candidates:
            candidates.sort()
            return candidates[0][1], False

        # Do not pass newWindow/background/windowState in the dedicated
        # headless process.  This exact minimal form is supported by old and new
        # headless CDP implementations alike.
        created = connection.call("Target.createTarget", {"url": "about:blank"})
        target_id = created.get("targetId")
        if not isinstance(target_id, str) or not target_id:
            raise BrowserFetchError("Chrome did not return a headless page target id")
        return target_id, True

    def start(self, chrome: Any = None) -> "BrowserFetchTransport":
        with self._lock:
            if self._closed:
                raise BrowserFetchError("browser transport is closed")
            if self._started:
                return self
            try:
                if self._connection is None:
                    endpoint = self._endpoint_resolver(
                        self.cdp_port,
                        chrome,
                        self._discovery_timeout,
                        host=self.host,
                        profile_dir=self.profile_dir,
                    )
                    self._connection = self._connection_factory(endpoint.browser_ws_url)
                connection = self._connection
                connection.add_listener(self._feed_cdp_event)
                self._listener_added = True
                if self.reuse_existing_page:
                    target_id, self._target_owned = (
                        self._existing_or_headless_target(connection)
                    )
                else:
                    target_id = self._create_automation_target(connection)
                    self._target_owned = True
                self._target_id = target_id
                attached = connection.call(
                    "Target.attachToTarget",
                    {"targetId": target_id, "flatten": True},
                )
                session_id = attached.get("sessionId")
                if not isinstance(session_id, str) or not session_id:
                    raise BrowserFetchError("Chrome did not attach the automation target")
                self._session_id = session_id
                for domain in ("Network.enable", "Runtime.enable", "Page.enable"):
                    connection.call(domain, {}, session_id=session_id)
                tree = connection.call(
                    "Page.getFrameTree",
                    {},
                    session_id=session_id,
                    timeout=self._discovery_timeout,
                )
                try:
                    main_frame_id = tree["frameTree"]["frame"]["id"]
                except (KeyError, TypeError) as exc:
                    raise BrowserFetchError(
                        "Chrome did not return the login target main frame"
                    ) from exc
                if not isinstance(main_frame_id, str) or not main_frame_id:
                    raise BrowserFetchError(
                        "Chrome did not return the login target main frame"
                    )
                # Arm before Page.navigate: Network.requestWillBeSent can be
                # delivered on the reader thread before the command response.
                self._arm_login_flow(main_frame_id)
                login_navigation = connection.call(
                    "Page.navigate",
                    {"url": SENSECORE_LOGIN_URL},
                    session_id=session_id,
                    timeout=self._discovery_timeout,
                )
                if (
                    isinstance(login_navigation, dict)
                    and login_navigation.get("errorText")
                ):
                    raise BrowserFetchError(
                        "SenseCore enterprise login navigation was rejected by Chrome"
                    )
                with self._login_flow_lock:
                    if self._login_phase == "armed":
                        # The exact Page.navigate call itself is authoritative
                        # for the parameter-free enterprise page.  A later
                        # shared-domain challenge still requires the Network
                        # redirect proof before it can receive credentials.
                        self._login_phase = "entry"
                self._started = True
                return self
            except Exception:
                self._cleanup_started_resources()
                raise

    def _ensure_started(self) -> None:
        if self.broken:
            raise BrowserFetchError("browser transport disconnected")
        if not self._started:
            self.start()

    def _navigate_console_page(self, *, retry: bool) -> "BrowserFetchTransport":
        """Navigate once, or perform the one bounded exact-GET bootstrap."""

        with self._lock:
            if self._closed:
                raise BrowserFetchError("browser transport is closed")
            if self.broken:
                raise BrowserFetchError("browser transport disconnected")
            if (
                not self._started
                or self._connection is None
                or self._session_id is None
                or self._target_id is None
            ):
                raise BrowserFetchError(
                    "browser transport must be started before console navigation"
                )
            with self._login_flow_lock:
                if self._login_phase != "ready":
                    raise BrowserFetchError(
                        "SenseCore login challenge is not complete"
                    )
                if retry:
                    if (
                        not self._login_console_navigation_active
                        or self._login_console_retry_used
                        or self._login_committed_url != self.console_url
                        or not self._login_console_committed.is_set()
                    ):
                        raise BrowserFetchError(
                            "SenseCore console authorization bootstrap is not safe"
                        )
                    self._login_console_retry_used = True
                else:
                    if self._login_console_navigation_active:
                        raise BrowserFetchError(
                            "SenseCore console navigation was already requested"
                        )
                    self._login_console_navigation_active = True
                    self._login_console_retry_used = False
                # Each fixed GET needs its own observed main-document request
                # and loader proof.  In particular, a duplicate frame event
                # from the first navigation cannot satisfy the retry commit.
                self._login_request_id = None
                self._login_loader_id = None
                self._login_console_committed.clear()
                navigation_generation = self._login_flow_generation

            connection = self._connection
            session_id = self._session_id
            target_id = self._target_id
            self._execution_context_id = None
            try:
                result = connection.call(
                    "Page.navigate",
                    {"url": self.console_url},
                    session_id=session_id,
                    timeout=self._discovery_timeout,
                )
                if isinstance(result, dict) and result.get("errorText"):
                    raise BrowserFetchError(
                        "SenseCore console navigation was rejected by Chrome"
                    )
            except Exception as exc:
                self._execution_context_id = None
                self._broken.set()
                if isinstance(exc, BrowserFetchError):
                    raise
                raise BrowserFetchError(
                    "could not navigate to SenseCore console"
                ) from exc

            if (
                self._session_id != session_id
                or self._target_id != target_id
                or self.broken
            ):
                self._execution_context_id = None
                self._broken.set()
                raise BrowserFetchError(
                    "browser session changed during console navigation"
                )
            with self._login_flow_lock:
                if (
                    self._login_phase != "ready"
                    or self._login_flow_generation != navigation_generation
                ):
                    self._execution_context_id = None
                    self._broken.set()
                    raise BrowserFetchError(
                        "SenseCore login proof changed during console navigation"
                    )
            self._execution_context_id = None
            return self

    def navigate_console(self) -> "BrowserFetchTransport":
        """Navigate the verified owned session to its fixed service page.

        This deliberately never starts or reattaches a transport.  The login
        checks and optional credential submission therefore occur in exactly
        the same flattened target session that later opens ``console_url``.
        Any navigation outcome invalidates the previous isolated world.
        """

        return self._navigate_console_page(retry=False)

    def wait_for_console_commit(self, timeout: float = 60.0) -> None:
        """Wait until the owned main frame commits the exact service-page GET."""
        if not self._login_console_committed.wait(max(0.0, float(timeout))):
            raise CDPTimeout("SenseCore console navigation did not finish")
        if self.broken:
            raise BrowserFetchError("browser transport disconnected")
        with self._login_flow_lock:
            if (
                self._login_phase != "ready"
                or not self._login_console_navigation_active
                or self._login_committed_url != self.console_url
            ):
                raise BrowserFetchError(
                    "SenseCore console navigation did not remain trusted"
                )

    def retry_console_navigation_for_auth(self) -> "BrowserFetchTransport":
        """Repeat the fixed console GET at most once after a proven commit.

        This is intentionally a new ``Page.navigate`` to the same exact URL,
        never ``Page.reload`` or XHR replay.  It cannot repeat a mutation and
        remains gated by the owned target's verified login proof.
        """

        return self._navigate_console_page(retry=True)

    def _isolated_context(self) -> int:
        if self._execution_context_id is not None:
            return self._execution_context_id
        if self._connection is None or self._session_id is None:
            raise BrowserFetchError("browser transport is not started")
        try:
            tree = self._connection.call(
                "Page.getFrameTree", {}, session_id=self._session_id
            )
            frame_id = tree["frameTree"]["frame"]["id"]
            if not isinstance(frame_id, str) or not frame_id:
                raise KeyError("frame")
            world = self._connection.call(
                "Page.createIsolatedWorld",
                {
                    "frameId": frame_id,
                    "worldName": "slaigpus-cci",
                    "grantUniveralAccess": False,
                },
                session_id=self._session_id,
            )
            context_id = world.get("executionContextId")
            if not isinstance(context_id, int):
                raise KeyError("executionContextId")
        except Exception as exc:
            self._broken.set()
            raise BrowserFetchError("could not create browser request context") from exc
        self._execution_context_id = context_id
        return context_id

    def _login_dom_call(
        self,
        function: str,
        arguments: Sequence[str] = (),
        *,
        timeout: float,
        navigation_is_transient: bool,
        required_login_generation: Optional[int] = None,
    ) -> str:
        """Run one redacted login-page operation in this transport's session."""
        command_failed = False
        raw: Dict[str, Any] = {}
        try:
            self._ensure_started()
            if self._connection is None or self._session_id is None:
                raise BrowserFetchError("browser transport is not started")
            context_id = self._isolated_context()
            if required_login_generation is not None:
                with self._login_flow_lock:
                    if (
                        self._login_flow_generation != required_login_generation
                        or self._login_phase == "unsafe"
                    ):
                        return "unknown"
            raw = self._connection.call(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": function,
                    "arguments": [{"value": value} for value in arguments],
                    "executionContextId": context_id,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                session_id=self._session_id,
                timeout=max(0.1, float(timeout)),
            )
        except Exception:  # noqa: BLE001 - params may contain credentials
            # Login navigation can destroy an isolated world between the DOM
            # check and the CDP response.  Never retain the failed context and
            # never retain the original exception: its traceback may reference
            # a CDP frame whose local params contain the credentials.
            self._execution_context_id = None
            command_failed = True
        if command_failed:
            return "loading" if navigation_is_transient else "unknown"
        if "exceptionDetails" in raw:
            self._execution_context_id = None
            return "loading" if navigation_is_transient else "unknown"
        try:
            value = raw["result"]["value"]
        except (KeyError, TypeError):
            return "ambiguous" if navigation_is_transient else "unknown"
        if not isinstance(value, str):
            return "ambiguous" if navigation_is_transient else "unknown"
        return value

    def inspect_login_page(self, *, timeout: float = 10.0) -> str:
        """Classify the proven SenseCore login flow without reading secrets.

        The canonical shared challenge is passed into the isolated page only
        after this transport observed the bounded Console/OAuth/IAM route from
        the exact enterprise bootstrap.  Only ``password_form`` authorizes the
        caller to retrieve credentials.  ``departed`` means the trusted flow
        reached the exact console landing page (or a stable SSO root); it is
        still only a candidate until the configured successful 2xx Bearer
        request is captured from this same browser session.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            trusted_challenge = self._trusted_login_challenge()
            state = self._login_dom_call(
                _LOGIN_INSPECT_FUNCTION,
                (trusted_challenge,),
                timeout=max(0.1, deadline - time.monotonic()),
                navigation_is_transient=True,
            )
            state = self._verified_login_dom_state(state)
            if state not in {"loading", "redirecting", "ambiguous", "untrusted"}:
                return state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return state
            time.sleep(min(0.25, remaining))

    def wait_for_login_completion(self, timeout: float) -> str:
        """Wait for the proven login challenge to reach the console origin.

        The transport must already own a flattened target session.  This
        method never starts a browser, navigates a page, or supplies CDP
        credentials; it repeats the strict classifier with only the pinned
        challenge URL.  Merely leaving either login page is never completion.
        """

        with self._lock:
            if self._closed:
                raise BrowserFetchError("browser transport is closed")
            if self.broken:
                raise BrowserFetchError("browser transport disconnected")
            if (
                not self._started
                or self._connection is None
                or self._session_id is None
                or self._target_id is None
            ):
                raise BrowserFetchError(
                    "browser transport must be started before waiting for login"
                )
            connection = self._connection
            session_id = self._session_id
            target_id = self._target_id

        deadline = time.monotonic() + max(0.0, float(timeout))
        polled_states = {"password_form", "loading", "ambiguous"}
        while True:
            with self._lock:
                if (
                    self._closed
                    or self.broken
                    or not self._started
                    or self._connection is not connection
                    or self._session_id != session_id
                    or self._target_id != target_id
                ):
                    raise BrowserFetchError(
                        "browser session changed while waiting for login"
                    )
            state = self._login_dom_call(
                _LOGIN_INSPECT_FUNCTION,
                (self._trusted_login_challenge(),),
                timeout=max(0.1, deadline - time.monotonic()),
                navigation_is_transient=True,
            )
            state = self._verified_login_dom_state(state)
            with self._lock:
                if (
                    self._closed
                    or self.broken
                    or not self._started
                    or self._connection is not connection
                    or self._session_id != session_id
                    or self._target_id != target_id
                ):
                    raise BrowserFetchError(
                        "browser session changed while waiting for login"
                    )
            if state in {"departed", "challenge", "untrusted"} or state not in (
                polled_states | {"redirecting"}
            ):
                return state
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return state
            time.sleep(min(0.25, remaining))

    def wait_for_login_departure(self, timeout: float) -> str:
        """Compatibility alias for :meth:`wait_for_login_completion`."""
        return self.wait_for_login_completion(timeout)

    def submit_login(self, username: str, password: str, *, timeout: float = 10.0) -> str:
        """Submit one strictly matched password form without logging values.

        The browser-side function atomically repeats the exact-origin and DOM
        checks before it writes either field.  ``unknown`` is an uncertainty
        boundary: callers must wait for authentication or fall back to a human,
        never blindly submit a second time.
        """
        if not isinstance(username, str) or not username.strip() or not isinstance(
            password, str
        ) or not password:
            return "rejected"
        if self.broken:
            return "unknown"
        with self._login_flow_lock:
            phase = self._login_phase
            trusted_challenge = self._login_challenge_url or ""
            flow_generation = self._login_flow_generation
        if phase != "challenge":
            return "rejected"
        result = self._login_dom_call(
            _LOGIN_SUBMIT_FUNCTION,
            (username, password, trusted_challenge),
            timeout=max(0.1, float(timeout)),
            navigation_is_transient=False,
            required_login_generation=flow_generation,
        )
        if result == "submitted":
            with self._login_flow_lock:
                if self._login_phase == "unsafe":
                    return "unknown"
                if self._login_flow_generation == flow_generation and (
                    self._login_phase == "challenge"
                ):
                    self._set_login_phase_locked("submitted")
                elif self._login_phase not in {
                    "submitted",
                    "submitted_oauth",
                    "submitted_iam",
                    "terminal_pending",
                    "ready",
                }:
                    return "unknown"
        return result if result in {"submitted", "challenge", "rejected", "unknown"} else "unknown"

    def _allowed_url(self, url: str) -> bool:
        return any(_url_matches_base(url, prefix) for prefix in self.allowed_request_prefixes)

    @staticmethod
    def _with_params(url: str, params: Any) -> str:
        if not params:
            return url
        parsed = urlsplit(url)
        encoded = urlencode(params, doseq=True)
        query = "&".join(part for part in (parsed.query, encoded) if part)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))

    def _fetch_once(
        self,
        request_data: Dict[str, Any],
        *,
        timeout: float,
    ) -> Tuple[int, str]:
        if self._connection is None or self._session_id is None:
            raise BrowserFetchError("browser transport is not started")
        context_id = self._isolated_context()
        try:
            raw = self._connection.call(
                "Runtime.callFunctionOn",
                {
                    "functionDeclaration": _FETCH_FUNCTION,
                    "arguments": [{"value": request_data}],
                    "executionContextId": context_id,
                    "awaitPromise": True,
                    "returnByValue": True,
                },
                session_id=self._session_id,
                timeout=timeout,
            )
        except Exception as exc:
            # Browser-side fetch failures arrive as exceptionDetails below.
            # An exception from the CDP command itself means the websocket or
            # flattened session is no longer trustworthy.
            self._broken.set()
            raise BrowserFetchError("browser request failed") from exc
        if "exceptionDetails" in raw:
            raise BrowserFetchError("browser request failed")
        try:
            value = raw["result"]["value"]
            status = int(value["status"])
            text = value["text"]
            if not isinstance(text, str) or status < 0 or status > 599:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise BrowserFetchError("browser returned an invalid response") from exc
        return status, text

    def wait_for_auth(self, timeout: Optional[float] = None) -> AuthLease:
        """Wait, reload once, and flag only a second authorization timeout.

        ``None`` uses this transport's configured authentication timeout.  CDP
        and browser-network failures propagate without being mislabeled as a
        login problem.
        """
        self._ensure_started()
        wait_timeout = self._auth_timeout if timeout is None else float(timeout)
        try:
            lease = self.auth.wait(timeout=wait_timeout)
        except CDPTimeout:
            try:
                lease = self.refresh_auth(timeout=wait_timeout)
            except CDPTimeout:
                self._login_required.set()
                raise
        self._login_required.clear()
        return lease

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        json_body: Any = None,
        body: Optional[str] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: float = 60.0,
    ) -> BrowserResponse:
        self._ensure_started()
        method = str(method).upper()
        if not method or not re.fullmatch(r"[A-Z]+", method):
            raise BrowserFetchError("invalid HTTP method")
        final_url = self._with_params(str(url), params)
        if not self._allowed_url(final_url):
            raise BrowserFetchError("browser request URL is outside the allow-list")
        if body is not None and json_body is not None:
            raise BrowserFetchError("pass body or json_body, not both")
        if json_body is not None:
            try:
                body = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise BrowserFetchError("request body is not JSON serializable") from exc

        request_headers: Dict[str, str] = {}
        for key, value in (headers or {}).items():
            lowered = str(key).strip().lower()
            if lowered in ("authorization", "cookie", "host"):
                raise BrowserFetchError(f"caller may not set {lowered} header")
            request_headers[str(key)] = str(value)
        if json_body is not None and not any(
            key.lower() == "content-type" for key in request_headers
        ):
            request_headers["Content-Type"] = "application/json"

        auth_wait = min(float(timeout), self._auth_timeout)
        lease = self.wait_for_auth(timeout=auth_wait)
        request_headers["Authorization"] = lease._header_value()
        if _url_matches_base(final_url, self.api_base):
            request_headers["x-ui-valid"] = "x-ui-valid"
        request_data = {
            "url": final_url,
            "method": method,
            "headers": request_headers,
            "body": body,
        }

        try:
            status, response_text = self._fetch_once(request_data, timeout=float(timeout))
        except BrowserFetchError:
            # Navigation replaces isolated worlds.  Retrying a GET is safe;
            # mutations must never be replayed implicitly.
            self._execution_context_id = None
            if method != "GET" or self.broken:
                raise
            status, response_text = self._fetch_once(request_data, timeout=float(timeout))
        response = BrowserResponse(status, response_text, lease.generation)
        if status == 401:
            self.auth.invalidate(lease.generation)
        return response

    def refresh_auth(self, timeout: Optional[float] = None) -> AuthLease:
        """Reload the current trusted page and wait for a newer auth capture."""
        self._ensure_started()
        if self._connection is None or self._session_id is None:
            raise BrowserFetchError("browser transport is not started")
        current = self.auth.current()
        generation = current.generation if current is not None else 0
        self._execution_context_id = None
        try:
            self._connection.call(
                "Page.reload", {"ignoreCache": False}, session_id=self._session_id
            )
        except Exception as exc:
            self._broken.set()
            raise BrowserFetchError("could not reload SenseCore console") from exc
        wait_timeout = self._auth_timeout if timeout is None else float(timeout)
        try:
            lease = self.auth.wait(
                after_generation=generation, timeout=wait_timeout
            )
        except CDPTimeout:
            self._login_required.set()
            raise
        self._login_required.clear()
        return lease

    def close_browser(self, timeout: float = 5.0) -> bool:
        """Ask the connected Chrome to flush its profile and exit gracefully.

        ``Browser.close`` can tear down the browser websocket before Chrome
        delivers the command response.  That expected disconnect counts as a
        successful shutdown request.  Other failures are reported only through
        the boolean result so cleanup callers can fall back to terminating the
        process without exposing protocol details.
        """
        with self._lock:
            connection = self._connection
            if connection is None or bool(getattr(connection, "is_closed", False)):
                return False
            try:
                connection.call("Browser.close", timeout=max(0.0, float(timeout)))
            except Exception:  # noqa: BLE001 - graceful close may drop CDP first
                return bool(getattr(connection, "is_closed", False))
            return True

    def _cleanup_started_resources(self) -> None:
        connection = self._connection
        if connection is not None and self._listener_added:
            try:
                connection.remove_listener(self._feed_cdp_event)
            except Exception:  # noqa: BLE001 - best effort
                pass
            self._listener_added = False
        if (
            connection is not None
            and self._session_id is not None
            and not self._target_owned
        ):
            try:
                connection.call(
                    "Target.detachFromTarget",
                    {"sessionId": self._session_id},
                    timeout=2.0,
                )
            except Exception:  # noqa: BLE001 - best effort teardown
                pass
        if (
            connection is not None
            and self._target_id is not None
            and self._target_owned
        ):
            try:
                connection.call(
                    "Target.closeTarget", {"targetId": self._target_id}, timeout=2.0
                )
            except Exception:  # noqa: BLE001 - best effort teardown
                pass
        self._target_id = None
        self._target_owned = False
        self._session_id = None
        self._execution_context_id = None
        self._started = False
        self._reset_login_flow()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._cleanup_started_resources()
            if self._owns_connection and self._connection is not None:
                try:
                    self._connection.close()
                except Exception:  # noqa: BLE001 - best effort teardown
                    pass
            self._connection = None
            if self._owns_auth:
                self.auth.close()
            self._closed = True

    def __enter__(self) -> "BrowserFetchTransport":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self.close()
        return False


__all__ = [
    "AuthLease",
    "BrowserFetchError",
    "BrowserFetchTransport",
    "BrowserResponse",
    "CCI_API_BASE",
    "CCI_API_ORIGIN",
    "CCI_API_PREFIX",
    "CCIAuthCapture",
    "CCIAuthorization",
    "CDPConnection",
    "CDPError",
    "CDPTimeout",
    "DevToolsEndpoint",
    "MANAGEMENT_API_BASE",
    "SENSECORE_CHALLENGE_ORIGIN",
    "SENSECORE_CONSOLE_ORIGIN",
    "SENSECORE_IAM_AUTH_CAPTURE_URL",
    "SENSECORE_LOGIN_ORIGIN",
    "SENSECORE_LOGIN_PATH",
    "SENSECORE_LOGIN_URL",
    "wait_for_devtools",
]
