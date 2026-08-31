"""Offline contracts for the guided first-use configuration assistant."""

from __future__ import annotations

import builtins
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import slaigpus.cli as cli  # noqa: E402
from slaigpus.config import Config, ConfigError, load_config, update_sensecore_config  # noqa: E402


_TEST_USERNAME = "visible-user-sentinel"
_TEST_PASSWORD = "visible-password-sentinel"


def _parse(*args: str):
    return cli.build_parser().parse_args(list(args))


def _answers(monkeypatch, values):
    prompts = []
    iterator = iter(values)

    def visible_input(prompt):
        prompts.append(prompt)
        return next(iterator)

    monkeypatch.setattr(builtins, "input", visible_input)
    return prompts


def test_configure_parser_can_be_invoked_again_with_explicit_files(tmp_path):
    config_path = tmp_path / "config.toml"
    credentials_path = tmp_path / "credentials.json"

    args = _parse(
        "--config",
        str(config_path),
        "configure",
        "--credentials-file",
        str(credentials_path),
    )

    assert args.func is cli.cmd_configure
    assert args.config == config_path
    assert args.credentials_file == credentials_path


def test_guided_student_direct_configuration_uses_visible_input(
    monkeypatch, tmp_path, capsys
):
    config_path = tmp_path / "config.toml"
    saved = []

    class Store:
        def save(self, credentials):
            saved.append(credentials)

    monkeypatch.setattr(cli, "FileCredentialStore", Store)
    prompts = _answers(
        monkeypatch,
        [_TEST_USERNAME, _TEST_PASSWORD, "1", ""],
    )

    args = _parse("--config", str(config_path), "configure")
    assert cli.cmd_configure(args) == 0

    config = load_config(config_path)
    assert config.sensecore_account_type == "student"
    assert config.sensecore is not None
    assert config.sensecore.mode == "direct"
    assert config.sensecore.ssh_host == ""
    assert saved == [
        cli.SenseCoreCredentials(
            username=_TEST_USERNAME,
            password=_TEST_PASSWORD,
        )
    ]
    assert prompts == [
        "SenseCore 账号: ",
        "SenseCore 密码: ",
        "请选择身份（1=正式学生/标准资源，2=RA/闲时资源）: ",
        "\n是否使用 SSH 代理？[y/N]: ",
    ]
    rendered = capsys.readouterr().out
    assert "标准资源（正式学生）" in rendered
    assert "网络方式：直连" in rendered
    assert _TEST_USERNAME not in rendered
    assert _TEST_PASSWORD not in rendered
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


def test_guided_ra_ssh_configuration_reprompts_alias_and_preserves_sites(
    monkeypatch, tmp_path, capsys
):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "# keep this generic site\n"
        "[sites.public]\n"
        'mode = "direct"\n'
        'url = "https://example.com/"\n',
        encoding="utf-8",
    )

    class Store:
        def save(self, _credentials):
            return None

    monkeypatch.setattr(cli, "FileCredentialStore", Store)
    _answers(
        monkeypatch,
        [
            _TEST_USERNAME,
            _TEST_PASSWORD,
            "ra",
            "yes",
            "../bad",
            "sensecore-proxy",
        ],
    )

    assert cli.cmd_configure(
        _parse("configure", "--config", str(config_path))
    ) == 0

    config = load_config(config_path)
    assert config.sensecore_account_type == "ra"
    assert config.sensecore is not None
    assert config.sensecore.mode == "ssh"
    assert config.sensecore.ssh_host == "sensecore-proxy"
    assert config.sites["public"].url == "https://example.com/"
    assert "# keep this generic site" in config_path.read_text(encoding="utf-8")
    rendered = capsys.readouterr().out
    assert "~/.ssh/config" in rendered
    assert "别名无效" in rendered
    assert "闲时资源（RA）" in rendered


def test_reconfiguration_replaces_only_managed_sensecore_tables(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[sensecore]\n"
        'account_type = "ra"\n\n'
        "[sensecore.network]\n"
        'mode = "ssh"\n'
        'ssh_host = "old-jump"\n\n'
        "[defaults]\n"
        'site = "public"\n\n'
        "[sites.public]\n"
        'mode = "direct"\n'
        'url = "https://example.com/"\n',
        encoding="utf-8",
    )

    update_sensecore_config(
        path,
        account_type="student",
        network_mode="direct",
    )

    config = load_config(path)
    assert config.sensecore_account_type == "student"
    assert config.sensecore is not None and config.sensecore.mode == "direct"
    assert config.default_site == "public"
    assert config.sites["public"].url == "https://example.com/"
    document = path.read_text(encoding="utf-8")
    assert document.count("[sensecore]") == 1
    assert document.count("[sensecore.network]") == 1
    assert "old-jump" not in document


