"""Command line interface for slaigpus."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import signal
import shlex
import stat
import subprocess
import sys
import threading
import time
import warnings
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from .acp import (
    ACPClient,
    DEFAULT_PORTABLE_REPLICAS,
    TrainingJobPlan,
    normalize_worker_overrides,
    validate_acp_workspace,
)
from .acp_resources import (
    DEFAULT_RESOURCE_PROFILE_KEY,
    RESOURCE_PROFILES,
    RESOURCE_PROFILE_KEYS,
    ResourceProfile,
)
from .browser import ChromeError, find_chrome, launch_chrome
from .config import (
    CONFIG_SEARCH_PATH,
    DEFAULT_URL,
    Config,
    ConfigError,
    Site,
    apply_overrides,
    config_path_for_write,
    default_site,
    is_default_sensecore_site,
    load_config,
    update_sensecore_config,
    validate_ssh_alias,
)
from .cci import (
    AutoRenewControlStore,
    CCIError,
    DEFAULT_CONSOLE_URL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_RENEW_AFTER,
    DEFAULT_WAIT_TIMEOUT,
    DEFAULT_WORKSPACE,
    RenewalSupervisor,
    SenseCoreClient,
    TargetResolver,
    WorkspaceRef,
    format_duration,
    parse_duration,
)
from .cdp import CDPError, CDPTimeout, wait_for_devtools
from .credentials import (
    CredentialStoreError,
    FileCredentialStore,
    SenseCoreCredentials,
)
from .dnat import (
    DNATClient,
    DNATError,
    DNATSpec,
    EIP_CONSOLE_URL,
    IAM_API_ORIGIN,
    MANAGEMENT_API_ORIGIN,
    NETWORK_API_ORIGIN,
)
from .monitor import (
    ACP_CONTAINER_NAME,
    ACP_HOST_IP,
    ACP_JOB_NAME,
    ACP_POD_NAME,
    MonitorClient,
    custom_filter,
    format_log_hit,
)
from .network import NetworkConnection
from .private_json import PrivateJSONError, load_private_json
from .tunnel import TunnelError, port_is_open


# --------------------------------------------------------------------- output

def _is_tty() -> bool:
    return sys.stderr.isatty()


def info(msg: str) -> None:
    prefix = "\033[36m·\033[0m" if _is_tty() else "·"
    print(f"{prefix} {msg}", file=sys.stderr)


def ok(msg: str) -> None:
    prefix = "\033[32m✓\033[0m" if _is_tty() else "+"
    print(f"{prefix} {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    prefix = "\033[33m!\033[0m" if _is_tty() else "!"
    print(f"{prefix} {msg}", file=sys.stderr)


def fail(msg: str) -> None:
    prefix = "\033[31m✗\033[0m" if _is_tty() else "x"
    print(f"{prefix} {msg}", file=sys.stderr)


# ------------------------------------------------------------------- helpers

def _resolve_site(args: argparse.Namespace, *, allow_builtin: bool = False) -> Site:
    """Merge config file + CLI flags into one Site, or build one from flags."""
    site_name = getattr(args, "site", None)
    explicit_config = getattr(args, "config", None) or os.environ.get("SLAIGPUS_CONFIG")
    ssh_host = getattr(args, "ssh_host", "") or ""
    direct = bool(getattr(args, "direct", False))
    explicit_url = getattr(args, "url", "") or ""

    # Bare SenseCore commands consume only the dedicated allowlisted network
    # table. An implicitly configured generic default can never redirect the
    # enterprise login URL, Chrome binary, or profile.
    if allow_builtin and not site_name and not explicit_url:
        config = load_config(getattr(args, "config", None))
        if explicit_config and config.sensecore is None and config.sites:
            site = config.get(None)
        else:
            site = config.sensecore or default_site()
    elif not site_name and not explicit_config and (explicit_url or ssh_host or direct):
        # Explicit ad-hoc targets must not inherit a generic default site's
        # browser flags, profile, SSH arguments, or URL.
        site = Site(name="adhoc", network_mode="direct")
    else:
        config: Config = load_config(getattr(args, "config", None))

        if config.sites:
            site = config.get(site_name)
        else:
            if not explicit_url and not ssh_host:
                raise ConfigError(
                    "nothing to connect to. Pass --url/--ssh-host, or write a config file at "
                    f"{CONFIG_SEARCH_PATH[0]}"
                )
            site = Site(name=site_name or "adhoc", network_mode="direct")

    site = apply_overrides(
        site,
        url=explicit_url,
        socks_port=getattr(args, "port", 0) or 0,
        chrome_binary=getattr(args, "chrome_binary", "") or "",
    )
    if direct:
        site = replace(
            site,
            network_mode="direct",
            ssh_host="",
            socks_port=0,
            ssh_args=[],
        )
    elif ssh_host:
        site = replace(site, network_mode="ssh", ssh_host=ssh_host, ssh_args=[])
    site.validate()
    if not site.uses_ssh and (
        int(getattr(args, "port", 0) or 0) or bool(getattr(args, "reuse", False))
    ):
        raise ConfigError("--port and --reuse require SSH mode")
    return site


def _resolve_sensecore_site(args: argparse.Namespace) -> Site:
    """Resolve only the built-in SenseCore identity plus its network route."""
    config = load_config(getattr(args, "config", None))
    site = config.sensecore or default_site()
    ssh_host = getattr(args, "ssh_host", "") or ""
    if bool(getattr(args, "direct", False)):
        site = replace(site, network_mode="direct", ssh_host="")
    elif ssh_host:
        site = replace(site, network_mode="ssh", ssh_host=ssh_host)
    site.validate()
    return site


def _probe_or_warn(tunnel: NetworkConnection, site: Site, strict: bool) -> bool:
    target = site.probe_target
    if not target:
        return True
    host, port = target
    try:
        tunnel.probe(host, port)
    except TunnelError as exc:
        if strict:
            fail(str(exc))
            return False
        warn(f"reachability check failed: {exc}")
        return True
    if site.uses_ssh:
        ok(f"{host}:{port} is reachable through SSH Host {site.ssh_host}")
    else:
        ok(f"{host}:{port} is directly reachable")
    return True


def _start_tunnel(site: Site, reuse: bool = False) -> NetworkConnection:
    if site.uses_ssh:
        info(f"opening SOCKS tunnel via SSH Host {site.ssh_host} …")
    else:
        info("using a direct network connection")
    tunnel = NetworkConnection(
        mode=site.mode,
        ssh_host=site.ssh_host,
        port=site.socks_port,
        ssh_args=site.ssh_args,
        reuse_existing=reuse,
    )
    tunnel.start()
    if site.uses_ssh:
        ok(f"tunnel up on {tunnel.socks_url}")
    else:
        ok("direct connection selected")
    return tunnel


# --------------------------------------------------------------- CCI helpers

@dataclass
class _CCIOptions:
    workspace: str
    app: str
    instance: str
    container: str
    namespace: str
    renew_after: float
    poll_interval: float
    wait_timeout: float


def _cci_options(args: argparse.Namespace) -> _CCIOptions:
    renew_after = parse_duration(
        getattr(args, "renew_after", "") or DEFAULT_RENEW_AFTER,
        label="renew-after",
    )
    if renew_after >= 4 * 3600:
        raise CCIError("renew-after must be less than 4h")
    return _CCIOptions(
        workspace=getattr(args, "cci_workspace", "") or DEFAULT_WORKSPACE,
        app=getattr(args, "cci_app", "") or "",
        instance=getattr(args, "cci_instance", "") or "",
        container=getattr(args, "cci_container", "") or "",
        namespace=getattr(args, "cci_namespace", "") or "",
        renew_after=renew_after,
        poll_interval=parse_duration(
            getattr(args, "poll_interval", "") or DEFAULT_POLL_INTERVAL,
            label="poll-interval",
        ),
        wait_timeout=parse_duration(
            getattr(args, "wait_timeout", "") or DEFAULT_WAIT_TIMEOUT,
            label="wait-timeout",
        ),
    )


def _remote_hostname_hint(site: Site) -> str:
    """Best-effort read-only hint for matching an SSH alias to a CCI app."""
    if not site.uses_ssh:
        return ""
    ssh_binary = os.environ.get("SLAIGPUS_SSH") or "ssh"
    command = [
        ssh_binary,
        "-o", "ControlMaster=no",
        "-o", "ControlPath=none",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        *site.ssh_args,
        site.ssh_host,
        "hostname",
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip().splitlines()[0] if completed.returncode == 0 and completed.stdout.strip() else ""


def _make_supervisor(
    transport: Any,
    site: Site,
    options: _CCIOptions,
    *,
    report: Callable[[str], None] = info,
    include_remote_hint: bool = True,
) -> RenewalSupervisor:
    client = SenseCoreClient(transport, options.workspace)
    hints = [site.ssh_host] if site.uses_ssh else []
    if include_remote_hint and site.uses_ssh:
        remote = _remote_hostname_hint(site)
        if remote:
            hints.append(remote)
    resolver = TargetResolver(
        client,
        app=options.app,
        instance=options.instance,
        container=options.container,
        namespace=options.namespace,
        hints=hints,
    )
    return RenewalSupervisor(
        client,
        resolver,
        renew_after=options.renew_after,
        poll_interval=options.poll_interval,
        wait_timeout=options.wait_timeout,
        report=report,
    )


def _make_browser_transport(
    cdp_port: int,
    options: _CCIOptions,
    *,
    profile_dir: Optional[Path] = None,
    reuse_existing_page: bool = False,
) -> Any:
    try:
        from .cdp import BrowserFetchTransport, SENSECORE_IAM_AUTH_CAPTURE_URL
    except ModuleNotFoundError as exc:
        raise CCIError(
            "CCI automation requires websocket-client; reinstall with `pip install -e .`"
        ) from exc
    workspace = WorkspaceRef.parse(options.workspace)
    return BrowserFetchTransport(
        cdp_port=cdp_port,
        console_url=DEFAULT_CONSOLE_URL,
        api_base=workspace.api_base,
        allowed_request_prefixes=[
            workspace.api_base,
            workspace.management_base,
            workspace.ccr_base,
            workspace.network_base,
        ],
        auth_capture_base=SENSECORE_IAM_AUTH_CAPTURE_URL,
        auth_capture_exact_path=True,
        auth_capture_methods=("GET",),
        auth_requires_console_navigation=False,
        profile_dir=profile_dir,
        reuse_existing_page=reuse_existing_page,
        # The enterprise entry is reached through the selected network path and can
        # legitimately take more than the generic CDP ten-second default.
        discovery_timeout=30.0,
    )


def _make_login_transport(
    cdp_port: int,
    *,
    profile_dir: Path,
) -> Any:
    """Attach only the strict SenseCore login state machine to work Chrome.

    This transport never navigates to the CCI app and is never exposed to a
    supervisor.  Its target starts at ``about:blank`` so the CDP session can
    observe the complete enterprise-entry redirect chain before credentials
    are considered.
    """
    try:
        from .cdp import BrowserFetchTransport, SENSECORE_CONSOLE_ROOT_URL
    except ModuleNotFoundError as exc:
        raise CDPError(
            "SenseCore automatic login requires websocket-client; reinstall slaigpus"
        ) from exc
    return BrowserFetchTransport(
        cdp_port=cdp_port,
        console_url=SENSECORE_CONSOLE_ROOT_URL,
        profile_dir=profile_dir,
        reuse_existing_page=True,
        # The enterprise entry is reached through the selected network path and can
        # legitimately take more than the generic CDP ten-second default.
        discovery_timeout=30.0,
    )


def _make_acp_transport(
    cdp_port: int,
    *,
    profile_dir: Optional[Path] = None,
    reuse_existing_page: bool = False,
) -> Any:
    """Create an authenticated browser transport for ACP and Monitor APIs."""
    try:
        from .acp import (
            AEC2_ORIGIN,
            CCR_ORIGIN,
            DEFAULT_ACP_CONSOLE_URL,
            MONITOR_ORIGIN,
        )
        from .cdp import BrowserFetchTransport, SENSECORE_IAM_AUTH_CAPTURE_URL
    except ModuleNotFoundError as exc:
        raise CCIError(
            "ACP automation requires websocket-client; reinstall slaigpus"
        ) from exc
    return BrowserFetchTransport(
        cdp_port=cdp_port,
        console_url=DEFAULT_ACP_CONSOLE_URL,
        # Capture the successful read-only IAM identity request emitted by the
        # proven Console login, then use that Bearer for the allowlisted ACP,
        # workspace, CCR, and Monitor APIs.  Planning no longer depends on the
        # ACP micro-frontend rendering and making its own AEC2 request first.
        api_base=AEC2_ORIGIN,
        allowed_request_prefixes=(AEC2_ORIGIN, CCR_ORIGIN, MONITOR_ORIGIN),
        auth_capture_base=SENSECORE_IAM_AUTH_CAPTURE_URL,
        auth_capture_exact_path=True,
        auth_capture_methods=("GET",),
        auth_requires_console_navigation=False,
        profile_dir=profile_dir,
        reuse_existing_page=reuse_existing_page,
        discovery_timeout=30.0,
    )


def _make_dnat_transport(cdp_port: int) -> Any:
    """Attach the account-routed DNAT client to an existing work viewer."""
    try:
        from .cdp import BrowserFetchTransport, SENSECORE_IAM_AUTH_CAPTURE_URL
    except ModuleNotFoundError as exc:
        raise CCIError(
            "DNAT automation requires websocket-client; reinstall slaigpus"
        ) from exc
    return BrowserFetchTransport(
        cdp_port=cdp_port,
        console_url=EIP_CONSOLE_URL,
        api_base=NETWORK_API_ORIGIN,
        allowed_request_prefixes=(
            NETWORK_API_ORIGIN,
            MANAGEMENT_API_ORIGIN,
            IAM_API_ORIGIN,
        ),
        auth_capture_base=SENSECORE_IAM_AUTH_CAPTURE_URL,
        auth_capture_exact_path=True,
        auth_capture_methods=("GET",),
        auth_requires_console_navigation=False,
        discovery_timeout=30.0,
    )


_AUTOMATION_LOGIN_MARKER = ".slaigpus-login-ready"


def _automation_login_marker(profile_dir: Path) -> Path:
    """Return the credential-free hint used to try automation headless first."""
    return Path(profile_dir) / _AUTOMATION_LOGIN_MARKER


def _automation_login_was_completed(profile_dir: Path) -> bool:
    try:
        mode = _automation_login_marker(profile_dir).lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o600


def _remember_automation_login(profile_dir: Path) -> None:
    """Persist only a 0600 empty marker; Chrome keeps the actual login state."""
    profile = Path(profile_dir)
    profile.mkdir(parents=True, exist_ok=True)
    path = _automation_login_marker(profile)
    try:
        existing_mode = path.lstat().st_mode
    except FileNotFoundError:
        existing_mode = None
    if existing_mode is not None and not stat.S_ISREG(existing_mode):
        raise CCIError(f"CCI automation login marker is not a regular file: {path}")
    flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


_TRUSTED_AUTOMATIC_LOGIN_CHROME = Path(
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
)

_TRUSTED_LINUX_AUTOMATIC_LOGIN_CHROME = (
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/google-chrome-stable"),
    Path("/opt/google/chrome/chrome"),
)
_AUTOMATIC_LOGIN_BINARY_UNSET = object()


def _strict_resolve_path(path: Path) -> Path:
    """Small filesystem boundary kept separate for deterministic tests."""
    return path.resolve(strict=True)


def _path_details(path: Path) -> os.stat_result:
    return path.stat()


def _path_is_executable(path: Path) -> bool:
    return os.access(path, os.X_OK)


def _root_controls_directory_chain(path: Path) -> bool:
    """Require root-owned, non-writable directories from ``/`` to ``path``."""
    if not path.is_absolute():
        return False
    current = Path(path.anchor)
    components = (current,) + tuple(
        current.joinpath(*path.parts[1 : index + 1])
        for index in range(1, len(path.parts))
    )
    try:
        for component in components:
            details = _path_details(component)
            if (
                not stat.S_ISDIR(details.st_mode)
                or details.st_uid != 0
                or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                return False
    except OSError:
        return False
    return True


def _trusted_linux_chrome_candidate(candidate: Path) -> Optional[Path]:
    """Return one immutable system browser target, or reject this alias."""
    try:
        resolved = _strict_resolve_path(candidate)
        details = _path_details(resolved)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_absolute():
        return None
    # Check both the distribution alias and its resolved target.  A safe final
    # file is insufficient when an unprivileged account can replace one of the
    # directories used to reach it between validation and Popen().
    if not _root_controls_directory_chain(candidate.parent):
        return None
    if not _root_controls_directory_chain(resolved.parent):
        return None
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != 0
        or details.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not _path_is_executable(resolved)
    ):
        return None
    return resolved


def _site_allows_automatic_login(site: Site) -> bool:
    """Limit credential injection to the exact built-in, hardened workflow."""
    if not is_default_sensecore_site(site):
        return False
    # A deny-list cannot cover Chrome's many diagnostics flags (for example a
    # sensitive net-log) or a replacement executable that impersonates CDP.
    # Custom browsers/flags remain supported for manual login, but stored
    # credentials are only available to the uncustomized built-in workflow.
    return not (
        site.chrome_args
        or site.chrome_binary
        or os.environ.get("SLAIGPUS_CHROME")
    )


def _trusted_automatic_login_chrome(site: Site) -> Optional[str]:
    """Resolve a fixed system Chrome path allowed to receive secrets.

    User-application and PATH fallbacks are intentionally excluded.  Each
    path component below ``/Applications`` must also be a real entry rather
    than a symlink.  Failure simply disables automatic credential injection;
    the same browser workflow then falls back to visible manual login.
    """
    if not _site_allows_automatic_login(site):
        return None
    if sys.platform.startswith("linux"):
        # Do not let general PATH discovery choose one unsafe early hit and
        # mask a later, fixed system installation.  Every allowed alias is
        # independently checked and unsafe entries are skipped.
        for candidate in _TRUSTED_LINUX_AUTOMATIC_LOGIN_CHROME:
            trusted = _trusted_linux_chrome_candidate(candidate)
            if trusted is not None:
                return str(trusted)
        return None
    if sys.platform != "darwin":
        return None
    try:
        executable = Path(find_chrome()).expanduser()
    except ChromeError:
        return None
    trusted = _TRUSTED_AUTOMATIC_LOGIN_CHROME
    if executable != trusted:
        return None
    try:
        current = Path("/Applications")
        for component in trusted.relative_to(current).parts:
            current = current / component
            if current.is_symlink():
                return None
        if not trusted.is_file() or not os.access(trusted, os.X_OK):
            return None
    except (OSError, ValueError):
        return None
    return str(trusted)


def _automatic_credential_store(
    args: argparse.Namespace,
    site: Site,
    automatic_login_binary: Optional[str],
) -> Any:
    """Select a credential backend only for the hardened built-in workflow."""
    credentials_file = getattr(args, "credentials_file", None)
    if automatic_login_binary is None:
        if credentials_file is not None:
            raise CredentialStoreError(
                "file credentials require the built-in SenseCore site and a trusted system Chrome"
            )
        return None
    if credentials_file is not None:
        return FileCredentialStore(credentials_file)
    return FileCredentialStore()


def _automation_chrome_args(site: Site) -> List[str]:
    """Disable profile extensions whenever credentials may enter the page."""
    arguments = list(site.chrome_args)
    if _site_allows_automatic_login(site) and not any(
        str(value).split("=", 1)[0] == "--disable-extensions"
        for value in arguments
    ):
        arguments.append("--disable-extensions")
    return arguments


_SERVICE_CONSOLE_COMMIT_TIMEOUT = 60.0
_SERVICE_AUTH_CAPTURE_TIMEOUT = 90.0
_SERVICE_AUTH_RETRY_TIMEOUT = 90.0
_LOGIN_PAGE_INSPECT_TIMEOUT = 30.0
_LOGIN_COMPLETION_TIMEOUT = 45.0


def _auth_requires_console_navigation(transport: Any) -> bool:
    """Default unknown transports to the historical fail-closed navigation."""
    try:
        return getattr(transport, "auth_requires_console_navigation", True) is not False
    except Exception:
        return True


def _capture_auth_after_login(
    transport: Any,
    *,
    timeout: float = _SERVICE_AUTH_CAPTURE_TIMEOUT,
) -> Any:
    """Use a successful owned-session IAM Bearer after login is proven."""
    auth = getattr(transport, "auth", None)
    current = getattr(auth, "current", None)
    wait_for_auth = getattr(auth, "wait", None)
    if not callable(current) or not callable(wait_for_auth):
        raise CCIError(
            "SenseCore automation browser cannot capture login authorization"
        )
    lease = current()
    if lease is not None:
        return lease
    return wait_for_auth(after_generation=0, timeout=float(timeout))


def _capture_auth_after_console_navigation(
    transport: Any,
    *,
    timeout: float = _SERVICE_AUTH_CAPTURE_TIMEOUT,
    commit_timeout: float = _SERVICE_CONSOLE_COMMIT_TIMEOUT,
    retry_timeout: float = _SERVICE_AUTH_RETRY_TIMEOUT,
) -> Any:
    """Open the service only after login, then capture owned-session auth.

    This deliberately calls ``CCIAuthorization.wait`` directly.  The normal
    transport recovery helper may reload a page; a login transition must
    never be retried or replayed implicitly.  A cold service SPA receives a
    longer wait beginning after its exact main-frame GET commits.  If it still
    produces no Bearer, one transport-gated navigation to that same fixed GET
    URL is permitted; no reload, XHR replay, or write request is used.
    """
    auth = getattr(transport, "auth", None)
    current = getattr(auth, "current", None)
    wait_for_new_auth = getattr(auth, "wait", None)
    navigate_console = getattr(transport, "navigate_console", None)
    wait_for_console_commit = getattr(transport, "wait_for_console_commit", None)
    retry_console = getattr(
        transport,
        "retry_console_navigation_for_auth",
        None,
    )
    if not all(
        callable(value)
        for value in (current, wait_for_new_auth, navigate_console)
    ):
        raise CCIError(
            "SenseCore automation browser cannot perform the verified login-to-console transition"
        )
    previous = current()
    raw_generation = getattr(previous, "generation", 0)
    generation = (
        raw_generation
        if isinstance(raw_generation, int)
        and not isinstance(raw_generation, bool)
        and raw_generation >= 0
        else 0
    )

    def fresh_current() -> Any:
        lease = current()
        if lease is None:
            return None
        if previous is None:
            return lease
        lease_generation = getattr(lease, "generation", None)
        if (
            isinstance(lease_generation, int)
            and not isinstance(lease_generation, bool)
            and lease_generation > generation
        ):
            return lease
        return None

    navigate_console()
    if callable(wait_for_console_commit):
        wait_for_console_commit(timeout=float(commit_timeout))
    lease = fresh_current()
    if lease is not None:
        return lease
    try:
        wait_for_new_auth(after_generation=generation, timeout=float(timeout))
    except CDPTimeout:
        # Eliminate the boundary race before requesting the one safe exact-GET
        # bootstrap.  Older test doubles without that transport primitive keep
        # the historical fail-closed behavior.
        lease = fresh_current()
        if lease is not None:
            return lease
        if not callable(retry_console):
            raise
        retry_console()
        if callable(wait_for_console_commit):
            wait_for_console_commit(timeout=float(commit_timeout))
        lease = fresh_current()
        if lease is not None:
            return lease
        wait_for_new_auth(
            after_generation=generation,
            timeout=float(retry_timeout),
        )
    lease = fresh_current()
    if lease is None:
        raise CDPTimeout(
            "SenseCore console did not produce a Bearer for CCI management after login"
        )
    return lease


def _safe_login_diagnostic(transport: Any) -> str:
    """Return only known token-free transport diagnostics."""
    value = getattr(transport, "login_diagnostic", "")
    allowed = {
        "idle",
        "armed",
        "entry",
        "entry_committed",
        "console_pending",
        "landing_pending",
        "console",
        "oauth",
        "iam",
        "challenge_pending",
        "challenge",
        "submitted",
        "submitted_oauth",
        "submitted_iam",
        "terminal_pending",
        "ready",
        "unsafe:bootstrap_request_changed",
        "unsafe:redirect_destination_not_allowed",
        "unsafe:redirect_proof_missing",
        "unsafe:redirect_request_changed",
        "unsafe:redirect_loader_changed",
        "unsafe:redirect_source_changed",
        "unsafe:redirect_status_not_allowed",
        "unsafe:redirect_method_not_allowed",
        "unsafe:redirect_not_trusted",
        "unsafe:renderer_intent_not_trusted",
        "unsafe:renderer_navigation_cancelled",
        "unsafe:renderer_intent_changed",
        "unsafe:renderer_schedule_missing",
        "unsafe:renderer_request_not_trusted",
        "unsafe:oauth_redirect_not_trusted",
        "unsafe:iam_redirect_not_trusted",
        "unsafe:challenge_left_trusted_route",
        "unsafe:pending_navigation_changed",
        "unsafe:entry_commit_not_trusted",
        "unsafe:frame_commit_not_trusted",
        "unsafe:console_left_trusted_route",
        "unsafe:document_route_not_trusted",
    }
    return value if isinstance(value, str) and value in allowed else "unavailable"


def _safe_cci_auth_diagnostic(transport: Any) -> str:
    """Format only fixed bool/int CCI counters; never stringify unknown data."""
    try:
        value = getattr(transport, "cci_auth_diagnostic", None)
    except Exception:
        return "unavailable"
    if not isinstance(value, Mapping):
        return "unavailable"
    try:
        commit = value["exact_main_frame_commit"]
        requests = value["owned_session_cci_requests"]
        bearer = value["bearer_candidates"]
        successful = value["effective_2xx"]
    except Exception:
        return "unavailable"
    if type(commit) is not bool:
        return "unavailable"
    counters = (requests, bearer, successful)
    if any(
        type(item) is not int or not 0 <= item <= 1_000_000_000
        for item in counters
    ):
        return "unavailable"
    return (
        f"exact_main_frame_commit={'yes' if commit else 'no'}, "
        f"owned_session_cci_requests={requests}, "
        f"bearer_candidates={bearer}, effective_2xx={successful}"
    )


def _attempt_sensecore_login_to_console(
    transport: Any,
    credential_store: Any,
    *,
    inspect_timeout: float = _LOGIN_PAGE_INSPECT_TIMEOUT,
    auth_timeout: float = _LOGIN_COMPLETION_TIMEOUT,
    session_ready: Optional[Callable[[], bool]] = None,
) -> str:
    """Reach the SenseCore Console through at most one strict IAM submission.

    The owned CDP target first visits the exact, parameter-free zhicheng login
    URL.  The shared signin challenge is eligible only when CDP proves the
    bounded Console/OAuth/IAM route from that bootstrap.  Page discovery happens
    before the secret is read, and the CDP layer atomically repeats the pinned
    IAM URL/form checks before writing ``tenant_code=zhicheng`` and the user
    credentials.  Reaching the exact Console landing page is the terminal state;
    this helper never opens the CCI app or waits for a CCI API Bearer.
    """
    if callable(session_ready) and session_ready():
        return "authenticated"
    inspect = getattr(transport, "inspect_login_page", None)
    submit = getattr(transport, "submit_login", None)
    wait_for_completion = getattr(transport, "wait_for_login_completion", None)
    if not callable(wait_for_completion):
        wait_for_completion = getattr(transport, "wait_for_login_departure", None)
    if not callable(inspect):
        return "unavailable"

    page_state = str(inspect(timeout=inspect_timeout))
    if callable(session_ready) and session_ready():
        return "authenticated"
    if page_state == "departed":
        return "authenticated"
    elif page_state != "password_form":
        return page_state
    else:
        if credential_store is None or not callable(submit) or not callable(
            wait_for_completion
        ):
            return "not_configured"
        try:
            credentials = credential_store.load()
        except CredentialStoreError:
            return "credential_error"
        if credentials is None:
            return "not_configured"
        if not isinstance(credentials, SenseCoreCredentials):
            return "credential_error"

        # Authentication can finish while the credential file is being read.
        # Recheck immediately before the one permitted credential submission.
        if callable(session_ready) and session_ready():
            del credentials
            return "authenticated"
        submit_failed = False
        submitted = ""
        try:
            submitted = str(
                submit(credentials.username, credentials.password, timeout=10.0)
            )
        except Exception:  # noqa: BLE001 - credential-bearing failure boundary
            submit_failed = True
        finally:
            # Python strings cannot be reliably wiped, but keeping the redacted
            # credential object scoped to this one call minimizes its lifetime.
            del credentials
        if submit_failed:
            return "submit_unknown"
        if submitted == "challenge":
            return "submit_challenge"
        if submitted == "rejected":
            return "submit_rejected"
        if submitted not in {"submitted", "unknown"}:
            return "submit_unknown"

        try:
            departure = str(wait_for_completion(timeout=auth_timeout))
        except Exception:  # noqa: BLE001 - submission outcome remains uncertain
            return "submit_unknown"
        if departure == "challenge":
            return "submit_challenge"
        if departure == "untrusted":
            return "session_unknown"
        if departure != "departed":
            return "failed" if submitted == "submitted" else "submit_unknown"

    return "authenticated"


def _attempt_automatic_login(
    transport: Any,
    credential_store: Any,
    profile_dir: Path,
    *,
    inspect_timeout: float = _LOGIN_PAGE_INSPECT_TIMEOUT,
    auth_timeout: float = _LOGIN_COMPLETION_TIMEOUT,
) -> str:
    """Log in through zhicheng, then capture the configured API credential.

    CCI transports capture the successful read-only IAM identity request made
    by the authenticated Console session and do not navigate to the CCI page.
    Older service transports may still explicitly require their fixed console
    page before they can produce a usable Bearer.
    """
    auth = getattr(transport, "auth", None)
    current = getattr(auth, "current", None)
    wait_for_new_auth = getattr(auth, "wait", None)
    if not all(callable(value) for value in (current, wait_for_new_auth)):
        return "unavailable"
    if _auth_requires_console_navigation(transport) and not callable(
        getattr(transport, "navigate_console", None)
    ):
        return "unavailable"
    result = _attempt_sensecore_login_to_console(
        transport,
        credential_store,
        inspect_timeout=inspect_timeout,
        auth_timeout=auth_timeout,
    )
    if result != "authenticated":
        return result

    try:
        if _auth_requires_console_navigation(transport):
            _capture_auth_after_console_navigation(
                transport,
                timeout=_SERVICE_AUTH_CAPTURE_TIMEOUT,
            )
        else:
            _capture_auth_after_login(
                transport,
                timeout=_SERVICE_AUTH_CAPTURE_TIMEOUT,
            )
    except CDPTimeout:
        return "session_failed"
    except Exception:  # noqa: BLE001 - no navigation or auth retry is safe here
        return "session_unknown"
    if current() is None:
        return "session_failed"
    _remember_automation_login(profile_dir)
    return "authenticated"


def _wait_for_captured_auth_without_navigation(
    transport: Any,
    *,
    chrome: Optional[subprocess.Popen] = None,
    stop_event: Optional[threading.Event] = None,
) -> Any:
    """Wait through the proven enterprise challenge, then capture API auth.

    For CCI this performs no service-page navigation: the successful IAM
    identity request from the authenticated Console session supplies the
    Bearer.  The conditional navigation branch exists only for older service
    transports that explicitly opt into it.
    """
    auth = getattr(transport, "auth", None)
    current = getattr(auth, "current", None)
    inspect = getattr(transport, "inspect_login_page", None)
    if not all(callable(value) for value in (current, inspect)):
        raise CCIError(
            "SenseCore automation browser cannot verify the zhicheng login-to-console transition"
        )
    if _auth_requires_console_navigation(transport) and not callable(
        getattr(transport, "navigate_console", None)
    ):
        raise CCIError(
            "SenseCore automation browser cannot verify the zhicheng login-to-console transition"
        )
    announced = False
    while True:
        if stop_event is not None and stop_event.is_set():
            raise CCIError("SenseCore automation browser stopped before login completed")
        if chrome is not None and chrome.poll() is not None:
            raise CCIError("SenseCore automation Chrome exited before login completed")
        if not announced:
            info(
                "waiting for SenseCore login through the enterprise entry "
                f"{DEFAULT_URL}"
            )
            announced = True
        try:
            page_state = str(inspect(timeout=1.0))
        except Exception as exc:
            raise CCIError(
                "could not verify the trusted SenseCore enterprise login flow"
            ) from exc
        if page_state == "departed":
            try:
                if _auth_requires_console_navigation(transport):
                    return _capture_auth_after_console_navigation(
                        transport,
                        timeout=_SERVICE_AUTH_CAPTURE_TIMEOUT,
                    )
                return _capture_auth_after_login(
                    transport,
                    timeout=_SERVICE_AUTH_CAPTURE_TIMEOUT,
                )
            except CDPTimeout:
                raise CCIError(
                    "SenseCore console authentication was not confirmed after the "
                    "zhicheng login page; retry to start again from the exact "
                    "enterprise login URL; CCI auth diagnostic: "
                    f"{_safe_cci_auth_diagnostic(transport)}"
                ) from None
        if page_state not in {
            "password_form",
            "loading",
            "ambiguous",
            "challenge",
            "redirecting",
        }:
            raise CCIError(
                "the SenseCore enterprise login flow left its trusted route"
            )
        if stop_event is not None:
            if stop_event.wait(0.25):
                raise CCIError(
                    "SenseCore automation browser stopped before login completed"
                )
        else:
            time.sleep(0.25)


class _CCIWatchWorker:
    """Own the SenseCore browser used to manage CCI debug containers.

    The user's work browser never exposes CDP merely because CCI watching is
    enabled.  This worker instead owns a second persistent Chrome profile.  A
    first login is deliberately visible in compatibility mode; a standalone
    controller can instead enforce headless-only operation and fail closed.
    """

    def __init__(
        self,
        site: Site,
        options: _CCIOptions,
        socks_port: int,
        *,
        credential_store: Any = None,
        headless_only: bool = False,
        automatic_login_binary: Any = _AUTOMATIC_LOGIN_BINARY_UNSET,
    ) -> None:
        self.cdp_port = 0
        self.site = site
        self.options = options
        self.socks_port = int(socks_port)
        self.headless_only = bool(headless_only)
        self.profile_dir = site.resolved_automation_profile_dir()
        self._automatic_login_binary = (
            _trusted_automatic_login_chrome(site)
            if automatic_login_binary is _AUTOMATIC_LOGIN_BINARY_UNSET
            else automatic_login_binary
        )
        self.credential_store = (
            credential_store
            if credential_store is not None
            else (
                FileCredentialStore()
                if self._automatic_login_binary is not None
                else None
            )
        )
        self._auto_login_submission_blocked = False
        self.chrome: Optional[subprocess.Popen] = None
        self.transport: Any = None
        self.stop_event = threading.Event()
        # Ready means that the automation Chrome and its CDP transport are connected. It
        # intentionally does not mean that an interactive login has finished.
        self.ready_event = threading.Event()
        self.finished_event = threading.Event()
        self.error: Optional[BaseException] = None
        self._resource_lock = threading.RLock()
        self._thread_started = False
        self._thread = threading.Thread(
            target=self._run, name="slaigpus-cci-watch", daemon=True
        )

    def start(self) -> None:
        self._thread.start()
        self._thread_started = True

    def _launch_automation_chrome(self, *, headless: bool) -> subprocess.Popen:
        launch_options = dict(
            socks_port=self.socks_port,
            profile_dir=self.profile_dir,
            # Start without a network navigation.  BrowserFetchTransport must
            # own the one and only navigation to the exact zhicheng enterprise
            # entry so it cannot attach halfway through an earlier redirect
            # and lose the redirect provenance required before credentials are
            # inspected or submitted.
            url="about:blank",
            cdp_port=0,
            enable_cdp=True,
            headless=headless,
            binary=self._automatic_login_binary or self.site.chrome_binary,
            block_local_dns=self.site.block_local_dns,
            extra_args=_automation_chrome_args(self.site),
            direct=not self.site.uses_ssh,
        )
        chrome = launch_chrome(**launch_options)
        with self._resource_lock:
            self.chrome = chrome
        return chrome

    def _claim_transport(self, expected: Any = None) -> Any:
        with self._resource_lock:
            if expected is not None and self.transport is not expected:
                return None
            transport, self.transport = self.transport, None
            return transport

    def _close_transport(self, expected: Any = None) -> None:
        transport = self._claim_transport(expected)
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    def _claim_chrome(
        self, expected: Optional[subprocess.Popen] = None
    ) -> Optional[subprocess.Popen]:
        with self._resource_lock:
            if expected is not None and self.chrome is not expected:
                return None
            chrome, self.chrome = self.chrome, None
            return chrome

    @staticmethod
    def _request_browser_close(transport: Any) -> bool:
        close_browser = getattr(transport, "close_browser", None)
        if not callable(close_browser):
            return False
        try:
            return bool(close_browser())
        except Exception:
            return False

    def _close_chrome(
        self,
        expected: Optional[subprocess.Popen] = None,
        *,
        graceful: bool = False,
    ) -> None:
        chrome = self._claim_chrome(expected)
        if chrome is None or chrome.poll() is not None:
            return
        if graceful:
            try:
                chrome.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:
                # Browser.close was accepted but the OS process did not leave;
                # fall back to the same bounded process cleanup used below.
                pass
        chrome.terminate()
        try:
            chrome.wait(timeout=10)
        except subprocess.TimeoutExpired:
            chrome.kill()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _finish_browser(self, transport: Any, chrome: subprocess.Popen) -> None:
        """Ask Chrome itself to flush/exit, then use the Popen safety net."""
        graceful = self._request_browser_close(transport)
        self._close_chrome(chrome, graceful=graceful)

    def _automation_chrome_error(self) -> CCIError:
        if not self.ready_event.is_set():
            return CCIError(
                "SenseCore automation Chrome exited before DevTools became ready; "
                f"close any Chrome already using {self.profile_dir} and retry"
            )
        return CCIError("SenseCore automation Chrome exited unexpectedly")

    def _try_automatic_login(self, transport: Any) -> str:
        """Permit at most one credential-bearing attempt per login episode."""
        if self.credential_store is None:
            return "disabled"
        if self._auto_login_submission_blocked:
            return "attempted"
        result = _attempt_automatic_login(
            transport,
            self.credential_store,
            self.profile_dir,
        )
        if result in {
            "authenticated",
            "failed",
            "session_failed",
            "session_unknown",
            "submit_challenge",
            "submit_rejected",
            "submit_unknown",
            "untrusted",
        }:
            # A bad password or uncertain click must not be resubmitted after
            # a timer, browser rebuild, or visible fallback.  Only a newly
            # captured authenticated session ends this login episode.
            self._auto_login_submission_blocked = result != "authenticated"
        return result

    def _wait_for_visible_login(self, transport: Any, chrome: subprocess.Popen) -> str:
        auto_result = self._try_automatic_login(transport)
        if auto_result == "authenticated":
            ok("SenseCore automatic login completed")
            return "authenticated"
        if auto_result in {"session_failed", "session_unknown", "untrusted"}:
            # The automatic path had already left the fixed enterprise page
            # but could not obtain usable API authentication.  Never invite a
            # human to enter credentials on whatever page is now displayed.
            # Closing this browser makes the next worker episode start again
            # from the exact, parameter-free zhicheng login URL.
            if auto_result == "untrusted":
                raise CCIError(
                    "SenseCore login left the trusted enterprise challenge flow "
                    f"({_safe_login_diagnostic(transport)})"
                )
            warn(
                "SenseCore console authentication was not confirmed; restarting "
                "from the exact enterprise login page; CCI auth diagnostic: "
                f"{_safe_cci_auth_diagnostic(transport)}"
            )
            return "login"
        if auto_result in {
            "failed",
            "submit_challenge",
            "submit_rejected",
            "submit_unknown",
            "credential_error",
        }:
            warn("automatic SenseCore login was not completed; manual login is required")
        info(
            "complete the SenseCore login in the automation Chrome; "
            "it will switch to headless mode automatically"
        )
        auth = getattr(transport, "auth", None)
        current = getattr(auth, "current", None)
        inspect = getattr(transport, "inspect_login_page", None)
        if not all(callable(value) for value in (current, inspect)):
            raise CCIError(
                "SenseCore automation browser cannot verify the zhicheng login-to-console transition"
            )
        if _auth_requires_console_navigation(transport) and not callable(
            getattr(transport, "navigate_console", None)
        ):
            raise CCIError(
                "SenseCore automation browser cannot verify the zhicheng login-to-console transition"
            )
        while not self.stop_event.is_set():
            if chrome.poll() is not None:
                return "restart"
            if bool(getattr(transport, "broken", False)):
                return "rebuild"
            try:
                page_state = str(inspect(timeout=1.0))
            except CDPError:
                if self.stop_event.is_set():
                    return "stop"
                if chrome.poll() is not None:
                    return "restart"
                if bool(getattr(transport, "broken", False)):
                    return "rebuild"
                raise
            if page_state == "departed":
                try:
                    if _auth_requires_console_navigation(transport):
                        _capture_auth_after_console_navigation(
                            transport,
                            timeout=_SERVICE_AUTH_CAPTURE_TIMEOUT,
                        )
                    else:
                        _capture_auth_after_login(
                            transport,
                            timeout=_SERVICE_AUTH_CAPTURE_TIMEOUT,
                        )
                except CDPTimeout:
                    # A redirect alone is not proof of an authenticated
                    # enterprise session.  Restarting the browser is the only
                    # safe interactive recovery because it re-establishes the
                    # exact zhicheng page before any further user input.
                    warn(
                        "SenseCore console authentication was not confirmed; "
                        "restarting from the exact enterprise login page; "
                        "CCI auth diagnostic: "
                        f"{_safe_cci_auth_diagnostic(transport)}"
                    )
                    return "login"
                except CDPError:
                    if self.stop_event.is_set():
                        return "stop"
                    if chrome.poll() is not None:
                        return "restart"
                    if bool(getattr(transport, "broken", False)):
                        return "rebuild"
                    raise
                if current() is None:
                    warn(
                        "SenseCore console authentication was not confirmed; "
                        "restarting from the exact enterprise login page; "
                        "CCI auth diagnostic: "
                        f"{_safe_cci_auth_diagnostic(transport)}"
                    )
                    return "login"
                _remember_automation_login(self.profile_dir)
                ok("SenseCore login captured; restarting the automation browser headless")
                return "authenticated"
            if page_state not in {
                "password_form",
                "challenge",
                "loading",
                "ambiguous",
                "redirecting",
            }:
                raise CCIError(
                    "the exact zhicheng enterprise login page could not be verified"
                )
            # Password forms and challenges are deliberately left untouched
            # for the human.  Polling only the page classification cannot
            # submit, reload, or navigate the page.
            if self.stop_event.wait(0.25):
                return "stop"
        return "stop"

    def _run_browser(self, *, headless: bool) -> str:
        chrome = self._launch_automation_chrome(headless=headless)
        try:
            # A browser-level websocket or automation target may disappear
            # while Chrome itself is healthy.  Rebuild only the single-use
            # transport in that case, preserving the browser/profile session.
            while not self.stop_event.is_set():
                transport = None
                started = False
                try:
                    transport = _make_browser_transport(
                        0,
                        self.options,
                        profile_dir=self.profile_dir,
                        reuse_existing_page=True,
                    )
                    with self._resource_lock:
                        self.transport = transport
                    transport.start(chrome)
                    started = True
                    self.ready_event.set()

                    if not headless:
                        outcome = self._wait_for_visible_login(transport, chrome)
                        self._finish_browser(transport, chrome)
                        return outcome

                    auto_result = self._try_automatic_login(transport)
                    if auto_result == "authenticated":
                        info("SenseCore automation Chrome login state is ready")
                    else:
                        # Headless mode must never enter the CCI supervisor
                        # until this exact CDP session has captured a valid
                        # Bearer request.  Challenge, ambiguous/loading pages,
                        # cooldown, missing credentials, and future unknown
                        # states all fail closed to the dedicated visible
                        # login window immediately.
                        if auto_result in {"session_failed", "session_unknown"}:
                            warn(
                                "CCI auth diagnostic: "
                                f"{_safe_cci_auth_diagnostic(transport)}"
                            )
                        if self.headless_only:
                            warn("SenseCore headless controller could not authenticate")
                        else:
                            warn(
                                "SenseCore requires interactive login; reopening the "
                                "SenseCore automation Chrome visibly"
                            )
                        self._finish_browser(transport, chrome)
                        return "login"

                    supervisor = _make_supervisor(transport, self.site, self.options)
                    supervisor.watch(stop_event=self.stop_event)
                    outcome = "stop" if self.stop_event.is_set() else "finished"
                    self._finish_browser(transport, chrome)
                    return outcome
                except BaseException as exc:
                    if self.stop_event.is_set():
                        return "stop"
                    if chrome.poll() is not None:
                        if not self.ready_event.is_set():
                            raise self._automation_chrome_error() from exc
                        return "restart"
                    if (
                        headless
                        and bool(getattr(transport, "login_required", False))
                    ):
                        # The active target has consumed this login episode. It
                        # cannot establish a fresh enterprise challenge
                        # provenance chain without rebuilding the target.
                        # End the episode so the next BrowserFetchTransport
                        # starts at the exact zhicheng URL before any automatic
                        # or interactive login handling.
                        if self.headless_only:
                            warn(
                                "SenseCore controller login expired; closing the "
                                "console session before authentication can be retried"
                            )
                        else:
                            warn(
                                "SenseCore login expired; reopening the CCI automation "
                                "Chrome at the exact enterprise login page"
                            )
                        self._finish_browser(transport, chrome)
                        return "login"
                    if started and bool(getattr(transport, "broken", False)):
                        info(
                            "SenseCore automation Chrome DevTools session disconnected; "
                            "CCI control is rebuilding it"
                        )
                        if self.stop_event.wait(1.0):
                            return "stop"
                        continue
                    if transport is None:
                        raise
                    if not started:
                        if bool(getattr(transport, "broken", False)):
                            info(
                                "SenseCore automation Chrome DevTools connection was interrupted; "
                                "CCI control is rebuilding it"
                            )
                            if self.stop_event.wait(1.0):
                                return "stop"
                            continue
                        if self.stop_event.wait(2.0):
                            return "stop"
                        info(
                            "SenseCore automation Chrome DevTools is not ready yet; "
                            "CCI control will retry"
                        )
                        continue
                    raise
                finally:
                    self._close_transport(transport)
            return "stop"
        finally:
            # Normal transitions already used Browser.close while the CDP
            # connection was alive.  These idempotent fallbacks cover setup
            # failures, crashes, and races with stop().
            self._close_transport()
            self._close_chrome(chrome)

    def _run(self) -> None:
        try:
            headless = self.headless_only or _automation_login_was_completed(
                self.profile_dir
            )
            if not headless and self.credential_store is not None:
                status = getattr(self.credential_store, "status", None)
                if callable(status):
                    try:
                        # Presence checks use only private-file metadata; they
                        # do not retrieve username/password.
                        # This lets a first configured run stay headless while
                        # preserving visible fallback for every failure.
                        headless = bool(status())
                    except CredentialStoreError:
                        headless = False
            while not self.stop_event.is_set():
                outcome = self._run_browser(headless=headless)
                if outcome == "authenticated":
                    headless = True
                    continue
                if outcome == "login":
                    if self.headless_only:
                        raise CCIError(
                            "headless controller could not log in; configure file "
                            "credentials or refresh its persistent automation profile"
                        )
                    headless = False
                    continue
                if outcome == "rebuild":
                    continue
                if outcome == "restart":
                    info(
                        "SenseCore automation Chrome exited; restarting it with the "
                        "same profile"
                    )
                    if self.stop_event.wait(1.0):
                        return
                    continue
                if outcome == "finished" and self.headless_only:
                    raise CCIError(
                        "headless CCI supervisor stopped unexpectedly"
                    )
                return
        except BaseException as exc:  # surfaced to the main terminal loop
            if not self.stop_event.is_set():
                self.error = exc
        finally:
            self._close_transport()
            self._close_chrome()
            self.finished_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        with self._resource_lock:
            transport = self.transport
            chrome = self.chrome
        # Browser.close gives Chrome a chance to flush the persistent SSO
        # profile.  transport.close then wakes any visible-login auth wait.
        graceful = self._request_browser_close(transport)
        self._close_chrome(chrome, graceful=graceful)
        self._close_transport(transport)
        if self._thread_started:
            self._thread.join(timeout=10)


def _use_cci_watch(args: argparse.Namespace, site: Site) -> bool:
    requested = getattr(args, "cci_watch", None)
    return is_default_sensecore_site(site) if requested is None else bool(requested)


# -------------------------------------------------------------- subcommands

def cmd_list(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if config.source:
        info(f"config: {config.source}")
    else:
        info("no config file found; searched:")
        for path in CONFIG_SEARCH_PATH:
            info(f"    {path}")

    sensecore = config.sensecore or default_site()
    if sensecore.uses_ssh:
        print(f"  sensecore  ssh via {sensecore.ssh_host:<20} {sensecore.url}")
    else:
        print(f"  sensecore  direct{'':<21} {sensecore.url}")

    if not config.sites:
        return 0

    width = max(len(n) for n in config.sites)
    for name, site in sorted(config.sites.items()):
        marker = "*" if name == config.default_site else " "
        route = f"ssh via {site.ssh_host}" if site.uses_ssh else "direct"
        print(f"{marker} {name:<{width}}  {route:<28} {site.url}")
    if config.default_site:
        info("* = default site")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    site = _resolve_site(args, allow_builtin=True)
    if not site.probe_target:
        fail("no URL configured for this site, so there is nothing to probe")
        return 2
    with _start_tunnel(site, reuse=args.reuse) as tunnel:
        return 0 if _probe_or_warn(tunnel, site, strict=True) else 1


def cmd_up(args: argparse.Namespace) -> int:
    """Hold a tunnel open and print how to use it. For manual/ad-hoc work."""
    site = _resolve_site(args, allow_builtin=True)
    if not site.uses_ssh:
        fail("`slaigpus up` requires SSH mode; direct mode has no tunnel to hold")
        return 2
    with _start_tunnel(site, reuse=args.reuse) as tunnel:
        if not args.no_probe:
            _probe_or_warn(tunnel, site, strict=False)
        print(tunnel.port)  # stdout stays machine-readable
        info("export it with:")
        info(f"    export ALL_PROXY={tunnel.socks_url_remote_dns}")
        info("press Ctrl-C to close the tunnel")
        try:
            while tunnel.is_running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print(file=sys.stderr)
        else:
            fail("ssh exited unexpectedly")
            if tunnel.log_tail:
                print(tunnel.log_tail, file=sys.stderr)
            return 1
    ok("tunnel closed")
    return 0


def _close_login_bootstrap(chrome: Any, transport: Any) -> None:
    """Flush and close the short-lived visible work-profile login bootstrap."""
    graceful = False
    if transport is not None:
        close_browser = getattr(transport, "close_browser", None)
        if callable(close_browser):
            try:
                graceful = bool(close_browser())
            except Exception:
                graceful = False

    if chrome is not None and chrome.poll() is None:
        if graceful:
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                graceful = False
        if not graceful and chrome.poll() is None:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
                try:
                    chrome.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass

    if transport is not None:
        try:
            transport.close()
        except Exception:
            pass


def _attempt_viewer_automatic_login(
    site: Site,
    socks_port: int,
    profile: Path,
    automatic_login_binary: str,
    credential_store: Any,
) -> str:
    """Populate only the work-profile Console session, then exit.

    The bootstrap is deliberately visible, short lived, and uses a random
    loopback CDP endpoint.  Closing the whole bootstrap browser before the
    normal work window starts removes that implicit DevTools endpoint and
    makes Chrome flush the persistent work profile first.
    """
    chrome: Any = None
    transport: Any = None
    browser_closed = False
    try:
        chrome = launch_chrome(
            socks_port=socks_port,
            profile_dir=profile,
            url="about:blank",
            cdp_port=0,
            enable_cdp=True,
            headless=False,
            binary=automatic_login_binary,
            block_local_dns=site.block_local_dns,
            extra_args=_automation_chrome_args(site),
            direct=not site.uses_ssh,
        )
        transport = _make_login_transport(0, profile_dir=profile)
        transport.start(chrome)
        return _attempt_sensecore_login_to_console(
            transport,
            credential_store,
        )
    except CDPError:
        browser_closed = bool(chrome is not None and chrome.poll() is not None)
        if browser_closed:
            return "browser_closed"
        return "unavailable"
    finally:
        _close_login_bootstrap(chrome, transport)


def cmd_open(args: argparse.Namespace) -> int:
    """Open the work browser and, optionally, an isolated CCI controller."""
    site = _resolve_site(args, allow_builtin=True)
    if not site.url and not args.url:
        warn("no URL given; Chrome will open on a blank page")
    cci_watch = _use_cci_watch(args, site)
    options = _cci_options(args) if cci_watch else None

    tunnel = _start_tunnel(site, reuse=args.reuse)
    chrome = None
    worker: Optional[_CCIWatchWorker] = None
    exit_code = 0
    try:
        if not args.no_probe:
            _probe_or_warn(tunnel, site, strict=False)

        profile = site.resolved_profile_dir()
        # Keep the explicit --cdp contract stable for scripts. The work profile's
        # optional login bootstrap uses a separate short-lived random endpoint;
        # the final work browser exposes DevTools only when --cdp was explicit.
        cdp_port = int(site.cdp_port) if args.cdp else 0
        enable_cdp = bool(args.cdp)
        if args.cdp and port_is_open(cdp_port):
            raise CDPError(
                f"Chrome DevTools port 127.0.0.1:{cdp_port} is already in use"
            )

        if bool(getattr(args, "viewer_auto_login", False)):
            automatic_binary = (
                None
                if bool(getattr(args, "reuse", False))
                else _trusted_automatic_login_chrome(site)
            )
            credential_store = _automatic_credential_store(
                args,
                site,
                automatic_binary,
            )
            credentials_configured = False
            if credential_store is not None:
                try:
                    credentials_configured = bool(credential_store.status())
                except CredentialStoreError:
                    if getattr(args, "credentials_file", None) is not None:
                        raise
                    warn(
                        "could not check the private credentials file; opening the visible "
                        "enterprise login page for manual login"
                    )
            if automatic_binary is None:
                info(
                    "automatic SenseCore login is unavailable for this browser "
                    "configuration; opening the visible login page"
                )
            elif credential_store is not None and credentials_configured:
                info("starting the visible SenseCore login bootstrap")
                login_result = _attempt_viewer_automatic_login(
                    site,
                    tunnel.port,
                    profile,
                    automatic_binary,
                    credential_store,
                )
                if login_result == "browser_closed":
                    raise ChromeError(
                        "the work Chrome profile is already in use or its login "
                        f"window closed before setup: {profile}; close that "
                        "slaigpus Chrome and retry"
                    )
                if login_result == "authenticated":
                    ok("SenseCore automatic login completed for the work profile")
                elif login_result == "not_configured":
                    info(
                        "SenseCore credentials are not configured; opening the "
                        "visible login page"
                    )
                else:
                    warn(
                        "automatic SenseCore login was not completed; opening a "
                        "fresh visible enterprise login page for manual login"
                    )
            else:
                info(
                    "SenseCore credentials are not configured; opening the "
                    "visible login page"
                )
        try:
            chrome = launch_chrome(
                socks_port=tunnel.port,
                profile_dir=profile,
                url=site.url,
                cdp_port=cdp_port,
                enable_cdp=enable_cdp,
                headless=False,
                binary=site.chrome_binary,
                block_local_dns=site.block_local_dns,
                extra_args=site.chrome_args,
                direct=not site.uses_ssh,
            )
        except ChromeError as exc:
            fail(str(exc))
            return 2

        ok(f"Chrome started (profile: {profile})")
        if args.cdp:
            endpoint = wait_for_devtools(
                cdp_port,
                chrome,
                timeout=30.0,
                profile_dir=profile if cdp_port == 0 else None,
            )
            ok(f"DevTools endpoint: http://127.0.0.1:{endpoint.port}")
        if cci_watch and options is not None:
            auto_renew_enabled = AutoRenewControlStore(options.workspace).status()
            info(
                f"CCI controller: {options.workspace}; auto-renew "
                f"{'enabled' if auto_renew_enabled else 'disabled'}, at "
                f"{format_duration(options.renew_after)}"
            )
            worker_kwargs = {}
            if getattr(args, "credentials_file", None) is not None:
                automatic_binary = _trusted_automatic_login_chrome(site)
                worker_kwargs["credential_store"] = _automatic_credential_store(
                    args, site, automatic_binary
                )
                worker_kwargs["automatic_login_binary"] = automatic_binary
            worker = _CCIWatchWorker(site, options, tunnel.port, **worker_kwargs)
            worker.start()
        if worker is None:
            info("close the Chrome window (or Ctrl-C here) to tear everything down")
        else:
            info(
                "close the work Chrome whenever you are done; the CCI controller "
                "continues in the background until Ctrl-C"
            )

        reported_worker_error = False
        reported_work_chrome_exit = False
        disconnected = False
        try:
            while True:
                assert chrome is not None
                work_chrome_exited = chrome.poll() is not None
                worker_running = bool(
                    worker is not None and not worker.finished_event.is_set()
                )
                if worker is not None and worker.error is not None and not reported_worker_error:
                    warn(f"CCI auto-renew stopped: {worker.error}")
                    if not work_chrome_exited:
                        warn("the work Chrome and SSH tunnel remain available")
                    reported_worker_error = True
                if work_chrome_exited:
                    if worker_running:
                        if not reported_work_chrome_exit:
                            info(
                                "work Chrome exited; the CCI controller is still running "
                                "in the background (press Ctrl-C to stop it)"
                            )
                            reported_work_chrome_exit = True
                    else:
                        info("Chrome exited")
                        if worker is not None and worker.error is not None:
                            exit_code = 1
                        break
                if not tunnel.is_running:
                    if not disconnected:
                        warn("SSH tunnel dropped (the CCI may be restarting); keeping Chrome open")
                        disconnected = True
                    tunnel.stop()
                    try:
                        tunnel.start()
                    except TunnelError as exc:
                        if chrome.poll() is not None and not worker_running:
                            break
                        warn(f"waiting for {site.ssh_host} to return: {str(exc).splitlines()[0]}")
                        time.sleep(5)
                        continue
                    ok(f"SSH tunnel restored on the same SOCKS port {tunnel.port}")
                    disconnected = False
                time.sleep(0.5)
        except KeyboardInterrupt:
            print(file=sys.stderr)
            info("shutting down …")
    finally:
        if worker is not None:
            worker.stop()
        # Once we have launched this dedicated Chrome, every path out of
        # cmd_open owns its cleanup.  This includes setup failures after the
        # launch (for example, constructing or starting the CCI watcher), not
        # only Ctrl-C.  On the ordinary path Chrome has already exited, so the
        # guard is a no-op.
        if chrome is not None and chrome.poll() is None:
            chrome.terminate()
            try:
                chrome.wait(timeout=10)
            except subprocess.TimeoutExpired:
                chrome.kill()
                try:
                    chrome.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        tunnel.stop()
    ok("network connection closed")
    return exit_code


def cmd_viewer(args: argparse.Namespace) -> int:
    """Run the visible browser without starting any CCI controller."""
    args.cci_watch = False
    args.viewer_auto_login = True
    return cmd_open(args)


def cmd_controller(args: argparse.Namespace) -> int:
    """Run the persistent, headless-only CCI controller."""
    site = _resolve_sensecore_site(args)
    options = _cci_options(args)
    automatic_binary = _trusted_automatic_login_chrome(site)
    credential_store = _automatic_credential_store(
        args, site, automatic_binary
    )

    tunnel: Optional[NetworkConnection] = None
    worker: Optional[_CCIWatchWorker] = None
    previous_sigterm: Any = None
    sigterm_installed = False

    def stop_for_sigterm(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    try:
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, stop_for_sigterm)
        sigterm_installed = True
    except (AttributeError, OSError, ValueError):
        # Embedders may call the handler outside Python's main thread.  Ctrl-C
        # and the normal finally cleanup remain available there.
        previous_sigterm = None
    try:
        tunnel = _start_tunnel(site, reuse=False)
        socks_port = tunnel.port
        if not args.no_probe:
            _probe_or_warn(tunnel, site, strict=False)

        enabled = AutoRenewControlStore(options.workspace).status()
        info(
            f"headless CCI controller: auto-renew "
            f"{'enabled' if enabled else 'disabled'}, at "
            f"{format_duration(options.renew_after)}"
        )
        worker = _CCIWatchWorker(
            site,
            options,
            socks_port,
            credential_store=credential_store,
            headless_only=True,
            automatic_login_binary=automatic_binary,
        )
        worker.start()
        info(
            "controller is running headlessly; local agents can use "
            "`slaigpus cci status`, `remaining`, and `renew`"
        )

        disconnected = False
        try:
            while not worker.finished_event.wait(0.5):
                if tunnel.is_running:
                    continue
                if not disconnected:
                    warn("SSH tunnel dropped; waiting for the managed CCI host to return")
                    disconnected = True
                tunnel.stop()
                try:
                    tunnel.start()
                except TunnelError as exc:
                    warn(f"waiting for {site.ssh_host}: {str(exc).splitlines()[0]}")
                    worker.stop_event.wait(5.0)
                    continue
                ok(f"SSH tunnel restored on the same SOCKS port {tunnel.port}")
                disconnected = False
        except KeyboardInterrupt:
            print(file=sys.stderr)
            info("shutting down headless controller …")

        if worker.error is not None:
            if isinstance(worker.error, Exception):
                raise worker.error
            raise CCIError("headless CCI controller stopped unexpectedly")
        return 0
    finally:
        if worker is not None:
            worker.stop()
        if tunnel is not None:
            tunnel.stop()
        if sigterm_installed:
            try:
                signal.signal(signal.SIGTERM, previous_sigterm)
            except (OSError, ValueError):
                pass


def cmd_run(args: argparse.Namespace) -> int:
    """Run any command with the tunnel up and proxy env vars injected."""
    command: List[str] = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        fail("nothing to run — usage: slaigpus run [site] -- curl https://…")
        return 2
    if not getattr(args, "used_separator", False):
        # Without `--`, argparse cannot tell `slaigpus run curl …` (no site)
        # from `slaigpus run intranet …` (site given). Refuse to guess.
        fail(
            "put `--` before the command so the site name is unambiguous:\n"
            f"    slaigpus run {args.site or '[site]'} -- "
            + " ".join(shlex.quote(c) for c in ([args.site] if args.site else []) + command)
        )
        return 2

    site = _resolve_site(args, allow_builtin=True)
    with _start_tunnel(site, reuse=args.reuse) as tunnel:
        if not args.no_probe:
            _probe_or_warn(tunnel, site, strict=False)

        import os

        env = {**os.environ, **tunnel.env}
        if site.url:
            env["SLAIGPUS_URL"] = site.url
        env["SLAIGPUS_PROFILE_DIR"] = str(site.resolved_profile_dir())

        info(f"running: {' '.join(shlex.quote(c) for c in command)}")
        try:
            completed = subprocess.run(command, env=env)
        except FileNotFoundError:
            fail(f"command not found: {command[0]}")
            return 127
        except KeyboardInterrupt:
            return 130
    return completed.returncode


class _TunnelKeeper:
    """Reconnect a managed tunnel on its original port while Chrome lives."""

    def __init__(self, tunnel: NetworkConnection, chrome: subprocess.Popen, site: Site) -> None:
        self.tunnel = tunnel
        self.chrome = chrome
        self.site = site
        self.stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="slaigpus-tunnel-keeper")

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        unavailable = False
        while not self.stop_event.wait(0.5):
            if self.chrome.poll() is not None:
                self.stop_event.set()
                return
            if self.tunnel.is_running:
                continue
            if not unavailable:
                warn("SSH tunnel dropped; waiting for the restarted CCI")
                unavailable = True
            self.tunnel.stop()
            try:
                self.tunnel.start()
            except TunnelError:
                self.stop_event.wait(5)
                continue
            ok(f"SSH tunnel restored on the same SOCKS port {self.tunnel.port}")
            unavailable = False

    def stop(self) -> None:
        self.stop_event.set()
        self._thread.join(timeout=10)


def _cdp_is_ready(port: int) -> bool:
    if not port:
        return False
    try:
        from urllib.request import ProxyHandler, build_opener

        opener = build_opener(ProxyHandler({}))
        with opener.open(
            f"http://127.0.0.1:{port}/json/version", timeout=0.5
        ) as response:
            return response.status == 200
    except Exception:
        return False


def _existing_profile_cdp_endpoint(profile: Path) -> Optional[Any]:
    """Return a live DevTools endpoint proven to belong to *profile*.

    Reading only the first line of DevToolsActivePort is insufficient: the
    port may have been reused by another Chrome between runs.  Random-port
    discovery also verifies the browser target path recorded on the second
    line against /json/version.
    """
    try:
        return wait_for_devtools(0, profile_dir=profile, timeout=0.2)
    except CDPError:
        return None


def _run_cci_command(
    args: argparse.Namespace,
    operation: Callable[[RenewalSupervisor, Optional[threading.Event]], int],
) -> int:
    # CCI management keeps the fixed SenseCore identity and API allowlist, but
    # direct versus SSH routing comes from the dedicated network configuration.
    site = _resolve_sensecore_site(args)
    options = _cci_options(args)
    # Standalone CCI commands share the controller's persistent profile, not
    # the user's work-browser profile.  An explicit port remains a direct
    # external attach and therefore makes no profile-ownership claim.
    profile = site.resolved_automation_profile_dir()
    requested_port = int(getattr(args, "cdp_port", 0) or 0)
    headless = bool(getattr(args, "headless", False))
    explicit_network = bool(
        getattr(args, "direct", False) or getattr(args, "ssh_host", "")
    )
    profile_endpoint = (
        None if requested_port else _existing_profile_cdp_endpoint(profile)
    )
    if profile_endpoint is not None and explicit_network:
        raise CCIError(
            "cannot apply --direct/--ssh-host while the automation Chrome is "
            "already running; stop the controller and retry"
        )
    # A user-supplied port is an explicit direct attach.  Automatic profile
    # discovery deliberately keeps cdp_port=0 so BrowserFetchTransport must
    # re-read and verify both lines of this profile's DevToolsActivePort file.
    cdp_port = requested_port
    tunnel: Optional[NetworkConnection] = None
    chrome: Optional[subprocess.Popen] = None
    keeper: Optional[_TunnelKeeper] = None
    transport = None
    automatic_login_binary = (
        None if requested_port else _trusted_automatic_login_chrome(site)
    )

    if requested_port:
        if not _cdp_is_ready(cdp_port):
            raise CCIError(f"no Chrome DevTools endpoint on 127.0.0.1:{cdp_port}")
        info(f"attaching to the managed Chrome DevTools endpoint on port {cdp_port}")
    elif profile_endpoint is not None:
        info(
            "attaching to the managed Chrome DevTools endpoint on port "
            f"{profile_endpoint.port}"
        )
    else:
        tunnel = _start_tunnel(site, reuse=False)
        if not args.no_probe:
            _probe_or_warn(tunnel, site, strict=False)
        cdp_port = 0
        try:
            launch_options = dict(
                socks_port=tunnel.port if tunnel is not None else 0,
                profile_dir=profile,
                # Let the attached BrowserFetchTransport originate the only
                # enterprise-login navigation after Network/Page events are
                # enabled.  Starting Chrome on DEFAULT_URL creates a race in
                # which the redirect can begin before the owned CDP session is
                # observing it.
                url="about:blank",
                cdp_port=cdp_port,
                enable_cdp=True,
                headless=headless,
                binary=automatic_login_binary or site.chrome_binary,
                block_local_dns=site.block_local_dns,
                extra_args=_automation_chrome_args(site),
                direct=not site.uses_ssh,
            )
            chrome = launch_chrome(**launch_options)
        except ChromeError:
            if tunnel is not None:
                tunnel.stop()
            raise
        if tunnel is not None and site.uses_ssh:
            keeper = _TunnelKeeper(tunnel, chrome, site)
            keeper.start()
        if headless:
            info("starting a temporary headless SenseCore automation browser")
        else:
            info("log in to SenseCore in the opened Chrome if prompted")

    try:
        transport = _make_browser_transport(
            cdp_port,
            options,
            profile_dir=profile if not requested_port else None,
            reuse_existing_page=not bool(requested_port),
        )
        transport.start(chrome)
        auto_result = "unavailable"
        if chrome is not None and (headless or automatic_login_binary is not None):
            credential_store = _automatic_credential_store(
                args, site, automatic_login_binary
            )
            auto_result = _attempt_automatic_login(
                transport,
                credential_store,
                profile,
            )
            if auto_result == "authenticated":
                ok("SenseCore automatic login completed")
            elif auto_result == "untrusted":
                raise CCIError(
                    "SenseCore login left the trusted enterprise challenge flow "
                    f"({_safe_login_diagnostic(transport)})"
                )
            elif auto_result in {"session_failed", "session_unknown"}:
                # The trusted login flow completed but did not produce usable
                # API authentication.  Do not leave the visible command asking
                # for manual input on the console (or any page it redirected
                # to).  A retry creates a fresh target whose first navigation
                # is the exact zhicheng login URL.
                raise CCIError(
                    "SenseCore console authentication was not confirmed after the "
                    "zhicheng login page; retry to start again from the exact "
                    "enterprise login URL; CCI auth diagnostic: "
                    f"{_safe_cci_auth_diagnostic(transport)}"
                )
            elif auto_result in {
                "credential_error",
                "failed",
                "submit_challenge",
                "submit_rejected",
                "submit_unknown",
            }:
                warn(
                    "automatic SenseCore login was not completed; use the "
                    "dedicated Chrome window to finish login"
                )
        if headless and chrome is not None and auto_result != "authenticated":
            raise CCIError(
                "headless SenseCore automation browser could not log in; "
                "start `slaigpus controller` "
                "or configure file credentials"
            )
        if auto_result != "authenticated":
            _wait_for_captured_auth_without_navigation(
                transport,
                chrome=chrome,
                stop_event=keeper.stop_event if keeper is not None else None,
            )
        supervisor_kwargs = {}
        if requested_port or profile_endpoint is not None:
            supervisor_kwargs["include_remote_hint"] = False
        supervisor = _make_supervisor(
            transport, site, options, **supervisor_kwargs
        )
        result = operation(supervisor, keeper.stop_event if keeper else None)
        if not requested_port:
            auth = getattr(transport, "auth", None)
            current = getattr(auth, "current", None)
            if callable(current) and current() is not None:
                _remember_automation_login(profile)
        return result
    finally:
        graceful = False
        if transport is not None and chrome is not None:
            graceful = _CCIWatchWorker._request_browser_close(transport)
        if chrome is not None and chrome.poll() is None:
            if graceful:
                try:
                    chrome.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    graceful = False
            if not graceful and chrome.poll() is None:
                chrome.terminate()
                try:
                    chrome.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    chrome.kill()
                    try:
                        chrome.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
        if keeper is not None:
            keeper.stop()
        if tunnel is not None:
            tunnel.stop()


def _run_acp_command(
    args: argparse.Namespace,
    operation: Callable[[Any, Optional[threading.Event]], int],
) -> int:
    """Run one ACP/Monitor operation in the isolated automation profile."""
    validate_acp_workspace(args.acp_workspace)
    site = _resolve_sensecore_site(args)
    profile = site.resolved_automation_profile_dir()
    requested_port = int(getattr(args, "cdp_port", 0) or 0)
    if requested_port:
        raise CCIError(
            "ACP commands do not accept an external Chrome; use the managed "
            "automation profile"
        )
    headless = bool(getattr(args, "headless", False))
    explicit_network = bool(
        getattr(args, "direct", False) or getattr(args, "ssh_host", "")
    )
    profile_endpoint = (
        None if requested_port else _existing_profile_cdp_endpoint(profile)
    )
    if profile_endpoint is not None and explicit_network:
        raise CCIError(
            "cannot apply --direct/--ssh-host while the automation Chrome is "
            "already running; stop the controller and retry"
        )
    cdp_port = requested_port
    tunnel: Optional[NetworkConnection] = None
    chrome: Optional[subprocess.Popen] = None
    keeper: Optional[_TunnelKeeper] = None
    transport: Any = None
    automatic_login_binary = (
        None if requested_port else _trusted_automatic_login_chrome(site)
    )

    if requested_port:
        if not _cdp_is_ready(cdp_port):
            raise CCIError(
                f"no Chrome DevTools endpoint on 127.0.0.1:{cdp_port}"
            )
        info(f"attaching to the managed Chrome DevTools endpoint on port {cdp_port}")
    elif profile_endpoint is not None:
        info(
            "attaching to the managed Chrome DevTools endpoint on port "
            f"{profile_endpoint.port}"
        )
    else:
        tunnel = _start_tunnel(site, reuse=False)
        if not args.no_probe:
            _probe_or_warn(tunnel, site, strict=False)
        try:
            chrome = launch_chrome(
                socks_port=tunnel.port,
                profile_dir=profile,
                url="about:blank",
                cdp_port=0,
                enable_cdp=True,
                headless=headless,
                binary=automatic_login_binary or site.chrome_binary,
                block_local_dns=site.block_local_dns,
                extra_args=_automation_chrome_args(site),
                direct=not site.uses_ssh,
            )
        except ChromeError:
            tunnel.stop()
            raise
        if site.uses_ssh:
            keeper = _TunnelKeeper(tunnel, chrome, site)
            keeper.start()
        if headless:
            info("starting a temporary headless SenseCore ACP browser")
        else:
            info("log in to SenseCore in the opened Chrome if prompted")

    try:
        transport = _make_acp_transport(
            cdp_port,
            profile_dir=profile if not requested_port else None,
            reuse_existing_page=not bool(requested_port),
        )
        transport.start(chrome)
        auto_result = "unavailable"
        if chrome is not None and (headless or automatic_login_binary is not None):
            credential_store = _automatic_credential_store(
                args,
                site,
                automatic_login_binary,
            )
            auto_result = _attempt_automatic_login(
                transport,
                credential_store,
                profile,
            )
            if auto_result == "authenticated":
                ok("SenseCore automatic login completed")
            elif auto_result == "untrusted":
                raise CCIError(
                    "SenseCore login left the trusted enterprise challenge flow "
                    f"({_safe_login_diagnostic(transport)})"
                )
            elif auto_result in {"session_failed", "session_unknown"}:
                raise CCIError(
                    "SenseCore Console authentication was not confirmed after the "
                    "zhicheng login page; retry from the enterprise entry"
                )
            elif auto_result in {
                "credential_error",
                "failed",
                "submit_challenge",
                "submit_rejected",
                "submit_unknown",
            }:
                warn(
                    "automatic SenseCore login was not completed; use the "
                    "dedicated Chrome window to finish login"
                )
        if headless and chrome is not None and auto_result != "authenticated":
            raise CCIError(
                "headless SenseCore automation browser could not log in; start "
                "`slaigpus controller` or configure file credentials"
            )
        if auto_result != "authenticated":
            _wait_for_captured_auth_without_navigation(
                transport,
                chrome=chrome,
                stop_event=keeper.stop_event if keeper is not None else None,
            )
        result = operation(transport, keeper.stop_event if keeper else None)
        if not requested_port:
            auth = getattr(transport, "auth", None)
            current = getattr(auth, "current", None)
            if callable(current) and current() is not None:
                _remember_automation_login(profile)
        return result
    finally:
        graceful = False
        if transport is not None and chrome is not None:
            graceful = _CCIWatchWorker._request_browser_close(transport)
        if chrome is not None and chrome.poll() is None:
            if graceful:
                try:
                    chrome.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    graceful = False
            if not graceful and chrome.poll() is None:
                chrome.terminate()
                try:
                    chrome.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    chrome.kill()
                    try:
                        chrome.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
        if keeper is not None:
            keeper.stop()
        if tunnel is not None:
            tunnel.stop()


_ACP_WORKER_CONFIG_KEYS = frozenset(
    {"version", "replicas", "mounts", "env", "barrier"}
)


def _load_acp_worker_config(path: Optional[Path]) -> Dict[str, Any]:
    """Read one private, versioned worker override without exposing values."""
    if path is None:
        return {}
    data: Any = None
    failed = False
    try:
        data = load_private_json(path, label="ACP worker configuration")
    except PrivateJSONError:
        failed = True
    if failed:
        # Raise outside the handler so even the already-redacted loader error
        # is absent from the public exception chain.
        raise CCIError("could not read private ACP worker configuration")

    valid = isinstance(data, Mapping) and set(data).issubset(
        _ACP_WORKER_CONFIG_KEYS
    )
    if valid:
        valid = type(data.get("version")) is int and data.get("version") == 1
    if valid and "replicas" in data:
        replicas = data.get("replicas")
        valid = type(replicas) is int and 1 <= replicas <= 10_000
    if valid and "mounts" in data:
        valid = isinstance(data.get("mounts"), list)
    if valid and "env" in data:
        valid = isinstance(data.get("env"), (list, Mapping))
    if valid and "barrier" in data:
        valid = isinstance(data.get("barrier"), Mapping)
    if not valid:
        data = None
        raise CCIError("invalid private ACP worker configuration") from None
    return dict(data)


def _acp_template_choice(args: argparse.Namespace) -> Optional[str]:
    """Use an explicitly selected template; otherwise stay account-portable."""
    if args.no_template:
        return None
    if args.template_job:
        return args.template_job
    return None


def _acp_profile_result(profile: ResourceProfile) -> Dict[str, Any]:
    """Return one complete, atomic hardware profile for trusted output."""
    return {
        "profile": profile.key,
        "spec": profile.spec_name,
        "gpu": {
            "type": profile.gpu_type,
            "cards": profile.gpu_cards,
        },
        "cpu": {
            "type": profile.cpu_type,
            "vcpus": profile.vcpus,
        },
        "memory_gib": profile.memory_gib,
    }


def cmd_acp_profiles(args: argparse.Namespace) -> int:
    """List the fixed non-debug ACP hardware catalogue without a browser."""
    selected = [
        profile
        for profile in RESOURCE_PROFILES
        if args.resource_class is None
        or args.resource_class in profile.classes
    ]
    if args.json:
        rows = []
        for profile in selected:
            row = _acp_profile_result(profile)
            row["resource_classes"] = sorted(profile.classes)
            rows.append(row)
        print(json.dumps({"profiles": rows}, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    print(
        "PROFILE\tGPU TYPE\tGPU CARDS\tCPU TYPE\t"
        "vCPUs\tMEMORY (GiB)\tRESOURCE CLASSES\tSPEC"
    )
    for profile in selected:
        print(
            f"{profile.key}\t{profile.gpu_type}\t{profile.gpu_cards}\t"
            f"{profile.cpu_type}\t{profile.vcpus}\t{profile.memory_gib}\t"
            f"{','.join(sorted(profile.classes))}\t{profile.spec_name}"
        )
    return 0


def _acp_plan_result(plan: TrainingJobPlan, *, applied: bool) -> Dict[str, Any]:
    """Return a useful plan summary without echoing image or startup contents."""
    pool = plan.pool
    return {
        "action": "submitted" if applied else "planned",
        "applied": bool(applied),
        "workspace": plan.workspace_id,
        "job": plan.job_name,
        "resource_class": pool.resource_class,
        "api_quota_type": pool.api_quota_type,
        "resource_pool": {
            "id": pool.resource_id,
            "name": pool.name,
            "vpc_id": pool.vpc_id,
            "zone": pool.zone,
            "capacity_basis": pool.capacity_basis,
            "relative_capacity": pool.relative_capacity,
        },
        "resources": _acp_profile_result(pool.profile),
        "worker": {
            "replicas": plan.worker_replicas,
            "mounts": plan.mount_count,
            "environment": plan.env_count,
        },
        "source": {
            "mode": "template" if plan.template_job is not None else "portable",
            "template_job": plan.template_job,
        },
    }


def _print_acp_plan(result: Mapping[str, Any]) -> None:
    pool = result["resource_pool"]
    resources = result["resources"]
    worker = result["worker"]
    source = result["source"]
    print(f"action:       {result['action']}")
    print(f"job:          {result['job']}")
    print(f"workspace:    {result['workspace']}")
    print(f"resource pool:{' ' if pool['name'] else ''}{pool['name']}")
    print(f"zone:         {pool['zone']}")
    print(f"class:        {result['resource_class']}")
    print(f"API quota:    {result['api_quota_type']}")
    print(f"profile:      {resources['profile']}")
    print(f"spec:         {resources['spec']}")
    print(f"GPU type:     {resources['gpu']['type']}")
    print(f"GPU cards:    {resources['gpu']['cards']}")
    print(f"CPU type:     {resources['cpu']['type']}")
    print(f"vCPUs:        {resources['cpu']['vcpus']}")
    print(f"memory:       {resources['memory_gib']} GiB")
    print(f"capacity:     {pool['relative_capacity']:.3g} relative replicas")
    if pool["capacity_basis"] == "current_spot_quota":
        print("capacity basis: current spot quota")
    else:
        print("capacity basis: reserved entitlement (not live remaining capacity)")
    print(f"replicas:     {worker['replicas']}")
    print(f"mounts:       {worker['mounts']}")
    print(f"environment:  {worker['environment']} variables")
    if source["mode"] == "template":
        print(f"source:       template {source['template_job']}")
    else:
        print("source:       portable defaults with Worker overrides")
    if not result["applied"]:
        print("dry-run:      no training job was created; add --apply to submit")


def cmd_acp_submit(args: argparse.Namespace) -> int:
    """Plan an ACP training job, and submit only with explicit ``--apply``."""

    workspace = validate_acp_workspace(args.acp_workspace).resource_id
    # ``main`` resolves the student/RA default. Keep the handler's historical
    # spot fallback for embedded callers that invoke it without CLI startup.
    resource_class = args.resource_class or "spot"
    worker_config = _load_acp_worker_config(args.worker_config)
    configured_replicas = (
        args.replicas
        if args.replicas is not None
        else worker_config.get("replicas")
    )
    mounts: Any = worker_config.get("mounts") if "mounts" in worker_config else None
    env: Any = worker_config.get("env") if "env" in worker_config else None
    if args.clear_mounts:
        mounts = []
    if args.clear_env:
        env = []
    barrier: Any = (
        worker_config.get("barrier") if "barrier" in worker_config else None
    )
    template_job = _acp_template_choice(args)
    validation_replicas = configured_replicas
    if template_job is None and validation_replicas is None:
        validation_replicas = DEFAULT_PORTABLE_REPLICAS
    worker_overrides = normalize_worker_overrides(
        replicas=validation_replicas,
        mounts=mounts,
        env=env,
        barrier=barrier,
    )

    def submit(transport: Any, _stop_event: Optional[threading.Event]) -> int:
        client = ACPClient(transport, workspace=workspace)
        plan = client.plan(
            name=args.name,
            display_name=args.display_name or None,
            image=args.image,
            startup=args.startup,
            resource_profile=args.resource_profile,
            resource_class=resource_class,
            replicas=worker_overrides.replicas,
            mounts=worker_overrides.mounts,
            env=worker_overrides.env,
            barrier=worker_overrides.barrier,
            template_job=template_job,
        )
        applied = bool(args.apply)
        if applied:
            # This is the sole ACP training-job write boundary in the CLI.
            # ACPClient.submit itself deliberately sends exactly one POST.
            client.submit(plan)
        result = _acp_plan_result(plan, applied=applied)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            _print_acp_plan(result)
        return 0

    return _run_acp_command(args, submit)


def _print_dnat_result(result: Mapping[str, Any]) -> None:
    print(f"action:       create DNAT ({'applied' if result['applied'] else 'dry-run'})")
    print(f"EIP:          {result['eip']}")
    print(f"display name: {result['eip_display_name']}")
    print(f"rule:         {result['rule_name']}")
    print(f"protocol:     {result['protocol']}")
    print(f"EIP port:     {result['eip_port']}")
    print(f"target:       {result['target_ip']}:{result['target_port']}")
    if not result["applied"]:
        print("dry-run:      no DNAT rule was created; add --apply to create it")


def _dnat_configured_username(credentials_file: Optional[Path]) -> str:
    store = (
        FileCredentialStore(credentials_file)
        if credentials_file is not None
        else FileCredentialStore()
    )
    credentials = store.load()
    if credentials is None:
        raise DNATError(
            "SenseCore credentials are not configured; run `slaigpus credentials set`"
        )
    username = credentials.username.strip()
    credentials = None
    return username


def cmd_dnat_create(args: argparse.Namespace) -> int:
    """Plan an account-routed DNAT rule, and create it only with ``--apply``."""

    cdp_port = int(args.cdp_port)
    if not _cdp_is_ready(cdp_port):
        raise CCIError(
            f"no slaigpus Chrome DevTools endpoint on 127.0.0.1:{cdp_port}; "
            "start `slaigpus viewer --cdp` first"
        )
    username = _dnat_configured_username(args.credentials_file)
    spec = DNATSpec(
        protocol=args.protocol,
        eip_port=args.eip_port,
        target_ip=args.target_ip,
        target_port=args.target_port,
    )
    transport = _make_dnat_transport(cdp_port)
    try:
        transport.start()
        client = DNATClient(transport, username)
        if args.apply:
            result = client.create(spec).to_dict()
        else:
            plan = client.plan_create(spec)
            result = plan.to_dict()
            result["applied"] = False
    finally:
        transport.close()

    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        _print_dnat_result(result)
    return 0


def _nested_nonempty_text(value: Any, path: str) -> str:
    current = value
    for component in path.split("."):
        if not isinstance(current, Mapping):
            return ""
        current = current.get(component)
    return current.strip() if isinstance(current, str) and current.strip() else ""


def _acp_log_job_identifier(job: Mapping[str, Any], fallback: str) -> str:
    """Prefer the ACP runtime UID used by k8s logs, falling back to job name."""
    for path in ("uid", "detail.uid", "metadata.uid"):
        selected = _nested_nonempty_text(job, path)
        if selected:
            return selected
    return fallback


def _log_hit_identity(hit: Any) -> str:
    """Build a deterministic cross-poll identity for one Monitor log hit."""
    if isinstance(hit, Mapping):
        for name in ("_id", "id"):
            value = hit.get(name)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                return f"{name}:{value}"
    try:
        return "body:" + json.dumps(
            hit,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError):
        return "text:" + format_log_hit(hit)


def _remember_log_hit(
    seen: Dict[str, None],
    identity: str,
    *,
    limit: int = 50_000,
) -> bool:
    """Remember an identity in a bounded FIFO cache; report whether it is new."""
    if identity in seen:
        return False
    seen[identity] = None
    while len(seen) > limit:
        del seen[next(iter(seen))]
    return True


def _emit_follow_log_hit(hit: Any, *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                hit,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
    else:
        print(format_log_hit(hit), flush=True)


def _wait_for_log_poll(
    stop_event: Optional[threading.Event], interval: float
) -> bool:
    """Wait between read-only queries; return true when shutdown was requested."""
    if stop_event is not None:
        return bool(stop_event.wait(interval))
    time.sleep(interval)
    return False


def _monitor_log_total(page: Mapping[str, Any]) -> Optional[int]:
    """Return a usable Monitor total from scalar or Elasticsearch envelopes."""
    value: Any = page.get("total")
    if isinstance(value, Mapping):
        value = value.get("value")
    if isinstance(value, bool):
        return None
    try:
        total = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return total if total >= 0 else None


def _query_follow_log_hits(
    monitor: MonitorClient,
    product: str,
    *,
    start: int,
    end: int,
    resource_id: str,
    page_size: int,
    order: str,
    filter_text: Optional[str],
    custom_filters: List[Dict[str, str]],
) -> List[Any]:
    """Page through one follow interval so bursts larger than one page survive."""
    offset = 0
    collected: List[Any] = []
    previous_signature: Optional[tuple] = None
    for _page_number in range(10_000):
        page = monitor.query_logs(
            product,
            start=start,
            end=end,
            resource_id=resource_id,
            page_size=page_size,
            offset=offset,
            order=order,
            filter=filter_text,
            custom_filter=custom_filters,
        )
        hits = page.get("hits", [])
        if not isinstance(hits, list):
            raise CCIError("Monitor returned an invalid log page")
        if not hits:
            break
        signature = tuple(_log_hit_identity(hit) for hit in hits)
        if offset and signature == previous_signature:
            raise CCIError("Monitor log pagination did not advance")
        previous_signature = signature
        collected.extend(hits)
        offset += len(hits)
        total = _monitor_log_total(page)
        if total is not None:
            if offset >= total:
                break
        elif len(hits) < page_size:
            break
    else:
        raise CCIError("Monitor log pagination exceeded the safety limit")
    return collected


def cmd_acp_logs(args: argparse.Namespace) -> int:
    """Query ACP container logs through one explicit telemetry station."""

    def logs(transport: Any, stop_event: Optional[threading.Event]) -> int:
        since_seconds = parse_duration(args.since, label="since")
        if since_seconds < 1:
            raise CCIError("since must be at least 1s")
        poll_interval = 0.0
        if args.follow:
            poll_interval = parse_duration(
                args.poll_interval,
                label="poll-interval",
            )
            if poll_interval <= 0:
                raise CCIError("poll-interval must be greater than zero")
            if args.offset:
                raise CCIError("offset must be zero with --follow")

        acp = ACPClient(transport, workspace=args.acp_workspace)
        job = acp.get_template_job(args.job)
        job_identifier = _acp_log_job_identifier(job, args.job)
        monitor = MonitorClient(transport, args.telemetry_station)
        product = monitor.select_acp_product(args.product)
        monitor_resource_id = monitor.resolve_resource_id(
            product,
            args.acp_workspace,
        )
        filters = [custom_filter(ACP_JOB_NAME, job_identifier)]
        filters.extend(custom_filter(ACP_POD_NAME, value) for value in args.pod)
        filters.extend(
            custom_filter(ACP_CONTAINER_NAME, value) for value in args.container
        )
        filters.extend(custom_filter(ACP_HOST_IP, value) for value in args.host)

        seen: Dict[str, None] = {}
        previous_end: Optional[int] = None
        while True:
            end = int(time.time())
            if not args.follow:
                start = end - int(since_seconds)
                page = monitor.query_logs(
                    product,
                    start=start,
                    end=end,
                    resource_id=monitor_resource_id,
                    page_size=args.page_size,
                    offset=args.offset,
                    order=args.order,
                    filter=args.filter,
                    custom_filter=filters,
                )
                hits = page.get("hits", [])
                if args.json:
                    output = dict(page)
                    output.update(
                        {
                            "job": args.job,
                            "job_filter": job_identifier,
                            "product": product,
                            "start": start,
                            "end": end,
                        }
                    )
                    print(
                        json.dumps(
                            output,
                            ensure_ascii=False,
                            sort_keys=True,
                            indent=2,
                        )
                    )
                else:
                    for hit in hits:
                        print(format_log_hit(hit))
                    if not hits:
                        info("no ACP log entries matched the query")
                return 0

            if previous_end is None:
                start = end - int(since_seconds)
            else:
                # Re-read an overlap to tolerate second-level timestamps and
                # ingestion delay.  The bounded identity cache removes the
                # duplicates while pagination preserves high-volume bursts.
                overlap = max(60, int(poll_interval * 2))
                start = max(0, previous_end - overlap)
            hits = _query_follow_log_hits(
                monitor,
                product,
                start=start,
                end=end,
                resource_id=monitor_resource_id,
                page_size=args.page_size,
                order=args.order,
                filter_text=args.filter,
                custom_filters=filters,
            )
            previous_end = end

            for hit in hits:
                identity = _log_hit_identity(hit)
                if not _remember_log_hit(seen, identity):
                    continue
                _emit_follow_log_hit(hit, as_json=args.json)
            if _wait_for_log_poll(stop_event, poll_interval):
                return 0

    return _run_acp_command(args, logs)


def cmd_cci_status(args: argparse.Namespace) -> int:
    def show(supervisor: RenewalSupervisor, stop_event: Optional[threading.Event]) -> int:
        status = supervisor.status(include_namespace=True, include_dnat=True)
        data = status.to_dict()
        if args.json:
            print(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(f"app:          {data['app']}")
            print(f"instance:     {data['instance']}")
            print(f"container:    {data['container']}")
            print(f"namespace:    {data['namespace'] or '-'}")
            print(f"image:        {data['image_path'] or '-'}")
            rules = data.get("dnat_rules") or []
            if rules:
                for index, rule in enumerate(rules):
                    label = "dnat:         " if index == 0 else "              "
                    print(f"{label}{rule['endpoint']}")
            else:
                print("dnat:         -")
            print(f"started:      {data['last_started_time']}")
            print(f"running:      {format_duration(data['age_seconds'])}")
            print(f"renew in:     {format_duration(max(0, data['due_in_seconds']))}")
            print(f"expires in:   {format_duration(data['expires_in_seconds'])}")
        return 0

    return _run_cci_command(args, show)


def cmd_cci_start(args: argparse.Namespace) -> int:
    def start(
        supervisor: RenewalSupervisor,
        stop_event: Optional[threading.Event],
    ) -> int:
        result = supervisor.start()
        data = {
            "action": result.action,
            "status": result.status.to_dict(),
        }
        if args.json:
            print(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2))
        elif result.action == "already_running":
            ok(
                f"CCI {result.status.target.app_name} is already RUNNING and ready"
            )
        else:
            ok(
                f"CCI {result.status.target.app_name} started; instance "
                f"{result.status.target.instance_name} is RUNNING and ready"
            )
        return 0

    return _run_cci_command(args, start)


def cmd_cci_renew(args: argparse.Namespace) -> int:
    def renew(supervisor: RenewalSupervisor, stop_event: Optional[threading.Event]) -> int:
        result = supervisor.renew(if_due=args.if_due)
        if args.json:
            print(
                json.dumps(
                    {
                        "action": result.action,
                        "image_uri": result.image_uri,
                        "status": result.status.to_dict(),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
            )
        elif result.action == "not_due":
            ok(f"not due; next renewal in {format_duration(result.status.due_in)}")
        elif result.action == "external_restart":
            ok(
                "an external/manual restart already reset the CCI running time; "
                "the stale pre-PATCH renewal was discarded without changing the CCI"
            )
        elif result.action == "external_change":
            ok(
                "an external/manual container image change was detected; "
                "the stale renewal was discarded and no PATCH was sent"
            )
        else:
            ok(
                f"CCI renewed from {result.image_uri}; new start time "
                f"{result.status.started_at.isoformat()}"
            )
        return 0

    return _run_cci_command(args, renew)


def cmd_cci_remaining(args: argparse.Namespace) -> int:
    """Report both the renewal threshold and SenseCore's hard four-hour edge."""

    def show(
        supervisor: RenewalSupervisor,
        stop_event: Optional[threading.Event],
    ) -> int:
        status = supervisor.status(include_namespace=False)
        source = status.to_dict()
        enabled = supervisor.control.status()
        data = {
            "app": source["app"],
            "instance": source["instance"],
            "last_started_time": source["last_started_time"],
            "checked_at": source["checked_at"],
            "renew_at": source["renew_at"],
            "due_in_seconds": source["due_in_seconds"],
            "renew_in_seconds": max(0, source["due_in_seconds"]),
            "due": source["due"],
            "expires_at": source["expires_at"],
            "expires_in_seconds": source["expires_in_seconds"],
            "expired": source["expired"],
            "auto_renew_enabled": enabled,
        }
        if args.json:
            print(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(f"auto-renew:   {'enabled' if enabled else 'disabled'}")
            print(f"renew at:     {data['renew_at']}")
            print(
                f"renew in:     "
                f"{format_duration(data['renew_in_seconds'])}"
            )
            print(f"expires at:   {data['expires_at']}")
            print(f"expires in:   {format_duration(data['expires_in_seconds'])}")
        return 0

    return _run_cci_command(args, show)


def cmd_cci_auto_renew(args: argparse.Namespace) -> int:
    """Read or change the durable switch without opening SSH or Chrome."""
    workspace = getattr(args, "cci_workspace", "") or DEFAULT_WORKSPACE
    control = AutoRenewControlStore(workspace)
    action = args.auto_renew_action
    if action == "on":
        enabled = control.enable()
    elif action == "off":
        enabled = control.disable()
    else:
        enabled = control.status()

    data = {
        "workspace": control.workspace.resource_id,
        "enabled": enabled,
    }
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2))
    elif action == "status":
        print(f"workspace:  {data['workspace']}")
        print(f"auto-renew: {'enabled' if enabled else 'disabled'}")
    else:
        ok(
            f"auto-renew {'enabled' if enabled else 'disabled'} for workspace "
            f"{data['workspace']}"
        )
    return 0


