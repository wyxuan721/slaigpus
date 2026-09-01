"""Offline tests for SenseCore CCI discovery and renewal safety.

The fakes deliberately model only the browser/API boundary used by
``slaigpus.cci``.  No SenseCore credentials, browser, network, or wall-clock
waiting is needed.
"""

from __future__ import annotations

import copy
import json
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import slaigpus.cci as cci  # noqa: E402
from slaigpus.cci import (  # noqa: E402
    CCI_HARD_LIMIT_SECONDS,
    CCIAPIError,
    CCIError,
    CCIStatus,
    CCITarget,
    AutoRenewControlStore,
    LockBusy,
    RenewalStateStore,
    RenewalSupervisor,
    SenseCoreClient,
    TargetAmbiguous,
    TargetResolver,
    WorkspaceRef,
    default_cci_state_root,
    parse_duration,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)
WORKSPACE = (
    "/subscriptions/example-subscription/"
    "resourceGroups/default/zones/cn-sh-01z/workspaces/example-workspace"
)


def test_cci_state_root_keeps_macos_layout_and_uses_xdg_elsewhere():
    mac_home = Path("/Users/tester")
    linux_home = Path("/home/tester")

    assert default_cci_state_root(
        platform="darwin", environ={}, home=mac_home
    ) == (
        mac_home / "Library" / "Application Support" / "slaigpus" / "cci"
    )
    assert default_cci_state_root(
        platform="linux",
        environ={"XDG_STATE_HOME": "/srv/state"},
        home=linux_home,
    ) == Path("/srv/state/slaigpus/cci")


def make_app(name="example-cci", *, containers=None):
    if containers is None:
        containers = [
            {
                "name": "trainer",
                "image_path": "registry.example.cn/team/base:v1",
                "env": [{"name": "MODE", "value": "debug"}],
            }
        ]
    return {
        "name": name,
        "display_name": name,
        "template": {
            "containers": copy.deepcopy(containers),
            "init_containers": [
                {"name": "init-data", "image_path": "registry/init:v1"}
            ],
            "volumes": [{"name": "workspace", "empty_dir": {}}],
            "restart_policy": "Always",
        },
    }


def make_instance(
    started_at,
    *,
    name="example-cci-0",
    state="RUNNING",
    container_state="RUNNING",
    ready=True,
):
    return {
        "name": name,
        "uid": f"uid-{name}",
        "state": state,
        "last_started_time": started_at.isoformat(),
        "container_infos": [
            {
                "name": "trainer",
                "container_state": container_state,
                "ready": ready,
            }
        ],
    }


def make_target(started_at, *, namespace="team", app=None, instance=None):
    app = copy.deepcopy(app or make_app())
    instance = copy.deepcopy(instance or make_instance(started_at))
    return CCITarget(
        app=app,
        instance=instance,
        container=app["template"]["containers"][0],
        namespace=namespace,
    )


def with_target_image(target, uri):
    target.app["template"]["containers"][0]["image_path"] = uri
    target.container = target.app["template"]["containers"][0]
    return target


class RecordingTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class RefreshingResponseTransport(RecordingTransport):
    def __init__(self, *responses):
        super().__init__(*responses)
        self.refresh_calls = []

    def refresh_auth(self, *, timeout):
        self.refresh_calls.append(timeout)


class DiscoveryClient:
    def __init__(
        self,
        *,
        apps,
        app_details,
        instances,
        snapshots=None,
        namespaces=None,
        namespace_infos=None,
    ):
        self.apps = apps
        self.app_details = app_details
        self.instances = instances
        self.snapshots = snapshots or {}
        self.namespaces = namespaces or []
        self.namespace_infos = namespace_infos or {}
        self.namespace_calls = 0
        self.namespace_info_calls = []

    def list_apps(self):
        return copy.deepcopy(self.apps)

    def get_app(self, name):
        return copy.deepcopy(self.app_details[name])

    def list_instances(self, name):
        return copy.deepcopy(self.instances[name])

    def list_snapshots(self, name):
        return copy.deepcopy(self.snapshots.get(name, []))

    def list_namespaces(self):
        self.namespace_calls += 1
        return copy.deepcopy(self.namespaces)

    def get_namespace_info(self, name):
        self.namespace_info_calls.append(name)
        return copy.deepcopy(self.namespace_infos[name])


class FakeClock:
    def __init__(self, now=NOW):
        self.current = now
        self.elapsed = 0.0
        self.sleeps = []

    def now(self):
        return self.current

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        seconds = float(seconds)
        self.sleeps.append(seconds)
        self.elapsed += seconds
        self.current += timedelta(seconds=seconds)


class SequenceResolver:
    """Return successive observed targets, then repeat the final one."""

    def __init__(self, *targets):
        self.targets = list(targets)
        self.calls = []

    def resolve(self, *, include_namespace=False):
        self.calls.append(include_namespace)
        index = min(len(self.calls) - 1, len(self.targets) - 1)
        return copy.deepcopy(self.targets[index])


class StartResolver:
    def __init__(self, app, *targets):
        self.app = copy.deepcopy(app)
        self.targets = list(targets)
        self.resolve_calls = 0

    def resolve_app(self):
        return copy.deepcopy(self.app)

    def resolve_replacement_instance(self, *, include_namespace=False):
        index = min(self.resolve_calls, len(self.targets) - 1)
        self.resolve_calls += 1
        return copy.deepcopy(self.targets[index])


class StartClient:
    def __init__(self, *, error=None):
        self.workspace = WorkspaceRef.parse(WORKSPACE)
        self.error = error
        self.start_calls = []

    def start_app(self, app_name):
        self.start_calls.append(app_name)
        if self.error is not None:
            raise self.error


class RenewalClient:
    """Scriptable client that records the snapshot/PATCH ordering."""

    def __init__(self, app, *, create_response, snapshot_results=()):
        self.workspace = WorkspaceRef.parse(WORKSPACE)
        self.app = copy.deepcopy(app)
        self.create_response = copy.deepcopy(create_response)
        self.snapshot_results = [copy.deepcopy(value) for value in snapshot_results]
        self.events = []
        self.create_calls = []
        self.patch_calls = []

    def list_snapshots(self, app_name):
        if self.snapshot_results:
            result = self.snapshot_results.pop(0)
        else:
            result = []
        states = ",".join(str(item.get("state")) for item in result)
        self.events.append(f"list:{states}")
        return copy.deepcopy(result)

    def create_snapshot(self, app_name, **kwargs):
        self.events.append(f"create:{self.create_response.get('state', '')}")
        self.create_calls.append((app_name, copy.deepcopy(kwargs)))
        return copy.deepcopy(self.create_response)

    def get_app(self, app_name):
        self.events.append("get_app")
        return copy.deepcopy(self.app)

    def update_container_image(self, app, container_name, uri):
        self.events.append("patch")
        self.patch_calls.append((copy.deepcopy(app), container_name, uri))
        for container in self.app["template"]["containers"]:
            if container["name"] == container_name:
                container["image_path"] = uri


def make_supervisor(
    tmp_path,
    monkeypatch,
    *,
    resolver,
    client,
    clock=None,
    renew_after=cci.DEFAULT_RENEW_AFTER,
    poll_interval=5,
    wait_timeout=60,
):
    clock = clock or FakeClock()
    # Avoid replacing the process-wide ``time.monotonic`` function.  The CCI
    # module only needs monotonic() here; sleep and wall time are injected.
    monkeypatch.setattr(cci, "time", SimpleNamespace(monotonic=clock.monotonic))
    supervisor = RenewalSupervisor(
        client,
        resolver,
        renew_after=renew_after,
        poll_interval=poll_interval,
        wait_timeout=wait_timeout,
        state_root=tmp_path,
        now=clock.now,
        sleep=clock.sleep,
    )
    return supervisor, clock


