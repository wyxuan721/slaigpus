"""Generic file transfers using rsync/SSH and configured rclone remotes.

No browser, SenseCore token, dataset schema, or training job is involved.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

from .config import Config, TransferTarget, default_state_root, load_config, validate_ssh_alias


class TransferError(RuntimeError):
    """Invalid route, unavailable backend, or unsuccessful verification."""


@dataclass(frozen=True)
class Endpoint:
    kind: str
    path: str
    target: Optional[TransferTarget] = None


@dataclass(frozen=True)
class TransferSpec:
    source: str
    destination: str
    backend: str = "auto"
    overwrite: bool = False
    dry_run: bool = False
    verify: str = "backend"
    on: str = ""


@dataclass
class TransferPlan:
    spec: TransferSpec
    source: Endpoint
    destination: Endpoint
    backend: str
    command: List[str]
    runner: Optional[TransferTarget] = None

    def summary(self) -> Dict[str, Any]:
        return {
            "spec": asdict(self.spec),
            "source": asdict(self.source),
            "destination": asdict(self.destination),
            "backend": self.backend,
            "runner": asdict(self.runner) if self.runner else None,
            "command": self.command,
            "existing_files": "update" if self.spec.overwrite else "skip",
            "deletes_source": False,
            "deletes_destination": False,
        }


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise TransferError(f"{label} must be nonempty text without control characters")
    return value


def _remote_path(path: str) -> str:
    # Absolute paths avoid shell tilde expansion and ambiguous rsync options.
    if not path.startswith("/"):
        raise TransferError("remote machine paths must be absolute")
    return path


def endpoint(value: str, config: Config, *, remote_local: bool = False) -> Endpoint:
    value = _text(value, "transfer path")
    if "://" in value:
        uri = urlsplit(value)
        if uri.scheme not in {"aoss", "s3", "rclone"} or uri.query or uri.fragment:
            raise TransferError("use a configured target:path or aoss://target/bucket/key")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", uri.netloc):
            raise TransferError("object URI must reference a target alias, without credentials")
        target = config.transfer_targets.get(uri.netloc)
        if target is None or target.kind != "rclone":
            raise TransferError("object URI must reference a configured rclone target")
        path = uri.path.lstrip("/")
        if not path:
            raise TransferError("object URI needs a bucket/path")
        return Endpoint("rclone", path, target)
    # ./file:name and absolute local paths always remain local.
    if not value.startswith(("/", "./", "../", "~")) and ":" in value:
        name, path = value.split(":", 1)
        _text(path, "remote path")
        target = config.transfer_targets.get(name)
        if target is None:
            target = TransferTarget(kind="ssh", host=validate_ssh_alias(name))
        if target.kind == "ssh":
            _remote_path(path)
        return Endpoint(target.kind, path, target)
    path = _remote_path(value) if remote_local else str(Path(value).expanduser().absolute())
    if value.endswith("/") and not path.endswith("/"):
        path += "/"
    return Endpoint("local", path)


def ssh_command(target: TransferTarget) -> List[str]:
    if target.kind != "ssh":
        raise TransferError("remote executor must be an SSH target")
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20",
               "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]
    if target.port is not None:
        command += ["-p", str(target.port)]
    if target.proxy_jump:
        command += ["-J", target.proxy_jump]
    if target.identity_file:
        command += ["-i", str(Path(target.identity_file).expanduser())]
    return command


def remote_command(target: TransferTarget, command: Sequence[str]) -> List[str]:
    """OpenSSH uses a remote shell; quote each argument exactly once."""
    return ssh_command(target) + ["--", target.host, shlex.join(command)]


def _rsync_path(selected: Endpoint) -> str:
    if selected.kind == "ssh":
        assert selected.target is not None
        return selected.target.host + ":" + selected.path
    return selected.path


def _rclone_path(selected: Endpoint) -> str:
    if selected.kind == "rclone":
        assert selected.target is not None
        return selected.target.remote + ":" + selected.path
    return selected.path


def build_plan(spec: TransferSpec, config: Config) -> TransferPlan:
    if spec.backend not in {"auto", "rsync", "rclone"}:
        raise TransferError("backend must be auto, rsync, or rclone")
    if spec.verify not in {"backend", "size", "sha256"}:
        raise TransferError("verify must be backend, size, or sha256")
    if type(spec.overwrite) is not bool or type(spec.dry_run) is not bool:
        raise TransferError("overwrite and dry_run must be boolean")
    runner = None
    if spec.on:
        runner = config.transfer_targets.get(spec.on)
        if runner is None:
            runner = TransferTarget(kind="ssh", host=validate_ssh_alias(spec.on))
        if runner.kind != "ssh":
            raise TransferError("--on requires an SSH machine target")
    src = endpoint(spec.source, config, remote_local=runner is not None)
    dst = endpoint(spec.destination, config, remote_local=runner is not None)
    # Receipts remain resumable when the caller changes its working directory.
    if not runner:
        spec = replace(
            spec,
            source=src.path if src.kind == "local" else spec.source,
            destination=dst.path if dst.kind == "local" else spec.destination,
        )
    if runner and (src.kind == "ssh" or dst.kind == "ssh"):
        raise TransferError("with --on use absolute machine paths, not a second SSH endpoint")
    if src.kind == dst.kind == "ssh":
        raise TransferError("two SSH endpoints are unsupported; use an explicit intermediate copy")
    if {src.kind, dst.kind} == {"ssh", "rclone"}:
        raise TransferError("object/machine copies must use --on MACHINE with an absolute machine path")
    if src == dst:
        raise TransferError("source and destination are the same")
    if not runner and src.kind == "local":
        path = Path(src.path)
        if not path.exists() or not (path.is_file() or path.is_dir()):
            raise TransferError("local source must be an existing file or directory")
        if path.is_dir() and dst.kind == "local" and Path(dst.path).resolve().is_relative_to(path.resolve()):
            raise TransferError("destination cannot be inside the source directory")
    backend = ("rclone" if "rclone" in {src.kind, dst.kind} else "rsync") if spec.backend == "auto" else spec.backend
    if backend == "rsync" and "rclone" in {src.kind, dst.kind}:
        raise TransferError("object storage requires the rclone backend")
    if backend == "rclone" and "ssh" in {src.kind, dst.kind}:
        raise TransferError("use rsync for SSH paths or configure an rclone SFTP target")
    if spec.verify != "backend":
        if "rclone" in {src.kind, dst.kind}:
            raise TransferError("explicit size/SHA verification currently supports machine files only")
        if spec.source.endswith("/") or spec.destination.endswith("/"):
            raise TransferError("size/SHA verification requires exact single-file paths")
        if not runner and src.kind == "local" and not Path(src.path).is_file():
            raise TransferError("size/SHA verification requires a single file")
        if not runner and dst.kind == "local" and Path(dst.path).is_dir():
            raise TransferError("size/SHA verification requires an exact destination file")
    if backend == "rsync":
        command = ["rsync", "--recursive", "--times", "--perms", "--safe-links",
                   "--links", "--secluded-args", "--partial-dir=.slaigpus-partial",
                   "--info=progress2,stats2", "--mkpath"]
        if not spec.overwrite:
            command.append("--ignore-existing")
        elif spec.verify == "sha256":
            command.append("--checksum")
        if spec.dry_run:
            command.append("--dry-run")
        remote = src if src.kind == "ssh" else dst if dst.kind == "ssh" else None
        if remote:
            assert remote.target is not None
            command += ["-e", shlex.join(ssh_command(remote.target))]
        # Preserve the user's trailing slash (rsync directory vs contents).
        source_path = _rsync_path(src)
        if spec.source.endswith("/") and not source_path.endswith("/"):
            source_path += "/"
        command += ["--", source_path, _rsync_path(dst)]
    else:
        # copyto works for exact files and copies contents for directories.
        # Never use rclone sync: it deletes destination-only objects.
        command = ["rclone", "copyto", "--stats=5s", "--stats-one-line"]
        if not spec.overwrite:
            command.append("--ignore-existing")
        elif spec.verify == "sha256":
            command.append("--checksum")
        if spec.dry_run:
            command.append("--dry-run")
        configs = {e.target.config_file for e in (src, dst)
                   if e.target is not None and e.target.config_file}
        if len(configs) > 1:
            raise TransferError("rclone endpoints must use the same config_file")
        if configs:
            config_file = configs.pop()
            if runner:
                _remote_path(config_file)
            else:
                config_file = str(Path(config_file).expanduser().absolute())
            command += ["--config", config_file]
        command += ["--", _rclone_path(src), _rclone_path(dst)]
    if runner:
        command = remote_command(runner, command)
    return TransferPlan(spec, src, dst, backend, command, runner)


def _time() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_receipt(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".transfer-", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_receipt(path: Path) -> Dict[str, Any]:
    try:
        with path.open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("oversized receipt")
        record = json.loads(raw)
        if not isinstance(record, dict) or record.get("schema") != "slaigpus.transfer.v1":
            raise ValueError("invalid schema")
        return record
    except (OSError, ValueError, UnicodeError) as exc:
        raise TransferError("could not read a valid transfer receipt") from exc


def _file_info(selected: Endpoint, runner: Optional[TransferTarget], verify: str) -> Dict[str, Any]:
    target = selected.target if selected.kind == "ssh" else runner
    if target:
        # A fixed script accepts only a quoted path, never user shell code.
        script = ("import hashlib,json,pathlib,sys\n"
                  "p=pathlib.Path(sys.argv[1])\nassert p.is_file()\n"
                  "r={'size':p.stat().st_size}\nh=hashlib.sha256()\n"
                  "with p.open('rb') as f:\n"
                  " for b in iter(lambda:f.read(8388608),b''): h.update(b)\n"
                  "r['sha256']=h.hexdigest()\nprint(json.dumps(r))") if verify == "sha256" else (
                  "import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                  "assert p.is_file(); print(json.dumps({'size':p.stat().st_size}))")
        result = subprocess.run(remote_command(target, ["python3", "-c", script, selected.path]),
                                stdin=subprocess.DEVNULL, capture_output=True, text=True)
        if result.returncode:
            raise TransferError("remote single-file verification failed")
        try:
            metadata = json.loads(result.stdout)
            keys = {"size", "sha256"} if verify == "sha256" else {"size"}
            if not isinstance(metadata, dict) or set(metadata) != keys:
                raise ValueError("invalid fields")
            if type(metadata["size"]) is not int or metadata["size"] < 0:
                raise ValueError("invalid size")
            if verify == "sha256" and (not isinstance(metadata["sha256"], str)
                                       or re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]) is None):
                raise ValueError("invalid checksum")
            return metadata
        except ValueError as exc:
            raise TransferError("invalid remote file metadata") from exc
    path = Path(selected.path)
    if not path.is_file():
        raise TransferError("verification requires a single file")
    result = {"size": path.stat().st_size}
    if verify == "sha256":
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(block)
        result["sha256"] = digest.hexdigest()
    return result


def verify_plan(plan: TransferPlan) -> Dict[str, Any]:
    if plan.spec.verify == "backend":
        return {"method": "backend", "note": "backend checks transferred files; existing files may be skipped"}
    source = _file_info(plan.source, plan.runner, plan.spec.verify)
    destination = _file_info(plan.destination, plan.runner, plan.spec.verify)
    if source != destination:
        raise TransferError("source and destination file verification differ")
    return {"method": plan.spec.verify, "source": source, "destination": destination, "matches": True}


def run_transfer(plan: TransferPlan, receipt: Optional[Path] = None) -> Dict[str, Any]:
    if shutil.which(plan.command[0]) is None:
        raise TransferError(f"required executable is unavailable: {plan.command[0]}")
    receipt = (receipt or default_state_root() / "transfers" / (uuid.uuid4().hex + ".json")).expanduser().absolute()
    if receipt.exists():
        raise TransferError("receipt already exists; resume it or select a new receipt path")
    if str(receipt) in {plan.source.path, plan.destination.path}:
        raise TransferError("receipt cannot replace a source or destination file")
    record = {"schema": "slaigpus.transfer.v1", "id": receipt.stem,
              "state": "running", "started_at": _time(), "plan": plan.summary(),
              "receipt": str(receipt)}
    write_receipt(receipt, record)
    print(f"transfer receipt: {receipt}", file=sys.stderr)
    try:
        process = subprocess.Popen(plan.command, stdin=subprocess.DEVNULL,
                                   stdout=sys.stderr, stderr=sys.stderr)
        record["pid"] = process.pid
        write_receipt(receipt, record)
        try:
            code = process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            record["state"] = "interrupted"
            record["returncode"] = 130
        else:
            record["returncode"] = code
            if code:
                record["state"] = "failed"
            elif plan.spec.dry_run:
                record["state"] = "dry_run"
            else:
                record["verification"] = verify_plan(plan)
                record["state"] = "completed"
    except KeyboardInterrupt:
        # This also covers cancellation during post-transfer verification.
        record["state"] = "interrupted"
        record["returncode"] = 130
    except (OSError, TransferError) as exc:
        record["state"] = "failed"
        record["returncode"] = 1
        # Do not persist backend stdout, environment, or auth material.
        record["error"] = "transfer execution or verification failed"
        print(str(exc), file=sys.stderr)
    finally:
        record["finished_at"] = _time()
        write_receipt(receipt, record)
    return record


def cmd_transfer(args: argparse.Namespace) -> int:
    action = args.transfer_cmd
    if action == "status":
        result = read_receipt(args.receipt)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(f"recorded state: {result.get('state')}")
            print(f"receipt: {args.receipt}")
        return 0
    config = load_config(args.config)
    if action in {"resume", "verify"}:
        original = read_receipt(args.receipt)
        try:
            spec = TransferSpec(**original["plan"]["spec"])
        except (KeyError, TypeError) as exc:
            raise TransferError("receipt contains an invalid transfer specification") from exc
        plan = build_plan(spec, config)
        if plan.summary() != original.get("plan"):
            raise TransferError("transfer route changed; start a new transfer rather than silently rerouting")
        if action == "verify":
            if spec.dry_run or original.get("state") != "completed":
                raise TransferError("verify requires a completed real transfer")
            result = verify_plan(build_plan(replace(spec, verify=args.method), config))
        else:
            state = original.get("state")
            if state == "running":
                pid = original.get("pid")
                if type(pid) is not int or pid <= 0:
                    raise TransferError("running receipt has no process ID; start a new copy")
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError as exc:
                    raise TransferError("cannot establish that the previous process stopped") from exc
                else:
                    raise TransferError("previous transfer process still exists")
            elif state not in {"failed", "interrupted"}:
                raise TransferError("resume requires a failed or interrupted transfer")
            # New receipt preserves the history of the previous attempt.
            result = run_transfer(plan)
            result["previous_receipt"] = str(args.receipt.absolute())
            write_receipt(Path(result["receipt"]), result)
    else:
        spec = TransferSpec(args.source, args.destination, args.backend,
                            args.overwrite, args.dry_run, args.verify, args.on)
        plan = build_plan(spec, config)
        result = plan.summary() if action == "plan" else run_transfer(plan, args.receipt)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    elif action == "plan":
        print(f"backend: {result['backend']}")
        print(f"command: {shlex.join(result['command'])}")
        print(f"existing files: {result['existing_files']}")
    else:
        print(f"state: {result.get('state', 'verified')}")
        if result.get("receipt"):
            print(f"receipt: {result['receipt']}")
        if result.get("verification"):
            print(f"verification: {result['verification']['method']}")
    return 130 if result.get("state") == "interrupted" else 1 if result.get("state") == "failed" else 0


def add_transfer_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser("transfer", help="copy files through SSH or configured rclone remotes")
    commands = parser.add_subparsers(dest="transfer_cmd", required=True)
    for action in ("copy", "push", "pull", "plan"):
        command = commands.add_parser(action)
        command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        if action == "push":
            command.add_argument("source", help="local source")
            command.add_argument("--target", dest="destination", required=True)
        elif action == "pull":
            command.add_argument("--source", required=True)
            command.add_argument("--target", dest="destination", required=True)
        else:
            command.add_argument("source")
            command.add_argument("destination")
        command.add_argument("--backend", choices=("auto", "rsync", "rclone"), default="auto")
        command.add_argument("--on", default="", metavar="MACHINE", help="execute on this SSH target; unqualified absolute paths then belong to that machine")
        command.add_argument("--overwrite", action="store_true", help="allow updating changed destination files")
        command.add_argument("--dry-run", action="store_true", help="run backend checks without copying")
        command.add_argument("--verify", choices=("backend", "size", "sha256"), default="backend", help="size/SHA require exact machine file paths; no directory scan")
        command.add_argument("--receipt", type=Path, help="write outcome here; default is the tool state directory")
        command.add_argument("--json", action="store_true")
        command.set_defaults(func=cmd_transfer)
    for action in ("status", "resume", "verify"):
        command = commands.add_parser(action)
        command.add_argument("receipt", type=Path)
        command.add_argument("--config", type=Path, default=argparse.SUPPRESS)
        command.add_argument("--json", action="store_true")
        if action == "verify":
            command.add_argument("--method", choices=("size", "sha256"), default="size")
        command.set_defaults(func=cmd_transfer)
