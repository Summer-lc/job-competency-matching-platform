from __future__ import annotations

import csv
import ctypes
from ctypes import wintypes
from functools import lru_cache
import hashlib
import io
import os
import re
import secrets
import stat
import subprocess
import time
from pathlib import Path

from src.job_collection.storage import StorageError


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_WINDOWS = os.name == "nt"
_DPAPI_MAGIC = b"JCK1DPAPI\x00"
_MAX_STORED_KEY_BYTES = 4096
_CONTROL_STATE_APP = "JobCompetencyMatching"


class UnsafeArtifact(StorageError):
    """A file failed identity, type, path, or size validation."""


class LockUnavailable(StorageError):
    """An active run claim already owns the requested operation."""


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _is_reparse(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & _REPARSE_FLAG)


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(path))


def default_control_state_root(collections_root: str | Path) -> Path:
    collections = _absolute(collections_root)
    workspace = (
        collections.parent.parent
        if collections.name.lower() == "collections"
        and collections.parent.name.lower() == "data"
        else collections.parent
    )
    canonical = os.path.normcase(str(workspace)).encode("utf-8")
    namespace = hashlib.sha256(canonical).hexdigest()[:24]
    if _WINDOWS:
        local_state = os.environ.get("LOCALAPPDATA")
        if not local_state or not Path(local_state).is_absolute():
            raise UnsafeArtifact("LOCALAPPDATA is required for protected control state")
        base = _absolute(local_state)
    else:
        configured = os.environ.get("XDG_STATE_HOME")
        base = _absolute(configured) if configured else Path.home() / ".local" / "state"
    return base / _CONTROL_STATE_APP / "job_collection" / namespace


def _parent_chain(root: Path, target: Path) -> tuple[Path, ...]:
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise UnsafeArtifact(f"artifact escapes trusted root: {target}") from exc
    chain = [root]
    current = root
    for part in relative.parts[:-1]:
        current /= part
        chain.append(current)
    return tuple(chain)


def _absolute_directory_chain(path: Path) -> tuple[Path, ...]:
    chain: list[Path] = []
    current = path
    while True:
        chain.append(current)
        if current.parent == current:
            break
        current = current.parent
    chain.reverse()
    return tuple(chain)


def _inspect_directories(paths: tuple[Path, ...]) -> dict[Path, tuple[int, int]]:
    identities: dict[Path, tuple[int, int]] = {}
    for path in paths:
        try:
            value = os.lstat(path)
        except OSError as exc:
            raise UnsafeArtifact(f"artifact parent cannot be inspected: {path}") from exc
        if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
            raise UnsafeArtifact(f"artifact parent is a symlink or reparse point: {path}")
        if not stat.S_ISDIR(value.st_mode):
            raise UnsafeArtifact(f"artifact parent is not a directory: {path}")
        identities[path] = (value.st_dev, value.st_ino)
    return identities


def _verify_directories(
    expected: dict[Path, tuple[int, int]], *, phase: str
) -> None:
    current = _inspect_directories(tuple(expected))
    if current != expected:
        raise UnsafeArtifact(f"artifact ancestor changed while {phase}")