def success_snapshot(uri="registry.example.cn/team/slaigpus-slai:v2"):
    return {
        "uid": "snapshot-1",
        "name": "slaigpus-example-cci",
        "ccr_namespace": "team",
        "state": "SUCCESS",
        "uri": uri,
        "created_time": NOW.isoformat(),
    }


def test_workspace_ref_and_duration_parsing():
    ref = WorkspaceRef.parse(WORKSPACE)

    assert ref.subscription == "example-subscription"
    assert ref.resource_group == "default"
    assert ref.zone == "cn-sh-01z"
    assert ref.workspace == "example-workspace"
    assert ref.resource_id == WORKSPACE
    assert ref.region == "cn-sh-01"
    assert ref.api_base == "https://cci.cn-sh-01.sensecore.cn/compute/cci/data/v2"
    assert ref.ccr_base == "https://ccr.cn-sh-01.sensecoreapi.cn"
    assert ref.apps_path.endswith("/workspaces/example-workspace/apps")
    assert ref.owned_apps_path.endswith("/workspaces/example-workspace/appsOwn")

    assert parse_duration("3h40m") == 13_200
    assert parse_duration("1h2m3s") == 3_723
    assert parse_duration("0.5h") == 1_800
    assert parse_duration(30) == 30

    for invalid in ("", "3:40", "3h 40m", "0s", -1):
        with pytest.raises(CCIError):
            parse_duration(invalid)
    with pytest.raises(CCIError, match="workspace must be"):
        WorkspaceRef.parse("/subscriptions/x/workspaces/y")


def test_status_reports_renewal_and_hard_expiry_timestamps():
    started = NOW - timedelta(hours=3, minutes=50)
    status = CCIStatus(
        make_target(started),
        started,
        NOW,
        cci.DEFAULT_RENEW_AFTER,
    )

    data = status.to_dict()

    assert CCI_HARD_LIMIT_SECONDS == 4 * 3600
    assert data["checked_at"] == NOW.isoformat()
    assert data["renew_at"] == NOW.isoformat()
    assert data["expires_at"] == (NOW + timedelta(minutes=10)).isoformat()
    assert data["expires_in_seconds"] == 10 * 60
    assert data["expired"] is False
    assert data["due"] is True

    expired = CCIStatus(
        make_target(started),
        started,
        started + timedelta(seconds=CCI_HARD_LIMIT_SECONDS + 1),
        status.renew_after,
    ).to_dict()
    assert expired["expires_in_seconds"] == 0
    assert expired["expired"] is True


def test_resolve_app_does_not_require_a_live_instance():
    app = make_app()
    app["state"] = "SUSPENDED"
    client = DiscoveryClient(
        apps=[app],
        app_details={app["name"]: app},
        instances={app["name"]: []},
    )

    selected = TargetResolver(client).resolve_app()

    assert selected["name"] == "example-cci"
    assert selected["state"] == "SUSPENDED"


def test_start_suspended_app_once_and_wait_for_ready(tmp_path, monkeypatch):
    app = make_app()
    app["state"] = "SUSPENDED"
    starting = make_target(
        NOW,
        instance=make_instance(
            NOW,
            state="PENDING",
            container_state="WAITING",
            ready=False,
        ),
    )
    ready = make_target(NOW + timedelta(seconds=5))
    resolver = StartResolver(app, starting, ready)
    client = StartClient()
    supervisor, clock = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=resolver,
        client=client,
    )

    result = supervisor.start()

    assert result.action == "started"
    assert result.status.target.instance_name == "example-cci-0"
    assert client.start_calls == ["example-cci"]
    assert clock.sleeps == [5.0]


def test_start_running_ready_app_is_a_noop(tmp_path, monkeypatch):
    app = make_app()
    app["state"] = "RUNNING"
    resolver = StartResolver(app, make_target(NOW))
    client = StartClient()
    supervisor, clock = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=resolver,
        client=client,
    )

    result = supervisor.start()

    assert result.action == "already_running"
    assert client.start_calls == []
    assert clock.sleeps == []


def test_start_rejects_transitional_state_without_post(tmp_path, monkeypatch):
    app = make_app()
    app["state"] = "UPDATING"
    resolver = StartResolver(app, make_target(NOW))
    client = StartClient()
    supervisor, _clock = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=resolver,
        client=client,
    )

    with pytest.raises(CCIError, match="UPDATING"):
        supervisor.start()

    assert client.start_calls == []


def test_start_post_error_is_not_replayed(tmp_path, monkeypatch):
    app = make_app()
    app["state"] = "SUSPENDED"
    resolver = StartResolver(app, make_target(NOW))
    client = StartClient(error=CCIError("start response was lost"))
    supervisor, _clock = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=resolver,
        client=client,
    )

    with pytest.raises(CCIError, match="response was lost"):
        supervisor.start()

    assert client.start_calls == ["example-cci"]


def test_auto_discovers_app_running_instance_container_and_namespace():
    target_app = make_app()
    other_app = make_app("unrelated-a100")
    client = DiscoveryClient(
        apps=[other_app, target_app],
        app_details={
            "unrelated-a100": other_app,
            "example-cci": target_app,
        },
        instances={
            "unrelated-a100": [make_instance(NOW, name="other-0")],
            "example-cci": [
                make_instance(NOW - timedelta(hours=5), name="old-0", state="STOPPED"),
                make_instance(NOW - timedelta(hours=3), name="example-cci-0"),
            ],
        },
        snapshots={
            "example-cci": [
                {"state": "FAIL", "ccr_namespace": "ignore-me"},
                {"state": "SUCCESS", "ccr_namespace": "team"},
            ]
        },
        namespaces=[{"name": "should-not-be-needed"}],
    )

    target = TargetResolver(client, hints=["example-cci"]).resolve(
        include_namespace=True
    )

    assert target.app_name == "example-cci"
    assert target.instance_name == "example-cci-0"
    assert target.container_name == "trainer"
    assert target.namespace == "team"
    assert client.namespace_calls == 0
    assert client.namespace_info_calls == []


@pytest.mark.parametrize("selector", ["cci-internal-123", "我的调试 CCI"])
def test_explicit_cci_selector_accepts_name_or_unicode_display_name(selector):
    app = make_app("cci-internal-123")
    app["display_name"] = "我的调试 CCI"
    client = DiscoveryClient(
        apps=[app],
        app_details={app["name"]: app},
        instances={app["name"]: [make_instance(NOW, name="selected-0")]},
    )

    target = TargetResolver(client, app=selector).resolve()

    assert target.app_name == "cci-internal-123"
    assert target.instance_name == "selected-0"


def test_explicit_cci_selector_rejects_duplicate_display_names():
    first = make_app("first-internal")
    second = make_app("second-internal")
    first["display_name"] = second["display_name"] = "共享显示名称"
    client = DiscoveryClient(
        apps=[first, second],
        app_details={first["name"]: first, second["name"]: second},
        instances={first["name"]: [], second["name"]: []},
    )

    with pytest.raises(TargetAmbiguous, match="matched more than once"):
        TargetResolver(client, app="共享显示名称").resolve()


def test_explicit_cci_selector_does_not_match_resource_pool_labels():
    app = make_app("cci-internal")
    app["resource_pool"] = {"display_name": "not-a-cci-name"}
    client = DiscoveryClient(
        apps=[app],
        app_details={app["name"]: app},
        instances={app["name"]: [make_instance(NOW)]},
    )

    with pytest.raises(CCIError, match="unknown CCI name or display name"):
        TargetResolver(client, app="not-a-cci-name").resolve()


def test_unique_management_namespace_is_auto_discovered():
    app = make_app()
    client = DiscoveryClient(
        apps=[app],
        app_details={app["name"]: app},
        instances={app["name"]: [make_instance(NOW)]},
        namespaces=[{"name": "only-private-namespace"}],
    )

    target = TargetResolver(client).resolve(include_namespace=True)

    assert target.namespace == "only-private-namespace"
    assert client.namespace_calls == 1
    assert client.namespace_info_calls == []


