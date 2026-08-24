"""Security tests for the private file credential backend."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import slaigpus.credentials as credentials_module  # noqa: E402
from slaigpus.credentials import (  # noqa: E402
    CredentialStoreError,
    FileCredentialStore,
    SenseCoreCredentials,
    default_credentials_file,
)


USERNAME = "username-traceback-sentinel"
PASSWORD = "password-traceback-sentinel#$"


def _secret_payload(*, version=1):
    return json.dumps(
        {"version": version, "username": USERNAME, "password": PASSWORD},
        separators=(",", ":"),
    ).encode("ascii")


def _private_file_store(tmp_path):
    parent = tmp_path / "private-credentials"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    path = parent / "credentials.json"
    return FileCredentialStore(path), path


def _write_private(path, payload):
    path.write_bytes(payload)
    path.chmod(0o600)


def _save_secret_sentinels(store):
    store.save(SenseCoreCredentials(USERNAME, PASSWORD))


def _assert_traceback_locals_are_redacted(error):
    """Inspect every retained frame exactly as a traceback consumer could."""

    username_bytes = USERNAME.encode("ascii")
    password_bytes = PASSWORD.encode("ascii")
    traceback = error.__traceback__
    assert traceback is not None
    while traceback is not None:
        frame = traceback.tb_frame
        for local_name, value in list(frame.f_locals.items()):
            try:
                rendered = repr(value)
            except Exception:  # pragma: no cover - diagnostic isolation
                rendered = "<unrepresentable>"
            location = f"{frame.f_code.co_name}.{local_name}"
            assert USERNAME not in rendered, location
            assert PASSWORD not in rendered, location
            if isinstance(value, (bytes, bytearray, memoryview)):
                raw_value = bytes(value)
                assert username_bytes not in raw_value, location
                assert password_bytes not in raw_value, location
        traceback = traceback.tb_next


def test_module_exports_only_generic_file_credential_types():
    assert credentials_module.__all__ == [
        "CredentialStore",
        "CredentialStoreError",
        "FileCredentialStore",
        "SenseCoreCredentials",
        "default_credentials_file",
    ]


def test_credentials_and_store_reprs_are_permanently_redacted(tmp_path):
    credentials = SenseCoreCredentials(USERNAME, PASSWORD)
    path = tmp_path / "path-must-not-appear" / "credentials.json"
    store = FileCredentialStore(path)

    assert repr(credentials) == "<SenseCoreCredentials redacted>"
    assert str(credentials) == "<SenseCoreCredentials redacted>"
    assert USERNAME not in repr(credentials)
    assert PASSWORD not in repr(credentials)
    assert repr(store) == "<FileCredentialStore>"
    assert str(path) not in repr(store)


def test_default_credentials_file_uses_xdg_or_private_home_config(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    expected = tmp_path / ".config" / "slaigpus" / "credentials.json"
    assert default_credentials_file() == expected

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert default_credentials_file() == xdg / "slaigpus" / "credentials.json"

    for invalid_xdg in ("relative-config", "~/.private-config", "~other/config"):
        monkeypatch.setenv("XDG_CONFIG_HOME", invalid_xdg)
        assert default_credentials_file() == expected


def test_default_store_uses_the_unified_config_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    FileCredentialStore().save(SenseCoreCredentials("user", "password"))

    assert (tmp_path / ".config" / "slaigpus" / "credentials.json").is_file()


def test_file_store_round_trip_is_private_atomic_and_exact_json(tmp_path):
    parent = tmp_path / "created-by-store"
    path = parent / "credentials.json"
    store = FileCredentialStore(path)

    store.save(SenseCoreCredentials(USERNAME, PASSWORD))

    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    details = path.stat()
    assert stat.S_ISREG(details.st_mode)
    assert stat.S_IMODE(details.st_mode) == 0o600
    assert details.st_uid == os.geteuid()
    assert details.st_nlink == 1
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "username": USERNAME,
        "password": PASSWORD,
    }
    assert store.status() is True
    assert store.is_configured() is True
    assert store.load() == SenseCoreCredentials(USERNAME, PASSWORD)
    assert list(parent.glob(".credentials.tmp-*")) == []

    first_inode = details.st_ino
    store.save(SenseCoreCredentials("replacement-user", "replacement-password"))
    assert path.stat().st_ino != first_inode
    assert store.load() == SenseCoreCredentials(
        "replacement-user", "replacement-password"
    )
    assert list(parent.glob(".credentials.tmp-*")) == []


def test_file_store_enforces_modes_even_under_restrictive_umask(tmp_path):
    parent = tmp_path / "umask-created"
    path = parent / "credentials.json"
    store = FileCredentialStore(path)
    previous = os.umask(0o777)
    try:
        store.save(SenseCoreCredentials("user", "password"))
    finally:
        os.umask(previous)

    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "credentials",
    [
        SenseCoreCredentials("", "password"),
        SenseCoreCredentials("   ", "password"),
        SenseCoreCredentials("username", ""),
    ],
)
def test_file_store_rejects_empty_credentials_without_creating_file(
    credentials, tmp_path
):
    store = FileCredentialStore(tmp_path / "new" / "credentials.json")
    with pytest.raises(CredentialStoreError, match="could not save"):
        store.save(credentials)
    assert not (tmp_path / "new").exists()


def test_file_store_missing_is_unconfigured_and_delete_is_idempotent(tmp_path):
    store = FileCredentialStore(tmp_path / "missing" / "credentials.json")
    assert store.status() is False
    assert store.load() is None
    assert store.delete() is False

    configured, path = _private_file_store(tmp_path)
    configured.save(SenseCoreCredentials("user", "password"))
    assert configured.delete() is True
    assert not path.exists()
    assert configured.delete() is False
    assert configured.status() is False


def test_file_status_checks_only_metadata_and_never_reads_payload(
    monkeypatch, tmp_path
):
    store, path = _private_file_store(tmp_path)
    _write_private(path, b"not-json-and-must-not-be-read")
    monkeypatch.setattr(
        "slaigpus.credentials.os.read",
        lambda *_args, **_kwargs: pytest.fail("status must not read secret bytes"),
    )
    assert store.status() is True


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"\xef\xbb\xbf{\"version\":1,\"username\":\"u\",\"password\":\"p\"}",
        b'{"version":true,"username":"u","password":"p"}',
        b'{"version":2,"username":"u","password":"p"}',
        b'{"version":1,"username":"","password":"p"}',
        b'{"version":1,"username":"u","password":""}',
        b'{"version":1,"username":"u","password":"p","extra":1}',
        b'{"version":1,"version":1,"username":"u","password":"p"}',
        b'{"version":1,"username":"u","password":"p","value":NaN}',
        b"\xff",
        b"x" * (16 * 1024 + 1),
    ],
)
def test_file_load_rejects_non_strict_or_oversized_payload(payload, tmp_path):
    store, path = _private_file_store(tmp_path)
    _write_private(path, payload)
    with pytest.raises(CredentialStoreError) as captured:
        store.load()
    assert str(captured.value) == "could not load SenseCore credentials file"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize("operation", ["status", "load", "delete", "save"])
def test_file_store_rejects_insecure_file_mode(operation, tmp_path):
    store, path = _private_file_store(tmp_path)
    _write_private(path, b'{"version":1,"username":"u","password":"p"}')
    path.chmod(0o644)
    with pytest.raises(CredentialStoreError) as captured:
        if operation == "save":
            store.save(SenseCoreCredentials("new-user", "new-password"))
        else:
            getattr(store, operation)()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_file_store_rejects_insecure_parent_and_parent_symlink(tmp_path):
    parent = tmp_path / "public-parent"
    parent.mkdir(mode=0o700)
    parent.chmod(0o755)
    with pytest.raises(CredentialStoreError, match="could not save"):
        FileCredentialStore(parent / "credentials.json").save(
            SenseCoreCredentials("user", "password")
        )

    real_parent = tmp_path / "real-private-parent"
    real_parent.mkdir(mode=0o700)
    real_parent.chmod(0o700)
    linked_parent = tmp_path / "linked-private-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(CredentialStoreError, match="could not save"):
        FileCredentialStore(linked_parent / "credentials.json").save(
            SenseCoreCredentials("user", "password")
        )
    assert list(real_parent.iterdir()) == []


def test_file_store_rejects_metadata_not_owned_by_current_uid(
    monkeypatch, tmp_path
):
    store, path = _private_file_store(tmp_path)
    _write_private(path, b'{"version":1,"username":"u","password":"p"}')
    monkeypatch.setattr(
        FileCredentialStore,
        "_current_uid",
        staticmethod(lambda: os.geteuid() + 1),
    )
    with pytest.raises(CredentialStoreError, match="could not check"):
        store.status()


def test_file_store_rejects_oversized_save_without_partial_file(tmp_path):
    store, path = _private_file_store(tmp_path)
    with pytest.raises(CredentialStoreError, match="could not save"):
        store.save(SenseCoreCredentials("user", "x" * (16 * 1024 + 1)))
    assert not path.exists()
    assert list(path.parent.glob(".credentials.tmp-*")) == []


def test_file_store_rejects_symlink_fifo_directory_and_hardlink(tmp_path):
    parent = tmp_path / "private-types"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)

    target = parent / "target"
    _write_private(target, b"target-must-remain")
    symlink = parent / "symlink.json"
    symlink.symlink_to(target)
    symlink_store = FileCredentialStore(symlink)
    with pytest.raises(CredentialStoreError, match="could not check"):
        symlink_store.status()
    with pytest.raises(CredentialStoreError, match="could not delete"):
        symlink_store.delete()
    assert target.read_bytes() == b"target-must-remain"

    fifo = parent / "fifo.json"
    os.mkfifo(fifo, mode=0o600)
    fifo.chmod(0o600)
    with pytest.raises(CredentialStoreError, match="could not check"):
        FileCredentialStore(fifo).status()

    directory = parent / "directory.json"
    directory.mkdir(mode=0o700)
    with pytest.raises(CredentialStoreError, match="could not check"):
        FileCredentialStore(directory).status()

    source = parent / "hardlink-source.json"
    _write_private(source, b'{"version":1,"username":"u","password":"p"}')
    hardlink = parent / "hardlink.json"
    os.link(source, hardlink)
    with pytest.raises(CredentialStoreError, match="could not check"):
        FileCredentialStore(hardlink).status()


def test_file_save_failure_is_atomic_cleans_temp_and_redacts_traceback(
    monkeypatch, tmp_path
):
    store, path = _private_file_store(tmp_path)
    store.save(SenseCoreCredentials("old-user", "old-password"))
    original = path.read_bytes()

    def fail_replace(*_args, **_kwargs):
        raise RuntimeError(f"{USERNAME}:{PASSWORD}")

    monkeypatch.setattr("slaigpus.credentials.os.replace", fail_replace)
    with pytest.raises(CredentialStoreError) as captured:
        _save_secret_sentinels(store)
    assert str(captured.value) == "could not save SenseCore credentials file"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    _assert_traceback_locals_are_redacted(captured.value)
    assert path.read_bytes() == original
    assert list(path.parent.glob(".credentials.tmp-*")) == []


def test_file_encoding_failure_is_context_free_and_redacted(monkeypatch, tmp_path):
    store, path = _private_file_store(tmp_path)

    def fail_encoding(*_args, **_kwargs):
        raise RuntimeError(f"{USERNAME}:{PASSWORD}")

    monkeypatch.setattr("slaigpus.credentials.json.dumps", fail_encoding)
    with pytest.raises(CredentialStoreError) as captured:
        _save_secret_sentinels(store)
    assert str(captured.value) == "could not save SenseCore credentials file"
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    _assert_traceback_locals_are_redacted(captured.value)
    assert not path.exists()


def test_file_invalid_secret_payload_failure_has_no_secret_traceback_locals(tmp_path):
    store, path = _private_file_store(tmp_path)
    _write_private(path, _secret_payload(version=2))
    with pytest.raises(CredentialStoreError) as captured:
        store.load()
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    _assert_traceback_locals_are_redacted(captured.value)


@pytest.mark.parametrize(
    "path",
    [
        Path("relative-secret-path/credentials.json"),
        Path("relative-secret-path/../credentials.json"),
    ],
)
def test_file_store_rejects_relative_or_parent_traversal_path_without_disclosing_it(
    path,
):
    store = FileCredentialStore(path)
    with pytest.raises(CredentialStoreError) as captured:
        store.status()
    assert str(captured.value) == "could not check SenseCore credentials file"
    assert "relative-secret-path" not in str(captured.value)
