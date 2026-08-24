"""Private file-backed credentials for slaigpus automation.

Credentials are stored only in a tightly permissioned JSON file. Secret
values are never accepted through command-line arguments or environment
variables, and public errors never include file paths or payload data.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol


_PAYLOAD_VERSION = 1
_PAYLOAD_KEYS = frozenset(("version", "username", "password"))
_MAX_CREDENTIAL_FILE_BYTES = 16 * 1024


class CredentialStoreError(RuntimeError):
    """A redacted credential-store failure safe to show to a user."""


@dataclass(frozen=True, repr=False)
class SenseCoreCredentials:
    """A login pair whose representation can never reveal either value."""

    username: str = field(repr=False)
    password: str = field(repr=False)

    def __repr__(self) -> str:
        return "<SenseCoreCredentials redacted>"


class CredentialStore(Protocol):
    """Small injectable interface used by CLI and browser automation."""

    def save(self, credentials: SenseCoreCredentials) -> None:
        """Create or replace the stored credentials."""

    def load(self) -> Optional[SenseCoreCredentials]:
        """Return the stored credentials, or ``None`` when unconfigured."""

    def status(self) -> bool:
        """Check for an item without reading its secret payload."""

    def is_configured(self) -> bool:
        """Alias for :meth:`status` for call-site readability."""

    def delete(self) -> bool:
        """Delete the item, returning whether one existed."""


class _CredentialsFileMissing(Exception):
    """Internal control flow for an unconfigured file store."""


class _InvalidCredentialsFile(Exception):
    """Internal error whose public replacement never includes path or data."""


def default_credentials_file() -> Path:
    """Return ``~/.config/slaigpus/credentials.json`` or its XDG equivalent.

    An absolute ``XDG_CONFIG_HOME`` is honored. Relative and tilde-prefixed
    values are ignored so credentials can never land below the working tree.
    """

    configured = os.environ.get("XDG_CONFIG_HOME", "")
    configured_path = Path(configured) if configured else None
    root = (
        configured_path
        if configured_path is not None and configured_path.is_absolute()
        else Path.home() / ".config"
    )
    return root / "slaigpus" / "credentials.json"


def _unique_json_object(pairs: Any) -> dict:
    """Build one JSON object while rejecting duplicate member names."""

    result = {}
    for key, value in pairs:
        if key in result:
            result = {}
            pairs = None
            key = None
            value = None
            raise ValueError("duplicate JSON member")
        result[key] = value
    pairs = None
    return result


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON value")


def _encode_payload(credentials: SenseCoreCredentials) -> bytes:
    """Encode one validated credential pair without retaining failure data."""

    valid = False
    try:
        valid = (
            isinstance(credentials, SenseCoreCredentials)
            and isinstance(credentials.username, str)
            and isinstance(credentials.password, str)
            and bool(credentials.username.strip())
            and bool(credentials.password)
        )
    except Exception:  # noqa: BLE001 - malformed injectable value boundary
        valid = False
    if not valid:
        credentials = None  # type: ignore[assignment]
        raise _InvalidCredentialsFile

    failed = False
    encoded = ""
    try:
        encoded = json.dumps(
            {
                "version": _PAYLOAD_VERSION,
                "username": credentials.username,
                "password": credentials.password,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
    except Exception:  # noqa: BLE001 - scrub before replacing the exception
        failed = True
    credentials = None  # type: ignore[assignment]
    if failed:
        encoded = ""
        raise _InvalidCredentialsFile

    payload = b""
    try:
        payload = encoded.encode("ascii")
    except Exception:  # noqa: BLE001 - scrub before replacing the exception
        failed = True
    encoded = ""
    if failed:
        payload = b""
        raise _InvalidCredentialsFile
    return payload


class FileCredentialStore:
    """Store one SenseCore login in a tightly permissioned JSON file.

    The containing directory must be owned by the current user with mode
    ``0700``. The file must be a single-link, user-owned regular file with
    mode ``0600``. Writes use a private temporary file and atomic rename.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        selected = (
            default_credentials_file() if path is None else Path(path).expanduser()
        )
        self._path = selected
        self._parent = selected.parent
        self._name = selected.name
        self._path_valid = bool(
            selected.is_absolute()
            and self._name not in ("", ".", "..")
            and ".." not in selected.parts
        )

    def __repr__(self) -> str:
        return "<FileCredentialStore>"

    @staticmethod
    def _error(operation: str) -> CredentialStoreError:
        return CredentialStoreError(
            f"could not {operation} SenseCore credentials file"
        )

    @staticmethod
    def _current_uid() -> int:
        getuid = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
        if not callable(getuid):
            raise _InvalidCredentialsFile
        return int(getuid())

    @classmethod
    def _validate_parent_details(cls, details: Any) -> None:
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != cls._current_uid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise _InvalidCredentialsFile

    @classmethod
    def _validate_file_details(cls, details: Any) -> None:
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != cls._current_uid()
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_nlink != 1
            or details.st_size < 0
            or details.st_size > _MAX_CREDENTIAL_FILE_BYTES
        ):
            raise _InvalidCredentialsFile

    @staticmethod
    def _directory_open_flags() -> int:
        if any(not hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW")):
            raise _InvalidCredentialsFile
        return (
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )

    @staticmethod
    def _file_open_flags(mode: int) -> int:
        if not hasattr(os, "O_NOFOLLOW"):
            raise _InvalidCredentialsFile
        return mode | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)

    def _ensure_parent(self) -> None:
        if not self._path_valid:
            raise _InvalidCredentialsFile
        created = False
        try:
            self._parent.mkdir(parents=True, mode=0o700, exist_ok=False)
            created = True
        except FileExistsError:
            pass
        if created:
            os.chmod(self._parent, 0o700)

    def _open_parent(self, *, create: bool) -> int:
        if not self._path_valid:
            raise _InvalidCredentialsFile
        if create:
            self._ensure_parent()
        try:
            descriptor = os.open(self._parent, self._directory_open_flags())
        except FileNotFoundError:
            raise _CredentialsFileMissing from None
        try:
            self._validate_parent_details(os.fstat(descriptor))
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def _file_details(self, parent_descriptor: int) -> Optional[Any]:
        try:
            details = os.stat(
                self._name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        self._validate_file_details(details)
        return details

    @staticmethod
    def _same_file_state(before: Any, after: Any) -> bool:
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size")
        if any(getattr(before, name) != getattr(after, name) for name in fields):
            return False
        for name in ("st_mtime_ns", "st_ctime_ns"):
            if hasattr(before, name) and getattr(before, name) != getattr(after, name):
                return False
        return True

    def _read_payload(self) -> bytes:
        parent_descriptor = self._open_parent(create=False)
        file_descriptor = -1
        chunks = []
        raw = b""
        chunk = b""
        details: Any = None
        before: Any = None
        after: Any = None
        try:
            details = self._file_details(parent_descriptor)
            if details is None:
                raise _CredentialsFileMissing
            file_descriptor = os.open(
                self._name,
                self._file_open_flags(os.O_RDONLY),
                dir_fd=parent_descriptor,
            )
            before = os.fstat(file_descriptor)
            self._validate_file_details(before)
            if before.st_dev != details.st_dev or before.st_ino != details.st_ino:
                raise _InvalidCredentialsFile
            remaining = _MAX_CREDENTIAL_FILE_BYTES + 1
            while remaining > 0:
                chunk = os.read(file_descriptor, min(8192, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            chunks = []
            after = os.fstat(file_descriptor)
            self._validate_file_details(after)
            if (
                len(raw) > _MAX_CREDENTIAL_FILE_BYTES
                or len(raw) != after.st_size
                or not self._same_file_state(before, after)
            ):
                raise _InvalidCredentialsFile
            return raw
        finally:
            chunks = []
            raw = b""
            chunk = b""
            details = None
            before = None
            after = None
            if file_descriptor >= 0:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            try:
                os.close(parent_descriptor)
            except OSError:
                pass

    @staticmethod
    def _decode_payload(raw: Any) -> SenseCoreCredentials:
        invalid = False
        text = ""
        data: Any = None
        username: Any = None
        password: Any = None
        decoded: Optional[SenseCoreCredentials] = None
        if isinstance(raw, bytes):
            try:
                text = raw.decode("utf-8")
            except Exception:  # noqa: BLE001 - replace after clearing raw data
                invalid = True
        else:
            invalid = True

        if not invalid:
            try:
                data = json.loads(
                    text,
                    object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant,
                )
            except Exception:  # noqa: BLE001 - JSON errors retain their document
                invalid = True

        valid_payload = False
        if not invalid and isinstance(data, dict) and set(data) == _PAYLOAD_KEYS:
            username = data.get("username")
            password = data.get("password")
            valid_payload = (
                type(data.get("version")) is int
                and data.get("version") == _PAYLOAD_VERSION
                and isinstance(username, str)
                and isinstance(password, str)
                and bool(username.strip())
                and bool(password)
            )
            if valid_payload:
                try:
                    decoded = SenseCoreCredentials(username=username, password=password)
                except Exception:  # noqa: BLE001 - scrub parsed values below
                    invalid = True

        raw = None
        text = ""
        data = None
        username = None
        password = None
        if valid_payload and not invalid and decoded is not None:
            return decoded
        decoded = None
        raise _InvalidCredentialsFile

    def _write_payload(self, payload: bytes) -> None:
        parent_descriptor = self._open_parent(create=True)
        file_descriptor = -1
        temporary_name = ""
        renamed = False
        try:
            existing = self._file_details(parent_descriptor)
            existing = None
            for _attempt in range(100):
                temporary_name = ".credentials.tmp-" + os.urandom(16).hex()
                try:
                    file_descriptor = os.open(
                        temporary_name,
                        self._file_open_flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL),
                        0o600,
                        dir_fd=parent_descriptor,
                    )
                    break
                except FileExistsError:
                    temporary_name = ""
            if file_descriptor < 0 or not temporary_name:
                raise _InvalidCredentialsFile
            os.fchmod(file_descriptor, 0o600)
            offset = 0
            while offset < len(payload):
                written = os.write(file_descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("short credentials write")
                offset += written
            os.fsync(file_descriptor)
            os.close(file_descriptor)
            file_descriptor = -1
            os.replace(
                temporary_name,
                self._name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            renamed = True
            temporary_name = ""
            details = self._file_details(parent_descriptor)
            if details is None:
                raise _InvalidCredentialsFile
            os.fsync(parent_descriptor)
        finally:
            payload = b""
            if file_descriptor >= 0:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            if temporary_name and not renamed:
                try:
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                except OSError:
                    pass
            try:
                os.close(parent_descriptor)
            except OSError:
                pass

    def save(self, credentials: SenseCoreCredentials) -> None:
        payload = b""
        failed = False
        try:
            payload = _encode_payload(credentials)
            if len(payload) > _MAX_CREDENTIAL_FILE_BYTES:
                raise _InvalidCredentialsFile
            self._write_payload(payload)
        except Exception:  # noqa: BLE001 - replace secret-bearing failures
            failed = True
        credentials = None  # type: ignore[assignment]
        payload = b""
        if failed:
            raise self._error("save")

    def load(self) -> Optional[SenseCoreCredentials]:
        raw: Any = None
        credentials: Optional[SenseCoreCredentials] = None
        failed = False
        missing = False
        try:
            raw = self._read_payload()
            credentials = self._decode_payload(raw)
        except _CredentialsFileMissing:
            missing = True
        except Exception:  # noqa: BLE001 - replace content/path-bearing failures
            failed = True
        raw = None
        if missing:
            credentials = None
            return None
        if failed:
            credentials = None
            raise self._error("load")
        return credentials

    def status(self) -> bool:
        configured = False
        failed = False
        missing = False
        parent_descriptor = -1
        try:
            parent_descriptor = self._open_parent(create=False)
            configured = self._file_details(parent_descriptor) is not None
        except _CredentialsFileMissing:
            missing = True
        except Exception:  # noqa: BLE001 - fixed public error below
            failed = True
        finally:
            if parent_descriptor >= 0:
                try:
                    os.close(parent_descriptor)
                except OSError:
                    failed = True
        if missing:
            return False
        if failed:
            raise self._error("check")
        return configured

    def is_configured(self) -> bool:
        return self.status()

    def _delete_file(self) -> bool:
        parent_descriptor = self._open_parent(create=False)
        try:
            details = self._file_details(parent_descriptor)
            if details is None:
                return False
            details = None
            os.unlink(self._name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
            return True
        finally:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass

    def delete(self) -> bool:
        deleted = False
        failed = False
        missing = False
        try:
            deleted = self._delete_file()
        except _CredentialsFileMissing:
            missing = True
        except Exception:  # noqa: BLE001 - fixed public error below
            failed = True
        if missing:
            return False
        if failed:
            raise self._error("delete")
        return deleted


__all__ = [
    "CredentialStore",
    "CredentialStoreError",
    "FileCredentialStore",
    "SenseCoreCredentials",
    "default_credentials_file",
]