def test_first_snapshot_uses_namespace_with_most_remaining_capacity():
    app = make_app(
        containers=[
            {
                "name": "trainer",
                # A base image path is not evidence of a prior CCI snapshot
                # destination and must not override the free-space comparison.
                "image_path": "registry.example.cn/team/base:v1",
            }
        ]
    )
    client = DiscoveryClient(
        apps=[app],
        app_details={app["name"]: app},
        instances={app["name"]: [make_instance(NOW)]},
        namespaces=[{"name": "team"}, {"name": "roomier"}],
        namespace_infos={
            "team": {
                "name": "team",
                "storageLimit": "4398046511104",
                "storageUsed": "4036004694520",
            },
            "roomier": {
                "name": "roomier",
                "storageLimit": "4398046511104",
                "storageUsed": "1515610276656",
            },
        },
    )

    target = TargetResolver(client).resolve(include_namespace=True)

    assert target.namespace == "roomier"
    assert client.namespace_info_calls == ["roomier", "team"]


@pytest.mark.parametrize(
    "invalid_info",
    [
        {"name": "beta", "storageLimit": "100"},
        {"name": "beta", "storageLimit": "100", "storageUsed": "NaN"},
        {"name": "beta", "storageLimit": 100, "storageUsed": 101},
        {"name": "different", "storageLimit": "100", "storageUsed": "1"},
    ],
)
def test_first_snapshot_rejects_unreliable_namespace_capacity(invalid_info):
    app = make_app()
    client = DiscoveryClient(
        apps=[app],
        app_details={app["name"]: app},
        instances={app["name"]: [make_instance(NOW)]},
        namespaces=[{"name": "alpha"}, {"name": "beta"}],
        namespace_infos={
            "alpha": {
                "name": "alpha",
                "storageLimit": "100",
                "storageUsed": "50",
            },
            "beta": invalid_info,
        },
    )

    with pytest.raises(TargetAmbiguous, match="remaining capacity"):
        TargetResolver(client).resolve(include_namespace=True)


def test_explicit_namespace_bypasses_snapshot_and_capacity_discovery():
    app = make_app()
    client = DiscoveryClient(
        apps=[app],
        app_details={app["name"]: app},
        instances={app["name"]: [make_instance(NOW)]},
        namespaces=[{"name": "alpha"}, {"name": "beta"}],
    )

    target = TargetResolver(client, namespace="chosen").resolve(
        include_namespace=True
    )

    assert target.namespace == "chosen"
    assert client.namespace_calls == 0
    assert client.namespace_info_calls == []


def test_ambiguous_app_requires_explicit_selector():
    first = make_app("first")
    second = make_app("second")
    client = DiscoveryClient(
        apps=[first, second],
        app_details={"first": first, "second": second},
        instances={"first": [], "second": []},
    )

    with pytest.raises(TargetAmbiguous, match="--cci"):
        TargetResolver(client).resolve()


def test_ambiguous_running_instance_requires_explicit_selector():
    app = make_app()
    client = DiscoveryClient(
        apps=[app],
        app_details={app["name"]: app},
        instances={
            app["name"]: [
                make_instance(NOW, name="replica-0"),
                make_instance(NOW, name="replica-1"),
            ]
        },
    )

    with pytest.raises(TargetAmbiguous, match="--cci-instance"):
        TargetResolver(client).resolve()


def test_ambiguous_container_requires_explicit_selector():
    app = make_app(
        containers=[
            {"name": "trainer", "image_path": "registry/team/a:v1"},
            {"name": "sidecar", "image_path": "registry/team/b:v1"},
        ]
    )
    client = DiscoveryClient(
        apps=[app],
        app_details={app["name"]: app},
        instances={app["name"]: [make_instance(NOW)]},
    )

    with pytest.raises(TargetAmbiguous, match="--cci-container"):
        TargetResolver(client).resolve()


def test_tied_largest_namespace_capacity_requires_explicit_selector():
    app = make_app()
    client = DiscoveryClient(
        apps=[app],
        app_details={app["name"]: app},
        instances={app["name"]: [make_instance(NOW)]},
        namespaces=[{"name": "alpha"}, {"name": "beta"}],
        namespace_infos={
            "alpha": {
                "name": "alpha",
                "storageLimit": "100",
                "storageUsed": "25",
            },
            "beta": {
                "name": "beta",
                "storageLimit": "200",
                "storageUsed": "125",
            },
        },
    )

    with pytest.raises(TargetAmbiguous, match="largest remaining capacity is tied"):
        TargetResolver(client).resolve(include_namespace=True)


def test_list_apps_uses_owned_collection_and_parses_apps():
    apps = [make_app("first"), make_app("second")]
    transport = RecordingTransport({"apps": apps, "page_token": 2})
    client = SenseCoreClient(transport, WORKSPACE)

    result = client.list_apps()

    assert result == apps
    assert result is not apps
    assert len(transport.calls) == 1
    request = transport.calls[0]
    assert request["method"] == "GET"
    assert request["url"] == (
        client.workspace.api_base + client.workspace.owned_apps_path
    )
    assert request["url"].endswith("/workspaces/example-workspace/appsOwn")
    assert request["params"] == {"page_size": 500, "page_token": 1}
    assert request["json_body"] is None
    assert request["headers"] == {"x-ui-valid": "x-ui-valid"}


def test_namespace_info_uses_exact_ccr_console_endpoint_and_byte_fields():
    info = {
        "name": "ccr team/one",
        "storageLimit": "4398046511104",
        "storageUsed": "1515610276656",
    }
    transport = RecordingTransport(info)
    client = SenseCoreClient(transport, WORKSPACE)

    assert client.get_namespace_info("ccr team/one") == info

    request = transport.calls[0]
    assert request["method"] == "GET"
    assert request["url"] == (
        "https://ccr.cn-sh-01.sensecoreapi.cn/ccr/v1/"
        "subscriptions/example-subscription/"
        "resourceGroups/default/zones/cn-sh-01z/namespaces/"
        "ccr%20team%2Fone/info"
    )
    assert request["headers"] == {"x-ui-valid": "x-ui-valid"}


def test_get_401_refreshes_auth_and_retries_once():
    transport = RefreshingResponseTransport(
        SimpleNamespace(status=401, text='{"message":"expired"}'),
        SimpleNamespace(status=200, text='{"apps":[]}'),
    )
    client = SenseCoreClient(transport, WORKSPACE)

    assert client.list_apps() == []
    assert transport.refresh_calls == [60.0]
    assert [call["method"] for call in transport.calls] == ["GET", "GET"]


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_mutation_401_is_never_refreshed_or_replayed(method):
    transport = RefreshingResponseTransport(
        SimpleNamespace(status=401, text='{"message":"expired"}'),
        SimpleNamespace(status=200, text="{}"),
    )
    client = SenseCoreClient(transport, WORKSPACE)

    with pytest.raises(CCIAPIError, match=f"CCI API {method} failed with HTTP 401"):
        client._request(
            method,
            client.workspace.api_base + client.workspace.apps_path + "/target",
            body={"template": {}},
        )

    assert transport.refresh_calls == []
    assert [call["method"] for call in transport.calls] == [method]
    assert len(transport.responses) == 1


def test_start_app_uses_official_action_endpoint_without_a_body():
    transport = RecordingTransport({})
    client = SenseCoreClient(transport, WORKSPACE)

    client.start_app("example cci")

    assert len(transport.calls) == 1
    request = transport.calls[0]
    assert request["method"] == "POST"
    assert request["url"].endswith("/apps/example%20cci:start")
    assert request["json_body"] is None
    assert request["headers"] == {"x-ui-valid": "x-ui-valid"}


