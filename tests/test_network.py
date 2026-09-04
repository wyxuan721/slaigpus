"""Offline contracts for direct access and optional SSH destinations."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import slaigpus.network as network_module  # noqa: E402
import slaigpus.automation as automation_module  # noqa: E402
from slaigpus.config import (  # noqa: E402
    ConfigError,
    DEFAULT_URL,
    load_config,
    validate_ssh_alias,
)
from slaigpus.network import NetworkConnection  # noqa: E402


def test_no_configured_sensecore_proxy_means_direct(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[sensecore.network]\nmode = \"direct\"\n", encoding="utf-8")

    config = load_config(path)

    assert config.sensecore is not None
    assert config.sensecore.mode == "direct"
    assert config.sensecore.ssh_host == ""
    assert config.sensecore.url == DEFAULT_URL


def test_sensecore_proxy_needs_only_an_openssh_host_alias(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[sensecore.network]\nssh_host = \"sensecore-proxy\"\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.sensecore is not None
    assert config.sensecore.mode == "ssh"
    assert config.sensecore.ssh_host == "sensecore-proxy"


def test_sensecore_proxy_accepts_user_at_host_destination(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        "[sensecore.network]\nssh_host = \"wyx@100.116.172.2\"\n",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.sensecore is not None
    assert config.sensecore.mode == "ssh"
    assert config.sensecore.ssh_host == "wyx@100.116.172.2"


@pytest.mark.parametrize(
    "document",
    [
        '[sensecore.network]\nmode = "ssh"\n',
        '[sensecore.network]\nmode = "direct"\nssh_host = "jump"\n',
        '[sensecore.network]\nmode = "automatic"\n',
    ],
)
def test_sensecore_network_rejects_inconsistent_modes(tmp_path, document):
    path = tmp_path / "config.toml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize(
    "extra",
    [
        'identity_file = "~/.ssh/id_ed25519"',
        'hostname = "gateway.example"',
        "port = 22",
        'ssh_args = ["-J", "bastion"]',
    ],
)
def test_sensecore_network_rejects_ssh_implementation_details(tmp_path, extra):
    path = tmp_path / "config.toml"
    path.write_text(
        "[sensecore.network]\n"
        'mode = "ssh"\n'
        'ssh_host = "gateway"\n'
        f"{extra}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"unknown \[sensecore.network\]"):
        load_config(path)


@pytest.mark.parametrize(
    "alias",
    [
        "",
        " gateway",
        "gateway ",
        "-oProxyCommand=bad",
        "two words",
        "../jump",
        "a/b",
        "user@@gateway",
        "user@",
        "@gateway",
        "gateway:22",
    ],
)
def test_ssh_alias_rejects_values_that_are_not_single_safe_destinations(alias):
    with pytest.raises(ConfigError, match="SSH destination"):
        validate_ssh_alias(alias)


def test_direct_connection_has_no_ssh_process_and_clears_proxy_environment():
    connection = NetworkConnection(mode="direct").start()

    assert connection.is_running is True
    assert connection.uses_ssh is False
    assert connection.port == 0
    assert connection.env["SLAIGPUS_CONNECTION"] == "direct"
    assert connection.env["ALL_PROXY"] == ""
    assert connection.env["NO_PROXY"] == "*"

    connection.stop()
    assert connection.is_running is False


def test_direct_probe_uses_a_local_tcp_connection(monkeypatch):
    calls = []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        network_module.socket,
        "create_connection",
        lambda address, timeout: calls.append((address, timeout)) or Socket(),
    )

    connection = NetworkConnection(mode="direct").start()
    connection.probe("console.sensecore.cn", 443, timeout=3.0)

    assert calls == [(('console.sensecore.cn', 443), 3.0)]


def test_ssh_connection_passes_only_the_alias_to_the_tunnel(monkeypatch):
    calls = []

    class Tunnel:
        port = 1080
        is_running = True
        log_tail = ""
        socks_url = "socks5://127.0.0.1:1080"
        socks_url_remote_dns = "socks5h://127.0.0.1:1080"
        env = {"ALL_PROXY": socks_url_remote_dns}

        def __init__(self, alias, **kwargs):
            calls.append((alias, kwargs))

        def start(self):
            calls.append("start")

        def stop(self):
            calls.append("stop")

        def probe(self, host, port, timeout=10.0):
            calls.append((host, port, timeout))

    monkeypatch.setattr(network_module, "SSHTunnel", Tunnel)

    connection = NetworkConnection(mode="ssh", ssh_host="sensecore-proxy").start()
    connection.probe("console.sensecore.cn", 443)
    connection.stop()

    assert calls == [
        (
            "sensecore-proxy",
            {"port": 0, "ssh_args": (), "reuse_existing": False},
        ),
        "start",
        ("console.sensecore.cn", 443, 10.0),
        "stop",
    ]


def test_playwright_context_omits_proxy_in_direct_mode(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(automation_module, "_require_playwright", lambda: None)
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(
            launch_persistent_context=lambda **kwargs: calls.append(kwargs) or object()
        )
    )

    automation_module.browser_context(
        playwright,
        NetworkConnection(mode="direct").start(),
        tmp_path / "profile",
        headless=True,
    )

    assert "proxy" not in calls[0]
    assert "--no-proxy-server" in calls[0]["args"]


def test_playwright_context_uses_socks_for_ssh_mode(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(automation_module, "_require_playwright", lambda: None)
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(
            launch_persistent_context=lambda **kwargs: calls.append(kwargs) or object()
        )
    )
    connection = SimpleNamespace(socks_url="socks5://127.0.0.1:1080")

    automation_module.browser_context(
        playwright,
        connection,
        tmp_path / "profile",
        headless=True,
    )

    assert calls[0]["proxy"] == {"server": "socks5://127.0.0.1:1080"}
    assert any(arg.startswith("--host-resolver-rules=") for arg in calls[0]["args"])
