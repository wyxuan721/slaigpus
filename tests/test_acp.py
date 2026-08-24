"""Offline contract and safety tests for ACP training-job planning."""

from __future__ import annotations

import copy
import gc
import json
import sys
import weakref
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slaigpus.acp import (  # noqa: E402
    ACP_ORIGIN,
    AEC2_ORIGIN,
    ACPAPIError,
    ACPClient,
    ACPError,
    CCR_ORIGIN,
    DEFAULT_ACP_CONSOLE_URL,
    DEFAULT_PORTABLE_ENV,
    DEFAULT_PORTABLE_FRAMEWORK,
    DEFAULT_TEMPLATE_JOB,
    MONITOR_ORIGIN,
    validate_acp_workspace,
)
from slaigpus.acp_resources import (  # noqa: E402
    DEFAULT_RESOURCE_PROFILE,
    DEFAULT_RESOURCE_PROFILE_KEY,
    RESOURCE_PROFILES_BY_KEY,
    ResourceProfile,
)
from slaigpus.cci import CCIError, DEFAULT_WORKSPACE  # noqa: E402


WORKSPACE = (
    "/subscriptions/example-subscription/"
    "resourceGroups/default/zones/cn-sh-01z/workspaces/example-workspace"
)
JOB_ID = WORKSPACE + "/trainingJobs/" + DEFAULT_TEMPLATE_JOB
POOL_A_ID = (
    "/subscriptions/example-subscription/"
    "resourceGroups/default/zones/cn-sh-01z/aec2s/pool-a"
)
POOL_B_ID = (
    "/subscriptions/example-subscription/"
    "resourceGroups/default/zones/cn-sh-01z/aec2s/pool-b"
)
DEFAULT_RESOURCE_SPEC = DEFAULT_RESOURCE_PROFILE.spec_name
LARGE_RESOURCE_PROFILE_KEY = "n6ls-80g-sxm5-4x-56c-792g"
LARGE_RESOURCE_PROFILE = RESOURCE_PROFILES_BY_KEY[LARGE_RESOURCE_PROFILE_KEY]


class RecordingTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.refresh_calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected ACP request")
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return copy.deepcopy(result)

    def refresh_auth(self, **kwargs):
        self.refresh_calls.append(kwargs)


def _template_job(*, replicas=1):
    job = {
        "metadata": {"labels": {"copied": "yes"}},
        "display_name": "old-display",
        "name": DEFAULT_TEMPLATE_JOB,
        "framework": "CUSTOM",
        "mount": [{"name": "workspace", "path": "/workspace"}],
        "tensorboard": {"log_path": "/logs", "unknown": "drop"},
        "async_checkpoint": {"max_ckpt_rounds": 3, "unknown": "drop"},
        "lme": {
            "enable_warmingup": True,
            "enable_checker": False,
            "enable_health_monitor": True,
            "max_retries": 2,
            "token": "drop-this",
        },
        "env": [{"name": "MODE", "value": "train"}],
        "scheduling": {
            "quota_type": "ON_DEMAND",
            "priority": 17,
            "scoring_strategy": {"type": "FAIR", "weight": "drop"},
            "enable_queuing": True,
        },
        "resource_pool": {"name": "old-pool", "token": "drop"},
        "ssh": {
            "auto_key_setup": True,
            "config_mount_path": "/root/.ssh",
            "private_key": "drop",
        },
        "fault_tolerance": {"max_restarts": 1},
        "barrier": {"type": "TCP", "port": 23456},
        "unknown_top_level": {"must": "drop"},
    }
    job["roles"] = [
        _worker(replicas=replicas),
        {"name": "Evaluator", "total_replicas": 99},
    ]
    return job


def _worker(*, replicas=1):
    return {
        "name": "Worker",
        "resource_spec": [{"name": "OLD"}],
        "total_replicas": replicas,
        "startup_script": "python old.py",
        "image_path": "registry.example.cn/team/old:v1",
        "privileged": True,
    }


def _binding(
    resource_id,
    name,
    *,
    state="ACTIVE",
    spot=True,
    gpu=4,
    cpu=56,
    memory=792,
    reserved_gpu=None,
    reserved_cpu=None,
    reserved_memory=None,
    quota_type="ALL",
    display_name=None,
    pool_type="",
):
    if reserved_gpu is None:
        reserved_gpu = gpu
    if reserved_cpu is None:
        reserved_cpu = cpu
    if reserved_memory is None:
        reserved_memory = memory
    return {
        "id": resource_id,
        "name": name,
        "display_name": display_name or f"{name} display",
        "state": state,
        "type": pool_type,
        "quota_type": quota_type,
        "vpc_id": "vpc-main",
        "zone": "cn-sh-01z",
        "reserved_number": reserved_gpu,
        "reserved_cpu": reserved_cpu,
        "reserved_memory": reserved_memory,
        "spot_status": (
            [
                {
                    "spot_name": "default",
                    "spot_quota": {"device": gpu, "cpu": cpu, "memory": memory},
                }
            ]
            if spot
            else []
        ),
}


def _profile_spec(profile=DEFAULT_RESOURCE_PROFILE):
    return {
        "name": profile.spec_name,
        "device": {
            "manufacturer": "NVIDIA",
            "type": "N6lS",
            "memory": 80,
            "physical_interface": "SXM5",
            "number": profile.gpu_cards,
        },
        "cpu": {
            "manufacturer": "Intel",
            "type": "8468",
            "frequency": 2.1,
            "vcpu_allocatable": profile.vcpus,
        },
        "memory": {"allocatable": profile.memory_gib},
    }


def _n6ls_spec_wrapped(profile=DEFAULT_RESOURCE_PROFILE):
    return {
        "spec": _profile_spec(profile)
    }


def _plan_transport(*, replicas=1):
    bindings = [
        _binding(POOL_A_ID, "pool-a", gpu=4, cpu=56, memory=792),
        _binding(POOL_B_ID, "pool-b", gpu=8, cpu=70, memory=990),
        _binding(POOL_A_ID, "inactive", state="INACTIVE"),
        _binding(POOL_A_ID, "no-spot", spot=False),
    ]
    # Deliberately use different harmless wrappers for every endpoint.
    return RecordingTransport(
        {"data": {"trainingJob": _template_job(replicas=replicas)}},
        {"payload": {"aec2s": bindings}},
        {"data": {"resourceSpecs": [_n6ls_spec_wrapped()]}},
        {"result": {"items": [_n6ls_spec_wrapped()]}},
    )


def _portable_transport(*, gpu=4, cpu=56, memory=792):
    return RecordingTransport(
        {
            "aec2s": [
                _binding(
                    POOL_A_ID,
                    "portable-pool",
                    gpu=gpu,
                    cpu=cpu,
                    memory=memory,
                )
            ]
        },
        {"resourceSpecs": [_n6ls_spec_wrapped()]},
    )


@pytest.mark.parametrize(
    "workspace",
    [
        WORKSPACE.replace("cn-sh-01z", "cn-bj-01z"),
        WORKSPACE.replace("cn-sh-01z", "cn-sh-02z"),
        WORKSPACE.replace("cn-sh-01z", "cn-sh-010z"),
    ],
)
def test_fixed_acp_endpoints_reject_other_regions_before_requests(workspace):
    transport = RecordingTransport()

    with pytest.raises(ACPError, match="supports cn-sh-01"):
        ACPClient(transport, workspace, origin="https://custom.example.invalid")

    assert transport.calls == []