def test_app_detail_instances_snapshots_and_patch_stay_on_apps_resource():
    app = make_app()
    instance = make_instance(NOW)
    snapshot = success_snapshot()
    transport = RecordingTransport(
        app,
        {"instances": [instance]},
        {"snapshots": [snapshot]},
        {"uid": "snapshot-new"},
        {},
    )
    client = SenseCoreClient(transport, WORKSPACE)

    assert client.get_app(app["name"]) == app
    assert client.list_instances(app["name"]) == [instance]
    assert client.list_snapshots(app["name"]) == [snapshot]
    assert client.create_snapshot(
        app["name"],
        name="snapshot-name",
        display_name="snapshot display",
        namespace="team",
        container_name="trainer",
        instance_name=instance["name"],
    ) == {"uid": "snapshot-new"}
    client.update_container_image(
        app,
        "trainer",
        "registry.example.cn/team/snapshot:v2",
    )

    app_url = (
        client.workspace.api_base
        + client.workspace.apps_path
        + "/"
        + app["name"]
    )
    assert [(call["method"], call["url"]) for call in transport.calls] == [
        ("GET", app_url),
        ("GET", app_url + "/instances"),
        ("GET", app_url + "/snapshots"),
        ("POST", app_url + "/snapshots"),
        ("PATCH", app_url),
    ]
    assert all("/appsOwn" not in call["url"] for call in transport.calls)
    assert transport.calls[1]["params"] == {"page_size": 500, "page_token": 1}
    assert transport.calls[2]["params"] == {"page_size": 500, "page_token": 1}
    assert transport.calls[3]["params"] == {"client_type": 0}
    assert transport.calls[4]["params"] is None


def test_patch_preserves_api_template_and_only_replaces_selected_image():
    app = make_app(
        containers=[
            {
                "name": "trainer",
                "image_path": "registry/team/old:v1",
                # Preserve even UI-looking keys when the API returned them;
                # only image_path is authorized to change.
                "id": "server-container-id",
                "index": 0,
                "image": {"type": "private", "tag": "v1"},
                "type": "MAIN",
                "env": [{"name": "KEEP", "value": "yes"}],
                "resources": {"limits": {"gpu": "1"}},
            },
            {
                "name": "metrics",
                "image_path": "registry/team/metrics:v1",
                "ports": [{"container_port": 9000}],
            },
        ]
    )
    original = copy.deepcopy(app)
    transport = RecordingTransport({})
    client = SenseCoreClient(transport, WORKSPACE)

    client.update_container_image(
        app, "trainer", "registry.example.cn/team/snapshot:v2"
    )

    assert app == original, "the caller's app object must not be mutated"
    assert len(transport.calls) == 1
    request = transport.calls[0]
    assert request["method"] == "PATCH"
    assert request["url"].endswith("/apps/example-cci")
    assert request["params"] is None, "the current API does not accept update_mask"
    assert request["headers"] == {"x-ui-valid": "x-ui-valid"}
    assert "authorization" not in {key.lower() for key in request["headers"]}

    expected_template = copy.deepcopy(original["template"])
    expected_template["containers"][0]["image_path"] = (
        "registry.example.cn/team/snapshot:v2"
    )
    assert request["json_body"] == {"template": expected_template}


@pytest.mark.parametrize(
    ("age", "expected_action", "expected_creates"),
    [
        (timedelta(hours=3, minutes=49, seconds=59), "not_due", 0),
        (timedelta(hours=3, minutes=50), "renewed", 1),
    ],
)
def test_renewal_threshold_is_exact(
    tmp_path, monkeypatch, age, expected_action, expected_creates
):
    old = NOW - age
    initial = make_target(old)
    snapshot = success_snapshot()
    restarted = with_target_image(
        make_target(NOW + timedelta(seconds=1)), snapshot["uri"]
    )
    # The supervisor re-checks the live target immediately before PATCH, then
    # polls once more to confirm the restarted instance.
    resolver = SequenceResolver(initial, initial, restarted)
    client = RenewalClient(
        initial.app,
        create_response=snapshot,
        snapshot_results=[[]],
    )
    supervisor, _ = make_supervisor(
        tmp_path, monkeypatch, resolver=resolver, client=client
    )

    result = supervisor.renew(if_due=True)

    assert result.action == expected_action
    assert len(client.create_calls) == expected_creates
    assert len(client.patch_calls) == expected_creates
    if expected_action == "not_due":
        assert not supervisor.store.path.exists()


def test_manual_renew_is_not_blocked_by_disabled_auto_control(tmp_path, monkeypatch):
    old = NOW - timedelta(hours=3, minutes=50)
    initial = make_target(old)
    snapshot = success_snapshot()
    restarted = with_target_image(
        make_target(NOW + timedelta(seconds=1)), snapshot["uri"]
    )
    client = RenewalClient(
        initial.app,
        create_response=snapshot,
        snapshot_results=[[]],
    )
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(initial, initial, restarted),
        client=client,
    )
    supervisor.control.disable()

    result = supervisor.renew(if_due=True)

    assert result.action == "renewed"
    assert len(client.create_calls) == 1
    assert len(client.patch_calls) == 1


def test_new_renewal_requires_ready_container_before_snapshot(tmp_path, monkeypatch):
    old = NOW - timedelta(hours=3, minutes=50)
    not_ready = make_target(
        old,
        instance=make_instance(old, ready=False),
    )
    client = RenewalClient(not_ready.app, create_response=success_snapshot())
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(not_ready),
        client=client,
    )

    with pytest.raises(CCIError, match="not explicitly ready; snapshot was not requested"):
        supervisor.renew()

    assert client.create_calls == []
    assert client.patch_calls == []
    assert not supervisor.store.path.exists()


def test_snapshot_must_succeed_before_patch_and_restart_needs_new_start_time(
    tmp_path, monkeypatch
):
    old = NOW - timedelta(hours=3, minutes=50)
    initial = make_target(old)
    still_old = make_target(old)
    restarted_at = NOW + timedelta(seconds=15)
    snapshot = success_snapshot()
    restarted = with_target_image(make_target(restarted_at), snapshot["uri"])
    resolver = SequenceResolver(initial, still_old, restarted)
    client = RenewalClient(
        initial.app,
        create_response={"uid": "snapshot-1", "state": "CREATING"},
        # First lookup reconciles before POST; second observes completion.
        snapshot_results=[[], [snapshot]],
    )
    supervisor, clock = make_supervisor(
        tmp_path, monkeypatch, resolver=resolver, client=client
    )

    result = supervisor.renew(if_due=True)

    assert result.action == "renewed"
    assert result.status.started_at == restarted_at
    assert result.image_uri == snapshot["uri"]
    assert client.events.index("create:CREATING") < client.events.index("list:SUCCESS")
    assert client.events.index("list:SUCCESS") < client.events.index("patch")
    assert len(client.patch_calls) == 1
    # A RUNNING response with the old last_started_time was deliberately
    # returned once; it must cause another poll, not premature success.
    assert clock.sleeps
    assert resolver.calls == [True, False, False]
    assert not supervisor.store.path.exists()


def test_failed_snapshot_never_patches_and_state_contains_no_token(
    tmp_path, monkeypatch
):
    old = NOW - timedelta(hours=3, minutes=50)
    target = make_target(old)
    # Secret-looking response fields must never leak into durable state.
    response = {
        "uid": "snapshot-1",
        "state": "FAIL",
        "reason": "registry quota exceeded",
        "access_token": "SUPER-SECRET-TOKEN",
    }
    client = RenewalClient(target.app, create_response=response, snapshot_results=[[]])
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(target),
        client=client,
    )

    with pytest.raises(CCIError, match="FAIL.*CCI was not modified"):
        supervisor.renew(if_due=True)

    assert client.patch_calls == []
    persisted = supervisor.store.path.read_text(encoding="utf-8")
    assert "SUPER-SECRET-TOKEN" not in persisted
    assert "access_token" not in persisted.lower()
    assert json.loads(persisted)["stage"] == "snapshot_requested"
    assert stat.S_IMODE(supervisor.store.path.stat().st_mode) == 0o600


