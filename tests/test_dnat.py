"""Offline tests for account-routed EIP DNAT planning and creation."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slaigpus.dnat import (  # noqa: E402
    DNATAPIError,
    DNATClient,
    DNATCreateUncertain,
    DNATError,
    DNATSpec,
    DNAT_RULE_NAME_PREFIX,
    EIP_RESOURCE_FILTER,
    EIP_RESOURCE_GROUP,
    EIP_RESOURCE_PAGE_SIZE,
    EIP_RESOURCE_PAGE_URL,
    EIP_SUBSCRIPTION,
    EIP_ZONE,
    IAM_API_ORIGIN,
    NETWORK_API_ORIGIN,
)


EIP_NAME = "eip-zhicheng-fab530d7"
EIP_DISPLAY_NAME = "L202500601_L202500750_01e_用于dnat创建"
EIP_RID = (
    f"/subscriptions/{EIP_SUBSCRIPTION}"
    f"/resourceGroups/{EIP_RESOURCE_GROUP}"
    f"/zones/{EIP_ZONE}/eips/{EIP_NAME}"
)
EIP_ID = (
    f"/resourceGroups/{EIP_RESOURCE_GROUP}"
    f"/zones/{EIP_ZONE}/eips/{EIP_NAME}"
)
STATUS_URL = (
    NETWORK_API_ORIGIN + "/network/eip/data/v1" + EIP_RID + "/status"
)
RULES_URL = (
    NETWORK_API_ORIGIN + "/network/eip/data/v1" + EIP_RID + "/dnatRules"
)
ME_URL = IAM_API_ORIGIN + "/iam/idp/v1/me/status"
TEST_UUID = uuid.UUID("12345678-1234-4234-9234-123456789abc")


def _resource(
    *,
    name: str = EIP_NAME,
    display_name: str = EIP_DISPLAY_NAME,
    rid: str | None = None,
    zone: str = EIP_ZONE,
    deleted: bool = False,
):
    return {
        "name": name,
        "display_name": display_name,
        "rid": rid
        or (
            f"/subscriptions/{EIP_SUBSCRIPTION}"
            f"/resourceGroups/{EIP_RESOURCE_GROUP}"
            f"/zones/{EIP_ZONE}/eips/{name}"
        ),
        "zone": zone,
        "deleted": deleted,
    }


def _status(
    *,
    name: str = EIP_NAME,
    display_name: str = EIP_DISPLAY_NAME,
    rid: str | None = None,
):
    selected_rid = rid or (
        f"/subscriptions/{EIP_SUBSCRIPTION}"
        f"/resourceGroups/{EIP_RESOURCE_GROUP}"
        f"/zones/{EIP_ZONE}/eips/{name}"
    )
    return {
        "eip_ip": "203.0.113.10",
        "eip_info": {
            "id": selected_rid,
            "uid": "eip-uuid-1",
            "name": name,
            "display_name": display_name,
            "owner_id": "owner-1",
            "tenant_id": "tenant-1",
            "zone": EIP_ZONE,
            "properties": {"association_id": "vpc-1"},
        },
    }


def _rule(
    *,
    name: str = "dnat-zhicheng-existing",
    protocol: str = "tcp",
    port: str = "80",
    state: str = "ACTIVE",
):
    return {
        "name": name,
        "state": state,
        "properties": {"protocol": protocol, "external_port": port},
    }


class FakeTransport:
    def __init__(
        self, *, resources=None, status=None, rules=None, post_response=None
    ):
        self.resources = [_resource()] if resources is None else resources
        self.status = _status() if status is None else status
        self.rules = [] if rules is None else rules
        self.post_response = {} if post_response is None else post_response
        self.calls = []
        self.post_error = None

    def request(
        self,
        method,
        url,
        *,
        params=None,
        json_body=None,
        timeout=60.0,
    ):
        self.calls.append((method, url, params, json_body, timeout))
        if method == "POST" and url == EIP_RESOURCE_PAGE_URL:
            return {"resources": self.resources, "total_size": len(self.resources)}
        base_urls = [
            NETWORK_API_ORIGIN + "/network/eip/data/v1" + str(item.get("rid"))
            for item in self.resources
        ] or [NETWORK_API_ORIGIN + "/network/eip/data/v1" + EIP_RID]
        if method == "GET" and any(url == base + "/status" for base in base_urls):
            return self.status
        if method == "GET" and any(
            url == base + "/dnatRules" for base in base_urls
        ):
            return {"dnat_rules": self.rules, "total_size": len(self.rules)}
        if method == "GET" and url == ME_URL:
            return {"status": "ACTIVE", "user_id": "user-1"}
        if method == "POST" and any(
            url.startswith(base + "/dnatRules/") for base in base_urls
        ):
            if self.post_error is not None:
                raise self.post_error
            return self.post_response
        raise AssertionError(f"unexpected request: {method} {url}")


def _client(transport):
    return DNATClient(
        transport, "L202500646", uuid_factory=lambda: TEST_UUID
    )


def test_plan_matches_the_console_payload_for_an_ip_target():
    transport = FakeTransport()
    plan = _client(transport).plan_create(
        DNATSpec("TCP", "20000-20002", "10.20.30.40", "30000-30002")
    )

    rule_name = DNAT_RULE_NAME_PREFIX + "12345678"
    assert plan.rule_name == rule_name
    assert plan.spec == DNATSpec("tcp", "20000-20002", "10.20.30.40", "30000-30002")
    assert plan.payload == {
        "id": EIP_ID + "/dnatRules/" + rule_name,
        "name": rule_name,
        "display_name": rule_name,
        "description": "",
        "uid": str(TEST_UUID),
        "resource_type": "network.eip.v1.dnatRule",
        "creator_id": "user-1",
        "owner_id": "owner-1",
        "tenant_id": "tenant-1",
        "zone": EIP_ZONE,
        "properties": {
            "nat_gateway_id": "vpc-1",
            "eip_id": "eip-uuid-1",
            "external_ip": "203.0.113.10",
            "external_port": "20000-20002",
            "protocol": "tcp",
            "internal_ip": "10.20.30.40",
            "internal_port": "30000-30002",
            "priority": 1,
            "internal_instance_type": "IP",
            "internal_instance_name": "10.20.30.40",
        },
    }
    assert [call[:2] for call in transport.calls] == [
        ("POST", EIP_RESOURCE_PAGE_URL),
        ("GET", STATUS_URL),
        ("GET", RULES_URL),
        ("GET", ME_URL),
    ]
    assert transport.calls[0][2] == {
        "filter": EIP_RESOURCE_FILTER,
        "page_size": EIP_RESOURCE_PAGE_SIZE,
    }
    assert transport.calls[0][3] == {}


def test_required_display_prefix_includes_the_trailing_underscore():
    transport = FakeTransport(
        resources=[
            _resource(display_name="L202500601_L202500750_01e-not-allowed")
        ]
    )

    with pytest.raises(DNATError, match="no EIP display-name account range"):
        _client(transport).plan_create(DNATSpec("tcp", 22, "10.0.0.2", 22))

    assert [call[:2] for call in transport.calls] == [
        ("POST", EIP_RESOURCE_PAGE_URL)
    ]


def test_selected_eip_identity_cannot_be_replaced_by_the_status_response():
    status = _status()
    status["eip_info"]["name"] = "eip-other"
    transport = FakeTransport(status=status)

    with pytest.raises(DNATError, match="different EIP"):
        _client(transport).plan_create(DNATSpec("tcp", 22, "10.0.0.2", 22))


@pytest.mark.parametrize("username", ["", "202500646", "l202500646", "L20A646"])
def test_invalid_configured_username_is_rejected_before_api_access(username):
    transport = FakeTransport()

    with pytest.raises(DNATError, match="L followed by digits"):
        DNATClient(transport, username)

    assert transport.calls == []


@pytest.mark.parametrize("username", ["L202500601", "L202500646", "L202500750"])
def test_account_number_selects_the_range_including_both_boundaries(username):
    transport = FakeTransport()
    plan = DNATClient(
        transport, username, uuid_factory=lambda: TEST_UUID
    ).plan_create(DNATSpec("tcp", 22, "10.0.0.2", 22))

    assert plan.eip_name == EIP_NAME
    assert plan.eip_display_name == EIP_DISPLAY_NAME


def test_a_different_account_range_selects_a_different_eip():
    alternate_name = "eip-zhicheng-alternate"
    alternate_display = "L202500751_L202500900_01e_用于dnat创建"
    alternate_rid = (
        f"/subscriptions/{EIP_SUBSCRIPTION}"
        f"/resourceGroups/{EIP_RESOURCE_GROUP}"
        f"/zones/{EIP_ZONE}/eips/{alternate_name}"
    )
    transport = FakeTransport(
        resources=[
            _resource(),
            _resource(name=alternate_name, display_name=alternate_display),
        ],
        status=_status(name=alternate_name, display_name=alternate_display),
    )

    plan = DNATClient(
        transport, "L202500800", uuid_factory=lambda: TEST_UUID
    ).plan_create(DNATSpec("tcp", 22, "10.0.0.2", 22))

    assert plan.eip_name == alternate_name
    assert plan.eip_rid == alternate_rid
    assert plan.payload["id"].startswith(
        f"/resourceGroups/{EIP_RESOURCE_GROUP}/zones/{EIP_ZONE}"
        f"/eips/{alternate_name}/dnatRules/"
    )


def test_no_matching_or_multiple_matching_ranges_fail_closed():
    no_match = FakeTransport()
    with pytest.raises(DNATError, match="no EIP display-name account range"):
        DNATClient(no_match, "L202500751").plan_create(
            DNATSpec("tcp", 22, "10.0.0.2", 22)
        )

    duplicate = FakeTransport(
        resources=[
            _resource(),
            _resource(
                name="eip-zhicheng-overlap",
                display_name="L202500640_L202500700_01e_other",
            ),
        ]
    )
    with pytest.raises(DNATError, match="multiple EIP display-name account ranges"):
        _client(duplicate).plan_create(DNATSpec("tcp", 22, "10.0.0.2", 22))

    assert [call[0] for call in duplicate.calls] == ["POST"]


def test_account_matched_resource_must_use_the_allowed_rid():
    transport = FakeTransport(
        resources=[
            _resource(
                rid=(
                    f"/subscriptions/{EIP_SUBSCRIPTION}"
                    f"/resourceGroups/other/zones/{EIP_ZONE}/eips/{EIP_NAME}"
                )
            )
        ]
    )

    with pytest.raises(DNATError, match="outside the allowed subscription"):
        _client(transport).plan_create(DNATSpec("tcp", 22, "10.0.0.2", 22))

    assert [call[0] for call in transport.calls] == ["POST"]


def test_incomplete_eip_resource_list_fails_closed():
    class IncompleteResourceTransport(FakeTransport):
        def request(self, method, url, **kwargs):
            if method == "POST" and url == EIP_RESOURCE_PAGE_URL:
                self.calls.append(
                    (
                        method,
                        url,
                        kwargs.get("params"),
                        kwargs.get("json_body"),
                        kwargs.get("timeout", 60.0),
                    )
                )
                return {"resources": [_resource()], "total_size": 2}
            return super().request(method, url, **kwargs)

    transport = IncompleteResourceTransport()
    with pytest.raises(DNATError, match="every EIP resource"):
        _client(transport).plan_create(DNATSpec("tcp", 22, "10.0.0.2", 22))


def test_display_range_change_between_list_and_status_fails_closed():
    transport = FakeTransport(
        status=_status(display_name="L202500751_L202500900_01e_changed")
    )

    with pytest.raises(DNATError, match="changed during planning"):
        _client(transport).plan_create(DNATSpec("tcp", 22, "10.0.0.2", 22))


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (DNATSpec("icmp", 22, "10.0.0.2", 22), "tcp or udp"),
        (DNATSpec("tcp", 0, "10.0.0.2", 22), "between 1 and 65535"),
        (DNATSpec("tcp", "100-99", "10.0.0.2", 22), "range end"),
        (DNATSpec("tcp", "1-501", "10.0.0.2", "1-501"), "at most 500"),
        (DNATSpec("tcp", "1-2", "10.0.0.2", "10-12"), "same number"),
        (DNATSpec("tcp", 22, "::1", 22), "IPv4"),
        (DNATSpec("tcp", 22, "127.0.0.1", 22), "unicast VPC"),
    ],
)
def test_invalid_specs_fail_before_any_api_request(spec, message):
    transport = FakeTransport()

    with pytest.raises(DNATError, match=message):
        _client(transport).plan_create(spec)

    assert transport.calls == []


def test_overlapping_port_is_rejected_only_for_the_same_protocol():
    tcp_transport = FakeTransport(rules=[_rule(port="2000-2010")])
    with pytest.raises(DNATError, match="conflicts"):
        _client(tcp_transport).plan_create(
            DNATSpec("tcp", "2005-2015", "10.0.0.2", "3000-3010")
        )

    udp_transport = FakeTransport(rules=[_rule(port="2000-2010")])
    plan = _client(udp_transport).plan_create(
        DNATSpec("udp", "2005-2015", "10.0.0.2", "3000-3010")
    )
    assert plan.spec.protocol == "udp"


def test_incomplete_rule_list_fails_closed():
    class IncompleteTransport(FakeTransport):
        def request(self, method, url, **kwargs):
            if method == "GET" and url == RULES_URL:
                self.calls.append((method, url, None, None, 60.0))
                return {"dnat_rules": [_rule()], "total_size": 2}
            return super().request(method, url, **kwargs)

    transport = IncompleteTransport()
    with pytest.raises(DNATError, match="every existing"):
        _client(transport).plan_create(DNATSpec("tcp", 22, "10.0.0.2", 22))


def test_ten_in_flight_changes_fail_closed_without_treating_total_as_the_limit():
    changing = [
        _rule(
            name=f"dnat-zhicheng-{index:08x}",
            port=str(1000 + index),
            state="CREATING",
        )
        for index in range(10)
    ]
    transport = FakeTransport(rules=changing)
    with pytest.raises(DNATError, match="10 DNAT rules"):
        _client(transport).plan_create(DNATSpec("tcp", 22, "10.0.0.2", 22))

    many_active = [
        _rule(name=f"dnat-zhicheng-{index:08x}", port=str(1000 + index))
        for index in range(20)
    ]
    plan = _client(FakeTransport(rules=many_active)).plan_create(
        DNATSpec("tcp", 22, "10.0.0.2", 22)
    )
    assert plan.rule_name.endswith("12345678")


def test_create_issues_exactly_one_post_and_returns_the_plan():
    transport = FakeTransport(post_response={"state": "CREATING"})
    result = _client(transport).create(DNATSpec("tcp", 2222, "10.0.0.2", 22))

    posts = [
        call
        for call in transport.calls
        if call[0] == "POST" and "/dnatRules/" in call[1]
    ]
    assert len(posts) == 1
    assert posts[0][1] == RULES_URL + "/" + result.plan.rule_name
    assert posts[0][3] == result.plan.payload
    assert result.to_dict()["state"] == "CREATING"


def test_create_transport_failure_is_not_retried_and_names_uncertain_rule():
    transport = FakeTransport()
    transport.post_error = RuntimeError("connection lost")

    with pytest.raises(DNATCreateUncertain, match="dnat-zhicheng-12345678"):
        _client(transport).create(DNATSpec("tcp", 2222, "10.0.0.2", 22))

    assert sum(
        call[0] == "POST" and "/dnatRules/" in call[1]
        for call in transport.calls
    ) == 1


def test_non_successful_http_response_is_reported_without_post_retry():
    transport = FakeTransport(
        post_response=SimpleNamespace(status=409, text='{"message":"conflict"}')
    )

    with pytest.raises(DNATAPIError, match="HTTP 409"):
        _client(transport).create(DNATSpec("tcp", 2222, "10.0.0.2", 22))

    assert sum(
        call[0] == "POST" and "/dnatRules/" in call[1]
        for call in transport.calls
    ) == 1