def test_supported_workspace_is_canonicalized():
    selected = validate_acp_workspace(f" {WORKSPACE}//")

    assert selected.resource_id == WORKSPACE


def test_origins_console_and_error_hierarchy_match_cli_contract():
    assert AEC2_ORIGIN == ACP_ORIGIN == "https://aec2.cn-sh-01.sensecoreapi.cn"
    assert CCR_ORIGIN == "https://ccr.cn-sh-01.sensecoreapi.cn"
    assert MONITOR_ORIGIN == "https://monitor.sensecoreapi.cn"
    assert DEFAULT_ACP_CONSOLE_URL == (
        "https://console.sensecore.cn/cn-sh-01/acp/list/create?workspace="
        + DEFAULT_WORKSPACE
    )
    assert issubclass(ACPError, CCIError)


def test_workers_are_runtime_data_for_logs_and_are_not_read_by_plan():
    worker_transport = RecordingTransport(
        {"data": {"workers": [{"name": "runtime-pod-0", "state": "RUNNING"}]}}
    )
    client = ACPClient(worker_transport, WORKSPACE)

    assert client.list_workers() == [
        {"name": "runtime-pod-0", "state": "RUNNING"}
    ]
    assert worker_transport.calls[0]["url"] == (
        ACP_ORIGIN + "/compute/acp/data/v2" + JOB_ID + "/workers"
    )
    assert worker_transport.calls[0]["params"] == {
        "page_size": 500,
        "page_token": 1,
    }

    plan_transport = _plan_transport()
    ACPClient(plan_transport, WORKSPACE).plan(name="no-runtime-worker-read")
    assert not any(call["url"].endswith("/workers") for call in plan_transport.calls)


def test_plan_uses_exact_get_contract_scores_pools_and_never_posts():
    transport = _plan_transport()
    client = ACPClient(transport, WORKSPACE)

    plan = client.plan(
        name="agent-train-01",
        display_name="Agent train 01",
        image="registry.example.cn/team/new:v2",
        startup="python train.py --epochs 5",
    )

    assert [call["method"] for call in transport.calls] == [
        "GET",
        "GET",
        "GET",
        "GET",
    ]
    assert transport.calls[0]["url"] == ACP_ORIGIN + "/compute/acp/data/v2" + JOB_ID
    assert transport.calls[1]["url"] == (
        ACP_ORIGIN
        + "/compute/workspace/data/v1"
        + WORKSPACE
        + "/workspaceAEC2Bindings"
    )
    assert transport.calls[1]["params"] is None
    assert transport.calls[2]["url"] == (
        ACP_ORIGIN + "/compute/aec2/data/v1" + POOL_A_ID + "/resourceSpecs"
    )
    assert transport.calls[3]["url"] == (
        ACP_ORIGIN + "/compute/aec2/data/v1" + POOL_B_ID + "/resourceSpecs"
    )
    assert all(call["headers"] == {"x-ui-valid": "x-ui-valid"} for call in transport.calls)

    # pool-a has 2x every resource. pool-b has min(4x, 2.5x, 2.5x), so wins.
    assert plan.pool.name == "pool-b"
    assert plan.pool.relative_capacity == 2.5
    assert plan.pool.profile is DEFAULT_RESOURCE_PROFILE
    assert plan.pool.profile.key == DEFAULT_RESOURCE_PROFILE_KEY
    assert plan.pool.resource_class == "spot"
    assert plan.pool.api_quota_type == "SPOT"
    assert plan.pool.spec.name == DEFAULT_RESOURCE_SPEC
    assert plan.pool.spec.gpu == 2
    assert plan.pool.spec.cpu == 28
    assert plan.pool.spec.memory_gib == 396
    assert plan.pool.spec.gpu_type == DEFAULT_RESOURCE_PROFILE.gpu_type
    assert plan.pool.spec.cpu_type == DEFAULT_RESOURCE_PROFILE.cpu_type


def test_plan_body_is_exact_whitelist_with_forced_spot_and_selected_pool():
    plan = ACPClient(_plan_transport(), WORKSPACE).plan(
        name="agent-train-01",
        display_name="训练一",
        image="registry.example.cn/team/new:v2",
        startup="python train.py\necho done",
    )

    assert plan.body == {
        "metadata": {"labels": {"copied": "yes"}},
        "display_name": "训练一",
        "name": "agent-train-01",
        "framework": "CUSTOM",
        "roles": [
            {
                "name": "Worker",
                "resource_spec": [{"name": DEFAULT_RESOURCE_SPEC}],
                "total_replicas": 1,
                "startup_script": "python train.py\necho done",
                "image_path": "registry.example.cn/team/new:v2",
            }
        ],
        "mount": [{"name": "workspace", "path": "/workspace"}],
        "tensorboard": {"log_path": "/logs"},
        "async_checkpoint": {"max_ckpt_rounds": 3},
        "lme": {
            "enable_warmingup": True,
            "enable_checker": False,
            "enable_health_monitor": True,
            "max_retries": 2,
        },
        "env": [{"key": "MODE", "value": "train"}],
        "scheduling": {
            "quota_type": "SPOT",
            "priority": 17,
            "scoring_strategy": {"type": "FAIR"},
        },
        "resource_pool": {
            "name": "pool-b",
            "vpc_id": "vpc-main",
            "zone": "cn-sh-01z",
        },
        "ssh": {"auto_key_setup": True, "config_mount_path": "/root/.ssh"},
        "fault_tolerance": {"max_restarts": 1},
    }
    assert "barrier" not in plan.body
    encoded = json.dumps(plan.body, ensure_ascii=False)
    for forbidden in (
        "unknown_top_level",
        "enable_queuing",
        "private_key",
        "privileged",
        "drop-this",
        '"weight"',
    ):
        assert forbidden not in encoded


def test_template_free_plan_uses_portable_defaults_and_never_gets_a_template():
    transport = _portable_transport()

    plan = ACPClient(transport, WORKSPACE).plan(
        name="portable-job",
        image="registry.example.cn/team/portable:v1",
        startup="python portable.py",
        template_job=None,
    )

    assert [call["method"] for call in transport.calls] == ["GET", "GET"]
    assert not any("/trainingJobs/" in call["url"] for call in transport.calls)
    assert plan.body == {
        "display_name": "portable-job",
        "name": "portable-job",
        "framework": DEFAULT_PORTABLE_FRAMEWORK,
        "roles": [
            {
                "name": "Worker",
                "resource_spec": [{"name": DEFAULT_RESOURCE_SPEC}],
                "total_replicas": 1,
                "startup_script": "python portable.py",
                "image_path": "registry.example.cn/team/portable:v1",
            }
        ],
        "mount": [],
        "env": [
            {"key": key, "value": value}
            for key, value in DEFAULT_PORTABLE_ENV.items()
        ],
        "scheduling": {"quota_type": "SPOT"},
        "resource_pool": {
            "name": "portable-pool",
            "vpc_id": "vpc-main",
            "zone": "cn-sh-01z",
        },
    }
    assert plan.worker_replicas == 1
    assert plan.mount_count == 0
    assert plan.env_count == 3
    assert plan.template_job is None
    for template_only in (
        "metadata",
        "tensorboard",
        "async_checkpoint",
        "lme",
        "ssh",
        "fault_tolerance",
        "barrier",
    ):
        assert template_only not in plan.body