def cmd_cci_watch(args: argparse.Namespace) -> int:
    def watch(supervisor: RenewalSupervisor, stop_event: Optional[threading.Event]) -> int:
        supervisor.watch(once=args.once, stop_event=stop_event)
        return 0

    return _run_cci_command(args, watch)


def _credential_prompt(label: str) -> str:
    """Read a value without echo; refuse getpass's unsafe stdin fallback."""
    failed = False
    value = ""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass(label)
    except (EOFError, getpass.GetPassWarning):
        failed = True
    if failed:
        raise CredentialStoreError(
            "credentials require an interactive terminal with hidden input"
        )
    return value


def _credential_result(args: argparse.Namespace, data: dict, message: str) -> int:
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        ok(message)
    return 0


def _credential_cli_store(args: argparse.Namespace) -> tuple[Any, str]:
    """Choose the local secret backend without resolving a site or network."""
    credentials_file = getattr(args, "credentials_file", None)
    if credentials_file is not None:
        return FileCredentialStore(credentials_file), "file"
    return FileCredentialStore(), "file"


def cmd_credentials_set(args: argparse.Namespace) -> int:
    """Prompt locally and save a SenseCore login in the selected backend."""
    username = ""
    password = ""
    confirmation = ""
    prompt_failed = False
    interrupted = False
    try:
        username = _credential_prompt("SenseCore username: ").strip()
        password = _credential_prompt("SenseCore password: ")
        confirmation = _credential_prompt("Confirm password: ")
    except CredentialStoreError:
        prompt_failed = True
    except KeyboardInterrupt:
        interrupted = True
    if prompt_failed or interrupted:
        username = ""
        password = ""
        confirmation = ""
        if interrupted:
            raise KeyboardInterrupt
        raise CredentialStoreError(
            "credentials require an interactive terminal with hidden input"
        )
    if not username or not password:
        username = ""
        password = ""
        confirmation = ""
        raise CredentialStoreError("SenseCore username and password must both be non-empty")
    if password != confirmation:
        username = ""
        password = ""
        confirmation = ""
        raise CredentialStoreError("SenseCore password confirmation does not match")
    store, backend = _credential_cli_store(args)
    credentials = SenseCoreCredentials(username=username, password=password)
    try:
        store.save(credentials)
    finally:
        del credentials
        del password
        del confirmation
        del username
    return _credential_result(
        args,
        {"backend": backend, "configured": True},
        f"SenseCore automatic-login credentials saved in {backend}",
    )


