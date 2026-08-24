from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slaigpus.cci import CCIError  # noqa: E402
from slaigpus.monitor import (  # noqa: E402
    ACP_CONTAINER_NAME,
    ACP_HOST_IP,
    ACP_JOB_NAME,
    ACP_POD_NAME,
    ACP_PRODUCTS,
    MONITOR,
    MONITOR_BASE_PATH,
    MonitorAPIError,
    MonitorClient,
    MonitorError,
    TelemetryStationRef,
    choose_histogram_interval,
    custom_filter,
    extract_log_hits,
    format_log_hit,
    normalize_log_page,
    select_acp_product,
)


STATION = (
    "/subscriptions/subscription-1/resourceGroups/default/zones/cn-sh-01z/"
    "telemetryStations/private-station-1"
)
WORKSPACE = (
    "/subscriptions/subscription-1/resourceGroups/default/zones/cn-sh-01z/"
    "workspaces/example-workspace"
)
RESOURCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_RESOURCE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
BASE = (
    MONITOR
    + "/monitor/ts/data/v1/subscriptions/subscription-1/resourceGroups/default/"
    "zones/cn-sh-01z/telemetryStations/private-station-1"
)


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.refresh_calls = []

    def request(self, method, url, *, json_body=None, timeout=60.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("unexpected Monitor request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def refresh_auth(self, *, timeout):
        self.refresh_calls.append(timeout)


def response(status, data=None, *, text=None):
    payload = text if text is not None else json.dumps(data or {})
    return SimpleNamespace(status=status, text=payload)


def test_monitor_constants_and_station_resource_build_the_exact_v1_base():
    assert MONITOR == "https://monitor.sensecoreapi.cn"
    assert MONITOR_BASE_PATH == (
        "/monitor/ts/data/v1/subscriptions/subscription_name/"
        "resourceGroups/resource_group_name/zones/zone/"
        "telemetryStations/telemetry_station_name"
    )

    station = TelemetryStationRef.parse(STATION)

    assert station.resource_id == STATION
    assert station.subscription == "subscription-1"
    assert station.subscription_name == "subscription-1"
    assert station.resource_group == "default"
    assert station.resource_group_name == "default"
    assert station.zone == "cn-sh-01z"
    assert station.telemetry_station == "private-station-1"
    assert station.telemetry_station_name == station.name == "private-station-1"
    assert station.base_url == BASE


def test_station_components_are_encoded_and_invalid_resource_ids_are_rejected():
    station = TelemetryStationRef.parse(
        "/subscriptions/sub id/resourceGroups/my group/zones/cn sh/"
        "telemetryStations/private station"
    )
    assert station.base_url.endswith(
        "/subscriptions/sub%20id/resourceGroups/my%20group/zones/cn%20sh/"
        "telemetryStations/private%20station"
    )

    invalid = [
        "",
        WORKSPACE,
        STATION + "/extra",
        STATION + "?next=evil",
        STATION.replace("private-station-1", ".."),
        STATION.replace("private-station-1", "bad\\name"),
    ]
    for value in invalid:
        with pytest.raises(MonitorError):
            TelemetryStationRef.parse(value)


def test_list_products_uses_get_and_tolerates_a_nested_schema():
    products = [
        {"key": ACP_PRODUCTS[0]},
        {"key": ACP_PRODUCTS[1], "label": "ACP new"},
    ]
    transport = FakeTransport({"data": {"products": products}})
    client = MonitorClient(transport, STATION)

    assert client.list_products(timeout=7) == products
    assert transport.calls == [
        {
            "method": "GET",
            "url": BASE + "/logStream/products",
            "json_body": None,
            "timeout": 7.0,
        }
    ]


def test_list_resources_uses_the_exact_safely_encoded_get_query_and_wrapper():
    resources = [
        {
            "name": "example-workspace",
            "display_name": "Example Workspace",
            "resource_id": RESOURCE_ID,
        }
    ]
    transport = FakeTransport({"response": {"data": {"items": resources}}})
    client = MonitorClient(transport, STATION)

    assert client.list_resources("product/name &extra=bad", timeout=8) == resources
    assert transport.calls == [
        {
            "method": "GET",
            "url": BASE + "/resources?product=product%2Fname%20%26extra%3Dbad",
            "json_body": None,
            "timeout": 8.0,
        }
    ]


@pytest.mark.parametrize("server_name", ["example-workspace", WORKSPACE])
def test_resolve_resource_id_uniquely_matches_short_or_full_workspace_name(
    server_name,
):
    transport = FakeTransport(
        {
            "data": {
                "resources": [
                    {
                        "name": "another-workspace",
                        "resource_id": OTHER_RESOURCE_ID,
                    },
                    {
                        "name": server_name,
                        "display_name": "Example Workspace",
                        "resource_id": RESOURCE_ID,
                    },
                ]
            }
        }
    )
    client = MonitorClient(transport, STATION)

    assert (
        client.resolve_resource_id(ACP_PRODUCTS[1], WORKSPACE, timeout=9)
        == RESOURCE_ID
    )
    assert transport.calls == [
        {
            "method": "GET",
            "url": BASE + "/resources?product=product.lepton-acp-new",
            "json_body": None,
            "timeout": 9.0,
        }
    ]


def test_resolve_resource_id_rejects_no_match_without_leaking_response():
    transport = FakeTransport(
        {
            "resources": [
                {
                    "name": "secret-other-workspace",
                    "resource_id": OTHER_RESOURCE_ID,
                    "secret": "do-not-leak",
                }
            ]
        }
    )
    client = MonitorClient(transport, STATION)

    with pytest.raises(MonitorError, match="no resource") as error:
        client.resolve_resource_id(ACP_PRODUCTS[1], WORKSPACE)

    assert "secret" not in str(error.value)
    assert OTHER_RESOURCE_ID not in str(error.value)


def test_resolve_resource_id_rejects_duplicate_and_ambiguous_matches():
    duplicate = {
        "resources": [
            {"name": "example-workspace", "resource_id": RESOURCE_ID},
            {"name": WORKSPACE, "resource_id": RESOURCE_ID},
        ]
    }
    ambiguous = {
        "resources": [
            {"name": "example-workspace", "resource_id": RESOURCE_ID},
            {"name": WORKSPACE, "resource_id": OTHER_RESOURCE_ID},
        ]
    }
    client = MonitorClient(FakeTransport(duplicate, ambiguous), STATION)

    with pytest.raises(MonitorError, match="duplicate") as duplicate_error:
        client.resolve_resource_id(ACP_PRODUCTS[1], WORKSPACE)
    with pytest.raises(MonitorError, match="ambiguous") as ambiguous_error:
        client.resolve_resource_id(ACP_PRODUCTS[1], WORKSPACE)

    assert RESOURCE_ID not in str(duplicate_error.value)
    assert OTHER_RESOURCE_ID not in str(ambiguous_error.value)


@pytest.mark.parametrize(
    "invalid_id",
    [
        None,
        "",
        "not-a-uuid",
        RESOURCE_ID.upper(),
        RESOURCE_ID + "?token=do-not-leak",
        RESOURCE_ID.replace("-", ""),
    ],
)
def test_resolve_resource_id_rejects_invalid_or_malicious_monitor_ids(invalid_id):
    transport = FakeTransport(
        {"resources": [{"name": "example-workspace", "resource_id": invalid_id}]}
    )
    client = MonitorClient(transport, STATION)

    with pytest.raises(MonitorError, match="canonical Monitor resource UUID") as error:
        client.resolve_resource_id(ACP_PRODUCTS[1], WORKSPACE)

    if invalid_id:
        assert str(invalid_id) not in str(error.value)


@pytest.mark.parametrize(
    "workspace",
    [
        "example-workspace",
        WORKSPACE + "?workspace=evil",
        WORKSPACE + "#fragment",
        WORKSPACE.replace("example-workspace", ".."),
        " " + WORKSPACE,
    ],
)
def test_resolve_resource_id_rejects_noncanonical_workspace_before_get(workspace):
    transport = FakeTransport()
    client = MonitorClient(transport, STATION)

    with pytest.raises(MonitorError):
        client.resolve_resource_id(ACP_PRODUCTS[1], workspace)

    assert transport.calls == []


def test_acp_product_selection_is_exact_and_prefers_new():
    products = [
        {"key": "product.not-acp", "label": "ACP lookalike"},
        {"key": ACP_PRODUCTS[0]},
        {"key": ACP_PRODUCTS[1]},
    ]

    assert select_acp_product(products) == ACP_PRODUCTS[1]
    assert select_acp_product(products, explicit=ACP_PRODUCTS[0]) == ACP_PRODUCTS[0]
    with pytest.raises(MonitorError, match="known ACP"):
        select_acp_product(products, explicit="product.not-acp")
    with pytest.raises(MonitorError, match="no ACP"):
        select_acp_product([{"key": "product.not-acp", "label": "ACP"}])

    assert select_acp_product(
        {"data": {"items": {"records": [{"label": ACP_PRODUCTS[0]}]}}}
    ) == ACP_PRODUCTS[0]


def test_client_explicit_acp_product_does_not_fetch_products():
    transport = FakeTransport()
    client = MonitorClient(transport, STATION)

    assert client.select_acp_product(ACP_PRODUCTS[0]) == ACP_PRODUCTS[0]
    assert transport.calls == []


@pytest.mark.parametrize(
    ("duration", "expected"),
    [
        (0, "60s"),
        (1, "60s"),
        (10 * 60, "60s"),
        (10 * 60 + 1, "10m"),
        (60 * 60, "10m"),
        (60 * 60 + 1, "1h"),
        (6 * 60 * 60, "1h"),
        (6 * 60 * 60 + 1, "3h"),
        (24 * 60 * 60, "3h"),
        (3 * 24 * 60 * 60, "12h"),
        (7 * 24 * 60 * 60, "24h"),
        (15 * 24 * 60 * 60, "48h"),
        (15 * 24 * 60 * 60 + 1, "72h"),
    ],
)
def test_histogram_interval_uses_console_discrete_bands(duration, expected):
    assert choose_histogram_interval("100", str(100 + duration)) == expected


def test_query_logs_sends_the_exact_read_only_post_body_and_normalizes_hits():
    hit = {
        "id": "log-1",
        "log_time": "2026-08-24T10:00:00Z",
        "body": "ready",
    }
    transport = FakeTransport(
        {
            "data": {
                "hits": [hit],
                "histogram": [{"string_time": "100", "doc_count": 1}],
                "total": 1,
                "offset": "2",
                "page_size": 40,
                "future_field": "preserved",
            }
        }
    )
    client = MonitorClient(transport, STATION)
    filters = [
        {"key": ACP_JOB_NAME, "value": "job-one"},
        {"key": ACP_POD_NAME, "value": "pod-one"},
        {"key": ACP_CONTAINER_NAME, "value": "main"},
        {"key": ACP_HOST_IP, "value": "10.0.0.8"},
    ]

    page = client.query_logs(
        ACP_PRODUCTS[1],
        start=100,
        end=100 + 2 * 60 * 60,
        resource_id=RESOURCE_ID,
        page_size=40,
        offset=2,
        order="DESC",
        filter="error OR warning",
        custom_filter=filters,
        timeout=11,
    )

    assert page["hits"] == [hit]
    assert page["histogram"] == [{"string_time": "100", "doc_count": 1}]
    assert page["total"] == 1
    assert page["future_field"] == "preserved"
    assert transport.calls == [
        {
            "method": "POST",
            "url": BASE + "/logStream/products/product.lepton-acp-new/logs",
            "json_body": {
                "start": "100",
                "end": "7300",
                "resource_id": [RESOURCE_ID],
                "page_size": 40,
                "offset": "2",
                "order": "desc",
                "histogram_interval": "1h",
                "filter": "error OR warning",
                "custom_filter": filters,
            },
            "timeout": 11.0,
        }
    ]


def test_query_logs_omits_optional_filters_and_accepts_explicit_interval():
    transport = FakeTransport({"hits": [], "total": 0})
    client = MonitorClient(transport, STATION)

    assert client.query_logs(
        "generic/product",
        start="100",
        end="200",
        resource_id=RESOURCE_ID,
        histogram_interval="5m",
    )["hits"] == []

    call = transport.calls[0]
    assert call["url"].endswith("/products/generic%2Fproduct/logs")
    assert call["json_body"] == {
        "start": "100",
        "end": "200",
        "resource_id": [RESOURCE_ID],
        "page_size": 40,
        "offset": "0",
        "order": "desc",
        "histogram_interval": "5m",
    }


def test_logs_401_refreshes_and_retries_exactly_once():
    transport = FakeTransport(
        response(401, {"error": "expired"}),
        response(200, {"hits": [{"body": "after refresh"}]}),
    )
    client = MonitorClient(transport, STATION)

    page = client.query_logs(
        ACP_PRODUCTS[0],
        start=100,
        end=200,
        resource_id=RESOURCE_ID,
        timeout=90,
    )

    assert page["hits"] == [{"body": "after refresh"}]
    assert len(transport.calls) == 2
    assert transport.calls[0] == transport.calls[1]
    assert transport.refresh_calls == [60.0]


def test_second_logs_401_is_not_retried_again():
    transport = FakeTransport(response(401), response(401))
    client = MonitorClient(transport, STATION)

    with pytest.raises(MonitorAPIError) as error:
        client.query_logs(
            ACP_PRODUCTS[0],
            start=100,
            end=200,
            resource_id=RESOURCE_ID,
        )

    assert error.value.status == 401
    assert len(transport.calls) == 2
    assert transport.refresh_calls == [60.0]


def test_products_get_401_never_uses_post_query_retry_policy():
    transport = FakeTransport(response(401))
    client = MonitorClient(transport, STATION)

    with pytest.raises(MonitorAPIError):
        client.list_products()

    assert len(transport.calls) == 1
    assert transport.refresh_calls == []


def test_unknown_post_cannot_opt_into_the_read_only_401_retry_allowlist():
    transport = FakeTransport(response(401))
    client = MonitorClient(transport, STATION)

    with pytest.raises(MonitorAPIError):
        client._request(
            "POST",
            BASE + "/not-a-log-query",
            body={},
            retry_readonly_post=True,
        )

    assert len(transport.calls) == 1
    assert transport.refresh_calls == []


def test_custom_filter_values_uses_product_scope_and_tolerates_wrappers():
    transport = FakeTransport(
        response(
            200,
            {
                "result": {
                    "values": [
                        "container-a",
                        {"value": "container-b", "label": "Container B"},
                        {"name": "container-c"},
                        None,
                    ]
                }
            },
        )
    )
    client = MonitorClient(transport, STATION)

    values = client.custom_filter_values(
        ACP_PRODUCTS[1],
        key=ACP_CONTAINER_NAME,
        resource_id=RESOURCE_ID,
        custom_filters=[custom_filter(ACP_JOB_NAME, "job-one")],
    )

    assert values == ["container-a", "container-b", "container-c"]
    assert transport.calls[0]["url"] == (
        BASE
        + "/logStream/products/product.lepton-acp-new/customFilterValues"
    )
    assert transport.calls[0]["json_body"] == {
        "key": ACP_CONTAINER_NAME,
        "resource_id": [RESOURCE_ID],
        "custom_filters": [{"key": ACP_JOB_NAME, "value": "job-one"}],
    }


def test_custom_filter_values_is_the_other_refreshable_read_only_post():
    transport = FakeTransport(response(401), response(200, {"values": ["pod-a"]}))
    client = MonitorClient(transport, STATION)

    assert client.custom_filter_values(
        ACP_PRODUCTS[0],
        key=ACP_POD_NAME,
        resource_id=RESOURCE_ID,
    ) == ["pod-a"]
    assert len(transport.calls) == 2
    assert transport.refresh_calls == [60.0]


def test_schema_tolerant_log_hit_extraction_and_formatting():
    hit = {
        "id": "hit-1",
        "resource": {"resource_id": WORKSPACE},
        "log_time": "2026-08-24T10:11:12Z",
        "severity_text": "INFO",
        "body": "training started",
        "attributes": {
            "k8s.pod.name": "trainer-pod",
            "k8s.container.name": "trainer",
            "k8s.host.ip": "10.0.0.9",
        },
    }
    wrapped = {"response": {"hits": {"hits": [hit], "total": {"value": 1}}}}

    assert extract_log_hits(wrapped) == [hit]
    assert normalize_log_page(wrapped)["total"] == {"value": 1}
    assert format_log_hit(hit) == (
        "2026-08-24T10:11:12Z [INFO] "
        "trainer-pod/trainer@10.0.0.9 training started"
    )

    deeply_nested = {
        "timestamp": "100",
        "message": "nested",
        "attributes": {"k8s": {"pod": {"name": "pod-a"}}},
    }
    assert format_log_hit(deeply_nested) == "100 pod-a nested"
    assert format_log_hit("plain log") == "plain log"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": 100, "end": 100, "resource_id": RESOURCE_ID},
        {"start": "1.5", "end": 100, "resource_id": RESOURCE_ID},
        {"start": 100, "end": 200, "resource_id": ""},
        {"start": 100, "end": 200, "resource_id": WORKSPACE},
        {"start": 100, "end": 200, "resource_id": RESOURCE_ID, "page_size": 0},
        {"start": 100, "end": 200, "resource_id": RESOURCE_ID, "offset": -1},
        {"start": 100, "end": 200, "resource_id": RESOURCE_ID, "order": "newest"},
        {"start": 100, "end": 200, "resource_id": RESOURCE_ID, "filter": 3},
        {
            "start": 100,
            "end": 200,
            "resource_id": RESOURCE_ID,
            "custom_filter": {"key": ACP_JOB_NAME, "value": "job"},
        },
        {
            "start": 100,
            "end": 200,
            "resource_id": RESOURCE_ID,
            "custom_filter": [{"key": ACP_JOB_NAME, "value": "job", "extra": 1}],
        },
    ],
)
def test_invalid_log_queries_fail_before_the_transport(kwargs):
    transport = FakeTransport()
    client = MonitorClient(transport, STATION)

    with pytest.raises(MonitorError):
        client.query_logs(ACP_PRODUCTS[0], **kwargs)

    assert transport.calls == []