def _open_relative_no_follow(target: Path, root: Path) -> int:
    supports_dir_fd = os.open in getattr(os, "supports_dir_fd", set())
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    binary = getattr(os, "O_BINARY", 0)
    if not supports_dir_fd or not directory_flag:
        return os.open(target, os.O_RDONLY | binary | no_follow)

    relative = target.relative_to(root)
    descriptor = os.open(root, os.O_RDONLY | directory_flag | no_follow)
    try:
        for part in relative.parts[:-1]:
            child = os.open(
                part,
                os.O_RDONLY | directory_flag | no_follow,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return os.open(
            relative.parts[-1], os.O_RDONLY | binary | no_follow, dir_fd=descriptor
        )
    finally:
        os.close(descriptor)


def secure_read_file(path: str | Path, *, root: str | Path, max_bytes: int) -> bytes:
    if isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    root_path = _absolute(root)
    target = _absolute(path)
    parents = _parent_chain(root_path, target)
    parent_identities = _inspect_directories(parents)
    try:
        before = os.lstat(target)
    except OSError as exc:
        raise UnsafeArtifact(f"artifact cannot be inspected: {target.name}") from exc
    if stat.S_ISLNK(before.st_mode) or _is_reparse(before):
        raise UnsafeArtifact(f"artifact is a symlink or reparse point: {target.name}")
    if not stat.S_ISREG(before.st_mode):
        raise UnsafeArtifact(f"artifact is not a regular file: {target.name}")
    if before.st_size > max_bytes:
        raise UnsafeArtifact(f"artifact exceeds byte limit: {target.name}")

    descriptor: int | None = None
    try:
        descriptor = _open_relative_no_follow(target, root_path)
        opened = os.fstat(descriptor)
        after_open = os.lstat(target)
        _verify_directories(parent_identities, phase="opening")
        if (
            _identity(before) != _identity(opened)
            or _identity(before) != _identity(after_open)
            or stat.S_ISLNK(after_open.st_mode)
            or _is_reparse(after_open)
        ):
            raise UnsafeArtifact(f"artifact changed while opening: {target.name}")
        chunks: list[bytes] = []
        received = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes - received + 1))
            if not chunk:
                break
            received += len(chunk)
            if received > max_bytes:
                raise UnsafeArtifact(f"artifact exceeds byte limit: {target.name}")
            chunks.append(chunk)
        after_read = os.fstat(descriptor)
        final_path = os.lstat(target)
        _verify_directories(parent_identities, phase="reading")
        if (
            _identity(opened) != _identity(after_read)
            or _identity(opened) != _identity(final_path)
            or stat.S_ISLNK(final_path.st_mode)
            or _is_reparse(final_path)
        ):
            raise UnsafeArtifact(f"artifact changed while reading: {target.name}")
        return b"".join(chunks)
    except UnsafeArtifact:
        raise
    except OSError as exc:
        raise UnsafeArtifact(f"artifact cannot be securely read: {target.name}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _ensure_secure_directory_shape(path: Path) -> bool:
    missing: list[Path] = []
    current = path
    while not os.path.lexists(current):
        missing.append(current)
        current = current.parent
    directory_identities = _inspect_directories(
        _absolute_directory_chain(current)
    )
    path_created = bool(missing)
    for directory in reversed(missing):
        _verify_directories(
            directory_identities, phase="preparing secure directory"
        )
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        _verify_directories(
            directory_identities, phase="creating secure directory"
        )
        created = os.lstat(directory)
        if stat.S_ISLNK(created.st_mode) or _is_reparse(created):
            raise UnsafeArtifact(
                f"secure directory is a symlink or reparse point: {directory}"
            )
        if not stat.S_ISDIR(created.st_mode):
            raise UnsafeArtifact(f"secure directory is not a directory: {directory}")
        directory_identities[directory] = (created.st_dev, created.st_ino)
    _verify_directories(directory_identities, phase="verifying secure directory")
    value = os.lstat(path)
    if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
        raise UnsafeArtifact(f"key directory is a symlink or reparse point: {path}")
    if not stat.S_ISDIR(value.st_mode):
        raise UnsafeArtifact(f"key directory is not a directory: {path}")
    return path_created


def _ensure_secure_directory(path: Path) -> None:
    path_created = _ensure_secure_directory_shape(path)
    value = os.lstat(path)
    if os.name != "nt" and stat.S_IMODE(value.st_mode) & 0o077:
        raise UnsafeArtifact("key directory permissions are not restrictive")
    if _WINDOWS:
        if path_created:
            _restrict_windows_path(path, is_directory=True)
        _verify_windows_path_acl(path)


def ensure_secure_directory(path: str | Path) -> Path:
    checked = _absolute(path)
    _ensure_secure_directory(checked)
    return checked


def provision_secure_directory(path: str | Path) -> Path:
    """Create a trusted storage root and restrict an existing root on first use."""
    checked = _absolute(path)
    _ensure_secure_directory_shape(checked)
    if _WINDOWS:
        _restrict_windows_path(checked, is_directory=True)
        _verify_windows_path_acl(checked)
    else:
        try:
            os.chmod(checked, 0o700)
        except OSError as exc:
            raise UnsafeArtifact("secure directory permissions cannot be restricted") from exc
        _ensure_secure_directory(checked)
    return checked


@lru_cache(maxsize=1)
def _current_windows_identity() -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        row = next(csv.reader(io.StringIO(result.stdout.strip())))
    except (OSError, subprocess.SubprocessError, StopIteration, csv.Error) as exc:
        raise UnsafeArtifact("Windows user identity cannot be verified") from exc
    if result.returncode != 0 or len(row) != 2 or not row[1].startswith("S-1-"):
        raise UnsafeArtifact("Windows user identity cannot be verified")
    return row[0], row[1]


def _restrict_windows_path(path: Path, *, is_directory: bool) -> None:
    _, sid = _current_windows_identity()
    permission = f"*{sid}:(OI)(CI)F" if is_directory else f"*{sid}:F"
    commands = (
        ["icacls", str(path), "/inheritance:r", "/grant:r", permission],
        ["icacls", str(path), "/setowner", f"*{sid}"],
    )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise UnsafeArtifact("Windows ACL protection failed") from exc
        if result.returncode != 0:
            raise UnsafeArtifact("Windows ACL protection failed")


def _windows_sddl(path: Path) -> str:
    owner_security_information = 0x00000001
    dacl_security_information = 0x00000004
    security_information = owner_security_information | dacl_security_information
    se_file_object = 1
    revision = 1
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    sddl = wintypes.LPWSTR()
    length = wintypes.DWORD()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        se_file_object,
        security_information,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise UnsafeArtifact("Windows ACL cannot be inspected")
    try:
        converted = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            revision,
            security_information,
            ctypes.byref(sddl),
            ctypes.byref(length),
        )
        if not converted:
            raise UnsafeArtifact("Windows ACL cannot be inspected")
        return sddl.value
    finally:
        if sddl:
            kernel32.LocalFree(sddl)
        if descriptor:
            kernel32.LocalFree(descriptor)


