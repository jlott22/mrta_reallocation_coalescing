"""Fail-closed recovery of stale causal campaign lock files.

Only lock paths derived from an already validated :class:`CausalConfig` are
considered.  Recovery first validates every present configured lock and proves
that its owner PID no longer exists on this host.  It does not unlink anything
unless the complete inspection succeeds.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .model import BoardBinding, CausalConfig


OUTPUT_LOCK_SCHEMA_VERSION = 2
OUTPUT_LOCK_KIND = "causal_output_board"
NATIVE_LEASE_SCHEMA_VERSION = 1


class LockRecoveryError(RuntimeError):
    """A lock could not be proven safe to remove."""


@dataclass(frozen=True)
class _LockSpec:
    kind: str
    path: Path
    board: BoardBinding
    worker_index: int


@dataclass(frozen=True)
class _Removal:
    spec: _LockSpec
    raw_bytes: bytes
    stat_identity: tuple[int, int, int, int]
    pid: int


def native_lease_path(repo_root: Path, expected_device_uid: str) -> Path:
    """Return the one global lease path used by ``BoardLease`` for a UID."""

    digest = hashlib.sha256(str(expected_device_uid).encode("utf-8")).hexdigest()[:24]
    return (
        Path(repo_root).resolve()
        / "study"
        / "native_device_leases"
        / f"rp2040-{digest}.lock"
    )


def _configured_specs(config: CausalConfig) -> tuple[_LockSpec, ...]:
    specs: list[_LockSpec] = []
    for worker_index, board in enumerate(config.boards):
        specs.append(_LockSpec(
            kind="output_board_lock",
            path=(config.output_root / "board_locks" / f"{board.board_id}.lock").resolve(),
            board=board,
            worker_index=worker_index,
        ))
        specs.append(_LockSpec(
            kind="native_device_lease",
            path=native_lease_path(config.repo_root, board.expected_device_uid),
            board=board,
            worker_index=worker_index,
        ))
    paths = [spec.path for spec in specs]
    if len(paths) != len(set(paths)):
        raise LockRecoveryError("configured lock paths are not unique")
    return tuple(specs)


def _pid_status(pid: int) -> tuple[str, str]:
    """Return ``dead``, ``alive``, or ``unknown`` without changing the process."""

    # The scientific host is Linux.  On Windows, ``os.kill(pid, 0)`` maps zero
    # to CTRL_C_EVENT rather than POSIX's non-signalling existence probe, so it
    # must never be used for recovery.
    if os.name != "posix":
        return "unknown", "safe non-signalling PID proof is unavailable off POSIX"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead", "os.kill(pid, 0) reported that no such process exists"
    except PermissionError as error:
        return "alive", f"process exists but is not signalable: {error}"
    except OSError as error:
        if error.errno == errno.ESRCH:
            return "dead", "os.kill(pid, 0) reported ESRCH"
        if error.errno == errno.EPERM:
            return "alive", f"process exists but is not signalable: {error}"
        return "unknown", f"PID state could not be proven: {error}"
    return "alive", "os.kill(pid, 0) succeeded"


def _plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_common(value: Mapping[str, Any], spec: _LockSpec, hostname: str) -> int:
    pid = value.get("pid")
    if not _plain_int(pid) or pid <= 0:
        raise LockRecoveryError(f"{spec.path}: pid must be a positive integer")
    lock_hostname = value.get("hostname")
    if not isinstance(lock_hostname, str) or not lock_hostname:
        raise LockRecoveryError(f"{spec.path}: hostname is absent or malformed")
    if lock_hostname != hostname:
        raise LockRecoveryError(
            f"{spec.path}: lock belongs to foreign host {lock_hostname!r}, "
            f"not current host {hostname!r}"
        )
    return pid


def _validate_payload(value: Mapping[str, Any], spec: _LockSpec, hostname: str) -> int:
    pid = _validate_common(value, spec, hostname)
    board = spec.board
    if spec.kind == "output_board_lock":
        expected = {
            "schema_version": OUTPUT_LOCK_SCHEMA_VERSION,
            "lock_kind": OUTPUT_LOCK_KIND,
            "board_id": board.board_id,
            "expected_device_uid": board.expected_device_uid,
            "serial_device": board.serial_device,
            "worker_index": spec.worker_index,
        }
        for field, expected_value in expected.items():
            if value.get(field) != expected_value:
                raise LockRecoveryError(
                    f"{spec.path}: {field} does not match configured board/UID binding"
                )
        claimed_at = value.get("claimed_at")
        if not isinstance(claimed_at, str) or not claimed_at:
            raise LockRecoveryError(f"{spec.path}: claimed_at is absent or malformed")
        return pid

    if value.get("schema_version") != NATIVE_LEASE_SCHEMA_VERSION:
        raise LockRecoveryError(f"{spec.path}: unsupported native lease schema")
    # BoardLease uses the immutable hardware UID as its board_id.
    if value.get("board_id") != board.expected_device_uid:
        raise LockRecoveryError(
            f"{spec.path}: native lease UID does not match the configured board"
        )
    token = value.get("token")
    if not isinstance(token, str) or not token:
        raise LockRecoveryError(f"{spec.path}: native lease token is absent or malformed")
    created = value.get("created_unix_s")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        raise LockRecoveryError(
            f"{spec.path}: native lease creation time is absent or malformed"
        )
    return pid


def _stat_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _inspect(spec: _LockSpec, hostname: str) -> _Removal | None:
    try:
        info = spec.path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise LockRecoveryError(f"{spec.path}: cannot inspect lock: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise LockRecoveryError(f"{spec.path}: lock path is not a regular file")
    if info.st_size <= 0 or info.st_size > 65536:
        raise LockRecoveryError(f"{spec.path}: lock file size is malformed")
    try:
        raw = spec.path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LockRecoveryError(f"{spec.path}: malformed lock JSON: {error}") from error
    if not isinstance(value, dict):
        raise LockRecoveryError(f"{spec.path}: lock JSON must be an object")
    pid = _validate_payload(value, spec, hostname)
    status, detail = _pid_status(pid)
    if status != "dead":
        raise LockRecoveryError(
            f"{spec.path}: refusing to remove PID {pid}; status={status}: {detail}"
        )
    try:
        identity = _stat_identity(spec.path)
    except OSError as error:
        raise LockRecoveryError(
            f"{spec.path}: lock changed during inspection: {error}"
        ) from error
    return _Removal(spec, raw, identity, pid)


def recover_stale_locks(config: CausalConfig) -> dict[str, Any]:
    """Remove only validated, local, configured locks whose PID is proven dead.

    Inspection is all-or-nothing: a live, foreign-host, malformed, mismatched,
    or unprovable lock aborts before any path is removed.  Missing configured
    lock files are benign.  No directory enumeration or glob deletion occurs.
    """

    hostname = socket.gethostname()
    specs = _configured_specs(config)
    removals: list[_Removal] = []
    errors: list[str] = []
    for spec in specs:
        try:
            removal = _inspect(spec, hostname)
        except LockRecoveryError as error:
            errors.append(str(error))
        else:
            if removal is not None:
                removals.append(removal)
    if errors:
        raise LockRecoveryError(
            "lock recovery refused before deletion:\n- " + "\n- ".join(errors)
        )

    # Recheck every candidate before the first unlink.  This catches ordinary
    # replacement/edit races while retaining the all-or-nothing decision rule.
    for removal in removals:
        try:
            current_identity = _stat_identity(removal.spec.path)
            current_bytes = removal.spec.path.read_bytes()
        except OSError as error:
            raise LockRecoveryError(
                f"lock changed or disappeared before recovery: {removal.spec.path}: {error}"
            ) from error
        if current_identity != removal.stat_identity or current_bytes != removal.raw_bytes:
            raise LockRecoveryError(
                f"lock changed before recovery; nothing was removed: {removal.spec.path}"
            )

    removed: list[dict[str, Any]] = []
    for removal in removals:
        removal.spec.path.unlink()
        removed.append({
            "kind": removal.spec.kind,
            "path": str(removal.spec.path),
            "board_id": removal.spec.board.board_id,
            "expected_device_uid": removal.spec.board.expected_device_uid,
            "dead_pid": removal.pid,
        })
    return {
        "schema_version": 1,
        "report_kind": "causal_stale_lock_recovery",
        "passed": True,
        "hostname": hostname,
        "configured_board_count": len(config.boards),
        "configured_exact_lock_count": len(specs),
        "present_stale_lock_count": len(removals),
        "removed_locks": removed,
        "used_directory_enumeration": False,
        "used_glob_deletion": False,
    }
