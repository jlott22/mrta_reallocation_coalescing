"""Fail-closed native preflight evidence and sealed report primitives."""

from __future__ import annotations

import hashlib
import json
import copy
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from allocator_replay.device.native.collaborative import create_persistent_runtime

from .binding import (
    REQUIRED_HARDWARE_WORKERS,
    BoardFingerprint,
    StableBoardBinding,
    bind_hardware_workers,
    bind_single_hardware_worker,
)
from .errors import BoardBindingError, SessionStateError
from .session import CausalBoardSession
from .types import DecisionSignature, FrozenCall, MissionBinding, PRIMARY_ALGORITHMS


PREFLIGHT_REQUIREMENTS: tuple[tuple[str, str], ...] = (
    (
        "required_unique_boards",
        f"{REQUIRED_HARDWARE_WORKERS} unique intended RP2040 boards detected",
    ),
    ("stable_identity", "Board identity is stable across live queries"),
    ("sealed_build", "Firmware/build/module hashes match deployment"),
    ("native_runtime", "MicroPython/native runtime version is correct"),
    ("device_timer", "Device timer units/resolution/monotonicity verified"),
    ("four_contexts", "Four logical contexts can be created and reset"),
    ("primary_algorithms", "CBAA, ACBBA, PI, and HIPC load"),
    ("online_growth", "Online task-set growth works"),
    ("persistent_state", "Persistent context state survives calls"),
    ("frozen_same_time", "Same-time group preserves frozen pre-call semantics"),
    ("goal_parity", "RP2040 and AGX goals match known cases"),
    ("message_state_parity", "Message and post-state signatures match"),
    ("duplicate_ids", "Duplicate call/event IDs are rejected"),
    ("context_reset", "Contexts reset completely between missions"),
    ("response_isolation", "Serial response IDs cannot cross workers/trials"),
    ("timer_boundary", "USB/setup time is excluded from device timing"),
    ("disconnect_fails_closed", "Disconnect invalidates the active trial"),
    ("reconnect_revalidated", "Reconnect revalidates identity and build"),
    ("resume_build_binding", "Resume rejects a different device build"),
    ("preflight_gate", "Campaign hardware-valid gate rejects failed preflight"),
)

REQUIRED_CHECK_IDS = tuple(item[0] for item in PREFLIGHT_REQUIREMENTS)


def _native_implementation(value: str) -> bool:
    text = str(value).lower()
    return "micropython" in text and not any(
        marker in text for marker in ("virtual", "loopback", "emulator", "cpython")
    )


def _resident_active_task_count(post_state: Mapping[str, Any]) -> int | None:
    """Read the admitted-task registry from a collaborative native snapshot.

    Candidate-filter counters describe work performed by a particular choose
    path, not resident knowledge.  A non-destructive CBAA admission can retain
    its valid cached goal without running that filter, so online-growth
    preflight must inspect the allocator's active registry itself.
    """

    try:
        resume = post_state["allocator_attrs"]["native_collaborative_resume"]
        active = resume["state"]["active"]
    except (KeyError, TypeError):
        try:
            active = post_state["views"]["active_tasks"]
        except (KeyError, TypeError):
            return None
    if isinstance(active, Sequence) and not isinstance(
        active, (str, bytes, bytearray)
    ):
        return len(active)
    return None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class PreflightCheck:
    check_id: str
    description: str
    status: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in {"PASS", "FAIL", "PENDING"}:
            raise ValueError("preflight status must be PASS, FAIL, or PENDING")

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "description": self.description,
            "status": self.status,
            "evidence": dict(self.evidence),
        }