@pytest.mark.parametrize(
    ("resource_class", "api_quota_type"),
    [("standard", "RESERVED"), ("spot", "SPOT")],
)
def test_resource_class_controls_api_quota_type(resource_class, api_quota_type):
    plan = ACPClient(_portable_transport(), WORKSPACE).plan(
        name=f"{resource_class}-job",
        image="registry.example.cn/team/image:v1",
        startup="python train.py",
        resource_class=resource_class,
        template_job=None,
    )

    assert plan.pool.resource_class == resource_class
    assert plan.pool.api_quota_type == api_quota_type
    assert plan.pool.capacity_basis == (
        "reserved_entitlement_without_usage"
        if resource_class == "standard"
        else "current_spot_quota"
    )
    assert plan.pool.profile is DEFAULT_RESOURCE_PROFILE
    assert plan.body["scheduling"]["quota_type"] == api_quota_type


@pytest.mark.parametrize(
    ("resource_class", "binding_quota_type", "expected_api_type"),
    [
        ("standard", "SPOT", "RESERVED"),
        ("standard", "ON_DEMAND", "RESERVED"),
        ("spot", "RESERVED", "SPOT"),
        ("spot", "ON_DEMAND", "SPOT"),
    ],
)
def test_resource_classes_never_fall_back_to_another_binding_quota_type(
    resource_class, binding_quota_type, expected_api_type
):
    transport = RecordingTransport(
        {
            "aec2s": [
                _binding(
                    POOL_A_ID,
                    "wrong-class-pool",
                    quota_type=binding_quota_type,
                    gpu=100,
                    cpu=1000,
                    memory=10000,
                )
            ]
        }
    )

    with pytest.raises(
        ACPError,
        match=rf"no ACTIVE {expected_api_type} resource pool",
    ):
        ACPClient(transport, WORKSPACE).plan(
            name="strict-resource-class",
            image="registry.example.cn/team/image:v1",
            startup="python train.py",
            resource_class=resource_class,
            template_job=None,
        )

    assert len(transport.calls) == 1
    assert not any(call["url"].endswith("/resourceSpecs") for call in transport.calls)


def test_explicit_worker_overrides_replace_template_collections_defensively():
    mounts = [
        {
            "name": "data",
            "mountPath": "/data",
            "options": {"readOnly": True},
        }
    ]
    environment = {
        "MODE": "evaluate",
        "EMPTY_VALUE": "",
    }
    barrier = {"type": "TCP", "port": 24567}

    plan = ACPClient(_plan_transport(), WORKSPACE).plan(
        name="override-job",
        replicas=2,
        mounts=mounts,
        env=environment,
        barrier=barrier,
    )
    mounts[0]["mountPath"] = "/changed"
    mounts[0]["options"]["readOnly"] = False
    environment["MODE"] = "changed"
    barrier["port"] = 1

    assert plan.body["roles"][0]["total_replicas"] == 2
    assert plan.body["mount"] == [
        {
            "name": "data",
            "mountPath": "/data",
            "options": {"readOnly": True},
        }
    ]
    assert plan.body["env"] == [
        {"key": "MODE", "value": "evaluate"},
        {"key": "EMPTY_VALUE", "value": ""},
    ]
    assert plan.body["barrier"] == {"type": "TCP", "port": 24567}
    assert plan.worker_replicas == 2
    assert plan.mount_count == 1
    assert plan.env_count == 2
    assert plan.template_job == DEFAULT_TEMPLATE_JOB


def test_explicit_empty_worker_collections_clear_template_values():
    plan = ACPClient(_plan_transport(), WORKSPACE).plan(
        name="clear-template-values",
        mounts=[],
        env=[],
    )

    assert plan.body["mount"] == []
    assert plan.body["env"] == []
    assert plan.mount_count == 0
    assert plan.env_count == 0


def test_multi_replica_template_inherits_barrier_and_replica_count():
    plan = ACPClient(_plan_transport(replicas=2), WORKSPACE).plan(
        name="distributed-job"
    )

    assert plan.body["roles"][0]["total_replicas"] == 2
    assert plan.body["barrier"] == {"type": "TCP", "port": 23456}
    assert plan.worker_replicas == 2


def test_replica_capacity_is_checked_for_the_whole_worker_group():
    transport = _portable_transport(gpu=4, cpu=56, memory=792)

    with pytest.raises(ACPError, match="no ACTIVE SPOT resource pool"):
        ACPClient(transport, WORKSPACE).plan(
            name="too-many-workers",
            image="registry.example.cn/team/image:v1",
            startup="python train.py",
            replicas=3,
            barrier={"type": "TCP", "port": 23456},
            template_job=None,
        )

    assert len(transport.calls) == 2
    assert all(call["method"] == "GET" for call in transport.calls)


def test_portable_multi_replica_plan_requires_and_accepts_explicit_barrier():
    missing_transport = RecordingTransport()
    with pytest.raises(ACPError, match="no barrier configuration"):
        ACPClient(missing_transport, WORKSPACE).plan(
            name="missing-barrier",
            image="registry.example.cn/team/image:v1",
            startup="python train.py",
            replicas=2,
            template_job=None,
        )
    assert missing_transport.calls == []

    plan = ACPClient(_portable_transport(), WORKSPACE).plan(
        name="portable-distributed",
        image="registry.example.cn/team/image:v1",
        startup="python train.py",
        replicas=2,
        barrier={"type": "TCP", "port": 23456},
        template_job=None,
    )
    assert plan.body["roles"][0]["total_replicas"] == 2
    assert plan.body["barrier"] == {"type": "TCP", "port": 23456}


def test_single_replica_rejects_explicit_barrier_before_any_request():
    transport = RecordingTransport()

    with pytest.raises(ACPError, match="requires multiple Worker replicas"):
        ACPClient(transport, WORKSPACE).plan(
            name="single-with-barrier",
            replicas=1,
            barrier={"type": "TCP", "port": 23456},
        )

    assert transport.calls == []


@pytest.mark.parametrize(
    "barrier",
    [
        [],
        {},
        {"ratio": float("inf")},
        {"secret": "barrier-secret-sentinel\x00"},
    ],
)
def test_invalid_barrier_is_rejected_without_requests_or_value_leaks(barrier):
    transport = RecordingTransport()

    with pytest.raises(ACPError, match="invalid barrier configuration") as failure:
        ACPClient(transport, WORKSPACE).plan(
            name="invalid-barrier",
            replicas=2,
            barrier=barrier,
            template_job=None,
        )

    assert "barrier-secret-sentinel" not in str(failure.value)
    assert transport.calls == []


def test_multi_replica_template_without_barrier_fails_before_pool_discovery():
    template = _template_job(replicas=2)
    template.pop("barrier")
    transport = RecordingTransport({"job": template})

    with pytest.raises(ACPError, match="no barrier configuration"):
        ACPClient(transport, WORKSPACE).plan(name="template-without-barrier")

    assert len(transport.calls) == 1
    assert transport.calls[0]["url"].endswith(
        "/trainingJobs/" + DEFAULT_TEMPLATE_JOB
    )


@pytest.mark.parametrize("replicas", [True, False, 1.0, "1", 0, -1, 10001])
def test_explicit_replica_count_is_a_strict_bounded_integer(replicas):
    transport = RecordingTransport()

    with pytest.raises(ACPError, match="invalid Worker replica count"):
        ACPClient(transport, WORKSPACE).plan(
            name="invalid-replicas",
            replicas=replicas,
        )

    assert transport.calls == []


