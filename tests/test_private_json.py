"""Security and parsing tests for private JSON configuration files."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import slaigpus.private_json as private_json  # noqa: E402
from slaigpus.private_json import PrivateJSONError, load_private_json  # noqa: E402


LABEL = "ACP worker configuration"


def _private_document(tmp_path: Path, payload: bytes) -> Path:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    path = parent / "worker.json"
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def _assert_redacted(error: PrivateJSONError, *forbidden: str) -> None:
    rendered = str(error) + repr(error)
    assert str(error) == f"could not load {LABEL}"
    assert repr(error) == "<PrivateJSONError>"
    assert error.__context__ is None
    assert error.__cause__ is None
    for value in forbidden:
        assert value not in rendered


def test_loads_valid_nested_strict_utf8_json(tmp_path):
    payload = {
        "version": 1,
        "replicas": 2,
        "mounts": [],
        "env": [{"name": "问候", "value": "你好\nworld"}],
        "enabled": True,
        "nothing": None,
        "ratio": 1.25,
    }
    path = _private_document(
        tmp_path,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )

    assert load_private_json(path, label=LABEL) == payload


def test_expanduser_is_applied_but_relative_paths_are_rejected(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    home.chmod(0o700)
    path = home / "worker.json"
    path.write_text('{"ok":true}', encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("HOME", str(home))

    assert load_private_json(Path("~/worker.json"), label=LABEL) == {"ok": True}

    with pytest.raises(PrivateJSONError) as captured:
        load_private_json(Path("worker.json"), label=LABEL)
    _assert_redacted(captured.value, "worker.json")


@pytest.mark.parametrize("parent_mode", [0o755, 0o750, 0o770])
def test_rejects_nonprivate_parent_permissions(tmp_path, parent_mode):
    path = _private_document(tmp_path, b"{}")
    path.parent.chmod(parent_mode)

    with pytest.raises(PrivateJSONError) as captured:
        load_private_json(path, label=LABEL)
    _assert_redacted(captured.value, str(path))


def test_rejects_parent_not_owned_by_current_user(tmp_path, monkeypatch):
    path = _private_document(tmp_path, b"{}")
    actual_uid = os.stat(path).st_uid
    monkeypatch.setattr(private_json, "_current_uid", lambda: actual_uid + 1)

    with pytest.raises(PrivateJSONError) as captured:
        load_private_json(path, label=LABEL)
    _assert_redacted(captured.value, str(path))


def test_rejects_file_not_owned_by_current_user(tmp_path, monkeypatch):
    path = _private_document(tmp_path, b"{}")
    actual_uid = os.stat(path).st_uid
    monkeypatch.setattr(private_json, "_validate_parent", lambda _details: None)
    monkeypatch.setattr(private_json, "_current_uid", lambda: actual_uid + 1)

    with pytest.raises(PrivateJSONError) as captured:
        load_private_json(path, label=LABEL)
    _assert_redacted(captured.value, str(path))


def test_rejects_symlink_parent_and_symlink_file(tmp_path):
    path = _private_document(tmp_path, b"{}")
    linked_parent = tmp_path / "linked-private"
    linked_parent.symlink_to(path.parent, target_is_directory=True)

    with pytest.raises(PrivateJSONError):
        load_private_json(linked_parent / path.name, label=LABEL)

    link = path.parent / "linked.json"
    link.symlink_to(path)
    with pytest.raises(PrivateJSONError):
        load_private_json(link, label=LABEL)


def test_rejects_hardlinks_nonregular_files_and_wrong_file_mode(tmp_path):
    path = _private_document(tmp_path, b"{}")
    hardlink = path.parent / "hardlink.json"
    os.link(path, hardlink)
    with pytest.raises(PrivateJSONError):
        load_private_json(path, label=LABEL)
    hardlink.unlink()

    path.chmod(0o640)
    with pytest.raises(PrivateJSONError):
        load_private_json(path, label=LABEL)

    path.unlink()
    path.mkdir(mode=0o600)
    with pytest.raises(PrivateJSONError):
        load_private_json(path, label=LABEL)


def test_rejects_payload_larger_than_the_selected_bound(tmp_path):
    path = _private_document(tmp_path, b'{"value":"123456789"}')

    with pytest.raises(PrivateJSONError) as captured:
        load_private_json(path, label=LABEL, max_bytes=8)
    _assert_redacted(captured.value, "123456789", str(path))


@pytest.mark.parametrize("limit", [None, True, 0, -1, 1.5, "100"])
def test_rejects_invalid_bounds_without_reading(tmp_path, limit):
    path = _private_document(tmp_path, b"{}")

    with pytest.raises(PrivateJSONError):
        load_private_json(path, label=LABEL, max_bytes=limit)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"outer":{"same":1,"same":2}}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":1e9999}',
        b'{"broken":',
        b'"\xff"',
    ],
)
def test_rejects_duplicate_nonfinite_malformed_and_non_utf8_json(tmp_path, payload):
    path = _private_document(tmp_path, payload)

    with pytest.raises(PrivateJSONError) as captured:
        load_private_json(path, label=LABEL)
    _assert_redacted(captured.value, str(path), payload.decode("latin-1"))


def test_rejects_excessive_depth_and_node_count(tmp_path):
    nested = 0
    for _ in range(private_json.MAX_PRIVATE_JSON_DEPTH + 1):
        nested = [nested]
    path = _private_document(tmp_path, json.dumps(nested).encode("utf-8"))
    with pytest.raises(PrivateJSONError):
        load_private_json(path, label=LABEL)

    path.write_text(
        json.dumps([0] * private_json.MAX_PRIVATE_JSON_NODES),
        encoding="utf-8",
    )
    path.chmod(0o600)
    with pytest.raises(PrivateJSONError):
        load_private_json(path, label=LABEL)


def test_rejects_in_place_mutation_during_read(tmp_path, monkeypatch):
    path = _private_document(tmp_path, b'{"value":1}')
    real_read = private_json.os.read
    changed = False

    def mutating_read(descriptor, count):
        nonlocal changed
        data = real_read(descriptor, count)
        if data and not changed:
            changed = True
            path.write_bytes(b'{"value":2}')
            path.chmod(0o600)
        return data

    monkeypatch.setattr(private_json.os, "read", mutating_read)

    with pytest.raises(PrivateJSONError):
        load_private_json(path, label=LABEL)


def test_rejects_atomic_path_replacement_during_read(tmp_path, monkeypatch):
    path = _private_document(tmp_path, b'{"value":1}')
    replacement = path.parent / "replacement.json"
    replacement.write_bytes(b'{"value":2}')
    replacement.chmod(0o600)
    real_read = private_json.os.read
    replaced = False

    def replacing_read(descriptor, count):
        nonlocal replaced
        data = real_read(descriptor, count)
        if data and not replaced:
            replaced = True
            os.replace(replacement, path)
        return data

    monkeypatch.setattr(private_json.os, "read", replacing_read)

    with pytest.raises(PrivateJSONError):
        load_private_json(path, label=LABEL)


def test_os_and_payload_errors_are_fully_redacted(tmp_path, monkeypatch):
    sentinel = "underlying-secret-sentinel"
    path = _private_document(tmp_path, sentinel.encode("utf-8"))

    with pytest.raises(PrivateJSONError) as payload_error:
        load_private_json(path, label=LABEL)
    _assert_redacted(payload_error.value, sentinel, str(path))

    def fail_open(*_args, **_kwargs):
        raise OSError(sentinel)

    monkeypatch.setattr(private_json.os, "open", fail_open)
    with pytest.raises(PrivateJSONError) as os_error:
        load_private_json(path, label=LABEL)
    _assert_redacted(os_error.value, sentinel, str(path))


def test_malformed_label_cannot_inject_control_text_into_error(tmp_path):
    path = _private_document(tmp_path, b"not-json")

    with pytest.raises(PrivateJSONError) as captured:
        load_private_json(path, label="secret\npath")

    assert str(captured.value) == "could not load private JSON"
    assert repr(captured.value) == "<PrivateJSONError>"
    assert captured.value.__context__ is None