def cmd_credentials_status(args: argparse.Namespace) -> int:
    store, backend = _credential_cli_store(args)
    configured = store.status()
    return _credential_result(
        args,
        {"backend": backend, "configured": configured},
        "SenseCore automatic-login credentials are "
        + ("configured" if configured else "not configured"),
    )


def cmd_credentials_delete(args: argparse.Namespace) -> int:
    store, backend = _credential_cli_store(args)
    deleted = store.delete()
    return _credential_result(
        args,
        {
            "backend": backend,
            "configured": False,
            "deleted": deleted,
        },
        (
            f"SenseCore automatic-login credentials deleted from {backend}"
            if deleted
            else "SenseCore automatic-login credentials were not configured"
        ),
    )


# ------------------------------------------------------ guided configuration

def _visible_configuration_input(prompt: str) -> str:
    """Read one visibly echoed wizard value and normalize terminal failures."""
    try:
        return input(prompt)
    except EOFError as exc:
        raise ConfigError(
            "配置助手需要交互式输入；请在终端运行 `slaigpus configure`"
        ) from exc


def _prompt_nonempty(prompt: str) -> str:
    while True:
        value = _visible_configuration_input(prompt)
        if value.strip():
            return value
        print("输入不能为空，请重新输入。")


def _prompt_account_type() -> str:
    print("\n请选择身份：")
    print("  1. 正式学生（ACP 默认使用标准资源）")
    print("  2. RA（ACP 默认使用闲时资源）")
    while True:
        value = _visible_configuration_input(
            "请选择身份（1=正式学生/标准资源，2=RA/闲时资源）: "
        ).strip().lower()
        if value in {"1", "student", "正式学生", "学生"}:
            return "student"
        if value in {"2", "ra"}:
            return "ra"
        print("请输入 1（正式学生）或 2（RA）。")


