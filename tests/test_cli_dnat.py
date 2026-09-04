"""Offline CLI tests for guarded DNAT creation."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import slaigpus.cli as cli  # noqa: E402
from slaigpus.cdp import SENSECORE_IAM_AUTH_CAPTURE_URL  # noqa: E402
from slaigpus.credentials import SenseCoreCredentials  # noqa: E402
from slaigpus.dnat import (  # noqa: E402
    DNATError,
    EIP_CONSOLE_URL,
    IAM_API_ORIGIN,
    MANAGEMENT_API_ORIGIN,
    NETWORK_API_ORIGIN,
)


EIP_NAME = "eip-zhicheng-fab530d7"
EIP_DISPLAY_NAME = "L202500601_L202500750_01e_用途"


def _parse(*args: str):
    return cli.build_parser().parse_args(list(args))


def test_dnat_create_parser_defaults_to_dry_run_with_local_credentials():
    args = _parse(
        "dnat",
        "create",
        "--protocol",
        "tcp",
        "--eip-port",
        "2222",
        "--target-ip",
        "10.0.0.2",
        "--target-port",
        "22",
    )

    assert args.func is cli.cmd_dnat_create
    assert args.protocol == "tcp"
    assert args.eip_port == "2222"
    assert args.target_ip == "10.0.0.2"
    assert args.target_port == "22"
    assert args.cdp_port == 9222
    assert args.credentials_file is None
    assert args.apply is False
    assert args.json is False
    assert not hasattr(args, "eip")


def test_dnat_transport_has_only_the_required_api_origins(monkeypatch):
    captured = {}

    class Transport:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("slaigpus.cdp.BrowserFetchTransport", Transport)

    assert isinstance(cli._make_dnat_transport(43123), Transport)
    assert captured["cdp_port"] == 43123
    assert captured["console_url"] == EIP_CONSOLE_URL
    assert captured["api_base"] == NETWORK_API_ORIGIN
    assert captured["allowed_request_prefixes"] == (
        NETWORK_API_ORIGIN,
        MANAGEMENT_API_ORIGIN,
        IAM_API_ORIGIN,
    )
    assert captured["auth_capture_base"] == SENSECORE_IAM_AUTH_CAPTURE_URL
    assert captured["auth_capture_exact_path"] is True
    assert captured["auth_capture_methods"] == ("GET",)
    assert captured["auth_requires_console_navigation"] is False


def test_dnat_cli_dry_run_never_calls_create(monkeypatch, capsys):
    events = []

    class Transport:
        def start(self):
            events.append("start")

        def close(self):
            events.append("close")

    class Client:
        def __init__(self, transport, username):
            assert isinstance(transport, Transport)
            assert username == "L202500646"

        def plan_create(self, spec):
            events.append(("plan", spec))
            return SimpleNamespace(
                to_dict=lambda: {
                    "eip": EIP_NAME,
                    "eip_display_name": EIP_DISPLAY_NAME,
                    "eip_port": "2222",
                    "protocol": "tcp",
                    "rule_name": "dnat-zhicheng-12345678",
                    "target_ip": "10.0.0.2",
                    "target_port": "22",
                }
            )

        def create(self, _spec):
            pytest.fail("dry-run must not call create")

    monkeypatch.setattr(cli, "_cdp_is_ready", lambda port: port == 9222)
    monkeypatch.setattr(
        cli, "_dnat_configured_username", lambda _path: "L202500646"
    )
    monkeypatch.setattr(cli, "_make_dnat_transport", lambda _port: Transport())
    monkeypatch.setattr(cli, "DNATClient", Client)

    args = _parse(
        "dnat",
        "create",
        "--protocol",
        "tcp",
        "--eip-port",
        "2222",
        "--target-ip",
        "10.0.0.2",
        "--target-port",
        "22",
        "--json",
    )
    assert cli.cmd_dnat_create(args) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["applied"] is False
    assert events[0] == "start"
    assert events[-1] == "close"
    assert [item[0] for item in events if isinstance(item, tuple)] == ["plan"]


def test_dnat_cli_apply_uses_the_single_create_boundary(monkeypatch, capsys):
    events = []

    class Transport:
        def start(self):
            events.append("start")

        def close(self):
            events.append("close")

    class Client:
        def __init__(self, _transport, username):
            assert username == "L202500646"

        def plan_create(self, _spec):
            pytest.fail("apply delegates its preflight to create")

        def create(self, spec):
            events.append(("create", spec))
            return SimpleNamespace(
                to_dict=lambda: {
                    "applied": True,
                    "eip": EIP_NAME,
                    "eip_display_name": EIP_DISPLAY_NAME,
                    "eip_port": "2222",
                    "protocol": "tcp",
                    "rule_name": "dnat-zhicheng-12345678",
                    "target_ip": "10.0.0.2",
                    "target_port": "22",
                }
            )

    monkeypatch.setattr(cli, "_cdp_is_ready", lambda _port: True)
    monkeypatch.setattr(
        cli, "_dnat_configured_username", lambda _path: "L202500646"
    )
    monkeypatch.setattr(cli, "_make_dnat_transport", lambda _port: Transport())
    monkeypatch.setattr(cli, "DNATClient", Client)

    args = _parse(
        "dnat",
        "create",
        "--protocol",
        "tcp",
        "--eip-port",
        "2222",
        "--target-ip",
        "10.0.0.2",
        "--target-port",
        "22",
        "--apply",
        "--json",
    )
    assert cli.cmd_dnat_create(args) == 0

    assert json.loads(capsys.readouterr().out)["applied"] is True
    assert [item[0] for item in events if isinstance(item, tuple)] == ["create"]
    assert events[-1] == "close"


def test_dnat_username_comes_from_the_selected_local_credential_store(monkeypatch):
    selected_paths = []

    class Store:
        def __init__(self, path=None):
            selected_paths.append(path)

        def load(self):
            return SenseCoreCredentials(" L202500646 ", "not-observed")

    monkeypatch.setattr(cli, "FileCredentialStore", Store)
    path = Path("/private/tmp/test-dnat-credentials.json")

    assert cli._dnat_configured_username(path) == "L202500646"
    assert selected_paths == [path]


def test_dnat_requires_configured_local_credentials(monkeypatch):
    class Store:
        def __init__(self):
            pass

        def load(self):
            return None

    monkeypatch.setattr(cli, "FileCredentialStore", Store)

    with pytest.raises(DNATError, match="credentials are not configured"):
        cli._dnat_configured_username(None)


def test_dnat_cli_requires_an_existing_cdp_viewer(capsys):
    result = cli.main(
        [
            "dnat",
            "create",
            "--protocol",
            "tcp",
            "--eip-port",
            "2222",
            "--target-ip",
            "10.0.0.2",
            "--target-port",
            "22",
            "--cdp-port",
            "43123",
        ]
    )

    assert result == 1
    assert "slaigpus viewer --cdp" in capsys.readouterr().err