def test_reconfiguration_refuses_inline_layout_without_changing_file(tmp_path):
    path = tmp_path / "config.toml"
    document = (
        "sensecore = { account_type = \"ra\", "
        "network = { mode = \"direct\" } }\n"
    )
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigError, match="unsupported TOML layout"):
        update_sensecore_config(
            path,
            account_type="student",
            network_mode="direct",
        )

    assert path.read_text(encoding="utf-8") == document


@pytest.mark.parametrize(
    ("account_type", "expected"),
    [("student", "standard"), ("ra", "spot")],
)
def test_acp_default_resource_class_follows_configured_identity(
    monkeypatch, account_type, expected
):
    args = _parse(
        "acp",
        "submit",
        "--name",
        "job",
        "--image",
        "image",
        "--command",
        "run",
    )
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path=None: Config(sensecore_account_type=account_type),
    )

    cli._apply_account_defaults(args)

    assert args.resource_class == expected


def test_student_can_explicitly_select_spot_resources(monkeypatch):
    args = _parse(
        "acp",
        "submit",
        "--name",
        "job",
        "--image",
        "image",
        "--command",
        "run",
        "--resource-class",
        "spot",
    )
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path=None: Config(sensecore_account_type="student"),
    )

    cli._apply_account_defaults(args)

    assert args.resource_class == "spot"


def test_ra_cannot_explicitly_select_standard_resources(monkeypatch):
    args = _parse(
        "acp",
        "submit",
        "--name",
        "job",
        "--image",
        "image",
        "--command",
        "run",
        "--resource-class",
        "standard",
    )
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path=None: Config(sensecore_account_type="ra"),
    )

    with pytest.raises(
        ConfigError,
        match="RA accounts cannot submit standard ACP jobs",
    ):
        cli._apply_account_defaults(args)


def test_main_rejects_ra_standard_before_acp_handler(monkeypatch, capsys):
    handler_calls = []
    monkeypatch.setattr(
        cli,
        "_ensure_initial_configuration",
        lambda _args: None,
    )
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda _path=None: Config(sensecore_account_type="ra"),
    )
    monkeypatch.setattr(
        cli,
        "cmd_acp_submit",
        lambda _args: handler_calls.append(True) or 0,
    )

    result = cli.main(
        [
            "acp",
            "submit",
            "--name",
            "job",
            "--image",
            "image",
            "--command",
            "run",
            "--resource-class",
            "standard",
            "--apply",
        ]
    )

    assert result == 1
    assert handler_calls == []
    assert "RA accounts cannot submit standard ACP jobs" in capsys.readouterr().err


def test_noninteractive_first_use_never_waits_for_input(monkeypatch):
    args = _parse("cci", "status")
    monkeypatch.setattr(cli, "_initial_configuration_complete", lambda _args: False)
    monkeypatch.setattr(cli, "_interactive_configuration_available", lambda: False)
    monkeypatch.setattr(
        cli,
        "cmd_configure",
        lambda _args: pytest.fail("noninteractive command must not start the wizard"),
    )

    with pytest.raises(ConfigError, match="slaigpus configure"):
        cli._ensure_initial_configuration(args)


def test_interactive_first_use_runs_wizard_once_then_continues(monkeypatch):
    args = _parse("viewer")
    checks = iter([False, True])
    calls = []
    monkeypatch.setattr(
        cli,
        "_initial_configuration_complete",
        lambda _args: next(checks),
    )
    monkeypatch.setattr(cli, "_interactive_configuration_available", lambda: True)
    monkeypatch.setattr(cli, "cmd_configure", lambda value: calls.append(value) or 0)

    cli._ensure_initial_configuration(args)

    assert calls == [args]


@pytest.mark.parametrize(
    "argv",
    [
        ("list",),
        ("acp", "profiles"),
        ("cci", "auto-renew", "status"),
        ("viewer", "custom-site"),
        ("open", "--url", "https://example.com/"),
    ],
)
def test_local_or_generic_commands_do_not_trigger_first_use(argv):
    assert cli._command_needs_initial_configuration(_parse(*argv)) is False