def _verify_windows_path_acl(path: Path) -> None:
    _, sid = _current_windows_identity()
    sddl = _windows_sddl(path)
    owner_match = re.match(r"O:(.+?)(?:G:|D:)", sddl)
    principals = set(re.findall(r";;;([^)]+)\)", sddl))
    dacl = sddl.split("D:", 1)[1] if "D:" in sddl else ""
    if (
        owner_match is None
        or owner_match.group(1) != sid
        or not dacl.startswith("P")
        or principals != {sid}
    ):
        raise UnsafeArtifact("Windows ACL is not current-user-only and protected")


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(value: bytes) -> tuple[_DataBlob, object]:
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _dpapi(value: bytes, *, protect: bool) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source, source_buffer = _blob(value)
    entropy, entropy_buffer = _blob(b"langchain-deepseek-job-collection-attestation-v1")
    output = _DataBlob()
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    description = "Job collection attestation key" if protect else None
    success = function(
        ctypes.byref(source),
        description,
        ctypes.byref(entropy),
        None,
        None,
        0x1,
        ctypes.byref(output),
    )
    del source_buffer, entropy_buffer
    if not success:
        raise UnsafeArtifact("Windows DPAPI key protection failed")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _protect_windows_key(value: bytes) -> bytes:
    return _dpapi(value, protect=True)


def _unprotect_windows_key(value: bytes) -> bytes:
    return _dpapi(value, protect=False)


def _encode_stored_key(key: bytes) -> bytes:
    return _DPAPI_MAGIC + _protect_windows_key(key) if _WINDOWS else key


def _decode_stored_key(stored: bytes) -> bytes:
    if _WINDOWS:
        if not stored.startswith(_DPAPI_MAGIC):
            raise UnsafeArtifact("attestation key is not DPAPI protected")
        return _unprotect_windows_key(stored[len(_DPAPI_MAGIC) :])
    return stored


def _read_provisioned_key(key_path: Path, key_root: Path) -> bytes:
    for attempt in range(50):
        try:
            stored = secure_read_file(
                key_path, root=key_root, max_bytes=_MAX_STORED_KEY_BYTES
            )
            if _WINDOWS:
                _verify_windows_path_acl(key_path)
            return _decode_stored_key(stored)
        except UnsafeArtifact:
            try:
                value = os.lstat(key_path)
            except OSError:
                raise
            provisioned_regular_file = (
                stat.S_ISREG(value.st_mode)
                and not stat.S_ISLNK(value.st_mode)
                and not _is_reparse(value)
                and value.st_size <= _MAX_STORED_KEY_BYTES
            )
            if not provisioned_regular_file or attempt == 49:
                raise
            time.sleep(0.01)
    raise UnsafeArtifact("attestation key provisioning did not complete")


def _write_all(descriptor: int, content: bytes) -> None:
    written = 0
    while written < len(content):
        count = os.write(descriptor, content[written:])
        if count <= 0:
            raise OSError("attestation key write made no progress")
        written += count


def load_or_create_attestation_key(*, root: str | Path) -> bytes:
    key_root = _absolute(root)
    try:
        _ensure_secure_directory(key_root)
    except UnsafeArtifact as exc:
        raise UnsafeArtifact(f"attestation key storage is unsafe: {exc}") from exc
    directory_identities = _inspect_directories(
        _absolute_directory_chain(key_root)
    )
    key_path = key_root / "attestation.key"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    generated = secrets.token_bytes(32)
    stored = _encode_stored_key(generated)
    try:
        descriptor = os.open(key_path, flags, 0o600)
    except FileExistsError:
        key = _read_provisioned_key(key_path, key_root)
    else:
        try:
            _write_all(descriptor, stored)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            if _WINDOWS:
                _restrict_windows_path(key_path, is_directory=False)
                _verify_windows_path_acl(key_path)
            else:
                os.chmod(key_path, 0o600)
        except (OSError, UnsafeArtifact) as exc:
            key_path.unlink(missing_ok=True)
            raise UnsafeArtifact("attestation key permissions cannot be restricted") from exc
        key = _read_provisioned_key(key_path, key_root)
    _verify_directories(directory_identities, phase="loading attestation key")
    if len(key) != 32:
        raise UnsafeArtifact("attestation key must contain exactly 32 bytes")
    if _WINDOWS:
        _verify_windows_path_acl(key_root)
        _verify_windows_path_acl(key_path)
    elif stat.S_IMODE(os.lstat(key_path).st_mode) & 0o077:
        raise UnsafeArtifact("attestation key permissions are not restrictive")
    return key


