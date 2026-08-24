"""Read-mostly SenseCore ACP training-job planning.

The ACP UI spans three AEC2 APIs: an existing training job is the template,
workspace bindings describe the eligible resource pools, and each pool
publishes the resource specifications it supports.  This module keeps that
discovery separate from creation.  :meth:`ACPClient.plan` performs GETs only;
the sole mutation is the explicitly named :meth:`ACPClient.submit` method.

No bearer, response body, template body, image, or startup script is included
in an exception.  The browser transport remains the authentication boundary.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import posixpath
import re
import weakref
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from .acp_resources import (
    DEFAULT_RESOURCE_PROFILE_KEY,
    ResourceProfile,
    ResourceProfileError,
    resolve_resource_profile,
)
from .cci import CCIError, DEFAULT_WORKSPACE, WorkspaceRef


ACP_ORIGIN = "https://aec2.cn-sh-01.sensecoreapi.cn"
AEC2_ORIGIN = ACP_ORIGIN
CCR_ORIGIN = "https://ccr.cn-sh-01.sensecoreapi.cn"
MONITOR_ORIGIN = "https://monitor.sensecoreapi.cn"
DEFAULT_ACP_CONSOLE_URL = (
    "https://console.sensecore.cn/cn-sh-01/acp/list/create?workspace="
    "/subscriptions/0197ee17-b6eb-7846-b2b4-a77c5f509b92/"
    "resourceGroups/default/zones/cn-sh-01z/workspaces/share-space-01e"
)
DEFAULT_TEMPLATE_JOB = "example-template-job"
DEFAULT_PAGE_SIZE = 500
SUPPORTED_ACP_ZONE = "cn-sh-01z"
DEFAULT_PORTABLE_FRAMEWORK = "PYTORCH"
DEFAULT_PORTABLE_REPLICAS = 1
DEFAULT_PORTABLE_ENV: Mapping[str, str] = MappingProxyType(
    {
        "NCCL_IB_TIMEOUT": "22",
        "NCCL_IB_RETRY_CNT": "13",
        "NCCL_IB_AR_THRESHOLD": "0",
    }
)

_ACP_DATA_PREFIX = "/compute/acp/data/v2"
_WORKSPACE_DATA_PREFIX = "/compute/workspace/data/v1"
_AEC2_DATA_PREFIX = "/compute/aec2/data/v1"

_JOB_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_RESOURCE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RESOURCE_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ZONE_RE = re.compile(r"[a-z]{2}-[a-z0-9]+-\d{2}[a-z]?\Z")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")

_MAX_REPLICAS = 10000
_MAX_MOUNTS = 64
_MAX_MOUNT_JSON_DEPTH = 8
_MAX_MOUNT_JSON_NODES = 4096
_MAX_MOUNT_JSON_BYTES = 256 * 1024
_MAX_JSON_COLLECTION_ITEMS = 512
_MAX_JSON_KEY_LENGTH = 256
_MAX_JSON_STRING_LENGTH = 64 * 1024
_MAX_MOUNT_PATH_LENGTH = 4096
_MAX_ENV = 256
_MAX_ENV_VALUE_LENGTH = 32 * 1024
_MAX_BARRIER_JSON_BYTES = 16 * 1024

_API_QUOTA_TYPES: Mapping[str, str] = MappingProxyType(
    {"standard": "RESERVED", "spot": "SPOT"}
)
_CAPACITY_BASIS_BY_RESOURCE_CLASS: Mapping[str, str] = MappingProxyType(
    {
        "standard": "reserved_entitlement_without_usage",
        "spot": "current_spot_quota",
    }
)
_KNOWN_BINDING_QUOTA_TYPES = frozenset(
    {"ALL", "RESERVED", "ON_DEMAND", "SPOT"}
)
_BINDING_QUOTA_TYPES_BY_RESOURCE_CLASS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "standard": frozenset({"ALL", "RESERVED"}),
        "spot": frozenset({"ALL", "SPOT"}),
    }
)


class ACPError(CCIError):
    """An ACP response, template, resource choice, or submission was invalid."""


class ACPAPIError(ACPError):
    """A redacted HTTP failure safe to show to a caller."""

    def __init__(self, operation: str, status: int) -> None:
        self.operation = str(operation)
        self.status = int(status)
        super().__init__(f"ACP {self.operation} failed with HTTP {self.status}")


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
class ResourceSpec:
    """One fully identified live AEC2 hardware specification."""

    name: str
    gpu_manufacturer: str
    gpu_model: str
    gpu_memory_gib: float
    gpu_interface: str
    gpu_cards: float
    cpu_manufacturer: str
    cpu_model: str
    cpu_frequency_ghz: float
    vcpus: float
    memory_gib: float

    @property
    def gpu_type(self) -> str:
        memory = _hardware_number(self.gpu_memory_gib)
        return (
            f"{self.gpu_manufacturer} {self.gpu_model}-"
            f"{memory}G-{self.gpu_interface}"
        )

    @property
    def cpu_type(self) -> str:
        frequency = _hardware_decimal(self.cpu_frequency_ghz)
        return f"{self.cpu_manufacturer} {self.cpu_model}-{frequency}GHz"

    # Compatibility read-only aliases for redacted plan renderers.  Callers
    # cannot use them to construct an arbitrary request: selection accepts
    # only a fixed ResourceProfile key.
    @property
    def gpu(self) -> float:
        return self.gpu_cards

    @property
    def cpu(self) -> float:
        return self.vcpus


@dataclass(frozen=True)
class ResourcePoolChoice:
    """An eligible non-debug AEC2 pool for one fixed hardware profile."""

    resource_id: str
    name: str
    vpc_id: str
    zone: str
    profile: ResourceProfile
    resource_class: str
    api_quota_type: str
    spec: ResourceSpec
    capacity_gpu: float
    capacity_cpu: float
    capacity_memory_gib: float
    relative_capacity: float

    @property
    def capacity_basis(self) -> str:
        """Describe what the pool capacity numbers actually represent."""
        try:
            return _CAPACITY_BASIS_BY_RESOURCE_CLASS[self.resource_class]
        except (KeyError, TypeError):
            raise ACPError("invalid ACP resource class") from None


@dataclass(frozen=True)
class _BindingCapacity:
    resource_id: str
    name: str
    vpc_id: str
    zone: str
    gpu: float
    cpu: float
    memory_gib: float


@dataclass(frozen=True, repr=False)
class TrainingJobPlan:
    """A frozen, redacted plan capability; controlled mutation invalidates it."""

    workspace_id: str
    create_url: str
    job_name: str
    pool: ResourcePoolChoice
    worker_replicas: int
    mount_count: int
    env_count: int
    template_job: Optional[str]
    _body: Dict[str, Any] = field(repr=False)

    @property
    def body(self) -> Dict[str, Any]:
        """Return a defensive copy of the whitelisted POST body."""
        return copy.deepcopy(self._body)

    def __repr__(self) -> str:
        return (
            "<TrainingJobPlan "
            f"name={self.job_name!r} pool={self.pool.name!r} "
            f"spec={self.pool.spec.name!r} replicas={self.worker_replicas}>"
        )


@dataclass(frozen=True, repr=False)
class WorkerOverrides:
    """Validated Worker overrides whose representation never exposes values."""

    replicas: Optional[int]
    mounts: Optional[List[Dict[str, Any]]]
    env: Optional[List[Dict[str, str]]]
    barrier: Optional[Dict[str, Any]]

    def __repr__(self) -> str:
        return "<WorkerOverrides redacted>"


def _validate_job_name(value: Any) -> str:
    if not isinstance(value, str) or not _JOB_NAME_RE.fullmatch(value):
        raise ACPError("invalid training job name")
    return value


def _validate_resource_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _RESOURCE_NAME_RE.fullmatch(value):
        raise ACPError(f"invalid {label}")
    return value


def _validate_display_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ACPError("invalid training job display name")
    if not value or len(value) > 128 or any(ord(character) < 32 for character in value):
        raise ACPError("invalid training job display name")
    return value


def _validate_pool_display_name(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 256
        or any(ord(character) < 32 for character in value)
    ):
        raise ACPError("invalid AEC2 display name")
    return value


def _validate_resource_id(value: Any, *, kind: str) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ACPError(f"invalid {kind} resource id")
    if any(character in value for character in ("?", "#", "\\", "%")):
        raise ACPError(f"invalid {kind} resource id")
    parts = value.split("/")[1:]
    if len(parts) < 2 or len(parts) % 2 != 0:
        raise ACPError(f"invalid {kind} resource id")
    if any(not _RESOURCE_SEGMENT_RE.fullmatch(part) for part in parts):
        raise ACPError(f"invalid {kind} resource id")
    expected_collection = {"workspace": "workspaces", "AEC2": "aec2s"}.get(kind)
    if expected_collection is not None and parts[-2] != expected_collection:
        raise ACPError(f"invalid {kind} resource id")
    return "/" + "/".join(parts)


def validate_acp_workspace(value: "str | WorkspaceRef") -> WorkspaceRef:
    """Return one canonical workspace supported by the fixed ACP endpoints."""

    workspace = value if isinstance(value, WorkspaceRef) else WorkspaceRef.parse(value)
    _validate_resource_id(workspace.resource_id, kind="workspace")
    if workspace.zone != SUPPORTED_ACP_ZONE:
        raise ACPError("ACP automation currently supports cn-sh-01 workspaces only")
    return workspace


def _zone_from_resource_id(resource_id: str) -> str:
    """Return the authoritative zone segment from a validated resource id."""
    parts = resource_id.split("/")[1:]
    zones = [
        parts[index + 1]
        for index in range(0, len(parts), 2)
        if parts[index] == "zones"
    ]
    if len(zones) != 1 or not _ZONE_RE.fullmatch(zones[0]):
        raise ACPError("invalid AEC2 zone")
    return zones[0]


def _region_from_zone(zone: str) -> str:
    match = re.fullmatch(r"([a-z]{2}-[a-z0-9]+-\d{2})[a-z]?", zone)
    if match is None:
        raise ACPError("invalid AEC2 zone")
    return match.group(1)


def _validate_origin(value: Any) -> str:
    if not isinstance(value, str):
        raise ACPError("invalid ACP origin")
    selected = value.rstrip("/")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::\d{1,5})?", selected):
        raise ACPError("invalid ACP origin")
    return selected


def _safe_text(value: Any, *, label: str, limit: int, multiline: bool) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or "\x00" in value:
        raise ACPError(f"invalid {label}")
    if not multiline and any(character.isspace() for character in value):
        raise ACPError(f"invalid {label}")
    return value


def _validate_replicas(value: Any) -> int:
    """Return a strict Worker replica count without coercing strings/bools."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > _MAX_REPLICAS
    ):
        raise ACPError("invalid Worker replica count")
    return value