def _prompt_use_ssh() -> bool:
    while True:
        value = _visible_configuration_input(
            "\n是否使用 SSH 代理？[y/N]: "
        ).strip().lower()
        if value in {"", "n", "no", "否"}:
            return False
        if value in {"y", "yes", "是"}:
            return True
        print("请输入 y 或 n。")


def _prompt_ssh_alias() -> str:
    print(
        "\n请先在 ~/.ssh/config 中配置可用的 OpenSSH Host，例如：\n"
        "\n"
        "Host sensecore-proxy\n"
        "    HostName <跳板机地址>\n"
        "    User <SSH 用户名>\n"
        "    IdentityFile ~/.ssh/id_ed25519\n"
        "\n"
        "也可以直接输入 user@host；端口、密钥和 ProxyJump 仍由 ~/.ssh/config 管理。"
    )
    while True:
        alias = _visible_configuration_input("请输入 SSH Host 别名或 user@host: ").strip()
        try:
            return validate_ssh_alias(alias)
        except ConfigError as exc:
            print(f"SSH 目标无效：{exc}")


def cmd_configure(args: argparse.Namespace) -> int:
    """Run or rerun the visibly echoed first-use configuration wizard."""
    print("slaigpus 引导式配置")
    print("注意：按你的要求，下面输入的账号和密码会在终端中明文显示。")
    username = _prompt_nonempty("SenseCore 账号: ").strip()
    password = _prompt_nonempty("SenseCore 密码: ")
    account_type = _prompt_account_type()
    use_ssh = _prompt_use_ssh()
    ssh_host = _prompt_ssh_alias() if use_ssh else ""
    mode = "ssh" if use_ssh else "direct"

    config_path = config_path_for_write(getattr(args, "config", None))
    update_sensecore_config(
        config_path,
        account_type=account_type,
        network_mode=mode,
        ssh_host=ssh_host,
    )

    store, _backend = _credential_cli_store(args)
    credentials = SenseCoreCredentials(username=username, password=password)
    try:
        store.save(credentials)
    finally:
        del credentials
        del password
        del username

    print("\n配置完成。")
    print(f"配置文件：{config_path}")
    print("登录凭据：已保存到本机私有凭据文件（不会写入 TOML）")
    print(
        "ACP 默认资源："
        + ("标准资源（正式学生）" if account_type == "student" else "闲时资源（RA）")
    )
    print("网络方式：" + (f"SSH 代理（{ssh_host}）" if use_ssh else "直连"))
    return 0


