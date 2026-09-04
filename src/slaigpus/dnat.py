"""Guarded DNAT creation on the EIP assigned to the local SenseCore account."""

from __future__ import annotations

import ipaddress
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Protocol, Tuple, Union
from urllib.parse import quote

from .cci import CCIError


EIP_SUBSCRIPTION = "0197ee17-b6eb-7846-b2b4-a77c5f509b92"
EIP_RESOURCE_GROUP = "default"
EIP_ZONE = "cn-sh-01e"
EIP_ZONE_LABEL = "01e"
EIP_TENANT_CODE = "zhicheng"

NETWORK_API_ORIGIN = "https://network.cn-sh-01.sensecoreapi.cn"
MANAGEMENT_API_ORIGIN = "https://management.sensecoreapi.cn"
IAM_API_ORIGIN = "https://iam.sensecoreapi.cn"
EIP_CONSOLE_URL = "https://console.sensecore.cn/cn-sh-01/eip/list"
EIP_RESOURCE_PAGE_URL = MANAGEMENT_API_ORIGIN + "/rmh/v1/resources:page"
EIP_RESOURCE_PAGE_SIZE = 10_000
EIP_RESOURCE_FILTER = "resource_type='network.eip.v1.eip'"

DNAT_RULE_NAME_PREFIX = f"dnat-{EIP_TENANT_CODE}-"
_ACCOUNT_PATTERN = re.compile(r"^L([0-9]+)$")
_EIP_DISPLAY_PATTERN = re.compile(
    rf"^L([0-9]+)_L([0-9]+)_{re.escape(EIP_ZONE_LABEL)}_"
)
MAX_PORTS_PER_RULE = 500
MAX_CONCURRENT_CHANGES = 10
_ACTIVE_CHANGE_STATES = frozenset(("CREATING", "BINDWAITING"))
_SUPPORTED_PROTOCOLS = frozenset(("tcp", "udp"))


class DNATError(CCIError):
    """A DNAT request or safety check failed."""


class DNATAPIError(DNATError):
    def __init__(self, method: str, url: str, status: int, detail: str = "") -> None:
        self.status = int(status)
        suffix = f": {detail[:300]}" if detail else ""
        super().__init__(f"DNAT API {method} failed with HTTP {status} ({url}){suffix}")


class DNATCreateUncertain(DNATError):
    """The transport failed after a create request may have reached the API."""


class DNATTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
    ) -> Any:
        ...


@dataclass(frozen=True)
class DNATSpec:
    protocol: str
    eip_port: Union[str, int]
    target_ip: str
    target_port: Union[str, int]


@dataclass(frozen=True)
class DNATCreatePlan:
    rule_name: str
    eip_name: str
    eip_rid: str
    eip_display_name: str
    spec: DNATSpec
    payload: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "eip": self.eip_name,
            "eip_display_name": self.eip_display_name,
            "eip_port": str(self.spec.eip_port),
            "protocol": self.spec.protocol,
            "rule_name": self.rule_name,
            "target_ip": self.spec.target_ip,
            "target_port": str(self.spec.target_port),
        }