def test_inherited_replica_count_is_not_coerced_from_a_string():
    transport = RecordingTransport({"job": _template_job(replicas="2")})

    with pytest.raises(ACPError, match="invalid Worker replica count"):
        ACPClient(transport, WORKSPACE).plan(name="strict-template-replicas")

    assert len(transport.calls) == 1


def test_explicit_catalog_profile_matches_only_its_exact_pool_spec():
    custom_spec = {
        "name": LARGE_RESOURCE_PROFILE.spec_name,
        "device": {
            "manufacturer": "NVIDIA",
            "type": "N6lS",
            "memory": "80Gi",
            "physical_interface": "SXM5",
            "number": "4",
        },
        "cpu": {
            "manufacturer": "Intel",
            "type": "8468",
            "frequency": "2.1GHz",
            "vcpu_allocatable": "56000m",
        },
        "memory": {"allocatable": "792Gi"},
    }
    transport = RecordingTransport(
        {"job": _template_job()},
        {"aec2s": [_binding(POOL_A_ID, "pool-a", gpu=8, cpu=112, memory=1584)]},
        {"resource_specs": [{"bad": "future-entry"}, custom_spec]},
    )
    client = ACPClient(transport, WORKSPACE)

    plan = client.plan(
        name="large-job",
        resource_profile=LARGE_RESOURCE_PROFILE_KEY,
    )

    assert plan.pool.profile is LARGE_RESOURCE_PROFILE
    assert plan.pool.spec.name == LARGE_RESOURCE_PROFILE.spec_name
    assert plan.body["roles"][0]["resource_spec"] == [
        {"name": LARGE_RESOURCE_PROFILE.spec_name}
    ]
    assert plan.pool.relative_capacity == 2


def test_low_level_pool_selection_rejects_noncanonical_profiles_without_requests():
    unknown = ResourceProfile(
        key="n6ls-80g-sxm5-3x-42c-594g",
        spec_name="not-in-the-catalog",
        gpu_type=DEFAULT_RESOURCE_PROFILE.gpu_type,
        gpu_cards=3,
        cpu_type=DEFAULT_RESOURCE_PROFILE.cpu_type,
        vcpus=42,
        memory_gib=594,
        classes=DEFAULT_RESOURCE_PROFILE.classes,
    )
    tampered = ResourceProfile(
        key=DEFAULT_RESOURCE_PROFILE.key,
        spec_name=DEFAULT_RESOURCE_PROFILE.spec_name + ".tampered",
        gpu_type=DEFAULT_RESOURCE_PROFILE.gpu_type,
        gpu_cards=DEFAULT_RESOURCE_PROFILE.gpu_cards,
        cpu_type=DEFAULT_RESOURCE_PROFILE.cpu_type,
        vcpus=DEFAULT_RESOURCE_PROFILE.vcpus,
        memory_gib=DEFAULT_RESOURCE_PROFILE.memory_gib,
        classes=DEFAULT_RESOURCE_PROFILE.classes,
    )

    for profile, expected in ((unknown, "unknown"), (tampered, "invalid")):
        transport = RecordingTransport()
        with pytest.raises(ACPError, match=rf"{expected} ACP resource profile"):
            ACPClient(transport, WORKSPACE).select_resource_pool(
                [],
                profile,
                resource_class="spot",
            )
        assert transport.calls == []


def test_job_detail_and_dotted_attribute_list_schema_variants_are_supported():
    detail = _template_job()
    spec = {
        "name": DEFAULT_RESOURCE_SPEC,
        "attributes": [
            {"name": "device.manufacturer", "value": "NVIDIA"},
            {"name": "device.type", "value": "N6lS"},
            {"name": "device.memory", "value": "80Gi"},
            {"name": "device.physical_interface", "value": "SXM5"},
            {"name": "device.number", "value": "2"},
            {"name": "cpu.manufacturer", "value": "Intel"},
            {"name": "cpu.type", "value": "8468"},
            {"name": "cpu.frequency", "value": "2.1GHz"},
            {"name": "cpu.vcpu_allocatable", "value": "28000m"},
            {"name": "memory.allocatable", "value": "405504Mi"},
        ],
    }
    transport = RecordingTransport(
        {"result": {"job": {"detail": detail}}},
        {"data": {"aec2s": [_binding(POOL_A_ID, "pool-a")]}},
        {"payload": {"specs": [spec]}},
    )

    plan = ACPClient(transport, WORKSPACE).plan(name="schema-variant")

    assert plan.pool.spec.gpu == 2
    assert plan.pool.spec.cpu == 28
    assert plan.pool.spec.memory_gib == 396
    assert plan.pool.spec.gpu_type == "NVIDIA N6lS-80G-SXM5"
    assert plan.pool.spec.cpu_type == "Intel 8468-2.1GHz"
    assert plan.body["roles"][0]["image_path"].endswith("old:v1")


def test_flat_live_workspace_binding_schema_is_supported():
    binding = _binding(
        POOL_A_ID,
        "flat-live-pool",
        state="active",
        quota_type="ALL",
        gpu=6,
        cpu=84,
        memory=1188,
        reserved_gpu=4,
        reserved_cpu=56,
        reserved_memory=792,
    )
    assert "aec2" not in binding
    transport = RecordingTransport(
        {"aec2s": [binding]},
        {"resourceSpecs": [_n6ls_spec_wrapped()]},
    )

    plan = ACPClient(transport, WORKSPACE).plan(
        name="flat-live-schema",
        image="registry.example.cn/team/image:v1",
        startup="python train.py",
        template_job=None,
    )

    assert plan.pool.name == "flat-live-pool"
    assert plan.pool.capacity_gpu == 6
    assert plan.pool.capacity_cpu == 84
    assert plan.pool.capacity_memory_gib == 1188


def test_nested_zone_name_in_live_binding_schema_is_supported():
    binding = _binding(POOL_A_ID, "pool-a")
    binding["zone"] = {"name": "cn-sh-01z"}
    transport = RecordingTransport(
        {"job": _template_job()},
        {"aec2s": [binding]},
        {"resourceSpecs": [_n6ls_spec_wrapped()]},
    )

    plan = ACPClient(transport, WORKSPACE).plan(name="nested-zone")

    assert plan.pool.zone == "cn-sh-01z"
    assert plan.body["resource_pool"]["zone"] == "cn-sh-01z"


@pytest.mark.parametrize(
    ("section", "field", "wrong_value"),
    [
        ("device", "manufacturer", "AMD"),
        ("device", "type", "H100"),
        ("device", "memory", 79),
        ("device", "physical_interface", "PCIe"),
        ("device", "number", 1),
        ("cpu", "manufacturer", "AMD"),
        ("cpu", "type", "EPYC"),
        ("cpu", "frequency", 2.2),
        ("cpu", "vcpu_allocatable", 27),
        ("memory", "allocatable", 395),
    ],
)
def test_same_named_spec_must_match_every_catalog_hardware_field(
    section, field, wrong_value
):
    wrong_n6ls = _profile_spec()
    wrong_n6ls[section][field] = wrong_value
    transport = RecordingTransport(
        {"job": _template_job()},
        {"aec2s": [_binding(POOL_A_ID, "pool-a")]},
        {"resourceSpecs": [wrong_n6ls]},
    )

    with pytest.raises(
        ACPError,
        match="selected resource specification does not match the fixed hardware profile",
    ):
        ACPClient(transport, WORKSPACE).plan(name="shape-check")


