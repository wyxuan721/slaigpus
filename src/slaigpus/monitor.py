"""Read-only client helpers for SenseCore Monitor log queries.

The API is scoped to a PRIVATE telemetry-station resource.  That resource is
not derivable from a workspace id, so callers must provide its complete
resource id; :class:`TelemetryStationRef` validates every path component
before it is used in a URL.

Although the log and custom-filter queries use HTTP POST, they are read-only
queries.  They are the only POSTs in this module and the only POSTs for which
an expired browser authorization may be refreshed and retried once.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence
from urllib.parse import quote

from .cci import CCIError, WorkspaceRef


MONITOR = "https://monitor.sensecoreapi.cn"
MONITOR_ORIGIN = MONITOR
MONITOR_BASE_PATH = (
    "/monitor/ts/data/v1/subscriptions/subscription_name/"
    "resourceGroups/resource_group_name/zones/zone/"
    "telemetryStations/telemetry_station_name"
)
MONITOR_BASE = MONITOR + MONITOR_BASE_PATH
MONITOR_BASE_URL = MONITOR_BASE
_MONITOR_BASE_PATH_TEMPLATE = (
    "/monitor/ts/data/v1/subscriptions/{subscription_name}/"
    "resourceGroups/{resource_group_name}/zones/{zone}/"
    "telemetryStations/{telemetry_station_name}"
)

ACP_JOB_NAME = "Attributes.k8s.job.name"
ACP_POD_NAME = "Attributes.k8s.pod.name"
ACP_CONTAINER_NAME = "Attributes.k8s.container.name"
ACP_HOST_IP = "Attributes.k8s.host.ip"
ACP_CUSTOM_FILTER_KEYS = (
    ACP_JOB_NAME,
    ACP_POD_NAME,
    ACP_CONTAINER_NAME,
    ACP_HOST_IP,
)
ACP_PRODUCTS = (
    "product.lepton-acp",
    "product.lepton-acp-new",
)


class MonitorError(CCIError):
    """A Monitor v1 response, query, or schema was unusable."""


class MonitorAPIError(MonitorError):
    """A Monitor v1 endpoint returned a non-success HTTP status."""

    def __init__(self, method: str, url: str, status: int) -> None:
        self.method = str(method)
        self.url = str(url)
        self.status = int(status)
        super().__init__(
            f"Monitor API {self.method} failed with HTTP {self.status} ({self.url})"
        )


class MonitorTransport(Protocol):
    """The small BrowserFetchTransport surface used by :class:`MonitorClient`."""

    def request(
        self,
        method: str,
        url: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> Any:
        ...


@dataclass(frozen=True)
class TelemetryStationRef:
    """A safely parsed PRIVATE telemetry-station resource id."""

    subscription_name: str
    resource_group_name: str
    zone: str
    telemetry_station_name: str

    @classmethod
    def parse(cls, value: Any) -> "TelemetryStationRef":
        if not isinstance(value, str) or not value.startswith("/"):
            raise MonitorError(
                "telemetry station must be /subscriptions/.../resourceGroups/.../"
                "zones/.../telemetryStations/..."
            )
        if (
            value.strip() != value
            or "?" in value
            or "#" in value
            or "\\" in value
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            raise MonitorError("telemetry station resource id is invalid")
        parts = value.split("/")[1:]
        expected = [
            "subscriptions",
            "resourceGroups",
            "zones",
            "telemetryStations",
        ]
        if len(parts) != 8 or parts[::2] != expected:
            raise MonitorError(
                "telemetry station must be /subscriptions/.../resourceGroups/.../"
                "zones/.../telemetryStations/..."
            )
        components = parts[1::2]
        if any(not item or item in {".", ".."} for item in components):
            raise MonitorError("telemetry station resource id contains an invalid component")
        return cls(*components)

    # Short aliases make the resource feel consistent with WorkspaceRef while
    # the canonical fields retain the Monitor path-template vocabulary.
    @property
    def subscription(self) -> str:
        return self.subscription_name

    @property
    def resource_group(self) -> str:
        return self.resource_group_name

    @property
    def telemetry_station(self) -> str:
        return self.telemetry_station_name

    @property
    def name(self) -> str:
        return self.telemetry_station_name

    @property
    def resource_id(self) -> str:
        return (
            f"/subscriptions/{self.subscription_name}/"
            f"resourceGroups/{self.resource_group_name}/zones/{self.zone}/"
            f"telemetryStations/{self.telemetry_station_name}"
        )

    @property
    def base_path(self) -> str:
        return _MONITOR_BASE_PATH_TEMPLATE.format(
            subscription_name=quote(self.subscription_name, safe=""),
            resource_group_name=quote(self.resource_group_name, safe=""),
            zone=quote(self.zone, safe=""),
            telemetry_station_name=quote(self.telemetry_station_name, safe=""),
        )

    @property
    def base_url(self) -> str:
        return MONITOR + self.base_path


def _nonempty_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MonitorError(f"{label} must be a non-empty string")
    return value.strip()


def _workspace_reference(value: Any) -> WorkspaceRef:
    """Parse one canonical workspace RID without accepting URL-like input."""
    if not isinstance(value, str) or not value.startswith("/"):
        raise MonitorError("workspace must be a complete workspace resource id")
    if (
        value.strip() != value
        or "?" in value
        or "#" in value
        or "\\" in value
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise MonitorError("workspace resource id is invalid")
    workspace: Optional[WorkspaceRef] = None
    try:
        workspace = WorkspaceRef.parse(value)
    except CCIError:
        raise MonitorError("workspace must be a complete workspace resource id") from None
    if (
        workspace.resource_id != value
        or any(
            component in {"", ".", ".."}
            for component in (
                workspace.subscription,
                workspace.resource_group,
                workspace.zone,
                workspace.workspace,
            )
        )
    ):
        raise MonitorError("workspace must be a canonical workspace resource id")
    return workspace


_RESOURCE_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _monitor_resource_id(value: Any) -> str:
    """Validate the opaque UUID expected by the legacy Monitor log API."""
    if not isinstance(value, str) or _RESOURCE_UUID.fullmatch(value) is None:
        raise MonitorError("resource_id must be a canonical Monitor resource UUID")
    return value


def _unix_second(value: Any, *, label: str) -> str:
    """Return the Monitor wire representation for one Unix-second value."""
    if isinstance(value, bool):
        raise MonitorError(f"{label} must be a Unix timestamp in seconds")
    if isinstance(value, datetime):
        number = value.timestamp()
    elif isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        return value.strip()
    else:
        raise MonitorError(f"{label} must be a Unix timestamp in seconds")
    if number == float("inf") or number == float("-inf") or number != number:
        raise MonitorError(f"{label} must be a whole Unix timestamp in seconds")
    if number < 0 or not number.is_integer():
        raise MonitorError(f"{label} must be a whole Unix timestamp in seconds")
    return str(int(number))


def _positive_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool):
        raise MonitorError(f"{label} must be a positive integer")
    parse_failed = False
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        parse_failed = True
        number = 0
    if parse_failed:
        raise MonitorError(f"{label} must be a positive integer")
    if number <= 0 or str(value).strip() not in {str(number), f"+{number}"}:
        raise MonitorError(f"{label} must be a positive integer")
    return number


def _offset(value: Any) -> str:
    if isinstance(value, bool):
        raise MonitorError("offset must be a non-negative integer")
    parse_failed = False
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        parse_failed = True
        number = -1
    if parse_failed:
        raise MonitorError("offset must be a non-negative integer")
    if number < 0 or str(value).strip() not in {str(number), f"+{number}"}:
        raise MonitorError("offset must be a non-negative integer")
    return str(number)


def choose_histogram_interval(start: Any, end: Any) -> str:
    """Return the discrete histogram interval used by the Console log page."""
    start_value = int(_unix_second(start, label="start"))
    end_value = int(_unix_second(end, label="end"))
    if end_value < start_value:
        raise MonitorError("end must not be earlier than start")
    duration = end_value - start_value
    intervals = (
        (10 * 60, "60s"),
        (60 * 60, "10m"),
        (6 * 60 * 60, "1h"),
        (24 * 60 * 60, "3h"),
        (3 * 24 * 60 * 60, "12h"),
        (7 * 24 * 60 * 60, "24h"),
        (15 * 24 * 60 * 60, "48h"),
    )
    for upper_bound, interval in intervals:
        if duration <= upper_bound:
            return interval
    return "72h"


# Public compatibility spelling matching the request-body field.
histogram_interval = choose_histogram_interval


def custom_filter(key: Any, value: Any) -> Dict[str, str]:
    """Build one exact Monitor ``{key, value}`` custom-filter object."""
    selected_key = _nonempty_text(key, label="custom filter key")
    selected_value = _nonempty_text(value, label="custom filter value")
    return {"key": selected_key, "value": selected_value}


def _custom_filters(value: Any, *, label: str) -> List[Dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(
        value, Sequence
    ):
        raise MonitorError(f"{label} must be a list of key/value objects")
    normalized: List[Dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"key", "value"}:
            raise MonitorError(f"{label} must contain only key/value objects")
        normalized.append(custom_filter(item.get("key"), item.get("value")))
    return normalized


def _first_list(value: Any, keys: Sequence[str], *, _depth: int = 0) -> List[Any]:
    """Find a common list envelope without depending on one response wrapper."""
    if _depth > 6:
        return []
    if isinstance(value, list):
        return list(value)
    if not isinstance(value, Mapping):
        return []
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, list):
            return list(candidate)
        if isinstance(candidate, Mapping):
            found = _first_list(candidate, keys, _depth=_depth + 1)
            if found:
                return found
    for wrapper in ("data", "result", "response", "payload"):
        candidate = value.get(wrapper)
        if isinstance(candidate, (Mapping, list)):
            found = _first_list(candidate, keys, _depth=_depth + 1)
            if found or isinstance(candidate, list):
                return found
    return []


def _product_identifier(product: Any) -> str:
    if isinstance(product, str):
        return product.strip()
    if not isinstance(product, Mapping):
        return ""
    for key in (
        "key",
        "id",
        "name",
        "label",
        "value",
        "product",
        "product_id",
    ):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def select_acp_product(products: Any, explicit: str = "") -> str:
    """Select one ACP log product, preferring an explicit product key."""
    if explicit:
        selected = _nonempty_text(explicit, label="product")
        if selected not in ACP_PRODUCTS:
            raise MonitorError("product is not a known ACP log product")
        return selected
    available = _first_list(
        products,
        ("products", "items", "records", "list", "results"),
    )
    candidates: Dict[str, None] = {}
    for product in available:
        identifier = _product_identifier(product)
        if identifier in ACP_PRODUCTS:
            candidates.setdefault(identifier, None)
    if not candidates:
        raise MonitorError("Monitor returned no ACP log product")
    if ACP_PRODUCTS[1] in candidates:
        return ACP_PRODUCTS[1]
    return ACP_PRODUCTS[0]


def _mapping_with_key(value: Any, key: str) -> Optional[Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    if key in value:
        return value
    for wrapper in ("data", "result", "response", "payload"):
        nested = value.get(wrapper)
        found = _mapping_with_key(nested, key)
        if found is not None:
            return found
    return None


def extract_log_hits(response: Any) -> List[Dict[str, Any]]:
    """Extract mapping hits from direct, wrapped, or Elasticsearch-like data."""
    envelope = _mapping_with_key(response, "hits")
    if envelope is None:
        return []
    raw_hits: Any = envelope.get("hits")
    if isinstance(raw_hits, Mapping):
        raw_hits = raw_hits.get("hits")
    if not isinstance(raw_hits, list):
        return []
    return [dict(hit) for hit in raw_hits if isinstance(hit, Mapping)]


def normalize_log_page(response: Any) -> Dict[str, Any]:
    """Return a stable page mapping while preserving unknown server fields."""
    envelope = _mapping_with_key(response, "hits")
    page: Dict[str, Any] = dict(envelope) if envelope is not None else {}
    nested_hits = page.get("hits")
    if isinstance(nested_hits, Mapping):
        if "total" not in page and "total" in nested_hits:
            page["total"] = nested_hits.get("total")
    page["hits"] = extract_log_hits(response)
    if "histogram" in page and not isinstance(page.get("histogram"), list):
        page["histogram"] = []
    return page


def _lookup(value: Any, path: str) -> Any:
    current = value
    parts = path.split(".")
    index = 0
    while index < len(parts):
        if not isinstance(current, Mapping):
            return None
        remaining = ".".join(parts[index:])
        if remaining in current:
            return current.get(remaining)
        part = parts[index]
        if part not in current:
            return None
        current = current.get(part)
        index += 1
    return current


def _first_value(hit: Mapping[str, Any], paths: Sequence[str]) -> Any:
    for path in paths:
        value = _lookup(hit, path)
        if value not in (None, ""):
            return value
    return None


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, (Mapping, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def format_log_hit(hit: Any) -> str:
    """Format one Monitor hit without assuming every optional field exists.

    Current Monitor hits use ``log_time``, ``body`` and dotted keys below the
    ``attributes`` object.  Older and wrapped spellings are accepted so a
    harmless response-envelope change does not make log output unusable.
    """
    if not isinstance(hit, Mapping):
        return _display(hit)
    source = hit.get("_source")
    merged: Dict[str, Any] = dict(source) if isinstance(source, Mapping) else {}
    merged.update(hit)
    timestamp = _display(
        _first_value(merged, ("log_time", "timestamp", "time", "string_time"))
    )
    severity = _display(
        _first_value(merged, ("severity_text", "severity", "level"))
    )
    pod = _display(
        _first_value(
            merged,
            ("attributes.k8s.pod.name", ACP_POD_NAME, "k8s.pod.name"),
        )
    )
    container = _display(
        _first_value(
            merged,
            (
                "attributes.k8s.container.name",
                ACP_CONTAINER_NAME,
                "k8s.container.name",
            ),
        )
    )
    host = _display(
        _first_value(
            merged,
            ("attributes.k8s.host.ip", ACP_HOST_IP, "k8s.host.ip"),
        )
    )
    message = _display(_first_value(merged, ("body", "msg", "message", "log")))

    fields: List[str] = []
    if timestamp:
        fields.append(timestamp)
    if severity:
        fields.append(f"[{severity}]")
    location = "/".join(part for part in (pod, container) if part)
    if host:
        location = f"{location}@{host}" if location else host
    if location:
        fields.append(location)
    if message:
        fields.append(message)
    if fields:
        return " ".join(fields)
    return _display(dict(hit))


class MonitorClient:
    """Schema-tolerant, read-only client for the fixed Monitor log v1 API."""

    def __init__(
        self,
        transport: MonitorTransport,
        telemetry_station: "str | TelemetryStationRef",
    ) -> None:
        self.transport = transport
        self.telemetry_station = (
            telemetry_station
            if isinstance(telemetry_station, TelemetryStationRef)
            else TelemetryStationRef.parse(telemetry_station)
        )
        self.base_url = self.telemetry_station.base_url

    @property
    def products_url(self) -> str:
        return self.base_url + "/logStream/products"

    @property
    def resources_url(self) -> str:
        return self.base_url + "/resources"

    def _product_url(self, product: Any, suffix: str) -> str:
        selected = _nonempty_text(product, label="product")
        return self.products_url + "/" + quote(selected, safe="") + suffix

    @staticmethod
    def _decode_response(response: Any, method: str, url: str) -> Any:
        status_value = getattr(response, "status", None)
        if status_value is None:
            return response
        invalid_status = False
        try:
            status = int(status_value)
        except (TypeError, ValueError, OverflowError):
            invalid_status = True
            status = 0
        if invalid_status:
            response = None
            raise MonitorError("Monitor transport returned an invalid HTTP status")
        if status < 200 or status >= 300:
            raise MonitorAPIError(method, url, status)

        if hasattr(response, "text"):
            text = getattr(response, "text")
            if text is None or str(text).strip() == "":
                return {}
            decode_failed = False
            decoded: Any = None
            try:
                decoded = json.loads(str(text))
            except (TypeError, ValueError):
                decode_failed = True
            if decode_failed:
                text = None
                response = None
                raise MonitorError(
                    f"Monitor API returned invalid JSON for {method} {url}"
                )
            return decoded
        decoder = getattr(response, "json", None)
        if callable(decoder):
            decode_failed = False
            decoded = None
            try:
                decoded = decoder()
            except Exception:
                decode_failed = True
            if decode_failed:
                response = None
                raise MonitorError(
                    f"Monitor API returned invalid JSON for {method} {url}"
                )
            return decoded
        return {}

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
        retry_readonly_post: bool = False,
    ) -> Any:
        def perform() -> Any:
            request_failed = False
            result: Any = None
            try:
                result = self.transport.request(
                    method,
                    url,
                    json_body=body,
                    timeout=float(timeout),
                )
            except Exception:
                request_failed = True
            if request_failed:
                result = None
                raise MonitorError("Monitor transport request failed")
            return result

        response = perform()
        known_readonly_post = bool(
            method == "POST"
            and re.fullmatch(
                re.escape(self.products_url)
                + r"/[^/]+/(?:logs|customFilterValues)",
                url,
            )
        )
        if (
            retry_readonly_post
            and known_readonly_post
            and getattr(response, "status", None) == 401
        ):
            refresh = getattr(self.transport, "refresh_auth", None)
            if callable(refresh):
                refresh_failed = False
                try:
                    refresh(timeout=min(float(timeout), 60.0))
                except Exception:
                    refresh_failed = True
                if refresh_failed:
                    response = None
                    raise MonitorError("Monitor authorization refresh failed")
                response = perform()
        return self._decode_response(response, method, url)

    def list_products(self, *, timeout: float = 60.0) -> List[Any]:
        data = self._request("GET", self.products_url, timeout=timeout)
        return _first_list(
            data,
            ("products", "items", "records", "list", "results"),
        )

    get_products = list_products

    def list_resources(
        self,
        product: str,
        *,
        timeout: float = 60.0,
    ) -> List[Mapping[str, Any]]:
        """List telemetry resources registered for one Monitor product.

        ``MonitorTransport`` intentionally has no generic ``params`` surface,
        so the single query value is percent-encoded before it is appended.
        This keeps an untrusted product string from adding another parameter.
        """
        selected_product = _nonempty_text(product, label="product")
        url = self.resources_url + "?product=" + quote(selected_product, safe="")
        data = self._request("GET", url, timeout=timeout)
        raw_resources = _first_list(
            data,
            ("resources", "items", "records", "list", "results"),
        )
        resources: List[Mapping[str, Any]] = []
        for resource in raw_resources:
            if not isinstance(resource, Mapping):
                raise MonitorError("Monitor returned an invalid resource list")
            resources.append(dict(resource))
        return resources

    get_resources = list_resources

    def resolve_resource_id(
        self,
        product: str,
        workspace: "str | WorkspaceRef",
        *,
        timeout: float = 60.0,
    ) -> str:
        """Resolve a workspace RID to the UUID required by Monitor queries.

        Current responses put the workspace's short name in ``name``.  Older
        deployments may put its complete resource id there, so both exact
        representations are supported.  No fuzzy or display-name matching is
        used because that could silently select a different workspace.
        """
        if isinstance(workspace, WorkspaceRef):
            selected_workspace = _workspace_reference(workspace.resource_id)
        else:
            selected_workspace = _workspace_reference(workspace)
        expected_names = {
            selected_workspace.workspace,
            selected_workspace.resource_id,
        }
        matches: List[str] = []
        for resource in self.list_resources(product, timeout=timeout):
            name = resource.get("name")
            if not isinstance(name, str) or name not in expected_names:
                continue
            matches.append(_monitor_resource_id(resource.get("resource_id")))

        if not matches:
            raise MonitorError("Monitor returned no resource for the workspace")
        if len(matches) > 1:
            if len(set(matches)) == 1:
                raise MonitorError("Monitor returned a duplicate workspace resource")
            raise MonitorError("Monitor returned ambiguous workspace resources")
        return matches[0]

    def select_acp_product(self, product: str = "", *, timeout: float = 60.0) -> str:
        if product:
            return select_acp_product([], explicit=product)
        return select_acp_product(self.list_products(timeout=timeout))

    def query_logs(
        self,
        product: str,
        *,
        start: Any,
        end: Any,
        resource_id: str,
        page_size: Any = 40,
        offset: Any = "0",
        order: str = "desc",
        histogram_interval: Optional[str] = None,
        filter: Optional[str] = None,
        custom_filter: Any = None,
        timeout: float = 60.0,
    ) -> Dict[str, Any]:
        """Query one log page using a UUID from :meth:`resolve_resource_id`."""
        start_text = _unix_second(start, label="start")
        end_text = _unix_second(end, label="end")
        if int(end_text) <= int(start_text):
            raise MonitorError("end must be later than start")
        selected_resource_id = _monitor_resource_id(resource_id)
        selected_order = _nonempty_text(order, label="order").lower()
        if selected_order not in {"asc", "desc"}:
            raise MonitorError("order must be asc or desc")
        interval = histogram_interval
        if interval is None:
            interval = choose_histogram_interval(start_text, end_text)
        interval = _nonempty_text(interval, label="histogram_interval")

        body: Dict[str, Any] = {
            "start": start_text,
            "end": end_text,
            "resource_id": [selected_resource_id],
            "page_size": _positive_integer(page_size, label="page_size"),
            "offset": _offset(offset),
            "order": selected_order,
            "histogram_interval": interval,
        }
        if filter is not None:
            if not isinstance(filter, str):
                raise MonitorError("filter must be a string")
            body["filter"] = filter
        filters = _custom_filters(custom_filter, label="custom_filter")
        if filters:
            body["custom_filter"] = filters

        data = self._request(
            "POST",
            self._product_url(product, "/logs"),
            body=body,
            timeout=timeout,
            retry_readonly_post=True,
        )
        return normalize_log_page(data)

    get_logs = query_logs

    def custom_filter_values(
        self,
        product: str,
        *,
        key: str,
        resource_id: str,
        custom_filters: Any = None,
        timeout: float = 60.0,
    ) -> List[str]:
        """List filter values using a UUID from :meth:`resolve_resource_id`."""
        body = {
            "key": _nonempty_text(key, label="custom filter key"),
            "resource_id": [_monitor_resource_id(resource_id)],
            "custom_filters": _custom_filters(
                custom_filters,
                label="custom_filters",
            ),
        }
        data = self._request(
            "POST",
            self._product_url(product, "/customFilterValues"),
            body=body,
            timeout=timeout,
            retry_readonly_post=True,
        )
        raw_values = _first_list(data, ("values", "items", "list", "results"))
        values: List[str] = []
        for value in raw_values:
            selected: Any = value
            if isinstance(value, Mapping):
                selected = next(
                    (
                        value.get(name)
                        for name in ("value", "key", "name", "label")
                        if value.get(name) not in (None, "")
                    ),
                    None,
                )
            if isinstance(selected, str):
                values.append(selected)
        return values

    get_custom_filter_values = custom_filter_values


__all__ = [
    "ACP_CONTAINER_NAME",
    "ACP_CUSTOM_FILTER_KEYS",
    "ACP_HOST_IP",
    "ACP_JOB_NAME",
    "ACP_POD_NAME",
    "ACP_PRODUCTS",
    "MONITOR",
    "MONITOR_BASE",
    "MONITOR_BASE_PATH",
    "MONITOR_BASE_URL",
    "MONITOR_ORIGIN",
    "MonitorAPIError",
    "MonitorClient",
    "MonitorError",
    "TelemetryStationRef",
    "choose_histogram_interval",
    "custom_filter",
    "extract_log_hits",
    "format_log_hit",
    "histogram_interval",
    "normalize_log_page",
    "select_acp_product",
]
