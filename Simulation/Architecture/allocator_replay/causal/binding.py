"""Stable RP2040 identity, publication-cohort binding, and process leases."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import BoardBindingError, BoardLeaseError


REQUIRED_HARDWARE_WORKERS = 3
MAX_DEVELOPMENT_WORKERS = 4
_PROCESS_LEASES: set[str] = set()
_PROCESS_LEASE_LOCK = threading.Lock()


@dataclass(frozen=True)
class BoardFingerprint:
    """Fields that must survive reconnect and campaign resume unchanged."""

    device_id: str
    port: str
    build_id: str
    firmware_sha256: str
    module_set_sha256: str
    implementation: str
    frequency_hz: int
    timer_unit: str = "unknown"
    timer_resolution_us: int = -1
    timer_monotonic: bool = False
    timer_wraparound_safe: bool = False
    virtual_device: bool = False

    @classmethod
    def inspect(
        cls,
        device: Any,
        *,
        expected_build_id: str | None = None,
        expected_module_set_sha256: str | None = None,
        expected_firmware_sha256: str | None = None,
        repeat_hello: bool = True,
    ) -> "BoardFingerprint":
        hello = getattr(device, "hello", None)
        check_method = getattr(device, "check", None)
        if not callable(hello) or not callable(check_method):
            raise BoardBindingError("device must expose live hello() and check()")
        first = hello()
        second = hello() if repeat_hello else first
        identity_fields = (
            "device_id",
            "build_id",
            "firmware_sha256",
            "implementation",
            "frequency_hz",
        )
        changed = [
            name
            for name in identity_fields
            if getattr(first, name, None) != getattr(second, name, None)
        ]
        if changed:
            raise BoardBindingError(
                "unstable board identity across live HELLO: " + ", ".join(changed)
            )
        check = check_method()
        if bool(check.get("motors_initialized")) or bool(
            check.get("sensors_initialized")
        ):
            raise BoardBindingError("allocator timing worker initialized motors/sensors")
        actual_module = str(check.get("actual_module_set_sha256", ""))
        worker_module = str(check.get("expected_module_set_sha256", ""))
        if not actual_module or actual_module != worker_module:
            raise BoardBindingError(
                "device module-set hash is absent or differs from worker expectation"
            )
        build_id = str(getattr(first, "build_id", ""))
        if expected_build_id is not None and build_id != str(expected_build_id):
            raise BoardBindingError(
                f"device build mismatch: {build_id!r} != {expected_build_id!r}"
            )
        if (
            expected_module_set_sha256 is not None
            and actual_module != str(expected_module_set_sha256)
        ):
            raise BoardBindingError("device module-set hash differs from selected build")
        firmware_sha256 = str(getattr(first, "firmware_sha256", ""))
        if (
            expected_firmware_sha256 is not None
            and firmware_sha256 != str(expected_firmware_sha256)
        ):
            raise BoardBindingError("device firmware hash differs from selected build")
        device_id = str(getattr(first, "device_id", "")).strip()
        if not device_id:
            raise BoardBindingError("device returned an empty unique identity")
        frequency_hz = int(getattr(first, "frequency_hz", 0))
        if frequency_hz <= 0:
            raise BoardBindingError("device returned an invalid timing frequency")
        return cls(
            device_id=device_id,
            port=str(getattr(first, "port", getattr(device, "port", ""))),
            build_id=build_id,
            firmware_sha256=firmware_sha256,
            module_set_sha256=actual_module,
            implementation=str(getattr(first, "implementation", "")),
            frequency_hz=frequency_hz,
            timer_unit=str(check.get("timer_unit", "unknown")),
            timer_resolution_us=int(check.get("timer_resolution_us", -1)),
            timer_monotonic=bool(check.get("timer_monotonic", False)),
            timer_wraparound_safe=bool(
                check.get("timer_wraparound_safe", False)
            ),
            virtual_device=bool(check.get("virtual_device", False)),
        )

    def reconnect_key(self) -> tuple[Any, ...]:
        """Port is intentionally excluded because Linux enumeration may change."""

        return (
            self.device_id,
            self.build_id,
            self.firmware_sha256,
            self.module_set_sha256,
            self.implementation,
            self.frequency_hz,
            self.timer_unit,
            self.timer_monotonic,
            self.timer_wraparound_safe,
            self.virtual_device,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "port": self.port,
            "build_id": self.build_id,
            "firmware_sha256": self.firmware_sha256,
            "module_set_sha256": self.module_set_sha256,
            "implementation": self.implementation,
            "frequency_hz": self.frequency_hz,
            "timer_unit": self.timer_unit,
            "timer_resolution_us": self.timer_resolution_us,
            "timer_monotonic": self.timer_monotonic,
            "timer_wraparound_safe": self.timer_wraparound_safe,
            "virtual_device": self.virtual_device,
        }


@dataclass(frozen=True)
class StableBoardBinding:
    worker_index: int
    fingerprint: BoardFingerprint
    device: Any = field(compare=False, repr=False)

    @property
    def board_id(self) -> str:
        return self.fingerprint.device_id

    @property
    def serial_device(self) -> str:
        return self.fingerprint.port

    def as_dict(self) -> dict[str, Any]:
        return {
            "worker_index": self.worker_index,
            **self.fingerprint.as_dict(),
        }


def _normalize_mapping(mapping: Mapping[Any, str]) -> dict[int, str]:
    normalized: dict[int, str] = {}
    for raw_key, raw_value in mapping.items():
        text = str(raw_key).lower().removeprefix("worker_").removeprefix("worker-")
        try:
            index = int(text)
        except ValueError as exc:
            raise BoardBindingError(f"invalid explicit worker key: {raw_key!r}") from exc
        if index in normalized:
            raise BoardBindingError("explicit board map repeats a worker index")
        normalized[index] = str(raw_value)
    return normalized


def bind_hardware_workers(
    devices: Sequence[Any],
    *,
    explicit_mapping: Mapping[Any, str] | None = None,
    development_override: bool = False,
    expected_build_id: str | None = None,
    expected_module_set_sha256: str | None = None,
    expected_firmware_sha256: str | Mapping[str, str] | None = None,
) -> tuple[StableBoardBinding, ...]:
    """Bind stable identities to worker slots for the publication cohort.

    Serial enumeration order is never an identity.  Without an explicit map,
    stable device IDs are sorted.  The development override permits fewer
    virtual/loopback devices, but never more than the publication cohort.
    """

    count = len(devices)
    if count != REQUIRED_HARDWARE_WORKERS and not development_override:
        raise BoardBindingError(
            "hardware campaign requires exactly "
            f"{REQUIRED_HARDWARE_WORKERS} boards; detected {count}"
        )
    if count < 1 or count > MAX_DEVELOPMENT_WORKERS:
        raise BoardBindingError(
            "development board count must be between one and "
            f"{MAX_DEVELOPMENT_WORKERS}"
        )
    inspected = [
        (
            BoardFingerprint.inspect(
                device,
                expected_build_id=expected_build_id,
                expected_module_set_sha256=expected_module_set_sha256,
            ),
            device,
        )
        for device in devices
    ]
    ids = [item[0].device_id for item in inspected]
    if len(ids) != len(set(ids)):
        raise BoardBindingError("duplicate board identity in worker binding")
    if expected_firmware_sha256 is not None:
        if isinstance(expected_firmware_sha256, Mapping):
            expected_by_id = {
                str(key): str(value)
                for key, value in expected_firmware_sha256.items()
            }
            if set(expected_by_id) != set(ids):
                raise BoardBindingError(
                    "expected firmware map must contain every detected board ID"
                )
        else:
            expected_by_id = {
                device_id: str(expected_firmware_sha256) for device_id in ids
            }
        for fingerprint, _ in inspected:
            if (
                fingerprint.firmware_sha256
                != expected_by_id[fingerprint.device_id]
            ):
                raise BoardBindingError(
                    f"firmware hash mismatch on {fingerprint.device_id}"
                )
    if explicit_mapping is None:
        ordered = sorted(inspected, key=lambda item: item[0].device_id)
        return tuple(
            StableBoardBinding(index, fingerprint, device)
            for index, (fingerprint, device) in enumerate(ordered)
        )
    mapping = _normalize_mapping(explicit_mapping)
    expected_indexes = set(range(count))
    if set(mapping) != expected_indexes:
        raise BoardBindingError(
            f"explicit map must contain worker indexes {sorted(expected_indexes)}"
        )
    if set(mapping.values()) != set(ids):
        raise BoardBindingError(
            "explicit map must reference every detected board identity exactly once"
        )
    by_id = {fingerprint.device_id: (fingerprint, device) for fingerprint, device in inspected}
    return tuple(
        StableBoardBinding(index, *by_id[mapping[index]]) for index in range(count)
    )


def bind_single_hardware_worker(
    device: Any,
    *,
    worker_index: int,
    expected_device_id: str,
    expected_build_id: str,
    expected_module_set_sha256: str,
    expected_firmware_sha256: str | None = None,
) -> StableBoardBinding:
    """Revalidate one preflight-sealed board inside its owning process.

    Global cohort uniqueness belongs to discovery/preflight.  Each spawned
    worker opens only its assigned serial endpoint and uses this helper before
    it acquires a board lease or starts a mission.
    """

    index = int(worker_index)
    if index < 0 or index >= REQUIRED_HARDWARE_WORKERS:
        raise BoardBindingError(
            "worker index must be in the publication range 0.."
            f"{REQUIRED_HARDWARE_WORKERS - 1}"
        )
    fingerprint = BoardFingerprint.inspect(
        device,
        expected_build_id=expected_build_id,
        expected_module_set_sha256=expected_module_set_sha256,
        expected_firmware_sha256=expected_firmware_sha256,
    )
    if fingerprint.device_id != str(expected_device_id):
        raise BoardBindingError(
            f"worker {index} opened board {fingerprint.device_id!r}, "
            f"expected {expected_device_id!r}"
        )
    return StableBoardBinding(index, fingerprint, device)


class BoardLease:
    """Exclusive board ownership visible to threads and worker processes."""

    def __init__(self, board_id: str, lock_root: Path) -> None:
        if not str(board_id):
            raise ValueError("board_id must not be empty")
        self.board_id = str(board_id)
        self.lock_root = Path(lock_root).resolve()
        digest = hashlib.sha256(self.board_id.encode("utf-8")).hexdigest()[:24]
        self.path = self.lock_root / f"rp2040-{digest}.lock"
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self) -> "BoardLease":
        if self.acquired:
            raise BoardLeaseError("board lease is not re-entrant")
        self.lock_root.mkdir(parents=True, exist_ok=True)
        with _PROCESS_LEASE_LOCK:
            if self.board_id in _PROCESS_LEASES:
                raise BoardLeaseError(f"board {self.board_id!r} is already leased")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            try:
                descriptor = os.open(str(self.path), flags, 0o600)
            except FileExistsError as exc:
                detail = ""
                try:
                    detail = self.path.read_text(encoding="utf-8")
                except OSError:
                    pass
                raise BoardLeaseError(
                    f"board {self.board_id!r} is locked by another worker: {detail}"
                ) from exc
            metadata = {
                "schema_version": 1,
                "board_id": self.board_id,
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "token": self.token,
                "created_unix_s": time.time(),
            }
            try:
                os.write(
                    descriptor,
                    json.dumps(metadata, sort_keys=True).encode("utf-8"),
                )
            finally:
                os.close(descriptor)
            _PROCESS_LEASES.add(self.board_id)
            self.acquired = True
        return self

    def release(self) -> None:
        if not self.acquired:
            return
        with _PROCESS_LEASE_LOCK:
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = {}
            # Never remove another worker's replacement lock.
            if existing.get("token") == self.token:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
            _PROCESS_LEASES.discard(self.board_id)
            self.acquired = False

    def __enter__(self) -> "BoardLease":
        return self.acquire()

    def __exit__(self, *_: Any) -> None:
        self.release()
