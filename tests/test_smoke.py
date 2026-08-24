"""Smoke tests that do not need a real jump host.

A fake `ssh` binary stands in for the real one: it binds the requested port
and speaks just enough SOCKS5 to exercise the probe logic, including the
failure paths.  Run with:  python -m pytest tests/ -v
"""

from __future__ import annotations

import os
import socket
import stat
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import slaigpus.browser as browser_module  # noqa: E402
from slaigpus.browser import (  # noqa: E402
    ChromeArgumentError,
    ChromeError,
    build_chrome_args,
    ensure_private_profile_dir,
    launch_chrome,
)
from slaigpus.config import (  # noqa: E402
    ConfigError,
    Site,
    config_search_paths,
    default_config_root,
    default_state_root,
    load_config,
)
from slaigpus.tunnel import SSHTunnel, TunnelError, free_port  # noqa: E402


FAKE_SSH = textwrap.dedent(
    '''
    #!/usr/bin/env python3
    """Pretends to be `ssh -N -D 127.0.0.1:PORT host`."""
    import os, socket, struct, sys, threading

    port = None
    for i, a in enumerate(sys.argv):
        if a == "-D":
            port = int(sys.argv[i + 1].rsplit(":", 1)[-1])
    if port is None:
        sys.exit(2)

    argv_log = os.environ.get("SLAIGPUS_TEST_ARGV")
    if argv_log:
        with open(argv_log, "w") as fh:
            fh.write("\\n".join(sys.argv[1:]))

    # SLAIGPUS_TEST_MODE: ok | refuse | exit
    mode = os.environ.get("SLAIGPUS_TEST_MODE", "ok")
    if mode == "exit":
        sys.stderr.write("ssh: connect to host b port 22: Connection refused\\n")
        sys.exit(255)

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)

    def handle(conn):
        try:
            conn.recv(3)                     # greeting
            conn.sendall(b"\\x05\\x00")      # no auth
            head = conn.recv(4)              # ver cmd rsv atyp
            if len(head) < 4:
                return
            n = conn.recv(1)[0]
            conn.recv(n + 2)                 # host + port
            code = 0x04 if mode == "refuse" else 0x00
            conn.sendall(bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0]))
        except OSError:
            pass
        finally:
            conn.close()

    while True:
        try:
            c, _ = srv.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(c,), daemon=True).start()
    '''
).strip()


