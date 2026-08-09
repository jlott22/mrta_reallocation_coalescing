from __future__ import annotations

import ast
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from allocator_replay.config.study import (
    ALGORITHMS,
    CALIBRATION_MEDIAN_TOLERANCE,
    CALL_TIMEOUT_SECONDS,
    RESULTS_ROOT,
)
from allocator_replay.host.deployment import load_build
from allocator_replay.host.transport import (
    ReplayAllocatorMemoryError,
    ReplayMemoryError,
    ReplayOutputSerializationError,
    ReplayStateSetupError,
    ReplayTimeout,
    ReplayTransportError,
    SerialReplayDevice,
)


PREFLIGHT_PATH = RESULTS_ROOT / "Preflight" / "device_preflight.json"
PERSISTENT_GRID_SIZE = 5
PERSISTENT_TASKS = ((1, 0), (2, 1), (3, 2))
PERSISTENT_PARITY_GOALS = {
    "bayesian": (1, 0),
    "collaborative": (1, 0),
}


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def verify_source_safety(build_root: Path) -> dict[str, object]:
    forbidden_prefixes = (
        "motor",
        "motors",
        "sensor",
        "sensors",
        "hardware",
        "pololu",
    )
    violations: list[str] = []
    for source in sorted(build_root.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".", 1)[0].lower()
                if root.startswith(forbidden_prefixes):
                    violations.append(f"{source.name}:{getattr(node, 'lineno', 0)}:{name}")
    return {
        "passed": not violations,
        "violations": violations,
        "main_module_present": any(
            (build_root / filename).exists()
            for filename in ("main.py", "main.mpy")
        ),
    }


def _empty_persistent_state() -> dict[str, dict[str, Any]]:
    return {
        "robot_attrs": {},
        "views": {},
        "cfg": {},
        "belief": {},
        "allocator_attrs": {},
    }


def _persistent_trial_config(
    mission: str,
    algorithm: str,
    trial_key: str,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "trial_key": trial_key,
        "condition_id": (
            f"preflight_persistent_{mission}_{algorithm.lower()}_k1"
        ),
        "mission": mission,
        "algorithm": algorithm,
        "top_k_level": "K=1",
        "top_k_rate": (
            1.0 / float(PERSISTENT_GRID_SIZE**2)
            if mission == "bayesian"
            else 1.0 / float(len(PERSISTENT_TASKS))
        ),
        "top_k_cells": 1,
        "max_candidate_cells": 1,
        "grid_size": PERSISTENT_GRID_SIZE,
        "robot_id": "00",
        "robot_ids": ["00"],
        "commitment_horizon": 3,
        "seed": 1009,
    }


def _persistent_restore_state(
    mission: str,
    algorithm: str = "CBAA",
) -> dict[str, dict[str, Any]]:
    state = _empty_persistent_state()
    if mission == "bayesian":
        probabilities = [
            [0.01, 0.90, 0.02, 0.01, 0.01],
            [0.01, 0.05, 0.80, 0.02, 0.01],
            [0.01, 0.02, 0.04, 0.70, 0.01],
            [0.01, 0.02, 0.03, 0.04, 0.05],
            [0.01, 0.01, 0.01, 0.01, 0.01],
        ]
        state["robot_attrs"] = {
            "rid": "00",
            "pos": [0, 0],
            "heading": [1, 0],
            "grid_size": PERSISTENT_GRID_SIZE,
            "current_goal": None,
            "last_goal": None,
            "collision_avoidance_active": False,
        }
        state["views"] = {
            "known_clues": [[4, 4]],
            "searched": [],
            "local_searched": [],
            "target_p": probabilities,
            "peer_positions": {},
            "known_obstacles": [],
            "obstacles": [],
        }
        state["cfg"] = {
            "trial_mode": "clue_search",
            "robot_ids": ["00"],
            "max_candidate_cells": 1,
            "commitment_horizon": 3,
        }
        state["belief"] = {"revision": 1}
        if algorithm == "DMCHBA":
            state["allocator_attrs"] = {
                "_workspace_n": 0,
                "_h_u": {"@": "array", "typecode": "d", "v": []},
                "_h_v": {"@": "array", "typecode": "d", "v": []},
                "_h_minv": {"@": "array", "typecode": "d", "v": []},
                "_h_p": {"@": "array", "typecode": "I", "v": []},
                "_h_way": {"@": "array", "typecode": "I", "v": []},
                "_h_used": {"@": "bytearray", "v": ""},
                "_h_assignment": {
                    "@": "array",
                    "typecode": "i",
                    "v": [],
                },
            }
        elif algorithm == "DGA":
            state["allocator_attrs"] = {
                "_repair_candidates_ref": None,
                "_repair_candidate_mask": {"@": "bytearray", "v": ""},
                "_repair_seen_mask": {"@": "bytearray", "v": ""},
            }
        return state

    state["robot_attrs"] = {
        "rid": "00",
        "robot_id": "00",
        "pos": [0, 0],
        "grid_size": PERSISTENT_GRID_SIZE,
    }
    state["views"] = {
        "active_tasks": [list(cell) for cell in PERSISTENT_TASKS],
        "peer_positions": {},
        "target_p": [0.90, 0.80, 0.70],
    }
    state["cfg"] = {
        "robot_ids": ["00"],
        "max_candidate_cells": 1,
        "commitment_horizon": 3,
    }
    return state


