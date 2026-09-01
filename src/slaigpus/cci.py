"""SenseCore CCI discovery and safe snapshot-based renewal.

The browser is responsible for authentication and transport (see
``slaigpus.cdp``).  This module deliberately knows nothing about cookies or
tokens: it only builds CCI requests and implements the resumable state
machine used by the CLI.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence
from urllib.parse import quote

from .config import default_state_root


DEFAULT_WORKSPACE = (
    "/subscriptions/0197ee17-b6eb-7846-b2b4-a77c5f509b92/"
    "resourceGroups/default/zones/cn-sh-01z/workspaces/share-space-01e"
)
DEFAULT_CONSOLE_URL = (
    "https://console.sensecore.cn/cn-sh-01/cci/app?workspace="
    "%2Fsubscriptions%2F0197ee17-b6eb-7846-b2b4-a77c5f509b92"
    "%2FresourceGroups%2Fdefault%2Fzones%2Fcn-sh-01z"
    "%2Fworkspaces%2Fshare-space-01e"
)
DEFAULT_RENEW_AFTER = 3 * 3600 + 50 * 60
DEFAULT_POLL_INTERVAL = 30.0
DEFAULT_WAIT_TIMEOUT = 15 * 60.0
CCI_HARD_LIMIT_SECONDS = 4 * 3600


def default_cci_state_root(
    *,
    platform: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    home: Optional[Path] = None,
) -> Path:
    """Return the CCI state directory using slaigpus's platform policy."""
    return default_state_root(
        platform=platform,
        environ=environ,
        home=home,
    ) / "cci"


STATE_ROOT = default_cci_state_root()


class CCIError(RuntimeError):
    """A CCI request, target resolution, or renewal operation failed."""


class CCIAPIError(CCIError):
    def __init__(self, method: str, url: str, status: int, detail: str = "") -> None:
        self.status = int(status)
        suffix = f": {detail[:300]}" if detail else ""
        super().__init__(f"CCI API {method} failed with HTTP {status} ({url}){suffix}")


class TargetAmbiguous(CCIError):
    """Automatic discovery found more than one safe candidate."""


class LockBusy(CCIError):
    """Another watcher is already managing this workspace."""


_STATE_DIRECTORY_MODE = 0o700
_STATE_FILE_MODE = 0o600
_MAX_STATE_FILE_BYTES = 1024 * 1024
_MISSING_STATE_FILE = object()


def _state_current_uid() -> int:
    """Return the effective uid used to protect local controller state."""

    getter = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    if not callable(getter):
        raise CCIError("secure CCI state requires operating-system user ownership")
    try:
        return int(getter())
    except Exception:
        raise CCIError(
            "secure CCI state requires operating-system user ownership"
        ) from None


def _open_private_state_root(root: Path, *, create: bool) -> Optional[int]:
    """Open a real, private state directory and return its directory fd.

    All state-file operations are relative to this descriptor.  That keeps a
    path swap after validation from redirecting a read, write, or lock into a
    different directory.
    """

    selected = Path(root).expanduser()
    try:
        lexical = selected.lstat()
    except FileNotFoundError:
        if not create:
            return None
        try:
            selected.mkdir(
                parents=True,
                mode=_STATE_DIRECTORY_MODE,
                exist_ok=False,
            )
        except FileExistsError:
            pass
        except OSError:
            raise CCIError(
                f"cannot create private CCI state directory: {selected}"
            ) from None
        try:
            lexical = selected.lstat()
        except OSError:
            raise CCIError(
                f"cannot securely inspect CCI state directory: {selected}"
            ) from None
    except OSError:
        raise CCIError(
            f"cannot securely inspect CCI state directory: {selected}"
        ) from None

    if stat.S_ISLNK(lexical.st_mode) or not stat.S_ISDIR(lexical.st_mode):
        raise CCIError(
            f"CCI state directory must be a real directory, not a symlink: {selected}"
        )

    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        raise CCIError("secure CCI state directories are unsupported on this platform")
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        descriptor = os.open(selected, flags)
    except OSError:
        raise CCIError(
            f"cannot securely open CCI state directory: {selected}"
        ) from None

    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_dev != lexical.st_dev
            or details.st_ino != lexical.st_ino
            or details.st_uid != _state_current_uid()
            or stat.S_IMODE(details.st_mode) != _STATE_DIRECTORY_MODE
        ):
            raise CCIError(
                "CCI state directory must be a real, current-user-owned "
                f"directory with mode 0700: {selected}"
            )
    except Exception:
        os.close(descriptor)
        raise

    # ``mkdir(mode=0700)`` is intentionally validated instead of repaired.
    # Existing roots, including a directory won in a creation race, must never
    # be silently chmodded because that could change another principal's data.
    return descriptor


def _validate_private_state_file(details: Any, *, description: str) -> None:
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != _state_current_uid()
        or stat.S_IMODE(details.st_mode) != _STATE_FILE_MODE
        or details.st_nlink != 1
        or details.st_size < 0
        or details.st_size > _MAX_STATE_FILE_BYTES
    ):
        raise CCIError(
            f"{description} must be a current-user-owned regular file with "
            "mode 0600, one link, and a bounded size"
        )