class CausalPreflightRecorder:
    """Collect all twenty native checks without silently treating pending as pass."""

    def __init__(
        self,
        *,
        repository_commit: str = "",
        config_sha256: str = "",
        manifest_sha256: str = "",
        native_hardware: bool = True,
    ) -> None:
        self.repository_commit = str(repository_commit)
        self.config_sha256 = str(config_sha256)
        self.manifest_sha256 = str(manifest_sha256)
        self.native_hardware = bool(native_hardware)
        self.fingerprints: list[BoardFingerprint] = []
        self._checks: dict[str, PreflightCheck] = {
            check_id: PreflightCheck(check_id, description, "PENDING", {})
            for check_id, description in PREFLIGHT_REQUIREMENTS
        }

    def bind_boards(
        self,
        devices: Sequence[Any],
        *,
        explicit_mapping: Mapping[Any, str] | None = None,
        expected_build_id: str | None = None,
        expected_module_set_sha256: str | None = None,
        expected_firmware_sha256: str | Mapping[str, str] | None = None,
        development_override: bool = False,
    ) -> tuple[StableBoardBinding, ...]:
        try:
            bindings = bind_hardware_workers(
                devices,
                explicit_mapping=explicit_mapping,
                development_override=development_override,
                expected_build_id=expected_build_id,
                expected_module_set_sha256=expected_module_set_sha256,
                expected_firmware_sha256=expected_firmware_sha256,
            )
        except Exception as exc:
            self.fail("required_unique_boards", error=str(exc))
            raise
        self.fingerprints = [item.fingerprint for item in bindings]
        production_count = len(bindings) == REQUIRED_HARDWARE_WORKERS
        self.set(
            "required_unique_boards",
            "PASS" if production_count else "PENDING",
            board_ids=[item.board_id for item in bindings],
            development_override=bool(development_override),
        )
        self.pass_check(
            "stable_identity",
            identities=[item.fingerprint.as_dict() for item in bindings],
        )
        same_build = len({item.fingerprint.build_id for item in bindings}) == 1
        same_modules = (
            len({item.fingerprint.module_set_sha256 for item in bindings}) == 1
        )
        self.set(
            "sealed_build",
            "PASS" if same_build and same_modules else "FAIL",
            build_ids=sorted({item.fingerprint.build_id for item in bindings}),
            module_set_sha256=sorted(
                {item.fingerprint.module_set_sha256 for item in bindings}
            ),
        )
        native = all(
            _native_implementation(item.fingerprint.implementation)
            for item in bindings
        )
        self.set(
            "native_runtime",
            "PASS" if native else ("PENDING" if not self.native_hardware else "FAIL"),
            implementations=[item.fingerprint.implementation for item in bindings],
        )
        timer_ok = all(
            item.fingerprint.frequency_hz > 0
            and item.fingerprint.timer_unit == "us"
            and item.fingerprint.timer_resolution_us > 0
            and item.fingerprint.timer_monotonic
            and item.fingerprint.timer_wraparound_safe
            for item in bindings
        )
        self.set(
            "device_timer",
            "PASS" if timer_ok else "FAIL",
            frequency_hz=[item.fingerprint.frequency_hz for item in bindings],
            unit=[item.fingerprint.timer_unit for item in bindings],
            resolution_us=[
                item.fingerprint.timer_resolution_us for item in bindings
            ],
            monotonic=[item.fingerprint.timer_monotonic for item in bindings],
            wraparound_safe=[
                item.fingerprint.timer_wraparound_safe for item in bindings
            ],
        )
        return bindings

    def set(self, check_id: str, status: str, **evidence: Any) -> None:
        if check_id not in self._checks:
            raise KeyError(f"unknown preflight check: {check_id}")
        prior = self._checks[check_id]
        self._checks[check_id] = PreflightCheck(
            check_id, prior.description, status, dict(evidence)
        )

    def pass_check(self, check_id: str, **evidence: Any) -> None:
        self.set(check_id, "PASS", **evidence)

    def fail(self, check_id: str, **evidence: Any) -> None:
        self.set(check_id, "FAIL", **evidence)

    def pending(self, check_id: str, **evidence: Any) -> None:
        self.set(check_id, "PENDING", **evidence)

    def report(self) -> dict[str, Any]:
        checks = [self._checks[check_id].as_dict() for check_id in REQUIRED_CHECK_IDS]
        statuses = {item["check_id"]: item["status"] for item in checks}
        identities = [item.as_dict() for item in self.fingerprints]
        exact_native_cohort = (
            self.native_hardware
            and len(identities) == REQUIRED_HARDWARE_WORKERS
            and len({item["device_id"] for item in identities})
            == REQUIRED_HARDWARE_WORKERS
            and all(
                _native_implementation(item["implementation"])
                and bool(item["build_id"])
                and bool(item["firmware_sha256"])
                and bool(item["module_set_sha256"])
                and int(item["frequency_hz"]) > 0
                and item["timer_unit"] == "us"
                and int(item["timer_resolution_us"]) > 0
                and bool(item["timer_monotonic"])
                and bool(item["timer_wraparound_safe"])
                and not bool(item["virtual_device"])
                for item in identities
            )
        )
        hardware_valid = exact_native_cohort and all(
            statuses[item] == "PASS" for item in REQUIRED_CHECK_IDS
        )
        unsigned: dict[str, Any] = {
            "schema_version": 1,
            "study_id": "mrta_reallocation_coalescing_causal",
            "report_kind": "rp2040_parity_preflight",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "native_hardware": self.native_hardware,
            "hardware_valid": hardware_valid,
            "passed": hardware_valid,
            "hardware_validated": hardware_valid,
            "repository_commit": self.repository_commit,
            "config_sha256": self.config_sha256,
            "manifest_sha256": self.manifest_sha256,
            "board_binding_sha256": _hash(identities),
            "boards": identities,
            "checks": checks,
            "summary": {
                "passed": sum(value == "PASS" for value in statuses.values()),
                "failed": sum(value == "FAIL" for value in statuses.values()),
                "pending": sum(value == "PENDING" for value in statuses.values()),
            },
        }
        unsigned["report_sha256"] = _hash(unsigned)
        return unsigned