def test_snapshot_requested_state_resumes_without_duplicate_post(
    tmp_path, monkeypatch
):
    old = NOW - timedelta(hours=3, minutes=50)
    initial = make_target(old)
    snapshot = success_snapshot()
    restarted = with_target_image(
        make_target(NOW + timedelta(seconds=1)), snapshot["uri"]
    )
    client = RenewalClient(
        initial.app,
        create_response=AssertionError("resume must not submit another snapshot"),
        snapshot_results=[[snapshot]],
    )
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        # The first observation builds the interrupted state, the second is
        # the resumed preflight, the third is the pre-PATCH safety check, and
        # only the fourth is the restarted CCI.
        resolver=SequenceResolver(initial, initial, initial, restarted),
        client=client,
    )
    interrupted = supervisor._new_state(supervisor.status(include_namespace=True))
    interrupted.update(
        {
            "stage": "snapshot_requested",
            "snapshot_uid": "snapshot-1",
        }
    )
    supervisor.store.save(interrupted)

    result = supervisor.renew(if_due=True)

    assert result.action == "renewed"
    assert client.create_calls == []
    assert len(client.patch_calls) == 1
    assert not supervisor.store.path.exists()


def test_lost_snapshot_response_with_stale_list_is_never_repeated_and_recovers(
    tmp_path, monkeypatch
):
    old = NOW - timedelta(hours=3, minutes=50)
    initial = make_target(old)
    first_client = RenewalClient(
        initial.app,
        create_response={},
        snapshot_results=[[]],
    )
    first_supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(initial),
        client=first_client,
    )

    def lose_snapshot_response(app_name, **kwargs):
        assert first_supervisor.store.load()["stage"] == "snapshot_submitting"
        first_client.events.append("create:lost")
        first_client.create_calls.append((app_name, copy.deepcopy(kwargs)))
        raise CCIError("snapshot POST response was lost")

    monkeypatch.setattr(first_client, "create_snapshot", lose_snapshot_response)

    with pytest.raises(CCIError, match="snapshot POST response was lost"):
        first_supervisor.renew(if_due=True)

    interrupted = first_supervisor.store.load()
    assert interrupted["stage"] == "snapshot_submitting"
    assert len(first_client.create_calls) == 1

    snapshot = success_snapshot()
    snapshot.update(
        {
            "display_name": interrupted["snapshot_display_name"],
            "container_name": interrupted["container"],
            "instance_uuid": interrupted["instance"],
        }
    )
    restarted = with_target_image(
        make_target(NOW + timedelta(seconds=1)), snapshot["uri"]
    )
    # Model a rebuilt browser transport with a fresh client.  Its first list
    # is eventually consistent and still empty; only a later retry sees the
    # snapshot created by the request whose response was lost.
    second_client = RenewalClient(
        initial.app,
        create_response={},
        snapshot_results=[[], [snapshot]],
    )
    second_supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(initial, initial, initial, restarted),
        client=second_client,
    )

    with pytest.raises(CCIError, match="unknown outcome.*duplicate POST"):
        second_supervisor.renew(if_due=True)

    assert second_client.create_calls == []
    assert second_supervisor.store.load()["stage"] == "snapshot_submitting"

    result = second_supervisor.renew(if_due=True)

    assert result.action == "renewed"
    assert result.image_uri == snapshot["uri"]
    assert len(first_client.create_calls) == 1
    assert second_client.create_calls == []
    assert len(second_client.patch_calls) == 1
    assert not second_supervisor.store.path.exists()


def test_lost_patch_response_with_stale_get_is_never_repeated_and_can_recover(
    tmp_path, monkeypatch
):
    old = NOW - timedelta(hours=3, minutes=50)
    initial = make_target(old)
    snapshot = success_snapshot()
    manually_recovered = with_target_image(
        make_target(NOW + timedelta(seconds=1)), snapshot["uri"]
    )
    client = RenewalClient(
        initial.app,
        create_response=snapshot,
        snapshot_results=[[]],
    )
    # First renewal resolves twice (initial + pre-PATCH).  The next GET is
    # deliberately stale even though the PATCH call was recorded.  Finally,
    # model the documented manual recovery by applying the saved image.
    resolver = SequenceResolver(initial, initial, initial, manually_recovered)
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=resolver,
        client=client,
    )

    def lose_patch_response(app, container_name, uri):
        # The durable uncertainty marker must exist before the request leaves.
        assert supervisor.store.load()["stage"] == "patch_submitting"
        client.events.append("patch")
        client.patch_calls.append((copy.deepcopy(app), container_name, uri))
        raise CCIError("PATCH response was lost")

    monkeypatch.setattr(client, "update_container_image", lose_patch_response)

    with pytest.raises(CCIError, match="PATCH response was lost"):
        supervisor.renew(if_due=True)

    assert len(client.patch_calls) == 1
    assert supervisor.store.load()["stage"] == "patch_submitting"

    with pytest.raises(CCIError, match="unknown outcome.*manually"):
        supervisor.renew(if_due=True)

    assert len(client.patch_calls) == 1, "a stale GET must never trigger another PATCH"
    assert supervisor.store.load()["stage"] == "patch_submitting"

    result = supervisor.renew(if_due=True)

    assert result.action == "recovered"
    assert result.image_uri == snapshot["uri"]
    assert len(client.patch_calls) == 1
    assert not supervisor.store.path.exists()


@pytest.mark.parametrize("stage", ["image_ready", "patch_submitting", "patch_sent"])
def test_newer_instance_recovers_only_when_saved_image_is_applied(
    tmp_path, monkeypatch, stage
):
    old = NOW - timedelta(hours=3, minutes=50)
    current = with_target_image(
        make_target(NOW), "registry.example.cn/team/snapshot:v2"
    )
    client = RenewalClient(
        current.app,
        create_response=AssertionError("recovery must not submit a snapshot"),
    )
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(current),
        client=client,
    )
    supervisor.store.save(
        {
            "version": 2,
            "workspace": WORKSPACE,
            "app": current.app_name,
            "instance": current.instance_name,
            "container": current.container_name,
            "namespace": current.namespace,
            "old_started_at": old.isoformat(),
            "old_image_path": "registry.example.cn/team/base:v1",
            "requested_at": old.isoformat(),
            "snapshot_name": "slaigpus-example-cci",
            "snapshot_display_name": "slaigpus auto interrupted",
            "snapshot_uid": "snapshot-1",
            "image_uri": "registry.example.cn/team/snapshot:v2",
            "stage": stage,
        }
    )

    result = supervisor.renew(if_due=True)

    assert result.action == "recovered"
    assert result.image_uri == "registry.example.cn/team/snapshot:v2"
    assert client.create_calls == []
    assert client.patch_calls == []
    assert not supervisor.store.path.exists()


def test_recovery_releases_an_explicit_old_instance_selector(tmp_path, monkeypatch):
    old = NOW - timedelta(hours=3, minutes=50)
    image_uri = "registry.example.cn/team/snapshot:v2"
    replacement = with_target_image(
        make_target(
            NOW,
            instance=make_instance(NOW, name="replacement-instance"),
        ),
        image_uri,
    )

    class PinnedResolver:
        def __init__(self):
            self.normal_calls = []
            self.replacement_calls = []

        def resolve(self, *, include_namespace=False):
            self.normal_calls.append(include_namespace)
            pytest.fail("recovery must not keep selecting the pre-restart instance")

        def resolve_replacement_instance(self, *, include_namespace=False):
            self.replacement_calls.append(include_namespace)
            return copy.deepcopy(replacement)

    resolver = PinnedResolver()
    client = RenewalClient(
        replacement.app,
        create_response=AssertionError("recovery must not submit a snapshot"),
    )
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=resolver,
        client=client,
    )
    supervisor.store.save(
        {
            "version": 2,
            "workspace": WORKSPACE,
            "app": replacement.app_name,
            "instance": "old-explicit-instance",
            "container": replacement.container_name,
            "namespace": replacement.namespace,
            "old_started_at": old.isoformat(),
            "old_image_path": "registry.example.cn/team/base:v1",
            "requested_at": old.isoformat(),
            "snapshot_name": "slaigpus-example-cci",
            "snapshot_display_name": "slaigpus-auto-interrupted",
            "snapshot_uid": "snapshot-1",
            "image_uri": image_uri,
            "stage": "patch_sent",
        }
    )

    result = supervisor.renew()

    assert result.action == "recovered"
    assert result.status.target.instance_name == "replacement-instance"
    assert resolver.normal_calls == []
    assert resolver.replacement_calls == [False]
    assert client.create_calls == []
    assert client.patch_calls == []
    assert not supervisor.store.path.exists()