def test_same_named_malformed_spec_fails_closed_instead_of_falling_back():
    malformed = _profile_spec()
    del malformed["cpu"]["frequency"]
    transport = RecordingTransport(
        {"job": _template_job()},
        {"aec2s": [_binding(POOL_A_ID, "pool-a")]},
        {"resourceSpecs": [malformed]},
    )

    with pytest.raises(
        ACPError,
        match="selected resource specification has an invalid hardware schema",
    ):
        ACPClient(transport, WORKSPACE).plan(name="malformed-selected-spec")


def test_duplicate_same_named_specs_fail_closed_as_ambiguous():
    transport = RecordingTransport(
        {"job": _template_job()},
        {"aec2s": [_binding(POOL_A_ID, "pool-a")]},
        {"resourceSpecs": [_profile_spec(), _profile_spec()]},
    )

    with pytest.raises(
        ACPError,
        match="ambiguous selected resource specification",
    ):
        ACPClient(transport, WORKSPACE).plan(name="duplicate-selected-spec")


def test_unrelated_malformed_specs_are_ignored_but_never_selected():
    cpu_only = {
        "name": "N6lS.Iu.I10.64c256g",
        "device": {"number": 0},
        "cpu": {"vcpu_allocatable": 64},
        "memory": {"allocatable": 256},
    }
    transport = RecordingTransport(
        {"job": _template_job()},
        {"aec2s": [_binding(POOL_A_ID, "cpu-only-pool")]},
        {"resourceSpecs": [cpu_only]},
    )

    with pytest.raises(ACPError, match="no ACTIVE SPOT resource pool"):
        ACPClient(transport, WORKSPACE).plan(name="gpu-job")


def test_empty_specs_skip_one_pool_and_allow_another_eligible_pool():
    transport = RecordingTransport(
        {"job": _template_job()},
        {
            "aec2s": [
                _binding(POOL_A_ID, "empty-pool"),
                _binding(POOL_B_ID, "usable-pool"),
            ]
        },
        {"resourceSpecs": []},
        {"resourceSpecs": [_n6ls_spec_wrapped()]},
    )

    plan = ACPClient(transport, WORKSPACE).plan(name="skip-empty-pool")

    assert plan.pool.name == "usable-pool"


@pytest.mark.parametrize(
    ("identity_field", "debug_value"),
    [
        ("id", POOL_A_ID.replace("pool-a", "DeBuG-pool")),
        ("name", "mixed-DeBuG-name"),
        ("display_name", "Mixed dEbUg display"),
        ("type", "DeBuG_CLUSTER"),
    ],
)
def test_debug_identity_fields_are_excluded_before_specs_and_fall_back(
    identity_field, debug_value
):
    debug_binding = _binding(POOL_A_ID, "pool-a")
    debug_binding[identity_field] = debug_value
    transport = RecordingTransport(
        {
            "aec2s": [
                debug_binding,
                _binding(POOL_B_ID, "normal-pool"),
            ]
        },
        {"resourceSpecs": [_n6ls_spec_wrapped()]},
    )

    plan = ACPClient(transport, WORKSPACE).plan(
        name="exclude-debug-pool",
        image="registry.example.cn/team/image:v1",
        startup="python train.py",
        template_job=None,
    )

    assert plan.pool.resource_id == POOL_B_ID
    spec_calls = [call for call in transport.calls if call["url"].endswith("/resourceSpecs")]
    assert len(spec_calls) == 1
    assert POOL_B_ID in spec_calls[0]["url"]


@pytest.mark.parametrize(
    ("identity_field", "debug_value"),
    [
        ("id", POOL_A_ID.replace("pool-a", "outer-DeBuG-pool")),
        ("name", "outer-DeBuG-name"),
        ("display_name", "Outer dEbUg display"),
        ("type", "OUTER_DEBUG_CLUSTER"),
    ],
)
def test_nested_binding_view_cannot_hide_outer_debug_identity(
    identity_field, debug_value
):
    hidden_debug = _binding(POOL_A_ID, "outer-normal")
    hidden_debug["aec2"] = {
        "id": POOL_A_ID,
        "name": "nested-normal",
        "display_name": "Nested normal pool",
        "state": "ACTIVE",
        "type": "",
        "quota_type": "ALL",
        "vpc_id": "vpc-main",
    }
    hidden_debug[identity_field] = debug_value
    transport = RecordingTransport(
        {
            "aec2s": [
                hidden_debug,
                _binding(POOL_B_ID, "normal-pool"),
            ]
        },
        {"resourceSpecs": [_n6ls_spec_wrapped()]},
    )

    plan = ACPClient(transport, WORKSPACE).plan(
        name="nested-debug-shadow",
        image="registry.example.cn/team/image:v1",
        startup="python train.py",
        template_job=None,
    )

    assert plan.pool.resource_id == POOL_B_ID
    spec_calls = [call for call in transport.calls if call["url"].endswith("/resourceSpecs")]
    assert len(spec_calls) == 1
    assert POOL_B_ID in spec_calls[0]["url"]


def test_multiple_spot_status_entries_are_ambiguous_before_specs_lookup():
    binding = _binding(POOL_A_ID, "ambiguous-spot")
    binding["spot_status"].append(
        {"spot_quota": {"device": 8, "cpu": 112, "memory": 1584}}
    )
    transport = RecordingTransport({"aec2s": [binding]})

    with pytest.raises(ACPError, match="ambiguous SPOT quota status"):
        ACPClient(transport, WORKSPACE).plan(
            name="ambiguous-spot-status",
            image="registry.example.cn/team/image:v1",
            startup="python train.py",
            template_job=None,
        )

    assert len(transport.calls) == 1
    assert not any(call["url"].endswith("/resourceSpecs") for call in transport.calls)


@pytest.mark.parametrize("invalid_name", [None, "burst", 1, True])
def test_spot_status_requires_the_exact_default_bucket_before_specs_lookup(
    invalid_name,
):
    binding = _binding(POOL_A_ID, "invalid-spot-name")
    if invalid_name is None:
        binding["spot_status"][0].pop("spot_name")
    else:
        binding["spot_status"][0]["spot_name"] = invalid_name
    transport = RecordingTransport({"aec2s": [binding]})

    with pytest.raises(ACPError, match="invalid SPOT quota name"):
        ACPClient(transport, WORKSPACE).plan(
            name="invalid-spot-name",
            image="registry.example.cn/team/image:v1",
            startup="python train.py",
            template_job=None,
        )

    assert len(transport.calls) == 1
    assert not any(call["url"].endswith("/resourceSpecs") for call in transport.calls)


@pytest.mark.parametrize("include_snake_alias", [False, True])
def test_spot_status_accepts_camel_alias_when_aliases_agree(include_snake_alias):
    binding = _binding(POOL_A_ID, "camel-spot-name")
    status = binding["spot_status"][0]
    status["spotName"] = "default"
    if not include_snake_alias:
        status.pop("spot_name")
    transport = RecordingTransport(
        {"aec2s": [binding]},
        {"resourceSpecs": [_n6ls_spec_wrapped()]},
    )

    plan = ACPClient(transport, WORKSPACE).plan(
        name="camel-spot-name",
        image="registry.example.cn/team/image:v1",
        startup="python train.py",
        template_job=None,
    )

    assert plan.pool.name == "camel-spot-name"