def _bounded_json_copy(
    value: Any,
    *,
    label: str,
    max_depth: int,
    max_nodes: int,
    max_bytes: int,
) -> Any:
    """Copy a JSON value while bounding recursion, fan-out, strings, and size."""
    node_count = 0

    def copy_value(current: Any, depth: int) -> Any:
        nonlocal node_count
        node_count += 1
        if node_count > max_nodes or depth > max_depth:
            raise ACPError(f"invalid {label}")
        if current is None or isinstance(current, bool):
            return current
        if isinstance(current, int):
            return current
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ACPError(f"invalid {label}")
            return current
        if isinstance(current, str):
            if len(current) > _MAX_JSON_STRING_LENGTH or "\x00" in current:
                raise ACPError(f"invalid {label}")
            return current
        if isinstance(current, Mapping):
            try:
                items = list(current.items())
            except Exception:
                raise ACPError(f"invalid {label}") from None
            if len(items) > _MAX_JSON_COLLECTION_ITEMS:
                raise ACPError(f"invalid {label}")
            result: Dict[str, Any] = {}
            for key, nested in items:
                if (
                    not isinstance(key, str)
                    or not key
                    or len(key) > _MAX_JSON_KEY_LENGTH
                    or "\x00" in key
                    or any(ord(character) < 32 for character in key)
                    or key in result
                ):
                    raise ACPError(f"invalid {label}")
                result[key] = copy_value(nested, depth + 1)
            return result
        if isinstance(current, (list, tuple)):
            if len(current) > _MAX_JSON_COLLECTION_ITEMS:
                raise ACPError(f"invalid {label}")
            return [copy_value(item, depth + 1) for item in current]
        raise ACPError(f"invalid {label}")

    try:
        result = copy_value(value, 0)
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except ACPError:
        raise
    except Exception:
        raise ACPError(f"invalid {label}") from None
    if len(encoded) > max_bytes:
        encoded = b""
        result = None
        raise ACPError(f"invalid {label}")
    encoded = b""
    return result


def _normalized_mount_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_MOUNT_PATH_LENGTH
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
        or not value.startswith("/")
        or value.startswith("//")
        or posixpath.normpath(value) != value
    ):
        raise ACPError("invalid mount configuration")
    return value