def _command_needs_initial_configuration(args: argparse.Namespace) -> bool:
    command = getattr(args, "cmd", "")
    if command in {"open", "viewer"}:
        # Named/custom sites are generic browser features and must not trigger
        # SenseCore credential collection.
        return not (
            bool(getattr(args, "site", None))
            or bool(getattr(args, "url", ""))
        )
    if command == "controller":
        return True
    if command == "cci":
        return getattr(args, "cci_cmd", "") in {
            "status",
            "renew",
            "remaining",
            "watch",
        }
    if command == "acp":
        return getattr(args, "acp_cmd", "") in {"submit", "logs"}
    return False


def _initial_configuration_complete(args: argparse.Namespace) -> bool:
    explicit = getattr(args, "config", None)
    selected_path = config_path_for_write(explicit)
    if explicit is not None and not selected_path.is_file():
        return False
    if os.environ.get("SLAIGPUS_CONFIG") and not selected_path.is_file():
        return False
    config = load_config(explicit)
    if config.sensecore_account_type not in {"student", "ra"}:
        return False
    store, _backend = _credential_cli_store(args)
    credentials: Optional[SenseCoreCredentials] = None
    try:
        credentials = store.load()
        return credentials is not None
    except CredentialStoreError:
        return False
    finally:
        del credentials