def test_conflicting_spot_name_aliases_fail_before_specs_lookup():
    binding = _binding(POOL_A_ID, "conflicting-spot-name")
    binding["spot_status"][0]["spotName"] = "future-bucket"
    transport = RecordingTransport({"aec2s": [binding]})

    with pytest.raises(ACPError, match="invalid SPOT quota name"):
        ACPClient(transport, WORKSPACE).plan(
            name="conflicting-spot-name",
            image="registry.example.cn/team/image:v1",
            startup="python train.py",
            template_job=None,
        )

    assert len(transport.calls) == 1
    assert not any(call["url"].endswith("/resourceSpecs") for call in transport.calls)


@pytest.mark.parametrize(
    ("reserved_gpu", "reserved_cpu", "reserved_memory"),
    [
        (5, 84, 1188),
        (6, 83, 1188),
        (6, 84, 1187),
    ],
)
def test_standard_multi_replica_capacity_uses_all_reserved_dimensions(
    reserved_gpu, reserved_cpu, reserved_memory
):
    transport = RecordingTransport(
        {
            "aec2s": [
                _binding(
                    POOL_A_ID,
                    "standard-pool",
                    quota_type="RESERVED",
                    gpu=100,
                    cpu=1000,
                    memory=10000,
                    reserved_gpu=reserved_gpu,
                    reserved_cpu=reserved_cpu,
                    reserved_memory=reserved_memory,
                )
            ]
        },
        {"resourceSpecs": [_n6ls_spec_wrapped()]},
    )

    with pytest.raises(ACPError, match="no ACTIVE RESERVED resource pool"):
        ACPClient(transport, WORKSPACE).plan(
            name="reserved-capacity-bottleneck",
            image="registry.example.cn/team/image:v1",
            startup="python train.py",
            resource_class="standard",
            replicas=3,
            barrier={"type": "TCP", "port": 23456},
            template_job=None,
        )

    assert len(transport.calls) == 2


def test_standard_multi_replica_accepts_exact_full_reserved_capacity():
    transport = RecordingTransport(
        {
            "aec2s": [
                _binding(
                    POOL_A_ID,
                    "standard-pool",
                    quota_type="RESERVED",
                    gpu=1,
                    cpu=1,
                    memory=1,
                    reserved_gpu=6,
                    reserved_cpu=84,
                    reserved_memory=1188,
                )
            ]
        },
        {"resourceSpecs": [_n6ls_spec_wrapped()]},
    )

    plan = ACPClient(transport, WORKSPACE).plan(
        name="exact-reserved-capacity",
        image="registry.example.cn/team/image:v1",
        startup="python train.py",
        resource_class="standard",
        replicas=3,
        barrier={"type": "TCP", "port": 23456},
        template_job=None,
    )

    assert plan.pool.relative_capacity == 3
    assert plan.pool.capacity_gpu == 6
    assert plan.pool.capacity_cpu == 84
    assert plan.pool.capacity_memory_gib == 1188
    assert plan.body["scheduling"]["quota_type"] == "RESERVED"


def test_cross_region_active_binding_fails_before_resource_spec_request():
    cross_region_pool = POOL_A_ID.replace("cn-sh-01z", "cn-bj-01z")
    transport = RecordingTransport(
        {"job": _template_job()},
        {"aec2s": [_binding(cross_region_pool, "wrong-region-pool")]},
    )

    with pytest.raises(ACPError, match="different region"):
        ACPClient(transport, WORKSPACE).plan(name="cross-region-pool")

    assert len(transport.calls) == 2
    assert not any(call["url"].endswith("/resourceSpecs") for call in transport.calls)


def test_workspace_wide_zone_accepts_a_pool_in_the_same_region_az():
    zonal_pool = POOL_A_ID.replace("cn-sh-01z", "cn-sh-01a")
    transport = RecordingTransport(
        {"job": _template_job()},
        {"aec2s": [_binding(zonal_pool, "zonal-pool")]},
        {"resourceSpecs": [_n6ls_spec_wrapped()]},
    )

    plan = ACPClient(transport, WORKSPACE).plan(name="same-region-pool")

    assert plan.pool.zone == "cn-sh-01a"
    assert plan.body["resource_pool"]["zone"] == "cn-sh-01a"


def test_inactive_no_spot_and_insufficient_pools_are_never_selected():
    transport = RecordingTransport(
        {"job": _template_job()},
        {
            "aec2s": [
                _binding(POOL_A_ID, "inactive", state="DELETING"),
                _binding(POOL_A_ID, "on-demand-only", spot=False),
                _binding(POOL_B_ID, "too-small", gpu=1, cpu=100, memory=1000),
            ]
        },
        {"resourceSpecs": [_n6ls_spec_wrapped()]},
    )

    with pytest.raises(ACPError, match="no ACTIVE SPOT resource pool"):
        ACPClient(transport, WORKSPACE).plan(name="no-capacity")

    spec_calls = [call for call in transport.calls if call["url"].endswith("/resourceSpecs")]
    assert len(spec_calls) == 1
    assert POOL_B_ID in spec_calls[0]["url"]
    assert all(call["method"] == "GET" for call in transport.calls)


def test_submit_is_the_only_post_and_does_not_add_enable_queuing():
    transport = _plan_transport()
    transport.responses.append({"data": {"training_job": {"name": "agent-train-01"}}})
    client = ACPClient(transport, WORKSPACE)
    plan = client.plan(name="agent-train-01")

    assert not any(call["method"] == "POST" for call in transport.calls)
    result = client.submit(plan)

    assert result == {"name": "agent-train-01"}
    post_calls = [call for call in transport.calls if call["method"] == "POST"]
    assert len(post_calls) == 1
    post = post_calls[0]
    assert post["url"] == (
        ACP_ORIGIN + "/compute/acp/data/v2" + WORKSPACE + "/trainingJobs"
    )
    assert post["params"] is None
    assert "enable_queuing" not in post["json_body"]
    assert post["json_body"] == plan.body


def test_discarded_dry_run_plan_does_not_retain_its_sensitive_body():
    image = "registry.example.cn/private/weak-plan-sensitive-image:v1"
    startup = "python train.py --secret weak-plan-sensitive-command"
    client = ACPClient(_plan_transport(), WORKSPACE)
    plan = client.plan(name="discarded-dry-run", image=image, startup=startup)
    plan_id = id(plan)
    plan_ref = weakref.ref(plan)

    assert client._issued_plans[plan_id][0]() is plan
    del plan
    gc.collect()

    assert plan_ref() is None
    assert plan_id not in client._issued_plans
    assert image not in repr(client._issued_plans)
    assert startup not in repr(client._issued_plans)


def test_stale_plan_callback_cannot_remove_a_reused_id_entry():
    client = ACPClient(_plan_transport(), WORKSPACE)
    old_plan = client.plan(name="old-dry-run")
    old_id = id(old_plan)
    old_ref = client._issued_plans[old_id][0]
    newer_client = ACPClient(_plan_transport(), WORKSPACE)
    newer_plan = newer_client.plan(name="newer-dry-run")
    newer_issued = newer_client._issued_plans[id(newer_plan)]

    # Simulate another Python implementation delaying the old weakref callback
    # until after its numeric object id has been assigned a newer registry row.
    client._issued_plans[old_id] = newer_issued
    callback = old_ref.__callback__
    assert callback is not None
    callback(old_ref)

    assert client._issued_plans[old_id] is newer_issued
    client._issued_plans.pop(old_id, None)


