from __future__ import annotations

import os
from pathlib import Path
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest


def test_exclusive_run_lock_fails_fast_and_does_not_delete_active_lock(tmp_path):
    from src.job_collection.security import ExclusiveRunLock, LockUnavailable

    first = ExclusiveRunLock(tmp_path / "locks", "run-001", "commit")
    second = ExclusiveRunLock(tmp_path / "locks", "run-001", "commit")

    with first:
        lock_path = first.path
        with pytest.raises(LockUnavailable, match="already claimed"):
            with second:
                pass
        assert lock_path.exists()
    assert not lock_path.exists()


def test_collect_and_commit_share_one_run_lock(tmp_path):
    from src.job_collection.security import ExclusiveRunLock, LockUnavailable

    collect = ExclusiveRunLock(tmp_path / "locks", "run-001", "collect")
    commit = ExclusiveRunLock(tmp_path / "locks", "run-001", "commit")

    with collect:
        with pytest.raises(LockUnavailable, match="already claimed"):
            with commit:
                pass


def test_attestation_key_is_created_once_and_reused_without_environment(tmp_path):
    from src.job_collection.security import load_or_create_attestation_key

    root = tmp_path / "keys"
    first = load_or_create_attestation_key(root=root)
    second = load_or_create_attestation_key(root=root)

    assert len(first) == 32
    assert second == first
    stored = (root / "attestation.key").read_bytes()
    if os.name == "nt":
        assert stored != first
        assert stored.startswith(b"JCK1DPAPI\x00")
    else:
        assert stored == first
        assert (root / "attestation.key").stat().st_mode & 0o077 == 0


def test_windows_attestation_key_is_dpapi_protected_and_acl_verified(
    tmp_path, monkeypatch
):
    import src.job_collection.security as security

    protected = []
    verified = []
    monkeypatch.setattr(security, "_WINDOWS", True)
    monkeypatch.setattr(
        security,
        "_protect_windows_key",
        lambda value: b"ciphertext:" + value[::-1],
    )
    monkeypatch.setattr(
        security,
        "_unprotect_windows_key",
        lambda value: value.removeprefix(b"ciphertext:")[::-1],
    )
    monkeypatch.setattr(
        security,
        "_restrict_windows_path",
        lambda path, *, is_directory: protected.append((Path(path), is_directory)),
    )
    monkeypatch.setattr(
        security,
        "_verify_windows_path_acl",
        lambda path: verified.append(Path(path)),
    )

    root = tmp_path / "keys"
    first = security.load_or_create_attestation_key(root=root)
    second = security.load_or_create_attestation_key(root=root)

    stored = (root / "attestation.key").read_bytes()
    assert first == second
    assert stored.startswith(b"JCK1DPAPI\x00ciphertext:")
    assert stored != first
    assert (root, True) in protected
    assert (root / "attestation.key", False) in protected
    assert root in verified
    assert root / "attestation.key" in verified


def test_windows_key_provisioning_fails_closed_when_acl_cannot_be_protected(
    tmp_path, monkeypatch
):
    import src.job_collection.security as security

    monkeypatch.setattr(security, "_WINDOWS", True)
    monkeypatch.setattr(security, "_protect_windows_key", lambda value: b"cipher")
    monkeypatch.setattr(
        security,
        "_restrict_windows_path",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            security.UnsafeArtifact("Windows ACL protection failed")
        ),
    )

    with pytest.raises(security.UnsafeArtifact, match="ACL"):
        security.load_or_create_attestation_key(root=tmp_path / "keys")


def test_windows_provision_secure_directory_tightens_existing_root(
    tmp_path, monkeypatch
):
    import src.job_collection.security as security

    root = tmp_path / "data" / "collections"
    root.mkdir(parents=True)
    protected = []
    verified = []
    monkeypatch.setattr(security, "_WINDOWS", True)
    monkeypatch.setattr(
        security,
        "_restrict_windows_path",
        lambda path, *, is_directory: protected.append((Path(path), is_directory)),
    )
    monkeypatch.setattr(
        security,
        "_verify_windows_path_acl",
        lambda path: verified.append(Path(path)),
    )

    result = security.provision_secure_directory(root)

    assert result == root.resolve()
    assert protected == [(root.resolve(), True)]
    assert verified == [root.resolve()]