def _persistent_delta_state(
    mission: str,
    prior_goal: tuple[int, int],
) -> dict[str, dict[str, Any]]:
    state = _empty_persistent_state()
    state["robot_attrs"]["pos"] = list(prior_goal)
    if mission != "bayesian":
        state["robot_attrs"]["sequence"] = 1
        state["robot_attrs"]["completed_tasks"] = [list(prior_goal)]
    return state


def _persistent_setup(
    config: dict[str, Any],
    *,
    context_id: str,
    mode: str,
    state: dict[str, dict[str, Any]],
    phase: str,
) -> dict[str, Any]:
    return {
        "schema": 1,
        "fixture_id": f"{config['condition_id']}/{phase}",
        "condition_id": config["condition_id"],
        "mission": config["mission"],
        "algorithm": config["algorithm"],
        "context_id": context_id,
        "setup_mode": mode,
        "deleted": {},
        "events": [],
        "resume_state": {},
        "pre_state": state,
    }


def _decode_cell(value: Any) -> tuple[int, int] | None:
    if isinstance(value, dict) and value.get("@") == "tuple":
        value = value.get("v")
    try:
        if len(value) != 2:
            return None
        cell = int(value[0]), int(value[1])
    except (KeyError, TypeError, ValueError):
        return None
    if not all(0 <= coordinate < PERSISTENT_GRID_SIZE for coordinate in cell):
        return None
    return cell


