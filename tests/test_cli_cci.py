"""Offline integration tests for the CCI-facing CLI defaults.

These tests deliberately stop at process boundaries: they exercise argument
parsing and the configuration/browser/tunnel glue without opening Chrome,
starting a real SSH process, or contacting SenseCore.
"""

from __future__ import annotations

import json
import sys
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import slaigpus.cli as cli  # noqa: E402
import slaigpus.cdp as cdp_module  # noqa: E402
import slaigpus.tunnel as tunnel_module  # noqa: E402
from slaigpus.browser import ChromeArgumentError, build_chrome_args  # noqa: E402
from slaigpus.cci import (  # noqa: E402
    CCIError,
    DEFAULT_RENEW_AFTER,
    DEFAULT_WORKSPACE,
)
from slaigpus.cdp import CDPTimeout  # noqa: E402
from slaigpus.config import (  # noqa: E402
    DEFAULT_SITE_NAME,
    DEFAULT_SSH_HOST,
    DEFAULT_URL,
    Config,
    Site,
)
from slaigpus.tunnel import SSHTunnel  # noqa: E402


def _parse(*args: str):
    return cli.build_parser().parse_args(list(args))


def test_cci_browser_transport_allowlists_namespace_capacity_api(monkeypatch):
    captured = {}
    sentinel = object()

    def build(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(cdp_module, "BrowserFetchTransport", build)
    options = SimpleNamespace(workspace=DEFAULT_WORKSPACE)

    assert cli._make_browser_transport(45678, options) is sentinel
    assert "https://ccr.cn-sh-01.sensecoreapi.cn" in captured[
        "allowed_request_prefixes"
    ]
    assert "https://network.cn-sh-01.sensecoreapi.cn" in captured[
        "allowed_request_prefixes"
    ]


def test_bare_open_resolves_builtin_site_without_config(monkeypatch):
    """The user's everyday command is useful with no TOML or flags at all."""
    monkeypatch.setattr(cli, "load_config", lambda _path=None: Config())

    args = _parse("open")
    site = cli._resolve_site(args, allow_builtin=True)

    assert site.name == DEFAULT_SITE_NAME
    assert site.ssh_host == DEFAULT_SSH_HOST == ""
    assert site.mode == "direct"
    assert site.url == DEFAULT_URL == "https://zhicheng.signin.sensecore.cn/"
    assert cli._use_cci_watch(args, site) is True


def test_sensecore_network_config_and_cli_override_share_one_resolver(monkeypatch):
    configured = Site(
        name=DEFAULT_SITE_NAME,
        ssh_host="configured-jump",
        url=DEFAULT_URL,
        network_mode="ssh",
    )
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path=None: Config(sensecore=configured),
    )

    selected = cli._resolve_sensecore_site(_parse("cci", "status"))
    assert selected is configured
    assert selected.mode == "ssh"
    assert selected.ssh_host == "configured-jump"

    direct = cli._resolve_sensecore_site(_parse("cci", "status", "--direct"))
    assert direct.mode == "direct"
    assert direct.ssh_host == ""

    overridden = cli._resolve_sensecore_site(
        _parse("cci", "status", "--ssh-host", "temporary-jump")
    )
    assert overridden.mode == "ssh"
    assert overridden.ssh_host == "temporary-jump"


def test_explicit_network_override_refuses_a_running_automation_chrome(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_sensecore_site", lambda _args: cli.default_site())
    monkeypatch.setattr(
        cli,
        "_existing_profile_cdp_endpoint",
        lambda _profile: SimpleNamespace(port=45678),
    )

    with pytest.raises(CCIError, match="stop the controller"):
        cli._run_cci_command(
            _parse("cci", "status", "--direct"),
            lambda *_args: pytest.fail("operation must not run"),
        )


def test_viewer_parser_and_command_force_cci_watcher_off(monkeypatch):
    args = _parse("viewer")

    assert args.func is cli.cmd_viewer
    assert args.cci_watch is False
    assert args.viewer_auto_login is True
    assert args.credentials_file is None
    assert cli._use_cci_watch(
        args, Site("sensecore", DEFAULT_SSH_HOST, DEFAULT_URL)
    ) is False

    forwarded = []
    monkeypatch.setattr(
        cli,
        "cmd_open",
        lambda command_args: forwarded.append(command_args.cci_watch) or 17,
    )
    # The handler is also defensive if an embedding caller constructs or
    # mutates a Namespace without going through build_parser().
    args.cci_watch = True

    assert cli.cmd_viewer(args) == 17
    assert forwarded == [False]


def test_viewer_parser_accepts_a_private_credentials_file(tmp_path):
    credentials = tmp_path / "viewer-credentials.json"

    args = _parse("viewer", "--credentials-file", str(credentials))

    assert args.credentials_file == credentials
    assert args.viewer_auto_login is True


def test_controller_parser_accepts_network_selection_and_parses_credentials(tmp_path):
    defaults = _parse("controller")

    assert defaults.func is cli.cmd_controller
    assert defaults.credentials_file is None
    assert defaults.direct is False
    assert defaults.ssh_host == ""
    assert "site" not in vars(defaults)
    # There is deliberately no opt-out: cmd_controller always constructs a
    # headless-only worker rather than trusting a user-controlled flag.
    assert "headless" not in vars(defaults)

    credentials_path = tmp_path / "controller-credentials.json"
    explicit = _parse(
        "controller",
        "--credentials-file",
        str(credentials_path),
    )

    assert explicit.func is cli.cmd_controller
    assert explicit.credentials_file == credentials_path


@pytest.mark.parametrize("argv", [("controller", "other-site"), ("controller", "--direct", "--ssh-host", "jump")])
def test_cci_controller_parser_rejects_site_and_conflicting_network(argv):
    with pytest.raises(SystemExit):
        _parse(*argv)


def test_product_commands_accept_direct_or_ssh_alias_overrides():
    assert _parse("controller", "--direct").direct is True
    assert _parse("controller", "--ssh-host", "jump").ssh_host == "jump"
    assert _parse("cci", "status", "--direct").direct is True
    assert _parse("cci", "status", "--ssh-host", "jump").ssh_host == "jump"


def test_only_managed_sensecore_identity_enables_cci_watch_by_default(monkeypatch):
    configured = Site(
        name="my-existing-config-name",
        ssh_host=DEFAULT_SSH_HOST,
        url=DEFAULT_URL,
    )
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path=None: Config(
            sites={configured.name: configured}, default_site=configured.name
        ),
    )

    args = _parse("open", configured.name)
    site = cli._resolve_site(args, allow_builtin=True)

    assert site is configured
    assert cli._use_cci_watch(args, site) is False


def test_generic_site_does_not_enable_cci_watch_implicitly(monkeypatch):
    generic = Site(
        name="intranet",
        ssh_host="ordinary-jump-host",
        url="https://wiki.internal/",
    )
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path=None: Config(sites={generic.name: generic}),
    )

    args = _parse("open", generic.name)
    site = cli._resolve_site(args, allow_builtin=True)

    assert cli._use_cci_watch(args, site) is False


def test_bare_open_ignores_implicitly_found_legacy_default(monkeypatch):
    legacy = Site("legacy", "old-host", "https://old.example/")
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path=None: Config(
            sites={legacy.name: legacy}, default_site=legacy.name
        ),
    )

    site = cli._resolve_site(_parse("open"), allow_builtin=True)

    assert site.ssh_host == DEFAULT_SSH_HOST
    assert site.mode == "direct"
    assert site.url == DEFAULT_URL


def test_explicit_target_is_clean_and_does_not_load_implicit_config(monkeypatch):
    legacy = Site(
        "legacy",
        "old-host",
        "https://old.example/",
        socks_port=1088,
        profile_dir=Path("/legacy-profile"),
        ssh_args=["-J", "old-bastion"],
        block_local_dns=False,
        chrome_args=["--ignore-certificate-errors"],
    )
    load_calls = []

    def load_implicit(path=None):
        load_calls.append(path)
        return Config(sites={legacy.name: legacy}, default_site=legacy.name)

    monkeypatch.delenv("SLAIGPUS_CONFIG", raising=False)
    monkeypatch.setattr(cli, "load_config", load_implicit)

    site = cli._resolve_site(
        _parse(
            "open",
            "--ssh-host",
            "chosen-jump",
            "--url",
            DEFAULT_URL,
        ),
        allow_builtin=True,
    )

    assert load_calls == []
    assert site.name == "adhoc"
    assert site.ssh_host == "chosen-jump"
    assert site.mode == "ssh"
    assert site.url == DEFAULT_URL
    assert site.socks_port == 0
    assert site.profile_dir is None
    assert site.ssh_args == []
    assert site.block_local_dns is True
    assert site.chrome_args == []


def test_explicit_config_still_selects_its_default_site(monkeypatch, tmp_path):
    configured = Site("configured", "chosen-host", "https://chosen.example/")
    config_path = tmp_path / "chosen.toml"
    calls = []

    def load_explicit(path=None):
        calls.append(path)
        return Config(
            sites={configured.name: configured}, default_site=configured.name
        )

    monkeypatch.setattr(cli, "load_config", load_explicit)

    site = cli._resolve_site(
        _parse("open", "--config", str(config_path)), allow_builtin=True
    )

    assert calls == [config_path]
    assert site is configured


@pytest.mark.parametrize(
    ("argv", "site", "expected"),
    [
        (("open", "--no-cci-watch"), Site("sensecore", DEFAULT_SSH_HOST, DEFAULT_URL), False),
        (("open", "--cci-watch"), Site("other", "jump", "https://wiki.internal/"), True),
    ],
)
def test_explicit_cci_watch_switch_overrides_site_default(argv, site, expected):
    args = _parse(*argv)

    assert cli._use_cci_watch(args, site) is expected


@pytest.mark.parametrize(
    ("argv", "handler", "extra_name", "extra_value"),
    [
        (("cci", "status", "--json"), cli.cmd_cci_status, "json", True),
        (("cci", "start", "--json"), cli.cmd_cci_start, "json", True),
        (("cci", "renew", "--if-due"), cli.cmd_cci_renew, "if_due", True),
        (("cci", "remaining", "--json"), cli.cmd_cci_remaining, "json", True),
        (("cci", "watch", "--once"), cli.cmd_cci_watch, "once", True),
    ],
)
def test_cci_subcommands_parse(argv, handler, extra_name, extra_value):
    args = _parse(*argv)

    assert args.cmd == "cci"
    assert args.func is handler
    assert getattr(args, extra_name) is extra_value
    options = cli._cci_options(args)
    assert options.workspace == DEFAULT_WORKSPACE
    assert options.renew_after == DEFAULT_RENEW_AFTER == 3 * 3600 + 50 * 60


def test_cci_target_and_timing_options_parse_on_subcommands():
    args = _parse(
        "cci",
        "watch",
        "--workspace",
        DEFAULT_WORKSPACE,
        "--app",
        "example-cci",
        "--instance",
        "example-cci-0",
        "--container",
        "trainer",
        "--namespace",
        "private-team",
        "--renew-after",
        "3h40m",
        "--poll-interval",
        "45s",
        "--wait-timeout",
        "12m",
    )

    options = cli._cci_options(args)

    assert options.workspace == DEFAULT_WORKSPACE
    assert options.app == "example-cci"
    assert options.instance == "example-cci-0"
    assert options.container == "trainer"
    assert options.namespace == "private-team"
    assert options.renew_after == 3 * 3600 + 40 * 60
    assert options.poll_interval == 45
    assert options.wait_timeout == 12 * 60


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("controller", "--cci", "训练容器 A"), "训练容器 A"),
        (("open", "--cci", "visible-cci"), "visible-cci"),
        (("cci", "watch", "--cci", "watch-target"), "watch-target"),
        (("cci", "status", "--app", "legacy-app"), "legacy-app"),
        (("open", "--cci-app", "legacy-open-app"), "legacy-open-app"),
    ],
)
def test_cci_selector_parameter_and_legacy_aliases(argv, expected):
    args = _parse(*argv)

    assert args.cci_app == expected
    assert cli._cci_options(args).app == expected


@pytest.mark.parametrize(
    "flags",
    [
        ("--renew-after", "not-a-duration"),
        ("--renew-after", "4h"),
        ("--poll-interval", "0s"),
        ("--wait-timeout", "3:40"),
    ],
)
def test_invalid_cci_durations_are_rejected(flags):
    args = _parse("cci", "watch", *flags)

    with pytest.raises(CCIError):
        cli._cci_options(args)


def test_cmd_cci_renew_reports_external_image_change(monkeypatch):
    messages = []
    result = SimpleNamespace(action="external_change")
    supervisor = SimpleNamespace(renew=lambda **_kwargs: result)

    def run_command(_args, operation):
        return operation(supervisor, None)

    monkeypatch.setattr(cli, "_run_cci_command", run_command)
    monkeypatch.setattr(cli, "ok", messages.append)

    assert cli.cmd_cci_renew(_parse("cci", "renew")) == 0
    assert messages == [
        "an external/manual container image change was detected; "
        "the stale renewal was discarded and no PATCH was sent"
    ]


def test_cmd_cci_start_json_reports_action_and_ready_status(monkeypatch, capsys):
    status_data = {"app": "trainer", "state": "RUNNING"}
    result = SimpleNamespace(
        action="started",
        status=SimpleNamespace(to_dict=lambda: status_data),
    )
    supervisor = SimpleNamespace(start=lambda: result)
    monkeypatch.setattr(
        cli,
        "_run_cci_command",
        lambda _args, operation: operation(supervisor, None),
    )

    assert cli.cmd_cci_start(_parse("cci", "start", "--json")) == 0

    assert json.loads(capsys.readouterr().out) == {
        "action": "started",
        "status": status_data,
    }


