"""Direct and SSH SOCKS network paths used by slaigpus commands."""

from __future__ import annotations

import socket
from typing import Dict, Sequence

from .tunnel import SSHTunnel, TunnelError


class NetworkConnection:
    """A small common lifecycle around direct access or ``ssh -D``."""

    def __init__(
        self,
        *,
        mode: str,
        ssh_host: str = "",
        port: int = 0,
        ssh_args: Sequence[str] = (),
        reuse_existing: bool = False,
    ) -> None:
        if mode not in {"direct", "ssh"}:
            raise TunnelError("network mode must be 'direct' or 'ssh'")
        if mode == "ssh" and not ssh_host:
            raise TunnelError("SSH mode requires a Host alias")
        self.mode = mode
        self.ssh_host = ssh_host
        self._started = False
        self._tunnel = (
            SSHTunnel(
                ssh_host,
                port=port,
                ssh_args=ssh_args,
                reuse_existing=reuse_existing,
            )
            if mode == "ssh"
            else None
        )

    @property
    def uses_ssh(self) -> bool:
        return self._tunnel is not None

    @property
    def port(self) -> int:
        return self._tunnel.port if self._tunnel is not None else 0

    @property
    def is_running(self) -> bool:
        if self._tunnel is not None:
            return self._tunnel.is_running
        return self._started

    @property
    def log_tail(self) -> str:
        return self._tunnel.log_tail if self._tunnel is not None else ""

    @property
    def socks_url(self) -> str:
        return self._tunnel.socks_url if self._tunnel is not None else ""

    @property
    def socks_url_remote_dns(self) -> str:
        return self._tunnel.socks_url_remote_dns if self._tunnel is not None else ""

    @property
    def env(self) -> Dict[str, str]:
        if self._tunnel is not None:
            return {**self._tunnel.env, "SLAIGPUS_CONNECTION": "ssh"}
        # Empty proxy values make the direct contract deterministic for common
        # command-line clients instead of inheriting a shell proxy by accident.
        return {
            "ALL_PROXY": "",
            "all_proxy": "",
            "HTTP_PROXY": "",
            "http_proxy": "",
            "HTTPS_PROXY": "",
            "https_proxy": "",
            "NO_PROXY": "*",
            "no_proxy": "*",
            "SLAIGPUS_CONNECTION": "direct",
            "SLAIGPUS_SOCKS_PORT": "",
            "SLAIGPUS_SOCKS_URL": "",
        }

    def start(self) -> "NetworkConnection":
        if self._tunnel is not None:
            self._tunnel.start()
        self._started = True
        return self

    def stop(self) -> None:
        if self._tunnel is not None:
            self._tunnel.stop()
        self._started = False

    def probe(self, host: str, port: int, timeout: float = 10.0) -> None:
        if self._tunnel is not None:
            self._tunnel.probe(host, port, timeout=timeout)
            return
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return
        except socket.gaierror as exc:
            raise TunnelError(f"direct DNS lookup failed for {host}: {exc}") from exc
        except OSError as exc:
            raise TunnelError(f"cannot directly reach {host}:{port}: {exc}") from exc

    def __enter__(self) -> "NetworkConnection":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.stop()
        return False


__all__ = ["NetworkConnection"]