def test_monitor_errors_inherit_cci_error_and_invalid_json_is_redacted():
    assert issubclass(MonitorError, CCIError)
    assert issubclass(MonitorAPIError, CCIError)
    transport = FakeTransport(response(200, text="not-json"))
    client = MonitorClient(transport, STATION)

    with pytest.raises(MonitorError, match="invalid JSON") as error:
        client.list_products()

    assert "not-json" not in str(error.value)
    assert error.value.__context__ is None


def test_transport_failures_cross_the_module_as_monitor_errors():
    transport = FakeTransport(RuntimeError("secret-bearing transport detail"))
    client = MonitorClient(transport, STATION)

    with pytest.raises(MonitorError, match="transport request failed") as error:
        client.list_products()

    assert "secret-bearing" not in str(error.value)
    assert error.value.__context__ is None


def test_refresh_failures_do_not_retain_the_transport_exception():
    class RefreshFailureTransport(FakeTransport):
        def refresh_auth(self, *, timeout):
            raise RuntimeError("secret-bearing refresh detail")

    client = MonitorClient(RefreshFailureTransport(response(401)), STATION)

    with pytest.raises(MonitorError, match="authorization refresh failed") as error:
        client.query_logs(
            ACP_PRODUCTS[0],
            start=100,
            end=200,
            resource_id=RESOURCE_ID,
        )

    assert "secret-bearing" not in str(error.value)
    assert error.value.__context__ is None