@dataclass(frozen=True)
class DNATCreateResult:
    plan: DNATCreatePlan
    response: Mapping[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        result = self.plan.to_dict()
        result["applied"] = True
        state = self.response.get("state")
        if state is not None:
            result["state"] = state
        return result


def _json_object(value: Any, *, label: str) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError as exc:
            raise DNATError(f"{label} is not valid JSON") from exc
        if isinstance(decoded, Mapping):
            return dict(decoded)
    raise DNATError(f"{label} is not an object")


def _port_range(value: Union[str, int], *, label: str) -> Tuple[int, int, str]:
    if isinstance(value, bool):
        raise DNATError(f"{label} must be a port or port range")
    text = str(value).strip()
    match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", text)
    if match is None:
        raise DNATError(f"{label} must be a port or port range such as 22 or 8000-8005")
    start = int(match.group(1))
    end = int(match.group(2) or start)
    if start < 1 or end > 65535:
        raise DNATError(f"{label} must stay between 1 and 65535")
    if end < start:
        raise DNATError(f"{label} range end must not be less than its start")
    count = end - start + 1
    if count > MAX_PORTS_PER_RULE:
        raise DNATError(f"{label} range may contain at most {MAX_PORTS_PER_RULE} ports")
    normalized = str(start) if start == end else f"{start}-{end}"
    return start, end, normalized


def _normalize_spec(spec: DNATSpec) -> DNATSpec:
    protocol = str(spec.protocol).strip().lower()
    if protocol not in _SUPPORTED_PROTOCOLS:
        raise DNATError("protocol must be tcp or udp")
    external_start, external_end, external = _port_range(
        spec.eip_port, label="EIP port"
    )
    internal_start, internal_end, internal = _port_range(
        spec.target_port, label="target port"
    )
    if external_end - external_start != internal_end - internal_start:
        raise DNATError(
            "EIP and target port ranges must contain the same number of ports"
        )
    try:
        address = ipaddress.ip_address(str(spec.target_ip).strip())
    except ValueError as exc:
        raise DNATError("target IP must be a valid IPv4 address") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise DNATError("target IP must be a valid IPv4 address")
    if address.is_unspecified or address.is_multicast or address.is_loopback:
        raise DNATError("target IP must be a unicast VPC IPv4 address")
    return DNATSpec(
        protocol=protocol,
        eip_port=external,
        target_ip=str(address),
        target_port=internal,
    )


def _account_number(username: str) -> Tuple[int, int]:
    text = str(username).strip()
    match = _ACCOUNT_PATTERN.fullmatch(text)
    if match is None:
        raise DNATError(
            "stored SenseCore username must have the form L followed by digits"
        )
    digits = match.group(1)
    return int(digits), len(digits)


def _display_account_range(display_name: Any) -> Optional[Tuple[int, int, int]]:
    if not isinstance(display_name, str):
        return None
    match = _EIP_DISPLAY_PATTERN.match(display_name)
    if match is None:
        return None
    start_digits, end_digits = match.groups()
    if len(start_digits) != len(end_digits):
        return None
    start = int(start_digits)
    end = int(end_digits)
    if end < start:
        return None
    return start, end, len(start_digits)


def _eip_rid(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is None:
        raise DNATError("selected EIP has an invalid internal name")
    return (
        f"/subscriptions/{EIP_SUBSCRIPTION}"
        f"/resourceGroups/{EIP_RESOURCE_GROUP}"
        f"/zones/{EIP_ZONE}/eips/{name}"
    )


def _eip_id(name: str) -> str:
    return (
        f"/resourceGroups/{EIP_RESOURCE_GROUP}"
        f"/zones/{EIP_ZONE}/eips/{name}"
    )


class DNATClient:
    """Plan or create one IP-target DNAT rule on the account-assigned EIP."""

    def __init__(
        self,
        transport: DNATTransport,
        username: str,
        *,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self.transport = transport
        self._account_number, self._account_width = _account_number(username)
        self._uuid_factory = uuid_factory

    @staticmethod
    def _eip_base_url(eip_rid: str) -> str:
        return NETWORK_API_ORIGIN + "/network/eip/data/v1" + eip_rid

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        timeout: float = 60.0,
        retry_auth: bool = False,
    ) -> Any:
        def perform() -> Any:
            return self.transport.request(
                method,
                url,
                params=params,
                json_body=body,
                timeout=timeout,
            )

        response = perform()
        if (
            retry_auth
            and getattr(response, "status", None) == 401
            and hasattr(self.transport, "refresh_auth")
        ):
            self.transport.refresh_auth(timeout=min(timeout, 60.0))
            response = perform()
        if hasattr(response, "status") and hasattr(response, "text"):
            status = int(response.status)
            text = str(response.text or "")
            if status < 200 or status >= 300:
                detail = text
                try:
                    decoded = json.loads(text)
                    if isinstance(decoded, Mapping):
                        detail = str(
                            decoded.get("message")
                            or decoded.get("error")
                            or text
                        )
                except ValueError:
                    pass
                raise DNATAPIError(method, url, status, detail)
            if not text.strip():
                return {}
            try:
                return json.loads(text)
            except ValueError as exc:
                raise DNATError(
                    f"DNAT API returned invalid JSON for {method} {url}"
                ) from exc
        return response

    def _discover_eip(self) -> Dict[str, Any]:
        data = self._request(
            "POST",
            EIP_RESOURCE_PAGE_URL,
            params={
                "filter": EIP_RESOURCE_FILTER,
                "page_size": EIP_RESOURCE_PAGE_SIZE,
            },
            body={},
            retry_auth=True,
        )
        if not isinstance(data, Mapping):
            raise DNATError("SenseCore EIP resource response is not an object")
        raw_resources = data.get("resources") or []
        if not isinstance(raw_resources, list) or not all(
            isinstance(resource, Mapping) for resource in raw_resources
        ):
            raise DNATError("SenseCore EIP resource list is invalid")
        resources = [dict(resource) for resource in raw_resources]
        total_size = data.get("total_size")
        if (
            (isinstance(total_size, int) and total_size > len(resources))
            or len(resources) >= EIP_RESOURCE_PAGE_SIZE
        ):
            raise DNATError(
                "SenseCore did not return every EIP resource; refusing to select one"
            )

        matches: List[Dict[str, Any]] = []
        for resource in resources:
            if resource.get("deleted") is True or str(
                resource.get("state") or ""
            ).upper() == "DELETED":
                continue
            account_range = _display_account_range(resource.get("display_name"))
            if account_range is None:
                continue
            start, end, width = account_range
            if width != self._account_width or not (
                start <= self._account_number <= end
            ):
                continue
            name = resource.get("name")
            rid = resource.get("rid")
            if not isinstance(name, str) or rid != _eip_rid(name):
                raise DNATError(
                    "account-matched EIP is outside the allowed subscription, "
                    "resource group, or zone"
                )
            resource_zone = resource.get("zone")
            if resource_zone not in (None, "", EIP_ZONE):
                raise DNATError("account-matched EIP has an unexpected zone")
            matches.append(resource)

        if not matches:
            raise DNATError(
                "no EIP display-name account range contains the stored "
                "SenseCore username"
            )
        if len(matches) != 1:
            raise DNATError(
                "multiple EIP display-name account ranges contain the stored SenseCore "
                "username"
            )
        return self._load_selected_eip(matches[0])

    def _load_selected_eip(self, resource: Mapping[str, Any]) -> Dict[str, Any]:
        name = str(resource["name"])
        rid = _eip_rid(name)
        data = self._request(
            "GET", self._eip_base_url(rid) + "/status", retry_auth=True
        )
        if not isinstance(data, Mapping):
            raise DNATError("selected EIP status response is not an object")
        info = data.get("eip_info")
        if not isinstance(info, Mapping):
            raise DNATError("selected EIP status has no eip_info object")
        selected = dict(info)
        properties = _json_object(
            selected.get("properties"), label="selected EIP properties"
        )
        selected["properties"] = properties
        selected["eip_ip"] = data.get("eip_ip")
        selected["rid"] = rid

        if selected.get("name") != name:
            raise DNATError(
                "SenseCore returned a different EIP than the selected resource"
            )
        display_name = selected.get("display_name")
        account_range = _display_account_range(display_name)
        if account_range is None or display_name != resource.get("display_name"):
            raise DNATError(
                "selected EIP display-name account range changed during planning"
            )
        start, end, width = account_range
        if width != self._account_width or not (
            start <= self._account_number <= end
        ):
            raise DNATError(
                "selected EIP no longer covers the stored SenseCore username"
            )
        if selected.get("id") != rid or selected.get("zone") != EIP_ZONE:
            raise DNATError(
                "selected EIP identity or zone does not match its resource ID"
            )
        required = {
            "uid": selected.get("uid"),
            "owner_id": selected.get("owner_id"),
            "tenant_id": selected.get("tenant_id"),
            "eip_ip": selected.get("eip_ip"),
            "association_id": properties.get("association_id"),
        }
        missing = sorted(key for key, value in required.items() if not value)
        if missing:
            raise DNATError(
                "selected EIP is missing required metadata: " + ", ".join(missing)
            )
        return selected

    def _list_rules(self, eip_rid: str) -> List[Dict[str, Any]]:
        data = self._request(
            "GET", self._eip_base_url(eip_rid) + "/dnatRules", retry_auth=True
        )
        if not isinstance(data, Mapping):
            raise DNATError("DNAT rule list response is not an object")
        raw_rules = data.get("dnat_rules") or []
        if not isinstance(raw_rules, list) or not all(
            isinstance(rule, Mapping) for rule in raw_rules
        ):
            raise DNATError("DNAT rule list is invalid")
        rules = [dict(rule) for rule in raw_rules]
        total_size = data.get("total_size")
        if isinstance(total_size, int) and total_size > len(rules):
            raise DNATError(
                "SenseCore did not return every existing DNAT rule; refusing to create"
            )
        changing = sum(
            1
            for rule in rules
            if str(rule.get("state") or "").upper() in _ACTIVE_CHANGE_STATES
        )
        if changing >= MAX_CONCURRENT_CHANGES:
            raise DNATError(
                "the selected EIP already has 10 DNAT rules being created or bound"
            )
        return rules

    @staticmethod
    def _check_port_conflicts(spec: DNATSpec, rules: List[Dict[str, Any]]) -> None:
        requested_start, requested_end, _ = _port_range(
            spec.eip_port, label="EIP port"
        )
        for rule in rules:
            state = str(rule.get("state") or "").upper()
            if rule.get("deleted") is True or state == "DELETED":
                continue
            properties = _json_object(
                rule.get("properties"), label="existing DNAT rule properties"
            )
            protocol = str(properties.get("protocol") or "").strip().lower()
            if protocol not in _SUPPORTED_PROTOCOLS:
                raise DNATError(
                    "an existing DNAT rule has an unsupported protocol; "
                    "refusing an incomplete conflict check"
                )
            if protocol != spec.protocol:
                continue
            existing_start, existing_end, existing = _port_range(
                properties.get("external_port"), label="existing EIP port"
            )
            if requested_start <= existing_end and existing_start <= requested_end:
                raise DNATError(
                    f"{spec.protocol} EIP port {spec.eip_port} conflicts with "
                    f"existing rule {rule.get('name') or '(unnamed)'} on {existing}"
                )

    def _current_user_id(self) -> str:
        data = self._request(
            "GET",
            IAM_API_ORIGIN + "/iam/idp/v1/me/status",
            retry_auth=True,
        )
        if not isinstance(data, Mapping):
            raise DNATError("SenseCore current-user response is not an object")
        user_id = data.get("user_id")
        if not isinstance(user_id, str) or not user_id:
            raise DNATError("SenseCore current-user response has no user_id")
        return user_id

    def plan_create(self, spec: DNATSpec) -> DNATCreatePlan:
        normalized = _normalize_spec(spec)
        eip = self._discover_eip()
        rules = self._list_rules(str(eip["rid"]))
        self._check_port_conflicts(normalized, rules)
        creator_id = self._current_user_id()

        rule_uuid = str(self._uuid_factory())
        if not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            rule_uuid,
        ):
            raise DNATError("UUID factory returned an invalid UUID")
        rule_name = DNAT_RULE_NAME_PREFIX + rule_uuid[:8]
        payload = {
            "id": _eip_id(str(eip["name"])) + "/dnatRules/" + rule_name,
            "name": rule_name,
            "display_name": rule_name,
            "description": "",
            "uid": rule_uuid,
            "resource_type": "network.eip.v1.dnatRule",
            "creator_id": creator_id,
            "owner_id": eip["owner_id"],
            "tenant_id": eip["tenant_id"],
            "zone": EIP_ZONE,
            "properties": {
                "nat_gateway_id": eip["properties"]["association_id"],
                "eip_id": eip["uid"],
                "external_ip": eip["eip_ip"],
                "external_port": normalized.eip_port,
                "protocol": normalized.protocol,
                "internal_ip": normalized.target_ip,
                "internal_port": normalized.target_port,
                "priority": 1,
                "internal_instance_type": "IP",
                "internal_instance_name": normalized.target_ip,
            },
        }
        return DNATCreatePlan(
            rule_name=rule_name,
            eip_name=str(eip["name"]),
            eip_rid=str(eip["rid"]),
            eip_display_name=str(eip["display_name"]),
            spec=normalized,
            payload=payload,
        )

    def create(self, spec: DNATSpec) -> DNATCreateResult:
        """Re-read safety state, then issue exactly one create request."""

        plan = self.plan_create(spec)
        url = self._eip_base_url(plan.eip_rid) + "/dnatRules/" + quote(
            plan.rule_name, safe=""
        )
        try:
            response = self._request("POST", url, body=plan.payload)
        except DNATAPIError:
            raise
        except Exception as exc:
            raise DNATCreateUncertain(
                "DNAT create result is uncertain for rule "
                f"{plan.rule_name}; list the selected EIP rules before retrying"
            ) from exc
        if not isinstance(response, Mapping):
            raise DNATCreateUncertain(
                "DNAT create returned an invalid result for rule "
                f"{plan.rule_name}; list the selected EIP rules before retrying"
            )
        return DNATCreateResult(plan=plan, response=dict(response))
