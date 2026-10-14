"""Transfer contracts and real local-backend round trips, without cloud data."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slaigpus import cli  # noqa: E402
from slaigpus.config import Config, ConfigError, TransferTarget, load_config  # noqa: E402
from slaigpus import transfer as tr  # noqa: E402


@pytest.fixture
def config():
    return Config(transfer_targets={
        "gpu": TransferTarget("ssh", host="worker", port=2222, proxy_jump="gateway"),
        "store": TransferTarget("rclone", remote="aoss"),
    })


@pytest.fixture
def local_ssh(tmp_path, monkeypatch):
    """Replace encryption transport, preserving real backend protocol/stdin."""
    fake = tmp_path / "bin" / "ssh"
    fake.parent.mkdir()
    fake.write_text(f"#!{sys.executable}\nimport os,shlex,sys\n"
                    "args=sys.argv[1:]\n"
                    "while args and args[0].startswith('-'):\n"
                    " if args[0]=='--': args=args[1:]; break\n"
                    " args=args[2:]\n"
                    "args=args[1:]\n"
                    "cmd=shlex.split(args[0]) if len(args)==1 else args\n"
                    "os.execvp(cmd[0],cmd)\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake.parent) + os.pathsep + os.environ["PATH"])


def test_transfer_config_is_separate_from_browser_route(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[transfer.targets.gpu]\nkind="ssh"\nhost="gpu-alias"\n'
                    '[transfer.targets.store]\nkind="rclone"\nremote="aoss"\n')
    loaded = load_config(path)
    assert loaded.sensecore is None
    assert loaded.transfer_targets["gpu"].host == "gpu-alias"
    assert loaded.transfer_targets["gpu"].port is None
    assert loaded.transfer_targets["store"].remote == "aoss"


@pytest.mark.parametrize("fields", [
    'kind="ssh"\nhost="worker"\npassword="secret"',
    'kind="ssh"\nhost="worker"\nport=true',
    'kind="ssh"\nhost="worker"\nport=0',
    'kind="ssh"\nhost="worker"\nport=65536',
    'kind="ssh"\nhost="-oProxyCommand=bad"',
    'kind="ssh"\nhost="worker"\nproxy_jump=123',
    'kind="rclone"\nremote="ak:sk@host"',
    'kind="rclone"\nremote="aoss"\naccess_key="secret"',
    'kind=[]',
])
def test_invalid_transfer_config_rejected_without_secret_values(tmp_path, fields):
    path = tmp_path / "config.toml"
    path.write_text('[transfer.targets.target]\n' + fields)
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert "secret" not in str(error.value)


def test_routes_quote_remote_paths_and_preserve_ssh_config_port(config, tmp_path):
    source = tmp_path / "file with spaces;$(not-executed)"
    source.write_bytes(b"data")
    plan = tr.build_plan(tr.TransferSpec(str(source), "gpu:/data/a;$(no)"), config)
    assert plan.command[-1] == "worker:/data/a;$(no)"
    assert "--secluded-args" in plan.command
    shell = shlex.split(plan.command[plan.command.index("-e") + 1])
    assert shell[-4:] == ["-p", "2222", "-J", "gateway"]
    raw = tr.build_plan(tr.TransferSpec(str(source), "alias:/data/a"), config)
    assert "-p" not in shlex.split(raw.command[raw.command.index("-e") + 1])
    assert "--ignore-existing" in plan.command
    assert "--delete" not in plan.command
    assert "--inplace" not in plan.command


def test_object_route_and_remote_execution_use_no_secret_or_delete(config, tmp_path):
    source = tmp_path / "file"
    source.write_bytes(b"data")
    plan = tr.build_plan(tr.TransferSpec(str(source), "aoss://store/bucket/file"), config)
    assert plan.backend == "rclone"
    assert plan.command[-1] == "aoss:bucket/file"
    assert plan.command[1] == "copyto"
    assert "--ignore-existing" in plan.command
    plan = tr.build_plan(tr.TransferSpec("store:bucket/a'$(bad)", "/afs/output", on="gpu"), config)
    assert plan.command[0] == "ssh"
    inner = shlex.split(plan.command[-1])
    assert inner[-2:] == ["aoss:bucket/a'$(bad)", "/afs/output"]
    assert plan.runner == config.transfer_targets["gpu"]


@pytest.mark.parametrize("src,dst,options", [
    ("gpu:/data/a", "other:/data/b", {}),
    ("gpu:relative", "/tmp/b", {}),
    ("store:bucket/a", "gpu:/data/a", {}),
    ("store:bucket/a", "/data/a", {"backend": "rsync"}),
    ("store:bucket/a", "/data/a", {"verify": "sha256"}),
    ("store:bucket/a", "/data/a", {"on": "store"}),
    ("gpu:/data/a", "/data/a", {"on": "gpu"}),
    ("aoss://ak:sk@bucket/file", "/data/a", {}),
    ("https://host/file?token=secret", "/data/a", {}),
])
def test_unsupported_or_credential_routes_fail_before_execution(config, src, dst, options):
    with pytest.raises((tr.TransferError, ConfigError)):
        tr.build_plan(tr.TransferSpec(src, dst, **options), config)


def test_plan_and_dry_run_do_not_modify_destination(config, tmp_path):
    if not shutil.which("rsync"):
        pytest.skip("rsync unavailable")
    source = tmp_path / "src"
    source.write_bytes(b"source")
    destination = tmp_path / "nonexistent" / "dst"
    plan = tr.build_plan(tr.TransferSpec(str(source), str(destination), dry_run=True), config)
    assert not destination.parent.exists()
    record = tr.run_transfer(plan, tmp_path / "receipt.json")
    assert record["state"] == "dry_run"
    assert not destination.parent.exists()


@pytest.mark.parametrize("backend", ["rsync", "rclone"])
def test_real_single_file_roundtrip_and_sha_validation(backend, tmp_path):
    if not shutil.which(backend):
        pytest.skip(f"{backend} unavailable")
    source = tmp_path / "input;$(literal).bin"
    source.write_bytes(os.urandom(10000))
    destination = tmp_path / "nested" / "output.bin"
    plan = tr.build_plan(tr.TransferSpec(str(source), str(destination), backend=backend,
                                        verify="sha256"), Config())
    record = tr.run_transfer(plan, tmp_path / "receipt.json")
    assert record["state"] == "completed"
    assert destination.read_bytes() == source.read_bytes()
    assert record["verification"]["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert tr.read_receipt(tmp_path / "receipt.json") == record
    assert (tmp_path / "receipt.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("backend", ["rsync", "rclone"])
def test_default_never_overwrites_existing_file_and_update_is_explicit(backend, tmp_path):
    if not shutil.which(backend):
        pytest.skip(f"{backend} unavailable")
    source, dst = tmp_path / "input", tmp_path / "output"
    source.write_bytes(b"new content")
    dst.write_bytes(b"old")
    plan = tr.build_plan(tr.TransferSpec(str(source), str(dst), backend=backend, verify="sha256"), Config())
    record = tr.run_transfer(plan, tmp_path / "failed.json")
    assert record["state"] == "failed"
    assert dst.read_bytes() == b"old"
    plan = tr.build_plan(tr.TransferSpec(str(source), str(dst), backend=backend,
                                        overwrite=True, verify="sha256"), Config())
    assert tr.run_transfer(plan, tmp_path / "updated.json")["state"] == "completed"
    assert dst.read_bytes() == b"new content"


@pytest.mark.parametrize("backend", ["rsync", "rclone"])
def test_directory_content_copy_preserves_destination_extra_files(backend, tmp_path):
    if not shutil.which(backend):
        pytest.skip(f"{backend} unavailable")
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "nested").mkdir(parents=True)
    (src / "nested" / "a").write_text("hello")
    dst.mkdir()
    (dst / "keep").write_text("keep")
    plan = tr.build_plan(tr.TransferSpec(str(src) + "/", str(dst), backend=backend), Config())
    assert tr.run_transfer(plan, tmp_path / "dir.json")["state"] == "completed"
    assert (dst / "nested" / "a").read_text() == "hello"
    assert (dst / "keep").read_text() == "keep"


def test_existing_rsync_partial_file_is_reused_and_completed(tmp_path):
    if not shutil.which("rsync"):
        pytest.skip("rsync unavailable")
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(os.urandom(100000))
    partial = tmp_path / ".slaigpus-partial" / "dst"
    partial.parent.mkdir()
    partial.write_bytes(src.read_bytes()[:1000])
    plan = tr.build_plan(tr.TransferSpec(str(src), str(dst), verify="sha256"), Config())
    assert tr.run_transfer(plan, tmp_path / "partial.json")["state"] == "completed"
    assert not partial.exists()
    assert dst.read_bytes() == src.read_bytes()


def test_local_destination_trailing_slash_creates_directory_not_file(tmp_path):
    if not shutil.which("rsync"):
        pytest.skip("rsync unavailable")
    source = tmp_path / "file.bin"
    source.write_bytes(b"data")
    destination = tmp_path / "downloads"
    plan = tr.build_plan(tr.TransferSpec(str(source), str(destination) + "/"), Config())
    assert plan.spec.destination.endswith("/")
    assert tr.run_transfer(plan, tmp_path / "download.json")["state"] == "completed"
    assert (destination / "file.bin").read_bytes() == b"data"


def test_ssh_protocol_boundary_and_remote_verification(config, tmp_path, local_ssh):
    if not shutil.which("rsync"):
        pytest.skip("rsync unavailable")
    # Stand in for the SSH encryption layer only; run the real rsync server
    # and real remote python verification commands over inherited stdio.
    source = tmp_path / "source 'with space'"
    source.write_bytes(os.urandom(10000))
    remote = tmp_path / "remote" / "file 'with space';$(literal)"
    plan = tr.build_plan(tr.TransferSpec(str(source), "gpu:" + str(remote), verify="sha256"), config)
    record = tr.run_transfer(plan, tmp_path / "upload.json")
    assert record["state"] == "completed"
    local = tmp_path / "downloaded"
    plan = tr.build_plan(tr.TransferSpec("gpu:" + str(remote), str(local), verify="sha256"), config)
    assert tr.run_transfer(plan, tmp_path / "download.json")["state"] == "completed"
    assert local.read_bytes() == source.read_bytes()


def test_rclone_target_roundtrip_and_on_machine_execution(config, tmp_path, local_ssh):
    if not shutil.which("rclone"):
        pytest.skip("rclone unavailable")
    # The named local rclone backend exercises route/config handling without
    # object-store credentials. Real AOSS access is a separate deployment check.
    rclone_config = tmp_path / "private-rclone.conf"
    rclone_config.write_text("[fixture]\ntype = local\n")
    config.transfer_targets["store"] = TransferTarget("rclone", remote="fixture",
                                                       config_file=str(rclone_config))
    source = tmp_path / "src.bin"
    source.write_bytes(os.urandom(10000))
    stored = "store:" + str(tmp_path / "store" / "data.bin")
    upload = tr.build_plan(tr.TransferSpec(str(source), stored), config)
    assert tr.run_transfer(upload, tmp_path / "rclone-upload.json")["state"] == "completed"
    output = tmp_path / "afs" / "data.bin"
    download = tr.build_plan(tr.TransferSpec(stored, str(output), on="gpu"), config)
    assert tr.run_transfer(download, tmp_path / "rclone-download.json")["state"] == "completed"
    assert output.read_bytes() == source.read_bytes()


def test_sha_overwrite_repairs_same_size_same_timestamp_content(tmp_path):
    if not shutil.which("rsync"):
        pytest.skip("rsync unavailable")
    source, destination = tmp_path / "src", tmp_path / "dst"
    source.write_bytes(b"good")
    destination.write_bytes(b"evil")
    os.utime(destination, ns=(source.stat().st_atime_ns, source.stat().st_mtime_ns))
    plan = tr.build_plan(tr.TransferSpec(str(source), str(destination),
                                        overwrite=True, verify="sha256"), Config())
    assert tr.run_transfer(plan, tmp_path / "repair.json")["state"] == "completed"
    assert destination.read_bytes() == b"good"


def test_cli_transfer_does_not_require_sensecore_login(tmp_path, monkeypatch, capsys):
    src = tmp_path / "source"
    src.write_text("data")
    monkeypatch.setattr(cli, "_ensure_initial_configuration", lambda args: (
        pytest.fail("unexpected login") if cli._command_needs_initial_configuration(args) else None))
    config = tmp_path / "empty.toml"
    config.write_text("")
    assert cli.main(["transfer", "plan", str(src), "alias:/data/file",
                     "--config", str(config), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["backend"] == "rsync"


def test_interruption_records_recoverable_state(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.write_text("data")
    plan = tr.build_plan(tr.TransferSpec(str(src), str(tmp_path / "dst")), Config())
    monkeypatch.setattr(tr.shutil, "which", lambda _: "/bin/rsync")
    class Process:
        calls = []
        pid = 123
        def wait(self, timeout=None):
            if timeout is None:
                raise KeyboardInterrupt
            self.calls.append("wait")
        def terminate(self):
            self.calls.append("terminate")
    monkeypatch.setattr(tr.subprocess, "Popen", lambda *_a, **_kw: Process())
    record = tr.run_transfer(plan, tmp_path / "interrupted.json")
    assert record["state"] == "interrupted"
    assert record["returncode"] == 130
    assert Process.calls == ["terminate", "wait"]


def test_cancellation_during_verification_is_not_recorded_as_running(tmp_path, monkeypatch):
    if not shutil.which("rsync"):
        pytest.skip("rsync unavailable")
    src = tmp_path / "src"
    src.write_text("data")
    plan = tr.build_plan(tr.TransferSpec(str(src), str(tmp_path / "dst")), Config())
    def cancel(_plan):
        raise KeyboardInterrupt
    monkeypatch.setattr(tr, "verify_plan", cancel)
    record = tr.run_transfer(plan, tmp_path / "interrupted-verify.json")
    assert record["state"] == "interrupted"
    assert record["returncode"] == 130


def test_resume_preserves_history_and_refuses_changed_route(config, tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.write_text("data")
    plan = tr.build_plan(tr.TransferSpec(str(source), "gpu:/data/file"), config)
    old = tmp_path / "old.json"
    tr.write_receipt(old, {"schema": "slaigpus.transfer.v1", "state": "interrupted", "plan": plan.summary()})
    monkeypatch.setattr(tr, "load_config", lambda _path: config)
    new = tmp_path / "new.json"
    monkeypatch.setattr(tr, "run_transfer", lambda plan: {"schema": "slaigpus.transfer.v1", "state": "completed", "receipt": str(new)})
    args = cli.build_parser().parse_args(["transfer", "resume", str(old), "--json"])
    assert tr.cmd_transfer(args) == 0
    assert tr.read_receipt(old)["state"] == "interrupted"
    assert tr.read_receipt(new)["previous_receipt"] == str(old)
    config.transfer_targets["gpu"] = TransferTarget("ssh", host="different")
    with pytest.raises(tr.TransferError, match="route changed"):
        tr.cmd_transfer(args)


def test_normalized_local_paths_survive_changed_cwd(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "file").write_text("data")
    monkeypatch.chdir(tmp_path)
    plan = tr.build_plan(tr.TransferSpec("./source/", "./out"), Config())
    monkeypatch.chdir(source)
    restored = tr.build_plan(plan.spec, Config())
    assert restored.summary() == plan.summary()


def test_resume_never_duplicates_an_active_process(config, tmp_path, monkeypatch):
    source = tmp_path / "src"
    source.write_text("data")
    plan = tr.build_plan(tr.TransferSpec(str(source), "gpu:/data/file"), config)
    receipt = tmp_path / "running.json"
    tr.write_receipt(receipt, {"schema": "slaigpus.transfer.v1", "state": "running",
                              "pid": os.getpid(), "plan": plan.summary()})
    monkeypatch.setattr(tr, "load_config", lambda _path: config)
    args = cli.build_parser().parse_args(["transfer", "resume", str(receipt)])
    with pytest.raises(tr.TransferError, match="still exists"):
        tr.cmd_transfer(args)


def test_receipt_and_nested_destination_cannot_destroy_input(tmp_path):
    source = tmp_path / "file"
    source.write_text("data")
    plan = tr.build_plan(tr.TransferSpec(str(source), str(tmp_path / "dst")), Config())
    with pytest.raises(tr.TransferError, match="receipt already exists"):
        tr.run_transfer(plan, source)
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(tr.TransferError, match="inside"):
        tr.build_plan(tr.TransferSpec(str(directory), str(directory / "nested")), Config())
