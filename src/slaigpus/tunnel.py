"""slaigpus SSH dynamic-forward (SOCKS5) tunnel management.

The whole point of this module is that ``ssh -D`` is easy to start and
annoying to babysit: you have to know when it is actually ready, you have to
notice when it dies, and you have to make sure it does not outlive the thing
that needed it.  ``SSHTunnel`` handles all three.
"""

from __future__ import annotations

import atexit
import errno
import os
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


class TunnelError(RuntimeError):
    """The tunnel could not be established, or died unexpectedly."""


def free_port() -> int:
    """Ask the OS for an unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_is_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


class SSHTunnel:
    """A ``ssh -N -D`` SOCKS5 proxy, managed as a context manager.

    Parameters
    ----------
    ssh_host:
        Anything ssh accepts as a destination: ``user@host``, or an alias
        defined in ``~/.ssh/config`` (recommended — keep credentials there,
        not here).
    port:
        Local SOCKS port.  ``0`` means "pick a free one", which is what you
        want for automation: no collisions when two agents run at once.
    ssh_args:
        Extra arguments spliced in before the destination, e.g.
        ``["-J", "bastion"]`` to chain through another jump host.
    """

    def __init__(
        self,
        ssh_host: str,
        port: int = 0,
        ssh_args: Optional[Sequence[str]] = None,
        ssh_binary: str = "",
        connect_timeout: float = 20.0,
        keepalive: int = 30,
        reuse_existing: bool = False,
    ) -> None:
        if not ssh_host:
            raise TunnelError("ssh_host is required")
        self.ssh_host = ssh_host
        self.requested_port = int(port)
        self.port = int(port)
        self.ssh_args: List[str] = list(ssh_args or [])
        # SLAIGPUS_SSH lets you point at a different ssh (a newer Homebrew one,
        # say) without touching any code.
        self.ssh_binary = ssh_binary or os.environ.get("SLAIGPUS_SSH") or "ssh"
        self.connect_timeout = float(connect_timeout)
        self.keepalive = int(keepalive)
        self.reuse_existing = reuse_existing

        self._proc: Optional[subprocess.Popen] = None
        self._log_path: Optional[Path] = None
        self._adopted = False  # True when we attached to a pre-existing tunnel
        self._atexit_registered = False

    # ------------------------------------------------------------------ repr

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        state = "running" if self.is_running else "stopped"
        return f"<SSHTunnel {self.ssh_host} port={self.port or '?'} {state}>"

    # ------------------------------------------------------------ properties

    @property
    def socks_url(self) -> str:
        """``socks5://127.0.0.1:PORT`` — the form Chrome and Playwright want."""
        return f"socks5://127.0.0.1:{self.port}"

    @property
    def socks_url_remote_dns(self) -> str:
        """``socks5h://...`` — the form curl/requests want for remote DNS."""
        return f"socks5h://127.0.0.1:{self.port}"

    @property
    def is_running(self) -> bool:
        if self._adopted:
            return port_is_open(self.port)
        return self._proc is not None and self._proc.poll() is None

    @property
    def env(self) -> Dict[str, str]:
        """Environment overlay for child processes (curl, python, etc.).

        ``socks5h`` is deliberate: it makes the *remote* side resolve DNS,
        which matters when the hostname only exists in the jump host's
        resolver.
        """
        url = self.socks_url_remote_dns
        return {
            "ALL_PROXY": url,
            "all_proxy": url,
            "HTTP_PROXY": url,
            "http_proxy": url,
            "HTTPS_PROXY": url,
            "https_proxy": url,
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "no_proxy": "localhost,127.0.0.1,::1",
            "SLAIGPUS_SOCKS_PORT": str(self.port),
            "SLAIGPUS_SOCKS_URL": self.socks_url,
        }

    @property
    def log_tail(self) -> str:
        """Whatever ssh wrote to stderr, for error messages."""
        if not self._log_path or not self._log_path.exists():
            return ""
        try:
            text = self._log_path.read_text(errors="replace").strip()
        except OSError:
            return ""
        lines = text.splitlines()
        return "\n".join(lines[-12:])

    # ---------------------------------------------------------------- control

    def start(self) -> "SSHTunnel":
        if self.is_running:
            return self

        # A tunnel may be restarted after the remote CCI reboots.  Chrome's
        # proxy setting cannot be changed in place, so retain the port chosen
        # on the first start even when the caller originally requested 0.
        # ``_adopted`` only describes the previous start and must not prevent
        # us from owning a replacement once that listener disappears.
        self._adopted = False

        if shutil.which(self.ssh_binary) is None:
            raise TunnelError(f"ssh binary not found: {self.ssh_binary}")

        restart_port = self.requested_port or self.port
        if restart_port:
            self.port = restart_port
            if port_is_open(self.port):
                if self.reuse_existing:
                    self._adopted = True
                    return self
                raise TunnelError(
                    f"local port {self.port} is already in use — stop whatever "
                    f"is listening, choose another port, or pass "
                    f"reuse_existing=True"
                )
        else:
            self.port = free_port()

        log = tempfile.NamedTemporaryFile(
            mode="w+", prefix="slaigpus-", suffix=".log", delete=False
        )
        self._log_path = Path(log.name)

        cmd = [
            self.ssh_binary,
            "-N",                                    # no remote command
            "-D", f"127.0.0.1:{self.port}",          # the SOCKS5 listener
            # The tunnel must remain attached to a process we own.  If the
            # user's ssh config enables ControlMaster/ControlPersist, a new
            # client can hand the forwarding to an existing master and exit
            # successfully, which looks like a dead tunnel and also prevents
            # us from cleaning it up reliably.
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            "-o", "ExitOnForwardFailure=yes",        # fail loudly, not silently
            "-o", f"ServerAliveInterval={self.keepalive}",
            "-o", "ServerAliveCountMax=3",
            "-o", "BatchMode=yes",                   # key auth only, never prompt
            "-o", "StrictHostKeyChecking=accept-new",
            *self.ssh_args,
            self.ssh_host,
        ]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,  # own process group -> clean group kill
            )
        except OSError as exc:
            log.close()
            raise TunnelError(f"could not launch ssh: {exc}") from exc
        finally:
            log.close()

        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True

        self._wait_until_ready()
        return self

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.connect_timeout
        while time.monotonic() < deadline:
            assert self._proc is not None
            if self._proc.poll() is not None:
                detail = self.log_tail or "(ssh produced no output)"
                raise TunnelError(
                    f"ssh exited with code {self._proc.returncode} before the "
                    f"tunnel came up:\n{detail}"
                )
            if port_is_open(self.port):
                return
            time.sleep(0.15)

        self.stop()
        raise TunnelError(
            f"timed out after {self.connect_timeout:.0f}s waiting for the SOCKS "
            f"listener on port {self.port}."
            + (f"\nssh said:\n{self.log_tail}" if self.log_tail else "")
        )

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            self._cleanup_log()
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        except OSError as exc:  # pragma: no cover - platform quirks
            if exc.errno != errno.ESRCH:
                raise
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - stubborn ssh
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            proc.wait(timeout=5)
        self._cleanup_log()

    def _cleanup_log(self) -> None:
        if self._log_path and self._log_path.exists():
            try:
                self._log_path.unlink()
            except OSError:  # pragma: no cover
                pass
        self._log_path = None

    # --------------------------------------------------------------- probing

    def probe(self, host: str, port: int, timeout: float = 8.0) -> None:
        """Open a real SOCKS5 CONNECT to *host:port* through the tunnel.

        This is the only check that proves the whole chain works — that ssh is
        up, that the jump host can resolve the name, and that it can reach the
        target.  A bound local port proves none of that.

        Raises ``TunnelError`` describing exactly which stage failed.
        """
        if not self.is_running:
            raise TunnelError("tunnel is not running")

        encoded = host.encode("idna") if _is_hostname(host) else host.encode()
        if len(encoded) > 255:
            raise TunnelError(f"hostname too long for SOCKS5: {host}")

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            try:
                sock.connect(("127.0.0.1", self.port))
            except OSError as exc:
                raise TunnelError(f"cannot reach local SOCKS port: {exc}") from exc

            try:
                # Greeting: SOCKS5, one method, "no authentication".
                sock.sendall(b"\x05\x01\x00")
                greeting = _recv_exact(sock, 2)
                if greeting[0] != 0x05:
                    raise TunnelError(
                        f"port {self.port} is not speaking SOCKS5 — is something "
                        f"else listening on it?"
                    )
                if greeting[1] != 0x00:
                    raise TunnelError(
                        "SOCKS proxy demanded authentication, which ssh -D never "
                        "does; something else is on this port"
                    )

                # CONNECT with an ATYP=3 domain name, so the jump host resolves it.
                request = (
                    b"\x05\x01\x00\x03"
                    + bytes([len(encoded)])
                    + encoded
                    + struct.pack("!H", port)
                )
                sock.sendall(request)
                reply = _recv_exact(sock, 4)
                if reply[1] != 0x00:
                    raise TunnelError(
                        f"jump host could not reach {host}:{port} — "
                        f"{_SOCKS5_ERRORS.get(reply[1], f'SOCKS error 0x{reply[1]:02x}')}"
                    )
            except socket.timeout as exc:
                raise TunnelError(
                    f"timed out talking to {host}:{port} through the tunnel"
                ) from exc
            except OSError as exc:
                raise TunnelError(f"SOCKS handshake failed: {exc}") from exc

    # ------------------------------------------------------- context manager

    def __enter__(self) -> "SSHTunnel":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.stop()
        return False


_SOCKS5_ERRORS = {
    0x01: "general failure",
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable (DNS failed on the jump host, or no route)",
    0x05: "connection refused by the target",
    0x06: "TTL expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


def _is_hostname(value: str) -> bool:
    try:
        socket.inet_aton(value)
        return False
    except OSError:
        return ":" not in value


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise TunnelError("SOCKS proxy closed the connection unexpectedly")
        buf += chunk
    return buf


def open_tunnel(
    ssh_host: str,
    port: int = 0,
    probe: Optional[Tuple[str, int]] = None,
    **kwargs,
) -> SSHTunnel:
    """Convenience: start a tunnel and optionally verify it end to end."""
    tunnel = SSHTunnel(ssh_host, port=port, **kwargs).start()
    if probe:
        try:
            tunnel.probe(*probe)
        except TunnelError:
            tunnel.stop()
            raise
    return tunnel