def test_cmd_cci_renew_json_is_structured_and_suppresses_human_message(
    monkeypatch, capsys
):
    status_data = {"app": "trainer", "due": False}
    result = SimpleNamespace(
        action="renewed",
        image_uri="private/image:snapshot",
        status=SimpleNamespace(to_dict=lambda: status_data),
    )
    supervisor = SimpleNamespace(renew=lambda **_kwargs: result)
    messages = []

    monkeypatch.setattr(
        cli,
        "_run_cci_command",
        lambda _args, operation: operation(supervisor, None),
    )
    monkeypatch.setattr(cli, "ok", messages.append)

    assert cli.cmd_cci_renew(_parse("cci", "renew", "--json")) == 0

    assert json.loads(capsys.readouterr().out) == {
        "action": "renewed",
        "image_uri": "private/image:snapshot",
        "status": status_data,
    }
    assert messages == []


def test_cmd_cci_remaining_json_reports_both_deadlines_and_control(
    monkeypatch, capsys
):
    source = {
        "app": "sensecore-proxy",
        "instance": "sensecore-proxy-0",
        "last_started_time": "2026-08-23T08:30:00+00:00",
        "checked_at": "2026-08-23T12:00:00+00:00",
        "renew_at": "2026-08-23T12:10:00+00:00",
        "due_in_seconds": 600,
        "due": False,
        "expires_at": "2026-08-23T12:30:00+00:00",
        "expires_in_seconds": 1800,
        "expired": False,
    }
    status_calls = []
    supervisor = SimpleNamespace(
        status=lambda **kwargs: status_calls.append(kwargs)
        or SimpleNamespace(to_dict=lambda: source),
        control=SimpleNamespace(status=lambda: False),
    )
    monkeypatch.setattr(
        cli,
        "_run_cci_command",
        lambda _args, operation: operation(supervisor, None),
    )

    assert cli.cmd_cci_remaining(_parse("cci", "remaining", "--json")) == 0

    assert json.loads(capsys.readouterr().out) == {
        **source,
        "renew_in_seconds": 600,
        "auto_renew_enabled": False,
    }
    assert status_calls == [{"include_namespace": False}]


def test_cmd_cci_status_human_output_includes_hard_expiry(monkeypatch, capsys):
    data = {
        "app": "app",
        "instance": "instance",
        "container": "container",
        "namespace": "namespace",
        "image_path": "image",
        "last_started_time": "2026-08-23T08:00:00+00:00",
        "age_seconds": 3 * 3600,
        "due_in_seconds": 40 * 60,
        "expires_in_seconds": 3600,
        "dnat_rules": [
            {"endpoint": "180.184.249.129:10244→22(tcp)"},
        ],
    }
    status_calls = []
    supervisor = SimpleNamespace(
        status=lambda **kwargs: status_calls.append(kwargs)
        or SimpleNamespace(to_dict=lambda: data)
    )
    monkeypatch.setattr(
        cli,
        "_run_cci_command",
        lambda _args, operation: operation(supervisor, None),
    )

    assert cli.cmd_cci_status(_parse("cci", "status")) == 0

    output = capsys.readouterr().out
    assert "dnat:         180.184.249.129:10244→22(tcp)" in output
    assert "renew in:     40m" in output
    assert "expires in:   1h" in output
    assert status_calls == [{"include_namespace": True, "include_dnat": True}]


@pytest.mark.parametrize(
    ("argv", "expected_method", "expected_enabled"),
    [
        (("cci", "auto-renew", "on", "--json"), "enable", True),
        (("cci", "auto-renew", "off", "--json"), "disable", False),
        (("cci", "auto-renew", "status", "--json"), "status", False),
    ],
)
def test_cmd_cci_auto_renew_is_local_only(
    monkeypatch, capsys, argv, expected_method, expected_enabled
):
    calls = []

    class Control:
        def __init__(self, workspace):
            calls.append(("construct", workspace))
            self.workspace = SimpleNamespace(resource_id=workspace)

        def enable(self):
            calls.append(("enable",))
            return True

        def disable(self):
            calls.append(("disable",))
            return False

        def status(self):
            calls.append(("status",))
            return False

    monkeypatch.setattr(cli, "AutoRenewControlStore", Control)
    monkeypatch.setattr(
        cli,
        "_resolve_site",
        lambda *_args, **_kwargs: pytest.fail("control must not resolve a site"),
    )
    monkeypatch.setattr(
        cli,
        "_start_tunnel",
        lambda *_args, **_kwargs: pytest.fail("control must not start SSH"),
    )
    monkeypatch.setattr(
        cli,
        "launch_chrome",
        lambda *_args, **_kwargs: pytest.fail("control must not launch Chrome"),
    )

    assert cli.cmd_cci_auto_renew(_parse(*argv)) == 0

    assert calls == [("construct", DEFAULT_WORKSPACE), (expected_method,)]
    if argv[-1] == "--json":
        assert json.loads(capsys.readouterr().out) == {
            "workspace": DEFAULT_WORKSPACE,
            "enabled": expected_enabled,
        }


def test_chrome_cdp_is_loopback_only_without_global_origin_exception(tmp_path):
    args = build_chrome_args(
        socks_port=1080,
        profile_dir=tmp_path,
        cdp_port=49152,
    )

    assert "--remote-debugging-port=49152" in args
    assert "--remote-debugging-address=127.0.0.1" in args
    assert not any(arg.startswith("--remote-allow-origins") for arg in args)


def test_chrome_random_cdp_and_protected_flags(tmp_path):
    args = build_chrome_args(
        socks_port=1080,
        profile_dir=tmp_path,
        cdp_port=0,
        enable_cdp=True,
    )
    assert "--remote-debugging-port=0" in args
    assert "--remote-debugging-address=127.0.0.1" in args

    with pytest.raises(ChromeArgumentError, match="managed browser settings"):
        build_chrome_args(
            socks_port=1080,
            profile_dir=tmp_path,
            enable_cdp=True,
            extra_args=["--remote-debugging-address=0.0.0.0"],
        )


@pytest.mark.parametrize(
    "conflicting_arg",
    [
        "--proxy-bypass-list=*",
        "--no-proxy-server",
        "--proxy-pac-url=http://127.0.0.1/proxy.pac",
        "--proxy-auto-detect",
    ],
)
def test_chrome_rejects_flags_that_can_bypass_managed_socks(
    tmp_path, conflicting_arg
):
    with pytest.raises(ChromeArgumentError, match="managed browser settings"):
        build_chrome_args(
            socks_port=1080,
            profile_dir=tmp_path,
            enable_cdp=True,
            extra_args=[conflicting_arg],
        )


class _FakeSSHProcess:
    def __init__(self) -> None:
        self.alive = True
        self.returncode = None

    def poll(self):
        return None if self.alive else self.returncode


def test_dead_tunnel_restarts_on_the_original_dynamic_port(monkeypatch):
    """Chrome keeps its proxy port, so a CCI reboot must not allocate a new one."""
    chosen_ports = []
    processes = []
    commands = []

    def choose_port():
        chosen_ports.append(43871)
        return 43871

    def fake_popen(command, **_kwargs):
        process = _FakeSSHProcess()
        processes.append(process)
        commands.append(command)
        return process

    monkeypatch.setattr(tunnel_module.shutil, "which", lambda _binary: "/usr/bin/ssh")
    monkeypatch.setattr(tunnel_module, "free_port", choose_port)
    monkeypatch.setattr(tunnel_module, "port_is_open", lambda _port: False)
    monkeypatch.setattr(tunnel_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(SSHTunnel, "_wait_until_ready", lambda self: None)

    tunnel = SSHTunnel("sensecore-proxy")
    tunnel.start()
    original_port = tunnel.port
    processes[0].alive = False
    processes[0].returncode = 255

    tunnel.start()

    assert tunnel.port == original_port == 43871
    assert chosen_ports == [43871]
    assert len(commands) == 2
    for command in commands:
        dynamic_index = command.index("-D")
        assert command[dynamic_index + 1] == "127.0.0.1:43871"

    # Avoid asking stop() to signal a process that exists only in this test.
    processes[-1].alive = False
    processes[-1].returncode = 0
    tunnel.stop()


class _FakeOpenTunnel:
    port = 1080
    is_running = True

    def __init__(self, events=None) -> None:
        self.events = events
        self.stop_calls = 0

    def stop(self) -> None:
        if self.events is not None:
            self.events.append("tunnel.stop")
        self.stop_calls += 1


class _FakeOpenChrome:
    def __init__(self, events=None) -> None:
        self.events = events
        self.alive = True
        self.terminate_calls = 0
        self.wait_timeouts = []
        self.kill_calls = 0

    def poll(self):
        return None if self.alive else 0

    def terminate(self) -> None:
        if self.events is not None:
            self.events.append("chrome.terminate")
        self.terminate_calls += 1
        self.alive = False

    def wait(self, *, timeout):
        if self.events is not None:
            self.events.append("chrome.wait")
        self.wait_timeouts.append(timeout)
        return 0

    def kill(self) -> None:
        if self.events is not None:
            self.events.append("chrome.kill")
        self.kill_calls += 1
        self.alive = False


def test_viewer_login_bootstrap_is_visible_login_only_and_closes_cleanly(
    monkeypatch, tmp_path
):
    events = []
    launches = []
    profile = tmp_path / "work-profile"
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=profile,
    )

    class BootstrapChrome(_FakeOpenChrome):
        def wait(self, *, timeout):
            events.append(("chrome.wait", timeout))
            self.alive = False
            return 0

    chrome = BootstrapChrome()

    class Store:
        def load(self):
            pytest.fail("an existing Console session must not read credentials")

    class LoginTransport:
        def start(self, selected_chrome):
            assert selected_chrome is chrome
            events.append("transport.start")

        def inspect_login_page(self, *, timeout):
            events.append(("inspect", timeout))
            return "departed"

        def navigate_console(self):
            pytest.fail("host A must never navigate to the CCI app")

        def request(self, *_args, **_kwargs):
            pytest.fail("host A login must never issue an API request")

        def close_browser(self):
            events.append("browser.close")
            return True

        def close(self):
            events.append("transport.close")

    transport = LoginTransport()

    def launch(**kwargs):
        launches.append(kwargs)
        events.append("launch")
        return chrome

    def make_transport(cdp_port, *, profile_dir):
        assert cdp_port == 0
        assert profile_dir == profile
        return transport

    monkeypatch.setattr(cli, "launch_chrome", launch)
    monkeypatch.setattr(cli, "_make_login_transport", make_transport)

    assert cli._attempt_viewer_automatic_login(
        site,
        1080,
        profile,
        str(cli._TRUSTED_AUTOMATIC_LOGIN_CHROME),
        Store(),
    ) == "authenticated"

    assert launches == [
        {
            "socks_port": 1080,
            "profile_dir": profile,
            "url": "about:blank",
            "cdp_port": 0,
            "enable_cdp": True,
            "headless": False,
            "binary": str(cli._TRUSTED_AUTOMATIC_LOGIN_CHROME),
            "block_local_dns": True,
            "extra_args": ["--disable-extensions"],
            "direct": True,
        }
    ]
    assert events == [
        "launch",
        "transport.start",
        ("inspect", 30.0),
        "browser.close",
        ("chrome.wait", 10),
        "transport.close",
    ]


def test_cmd_viewer_bootstraps_then_opens_normal_work_chrome(
    monkeypatch, tmp_path
):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    tunnel = _FakeOpenTunnel()
    final_chrome = _FakeOpenChrome()
    final_chrome.alive = False
    bootstrap_calls = []
    final_launches = []

    class Store:
        @staticmethod
        def status():
            return True

    store = Store()

    monkeypatch.setattr(cli, "_resolve_site", lambda *_args, **_kwargs: site)
    monkeypatch.setattr(cli, "_start_tunnel", lambda *_args, **_kwargs: tunnel)
    monkeypatch.setattr(
        cli,
        "_trusted_automatic_login_chrome",
        lambda _site: str(cli._TRUSTED_AUTOMATIC_LOGIN_CHROME),
    )
    monkeypatch.setattr(
        cli,
        "_automatic_credential_store",
        lambda *_args, **_kwargs: store,
    )
    monkeypatch.setattr(
        cli,
        "_attempt_viewer_automatic_login",
        lambda *args: bootstrap_calls.append(args) or "authenticated",
    )
    monkeypatch.setattr(
        cli,
        "launch_chrome",
        lambda **kwargs: final_launches.append(kwargs) or final_chrome,
    )

    assert cli.cmd_viewer(_parse("viewer", "--no-probe")) == 0

    assert bootstrap_calls == [
        (
            site,
            tunnel.port,
            site.resolved_profile_dir(),
            str(cli._TRUSTED_AUTOMATIC_LOGIN_CHROME),
            store,
        )
    ]
    assert len(final_launches) == 1
    assert final_launches[0]["profile_dir"] == site.resolved_profile_dir()
    assert final_launches[0]["url"] == DEFAULT_URL
    assert final_launches[0]["enable_cdp"] is False
    assert final_launches[0]["cdp_port"] == 0
    assert final_launches[0]["extra_args"] == []
    assert final_launches[0]["direct"] is True
    assert tunnel.stop_calls == 1