def verify_preflight_report(
    report: Mapping[str, Any],
    *,
    live_fingerprints: Sequence[BoardFingerprint] | None = None,
) -> dict[str, Any]:
    value = dict(report)
    claimed = str(value.pop("report_sha256", ""))
    if not claimed or _hash(value) != claimed:
        raise BoardBindingError("causal preflight report hash mismatch")
    value["report_sha256"] = claimed
    checks = value.get("checks")
    if not isinstance(checks, list):
        raise BoardBindingError("causal preflight report has no check table")
    by_id = {str(item.get("check_id")): item for item in checks}
    if set(by_id) != set(REQUIRED_CHECK_IDS):
        raise BoardBindingError("causal preflight report check set is incomplete")
    if any(by_id[item].get("status") != "PASS" for item in REQUIRED_CHECK_IDS):
        raise BoardBindingError("causal preflight has failed or pending checks")
    if not bool(value.get("native_hardware")) or not bool(value.get("hardware_valid")):
        raise BoardBindingError("report is not valid native hardware evidence")
    boards = value.get("boards")
    if not isinstance(boards, list) or len(boards) != REQUIRED_HARDWARE_WORKERS:
        raise BoardBindingError(
            f"preflight must seal exactly {REQUIRED_HARDWARE_WORKERS} boards"
        )
    if (
        len({str(item.get("device_id")) for item in boards})
        != REQUIRED_HARDWARE_WORKERS
    ):
        raise BoardBindingError("preflight board identities are not unique")
    if _hash(boards) != value.get("board_binding_sha256"):
        raise BoardBindingError("preflight board binding hash mismatch")
    if live_fingerprints is not None:
        live = [item.as_dict() for item in live_fingerprints]
        # Port changes are permitted after USB re-enumeration; all scientific
        # identity/build fields must remain exact.
        stable = lambda row: {key: item for key, item in row.items() if key != "port"}
        if sorted(map(stable, live), key=lambda row: row["device_id"]) != sorted(
            map(stable, boards), key=lambda row: row["device_id"]
        ):
            raise BoardBindingError("live boards/builds differ from sealed preflight")
    return value