def _state_open_flags(mode: int) -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise CCIError("secure CCI state files are unsupported on this platform")
    return mode | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _existing_state_file_details(
    root_descriptor: int,
    name: str,
    *,
    description: str,
) -> Optional[Any]:
    try:
        details = os.stat(
            name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    except OSError:
        raise CCIError(f"cannot securely inspect {description}") from None
    _validate_private_state_file(details, description=description)
    return details


def _same_state_file(before: Any, after: Any) -> bool:
    fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        return False
    for field in ("st_mtime_ns", "st_ctime_ns"):
        if hasattr(before, field) and getattr(before, field) != getattr(after, field):
            return False
    return True


def _read_private_state_json(root: Path, name: str, *, description: str) -> Any:
    root_descriptor = _open_private_state_root(root, create=False)
    if root_descriptor is None:
        return _MISSING_STATE_FILE
    file_descriptor = -1
    chunks: List[bytes] = []
    raw = b""
    try:
        expected = _existing_state_file_details(
            root_descriptor,
            name,
            description=description,
        )
        if expected is None:
            return _MISSING_STATE_FILE
        try:
            file_descriptor = os.open(
                name,
                _state_open_flags(os.O_RDONLY),
                dir_fd=root_descriptor,
            )
        except OSError:
            raise CCIError(f"cannot securely open {description}") from None
        before = os.fstat(file_descriptor)
        _validate_private_state_file(before, description=description)
        if before.st_dev != expected.st_dev or before.st_ino != expected.st_ino:
            raise CCIError(f"{description} changed while it was being opened")

        remaining = _MAX_STATE_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(file_descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(file_descriptor)
        _validate_private_state_file(after, description=description)
        if (
            len(raw) > _MAX_STATE_FILE_BYTES
            or len(raw) != after.st_size
            or not _same_state_file(before, after)
        ):
            raise CCIError(f"{description} changed while it was being read")
    finally:
        chunks = []
        if file_descriptor >= 0:
            os.close(file_descriptor)
        os.close(root_descriptor)

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise CCIError(f"cannot read {description}: invalid JSON") from None
    finally:
        raw = b""


def _atomic_write_private_state_json(
    root: Path,
    name: str,
    value: Any,
    *,
    description: str,
    temporary_prefix: str,
) -> None:
    try:
        payload = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise CCIError(f"cannot encode {description}") from None
    if len(payload) > _MAX_STATE_FILE_BYTES:
        payload = b""
        raise CCIError(f"cannot save {description}: state is too large")

    root_descriptor = _open_private_state_root(root, create=True)
    assert root_descriptor is not None
    temporary_name = f"{temporary_prefix}{uuid.uuid4().hex}.json"
    file_descriptor = -1
    try:
        _existing_state_file_details(
            root_descriptor,
            name,
            description=description,
        )
        try:
            file_descriptor = os.open(
                temporary_name,
                _state_open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                _STATE_FILE_MODE,
                dir_fd=root_descriptor,
            )
        except OSError:
            raise CCIError(f"cannot create a private temporary {description}") from None
        os.fchmod(file_descriptor, _STATE_FILE_MODE)
        offset = 0
        while offset < len(payload):
            written = os.write(file_descriptor, payload[offset:])
            if written <= 0:
                raise CCIError(f"cannot save {description}: short write")
            offset += written
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        file_descriptor = -1
        try:
            os.replace(
                temporary_name,
                name,
                src_dir_fd=root_descriptor,
                dst_dir_fd=root_descriptor,
            )
        except OSError:
            raise CCIError(f"cannot atomically replace {description}") from None
    finally:
        payload = b""
        if file_descriptor >= 0:
            os.close(file_descriptor)
        try:
            os.unlink(temporary_name, dir_fd=root_descriptor)
        except FileNotFoundError:
            pass
        os.close(root_descriptor)


def _unlink_private_state_file(root: Path, name: str, *, description: str) -> None:
    root_descriptor = _open_private_state_root(root, create=False)
    if root_descriptor is None:
        return
    try:
        if _existing_state_file_details(
            root_descriptor,
            name,
            description=description,
        ) is None:
            return
        try:
            os.unlink(name, dir_fd=root_descriptor)
        except FileNotFoundError:
            pass
        except OSError:
            raise CCIError(f"cannot remove {description}") from None
    finally:
        os.close(root_descriptor)


class JsonTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: float = 60.0,
    ) -> Any:
        ...


@dataclass(frozen=True)
class WorkspaceRef:
    subscription: str
    resource_group: str
    zone: str
    workspace: str

    @classmethod
    def parse(cls, value: str) -> "WorkspaceRef":
        parts = [p for p in str(value).strip().split("/") if p]
        expected = ["subscriptions", "resourceGroups", "zones", "workspaces"]
        if len(parts) != 8 or parts[::2] != expected:
            raise CCIError(
                "workspace must be /subscriptions/.../resourceGroups/.../"
                "zones/.../workspaces/..."
            )
        if any(not item for item in parts[1::2]):
            raise CCIError("workspace resource id contains an empty component")
        return cls(parts[1], parts[3], parts[5], parts[7])

    @property
    def resource_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription}/resourceGroups/{self.resource_group}"
            f"/zones/{self.zone}/workspaces/{self.workspace}"
        )

    @property
    def region(self) -> str:
        # SenseCore's workspace-wide zone ends in ``z`` (cn-sh-01z), while
        # service hostnames use the region (cn-sh-01).
        if re.fullmatch(r"[a-z]{2}-[a-z]+-\d{2}z", self.zone):
            return self.zone[:-1]
        match = re.match(r"(.+-\d{2})[a-z]$", self.zone)
        return match.group(1) if match else self.zone

    @property
    def api_base(self) -> str:
        return f"https://cci.{self.region}.sensecore.cn/compute/cci/data/v2"

    @property
    def management_base(self) -> str:
        if self.region == "cn-sh-01":
            return "https://management.sensecoreapi.cn/rmh/v1"
        return f"https://management.{self.region}.sensecoreapi.cn/rmh/v1"

    @property
    def ccr_base(self) -> str:
        return f"https://ccr.{self.region}.sensecoreapi.cn"

    @property
    def apps_path(self) -> str:
        encoded = "/".join(
            quote(v, safe="")
            for v in (
                "subscriptions",
                self.subscription,
                "resourceGroups",
                self.resource_group,
                "zones",
                self.zone,
                "workspaces",
                self.workspace,
                "apps",
            )
        )
        return f"/{encoded}"

    @property
    def owned_apps_path(self) -> str:
        """Collection used by the current Console to list the user's CCIs."""
        return self.apps_path + "Own"


def parse_duration(value: "str | int | float", *, label: str = "duration") -> float:
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip().lower()
        if not text:
            raise CCIError(f"{label} cannot be empty")
        position = 0
        seconds = 0.0
        for match in re.finditer(r"(\d+(?:\.\d+)?)([hms])", text):
            if match.start() != position:
                raise CCIError(f"invalid {label}: {value!r} (try 3h50m or 30s)")
            number = float(match.group(1))
            seconds += number * {"h": 3600, "m": 60, "s": 1}[match.group(2)]
            position = match.end()
        if position != len(text) or position == 0:
            raise CCIError(f"invalid {label}: {value!r} (try 3h50m or 30s)")
    if seconds <= 0:
        raise CCIError(f"{label} must be greater than zero")
    return seconds


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw /= 1000.0
        result = datetime.fromtimestamp(raw, timezone.utc)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError as exc:
            raise CCIError(f"invalid SenseCore timestamp: {value!r}") from exc
    else:
        raise CCIError("CCI instance has no last_started_time")
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