@pytest.mark.parametrize(
    ("state", "container_state", "ready"),
    [
        ("STOPPED", "TERMINATED", False),
        ("RUNNING", "RUNNING", False),
    ],
)
def test_recovery_keeps_state_until_new_instance_and_container_are_ready(
    tmp_path, monkeypatch, state, container_state, ready
):
    old = NOW - timedelta(hours=3, minutes=50)
    image_uri = "registry.example.cn/team/snapshot:v2"
    current = with_target_image(
        make_target(
            NOW,
            instance=make_instance(
                NOW,
                state=state,
                container_state=container_state,
                ready=ready,
            ),
        ),
        image_uri,
    )
    client = RenewalClient(current.app, create_response={})
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(current),
        client=client,
        poll_interval=1,
        wait_timeout=1,
    )
    supervisor.store.save(
        {
            "version": 2,
            "workspace": WORKSPACE,
            "app": current.app_name,
            "instance": current.instance_name,
            "container": current.container_name,
            "namespace": current.namespace,
            "old_started_at": old.isoformat(),
            "old_image_path": "registry.example.cn/team/base:v1",
            "requested_at": old.isoformat(),
            "snapshot_name": "slaigpus-example-cci",
            "snapshot_display_name": "slaigpus-auto-interrupted",
            "snapshot_uid": "snapshot-1",
            "image_uri": image_uri,
            "stage": "image_ready",
        }
    )

    with pytest.raises(CCIError, match="new RUNNING instance was not observed"):
        supervisor.renew(if_due=True)

    assert supervisor.store.load()["stage"] == "patch_sent"
    assert client.patch_calls == []


def test_external_restart_discards_prepatch_state_without_claiming_success(
    tmp_path, monkeypatch
):
    old = NOW - timedelta(hours=3, minutes=50)
    current = make_target(NOW)
    resolver = SequenceResolver(current)
    client = RenewalClient(current.app, create_response={})
    supervisor, _ = make_supervisor(
        tmp_path, monkeypatch, resolver=resolver, client=client
    )
    supervisor.store.save(
        {
            "version": 2,
            "workspace": WORKSPACE,
            "app": current.app_name,
            "instance": current.instance_name,
            "container": current.container_name,
            "namespace": current.namespace,
            "old_started_at": old.isoformat(),
            "old_image_path": "registry.example.cn/team/base:v1",
            "requested_at": old.isoformat(),
            "snapshot_name": "slaigpus-example-cci",
            "snapshot_display_name": "slaigpus-auto-old",
            "snapshot_uid": "snapshot-1",
            "image_uri": "",
            "stage": "snapshot_requested",
        }
    )

    result = supervisor.renew(if_due=True)

    assert result.action == "external_restart"
    assert resolver.calls == [False], "recovery must not rediscover namespace"
    assert client.create_calls == []
    assert client.patch_calls == []
    assert not supervisor.store.path.exists()


def test_restart_while_snapshot_is_pending_never_sends_patch(tmp_path, monkeypatch):
    old = NOW - timedelta(hours=3, minutes=50)
    initial = make_target(old)
    externally_restarted = make_target(NOW)
    snapshot = success_snapshot()
    client = RenewalClient(
        initial.app,
        create_response={"uid": "snapshot-1", "state": "CREATING"},
        snapshot_results=[[], [snapshot]],
    )
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(initial, externally_restarted),
        client=client,
    )

    result = supervisor.renew(if_due=True)

    assert result.action == "external_restart"
    assert client.patch_calls == []
    assert not supervisor.store.path.exists()


def test_manual_image_change_while_snapshot_is_pending_never_sends_patch(
    tmp_path, monkeypatch
):
    old = NOW - timedelta(hours=3, minutes=50)
    initial = make_target(old)
    manually_changed = with_target_image(
        make_target(old), "registry.example.cn/team/manually-selected:v9"
    )
    snapshot = success_snapshot()
    client = RenewalClient(
        initial.app,
        create_response={"uid": "snapshot-1", "state": "CREATING"},
        snapshot_results=[[], [snapshot]],
    )
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(initial, manually_changed),
        client=client,
    )

    result = supervisor.renew(if_due=True)

    assert result.action == "external_change"
    assert client.patch_calls == []
    assert not supervisor.store.path.exists()


def test_snapshot_without_uid_requires_exact_unique_display_name(
    tmp_path, monkeypatch
):
    old = NOW - timedelta(hours=3, minutes=50)
    target = make_target(old)
    exact = success_snapshot()
    exact.update(
        {
            "uid": "",
            "display_name": "slaigpus-auto-exact-1234",
            "container_name": target.container_name,
            "instance_uuid": target.instance_name,
        }
    )
    wrong_display = copy.deepcopy(exact)
    wrong_display["display_name"] = "slaigpus-auto-someone-else"
    client = RenewalClient(
        target.app,
        create_response={},
        snapshot_results=[[wrong_display, exact]],
    )
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(target),
        client=client,
    )
    state = supervisor._new_state(supervisor.status(include_namespace=True))
    state["snapshot_display_name"] = exact["display_name"]

    assert supervisor._find_snapshot(state) == exact


def test_snapshot_without_uid_rejects_duplicate_exact_matches(tmp_path, monkeypatch):
    old = NOW - timedelta(hours=3, minutes=50)
    target = make_target(old)
    first = success_snapshot()
    first.update({"uid": "", "display_name": "slaigpus-auto-exact-1234"})
    second = copy.deepcopy(first)
    second["created_time"] = (NOW + timedelta(seconds=1)).isoformat()
    client = RenewalClient(
        target.app,
        create_response={},
        snapshot_results=[[first, second]],
    )
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(target),
        client=client,
    )
    state = supervisor._new_state(supervisor.status(include_namespace=True))
    state["snapshot_display_name"] = first["display_name"]

    with pytest.raises(CCIError, match="multiple snapshots"):
        supervisor._find_snapshot(state)


def test_pending_state_refuses_a_different_resolved_container(tmp_path, monkeypatch):
    old = NOW - timedelta(hours=3, minutes=50)
    target = make_target(old)
    target.container = {"name": "different", "image_path": "registry/other:v1"}
    client = RenewalClient(target.app, create_response={})
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(target),
        client=client,
    )
    state = {
        "version": 2,
        "workspace": WORKSPACE,
        "app": target.app_name,
        "instance": target.instance_name,
        "container": "trainer",
        "namespace": "team",
        "old_started_at": old.isoformat(),
        "old_image_path": "registry.example.cn/team/base:v1",
        "requested_at": NOW.isoformat(),
        "snapshot_name": "slaigpus-example-cci",
        "snapshot_display_name": "slaigpus-auto-old",
        "snapshot_uid": "",
        "image_uri": "",
        "stage": "snapshot_prepared",
    }
    supervisor.store.save(state)

    with pytest.raises(CCIError, match="different container"):
        supervisor.renew(if_due=True)

    assert client.create_calls == []
    assert client.patch_calls == []