@pytest.mark.parametrize(
    "mutation",
    [
        "resource-spec",
        "resource-class",
        "resource-pool",
        "replicas",
        "unknown-field",
    ],
)
def test_submit_revalidates_mutated_private_body_before_any_post(mutation):
    transport = _plan_transport()
    transport.responses.append({"training_job": {"name": "must-not-submit"}})
    client = ACPClient(transport, WORKSPACE)
    plan = client.plan(name="mutated-plan")
    calls_before_submit = len(transport.calls)

    if mutation == "resource-spec":
        plan._body["roles"][0]["resource_spec"] = [  # type: ignore[attr-defined]
            {"name": LARGE_RESOURCE_PROFILE.spec_name}
        ]
    elif mutation == "resource-class":
        plan._body["scheduling"]["quota_type"] = "RESERVED"  # type: ignore[attr-defined]
    elif mutation == "resource-pool":
        plan._body["resource_pool"]["name"] = "forged-pool"  # type: ignore[attr-defined]
    elif mutation == "replicas":
        plan._body["roles"][0]["total_replicas"] = 2  # type: ignore[attr-defined]
    else:
        plan._body["uncontrolled"] = True  # type: ignore[attr-defined]

    with pytest.raises(ACPError, match="invalid training job plan"):
        client.submit(plan)

    assert len(transport.calls) == calls_before_submit
    assert not any(call["method"] == "POST" for call in transport.calls)


def test_submit_rejects_forged_plan_components_before_any_post():
    transport = _plan_transport()
    transport.responses.append({"training_job": {"name": "must-not-submit"}})
    client = ACPClient(transport, WORKSPACE)
    plan = client.plan(name="forged-plan")
    calls_before_submit = len(transport.calls)
    profile = plan.pool.profile
    equal_but_noncanonical_profile = ResourceProfile(
        key=profile.key,
        spec_name=profile.spec_name,
        gpu_type=profile.gpu_type,
        gpu_cards=profile.gpu_cards,
        cpu_type=profile.cpu_type,
        vcpus=profile.vcpus,
        memory_gib=profile.memory_gib,
        classes=profile.classes,
    )
    assert equal_but_noncanonical_profile == profile
    assert equal_but_noncanonical_profile is not profile

    forged_plans = [
        replace(
            plan,
            pool=replace(plan.pool, profile=equal_but_noncanonical_profile),
        ),
        replace(
            plan,
            pool=replace(plan.pool, api_quota_type="RESERVED"),
        ),
        replace(
            plan,
            pool=replace(plan.pool, spec=replace(plan.pool.spec, cpu_model="EPYC")),
        ),
        replace(
            plan,
            pool=replace(plan.pool, name="forged-pool"),
        ),
        replace(plan, worker_replicas=2),
    ]

    for forged_plan in forged_plans:
        with pytest.raises(ACPError, match="invalid training job plan"):
            client.submit(forged_plan)

    assert len(transport.calls) == calls_before_submit
    assert not any(call["method"] == "POST" for call in transport.calls)


def test_submit_rejects_coordinated_forgery_before_any_post():
    transport = _plan_transport()
    transport.responses.append({"training_job": {"name": "must-not-submit"}})
    client = ACPClient(transport, WORKSPACE)
    plan = client.plan(name="coordinated-forgery")
    calls_before_submit = len(transport.calls)

    debug_body = plan.body
    debug_body["resource_pool"]["name"] = "debug-cluster-01e"
    debug_plan = replace(
        plan,
        pool=replace(plan.pool, name="debug-cluster-01e"),
        _body=debug_body,
    )

    standard_body = plan.body
    standard_body["scheduling"]["quota_type"] = "RESERVED"
    standard_plan = replace(
        plan,
        pool=replace(
            plan.pool,
            resource_class="standard",
            api_quota_type="RESERVED",
        ),
        _body=standard_body,
    )

    large_body = plan.body
    large_body["roles"][0]["resource_spec"] = [
        {"name": LARGE_RESOURCE_PROFILE.spec_name}
    ]
    large_spec = replace(
        plan.pool.spec,
        name=LARGE_RESOURCE_PROFILE.spec_name,
        gpu_cards=LARGE_RESOURCE_PROFILE.gpu_cards,
        vcpus=LARGE_RESOURCE_PROFILE.vcpus,
        memory_gib=LARGE_RESOURCE_PROFILE.memory_gib,
    )
    large_relative_capacity = min(
        plan.pool.capacity_gpu / LARGE_RESOURCE_PROFILE.gpu_cards,
        plan.pool.capacity_cpu / LARGE_RESOURCE_PROFILE.vcpus,
        plan.pool.capacity_memory_gib / LARGE_RESOURCE_PROFILE.memory_gib,
    )
    large_plan = replace(
        plan,
        pool=replace(
            plan.pool,
            profile=LARGE_RESOURCE_PROFILE,
            spec=large_spec,
            relative_capacity=large_relative_capacity,
        ),
        _body=large_body,
    )

    for forged_plan in (debug_plan, standard_plan, large_plan):
        with pytest.raises(ACPError, match="invalid training job plan"):
            client.submit(forged_plan)

    assert len(transport.calls) == calls_before_submit
    assert not any(call["method"] == "POST" for call in transport.calls)


def test_submit_never_retries_a_mutation_after_401():
    transport = _plan_transport()
    transport.responses.append(SimpleNamespace(status=401, text='{"token":"secret"}'))
    client = ACPClient(transport, WORKSPACE)
    plan = client.plan(name="one-post-only")

    with pytest.raises(ACPAPIError, match="HTTP 401"):
        client.submit(plan)

    assert sum(call["method"] == "POST" for call in transport.calls) == 1
    assert transport.refresh_calls == []


def test_get_can_refresh_once_without_replaying_a_post():
    transport = RecordingTransport(
        SimpleNamespace(status=401, text="unauthorized"),
        SimpleNamespace(status=200, text=json.dumps({"job": _template_job()})),
    )

    job = ACPClient(transport, WORKSPACE).get_template_job()

    assert job["framework"] == "CUSTOM"
    assert len(transport.calls) == 2
    assert transport.refresh_calls == [{"timeout": 60.0}]


def test_plan_body_and_repr_are_defensive_and_redacted():
    image = "registry.example.cn/private/image:sensitive-tag"
    startup = "python train.py --secret startup-sentinel"
    mount_name = "sensitive-mount-sentinel"
    env_value = "sensitive-environment-sentinel"
    plan = ACPClient(_plan_transport(), WORKSPACE).plan(
        name="redacted-plan",
        image=image,
        startup=startup,
        mounts=[{"name": mount_name, "path": "/private-data"}],
        env={"PRIVATE_VALUE": env_value},
    )

    first = plan.body
    first["roles"][0]["image_path"] = "tampered"
    first["mount"][0]["name"] = "tampered"
    first["env"][0]["value"] = "tampered"
    assert plan.body["roles"][0]["image_path"] == image
    assert plan.body["mount"][0]["name"] == mount_name
    assert plan.body["env"][0]["value"] == env_value
    rendered = repr(plan)
    for secret in (image, startup, mount_name, env_value):
        assert secret not in rendered


@pytest.mark.parametrize(
    "name",
    [
        "",
        "Uppercase",
        "-leading",
        "trailing-",
        "has/slash",
        "has space",
        "a" * 64,
    ],
)
def test_invalid_job_names_fail_before_any_request(name):
    transport = RecordingTransport()

    with pytest.raises(ACPError, match="invalid training job name"):
        ACPClient(transport, WORKSPACE).plan(name=name)

    assert transport.calls == []