def _interactive_configuration_available() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _ensure_initial_configuration(args: argparse.Namespace) -> None:
    if not _command_needs_initial_configuration(args):
        return
    if _initial_configuration_complete(args):
        return
    if not _interactive_configuration_available():
        raise ConfigError(
            "首次配置尚未完成；请在交互式终端运行 `slaigpus configure`"
        )
    cmd_configure(args)
    if not _initial_configuration_complete(args):
        raise ConfigError("首次配置未能完成；请重新运行 `slaigpus configure`")


def _apply_account_defaults(args: argparse.Namespace) -> None:
    """Resolve and enforce ACP resource access for the configured identity."""
    if not (
        getattr(args, "cmd", "") == "acp"
        and getattr(args, "acp_cmd", "") == "submit"
    ):
        return
    config = load_config(getattr(args, "config", None))
    account_type = config.sensecore_account_type
    requested_class = getattr(args, "resource_class", None)
    if account_type == "ra" and requested_class == "standard":
        raise ConfigError(
            "RA accounts cannot submit standard ACP jobs; use "
            "--resource-class spot"
        )
    if requested_class is None:
        args.resource_class = "standard" if account_type == "student" else "spot"


# ---------------------------------------------------------------- arg parsing

def _add_config(parser: argparse.ArgumentParser) -> None:
    """Accept --config after the subcommand too.

    `slaigpus open --config x` is what people actually type. SUPPRESS means the
    attribute is only set when the flag is given, so it never clobbers the
    value already parsed from the global position.
    """
    parser.add_argument(
        "--config", type=Path, default=argparse.SUPPRESS, help="path to config.toml"
    )