def _secure_unlink(
    path: Path,
    *,
    directory_identities: dict[Path, tuple[int, int]],
    expected_identity: tuple[int, int, int, int],
) -> None:
    try:
        _verify_directories(directory_identities, phase="cleaning temporary file")
        current = os.lstat(path)
        if _identity(current) == expected_identity and stat.S_ISREG(current.st_mode):
            path.unlink()
    except (OSError, UnsafeArtifact):
        return


def _ensure_directory_tree(root: Path, parent: Path) -> None:
    _ensure_secure_directory(root)
    current = root
    for part in parent.relative_to(root).parts:
        current /= part
        _ensure_secure_directory(current)


def secure_atomic_write(
    path: str | Path, content: bytes, *, root: str | Path
) -> None:
    root_path = _absolute(root)
    target = _absolute(path)
    _parent_chain(root_path, target)
    _ensure_directory_tree(root_path, target.parent)
    directory_identities = _inspect_directories(
        _absolute_directory_chain(target.parent)
    )
    temporary = target.parent / f".jc-{secrets.token_hex(16)}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    temporary_identity: tuple[int, int, int, int] | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        temporary_identity = _identity(os.fstat(descriptor))
        os.close(descriptor)
        descriptor = None
        _verify_directories(directory_identities, phase="writing artifact")
        if target.exists():
            current = os.lstat(target)
            if stat.S_ISLNK(current.st_mode) or _is_reparse(current):
                raise UnsafeArtifact(f"artifact target is a symlink or reparse point: {target}")
            if not stat.S_ISREG(current.st_mode):
                raise UnsafeArtifact(f"artifact target is not a regular file: {target}")
        os.replace(temporary, target)
        _verify_directories(directory_identities, phase="replacing artifact")
        installed = os.lstat(target)
        if temporary_identity != _identity(installed) or _is_reparse(installed):
            raise UnsafeArtifact(f"artifact changed while replacing: {target.name}")
    except UnsafeArtifact:
        raise
    except OSError as exc:
        raise UnsafeArtifact(f"cannot atomically write {target}: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_identity is not None:
            _secure_unlink(
                temporary,
                directory_identities=directory_identities,
                expected_identity=temporary_identity,
            )


class ExclusiveRunLock:
    def __init__(self, root: str | Path, run_id: str, purpose: str) -> None:
        if not _RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError("invalid run_id for lock")
        if purpose not in {"collect", "commit"}:
            raise ValueError("invalid lock purpose")
        self.root = _absolute(root)
        self.path = self.root / f"{run_id}.lock"
        self._token = secrets.token_hex(32).encode("ascii")
        self._identity: tuple[int, int, int, int] | None = None

    def acquire(self) -> None:
        _ensure_secure_directory(self.root)
        directory_identities = _inspect_directories(
            _absolute_directory_chain(self.root)
        )
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            raise LockUnavailable(
                f"run {self.path.stem} is already claimed"
            ) from exc
        try:
            os.write(descriptor, self._token)
            os.fsync(descriptor)
            self._identity = _identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)
        try:
            _verify_directories(directory_identities, phase="creating run lock")
            current = os.lstat(self.path)
            if _identity(current) != self._identity or _is_reparse(current):
                raise UnsafeArtifact("run lock changed while creating")
            if _WINDOWS:
                _restrict_windows_path(self.path, is_directory=False)
                _verify_windows_path_acl(self.path)
        except (OSError, UnsafeArtifact):
            if self._identity is not None:
                _secure_unlink(
                    self.path,
                    directory_identities=directory_identities,
                    expected_identity=self._identity,
                )
            self._identity = None
            raise

    def release(self) -> None:
        if self._identity is None:
            return
        try:
            current = os.lstat(self.path)
            if _identity(current) != self._identity:
                return
            content = secure_read_file(
                self.path, root=self.root, max_bytes=len(self._token)
            )
            if content == self._token:
                self.path.unlink()
        except (OSError, UnsafeArtifact):
            return
        finally:
            self._identity = None

    def __enter__(self) -> "ExclusiveRunLock":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


__all__ = [
    "ExclusiveRunLock",
    "LockUnavailable",
    "UnsafeArtifact",
    "default_control_state_root",
    "ensure_secure_directory",
    "provision_secure_directory",
    "load_or_create_attestation_key",
    "secure_atomic_write",
    "secure_read_file",
]