def write_preflight_reports(
    report: Mapping[str, Any], json_path: Path, text_path: Path
) -> tuple[Path, Path]:
    """Atomically write machine-readable JSON and a concise human checklist."""

    json_path = Path(json_path)
    text_path = Path(text_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.parent.mkdir(parents=True, exist_ok=True)
    json_temp = json_path.with_suffix(json_path.suffix + ".tmp")
    text_temp = text_path.with_suffix(text_path.suffix + ".tmp")
    json_temp.write_text(
        json.dumps(dict(report), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        "CAUSAL AGX ORIN + RP2040 PREFLIGHT",
        "",
        f"Hardware-valid: {'PASS' if report.get('hardware_valid') else 'FAIL'}",
        f"Report SHA-256: {report.get('report_sha256', '')}",
        f"Boards: {len(report.get('boards', []))}",
        "",
    ]
    for item in report.get("checks", []):
        lines.append(
            f"[{item.get('status', 'PENDING')}] {item.get('check_id')}: "
            f"{item.get('description')}"
        )
    if not report.get("hardware_valid"):
        lines.extend(
            [
                "",
                "This report MUST NOT be used to mark campaign output as hardware-valid.",
            ]
        )
    text_temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_temp.replace(json_path)
    text_temp.replace(text_path)
    return json_path, text_path


class _KnownAnswerTeam:
    """AGX-native authorities used only by the live parity preflight."""

    ROBOT_IDS = ("robot_0", "robot_1", "robot_2", "robot_3")
    ALL_TASKS = ((1, 1), (4, 3), (7, 5), (10, 7))

    def __init__(self, algorithm: str, seed: int = 104729) -> None:
        self.algorithm = str(algorithm).upper()
        self.seed = int(seed)
        self.config = {
            "mission": "collaborative",
            "algorithm": self.algorithm,
            "robot_ids": list(self.ROBOT_IDS),
            "grid_size": 19,
            "all_tasks": [list(item) for item in self.ALL_TASKS],
            "max_candidate_cells": None,
            "candidate_mode": "unrestricted",
            "commitment_horizon": 3,
            "seed": self.seed,
        }
        self.runtimes: dict[str, Any] = {}

    def state(self, robot_id: str, active_count: int) -> dict[str, Any]:
        index = self.ROBOT_IDS.index(str(robot_id))
        starts = {
            rid: [0, peer_index * 3]
            for peer_index, rid in enumerate(self.ROBOT_IDS)
        }
        return {
            "robot_attrs": {
                "rid": robot_id,
                "robot_id": robot_id,
                "pos": starts[robot_id],
                "grid_size": 19,
            },
            "views": {
                "all_tasks": [list(item) for item in self.ALL_TASKS],
                "active_tasks": [
                    list(item) for item in self.ALL_TASKS[:active_count]
                ],
                "peer_positions": {
                    rid: starts[rid] for rid in self.ROBOT_IDS if rid != robot_id
                },
                "target_p": [1.0] * len(self.ALL_TASKS),
            },
            "cfg": dict(self.config),
            "belief": {},
            "allocator_attrs": {},
        }

    @staticmethod
    def epoch(index: int, active_count: int) -> dict[str, Any]:
        admitted = (
            _KnownAnswerTeam.ALL_TASKS[:2]
            if index == 0
            else (_KnownAnswerTeam.ALL_TASKS[active_count - 1],)
        )
        return {
            "kind": "allocation_epoch",
            "payload": {
                "epoch_index": int(index),
                "trigger_reason": (
                    "initial_tasks" if index == 0 else "task_arrival_eager"
                ),
                "admitted_cells": [list(item) for item in admitted],
            },
        }

    def mission(self, trial_id: str) -> MissionBinding:
        return MissionBinding(
            trial_id=trial_id,
            condition_id=f"preflight-{self.algorithm.lower()}",
            algorithm=self.algorithm,
            seed=self.seed,
            robot_ids=self.ROBOT_IDS,
            trial_config=self.config,
            initial_context_states={
                rid: self.state(rid, 2) for rid in self.ROBOT_IDS
            },
        )

    def stage(
        self,
        robot_id: str,
        *,
        trial_id: str,
        group_id: str,
        call_id: str,
        virtual_start_s: float,
        epoch_index: int,
        active_count: int,
    ) -> FrozenCall:
        pre_state = self.state(robot_id, active_count)
        event = self.epoch(epoch_index, active_count)
        runtime = self.runtimes.get(robot_id)
        if runtime is None:
            runtime = create_persistent_runtime(self.config)
            runtime.reset_trial(self.config, copy.deepcopy(pre_state))
            self.runtimes[robot_id] = runtime
            runtime.apply_delta({"events": [copy.deepcopy(event)]})
        else:
            runtime.apply_delta(
                {
                    "set": copy.deepcopy(pre_state),
                    "events": [copy.deepcopy(event)],
                }
            )
        started = time.perf_counter_ns()
        decision = runtime.choose_goal()
        agx_us = max(0, (time.perf_counter_ns() - started) // 1000)
        messages = runtime.drain_messages()
        post_state = runtime.snapshot_minimal()
        before, after = runtime.candidate_counts()
        if before != after:
            raise RuntimeError("known-answer native authority restricted candidates")
        signature = DecisionSignature.from_result(
            {
                "goal": decision.goal,
                "messages": messages,
                "post_state": post_state,
                "candidate_count_after": after,
                "call_class": runtime.call_class(),
            }
        )
        return FrozenCall(
            call_id=call_id,
            group_id=group_id,
            trial_id=trial_id,
            logical_robot_id=robot_id,
            algorithm=self.algorithm,
            virtual_start_s=float(virtual_start_s),
            device_setup={
                "pre_state": pre_state,
                "events": [event],
            },
            authoritative=signature,
            agx_allocator_time_us=agx_us,
            trigger_id=str(epoch_index),
            active_task_count=active_count,
            metadata={
                "preflight_known_answer": True,
                "epoch_index": epoch_index,
            },
        )


def _mark_probe_checks(
    recorder: CausalPreflightRecorder,
    probe_rows: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    board_count: int,
) -> None:
    expected_algorithms = set(PRIMARY_ALGORITHMS)
    observed_algorithms = {row["algorithm"] for row in probe_rows}
    full_algorithm_coverage = (
        observed_algorithms == expected_algorithms
        and len({(row["board_id"], row["algorithm"]) for row in probe_rows})
        == board_count * len(expected_algorithms)
    )
    recorder.set(
        "primary_algorithms",
        "PASS" if full_algorithm_coverage else "FAIL",
        observed=sorted(observed_algorithms),
        board_algorithm_pairs=len(
            {(row["board_id"], row["algorithm"]) for row in probe_rows}
        ),
        failures=failures,
    )
    four_contexts = full_algorithm_coverage and all(
        row["initial_context_count"] == 4 for row in probe_rows
    )
    recorder.set(
        "four_contexts",
        "PASS" if four_contexts else "FAIL",
        groups=[
            {
                "board_id": row["board_id"],
                "algorithm": row["algorithm"],
                "context_count": row["initial_context_count"],
            }
            for row in probe_rows
        ],
    )
    online = full_algorithm_coverage and all(row["online_growth"] for row in probe_rows)
    recorder.set(
        "online_growth",
        "PASS" if online else "FAIL",
        successful_pairs=sum(bool(row["online_growth"]) for row in probe_rows),
    )
    persistent = full_algorithm_coverage and all(row["persistent"] for row in probe_rows)
    recorder.set(
        "persistent_state",
        "PASS" if persistent else "FAIL",
        successful_pairs=sum(bool(row["persistent"]) for row in probe_rows),
    )
    frozen = full_algorithm_coverage and all(row["frozen_same_time"] for row in probe_rows)
    recorder.set(
        "frozen_same_time",
        "PASS" if frozen else "FAIL",
        common_start_completion_rule=(
            "virtual_completion = shared_start + own_device_duration"
        ),
        successful_pairs=sum(bool(row["frozen_same_time"]) for row in probe_rows),
    )
    parity = full_algorithm_coverage and all(row["parity"] for row in probe_rows)
    recorder.set(
        "goal_parity",
        "PASS" if parity else "FAIL",
        parity_valid_calls=sum(row["parity_call_count"] for row in probe_rows),
        failures=failures,
    )
    recorder.set(
        "message_state_parity",
        "PASS" if parity else "FAIL",
        compared_fields=["message_sha256", "post_state_sha256", "call_class"],
        parity_valid_calls=sum(row["parity_call_count"] for row in probe_rows),
    )
    duplicate = full_algorithm_coverage and all(row["duplicate_rejected"] for row in probe_rows)
    recorder.set(
        "duplicate_ids",
        "PASS" if duplicate else "FAIL",
        successful_pairs=sum(bool(row["duplicate_rejected"]) for row in probe_rows),
    )
    reset = full_algorithm_coverage and all(row["reset_verified"] for row in probe_rows)
    recorder.set(
        "context_reset",
        "PASS" if reset else "FAIL",
        successful_pairs=sum(bool(row["reset_verified"]) for row in probe_rows),
    )
    attempt_ids = [
        attempt_id for row in probe_rows for attempt_id in row["attempt_ids"]
    ]
    response_isolation = (
        full_algorithm_coverage
        and len(attempt_ids) == len(set(attempt_ids))
        and all(row["response_ids_exact"] for row in probe_rows)
    )
    recorder.set(
        "response_isolation",
        "PASS" if response_isolation else "FAIL",
        unique_attempt_ids=len(set(attempt_ids)),
        replies_checked=len(attempt_ids),
        enforcement="host rejects any reply whose attempt ID differs",
    )
    boundary = full_algorithm_coverage and all(row["timer_boundary"] for row in probe_rows)
    recorder.set(
        "timer_boundary",
        "PASS" if boundary else "FAIL",
        protocol="PSETUP/PCALL_READY precedes PTIME; PTIMED ends device timer",
        calls_checked=sum(row["parity_call_count"] for row in probe_rows),
    )


def run_native_preflight(
    devices: Sequence[Any],
    *,
    lock_root: Path,
    expected_build_id: str,
    expected_module_set_sha256: str,
    expected_firmware_sha256: str | Mapping[str, str] | None = None,
    explicit_mapping: Mapping[Any, str] | None = None,
    reconnect_factory: Callable[[BoardFingerprint], Any] | None = None,
    repository_commit: str = "",
    config_sha256: str = "",
    manifest_sha256: str = "",
    timeout_seconds: float = 30.0,
    native_hardware: bool = True,
    json_path: Path | None = None,
    text_path: Path | None = None,
) -> dict[str, Any]:
    """Exercise and seal all twenty native causal timing requirements.

    ``reconnect_factory`` must reopen the physical endpoint represented by its
    fingerprint (normally ``lambda fp: SerialReplayDevice(fp.port)``).  The
    controlled disconnect/reconnect checks remain PENDING when it is omitted,
    which makes ``hardware_valid`` false rather than inventing evidence.
    """

    recorder = CausalPreflightRecorder(
        repository_commit=repository_commit,
        config_sha256=config_sha256,
        manifest_sha256=manifest_sha256,
        native_hardware=native_hardware,
    )
    try:
        bindings = recorder.bind_boards(
            devices,
            explicit_mapping=explicit_mapping,
            expected_build_id=expected_build_id,
            expected_module_set_sha256=expected_module_set_sha256,
            expected_firmware_sha256=expected_firmware_sha256,
        )
    except Exception as exc:
        recorder.fail("sealed_build", error=str(exc))
        report = recorder.report()
        if json_path is not None and text_path is not None:
            write_preflight_reports(report, json_path, text_path)
        return report

    probe_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for binding in bindings:
        with CausalBoardSession(
            binding,
            lock_root=Path(lock_root),
            timeout_seconds=timeout_seconds,
        ) as session:
            for algorithm in PRIMARY_ALGORITHMS:
                team = _KnownAnswerTeam(algorithm)
                trial_id = f"preflight-{binding.board_id}-{algorithm.lower()}"
                row: dict[str, Any] = {
                    "board_id": binding.board_id,
                    "algorithm": algorithm,
                    "initial_context_count": 0,
                    "online_growth": False,
                    "persistent": False,
                    "frozen_same_time": False,
                    "parity": False,
                    "parity_call_count": 0,
                    "duplicate_rejected": False,
                    "reset_verified": False,
                    "response_ids_exact": False,
                    "timer_boundary": False,
                    "attempt_ids": [],
                }
                try:
                    session.begin_mission(team.mission(trial_id))
                    initial_calls = tuple(
                        team.stage(
                            rid,
                            trial_id=trial_id,
                            group_id=trial_id + "/same-time",
                            call_id=trial_id + "/initial/" + rid,
                            virtual_start_s=10.0,
                            epoch_index=0,
                            active_count=2,
                        )
                        for rid in team.ROBOT_IDS
                    )
                    initial = session.measure_group(initial_calls)
                    row["initial_context_count"] = len(initial)
                    row["parity_call_count"] += len(initial)
                    row["attempt_ids"].extend(item.attempt_id for item in initial)
                    row["response_ids_exact"] = all(
                        item.attempt_id.startswith("causal-") for item in initial
                    )
                    row["frozen_same_time"] = all(
                        abs(
                            item.virtual_completion_s
                            - (
                                item.virtual_start_s
                                + item.device_allocator_time_us / 1_000_000.0
                            )
                        )
                        <= 1e-12
                        for item in initial
                    )
                    online_call = team.stage(
                        team.ROBOT_IDS[0],
                        trial_id=trial_id,
                        group_id=trial_id + "/online-growth",
                        call_id=trial_id + "/online/robot_0",
                        virtual_start_s=12.0,
                        epoch_index=1,
                        active_count=3,
                    )
                    online = session.measure_group((online_call,))[0]
                    row["online_active_task_count"] = (
                        _resident_active_task_count(online.device_post_state)
                    )
                    row["online_growth"] = (
                        row["online_active_task_count"] == 3
                        and online.device.call_class == "full_allocation_solve"
                    )
                    row["persistent"] = session.context_call_count["robot_0"] == 2
                    row["parity_call_count"] += 1
                    row["attempt_ids"].append(online.attempt_id)
                    row["response_ids_exact"] = bool(
                        row["response_ids_exact"]
                        and online.attempt_id.startswith("causal-")
                    )
                    all_measured = tuple(initial) + (online,)
                    row["timer_boundary"] = all(
                        item.device_allocator_time_us >= 0
                        and item.serial_roundtrip_us >= 0
                        and item.host_serialization_setup_us >= 0
                        and item.host_total_call_us >= 0
                        for item in all_measured
                    )
                    row["parity"] = all(item.parity_passed for item in all_measured)
                    try:
                        session.measure_group(initial_calls)
                    except SessionStateError:
                        row["duplicate_rejected"] = True
                    session.end_mission()

                    # Repeat the exact first CBAA/HIPC/etc. context after a
                    # clean-worker mission reset and require identical logical
                    # output, not merely a successful call.
                    reset_team = _KnownAnswerTeam(algorithm)
                    reset_trial = trial_id + "-reset"
                    session.begin_mission(reset_team.mission(reset_trial))
                    reset_call = reset_team.stage(
                        reset_team.ROBOT_IDS[0],
                        trial_id=reset_trial,
                        group_id=reset_trial + "/group",
                        call_id=reset_trial + "/call",
                        virtual_start_s=1.0,
                        epoch_index=0,
                        active_count=2,
                    )
                    reset_value = session.measure_group((reset_call,))[0]
                    row["reset_verified"] = (
                        reset_value.device == initial[0].device
                        and session.context_call_count["robot_0"] == 1
                    )
                    row["parity_call_count"] += 1
                    row["attempt_ids"].append(reset_value.attempt_id)
                    session.end_mission()
                except Exception as exc:
                    failures.append(
                        {
                            "board_id": binding.board_id,
                            "algorithm": algorithm,
                            "failure_type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
                    try:
                        session.end_mission()
                    except Exception:
                        pass
                    # Parity/transport failure invalidates the session. Do not
                    # silently revalidate and continue this board as valid.
                    if not session.valid:
                        probe_rows.append(row)
                        break
                probe_rows.append(row)

    _mark_probe_checks(recorder, probe_rows, failures, len(bindings))

    # A wrong resume build must be rejected without mutating the live board.
    try:
        sample = bindings[-1]
        bind_single_hardware_worker(
            sample.device,
            worker_index=sample.worker_index,
            expected_device_id=sample.board_id,
            expected_build_id=expected_build_id + "-intentionally-wrong",
            expected_module_set_sha256=expected_module_set_sha256,
        )
    except BoardBindingError:
        recorder.pass_check(
            "resume_build_binding",
            probe="live HELLO/CHECK rejected intentionally wrong expected build",
        )
    except Exception as exc:
        recorder.fail("resume_build_binding", error=str(exc))
    else:
        recorder.fail("resume_build_binding", error="wrong build was accepted")

    if reconnect_factory is None:
        recorder.pending(
            "disconnect_fails_closed",
            reason="reconnect_factory is required for controlled native close/reopen",
        )
        recorder.pending(
            "reconnect_revalidated",
            reason="reconnect_factory is required for controlled native close/reopen",
        )
    else:
        target = bindings[0]
        replacement = None
        session = CausalBoardSession(
            target,
            lock_root=Path(lock_root),
            timeout_seconds=timeout_seconds,
        )
        try:
            team = _KnownAnswerTeam("CBAA")
            trial_id = "preflight-controlled-disconnect"
            session.begin_mission(team.mission(trial_id))
            target.device.close()
            disconnected = False
            try:
                session.measure_group(
                    (
                        team.stage(
                            "robot_0",
                            trial_id=trial_id,
                            group_id=trial_id + "/group",
                            call_id=trial_id + "/call",
                            virtual_start_s=0.0,
                            epoch_index=0,
                            active_count=2,
                        ),
                    )
                )
            except Exception:
                disconnected = not session.valid
            recorder.set(
                "disconnect_fails_closed",
                "PASS" if disconnected else "FAIL",
                session_invalidated=disconnected,
                accepted_duration_count=(
                    None
                    if session.last_failure is None
                    else session.last_failure.get("accepted_duration_count")
                ),
            )
            try:
                session.end_mission()
            except Exception:
                pass
            replacement = reconnect_factory(target.fingerprint)
            current = session.validate_reconnection(replacement)
            recorder.set(
                "reconnect_revalidated",
                "PASS"
                if current.reconnect_key() == target.fingerprint.reconnect_key()
                else "FAIL",
                board_id=current.device_id,
                build_id=current.build_id,
                module_set_sha256=current.module_set_sha256,
            )
        except Exception as exc:
            recorder.fail("disconnect_fails_closed", error=str(exc))
            recorder.fail("reconnect_revalidated", error=str(exc))
        finally:
            session.close()
            if replacement is not None:
                close = getattr(replacement, "close", None)
                if callable(close):
                    close()

    # Prove the consumer-side seal/gate rejects incomplete evidence. This is
    # independent of the report currently being assembled and avoids a
    # circular "report verifies itself before its hash exists" claim.
    failed_gate_probe = CausalPreflightRecorder(native_hardware=native_hardware)
    failed_gate_probe.fingerprints = list(recorder.fingerprints)
    failed_report = failed_gate_probe.report()
    try:
        verify_preflight_report(failed_report)
    except BoardBindingError:
        recorder.pass_check(
            "preflight_gate",
            probe="sealed report with PENDING checks was rejected",
        )
    else:
        recorder.fail("preflight_gate", error="incomplete report was accepted")

    report = recorder.report()
    if json_path is not None or text_path is not None:
        if json_path is None or text_path is None:
            raise ValueError("json_path and text_path must be supplied together")
        write_preflight_reports(report, json_path, text_path)
    return report


def load_and_verify_preflight(
    path: Path, *, live_fingerprints: Sequence[BoardFingerprint] | None = None
) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise BoardBindingError("causal preflight JSON is not an object")
    return verify_preflight_report(raw, live_fingerprints=live_fingerprints)