@pytest.mark.parametrize("failure", ["wrong_image", "missing_ready"])
def test_restart_requires_saved_image_and_explicit_container_ready(
    tmp_path, monkeypatch, failure
):
    old = NOW - timedelta(hours=3, minutes=50)
    image_uri = "registry.example.cn/team/snapshot:v2"
    restarted = with_target_image(make_target(NOW), image_uri)
    if failure == "wrong_image":
        restarted = make_target(NOW)
    else:
        restarted.instance["container_infos"][0].pop("ready")
    client = RenewalClient(restarted.app, create_response={})
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(restarted),
        client=client,
        poll_interval=1,
        wait_timeout=1,
    )
    state = {
        "app": restarted.app_name,
        "container": restarted.container_name,
        "old_started_at": old.isoformat(),
        "image_uri": image_uri,
    }

    with pytest.raises(CCIError, match="new RUNNING instance was not observed"):
        supervisor._wait_for_restart(state)


def test_state_store_rejects_non_object_json(tmp_path):
    store = RenewalStateStore(tmp_path, WorkspaceRef.parse(WORKSPACE))
    store.root.mkdir(parents=True, exist_ok=True)
    store.path.write_text("[]\n", encoding="utf-8")
    store.path.chmod(0o600)

    with pytest.raises(CCIError, match="expected a JSON object"):
        store.load()


def test_state_store_creates_private_directory_and_file(tmp_path):
    root = tmp_path / "new-state"
    store = RenewalStateStore(root, WorkspaceRef.parse(WORKSPACE))

    store.save({"version": 2, "stage": "snapshot_prepared"})

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert store.load()["stage"] == "snapshot_prepared"


def test_state_directory_rejects_symlink_and_does_not_touch_target(tmp_path):
    target = tmp_path / "real-state"
    target.mkdir(mode=0o700)
    root = tmp_path / "linked-state"
    root.symlink_to(target, target_is_directory=True)
    store = RenewalStateStore(root, WorkspaceRef.parse(WORKSPACE))

    with pytest.raises(CCIError, match="real directory, not a symlink"):
        store.save({"version": 2})

    assert list(target.iterdir()) == []


def test_state_directory_rejects_wrong_mode(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    root.chmod(0o755)
    store = RenewalStateStore(root, WorkspaceRef.parse(WORKSPACE))

    with pytest.raises(CCIError, match="mode 0700"):
        store.load()


def test_state_directory_rejects_wrong_owner(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    store = RenewalStateStore(root, WorkspaceRef.parse(WORKSPACE))
    monkeypatch.setattr(cci, "_state_current_uid", lambda: root.stat().st_uid + 1)

    with pytest.raises(CCIError, match="current-user-owned.*mode 0700"):
        store.load()


def test_state_file_rejects_wrong_mode_and_symlink(tmp_path):
    store = RenewalStateStore(tmp_path, WorkspaceRef.parse(WORKSPACE))
    store.save({"version": 2})
    store.path.chmod(0o644)

    with pytest.raises(CCIError, match="mode 0600"):
        store.load()

    store.path.unlink()
    target = tmp_path / "outside-state.json"
    target.write_text('{"version":2}\n', encoding="utf-8")
    target.chmod(0o600)
    store.path.symlink_to(target.name)

    with pytest.raises(CCIError, match="regular file.*mode 0600"):
        store.load()
    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 2}


def test_renewal_lock_rejects_symlink(tmp_path):
    store = RenewalStateStore(tmp_path, WorkspaceRef.parse(WORKSPACE))
    target = tmp_path / "outside.lock"
    target.touch(mode=0o600)
    target.chmod(0o600)
    store.lock_path.symlink_to(target.name)

    with pytest.raises(CCIError, match="securely open CCI renewal lock"):
        with store.lock():
            pass


def test_auto_renew_control_defaults_on_and_persists_atomically_with_private_mode(
    tmp_path,
):
    control = AutoRenewControlStore(WORKSPACE, root=tmp_path)

    assert control.status() is True
    assert not control.path.exists()

    assert control.disable() is False
    assert control.status() is False
    assert json.loads(control.path.read_text(encoding="utf-8")) == {
        "enabled": False,
        "version": 1,
    }
    assert stat.S_IMODE(control.path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".control-*.json")) == []
    assert AutoRenewControlStore(WORKSPACE, root=tmp_path).status() is False

    assert control.enable() is True
    assert control.status() is True
    assert stat.S_IMODE(control.path.stat().st_mode) == 0o600


def test_auto_renew_control_is_per_workspace_and_rejects_corrupt_state(tmp_path):
    first = AutoRenewControlStore(WORKSPACE, root=tmp_path)
    second_workspace = WORKSPACE.replace("example-workspace", "another-workspace")
    second = AutoRenewControlStore(second_workspace, root=tmp_path)

    first.disable()

    assert first.path != second.path
    assert first.status() is False
    assert second.status() is True

    second.root.mkdir(parents=True, exist_ok=True)
    second.path.write_text('{"version":1,"enabled":"no"}\n', encoding="utf-8")
    second.path.chmod(0o600)
    with pytest.raises(CCIError, match="boolean enabled"):
        second.status()


def test_watch_auto_starts_suspended_app_when_auto_renew_is_enabled():
    reports = []
    starts = []

    class StopAfterOneWait:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.stopped = True
            return True

    supervisor = object.__new__(RenewalSupervisor)
    supervisor.poll_interval = 5.0
    supervisor.status = lambda **_kwargs: (_ for _ in ()).throw(
        TargetAmbiguous("Candidates: (none)")
    )
    suspended = make_app()
    suspended["state"] = "SUSPENDED"
    supervisor.resolver = SimpleNamespace(resolve_app=lambda: suspended)
    supervisor.store = SimpleNamespace(load=lambda: {})
    supervisor.start = lambda: starts.append(True) or SimpleNamespace(
        status=SimpleNamespace(target=SimpleNamespace(app_name="example-cci"))
    )
    supervisor._report = reports.append
    supervisor._sleep = pytest.fail

    supervisor.watch(stop_event=StopAfterOneWait(), enabled=True)

    assert starts == [True]
    assert any("started and ready" in message for message in reports)


def test_watch_does_not_start_suspended_app_when_auto_renew_is_disabled():
    reports = []

    class StopAfterOneWait:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, _seconds):
            self.stopped = True
            return True

    supervisor = object.__new__(RenewalSupervisor)
    supervisor.poll_interval = 5.0
    supervisor.status = lambda **_kwargs: (_ for _ in ()).throw(
        TargetAmbiguous("Candidates: (none)")
    )
    suspended = make_app()
    suspended["state"] = "SUSPENDED"
    supervisor.resolver = SimpleNamespace(resolve_app=lambda: suspended)
    supervisor.store = SimpleNamespace(load=lambda: {})
    supervisor.start = pytest.fail
    supervisor._report = reports.append
    supervisor._sleep = pytest.fail

    supervisor.watch(stop_event=StopAfterOneWait(), enabled=False)

    assert any("auto-renew is disabled" in message for message in reports)


