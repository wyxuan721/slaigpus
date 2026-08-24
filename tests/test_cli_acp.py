"""Offline CLI tests for ACP planning/submission and Monitor log reading."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import slaigpus.cli as cli  # noqa: E402
from slaigpus.acp import (  # noqa: E402
    DEFAULT_PORTABLE_REPLICAS,
    DEFAULT_TEMPLATE_JOB,
)
from slaigpus.acp_resources import (  # noqa: E402
    DEFAULT_RESOURCE_PROFILE,
    DEFAULT_RESOURCE_PROFILE_KEY,
    RESOURCE_PROFILES,
    RESOURCE_PROFILE_KEYS,
)
from slaigpus.cci import CCIError, DEFAULT_WORKSPACE  # noqa: E402
from slaigpus.cdp import SENSECORE_IAM_AUTH_CAPTURE_URL  # noqa: E402
from slaigpus.monitor import (  # noqa: E402
    ACP_CONTAINER_NAME,
    ACP_HOST_IP,
    ACP_JOB_NAME,
    ACP_POD_NAME,
)


STATION = (
    "/subscriptions/sub/resourceGroups/default/zones/cn-sh-01z/"
    "telemetryStations/private-logs"
)
MONITOR_RESOURCE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _parse(*args: str):
    return cli.build_parser().parse_args(list(args))


def _plan(
    name: str = "new-job",
    *,
    profile=DEFAULT_RESOURCE_PROFILE,
    resource_class: str = "spot",
    template_job=DEFAULT_TEMPLATE_JOB,
):
    pool = SimpleNamespace(
        resource_id=(
            "/subscriptions/sub/resourceGroups/default/zones/cn-sh-01z/"
            "aec2s/spot-pool"
        ),
        name="spot-pool",
        vpc_id="vpc-a",
        zone="cn-sh-01z",
        profile=profile,
        resource_class=resource_class,
        api_quota_type="RESERVED" if resource_class == "standard" else "SPOT",
        capacity_basis=(
            "reserved_entitlement_without_usage"
            if resource_class == "standard"
            else "current_spot_quota"
        ),
        relative_capacity=3.5,
    )
    return SimpleNamespace(
        workspace_id=DEFAULT_WORKSPACE,
        job_name=name,
        pool=pool,
        worker_replicas=1,
        mount_count=1,
        env_count=3,
        template_job=template_job,
    )


def test_acp_submit_parser_defaults_to_dry_run_and_fixed_resources():
    args = _parse(
        "acp",
        "submit",
        "--name",
        "new-job",
        "--image",
        "private/image:v1",
        "--command",
        "python train.py",
    )

    assert args.func is cli.cmd_acp_submit
    assert args.acp_workspace == DEFAULT_WORKSPACE
    assert args.template_job == ""
    assert args.no_template is False
    assert args.worker_config is None
    assert args.replicas is None
    assert args.clear_mounts is False
    assert args.clear_env is False
    assert args.resource_profile == DEFAULT_RESOURCE_PROFILE_KEY
    assert args.resource_class is None
    assert args.apply is False
    assert args.headless is False
    assert "cdp_port" not in vars(args)
    assert args.no_probe is False
    assert args.direct is False
    assert args.ssh_host == ""


def test_acp_browser_uses_direct_mode_without_tunnel_keeper(monkeypatch, tmp_path):
    site = cli.default_site()
    site.profile_dir = tmp_path / "work"
    launches = []
    operation_calls = []

    class Connection:
        port = 0
        stop_calls = 0

        def stop(self):
            self.stop_calls += 1

    connection = Connection()

    class Chrome:
        @staticmethod
        def poll():
            return 0

    chrome = Chrome()

    class Transport:
        def __init__(self):
            self.start_calls = []
            self.close_calls = 0

        def start(self, selected_chrome):
            self.start_calls.append(selected_chrome)

        def close(self):
            self.close_calls += 1

    transport = Transport()

    monkeypatch.setattr(cli, "_resolve_sensecore_site", lambda _args: site)
    monkeypatch.setattr(cli, "_existing_profile_cdp_endpoint", lambda _profile: None)
    monkeypatch.setattr(cli, "_start_tunnel", lambda *_args, **_kwargs: connection)
    monkeypatch.setattr(
        cli,
        "_probe_or_warn",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        cli,
        "_trusted_automatic_login_chrome",
        lambda _site: "/trusted/chrome",
    )
    monkeypatch.setattr(
        cli,
        "_automatic_credential_store",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        cli,
        "_attempt_automatic_login",
        lambda *_args, **_kwargs: "authenticated",
    )
    monkeypatch.setattr(
        cli,
        "launch_chrome",
        lambda **kwargs: launches.append(kwargs) or chrome,
    )
    monkeypatch.setattr(cli, "_make_acp_transport", lambda *_args, **_kwargs: transport)
    monkeypatch.setattr(
        cli,
        "_TunnelKeeper",
        lambda *_args, **_kwargs: pytest.fail(
            "direct ACP must not start an SSH tunnel keeper"
        ),
    )

    args = _parse(
        "acp",
        "submit",
        "--name",
        "job",
        "--image",
        "private/image:v1",
        "--command",
        "true",
        "--no-probe",
    )
    result = cli._run_acp_command(
        args,
        lambda selected_transport, stop_event: operation_calls.append(
            (selected_transport, stop_event)
        )
        or 17,
    )

    assert result == 17
    assert launches[0]["direct"] is True
    assert launches[0]["socks_port"] == 0
    assert transport.start_calls == [chrome]
    assert operation_calls == [(transport, None)]
    assert transport.close_calls == 1
    assert connection.stop_calls == 1


def test_acp_transport_captures_only_the_read_only_iam_identity_request(
    monkeypatch,
):
    captured = {}

    class Transport:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("slaigpus.cdp.BrowserFetchTransport", Transport)

    transport = cli._make_acp_transport(
        43123,
        profile_dir=Path("/private/profile"),
        reuse_existing_page=True,
    )

    assert isinstance(transport, Transport)
    assert captured["auth_capture_base"] == SENSECORE_IAM_AUTH_CAPTURE_URL
    assert captured["auth_capture_exact_path"] is True
    assert captured["auth_capture_methods"] == ("GET",)
    assert captured["auth_requires_console_navigation"] is False
    assert captured["reuse_existing_page"] is True


def test_acp_submit_accepts_startup_alias_and_fixed_resource_choices():
    selected_profile = RESOURCE_PROFILE_KEYS[-1]
    args = _parse(
        "acp",
        "submit",
        "--name",
        "new-job",
        "--image",
        "private/image:v2",
        "--startup",
        "bash run.sh",
        "--resource-profile",
        selected_profile,
        "--resource-class",
        "standard",
        "--apply",
    )

    assert args.startup == "bash run.sh"
    assert args.resource_profile == selected_profile
    assert args.resource_class == "standard"
    assert args.apply is True


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--resource-spec", "custom-spec"),
        ("--gpus", "4"),
        ("--cpus", "56"),
        ("--memory-gib", "792"),
    ],
)
def test_acp_submit_rejects_removed_free_form_resource_flags(flag, value):
    with pytest.raises(SystemExit):
        _parse(
            "acp",
            "submit",
            "--name",
            "new-job",
            "--image",
            "private/image:v2",
            "--command",
            "run",
            flag,
            value,
        )


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--resource-profile", "not-in-the-fixed-library"),
        ("--resource-class", "debug"),
        ("--resource-class", "idle"),
    ],
)
def test_acp_submit_rejects_unknown_profile_and_resource_classes(flag, value):
    with pytest.raises(SystemExit):
        _parse(
            "acp",
            "submit",
            "--name",
            "new-job",
            "--image",
            "private/image:v2",
            "--command",
            "run",
            flag,
            value,
        )


def test_acp_profiles_parser_is_local_and_supports_class_and_json():
    args = _parse(
        "acp",
        "profiles",
        "--resource-class",
        "standard",
        "--json",
    )

    assert args.func is cli.cmd_acp_profiles
    assert args.resource_class == "standard"
    assert args.json is True
    assert "headless" not in vars(args)
    assert "acp_workspace" not in vars(args)


def test_cmd_acp_profiles_json_lists_only_fixed_atomic_profiles(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda *_args, **_kwargs: pytest.fail("profile listing must stay local"),
    )

    args = _parse(
        "acp",
        "profiles",
        "--resource-class",
        "spot",
        "--json",
    )
    assert cli.cmd_acp_profiles(args) == 0

    output = json.loads(capsys.readouterr().out)
    assert len(output["profiles"]) == len(RESOURCE_PROFILES) == 11
    assert {row["profile"] for row in output["profiles"]} == set(
        RESOURCE_PROFILE_KEYS
    )
    assert all(row["resource_classes"] == ["spot", "standard"] for row in output["profiles"])
    assert output["profiles"][0].keys() == {
        "profile",
        "spec",
        "gpu",
        "cpu",
        "memory_gib",
        "resource_classes",
    }
    assert all("debug" not in row["resource_classes"] for row in output["profiles"])


def test_cmd_acp_profiles_human_output_contains_complete_hardware_columns(capsys):
    args = _parse("acp", "profiles")

    assert cli.cmd_acp_profiles(args) == 0

    output = capsys.readouterr().out
    assert "GPU TYPE\tGPU CARDS\tCPU TYPE\tvCPUs\tMEMORY (GiB)" in output
    assert DEFAULT_RESOURCE_PROFILE.key in output
    assert DEFAULT_RESOURCE_PROFILE.gpu_type in output
    assert DEFAULT_RESOURCE_PROFILE.cpu_type in output
    assert DEFAULT_RESOURCE_PROFILE.spec_name in output


def test_acp_submit_accepts_portable_worker_interfaces_and_alias():
    args = _parse(
        "acp",
        "submit",
        "--name",
        "portable-job",
        "--image",
        "private/image:v2",
        "--command",
        "python train.py",
        "--no-template",
        "--worker-config",
        "/private/config/worker.json",
        "--worker-replicas",
        "4",
        "--clear-mounts",
        "--clear-env",
    )

    assert args.no_template is True
    assert args.template_job == ""
    assert args.worker_config == Path("/private/config/worker.json")
    assert args.replicas == 4
    assert args.clear_mounts is True
    assert args.clear_env is True


def test_template_job_and_no_template_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        _parse(
            "acp",
            "submit",
            "--name",
            "job",
            "--image",
            "image:v1",
            "--command",
            "run",
            "--template-job",
            "source-job",
            "--no-template",
        )


def test_acp_logs_parser_requires_station_and_supports_repeatable_filters():
    args = _parse(
        "acp",
        "logs",
        "--job",
        "job-one",
        "--telemetry-station",
        STATION,
        "--pod",
        "pod-a",
        "--pod",
        "pod-b",
        "--container",
        "worker",
        "--host",
        "10.0.0.1",
        "--since",
        "30m",
        "--follow",
    )

    assert args.func is cli.cmd_acp_logs
    assert args.acp_workspace == DEFAULT_WORKSPACE
    assert args.telemetry_station == STATION
    assert args.pod == ["pod-a", "pod-b"]
    assert args.container == ["worker"]
    assert args.host == ["10.0.0.1"]
    assert args.since == "30m"
    assert args.page_size == 40
    assert args.offset == 0
    assert args.order == "desc"
    assert args.follow is True
    assert args.poll_interval == "5s"


@pytest.mark.parametrize(
    "argv",
    [
        ("acp", "submit", "--image", "image", "--command", "run"),
        ("acp", "submit", "--name", "job", "--command", "run"),
        ("acp", "submit", "--name", "job", "--image", "image"),
        ("acp", "logs", "--job", "job"),
        ("acp", "logs", "--telemetry-station", STATION),
    ],
)
def test_acp_required_arguments_fail_during_parsing(argv):
    with pytest.raises(SystemExit):
        _parse(*argv)


def test_cmd_acp_submit_dry_run_never_calls_submit_and_redacts_inputs(
    monkeypatch, capsys
):
    calls = []
    secret_image = "private.registry/team/sensitive-image:v1"
    secret_command = "python train.py --access-token=sensitive-command-value"

    class Client:
        def __init__(self, transport, workspace):
            calls.append(("construct", transport, workspace))

        def plan(self, **kwargs):
            calls.append(("plan", kwargs))
            return _plan(kwargs["name"], template_job=kwargs["template_job"])

        def submit(self, plan):
            pytest.fail("dry-run must not submit")

    transport = object()
    monkeypatch.setattr(cli, "ACPClient", Client)
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda _args, operation: operation(transport, None),
    )

    args = _parse(
        "acp",
        "submit",
        "--name",
        "new-job",
        "--image",
        secret_image,
        "--command",
        secret_command,
        "--json",
    )
    assert cli.cmd_acp_submit(args) == 0

    output_text = capsys.readouterr().out
    output = json.loads(output_text)
    assert output["action"] == "planned"
    assert output["applied"] is False
    assert output["job"] == "new-job"
    assert output["resource_class"] == "spot"
    assert output["api_quota_type"] == "SPOT"
    assert output["resource_pool"]["capacity_basis"] == "current_spot_quota"
    assert output["resources"] == {
        "profile": DEFAULT_RESOURCE_PROFILE.key,
        "spec": DEFAULT_RESOURCE_PROFILE.spec_name,
        "gpu": {
            "type": DEFAULT_RESOURCE_PROFILE.gpu_type,
            "cards": DEFAULT_RESOURCE_PROFILE.gpu_cards,
        },
        "cpu": {
            "type": DEFAULT_RESOURCE_PROFILE.cpu_type,
            "vcpus": DEFAULT_RESOURCE_PROFILE.vcpus,
        },
        "memory_gib": DEFAULT_RESOURCE_PROFILE.memory_gib,
    }
    assert output["worker"] == {
        "replicas": 1,
        "mounts": 1,
        "environment": 3,
    }
    assert output["source"] == {
        "mode": "portable",
        "template_job": None,
    }
    assert secret_image not in output_text
    assert secret_command not in output_text
    assert [item[0] for item in calls] == ["construct", "plan"]
    assert calls[1][1]["image"] == secret_image
    assert calls[1][1]["startup"] == secret_command
    assert calls[1][1]["resource_profile"] == DEFAULT_RESOURCE_PROFILE_KEY
    assert calls[1][1]["resource_class"] == "spot"
    assert calls[1][1]["replicas"] == DEFAULT_PORTABLE_REPLICAS
    assert calls[1][1]["mounts"] is None
    assert calls[1][1]["env"] is None
    assert calls[1][1]["barrier"] is None
    assert calls[1][1]["template_job"] is None


def test_cmd_acp_submit_forwards_standard_profile_and_prints_full_shape(
    monkeypatch, capsys
):
    selected = RESOURCE_PROFILES[-1]
    captured = {}

    class Client:
        def __init__(self, _transport, workspace):
            assert workspace == DEFAULT_WORKSPACE

        def plan(self, **kwargs):
            captured.update(kwargs)
            return _plan(
                kwargs["name"],
                profile=selected,
                resource_class="standard",
            )

        def submit(self, _plan):
            pytest.fail("dry-run must not submit")

    monkeypatch.setattr(cli, "ACPClient", Client)
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda _args, operation: operation(object(), None),
    )
    args = _parse(
        "acp",
        "submit",
        "--name",
        "standard-job",
        "--image",
        "private/image:v1",
        "--command",
        "run",
        "--resource-profile",
        selected.key,
        "--resource-class",
        "standard",
    )

    assert cli.cmd_acp_submit(args) == 0

    assert captured["resource_profile"] == selected.key
    assert captured["resource_class"] == "standard"
    output = capsys.readouterr().out
    assert "class:        standard" in output
    assert "API quota:    RESERVED" in output
    assert f"profile:      {selected.key}" in output
    assert f"GPU type:     {selected.gpu_type}" in output
    assert f"GPU cards:    {selected.gpu_cards}" in output
    assert f"CPU type:     {selected.cpu_type}" in output
    assert f"vCPUs:        {selected.vcpus}" in output
    assert f"memory:       {selected.memory_gib} GiB" in output
    assert (
        "capacity basis: reserved entitlement (not live remaining capacity)"
        in output
    )


def test_cmd_acp_submit_worker_config_overrides_without_echoing_values(
    monkeypatch, capsys
):
    secret = "worker-config-secret-sentinel"
    captured = {}

    class Client:
        def __init__(self, _transport, workspace):
            assert workspace == DEFAULT_WORKSPACE

        def plan(self, **kwargs):
            captured.update(kwargs)
            plan = _plan(kwargs["name"])
            plan.template_job = None
            plan.worker_replicas = 4
            plan.mount_count = 0
            plan.env_count = 0
            return plan

        def submit(self, _plan):
            pytest.fail("dry-run must not submit")

    monkeypatch.setattr(
        cli,
        "load_private_json",
        lambda _path, **_kwargs: {
            "version": 1,
            "replicas": 2,
            "mounts": [{"type": "PV_AFS", "mount_path": "/secret", "id": secret}],
            "env": [{"key": "SECRET", "value": secret}],
            "barrier": {"type": "TCP", "secret": secret},
        },
    )
    monkeypatch.setattr(cli, "ACPClient", Client)
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda _args, operation: operation(object(), None),
    )

    args = _parse(
        "acp",
        "submit",
        "--name",
        "portable-job",
        "--image",
        "private/image:v1",
        "--command",
        "run",
        "--no-template",
        "--worker-config",
        "/private/worker.json",
        "--replicas",
        "4",
        "--clear-mounts",
        "--clear-env",
        "--json",
    )
    assert cli.cmd_acp_submit(args) == 0

    assert captured["template_job"] is None
    assert captured["replicas"] == 4
    assert captured["mounts"] == []
    assert captured["env"] == []
    assert captured["barrier"] == {"type": "TCP", "secret": secret}
    output = capsys.readouterr().out
    assert secret not in output
    result = json.loads(output)
    assert result["source"]["mode"] == "portable"
    assert result["worker"] == {
        "replicas": 4,
        "mounts": 0,
        "environment": 0,
    }


def test_cmd_acp_submit_forwards_file_only_worker_values_and_partial_inheritance(
    monkeypatch, capsys
):
    secret = "file-only-worker-secret"
    captured = []

    class Client:
        def __init__(self, _transport, workspace):
            assert workspace == DEFAULT_WORKSPACE

        def plan(self, **kwargs):
            captured.append(kwargs)
            plan = _plan(kwargs["name"])
            plan.worker_replicas = kwargs["replicas"] or 1
            plan.mount_count = len(kwargs["mounts"] or [])
            plan.env_count = len(kwargs["env"] or [])
            return plan

        def submit(self, _plan):
            pytest.fail("dry-run must not submit")

    configurations = iter(
        [
            {
                "version": 1,
                "replicas": 2,
                "mounts": [
                    {"type": "PV_AFS", "mount_path": "/data", "id": secret}
                ],
                "env": [{"key": "PRIVATE_VALUE", "value": secret}],
                "barrier": {"type": "TCP", "label": secret},
            },
            {"version": 1, "replicas": 2},
        ]
    )
    monkeypatch.setattr(
        cli,
        "load_private_json",
        lambda _path, **_kwargs: next(configurations),
    )
    monkeypatch.setattr(cli, "ACPClient", Client)
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda _args, operation: operation(object(), None),
    )

    common = (
        "acp",
        "submit",
        "--name",
        "file-job",
        "--image",
        "private/image:v1",
        "--command",
        "run",
        "--worker-config",
        "/private/worker.json",
        "--json",
    )
    assert cli.cmd_acp_submit(_parse(*common)) == 0
    assert cli.cmd_acp_submit(_parse(*common)) == 0

    assert captured[0]["replicas"] == 2
    assert captured[0]["mounts"] == [
        {"type": "PV_AFS", "mount_path": "/data", "id": secret}
    ]
    assert captured[0]["env"] == [{"key": "PRIVATE_VALUE", "value": secret}]
    assert captured[0]["barrier"] == {"type": "TCP", "label": secret}
    assert captured[1]["replicas"] == 2
    assert captured[1]["mounts"] is None
    assert captured[1]["env"] is None
    assert captured[1]["barrier"] is None
    assert secret not in capsys.readouterr().out


def test_other_workspace_defaults_to_portable_but_explicit_template_wins():
    other = (
        "/subscriptions/other/resourceGroups/default/zones/cn-sh-01z/"
        "workspaces/other-space"
    )
    portable = _parse(
        "acp",
        "submit",
        "--workspace",
        other,
        "--name",
        "job",
        "--image",
        "image:v1",
        "--command",
        "run",
    )
    templated = _parse(
        "acp",
        "submit",
        "--workspace",
        other,
        "--name",
        "job",
        "--image",
        "image:v1",
        "--command",
        "run",
        "--template-job",
        "their-template",
    )

    assert cli._acp_template_choice(portable) is None
    assert cli._acp_template_choice(templated) == "their-template"


@pytest.mark.parametrize(
    "workspace",
    [
        f" {DEFAULT_WORKSPACE} ",
        DEFAULT_WORKSPACE + "/",
        DEFAULT_WORKSPACE.replace("/resourceGroups/", "//resourceGroups/"),
    ],
)
def test_default_workspace_spelling_stays_portable_without_explicit_template(workspace):
    args = _parse(
        "acp",
        "submit",
        "--workspace",
        workspace,
        "--name",
        "job",
        "--image",
        "image:v1",
        "--command",
        "run",
    )

    assert cli._acp_template_choice(args) is None


def test_unsupported_acp_workspace_fails_before_browser(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda *_args, **_kwargs: pytest.fail("unsupported region must fail first"),
    )
    args = _parse(
        "acp",
        "submit",
        "--workspace",
        DEFAULT_WORKSPACE.replace("cn-sh-01z", "cn-bj-01z"),
        "--name",
        "job",
        "--image",
        "image:v1",
        "--command",
        "run",
    )

    with pytest.raises(CCIError, match="supports cn-sh-01"):
        cli.cmd_acp_submit(args)


def test_invalid_worker_config_fails_before_browser_and_redacts_payload(
    monkeypatch,
):
    secret = "invalid-worker-config-secret"
    monkeypatch.setattr(
        cli,
        "load_private_json",
        lambda _path, **_kwargs: {"version": 1, "unknown": secret},
    )
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda *_args, **_kwargs: pytest.fail("invalid local config must fail first"),
    )
    args = _parse(
        "acp",
        "submit",
        "--name",
        "job",
        "--image",
        "image:v1",
        "--command",
        "run",
        "--worker-config",
        "/private/worker.json",
    )

    with pytest.raises(CCIError, match="invalid private ACP worker") as error:
        cli.cmd_acp_submit(args)
    assert secret not in str(error.value)
    assert error.value.__context__ is None


def test_private_worker_config_read_failure_has_no_exception_chain(monkeypatch):
    def fail_loader(*_args, **_kwargs):
        raise cli.PrivateJSONError("ACP worker configuration")

    monkeypatch.setattr(cli, "load_private_json", fail_loader)
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda *_args, **_kwargs: pytest.fail("local read failure must fail first"),
    )
    args = _parse(
        "acp",
        "submit",
        "--name",
        "job",
        "--image",
        "image:v1",
        "--command",
        "run",
        "--worker-config",
        "/private/worker.json",
    )

    with pytest.raises(CCIError, match="could not read private ACP worker") as error:
        cli.cmd_acp_submit(args)
    assert error.value.__context__ is None


@pytest.mark.parametrize(
    "configuration,extra_args",
    [
        ({"version": 1}, ("--replicas", "0")),
        ({"version": 1, "replicas": 10001}, ()),
        (
            {
                "version": 1,
                "mounts": [{"type": "PV_AFS", "mount_path": "relative"}],
            },
            (),
        ),
        ({"version": 1, "env": [{"key": "INVALID-NAME", "value": "x"}]}, ()),
        ({"version": 1, "barrier": {}}, ()),
        (
            {"version": 1, "barrier": {"type": "TCP", "port": 23456}},
            ("--no-template",),
        ),
    ],
)
def test_invalid_worker_values_fail_before_browser(
    monkeypatch, configuration, extra_args
):
    monkeypatch.setattr(
        cli,
        "load_private_json",
        lambda _path, **_kwargs: configuration,
    )
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda *_args, **_kwargs: pytest.fail("invalid Worker data must fail first"),
    )
    args = _parse(
        "acp",
        "submit",
        "--name",
        "job",
        "--image",
        "image:v1",
        "--command",
        "run",
        "--worker-config",
        "/private/worker.json",
        *extra_args,
    )

    with pytest.raises(CCIError):
        cli.cmd_acp_submit(args)


def test_cmd_acp_submit_apply_submits_the_prepared_plan_exactly_once(
    monkeypatch, capsys
):
    selected_plan = _plan("new-job")
    submissions = []

    class Client:
        def __init__(self, _transport, workspace):
            assert workspace == DEFAULT_WORKSPACE

        def plan(self, **_kwargs):
            return selected_plan

        def submit(self, plan):
            submissions.append(plan)
            return {"name": "new-job"}

    monkeypatch.setattr(cli, "ACPClient", Client)
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda _args, operation: operation(object(), None),
    )

    args = _parse(
        "acp",
        "submit",
        "--name",
        "new-job",
        "--image",
        "private/image:v1",
        "--command",
        "python train.py",
        "--apply",
        "--json",
    )
    assert cli.cmd_acp_submit(args) == 0

    assert submissions == [selected_plan]
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "submitted"
    assert output["applied"] is True


def test_cmd_acp_logs_uses_job_uid_and_all_exact_filters(monkeypatch, capsys):
    calls = []

    class ACP:
        def __init__(self, transport, workspace):
            calls.append(("acp", transport, workspace))

        def get_template_job(self, name):
            calls.append(("job", name))
            return {"detail": {"uid": "runtime-job-uid"}}

    class Monitor:
        def __init__(self, transport, station):
            calls.append(("monitor", transport, station))

        def select_acp_product(self, product):
            calls.append(("product", product))
            return "product.lepton-acp-new"

        def resolve_resource_id(self, product, workspace):
            calls.append(("resource", product, workspace))
            return MONITOR_RESOURCE

        def query_logs(self, product, **kwargs):
            calls.append(("logs", product, kwargs))
            return {
                "total": 1,
                "hits": [{"_id": "one", "body": "hello"}],
            }

    monkeypatch.setattr(cli, "ACPClient", ACP)
    monkeypatch.setattr(cli, "MonitorClient", Monitor)
    monkeypatch.setattr(cli.time, "time", lambda: 10_000)
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda _args, operation: operation("transport", None),
    )

    args = _parse(
        "acp",
        "logs",
        "--job",
        "job-one",
        "--telemetry-station",
        STATION,
        "--pod",
        "pod-a",
        "--container",
        "worker",
        "--host",
        "10.0.0.1",
        "--since",
        "30m",
        "--page-size",
        "80",
        "--offset",
        "3",
        "--order",
        "asc",
        "--filter",
        "error",
        "--json",
    )
    assert cli.cmd_acp_logs(args) == 0

    log_call = next(item for item in calls if item[0] == "logs")
    assert log_call[1] == "product.lepton-acp-new"
    assert log_call[2] == {
        "start": 8_200,
        "end": 10_000,
        "resource_id": MONITOR_RESOURCE,
        "page_size": 80,
        "offset": 3,
        "order": "asc",
        "filter": "error",
        "custom_filter": [
            {"key": ACP_JOB_NAME, "value": "runtime-job-uid"},
            {"key": ACP_POD_NAME, "value": "pod-a"},
            {"key": ACP_CONTAINER_NAME, "value": "worker"},
            {"key": ACP_HOST_IP, "value": "10.0.0.1"},
        ],
    }
    output = json.loads(capsys.readouterr().out)
    assert output["job"] == "job-one"
    assert output["job_filter"] == "runtime-job-uid"
    assert output["product"] == "product.lepton-acp-new"
    assert output["hits"] == [{"_id": "one", "body": "hello"}]


def test_cmd_acp_logs_falls_back_to_job_name_when_uid_is_absent(
    monkeypatch, capsys
):
    queries = []

    class ACP:
        def __init__(self, _transport, workspace):
            assert workspace == DEFAULT_WORKSPACE

        def get_template_job(self, _name):
            return {"name": "job-one"}

    class Monitor:
        def __init__(self, _transport, _station):
            pass

        def select_acp_product(self, _product):
            return "product.lepton-acp"

        def resolve_resource_id(self, _product, workspace):
            assert workspace == DEFAULT_WORKSPACE
            return MONITOR_RESOURCE

        def query_logs(self, _product, **kwargs):
            queries.append(kwargs)
            return {"hits": []}

    monkeypatch.setattr(cli, "ACPClient", ACP)
    monkeypatch.setattr(cli, "MonitorClient", Monitor)
    monkeypatch.setattr(cli.time, "time", lambda: 10_000)
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda _args, operation: operation(object(), None),
    )

    assert cli.cmd_acp_logs(
        _parse(
            "acp",
            "logs",
            "--job",
            "job-one",
            "--telemetry-station",
            STATION,
            "--json",
        )
    ) == 0

    assert queries[0]["custom_filter"][0] == {
        "key": ACP_JOB_NAME,
        "value": "job-one",
    }
    assert json.loads(capsys.readouterr().out)["job_filter"] == "job-one"


def test_cmd_acp_logs_follow_polls_and_deduplicates_across_pages(
    monkeypatch, capsys
):
    pages = [
        {
            "hits": [
                {"_id": "one", "body": "first"},
                {"_id": "two", "body": "second"},
            ]
        },
        {
            "hits": [
                {"_id": "one", "body": "first"},
                {"_id": "two", "body": "second"},
                {"_id": "three", "body": "third"},
            ]
        },
    ]
    query_count = []

    class ACP:
        def __init__(self, _transport, workspace):
            assert workspace == DEFAULT_WORKSPACE
            pass

        def get_template_job(self, _name):
            return {"uid": "runtime-job-uid"}

    class Monitor:
        def __init__(self, _transport, _station):
            pass

        def select_acp_product(self, _product):
            return "product.lepton-acp-new"

        def resolve_resource_id(self, _product, workspace):
            assert workspace == DEFAULT_WORKSPACE
            return MONITOR_RESOURCE

        def query_logs(self, _product, **_kwargs):
            index = len(query_count)
            query_count.append(index)
            return pages[index]

    class StopAfterSecondPoll:
        def __init__(self):
            self.waits = []

        def wait(self, interval):
            self.waits.append(interval)
            return len(self.waits) == 2

    stop_event = StopAfterSecondPoll()
    monkeypatch.setattr(cli, "ACPClient", ACP)
    monkeypatch.setattr(cli, "MonitorClient", Monitor)
    monkeypatch.setattr(cli.time, "time", lambda: 10_000)
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda _args, operation: operation(object(), stop_event),
    )

    args = _parse(
        "acp",
        "logs",
        "--job",
        "job-one",
        "--telemetry-station",
        STATION,
        "--follow",
        "--poll-interval",
        "2s",
        "--json",
    )
    assert cli.cmd_acp_logs(args) == 0

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["_id"] for line in lines] == ["one", "two", "three"]
    assert query_count == [0, 1]
    assert stop_event.waits == [2.0, 2.0]


def test_follow_paginates_each_interval_so_a_large_burst_is_not_lost(
    monkeypatch, capsys
):
    calls = []
    polls = [
        {
            0: [{"_id": "five"}, {"_id": "four"}],
            2: [{"_id": "three"}, {"_id": "two"}],
            4: [{"_id": "one"}],
        },
        {
            0: [{"_id": "seven"}, {"_id": "six"}],
            2: [{"_id": "five"}, {"_id": "four"}],
            4: [{"_id": "three"}, {"_id": "two"}],
            6: [{"_id": "one"}],
        },
    ]
    poll_index = {"value": 0}

    class ACP:
        def __init__(self, _transport, workspace):
            assert workspace == DEFAULT_WORKSPACE
            pass

        def get_template_job(self, _name):
            return {"uid": "runtime-job-uid"}

    class Monitor:
        def __init__(self, _transport, _station):
            pass

        def select_acp_product(self, _product):
            return "product.lepton-acp-new"

        def resolve_resource_id(self, _product, workspace):
            assert workspace == DEFAULT_WORKSPACE
            return MONITOR_RESOURCE

        def query_logs(self, _product, **kwargs):
            offset = kwargs["offset"]
            calls.append((poll_index["value"], offset, kwargs["start"], kwargs["end"]))
            hits = polls[poll_index["value"]][offset]
            return {
                "hits": hits,
                "total": sum(
                    len(page) for page in polls[poll_index["value"]].values()
                ),
            }

    class StopAfterSecondPoll:
        def wait(self, _interval):
            poll_index["value"] += 1
            return poll_index["value"] == 2

    times = iter((10_000, 10_005))
    monkeypatch.setattr(cli, "ACPClient", ACP)
    monkeypatch.setattr(cli, "MonitorClient", Monitor)
    monkeypatch.setattr(cli.time, "time", lambda: next(times))
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda _args, operation: operation(object(), StopAfterSecondPoll()),
    )

    args = _parse(
        "acp",
        "logs",
        "--job",
        "job-one",
        "--telemetry-station",
        STATION,
        "--page-size",
        "2",
        "--follow",
        "--json",
    )
    assert cli.cmd_acp_logs(args) == 0

    output = [json.loads(line)["_id"] for line in capsys.readouterr().out.splitlines()]
    assert output == ["five", "four", "three", "two", "one", "seven", "six"]
    assert [(poll, offset) for poll, offset, _start, _end in calls] == [
        (0, 0),
        (0, 2),
        (0, 4),
        (1, 0),
        (1, 2),
        (1, 4),
        (1, 6),
    ]
    # The second poll uses an overlap rather than rescanning the whole --since
    # window; identities still suppress repeated entries.
    assert calls[3][2:] == (9_940, 10_005)


def test_follow_deduplication_cache_is_bounded_and_fifo():
    seen = {}

    assert cli._remember_log_hit(seen, "one", limit=2) is True
    assert cli._remember_log_hit(seen, "two", limit=2) is True
    assert cli._remember_log_hit(seen, "two", limit=2) is False
    assert cli._remember_log_hit(seen, "three", limit=2) is True
    assert list(seen) == ["two", "three"]
    # Once an old entry leaves the rolling cache, seeing it again is new and
    # evicts the next oldest identity without exceeding the bound.
    assert cli._remember_log_hit(seen, "one", limit=2) is True
    assert list(seen) == ["three", "one"]


@pytest.mark.parametrize("since", ["0s", "not-a-duration"])
def test_cmd_acp_logs_rejects_invalid_since_without_querying(monkeypatch, since):
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda _args, operation: operation(object(), None),
    )
    monkeypatch.setattr(
        cli,
        "ACPClient",
        lambda *_args, **_kwargs: pytest.fail("invalid duration must fail first"),
    )
    args = _parse(
        "acp",
        "logs",
        "--job",
        "job-one",
        "--telemetry-station",
        STATION,
        "--since",
        since,
    )

    with pytest.raises(CCIError):
        cli.cmd_acp_logs(args)


def test_follow_rejects_nonzero_offset_before_contacting_acp(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_run_acp_command",
        lambda _args, operation: operation(object(), None),
    )
    monkeypatch.setattr(
        cli,
        "ACPClient",
        lambda *_args, **_kwargs: pytest.fail("invalid offset must fail first"),
    )

    args = _parse(
        "acp",
        "logs",
        "--job",
        "job-one",
        "--telemetry-station",
        STATION,
        "--follow",
        "--offset",
        "1",
    )

    with pytest.raises(CCIError, match="offset must be zero"):
        cli.cmd_acp_logs(args)