@pytest.fixture(scope="session")
def fake_ssh(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("bin") / "fake-ssh"
    path.write_text(FAKE_SSH)
    path.chmod(0o755)
    return str(path)


def test_tunnel_starts_and_stops(fake_ssh):
    with SSHTunnel("b", ssh_binary=fake_ssh) as tunnel:
        assert tunnel.is_running
        assert tunnel.port > 0
        assert tunnel.socks_url == f"socks5://127.0.0.1:{tunnel.port}"
        with socket.socket() as s:
            s.settimeout(2)
            assert s.connect_ex(("127.0.0.1", tunnel.port)) == 0
    assert not tunnel.is_running


def test_probe_success(fake_ssh):
    with SSHTunnel("b", ssh_binary=fake_ssh) as tunnel:
        tunnel.probe("wiki.internal.example.com", 443)  # must not raise


def test_probe_reports_unreachable_target(fake_ssh, monkeypatch):
    monkeypatch.setenv("SLAIGPUS_TEST_MODE", "refuse")
    with SSHTunnel("b", ssh_binary=fake_ssh) as tunnel:
        with pytest.raises(TunnelError, match="host unreachable"):
            tunnel.probe("nope.internal", 443)


def test_ssh_failure_surfaces_stderr(fake_ssh, monkeypatch):
    monkeypatch.setenv("SLAIGPUS_TEST_MODE", "exit")
    with pytest.raises(TunnelError, match="Connection refused"):
        SSHTunnel("b", ssh_binary=fake_ssh, connect_timeout=5).start()


def test_port_conflict_is_detected(fake_ssh):
    port = free_port()
    holder = socket.socket()
    holder.bind(("127.0.0.1", port))
    # start() probes the listener once for the conflict and again when
    # reuse_existing=True.  Leave enough backlog for both connections on
    # platforms that keep closed-but-unaccepted sockets queued briefly.
    holder.listen(8)
    try:
        with pytest.raises(TunnelError, match="already in use"):
            SSHTunnel("b", port=port, ssh_binary=fake_ssh).start()
        # …unless we explicitly ask to reuse it
        tunnel = SSHTunnel("b", port=port, ssh_binary=fake_ssh, reuse_existing=True)
        tunnel.start()
        assert tunnel.port == port
        tunnel.stop()
    finally:
        holder.close()


def test_env_uses_remote_dns(fake_ssh):
    with SSHTunnel("b", ssh_binary=fake_ssh) as tunnel:
        env = tunnel.env
        # socks5h, not socks5: DNS must happen on the jump host.
        assert env["ALL_PROXY"].startswith("socks5h://")
        assert env["SLAIGPUS_SOCKS_PORT"] == str(tunnel.port)
        assert "127.0.0.1" in env["NO_PROXY"]


def test_tunnel_disables_ssh_connection_sharing(fake_ssh, monkeypatch, tmp_path):
    argv_log = tmp_path / "ssh-argv.txt"
    monkeypatch.setenv("SLAIGPUS_TEST_ARGV", str(argv_log))

    with SSHTunnel("b", ssh_binary=fake_ssh):
        args = argv_log.read_text().splitlines()

    assert "ControlMaster=no" in args
    assert "ControlPath=none" in args


def test_missing_ssh_binary():
    with pytest.raises(TunnelError, match="not found"):
        SSHTunnel("b", ssh_binary="definitely-not-a-real-binary-xyz").start()


def test_chrome_args_are_sane(tmp_path):
    args = build_chrome_args(
        socks_port=1080,
        profile_dir=tmp_path,
        url="https://wiki.internal/",
        cdp_port=9222,
    )
    assert "--proxy-server=socks5://127.0.0.1:1080" in args
    assert f"--user-data-dir={tmp_path}" in args
    assert any(a.startswith("--host-resolver-rules=") for a in args)
    assert "--remote-debugging-port=9222" in args
    assert args[-1] == "https://wiki.internal/"  # URL must come last


def test_chrome_args_without_dns_blocking(tmp_path):
    args = build_chrome_args(socks_port=1, profile_dir=tmp_path, block_local_dns=False)
    assert not any(a.startswith("--host-resolver-rules=") for a in args)


def test_direct_chrome_args_omit_proxy_and_remote_dns(tmp_path):
    args = build_chrome_args(
        socks_port=1080,
        profile_dir=tmp_path,
        direct=True,
        block_local_dns=True,
    )

    assert not any(a.startswith("--proxy-server=") for a in args)
    assert not any(a.startswith("--host-resolver-rules=") for a in args)
    assert "--no-proxy-server" in args
    assert f"--user-data-dir={tmp_path}" in args


@pytest.mark.parametrize(
    "extra",
    [
        "--proxy-server=http://127.0.0.1:8888",
        "--proxy-bypass-list=*",
        "--no-proxy-server",
        "--proxy-pac-url=https://example.invalid/proxy.pac",
        "--proxy-auto-detect",
        "--host-resolver-rules=MAP * 127.0.0.1",
    ],
)
def test_direct_chrome_still_rejects_network_policy_overrides(tmp_path, extra):
    with pytest.raises(ChromeArgumentError, match="managed browser settings"):
        build_chrome_args(
            socks_port=0,
            profile_dir=tmp_path,
            direct=True,
            extra_args=[extra],
        )


def test_headless_chrome_args_are_controlled_and_have_no_new_window(tmp_path):
    args = build_chrome_args(
        socks_port=1080,
        profile_dir=tmp_path,
        cdp_port=0,
        enable_cdp=True,
        headless=True,
    )

    assert "--headless=new" in args
    assert "--new-window" not in args
    assert "--remote-debugging-port=0" in args

    for extra in ("--headless", "--headless=old", "--new-window"):
        with pytest.raises(ChromeArgumentError, match="managed browser settings"):
            build_chrome_args(
                socks_port=1080,
                profile_dir=tmp_path,
                headless=True,
                extra_args=[extra],
            )


@pytest.mark.parametrize("headless", [False, True])
@pytest.mark.parametrize("extra", ["--incognito", "--incognito=1", "--guest"])
def test_managed_profile_rejects_ephemeral_login_modes(
    tmp_path, headless, extra
):
    with pytest.raises(ChromeArgumentError, match="managed browser settings"):
        build_chrome_args(
            socks_port=1080,
            profile_dir=tmp_path,
            headless=headless,
            extra_args=[extra],
        )


def test_launch_chrome_forwards_controlled_headless_mode(monkeypatch, tmp_path):
    observed = {}
    process = object()

    monkeypatch.setattr(browser_module, "find_chrome", lambda _binary="": "/chrome")

    def fake_popen(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return process

    monkeypatch.setattr(browser_module.subprocess, "Popen", fake_popen)

    assert launch_chrome(
        socks_port=1080,
        profile_dir=tmp_path / "automation",
        enable_cdp=True,
        headless=True,
    ) is process
    assert "--headless=new" in observed["argv"]
    assert "--new-window" not in observed["argv"]


def test_launch_chrome_forwards_direct_mode(monkeypatch, tmp_path):
    observed = {}
    process = object()

    monkeypatch.setattr(browser_module, "find_chrome", lambda _binary="": "/chrome")

    def fake_popen(argv, **kwargs):
        observed["argv"] = argv
        return process

    monkeypatch.setattr(browser_module.subprocess, "Popen", fake_popen)

    assert launch_chrome(
        socks_port=0,
        profile_dir=tmp_path / "direct",
        direct=True,
    ) is process
    assert not any(arg.startswith("--proxy-server=") for arg in observed["argv"])
    assert not any(
        arg.startswith("--host-resolver-rules=") for arg in observed["argv"]
    )
    assert "--no-proxy-server" in observed["argv"]


def test_managed_profile_directory_is_private(tmp_path):
    profile = tmp_path / "automation"

    assert ensure_private_profile_dir(profile) == profile
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700

    profile.chmod(0o755)
    ensure_private_profile_dir(profile)
    assert stat.S_IMODE(profile.stat().st_mode) == 0o700


def test_managed_profile_rejects_symlinks_and_files(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "profile-link"
    link.symlink_to(target, target_is_directory=True)
    regular = tmp_path / "profile-file"
    regular.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ChromeError, match="real directory"):
        ensure_private_profile_dir(link)
    with pytest.raises(ChromeError, match="real directory"):
        ensure_private_profile_dir(regular)


def test_site_probe_target_defaults_ports():
    assert Site("x", url="https://a.internal/").probe_target == ("a.internal", 443)
    assert Site("x", url="http://a.internal/").probe_target == ("a.internal", 80)
    assert Site("x", url="http://a.internal:8080/x").probe_target == ("a.internal", 8080)
    assert Site("x", url="a.internal").probe_target == ("a.internal", 443)
    assert Site("x").probe_target is None


def test_profile_dirs_are_per_site():
    a = Site("intranet").resolved_profile_dir()
    b = Site("dashboard").resolved_profile_dir()
    assert a != b
    assert a.name == "intranet"


def test_automation_profile_is_a_stable_distinct_sibling(tmp_path):
    site = Site("sensecore", profile_dir=tmp_path / "working")

    working = site.resolved_profile_dir()
    automation = site.resolved_automation_profile_dir()

    assert automation == tmp_path / "working-automation"
    assert automation.parent == working.parent
    assert automation != working
    assert site.resolved_automation_profile_dir() == automation


def test_macos_default_paths_keep_the_existing_layout():
    home = Path("/Users/tester")
    cwd = Path("/work/slaigpus")

    assert default_state_root(platform="darwin", environ={}, home=home) == (
        home / "Library" / "Application Support" / "slaigpus"
    )
    assert default_config_root(platform="darwin", environ={}, home=home) == (
        home / ".config" / "slaigpus"
    )
    assert config_search_paths(
        platform="darwin", environ={}, home=home, cwd=cwd
    ) == [
        home / ".config" / "slaigpus" / "config.toml",
        home / "Library" / "Application Support" / "slaigpus" / "config.toml",
        cwd / "slaigpus.toml",
    ]


def test_non_macos_default_paths_follow_xdg():
    home = Path("/home/tester")
    environ = {
        "XDG_STATE_HOME": "/srv/user-state",
        "XDG_CONFIG_HOME": "/srv/user-config",
    }

    assert default_state_root(platform="linux", environ=environ, home=home) == (
        Path("/srv/user-state") / "slaigpus"
    )
    assert default_config_root(platform="linux", environ=environ, home=home) == (
        Path("/srv/user-config") / "slaigpus"
    )
    assert config_search_paths(
        platform="linux", environ=environ, home=home, cwd=Path("/work")
    ) == [
        Path("/srv/user-config/slaigpus/config.toml"),
        Path("/work/slaigpus.toml"),
    ]


def test_relative_xdg_paths_are_ignored():
    home = Path("/home/tester")
    environ = {
        "XDG_STATE_HOME": "~/relative/state",
        "XDG_CONFIG_HOME": "relative/config",
    }

    assert default_state_root(platform="linux", environ=environ, home=home) == (
        home / ".local" / "state" / "slaigpus"
    )
    assert default_config_root(platform="linux", environ=environ, home=home) == (
        home / ".config" / "slaigpus"
    )


@pytest.mark.parametrize(
    "document",
    [
        '[credentials]\nusername = "placeholder"\npassword = "placeholder"\n',
        '[sites.sensecore]\nssh_host = "jump"\nurl = "https://example.invalid"\npassword = "placeholder"\n',
    ],
)
def test_site_toml_rejects_credential_fields(tmp_path, document):
    path = tmp_path / "config.toml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigError, match="credentials are not allowed"):
        load_config(path)


def _cli(*argv):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    return subprocess.run(
        [sys.executable, "-m", "slaigpus.cli", *argv],
        capture_output=True, text=True, env=env,
    )


def test_run_separator_is_not_swallowed(fake_ssh):
    """`--` must not let `sh` be mistaken for the site name."""
    env_extra = {"SLAIGPUS_SSH": fake_ssh}
    proc = subprocess.run(
        [
            sys.executable, "-m", "slaigpus.cli", "run",
            "--ssh-host", "b", "--no-probe", "--",
            "sh", "-c", "echo $ALL_PROXY; exit 5",
        ],
        capture_output=True, text=True,
        env={
            **os.environ,
            **env_extra,
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
    )
    assert proc.returncode == 5, proc.stderr        # child's code propagates
    assert proc.stdout.startswith("socks5h://")     # env actually injected


def test_run_without_separator_is_rejected():
    proc = _cli("run", "--ssh-host", "b", "echo", "hi")
    assert proc.returncode == 2
    assert "put `--` before the command" in proc.stderr


def test_separator_outside_run_is_rejected():
    proc = _cli("probe", "--ssh-host", "b", "--", "foo")
    assert proc.returncode == 2
    assert "only meaningful" in proc.stderr


def test_cli_help_runs():
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    out = subprocess.run(
        [sys.executable, "-m", "slaigpus.cli", "--help"],
        capture_output=True, text=True, env=env,
    )
    assert out.returncode == 0
    for word in ("open", "up", "run", "probe", "list"):
        assert word in out.stdout