def test_watch_retries_pending_renewal_even_when_restarted_instance_is_young():
    """A lost PATCH response must resume now, not at the next 3h50m boundary."""
    status = SimpleNamespace(
        age=30.0,
        due=False,
        due_in=13_170.0,
        target=SimpleNamespace(app_name="example-cci", instance_name="cci-0"),
    )
    reports = []
    sleeps = []
    attempts = []
    supervisor = object.__new__(RenewalSupervisor)
    supervisor.poll_interval = 5.0
    supervisor.store = SimpleNamespace(load=lambda: {"stage": "image_ready"})
    supervisor.status = lambda **_kwargs: status
    supervisor._report = reports.append
    supervisor._sleep = sleeps.append

    def renew(**_kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise CCIError("SSH proxy disappeared during PATCH response")
        return SimpleNamespace(action="recovered")

    supervisor.renew = renew

    supervisor.watch(once=True)

    assert len(attempts) == 2
    assert sleeps == [5.0]
    assert any("will retry safely" in message for message in reports)


def test_disabled_auto_renew_does_not_start_due_cycle_or_hot_loop(tmp_path):
    status = SimpleNamespace(
        age=4 * 3600.0,
        due=True,
        due_in=-1.0,
        target=SimpleNamespace(app_name="example-cci", instance_name="cci-0"),
    )
    waits = []
    renew_calls = []

    class StopAfterOneWait:
        stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            waits.append(seconds)
            self.stopped = True
            return True

    supervisor = object.__new__(RenewalSupervisor)
    supervisor.poll_interval = 5.0
    supervisor.status = lambda **_kwargs: status
    supervisor.store = SimpleNamespace(load=lambda: {})
    supervisor.control = AutoRenewControlStore(WORKSPACE, root=tmp_path)
    supervisor.control.disable()
    supervisor.renew = lambda **_kwargs: renew_calls.append(True)
    supervisor._report = lambda _message: None
    supervisor._sleep = pytest.fail

    supervisor.watch(stop_event=StopAfterOneWait())

    assert waits == [5.0], "disabled and overdue must use the normal poll interval"
    assert renew_calls == []


def test_disabled_auto_renew_still_finishes_durable_recovery():
    status = SimpleNamespace(
        age=30.0,
        due=False,
        due_in=13_170.0,
        target=SimpleNamespace(app_name="example-cci", instance_name="cci-0"),
    )
    attempts = []
    supervisor = object.__new__(RenewalSupervisor)
    supervisor.poll_interval = 5.0
    supervisor.status = lambda **_kwargs: status
    supervisor.store = SimpleNamespace(load=lambda: {"stage": "patch_submitting"})
    supervisor.renew = lambda **_kwargs: attempts.append(True) or SimpleNamespace(
        action="recovered"
    )
    supervisor._report = lambda _message: None
    supervisor._sleep = pytest.fail

    supervisor.watch(
        once=True,
        enabled=lambda: pytest.fail("pending recovery must ignore the off switch"),
    )

    assert attempts == [True]


def test_watch_reloads_dynamic_auto_renew_control_each_poll():
    status = SimpleNamespace(
        age=4 * 3600.0,
        due=True,
        due_in=0.0,
        target=SimpleNamespace(app_name="example-cci", instance_name="cci-0"),
    )
    controls = iter([False, True])
    sleeps = []
    attempts = []
    supervisor = object.__new__(RenewalSupervisor)
    supervisor.poll_interval = 5.0
    supervisor.status = lambda **_kwargs: status
    supervisor.store = SimpleNamespace(load=lambda: {})
    supervisor.renew = lambda **_kwargs: attempts.append(True) or SimpleNamespace(
        action="renewed"
    )
    supervisor._report = lambda _message: None
    supervisor._sleep = sleeps.append

    supervisor.watch(once=True, enabled=lambda: next(controls))

    assert sleeps == [5.0]
    assert attempts == [True]


def test_watch_escalates_a_broken_browser_transport_for_reconstruction():
    supervisor = object.__new__(RenewalSupervisor)
    supervisor.client = SimpleNamespace(
        transport=SimpleNamespace(broken=True)
    )
    supervisor.poll_interval = 5.0
    supervisor.status = lambda **_kwargs: (_ for _ in ()).throw(
        CCIError("closed CDP session")
    )
    supervisor._report = pytest.fail
    supervisor._sleep = pytest.fail

    with pytest.raises(CCIError, match="closed CDP session"):
        supervisor.watch()


def test_watch_escalates_login_required_during_status():
    supervisor = object.__new__(RenewalSupervisor)
    supervisor.client = SimpleNamespace(
        transport=SimpleNamespace(broken=False, login_required=True)
    )
    supervisor.poll_interval = 5.0
    supervisor.status = lambda **_kwargs: (_ for _ in ()).throw(
        CCIError("automation profile login required")
    )
    supervisor._report = pytest.fail
    supervisor._sleep = pytest.fail

    with pytest.raises(CCIError, match="login required"):
        supervisor.watch()


def test_watch_escalates_login_required_during_renewal_retry():
    status = SimpleNamespace(
        age=13_200.0,
        due=True,
        due_in=0.0,
        target=SimpleNamespace(app_name="example-cci", instance_name="cci-0"),
    )
    supervisor = object.__new__(RenewalSupervisor)
    supervisor.client = SimpleNamespace(
        transport=SimpleNamespace(broken=False, login_required=True)
    )
    supervisor.poll_interval = 5.0
    supervisor.status = lambda **_kwargs: status
    supervisor.store = SimpleNamespace(load=lambda: {})
    supervisor.renew = lambda **_kwargs: (_ for _ in ()).throw(
        CCIError("automation profile login required")
    )
    supervisor._report = lambda _message: None
    supervisor._sleep = pytest.fail

    with pytest.raises(CCIError, match="login required"):
        supervisor.watch()


def test_snapshot_wait_escalates_a_broken_browser_transport():
    supervisor = object.__new__(RenewalSupervisor)
    supervisor.client = SimpleNamespace(
        transport=SimpleNamespace(broken=True)
    )
    supervisor.wait_timeout = 60.0
    supervisor.poll_interval = 5.0
    supervisor._find_snapshot = lambda _state: (_ for _ in ()).throw(
        CCIError("closed CDP during snapshot poll")
    )
    supervisor._report = pytest.fail
    supervisor._sleep = pytest.fail

    with pytest.raises(CCIError, match="closed CDP during snapshot poll"):
        supervisor._wait_for_snapshot({})


def test_restart_wait_escalates_a_broken_browser_transport():
    supervisor = object.__new__(RenewalSupervisor)
    supervisor.client = SimpleNamespace(
        transport=SimpleNamespace(broken=True)
    )
    supervisor.wait_timeout = 60.0
    supervisor.poll_interval = 5.0
    supervisor.status = lambda **_kwargs: (_ for _ in ()).throw(
        CCIError("closed CDP during restart poll")
    )
    supervisor._sleep = pytest.fail

    with pytest.raises(CCIError, match="closed CDP during restart poll"):
        supervisor._wait_for_restart(
            {
                "old_started_at": NOW.isoformat(),
                "container": "trainer",
                "image_uri": "registry.example.cn/team/snapshot:v2",
            }
        )


def test_workspace_lock_rejects_second_watcher(tmp_path):
    store = RenewalStateStore(tmp_path, WorkspaceRef.parse(WORKSPACE))

    with store.lock():
        assert stat.S_IMODE(store.lock_path.stat().st_mode) == 0o600
        with pytest.raises(LockBusy, match="already running"):
            with store.lock():
                pass


def test_generated_state_does_not_serialize_app_or_instance_tokens(
    tmp_path, monkeypatch
):
    app = make_app()
    app["access_token"] = "APP-TOKEN-MUST-NOT-BE-SAVED"
    instance = make_instance(NOW - timedelta(hours=3, minutes=50))
    instance["authorization"] = "Bearer INSTANCE-TOKEN-MUST-NOT-BE-SAVED"
    target = make_target(instance=instance, started_at=NOW, app=app)
    client = RenewalClient(app, create_response={})
    supervisor, _ = make_supervisor(
        tmp_path,
        monkeypatch,
        resolver=SequenceResolver(target),
        client=client,
    )

    state = supervisor._new_state(supervisor.status(include_namespace=True))
    supervisor.store.save(state)
    persisted = supervisor.store.path.read_text(encoding="utf-8")

    assert "token" not in persisted.lower()
    assert "authorization" not in persisted.lower()
    assert set(json.loads(persisted)) == {
        "version",
        "workspace",
        "app",
        "instance",
        "container",
        "namespace",
        "old_started_at",
        "old_image_path",
        "requested_at",
        "snapshot_name",
        "snapshot_display_name",
        "snapshot_uid",
        "image_uri",
        "stage",
    }