@pytest.mark.parametrize(
    "mounts",
    [
        {"path": "/data"},
        [123],
        [{"name": "missing-path"}],
        [{"path": "relative"}],
        [{"path": "//data"}],
        [{"path": "/data/../secret"}],
        [{"path": "/data", "type": "hostPath"}],
        [{"path": "/data", "source": {"type": "HOST_PATH_VOLUME"}}],
        [{"path": "/same"}, {"mountPath": "/same"}],
        [{"path": "/data", "mount_path": "/other"}],
        [{"path": "/data", "options": {"ratio": float("nan")}}],
    ],
)
def test_invalid_or_dangerous_mount_overrides_fail_before_requests(mounts):
    transport = RecordingTransport()

    with pytest.raises(ACPError, match="invalid mount configuration"):
        ACPClient(transport, WORKSPACE).plan(
            name="invalid-mount",
            mounts=mounts,
        )

    assert transport.calls == []


def test_mount_limits_bound_entry_count_and_nested_json_depth():
    too_many = [{"path": f"/mount-{index}"} for index in range(65)]
    deeply_nested = {"leaf": True}
    for _ in range(9):
        deeply_nested = {"nested": deeply_nested}

    for mounts in (too_many, [{"path": "/data", "options": deeply_nested}]):
        transport = RecordingTransport()
        with pytest.raises(ACPError, match="invalid mount configuration"):
            ACPClient(transport, WORKSPACE).plan(
                name="bounded-mount",
                mounts=mounts,
            )
        assert transport.calls == []


def test_mount_aliases_accept_absolute_normalized_unique_paths():
    plan = ACPClient(_portable_transport(), WORKSPACE).plan(
        name="mount-aliases",
        image="registry.example.cn/team/image:v1",
        startup="python train.py",
        mounts=[
            {"name": "one", "mount_path": "/mnt/one"},
            {"name": "two", "mountPath": "/mnt/two"},
            {"name": "three", "path": "/mnt/three"},
        ],
        template_job=None,
    )

    assert plan.mount_count == 3
    assert [
        entry.get("mount_path", entry.get("mountPath", entry.get("path")))
        for entry in plan.body["mount"]
    ] == ["/mnt/one", "/mnt/two", "/mnt/three"]


def test_environment_forms_normalize_to_unique_key_value_entries():
    plan = ACPClient(_portable_transport(), WORKSPACE).plan(
        name="environment-forms",
        image="registry.example.cn/team/image:v1",
        startup="python train.py",
        env=[
            {"name": "FIRST", "value": "one"},
            {"key": "SECOND_2", "value": "two"},
        ],
        template_job=None,
    )
    assert plan.body["env"] == [
        {"key": "FIRST", "value": "one"},
        {"key": "SECOND_2", "value": "two"},
    ]

    single = ACPClient(_portable_transport(), WORKSPACE).plan(
        name="single-environment-entry",
        image="registry.example.cn/team/image:v1",
        startup="python train.py",
        env={"name": "ONLY", "value": "value"},
        template_job=None,
    )
    assert single.body["env"] == [{"key": "ONLY", "value": "value"}]


@pytest.mark.parametrize(
    "environment",
    [
        [{"key": "1INVALID", "value": "secret-sentinel"}],
        [{"key": "DUP", "value": "one"}, {"name": "DUP", "value": "two"}],
        [{"key": "EXTRA", "value": "one", "unknown": True}],
        [{"key": "NOT_TEXT", "value": 123}],
        {"HAS_NUL": "secret-sentinel\x00"},
        ["not-an-object"],
    ],
)
def test_invalid_environment_overrides_are_rejected_without_value_leaks(environment):
    transport = RecordingTransport()

    with pytest.raises(ACPError, match="invalid environment variables") as failure:
        ACPClient(transport, WORKSPACE).plan(
            name="invalid-environment",
            env=environment,
        )

    assert "secret-sentinel" not in str(failure.value)
    assert transport.calls == []


def test_environment_count_and_value_length_are_bounded():
    invalid_values = (
        {f"ENV_{index}": "value" for index in range(257)},
        {"TOO_LONG": "s" * (32 * 1024 + 1)},
    )
    for environment in invalid_values:
        transport = RecordingTransport()
        with pytest.raises(ACPError, match="invalid environment variables"):
            ACPClient(transport, WORKSPACE).plan(
                name="bounded-environment",
                env=environment,
            )
        assert transport.calls == []


@pytest.mark.parametrize(
    "resource_id",
    [
        "relative/aec2",
        "/subscriptions/x//aec2s/y",
        "/subscriptions/x/aec2s/../y/extra",
        "/subscriptions/x/aec2s/y?token=secret",
        "/subscriptions/x/aec2s/%2e%2e",
    ],
)
def test_invalid_aec2_resource_ids_never_reach_transport(resource_id):
    transport = RecordingTransport()

    with pytest.raises(ACPError, match="invalid AEC2 resource id"):
        ACPClient(transport, WORKSPACE).list_resource_specs(resource_id)

    assert transport.calls == []


def test_malformed_active_spot_binding_fails_closed_before_pool_fallback():
    invalid = _binding("/subscriptions/x/aec2s/%2e%2e", "bad-pool")
    transport = RecordingTransport(
        {"job": _template_job()},
        {"aec2s": [invalid, _binding(POOL_A_ID, "pool-a")]},
    )

    with pytest.raises(ACPError, match="invalid AEC2 resource id"):
        ACPClient(transport, WORKSPACE).plan(name="fail-closed")

    assert not any(call["url"].endswith("/resourceSpecs") for call in transport.calls)


def test_transport_and_http_errors_do_not_leak_token_or_response_body():
    token = "Bearer token-sentinel"
    body = '{"startup_script":"body-sentinel"}'

    with pytest.raises(ACPError) as transport_failure:
        ACPClient(RecordingTransport(RuntimeError(token)), WORKSPACE).get_template_job()
    assert token not in str(transport_failure.value)
    assert token not in repr(transport_failure.value)
    assert transport_failure.value.__context__ is None

    response = SimpleNamespace(status=500, text=body + token)
    with pytest.raises(ACPAPIError) as http_failure:
        ACPClient(RecordingTransport(response), WORKSPACE).get_template_job()
    rendered = str(http_failure.value) + repr(http_failure.value)
    assert token not in rendered
    assert "body-sentinel" not in rendered

    invalid_json = SimpleNamespace(status=200, text=body + token)
    with pytest.raises(ACPError, match="invalid JSON") as decode_failure:
        ACPClient(RecordingTransport(invalid_json), WORKSPACE).get_template_job()
    rendered = str(decode_failure.value) + repr(decode_failure.value)
    assert token not in rendered
    assert "body-sentinel" not in rendered
    assert decode_failure.value.__context__ is None


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"resource_profile": "not-a-catalog-profile"}, "unknown ACP resource profile"),
        ({"resource_class": "debug"}, "unknown ACP resource class"),
    ],
)
def test_invalid_profile_or_class_fails_before_any_request(kwargs, message):
    transport = RecordingTransport()

    with pytest.raises(ACPError, match=message):
        ACPClient(transport, WORKSPACE).plan(name="invalid-resource-choice", **kwargs)

    assert transport.calls == []


def test_submit_rejects_non_plan_without_touching_transport():
    transport = RecordingTransport()

    with pytest.raises(ACPError, match="requires a training job plan"):
        ACPClient(transport, WORKSPACE).submit({"name": "not-a-plan"})  # type: ignore[arg-type]

    assert transport.calls == []