def _add_common(parser: argparse.ArgumentParser, with_site: bool = True) -> None:
    if with_site:
        parser.add_argument(
            "site", nargs="?", help="optional named site from config"
        )
    _add_config(parser)
    network = parser.add_mutually_exclusive_group()
    network.add_argument(
        "--direct",
        action="store_true",
        help="connect directly without starting an SSH SOCKS proxy",
    )
    network.add_argument(
        "--ssh-host",
        default="",
        metavar="DESTINATION",
        help="use this Host alias or user@host as an SSH SOCKS proxy",
    )
    parser.add_argument("--url", default="", help="URL to open / probe")
    parser.add_argument("--port", type=int, default=0, help="local SOCKS port (0 = auto)")
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="attach to an existing listener on --port instead of failing",
    )
    parser.add_argument(
        "--no-probe", action="store_true", help="skip the reachability check"
    )


def _add_sensecore_network(parser: argparse.ArgumentParser) -> None:
    """Add the allowlisted direct/SSH selector used by product commands."""
    _add_config(parser)
    network = parser.add_mutually_exclusive_group()
    network.add_argument(
        "--direct",
        action="store_true",
        help="override config and connect directly",
    )
    network.add_argument(
        "--ssh-host",
        default="",
        metavar="DESTINATION",
        help="override config with a Host alias or user@host",
    )


def _add_cci_target(parser: argparse.ArgumentParser, *, open_flags: bool = False) -> None:
    prefix = "cci-" if open_flags else ""
    parser.add_argument(
        f"--{prefix}workspace",
        dest="cci_workspace",
        default="",
        metavar="RESOURCE_ID",
        help="SenseCore workspace resource id (built-in for the SLAI site)",
    )
    parser.add_argument(
        "--cci",
        f"--{prefix}app",
        dest="cci_app",
        default="",
        metavar="NAME_OR_DISPLAY_NAME",
        help="select one CCI by its exact name or display name",
    )
    parser.add_argument(
        f"--{prefix}instance",
        dest="cci_instance",
        default="",
        help="pin the CCI instance name/uid",
    )
    parser.add_argument(
        f"--{prefix}container",
        dest="cci_container",
        default="",
        help="pin the main container name",
    )
    parser.add_argument(
        f"--{prefix}namespace",
        dest="cci_namespace",
        default="",
        help="pin the private-image namespace",
    )


def _add_cci_timing(parser: argparse.ArgumentParser, *, include_renew: bool = True) -> None:
    if include_renew:
        parser.add_argument(
            "--renew-after", default="3h50m", metavar="DURATION", help="renewal age (default: 3h50m)"
        )
    parser.add_argument(
        "--poll-interval", default="30s", metavar="DURATION", help="polling interval (default: 30s)"
    )
    parser.add_argument(
        "--wait-timeout", default="15m", metavar="DURATION", help="snapshot/restart timeout (default: 15m)"
    )


