from __future__ import annotations

import base64
import binascii
import array
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from allocator_replay.capture.codec import canonical_json_bytes
from allocator_replay.device.common.replay_fingerprint import logical_sha256


PROTOCOL = "AR1"
DEFAULT_BAUDRATE = 115_200
CHUNK_BYTES = 384
PART_PAYLOAD_BYTES = 768
ARRAY_PART_RAW_BYTES = 384
TRANSFER_RETRIES = 3


class ReplayTransportError(RuntimeError):
    """USB/serial/protocol failure that is not an allocator timing attempt."""


class ReplayTimeout(RuntimeError):
    """An acknowledged allocator call exceeded its timing deadline."""

    def __init__(
        self,
        message: str,
        *,
        heap_free_at_ready: int | None = None,
        host_elapsed_us: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.heap_free_at_ready = heap_free_at_ready
        self.host_elapsed_us = host_elapsed_us
        self.timeout_seconds = timeout_seconds

    def timeout_diagnostics(self) -> dict[str, int | float]:
        return {
            name: value
            for name, value in (
                ("heap_free_at_ready", self.heap_free_at_ready),
                ("host_elapsed_us", self.host_elapsed_us),
                ("timeout_seconds", self.timeout_seconds),
            )
            if value is not None
        }


class ReplayMemoryError(RuntimeError):
    """The native replay state or allocator could not fit in device memory."""


class ReplayStateSetupError(RuntimeError):
    """A context could not be restored or updated before timing began."""


class ReplayOutputSerializationError(RuntimeError):
    """Allocator finished, but its bounded result could not be returned."""


class _TimedAllocatorFailure:
    """Diagnostics attached to failures raised inside timed choose_goal."""

    def _set_timed_failure_diagnostics(
        self,
        *,
        heap_free_before: int | None = None,
        heap_free_after: int | None = None,
        elapsed_until_failure_us: int | None = None,
    ) -> None:
        self.heap_free_before = heap_free_before
        self.heap_free_after = heap_free_after
        self.elapsed_until_failure_us = elapsed_until_failure_us

    def timed_failure_diagnostics(self) -> dict[str, int]:
        return {
            name: value
            for name, value in (
                ("heap_free_before", self.heap_free_before),
                ("heap_free_after", self.heap_free_after),
                (
                    "elapsed_until_failure_us",
                    self.elapsed_until_failure_us,
                ),
            )
            if value is not None
        }


class ReplayAllocatorError(_TimedAllocatorFailure, RuntimeError):
    """The allocator raised a non-memory exception in choose_goal."""

    def __init__(
        self,
        message: str,
        *,
        heap_free_before: int | None = None,
        heap_free_after: int | None = None,
        elapsed_until_failure_us: int | None = None,
    ) -> None:
        RuntimeError.__init__(self, message)
        self._set_timed_failure_diagnostics(
            heap_free_before=heap_free_before,
            heap_free_after=heap_free_after,
            elapsed_until_failure_us=elapsed_until_failure_us,
        )


class ReplayAllocatorMemoryError(_TimedAllocatorFailure, ReplayMemoryError):
    """MemoryError raised inside the acknowledged choose_goal region."""

    def __init__(
        self,
        message: str,
        *,
        heap_free_before: int | None = None,
        heap_free_after: int | None = None,
        elapsed_until_failure_us: int | None = None,
    ) -> None:
        ReplayMemoryError.__init__(self, message)
        self._set_timed_failure_diagnostics(
            heap_free_before=heap_free_before,
            heap_free_after=heap_free_after,
            elapsed_until_failure_us=elapsed_until_failure_us,
        )


@dataclass(frozen=True)
class DeviceIdentity:
    port: str
    device_id: str
    build_id: str
    implementation: str
    frequency_hz: int
    heap_free: int
    firmware_sha256: str


def _crc32(payload: bytes) -> int:
    return int(binascii.crc32(payload)) & 0xFFFFFFFF


def _project_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_project_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    tag = value.get("@")
    if tag == "array":
        items = []
        for item in value["v"]:
            if isinstance(item, dict) and item.get("@") == "float":
                item = {
                    "nan": float("nan"),
                    "inf": float("inf"),
                    "-inf": -float("inf"),
                }[item["v"]]
            items.append(item)
        packed = array.array(value["typecode"], items)
        return {
            "@": "arraybin",
            "typecode": value["typecode"],
            "byteorder": sys.byteorder,
            "v": base64.b64encode(packed.tobytes()).decode("ascii"),
        }
    if tag == "rng":
        encoded_state = value["v"]
        state_items = encoded_state["v"]
        version = int(state_items[0])
        mt_values = state_items[1]["v"]
        packed = array.array("I", (int(item) for item in mt_values))
        return {
            "@": "rngbin",
            "version": version,
            "byteorder": sys.byteorder,
            "state": base64.b64encode(packed.tobytes()).decode("ascii"),
            "gauss": _project_value(state_items[2]),
        }
    return {key: _project_value(item) for key, item in value.items()}


def project_device_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    """Remove host-only audit data and binary-pack arrays for controller RAM."""
    expected = fixture["expected"]
    post_state = expected["post_state"]
    changed_immutable_sections = [
        section
        for section in ("views", "cfg", "belief")
        if fixture["pre_state"][section] != post_state[section]
    ]
    if changed_immutable_sections:
        raise ValueError(
            "allocator changed fixture sections excluded from compact parity: "
            + ", ".join(changed_immutable_sections)
        )
    mutable_post = {
        "robot_attrs": post_state["robot_attrs"],
        "allocator_attrs": post_state["allocator_attrs"],
    }
    post_array_typecodes = {
        section: {
            name: value["typecode"]
            for name, value in post_state[section].items()
            if (
                isinstance(value, dict)
                and value.get("@") == "array"
            )
        }
        for section in ("robot_attrs", "allocator_attrs")
    }
    projected = {
        "schema": fixture["schema"],
        "fixture_id": fixture["fixture_id"],
        "fixture_sha256": fixture["fixture_sha256"],
        "condition_id": fixture["condition_id"],
        "mission": fixture["mission"],
        "algorithm": fixture["algorithm"],
        "pre_state": _project_value(fixture["pre_state"]),
        "expected": {
            "goal": _project_value(expected["goal"]),
            "messages_sha256": logical_sha256(expected["messages"]),
            "post_mutable_state_sha256": logical_sha256(mutable_post),
            "post_robot_attr_names": expected["post_robot_attr_names"],
            "post_allocator_attr_names": expected[
                "post_allocator_attr_names"
            ],
            "post_array_typecodes": post_array_typecodes,
        },
    }
    return projected


def project_authoritative_fixture(fixture: dict[str, Any]) -> dict[str, Any]:
    """Binary-pack a live call without embedding any desktop expected output."""
    return {
        "schema": int(fixture.get("schema", 1)),
        "fixture_id": fixture["fixture_id"],
        "condition_id": fixture["condition_id"],
        "mission": fixture["mission"],
        "algorithm": fixture["algorithm"],
        "authoritative": True,
        "pre_state": _project_value(fixture["pre_state"]),
    }


def _empty_state() -> dict[str, dict[str, Any]]:
    return {
        "robot_attrs": {},
        "views": {},
        "cfg": {},
        "belief": {},
        "allocator_attrs": {},
    }


def project_persistent_setup(setup: dict[str, Any]) -> dict[str, Any]:
    """Pack one restore/delta payload without embedding expected output."""
    state = _project_value(setup["pre_state"])
    # The simulator snapshot exposes the same probability map through both
    # robot.target_p and robot.belief.target_p.  Transmit it once; the replay
    # runtime restores the belief alias without allocating a duplicate map.
    views = state.get("views", {})
    belief = state.get("belief", {})
    if (
        "target_p" in views
        and "target_p" in belief
        and views["target_p"] == belief["target_p"]
    ):
        belief.pop("target_p", None)
    # Bayesian Robot.searched and Robot.local_searched are properties backed
    # by the exact same Belief.searched set.  A snapshot necessarily exposes
    # that one object through all three paths.  Restoring three decoded copies
    # wastes most of the controller heap late in a mission, so transmit the
    # value once and declare the original aliases explicitly.  Alias metadata
    # is applied after streamed parts have been decoded and before allocator
    # setup; unequal values are never coalesced.
    state_aliases: list[dict[str, str]] = []
    if "searched" in views:
        searched = views["searched"]
        if (
            "local_searched" in views
            and views["local_searched"] == searched
        ):
            views.pop("local_searched")
            state_aliases.append(
                {
                    "source_section": "views",
                    "source_name": "searched",
                    "target_section": "views",
                    "target_name": "local_searched",
                }
            )
        if (
            "searched" in belief
            and belief["searched"] == searched
        ):
            belief.pop("searched")
            state_aliases.append(
                {
                    "source_section": "views",
                    "source_name": "searched",
                    "target_section": "belief",
                    "target_name": "searched",
                }
            )
    return {
        "schema": int(setup.get("schema", 1)),
        "fixture_id": setup["fixture_id"],
        "condition_id": setup["condition_id"],
        "mission": setup["mission"],
        "algorithm": setup["algorithm"],
        "persistent": True,
        "context_id": str(setup["context_id"]),
        "setup_mode": setup["setup_mode"],
        "deleted": setup.get("deleted", {}),
        "events": _project_value(setup.get("events", [])),
        "resume_state": _project_value(setup.get("resume_state", {})),
        "state_aliases": state_aliases,
        "pre_state": state,
    }


def _serial_module():
    try:
        import serial
    except ImportError as exc:  # pragma: no cover - depends on host setup
        raise RuntimeError(
            "pyserial is required; install allocator_replay/requirements-host.txt"
        ) from exc
    return serial


class SerialReplayDevice:
    """Chunked, acknowledged connection to ``replay_worker`` on one Pololu."""

    def __init__(
        self,
        port: str,
        *,
        baudrate: int = DEFAULT_BAUDRATE,
        start_worker: bool = True,
        serial_object: Any | None = None,
    ) -> None:
        if serial_object is None:
            serial = _serial_module()
            try:
                self.serial = serial.Serial(
                    port,
                    baudrate=baudrate,
                    timeout=0.20,
                    write_timeout=2.0,
                    dsrdtr=False,
                    rtscts=False,
                )
            except serial.SerialException as exc:
                raise ReplayTransportError(f"cannot open {port}: {exc}") from exc
        else:
            self.serial = serial_object
        self.port = port
        self.identity: DeviceIdentity | None = None
        self._persistent_ready_heap: dict[str, int] = {}
        if start_worker:
            self.start_worker()

    def close(self) -> None:
        try:
            self.serial.close()
        except Exception:
            pass

    def _write_line(self, *fields: object) -> None:
        line = "|".join(str(field) for field in fields) + "\n"
        try:
            self.serial.write(line.encode("ascii"))
            self.serial.flush()
        except Exception as exc:
            raise ReplayTransportError(
                f"serial write failed on {self.port}: {exc}"
            ) from exc

    def _read_protocol(
        self,
        *,
        deadline: float,
        expected: set[str] | None = None,
    ) -> list[str]:
        while time.monotonic() < deadline:
            try:
                raw = self.serial.readline()
            except Exception as exc:
                raise ReplayTransportError(
                    f"serial read failed on {self.port}: {exc}"
                ) from exc
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            marker = line.find(PROTOCOL + "|")
            if marker < 0:
                continue
            fields = line[marker:].split("|")
            command = fields[1] if len(fields) > 1 else ""
            if command == "ERROR":
                if len(fields) >= 4 and fields[3] == "MemoryError":
                    raise ReplayMemoryError(
                        f"device memory failure on {self.port}: "
                        f"{'|'.join(fields[2:])}"
                    )
                raise ReplayTransportError(
                    f"device protocol error on {self.port}: {'|'.join(fields[2:])}"
                )
            if command == "PFAIL":
                if len(fields) < 5:
                    raise ReplayTransportError(
                        f"malformed persistent failure on {self.port}"
                    )
                try:
                    detail = base64.b64decode(fields[4]).decode(
                        "utf-8",
                        errors="replace",
                    )
                except Exception:
                    detail = fields[4]
                message = (
                    f"{fields[3]} for {fields[2]} on {self.port}: {detail}"
                )
                failures = {
                    "state_setup_failure": ReplayStateSetupError,
                    "output_serialization_failure": (
                        ReplayOutputSerializationError
                    ),
                    "allocator_memory_failure": ReplayAllocatorMemoryError,
                    "allocator_failure": ReplayAllocatorError,
                }
                failure_class = failures.get(
                    fields[3],
                    ReplayTransportError,
                )
                diagnostics: dict[str, int] = {}
                if len(fields) >= 8:
                    try:
                        diagnostics = {
                            "heap_free_before": int(fields[5]),
                            "heap_free_after": int(fields[6]),
                            "elapsed_until_failure_us": int(fields[7]),
                        }
                    except (TypeError, ValueError) as exc:
                        raise ReplayTransportError(
                            "malformed persistent failure diagnostics "
                            f"on {self.port}: {'|'.join(fields[5:8])}"
                        ) from exc
                if failure_class in {
                    ReplayAllocatorMemoryError,
                    ReplayAllocatorError,
                }:
                    raise failure_class(message, **diagnostics)
                raise failure_class(message)
            if expected is None or command in expected:
                return fields
        raise ReplayTransportError(
            f"no protocol response from {self.port} before host deadline"
        )

    @staticmethod
    def _identity(fields: list[str], port: str) -> DeviceIdentity:
        if len(fields) < 8:
            raise ReplayTransportError(f"malformed identity response on {port}")
        return DeviceIdentity(
            port=port,
            device_id=fields[2],
            build_id=fields[3],
            implementation=fields[4],
            frequency_hz=int(fields[5]),
            heap_free=int(fields[6]),
            firmware_sha256=fields[7],
        )

    def start_worker(self) -> DeviceIdentity:
        restart_hook = getattr(
            self.serial,
            "restart_clean_worker",
            None,
        )
        if callable(restart_hook):
            self.identity = restart_hook()
            return self.identity
        return self.restart_clean_worker()

    def _read_until_bytes(
        self,
        ending: bytes,
        *,
        timeout_seconds: float,
    ) -> bytes:
        deadline = time.monotonic() + timeout_seconds
        data = bytearray()
        while time.monotonic() < deadline:
            try:
                item = self.serial.read(1)
            except Exception as exc:
                raise ReplayTransportError(
                    f"serial read failed on {self.port}: {exc}"
                ) from exc
            if not item:
                continue
            data.extend(item)
            if data.endswith(ending):
                return bytes(data)
        raise ReplayTransportError(
            f"raw REPL on {self.port} did not reach {ending!r}; "
            f"tail={bytes(data[-160:])!r}"
        )

    def restart_clean_worker(self) -> DeviceIdentity:
        """Soft-reset into raw REPL and start a fresh motor-free worker.

        Raw-REPL soft reset deliberately bypasses ``main.py``. It clears the
        MicroPython heap and interned module state between conditions, matching
        a native one-allocator boot without initializing the robot program.
        """

        try:
            # A replay worker deliberately catches Ctrl-C so it can emit an
            # INTERRUPTED acknowledgement.  Ask an idle worker to exit first;
            # if it is still inside an allocator call, interrupt it and then
            # ask it to exit.  This leaves raw REPL in control before the soft
            # reset below and avoids treating a healthy board as disconnected.
            stopped = False
            try:
                self._write_line(PROTOCOL, "EXIT")
                fields = self._read_protocol(
                    deadline=time.monotonic()
                    + (5.0 if self.identity is not None else 0.35),
                    expected={"BYE"},
                )
                stopped = len(fields) >= 2 and fields[1] == "BYE"
            except Exception:
                pass
            if not stopped:
                try:
                    self.serial.write(b"\x03")
                    self.serial.flush()
                    self._read_protocol(
                        deadline=time.monotonic()
                        + (5.0 if self.identity is not None else 0.75),
                        expected={"INTERRUPTED"},
                    )
                    self._write_line(PROTOCOL, "EXIT")
                    fields = self._read_protocol(
                        deadline=time.monotonic() + 1.0,
                        expected={"BYE"},
                    )
                    stopped = len(fields) >= 2 and fields[1] == "BYE"
                except Exception:
                    # Raw/friendly REPL has no worker protocol to acknowledge.
                    # The normalization sequence below handles those states.
                    pass
            self.serial.reset_input_buffer()
            self.serial.write(b"\r\x03")
            self.serial.flush()
            time.sleep(0.05)
            self.serial.reset_input_buffer()
            self.serial.write(b"\r\x01")
            self.serial.flush()
            self._read_until_bytes(
                b"raw REPL; CTRL-B to exit\r\n>",
                timeout_seconds=8.0,
            )
            self.serial.write(b"\x04")
            self.serial.flush()
            self._read_until_bytes(
                b"soft reboot\r\n",
                timeout_seconds=8.0,
            )
            self._read_until_bytes(
                b"raw REPL; CTRL-B to exit\r\n",
                timeout_seconds=8.0,
            )
            self._read_until_bytes(b">", timeout_seconds=3.0)
            self.serial.write(
                b"import replay_worker; replay_worker.main()\x04"
            )
            self.serial.flush()
            acknowledgement = self._read_until_bytes(
                b"OK",
                timeout_seconds=3.0,
            )
            if not acknowledgement.endswith(b"OK"):
                raise ReplayTransportError(
                    f"raw worker start failed on {self.port}: "
                    f"{acknowledgement!r}"
                )
        except Exception as exc:
            if isinstance(exc, ReplayTransportError):
                raise
            raise ReplayTransportError(
                f"could not enter replay worker on {self.port}: {exc}"
            ) from exc
        fields = self._read_protocol(
            deadline=time.monotonic() + 8.0,
            expected={"READY"},
        )
        self.identity = self._identity(fields, self.port)
        return self.identity

    def hello(self) -> DeviceIdentity:
        self._write_line(PROTOCOL, "HELLO")
        fields = self._read_protocol(
            deadline=time.monotonic() + 3.0,
            expected={"HELLO"},
        )
        self.identity = self._identity(fields, self.port)
        return self.identity

    def check(self) -> dict[str, Any]:
        self._write_line(PROTOCOL, "CHECK")
        fields = self._read_protocol(
            deadline=time.monotonic() + 3.0,
            expected={"CHECK"},
        )
        if len(fields) < 8:
            raise ReplayTransportError("malformed CHECK response")
        return {
            "double_array": fields[2] == "1",
            "motors_initialized": fields[3] == "1",
            "sensors_initialized": fields[4] == "1",
            "heap_free": int(fields[5]),
            "actual_module_set_sha256": fields[6],
            "expected_module_set_sha256": fields[7],
        }

    def _expect_ack(self, kind: str, value: object, timeout: float = 3.0) -> None:
        fields = self._read_protocol(
            deadline=time.monotonic() + timeout,
            expected={"ACK"},
        )
        if len(fields) < 4 or fields[2] != kind or fields[3] != str(value):
            raise ReplayTransportError(
                f"unexpected ACK on {self.port}: {'|'.join(fields)}"
            )

    def _load_header(self, fixture: dict[str, Any]) -> None:
        payload = canonical_json_bytes(fixture)
        fixture_id = fixture["fixture_id"]
        self._write_line(
            PROTOCOL,
            "BEGIN",
            fixture_id,
            len(payload),
            _crc32(payload),
        )
        self._expect_ack("BEGIN", fixture_id)
        for sequence, offset in enumerate(range(0, len(payload), CHUNK_BYTES)):
            chunk = payload[offset : offset + CHUNK_BYTES]
            encoded = base64.b64encode(chunk).decode("ascii")
            for retry in range(TRANSFER_RETRIES):
                self._write_line(
                    PROTOCOL,
                    "DATA",
                    sequence,
                    encoded,
                    _crc32(chunk),
                )
                try:
                    self._expect_ack("DATA", sequence)
                    break
                except ReplayTransportError:
                    if retry + 1 == TRANSFER_RETRIES:
                        raise
        self._write_line(PROTOCOL, "END")
        fields = self._read_protocol(
            deadline=time.monotonic() + 8.0,
            expected={"LOADED"},
        )
        if len(fields) < 3 or fields[2] != fixture_id:
            raise ReplayTransportError(
                f"wrong fixture loaded on {self.port}: {'|'.join(fields)}"
            )

    @staticmethod
    def _batch(values: list[Any]) -> list[list[Any]]:
        batches: list[list[Any]] = []
        current: list[Any] = []
        current_size = 2
        for value in values:
            size = len(canonical_json_bytes(value)) + 1
            if current and current_size + size > PART_PAYLOAD_BYTES:
                batches.append(current)
                current = []
                current_size = 2
            current.append(value)
            current_size += size
        if current:
            batches.append(current)
        return batches

    def _send_part(
        self,
        section: str,
        name: str,
        kind: str,
        reset: bool,
        value: Any,
    ) -> None:
        payload = canonical_json_bytes(value)
        encoded_name = base64.b64encode(name.encode("utf-8")).decode("ascii")
        self._write_line(
            PROTOCOL,
            "PBEGIN",
            section,
            encoded_name,
            kind,
            int(reset),
            len(payload),
            _crc32(payload),
        )
        self._expect_ack("PBEGIN", encoded_name)
        for sequence, offset in enumerate(range(0, len(payload), CHUNK_BYTES)):
            chunk = payload[offset : offset + CHUNK_BYTES]
            encoded = base64.b64encode(chunk).decode("ascii")
            for retry in range(TRANSFER_RETRIES):
                self._write_line(
                    PROTOCOL,
                    "PDATA",
                    sequence,
                    encoded,
                    _crc32(chunk),
                )
                try:
                    self._expect_ack("PDATA", sequence)
                    break
                except ReplayTransportError:
                    if retry + 1 == TRANSFER_RETRIES:
                        raise
        self._write_line(PROTOCOL, "PEND")
        fields = self._read_protocol(
            deadline=time.monotonic() + 8.0,
            expected={"PART"},
        )
        if len(fields) < 4 or fields[2] != section or fields[3] != encoded_name:
            raise ReplayTransportError("wrong PART response")

    def load_fixture(self, fixture: dict[str, Any]) -> None:
        projected = (
            project_device_fixture(fixture)
            if "post_state" in fixture.get("expected", {})
            else fixture
        )
        state = projected["pre_state"]
        header = {
            key: value
            for key, value in projected.items()
            if key != "pre_state"
        }
        header["pre_state"] = {
            "robot_attrs": {},
            "views": {},
            "cfg": {},
            "belief": {},
            "allocator_attrs": {},
        }
        self._load_header(header)
        for section in (
            "robot_attrs",
            "views",
            "cfg",
            "belief",
            "allocator_attrs",
        ):
            for name, value in state[section].items():
                encoded_size = len(canonical_json_bytes(value))
                if (
                    isinstance(value, dict)
                    and value.get("@") == "arraybin"
                    and encoded_size > PART_PAYLOAD_BYTES
                ):
                    raw = base64.b64decode(value["v"])
                    item_size = array.array(value["typecode"]).itemsize
                    chunk_size = max(
                        item_size,
                        (ARRAY_PART_RAW_BYTES // item_size) * item_size,
                    )
                    for index, offset in enumerate(
                        range(0, len(raw), chunk_size)
                    ):
                        self._send_part(
                            section,
                            name,
                            "array_bytes",
                            index == 0,
                            {
                                "typecode": value["typecode"],
                                "byteorder": value["byteorder"],
                                "v": base64.b64encode(
                                    raw[offset : offset + chunk_size]
                                ).decode("ascii"),
                            },
                        )
                elif isinstance(value, list) and encoded_size > PART_PAYLOAD_BYTES:
                    for index, batch in enumerate(self._batch(value)):
                        self._send_part(
                            section,
                            name,
                            "list_items",
                            index == 0,
                            batch,
                        )
                elif (
                    isinstance(value, dict)
                    and value.get("@") == "set"
                    and encoded_size > PART_PAYLOAD_BYTES
                ):
                    # Decode directly into one resident set in bounded
                    # batches. Sending the encoded set wrapper as one "value"
                    # requires an 8+ KiB contiguous JSON buffer near the end
                    # of a 19x19 Bayesian mission.
                    for index, batch in enumerate(self._batch(value["v"])):
                        self._send_part(
                            section,
                            name,
                            "set_items",
                            index == 0,
                            batch,
                        )
                elif (
                    isinstance(value, dict)
                    and value.get("@") == "dict"
                    and encoded_size > PART_PAYLOAD_BYTES
                ):
                    for index, batch in enumerate(self._batch(value["v"])):
                        self._send_part(
                            section,
                            name,
                            "dict_items",
                            index == 0,
                            batch,
                        )
                elif (
                    isinstance(value, dict)
                    and value.get("@") == "cellmap"
                    and encoded_size > PART_PAYLOAD_BYTES
                ):
                    pairs = value["v"]["v"]
                    for index, batch in enumerate(self._batch(pairs)):
                        self._send_part(
                            section,
                            name,
                            "cellmap_items",
                            index == 0,
                            {
                                "grid_size": value["grid_size"],
                                "numeric": value["numeric"],
                                "items": batch,
                            },
                        )
                else:
                    self._send_part(section, name, "value", True, value)

    def run_loaded(self, attempt_id: str, timeout_seconds: float) -> dict[str, Any]:
        self._write_line(PROTOCOL, "RUN", attempt_id)
        started = self._read_protocol(
            deadline=time.monotonic() + 5.0,
            expected={"START"},
        )
        if len(started) < 4 or started[2] != attempt_id:
            raise ReplayTransportError(
                f"wrong START response on {self.port}: {'|'.join(started)}"
            )
        deadline = time.monotonic() + timeout_seconds
        timed_result: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                fields = self._read_protocol(
                    deadline=min(deadline, time.monotonic() + 0.50),
                    expected={"TIMED", "RESULT"},
                )
            except ReplayTransportError as exc:
                # A short read deadline is normal while an allocator is still
                # running.  A closed or failed serial port is not.
                if not getattr(self.serial, "is_open", False):
                    raise
                if "no protocol response" in str(exc):
                    continue
                raise
            if len(fields) < 4:
                raise ReplayTransportError("malformed timed/result response")
            payload = base64.b64decode(fields[2])
            if _crc32(payload) != int(fields[3]):
                raise ReplayTransportError("result CRC mismatch")
            result = json.loads(payload.decode("utf-8"))
            if result.get("attempt_id") != attempt_id:
                raise ReplayTransportError("result attempt ID mismatch")
            if fields[1] == "RESULT":
                return result
            timed_result = result
            break
        if timed_result is None:
            raise ReplayTimeout(
                f"allocator attempt {attempt_id} exceeded {timeout_seconds:.3f}s"
            )
        # Parity and state hashing are deliberately outside the 30-second
        # allocator deadline.  They still need a finite transport watchdog so
        # a broken worker cannot block campaign orchestration indefinitely.
        fields = self._read_protocol(
            deadline=time.monotonic() + 300.0,
            expected={"RESULT"},
        )
        if len(fields) < 4:
            raise ReplayTransportError("malformed RESULT response")
        payload = base64.b64decode(fields[2])
        if _crc32(payload) != int(fields[3]):
            raise ReplayTransportError("result CRC mismatch")
        result = json.loads(payload.decode("utf-8"))
        if result.get("attempt_id") != attempt_id:
            raise ReplayTransportError("result attempt ID mismatch")
        for field, value in timed_result.items():
            if field in result and result[field] != value:
                raise ReplayTransportError(
                    f"TIMED/RESULT mismatch for {field}"
                )
        return result

    def run_authoritative_loaded(
        self,
        attempt_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self._write_line(PROTOCOL, "ARUN", attempt_id)
        started = self._read_protocol(
            deadline=time.monotonic() + 5.0,
            expected={"START"},
        )
        if len(started) < 4 or started[2] != attempt_id:
            raise ReplayTransportError(
                f"wrong START response on {self.port}: {'|'.join(started)}"
        )
        deadline = time.monotonic() + timeout_seconds
        timed_result: dict[str, Any] | None = None
        begin: list[str] | None = None
        while time.monotonic() < deadline:
            try:
                fields = self._read_protocol(
                    deadline=min(deadline, time.monotonic() + 0.50),
                    expected={"TIMED", "ABEGIN"},
                )
            except ReplayTransportError as exc:
                if not getattr(self.serial, "is_open", False):
                    raise
                if "no protocol response" in str(exc):
                    continue
                raise
            if fields[1] == "ABEGIN":
                # Construction, state restoration, and pre-timing allocator
                # failures legitimately have no TIMED record.  The chunked
                # authoritative result carries the structured failure and
                # must be consumed so the campaign can classify/retry it.
                begin = fields
                break
            payload = base64.b64decode(fields[2])
            if _crc32(payload) != int(fields[3]):
                raise ReplayTransportError("timed result CRC mismatch")
            timed_result = json.loads(payload.decode("utf-8"))
            break
        if timed_result is None and begin is None:
            raise ReplayTimeout(
                f"allocator attempt {attempt_id} exceeded {timeout_seconds:.3f}s"
            )
        if begin is None:
            begin = self._read_protocol(
                deadline=time.monotonic() + 300.0,
                expected={"ABEGIN"},
            )
        if len(begin) != 4:
            raise ReplayTransportError("malformed ABEGIN response")
        expected_length = int(begin[2])
        expected_crc = int(begin[3])
        buffer = bytearray()
        expected_sequence = 0
        while True:
            fields = self._read_protocol(
                deadline=time.monotonic() + 300.0,
                expected={"ADATA", "AEND"},
            )
            if fields[1] == "AEND":
                if len(fields) < 3 or int(fields[2]) != expected_sequence:
                    raise ReplayTransportError("authoritative chunk count mismatch")
                break
            if len(fields) != 5 or int(fields[2]) != expected_sequence:
                raise ReplayTransportError("authoritative chunk sequence mismatch")
            chunk = base64.b64decode(fields[3])
            if _crc32(chunk) != int(fields[4]):
                raise ReplayTransportError("authoritative chunk CRC mismatch")
            buffer.extend(chunk)
            expected_sequence += 1
        raw = bytes(buffer)
        if len(raw) != expected_length or _crc32(raw) != expected_crc:
            raise ReplayTransportError("authoritative result length/CRC mismatch")
        result = json.loads(raw.decode("utf-8"))
        if result.get("attempt_id") != attempt_id:
            raise ReplayTransportError("authoritative attempt ID mismatch")
        if timed_result is not None:
            for field, value in timed_result.items():
                if field in result and result[field] != value:
                    raise ReplayTransportError(
                        f"TIMED/authoritative mismatch for {field}"
                    )
        return result

    def interrupt(self) -> None:
        try:
            self.serial.write(b"\x03")
            self.serial.flush()
            self._read_protocol(
                deadline=time.monotonic() + 5.0,
                expected={"INTERRUPTED"},
            )
        except Exception as exc:
            raise ReplayTransportError(
                f"could not interrupt worker on {self.port}: {exc}"
            ) from exc

    def execute(
        self,
        fixture: dict[str, Any],
        attempt_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.load_fixture(fixture)
        result = self.run_loaded(attempt_id, timeout_seconds)
        self._write_line(PROTOCOL, "DROP")
        self._read_protocol(
            deadline=time.monotonic() + 3.0,
            expected={"DROPPED"},
        )
        return result

    def execute_authoritative(
        self,
        fixture: dict[str, Any],
        attempt_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.load_fixture(project_authoritative_fixture(fixture))
        result = self.run_authoritative_loaded(attempt_id, timeout_seconds)
        self._write_line(PROTOCOL, "DROP")
        self._read_protocol(
            deadline=time.monotonic() + 3.0,
            expected={"DROPPED"},
        )
        return result

    def begin_persistent_trial(self, config: dict[str, Any]) -> None:
        trial_key = str(config["trial_key"])
        fixture = {
            "schema": 1,
            "fixture_id": "persistent-trial/" + trial_key,
            "condition_id": config["condition_id"],
            "mission": config["mission"],
            "algorithm": config["algorithm"],
            "trial_key": trial_key,
            "trial_config": dict(config),
            "pre_state": _empty_state(),
        }
        try:
            self.load_fixture(fixture)
            self._write_line(PROTOCOL, "PTRIAL", trial_key)
            fields = self._read_protocol(
                deadline=time.monotonic() + 15.0,
                expected={"PTRIAL_READY"},
            )
        except ReplayMemoryError as exc:
            raise ReplayStateSetupError(str(exc)) from exc
        if len(fields) < 4 or fields[2] != trial_key:
            raise ReplayStateSetupError(
                f"wrong persistent trial acknowledgement: {'|'.join(fields)}"
            )

    def prepare_persistent_call(
        self,
        setup: dict[str, Any],
        attempt_id: str,
    ) -> None:
        try:
            if setup.get("setup_mode") == "restore":
                context_id = str(setup["context_id"])
                self._write_line(PROTOCOL, "PCLEAR", context_id)
                self._expect_ack("PCLEAR", context_id)
            self.load_fixture(project_persistent_setup(setup))
            self._write_line(PROTOCOL, "PSETUP", attempt_id)
            fields = self._read_protocol(
                deadline=time.monotonic() + 300.0,
                expected={"PCALL_READY"},
            )
        except ReplayMemoryError as exc:
            raise ReplayStateSetupError(str(exc)) from exc
        if (
            len(fields) < 5
            or fields[2] != attempt_id
            or fields[3] != str(setup["context_id"])
        ):
            raise ReplayStateSetupError(
                f"wrong ready-to-time acknowledgement: {'|'.join(fields)}"
            )
        self._persistent_ready_heap[attempt_id] = int(fields[4])

    def _read_persistent_field(
        self,
        *,
        deadline: float,
        expected_index: int,
    ) -> tuple[str, str, str, Any]:
        header = self._read_protocol(
            deadline=deadline,
            expected={"PFIELD"},
        )
        if len(header) != 8 or int(header[2]) != expected_index:
            raise ReplayOutputSerializationError(
                "malformed persistent result field"
            )
        kind = header[3]
        section = header[4]
        name = base64.b64decode(header[5]).decode("utf-8")
        expected_length = int(header[6])
        expected_crc = int(header[7])
        buffer = bytearray()
        expected_sequence = 0
        while True:
            frame = self._read_protocol(
                deadline=deadline,
                expected={"PFDATA", "PFEND"},
            )
            if frame[1] == "PFEND":
                if (
                    len(frame) != 4
                    or int(frame[2]) != expected_index
                    or int(frame[3]) != expected_sequence
                ):
                    raise ReplayOutputSerializationError(
                        "persistent result chunk count mismatch"
                    )
                break
            if (
                len(frame) != 6
                or int(frame[2]) != expected_index
                or int(frame[3]) != expected_sequence
            ):
                raise ReplayOutputSerializationError(
                    "persistent result chunk sequence mismatch"
                )
            chunk = base64.b64decode(frame[4])
            if _crc32(chunk) != int(frame[5]):
                raise ReplayOutputSerializationError(
                    "persistent result chunk CRC mismatch"
                )
            buffer.extend(chunk)
            expected_sequence += 1
        raw = bytes(buffer)
        if len(raw) != expected_length or _crc32(raw) != expected_crc:
            raise ReplayOutputSerializationError(
                "persistent result field length/CRC mismatch"
            )
        return kind, section, name, json.loads(raw.decode("utf-8"))

    def run_persistent_ready(
        self,
        attempt_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        # The deadline deliberately begins after PCALL_READY and immediately
        # before PTIME.  No fixture setup is included in allocator timeout.
        ready_heap = self._persistent_ready_heap.pop(attempt_id, None)
        host_started = time.monotonic()
        self._write_line(PROTOCOL, "PTIME", attempt_id)
        deadline = host_started + timeout_seconds
        try:
            timed = self._read_protocol(
                deadline=deadline,
                expected={"PTIMED"},
            )
        except ReplayTransportError as exc:
            if getattr(self.serial, "is_open", False) and (
                "no protocol response" in str(exc)
            ):
                raise ReplayTimeout(
                    f"allocator attempt {attempt_id} exceeded "
                    f"{timeout_seconds:.3f}s after PCALL_READY",
                    heap_free_at_ready=ready_heap,
                    host_elapsed_us=max(
                        0,
                        int((time.monotonic() - host_started) * 1_000_000),
                    ),
                    timeout_seconds=timeout_seconds,
                ) from exc
            raise
        if len(timed) != 12 or timed[2] != attempt_id:
            raise ReplayTransportError("malformed PTIMED response")
        result = {
            "attempt_id": attempt_id,
            "status": "completed",
            "failure_type": "",
            "allocator_time_us": int(timed[3]),
            "candidate_filter_time_us": int(timed[4]),
            "allocator_exclusive_time_us": int(timed[5]),
            "candidate_filter_calls": int(timed[6]),
            "heap_free_before": int(timed[7]),
            "heap_free_after": int(timed[8]),
            "candidate_count_before": int(timed[9]),
            "candidate_count_after": int(timed[10]),
            "call_class": timed[11],
        }
        output_deadline = time.monotonic() + 300.0
        output_stage = "result_begin"
        output_field_index = -1
        try:
            begin = self._read_protocol(
                deadline=output_deadline,
                expected={"PRESULT_BEGIN"},
            )
            if len(begin) != 4 or begin[2] != attempt_id:
                raise ReplayOutputSerializationError(
                    "malformed persistent result begin"
                )
            field_count = int(begin[3])
            messages: list[Any] = []
            state = _empty_state()
            resume_state: dict[str, Any] = {}
            goal = None
            for index in range(field_count):
                output_stage = "field"
                output_field_index = index
                kind, section, name, value = self._read_persistent_field(
                    deadline=output_deadline,
                    expected_index=index,
                )
                if kind == "goal":
                    goal = value
                elif kind == "message":
                    messages.append(value)
                elif kind == "state" and section in state:
                    state[section][name] = value
                elif kind == "resume":
                    resume_state[name] = value
                else:
                    raise ReplayOutputSerializationError(
                        f"unknown persistent output field {kind}/{section}"
                    )
            output_stage = "result_end"
            end = self._read_protocol(
                deadline=output_deadline,
                expected={"PRESULT_END"},
            )
            if (
                len(end) != 4
                or end[2] != attempt_id
                or int(end[3]) != field_count
            ):
                raise ReplayOutputSerializationError(
                    "malformed persistent result end"
                )
        except ReplayOutputSerializationError as exc:
            exc.timed_result = dict(result)
            exc.output_stage = output_stage
            exc.output_field_index = output_field_index
            raise
        except ReplayTransportError as exc:
            if isinstance(
                exc,
                (ReplayOutputSerializationError, ReplayAllocatorError),
            ):
                raise
            if getattr(self.serial, "is_open", False):
                wrapped = ReplayOutputSerializationError(str(exc))
                wrapped.timed_result = dict(result)
                raise wrapped from exc
            raise
        result["goal"] = goal
        result["messages"] = messages
        result["post_state"] = state
        result["resume_state"] = resume_state
        return result

    def execute_persistent(
        self,
        setup: dict[str, Any],
        attempt_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        self.prepare_persistent_call(setup, attempt_id)
        return self.run_persistent_ready(attempt_id, timeout_seconds)

    def end_persistent_trial(self) -> None:
        self._write_line(PROTOCOL, "PENDTRIAL")
        fields = self._read_protocol(
            deadline=time.monotonic() + 5.0,
            expected={"PTRIAL_ENDED"},
        )
        if len(fields) < 3:
            raise ReplayTransportError("malformed PTRIAL_ENDED response")

    def exit(self) -> None:
        self._write_line(PROTOCOL, "EXIT")
        self._read_protocol(
            deadline=time.monotonic() + 3.0,
            expected={"BYE"},
        )


def build_path_from_manifest(path: Path) -> str:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    return str(manifest["build_id"])