class SenseCoreClient:
    """Thin, schema-tolerant client for the API used by the current CCI UI."""

    def __init__(self, transport: JsonTransport, workspace: "str | WorkspaceRef") -> None:
        self.transport = transport
        self.workspace = (
            workspace if isinstance(workspace, WorkspaceRef) else WorkspaceRef.parse(workspace)
        )

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> Any:
        def perform() -> Any:
            return self.transport.request(
                method,
                url,
                params=params,
                json_body=body,
                headers=(
                    {"x-ui-valid": "x-ui-valid"}
                    if any(
                        url == base or url.startswith(base + "/")
                        for base in (
                            self.workspace.api_base,
                            self.workspace.ccr_base,
                        )
                    )
                    else {}
                ),
                timeout=timeout,
            )

        response = perform()
        if (
            str(method).upper() == "GET"
            and getattr(response, "status", None) == 401
            and hasattr(self.transport, "refresh_auth")
        ):
            # The browser capture invalidates only the generation that got the
            # 401.  Reload lets SenseCore's own SDK refresh it; retry exactly
            # one idempotent GET and never persist or expose the credential.
            # Mutations deliberately fail with their durable uncertainty marker
            # intact so recovery can reconcile with GET instead of replaying a
            # POST or PATCH whose server-side outcome is unknown.
            self.transport.refresh_auth(timeout=min(timeout, 60.0))
            response = perform()
        # BrowserFetchTransport returns a small response object; simple fake
        # transports may return decoded JSON directly.
        if hasattr(response, "status") and hasattr(response, "text"):
            status = int(response.status)
            text = str(response.text or "")
            if status < 200 or status >= 300:
                detail = text
                try:
                    parsed = json.loads(text)
                    detail = str(parsed.get("message") or parsed.get("error") or text)
                except (ValueError, AttributeError):
                    pass
                raise CCIAPIError(method, url, status, detail)
            if not text.strip():
                return {}
            try:
                return json.loads(text)
            except ValueError as exc:
                raise CCIError(f"CCI API returned invalid JSON for {method} {url}") from exc
        return response

    def _app_url(self, app_name: str = "", suffix: str = "") -> str:
        url = self.workspace.api_base + self.workspace.apps_path
        if app_name:
            url += "/" + quote(app_name, safe="")
        return url + suffix

    def list_apps(self) -> List[dict]:
        data = self._request(
            "GET",
            self.workspace.api_base + self.workspace.owned_apps_path,
            params={"page_size": 500, "page_token": 1},
        )
        return list((data or {}).get("apps") or [])

    def get_app(self, app_name: str) -> dict:
        data = self._request("GET", self._app_url(app_name))
        if not isinstance(data, dict):
            raise CCIError(f"invalid app response for {app_name}")
        return data

    def start_app(self, app_name: str) -> None:
        """Start one suspended app using SenseCore's app action endpoint."""
        self._request("POST", self._app_url(app_name, ":start"))

    def list_instances(self, app_name: str) -> List[dict]:
        data = self._request(
            "GET",
            self._app_url(app_name, "/instances"),
            params={"page_size": 500, "page_token": 1},
        )
        return list((data or {}).get("instances") or [])

    def list_snapshots(self, app_name: str) -> List[dict]:
        data = self._request(
            "GET",
            self._app_url(app_name, "/snapshots"),
            params={"page_size": 500, "page_token": 1},
        )
        return list((data or {}).get("snapshots") or [])

    def create_snapshot(
        self,
        app_name: str,
        *,
        name: str,
        display_name: str,
        namespace: str,
        container_name: str,
        instance_name: str,
    ) -> dict:
        result = self._request(
            "POST",
            self._app_url(app_name, "/snapshots"),
            params={"client_type": 0},
            body={
                "name": name,
                "display_name": display_name,
                "ccr_namespace": namespace,
                "container_name": container_name,
                # Despite the API field name, the official UI passes
                # Instance.name here, not uid.
                "instance_uuid": instance_name,
            },
            timeout=90,
        )
        return result if isinstance(result, dict) else {}

    def update_container_image(self, app: dict, container_name: str, uri: str) -> None:
        template = copy.deepcopy(app.get("template") or {})
        containers = list(template.get("containers") or [])
        found = False
        updated: List[dict] = []
        for container in containers:
            preserved = copy.deepcopy(container)
            if _container_name(preserved) == container_name:
                preserved["image_path"] = uri
                found = True
            updated.append(preserved)
        if not found:
            raise CCIError(f"container {container_name!r} disappeared before PATCH")
        template["containers"] = updated
        # v2.30.0's official client currently ignores its update_mask argument
        # and sends this exact PATCH without query parameters.  Keep every
        # server-returned template field byte-for-byte equivalent at the JSON
        # value level; changing anything except image_path would broaden this
        # automation beyond the user's manual operation.
        self._request("PATCH", self._app_url(_app_name(app)), body={"template": template})

    def list_namespaces(self) -> List[dict]:
        data = self._request(
            "GET",
            self.workspace.management_base + "/resources",
            params={
                "filter": (
                    'state="ACTIVE" AND '
                    'resource_type="devtools.ccr.v1.namespace"'
                )
            },
        )
        return list((data or {}).get("resources") or [])

    def get_namespace_info(self, namespace: str) -> dict:
        name = str(namespace).strip()
        if not name:
            raise CCIError("private-image namespace name cannot be empty")
        components = (
            "subscriptions",
            self.workspace.subscription,
            "resourceGroups",
            self.workspace.resource_group,
            "zones",
            self.workspace.zone,
            "namespaces",
            name,
            "info",
        )
        path = "/".join(quote(component, safe="") for component in components)
        data = self._request(
            "GET",
            self.workspace.ccr_base + "/ccr/v1/" + path,
        )
        if not isinstance(data, dict):
            raise CCIError(f"invalid CCR namespace info response for {name!r}")
        return data


def _nested(value: dict, *paths: str) -> Any:
    for path in paths:
        current: Any = value
        for part in path.split("."):
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(part)
        if current not in (None, ""):
            return current
    return None


def _app_name(app: dict) -> str:
    return str(_nested(app, "name", "info.name", "id") or "")


def _app_display_name(app: dict) -> str:
    return str(_nested(app, "display_name", "info.display_name") or "")


def _app_selector_labels(app: dict) -> List[str]:
    """Names users may explicitly pass for one CCI application."""
    return [value for value in (_app_name(app), _app_display_name(app)) if value]


def _app_labels(app: dict) -> List[str]:
    values = [
        _app_name(app),
        _app_display_name(app),
        _nested(app, "resource_pool.name"),
        _nested(app, "resource_pool.display_name"),
        _nested(app, "aec2.name"),
        _nested(app, "aec2.display_name"),
    ]
    return [str(value) for value in values if value]


def _instance_name(instance: dict) -> str:
    return str(_nested(instance, "name", "info.name", "id") or "")


def _container_name(container: dict) -> str:
    return str(container.get("name") or container.get("container_name") or "")


def _namespace_remaining_bytes(info: Mapping[str, Any]) -> Optional[int]:
    """Return exact free bytes from the CCR namespace ``/info`` response.

    The current Console compares the byte-valued ``storageLimit`` and
    ``storageUsed`` fields.  Do not fall back to the management resource's
    purchased-capacity annotation: it contains no usage and therefore cannot
    identify the namespace with the most free space.
    """

    values: List[int] = []
    for key in ("storageLimit", "storageUsed"):
        raw = info.get(key)
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            parsed = raw
        elif isinstance(raw, str) and re.fullmatch(r"\d+", raw.strip()):
            parsed = int(raw.strip())
        else:
            return None
        if parsed < 0:
            return None
        values.append(parsed)
    limit, used = values
    if used > limit:
        return None
    return limit - used


def _is_explicitly_ready(value: Any) -> bool:
    """Accept the boolean-like values used by the API, never a missing value."""
    if value is True or value == 1:
        return True
    return isinstance(value, str) and value.strip().lower() in {"true", "1"}


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _match_score(labels: Sequence[str], hints: Sequence[str]) -> int:
    score = 0
    for raw_label in labels:
        label = _normalize(raw_label)
        if not label:
            continue
        label_tokens = set(label.split("-"))
        for raw_hint in hints:
            hint = _normalize(raw_hint.split("@")[-1])
            if not hint:
                continue
            if label == hint:
                score = max(score, 100)
                continue
            if label in hint or hint in label:
                score = max(score, 80)
            shared = label_tokens & set(hint.split("-"))
            if len(shared) >= 2 and any(token.isdigit() for token in shared):
                score = max(score, 70 + min(len(shared), 5))
    return score


def _selector_key(value: str) -> str:
    """Normalize exact user selectors without discarding non-ASCII names."""
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _select_explicit(items: Sequence[dict], value: str, label: str, names: Callable[[dict], Iterable[str]]) -> dict:
    wanted = _selector_key(value)
    matches = [
        item
        for item in items
        if wanted in {_selector_key(name) for name in names(item)}
    ]
    if len(matches) == 1:
        return matches[0]
    candidates = sorted({n for item in items for n in names(item) if n})
    if not matches:
        raise CCIError(f"unknown {label} {value!r}; candidates: {', '.join(candidates) or '(none)'}")
    raise TargetAmbiguous(f"{label} {value!r} matched more than once: {', '.join(candidates)}")