def _mount_has_dangerous_type(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized_key == "hostpath":
                return True
            if normalized_key in ("type", "mounttype", "volumetype") and isinstance(
                nested, str
            ):
                normalized_value = re.sub(r"[^a-z0-9]", "", nested.lower())
                if (
                    "hostpath" in normalized_value
                    or normalized_value in ("localpath", "bindmount")
                ):
                    return True
            if _mount_has_dangerous_type(nested):
                return True
    elif isinstance(value, list):
        return any(_mount_has_dangerous_type(item) for item in value)
    return False


def _normalize_mounts(value: Any) -> List[Dict[str, Any]]:
    try:
        valid_container = isinstance(value, (list, tuple))
        mount_count = len(value) if valid_container else -1
    except Exception:
        raise ACPError("invalid mount configuration") from None
    if not valid_container or mount_count > _MAX_MOUNTS:
        raise ACPError("invalid mount configuration")
    copied = _bounded_json_copy(
        value,
        label="mount configuration",
        max_depth=_MAX_MOUNT_JSON_DEPTH,
        max_nodes=_MAX_MOUNT_JSON_NODES,
        max_bytes=_MAX_MOUNT_JSON_BYTES,
    )
    if not isinstance(copied, list):  # Defensive: the copier preserves type shape.
        raise ACPError("invalid mount configuration")
    paths = set()
    result: List[Dict[str, Any]] = []
    for entry in copied:
        if not isinstance(entry, dict) or _mount_has_dangerous_type(entry):
            raise ACPError("invalid mount configuration")
        path_keys = [
            key for key in ("mount_path", "path", "mountPath") if key in entry
        ]
        if len(path_keys) != 1:
            raise ACPError("invalid mount configuration")
        mount_path = _normalized_mount_path(entry[path_keys[0]])
        if mount_path in paths:
            raise ACPError("invalid mount configuration")
        paths.add(mount_path)
        result.append(entry)
    return result


def _validate_env_pair(name: Any, value: Any) -> Tuple[str, str]:
    if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(name):
        raise ACPError("invalid environment variables")
    if (
        not isinstance(value, str)
        or len(value) > _MAX_ENV_VALUE_LENGTH
        or "\x00" in value
    ):
        raise ACPError("invalid environment variables")
    return name, value


def _normalize_env(value: Any) -> List[Dict[str, str]]:
    """Normalize mappings and name/value variants to ACP's key/value form."""
    pairs: List[Tuple[Any, Any]] = []
    try:
        if isinstance(value, Mapping):
            if len(value) > _MAX_ENV:
                raise ACPError("invalid environment variables")
            raw_items = list(value.items())
            keys = {key for key, _nested in raw_items if isinstance(key, str)}
            if len(keys) == len(raw_items) and keys in (
                {"key", "value"},
                {"name", "value"},
            ):
                raw = dict(raw_items)
                pairs = [(raw.get("key", raw.get("name")), raw.get("value"))]
            else:
                pairs = raw_items
        elif isinstance(value, (list, tuple)):
            if len(value) > _MAX_ENV:
                raise ACPError("invalid environment variables")
            for entry in value:
                if not isinstance(entry, Mapping):
                    raise ACPError("invalid environment variables")
                raw_items = list(entry.items())
                raw = dict(raw_items)
                if len(raw) != len(raw_items):
                    raise ACPError("invalid environment variables")
                keys = set(raw)
                if keys == {"key", "value"}:
                    pairs.append((raw["key"], raw["value"]))
                elif keys == {"name", "value"}:
                    pairs.append((raw["name"], raw["value"]))
                else:
                    raise ACPError("invalid environment variables")
        else:
            raise ACPError("invalid environment variables")
    except ACPError:
        raise
    except Exception:
        pairs = []
        raise ACPError("invalid environment variables") from None
    if len(pairs) > _MAX_ENV:
        pairs = []
        raise ACPError("invalid environment variables")
    names = set()
    result: List[Dict[str, str]] = []
    for raw_name, raw_value in pairs:
        name, selected_value = _validate_env_pair(raw_name, raw_value)
        if name in names:
            result = []
            pairs = []
            raise ACPError("invalid environment variables")
        names.add(name)
        result.append({"key": name, "value": selected_value})
    pairs = []
    return result


def _normalize_barrier(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ACPError("invalid barrier configuration")
    copied = _bounded_json_copy(
        value,
        label="barrier configuration",
        max_depth=6,
        max_nodes=256,
        max_bytes=_MAX_BARRIER_JSON_BYTES,
    )
    if not isinstance(copied, dict) or not copied:
        raise ACPError("invalid barrier configuration")
    return copied


def normalize_worker_overrides(
    *,
    replicas: Optional[int] = None,
    mounts: Optional[Sequence[Mapping[str, Any]]] = None,
    env: Optional[Any] = None,
    barrier: Optional[Mapping[str, Any]] = None,
) -> WorkerOverrides:
    """Validate and defensively copy portable/template Worker overrides.

    This pure validation boundary is shared by the CLI and :class:`ACPClient`.
    It intentionally preserves ``None`` (inherit/default) versus an empty
    collection (explicitly clear).
    """

    selected_replicas = None if replicas is None else _validate_replicas(replicas)
    selected_mounts = None if mounts is None else _normalize_mounts(mounts)
    selected_env = None if env is None else _normalize_env(env)
    selected_barrier = None if barrier is None else _normalize_barrier(barrier)
    if selected_replicas == 1 and selected_barrier is not None:
        raise ACPError("barrier configuration requires multiple Worker replicas")
    return WorkerOverrides(
        replicas=selected_replicas,
        mounts=selected_mounts,
        env=selected_env,
        barrier=selected_barrier,
    )


def _mapping(value: Any) -> Optional[Mapping[str, Any]]:
    return value if isinstance(value, Mapping) else None


def _path(value: Any, dotted: str) -> Any:
    current = value
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _first(value: Any, paths: Sequence[str]) -> Any:
    for path in paths:
        found = _path(value, path)
        if found is not None:
            return found
    return None


def _unwrap_object(value: Any, preferred: Sequence[str]) -> Mapping[str, Any]:
    current = value
    for _ in range(5):
        if not isinstance(current, Mapping):
            break
        selected = None
        for key in preferred:
            candidate = current.get(key)
            if isinstance(candidate, Mapping):
                selected = candidate
                break
        if selected is None:
            for key in ("data", "result", "payload"):
                candidate = current.get(key)
                if isinstance(candidate, Mapping):
                    selected = candidate
                    break
        if selected is None:
            return current
        current = selected
    if not isinstance(current, Mapping):
        raise ACPError("ACP API returned an invalid object")
    return current


def _unwrap_list(value: Any, preferred: Sequence[str]) -> List[Mapping[str, Any]]:
    current = value
    for _ in range(6):
        if isinstance(current, list):
            if not all(isinstance(item, Mapping) for item in current):
                raise ACPError("ACP API returned an invalid list")
            return list(current)
        if not isinstance(current, Mapping):
            break
        selected = None
        for key in tuple(preferred) + ("items", "results", "list"):
            candidate = current.get(key)
            if isinstance(candidate, (list, Mapping)):
                selected = candidate
                break
        if selected is not None:
            current = selected
            continue
        for key in ("data", "result", "payload"):
            candidate = current.get(key)
            if isinstance(candidate, (list, Mapping)):
                selected = candidate
                break
        if selected is None:
            break
        current = selected
    raise ACPError("ACP API returned an invalid list")


def _quantity(value: Any, *, kind: str) -> float:
    """Normalize CPU cores and memory GiB while accepting common API forms."""
    if isinstance(value, Mapping):
        value = _first(
            value,
            (
                "available",
                "free",
                "allocatable",
                "number",
                "value",
                "count",
                "quota",
            ),
        )
    if isinstance(value, bool) or value is None:
        raise ACPError(f"invalid {kind} quantity")
    if isinstance(value, (int, float)):
        result = float(value)
        if kind == "memory" and result > 1024 * 1024:
            result /= float(1024 ** 3)
    elif isinstance(value, str):
        text = value.strip()
        match = re.fullmatch(r"(\d+(?:\.\d+)?)([A-Za-z]*)", text)
        if match is None:
            raise ACPError(f"invalid {kind} quantity")
        result = float(match.group(1))
        suffix = match.group(2).lower()
        if kind == "cpu":
            if suffix == "m":
                result /= 1000.0
            elif suffix:
                raise ACPError("invalid cpu quantity")
        elif kind == "memory":
            factors = {
                "": None,
                "gi": 1.0,
                "gib": 1.0,
                "mi": 1.0 / 1024.0,
                "mib": 1.0 / 1024.0,
                "ti": 1024.0,
                "tib": 1024.0,
                "g": 1_000_000_000.0 / float(1024 ** 3),
                "gb": 1_000_000_000.0 / float(1024 ** 3),
            }
            if suffix not in factors:
                raise ACPError("invalid memory quantity")
            factor = factors[suffix]
            if factor is not None:
                result *= factor
            elif result > 1024 * 1024:
                result /= float(1024 ** 3)
        elif suffix:
            raise ACPError(f"invalid {kind} quantity")
    else:
        raise ACPError(f"invalid {kind} quantity")
    if not math.isfinite(result) or result < 0:
        raise ACPError(f"invalid {kind} quantity")
    return result


def _same_quantity(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)


def _hardware_number(value: float) -> str:
    """Render one verified integral hardware value without a decimal suffix."""
    rounded = round(float(value))
    if not _same_quantity(float(value), float(rounded)):
        raise ACPError("resource specification has a non-integral hardware value")
    return str(int(rounded))


def _hardware_decimal(value: float) -> str:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ACPError("resource specification has an invalid hardware value")
    return format(number, ".15g")


def _frequency_ghz(value: Any) -> float:
    if isinstance(value, bool):
        raise ACPError("invalid resource specification CPU frequency")
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:GHz)?", value)
        if match is None:
            raise ACPError("invalid resource specification CPU frequency")
        result = float(match.group(1))
    else:
        raise ACPError("invalid resource specification CPU frequency")
    if not math.isfinite(result) or result <= 0:
        raise ACPError("invalid resource specification CPU frequency")
    return result


def _hardware_text(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > 128
        or any(ord(character) < 32 for character in value)
    ):
        raise ACPError(f"invalid resource specification {label}")
    return value


def _spec_attribute(view: Mapping[str, Any], names: Sequence[str]) -> Any:
    """Read nested, dotted-key, mapping, or name/value-list attributes."""
    for name in names:
        if name in view:
            return view[name]
        nested = _path(view, name)
        if nested is not None:
            return nested
    for container_name in ("attributes", "properties", "resources"):
        container = view.get(container_name)
        if isinstance(container, Mapping):
            for name in names:
                if name in container:
                    return container[name]
                nested = _path(container, name)
                if nested is not None:
                    return nested
        elif isinstance(container, list):
            for item in container:
                if not isinstance(item, Mapping):
                    continue
                item_name = _first(item, ("name", "key", "attribute", "property"))
                if item_name not in names:
                    continue
                return _first(
                    item,
                    ("value", "allocatable", "number", "quantity", "count"),
                )
    return None


def _resource_spec(record: Mapping[str, Any]) -> ResourceSpec:
    view = _unwrap_object(record, ("resource_spec", "resourceSpec", "spec"))
    name = _validate_resource_name(
        _first(view, ("name", "resource_spec_name", "resourceSpecName", "code")),
        "resource specification name",
    )
    gpu_cards = _quantity(
        _spec_attribute(
            view,
            (
                "device.number",
                "resources.device.number",
                "attributes.device.number",
                "device",
                "gpu",
                "gpu_count",
            ),
        ),
        kind="GPU",
    )
    gpu_manufacturer = _hardware_text(
        _spec_attribute(
            view,
            (
                "device.manufacturer",
                "resources.device.manufacturer",
                "attributes.device.manufacturer",
            ),
        ),
        label="GPU manufacturer",
    )
    gpu_model = _hardware_text(
        _spec_attribute(
            view,
            (
                "device.type",
                "resources.device.type",
                "attributes.device.type",
            ),
        ),
        label="GPU model",
    )
    gpu_memory = _quantity(
        _spec_attribute(
            view,
            (
                "device.memory",
                "resources.device.memory",
                "attributes.device.memory",
            ),
        ),
        kind="memory",
    )
    gpu_interface = _hardware_text(
        _spec_attribute(
            view,
            (
                "device.physical_interface",
                "device.physicalInterface",
                "resources.device.physical_interface",
                "attributes.device.physical_interface",
            ),
        ),
        label="GPU physical interface",
    )
    vcpus = _quantity(
        _spec_attribute(
            view,
            (
                "cpu.vcpu_allocatable",
                "resources.cpu.vcpu_allocatable",
                "attributes.cpu.vcpu_allocatable",
                "cpu",
                "vcpu",
            ),
        ),
        kind="cpu",
    )
    cpu_manufacturer = _hardware_text(
        _spec_attribute(
            view,
            (
                "cpu.manufacturer",
                "resources.cpu.manufacturer",
                "attributes.cpu.manufacturer",
            ),
        ),
        label="CPU manufacturer",
    )
    cpu_model = _hardware_text(
        _spec_attribute(
            view,
            (
                "cpu.type",
                "resources.cpu.type",
                "attributes.cpu.type",
            ),
        ),
        label="CPU model",
    )
    cpu_frequency = _frequency_ghz(
        _spec_attribute(
            view,
            (
                "cpu.frequency",
                "resources.cpu.frequency",
                "attributes.cpu.frequency",
            ),
        ),
    )
    memory = _quantity(
        _spec_attribute(
            view,
            (
                "memory.allocatable",
                "resources.memory.allocatable",
                "attributes.memory.allocatable",
                "memory",
                "memory_gib",
            ),
        ),
        kind="memory",
    )
    # AEC2 returns CPU-only specifications in the same list with device.number
    # equal to zero.  They are valid records, just not candidates for this
    # GPU-job interface.  Negative GPU or non-positive CPU/memory remains an
    # invalid schema.
    if gpu_cards <= 0 or gpu_memory <= 0 or vcpus <= 0 or memory <= 0:
        raise ACPError("resource specification has non-positive capacity")
    return ResourceSpec(
        name=name,
        gpu_manufacturer=gpu_manufacturer,
        gpu_model=gpu_model,
        gpu_memory_gib=gpu_memory,
        gpu_interface=gpu_interface,
        gpu_cards=gpu_cards,
        cpu_manufacturer=cpu_manufacturer,
        cpu_model=cpu_model,
        cpu_frequency_ghz=cpu_frequency,
        vcpus=vcpus,
        memory_gib=memory,
    )


def _matches_profile(spec: ResourceSpec, profile: ResourceProfile) -> bool:
    """Require the live specification to equal every catalogued field."""
    return (
        spec.name == profile.spec_name
        and spec.gpu_type == profile.gpu_type
        and _same_quantity(spec.gpu_cards, float(profile.gpu_cards))
        and spec.cpu_type == profile.cpu_type
        and _same_quantity(spec.vcpus, float(profile.vcpus))
        and _same_quantity(spec.memory_gib, float(profile.memory_gib))
    )


def _json_copy(value: Any, *, label: str) -> Any:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        return json.loads(encoded)
    except (TypeError, ValueError, OverflowError):
        raise ACPError(f"template has invalid {label}") from None


def _copy_if_present(
    output: Dict[str, Any],
    source: Mapping[str, Any],
    key: str,
) -> None:
    if key in source and source[key] is not None:
        output[key] = _json_copy(source[key], label=key)


class ACPClient:
    """Schema-tolerant ACP client with an explicit GET-plan/POST-submit split."""

    def __init__(
        self,
        transport: JsonTransport,
        workspace: "str | WorkspaceRef" = DEFAULT_WORKSPACE,
        *,
        origin: str = ACP_ORIGIN,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> None:
        self.transport = transport
        self.workspace = validate_acp_workspace(workspace)
        # Re-validate the canonical form before concatenating it into trusted paths.
        self.workspace_id = _validate_resource_id(
            self.workspace.resource_id, kind="workspace"
        )
        self.origin = _validate_origin(origin)
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 1000:
            raise ACPError("page size must be between 1 and 1000")
        self.page_size = page_size
        # Plans are intentionally process-local, single-use capabilities.  The
        # fingerprint catches mutation of frozen dataclass internals, while
        # object identity rejects caller-constructed replacement plans.
        self._issued_plans: Dict[
            int,
            Tuple[weakref.ReferenceType[TrainingJobPlan], str],
        ] = {}

    @property
    def create_url(self) -> str:
        return (
            self.origin
            + _ACP_DATA_PREFIX
            + self.workspace_id
            + "/trainingJobs"
        )

    def template_job_id(self, template_job: str = DEFAULT_TEMPLATE_JOB) -> str:
        name = _validate_job_name(template_job)
        return self.workspace_id + "/trainingJobs/" + name

    def _request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> Any:
        method = str(method).upper()

        def perform() -> Any:
            failed = False
            result: Any = None
            try:
                result = self.transport.request(
                    method,
                    url,
                    params=params,
                    json_body=body,
                    headers={"x-ui-valid": "x-ui-valid"},
                    timeout=timeout,
                )
            except Exception:
                # Raise outside the except suite so a transport exception that
                # embeds a bearer or response body is not retained as context.
                failed = True
            if failed:
                result = None
                raise ACPError(f"ACP {operation} request failed")
            return result

        try:
            response = perform()
        except ACPError:
            body = None
            raise
        if (
            method == "GET"
            and getattr(response, "status", None) == 401
            and callable(getattr(self.transport, "refresh_auth", None))
        ):
            try:
                self.transport.refresh_auth(timeout=min(timeout, 60.0))
            except Exception:
                refresh_failed = True
            else:
                refresh_failed = False
            if refresh_failed:
                response = None
                body = None
                raise ACPError(f"ACP {operation} authentication refresh failed")
            try:
                response = perform()
            except ACPError:
                body = None
                raise

        if hasattr(response, "status") and hasattr(response, "text"):
            invalid_status = False
            try:
                status = int(response.status)
            except (TypeError, ValueError, OverflowError):
                invalid_status = True
                status = 0
            if invalid_status:
                response = None
                body = None
                raise ACPError(f"ACP {operation} returned an invalid status")
            if status < 200 or status >= 300:
                response = None
                body = None
                raise ACPAPIError(operation, status)
            text = response.text
            response = None
            body = None
            if not isinstance(text, str):
                raise ACPError(f"ACP {operation} returned an invalid response")
            if not text.strip():
                return {}
            decode_failed = False
            decoded: Any = None
            try:
                decoded = json.loads(text)
            except ValueError:
                decode_failed = True
            if decode_failed:
                text = ""
                raise ACPError(f"ACP {operation} returned invalid JSON")
            return decoded
        return response

    def get_template_job(self, template_job: str = DEFAULT_TEMPLATE_JOB) -> Mapping[str, Any]:
        job_id = self.template_job_id(template_job)
        data = self._request(
            "GET",
            self.origin + _ACP_DATA_PREFIX + job_id,
            operation="template job lookup",
        )
        return _unwrap_object(data, ("training_job", "trainingJob", "job"))

    def list_workers(
        self, template_job: str = DEFAULT_TEMPLATE_JOB
    ) -> List[Mapping[str, Any]]:
        """Return runtime worker pods for logs/events; never a POST template."""
        job_id = self.template_job_id(template_job)
        data = self._request(
            "GET",
            self.origin + _ACP_DATA_PREFIX + job_id + "/workers",
            operation="template workers lookup",
            params={"page_size": self.page_size, "page_token": 1},
        )
        return _unwrap_list(data, ("workers", "roles"))

    # Compatibility name retained for callers that identify workers by their
    # source job.  Planning intentionally never calls either spelling.
    list_template_workers = list_workers

    def list_workspace_bindings(self) -> List[Mapping[str, Any]]:
        data = self._request(
            "GET",
            self.origin
            + _WORKSPACE_DATA_PREFIX
            + self.workspace_id
            + "/workspaceAEC2Bindings",
            operation="workspace bindings lookup",
        )
        return _unwrap_list(data, ("aec2s", "workspace_aec2_bindings", "bindings"))

    # Short alias matching the API noun used by callers.
    list_bindings = list_workspace_bindings

    def list_resource_specs(
        self,
        aec2_id: str,
        *,
        expected_profile: Optional[ResourceProfile] = None,
    ) -> List[ResourceSpec]:
        resource_id = _validate_resource_id(aec2_id, kind="AEC2")
        data = self._request(
            "GET",
            self.origin + _AEC2_DATA_PREFIX + resource_id + "/resourceSpecs",
            operation="resource specifications lookup",
        )
        records = _unwrap_list(
            data,
            ("resource_specs", "resourceSpecs", "specs"),
        )
        # ACTIVE bindings may legitimately expose an empty specification
        # list while a pool is unavailable for containers.  It contributes no
        # candidate.
        if not records:
            return []
        specs: List[ResourceSpec] = []
        selected_count = 0
        for record in records:
            view = _unwrap_object(record, ("resource_spec", "resourceSpec", "spec"))
            raw_name = _first(
                view,
                ("name", "resource_spec_name", "resourceSpecName", "code"),
            )
            is_selected = (
                expected_profile is not None
                and raw_name == expected_profile.spec_name
            )
            if is_selected:
                selected_count += 1
                if selected_count > 1:
                    raise ACPError(
                        "AEC2 returned an ambiguous selected resource specification"
                    )
            try:
                spec = _resource_spec(record)
            except ACPError:
                if is_selected:
                    raise ACPError(
                        "selected resource specification has an invalid "
                        "hardware schema"
                    ) from None
                # Forward-compatible lists may contain CPU-only or unrelated
                # entries.  They cannot enter this fixed GPU-profile library.
                continue
            if is_selected and not _matches_profile(spec, expected_profile):
                raise ACPError(
                    "selected resource specification does not match the fixed "
                    "hardware profile"
                )
            specs.append(spec)
        if not specs and expected_profile is None:
            raise ACPError("AEC2 returned no usable resource specifications")
        return specs

    @staticmethod
    def _binding_value(
        record: Mapping[str, Any],
        view: Mapping[str, Any],
        paths: Sequence[str],
    ) -> Any:
        value = _first(view, paths)
        if value is None and view is not record:
            value = _first(record, paths)
        return value

    @staticmethod
    def _is_debug_pool(*values: Any) -> bool:
        # The live API currently leaves the structured ``type`` empty even for
        # debug pools.  Identity fields are therefore the only available
        # exclusion signal.  A broad substring deny rule is deliberate: a
        # false positive merely removes capacity, while a false negative could
        # submit a training job to a forbidden debug cluster.
        return any(
            isinstance(value, str) and "debug" in value.casefold()
            for value in values
        )

    @classmethod
    def _active_binding_capacity(
        cls,
        record: Mapping[str, Any],
        *,
        resource_class: str,
    ) -> Optional[_BindingCapacity]:
        view = _unwrap_object(record, ("aec2", "resource_pool", "resourcePool"))
        value = lambda paths: cls._binding_value(record, view, paths)

        state = value(("state", "status", "aec2_state"))
        if not isinstance(state, str) or state.casefold() != "active":
            return None

        # A nested AEC2 object is the authoritative source for selection, but
        # it must not be allowed to hide a Debug marker on the outer binding.
        # Inspect every supported identity alias on both objects before using
        # the preferred values below.
        identity_paths = (
            "id",
            "resource_id",
            "resourceId",
            "name",
            "aec2_name",
            "display_name",
            "displayName",
            "type",
            "aec2_type",
            "aec2Type",
        )
        identity_values = [
            candidate
            for source in (record, view)
            for path in identity_paths
            if (candidate := _path(source, path)) is not None
        ]
        if cls._is_debug_pool(*identity_values):
            return None

        raw_id = value(("id", "resource_id", "resourceId"))
        raw_name = value(("name", "aec2_name"))
        raw_display_name = value(("display_name", "displayName"))
        raw_type = value(("type", "aec2_type", "aec2Type"))

        quota_type = value(("quota_type", "quotaType"))
        if (
            not isinstance(quota_type, str)
            or quota_type not in _KNOWN_BINDING_QUOTA_TYPES
        ):
            raise ACPError("AEC2 binding has an invalid quota type")
        allowed_quota_types = _BINDING_QUOTA_TYPES_BY_RESOURCE_CLASS[resource_class]
        if quota_type not in allowed_quota_types:
            return None

        if resource_class == "spot":
            spot_status = value(("spot_status", "spotStatus"))
            if spot_status is None or spot_status == []:
                return None
            if not isinstance(spot_status, list):
                raise ACPError("AEC2 binding has an invalid SPOT quota status")
            if len(spot_status) != 1:
                raise ACPError("AEC2 binding has ambiguous SPOT quota status")
            status = spot_status[0]
            if not isinstance(status, Mapping):
                raise ACPError("AEC2 binding has an invalid SPOT quota status")
            # The live API currently publishes one named bucket.  Do not
            # silently consume a future/unknown bucket: its quota semantics
            # may differ from the Console's ordinary idle-resource choice.
            spot_names = [
                status[key]
                for key in ("spot_name", "spotName")
                if key in status
            ]
            if not spot_names or any(name != spot_names[0] for name in spot_names[1:]):
                raise ACPError("AEC2 binding has an invalid SPOT quota name")
            spot_name = spot_names[0]
            if type(spot_name) is not str or spot_name != "default":
                raise ACPError("AEC2 binding has an invalid SPOT quota name")
            quota = _first(status, ("spot_quota", "spotQuota"))
            if not isinstance(quota, Mapping):
                raise ACPError("AEC2 binding has an invalid SPOT quota")
            raw_gpu = _first(quota, ("device", "gpu", "gpu_count"))
            raw_cpu = _first(quota, ("cpu", "vcpu"))
            raw_memory = _first(quota, ("memory", "memory_gib"))
            if any(item is None for item in (raw_gpu, raw_cpu, raw_memory)):
                raise ACPError("AEC2 binding has an incomplete SPOT quota")
        else:
            raw_gpu = value(("reserved_number", "reservedNumber"))
            raw_cpu = value(("reserved_cpu", "reservedCpu"))
            raw_memory = value(("reserved_memory", "reservedMemory"))
            reserved = (raw_gpu, raw_cpu, raw_memory)
            if all(item is None for item in reserved):
                return None
            if any(item is None for item in reserved):
                raise ACPError("AEC2 binding has incomplete standard capacity")

        resource_id = _validate_resource_id(raw_id, kind="AEC2")
        name = _validate_resource_name(raw_name, "AEC2 name")
        _validate_pool_display_name(raw_display_name)
        if raw_type is not None and not isinstance(raw_type, str):
            raise ACPError("invalid AEC2 type")
        vpc_id = _validate_resource_name(
            value(("vpc_id", "vpcId", "vpc.name")), "VPC id"
        )
        # Display-oriented zone fields have changed shape.  The validated id
        # is the authoritative source used by the create API.
        zone = _zone_from_resource_id(resource_id)
        return _BindingCapacity(
            resource_id=resource_id,
            name=name,
            vpc_id=vpc_id,
            zone=zone,
            gpu=_quantity(raw_gpu, kind="GPU"),
            cpu=_quantity(raw_cpu, kind="cpu"),
            memory_gib=_quantity(raw_memory, kind="memory"),
        )

    def select_resource_pool(
        self,
        bindings: Sequence[Mapping[str, Any]],
        profile: ResourceProfile,
        *,
        resource_class: str,
        replicas: int = DEFAULT_PORTABLE_REPLICAS,
    ) -> ResourcePoolChoice:
        if not isinstance(profile, ResourceProfile):
            raise ACPError("invalid ACP resource profile")
        try:
            catalog_profile = resolve_resource_profile(
                profile.key,
                resource_class=resource_class,
            )
        except ResourceProfileError as exc:
            raise ACPError(str(exc)) from None
        if profile != catalog_profile:
            raise ACPError("invalid ACP resource profile")
        profile = catalog_profile
        required_replicas = _validate_replicas(replicas)
        candidates: List[ResourcePoolChoice] = []
        for binding in bindings:
            if not isinstance(binding, Mapping):
                continue
            # Debug is excluded before resource-spec discovery.  An ACTIVE
            # entry that claims the selected resource class must otherwise
            # have a complete identity/network/capacity shape; malformed
            # candidates fail closed rather than silently selecting a fallback.
            selected = self._active_binding_capacity(
                binding,
                resource_class=resource_class,
            )
            if selected is None:
                continue
            if _region_from_zone(selected.zone) != self.workspace.region:
                raise ACPError("AEC2 resource pool belongs to a different region")
            specs = self.list_resource_specs(
                selected.resource_id,
                expected_profile=profile,
            )
            matching = [spec for spec in specs if _matches_profile(spec, profile)]
            if not matching:
                continue
            if len(matching) != 1:
                raise ACPError("AEC2 returned an ambiguous resource specification")
            spec = matching[0]
            ratios = (
                selected.gpu / float(profile.gpu_cards),
                selected.cpu / float(profile.vcpus),
                selected.memory_gib / float(profile.memory_gib),
            )
            relative_capacity = min(ratios)
            if relative_capacity < float(required_replicas):
                continue
            candidates.append(
                ResourcePoolChoice(
                    resource_id=selected.resource_id,
                    name=selected.name,
                    vpc_id=selected.vpc_id,
                    zone=selected.zone,
                    profile=profile,
                    resource_class=resource_class,
                    api_quota_type=_API_QUOTA_TYPES[resource_class],
                    spec=spec,
                    capacity_gpu=selected.gpu,
                    capacity_cpu=selected.cpu,
                    capacity_memory_gib=selected.memory_gib,
                    relative_capacity=relative_capacity,
                )
            )
        if not candidates:
            label = _API_QUOTA_TYPES[resource_class]
            raise ACPError(
                f"no ACTIVE {label} resource pool can satisfy the selected "
                "profile capacity"
            )
        candidates.sort(
            key=lambda item: (-item.relative_capacity, item.name, item.resource_id)
        )
        return candidates[0]

    @staticmethod
    def _template_role(job: Mapping[str, Any]) -> Mapping[str, Any]:
        candidates: List[Mapping[str, Any]] = []
        embedded = _first(job, ("roles", "detail.roles"))
        if isinstance(embedded, list):
            for role in embedded:
                if not isinstance(role, Mapping):
                    continue
                view = _unwrap_object(role, ("role", "spec", "template"))
                role_name = _first(view, ("name", "role_name", "roleName"))
                if role_name in ("Worker", "worker"):
                    candidates.append(view)
        if len(candidates) != 1:
            raise ACPError("template must contain exactly one Worker role")
        return candidates[0]

    @staticmethod
    def _template_source(job: Mapping[str, Any]) -> Mapping[str, Any]:
        source = _unwrap_object(job, ("training_job", "trainingJob", "job", "spec"))
        detail = _mapping(source.get("detail"))
        if detail is None:
            return source
        merged_source: Dict[str, Any] = dict(source)
        merged_source.update(detail)
        return merged_source

    @staticmethod
    def _build_body(
        source: Optional[Mapping[str, Any]],
        *,
        role_source: Optional[Mapping[str, Any]],
        job_name: str,
        display_name: str,
        image: Optional[str],
        startup: Optional[str],
        replicas: int,
        mounts: Sequence[Mapping[str, Any]],
        include_mounts: bool,
        env: Sequence[Mapping[str, str]],
        include_env: bool,
        barrier: Optional[Mapping[str, Any]],
        pool: ResourcePoolChoice,
    ) -> Dict[str, Any]:
        source_view: Mapping[str, Any] = source if source is not None else {}
        source_image = (
            _first(
                role_source,
                ("image_path", "imagePath", "image", "container.image_path"),
            )
            if role_source is not None
            else None
        )
        selected_image = source_image if image is None else image
        selected_image = _safe_text(
            selected_image, label="container image", limit=2048, multiline=False
        )
        source_startup = (
            _first(
                role_source,
                ("startup_script", "startupScript", "startup", "command"),
            )
            if role_source is not None
            else None
        )
        selected_startup = source_startup if startup is None else startup
        selected_startup = _safe_text(
            selected_startup, label="startup script", limit=65536, multiline=True
        )

        body: Dict[str, Any] = {}
        _copy_if_present(body, source_view, "metadata")
        body["display_name"] = display_name
        body["name"] = job_name
        if source is None:
            body["framework"] = DEFAULT_PORTABLE_FRAMEWORK
        else:
            _copy_if_present(body, source_view, "framework")
        body["roles"] = [
            {
                "name": "Worker",
                "resource_spec": [{"name": pool.spec.name}],
                "total_replicas": replicas,
                "startup_script": selected_startup,
                "image_path": selected_image,
            }
        ]
        if include_mounts:
            body["mount"] = copy.deepcopy(list(mounts))

        tensorboard = _mapping(source_view.get("tensorboard"))
        if tensorboard is not None and tensorboard.get("log_path") is not None:
            body["tensorboard"] = {
                "log_path": _json_copy(tensorboard["log_path"], label="tensorboard")
            }
        checkpoint = _mapping(source_view.get("async_checkpoint"))
        if checkpoint is not None and checkpoint.get("max_ckpt_rounds") is not None:
            body["async_checkpoint"] = {
                "max_ckpt_rounds": _json_copy(
                    checkpoint["max_ckpt_rounds"], label="async checkpoint"
                )
            }
        lme_source = _mapping(source_view.get("lme"))
        if lme_source is not None:
            lme: Dict[str, Any] = {}
            for key in (
                "enable_warmingup",
                "enable_checker",
                "enable_health_monitor",
                "max_retries",
            ):
                _copy_if_present(lme, lme_source, key)
            if lme:
                body["lme"] = lme
        if include_env:
            body["env"] = copy.deepcopy(list(env))

        scheduling_source = _mapping(source_view.get("scheduling")) or {}
        # Never inherit a template's quota class.  The controlled profile
        # selection maps standard -> RESERVED and spot -> SPOT.
        scheduling: Dict[str, Any] = {"quota_type": pool.api_quota_type}
        _copy_if_present(scheduling, scheduling_source, "priority")
        scoring_source = _mapping(scheduling_source.get("scoring_strategy"))
        if scoring_source is not None and scoring_source.get("type") is not None:
            scheduling["scoring_strategy"] = {
                "type": _json_copy(scoring_source["type"], label="scoring strategy")
            }
        body["scheduling"] = scheduling
        body["resource_pool"] = {
            "name": pool.name,
            "vpc_id": pool.vpc_id,
            "zone": pool.zone,
        }

        ssh_source = _mapping(source_view.get("ssh"))
        if ssh_source is not None:
            ssh: Dict[str, Any] = {}
            for key in ("auto_key_setup", "config_mount_path"):
                _copy_if_present(ssh, ssh_source, key)
            if ssh:
                body["ssh"] = ssh
        _copy_if_present(body, source_view, "fault_tolerance")
        if replicas > 1 and barrier is not None:
            body["barrier"] = copy.deepcopy(dict(barrier))
        return body

    @staticmethod
    def _plan_fingerprint(plan: TrainingJobPlan) -> str:
        """Hash the complete issued plan without exposing sensitive values."""
        pool = plan.pool
        profile = pool.profile
        spec = pool.spec
        controlled = {
            "workspace_id": plan.workspace_id,
            "create_url": plan.create_url,
            "job_name": plan.job_name,
            "worker_replicas": plan.worker_replicas,
            "mount_count": plan.mount_count,
            "env_count": plan.env_count,
            "template_job": plan.template_job,
            "pool": {
                "resource_id": pool.resource_id,
                "name": pool.name,
                "vpc_id": pool.vpc_id,
                "zone": pool.zone,
                "resource_class": pool.resource_class,
                "api_quota_type": pool.api_quota_type,
                "capacity_basis": pool.capacity_basis,
                "capacity_gpu": pool.capacity_gpu,
                "capacity_cpu": pool.capacity_cpu,
                "capacity_memory_gib": pool.capacity_memory_gib,
                "relative_capacity": pool.relative_capacity,
                "profile": {
                    "key": profile.key,
                    "spec_name": profile.spec_name,
                    "gpu_type": profile.gpu_type,
                    "gpu_cards": profile.gpu_cards,
                    "cpu_type": profile.cpu_type,
                    "vcpus": profile.vcpus,
                    "memory_gib": profile.memory_gib,
                    "classes": sorted(profile.classes),
                },
                "spec": {
                    "name": spec.name,
                    "gpu_manufacturer": spec.gpu_manufacturer,
                    "gpu_model": spec.gpu_model,
                    "gpu_memory_gib": spec.gpu_memory_gib,
                    "gpu_interface": spec.gpu_interface,
                    "gpu_cards": spec.gpu_cards,
                    "cpu_manufacturer": spec.cpu_manufacturer,
                    "cpu_model": spec.cpu_model,
                    "cpu_frequency_ghz": spec.cpu_frequency_ghz,
                    "vcpus": spec.vcpus,
                    "memory_gib": spec.memory_gib,
                },
            },
            "body": plan.body,
        }
        encoded = json.dumps(
            controlled,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def plan(
        self,
        *,
        name: str,
        display_name: Optional[str] = None,
        image: Optional[str] = None,
        startup: Optional[str] = None,
        resource_profile: str = DEFAULT_RESOURCE_PROFILE_KEY,
        resource_class: str = "spot",
        replicas: Optional[int] = None,
        mounts: Optional[Sequence[Mapping[str, Any]]] = None,
        env: Optional[Any] = None,
        barrier: Optional[Mapping[str, Any]] = None,
        template_job: Optional[str] = DEFAULT_TEMPLATE_JOB,
    ) -> TrainingJobPlan:
        """Build a whitelisted training-job plan using GET requests only."""
        selected_name = _validate_job_name(name)
        selected_display = _validate_display_name(
            selected_name if display_name is None else display_name
        )
        try:
            profile = resolve_resource_profile(
                resource_profile,
                resource_class=resource_class,
            )
        except ResourceProfileError as exc:
            raise ACPError(str(exc)) from None
        worker_overrides = normalize_worker_overrides(
            replicas=replicas,
            mounts=mounts,
            env=env,
            barrier=barrier,
        )
        explicit_replicas = worker_overrides.replicas
        mount_override = worker_overrides.mounts
        env_override = worker_overrides.env
        barrier_override = worker_overrides.barrier

        selected_template_job: Optional[str] = None
        source: Optional[Mapping[str, Any]] = None
        role_source: Optional[Mapping[str, Any]] = None
        if template_job is not None:
            selected_template_job = _validate_job_name(template_job)
            job = self.get_template_job(selected_template_job)
            source = self._template_source(job)
            role_source = self._template_role(source)

        if explicit_replicas is not None:
            selected_replicas = explicit_replicas
        elif role_source is not None:
            selected_replicas = _validate_replicas(
                _first(role_source, ("total_replicas", "totalReplicas", "replicas"))
            )
        else:
            selected_replicas = DEFAULT_PORTABLE_REPLICAS

        if barrier_override is not None and selected_replicas == 1:
            raise ACPError("barrier configuration requires multiple Worker replicas")

        if mount_override is not None:
            selected_mounts = mount_override
            include_mounts = True
        elif source is None:
            selected_mounts = []
            include_mounts = True
        elif source.get("mount") is not None:
            selected_mounts = _normalize_mounts(source["mount"])
            include_mounts = True
        else:
            selected_mounts = []
            include_mounts = False

        if env_override is not None:
            selected_env = env_override
            include_env = True
        elif source is None:
            selected_env = _normalize_env(DEFAULT_PORTABLE_ENV)
            include_env = True
        elif source.get("env") is not None:
            selected_env = _normalize_env(source["env"])
            include_env = True
        else:
            selected_env = []
            include_env = False

        selected_barrier: Optional[Dict[str, Any]] = None
        if selected_replicas > 1:
            if barrier_override is not None:
                selected_barrier = barrier_override
            elif source is not None and source.get("barrier") is not None:
                selected_barrier = _normalize_barrier(source["barrier"])
            else:
                raise ACPError("multi-replica job has no barrier configuration")

        bindings = self.list_workspace_bindings()
        pool = self.select_resource_pool(
            bindings,
            profile,
            resource_class=resource_class,
            replicas=selected_replicas,
        )
        body = self._build_body(
            source,
            role_source=role_source,
            job_name=selected_name,
            display_name=selected_display,
            image=image,
            startup=startup,
            replicas=selected_replicas,
            mounts=selected_mounts,
            include_mounts=include_mounts,
            env=selected_env,
            include_env=include_env,
            barrier=selected_barrier,
            pool=pool,
        )
        selected_plan = TrainingJobPlan(
            workspace_id=self.workspace_id,
            create_url=self.create_url,
            job_name=selected_name,
            pool=pool,
            worker_replicas=selected_replicas,
            mount_count=len(selected_mounts),
            env_count=len(selected_env),
            template_job=selected_template_job,
            _body=body,
        )
        fingerprint = self._plan_fingerprint(selected_plan)
        plan_id = id(selected_plan)
        client_ref = weakref.ref(self)

        def release_plan(
            plan_ref: weakref.ReferenceType[TrainingJobPlan],
            *,
            issued_id: int = plan_id,
            owner_ref: weakref.ReferenceType[ACPClient] = client_ref,
        ) -> None:
            owner = owner_ref()
            if owner is None:
                return
            issued = owner._issued_plans.get(issued_id)
            # A delayed callback must not erase a newer plan whose object id was
            # reused after this plan died.
            if issued is not None and issued[0] is plan_ref:
                owner._issued_plans.pop(issued_id, None)

        selected_ref = weakref.ref(selected_plan, release_plan)
        self._issued_plans[plan_id] = (selected_ref, fingerprint)
        return selected_plan

    plan_training_job = plan

    def _validated_submit_body(self, plan: TrainingJobPlan) -> Dict[str, Any]:
        """Revalidate every controlled plan field at the mutation boundary."""

        def capacity_number(value: Any) -> float:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ACPError("invalid training job plan")
            return float(value)

        try:
            if (
                plan.workspace_id != self.workspace_id
                or plan.create_url != self.create_url
                or plan.job_name != _validate_job_name(plan.job_name)
            ):
                raise ACPError("invalid training job plan")
            if plan.template_job is not None:
                _validate_job_name(plan.template_job)
            replicas = _validate_replicas(plan.worker_replicas)
            if (
                type(plan.mount_count) is not int
                or not 0 <= plan.mount_count <= _MAX_MOUNTS
                or type(plan.env_count) is not int
                or not 0 <= plan.env_count <= _MAX_ENV
            ):
                raise ACPError("invalid training job plan")

            pool = plan.pool
            if type(pool) is not ResourcePoolChoice:
                raise ACPError("invalid training job plan")
            if type(pool.profile) is not ResourceProfile:
                raise ACPError("invalid training job plan")
            catalog_profile = resolve_resource_profile(
                pool.profile.key,
                resource_class=pool.resource_class,
            )
            # Selection always stores the canonical catalogue object.  An
            # equal-looking caller-created profile is not trusted provenance.
            if pool.profile is not catalog_profile:
                raise ACPError("invalid training job plan")
            expected_quota_type = _API_QUOTA_TYPES[pool.resource_class]
            if (
                pool.api_quota_type != expected_quota_type
                or pool.capacity_basis
                != _CAPACITY_BASIS_BY_RESOURCE_CLASS[pool.resource_class]
                or type(pool.spec) is not ResourceSpec
                or not _matches_profile(pool.spec, catalog_profile)
            ):
                raise ACPError("invalid training job plan")

            resource_id = _validate_resource_id(pool.resource_id, kind="AEC2")
            pool_name = _validate_resource_name(pool.name, "AEC2 name")
            vpc_id = _validate_resource_name(pool.vpc_id, "VPC id")
            zone = _zone_from_resource_id(resource_id)
            if (
                pool.zone != zone
                or _region_from_zone(zone) != self.workspace.region
                or self._is_debug_pool(resource_id, pool_name)
            ):
                raise ACPError("invalid training job plan")

            capacity_gpu = capacity_number(pool.capacity_gpu)
            capacity_cpu = capacity_number(pool.capacity_cpu)
            capacity_memory = capacity_number(pool.capacity_memory_gib)
            relative_capacity = capacity_number(pool.relative_capacity)
            expected_relative_capacity = min(
                capacity_gpu / float(catalog_profile.gpu_cards),
                capacity_cpu / float(catalog_profile.vcpus),
                capacity_memory / float(catalog_profile.memory_gib),
            )
            if (
                not _same_quantity(relative_capacity, expected_relative_capacity)
                or relative_capacity < float(replicas)
            ):
                raise ACPError("invalid training job plan")

            body = plan.body
            allowed_body_keys = frozenset(
                {
                    "metadata",
                    "display_name",
                    "name",
                    "framework",
                    "roles",
                    "mount",
                    "tensorboard",
                    "async_checkpoint",
                    "lme",
                    "env",
                    "scheduling",
                    "resource_pool",
                    "ssh",
                    "fault_tolerance",
                    "barrier",
                }
            )
            required_body_keys = frozenset(
                {"display_name", "name", "roles", "scheduling", "resource_pool"}
            )
            if (
                type(body) is not dict
                or not required_body_keys.issubset(body)
                or not set(body).issubset(allowed_body_keys)
                or body["name"] != plan.job_name
            ):
                raise ACPError("invalid training job plan")
            _validate_display_name(body["display_name"])

            roles = body["roles"]
            if type(roles) is not list or len(roles) != 1 or type(roles[0]) is not dict:
                raise ACPError("invalid training job plan")
            role = roles[0]
            if set(role) != {
                "name",
                "resource_spec",
                "total_replicas",
                "startup_script",
                "image_path",
            }:
                raise ACPError("invalid training job plan")
            resource_specs = role["resource_spec"]
            if (
                role["name"] != "Worker"
                or _validate_replicas(role["total_replicas"]) != replicas
                or type(resource_specs) is not list
                or len(resource_specs) != 1
                or type(resource_specs[0]) is not dict
                or resource_specs[0] != {"name": catalog_profile.spec_name}
            ):
                raise ACPError("invalid training job plan")
            _safe_text(
                role["startup_script"],
                label="startup script",
                limit=65536,
                multiline=True,
            )
            _safe_text(
                role["image_path"],
                label="container image",
                limit=2048,
                multiline=False,
            )

            scheduling = body["scheduling"]
            if (
                type(scheduling) is not dict
                or "quota_type" not in scheduling
                or not set(scheduling).issubset(
                    {"quota_type", "priority", "scoring_strategy"}
                )
                or scheduling["quota_type"] != expected_quota_type
            ):
                raise ACPError("invalid training job plan")
            if "scoring_strategy" in scheduling:
                scoring = scheduling["scoring_strategy"]
                if type(scoring) is not dict or set(scoring) != {"type"}:
                    raise ACPError("invalid training job plan")

            expected_pool_body = {
                "name": pool_name,
                "vpc_id": vpc_id,
                "zone": zone,
            }
            if (
                type(body["resource_pool"]) is not dict
                or body["resource_pool"] != expected_pool_body
            ):
                raise ACPError("invalid training job plan")

            if "mount" in body:
                mounts = body["mount"]
                if (
                    type(mounts) is not list
                    or any(type(item) is not dict for item in mounts)
                    or _normalize_mounts(mounts) != mounts
                    or len(mounts) != plan.mount_count
                ):
                    raise ACPError("invalid training job plan")
            elif plan.mount_count != 0:
                raise ACPError("invalid training job plan")

            if "env" in body:
                environment = body["env"]
                if (
                    type(environment) is not list
                    or any(type(item) is not dict for item in environment)
                    or _normalize_env(environment) != environment
                    or len(environment) != plan.env_count
                ):
                    raise ACPError("invalid training job plan")
            elif plan.env_count != 0:
                raise ACPError("invalid training job plan")

            if replicas > 1:
                if (
                    "barrier" not in body
                    or type(body["barrier"]) is not dict
                    or _normalize_barrier(body["barrier"]) != body["barrier"]
                ):
                    raise ACPError("invalid training job plan")
            elif "barrier" in body:
                raise ACPError("invalid training job plan")

            if "framework" in body:
                _safe_text(
                    body["framework"],
                    label="framework",
                    limit=128,
                    multiline=False,
                )
            if "metadata" in body and type(body["metadata"]) is not dict:
                raise ACPError("invalid training job plan")
            if "fault_tolerance" in body and type(body["fault_tolerance"]) is not dict:
                raise ACPError("invalid training job plan")
            if "tensorboard" in body:
                tensorboard = body["tensorboard"]
                if type(tensorboard) is not dict or set(tensorboard) != {"log_path"}:
                    raise ACPError("invalid training job plan")
            if "async_checkpoint" in body:
                checkpoint = body["async_checkpoint"]
                if (
                    type(checkpoint) is not dict
                    or set(checkpoint) != {"max_ckpt_rounds"}
                ):
                    raise ACPError("invalid training job plan")
            if "lme" in body:
                lme = body["lme"]
                if (
                    type(lme) is not dict
                    or not lme
                    or not set(lme).issubset(
                        {
                            "enable_warmingup",
                            "enable_checker",
                            "enable_health_monitor",
                            "max_retries",
                        }
                    )
                ):
                    raise ACPError("invalid training job plan")
            if "ssh" in body:
                ssh = body["ssh"]
                if (
                    type(ssh) is not dict
                    or not ssh
                    or not set(ssh).issubset(
                        {"auto_key_setup", "config_mount_path"}
                    )
                ):
                    raise ACPError("invalid training job plan")

            json.dumps(
                body,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            return body
        except Exception:
            raise ACPError("invalid training job plan") from None

    def submit(self, plan: TrainingJobPlan, *, timeout: float = 90.0) -> Mapping[str, Any]:
        """Explicitly POST a previously prepared plan exactly once."""
        if type(plan) is not TrainingJobPlan:
            raise ACPError("submit requires a training job plan")
        issued = self._issued_plans.pop(id(plan), None)
        if issued is None or issued[0]() is not plan:
            raise ACPError("invalid training job plan")
        try:
            fingerprint = self._plan_fingerprint(plan)
        except Exception:
            raise ACPError("invalid training job plan") from None
        if fingerprint != issued[1]:
            raise ACPError("invalid training job plan")
        body = self._validated_submit_body(plan)
        data = self._request(
            "POST",
            self.create_url,
            operation="training job creation",
            body=body,
            timeout=timeout,
        )
        return _unwrap_object(data, ("training_job", "trainingJob", "job"))


__all__ = [
    "ACP_ORIGIN",
    "AEC2_ORIGIN",
    "CCR_ORIGIN",
    "MONITOR_ORIGIN",
    "DEFAULT_ACP_CONSOLE_URL",
    "DEFAULT_TEMPLATE_JOB",
    "SUPPORTED_ACP_ZONE",
    "DEFAULT_PORTABLE_FRAMEWORK",
    "DEFAULT_PORTABLE_REPLICAS",
    "DEFAULT_PORTABLE_ENV",
    "ACPError",
    "ACPAPIError",
    "ResourceSpec",
    "ResourcePoolChoice",
    "WorkerOverrides",
    "normalize_worker_overrides",
    "validate_acp_workspace",
    "TrainingJobPlan",
    "ACPClient",
]