@pytest.mark.parametrize("failure_stage", ["construct", "start"])
def test_cmd_open_cleans_up_when_cci_worker_startup_fails(
    monkeypatch, failure_stage
):
    """Once Chrome exists, any watcher startup error must close owned resources."""
    site = Site("sensecore", DEFAULT_SSH_HOST, DEFAULT_URL)
    tunnel = _FakeOpenTunnel()
    chrome = _FakeOpenChrome()

    class FailingWorker:
        error = None

        def start(self):
            raise RuntimeError("CCI worker startup failed")

        def stop(self):
            pass

    def make_worker(*_args, **_kwargs):
        if failure_stage == "construct":
            raise RuntimeError("CCI worker startup failed")
        return FailingWorker()

    monkeypatch.setattr(cli, "_resolve_site", lambda *_args, **_kwargs: site)
    monkeypatch.setattr(cli, "_start_tunnel", lambda *_args, **_kwargs: tunnel)
    monkeypatch.setattr(cli, "_probe_or_warn", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(cli, "launch_chrome", lambda **_kwargs: chrome)
    monkeypatch.setattr(cli, "_CCIWatchWorker", make_worker)

    with pytest.raises(RuntimeError, match="CCI worker startup failed"):
        cli.cmd_open(_parse("open"))

    assert chrome.terminate_calls == 1
    assert chrome.wait_timeouts == [10]
    assert chrome.kill_calls == 0
    assert tunnel.stop_calls == 1


def test_cmd_open_keeps_watcher_after_work_chrome_exits(monkeypatch, capsys):
    """Chrome A is only a work window; Chrome B and the tunnel outlive it."""
    site = Site("sensecore", DEFAULT_SSH_HOST, DEFAULT_URL)
    tunnel = _FakeOpenTunnel()
    chrome = _FakeOpenChrome()
    chrome.alive = False

    class BackgroundWorker:
        def __init__(self, worker_site, _options, socks_port):
            assert worker_site is site
            assert socks_port == tunnel.port
            self.ready_event = cli.threading.Event()
            self.finished_event = cli.threading.Event()
            self.error = None
            self.start_calls = 0
            self.stop_calls = 0

        def start(self):
            self.start_calls += 1

        def stop(self):
            self.stop_calls += 1

    workers = []

    def make_worker(*args, **kwargs):
        worker = BackgroundWorker(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(cli, "_resolve_site", lambda *_args, **_kwargs: site)
    monkeypatch.setattr(cli, "_start_tunnel", lambda *_args, **_kwargs: tunnel)
    monkeypatch.setattr(cli, "_probe_or_warn", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(cli, "launch_chrome", lambda **_kwargs: chrome)
    monkeypatch.setattr(cli, "_CCIWatchWorker", make_worker)
    monkeypatch.setattr(
        cli.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert cli.cmd_open(_parse("open")) == 0

    assert "still running in the background" in capsys.readouterr().err
    assert len(workers) == 1
    assert workers[0].start_calls == 1
    assert workers[0].stop_calls == 1
    assert tunnel.stop_calls == 1


def test_cmd_open_explicit_cdp_uses_site_port(monkeypatch):
    site = Site("manual-cdp", "jump", "https://example.test/", cdp_port=9222)
    tunnel = _FakeOpenTunnel()
    chrome = SimpleNamespace(poll=lambda: 0)
    launches = []
    discoveries = []

    monkeypatch.setattr(cli, "_resolve_site", lambda *_args, **_kwargs: site)
    monkeypatch.setattr(cli, "_start_tunnel", lambda *_args, **_kwargs: tunnel)
    monkeypatch.setattr(cli, "launch_chrome", lambda **kwargs: launches.append(kwargs) or chrome)
    monkeypatch.setattr(
        cli,
        "wait_for_devtools",
        lambda port, *_args, **kwargs: discoveries.append((port, kwargs))
        or SimpleNamespace(port=9222),
    )

    assert cli.cmd_open(
        _parse("open", "--cdp", "--no-cci-watch", "--no-probe")
    ) == 0

    assert launches[0]["cdp_port"] == 9222
    assert launches[0]["enable_cdp"] is True
    assert discoveries[0][0] == 9222
    assert discoveries[0][1]["profile_dir"] is None
    assert tunnel.stop_calls == 1


def test_cmd_open_watcher_does_not_enable_cdp_on_work_chrome(monkeypatch, capsys):
    site = Site("sensecore", DEFAULT_SSH_HOST, DEFAULT_URL, cdp_port=9222)
    tunnel = _FakeOpenTunnel()
    chrome = _FakeOpenChrome()
    launches = []
    worker_args = []

    class ReadyWorker:
        error = None

        def __init__(self, *args):
            worker_args.append(args)
            self.ready_event = cli.threading.Event()
            self.finished_event = cli.threading.Event()

        def start(self):
            self.ready_event.set()

        def stop(self):
            pass

    monkeypatch.setattr(cli, "_resolve_site", lambda *_args, **_kwargs: site)
    monkeypatch.setattr(cli, "_start_tunnel", lambda *_args, **_kwargs: tunnel)
    monkeypatch.setattr(cli, "launch_chrome", lambda **kwargs: launches.append(kwargs) or chrome)
    monkeypatch.setattr(cli, "_CCIWatchWorker", ReadyWorker)
    monkeypatch.setattr(
        cli,
        "AutoRenewControlStore",
        lambda workspace: SimpleNamespace(status=lambda: False),
    )
    monkeypatch.setattr(
        cli.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        cli,
        "wait_for_devtools",
        lambda *_args, **_kwargs: pytest.fail(
            "automatic watcher must discover random CDP through its profile"
        ),
    )

    assert cli.cmd_open(_parse("open", "--no-probe")) == 0

    assert launches[0]["cdp_port"] == 0
    assert launches[0]["enable_cdp"] is False
    assert launches[0]["headless"] is False
    assert worker_args == [(site, cli._cci_options(_parse("open")), tunnel.port)]
    assert "auto-renew disabled" in capsys.readouterr().err
    assert tunnel.stop_calls == 1


def test_cmd_open_ctrl_c_cleans_worker_then_work_chrome_then_tunnel(monkeypatch):
    events = []
    site = Site("sensecore", DEFAULT_SSH_HOST, DEFAULT_URL)
    tunnel = _FakeOpenTunnel(events)
    chrome = _FakeOpenChrome(events)

    class Worker:
        error = None

        def __init__(self, *_args):
            self.finished_event = cli.threading.Event()

        def start(self):
            events.append("worker.start")

        def stop(self):
            events.append("worker.stop")

    monkeypatch.setattr(cli, "_resolve_site", lambda *_args, **_kwargs: site)
    monkeypatch.setattr(cli, "_start_tunnel", lambda *_args, **_kwargs: tunnel)
    monkeypatch.setattr(cli, "launch_chrome", lambda **_kwargs: chrome)
    monkeypatch.setattr(cli, "_CCIWatchWorker", Worker)
    monkeypatch.setattr(
        cli.time,
        "sleep",
        lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert cli.cmd_open(_parse("open", "--no-probe")) == 0

    assert events == [
        "worker.start",
        "worker.stop",
        "chrome.terminate",
        "chrome.wait",
        "tunnel.stop",
    ]


def test_cmd_open_without_watcher_exits_with_work_chrome(monkeypatch):
    site = Site("sensecore", DEFAULT_SSH_HOST, DEFAULT_URL)
    tunnel = _FakeOpenTunnel()
    chrome = _FakeOpenChrome()
    chrome.alive = False

    monkeypatch.setattr(cli, "_resolve_site", lambda *_args, **_kwargs: site)
    monkeypatch.setattr(cli, "_start_tunnel", lambda *_args, **_kwargs: tunnel)
    monkeypatch.setattr(cli, "launch_chrome", lambda **_kwargs: chrome)
    monkeypatch.setattr(
        cli.time,
        "sleep",
        lambda _seconds: pytest.fail("closed work Chrome should end without watcher"),
    )

    assert cli.cmd_open(_parse("open", "--no-cci-watch", "--no-probe")) == 0
    assert tunnel.stop_calls == 1


def test_cmd_open_reports_worker_failure_before_closed_work_chrome_exit(
    monkeypatch, capsys
):
    site = Site("sensecore", DEFAULT_SSH_HOST, DEFAULT_URL)
    tunnel = _FakeOpenTunnel()
    chrome = _FakeOpenChrome()
    chrome.alive = False

    class FailedWorker:
        def __init__(self, *_args):
            self.finished_event = cli.threading.Event()
            self.finished_event.set()
            self.error = CCIError("automation Chrome profile is locked")

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(cli, "_resolve_site", lambda *_args, **_kwargs: site)
    monkeypatch.setattr(cli, "_start_tunnel", lambda *_args, **_kwargs: tunnel)
    monkeypatch.setattr(cli, "launch_chrome", lambda **_kwargs: chrome)
    monkeypatch.setattr(cli, "_CCIWatchWorker", FailedWorker)

    assert cli.cmd_open(_parse("open", "--no-probe")) == 1

    stderr = capsys.readouterr().err
    assert "CCI auto-renew stopped: automation Chrome profile is locked" in stderr
    assert "Chrome exited" in stderr


def test_cmd_controller_uses_default_direct_connection_and_cleans_headless_worker(
    monkeypatch, tmp_path
):
    site = cli.default_site()
    tunnel = _FakeOpenTunnel()
    credentials_path = tmp_path / "controller-credentials.json"
    credential_store = object()
    store_calls = []
    worker_calls = []
    workers = []
    tunnel_calls = []
    probe_calls = []

    class FinishedWorker:
        def __init__(
            self,
            worker_site,
            worker_options,
            socks_port,
            **kwargs,
        ):
            worker_calls.append(
                (worker_site, worker_options, socks_port, kwargs)
            )
            self.finished_event = cli.threading.Event()
            self.finished_event.set()
            self.stop_event = cli.threading.Event()
            self.error = None
            self.start_calls = 0
            self.stop_calls = 0
            workers.append(self)

        def start(self):
            self.start_calls += 1

        def stop(self):
            self.stop_calls += 1

    def choose_store(command_args, selected_site, automatic_binary):
        store_calls.append(
            (command_args.credentials_file, selected_site, automatic_binary)
        )
        return credential_store

    def start_tunnel(selected_site, reuse=False):
        tunnel_calls.append((selected_site, reuse))
        return tunnel

    trusted_binary = str(cli._TRUSTED_AUTOMATIC_LOGIN_CHROME)
    monkeypatch.setattr(cli, "default_site", lambda: site)
    monkeypatch.setattr(
        cli,
        "_resolve_site",
        lambda *_args, **_kwargs: pytest.fail(
            "the fixed controller must not resolve a configurable site"
        ),
    )
    monkeypatch.setattr(
        cli,
        "_trusted_automatic_login_chrome",
        lambda _site: trusted_binary,
    )
    monkeypatch.setattr(cli, "_automatic_credential_store", choose_store)
    monkeypatch.setattr(cli, "_CCIWatchWorker", FinishedWorker)
    monkeypatch.setattr(
        cli,
        "AutoRenewControlStore",
        lambda _workspace: SimpleNamespace(status=lambda: False),
    )
    monkeypatch.setattr(
        cli,
        "_start_tunnel",
        start_tunnel,
    )
    monkeypatch.setattr(
        cli,
        "_probe_or_warn",
        lambda *call_args, **call_kwargs: probe_calls.append(
            (call_args, call_kwargs)
        )
        or True,
    )

    args = _parse(
        "controller",
        "--credentials-file",
        str(credentials_path),
    )

    assert cli.cmd_controller(args) == 0

    assert store_calls == [(credentials_path, site, trusted_binary)]
    assert len(worker_calls) == 1
    worker_site, worker_options, socks_port, kwargs = worker_calls[0]
    assert worker_site is site
    assert worker_site.ssh_host == DEFAULT_SSH_HOST == ""
    assert worker_site.mode == "direct"
    assert worker_options == cli._cci_options(args)
    assert socks_port == tunnel.port
    assert kwargs == {
        "credential_store": credential_store,
        "headless_only": True,
        "automatic_login_binary": trusted_binary,
    }
    assert tunnel_calls == [(site, False)]
    assert probe_calls == [((tunnel, site), {"strict": False})]
    assert tunnel.stop_calls == 1
    assert len(workers) == 1
    assert workers[0].start_calls == 1
    assert workers[0].stop_calls == 1


class _RecordingBrowserTransport:
    def __init__(self):
        self.events = []
        self.auth = _FakeAuth(events=self.events)
        self.auth_requires_console_navigation = False
        self.start_args = []
        self.close_calls = 0

    def start(self, chrome=None):
        self.start_args.append(chrome)

    def inspect_login_page(self, *, timeout):
        self.events.append(("inspect", timeout))
        return "departed"

    def wait_for_login_departure(self, *, timeout):
        self.events.append(("departure", timeout))
        return "departed"

    def navigate_console(self):
        self.events.append("navigate_console")

    def close(self):
        self.close_calls += 1


def _exercise_standalone_cci_transport(monkeypatch, tmp_path, argv, discovered_port):
    work_profile = tmp_path / "work"
    automation_profile = tmp_path / "work-automation"
    site = Site(
        "sensecore",
        "sensecore-proxy",
        DEFAULT_URL,
        profile_dir=work_profile,
    )
    transport = _RecordingBrowserTransport()
    calls = []

    monkeypatch.setattr(cli, "default_site", lambda: site)
    discovered_profiles = []

    def discover_profile(profile):
        discovered_profiles.append(profile)
        if callable(discovered_port):
            return discovered_port("explicit --cdp-port must skip profile discovery")
        return SimpleNamespace(port=discovered_port)

    monkeypatch.setattr(cli, "_existing_profile_cdp_endpoint", discover_profile)
    monkeypatch.setattr(cli, "_cdp_is_ready", lambda _port: True)
    monkeypatch.setattr(
        cli,
        "_start_tunnel",
        lambda *_args, **_kwargs: pytest.fail(
            "an existing managed Chrome must not launch another tunnel/browser"
        ),
    )

    def make_transport(
        cdp_port, _options, *, profile_dir=None, reuse_existing_page=False
    ):
        calls.append((cdp_port, profile_dir, reuse_existing_page))
        return transport

    monkeypatch.setattr(cli, "_make_browser_transport", make_transport)
    monkeypatch.setattr(
        cli,
        "_remote_hostname_hint",
        lambda _site: pytest.fail(
            "attaching to an existing controller must not open a second SSH session"
        ),
    )

    result = cli._run_cci_command(
        _parse(*argv), lambda _supervisor, _stop_event: 17
    )
    return result, calls, transport, discovered_profiles, automation_profile


def test_standalone_cci_auto_profile_uses_owned_random_endpoint(
    monkeypatch, tmp_path
):
    result, calls, transport, discovered, automation_profile = (
        _exercise_standalone_cci_transport(
        monkeypatch,
        tmp_path,
        ("cci", "status", "--no-probe"),
        discovered_port=45123,
        )
    )

    assert result == 17
    assert discovered == [automation_profile]
    assert calls == [(0, automation_profile, True)]
    assert transport.start_args == [None]
    assert transport.close_calls == 1


def test_standalone_cci_explicit_port_does_not_claim_profile_ownership(
    monkeypatch, tmp_path
):
    result, calls, transport, discovered, _automation_profile = (
        _exercise_standalone_cci_transport(
        monkeypatch,
        tmp_path,
        ("cci", "status", "--no-probe", "--cdp-port", "45678"),
        discovered_port=pytest.fail,
        )
    )

    assert result == 17
    assert discovered == []
    assert calls == [(45678, None, False)]
    assert transport.start_args == [None]
    assert transport.close_calls == 1


def test_standalone_cci_launches_direct_automation_profile_without_keeper(
    monkeypatch, tmp_path
):
    events = []
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    tunnel = _FakeOpenTunnel(events)
    chrome = _FakeOpenChrome(events)
    launches = []
    transport_calls = []

    class Transport(_RecordingBrowserTransport):
        def close_browser(self):
            events.append("browser.close")
            return True

        def close(self):
            events.append("transport.close")
            super().close()

    transport = Transport()

    monkeypatch.setattr(cli, "default_site", lambda: site)
    monkeypatch.setattr(cli, "_existing_profile_cdp_endpoint", lambda _profile: None)
    trusted_binary = str(cli._TRUSTED_AUTOMATIC_LOGIN_CHROME)
    monkeypatch.setattr(
        cli,
        "_trusted_automatic_login_chrome",
        lambda _site: trusted_binary,
    )
    monkeypatch.setattr(cli, "_start_tunnel", lambda *_args, **_kwargs: tunnel)
    monkeypatch.setattr(
        cli,
        "launch_chrome",
        lambda **kwargs: launches.append(kwargs) or chrome,
    )
    monkeypatch.setattr(
        cli,
        "_TunnelKeeper",
        lambda *_args, **_kwargs: pytest.fail(
            "direct CCI must not start an SSH tunnel keeper"
        ),
    )

    def make_transport(
        cdp_port, _options, *, profile_dir=None, reuse_existing_page=False
    ):
        transport_calls.append((cdp_port, profile_dir, reuse_existing_page))
        return transport

    monkeypatch.setattr(cli, "_make_browser_transport", make_transport)
    monkeypatch.setattr(cli, "_make_supervisor", lambda *_args: object())

    assert cli._run_cci_command(
        _parse("cci", "status", "--no-probe"),
        lambda _supervisor, _stop_event: 17,
    ) == 17

    assert launches == [
        {
            "socks_port": tunnel.port,
            "profile_dir": tmp_path / "work-automation",
            "url": "about:blank",
            "cdp_port": 0,
            "enable_cdp": True,
            "headless": False,
            "binary": trusted_binary,
            "block_local_dns": True,
            "extra_args": ["--disable-extensions"],
            "direct": True,
        }
    ]
    assert transport_calls == [(0, tmp_path / "work-automation", True)]
    assert transport.start_args == [chrome]
    assert chrome.terminate_calls == 0
    assert events == [
        "browser.close",
        "chrome.wait",
        "transport.close",
        "tunnel.stop",
    ]


def test_temporary_headless_cci_uses_configured_ssh_alias(
    monkeypatch, tmp_path
):
    site = Site(
        "sensecore",
        "sensecore-proxy",
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    profile = tmp_path / "work-automation"
    credentials_path = tmp_path / "controller-credentials.json"
    credential_store = object()
    chrome = _FakeOpenChrome()
    transport = _RecordingBrowserTransport()
    launches = []
    transport_calls = []
    store_calls = []
    login_calls = []
    operation_calls = []
    supervisor = object()
    trusted_binary = str(cli._TRUSTED_AUTOMATIC_LOGIN_CHROME)
    tunnel = _FakeOpenTunnel()
    tunnel_calls = []
    probe_calls = []
    keepers = []

    monkeypatch.setattr(cli, "default_site", lambda: site)
    monkeypatch.setattr(
        cli,
        "_resolve_site",
        lambda *_args, **_kwargs: pytest.fail(
            "temporary headless CCI must not resolve a configurable SSH host"
        ),
    )
    monkeypatch.setattr(cli, "_existing_profile_cdp_endpoint", lambda _profile: None)
    monkeypatch.setattr(
        cli,
        "_trusted_automatic_login_chrome",
        lambda _site: trusted_binary,
    )
    def start_tunnel(selected_site, reuse=False):
        tunnel_calls.append((selected_site, reuse))
        return tunnel

    monkeypatch.setattr(cli, "_start_tunnel", start_tunnel)
    monkeypatch.setattr(
        cli,
        "_probe_or_warn",
        lambda *call_args, **call_kwargs: probe_calls.append(
            (call_args, call_kwargs)
        )
        or True,
    )

    class Keeper:
        def __init__(self, kept_tunnel, kept_chrome, kept_site):
            assert (kept_tunnel, kept_chrome, kept_site) == (tunnel, chrome, site)
            self.stop_event = cli.threading.Event()
            self.start_calls = 0
            self.stop_calls = 0
            keepers.append(self)

        def start(self):
            self.start_calls += 1

        def stop(self):
            self.stop_calls += 1

    monkeypatch.setattr(cli, "_TunnelKeeper", Keeper)
    monkeypatch.setattr(
        cli,
        "launch_chrome",
        lambda **kwargs: launches.append(kwargs) or chrome,
    )

    def make_transport(
        cdp_port, _options, *, profile_dir=None, reuse_existing_page=False
    ):
        transport_calls.append((cdp_port, profile_dir, reuse_existing_page))
        return transport

    def choose_store(command_args, selected_site, automatic_binary):
        store_calls.append(
            (command_args.credentials_file, selected_site, automatic_binary)
        )
        return credential_store

    def automatic_login(selected_transport, store, selected_profile):
        login_calls.append((selected_transport, store, selected_profile))
        return "authenticated"

    monkeypatch.setattr(cli, "_make_browser_transport", make_transport)
    monkeypatch.setattr(cli, "_automatic_credential_store", choose_store)
    monkeypatch.setattr(cli, "_attempt_automatic_login", automatic_login)
    supervisor_calls = []

    def make_supervisor(*args, **kwargs):
        supervisor_calls.append((args, kwargs))
        return supervisor

    monkeypatch.setattr(cli, "_make_supervisor", make_supervisor)
    monkeypatch.setattr(
        cli,
        "FileCredentialStore",
        lambda *_args, **_kwargs: pytest.fail(
            "offline headless test injects its credential store"
        ),
    )

    args = _parse(
        "cci",
        "status",
        "--headless",
        "--credentials-file",
        str(credentials_path),
    )
    # Product identity fields remain fixed even if an embedder adds unrelated
    # legacy attributes to the Namespace.
    args.site = "other-site"
    args.url = "https://other.invalid/"
    result = cli._run_cci_command(
        args,
        lambda selected_supervisor, stop_event: operation_calls.append(
            (selected_supervisor, stop_event)
        )
        or 17,
    )

    assert result == 17
    assert launches == [
        {
            "socks_port": tunnel.port,
            "profile_dir": profile,
            "url": "about:blank",
            "cdp_port": 0,
            "enable_cdp": True,
            "headless": True,
            "binary": trusted_binary,
            "block_local_dns": True,
            "extra_args": ["--disable-extensions"],
            "direct": False,
        }
    ]
    assert transport_calls == [(0, profile, True)]
    assert transport.start_args == [chrome]
    assert store_calls == [(credentials_path, site, trusted_binary)]
    assert login_calls == [(transport, credential_store, profile)]
    assert len(keepers) == 1
    assert operation_calls == [(supervisor, keepers[0].stop_event)]
    assert supervisor_calls == [
        ((transport, site, cli._cci_options(args)), {})
    ]
    assert tunnel_calls == [(site, False)]
    assert site.ssh_host == "sensecore-proxy"
    assert probe_calls == [((tunnel, site), {"strict": False})]
    assert keepers[0].start_calls == 1
    assert keepers[0].stop_calls == 1
    assert tunnel.stop_calls == 1
    assert transport.close_calls == 1
    assert chrome.terminate_calls == 1


class _FastEvent:
    def __init__(self):
        self.value = False

    def is_set(self):
        return self.value

    def set(self):
        self.value = True

    def wait(self, _timeout=None):
        return self.value


class _FakeAuth:
    def __init__(self, current=None, *, wait_error=None, events=None):
        self.value = current
        self.wait_error = wait_error
        self.wait_calls = []
        self.events = events

    def current(self):
        return self.value

    def wait(self, *, after_generation, timeout):
        self.wait_calls.append((after_generation, timeout))
        if self.events is not None:
            self.events.append(("auth.wait", after_generation, timeout))
        if self.wait_error is not None:
            raise self.wait_error
        self.value = object()
        return self.value


class _WorkerTransport:
    def __init__(self, *, auth=None, graceful=False):
        self.events = []
        self.auth = auth or _FakeAuth(events=self.events)
        # Mirror the production CCI transport: login authentication comes from
        # the fixed IAM identity request and does not require a CCI page visit.
        self.auth_requires_console_navigation = False
        if isinstance(self.auth, _FakeAuth) and self.auth.events is None:
            self.auth.events = self.events
        self.graceful = graceful
        self.broken = False
        self.login_required = False
        self.start_calls = []
        self.wait_for_auth_calls = []
        self.close_browser_calls = 0
        self.close_calls = 0

    def start(self, chrome):
        self.start_calls.append(chrome)

    def wait_for_auth(self, timeout=None):
        self.wait_for_auth_calls.append(timeout)
        self.auth.value = object()
        return self.auth.value

    def inspect_login_page(self, *, timeout):
        self.events.append(("inspect", timeout))
        return "departed"

    def wait_for_login_departure(self, *, timeout):
        self.events.append(("departure", timeout))
        return "departed"

    def navigate_console(self):
        self.events.append("navigate_console")

    def close_browser(self):
        self.close_browser_calls += 1
        return self.graceful

    def close(self):
        self.close_calls += 1


def _patch_worker_launch(monkeypatch, launches):
    trusted_binary = str(cli._TRUSTED_AUTOMATIC_LOGIN_CHROME)
    monkeypatch.setattr(
        cli,
        "_trusted_automatic_login_chrome",
        lambda _site: trusted_binary,
    )

    class EmptyCredentialStore:
        @staticmethod
        def status():
            return False

        @staticmethod
        def load():
            return None

    monkeypatch.setattr(cli, "FileCredentialStore", EmptyCredentialStore)

    def launch(**kwargs):
        chrome = _FakeOpenChrome()
        launches.append((kwargs, chrome))
        return chrome

    monkeypatch.setattr(cli, "launch_chrome", launch)


def test_automation_login_marker_must_be_private_regular_file(tmp_path):
    marker = cli._automation_login_marker(tmp_path)
    marker.touch(mode=0o644)
    marker.chmod(0o644)

    assert cli._automation_login_was_completed(tmp_path) is False

    marker.chmod(0o600)
    assert cli._automation_login_was_completed(tmp_path) is True


def test_cci_worker_bootstraps_visible_then_restarts_headless(
    monkeypatch, tmp_path
):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    profile = tmp_path / "work-automation"
    launches = []
    transports = [
        _WorkerTransport(graceful=True),
        _WorkerTransport(
            auth=_FakeAuth(SimpleNamespace(generation=1)),
            graceful=True,
        ),
    ]
    transport_calls = []
    watched = []
    _patch_worker_launch(monkeypatch, launches)

    def make_transport(
        cdp_port, _options, *, profile_dir=None, reuse_existing_page=False
    ):
        transport_calls.append((cdp_port, profile_dir, reuse_existing_page))
        return transports[len(transport_calls) - 1]

    def make_supervisor(transport, _site, _options):
        assert transport is transports[1]

        class Supervisor:
            def watch(self, *, stop_event):
                watched.append((transport, stop_event))

        return Supervisor()

    monkeypatch.setattr(cli, "_make_browser_transport", make_transport)
    monkeypatch.setattr(cli, "_make_supervisor", make_supervisor)

    worker = cli._CCIWatchWorker(
        site, cli._cci_options(_parse("open")), socks_port=1080
    )
    worker._run()

    assert [call[0]["headless"] for call in launches] == [False, True]
    assert all(call[0]["profile_dir"] == profile for call in launches)
    assert all(call[0]["socks_port"] == 1080 for call in launches)
    assert all(call[0]["cdp_port"] == 0 for call in launches)
    assert all(call[0]["enable_cdp"] is True for call in launches)
    assert all(call[0]["url"] == "about:blank" for call in launches)
    assert all(call[0]["direct"] is True for call in launches)
    assert all(
        call[0]["binary"] == str(cli._TRUSTED_AUTOMATIC_LOGIN_CHROME)
        for call in launches
    )
    assert transport_calls == [(0, profile, True), (0, profile, True)]
    assert transports[0].wait_for_auth_calls == []
    assert transports[0].auth.wait_calls == [(0, 90.0)]
    assert transports[0].events == [
        ("inspect", 30.0),
        ("auth.wait", 0, 90.0),
    ]
    assert transports[1].events == [("inspect", 30.0)]
    assert [item.start_calls for item in transports] == [
        [launches[0][1]],
        [launches[1][1]],
    ]
    assert [item.close_calls for item in transports] == [1, 1]
    assert [item.close_browser_calls for item in transports] == [1, 1]
    assert watched == [(transports[1], worker.stop_event)]
    marker = cli._automation_login_marker(profile)
    assert marker.read_bytes() == b""
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert [chrome.terminate_calls for _kwargs, chrome in launches] == [0, 0]
    assert [chrome.wait_timeouts for _kwargs, chrome in launches] == [[10], [10]]
    assert worker.ready_event.is_set()
    assert worker.finished_event.is_set()
    assert worker.error is None


def test_configured_credentials_file_starts_first_controller_headless(tmp_path):
    status_calls = []

    class Store:
        def status(self):
            status_calls.append("status")
            return True

        def load(self):
            pytest.fail("headless selection must not read the credentials secret")

    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    worker = cli._CCIWatchWorker(
        site,
        cli._cci_options(_parse("open")),
        socks_port=1080,
        credential_store=Store(),
    )
    launches = []
    worker._run_browser = lambda *, headless: launches.append(headless) or "finished"

    worker._run()

    assert status_calls == ["status"]
    assert launches == [True]
    assert worker.error is None


def test_headless_only_worker_records_unexpected_supervisor_return(tmp_path):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    worker = cli._CCIWatchWorker(
        site,
        cli._cci_options(_parse("controller")),
        socks_port=1080,
        credential_store=SimpleNamespace(status=lambda: True),
        headless_only=True,
    )
    worker._run_browser = lambda *, headless: "finished"

    worker._run()

    assert worker.finished_event.is_set()
    assert isinstance(worker.error, CCIError)
    assert str(worker.error) == "headless CCI supervisor stopped unexpectedly"


def test_cci_worker_headless_requires_current_auth_before_supervisor(
    monkeypatch, tmp_path
):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    launches = []
    class LoginPageTransport(_WorkerTransport):
        def inspect_login_page(self, *, timeout):
            self.events.append(("inspect", timeout))
            return "password_form"

    unauthenticated = LoginPageTransport(graceful=True)
    authenticated = _WorkerTransport(
        auth=_FakeAuth(object()),
        graceful=True,
    )
    transports = iter([unauthenticated, authenticated])
    watched = []
    _patch_worker_launch(monkeypatch, launches)

    monkeypatch.setattr(
        cli,
        "_make_browser_transport",
        lambda *_args, **_kwargs: next(transports),
    )

    def make_supervisor(transport, _site, _options):
        assert transport is authenticated

        class Supervisor:
            def watch(self, *, stop_event):
                watched.append((transport, stop_event))

        return Supervisor()

    monkeypatch.setattr(cli, "_make_supervisor", make_supervisor)
    worker = cli._CCIWatchWorker(
        site, cli._cci_options(_parse("open")), socks_port=1080
    )

    assert worker._run_browser(headless=True) == "login"
    assert watched == []
    assert worker._run_browser(headless=True) == "finished"

    assert watched == [(authenticated, worker.stop_event)]
    assert [call[0]["headless"] for call in launches] == [True, True]
    assert unauthenticated.auth.current() is None
    assert authenticated.auth.current() is not None
    assert unauthenticated.events == [("inspect", 30.0)]


def test_headless_only_worker_auth_failure_never_switches_visible_and_records_error(
    monkeypatch, tmp_path
):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    launches = []
    store_load_calls = []

    class Store:
        def load(self):
            store_load_calls.append("load")
            pytest.fail("challenge page must be rejected before reading credentials")

    class ChallengeTransport(_WorkerTransport):
        def inspect_login_page(self, *, timeout):
            assert timeout == 30.0
            return "challenge"

        def submit_login(self, *_args, **_kwargs):
            pytest.fail("headless controller must not submit on a challenge page")

    transport = ChallengeTransport(graceful=True)
    _patch_worker_launch(monkeypatch, launches)
    monkeypatch.setattr(
        cli,
        "_make_browser_transport",
        lambda *_args, **_kwargs: transport,
    )
    monkeypatch.setattr(
        cli,
        "_make_supervisor",
        lambda *_args, **_kwargs: pytest.fail(
            "unauthenticated headless controller must not enter supervisor"
        ),
    )

    worker = cli._CCIWatchWorker(
        site,
        cli._cci_options(_parse("controller")),
        socks_port=1080,
        credential_store=Store(),
        headless_only=True,
    )
    worker._run()

    assert [call[0]["headless"] for call in launches] == [True]
    assert False not in [call[0]["headless"] for call in launches]
    assert store_load_calls == []
    assert transport.start_calls == [launches[0][1]]
    assert transport.close_browser_calls == 1
    assert transport.close_calls == 1
    assert worker.ready_event.is_set()
    assert worker.finished_event.is_set()
    assert isinstance(worker.error, CCIError)
    assert "headless controller could not log in" in str(worker.error)


def test_headless_controller_reports_one_token_free_cci_auth_diagnostic(
    monkeypatch, tmp_path, capsys
):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    launches = []
    transport = _WorkerTransport(graceful=True)
    transport.cci_auth_diagnostic = {
        "exact_main_frame_commit": True,
        "owned_session_cci_requests": 2,
        "bearer_candidates": 0,
        "effective_2xx": 2,
    }
    _patch_worker_launch(monkeypatch, launches)
    monkeypatch.setattr(
        cli,
        "_make_browser_transport",
        lambda *_args, **_kwargs: transport,
    )
    monkeypatch.setattr(
        cli,
        "_attempt_automatic_login",
        lambda *_args, **_kwargs: "session_failed",
    )
    monkeypatch.setattr(
        cli,
        "_make_supervisor",
        lambda *_args, **_kwargs: pytest.fail(
            "failed authentication must not start the supervisor"
        ),
    )
    worker = cli._CCIWatchWorker(
        site,
        cli._cci_options(_parse("controller")),
        socks_port=1080,
        credential_store=object(),
        headless_only=True,
    )

    assert worker._run_browser(headless=True) == "login"

    rendered = capsys.readouterr().err
    assert rendered.count("CCI auth diagnostic:") == 1
    assert "exact_main_frame_commit=yes" in rendered
    assert "owned_session_cci_requests=2" in rendered
    assert "bearer_candidates=0" in rendered
    assert "effective_2xx=2" in rendered


def test_cci_worker_expired_headless_login_returns_to_visible_bootstrap(
    monkeypatch, tmp_path
):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    profile = site.resolved_automation_profile_dir()
    cli._remember_automation_login(profile)
    launches = []
    transports = [
        _WorkerTransport(auth=_FakeAuth(object())),
        _WorkerTransport(),
        _WorkerTransport(auth=_FakeAuth(object())),
    ]
    made = []
    watched = []
    warnings = []
    _patch_worker_launch(monkeypatch, launches)

    def make_transport(*_args, **_kwargs):
        transport = transports[len(made)]
        made.append(transport)
        return transport

    def make_supervisor(transport, _site, _options):
        class Supervisor:
            def watch(self, *, stop_event):
                watched.append(transport)
                if transport is transports[0]:
                    transport.auth.value = None
                    transport.login_required = True
                    raise CDPTimeout("authorization expired")

        return Supervisor()

    monkeypatch.setattr(cli, "_make_browser_transport", make_transport)
    monkeypatch.setattr(cli, "_make_supervisor", make_supervisor)
    monkeypatch.setattr(cli, "warn", warnings.append)

    worker = cli._CCIWatchWorker(
        site, cli._cci_options(_parse("open")), socks_port=1080
    )
    worker._run()

    assert [call[0]["headless"] for call in launches] == [True, False, True]
    assert watched == [transports[0], transports[2]]
    assert all(transport.close_calls == 1 for transport in transports)
    assert any("login expired" in message for message in warnings)
    assert worker.error is None


def test_cci_worker_rebuilds_a_broken_runtime_transport(monkeypatch, tmp_path):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    cli._remember_automation_login(site.resolved_automation_profile_dir())
    launches = []
    transports = [
        _WorkerTransport(auth=_FakeAuth(object())),
        _WorkerTransport(auth=_FakeAuth(object())),
    ]
    watched = []
    messages = []
    _patch_worker_launch(monkeypatch, launches)

    def make_transport(*_args, **_kwargs):
        return transports[len(watched)]

    def make_supervisor(transport, _site, _options):
        class Supervisor:
            def watch(self, *, stop_event):
                watched.append(transport)
                if len(watched) == 1:
                    transport.broken = True
                    raise CCIError("closed CDP session")

        return Supervisor()

    monkeypatch.setattr(cli, "_make_browser_transport", make_transport)
    monkeypatch.setattr(cli, "_make_supervisor", make_supervisor)
    monkeypatch.setattr(cli, "info", messages.append)

    worker = cli._CCIWatchWorker(
        site, cli._cci_options(_parse("open")), socks_port=1080
    )
    worker.stop_event = _FastEvent()
    worker._run()

    assert len(launches) == 1
    assert watched == transports
    assert [item.start_calls for item in transports] == [
        [launches[0][1]],
        [launches[0][1]],
    ]
    assert [item.close_calls for item in transports] == [1, 1]
    assert worker.ready_event.is_set()
    assert worker.finished_event.is_set()
    assert worker.transport is None
    assert worker.error is None
    assert any("rebuilding" in message for message in messages)


def test_cci_worker_restarts_automation_chrome_after_ready_crash(
    monkeypatch, tmp_path
):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    cli._remember_automation_login(site.resolved_automation_profile_dir())
    launches = []
    transports = [
        _WorkerTransport(auth=_FakeAuth(object())),
        _WorkerTransport(auth=_FakeAuth(object())),
    ]
    watched = []
    messages = []
    _patch_worker_launch(monkeypatch, launches)

    def make_transport(*_args, **_kwargs):
        return transports[len(watched)]

    def make_supervisor(transport, _site, _options):
        class Supervisor:
            def watch(self, *, stop_event):
                watched.append(transport)
                if len(watched) == 1:
                    launches[0][1].alive = False
                    raise CCIError("browser process disappeared")

        return Supervisor()

    monkeypatch.setattr(cli, "_make_browser_transport", make_transport)
    monkeypatch.setattr(cli, "_make_supervisor", make_supervisor)
    monkeypatch.setattr(cli, "info", messages.append)

    worker = cli._CCIWatchWorker(
        site, cli._cci_options(_parse("open")), socks_port=1080
    )
    worker.stop_event = _FastEvent()
    worker._run()

    assert [call[0]["headless"] for call in launches] == [True, True]
    assert watched == transports
    assert worker.error is None
    assert any("restarting it" in message for message in messages)


def test_cci_worker_treats_pre_ready_automation_exit_as_profile_error(
    monkeypatch, tmp_path
):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    launches = []
    transport = _WorkerTransport()

    def launch(**kwargs):
        chrome = _FakeOpenChrome()
        chrome.alive = False
        launches.append((kwargs, chrome))
        return chrome

    def start_then_fail(_chrome):
        raise CDPTimeout("DevToolsActivePort never appeared")

    transport.start = start_then_fail
    monkeypatch.setattr(cli, "launch_chrome", launch)
    monkeypatch.setattr(cli, "_make_browser_transport", lambda *_args, **_kwargs: transport)

    worker = cli._CCIWatchWorker(
        site, cli._cci_options(_parse("open")), socks_port=1080
    )
    worker._run()

    assert isinstance(worker.error, CCIError)
    assert "before DevTools became ready" in str(worker.error)
    assert not worker.ready_event.is_set()
    assert worker.finished_event.is_set()


def test_cci_worker_surfaces_transport_construction_errors(monkeypatch, tmp_path):
    failure = CCIError("invalid browser transport configuration")
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    launches = []
    _patch_worker_launch(monkeypatch, launches)

    def fail_transport(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(cli, "_make_browser_transport", fail_transport)
    monkeypatch.setattr(cli, "info", lambda _message: None)
    worker = cli._CCIWatchWorker(
        site, cli._cci_options(_parse("open")), socks_port=1080
    )

    worker._run()

    assert worker.error is failure
    assert not worker.ready_event.is_set()
    assert worker.finished_event.is_set()
    assert launches[0][1].terminate_calls == 1


def test_cci_worker_waits_after_killing_stuck_chrome(monkeypatch, tmp_path):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    events = []

    class StuckChrome:
        def poll(self):
            return None

        def terminate(self):
            events.append("terminate")

        def wait(self, *, timeout):
            events.append(("wait", timeout))
            if timeout == 10:
                raise cli.subprocess.TimeoutExpired("chrome", timeout)
            return 0

        def kill(self):
            events.append("kill")

    worker = cli._CCIWatchWorker(
        site, cli._cci_options(_parse("open")), socks_port=1080
    )
    worker.chrome = StuckChrome()

    worker._close_chrome()

    assert events == ["terminate", ("wait", 10), "kill", ("wait", 5)]


def test_cci_worker_stop_requests_browser_close_before_transport_close(
    monkeypatch, tmp_path
):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    events = []

    class Transport:
        def close_browser(self):
            events.append("browser.close")
            return True

        def close(self):
            events.append("transport.close")

    class Chrome:
        def poll(self):
            return None

        def wait(self, *, timeout):
            events.append(("chrome.wait", timeout))
            return 0

        def terminate(self):
            events.append("chrome.terminate")

        def kill(self):
            events.append("chrome.kill")

    worker = cli._CCIWatchWorker(
        site, cli._cci_options(_parse("open")), socks_port=1080
    )
    worker.transport = Transport()
    worker.chrome = Chrome()

    worker.stop()

    assert events == ["browser.close", ("chrome.wait", 10), "transport.close"]


# ----------------------------------------------------- credential CLI/login

_TEST_CREDENTIAL_USERNAME = "sentinel-user-for-offline-test"
_TEST_CREDENTIAL_PASSWORD = "sentinel-password-for-offline-test#$"


@pytest.mark.parametrize(
    ("action", "handler"),
    [
        ("set", cli.cmd_credentials_set),
        ("status", cli.cmd_credentials_status),
        ("delete", cli.cmd_credentials_delete),
    ],
)
def test_credentials_subcommands_parse_without_secret_parameters(action, handler):
    args = _parse("credentials", action, "--json")

    assert args.cmd == "credentials"
    assert args.credentials_action == action
    assert args.func is handler
    assert args.json is True
    assert "username" not in vars(args)
    assert "password" not in vars(args)


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_default_credentials_backend_is_a_private_file_on_every_platform(
    monkeypatch, platform
):
    created_with = []

    class Store:
        pass

    def make_store(*args):
        created_with.append(args)
        return Store()

    monkeypatch.setattr(cli.sys, "platform", platform)
    monkeypatch.setattr(cli, "FileCredentialStore", make_store)
    command_args = SimpleNamespace(credentials_file=None)

    command_store, backend = cli._credential_cli_store(command_args)
    automatic_store = cli._automatic_credential_store(
        command_args,
        cli.default_site(),
        "/fixed/trusted/chrome",
    )
    worker = cli._CCIWatchWorker(
        cli.default_site(),
        cli._cci_options(_parse("open")),
        socks_port=1080,
        automatic_login_binary="/fixed/trusted/chrome",
    )

    assert backend == "file"
    assert isinstance(command_store, Store)
    assert isinstance(automatic_store, Store)
    assert isinstance(worker.credential_store, Store)
    assert created_with == [(), (), ()]


def test_explicit_credentials_file_is_preserved_for_cli_and_automation(
    monkeypatch, tmp_path
):
    credentials_path = tmp_path / "explicit-credentials.json"
    created_with = []

    def make_store(*args):
        created_with.append(args)
        return object()

    monkeypatch.setattr(cli, "FileCredentialStore", make_store)
    command_args = SimpleNamespace(credentials_file=credentials_path)

    command_store, backend = cli._credential_cli_store(command_args)
    automatic_store = cli._automatic_credential_store(
        command_args,
        cli.default_site(),
        "/fixed/trusted/chrome",
    )

    assert backend == "file"
    assert command_store is not None
    assert automatic_store is not None
    assert created_with == [(credentials_path,), (credentials_path,)]


@pytest.mark.parametrize("option", ["--username", "--password"])
def test_credentials_parser_rejects_secret_options(option):
    with pytest.raises(SystemExit) as captured:
        _parse("credentials", "set", option, "test-placeholder")

    assert captured.value.code == 2


def test_credentials_set_uses_three_hidden_prompts_without_echo(
    monkeypatch, capsys
):
    prompts = []
    answers = iter(
        [
            _TEST_CREDENTIAL_USERNAME,
            _TEST_CREDENTIAL_PASSWORD,
            _TEST_CREDENTIAL_PASSWORD,
        ]
    )
    saved = []

    def hidden_prompt(label):
        prompts.append(label)
        return next(answers)

    class Store:
        def save(self, credentials):
            saved.append(credentials)

    monkeypatch.setattr(cli.getpass, "getpass", hidden_prompt)
    monkeypatch.setattr(cli, "FileCredentialStore", Store)

    assert cli.cmd_credentials_set(_parse("credentials", "set")) == 0

    assert prompts == [
        "SenseCore username: ",
        "SenseCore password: ",
        "Confirm password: ",
    ]
    assert saved == [
        cli.SenseCoreCredentials(
            username=_TEST_CREDENTIAL_USERNAME,
            password=_TEST_CREDENTIAL_PASSWORD,
        )
    ]
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert _TEST_CREDENTIAL_USERNAME not in rendered
    assert _TEST_CREDENTIAL_PASSWORD not in rendered


def test_credentials_set_rejects_mismatched_confirmation_before_file_store(
    monkeypatch, capsys
):
    answers = iter(
        [
            _TEST_CREDENTIAL_USERNAME,
            _TEST_CREDENTIAL_PASSWORD,
            "different-confirmation-sentinel",
        ]
    )
    monkeypatch.setattr(cli.getpass, "getpass", lambda _label: next(answers))
    monkeypatch.setattr(
        cli,
        "FileCredentialStore",
        lambda: pytest.fail("mismatched credentials must not reach the file store"),
    )

    with pytest.raises(cli.CredentialStoreError, match="does not match"):
        cli.cmd_credentials_set(_parse("credentials", "set"))

    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert _TEST_CREDENTIAL_USERNAME not in rendered
    assert _TEST_CREDENTIAL_PASSWORD not in rendered


def test_credentials_set_refuses_getpass_unsafe_tty_fallback(
    monkeypatch, capsys
):
    def unsafe_prompt(_label):
        cli.warnings.warn(
            "test-only terminal fallback",
            cli.getpass.GetPassWarning,
        )
        return _TEST_CREDENTIAL_PASSWORD

    monkeypatch.setattr(cli.getpass, "getpass", unsafe_prompt)
    monkeypatch.setattr(
        cli,
        "FileCredentialStore",
        lambda: pytest.fail("unsafe input must not reach the file store"),
    )

    with pytest.raises(cli.CredentialStoreError, match="interactive terminal"):
        cli.cmd_credentials_set(_parse("credentials", "set"))

    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert _TEST_CREDENTIAL_PASSWORD not in rendered


def test_credentials_json_results_never_include_values(monkeypatch, capsys):
    answers = iter(
        [
            _TEST_CREDENTIAL_USERNAME,
            _TEST_CREDENTIAL_PASSWORD,
            _TEST_CREDENTIAL_PASSWORD,
        ]
    )

    class Store:
        def save(self, _credentials):
            return None

        def status(self):
            return True

        def delete(self):
            return True

    monkeypatch.setattr(cli.getpass, "getpass", lambda _label: next(answers))
    monkeypatch.setattr(cli, "FileCredentialStore", Store)

    assert cli.cmd_credentials_set(_parse("credentials", "set", "--json")) == 0
    set_output = capsys.readouterr().out
    assert json.loads(set_output) == {
        "backend": "file",
        "configured": True,
    }

    assert cli.cmd_credentials_status(
        _parse("credentials", "status", "--json")
    ) == 0
    status_output = capsys.readouterr().out
    assert json.loads(status_output) == {
        "backend": "file",
        "configured": True,
    }

    assert cli.cmd_credentials_delete(
        _parse("credentials", "delete", "--json")
    ) == 0
    delete_output = capsys.readouterr().out
    assert json.loads(delete_output) == {
        "backend": "file",
        "configured": False,
        "deleted": True,
    }

    rendered = set_output + status_output + delete_output
    assert _TEST_CREDENTIAL_USERNAME not in rendered
    assert _TEST_CREDENTIAL_PASSWORD not in rendered
    assert "username" not in rendered
    assert "password" not in rendered


@pytest.mark.parametrize("action", ["set", "status", "delete"])
def test_credentials_commands_are_local_only(monkeypatch, action):
    def forbidden(*_args, **_kwargs):
        pytest.fail("credential management must not resolve a site or start I/O")

    class Store:
        def save(self, _credentials):
            return None

        def status(self):
            return False

        def delete(self):
            return False

    answers = iter(
        [
            _TEST_CREDENTIAL_USERNAME,
            _TEST_CREDENTIAL_PASSWORD,
            _TEST_CREDENTIAL_PASSWORD,
        ]
    )
    monkeypatch.setattr(cli.getpass, "getpass", lambda _label: next(answers))
    monkeypatch.setattr(cli, "FileCredentialStore", Store)
    monkeypatch.setattr(cli, "_resolve_site", forbidden)
    monkeypatch.setattr(cli, "_start_tunnel", forbidden)
    monkeypatch.setattr(cli, "launch_chrome", forbidden)

    args = _parse("credentials", action)
    assert args.func(args) == 0


@pytest.mark.parametrize(
    "diagnostic",
    [
        "console_pending",
        "landing_pending",
        "challenge_pending",
        "terminal_pending",
        "submitted_oauth",
        "submitted_iam",
        "unsafe:renderer_navigation_cancelled",
        "unsafe:renderer_request_not_trusted",
        "unsafe:oauth_redirect_not_trusted",
        "unsafe:iam_redirect_not_trusted",
        "unsafe:pending_navigation_changed",
        "unsafe:frame_commit_not_trusted",
    ],
)
def test_safe_login_diagnostic_allows_only_known_token_free_states(diagnostic):
    transport = SimpleNamespace(login_diagnostic=diagnostic)

    assert cli._safe_login_diagnostic(transport) == diagnostic


def test_safe_login_diagnostic_redacts_unknown_values():
    transport = SimpleNamespace(
        login_diagnostic="unsafe:opaque-challenge-must-not-appear"
    )

    assert cli._safe_login_diagnostic(transport) == "unavailable"


def test_safe_cci_auth_diagnostic_formats_only_fixed_bool_int_fields():
    transport = SimpleNamespace(
        cci_auth_diagnostic={
            "exact_main_frame_commit": True,
            "owned_session_cci_requests": 7,
            "bearer_candidates": 3,
            "effective_2xx": 5,
            "ignored": "Bearer must-never-be-rendered",
        }
    )

    assert cli._safe_cci_auth_diagnostic(transport) == (
        "exact_main_frame_commit=yes, owned_session_cci_requests=7, "
        "bearer_candidates=3, effective_2xx=5"
    )


@pytest.mark.parametrize(
    "diagnostic",
    [
        None,
        "Bearer must-never-be-rendered",
        {
            "exact_main_frame_commit": "yes",
            "owned_session_cci_requests": 1,
            "bearer_candidates": 1,
            "effective_2xx": 1,
        },
        {
            "exact_main_frame_commit": False,
            "owned_session_cci_requests": -1,
            "bearer_candidates": 0,
            "effective_2xx": 0,
        },
    ],
)
def test_safe_cci_auth_diagnostic_rejects_unknown_or_invalid_values(diagnostic):
    secret = "Bearer must-never-be-rendered"
    rendered = cli._safe_cci_auth_diagnostic(
        SimpleNamespace(cci_auth_diagnostic=diagnostic)
    )

    assert rendered == "unavailable"
    assert secret not in rendered


def test_safe_cci_auth_diagnostic_swallows_secret_bearing_property_error():
    secret = "Bearer property-error-must-not-escape"

    class Transport:
        @property
        def cci_auth_diagnostic(self):
            raise RuntimeError(secret)

    rendered = cli._safe_cci_auth_diagnostic(Transport())

    assert rendered == "unavailable"
    assert secret not in rendered


class _SequencedCaptureAuth:
    """Drive the post-login capture boundary without inventing page state."""

    def __init__(self, *outcomes):
        self.outcomes = iter(outcomes)
        self.value = None
        self.wait_calls = []
        self.events = None

    def current(self):
        return self.value

    def wait(self, *, after_generation, timeout):
        self.wait_calls.append((after_generation, timeout))
        if self.events is not None:
            self.events.append(("auth.wait", after_generation, timeout))
        outcome = next(self.outcomes)
        if outcome == "success":
            self.value = SimpleNamespace(generation=len(self.wait_calls))
            return self.value
        if outcome == "timeout_with_lease":
            self.value = SimpleNamespace(generation=len(self.wait_calls))
        raise CDPTimeout("test-only service authorization timeout")


class _RecordingCaptureTransport:
    """Model only the transport-gated exact-GET navigation primitives."""

    def __init__(self, auth):
        self.auth = auth
        self.events = []
        auth.events = self.events
        self.retry_calls = 0

    def navigate_console(self):
        self.events.append(("navigate", cli.DEFAULT_CONSOLE_URL, "GET"))

    def wait_for_console_commit(self, *, timeout):
        self.events.append(
            ("wait_exact_commit", cli.DEFAULT_CONSOLE_URL, timeout)
        )

    def retry_console_navigation_for_auth(self):
        self.retry_calls += 1
        self.events.append(("retry", cli.DEFAULT_CONSOLE_URL, "GET"))


def test_capture_auth_waits_for_exact_console_commit_before_auth_capture():
    auth = _SequencedCaptureAuth("success")
    transport = _RecordingCaptureTransport(auth)

    lease = cli._capture_auth_after_console_navigation(transport)

    assert lease is auth.value
    assert transport.events == [
        ("navigate", cli.DEFAULT_CONSOLE_URL, "GET"),
        ("wait_exact_commit", cli.DEFAULT_CONSOLE_URL, 60.0),
        ("auth.wait", 0, 90.0),
    ]
    assert auth.wait_calls == [(0, 90.0)]
    assert transport.retry_calls == 0


def test_capture_auth_retries_fixed_get_once_and_second_wait_succeeds():
    auth = _SequencedCaptureAuth("timeout", "success")
    transport = _RecordingCaptureTransport(auth)

    lease = cli._capture_auth_after_console_navigation(transport)

    assert lease is auth.value
    assert transport.events == [
        ("navigate", cli.DEFAULT_CONSOLE_URL, "GET"),
        ("wait_exact_commit", cli.DEFAULT_CONSOLE_URL, 60.0),
        ("auth.wait", 0, 90.0),
        ("retry", cli.DEFAULT_CONSOLE_URL, "GET"),
        ("wait_exact_commit", cli.DEFAULT_CONSOLE_URL, 60.0),
        ("auth.wait", 0, 90.0),
    ]
    assert auth.wait_calls == [(0, 90.0), (0, 90.0)]
    assert transport.retry_calls == 1


def test_capture_auth_timeout_boundary_lease_prevents_fixed_get_retry():
    auth = _SequencedCaptureAuth("timeout_with_lease")
    transport = _RecordingCaptureTransport(auth)

    lease = cli._capture_auth_after_console_navigation(transport)

    assert lease is auth.value
    assert transport.events == [
        ("navigate", cli.DEFAULT_CONSOLE_URL, "GET"),
        ("wait_exact_commit", cli.DEFAULT_CONSOLE_URL, 60.0),
        ("auth.wait", 0, 90.0),
    ]
    assert auth.wait_calls == [(0, 90.0)]
    assert transport.retry_calls == 0


def test_capture_auth_two_timeouts_stop_after_one_fixed_get_retry():
    auth = _SequencedCaptureAuth("timeout", "timeout")
    transport = _RecordingCaptureTransport(auth)

    with pytest.raises(CDPTimeout, match="service authorization timeout"):
        cli._capture_auth_after_console_navigation(transport)

    assert transport.events == [
        ("navigate", cli.DEFAULT_CONSOLE_URL, "GET"),
        ("wait_exact_commit", cli.DEFAULT_CONSOLE_URL, 60.0),
        ("auth.wait", 0, 90.0),
        ("retry", cli.DEFAULT_CONSOLE_URL, "GET"),
        ("wait_exact_commit", cli.DEFAULT_CONSOLE_URL, 60.0),
        ("auth.wait", 0, 90.0),
    ]
    assert auth.wait_calls == [(0, 90.0), (0, 90.0)]
    assert transport.retry_calls == 1


def test_capture_auth_never_accepts_pre_navigation_stale_lease():
    class Auth:
        def __init__(self):
            self.value = SimpleNamespace(generation=7)
            self.wait_calls = []

        def current(self):
            return self.value

        def wait(self, *, after_generation, timeout):
            self.wait_calls.append((after_generation, timeout))
            self.events.append(("auth.wait", after_generation, timeout))
            self.value = SimpleNamespace(generation=8)
            return self.value

    auth = Auth()
    transport = _RecordingCaptureTransport(auth)

    lease = cli._capture_auth_after_console_navigation(transport)

    assert lease.generation == 8
    assert auth.wait_calls == [(7, 90.0)]
    assert transport.events == [
        ("navigate", cli.DEFAULT_CONSOLE_URL, "GET"),
        ("wait_exact_commit", cli.DEFAULT_CONSOLE_URL, 60.0),
        ("auth.wait", 7, 90.0),
    ]


def test_automatic_login_uses_proven_login_iam_lease_without_opening_cci_page(
    tmp_path,
):
    events = []

    class Auth:
        def __init__(self):
            self.value = SimpleNamespace(generation=7)

        def current(self):
            return self.value

        def wait(self, *, after_generation, timeout):
            pytest.fail("the already captured login Bearer must be reused")

    class Store:
        def load(self):
            pytest.fail("an existing Console SSO session must not read credentials")

    class Transport:
        def __init__(self):
            self.auth = Auth()
            self.auth_requires_console_navigation = False

        def inspect_login_page(self, *, timeout):
            events.append(("inspect", timeout))
            return "departed"

        def navigate_console(self):
            pytest.fail("CCI status must call APIs directly after login")

    profile = tmp_path / "automation-profile"
    transport = Transport()

    assert cli._attempt_automatic_login(transport, Store(), profile) == (
        "authenticated"
    )
    assert events == [
        ("inspect", 30.0),
    ]
    assert cli._automation_login_was_completed(profile) is True


def test_automatic_login_inspects_page_before_loading_credentials(tmp_path):
    events = []
    submitted_values = []
    auth = _FakeAuth(events=events)

    class Store:
        def load(self):
            events.append("load")
            return cli.SenseCoreCredentials(
                username=_TEST_CREDENTIAL_USERNAME,
                password=_TEST_CREDENTIAL_PASSWORD,
            )

    class Transport:
        def __init__(self):
            self.auth = auth
            self.wait_for_auth_calls = []
            self.reload_calls = []

        def inspect_login_page(self, *, timeout):
            events.append(("inspect", timeout))
            return "password_form"

        def submit_login(self, username, password, *, timeout):
            events.append(("submit", timeout))
            submitted_values.append((username, password))
            return "submitted"

        def wait_for_login_departure(self, *, timeout):
            events.append(("departure", timeout))
            return "departed"

        def navigate_console(self):
            events.append("navigate_console")

        def wait_for_auth(self, *, timeout):
            self.wait_for_auth_calls.append(timeout)
            self.reload_calls.append("Page.reload")
            pytest.fail(
                "automatic login must wait on auth state without reload recovery"
            )

    profile = tmp_path / "automation-profile"
    transport = Transport()

    assert cli._attempt_automatic_login(transport, Store(), profile) == (
        "authenticated"
    )

    assert events == [
        ("inspect", 30.0),
        "load",
        ("submit", 10.0),
        ("departure", 45.0),
        "navigate_console",
        ("auth.wait", 0, 90.0),
    ]
    assert auth.wait_calls == [(0, 90.0)]
    assert transport.wait_for_auth_calls == []
    assert transport.reload_calls == []
    assert submitted_values == [
        (_TEST_CREDENTIAL_USERNAME, _TEST_CREDENTIAL_PASSWORD)
    ]
    assert cli._automation_login_was_completed(profile) is True


def test_automatic_login_bootstrap_redirect_is_not_authentication(tmp_path):
    """Leaving zhicheng for signin's challenge must not open CCI yet."""

    events = []
    auth = _FakeAuth(events=events)

    class Store:
        def load(self):
            events.append("load")
            return cli.SenseCoreCredentials(
                username=_TEST_CREDENTIAL_USERNAME,
                password=_TEST_CREDENTIAL_PASSWORD,
            )

    class Transport:
        def __init__(self):
            self.auth = auth

        def inspect_login_page(self, *, timeout):
            events.append(("inspect", timeout))
            # This is the historical coarse state emitted for the real
            # zhicheng -> signin login_challenge redirect.  It is explicitly
            # not evidence that the challenge has been completed.
            return "redirecting"

        def navigate_console(self):
            events.append("navigate_console")

    profile = tmp_path / "automation-profile"
    result = cli._attempt_automatic_login(Transport(), Store(), profile)

    assert result != "authenticated"
    assert events == [("inspect", 30.0)]
    assert auth.wait_calls == []
    assert not profile.exists()


def test_automatic_login_opens_console_only_after_verified_challenge_departure(
    tmp_path,
):
    """The trusted challenge form must finish before console auth capture."""

    events = []
    auth = _FakeAuth(events=events)

    class Store:
        def load(self):
            events.append("load")
            return cli.SenseCoreCredentials(
                username=_TEST_CREDENTIAL_USERNAME,
                password=_TEST_CREDENTIAL_PASSWORD,
            )

    class Transport:
        def __init__(self):
            self.auth = auth

        def inspect_login_page(self, *, timeout):
            events.append(("inspect", timeout))
            # CDP has already proven this form came from the owned bootstrap
            # redirect chain; URL validation itself is covered in test_cdp.py.
            return "password_form"

        def submit_login(self, username, password, *, timeout):
            events.append(("submit", timeout, username, password))
            return "submitted"

        def wait_for_login_departure(self, *, timeout):
            events.append(("completion", timeout))
            return "departed"

        def navigate_console(self):
            events.append("navigate_console")

    profile = tmp_path / "automation-profile"
    result = cli._attempt_automatic_login(Transport(), Store(), profile)

    assert result == "authenticated"
    assert events == [
        ("inspect", 30.0),
        "load",
        (
            "submit",
            10.0,
            _TEST_CREDENTIAL_USERNAME,
            _TEST_CREDENTIAL_PASSWORD,
        ),
        ("completion", 45.0),
        "navigate_console",
        ("auth.wait", 0, 90.0),
    ]
    assert cli._automation_login_was_completed(profile) is True


def test_post_submit_redirect_without_challenge_completion_never_opens_console(
    tmp_path,
):
    """A second arbitrary redirect is no stronger than the bootstrap one."""

    events = []
    auth = _FakeAuth(events=events)

    class Store:
        def load(self):
            events.append("load")
            return cli.SenseCoreCredentials(
                username=_TEST_CREDENTIAL_USERNAME,
                password=_TEST_CREDENTIAL_PASSWORD,
            )

    class Transport:
        def __init__(self):
            self.auth = auth

        def inspect_login_page(self, *, timeout):
            events.append(("inspect", timeout))
            return "password_form"

        def submit_login(self, _username, _password, *, timeout):
            events.append(("submit", timeout))
            return "submitted"

        def wait_for_login_departure(self, *, timeout):
            events.append(("completion", timeout))
            return "redirecting"

        def navigate_console(self):
            events.append("navigate_console")

    profile = tmp_path / "automation-profile"
    result = cli._attempt_automatic_login(Transport(), Store(), profile)

    assert result != "authenticated"
    assert events == [
        ("inspect", 30.0),
        "load",
        ("submit", 10.0),
        ("completion", 45.0),
    ]
    assert auth.wait_calls == []
    assert not profile.exists()


def test_post_submit_password_form_timeout_fails_without_resubmission(tmp_path):
    events = []

    class Store:
        def load(self):
            events.append("load")
            return cli.SenseCoreCredentials(
                username=_TEST_CREDENTIAL_USERNAME,
                password=_TEST_CREDENTIAL_PASSWORD,
            )

    class Transport:
        auth = _FakeAuth()

        def inspect_login_page(self, *, timeout):
            events.append(("inspect", timeout))
            return "password_form"

        def submit_login(self, _username, _password, *, timeout):
            events.append(("submit", timeout))
            return "submitted"

        def wait_for_login_completion(self, *, timeout):
            events.append(("completion", timeout))
            return "password_form"

        def navigate_console(self):
            pytest.fail("a timed-out password form must not open the console")

    profile = tmp_path / "automation-profile"

    assert cli._attempt_automatic_login(Transport(), Store(), profile) == "failed"
    assert events == [
        ("inspect", 30.0),
        "load",
        ("submit", 10.0),
        ("completion", 45.0),
    ]
    assert sum(event[0] == "submit" for event in events if isinstance(event, tuple)) == 1
    assert not profile.exists()


@pytest.mark.parametrize("page_state", ["challenge", "loading", "ambiguous"])
def test_automatic_login_unverified_form_never_loads_credentials(
    tmp_path, page_state
):
    class Store:
        def load(self):
            pytest.fail("unsafe page state must not read stored credentials")

    class Transport:
        auth = _FakeAuth()

        def inspect_login_page(self, *, timeout):
            assert timeout == 30.0
            return page_state

        def submit_login(self, *_args, **_kwargs):
            pytest.fail("unsafe page state must not receive credentials")

        def navigate_console(self):
            pytest.fail("login-page challenge must not open the CCI console")

    profile = tmp_path / "automation-profile"

    assert cli._attempt_automatic_login(Transport(), Store(), profile) == page_state
    assert not profile.exists()


def test_automatic_login_existing_session_opens_console_only_after_departure(tmp_path):
    events = []
    auth = _FakeAuth(events=events)

    class Store:
        def load(self):
            pytest.fail("a departed login page must not read credentials")

    class Transport:
        def __init__(self):
            self.auth = auth

        def inspect_login_page(self, *, timeout):
            events.append(("inspect", timeout))
            return "departed"

        def submit_login(self, *_args, **_kwargs):
            pytest.fail("a departed login page must not receive credentials")

        def navigate_console(self):
            events.append("navigate_console")

    profile = tmp_path / "automation-profile"

    assert cli._attempt_automatic_login(Transport(), Store(), profile) == (
        "authenticated"
    )
    assert events == [
        ("inspect", 30.0),
        "navigate_console",
        ("auth.wait", 0, 90.0),
    ]
    assert cli._automation_login_was_completed(profile) is True


def test_explicit_cdp_port_never_constructs_or_injects_credential_store(
    monkeypatch, tmp_path
):
    transport_calls = []

    class Transport:
        def __init__(self):
            self.auth = _FakeAuth(object())
            self.auth_requires_console_navigation = False
            self.start_calls = []
            self.close_calls = 0

        def start(self, chrome):
            self.start_calls.append(chrome)

        def close(self):
            self.close_calls += 1

        def inspect_login_page(self, *, timeout):
            assert timeout == 1.0
            return "departed"

        def navigate_console(self):
            pytest.fail("a current owned-session auth lease needs no navigation")

    transport = Transport()
    supervisor = object()
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )

    def make_transport(cdp_port, _options, **kwargs):
        transport_calls.append((cdp_port, kwargs))
        return transport

    monkeypatch.setattr(cli, "default_site", lambda: site)
    monkeypatch.setattr(cli, "_cdp_is_ready", lambda _port: True)
    monkeypatch.setattr(cli, "_make_browser_transport", make_transport)
    monkeypatch.setattr(
        cli, "_make_supervisor", lambda *_args, **_kwargs: supervisor
    )
    monkeypatch.setattr(
        cli,
        "FileCredentialStore",
        lambda: pytest.fail("explicit CDP attach must not access credentials"),
    )
    monkeypatch.setattr(
        cli,
        "_start_tunnel",
        lambda *_args, **_kwargs: pytest.fail("explicit CDP attach must not start SSH"),
    )
    monkeypatch.setattr(
        cli,
        "launch_chrome",
        lambda *_args, **_kwargs: pytest.fail("explicit CDP attach must not launch Chrome"),
    )

    args = _parse("cci", "status", "--cdp-port", "43123")
    seen_operation = []
    result = cli._run_cci_command(
        args,
        lambda supervisor, stop_event: seen_operation.append(
            (supervisor, stop_event)
        )
        or 17,
    )

    assert result == 17
    assert seen_operation == [(supervisor, None)]
    assert transport_calls == [
        (
            43123,
            {
                "profile_dir": None,
                "reuse_existing_page": False,
            },
        )
    ]
    assert transport.start_calls == [None]
    assert transport.close_calls == 1


def _fake_path_details(*, directory=True, uid=0, mode=0o755):
    kind = stat.S_IFDIR if directory else stat.S_IFREG
    return SimpleNamespace(st_mode=kind | mode, st_uid=uid)


def test_linux_automatic_login_skips_unsafe_alias_and_uses_safe_system_chrome(
    monkeypatch,
):
    unsafe = Path("/unsafe/bin/google-chrome")
    safe = Path("/opt/google/chrome/google-chrome")
    unsafe_target = Path("/opt/google/chrome/unsafe-target")
    resolved = {unsafe: unsafe_target, safe: safe}
    details = {
        Path("/"): _fake_path_details(),
        Path("/unsafe"): _fake_path_details(mode=0o777),
        Path("/unsafe/bin"): _fake_path_details(),
        Path("/opt"): _fake_path_details(),
        Path("/opt/google"): _fake_path_details(),
        Path("/opt/google/chrome"): _fake_path_details(),
        unsafe_target: _fake_path_details(directory=False),
        safe: _fake_path_details(directory=False),
    }
    monkeypatch.delenv("SLAIGPUS_CHROME", raising=False)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(
        cli, "_TRUSTED_LINUX_AUTOMATIC_LOGIN_CHROME", (unsafe, safe)
    )
    monkeypatch.setattr(cli, "_strict_resolve_path", resolved.__getitem__)
    monkeypatch.setattr(cli, "_path_details", details.__getitem__)
    monkeypatch.setattr(cli, "_path_is_executable", lambda _path: True)
    monkeypatch.setattr(
        cli,
        "find_chrome",
        lambda: pytest.fail("Linux credential path must not use PATH discovery"),
    )

    assert cli._trusted_automatic_login_chrome(cli.default_site()) == str(safe)


@pytest.mark.parametrize(
    ("bad_component", "details"),
    [
        (Path("/usr/bin"), _fake_path_details(uid=1000)),
        (Path("/opt/google/chrome/chrome"), _fake_path_details(directory=False, mode=0o775)),
        (Path("/opt/google/chrome/chrome"), _fake_path_details(directory=False, uid=1000)),
    ],
)
def test_linux_automatic_login_rejects_replaceable_path_components(
    monkeypatch, bad_component, details
):
    alias = Path("/usr/bin/google-chrome")
    target = Path("/opt/google/chrome/chrome")
    path_details = {
        Path("/"): _fake_path_details(),
        Path("/usr"): _fake_path_details(),
        Path("/usr/bin"): _fake_path_details(),
        Path("/opt"): _fake_path_details(),
        Path("/opt/google"): _fake_path_details(),
        Path("/opt/google/chrome"): _fake_path_details(),
        target: _fake_path_details(directory=False),
    }
    path_details[bad_component] = details
    monkeypatch.delenv("SLAIGPUS_CHROME", raising=False)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli, "_TRUSTED_LINUX_AUTOMATIC_LOGIN_CHROME", (alias,))
    monkeypatch.setattr(cli, "_strict_resolve_path", lambda _path: target)
    monkeypatch.setattr(cli, "_path_details", path_details.__getitem__)
    monkeypatch.setattr(cli, "_path_is_executable", lambda _path: True)

    assert cli._trusted_automatic_login_chrome(cli.default_site()) is None


def test_linux_automatic_login_rejects_non_executable_system_target(monkeypatch):
    alias = Path("/usr/bin/google-chrome")
    target = Path("/opt/google/chrome/chrome")
    path_details = {
        Path("/"): _fake_path_details(),
        Path("/usr"): _fake_path_details(),
        Path("/usr/bin"): _fake_path_details(),
        Path("/opt"): _fake_path_details(),
        Path("/opt/google"): _fake_path_details(),
        Path("/opt/google/chrome"): _fake_path_details(),
        target: _fake_path_details(directory=False),
    }
    monkeypatch.delenv("SLAIGPUS_CHROME", raising=False)
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli, "_TRUSTED_LINUX_AUTOMATIC_LOGIN_CHROME", (alias,))
    monkeypatch.setattr(cli, "_strict_resolve_path", lambda _path: target)
    monkeypatch.setattr(cli, "_path_details", path_details.__getitem__)
    monkeypatch.setattr(cli, "_path_is_executable", lambda _path: False)

    assert cli._trusted_automatic_login_chrome(cli.default_site()) is None


@pytest.mark.parametrize(
    "browser_case",
    [
        "chrome_args",
        "chrome_binary",
        "SLAIGPUS_CHROME",
        "path_or_nonfixed_chrome",
    ],
)
def test_custom_or_nonfixed_chrome_never_constructs_or_reads_credentials(
    monkeypatch, tmp_path, browser_case
):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    monkeypatch.delenv("SLAIGPUS_CHROME", raising=False)

    if browser_case == "chrome_args":
        site.chrome_args = ["--window-size=1200,900"]
    elif browser_case == "chrome_binary":
        site.chrome_binary = "/custom/browser-for-manual-login"
    elif browser_case == "SLAIGPUS_CHROME":
        monkeypatch.setenv("SLAIGPUS_CHROME", "/custom/browser-from-environment")
    else:
        monkeypatch.setattr(
            cli,
            "find_chrome",
            lambda: "/usr/local/bin/google-chrome",
        )

    if browser_case != "path_or_nonfixed_chrome":
        monkeypatch.setattr(
            cli,
            "find_chrome",
            lambda: pytest.fail(
                "browser customization must reject auto-login before discovery"
            ),
        )

    monkeypatch.setattr(
        cli,
        "FileCredentialStore",
        lambda: pytest.fail(
            "custom, PATH-resolved, or nonfixed Chrome must not construct credentials"
        ),
    )

    class Transport(_RecordingBrowserTransport):
        auth = _FakeAuth()

        def inspect_login_page(self, *, timeout):
            self.events.append(("inspect", timeout))
            return "departed"

    worker = cli._CCIWatchWorker(
        site, cli._cci_options(_parse("open")), socks_port=1080
    )
    assert worker.credential_store is None
    assert worker._try_automatic_login(Transport()) == "disabled"

    tunnel = _FakeOpenTunnel()
    chrome = _FakeOpenChrome()
    transport = Transport()
    monkeypatch.setattr(cli, "default_site", lambda: site)
    monkeypatch.setattr(cli, "_existing_profile_cdp_endpoint", lambda _profile: None)
    monkeypatch.setattr(cli, "_start_tunnel", lambda *_args, **_kwargs: tunnel)
    monkeypatch.setattr(cli, "launch_chrome", lambda **_kwargs: chrome)
    monkeypatch.setattr(
        cli,
        "_make_browser_transport",
        lambda *_args, **_kwargs: transport,
    )
    monkeypatch.setattr(cli, "_make_supervisor", lambda *_args: object())

    assert cli._run_cci_command(
        _parse("cci", "status", "--no-probe"),
        lambda _supervisor, _stop_event: 17,
    ) == 17
    assert transport.start_args == [chrome]
    assert transport.close_calls == 1


def test_worker_failed_console_capture_restarts_at_exact_login_page(
    monkeypatch, tmp_path, capsys
):
    events = []
    store_calls = []
    inspect_calls = []
    submit_calls = []

    class Auth(_FakeAuth):
        def wait(self, *, after_generation, timeout):
            self.wait_calls.append((after_generation, timeout))
            events.append(("auth.wait", after_generation, timeout))
            raise CDPTimeout("test-only automatic authentication timeout")

    auth = Auth()

    class Store:
        def load(self):
            store_calls.append("load")
            return cli.SenseCoreCredentials(
                username=_TEST_CREDENTIAL_USERNAME,
                password=_TEST_CREDENTIAL_PASSWORD,
            )

    class Transport:
        broken = False

        def __init__(self):
            self.auth = auth
            self.wait_for_auth_calls = []

        def inspect_login_page(self, *, timeout):
            inspect_calls.append(timeout)
            events.append(("inspect", timeout))
            return "password_form"

        def submit_login(self, username, password, *, timeout):
            submit_calls.append((username, password, timeout))
            events.append(("submit", timeout))
            return "submitted"

        def wait_for_login_departure(self, *, timeout):
            events.append(("departure", timeout))
            return "departed"

        def navigate_console(self):
            events.append("navigate_console")

        def wait_for_auth(self, timeout=None):
            self.wait_for_auth_calls.append(timeout)
            assert timeout is None
            auth.value = object()
            return auth.value

    class Chrome:
        @staticmethod
        def poll():
            return None

    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    transport = Transport()
    worker = cli._CCIWatchWorker(
        site,
        cli._cci_options(_parse("open")),
        socks_port=1080,
        credential_store=Store(),
    )

    assert worker._wait_for_visible_login(transport, Chrome()) == "login"

    assert store_calls == ["load"]
    assert inspect_calls == [30.0]
    assert submit_calls == [
        (_TEST_CREDENTIAL_USERNAME, _TEST_CREDENTIAL_PASSWORD, 10.0)
    ]
    assert auth.wait_calls == [(0, 90.0)]
    assert events == [
        ("inspect", 30.0),
        ("submit", 10.0),
        ("departure", 45.0),
        "navigate_console",
        ("auth.wait", 0, 90.0),
    ]
    assert transport.wait_for_auth_calls == []
    assert worker._auto_login_submission_blocked is True
    assert cli._automation_login_was_completed(worker.profile_dir) is False
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert _TEST_CREDENTIAL_USERNAME not in rendered
    assert _TEST_CREDENTIAL_PASSWORD not in rendered


def test_worker_manual_login_captures_iam_without_opening_cci_page(tmp_path):
    events = []
    states = iter(["password_form", "challenge", "departed"])
    auth = _FakeAuth(events=events)

    class Transport:
        broken = False

        def __init__(self):
            self.auth = auth
            self.auth_requires_console_navigation = False

        def inspect_login_page(self, *, timeout):
            events.append(("inspect", timeout, page_state := next(states)))
            return page_state

        def submit_login(self, *_args, **_kwargs):
            pytest.fail("manual forms and challenges must be left to the human")

        def navigate_console(self):
            pytest.fail("CCI manual login must call its API without opening the CCI page")

    class Chrome:
        @staticmethod
        def poll():
            return None

    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    worker = cli._CCIWatchWorker(
        site,
        cli._cci_options(_parse("open")),
        socks_port=1080,
        automatic_login_binary=None,
    )
    worker.stop_event = _FastEvent()

    assert worker._wait_for_visible_login(Transport(), Chrome()) == "authenticated"
    assert events == [
        ("inspect", 1.0, "password_form"),
        ("inspect", 1.0, "challenge"),
        ("inspect", 1.0, "departed"),
        ("auth.wait", 0, 90.0),
    ]


def test_worker_auto_login_hard_latch_survives_five_minutes_and_rebuild(
    monkeypatch, tmp_path
):
    load_calls = []
    inspect_calls = []
    submit_calls = []
    clock = [100.0]

    class Store:
        def load(self):
            load_calls.append("load")
            return cli.SenseCoreCredentials(
                username=_TEST_CREDENTIAL_USERNAME,
                password=_TEST_CREDENTIAL_PASSWORD,
            )

    class Transport:
        broken = False

        def __init__(self):
            self.auth = _FakeAuth(
                wait_error=CDPTimeout(
                    "test-only uncertain post-submit authentication"
                )
            )

        def inspect_login_page(self, *, timeout):
            inspect_calls.append((self, timeout))
            return "password_form"

        def submit_login(self, username, password, *, timeout):
            submit_calls.append((self, username, password, timeout))
            return "submitted"

        def wait_for_login_departure(self, *, timeout):
            assert timeout == 45.0
            return "departed"

        def navigate_console(self):
            return None

        def wait_for_auth(self, *_args, **_kwargs):
            pytest.fail("automatic login must never use reload-capable wait_for_auth")

    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    worker = cli._CCIWatchWorker(
        site,
        cli._cci_options(_parse("open")),
        socks_port=1080,
        credential_store=Store(),
    )
    original = Transport()
    rebuilt = Transport()

    assert worker._try_automatic_login(original) == "session_failed"
    clock[0] += 5 * 60 + 1
    assert worker._try_automatic_login(original) == "attempted"
    assert worker._try_automatic_login(rebuilt) == "attempted"

    assert load_calls == ["load"]
    assert inspect_calls == [(original, 30.0)]
    assert submit_calls == [
        (
            original,
            _TEST_CREDENTIAL_USERNAME,
            _TEST_CREDENTIAL_PASSWORD,
            10.0,
        )
    ]
    assert original.auth.wait_calls == [(0, 90.0)]
    assert rebuilt.auth.wait_calls == []


def test_standalone_uncertain_console_capture_fails_closed_without_manual_prompt(
    monkeypatch, tmp_path
):
    site = Site(
        "sensecore",
        DEFAULT_SSH_HOST,
        DEFAULT_URL,
        profile_dir=tmp_path / "work",
    )
    tunnel = _FakeOpenTunnel()
    chrome = _FakeOpenChrome()
    load_calls = []
    submit_calls = []

    class Auth(_FakeAuth):
        def wait(self, *, after_generation, timeout):
            self.wait_calls.append((after_generation, timeout))
            if timeout == 90.0:
                raise CDPTimeout("test-only uncertain automatic submission")
            assert timeout == 1.0
            self.value = object()
            return self.value

    class Store:
        def load(self):
            load_calls.append("load")
            return cli.SenseCoreCredentials(
                username=_TEST_CREDENTIAL_USERNAME,
                password=_TEST_CREDENTIAL_PASSWORD,
            )

    class Transport(_RecordingBrowserTransport):
        cci_auth_diagnostic = {
            "exact_main_frame_commit": True,
            "owned_session_cci_requests": 4,
            "bearer_candidates": 0,
            "effective_2xx": 3,
        }

        def __init__(self):
            super().__init__()
            self.auth = Auth()

        def inspect_login_page(self, *, timeout):
            assert timeout == 30.0
            return "password_form"

        def submit_login(self, username, password, *, timeout):
            submit_calls.append((username, password, timeout))
            return "submitted"

        def wait_for_auth(self, *_args, **_kwargs):
            pytest.fail("standalone login must not use reload-capable wait_for_auth")

        def close_browser(self):
            return True

    class Keeper:
        def __init__(self, *_args):
            self.stop_event = cli.threading.Event()

        def start(self):
            return None

        def stop(self):
            return None

    transport = Transport()
    operation_calls = []
    monkeypatch.setattr(cli, "default_site", lambda: site)
    monkeypatch.setattr(cli, "_existing_profile_cdp_endpoint", lambda _profile: None)
    monkeypatch.setattr(cli, "_start_tunnel", lambda *_args, **_kwargs: tunnel)
    monkeypatch.setattr(cli, "launch_chrome", lambda **_kwargs: chrome)
    monkeypatch.setattr(cli, "_TunnelKeeper", Keeper)
    monkeypatch.setattr(cli, "_make_browser_transport", lambda *_args, **_kwargs: transport)
    monkeypatch.setattr(cli, "_make_supervisor", lambda *_args: object())
    monkeypatch.setattr(cli, "FileCredentialStore", Store)
    monkeypatch.setattr(
        cli,
        "_trusted_automatic_login_chrome",
        lambda _site: str(cli._TRUSTED_AUTOMATIC_LOGIN_CHROME),
    )

    with pytest.raises(
        CCIError,
        match="retry to start again from the exact enterprise login URL",
    ) as caught:
        cli._run_cci_command(
            _parse("cci", "status", "--no-probe"),
            lambda supervisor, stop_event: operation_calls.append(
                (supervisor, stop_event)
            )
            or 17,
        )

    assert "CCI auth diagnostic: exact_main_frame_commit=yes" in str(caught.value)
    assert "owned_session_cci_requests=4" in str(caught.value)
    assert "bearer_candidates=0" in str(caught.value)
    assert "effective_2xx=3" in str(caught.value)

    assert load_calls == ["load"]
    assert submit_calls == [
        (_TEST_CREDENTIAL_USERNAME, _TEST_CREDENTIAL_PASSWORD, 10.0)
    ]
    assert transport.auth.wait_calls == [(0, 90.0)]
    assert operation_calls == []
