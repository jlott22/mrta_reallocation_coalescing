from __future__ import annotations

import importlib
import base64
import binascii
import json
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from allocator_replay.host.deployment import load_build
from allocator_replay.host.transport import (
    DeviceIdentity,
    ReplayTimeout,
    ReplayTransportError,
    SerialReplayDevice,
    project_authoritative_fixture,
    project_device_fixture,
    project_persistent_setup,
)


_IMPORT_LOCK = threading.Lock()


class DesktopReplayDevice:
    """In-process serial-device substitute used by desktop campaign tests."""

    def __init__(
        self,
        device_id: str,
        *,
        build_root: Path | None = None,
        scripted_outcomes: dict[str, list[str]] | None = None,
    ) -> None:
        root, manifest = load_build(build_root)
        self.build_root = root
        self.scripted_outcomes = scripted_outcomes or {}
        self._outcome_indexes: dict[str, int] = {}
        with _IMPORT_LOCK:
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            importlib.invalidate_caches()
            self.worker = importlib.import_module("replay_worker")
        self.identity = DeviceIdentity(
            port=f"EMULATED:{device_id}",
            device_id=device_id,
            build_id=str(manifest["build_id"]),
            implementation="cpython-emulator",
            frequency_hz=125_000_000,
            heap_free=-1,
            firmware_sha256="desktop-emulator-firmware",
        )
        self.closed = False
        self.persistent_slot = self.worker.PersistentRuntimeSlot(
            self.worker._persistent_runtime
        )

    def hello(self) -> DeviceIdentity:
        if self.closed:
            raise ReplayTransportError("emulated device is closed")
        return self.identity

    def check(self) -> dict[str, Any]:
        _, manifest = load_build(self.build_root)
        return {
            "double_array": True,
            "motors_initialized": False,
            "sensors_initialized": False,
            "heap_free": -1,
            "actual_module_set_sha256": manifest[
                "deployed_module_set_sha256"
            ],
            "expected_module_set_sha256": manifest[
                "deployed_module_set_sha256"
            ],
        }

    def _scripted(self, fixture_id: str) -> str:
        outcomes = self.scripted_outcomes.get(fixture_id, [])
        index = self._outcome_indexes.get(fixture_id, 0)
        if index >= len(outcomes):
            return "run"
        self._outcome_indexes[fixture_id] = index + 1
        return outcomes[index]

    def execute(
        self,
        fixture: dict[str, Any],
        attempt_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        del timeout_seconds
        if self.closed:
            raise ReplayTransportError("emulated disconnect")
        scripted = self._scripted(fixture["fixture_id"])
        if scripted == "timeout":
            raise ReplayTimeout("scripted 30-second timeout")
        if scripted == "transport_error":
            self.closed = True
            raise ReplayTransportError("scripted USB disconnect")
        if scripted in {"memory_error", "parity_failure", "device_exception"}:
            return {
                "attempt_id": attempt_id,
                "fixture_id": fixture["fixture_id"],
                "condition_id": fixture["condition_id"],
                "device_id": self.identity.device_id,
                "status": "failed",
                "failure_type": scripted,
            }
        result = self.worker._run_fixture(
            project_device_fixture(fixture),
            attempt_id,
        )
        result["device_id"] = self.identity.device_id
        return result

    def execute_authoritative(
        self,
        fixture: dict[str, Any],
        attempt_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        del timeout_seconds
        if self.closed:
            raise ReplayTransportError("emulated disconnect")
        scripted = self._scripted(fixture["fixture_id"])
        if scripted == "timeout":
            raise ReplayTimeout("scripted 30-second timeout")
        if scripted == "transport_error":
            self.closed = True
            raise ReplayTransportError("scripted USB disconnect")
        if scripted in {"memory_error", "device_exception"}:
            return {
                "attempt_id": attempt_id,
                "fixture_id": fixture["fixture_id"],
                "condition_id": fixture["condition_id"],
                "device_id": self.identity.device_id,
                "status": "failed",
                "failure_type": scripted,
            }
        result = self.worker._run_authoritative(
            project_authoritative_fixture(fixture),
            attempt_id,
        )
        result["device_id"] = self.identity.device_id
        return result

    def begin_persistent_trial(self, config: dict[str, Any]) -> None:
        if self.closed:
            raise ReplayTransportError("emulated disconnect")
        self.persistent_slot.begin_trial(dict(config))

    def execute_persistent(
        self,
        setup: dict[str, Any],
        attempt_id: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        del timeout_seconds
        if self.closed:
            raise ReplayTransportError("emulated disconnect")
        scripted = self._scripted(setup["fixture_id"])
        if scripted == "timeout":
            raise ReplayTimeout("scripted 30-second timeout")
        if scripted == "transport_error":
            self.closed = True
            raise ReplayTransportError("scripted USB disconnect")
        projected = project_persistent_setup(setup)
        self.persistent_slot.prepare(
            projected["context_id"],
            projected["setup_mode"],
            projected["pre_state"],
            projected.get("deleted", {}),
            projected.get("events", []),
            projected.get("resume_state", {}),
            projected.get("state_aliases", []),
        )
        runtime = self.persistent_slot.runtime
        counters = self.worker._persistent_counters(runtime)
        samples = getattr(
            counters,
            "candidate_filter_time_us_samples",
            None,
        )
        if samples is not None:
            del samples[:]
        heap_before = self.worker._mem_free()
        started = self.worker.ticks_us()
        decision = runtime.choose_goal()
        measured_elapsed = max(
            0,
            self.worker.ticks_diff(self.worker.ticks_us(), started),
        )
        reported = decision if isinstance(decision, dict) else {}
        elapsed = int(reported.get("allocator_time_us", measured_elapsed))
        filter_us = int(
            reported.get(
                "candidate_filter_time_us",
                sum(samples) if samples is not None else 0,
            )
        )
        filter_calls = int(
            reported.get(
                "candidate_filter_calls",
                len(samples) if samples is not None else 0,
            )
        )
        before, after = self.worker._persistent_candidate_counts(runtime)
        class_method = getattr(runtime, "call_class", None)
        call_class = (
            reported.get(
                "call_class",
                reported.get(
                    "call_path",
                    (
                        class_method()
                        if callable(class_method)
                        else (
                            "candidate_filter_only"
                            if filter_calls
                            else "cached_or_maintenance"
                        )
                    ),
                ),
            )
        )
        if samples is not None:
            del samples[:]
        compact = runtime.snapshot_minimal()
        section_names = (
            "robot_attrs",
            "views",
            "cfg",
            "belief",
            "allocator_attrs",
        )
        sectioned = any(section in compact for section in section_names)
        return {
            "attempt_id": attempt_id,
            "status": "completed",
            "failure_type": "",
            "goal": self.worker.encode_value(
                decision.get("goal")
                if isinstance(decision, dict) and "goal" in decision
                else getattr(decision, "goal", decision)
            ),
            "messages": self.worker.encode_value(
                runtime.drain_messages()
            ),
            "post_state": compact if sectioned else {
                section: {} for section in section_names
            },
            "resume_state": {} if sectioned else compact,
            "allocator_time_us": elapsed,
            "candidate_filter_time_us": filter_us,
            "allocator_exclusive_time_us": int(
                reported.get(
                    "allocator_exclusive_time_us",
                    max(0, elapsed - filter_us),
                )
            ),
            "candidate_filter_calls": filter_calls,
            "heap_free_before": (
                reported.get("heap_free_before", heap_before)
                if reported.get("heap_free_before", heap_before) is not None
                else -1
            ),
            "heap_free_after": (
                reported.get("heap_free_after", self.worker._mem_free())
                if reported.get(
                    "heap_free_after",
                    self.worker._mem_free(),
                )
                is not None
                else -1
            ),
            "candidate_count_before": int(
                reported.get("candidate_count_before", before)
            ),
            "candidate_count_after": int(
                reported.get("candidate_count_after", after)
            ),
            "call_class": str(call_class),
        }

    def end_persistent_trial(self) -> None:
        self.persistent_slot.end_trial()

    def interrupt(self) -> None:
        if self.closed:
            raise ReplayTransportError("emulated disconnect")

    def close(self) -> None:
        self.closed = True


class _LoopbackSerial:
    """Minimal USB-serial emulator for the AR1 ASCII protocol."""

    def __init__(self, worker, identity: DeviceIdentity) -> None:
        self.worker = worker
        self.identity = identity
        self.responses: deque[bytes] = deque()
        self.lock = threading.Lock()
        self.fixture_buffer: bytearray | None = None
        self.fixture_meta: tuple[str, int, int] | None = None
        self.fixture: dict[str, Any] | None = None
        self.expected_sequence = 0
        self.part_buffer: bytearray | None = None
        self.part_meta: dict[str, Any] | None = None
        self.part_expected_sequence = 0
        self.worker_running = False
        self.is_open = True
        self.context_clear_count = 0
        self.persistent_slot = worker.PersistentRuntimeSlot(
            worker._persistent_runtime
        )

    @staticmethod
    def _crc(payload: bytes) -> int:
        return int(binascii.crc32(payload)) & 0xFFFFFFFF

    def _queue(self, *fields: object) -> None:
        value = "|".join(str(field) for field in fields) + "\n"
        with self.lock:
            self.responses.append(value.encode("utf-8"))

    def reset_input_buffer(self) -> None:
        with self.lock:
            self.responses.clear()

    def flush(self) -> None:
        return None

    def restart_clean_worker(self) -> DeviceIdentity:
        with self.lock:
            self.responses.clear()
        self.fixture_buffer = None
        self.fixture_meta = None
        self.fixture = None
        self.expected_sequence = 0
        self.part_buffer = None
        self.part_meta = None
        self.part_expected_sequence = 0
        self.persistent_slot = self.worker.PersistentRuntimeSlot(
            self.worker._persistent_runtime
        )
        self.worker_running = True
        return self.identity

    def close(self) -> None:
        self.is_open = False

    def write(self, payload: bytes) -> int:
        if not self.is_open:
            raise OSError("loopback serial closed")
        if b"\x03" in payload:
            if self.worker_running:
                self.fixture = None
                self.fixture_buffer = None
                self._queue("AR1", "INTERRUPTED", -1)
                self.persistent_slot.end_trial()
            payload = payload.replace(b"\x03", b"")
        text = payload.decode("utf-8", errors="ignore")
        if "import replay_worker; replay_worker.main()" in text:
            self.worker_running = True
            item = self.identity
            self._queue(
                "AR1",
                "READY",
                item.device_id,
                item.build_id,
                item.implementation,
                item.frequency_hz,
                item.heap_free,
                item.firmware_sha256,
            )
            return len(payload)
        for line in text.splitlines():
            if not line.startswith("AR1|"):
                continue
            fields = line.split("|")
            command = fields[1]
            if command == "HELLO":
                item = self.identity
                self._queue(
                    "AR1",
                    "HELLO",
                    item.device_id,
                    item.build_id,
                    item.implementation,
                    item.frequency_hz,
                    item.heap_free,
                    item.firmware_sha256,
                )
            elif command == "CHECK":
                import replay_build

                self._queue(
                    "AR1",
                    "CHECK",
                    1,
                    0,
                    0,
                    -1,
                    replay_build.MODULE_SET_SHA256,
                    replay_build.MODULE_SET_SHA256,
                )
            elif command == "BEGIN":
                self.fixture_meta = (
                    fields[2],
                    int(fields[3]),
                    int(fields[4]),
                )
                self.fixture_buffer = bytearray()
                self.expected_sequence = 0
                self.part_buffer = None
                self.part_meta = None
                self._queue("AR1", "ACK", "BEGIN", fields[2])
            elif command == "DATA":
                sequence = int(fields[2])
                chunk = base64.b64decode(fields[3])
                if (
                    self.fixture_buffer is None
                    or sequence != self.expected_sequence
                    or self._crc(chunk) != int(fields[4])
                ):
                    self._queue("AR1", "ERROR", "DATA", "invalid")
                else:
                    self.fixture_buffer.extend(chunk)
                    self.expected_sequence += 1
                    self._queue("AR1", "ACK", "DATA", sequence)
            elif command == "END":
                if self.fixture_buffer is None or self.fixture_meta is None:
                    self._queue("AR1", "ERROR", "END", "missing")
                else:
                    fixture_id, length, crc = self.fixture_meta
                    raw = bytes(self.fixture_buffer)
                    if len(raw) != length or self._crc(raw) != crc:
                        self._queue("AR1", "ERROR", "END", "crc")
                    else:
                        self.fixture = json.loads(raw.decode("utf-8"))
                        self.worker._allocator_class(self.fixture)
                        self._queue("AR1", "LOADED", fixture_id, -1)
            elif command == "PBEGIN":
                self.part_meta = {
                    "section": fields[2],
                    "name": base64.b64decode(fields[3]).decode("utf-8"),
                    "kind": fields[4],
                    "reset": bool(int(fields[5])),
                    "length": int(fields[6]),
                    "crc32": int(fields[7]),
                    "encoded_name": fields[3],
                }
                self.part_buffer = bytearray()
                self.part_expected_sequence = 0
                self._queue("AR1", "ACK", "PBEGIN", fields[3])
            elif command == "PDATA":
                sequence = int(fields[2])
                chunk = base64.b64decode(fields[3])
                if (
                    self.part_buffer is None
                    or sequence != self.part_expected_sequence
                    or self._crc(chunk) != int(fields[4])
                ):
                    self._queue("AR1", "ERROR", "PDATA", "invalid")
                else:
                    self.part_buffer.extend(chunk)
                    self.part_expected_sequence += 1
                    self._queue("AR1", "ACK", "PDATA", sequence)
            elif command == "PEND":
                if self.part_buffer is None or self.part_meta is None:
                    self._queue("AR1", "ERROR", "PEND", "missing")
                else:
                    raw = bytes(self.part_buffer)
                    if (
                        len(raw) != self.part_meta["length"]
                        or self._crc(raw) != self.part_meta["crc32"]
                    ):
                        self._queue("AR1", "ERROR", "PEND", "crc")
                    else:
                        try:
                            self.worker._apply_part(
                                self.fixture,
                                self.part_meta,
                                raw,
                            )
                            self._queue(
                                "AR1",
                                "PART",
                                self.part_meta["section"],
                                self.part_meta["encoded_name"],
                                -1,
                            )
                        except Exception as exc:
                            self._queue(
                                "AR1",
                                "ERROR",
                                "PEND",
                                type(exc).__name__,
                                str(exc),
                            )
                    self.part_buffer = None
                    self.part_meta = None
            elif command == "RUN":
                if self.fixture is None:
                    self._queue("AR1", "ERROR", "RUN", "missing")
                else:
                    attempt_id = fields[2]
                    self._queue(
                        "AR1",
                        "START",
                        attempt_id,
                        self.fixture["fixture_id"],
                    )
                    def send_timed(timed_result):
                        timed_raw = json.dumps(timed_result).encode("utf-8")
                        self._queue(
                            "AR1",
                            "TIMED",
                            base64.b64encode(timed_raw).decode("ascii"),
                            self._crc(timed_raw),
                        )

                    result = self.worker._run_fixture(
                        self.fixture,
                        attempt_id,
                        timed_callback=send_timed,
                    )
                    result["device_id"] = self.identity.device_id
                    raw = json.dumps(result).encode("utf-8")
                    self._queue(
                        "AR1",
                        "RESULT",
                        base64.b64encode(raw).decode("ascii"),
                        self._crc(raw),
                    )
            elif command == "ARUN":
                if self.fixture is None:
                    self._queue("AR1", "ERROR", "ARUN", "missing")
                else:
                    attempt_id = fields[2]
                    self._queue(
                        "AR1",
                        "START",
                        attempt_id,
                        self.fixture["fixture_id"],
                    )

                    def send_authoritative_timed(timed_result):
                        timed_raw = json.dumps(timed_result).encode("utf-8")
                        self._queue(
                            "AR1",
                            "TIMED",
                            base64.b64encode(timed_raw).decode("ascii"),
                            self._crc(timed_raw),
                        )

                    result = self.worker._run_authoritative(
                        self.fixture,
                        attempt_id,
                        timed_callback=send_authoritative_timed,
                    )
                    result["device_id"] = self.identity.device_id
                    raw = json.dumps(result).encode("utf-8")
                    self._queue("AR1", "ABEGIN", len(raw), self._crc(raw))
                    sequence = 0
                    for offset in range(0, len(raw), 384):
                        chunk = raw[offset:offset + 384]
                        self._queue(
                            "AR1",
                            "ADATA",
                            sequence,
                            base64.b64encode(chunk).decode("ascii"),
                            self._crc(chunk),
                        )
                        sequence += 1
                    self._queue("AR1", "AEND", sequence)
            elif command == "PTRIAL":
                if self.fixture is None:
                    self._queue("AR1", "ERROR", "PTRIAL", "missing")
                else:
                    config = dict(self.fixture.get("trial_config", {}))
                    for name in (
                        "mission",
                        "algorithm",
                        "condition_id",
                        "trial_key",
                    ):
                        if name in self.fixture:
                            config[name] = self.fixture[name]
                    self.persistent_slot.begin_trial(config)
                    trial_key = fields[2]
                    self.fixture = None
                    self._queue("AR1", "PTRIAL_READY", trial_key, -1)
            elif command == "PCLEAR":
                context_id = fields[2]
                self.persistent_slot.clear_context()
                self.context_clear_count += 1
                self.fixture = None
                self.fixture_buffer = None
                self.part_buffer = None
                self._queue(
                    "AR1",
                    "ACK",
                    "PCLEAR",
                    context_id,
                    -1,
                )
            elif command == "PSETUP":
                attempt_id = fields[2]
                try:
                    self.persistent_slot.prepare(
                        self.fixture["context_id"],
                        self.fixture["setup_mode"],
                        self.fixture["pre_state"],
                        self.fixture.get("deleted", {}),
                        self.fixture.get("events", []),
                        self.fixture.get("resume_state", {}),
                        self.fixture.get("state_aliases", []),
                    )
                    context_id = self.persistent_slot.context_id
                    self.fixture = None
                    self._queue(
                        "AR1",
                        "PCALL_READY",
                        attempt_id,
                        context_id,
                        -1,
                    )
                except Exception as exc:
                    encoded = base64.b64encode(
                        str(exc).encode("utf-8")
                    ).decode("ascii")
                    self._queue(
                        "AR1",
                        "PFAIL",
                        attempt_id,
                        "state_setup_failure",
                        encoded,
                    )
            elif command == "PTIME":
                self._run_persistent(fields[2])
            elif command == "PENDTRIAL":
                self.persistent_slot.end_trial()
                self.fixture = None
                self._queue("AR1", "PTRIAL_ENDED", -1)
            elif command == "DROP":
                self.fixture = None
                self.fixture_buffer = None
                self.fixture_meta = None
                self.part_buffer = None
                self.part_meta = None
                self._queue("AR1", "DROPPED", -1)
            elif command == "EXIT":
                self.persistent_slot.end_trial()
                self.worker_running = False
                self._queue("AR1", "BYE")
            else:
                self._queue("AR1", "ERROR", command, "unsupported")
        return len(payload)

    def _persistent_field(
        self,
        index: int,
        kind: str,
        section: str,
        name: object,
        value: Any,
        encode_replay_value: bool = False,
    ) -> None:
        length, checksum = self.worker._persistent_field_measure(
            value,
            encode_replay_value,
        )
        encoded_name = base64.b64encode(
            str(name).encode("utf-8")
        ).decode("ascii")
        self._queue(
            "AR1",
            "PFIELD",
            index,
            kind,
            section,
            encoded_name,
            length,
            checksum,
        )
        sequence = 0
        for chunk in self.worker.iter_json_chunks(
            value,
            encode_replay_value=encode_replay_value,
        ):
            self._queue(
                "AR1",
                "PFDATA",
                index,
                sequence,
                base64.b64encode(chunk).decode("ascii"),
                self._crc(chunk),
            )
            sequence += 1
        self._queue("AR1", "PFEND", index, sequence)

    def _persistent_fail(
        self,
        attempt_id: str,
        failure_type: str,
        exc: Exception,
        *,
        heap_free_before: int | None = None,
        heap_free_after: int | None = None,
        elapsed_until_failure_us: int | None = None,
    ) -> None:
        encoded = base64.b64encode(
            f"{type(exc).__name__}: {exc}".encode("utf-8")
        ).decode("ascii")
        fields: list[object] = [
            "AR1",
            "PFAIL",
            attempt_id,
            failure_type,
            encoded,
        ]
        if (
            heap_free_before is not None
            and heap_free_after is not None
            and elapsed_until_failure_us is not None
        ):
            fields.extend(
                (
                    heap_free_before,
                    heap_free_after,
                    elapsed_until_failure_us,
                )
            )
        self._queue(*fields)

    def _run_persistent(self, attempt_id: str) -> None:
        runtime = self.persistent_slot.runtime
        if runtime is None:
            self._persistent_fail(
                attempt_id,
                "state_setup_failure",
                RuntimeError("no active context"),
            )
            return
        counters = self.worker._persistent_counters(runtime)
        samples = getattr(
            counters,
            "candidate_filter_time_us_samples",
            None,
        )
        if samples is not None:
            del samples[:]
        heap_before = self.worker._mem_free()
        started = self.worker.ticks_us()
        try:
            decision = runtime.choose_goal()
            measured_elapsed = max(
                0,
                self.worker.ticks_diff(self.worker.ticks_us(), started),
            )
        except MemoryError as exc:
            elapsed_until_failure = max(
                0,
                self.worker.ticks_diff(
                    self.worker.ticks_us(),
                    started,
                ),
            )
            self._persistent_fail(
                attempt_id,
                "allocator_memory_failure",
                exc,
                heap_free_before=heap_before,
                heap_free_after=self.worker._mem_free(),
                elapsed_until_failure_us=elapsed_until_failure,
            )
            return
        except Exception as exc:
            elapsed_until_failure = max(
                0,
                self.worker.ticks_diff(
                    self.worker.ticks_us(),
                    started,
                ),
            )
            self._persistent_fail(
                attempt_id,
                "allocator_failure",
                exc,
                heap_free_before=heap_before,
                heap_free_after=self.worker._mem_free(),
                elapsed_until_failure_us=elapsed_until_failure,
            )
            return
        reported = decision if isinstance(decision, dict) else {}
        elapsed = int(reported.get("allocator_time_us", measured_elapsed))
        filter_us = int(
            reported.get(
                "candidate_filter_time_us",
                sum(samples) if samples is not None else 0,
            )
        )
        filter_calls = int(
            reported.get(
                "candidate_filter_calls",
                len(samples) if samples is not None else 0,
            )
        )
        before, after = self.worker._persistent_candidate_counts(runtime)
        class_method = getattr(runtime, "call_class", None)
        call_class = str(
            reported.get(
                "call_class",
                reported.get(
                    "call_path",
                    (
                        class_method()
                        if callable(class_method)
                        else (
                            "candidate_filter_only"
                            if filter_calls
                            else "cached_or_maintenance"
                        )
                    ),
                ),
            )
        )
        if samples is not None:
            del samples[:]
        self._queue(
            "AR1",
            "PTIMED",
            attempt_id,
            elapsed,
            filter_us,
            int(
                reported.get(
                    "allocator_exclusive_time_us",
                    max(0, elapsed - filter_us),
                )
            ),
            filter_calls,
            reported.get("heap_free_before", heap_before) or -1,
            reported.get("heap_free_after", self.worker._mem_free()) or -1,
            int(reported.get("candidate_count_before", before)),
            int(reported.get("candidate_count_after", after)),
            call_class,
        )
        try:
            goal = (
                decision.get("goal")
                if isinstance(decision, dict) and "goal" in decision
                else getattr(decision, "goal", decision)
            )
            messages = runtime.drain_messages()
            state = runtime.snapshot_minimal()
            sections = (
                "robot_attrs",
                "views",
                "cfg",
                "belief",
                "allocator_attrs",
            )
            sectioned = any(section in state for section in sections)
            state_values_encoded = bool(
                getattr(
                    runtime,
                    "snapshot_values_encoded",
                    True,
                )
            )
            state_count = 0
            if sectioned:
                for section in sections:
                    for name, value in state.get(section, {}).items():
                        state_count += (
                            self.worker._persistent_state_field_count(
                                section,
                                name,
                                value,
                                state_values_encoded,
                            )
                        )
            resume_count = 0 if sectioned else len(state)
            field_count = (
                1 + len(messages) + state_count + resume_count
            )
            self._queue(
                "AR1",
                "PRESULT_BEGIN",
                attempt_id,
                field_count,
            )
            index = 0
            self._persistent_field(
                index,
                "goal",
                "-",
                "-",
                goal,
                True,
            )
            index += 1
            for message_index, message in enumerate(messages):
                self._persistent_field(
                    index,
                    "message",
                    "-",
                    message_index,
                    message,
                    True,
                )
                index += 1
            for section in sections:
                values = state.get(section, {}) if sectioned else {}
                for name, value in values.items():
                    for (
                        wire_name,
                        wire_value,
                        encode_replay_value,
                    ) in (
                        self.worker._persistent_state_fields(
                            section,
                            name,
                            value,
                            state_values_encoded,
                        )
                    ):
                        self._persistent_field(
                            index,
                            "state",
                            section,
                            wire_name,
                            wire_value,
                            encode_replay_value,
                        )
                        index += 1
            if not sectioned:
                for name, value in state.items():
                    self._persistent_field(
                        index,
                        "resume",
                        "-",
                        name,
                        value,
                    )
                    index += 1
            self._queue("AR1", "PRESULT_END", attempt_id, index)
        except Exception as exc:
            self._persistent_fail(
                attempt_id,
                "output_serialization_failure",
                exc,
            )

    def readline(self) -> bytes:
        with self.lock:
            if self.responses:
                return self.responses.popleft()
        return b""


class LoopbackReplayDevice(SerialReplayDevice):
    """Full chunked-protocol loopback used for 1/2/3-device campaign tests."""

    def __init__(
        self,
        device_id: str,
        *,
        build_root: Path | None = None,
    ) -> None:
        root, manifest = load_build(build_root)
        with _IMPORT_LOCK:
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            importlib.invalidate_caches()
            worker = importlib.import_module("replay_worker")
        identity = DeviceIdentity(
            port=f"LOOPBACK:{device_id}",
            device_id=device_id,
            build_id=str(manifest["build_id"]),
            implementation="micropython-loopback",
            frequency_hz=125_000_000,
            heap_free=-1,
            firmware_sha256="loopback-firmware",
        )
        serial = _LoopbackSerial(worker, identity)
        super().__init__(
            identity.port,
            start_worker=True,
            serial_object=serial,
        )

    def restart_clean_worker(self) -> DeviceIdentity:
        """Reset the in-process VM with the same public API as hardware."""

        identity = self.serial.restart_clean_worker()
        self.identity = identity
        self._persistent_ready_heap.clear()
        return identity