def _encoded_mapping(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    tag = value.get("@")
    return tag is None or tag == "dict"


def _validate_persistent_result(
    result: Any,
    *,
    mission: str,
    require_filter: bool,
    expected_goal: tuple[int, int] | None = None,
) -> tuple[tuple[int, int] | None, list[str]]:
    errors: list[str] = []
    if not isinstance(result, dict):
        return None, ["result is not a mapping"]
    if result.get("status") != "completed":
        errors.append("status is not completed")

    goal = _decode_cell(result.get("goal"))
    if goal is None:
        errors.append("goal is not a valid in-grid cell")
    elif mission == "collaborative" and goal not in PERSISTENT_TASKS:
        errors.append("collaborative goal is not an active smoke target")
    if expected_goal is not None and goal != expected_goal:
        errors.append(
            f"goal parity mismatch: expected {expected_goal}, got {goal}"
        )

    messages = result.get("messages")
    if not isinstance(messages, list):
        errors.append("messages is not a list")
    elif any(not _encoded_mapping(message) for message in messages):
        errors.append("one or more allocator messages are not mappings")

    integer_fields = (
        "allocator_time_us",
        "candidate_filter_time_us",
        "allocator_exclusive_time_us",
        "candidate_filter_calls",
        "candidate_count_before",
        "candidate_count_after",
        "heap_free_before",
        "heap_free_after",
    )
    parsed: dict[str, int] = {}
    for name in integer_fields:
        value = result.get(name)
        if isinstance(value, bool):
            errors.append(f"{name} is not an integer")
            continue
        try:
            parsed[name] = int(value)
        except (TypeError, ValueError):
            errors.append(f"{name} is not an integer")
    for name in (
        "allocator_time_us",
        "candidate_filter_time_us",
        "allocator_exclusive_time_us",
        "candidate_filter_calls",
        "candidate_count_before",
        "candidate_count_after",
    ):
        if name in parsed and parsed[name] < 0:
            errors.append(f"{name} is negative")
    if {
        "allocator_time_us",
        "candidate_filter_time_us",
        "allocator_exclusive_time_us",
    } <= parsed.keys():
        total = parsed["allocator_time_us"]
        filtered = parsed["candidate_filter_time_us"]
        exclusive = parsed["allocator_exclusive_time_us"]
        if filtered > total:
            errors.append("candidate filter time exceeds allocator time")
        if exclusive != max(0, total - filtered):
            errors.append("allocator-exclusive time is inconsistent")
    if {
        "candidate_count_before",
        "candidate_count_after",
    } <= parsed.keys():
        if parsed["candidate_count_after"] > parsed["candidate_count_before"]:
            errors.append("candidate count grew during filtering")
        if parsed["candidate_count_after"] > 1:
            errors.append("K=1 smoke returned more than one candidate")
    if require_filter and parsed.get("candidate_filter_calls", 0) < 1:
        errors.append("fresh K=1 allocation did not invoke its candidate filter")
    if not str(result.get("call_class", "")).strip():
        errors.append("call classification is missing")
    if not isinstance(result.get("post_state"), dict):
        errors.append("post_state is not a mapping")
    if not isinstance(result.get("resume_state"), dict):
        errors.append("resume_state is not a mapping")
    return goal, errors


def _persistent_result_record(
    result: dict[str, Any],
    *,
    setup_succeeded: bool,
    validation_errors: list[str] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "status": result.get("status", ""),
        "setup_succeeded": setup_succeeded,
        "ready_to_time": setup_succeeded,
        "failure_type": result.get("failure_type", ""),
        "validation_errors": list(validation_errors or ()),
    }
    for name in (
        "allocator_time_us",
        "candidate_filter_time_us",
        "allocator_exclusive_time_us",
        "candidate_filter_calls",
        "candidate_count_before",
        "candidate_count_after",
        "heap_free_before",
        "heap_free_after",
        "call_class",
    ):
        if name in result:
            record[name] = result[name]
    return record


def _recover_after_timeout(
    device: SerialReplayDevice,
) -> dict[str, Any]:
    """Interrupt a timed call and prove that the worker accepts commands again.

    A successful Ctrl-C acknowledgement alone is not enough: a stale worker can
    acknowledge the interrupt but still be unusable by the next condition.  On
    real serial devices we therefore probe ``HELLO`` and, if necessary, enter a
    fresh replay worker before allowing preflight to continue.
    """

    recovery: dict[str, Any] = {
        "interrupt_succeeded": False,
        "worker_responsive": False,
        "worker_restarted": False,
        "passed": False,
        "errors": [],
    }
    try:
        device.interrupt()
        recovery["interrupt_succeeded"] = True
    except Exception as exc:
        recovery["errors"].append(
            f"interrupt: {type(exc).__name__}: {exc}"
        )

    hello = getattr(device, "hello", None)
    start_worker = getattr(device, "start_worker", None)
    if hello is None and start_worker is None:
        # Test doubles and direct in-process devices do not need a serial
        # health probe.  Their successful interrupt is the recovery proof.
        recovery["worker_responsive"] = recovery["interrupt_succeeded"]
        recovery["passed"] = recovery["worker_responsive"]
        return recovery

    if hello is not None:
        try:
            hello()
            recovery["worker_responsive"] = True
        except Exception as exc:
            recovery["errors"].append(
                f"hello: {type(exc).__name__}: {exc}"
            )
    if not recovery["worker_responsive"] and start_worker is not None:
        try:
            start_worker()
            recovery["worker_responsive"] = True
            recovery["worker_restarted"] = True
        except Exception as exc:
            recovery["errors"].append(
                f"restart: {type(exc).__name__}: {exc}"
            )
    recovery["passed"] = recovery["worker_responsive"]
    return recovery


def _end_persistent_trial_safely(
    device: SerialReplayDevice,
) -> dict[str, Any]:
    """End a preflight trial, recovering a clean worker if cleanup was stale."""

    try:
        device.end_persistent_trial()
        return {
            "passed": True,
            "restarted": False,
            "error": "",
        }
    except Exception as exc:
        record: dict[str, Any] = {
            "passed": False,
            "restarted": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
        start_worker = getattr(device, "start_worker", None)
        if start_worker is None:
            return record
        try:
            start_worker()
            record["passed"] = True
            record["restarted"] = True
        except Exception as restart_exc:
            record["restart_error"] = (
                f"{type(restart_exc).__name__}: {restart_exc}"
            )
        return record


def _isolate_persistent_worker(
    device: SerialReplayDevice,
    expected_identity: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    """Start one smoke/calibration group in a fresh allocator VM.

    Real serial devices expose ``restart_clean_worker`` so every condition can
    begin after a raw-REPL soft reset, matching the campaign's one-condition
    worker lifecycle.  Lightweight test doubles may omit that method; those
    remain compatible and explicitly record that isolation was unavailable.
    """

    restart = getattr(device, "restart_clean_worker", None)
    expected_device_id = str(
        getattr(expected_identity, "device_id", "")
    )
    record: dict[str, Any] = {
        "phase": phase,
        "method": "restart_clean_worker",
        "supported": callable(restart),
        "attempted": False,
        "performed": False,
        "identity_unchanged": None,
        "expected_device_id": expected_device_id,
        "passed": True,
    }
    if not callable(restart):
        record["method"] = "unavailable"
        record["reason"] = "restart_clean_worker_unavailable"
        return record

    record["attempted"] = True
    try:
        restarted_identity = restart()
        record["performed"] = True
    except Exception as exc:
        record["passed"] = False
        record["identity_unchanged"] = False
        record["failure_type"] = "worker_isolation_failure"
        record["error"] = f"{type(exc).__name__}: {exc}"
        return record

    actual_device_id = str(
        getattr(restarted_identity, "device_id", "")
    )
    record["actual_device_id"] = actual_device_id
    record["actual_identity"] = {
        name: getattr(restarted_identity, name, None)
        for name in (
            "device_id",
            "build_id",
            "implementation",
            "frequency_hz",
            "heap_free",
            "firmware_sha256",
        )
    }
    mismatches: list[dict[str, Any]] = []
    for name in (
        "device_id",
        "build_id",
        "implementation",
        "frequency_hz",
        "firmware_sha256",
    ):
        expected = getattr(expected_identity, name, None)
        if expected is None:
            continue
        actual = getattr(restarted_identity, name, None)
        if actual != expected:
            mismatches.append(
                {
                    "field": name,
                    "expected": expected,
                    "actual": actual,
                }
            )
    record["identity_mismatches"] = mismatches
    record["identity_unchanged"] = not mismatches
    if mismatches:
        record["passed"] = False
        record["failure_type"] = "worker_identity_changed"
        record["error"] = (
            "fresh worker returned a different device identity: "
            + ", ".join(
                f"{item['field']}={item['actual']!r} "
                f"(expected {item['expected']!r})"
                for item in mismatches
            )
        )
    return record


def _persistent_attempt(
    device: SerialReplayDevice,
    setup: dict[str, Any],
    attempt_id: str,
    *,
    mission: str,
    timeout_seconds: float,
    require_filter: bool,
    expected_goal: tuple[int, int] | None = None,
) -> tuple[
    dict[str, Any],
    tuple[int, int] | None,
    bool,
    dict[str, Any] | None,
]:
    try:
        device.prepare_persistent_call(setup, attempt_id)
    except Exception as exc:
        return (
            {
                "status": "failed",
                "setup_succeeded": False,
                "ready_to_time": False,
                "failure_type": type(exc).__name__,
                "error": str(exc),
                "deferred_to_campaign": False,
            },
            None,
            False,
            None,
        )

    try:
        result = device.run_persistent_ready(attempt_id, timeout_seconds)
    except (ReplayAllocatorMemoryError, ReplayMemoryError, ReplayTimeout) as exc:
        recovery = (
            _recover_after_timeout(device)
            if isinstance(exc, ReplayTimeout)
            else {
                "interrupt_succeeded": True,
                "worker_responsive": True,
                "worker_restarted": False,
                "passed": True,
                "errors": [],
                "required": False,
            }
        )
        return (
            {
                "status": "resource_limited",
                "setup_succeeded": True,
                "ready_to_time": True,
                "failure_type": type(exc).__name__,
                "error": str(exc),
                "deferred_to_campaign": True,
                "interrupt_succeeded": recovery[
                    "interrupt_succeeded"
                ],
                "worker_recovery": recovery,
            },
            None,
            bool(recovery["passed"]),
            None,
        )
    except (
        ReplayOutputSerializationError,
        ReplayStateSetupError,
        ReplayTransportError,
    ) as exc:
        return (
            {
                "status": "failed",
                "setup_succeeded": True,
                "ready_to_time": True,
                "failure_type": type(exc).__name__,
                "error": str(exc),
                "deferred_to_campaign": False,
            },
            None,
            False,
            None,
        )
    except Exception as exc:
        return (
            {
                "status": "failed",
                "setup_succeeded": True,
                "ready_to_time": True,
                "failure_type": type(exc).__name__,
                "error": str(exc),
                "deferred_to_campaign": False,
            },
            None,
            False,
            None,
        )

    goal, validation_errors = _validate_persistent_result(
        result,
        mission=mission,
        require_filter=require_filter,
        expected_goal=expected_goal,
    )
    record = _persistent_result_record(
        result,
        setup_succeeded=True,
        validation_errors=validation_errors,
    )
    if goal is not None:
        record["goal"] = list(goal)
    if expected_goal is not None:
        record["expected_goal"] = list(expected_goal)
        record["parity_passed"] = goal == expected_goal
    if validation_errors:
        record["status"] = "failed"
        record["failure_type"] = "invalid_persistent_output"
        record["error"] = "; ".join(validation_errors)
        return record, goal, False, result
    return record, goal, True, result


def _dga_context_restore_state(
    initial_state: dict[str, dict[str, Any]],
    returned_state: dict[str, Any],
    prior_goal: tuple[int, int],
) -> dict[str, dict[str, Any]]:
    """Build the next host snapshot from DGA's streamed minimal state."""

    restored = {
        section: dict(initial_state.get(section, {}))
        for section in _empty_persistent_state()
    }
    for section in restored:
        values = returned_state.get(section, {})
        if not isinstance(values, dict):
            raise TypeError(
                f"returned DGA {section} state is not a mapping"
            )
        if section == "allocator_attrs":
            restored[section] = dict(values)
        else:
            restored[section].update(values)
    next_host_state = _persistent_delta_state("bayesian", prior_goal)
    for section, values in next_host_state.items():
        restored[section].update(values)
    return restored


def _dga_stream_field_counts(
    returned_state: dict[str, Any],
) -> tuple[int, int]:
    robot_attrs = returned_state.get("robot_attrs", {})
    if not isinstance(robot_attrs, dict):
        return 0, 0
    names = tuple(str(name) for name in robot_attrs)
    return (
        sum(name.startswith("dga_rng_replay_rng_") for name in names),
        sum(
            name.startswith("dga_replay_population_")
            for name in names
        ),
    )


def run_persistent_checks(
    devices: Iterable[SerialReplayDevice],
    identities: Iterable[Any],
    device_checks: dict[str, dict[str, Any]],
    *,
    calibration_repetitions: int = 5,
    timeout_seconds: float = CALL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Exercise the exact persistent APIs used by stationary HIL.

    Most smokes use a fresh restore followed by a delta on the same resident
    context.  Bayesian DGA instead uses its second call to clear the context,
    restore the first call's streamed RNG/population state, and prove that the
    reconstructed runtime can continue.  Resource limits are deferrable only
    after ``PCALL_READY`` proves that construction and state application
    completed outside the timed call; the DGA context-restore proof itself is
    a required gate.
    """

    device_list = list(devices)
    identity_list = list(identities)
    if len(device_list) != len(identity_list):
        raise ValueError("persistent preflight devices/identities mismatch")
    if calibration_repetitions <= 0:
        raise ValueError("calibration_repetitions must be positive")

    motor_free_devices = [
        {
            "device_id": identity.device_id,
            "motors_initialized": bool(
                device_checks[identity.device_id]["motors_initialized"]
            ),
            "sensors_initialized": bool(
                device_checks[identity.device_id]["sensors_initialized"]
            ),
        }
        for identity in identity_list
    ]
    motor_free_passed = all(
        not item["motors_initialized"] and not item["sensors_initialized"]
        for item in motor_free_devices
    )

    smoke: list[dict[str, Any]] = []
    worker_isolation: list[dict[str, Any]] = []
    smoke_passed = True
    for device, identity in zip(device_list, identity_list):
        for mission in ("bayesian", "collaborative"):
            for algorithm in ALGORITHMS:
                context_id = "00"
                trial_key = (
                    f"preflight/{identity.device_id}/{mission}/"
                    f"{algorithm.lower()}"
                )
                config = _persistent_trial_config(
                    mission,
                    algorithm,
                    trial_key,
                )
                entry: dict[str, Any] = {
                    "device_id": identity.device_id,
                    "mission": mission,
                    "algorithm": algorithm,
                    "top_k_level": "K=1",
                    "top_k_cells": 1,
                    "context_id": context_id,
                    "restore": {"status": "not_run"},
                    "delta": {"status": "not_run"},
                }
                is_dga_context_gate = (
                    mission == "bayesian" and algorithm == "DGA"
                )
                if is_dga_context_gate:
                    entry["context_restore"] = {
                        "status": "not_run",
                        "passed": False,
                    }
                isolation = _isolate_persistent_worker(
                    device,
                    identity,
                    phase=f"smoke:{mission}:{algorithm}",
                )
                entry["worker_isolation"] = isolation
                worker_isolation.append(isolation)
                if not isolation["passed"]:
                    entry["status"] = "failed"
                    entry["failure_type"] = isolation.get(
                        "failure_type",
                        "worker_isolation_failure",
                    )
                    entry["error"] = isolation.get("error", "")
                    smoke_passed = False
                    smoke.append(entry)
                    continue
                trial_open = False
                try:
                    device.begin_persistent_trial(config)
                    trial_open = True
                    restore_setup = _persistent_setup(
                        config,
                        context_id=context_id,
                        mode="restore",
                        state=_persistent_restore_state(mission, algorithm),
                        phase="restore",
                    )
                    restore_id = (
                        f"preflight-persistent:{identity.device_id}:"
                        f"{mission}:{algorithm}:restore"
                    )
                    restore, goal, restore_ok, restore_result = (
                        _persistent_attempt(
                            device,
                            restore_setup,
                            restore_id,
                            mission=mission,
                            timeout_seconds=timeout_seconds,
                            require_filter=True,
                            expected_goal=(
                                PERSISTENT_PARITY_GOALS[mission]
                            ),
                        )
                    )
                    entry["restore"] = restore
                    if restore.get("status") == "resource_limited":
                        if is_dga_context_gate:
                            entry["context_restore"] = {
                                "status": "not_run",
                                "passed": False,
                                "failure_type": (
                                    "first_call_resource_limited"
                                ),
                            }
                        entry["status"] = "resource_limited"
                        entry["deferred_to_campaign"] = True
                        if not restore.get("worker_recovery", {}).get(
                            "passed", False
                        ):
                            smoke_passed = False
                            entry["status"] = "failed"
                        continue
                    if not restore_ok or goal is None:
                        if is_dga_context_gate:
                            entry["context_restore"] = {
                                "status": "not_run",
                                "passed": False,
                                "failure_type": (
                                    "first_call_invalid"
                                ),
                            }
                        entry["status"] = "failed"
                        smoke_passed = False
                        continue

                    if is_dga_context_gate:
                        if restore_result is None:
                            raise RuntimeError(
                                "DGA restore result was not retained"
                            )
                        returned_state = restore_result["post_state"]
                        rng_fields, population_fields = (
                            _dga_stream_field_counts(returned_state)
                        )
                        context_record: dict[str, Any] = {
                            "status": "not_run",
                            "passed": False,
                            "forced_pclear": True,
                            "streamed_rng_field_count": rng_fields,
                            "streamed_population_field_count": (
                                population_fields
                            ),
                        }
                        entry["context_restore"] = context_record
                        entry["delta"] = {
                            "status": "replaced_by_context_restore"
                        }
                        if rng_fields < 1 or population_fields < 1:
                            context_record["status"] = "failed"
                            context_record["failure_type"] = (
                                "missing_streamed_dga_state"
                            )
                            entry["status"] = "failed"
                            smoke_passed = False
                            continue
                        context_state = _dga_context_restore_state(
                            _persistent_restore_state(
                                mission,
                                algorithm,
                            ),
                            returned_state,
                            goal,
                        )
                        context_setup = _persistent_setup(
                            config,
                            context_id=context_id,
                            mode="restore",
                            state=context_state,
                            phase="context_restore",
                        )
                        context_setup["resume_state"] = dict(
                            restore_result.get("resume_state", {})
                        )
                        context_id_value = (
                            f"preflight-persistent:{identity.device_id}:"
                            "bayesian:DGA:context_restore"
                        )
                        context_attempt, _, context_ok, _ = (
                            _persistent_attempt(
                                device,
                                context_setup,
                                context_id_value,
                                mission=mission,
                                timeout_seconds=timeout_seconds,
                                require_filter=False,
                                expected_goal=(
                                    PERSISTENT_PARITY_GOALS[mission]
                                ),
                            )
                        )
                        for name, value in context_attempt.items():
                            context_record[name] = value
                        context_record["attempt_status"] = (
                            context_attempt.get("status", "")
                        )
                        context_passed = bool(
                            context_ok
                            and context_attempt.get("status")
                            == "completed"
                        )
                        context_record["passed"] = context_passed
                        if context_passed:
                            context_record["status"] = "completed"
                            entry["status"] = "completed"
                        else:
                            context_record["status"] = "failed"
                            entry["status"] = "failed"
                            smoke_passed = False
                        continue

                    delta_setup = _persistent_setup(
                        config,
                        context_id=context_id,
                        mode="delta",
                        state=_persistent_delta_state(mission, goal),
                        phase="delta",
                    )
                    delta_id = (
                        f"preflight-persistent:{identity.device_id}:"
                        f"{mission}:{algorithm}:delta"
                    )
                    delta, _, delta_ok, _ = _persistent_attempt(
                        device,
                        delta_setup,
                        delta_id,
                        mission=mission,
                        timeout_seconds=timeout_seconds,
                        require_filter=False,
                    )
                    entry["delta"] = delta
                    if delta.get("status") == "resource_limited":
                        entry["status"] = "resource_limited"
                        entry["deferred_to_campaign"] = True
                        if not delta.get("worker_recovery", {}).get(
                            "passed", False
                        ):
                            smoke_passed = False
                            entry["status"] = "failed"
                    elif delta_ok:
                        entry["status"] = "completed"
                    else:
                        entry["status"] = "failed"
                        smoke_passed = False
                except Exception as exc:
                    entry["status"] = "failed"
                    entry["failure_type"] = type(exc).__name__
                    entry["error"] = str(exc)
                    smoke_passed = False
                finally:
                    if trial_open:
                        cleanup = _end_persistent_trial_safely(device)
                        entry["cleanup"] = cleanup
                        if not cleanup["passed"]:
                            entry["status"] = "failed"
                            entry["failure_type"] = (
                                "persistent_cleanup_failure"
                            )
                            entry["error"] = cleanup["error"]
                            smoke_passed = False
                    smoke.append(entry)

    # CBAA at K=1 has one unambiguous candidate in each synthetic mission.
    # These two checks are the compact parity proof for the exact persistent
    # code path; resource-heavy algorithms may still be classified by the
    # campaign if they cannot finish a preflight call.
    parity = []
    for entry in smoke:
        if entry["algorithm"] != "CBAA":
            continue
        restore = entry["restore"]
        parity.append(
            {
                "device_id": entry["device_id"],
                "mission": entry["mission"],
                "algorithm": "CBAA",
                "expected_goal": list(
                    PERSISTENT_PARITY_GOALS[entry["mission"]]
                ),
                "actual_goal": restore.get("goal"),
                "passed": bool(restore.get("parity_passed", False)),
            }
        )
    parity_passed = (
        len(parity) == 2 * len(device_list)
        and all(item["passed"] for item in parity)
    )
    context_restore = [
        {
            "device_id": entry["device_id"],
            "mission": "bayesian",
            "algorithm": "DGA",
            **entry["context_restore"],
        }
        for entry in smoke
        if entry["mission"] == "bayesian"
        and entry["algorithm"] == "DGA"
    ]
    context_restore_passed = (
        len(context_restore) == len(device_list)
        and all(item.get("passed", False) for item in context_restore)
    )

    calibration: list[dict[str, Any]] = []
    medians: dict[str, float] = {}
    calibration_passed = True
    for device, identity in zip(device_list, identity_list):
        samples: list[int] = []
        attempts: list[dict[str, Any]] = []
        config = _persistent_trial_config(
            "bayesian",
            "CBAA",
            f"preflight/{identity.device_id}/persistent-calibration",
        )
        isolation = _isolate_persistent_worker(
            device,
            identity,
            phase="calibration",
        )
        worker_isolation.append(isolation)
        trial_open = False
        if not isolation["passed"]:
            calibration_passed = False
            attempts.append(
                {
                    "status": "failed",
                    "setup_succeeded": False,
                    "ready_to_time": False,
                    "failure_type": isolation.get(
                        "failure_type",
                        "worker_isolation_failure",
                    ),
                    "error": isolation.get("error", ""),
                    "phase": "worker_isolation",
                }
            )
        else:
            try:
                device.begin_persistent_trial(config)
                trial_open = True
                for repetition in range(calibration_repetitions):
                    setup = _persistent_setup(
                        config,
                        context_id="00",
                        mode="restore",
                        state=_persistent_restore_state(
                            "bayesian",
                            "CBAA",
                        ),
                        phase=f"calibration_{repetition}",
                    )
                    attempt_id = (
                        f"preflight-persistent-cal:{identity.device_id}:"
                        f"{repetition}"
                    )
                    record, _, ok, _ = _persistent_attempt(
                        device,
                        setup,
                        attempt_id,
                        mission="bayesian",
                        timeout_seconds=timeout_seconds,
                        require_filter=True,
                        expected_goal=(
                            PERSISTENT_PARITY_GOALS["bayesian"]
                        ),
                    )
                    record["repetition"] = repetition
                    attempts.append(record)
                    if not ok:
                        calibration_passed = False
                        break
                    samples.append(int(record["allocator_time_us"]))
            except Exception as exc:
                calibration_passed = False
                attempts.append(
                    {
                        "status": "failed",
                        "setup_succeeded": False,
                        "ready_to_time": False,
                        "failure_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
            finally:
                if trial_open:
                    cleanup = _end_persistent_trial_safely(device)
                    if not cleanup["passed"]:
                        calibration_passed = False
                        attempts.append(
                            {
                                "status": "failed",
                                "failure_type": (
                                    "persistent_cleanup_failure"
                                ),
                                "error": cleanup["error"],
                                "phase": "end_trial",
                            }
                        )
                    elif cleanup["restarted"]:
                        attempts.append(
                            {
                                "status": "recovered",
                                "phase": "end_trial",
                                "worker_restarted": True,
                                "error": cleanup["error"],
                            }
                        )
        median = (
            float(statistics.median(samples))
            if len(samples) == calibration_repetitions
            else None
        )
        if median is not None:
            medians[identity.device_id] = median
        calibration.append(
            {
                "device_id": identity.device_id,
                "mission": "bayesian",
                "algorithm": "CBAA",
                "top_k_level": "K=1",
                "worker_isolation": isolation,
                "samples_us": samples,
                "median_us": median,
                "attempts": attempts,
            }
        )

    complete_medians = list(medians.values())
    if len(complete_medians) != len(device_list):
        calibration_passed = False
    reference = (
        float(statistics.median(complete_medians))
        if complete_medians
        else None
    )
    for item in calibration:
        value = item["median_us"]
        deviation = None
        if value is not None and reference is not None:
            deviation = abs(float(value) - reference) / max(
                reference,
                1.0,
            )
        item["reference_median_us"] = reference
        item["deviation_fraction"] = deviation
        item["within_tolerance"] = (
            deviation is not None
            and deviation <= CALIBRATION_MEDIAN_TOLERANCE
        )
        if not item["within_tolerance"]:
            calibration_passed = False

    worker_isolation_passed = all(
        item.get("passed", False) for item in worker_isolation
    )

    return {
        "passed": (
            motor_free_passed
            and smoke_passed
            and parity_passed
            and context_restore_passed
            and calibration_passed
            and worker_isolation_passed
        ),
        "motor_free": {
            "passed": motor_free_passed,
            "devices": motor_free_devices,
        },
        "smoke_passed": smoke_passed,
        "smoke": smoke,
        "parity_passed": parity_passed,
        "parity": parity,
        "context_restore_passed": context_restore_passed,
        "context_restore": context_restore,
        "calibration_passed": calibration_passed,
        "calibration": calibration,
        "worker_isolation_passed": worker_isolation_passed,
        "worker_isolation": worker_isolation,
        "tolerance_fraction": CALIBRATION_MEDIAN_TOLERANCE,
    }


def run_preflight(
    devices: Iterable[SerialReplayDevice],
    *,
    build_root: Path | None = None,
    calibration_repetitions: int = 5,
) -> dict[str, object]:
    device_list = list(devices)
    if not device_list:
        raise RuntimeError("preflight requires at least one connected device")
    root, manifest = load_build(build_root)
    safety = verify_source_safety(root)
    if safety["main_module_present"]:
        safety["passed"] = False
    identities = [device.hello() for device in device_list]
    checks = {
        identity.device_id: device.check()
        for device, identity in zip(device_list, identities)
    }
    expected_build = str(manifest["build_id"])
    build_ids = {identity.build_id for identity in identities}
    frequencies = {identity.frequency_hz for identity in identities}
    implementations = {identity.implementation for identity in identities}
    firmware_hashes = {identity.firmware_sha256 for identity in identities}
    static_passed = (
        safety["passed"]
        and build_ids == {expected_build}
        and len(frequencies) == 1
        and len(implementations) == 1
        and len(firmware_hashes) == 1
        and all(value["double_array"] for value in checks.values())
        and not any(value["motors_initialized"] for value in checks.values())
        and not any(value["sensors_initialized"] for value in checks.values())
        and all(
            value["actual_module_set_sha256"]
            == value["expected_module_set_sha256"]
            == manifest["deployed_module_set_sha256"]
            for value in checks.values()
        )
    )
    persistent = run_persistent_checks(
        device_list,
        identities,
        checks,
        calibration_repetitions=calibration_repetitions,
    )

    # The old replay preflight loaded complete captured simulator snapshots and
    # timed the legacy offline-replay worker.  That is not the HIL execution
    # path: its packaging can exhaust heap before choose_goal, and a DGA result
    # can time out while serializing after the allocator already finished.
    # Running those fixtures here both misclassified healthy native ports and
    # left the worker in a stale state before calibration.  The persistent
    # restore/delta smoke above now supplies parity and calibration evidence.
    legacy_replay = {
        "executed": False,
        "gating": False,
        "reason": (
            "legacy full-snapshot replay packages a different execution path; "
            "use the offline replay campaign for diagnostics"
        ),
    }

    repl_exit_checks: list[dict[str, object]] = []
    repl_exit_passed = True
    for device, identity in zip(device_list, identities):
        check: dict[str, object] = {
            "device_id": identity.device_id,
            "passed": False,
            "clean_worker_started": False,
            "exit_acknowledged": False,
            "worker_restarted": False,
        }
        try:
            # Establish a known worker first.  This deliberately recovers from
            # a previous timed-call interrupt before testing the EXIT/BYE path.
            clean = device.start_worker()
            check["clean_worker_started"] = (
                clean.device_id == identity.device_id
            )
            if not check["clean_worker_started"]:
                raise ReplayTransportError(
                    "worker recovery returned a different device ID"
                )
            device.exit()
            check["exit_acknowledged"] = True
            restarted = device.start_worker()
            check["worker_restarted"] = (
                restarted.device_id == identity.device_id
            )
            check["passed"] = bool(
                check["clean_worker_started"]
                and check["exit_acknowledged"]
                and check["worker_restarted"]
            )
        except Exception as exc:
            check["error"] = f"{type(exc).__name__}: {exc}"
        repl_exit_passed = repl_exit_passed and bool(check["passed"])
        repl_exit_checks.append(check)

    passed = (
        static_passed
        and persistent["passed"]
        and repl_exit_passed
    )
    report: dict[str, object] = {
        "schema": 3,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "build_id": expected_build,
        "build_manifest": str((root / "manifest.json").resolve()),
        "safety": safety,
        "device_checks": checks,
        "devices": [
            {
                "port": identity.port,
                "device_id": identity.device_id,
                "build_id": identity.build_id,
                "implementation": identity.implementation,
                "frequency_hz": identity.frequency_hz,
                "heap_free": identity.heap_free,
                "firmware_sha256": identity.firmware_sha256,
            }
            for identity in identities
        ],
        "matching": {
            "build": build_ids == {expected_build},
            "frequency": len(frequencies) == 1,
            "implementation": len(implementations) == 1,
            "firmware": len(firmware_hashes) == 1,
            "module_hashes": all(
                value["actual_module_set_sha256"]
                == value["expected_module_set_sha256"]
                == manifest["deployed_module_set_sha256"]
                for value in checks.values()
            ),
        },
        "smoke": persistent["smoke"],
        "context_restore_passed": persistent[
            "context_restore_passed"
        ],
        "context_restore": persistent["context_restore"],
        "calibration": persistent["calibration"],
        "persistent": persistent,
        "legacy_replay": legacy_replay,
        "safe_repl_exit": repl_exit_checks,
        "tolerance_fraction": CALIBRATION_MEDIAN_TOLERANCE,
    }
    _atomic_json(PREFLIGHT_PATH, report)
    if not passed:
        raise RuntimeError(f"device preflight failed; inspect {PREFLIGHT_PATH}")
    return report