def _add_acp_browser_common(parser: argparse.ArgumentParser) -> None:
    """Add SenseCore browser/authentication options shared by ACP commands."""
    _add_sensecore_network(parser)
    parser.add_argument(
        "--workspace",
        dest="acp_workspace",
        default=DEFAULT_WORKSPACE,
        metavar="RESOURCE_ID",
        help="SenseCore workspace resource id",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="when launching, fail closed instead of opening a login window",
    )
    parser.add_argument(
        "--credentials-file",
        type=Path,
        default=None,
        help="private JSON credentials for a temporary automation browser",
    )
    parser.add_argument(
        "--no-probe",
        action="store_true",
        help="skip the selected network path reachability check",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slaigpus",
        description=(
            "Manage SenseCore directly or through an optional SSH SOCKS proxy, "
            "with snapshot-based CCI renewal."
        ),
        epilog=(
            "examples:\n"
            "  slaigpus configure            # guided first-use setup\n"
            "  slaigpus viewer               # visible browser only\n"
            "  slaigpus controller           # headless CCI controller\n"
            "  slaigpus credentials set --file ~/.config/slaigpus/credentials.json\n"
            "  slaigpus dnat create --protocol tcp --eip-port 2222 "
            "--target-ip 10.0.0.2 --target-port 22\n"
            "  slaigpus cci status           # read-only target/age check\n"
            "  slaigpus cci renew --if-due   # one safe due-only renewal\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    sub = parser.add_subparsers(dest="cmd")

    p_open = sub.add_parser(
        "open", help="open the managed Chrome and watch the SenseCore CCI"
    )
    _add_common(p_open)
    p_open.add_argument("--chrome-binary", default="", help="override Chrome path")
    p_open.add_argument(
        "--credentials-file",
        type=Path,
        default=None,
        help="use a private JSON credential file for the CCI controller",
    )
    p_open.add_argument(
        "--cdp",
        action="store_true",
        help="enable the configured loopback DevTools port for scripts",
    )
    p_open.add_argument(
        "--cci-watch",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="watch and renew the SenseCore CCI (on by default for the built-in site)",
    )
    _add_cci_target(p_open, open_flags=True)
    _add_cci_timing(p_open)
    p_open.set_defaults(func=cmd_open)

    p_viewer = sub.add_parser(
        "viewer", help="open only the visible managed Chrome"
    )
    _add_common(p_viewer)
    p_viewer.add_argument("--chrome-binary", default="", help="override Chrome path")
    p_viewer.add_argument(
        "--credentials-file",
        type=Path,
        default=None,
        help=(
            "private JSON credentials for automatic login "
            "(default: ~/.config/slaigpus/credentials.json)"
        ),
    )
    p_viewer.add_argument(
        "--cdp",
        action="store_true",
        help="enable the configured loopback DevTools port for scripts",
    )
    p_viewer.set_defaults(
        func=cmd_viewer,
        cci_watch=False,
        viewer_auto_login=True,
    )

    p_controller = sub.add_parser(
        "controller", help="run the persistent headless CCI controller"
    )
    _add_sensecore_network(p_controller)
    p_controller.add_argument(
        "--credentials-file",
        type=Path,
        default=None,
        help="private JSON credentials (default: ~/.config/slaigpus/credentials.json)",
    )
    p_controller.add_argument(
        "--no-probe",
        action="store_true",
        help="skip the selected network path reachability check",
    )
    _add_cci_target(p_controller)
    _add_cci_timing(p_controller)
    p_controller.set_defaults(func=cmd_controller, reuse=False, no_probe=False)

    p_up = sub.add_parser("up", help="hold an SSH SOCKS tunnel open, no browser")
    _add_common(p_up)
    p_up.set_defaults(func=cmd_up)

    p_run = sub.add_parser(
        "run", help="run a command with the selected network environment"
    )
    _add_common(p_run)
    p_run.add_argument("command", nargs=argparse.REMAINDER)
    p_run.set_defaults(func=cmd_run, used_separator=False)

    p_probe = sub.add_parser("probe", help="check the site is reachable, then exit")
    _add_common(p_probe)
    p_probe.set_defaults(func=cmd_probe)

    p_list = sub.add_parser("list", help="show configured sites")
    _add_config(p_list)
    p_list.set_defaults(func=cmd_list)

    p_configure = sub.add_parser(
        "configure",
        help="run the guided first-use or reconfiguration assistant",
    )
    _add_config(p_configure)
    p_configure.add_argument(
        "--credentials-file",
        type=Path,
        default=None,
        help="save login credentials to this private JSON file",
    )
    p_configure.set_defaults(func=cmd_configure)

    p_credentials = sub.add_parser(
        "credentials",
        help="manage local SenseCore automatic-login credentials",
    )
    credential_sub = p_credentials.add_subparsers(
        dest="credentials_action", required=True
    )
    credential_handlers = {
        "set": cmd_credentials_set,
        "status": cmd_credentials_status,
        "delete": cmd_credentials_delete,
    }
    for action, handler in credential_handlers.items():
        command = credential_sub.add_parser(action)
        command.add_argument(
            "--file",
            dest="credentials_file",
            type=Path,
            default=None,
            help="use this private JSON file instead of the default credentials file",
        )
        command.add_argument(
            "--json", action="store_true", help="emit machine-readable JSON"
        )
        command.set_defaults(func=handler)

    p_dnat = sub.add_parser(
        "dnat",
        help="manage DNAT on the EIP selected from the configured username",
    )
    dnat_sub = p_dnat.add_subparsers(dest="dnat_cmd", required=True)
    p_dnat_create = dnat_sub.add_parser(
        "create",
        help="plan an IP-target DNAT rule (add --apply to create it)",
    )
    p_dnat_create.add_argument(
        "--protocol",
        required=True,
        choices=("tcp", "udp"),
        help="DNAT protocol",
    )
    p_dnat_create.add_argument(
        "--eip-port",
        required=True,
        metavar="PORT_OR_RANGE",
        help="public port or range, for example 2222 or 8000-8005",
    )
    p_dnat_create.add_argument(
        "--target-ip",
        required=True,
        help="destination IPv4 address in the EIP's VPC",
    )
    p_dnat_create.add_argument(
        "--target-port",
        required=True,
        metavar="PORT_OR_RANGE",
        help="destination port or range",
    )
    p_dnat_create.add_argument(
        "--cdp-port",
        type=int,
        default=9222,
        help="existing `slaigpus viewer --cdp` port (default: 9222)",
    )
    p_dnat_create.add_argument(
        "--credentials-file",
        type=Path,
        default=None,
        help=(
            "private JSON credentials used to select the account's EIP "
            "(default: ~/.config/slaigpus/credentials.json)"
        ),
    )
    p_dnat_create.add_argument(
        "--apply",
        action="store_true",
        help="create the planned rule; without this flag no rule is created",
    )
    p_dnat_create.add_argument(
        "--json", action="store_true", help="emit a machine-readable plan/result"
    )
    p_dnat_create.set_defaults(func=cmd_dnat_create)

    p_acp = sub.add_parser(
        "acp",
        help="plan ACP jobs or read their container logs",
    )
    acp_sub = p_acp.add_subparsers(dest="acp_cmd", required=True)

    p_acp_submit = acp_sub.add_parser(
        "submit",
        help="plan a fixed-profile ACP job (add --apply to create it)",
    )
    _add_acp_browser_common(p_acp_submit)
    p_acp_submit.add_argument("--name", required=True, help="new training job name")
    p_acp_submit.add_argument(
        "--display-name",
        default="",
        help="optional display name (defaults to --name)",
    )
    p_acp_submit.add_argument(
        "--image",
        required=True,
        help="private container image for the Worker role",
    )
    p_acp_submit.add_argument(
        "--command",
        "--startup",
        dest="startup",
        required=True,
        help="Worker startup command/script",
    )
    template_source = p_acp_submit.add_mutually_exclusive_group()
    template_source.add_argument(
        "--template-job",
        default="",
        help=(
            "optional existing job used as a template; omitted uses portable defaults"
        ),
    )
    template_source.add_argument(
        "--no-template",
        action="store_true",
        help="build from portable defaults without reading an existing job",
    )
    p_acp_submit.add_argument(
        "--worker-config",
        type=Path,
        default=None,
        metavar="FILE",
        help=(
            "absolute path to private 0600 JSON with version and optional "
            "replicas, mounts, env, and barrier overrides"
        ),
    )
    p_acp_submit.add_argument(
        "--replicas",
        "--worker-replicas",
        type=int,
        default=None,
        help="Worker replica count (defaults to template or portable value)",
    )
    p_acp_submit.add_argument(
        "--clear-mounts",
        action="store_true",
        help="replace inherited or configured mounts with an empty list",
    )
    p_acp_submit.add_argument(
        "--clear-env",
        action="store_true",
        help="replace inherited or configured environment with an empty list",
    )
    p_acp_submit.add_argument(
        "--resource-profile",
        choices=RESOURCE_PROFILE_KEYS,
        default=DEFAULT_RESOURCE_PROFILE_KEY,
        help=(
            "fixed atomic GPU/CPU/memory profile "
            f"(default: {DEFAULT_RESOURCE_PROFILE_KEY})"
        ),
    )
    p_acp_submit.add_argument(
        "--resource-class",
        choices=("standard", "spot"),
        default=None,
        help=(
            "submit with standard (RESERVED) or idle (SPOT) resources; "
            "default comes from the configured student/RA identity"
        ),
    )
    p_acp_submit.add_argument(
        "--apply",
        action="store_true",
        help="create the planned job; without this flag no job is submitted",
    )
    p_acp_submit.add_argument(
        "--json",
        action="store_true",
        help="emit a redacted machine-readable plan/result",
    )
    p_acp_submit.set_defaults(func=cmd_acp_submit)

    p_acp_profiles = acp_sub.add_parser(
        "profiles",
        help="list the fixed non-debug ACP hardware profiles locally",
    )
    p_acp_profiles.add_argument(
        "--resource-class",
        choices=("standard", "spot"),
        default=None,
        help="show only profiles available for this resource class",
    )
    p_acp_profiles.add_argument(
        "--json",
        action="store_true",
        help="emit the fixed profile catalogue as JSON",
    )
    p_acp_profiles.set_defaults(func=cmd_acp_profiles)

    p_acp_logs = acp_sub.add_parser(
        "logs",
        help="query ACP container logs through a PRIVATE telemetry station",
    )
    _add_acp_browser_common(p_acp_logs)
    p_acp_logs.add_argument("--job", required=True, help="ACP training job name")
    p_acp_logs.add_argument(
        "--telemetry-station",
        required=True,
        metavar="RESOURCE_ID",
        help="complete PRIVATE telemetry-station resource id",
    )
    p_acp_logs.add_argument(
        "--product",
        default="",
        help="explicit ACP Monitor product (otherwise auto-select)",
    )
    p_acp_logs.add_argument(
        "--pod",
        action="append",
        default=[],
        help="exact pod name filter (repeatable)",
    )
    p_acp_logs.add_argument(
        "--container",
        action="append",
        default=[],
        help="exact container name filter (repeatable)",
    )
    p_acp_logs.add_argument(
        "--host",
        action="append",
        default=[],
        help="exact host IP filter (repeatable)",
    )
    p_acp_logs.add_argument(
        "--since",
        default="1h",
        metavar="DURATION",
        help="rolling query window (default: 1h)",
    )
    p_acp_logs.add_argument(
        "--page-size",
        type=int,
        default=40,
        help="maximum entries per query (default: 40)",
    )
    p_acp_logs.add_argument(
        "--offset",
        type=int,
        default=0,
        help="query offset (default: 0)",
    )
    p_acp_logs.add_argument(
        "--order",
        choices=("asc", "desc"),
        default="desc",
        help="log order (default: desc)",
    )
    p_acp_logs.add_argument(
        "--filter",
        default=None,
        help="Monitor full-text filter",
    )
    p_acp_logs.add_argument(
        "--follow",
        action="store_true",
        help="poll continuously and print each unique entry once",
    )
    p_acp_logs.add_argument(
        "--poll-interval",
        default="5s",
        metavar="DURATION",
        help="follow polling interval (default: 5s)",
    )
    p_acp_logs.add_argument(
        "--json",
        action="store_true",
        help="emit one JSON page, or JSON Lines with --follow",
    )
    p_acp_logs.set_defaults(func=cmd_acp_logs)

    p_cci = sub.add_parser("cci", help="inspect or renew the SenseCore CCI")
    cci_sub = p_cci.add_subparsers(dest="cci_cmd", required=True)

    def add_cci_common(command: argparse.ArgumentParser, *, timing: bool = True) -> None:
        _add_sensecore_network(command)
        _add_cci_target(command)
        if timing:
            _add_cci_timing(command)
        command.add_argument(
            "--cdp-port",
            type=int,
            default=0,
            help="attach to an existing slaigpus Chrome instead of launching one",
        )
        command.add_argument(
            "--headless",
            action="store_true",
            help="when launching, fail closed instead of opening a login window",
        )
        command.add_argument(
            "--credentials-file",
            type=Path,
            default=None,
            help="private JSON credentials for a temporary headless browser",
        )
        command.add_argument(
            "--no-probe",
            action="store_true",
            help="skip the selected network path reachability check",
        )

    p_status = cci_sub.add_parser("status", help="show the selected CCI and running age")
    add_cci_common(p_status)
    p_status.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    p_status.set_defaults(func=cmd_cci_status)

    p_start = cci_sub.add_parser(
        "start",
        help="start a suspended CCI and wait until ready",
    )
    add_cci_common(p_start)
    p_start.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON",
    )
    p_start.set_defaults(func=cmd_cci_start)

    p_renew = cci_sub.add_parser("renew", help="save an image and restart from it now")
    add_cci_common(p_renew)
    p_renew.add_argument(
        "--if-due", action="store_true", help="do nothing successfully before renew-after"
    )
    p_renew.add_argument(
        "--json", action="store_true", help="emit the renewal result as JSON"
    )
    p_renew.set_defaults(func=cmd_cci_renew)

    p_remaining = cci_sub.add_parser(
        "remaining",
        help="show time left until auto-renew and the four-hour hard limit",
    )
    add_cci_common(p_remaining)
    p_remaining.add_argument(
        "--json", action="store_true", help="emit compact machine-readable JSON"
    )
    p_remaining.set_defaults(func=cmd_cci_remaining)

    p_watch = cci_sub.add_parser("watch", help="wait and renew each time the threshold is reached")
    add_cci_common(p_watch)
    p_watch.add_argument(
        "--once", action="store_true", help="exit after the next successful renewal"
    )
    p_watch.set_defaults(func=cmd_cci_watch)

    p_auto_renew = cci_sub.add_parser(
        "auto-renew",
        help="change the durable auto-renew switch without opening Chrome",
    )
    p_auto_renew.add_argument(
        "--workspace",
        dest="cci_workspace",
        default="",
        metavar="RESOURCE_ID",
        help="SenseCore workspace resource id",
    )
    auto_renew_sub = p_auto_renew.add_subparsers(
        dest="auto_renew_action", required=True
    )
    for action in ("on", "off", "status"):
        command = auto_renew_sub.add_parser(action)
        command.add_argument(
            "--workspace",
            dest="cci_workspace",
            default=argparse.SUPPRESS,
            metavar="RESOURCE_ID",
            help="SenseCore workspace resource id",
        )
        command.add_argument(
            "--json", action="store_true", help="emit machine-readable JSON"
        )
        command.set_defaults(func=cmd_cci_auto_renew)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    # Split on the first `--` ourselves. argparse.REMAINDER consumes the
    # separator and then happily reads the next word as a positional, so
    # `slaigpus run -- sh -c ...` would otherwise parse `sh` as the site name.
    tail: List[str] = []
    if "--" in raw:
        idx = raw.index("--")
        raw, tail = raw[:idx], raw[idx + 1:]

    parser = build_parser()
    args = parser.parse_args(raw)

    if tail:
        if getattr(args, "cmd", None) != "run":
            fail("`--` is only meaningful with `slaigpus run`")
            return 2
        args.command = tail
        args.used_separator = True
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        _ensure_initial_configuration(args)
        _apply_account_defaults(args)
        return args.func(args)
    except (
        ConfigError,
        TunnelError,
        CCIError,
        CDPError,
        ChromeError,
        CredentialStoreError,
    ) as exc:
        fail(str(exc))
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