@dataclass
class CCITarget:
    app: dict
    instance: dict
    container: dict
    namespace: str = ""

    @property
    def app_name(self) -> str:
        return _app_name(self.app)

    @property
    def instance_name(self) -> str:
        return _instance_name(self.instance)

    @property
    def container_name(self) -> str:
        return _container_name(self.container)

    @property
    def image_path(self) -> str:
        return str(self.container.get("image_path") or "")


@dataclass
class CCIStatus:
    target: CCITarget
    started_at: datetime
    checked_at: datetime
    renew_after: float

    @property
    def age(self) -> float:
        return max(0.0, (self.checked_at - self.started_at).total_seconds())

    @property
    def due_in(self) -> float:
        return self.renew_after - self.age

    @property
    def due(self) -> bool:
        return self.due_in <= 0

    @property
    def renew_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.renew_after)

    @property
    def expires_at(self) -> datetime:
        return self.started_at + timedelta(seconds=CCI_HARD_LIMIT_SECONDS)

    @property
    def expires_in(self) -> float:
        return max(0.0, (self.expires_at - self.checked_at).total_seconds())

    @property
    def expired(self) -> bool:
        return self.checked_at >= self.expires_at

    def to_dict(self) -> dict:
        return {
            "app": self.target.app_name,
            "instance": self.target.instance_name,
            "container": self.target.container_name,
            "namespace": self.target.namespace,
            "image_path": self.target.image_path,
            "state": self.target.instance.get("state"),
            "last_started_time": self.started_at.isoformat(),
            "checked_at": self.checked_at.isoformat(),
            "renew_at": self.renew_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "age_seconds": int(self.age),
            "renew_after_seconds": int(self.renew_after),
            "due_in_seconds": int(self.due_in),
            "due": self.due,
            "expires_in_seconds": int(self.expires_in),
            "expired": self.expired,
        }


def _target_is_running_ready(target: CCITarget, container_name: str) -> bool:
    if target.container_name != container_name:
        return False
    if str(target.instance.get("state") or "").upper() != "RUNNING":
        return False
    info = [
        item
        for item in (target.instance.get("container_infos") or [])
        if _container_name(item) == container_name
    ]
    return bool(info) and (
        str(info[0].get("container_state") or "").upper() == "RUNNING"
        and _is_explicitly_ready(info[0].get("ready"))
    )


def _restart_is_confirmed(status: CCIStatus, state: Dict[str, Any]) -> bool:
    image_uri = str(state.get("image_uri") or "")
    return (
        bool(image_uri)
        and status.started_at > parse_timestamp(state.get("old_started_at"))
        and status.target.app_name == state.get("app")
        and status.target.container_name == state.get("container")
        and status.target.image_path == image_uri
        and _target_is_running_ready(status.target, str(state.get("container") or ""))
    )


class TargetResolver:
    def __init__(
        self,
        client: SenseCoreClient,
        *,
        app: str = "",
        instance: str = "",
        container: str = "",
        namespace: str = "",
        hints: Sequence[str] = (),
    ) -> None:
        self.client = client
        self.app_selector = app
        self.instance_selector = instance
        self.container_selector = container
        self.namespace_selector = namespace
        self.hints = [hint for hint in hints if hint]

    def resolve_app(self) -> dict:
        """Resolve the selected app without requiring a live instance."""
        apps = self.client.list_apps()
        if not apps:
            raise CCIError("no CCI apps found in the configured workspace")
        if self.app_selector:
            app_summary = _select_explicit(
                apps,
                self.app_selector,
                "CCI name or display name",
                _app_selector_labels,
            )
        elif len(apps) == 1:
            app_summary = apps[0]
        else:
            scored = [(item, _match_score(_app_labels(item), self.hints)) for item in apps]
            best = max(score for _, score in scored)
            winners = [item for item, score in scored if score == best and score >= 70]
            if len(winners) != 1:
                labels = ", ".join(_app_name(item) for item in apps)
                raise TargetAmbiguous(
                    "cannot safely choose a CCI app; pass --cci. "
                    f"Candidates: {labels}"
                )
            app_summary = winners[0]

        return self.client.get_app(_app_name(app_summary))

    def resolve(
        self,
        *,
        include_namespace: bool = False,
        _instance_selector: Optional[str] = None,
    ) -> CCITarget:
        instance_selector = (
            self.instance_selector
            if _instance_selector is None
            else str(_instance_selector)
        )
        app = self.resolve_app()
        instances = self.client.list_instances(_app_name(app))
        if instance_selector:
            instance = _select_explicit(
                instances,
                instance_selector,
                "instance",
                lambda item: [_instance_name(item), str(item.get("uid") or "")],
            )
        else:
            running = [item for item in instances if str(item.get("state") or "").upper() == "RUNNING"]
            candidates = running or instances
            if len(candidates) != 1:
                names = ", ".join(_instance_name(item) for item in candidates)
                raise TargetAmbiguous(
                    "cannot safely choose a running CCI instance; pass --cci-instance/--instance. "
                    f"Candidates: {names or '(none)'}"
                )
            instance = candidates[0]

        main = list((app.get("template") or {}).get("containers") or [])
        if self.container_selector:
            container = _select_explicit(
                main,
                self.container_selector,
                "container",
                lambda item: [_container_name(item)],
            )
        elif len(main) == 1:
            container = main[0]
        else:
            names = ", ".join(_container_name(item) for item in main)
            raise TargetAmbiguous(
                "cannot safely choose a main container; pass --cci-container/--container. "
                f"Candidates: {names or '(none)'}"
            )

        namespace = self.namespace_selector
        if include_namespace and not namespace:
            namespace = self._discover_namespace(app, container)
        return CCITarget(app=app, instance=instance, container=container, namespace=namespace)

    def resolve_replacement_instance(
        self, *, include_namespace: bool = False
    ) -> CCITarget:
        """Resolve the unique live instance after a persisted renewal began.

        An explicitly pinned instance identifies the pre-renewal target, but
        SenseCore assigns a different instance name after the image PATCH.
        Durable state still pins the app/container and validates the old start
        time, so recovery must deliberately release only the stale instance
        selector while retaining all other selection checks.
        """
        return self.resolve(
            include_namespace=include_namespace,
            _instance_selector="",
        )

    def _discover_namespace(self, app: dict, container: dict) -> str:
        snapshots = self.client.list_snapshots(_app_name(app))
        known = {
            str(item.get("ccr_namespace"))
            for item in snapshots
            if item.get("ccr_namespace") and str(item.get("state") or "").upper() == "SUCCESS"
        }
        if len(known) == 1:
            return next(iter(known))

        image_namespace = ""
        image = str(container.get("image_path") or "")
        image_without_tag = image.rsplit(":", 1)[0]
        path_parts = image_without_tag.split("/")
        if len(path_parts) >= 3 and "." in path_parts[0]:
            image_namespace = path_parts[1]
            if known and image_namespace in known:
                return image_namespace

        resources = self.client.list_namespaces()
        names = sorted({str(item.get("name")) for item in resources if item.get("name")})
        if len(names) == 1:
            return names[0]
        if known and image_namespace and image_namespace in names:
            return str(image_namespace)

        # With no successful snapshot there is no established destination to
        # preserve.  The management listing reports each namespace and its
        # purchased capacity but not usage, so query the same per-namespace
        # /info endpoint used by the CCR Console and choose the unique largest
        # value of storageLimit - storageUsed.  Missing, malformed, or tied
        # values must not authorize a snapshot into an arbitrary namespace.
        if not known and len(names) > 1:
            remaining: Dict[str, int] = {}
            invalid: List[str] = []
            for name in names:
                info = self.client.get_namespace_info(name)
                info_name = str(info.get("name") or "")
                free_bytes = _namespace_remaining_bytes(info)
                if (info_name and info_name != name) or free_bytes is None:
                    invalid.append(name)
                    continue
                remaining[name] = free_bytes
            if invalid or len(remaining) != len(names):
                raise TargetAmbiguous(
                    "cannot safely compare private-image namespace remaining "
                    "capacity; pass --cci-namespace/--namespace. "
                    f"Invalid capacity: {', '.join(invalid) or '(unknown)'}"
                )
            largest = max(remaining.values())
            winners = sorted(
                name for name, free_bytes in remaining.items() if free_bytes == largest
            )
            if len(winners) == 1:
                return winners[0]
            raise TargetAmbiguous(
                "cannot safely choose a private-image namespace because the "
                "largest remaining capacity is tied; pass "
                "--cci-namespace/--namespace. "
                f"Candidates: {', '.join(winners)}"
            )
        raise TargetAmbiguous(
            "cannot safely choose a private-image namespace; pass --cci-namespace/--namespace. "
            f"Candidates: {', '.join(names) or '(none)'}"
        )


class RenewalLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def __enter__(self) -> "RenewalLock":
        root_descriptor = _open_private_state_root(self.path.parent, create=True)
        assert root_descriptor is not None
        file_descriptor = -1
        try:
            try:
                file_descriptor = os.open(
                    self.path.name,
                    _state_open_flags(os.O_RDWR | os.O_CREAT),
                    _STATE_FILE_MODE,
                    dir_fd=root_descriptor,
                )
            except OSError:
                raise CCIError("cannot securely open CCI renewal lock") from None
            details = os.fstat(file_descriptor)
            _validate_private_state_file(
                details,
                description="CCI renewal lock",
            )
            self._fh = os.fdopen(file_descriptor, "a+", encoding="utf-8")
            file_descriptor = -1
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            assert self._fh is not None
            self._fh.close()
            self._fh = None
            raise LockBusy("another slaigpus CCI watcher is already running") from None
        except Exception:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            raise
        finally:
            if file_descriptor >= 0:
                os.close(file_descriptor)
            os.close(root_descriptor)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None
        return False


class RenewalStateStore:
    def __init__(self, root: Path, workspace: WorkspaceRef) -> None:
        digest_source = workspace.resource_id.encode("utf-8")
        import hashlib

        key = hashlib.sha256(digest_source).hexdigest()[:16]
        self.root = Path(root)
        self.path = self.root / f"{key}.json"
        self.lock_path = self.root / f"{key}.lock"

    def load(self) -> Dict[str, Any]:
        value = _read_private_state_json(
            self.root,
            self.path.name,
            description="CCI renewal state",
        )
        if value is _MISSING_STATE_FILE:
            return {}
        if not isinstance(value, dict):
            raise CCIError("cannot read CCI renewal state: expected a JSON object")
        return value

    def save(self, value: Dict[str, Any]) -> None:
        _atomic_write_private_state_json(
            self.root,
            self.path.name,
            value,
            description="CCI renewal state",
            temporary_prefix=".state-",
        )

    def clear(self) -> None:
        _unlink_private_state_file(
            self.root,
            self.path.name,
            description="CCI renewal state",
        )

    def lock(self) -> RenewalLock:
        return RenewalLock(self.lock_path)


class AutoRenewControlStore:
    """Durable per-workspace switch for starting automatic renewal cycles."""

    def __init__(
        self,
        workspace: "str | WorkspaceRef",
        root: Path = STATE_ROOT,
    ) -> None:
        import hashlib

        self.workspace = (
            workspace if isinstance(workspace, WorkspaceRef) else WorkspaceRef.parse(workspace)
        )
        key = hashlib.sha256(self.workspace.resource_id.encode("utf-8")).hexdigest()[:16]
        self.root = Path(root)
        self.path = self.root / f"{key}.control.json"

    def status(self) -> bool:
        """Return whether automatic renewal is enabled; absence means on."""
        value = _read_private_state_json(
            self.root,
            self.path.name,
            description="CCI auto-renew control",
        )
        if value is _MISSING_STATE_FILE:
            return True
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or not isinstance(value.get("enabled"), bool)
        ):
            raise CCIError(
                "cannot read CCI auto-renew control: expected version 1 "
                "with a boolean enabled field"
            )
        return bool(value["enabled"])

    def _set(self, enabled: bool) -> bool:
        _atomic_write_private_state_json(
            self.root,
            self.path.name,
            {"version": 1, "enabled": bool(enabled)},
            description="CCI auto-renew control",
            temporary_prefix=".control-",
        )
        return bool(enabled)

    def enable(self) -> bool:
        return self._set(True)

    def disable(self) -> bool:
        return self._set(False)


@dataclass
class RenewalResult:
    action: str
    status: CCIStatus
    image_uri: str = ""


@dataclass
class StartResult:
    action: str
    status: CCIStatus