def test_provision_secure_directory_rejects_existing_symlink(tmp_path):
    from src.job_collection.security import UnsafeArtifact, provision_secure_directory

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "collections"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(UnsafeArtifact, match="symlink|reparse"):
        provision_secure_directory(link)


def test_exclusive_lock_detects_parent_replacement_during_create(
    tmp_path, monkeypatch
):
    import src.job_collection.security as security

    root = tmp_path / "locks"
    root.mkdir()
    real_verify = security._verify_directories

    def inject_ancestor_race(expected, *, phase):
        if phase == "creating run lock":
            raise security.UnsafeArtifact("artifact ancestor changed while creating")
        return real_verify(expected, phase=phase)

    monkeypatch.setattr(security, "_verify_directories", inject_ancestor_race)
    monkeypatch.setattr(security, "_restrict_windows_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(security, "_verify_windows_path_acl", lambda *args: None)

    lock = security.ExclusiveRunLock(root, "run-race", "collect")
    with pytest.raises(security.UnsafeArtifact, match="ancestor changed"):
        lock.acquire()


def test_secure_atomic_write_detects_parent_replacement_after_replace(
    tmp_path, monkeypatch
):
    import src.job_collection.security as security

    root = tmp_path / "run"
    parent = root / "staged"
    parent.mkdir(parents=True)
    replacement = root / "replacement"
    replacement.mkdir()
    displaced = root / "displaced"
    target = parent / "jobs.jsonl"
    real_replace = security.os.replace
    swapped = False

    def racing_replace(source, destination):
        nonlocal swapped
        real_replace(source, destination)
        if Path(destination) == target and not swapped:
            swapped = True
            real_replace(parent, displaced)
            real_replace(replacement, parent)

    monkeypatch.setattr(security.os, "replace", racing_replace)
    monkeypatch.setattr(security, "_restrict_windows_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(security, "_verify_windows_path_acl", lambda *args: None)

    with pytest.raises(security.UnsafeArtifact, match="ancestor changed"):
        security.secure_atomic_write(target, b"bounded", root=root)
    assert list(parent.glob(".jobs.jsonl.*")) == []


def test_concurrent_attestation_key_creation_reuses_single_complete_key(
    tmp_path, monkeypatch
):
    import src.job_collection.security as security

    root = tmp_path / "keys"
    write_started = Event()
    loser_read = Event()
    allow_write = Event()
    real_write = security.os.write
    real_secure_read = security.secure_read_file
    delayed = False

    def delayed_first_write(descriptor, content):
        nonlocal delayed
        if not delayed and len(content) >= 32:
            delayed = True
            write_started.set()
            assert allow_write.wait(timeout=5)
        return real_write(descriptor, content)

    def observed_secure_read(path, **kwargs):
        if Path(path).exists() and Path(path).stat().st_size == 0:
            loser_read.set()
            raise security.UnsafeArtifact("attestation key is incomplete")
        return real_secure_read(path, **kwargs)

    monkeypatch.setattr(security.os, "write", delayed_first_write)
    monkeypatch.setattr(security, "secure_read_file", observed_secure_read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(security.load_or_create_attestation_key, root=root)
        assert write_started.wait(timeout=5)
        second = executor.submit(security.load_or_create_attestation_key, root=root)
        loser_observed = loser_read.wait(timeout=5)
        allow_write.set()
        assert loser_observed
        keys = [first.result(timeout=5), second.result(timeout=5)]

    assert len(keys[0]) == 32
    assert keys[1] == keys[0]


def test_attestation_key_fails_closed_when_key_path_is_symlink(tmp_path):
    from src.job_collection.security import UnsafeArtifact, load_or_create_attestation_key

    root = tmp_path / "keys"
    root.mkdir()
    target = tmp_path / "outside.key"
    target.write_bytes(b"x" * 32)
    try:
        (root / "attestation.key").symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(UnsafeArtifact, match="symlink|reparse"):
        load_or_create_attestation_key(root=root)


def _make_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip(f"junction creation unavailable: {result.stderr.strip()}")
        return
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")


def test_attestation_key_rejects_reparse_grandparent(tmp_path):
    from src.job_collection.security import UnsafeArtifact, load_or_create_attestation_key

    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    link = tmp_path / "linked-parent"
    _make_directory_link(link, outside)
    try:
        with pytest.raises(UnsafeArtifact, match="parent|symlink|reparse"):
            load_or_create_attestation_key(root=link / "nested" / "keys")
    finally:
        if os.name == "nt" and link.exists():
            os.rmdir(link)


def test_secure_read_rejects_symlink(tmp_path):
    from src.job_collection.security import UnsafeArtifact, secure_read_file

    root = tmp_path / "run"
    root.mkdir()
    target = root / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = root / "report.json"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(UnsafeArtifact, match="symlink|reparse"):
        secure_read_file(link, root=root, max_bytes=100)


def test_secure_read_detects_replacement_between_inspection_and_open(
    tmp_path, monkeypatch
):
    import src.job_collection.security as security

    root = tmp_path / "run"
    root.mkdir()
    target = root / "report.json"
    replacement = root / "replacement.json"
    displaced = root / "displaced.json"
    target.write_bytes(b'{"original":true}')
    replacement.write_bytes(b'{"replacement":true}')
    real_open = os.open
    replaced = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if Path(path) == target and not replaced:
            replaced = True
            os.replace(target, displaced)
            os.replace(replacement, target)
            path = displaced
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(security.os, "open", racing_open)

    with pytest.raises(security.UnsafeArtifact, match="changed while opening"):
        security.secure_read_file(target, root=root, max_bytes=100)


def test_secure_read_rejects_symlinked_parent(tmp_path):
    from src.job_collection.security import UnsafeArtifact, secure_read_file

    root = tmp_path / "run"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "report.json").write_text("{}", encoding="utf-8")
    try:
        (root / "nested").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")

    with pytest.raises(UnsafeArtifact, match="parent|symlink|reparse"):
        secure_read_file(root / "nested" / "report.json", root=root, max_bytes=100)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_secure_read_rejects_windows_junction_parent(tmp_path):
    from src.job_collection.security import UnsafeArtifact, secure_read_file

    root = tmp_path / "run"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "report.json").write_text("{}", encoding="utf-8")
    junction = root / "nested"
    _make_directory_link(junction, outside)
    try:
        with pytest.raises(UnsafeArtifact, match="parent|reparse"):
            secure_read_file(
                junction / "report.json", root=root, max_bytes=100
            )
    finally:
        if junction.exists():
            os.rmdir(junction)


def test_secure_read_detects_ancestor_replacement_during_open(tmp_path, monkeypatch):
    import src.job_collection.security as security

    root = tmp_path / "run"
    parent = root / "nested"
    replacement = root / "replacement"
    parent.mkdir(parents=True)
    replacement.mkdir()
    target = parent / "report.json"
    target.write_text("{}", encoding="utf-8")
    (replacement / "report.json").write_text('{"forged":true}', encoding="utf-8")
    displaced = root / "displaced"
    real_open = os.open
    replaced = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        if Path(path) == target and not replaced:
            replaced = True
            os.replace(parent, displaced)
            os.replace(replacement, parent)
            path = displaced / "report.json"
        if dir_fd is None:
            return real_open(path, flags, mode)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(security.os, "open", racing_open)

    with pytest.raises(security.UnsafeArtifact, match="parent|ancestor|changed"):
        security.secure_read_file(target, root=root, max_bytes=100)
