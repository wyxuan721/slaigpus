"""Strict, race-aware loading for private JSON configuration files.

This module is deliberately read-only.  It is used for configuration that may
contain container environment values, so paths, payloads, and operating-system
errors never cross the public exception boundary.
"""

from __future__ import annotations

import json
import math
import os
import stat
from pathlib import Path
from typing import Any, Iterable, Tuple


DEFAULT_MAX_PRIVATE_JSON_BYTES = 256 * 1024
MAX_PRIVATE_JSON_DEPTH = 64
MAX_PRIVATE_JSON_NODES = 10_000


class PrivateJSONError(RuntimeError):
    """A redacted private-JSON failure safe to show to a caller."""

    def __init__(self, label: str) -> None:
        self.label = _safe_label(label)
        super().__init__(f"could not load {self.label}")

    def __repr__(self) -> str:
        return "<PrivateJSONError>"


class _InvalidPrivateJSON(Exception):
    """Internal marker whose traceback and details are never exposed."""


def _safe_label(value: Any) -> str:
    """Keep even a malformed caller label from weakening error redaction."""
    if not isinstance(value, str):
        return "private JSON"
    selected = value.strip()
    if (
        not selected
        or len(selected) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in selected)
    ):
        return "private JSON"
    return selected


def _current_uid() -> int:
    getter = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    if not callable(getter):
        raise _InvalidPrivateJSON
    try:
        return int(getter())
    except Exception:
        raise _InvalidPrivateJSON


def _directory_open_flags() -> int:
    if any(not hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW")):
        raise _InvalidPrivateJSON
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
    )


def _file_open_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise _InvalidPrivateJSON
    return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _selected_path(value: Any) -> Tuple[Path, Path, str]:
    try:
        selected = Path(value).expanduser()
    except Exception:
        raise _InvalidPrivateJSON
    if (
        not selected.is_absolute()
        or selected.name in ("", ".", "..")
        or ".." in selected.parts
    ):
        raise _InvalidPrivateJSON
    return selected, selected.parent, selected.name


def _selected_limit(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise _InvalidPrivateJSON
    return value


def _validate_parent(details: Any) -> None:
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != _current_uid()
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise _InvalidPrivateJSON


def _validate_file(details: Any, max_bytes: int) -> None:
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != _current_uid()
        or stat.S_IMODE(details.st_mode) != 0o600
        or details.st_nlink != 1
        or details.st_size < 0
        or details.st_size > max_bytes
    ):
        raise _InvalidPrivateJSON


def _time_field(details: Any, nanoseconds: str, seconds: str) -> Any:
    return (
        getattr(details, nanoseconds)
        if hasattr(details, nanoseconds)
        else getattr(details, seconds)
    )


def _file_state(details: Any) -> Tuple[Any, ...]:
    """Return every security- or mutation-relevant field we require stable."""
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_uid,
        details.st_nlink,
        details.st_size,
        _time_field(details, "st_mtime_ns", "st_mtime"),
        _time_field(details, "st_ctime_ns", "st_ctime"),
    )


def _stat_entry(parent_descriptor: int, name: str) -> Any:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except Exception:
        raise _InvalidPrivateJSON


def _read_private_bytes(path: Any, max_bytes: Any) -> bytes:
    selected: Any = None
    parent: Any = None
    name = ""
    parent_descriptor = -1
    file_descriptor = -1
    chunks = []
    raw = b""
    try:
        selected, parent, name = _selected_path(path)
        limit = _selected_limit(max_bytes)
        try:
            parent_descriptor = os.open(parent, _directory_open_flags())
        except Exception:
            raise _InvalidPrivateJSON
        _validate_parent(os.fstat(parent_descriptor))

        path_before = _stat_entry(parent_descriptor, name)
        _validate_file(path_before, limit)
        try:
            file_descriptor = os.open(
                name,
                _file_open_flags(),
                dir_fd=parent_descriptor,
            )
        except Exception:
            raise _InvalidPrivateJSON
        descriptor_before = os.fstat(file_descriptor)
        _validate_file(descriptor_before, limit)
        if _file_state(path_before) != _file_state(descriptor_before):
            raise _InvalidPrivateJSON

        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(file_descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        chunks = []

        descriptor_after = os.fstat(file_descriptor)
        _validate_file(descriptor_after, limit)
        path_after = _stat_entry(parent_descriptor, name)
        _validate_file(path_after, limit)
        if (
            len(raw) > limit
            or len(raw) != descriptor_after.st_size
            or _file_state(descriptor_before) != _file_state(descriptor_after)
            or _file_state(descriptor_before) != _file_state(path_after)
        ):
            raise _InvalidPrivateJSON
        # The directory itself must remain private throughout the operation.
        _validate_parent(os.fstat(parent_descriptor))
        return raw
    except _InvalidPrivateJSON:
        raise
    except Exception:
        raise _InvalidPrivateJSON
    finally:
        selected = None
        parent = None
        name = ""
        chunks = []
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if parent_descriptor >= 0:
            try:
                os.close(parent_descriptor)
            except OSError:
                pass


def _unique_object(pairs: Iterable[Tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            result = {}
            raise _InvalidPrivateJSON
        result[key] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise _InvalidPrivateJSON


def _finite_float(value: str) -> float:
    try:
        result = float(value)
    except Exception:
        raise _InvalidPrivateJSON
    if not math.isfinite(result):
        raise _InvalidPrivateJSON
    return result


def _validate_complexity(value: Any) -> None:
    nodes = 0
    stack = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_PRIVATE_JSON_NODES or depth > MAX_PRIVATE_JSON_DEPTH:
            stack = []
            raise _InvalidPrivateJSON
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)


def _decode_private_json(raw: Any) -> Any:
    text = ""
    value: Any = None
    try:
        if not isinstance(raw, bytes):
            raise _InvalidPrivateJSON
        text = raw.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
        _validate_complexity(value)
        return value
    except _InvalidPrivateJSON:
        value = None
        raise
    except Exception:
        value = None
        raise _InvalidPrivateJSON
    finally:
        raw = None
        text = ""


def load_private_json(
    path: Any,
    *,
    label: str,
    max_bytes: int = DEFAULT_MAX_PRIVATE_JSON_BYTES,
) -> Any:
    """Load one private JSON document or raise a completely redacted error.

    The immediate parent must be a real, current-user-owned ``0700`` directory.
    The document must be a current-user-owned ``0600`` regular file with one
    hard link.  Both are opened without following the final path component.
    """

    public_label = _safe_label(label)
    raw: Any = None
    result: Any = None
    failed = False
    try:
        raw = _read_private_bytes(path, max_bytes)
        result = _decode_private_json(raw)
    except Exception:  # noqa: BLE001 - replace all path/content-bearing failures
        failed = True
    raw = None
    if failed:
        result = None
        # This raise intentionally occurs outside an active exception handler,
        # so ``__context__`` is genuinely None rather than merely suppressed.
        raise PrivateJSONError(public_label)
    return result