class RenewalSupervisor:
    """Poll CCI age and execute snapshot -> success -> PATCH -> ready."""

    def __init__(
        self,
        client: SenseCoreClient,
        resolver: TargetResolver,
        *,
        renew_after: float = DEFAULT_RENEW_AFTER,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
        state_root: Path = STATE_ROOT,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        report: Callable[[str], None] = lambda message: None,
    ) -> None:
        self.client = client
        self.resolver = resolver
        self.renew_after = float(renew_after)
        self.poll_interval = float(poll_interval)
        self.wait_timeout = float(wait_timeout)
        if not (0 < self.renew_after < 4 * 3600):
            raise CCIError("renew-after must be greater than 0 and less than 4h")
        if self.poll_interval <= 0 or self.wait_timeout <= 0:
            raise CCIError("poll interval and wait timeout must be greater than zero")
        self.store = RenewalStateStore(Path(state_root), client.workspace)
        self.control = AutoRenewControlStore(client.workspace, root=Path(state_root))
        self._now = now
        self._sleep = sleep
        self._report = report

    def _transport_is_broken(self) -> bool:
        """True when the browser/CDP layer requires full reconstruction."""
        client = getattr(self, "client", None)
        transport = getattr(client, "transport", None)
        return bool(getattr(transport, "broken", False))

    def _transport_login_required(self) -> bool:
        """True when browser control must pause for an interactive login."""
        client = getattr(self, "client", None)
        transport = getattr(client, "transport", None)
        return bool(getattr(transport, "login_required", False))

    def _auto_renew_enabled(self, source: Any = None) -> bool:
        """Read a dynamic callback/control object, defaulting to durable control."""
        if source is None:
            source = getattr(self, "control", None)
            if source is None:
                # Keeps lightweight embedders/backwards-compatible test doubles
                # enabled when they did not construct the normal control store.
                return True
        if isinstance(source, bool):
            return source
        if callable(source):
            return bool(source())
        status = getattr(source, "status", None)
        if callable(status):
            return bool(status())
        raise CCIError("auto-renew control must be a callback or expose status()")

    def _suspended_app_name(self) -> str:
        """Return the selected app name only when it is explicitly suspended."""
        resolve_app = getattr(self.resolver, "resolve_app", None)
        if not callable(resolve_app):
            return ""
        app = resolve_app()
        if str(app.get("state") or "").upper() != "SUSPENDED":
            return ""
        return _app_name(app)

    def status(
        self,
        *,
        include_namespace: bool = False,
        allow_replacement_instance: bool = False,
    ) -> CCIStatus:
        resolve_replacement = getattr(
            self.resolver,
            "resolve_replacement_instance",
            None,
        )
        if allow_replacement_instance and callable(resolve_replacement):
            target = resolve_replacement(include_namespace=include_namespace)
        else:
            target = self.resolver.resolve(include_namespace=include_namespace)
        started = parse_timestamp(target.instance.get("last_started_time"))
        return CCIStatus(target, started, self._now().astimezone(timezone.utc), self.renew_after)

    def start(self) -> StartResult:
        """Start a suspended app once and wait for its main container."""
        with self.store.lock():
            app = self.resolver.resolve_app()
            app_name = _app_name(app)
            state = str(app.get("state") or "UNKNOWN").upper()
            if state == "RUNNING":
                action = "already_running"
            elif state == "SUSPENDED":
                self._report(f"starting suspended CCI {app_name}")
                self.client.start_app(app_name)
                action = "started"
            else:
                raise CCIError(
                    f"CCI {app_name} is {state}; start is allowed only from "
                    "SUSPENDED (RUNNING is treated as a no-op)"
                )
            return StartResult(action, self._wait_for_running_ready(app_name, action))

    def _wait_for_running_ready(self, app_name: str, action: str) -> CCIStatus:
        deadline = time.monotonic() + self.wait_timeout
        last_error = ""
        while True:
            try:
                status = self.status(
                    include_namespace=False,
                    allow_replacement_instance=True,
                )
                if (
                    status.target.app_name == app_name
                    and _target_is_running_ready(
                        status.target,
                        status.target.container_name,
                    )
                ):
                    return status
                last_error = "the selected instance or container is not RUNNING and ready"
            except Exception as exc:
                if self._transport_is_broken() or self._transport_login_required():
                    raise
                last_error = str(exc)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sleep(min(self.poll_interval, max(0.1, remaining)))
        suffix = f" Last check: {last_error}" if last_error else ""
        verb = "was requested" if action == "started" else "was already RUNNING"
        raise CCIError(
            f"CCI start {verb}, but a RUNNING and ready container was not observed "
            f"within {format_duration(self.wait_timeout)}.{suffix}"
        )

    def renew(self, *, if_due: bool = False) -> RenewalResult:
        with self.store.lock():
            state = self.store.load()
            # Namespace discovery is needed only when preparing a brand-new
            # snapshot.  A recovery must use the persisted namespace and must
            # not be blocked (or redirected) by a changed namespace listing.
            initial = self.status(
                include_namespace=not bool(state),
                allow_replacement_instance=bool(state),
            )
            if if_due and not initial.due and not state:
                return RenewalResult("not_due", initial)
            return self._renew_locked(initial, state)

    def _renew_locked(
        self, current: CCIStatus, state: Optional[Dict[str, Any]] = None
    ) -> RenewalResult:
        state = self.store.load() if state is None else dict(state)
        if state:
            if state.get("version") != 2:
                raise CCIError("unsupported or corrupt renewal state version")
            required = (
                "workspace",
                "app",
                "instance",
                "container",
                "namespace",
                "old_started_at",
                "requested_at",
                "snapshot_name",
                "snapshot_display_name",
                "old_image_path",
                "stage",
            )
            missing = [key for key in required if not state.get(key)]
            if missing:
                raise CCIError(
                    "renewal state is incomplete; missing " + ", ".join(missing)
                )
            if state.get("workspace") != self.client.workspace.resource_id:
                raise CCIError("renewal state belongs to a different workspace")
            if state.get("app") != current.target.app_name:
                raise CCIError(
                    "unfinished renewal belongs to a different app; inspect the state before retrying"
                )
            if state.get("container") != current.target.container_name:
                raise CCIError(
                    "unfinished renewal belongs to a different container; inspect the state before retrying"
                )
            stage = str(state["stage"])
            valid_stages = {
                "snapshot_prepared",
                "snapshot_submitting",
                "snapshot_requested",
                "image_ready",
                "patch_submitting",
                "patch_sent",
            }
            if stage not in valid_stages:
                raise CCIError(f"unknown renewal stage: {stage}")
            if stage in {"image_ready", "patch_submitting", "patch_sent"} and not state.get(
                "image_uri"
            ):
                raise CCIError(
                    f"renewal state at {stage} has no image_uri; refusing to modify CCI"
                )
            stored_start = parse_timestamp(state.get("old_started_at"))
            if current.started_at > stored_start:
                image_uri = str(state.get("image_uri") or "")
                image_applied = bool(image_uri) and current.target.image_path == image_uri
                if image_applied and _restart_is_confirmed(current, state):
                    self.store.clear()
                    return RenewalResult("recovered", current, image_uri)
                if stage not in {"image_ready", "patch_submitting", "patch_sent"} or (
                    stage == "image_ready" and not image_applied
                ):
                    # No PATCH was durably recorded.  An external/manual
                    # restart already reset the four-hour clock, so abandon
                    # this stale pre-PATCH operation without claiming that our
                    # image renewal succeeded.
                    self.store.clear()
                    return RenewalResult("external_restart", current, image_uri)
                # PATCH was sent, or may have been sent before its response
                # was lost, but the selected image has not appeared yet. Keep
                # the state rather than declaring success or retrying blindly.
            elif state.get("instance") != current.target.instance_name:
                raise CCIError(
                    "unfinished renewal belongs to a different instance; inspect the state before retrying"
                )
        else:
            state = self._new_state(current)
            self.store.save(state)

        stage = str(state.get("stage") or "snapshot_prepared")
        if stage == "snapshot_prepared":
            snapshot = self._find_snapshot(state)
            if snapshot is None:
                self._report(
                    f"saving {state['container']} as private image "
                    f"{state['namespace']}/{state['snapshot_name']}"
                )
                # Persist the uncertainty boundary *before* POST.  If the
                # process or network dies after this point, a later run only
                # reconciles with GET and never blindly creates a duplicate.
                state["stage"] = "snapshot_submitting"
                self.store.save(state)
                try:
                    response = self.client.create_snapshot(
                        state["app"],
                        name=state["snapshot_name"],
                        display_name=state["snapshot_display_name"],
                        namespace=state["namespace"],
                        container_name=state["container"],
                        instance_name=state["instance"],
                    )
                except Exception:
                    # The POST may have reached the server even if its response
                    # was lost.  Keep the marker and never PATCH or re-POST.
                    raise
                state["snapshot_uid"] = str(response.get("uid") or "")
                state["stage"] = "snapshot_requested"
                self.store.save(state)
                snapshot = response if response.get("state") else None
            else:
                state["stage"] = "snapshot_requested"
                self.store.save(state)
        else:
            snapshot = None

        if state["stage"] == "snapshot_submitting":
            snapshot = self._find_snapshot(state)
            if snapshot is None:
                raise CCIError(
                    "a previous snapshot POST has an unknown outcome; no PATCH or duplicate "
                    "POST was sent. Check the CCI snapshot page, then retry."
                )
            state["stage"] = "snapshot_requested"
            state["snapshot_uid"] = str(snapshot.get("uid") or "")
            self.store.save(state)

        if state["stage"] == "snapshot_requested":
            snapshot = self._wait_for_snapshot(state, immediate=snapshot)
            uri = str(snapshot.get("uri") or "")
            if not uri:
                raise CCIError("snapshot reported SUCCESS without a usable uri")
            state["image_uri"] = uri
            state["snapshot_uid"] = str(snapshot.get("uid") or state.get("snapshot_uid") or "")
            state["stage"] = "image_ready"
            self.store.save(state)

        if state["stage"] == "image_ready":
            # Snapshot creation can take minutes.  Re-resolve immediately
            # before PATCH so a manual update or platform restart during that
            # wait cannot be overwritten by a stale image.
            prepatch = self.status(
                include_namespace=False,
                allow_replacement_instance=True,
            )
            if (
                prepatch.target.app_name != state["app"]
                or prepatch.target.container_name != state["container"]
            ):
                raise CCIError(
                    "CCI target changed while the snapshot was being saved; PATCH was not sent"
                )
            current_image = prepatch.target.image_path
            old_started = parse_timestamp(state["old_started_at"])
            if prepatch.started_at != old_started:
                if current_image == state["image_uri"]:
                    # PATCH may have succeeded while its response was lost.
                    # Record the uncertainty and let strict restart/ready
                    # reconciliation below decide when it is complete.
                    state["stage"] = "patch_sent"
                    self.store.save(state)
                else:
                    self.store.clear()
                    return RenewalResult(
                        "external_restart",
                        prepatch,
                        str(state.get("image_uri") or ""),
                    )
            else:
                if state["instance"] != prepatch.target.instance_name:
                    raise CCIError(
                        "CCI instance changed while the snapshot was being saved; PATCH was not sent"
                    )
                if not _target_is_running_ready(prepatch.target, state["container"]):
                    raise CCIError(
                        "CCI/container is not explicitly RUNNING and ready; PATCH was not sent"
                    )
                if current_image not in {
                    state["old_image_path"],
                    state["image_uri"],
                }:
                    self.store.clear()
                    return RenewalResult(
                        "external_change",
                        prepatch,
                        str(state.get("image_uri") or ""),
                    )
                if current_image != state["image_uri"]:
                    self._report("snapshot is ready; updating the CCI container image")
                    # Persist the uncertainty boundary before PATCH, just as
                    # snapshot_submitting does before POST.  A transport error
                    # cannot tell us whether SenseCore accepted the update, so
                    # a later run must reconcile with GET instead of blindly
                    # causing another restart.
                    state["stage"] = "patch_submitting"
                    self.store.save(state)
                    try:
                        self.client.update_container_image(
                            prepatch.target.app,
                            state["container"],
                            state["image_uri"],
                        )
                    except Exception:
                        # Keep patch_submitting: the request may have reached
                        # the server even though its response was lost.
                        raise
                state["stage"] = "patch_sent"
                self.store.save(state)

        if state["stage"] == "patch_submitting":
            if current.target.image_path == state["image_uri"]:
                # The PATCH took effect despite its missing response.  The
                # usual restart reconciliation below remains deliberately
                # strict about timestamp, image, RUNNING state, and readiness.
                state["stage"] = "patch_sent"
                self.store.save(state)
            else:
                raise CCIError(
                    "a previous CCI PATCH has an unknown outcome; no duplicate PATCH was "
                    "sent and recovery state was kept. Wait for the console to settle, "
                    "then retry. If the image is still unchanged, apply the saved image "
                    f"{state['image_uri']} manually in the CCI console and retry."
                )

        if state["stage"] != "patch_sent":
            raise CCIError(f"unknown renewal stage: {state['stage']}")
        completed = self._wait_for_restart(state)
        self.store.clear()
        return RenewalResult("renewed", completed, str(state.get("image_uri") or ""))

    def _new_state(self, status: CCIStatus) -> Dict[str, Any]:
        target = status.target
        if not target.namespace:
            raise CCIError(
                "the private-image namespace could not be resolved; snapshot was not requested"
            )
        if str(target.instance.get("state") or "").upper() != "RUNNING":
            raise CCIError("the selected CCI instance is not RUNNING; snapshot was not requested")
        running_info = [
            item
            for item in (target.instance.get("container_infos") or [])
            if _container_name(item) == target.container_name
        ]
        if not running_info:
            raise CCIError(
                "the selected container runtime status is unavailable; snapshot was not requested"
            )
        info = running_info[0]
        if str(info.get("container_state") or "").upper() != "RUNNING":
            raise CCIError("the selected container is not RUNNING; snapshot was not requested")
        if not _is_explicitly_ready(info.get("ready")):
            raise CCIError(
                "the selected container is not explicitly ready; snapshot was not requested"
            )
        if not target.image_path:
            raise CCIError("the selected container has no image_path; snapshot was not requested")
        stamp = self._now().astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
        nonce = uuid.uuid4().hex[:8]
        base = re.sub(r"[^a-z0-9._-]+", "-", target.app_name.lower()).strip("-._")
        while ".." in base:
            base = base.replace("..", ".")
        # SenseCore snapshot repository names are 1..63 lowercase characters
        # and cannot start/end with punctuation.  Display names also reject
        # spaces, despite being more permissive than repository names.
        name = (f"slaigpus-{base}" if base else "slaigpus-cci")[:63].rstrip("-._")
        return {
            "version": 2,
            "workspace": self.client.workspace.resource_id,
            "app": target.app_name,
            "instance": target.instance_name,
            "container": target.container_name,
            "namespace": target.namespace,
            "old_started_at": status.started_at.isoformat(),
            "old_image_path": target.image_path,
            "requested_at": self._now().astimezone(timezone.utc).isoformat(),
            "snapshot_name": name,
            "snapshot_display_name": f"slaigpus-auto-{stamp}-{nonce}",
            "snapshot_uid": "",
            "image_uri": "",
            "stage": "snapshot_prepared",
        }

    def _find_snapshot(self, state: Dict[str, Any]) -> Optional[dict]:
        snapshots = self.client.list_snapshots(state["app"])
        uid = str(state.get("snapshot_uid") or "")
        requested = parse_timestamp(state["requested_at"]) - timedelta(minutes=2)
        candidates: List[dict] = []
        for item in snapshots:
            if uid and str(item.get("uid") or "") == uid:
                return item
            if (
                str(item.get("name") or "") == state["snapshot_name"]
                and str(item.get("ccr_namespace") or "") == state["namespace"]
                and str(item.get("display_name") or "")
                == state["snapshot_display_name"]
            ):
                if (
                    item.get("container_name")
                    and str(item.get("container_name")) != state["container"]
                ):
                    continue
                if (
                    item.get("instance_uuid")
                    and str(item.get("instance_uuid")) != state["instance"]
                ):
                    continue
                try:
                    if parse_timestamp(item.get("created_time")) >= requested:
                        candidates.append(item)
                except CCIError:
                    continue
        if len(candidates) > 1:
            raise CCIError(
                "multiple snapshots match the interrupted request; refusing to guess"
            )
        return candidates[0] if candidates else None

    def _wait_for_snapshot(
        self, state: Dict[str, Any], *, immediate: Optional[dict] = None
    ) -> dict:
        deadline = time.monotonic() + self.wait_timeout
        snapshot = immediate
        while time.monotonic() < deadline:
            if snapshot is None or not snapshot.get("state"):
                try:
                    snapshot = self._find_snapshot(state)
                except Exception as exc:
                    if self._transport_is_broken() or self._transport_login_required():
                        raise
                    self._report(f"waiting for snapshot status ({exc})")
                    snapshot = None
            if snapshot is not None:
                state_name = str(snapshot.get("state") or "UNKNOWN").upper()
                if state_name == "SUCCESS":
                    return snapshot
                if state_name in {"FAIL", "INVALID", "UNKNOWN"}:
                    reason = str(snapshot.get("reason") or "no reason returned")
                    raise CCIError(
                        f"snapshot {state['snapshot_name']} ended as {state_name}: {reason}; "
                        "CCI was not modified"
                    )
            self._sleep(min(self.poll_interval, max(0.1, deadline - time.monotonic())))
            snapshot = None
        raise CCIError(
            f"timed out after {format_duration(self.wait_timeout)} waiting for snapshot; "
            "CCI was not modified and the operation can be resumed"
        )

    def _wait_for_restart(self, state: Dict[str, Any]) -> CCIStatus:
        deadline = time.monotonic() + self.wait_timeout
        old_started = parse_timestamp(state["old_started_at"])
        last_error = ""
        while time.monotonic() < deadline:
            try:
                status = self.status(
                    include_namespace=False,
                    allow_replacement_instance=True,
                )
                container_ready = _target_is_running_ready(
                    status.target, state["container"]
                )
                image_applied = status.target.image_path == state["image_uri"]
                if _restart_is_confirmed(status, state):
                    return status
                if not image_applied:
                    last_error = "the restarted app has not applied the saved image"
                elif not container_ready:
                    last_error = "the target container is not explicitly RUNNING and ready"
                else:
                    last_error = "the instance start time or state has not advanced"
            except Exception as exc:
                # During a CCI restart the selected network path and browser fetches are
                # expected to fail temporarily.  cmd_open reconnects the same
                # local port while this loop keeps the durable patch_sent state.
                if self._transport_is_broken() or self._transport_login_required():
                    raise
                last_error = str(exc)
            self._sleep(min(self.poll_interval, max(0.1, deadline - time.monotonic())))
        suffix = f" Last check: {last_error}" if last_error else ""
        raise CCIError(
            f"image was updated, but the new RUNNING instance was not observed within "
            f"{format_duration(self.wait_timeout)}; state was kept for recovery.{suffix}"
        )

    def watch(
        self,
        *,
        once: bool = False,
        stop_event: Any = None,
        enabled: Any = None,
    ) -> None:
        last_bucket: Optional[int] = None
        last_error = ""
        last_renewal_error = ""
        while stop_event is None or not stop_event.is_set():
            try:
                status = self.status(include_namespace=False)
            except Exception as exc:
                # Authentication, the network path, and the CCI instance can all
                # be briefly unavailable during a restart.  A watcher should
                # stay alive and let cmd_open repair the tunnel.
                if self._transport_is_broken() or self._transport_login_required():
                    raise
                try:
                    suspended_app = self._suspended_app_name()
                except TargetAmbiguous:
                    raise
                except Exception:
                    suspended_app = ""
                if suspended_app:
                    renewal_pending = bool(self.store.load())
                    start_enabled = (
                        True
                        if renewal_pending
                        else self._auto_renew_enabled(enabled)
                    )
                    if start_enabled:
                        try:
                            result = self.start()
                        except Exception as start_exc:
                            if (
                                self._transport_is_broken()
                                or self._transport_login_required()
                            ):
                                raise
                            message = (
                                "CCI is SUSPENDED; automatic start paused and will "
                                f"retry safely: {start_exc}"
                            )
                        else:
                            message = (
                                f"CCI {result.status.target.app_name} started and ready; "
                                "automatic renewal monitoring resumed"
                            )
                    else:
                        message = (
                            f"CCI {suspended_app} is SUSPENDED; auto-renew is disabled, "
                            "so it was not started"
                        )
                    if message != last_error:
                        self._report(message)
                        last_error = message
                    if stop_event is not None:
                        stop_event.wait(self.poll_interval)
                    else:
                        self._sleep(self.poll_interval)
                    continue
                if isinstance(exc, TargetAmbiguous):
                    raise
                message = str(exc)
                if message != last_error:
                    self._report(f"CCI status temporarily unavailable: {message}")
                    last_error = message
                if stop_event is not None:
                    stop_event.wait(self.poll_interval)
                else:
                    self._sleep(self.poll_interval)
                continue
            last_error = ""
            # A pending operation always wins over the control switch: turning
            # automatic renewal off must not abandon a POST/PATCH whose outcome
            # still needs reconciliation.  Otherwise read the switch every
            # loop so another CLI process can change it without restarting us.
            renewal_pending = bool(self.store.load())
            renewal_enabled = (
                True if renewal_pending else self._auto_renew_enabled(enabled)
            )
            bucket = int(status.age // 600)
            if (
                last_bucket is None
                or bucket != last_bucket
                or (status.due and renewal_enabled)
            ):
                self._report(
                    f"CCI {status.target.app_name}/{status.target.instance_name}: "
                    f"running {format_duration(status.age)}, renew in "
                    f"{format_duration(max(0, status.due_in))}"
                )
                last_bucket = bucket
            # A durable state means a prior POST/PATCH may have lost its
            # response while this very CCI (and therefore the SSH proxy) was
            # restarting.  Resume it even when the newly observed instance is
            # young; waiting for that instance to become due would postpone
            # reconciliation by another default renewal interval.
            if renewal_pending or (status.due and renewal_enabled):
                try:
                    result = self.renew(if_due=True)
                except TargetAmbiguous:
                    raise
                except Exception as exc:
                    # The state machine persists every uncertainty boundary
                    # before a mutation, so retrying here reconciles with GET
                    # and never blindly repeats a snapshot POST or PATCH.
                    if self._transport_is_broken() or self._transport_login_required():
                        raise
                    message = str(exc)
                    if message != last_renewal_error:
                        self._report(
                            f"CCI renewal paused; will retry safely: {message}"
                        )
                        last_renewal_error = message
                    delay = self.poll_interval
                else:
                    last_renewal_error = ""
                    if once and result.action in {"renewed", "recovered"}:
                        return
                    delay = self.poll_interval
            else:
                last_renewal_error = ""
                delay = (
                    self.poll_interval
                    if not renewal_enabled
                    else min(self.poll_interval, max(0.1, status.due_in))
                )
            if stop_event is not None:
                stop_event.wait(delay)
            else:
                self._sleep(delay)


def _find_container_image(app: dict, container_name: str) -> str:
    for container in (app.get("template") or {}).get("containers") or []:
        if _container_name(container) == container_name:
            return str(container.get("image_path") or "")
    return ""
