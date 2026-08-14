"""Semantic validation for causal trial artifacts before promotion."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import statistics
from pathlib import Path
from typing import Any, Iterable, Mapping

from study.manifests import sha256_file
from known_visit_sim.core.timing import parity_sha256

from .model import CausalConfig, CausalJob


REQUIRED_OUTPUTS = (
    "trial_summary.json",
    "task_events.csv",
    "allocator_calls.csv",
    "reallocation_events.csv",
    "pending_queue_samples.csv",
    "movement_events.csv",
    "run_provenance.json",
)

ALLOCATOR_PROCESSOR_WORK_DEFINITION = (
    "allocator input integration, consensus message handling, allocator-local "
    "recovery, and choose_goal; excludes transport, message decoding, generic "
    "PSETUP synchronization, outbound extraction, serialization, and explicit "
    "pre-call GC; GC triggered naturally inside the allocator transaction remains "
    "included"
)

LEGACY_DEVICE_ALLOCATOR_TIMER_SCOPE = (
    "choose_goal plus policy-induced on_allocation_epoch allocator callback; "
    "excludes generic PSETUP state synchronization, USB, explicit pre-call GC, "
    "and post-call result serialization; GC triggered naturally inside either "
    "measured allocator operation remains included"
)


class CausalOutputError(ValueError):
    pass


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CausalOutputError(f"cannot read {path.name}: {error}") from error
    if not isinstance(value, dict) or not value:
        raise CausalOutputError(f"{path.name} must be a nonempty JSON object")
    return value


def _csv(path: Path, required: Iterable[str], *, allow_empty: bool = False) -> list[dict[str, str]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            missing = set(required) - fields
            if missing:
                raise CausalOutputError(f"{path.name} missing columns: {', '.join(sorted(missing))}")
            rows = list(reader)
    except OSError as error:
        raise CausalOutputError(f"cannot read {path.name}: {error}") from error
    if not rows and not allow_empty:
        raise CausalOutputError(f"{path.name} contains no data rows")
    return rows


def _number(value: Any, field: str, *, optional: bool = False, nonnegative: bool = True) -> float | None:
    if value in {None, ""} and optional:
        return None
    if isinstance(value, bool):
        raise CausalOutputError(f"{field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise CausalOutputError(f"{field} must be numeric") from error
    if not math.isfinite(result) or (nonnegative and result < -1e-12):
        raise CausalOutputError(f"{field} must be finite{' and nonnegative' if nonnegative else ''}")
    return result


def _integer(value: Any, field: str, *, nonnegative: bool = True) -> int:
    if isinstance(value, bool):
        raise CausalOutputError(f"{field} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        result = int(value.strip())
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        # CSV integers arrive as strings and JSON integers arrive as ``int``;
        # this branch exists only for structurally compatible helper inputs.
        # Never route a large integer through float, which would silently lose
        # paired 64-bit runtime-seed precision.
        result = int(value)
    else:
        raise CausalOutputError(f"{field} must be an integer")
    if nonnegative and result < 0:
        raise CausalOutputError(f"{field} must be nonnegative")
    return result


def _bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    raise CausalOutputError(f"{field} must be boolean")


def _close(actual: float, expected: float, field: str, tolerance: float = 1e-8) -> None:
    if not math.isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance):
        raise CausalOutputError(f"{field} mismatch: {actual} != {expected}")


def _summary_value(summary: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in summary:
            return summary[name]
    raise CausalOutputError(f"trial_summary.json lacks any of: {', '.join(names)}")


def _json_mapping(value: Any, field: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as error:
        raise CausalOutputError(f"{field} must be a JSON object") from error
    if not isinstance(decoded, dict):
        raise CausalOutputError(f"{field} must be a JSON object")
    return {str(key): item for key, item in decoded.items()}


def _manifest(job: CausalJob) -> tuple[dict[str, Any], dict[str, Any]]:
    if sha256_file(job.scenario_path) != job.scenario_sha256:
        raise CausalOutputError("scenario manifest bytes changed")
    if sha256_file(job.release_path) != job.release_sha256:
        raise CausalOutputError("release manifest bytes changed")
    return _json(job.scenario_path), _json(job.release_path)


def validate_causal_outputs(config: CausalConfig, job: CausalJob, directory: Path) -> dict[str, Any]:
    missing = [name for name in REQUIRED_OUTPUTS if not (directory / name).is_file()]
    if missing:
        raise CausalOutputError(f"missing required outputs: {', '.join(missing)}")
    summary = _json(directory / "trial_summary.json")
    provenance = _json(directory / "run_provenance.json")
    output_schema_version = _integer(
        summary.get("schema_version", 1), "schema_version"
    )
    strict_admission_architecture = output_schema_version >= 3
    scenario, release = _manifest(job)
    binding = config.boards[job.worker_index]
    if binding.board_id != job.board_id or config.core_affinities[job.worker_index] != job.core_id:
        raise CausalOutputError("job worker/board/core binding is internally inconsistent")
    exact_dimensions = {
        "job_id": job.job_id,
        "block_id": job.block_id,
        "algorithm": job.algorithm,
        "arrival_load": job.load_id,
        "trace_id": job.trace_id,
        "policy_id": job.policy.policy_id,
        "policy_mode": job.policy.mode,
        "policy_batch_size": job.policy.batch_size,
        "policy_max_pending_age_s": job.policy.max_pending_age_s,
        "runtime_seed": job.runtime_seed,
        "board_id": job.board_id,
        "worker_index": job.worker_index,
        "core_id": job.core_id,
        "scenario_sha256": job.scenario_sha256,
        "release_sha256": job.release_sha256,
        "config_sha256": config.config_sha256,
        "manifest_index_sha256": config.manifest_index_sha256,
        "hardware_binding_sha256": config.hardware_binding_sha256,
        "expected_device_uid": binding.expected_device_uid,
        "expected_device_build_id": binding.expected_build_id,
        "expected_device_firmware_sha256": binding.expected_firmware_sha256,
        "expected_device_module_set_sha256": binding.expected_module_set_sha256,
        "communication_model": "ideal_fixed_delay_no_jitter",
        "communication_delay_s": 0.04,
        "communication_delay_jitter_s": 0.0,
        "python_hash_seed": (
            summary.get("python_hash_seed") if config.development_override else "0"
        ),
        "zero_compute": job.zero_compute,
    }
    for name, expected in exact_dimensions.items():
        if summary.get(name) != expected:
            raise CausalOutputError(f"summary {name} mismatch: {summary.get(name)!r} != {expected!r}")
    for name, expected in exact_dimensions.items():
        if provenance.get(name) != expected:
            raise CausalOutputError(f"provenance {name} mismatch")
    for name in (
        "config_sha256", "manifest_index_sha256", "hardware_binding_sha256",
        "board_id", "worker_index", "core_id", "expected_device_uid",
        "expected_device_build_id", "expected_device_firmware_sha256",
        "expected_device_module_set_sha256", "git_head", "source_tree_sha256",
    ):
        if name not in provenance:
            raise CausalOutputError(f"provenance lacks {name}")
    for name, pattern in (
        ("git_head", r"[0-9a-f]{40}"),
        ("source_tree_sha256", r"[0-9a-f]{64}"),
    ):
        value = str(provenance.get(name, ""))
        if re.fullmatch(pattern, value) is None or summary.get(name) != value:
            raise CausalOutputError(f"invalid or inconsistent provenance {name}")
    if strict_admission_architecture:
        for name in (
            "allocator_processor_work_definition",
            "allocator_processor_timing_source",
            "rp2040_performance_reported",
        ):
            if provenance.get(name) != summary.get(name):
                raise CausalOutputError(
                    f"allocator timing provenance mismatch: {name}"
                )
    if not config.development_override and provenance.get("relevant_source_dirty") is not False:
        raise CausalOutputError("promoted causal output was produced from a dirty source tree")
    if provenance.get("hidden_library_threads") != 1:
        raise CausalOutputError("provenance does not prove single-threaded numerical libraries")
    hardware_validated = _bool(summary.get("hardware_validated"), "hardware_validated")
    if strict_admission_architecture:
        if summary.get("allocator_processor_work_definition") != (
            ALLOCATOR_PROCESSOR_WORK_DEFINITION
        ):
            raise CausalOutputError(
                "summary allocator processor-work definition is missing or inaccurate"
            )
        expected_timing_source = (
            "rp2040_hardware"
            if hardware_validated else (
                "zero_compute_counterfactual"
                if job.zero_compute else
                "simulated_duration_development_proxy"
            )
        )
        if summary.get("allocator_processor_timing_source") != expected_timing_source:
            raise CausalOutputError("summary allocator timing source is mislabeled")
        if summary.get("timing_source") != expected_timing_source:
            raise CausalOutputError("summary timing_source is mislabeled")
        if _bool(
            summary.get("rp2040_performance_reported"),
            "rp2040_performance_reported",
        ) != hardware_validated:
            raise CausalOutputError(
                "RP2040 performance-reporting flag disagrees with hardware validation"
            )
    if not job.zero_compute and not config.development_override and not hardware_validated:
        raise CausalOutputError("hardware-timed job is not hardware validated")
    if not job.zero_compute and not _bool(summary.get("parity_passed"), "parity_passed"):
        raise CausalOutputError("hardware-timed job did not pass parity")

    csv_dimensions = {
        "job_id": job.job_id,
        "block_id": job.block_id,
        "algorithm": job.algorithm,
        "arrival_load": job.load_id,
        "trace_id": job.trace_id,
        "policy_id": job.policy.policy_id,
        "board_id": job.board_id,
        "scenario_sha256": job.scenario_sha256,
        "release_sha256": job.release_sha256,
        "config_sha256": config.config_sha256,
        "manifest_index_sha256": config.manifest_index_sha256,
    }

    def validate_csv_dimensions(rows: Iterable[Mapping[str, str]], table: str) -> None:
        for row in rows:
            for field, expected in csv_dimensions.items():
                if row.get(field) != str(expected):
                    raise CausalOutputError(f"{table} {field} dimension mismatch")

    required_task_fields = {
            "task_id", "x", "y", "release_time_s", "pending_time_s", "admission_time_s",
            "first_eligible_processing_time_s", "first_assignment_time_s", "first_assigned_robot",
            "completion_time_s", "completing_robot", "reassignment_count",
            "release_to_admission_latency_s",
            "release_to_first_assignment_latency_s", "release_to_completion_latency_s",
            "admission_to_first_assignment_latency_s",
            "admission_to_first_eligible_allocator_start_latency_s",
            "assignment_to_completion_latency_s", "completion_mode",
    }
    if strict_admission_architecture:
        required_task_fields.update({
            "admission_epoch_id", "admission_trigger", "terminal_residual",
            "knowledge_receipt_time_s_by_robot",
            "first_knowledge_receipt_time_s",
            "first_eligible_allocator_start_time_s", "first_eligible_robot",
            "first_current_goal_time_s", "first_current_goal_robot",
            "current_goal_events", "assignment_events",
            "admission_to_first_current_goal_latency_s",
        })
    task_rows = _csv(
        directory / "task_events.csv",
        required_task_fields,
    )
    validate_csv_dimensions(task_rows, "task_events.csv")
    manifest_tasks = {str(task["task_id"]): task for task in release["tasks"]}
    if len(task_rows) != len(manifest_tasks) or {row["task_id"] for row in task_rows} != set(manifest_tasks):
        raise CausalOutputError("task table does not contain the exact manifest task set")
    completed_times: list[float] = []
    assignment_latencies: list[float] = []
    completion_latencies: list[float] = []
    task_receipts: dict[str, dict[str, float]] = {}
    task_admission_epochs: dict[str, int] = {}
    for row in task_rows:
        expected = manifest_tasks[row["task_id"]]
        if _integer(row["x"], f"{row['task_id']}.x") != int(expected["x"]):
            raise CausalOutputError("task x coordinate mismatch")
        if _integer(row["y"], f"{row['task_id']}.y") != int(expected["y"]):
            raise CausalOutputError("task y coordinate mismatch")
        release_s = _number(
            row["release_time_s"], "release_time_s", optional=True
        )
        if release_s is not None:
            _close(release_s, float(expected["release_time_s"]), "task release time")
        elif str(row.get("state", "")) != "unreleased":
            raise CausalOutputError("task lacking a release time is not unreleased")
        pending_s = _number(
            row["pending_time_s"], "pending_time_s", optional=True
        )
        admission_s = _number(row["admission_time_s"], "admission_time_s", optional=True)
        eligible_s = _number(row["first_eligible_processing_time_s"], "first_eligible_processing_time_s", optional=True)
        assignment_s = _number(row["first_assignment_time_s"], "first_assignment_time_s", optional=True)
        completion_s = _number(row["completion_time_s"], "completion_time_s", optional=True)
        if release_s is None:
            if pending_s is not None:
                raise CausalOutputError("unreleased task has a pending timestamp")
        else:
            if pending_s is None:
                raise CausalOutputError("released task lacks a pending timestamp")
            _close(pending_s, release_s, "pending timestamp")
        observed = [value for value in (release_s, admission_s, eligible_s, assignment_s, completion_s) if value is not None]
        if observed != sorted(observed):
            raise CausalOutputError(f"task lifecycle timestamps are out of order: {row['task_id']}")
        if assignment_s is not None:
            if release_s is None or not row["first_assigned_robot"]:
                raise CausalOutputError("assigned task lacks first_assigned_robot")
            actual = _number(row["release_to_first_assignment_latency_s"], "assignment latency")
            assert actual is not None
            _close(actual, assignment_s - release_s, "assignment latency")
            assignment_latencies.append(actual)
        elif row["first_assigned_robot"] or row["release_to_first_assignment_latency_s"]:
            raise CausalOutputError("unassigned task has assignment fields")
        if completion_s is not None:
            if release_s is None or assignment_s is None or not row["completing_robot"]:
                raise CausalOutputError("completed task lacks assignment/completing robot")
            actual = _number(row["release_to_completion_latency_s"], "completion latency")
            assert actual is not None
            _close(actual, completion_s - release_s, "completion latency")
            completion_latencies.append(actual)
            completed_times.append(completion_s)
        elif row["completing_robot"] or row["release_to_completion_latency_s"]:
            raise CausalOutputError("incomplete task has completion fields")
        _integer(row["reassignment_count"], "reassignment_count")
        if admission_s is not None:
            if release_s is None:
                raise CausalOutputError("task was admitted before it was released")
            release_admission = _number(
                row["release_to_admission_latency_s"],
                "release_to_admission_latency_s",
            )
            assert release_admission is not None
            _close(release_admission, admission_s - release_s, "release-to-admission latency")
        elif row["release_to_admission_latency_s"]:
            raise CausalOutputError("unadmitted task has release-to-admission latency")
        if admission_s is not None and assignment_s is not None:
            admission_assignment = _number(
                row["admission_to_first_assignment_latency_s"],
                "admission_to_first_assignment_latency_s",
            )
            assert admission_assignment is not None
            _close(
                admission_assignment,
                assignment_s - admission_s,
                "admission-to-assignment latency",
            )
        elif row["admission_to_first_assignment_latency_s"]:
            raise CausalOutputError("task has invalid admission-to-assignment latency")
        if admission_s is not None and eligible_s is not None:
            admission_eligible = _number(
                row["admission_to_first_eligible_allocator_start_latency_s"],
                "admission_to_first_eligible_allocator_start_latency_s",
            )
            assert admission_eligible is not None
            _close(
                admission_eligible,
                eligible_s - admission_s,
                "admission-to-eligible latency",
            )
        elif row["admission_to_first_eligible_allocator_start_latency_s"]:
            raise CausalOutputError("task has invalid admission-to-eligible latency")
        if assignment_s is not None and completion_s is not None:
            assignment_completion = _number(
                row["assignment_to_completion_latency_s"],
                "assignment_to_completion_latency_s",
            )
            assert assignment_completion is not None
            _close(
                assignment_completion,
                completion_s - assignment_s,
                "assignment-to-completion latency",
            )
        elif row["assignment_to_completion_latency_s"]:
            raise CausalOutputError("task has invalid assignment-to-completion latency")

        if strict_admission_architecture:
            raw_epoch_id = row["admission_epoch_id"]
            epoch_id = (
                None
                if raw_epoch_id in {None, ""}
                else _integer(raw_epoch_id, "admission_epoch_id")
            )
            admission_trigger = str(row["admission_trigger"])
            terminal_residual = _bool(
                row["terminal_residual"], "terminal_residual"
            )
            receipt_raw = _json_mapping(
                row["knowledge_receipt_time_s_by_robot"],
                "knowledge_receipt_time_s_by_robot",
            )
            receipts: dict[str, float] = {}
            for robot_id, raw_time in receipt_raw.items():
                if robot_id not in {
                    str(item["robot_id"]) for item in scenario["robot_starts"]
                }:
                    raise CausalOutputError("task receipt names an unknown robot")
                receipt_s = _number(raw_time, "task knowledge receipt time")
                assert receipt_s is not None
                if admission_s is None or receipt_s + 1e-9 < admission_s:
                    raise CausalOutputError("task knowledge preceded task admission")
                receipts[robot_id] = receipt_s
            first_receipt_s = _number(
                row["first_knowledge_receipt_time_s"],
                "first_knowledge_receipt_time_s",
                optional=True,
            )
            if receipts:
                assert first_receipt_s is not None
                _close(
                    first_receipt_s,
                    min(receipts.values()),
                    "first task-knowledge receipt",
                )
            elif first_receipt_s is not None:
                raise CausalOutputError(
                    "task has a first receipt without per-robot receipts"
                )
            if admission_s is None:
                if epoch_id is not None or admission_trigger or terminal_residual or receipts:
                    raise CausalOutputError(
                        "unadmitted task has admission-message metadata"
                    )
            else:
                if epoch_id is None or not admission_trigger:
                    raise CausalOutputError(
                        "admitted task lacks admission epoch/trigger metadata"
                    )
                if terminal_residual != (admission_trigger == "terminal_residual"):
                    raise CausalOutputError(
                        "task terminal-residual flag disagrees with admission trigger"
                    )
                task_admission_epochs[row["task_id"]] = epoch_id
            task_receipts[row["task_id"]] = receipts

            eligible_alias_s = _number(
                row["first_eligible_allocator_start_time_s"],
                "first_eligible_allocator_start_time_s",
                optional=True,
            )
            if eligible_alias_s != eligible_s:
                if eligible_alias_s is None or eligible_s is None:
                    raise CausalOutputError(
                        "eligible allocator-start aliases disagree"
                    )
                _close(
                    eligible_alias_s,
                    eligible_s,
                    "eligible allocator-start aliases",
                )
            eligible_robot = str(row["first_eligible_robot"])
            if eligible_s is None:
                if eligible_robot:
                    raise CausalOutputError(
                        "task has an eligible robot without an eligible start"
                    )
            elif eligible_robot not in receipts:
                raise CausalOutputError(
                    "eligible allocator robot had not received task knowledge"
                )
            elif receipts[eligible_robot] > eligible_s + 1e-9:
                raise CausalOutputError(
                    "allocator considered task before its admission message arrived"
                )

            goal_s = _number(
                row["first_current_goal_time_s"],
                "first_current_goal_time_s",
                optional=True,
            )
            goal_robot = str(row["first_current_goal_robot"])
            goal_events = _integer(row["current_goal_events"], "current_goal_events")
            goal_latency_s = _number(
                row["admission_to_first_current_goal_latency_s"],
                "admission_to_first_current_goal_latency_s",
                optional=True,
            )
            if goal_s is None:
                if goal_robot or goal_events or goal_latency_s is not None:
                    raise CausalOutputError(
                        "task current-goal metadata is internally inconsistent"
                    )
            else:
                if admission_s is None or goal_robot not in receipts or goal_events < 1:
                    raise CausalOutputError(
                        "task current goal lacks causal admission receipt"
                    )
                if receipts[goal_robot] > goal_s + 1e-9:
                    raise CausalOutputError(
                        "task became a current goal before message receipt"
                    )
                assert goal_latency_s is not None
                _close(
                    goal_latency_s,
                    goal_s - admission_s,
                    "admission-to-current-goal latency",
                )
            if completion_s is not None:
                completing_robot = str(row["completing_robot"])
                if completing_robot not in receipts:
                    raise CausalOutputError(
                        "completing robot lacked message-delivered task knowledge"
                    )
                if receipts[completing_robot] > completion_s + 1e-9:
                    raise CausalOutputError(
                        "task completed before the completing robot learned it"
                    )

    required_call_fields = {
            "call_id", "call_group_id", "event_id", "logical_robot_id", "context_id",
            "algorithm", "virtual_compute_start_s", "rp2040_device_duration_s",
            "virtual_compute_completion_s", "agx_allocator_duration_s", "serial_roundtrip_s",
            "host_serialization_setup_s", "host_total_call_s",
            "timing_decomposition_schema", "psetup_transaction_s",
            "device_pre_call_setup_s", "ptime_result_transaction_s",
            "host_prepare_cpu_s", "host_serialization_setup_measured",
            "device_allocator_timer_scope", "serial_roundtrip_definition",
            "device_choose_goal_us", "algorithm_epoch_reset_us",
            "rp2040_choose_goal_duration_s",
            "rp2040_algorithm_epoch_reset_duration_s",
            "agx_choose_goal_duration_s",
            "agx_algorithm_epoch_reset_duration_s",
            "active_task_count", "candidate_count",
            "parity_passed", "agx_selected_goal", "rp2040_selected_goal", "agx_message_hash",
            "rp2040_message_hash", "agx_state_hash", "rp2040_state_hash", "board_id",
            "device_uid", "serial_device", "attempt_id", "physical_measurement_index",
            "hardware_validated", "hardware_attestation",
            "hardware_attestation_sha256", "hardware_validation_mode",
            "current_x", "current_y",
            "parity_level", "representation_hashes_differ",
            "agx_message_projection_hash", "rp2040_message_projection_hash",
            "agx_state_projection_hash", "rp2040_state_projection_hash",
    }
    if strict_admission_architecture:
        required_call_fields.update({
            "allocator_processor_duration_s",
            "allocator_processor_primary_duration_s",
            "allocator_processor_admission_callback_duration_s",
            "allocator_processor_work_definition",
            "allocator_processor_timing_source",
            "allocator_input_event_count", "recovery_invoked",
            "call_class", "trigger_reason",
        })
    else:
        required_call_fields.add("pending_task_count")
    call_rows = _csv(
        directory / "allocator_calls.csv",
        required_call_fields,
        allow_empty=job.zero_compute,
    )
    validate_csv_dimensions(call_rows, "allocator_calls.csv")
    call_ids: set[str] = set()
    allocator_work = 0.0
    allocator_primary_work = 0.0
    allocator_admission_callback_work = 0.0
    rp_work = 0.0
    agx_work = 0.0
    rp_choose_work = 0.0
    rp_reset_work = 0.0
    agx_choose_work = 0.0
    agx_reset_work = 0.0
    allocator_input_event_total = 0
    recovery_invocation_count = 0
    rp_call_times: list[float] = []
    allocator_call_times: list[float] = []
    agx_call_times: list[float] = []
    group_starts: dict[str, set[float]] = {}
    robot_ids = {str(row["robot_id"]) for row in scenario["robot_starts"]}
    for row in call_rows:
        if row["call_id"] in call_ids:
            raise CausalOutputError(f"duplicate call ID: {row['call_id']}")
        call_ids.add(row["call_id"])
        if row["logical_robot_id"] not in robot_ids or row["context_id"] != row["logical_robot_id"]:
            raise CausalOutputError("call logical robot/context identity mismatch")
        current_x = _integer(row["current_x"], "current_x")
        current_y = _integer(row["current_y"], "current_y")
        grid_size = int(scenario["grid_size"])
        if not (0 <= current_x < grid_size and 0 <= current_y < grid_size):
            raise CausalOutputError("call current position is outside the grid")
        if row["algorithm"] != job.algorithm or row["board_id"] != job.board_id:
            raise CausalOutputError("call algorithm/board binding mismatch")
        call_hardware = _bool(row["hardware_validated"], "call hardware_validated")
        device_uid = row["device_uid"]
        if job.zero_compute:
            if device_uid != "ZERO_COMPUTE":
                raise CausalOutputError("zero-compute call has a device identity")
        elif config.development_override:
            if device_uid != "SIMULATED_DURATION":
                raise CausalOutputError("development duration lacks simulated-device sentinel")
        elif device_uid != binding.expected_device_uid:
            raise CausalOutputError("call device UID differs from the bound RP2040")
        start = _number(row["virtual_compute_start_s"], "virtual_compute_start_s")
        if strict_admission_architecture:
            device = _number(
                row["allocator_processor_duration_s"],
                "allocator_processor_duration_s",
            )
            device_primary = _number(
                row["allocator_processor_primary_duration_s"],
                "allocator_processor_primary_duration_s",
            )
            device_admission_callback = _number(
                row["allocator_processor_admission_callback_duration_s"],
                "allocator_processor_admission_callback_duration_s",
            )
            if row["allocator_processor_work_definition"] != (
                ALLOCATOR_PROCESSOR_WORK_DEFINITION
            ):
                raise CausalOutputError(
                    "allocator processor-work definition is missing or inaccurate"
                )
            expected_source = (
                "rp2040_hardware"
                if call_hardware else (
                    "zero_compute_counterfactual"
                    if job.zero_compute else
                    "simulated_duration_development_proxy"
                )
            )
            if row["allocator_processor_timing_source"] != expected_source:
                raise CausalOutputError(
                    "allocator processor timing source is mislabeled"
                )
            rp_device = _number(
                row["rp2040_device_duration_s"],
                "rp2040_device_duration_s",
                optional=True,
            )
            rp_choose = _number(
                row["rp2040_choose_goal_duration_s"],
                "rp2040_choose_goal_duration_s",
                optional=True,
            )
            rp_reset = _number(
                row["rp2040_algorithm_epoch_reset_duration_s"],
                "rp2040_algorithm_epoch_reset_duration_s",
                optional=True,
            )
            if call_hardware:
                if None in {rp_device, rp_choose, rp_reset}:
                    raise CausalOutputError(
                        "hardware-validated call lacks RP2040 timing fields"
                    )
                assert rp_device is not None and rp_choose is not None and rp_reset is not None
                _close(rp_device, device, "RP2040/allocator duration")
                _close(rp_choose, device_primary, "RP2040/allocator primary duration")
                _close(
                    rp_reset,
                    device_admission_callback,
                    "RP2040/allocator admission-callback duration",
                )
            elif any(value is not None for value in (rp_device, rp_choose, rp_reset)):
                raise CausalOutputError(
                    "non-hardware call is mislabeled with RP2040 performance"
                )
        else:
            device = _number(
                row["rp2040_device_duration_s"], "rp2040_device_duration_s"
            )
            device_primary = _number(
                row["rp2040_choose_goal_duration_s"],
                "rp2040_choose_goal_duration_s",
            )
            device_admission_callback = _number(
                row["rp2040_algorithm_epoch_reset_duration_s"],
                "rp2040_algorithm_epoch_reset_duration_s",
            )
            rp_device = device
            rp_choose = device_primary
            rp_reset = device_admission_callback
        completion = _number(row["virtual_compute_completion_s"], "virtual_compute_completion_s")
        agx = _number(row["agx_allocator_duration_s"], "agx_allocator_duration_s")
        roundtrip = _number(row["serial_roundtrip_s"], "serial_roundtrip_s")
        setup = _number(row["host_serialization_setup_s"], "host_serialization_setup_s")
        host_total = _number(row["host_total_call_s"], "host_total_call_s")
        psetup = _number(row["psetup_transaction_s"], "psetup_transaction_s")
        device_setup = _number(
            row["device_pre_call_setup_s"],
            "device_pre_call_setup_s",
            optional=True,
        )
        ptime = _number(
            row["ptime_result_transaction_s"], "ptime_result_transaction_s"
        )
        host_prepare = _number(row["host_prepare_cpu_s"], "host_prepare_cpu_s")
        timing_schema = _integer(
            row["timing_decomposition_schema"], "timing_decomposition_schema"
        )
        setup_measured = _bool(
            row["host_serialization_setup_measured"],
            "host_serialization_setup_measured",
        )
        device_choose_goal_us = _integer(
            row["device_choose_goal_us"], "device_choose_goal_us"
        )
        algorithm_epoch_reset_us = _integer(
            row["algorithm_epoch_reset_us"], "algorithm_epoch_reset_us"
        )
        agx_choose = _number(
            row["agx_choose_goal_duration_s"], "agx_choose_goal_duration_s"
        )
        agx_reset = _number(
            row["agx_algorithm_epoch_reset_duration_s"],
            "agx_algorithm_epoch_reset_duration_s",
        )
        assert None not in (
            start, device, completion, agx, roundtrip, setup, host_total,
            psetup, ptime, host_prepare,
            device_primary, device_admission_callback, agx_choose, agx_reset,
        )
        _close(completion, start + device, "virtual compute completion")
        _close(
            device_primary,
            device_choose_goal_us / 1_000_000.0,
            "allocator primary duration representation",
        )
        _close(
            device_admission_callback,
            algorithm_epoch_reset_us / 1_000_000.0,
            "allocator admission-callback duration representation",
        )
        _close(
            device,
            device_primary + device_admission_callback,
            "allocator processor component sum",
        )
        _close(agx, agx_choose + agx_reset, "AGX allocator component sum")
        if timing_schema == 2:
            _close(roundtrip, psetup + ptime, "schema-2 total serial transaction")
            _close(setup, 0.0, "unmeasured host serialization/setup")
            if setup_measured:
                raise CausalOutputError(
                    "schema-2 host serialization is marked measured despite protocol limitation"
                )
            expected_timer_scope = (
                ALLOCATOR_PROCESSOR_WORK_DEFINITION
                if strict_admission_architecture
                else LEGACY_DEVICE_ALLOCATOR_TIMER_SCOPE
            )
            if row["device_allocator_timer_scope"] != expected_timer_scope:
                raise CausalOutputError("device allocator timer scope is missing or inaccurate")
            if row["serial_roundtrip_definition"] != (
                "PSETUP transaction wall plus PTIME/result transaction wall; "
                "includes USB/protocol and device-side work and is excluded "
                "from causal compute duration"
            ):
                raise CausalOutputError("serial round-trip definition is missing or inaccurate")
        elif not (job.zero_compute or config.development_override):
            raise CausalOutputError("physical causal call lacks timing decomposition schema 2")
        if job.zero_compute and device != 0.0:
            raise CausalOutputError("zero-compute run contains nonzero causal duration")
        if not job.zero_compute and not _bool(row["parity_passed"], "call parity_passed"):
            raise CausalOutputError("invalid parity call entered trial metrics")
        if strict_admission_architecture:
            input_events = _integer(
                row["allocator_input_event_count"],
                "allocator_input_event_count",
            )
            recovery_invoked = _bool(
                row["recovery_invoked"], "recovery_invoked"
            )
            allocator_input_event_total += input_events
            recovery_invocation_count += int(recovery_invoked)
            if recovery_invoked != (row["trigger_reason"] == "stalled_recovery"):
                raise CausalOutputError(
                    "recovery_invoked disagrees with the allocator call trigger"
                )
            if row["trigger_reason"] == "allocator_message" and input_events < 1:
                raise CausalOutputError(
                    "allocator-message call reports no allocator input event"
                )
        if job.zero_compute or config.development_override:
            if call_hardware or row["hardware_attestation"] or row["hardware_attestation_sha256"]:
                raise CausalOutputError("nonphysical call contains native hardware attestation")
        else:
            if not call_hardware or row["hardware_validation_mode"] != "rp2040_native_hardware":
                raise CausalOutputError("physical call lacks native hardware validation")
            try:
                attestation = json.loads(row["hardware_attestation"])
            except (TypeError, json.JSONDecodeError) as error:
                raise CausalOutputError("hardware attestation is not canonical JSON") from error
            if not isinstance(attestation, dict):
                raise CausalOutputError("hardware attestation must be an object")
            if parity_sha256(attestation) != row["hardware_attestation_sha256"]:
                raise CausalOutputError("hardware attestation hash mismatch")
            expected_attestation = {
                "device_id": binding.expected_device_uid,
                "build_id": binding.expected_build_id,
                "firmware_sha256": binding.expected_firmware_sha256,
                "module_set_sha256": binding.expected_module_set_sha256,
                "call_id": row["call_id"],
                "group_id": row["call_group_id"],
                "logical_context_id": row["context_id"],
                "attempt_id": row["attempt_id"],
                "agx_message_sha256": row["agx_message_hash"],
                "device_message_sha256": row["rp2040_message_hash"],
                "agx_post_state_sha256": row["agx_state_hash"],
                "device_post_state_sha256": row["rp2040_state_hash"],
                "parity_level": row["parity_level"],
                "representation_hashes_differ": _bool(
                    row["representation_hashes_differ"],
                    "representation_hashes_differ",
                ),
                "timing_decomposition_schema": timing_schema,
                "device_allocator_time_us": round(device * 1_000_000.0),
                "device_choose_goal_us": device_choose_goal_us,
                "algorithm_epoch_reset_us": algorithm_epoch_reset_us,
                "psetup_transaction_us": round(psetup * 1_000_000.0),
                "device_pre_call_setup_us": (
                    None if device_setup is None
                    else round(device_setup * 1_000_000.0)
                ),
                "ptime_result_transaction_us": round(ptime * 1_000_000.0),
                "host_prepare_cpu_us": round(host_prepare * 1_000_000.0),
                "serial_roundtrip_us": round(roundtrip * 1_000_000.0),
                "host_serialization_setup_us": round(setup * 1_000_000.0),
                "host_total_call_us": round(host_total * 1_000_000.0),
                "host_serialization_setup_measured": setup_measured,
                "device_allocator_timer_scope": row["device_allocator_timer_scope"],
                "serial_roundtrip_definition": row["serial_roundtrip_definition"],
            }
            for name, expected in expected_attestation.items():
                if attestation.get(name) != expected:
                    raise CausalOutputError(f"hardware attestation {name} mismatch")
            if (
                int(attestation.get("physical_measurement_index", -1))
                != _integer(row["physical_measurement_index"], "physical_measurement_index")
            ):
                raise CausalOutputError("hardware attestation measurement index mismatch")
            if row["serial_device"] != binding.serial_device:
                raise CausalOutputError("call serial endpoint differs from board binding")
            if device_setup is None:
                raise CausalOutputError("physical call lacks on-device pre-call setup timing")
        if not job.zero_compute:
            if row["agx_selected_goal"] != row["rp2040_selected_goal"]:
                raise CausalOutputError("selected-goal parity mismatch")
            raw_message_equal = row["agx_message_hash"] == row["rp2040_message_hash"]
            raw_state_equal = row["agx_state_hash"] == row["rp2040_state_hash"]
            if not (raw_message_equal and raw_state_equal):
                if (
                    row["parity_level"] != "shared_logical_state_and_message_effect"
                    or not _bool(
                        row["representation_hashes_differ"],
                        "representation_hashes_differ",
                    )
                    or not row["agx_message_projection_hash"]
                    or row["agx_message_projection_hash"]
                    != row["rp2040_message_projection_hash"]
                    or not row["agx_state_projection_hash"]
                    or row["agx_state_projection_hash"]
                    != row["rp2040_state_projection_hash"]
                ):
                    raise CausalOutputError("raw parity differs without matching logical projections")
                if not (job.zero_compute or config.development_override):
                    for attested_name, row_name in (
                        ("parity_level", "parity_level"),
                        ("agx_message_projection_sha256", "agx_message_projection_hash"),
                        ("device_message_projection_sha256", "rp2040_message_projection_hash"),
                        ("agx_state_projection_sha256", "agx_state_projection_hash"),
                        ("device_state_projection_sha256", "rp2040_state_projection_hash"),
                    ):
                        if attestation.get(attested_name) != row[row_name]:
                            raise CausalOutputError(
                                f"hardware attestation does not bind {row_name}"
                            )
        allocator_work += device
        allocator_primary_work += device_primary
        allocator_admission_callback_work += device_admission_callback
        agx_work += agx
        allocator_call_times.append(device)
        if call_hardware or not strict_admission_architecture:
            rp_work += device
            rp_choose_work += device_primary
            rp_reset_work += device_admission_callback
            rp_call_times.append(device)
        agx_choose_work += agx_choose
        agx_reset_work += agx_reset
        agx_call_times.append(agx)
        group_starts.setdefault(row["call_group_id"], set()).add(start)
    if any(len(starts) != 1 for starts in group_starts.values()):
        raise CausalOutputError("same-time call group has serialized virtual start times")
    online_releases = sorted({
        float(row["release_time_s"])
        for row in task_rows
        if row["release_time_s"] not in {None, ""}
        and float(row["release_time_s"]) > 0.0
    })
    releases_during_compute = sum(
        any(
            float(call["virtual_compute_start_s"]) < release_time
            < float(call["virtual_compute_completion_s"])
            for call in call_rows
        )
        for release_time in online_releases
    )
    if _integer(summary.get("online_release_event_count"), "online_release_event_count") != len(online_releases):
        raise CausalOutputError("online release event count mismatch")
    if _integer(
        summary.get("release_events_during_compute_count"),
        "release_events_during_compute_count",
    ) != releases_during_compute:
        raise CausalOutputError("release-during-compute count mismatch")
    reported_overlap_fraction = _number(
        summary.get("release_events_during_compute_fraction"),
        "release_events_during_compute_fraction",
    )
    assert reported_overlap_fraction is not None
    _close(
        reported_overlap_fraction,
        releases_during_compute / len(online_releases) if online_releases else 0.0,
        "release-during-compute fraction",
    )

    required_event_fields = {
            "event_id", "trigger_reason", "trigger_class", "event_time_s", "pending_count",
            "oldest_pending_age_s", "admitted_task_ids", "admitted_task_count", "piggybacked",
            "participating_logical_robots", "associated_call_ids",
            "virtual_event_completion_s",
    }
    if strict_admission_architecture:
        required_event_fields.update({
            "terminal_residual", "announcement_delivery_count",
            "announcement_delivery_time_s_by_robot",
        })
    event_rows = _csv(
        directory / "reallocation_events.csv",
        required_event_fields,
    )
    validate_csv_dimensions(event_rows, "reallocation_events.csv")
    event_ids: set[str] = set()
    event_associations: dict[str, set[str]] = {}
    event_reasons: dict[str, str] = {}
    admitted_tasks_seen: set[str] = set()
    tasks_by_id = {row["task_id"]: row for row in task_rows}
    arrival_events = mandatory_events = piggybacks = timeout_events = batch_events = final_flushes = 0
    terminal_residuals = 0
    terminal_residual_admitted_tasks = 0
    initial_events = 0
    trigger_reason_counts: dict[str, int] = {}
    released_task_times = [
        float(item["release_time_s"])
        for item in task_rows
        if item["release_time_s"] not in {None, ""}
    ]
    maximum_release_time = max(released_task_times, default=0.0)
    event_delivery_by_epoch: dict[int, dict[str, float]] = {}
    for row in event_rows:
        if row["event_id"] in event_ids:
            raise CausalOutputError(f"duplicate event ID: {row['event_id']}")
        event_ids.add(row["event_id"])
        event_reasons[row["event_id"]] = row["trigger_reason"]
        event_time = _number(row["event_time_s"], "event_time_s")
        assert event_time is not None
        trigger_class = row["trigger_class"]
        if trigger_class not in {
            "arrival_driven", "mandatory", "initial", "terminal_residual",
            "final_flush",
        }:
            raise CausalOutputError(f"unsupported trigger class: {trigger_class}")
        arrival_events += int(trigger_class == "arrival_driven")
        mandatory_events += int(trigger_class == "mandatory")
        reason = row["trigger_reason"]
        reason_to_class = {
            "initial_allocation": "initial",
            "task_arrival_eager": "arrival_driven",
            "batch_threshold": "arrival_driven",
            "age_timeout": "arrival_driven",
            "terminal_residual": "terminal_residual",
            "final_release_flush": "final_flush",
            "task_completion": "mandatory",
            "invalid_goal": "mandatory",
            "robot_idle": "mandatory",
        }
        if reason not in reason_to_class or trigger_class != reason_to_class[reason]:
            raise CausalOutputError("trigger reason/class semantics are inconsistent")
        trigger_reason_counts[reason] = trigger_reason_counts.get(reason, 0) + 1
        initial_events += int(reason == "initial_allocation")
        piggybacked = _bool(row["piggybacked"], "piggybacked")
        piggybacks += int(piggybacked)
        timeout_events += int(reason == "age_timeout")
        batch_events += int(reason == "batch_threshold")
        terminal_residuals += int(reason == "terminal_residual")
        final_flushes += int(reason == "final_release_flush")
        associated = [item for item in row["associated_call_ids"].split(";") if item]
        if len(associated) != len(set(associated)):
            raise CausalOutputError("reallocation event repeats an allocator call ID")
        if any(item not in call_ids for item in associated):
            raise CausalOutputError("event references an unknown allocator call")
        event_associations[row["event_id"]] = set(associated)
        admitted = [item for item in row["admitted_task_ids"].split(";") if item]
        if len(admitted) != len(set(admitted)):
            raise CausalOutputError("reallocation event repeats an admitted task ID")
        if admitted_tasks_seen.intersection(admitted):
            raise CausalOutputError("task is admitted by more than one reallocation event")
        if any(task_id not in tasks_by_id for task_id in admitted):
            raise CausalOutputError("event admits an unknown task")
        admitted_tasks_seen.update(admitted)
        admitted_count = _integer(row["admitted_task_count"], "admitted_task_count")
        if reason == "terminal_residual":
            terminal_residual_admitted_tasks += admitted_count
        pending_count = _integer(row["pending_count"], "pending_count")
        oldest_pending_age = _number(
            row["oldest_pending_age_s"], "oldest_pending_age_s"
        )
        assert oldest_pending_age is not None
        if admitted_count != len(admitted):
            raise CausalOutputError("admitted task count/list mismatch")
        terminal_flag = (
            _bool(row["terminal_residual"], "terminal_residual")
            if strict_admission_architecture else
            reason == "terminal_residual"
        )
        if terminal_flag != (reason == "terminal_residual"):
            raise CausalOutputError(
                "event terminal-residual metadata disagrees with its trigger"
            )
        if strict_admission_architecture:
            if piggybacked:
                raise CausalOutputError(
                    "strict-admission output contains a piggybacked admission"
                )
            if reason == "final_release_flush":
                raise CausalOutputError(
                    "strict-admission output contains a final-release flush"
                )
            if reason == "initial_allocation":
                if event_time != 0.0 or pending_count != 0:
                    raise CausalOutputError(
                        "initial allocation epoch has invalid queue semantics"
                    )
            elif admitted:
                if pending_count < admitted_count:
                    raise CausalOutputError(
                        "admission count exceeds the recorded pending queue"
                    )
                oldest_release = min(
                    float(tasks_by_id[task_id]["release_time_s"])
                    for task_id in admitted
                )
                _close(
                    oldest_pending_age,
                    event_time - oldest_release,
                    "event oldest pending age",
                )
            elif pending_count != 0 or oldest_pending_age != 0.0:
                raise CausalOutputError(
                    "autonomous bookkeeping event exposes pending-queue state"
                )
            if trigger_class == "mandatory" and admitted_count:
                raise CausalOutputError(
                    "autonomous event admitted pending tasks"
                )
        else:
            if reason == "initial_allocation":
                if event_time != 0.0 or pending_count != 0 or piggybacked:
                    raise CausalOutputError(
                        "initial allocation epoch has invalid queue semantics"
                    )
            else:
                if pending_count != admitted_count:
                    raise CausalOutputError(
                        "legacy admission epoch did not drain its pending queue"
                    )
                if admitted:
                    oldest_release = min(
                        float(tasks_by_id[task_id]["release_time_s"])
                        for task_id in admitted
                    )
                    _close(
                        oldest_pending_age,
                        event_time - oldest_release,
                        "event oldest pending age",
                    )
                else:
                    _close(oldest_pending_age, 0.0, "empty event oldest pending age")
            expected_piggyback = trigger_class == "mandatory" and admitted_count > 0
            if piggybacked != expected_piggyback:
                raise CausalOutputError(
                    "mandatory pending-admission piggyback flag is inconsistent"
                )
        if reason == "task_arrival_eager" and admitted_count != 1:
            raise CausalOutputError("Eager admission did not admit exactly one task")
        if reason == "batch_threshold" and (
            pending_count < job.policy.batch_size
            or (
                strict_admission_architecture
                and admitted_count != job.policy.batch_size
            )
        ):
            raise CausalOutputError(
                "batch threshold did not admit exactly configured B"
            )
        if reason == "age_timeout":
            if job.policy.mode != "bounded" or job.policy.max_pending_age_s is None:
                raise CausalOutputError("timeout event occurred under a non-bounded policy")
            if oldest_pending_age + 1e-8 < job.policy.max_pending_age_s:
                raise CausalOutputError("timeout event fired before configured W")
            if strict_admission_architecture and pending_count != admitted_count:
                raise CausalOutputError(
                    "age-timeout admission did not drain the pending set"
                )
        if reason == "final_release_flush":
            _close(event_time, maximum_release_time, "final flush/last release time")
            if pending_count >= job.policy.batch_size:
                raise CausalOutputError("final partial flush is not below configured B")
        if reason == "terminal_residual":
            if job.policy.mode == "eager":
                raise CausalOutputError("Eager policy emitted a terminal residual")
            if pending_count != admitted_count or pending_count >= job.policy.batch_size:
                raise CausalOutputError(
                    "terminal residual is not the final sub-B pending set"
                )
            if any(
                float(task["release_time_s"]) > event_time + 1e-9
                for task in manifest_tasks.values()
            ):
                raise CausalOutputError(
                    "terminal residual fired while tasks were still unreleased"
                )
            prior_task_ids = {
                task_id
                for task_id, task in tasks_by_id.items()
                if task_id not in admitted
                and task["admission_time_s"] not in {None, ""}
                and float(task["admission_time_s"]) <= event_time + 1e-9
            }
            if any(
                tasks_by_id[task_id]["completion_time_s"] in {None, ""}
                or float(tasks_by_id[task_id]["completion_time_s"])
                > event_time + 1e-9
                for task_id in prior_task_ids
            ):
                raise CausalOutputError(
                    "terminal residual fired before all previously admitted work completed"
                )
        if job.policy.mode == "eager" and reason in {
            "batch_threshold", "age_timeout", "terminal_residual",
            "final_release_flush",
        }:
            raise CausalOutputError("Eager policy emitted a coalescing trigger")
        if job.policy.mode == "count" and reason in {"task_arrival_eager", "age_timeout"}:
            raise CausalOutputError("count policy emitted an incompatible trigger")
        if job.policy.mode == "bounded" and reason == "task_arrival_eager":
            raise CausalOutputError("bounded policy emitted an Eager trigger")
        for task_id in admitted:
            admission_time = _number(
                tasks_by_id[task_id]["admission_time_s"],
                "admission_time_s",
            )
            assert admission_time is not None
            _close(admission_time, event_time, "event/task admission timestamp")
        if strict_admission_architecture:
            delivery_raw = _json_mapping(
                row["announcement_delivery_time_s_by_robot"],
                "announcement_delivery_time_s_by_robot",
            )
            deliveries: dict[str, float] = {}
            for robot_id, raw_time in delivery_raw.items():
                if robot_id not in robot_ids:
                    raise CausalOutputError(
                        "admission announcement names an unknown robot"
                    )
                delivery_s = _number(raw_time, "announcement delivery time")
                assert delivery_s is not None
                if delivery_s + 1e-9 < event_time:
                    raise CausalOutputError(
                        "admission announcement was delivered before admission"
                    )
                deliveries[robot_id] = delivery_s
            if _integer(
                row["announcement_delivery_count"],
                "announcement_delivery_count",
            ) != len(deliveries):
                raise CausalOutputError(
                    "admission announcement delivery count/map mismatch"
                )
            if not admitted and deliveries:
                raise CausalOutputError(
                    "non-admission event has admission-announcement deliveries"
                )
            epoch_match = re.search(r"/event-([0-9]+)$", row["event_id"])
            if epoch_match is None:
                raise CausalOutputError("event ID lacks a numeric admission epoch")
            epoch_id = int(epoch_match.group(1))
            event_delivery_by_epoch[epoch_id] = deliveries
            for task_id in admitted:
                if task_admission_epochs.get(task_id) != epoch_id:
                    raise CausalOutputError(
                        "task admission epoch metadata disagrees with event"
                    )
                if task_receipts.get(task_id, {}) != deliveries:
                    raise CausalOutputError(
                        "task receipt map disagrees with its admission announcement"
                    )
        call_subset = [item for item in call_rows if item["call_id"] in associated]
        if strict_admission_architecture and any(
            item["event_id"] != row["event_id"] for item in call_subset
        ):
            raise CausalOutputError(
                "event associates a call that did not consume its admission"
            )
        participants = {
            item for item in row["participating_logical_robots"].split(";") if item
        }
        if participants != {item["logical_robot_id"] for item in call_subset}:
            raise CausalOutputError("event participating robot/call set mismatch")
        completion = _number(
            row["virtual_event_completion_s"],
            "virtual_event_completion_s",
            optional=not associated,
        )
        if associated:
            assert completion is not None
            expected_completion = max(
                float(item["virtual_compute_completion_s"]) for item in call_subset
            )
            _close(completion, expected_completion, "event compute completion")
            if strict_admission_architecture:
                for item in call_subset:
                    delivery_s = event_delivery_by_epoch[epoch_id].get(
                        item["logical_robot_id"]
                    )
                    if delivery_s is None:
                        raise CausalOutputError(
                            "admission call lacks a recorded announcement receipt"
                        )
                    if float(item["virtual_compute_start_s"]) + 1e-9 < delivery_s:
                        raise CausalOutputError(
                            "admission call began before announcement delivery"
                        )

    for call in call_rows:
        event_id = call["event_id"]
        if strict_admission_architecture:
            admission_call_reasons = {
                "initial_allocation", "task_arrival_eager", "batch_threshold",
                "age_timeout", "terminal_residual",
            }
            autonomous_call_reasons = {
                "task_completion", "peer_task_completion", "invalid_goal",
                "robot_idle", "allocator_message", "peer_state_update",
                "stalled_recovery", "quarantine_expired", "consensus/internal",
                "consensus", "internal", "other",
            }
            if call["trigger_reason"] not in (
                admission_call_reasons | autonomous_call_reasons
            ):
                raise CausalOutputError("allocator call has an unknown trigger reason")
            if event_id:
                if call["trigger_reason"] != event_reasons.get(event_id):
                    raise CausalOutputError(
                        "allocator call trigger disagrees with admission event"
                    )
            elif call["trigger_reason"] in admission_call_reasons:
                raise CausalOutputError(
                    "admission-triggered allocator call lacks an event association"
                )
        if not event_id:
            continue
        if event_id not in event_associations or call["call_id"] not in event_associations[event_id]:
            raise CausalOutputError("allocator call/event association is not bidirectionally exact")
    expected_admitted = {
        row["task_id"] for row in task_rows if row["admission_time_s"] not in {"", None}
    }
    if admitted_tasks_seen != expected_admitted:
        raise CausalOutputError("event admission set differs from task lifecycle table")
    if initial_events != 1:
        raise CausalOutputError("trial must contain exactly one initial allocation epoch")

    queue_rows = _csv(
        directory / "pending_queue_samples.csv",
        {
            "sample_index", "sample_time_s", "pending_depth",
            "oldest_pending_age_s", "sample_event",
        },
        allow_empty=True,
    )
    validate_csv_dimensions(queue_rows, "pending_queue_samples.csv")
    queue_depths: list[int] = []
    queue_ages: list[float] = []
    for expected_index, row in enumerate(queue_rows):
        if _integer(row["sample_index"], "sample_index") != expected_index:
            raise CausalOutputError("pending queue sample indices are not contiguous")
        _number(row["sample_time_s"], "sample_time_s")
        queue_depths.append(_integer(row["pending_depth"], "pending_depth"))
        age = _number(row["oldest_pending_age_s"], "oldest_pending_age_s")
        assert age is not None
        queue_ages.append(age)
        if re.fullmatch(
            r"release|trigger:(?:task_arrival_eager|batch_threshold|age_timeout|"
            r"terminal_residual|final_release_flush|task_completion|invalid_goal|robot_idle)|"
            r"admit:(?:task_arrival_eager|batch_threshold|age_timeout|"
            r"terminal_residual|final_release_flush|task_completion|invalid_goal|robot_idle)",
            row["sample_event"],
        ) is None:
            raise CausalOutputError("pending queue sample has an unknown event label")

    movement_rows = _csv(
        directory / "movement_events.csv",
        {
            "movement_id", "logical_robot_id", "from_x", "from_y", "to_x", "to_y",
            "movement_start_s", "movement_duration_s", "movement_completion_s", "completed_task_ids",
        },
        allow_empty=not _bool(summary.get("all_tasks_completed"), "all_tasks_completed"),
    )
    validate_csv_dimensions(movement_rows, "movement_events.csv")
    movement_tasks: dict[str, float] = {}
    movement_action_indices: dict[str, list[int]] = {}
    movement_timing_keys: set[str] = set()
    for row in movement_rows:
        start = _number(row["movement_start_s"], "movement_start_s")
        duration = _number(row["movement_duration_s"], "movement_duration_s")
        completion = _number(row["movement_completion_s"], "movement_completion_s")
        assert start is not None and duration is not None and completion is not None
        _close(completion, start + duration, "movement completion")
        robot_id = row["logical_robot_id"]
        if robot_id not in robot_ids:
            raise CausalOutputError("movement row has an unknown logical robot")
        action_index = _integer(row.get("robot_action_index"), "robot_action_index")
        movement_action_indices.setdefault(robot_id, []).append(action_index)
        if re.fullmatch(r"[0-9a-f]{64}", row.get("movement_timing_key_sha256", "")) is None:
            raise CausalOutputError("movement row lacks deterministic timing-key hash")
        if row["movement_timing_key_sha256"] in movement_timing_keys:
            raise CausalOutputError("movement timing key is reused")
        movement_timing_keys.add(row["movement_timing_key_sha256"])
        if row.get("movement_timing_model") != summary.get("movement_timing_model"):
            raise CausalOutputError("movement timing model differs from trial summary")
        for task_id in (item for item in row["completed_task_ids"].split(";") if item):
            if task_id not in tasks_by_id or task_id in movement_tasks:
                raise CausalOutputError("movement completion task association is invalid")
            movement_tasks[task_id] = completion
    for robot_id, indices in movement_action_indices.items():
        if sorted(indices) != list(range(len(indices))):
            raise CausalOutputError(
                f"movement action indices are not contiguous for {robot_id}"
            )
    for task in task_rows:
        if not task["completion_time_s"]:
            continue
        mode = task.get("completion_mode")
        if mode in {"stationary_service", "current_cell_service"}:
            if task["task_id"] in movement_tasks:
                raise CausalOutputError("stationary task is also attributed to movement")
        else:
            if task["task_id"] not in movement_tasks:
                raise CausalOutputError("movement-completed task lacks movement association")
            _close(
                movement_tasks[task["task_id"]],
                float(task["completion_time_s"]),
                "task/movement completion timestamp",
            )

    all_completed = _bool(summary.get("all_tasks_completed"), "all_tasks_completed")
    if summary.get("technical_status") != "completed":
        raise CausalOutputError("promoted trial is not technically completed")
    expected_algorithmic = "completed" if all_completed else "incomplete"
    expected_trial_status = "completed" if all_completed else "algorithmic_incomplete"
    if summary.get("trial_status") != expected_trial_status:
        raise CausalOutputError("trial status disagrees with algorithmic outcome")
    if summary.get("algorithmic_status") != expected_algorithmic:
        raise CausalOutputError("algorithmic status disagrees with task completion")
    expected_failure_type = None if all_completed else (
        summary.get("algorithmic_failure_type") or "algorithmic_incomplete"
    )
    if summary.get("failure_type") != expected_failure_type:
        raise CausalOutputError("failure type disagrees with algorithmic outcome")
    if _integer(
        summary.get("causal_event_horizon_events"),
        "causal_event_horizon_events",
    ) != 251_000:
        raise CausalOutputError("causal event horizon differs from predeclared study value")
    if _integer(
        summary.get("causal_stagnation_horizon_events"),
        "causal_stagnation_horizon_events",
    ) != 5_500:
        raise CausalOutputError("causal stagnation horizon differs from predeclared study value")
    if _bool(summary.get("completion"), "completion") != all_completed:
        raise CausalOutputError("completion alias disagrees with task completion")
    expected_zero_pair = (
        job.job_id[:-len("__zero")]
        if job.zero_compute and job.job_id.endswith("__zero")
        else f"{job.job_id}__zero"
    )
    if summary.get("zero_compute_paired_id") != expected_zero_pair:
        raise CausalOutputError("zero-compute paired ID mismatch")
    if _integer(summary.get("movement_timing_seed"), "movement_timing_seed") != job.runtime_seed:
        raise CausalOutputError("movement timing seed differs from paired runtime seed")
    if summary.get("movement_timing_trace_id") != job.trace_id:
        raise CausalOutputError("movement timing trace identity mismatch")
    for field in ("movement_timing_model", "movement_timing_seed", "movement_timing_trace_id"):
        if provenance.get(field) != summary.get(field):
            raise CausalOutputError(f"movement timing provenance mismatch: {field}")
    if all_completed != (len(completed_times) == len(manifest_tasks)):
        raise CausalOutputError("summary/task completion status mismatch")
    if _integer(summary.get("completed_task_count"), "completed_task_count") != len(completed_times):
        raise CausalOutputError("completed task count mismatch")
    mission_raw = _summary_value(summary, "mission_elapsed_time_s", "causal_mission_elapsed_time_s")
    mission = _number(mission_raw, "mission_elapsed_time_s", optional=not all_completed)
    simulated_execution = _number(
        summary.get("simulated_execution_time_s"),
        "simulated_execution_time_s",
    )
    algorithmic_horizon = _number(
        summary.get("algorithmic_horizon_time_s"),
        "algorithmic_horizon_time_s",
        optional=True,
    )
    assert simulated_execution is not None
    if all_completed:
        assert mission is not None
        _close(mission, max(completed_times), "mission elapsed/final completion")
        _close(simulated_execution, mission, "completed causal event clock")
        if algorithmic_horizon is not None:
            raise CausalOutputError(
                "completed mission must not publish an algorithmic horizon time"
            )
    else:
        if mission is not None:
            raise CausalOutputError(
                "incomplete mission must leave mission_elapsed_time_s null"
            )
        if algorithmic_horizon is None:
            raise CausalOutputError(
                "incomplete mission lacks algorithmic_horizon_time_s"
            )
        _close(
            algorithmic_horizon,
            simulated_execution,
            "algorithmic horizon/causal event clock",
        )
    def nearest_p95(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]

    for prefix, values in (
        ("release_to_first_assignment", assignment_latencies),
        ("release_to_completion", completion_latencies),
    ):
        expected_values = {
            "mean": statistics.fmean(values) if values else 0.0,
            "median": statistics.median(values) if values else 0.0,
            "p95": nearest_p95(values),
            "max": max(values, default=0.0),
        }
        for statistic_name, expected in expected_values.items():
            field = f"{statistic_name}_{prefix}_latency_s"
            actual = _number(summary.get(field), field)
            assert actual is not None
            _close(actual, expected, field)
    reported_agx = _number(
        _summary_value(summary, "agx_allocator_processor_work_s", "total_agx_allocator_work_s"),
        "AGX allocator processor work",
    )
    assert reported_agx is not None
    _close(reported_agx, agx_work, "AGX processor work")
    if strict_admission_architecture:
        for field, expected in (
            ("allocator_processor_work_s", allocator_work),
            ("cumulative_allocator_time_s", allocator_work),
            ("allocator_processor_primary_work_s", allocator_primary_work),
            (
                "allocator_processor_admission_callback_work_s",
                allocator_admission_callback_work,
            ),
        ):
            actual = _number(summary.get(field), field)
            assert actual is not None
            _close(actual, expected, field)
        if hardware_validated:
            for field, expected in (
                ("rp2040_allocator_processor_work_s", rp_work),
                ("rp2040_choose_goal_processor_work_s", rp_choose_work),
                ("rp2040_epoch_reset_processor_work_s", rp_reset_work),
                ("W_alloc_rp2040_s", rp_work),
            ):
                actual = _number(summary.get(field), field)
                assert actual is not None
                _close(actual, expected, field)
        else:
            for field in (
                "rp2040_allocator_processor_work_s",
                "rp2040_choose_goal_processor_work_s",
                "rp2040_epoch_reset_processor_work_s",
                "W_alloc_rp2040_s",
                "mean_rp2040_allocator_call_time_s",
                "median_rp2040_allocator_call_time_s",
                "p95_rp2040_allocator_call_time_s",
                "rp2040_processor_work_per_call_s",
                "rp2040_processor_work_per_reallocation_event_s",
                "rp2040_processor_work_per_completed_task_s",
            ):
                if summary.get(field) is not None:
                    raise CausalOutputError(
                        f"non-hardware output reports RP2040 performance: {field}"
                    )
        if _integer(
            summary.get("allocator_input_event_total"),
            "allocator_input_event_total",
        ) != allocator_input_event_total:
            raise CausalOutputError("allocator input-event total mismatch")
        if _integer(
            summary.get("recovery_invocation_count"),
            "recovery_invocation_count",
        ) != recovery_invocation_count:
            raise CausalOutputError("recovery invocation count mismatch")
    else:
        reported_rp = _number(
            _summary_value(
                summary,
                "rp2040_allocator_processor_work_s",
                "total_rp2040_allocator_work_s",
            ),
            "rp2040 allocator processor work",
        )
        assert reported_rp is not None
        _close(reported_rp, rp_work, "RP2040 processor work")
    for field, expected in (
        ("agx_choose_goal_processor_work_s", agx_choose_work),
        ("agx_epoch_reset_processor_work_s", agx_reset_work),
    ):
        actual = _number(summary.get(field), field)
        assert actual is not None
        _close(actual, expected, field)
    if _integer(summary.get("allocator_call_count"), "allocator_call_count") != len(call_rows):
        raise CausalOutputError("allocator call count mismatch")
    if _integer(summary.get("total_team_steps"), "total_team_steps") != len(movement_rows):
        raise CausalOutputError("total team steps do not equal explicit movement events")
    maximum_steps = max((len(values) for values in movement_action_indices.values()), default=0)
    if _integer(summary.get("max_robot_steps"), "max_robot_steps") != maximum_steps:
        raise CausalOutputError("max robot steps do not match movement events")
    if _integer(_summary_value(summary, "reallocation_event_count", "allocation_epoch_count"), "event count") != len(event_rows):
        raise CausalOutputError("reallocation event count mismatch")
    for field, actual in (
        ("arrival_driven_event_count", arrival_events),
        ("mandatory_event_count", mandatory_events),
        ("piggybacked_admission_event_count", piggybacks),
        ("timeout_trigger_count", timeout_events),
        ("batch_threshold_trigger_count", batch_events),
        ("terminal_residual_event_count", terminal_residuals),
        ("final_flush_event_count", final_flushes),
    ):
        if field in summary and _integer(summary[field], field) != actual:
            raise CausalOutputError(f"{field} mismatch")
    exact_count_fields = {
        "arrival_induced_trigger_count": (
            arrival_events
            if strict_admission_architecture
            else arrival_events + final_flushes
        ),
        "mandatory_trigger_count": mandatory_events + initial_events,
        "mandatory_reallocation_trigger_count": mandatory_events,
        "initial_allocation_epoch_count": initial_events,
        "piggybacked_admission_epoch_count": piggybacks,
        "terminal_residual_trigger_count": terminal_residuals,
        "terminal_residual_admitted_task_count": (
            terminal_residual_admitted_tasks
        ),
        "final_flush_trigger_count": final_flushes,
    }
    for field, expected in exact_count_fields.items():
        if field in summary and _integer(summary[field], field) != expected:
            raise CausalOutputError(f"{field} mismatch")
    if summary.get("trigger_reason_counts") != dict(sorted(trigger_reason_counts.items())):
        raise CausalOutputError("trigger reason counts differ from reallocation events")
    if summary.get("pending_queue_sampling") != (
        "event_sampled_release_trigger_and_post_admission_not_time_weighted"
    ):
        raise CausalOutputError("pending queue sampling definition is missing or changed")
    for field, expected in (
        ("mean_pending_queue_depth", statistics.fmean(queue_depths) if queue_depths else 0.0),
        ("max_pending_queue_depth", max(queue_depths, default=0)),
        ("mean_pending_age_s", statistics.fmean(queue_ages) if queue_ages else 0.0),
        ("max_pending_age_s", max(queue_ages, default=0.0)),
    ):
        actual = _number(summary.get(field), field)
        assert actual is not None
        _close(actual, float(expected), field)

    call_distribution = {
        "mean_allocator_call_time_s": (
            statistics.fmean(allocator_call_times)
            if allocator_call_times else 0.0
        ),
        "median_allocator_call_time_s": (
            statistics.median(allocator_call_times)
            if allocator_call_times else 0.0
        ),
        "p95_allocator_call_time_s": nearest_p95(allocator_call_times),
        "max_allocator_call_time_s": max(allocator_call_times, default=0.0),
        "mean_agx_allocator_call_time_s": (
            statistics.fmean(agx_call_times) if agx_call_times else 0.0
        ),
        "median_agx_allocator_call_time_s": (
            statistics.median(agx_call_times) if agx_call_times else 0.0
        ),
        "p95_agx_allocator_call_time_s": nearest_p95(agx_call_times),
        "allocator_time_per_completed_task_s": (
            allocator_work / len(completed_times) if completed_times else 0.0
        ),
    }
    if strict_admission_architecture:
        call_distribution.update({
            "allocator_processor_work_per_call_s": (
                allocator_work / len(call_rows) if call_rows else 0.0
            ),
            "allocator_processor_work_per_reallocation_event_s": (
                allocator_work / len(event_rows) if event_rows else 0.0
            ),
            "allocator_processor_work_per_completed_task_s": (
                allocator_work / len(completed_times)
                if completed_times else 0.0
            ),
        })
    if hardware_validated or not strict_admission_architecture:
        call_distribution.update({
            "mean_rp2040_allocator_call_time_s": (
                statistics.fmean(rp_call_times) if rp_call_times else 0.0
            ),
            "median_rp2040_allocator_call_time_s": (
                statistics.median(rp_call_times) if rp_call_times else 0.0
            ),
            "p95_rp2040_allocator_call_time_s": nearest_p95(rp_call_times),
            "rp2040_processor_work_per_call_s": (
                rp_work / len(call_rows) if call_rows else 0.0
            ),
            "rp2040_processor_work_per_reallocation_event_s": (
                rp_work / len(event_rows) if event_rows else 0.0
            ),
            "rp2040_processor_work_per_completed_task_s": (
                rp_work / len(completed_times) if completed_times else 0.0
            ),
        })
    for field, expected in call_distribution.items():
        actual = _number(summary.get(field), field)
        assert actual is not None
        _close(actual, expected, field)
    if all_completed and mission is not None:
        capacity = _number(summary.get("processor_capacity_fraction"), "processor_capacity_fraction")
        assert capacity is not None
        _close(
            capacity,
            allocator_work / (4.0 * mission),
            "processor capacity fraction",
        )
    hashes = {name: sha256_file(directory / name) for name in REQUIRED_OUTPUTS}
    return {
        "schema_version": 1,
        "valid": True,
        "job_id": job.job_id,
        "task_count": len(task_rows),
        "completed_task_count": len(completed_times),
        "allocator_call_count": len(call_rows),
        "reallocation_event_count": len(event_rows),
        "movement_event_count": len(movement_rows),
        "allocator_processor_work_s": allocator_work,
        "rp2040_allocator_processor_work_s": (
            rp_work
            if hardware_validated or not strict_admission_architecture else None
        ),
        "agx_allocator_processor_work_s": agx_work,
        "mission_elapsed_time_s": mission,
        "required_output_sha256": hashes,
    }


def completion_fingerprint(config: CausalConfig, job: CausalJob, source_identity: Mapping[str, Any]) -> str:
    payload = {
        "schema_version": 1,
        "config_sha256": config.config_sha256,
        "manifest_index_sha256": config.manifest_index_sha256,
        "job": job.identity(),
        "source_identity": dict(source_identity),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
