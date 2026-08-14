"""In-process adapter for one RP2040-timed causal campaign job.

The native campaign keeps one timing provider/board session in each worker and
calls :func:`run_causal_manifest_job` repeatedly.  This module performs no
board discovery and owns no concurrency; it validates immutable paired inputs,
runs one causal mission, and atomically writes the canonical raw artifacts.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from known_visit_sim.algorithms.registry import load_allocator_class
from known_visit_sim.comms.models import make_comm_model
from known_visit_sim.config import SimConfig
from known_visit_sim.core.reallocation import ARRIVAL_REASONS, ReallocationPolicy
from known_visit_sim.core.scheduler import AsyncTrialRunner
from known_visit_sim.core.types import TrialScenario
from known_visit_sim.run_online_trials import load_paired_manifests


ALLOCATOR_PROCESSOR_WORK_DEFINITION = (
    "allocator input integration, consensus message handling, allocator-local "
    "recovery, and choose_goal; excludes transport, message decoding, generic "
    "PSETUP synchronization, outbound extraction, serialization, and explicit "
    "pre-call GC; GC triggered naturally inside the allocator transaction remains "
    "included"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_csv(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_csv(
    path: Path,
    rows: Iterable[Mapping[str, Any]],
    *,
    empty_fieldnames: Iterable[str] | None = None,
) -> None:
    values = list(rows)
    if not values and empty_fieldnames is None:
        raise ValueError(f"refusing to write empty canonical table: {path.name}")
    fields: list[str] = list(empty_fieldnames or ())
    for row in values:
        for name in row:
            if name not in fields:
                fields.append(name)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in values:
            writer.writerow({name: _normalize_csv(row.get(name)) for name in fields})
    os.replace(temporary, path)


def _git(repo_root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args], cwd=repo_root, text=True,
                capture_output=True, check=True, timeout=15,
            )
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "unavailable"

    status = run("status", "--porcelain=v1")
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "dirty": status not in {"", "unavailable"},
    }


def _policy(value: Mapping[str, Any]) -> ReallocationPolicy:
    return ReallocationPolicy.from_spec(value)


def _event_id(trace_id: str, epoch_id: int | None) -> str:
    return "" if epoch_id is None else f"{trace_id}/event-{epoch_id:06d}"


def _trigger_class(epoch: Any) -> str:
    if epoch.trigger_reason == "initial_allocation":
        return "initial"
    if epoch.trigger_reason == "terminal_residual":
        return "terminal_residual"
    if epoch.trigger_reason == "final_release_flush":
        return "final_flush"
    if epoch.mandatory:
        return "mandatory"
    if epoch.trigger_reason in ARRIVAL_REASONS:
        return "arrival_driven"
    return "arrival_driven"


def _allocator_event_id(trace_id: str, call: Any, scheduler: Any) -> str:
    """Associate only the call that consumes a task-admission announcement.

    Autonomous consensus, completion, invalid-goal, and recovery calls may retain
    the scheduler's most recent epoch ID as diagnostic context.  That context is
    not an admission-event association and must not make those calls look like
    centralized reallocations in the exported data.
    """

    epoch = scheduler.epoch_by_id(call.epoch_id)
    if (
        epoch is None
        or not epoch.admitted_task_ids
        or str(call.trigger_reason) != str(epoch.trigger_reason)
    ):
        return ""
    return _event_id(trace_id, epoch.epoch_id)


def _allocator_timing_source(*, zero_compute: bool, hardware_validated: bool) -> str:
    if hardware_validated:
        return "rp2040_hardware"
    if zero_compute:
        return "zero_compute_counterfactual"
    return "simulated_duration_development_proxy"


def _positive_release_times(task_rows: Iterable[Mapping[str, Any]]) -> list[float]:
    """Return released online-task times while retaining incomplete rows.

    An algorithmic horizon can be reached before every scheduled task release.
    Those retained task rows intentionally carry ``release_time_s=None`` and
    must not turn the scientific incomplete outcome into a reporting failure.
    """

    values: set[float] = set()
    for row in task_rows:
        raw = row.get("release_time_s")
        if raw is None or raw == "":
            continue
        value = float(raw)
        if value > 0.0:
            values.add(value)
    return sorted(values)


def run_causal_manifest_job(
    *,
    scenario_manifest: Path,
    release_manifest: Path,
    scenario_sha256: str,
    release_sha256: str,
    algorithm: str,
    arrival_load: str,
    trace_id: str,
    job_id: str,
    block_id: str,
    policy: Mapping[str, Any],
    runtime_seed: int,
    timing_provider: Any,
    zero_compute: bool,
    output_dir: Path,
    campaign_identity: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate, execute, and write one immutable causal mission."""

    repo_root = Path(__file__).resolve().parents[1]
    output = Path(output_dir).resolve()
    if output.exists() and any(output.iterdir()):
        # The orchestrator creates job/worker-binding evidence first.  Those
        # two files are the only permissible pre-existing artifacts.
        unexpected = {
            path.name for path in output.iterdir()
            if path.name not in {"job.json", "worker_binding.json"}
        }
        if unexpected:
            raise FileExistsError(
                "refusing to overwrite causal outputs: " + ", ".join(sorted(unexpected))
            )
    output.mkdir(parents=True, exist_ok=True)
    scenario_raw, release_raw, tasks = load_paired_manifests(
        Path(scenario_manifest).resolve(),
        Path(release_manifest).resolve(),
        scenario_sha256,
        release_sha256,
        trace_id,
        runtime_seed,
    )
    if str(release_raw.get("load_id")) != str(arrival_load):
        raise ValueError("arrival load differs from paired release manifest")
    algorithm = str(algorithm).upper()
    allocator_cls = load_allocator_class(algorithm)
    starts = scenario_raw.get("robot_starts")
    if not isinstance(starts, list) or len(starts) != 4:
        raise ValueError("causal hardware mission requires exactly four robot starts")
    robot_ids: list[str] = []
    start_positions: dict[str, tuple[int, int]] = {}
    start_headings: dict[str, tuple[int, int]] = {}
    for row in starts:
        rid = str(row["robot_id"])
        if rid in start_positions:
            raise ValueError(f"duplicate robot ID: {rid}")
        robot_ids.append(rid)
        start_positions[rid] = int(row["x"]), int(row["y"])
        start_headings[rid] = int(row.get("heading_x", 1)), int(row.get("heading_y", 0))
    targets = [(int(row["x"]), int(row["y"])) for row in tasks]
    trial = TrialScenario(
        trial_id=int.from_bytes(hashlib.sha256(trace_id.encode()).digest()[:4], "big"),
        targets=targets,
        metadata={
            "trace_id": trace_id,
            "runtime_seed": int(runtime_seed),
            "task_ids": [str(row["task_id"]) for row in tasks],
            "scenario_sha256": scenario_sha256,
            "release_sha256": release_sha256,
            # Predeclared algorithmic noncompletion guardrails for a 50-task
            # causal mission. Crossing one returns a retained, technically
            # valid incomplete outcome; it is never relabeled as transport or
            # parity failure.
            "causal_event_horizon_events": 251_000,
            "causal_stagnation_horizon_events": 5_500,
        },
    )
    cfg = SimConfig(
        grid_size=int(scenario_raw["grid_size"]),
        robot_ids=robot_ids,
        start_positions=start_positions,
        start_headings=start_headings,
        robot_start_layout="manifest",
        condition_id=job_id,
        # Ideal communication retains the legacy fixed 40 ms delivery delay
        # but removes sequential RNG jitter, so paired policy/provider runs do
        # not reassign exogenous delay draws when message interleaving changes.
        comm_delay_s=0.04,
        comm_delay_jitter_s=0.0,
        commitment_horizon=None,
        max_candidate_cells=None,
    )
    release_times = {
        cell: float(row["release_time_s"])
        for cell, row in zip(targets, tasks, strict=True)
    }
    policy_value = _policy(policy)
    state = AsyncTrialRunner(
        cfg,
        allocator_cls,
        make_comm_model("ideal", None),
        seed=int(runtime_seed),
        timing_provider=timing_provider,
    ).run_online_trial(trial, release_times, policy_value)
    state.validate_online_invariants()

    worker_board_id = str(campaign_identity.get("board_id", ""))
    dimensions = {
        "job_id": job_id,
        "block_id": block_id,
        "algorithm": algorithm,
        "arrival_load": arrival_load,
        "trace_id": trace_id,
        "policy_id": str(policy.get("policy_id", "")),
        "policy_mode": policy_value.mode,
        "policy_batch_size": policy_value.batch_size,
        "policy_max_pending_age_s": policy_value.max_pending_age_s,
        "B": policy_value.batch_size,
        "W": policy_value.max_pending_age_s,
        "runtime_seed": int(runtime_seed),
        "board_id": worker_board_id,
        "worker_index": int(campaign_identity.get("worker_index", -1)),
        "core_id": int(campaign_identity.get("core_id", -1)),
        "scenario_sha256": scenario_sha256,
        "release_sha256": release_sha256,
        "config_sha256": str(campaign_identity.get("config_sha256", "")),
        "manifest_index_sha256": str(
            campaign_identity.get("manifest_index_sha256", "")
        ),
        "hardware_binding_sha256": campaign_identity.get(
            "hardware_binding_sha256"
        ),
        "git_head": str(campaign_identity.get("git_head", "")),
        "source_tree_sha256": str(
            campaign_identity.get("source_tree_sha256", "")
        ),
        "expected_device_uid": str(
            campaign_identity.get("expected_device_uid", "")
        ),
        "expected_device_build_id": str(
            campaign_identity.get("expected_device_build_id", "")
        ),
        "expected_device_firmware_sha256": str(
            campaign_identity.get("expected_device_firmware_sha256", "")
        ),
        "expected_device_module_set_sha256": str(
            campaign_identity.get("expected_device_module_set_sha256", "")
        ),
        "communication_model": "ideal_fixed_delay_no_jitter",
        "communication_delay_s": 0.04,
        "communication_delay_jitter_s": 0.0,
        "python_hash_seed": str(
            campaign_identity.get(
                "python_hash_seed", os.environ.get("PYTHONHASHSEED", "uncontrolled")
            )
        ),
        "zero_compute": bool(zero_compute),
    }

    task_rows = []
    for row in state.task_rows():
        task_rows.append({
            **dimensions,
            "task_id": row["task_id"],
            "x": row["x"],
            "y": row["y"],
            "release_time_s": row["release_time_s"],
            "pending_time_s": row["pending_time_s"],
            "admission_time_s": row["admission_time_s"],
            "admission_epoch_id": row["admission_epoch_id"],
            "admission_trigger": row["admission_trigger"],
            "terminal_residual": row["terminal_residual"],
            "knowledge_receipt_time_s_by_robot": row[
                "knowledge_receipt_time_s_by_robot"
            ],
            "first_knowledge_receipt_time_s": row[
                "first_knowledge_receipt_time_s"
            ],
            "first_eligible_allocator_start_time_s": row[
                "first_eligible_allocator_start_time_s"
            ],
            "first_eligible_processing_time_s": row["first_eligible_processing_time_s"],
            "first_eligible_robot": row["first_eligible_robot"],
            "first_assignment_time_s": row["first_assignment_time_s"],
            "first_assigned_robot": row["first_assigned_robot"],
            "first_current_goal_time_s": row["first_current_goal_time_s"],
            "first_current_goal_robot": row["first_current_goal_robot"],
            "current_goal_events": row["current_goal_events"],
            "completion_time_s": row["completion_time_s"],
            "completing_robot": row["completing_robot"],
            "completion_mode": row["completion_mode"],
            "assignment_events": row["assignment_events"],
            "reassignment_count": row["reassignment_count"],
            "release_to_admission_latency_s": row["release_to_admission_latency_s"],
            "release_to_first_assignment_latency_s": row["release_to_first_assignment_latency_s"],
            "admission_to_first_assignment_latency_s": row["admission_to_first_assignment_latency_s"],
            "admission_to_first_current_goal_latency_s": row[
                "admission_to_first_current_goal_latency_s"
            ],
            "admission_to_first_eligible_allocator_start_latency_s": row[
                "admission_to_first_eligible_allocator_start_latency_s"
            ],
            "release_to_completion_latency_s": row["release_to_completion_latency_s"],
            "assignment_to_completion_latency_s": row[
                "assignment_to_completion_latency_s"
            ],
            "state": row["state"],
        })

    scheduler = state.reallocation_scheduler
    if scheduler is None:
        raise AssertionError("causal run has no reallocation scheduler")
    call_rows = []
    for call in scheduler.allocator_calls:
        if not call.valid_for_mission or not call.parity_passed:
            raise RuntimeError("invalid allocator call reached causal output promotion")
        provider_call_id = call.provider_call_id or str(call.call_id)
        call_board = (
            "ZERO_COMPUTE"
            if zero_compute
            else (
                call.board_id
                if call.hardware_validated
                else "SIMULATED_DURATION"
            )
        )
        device_goal = call.device_goal
        if device_goal is None and zero_compute:
            device_goal = call.authoritative_goal
        device_message_hash = call.device_message_sha256
        device_state_hash = call.device_post_state_sha256
        if zero_compute:
            device_message_hash = device_message_hash or call.outbound_message_sha256
            device_state_hash = device_state_hash or call.authoritative_post_state_sha256
        hardware_attestation = call.provider_metadata.get("hardware_attestation")
        allocator_timing_source = _allocator_timing_source(
            zero_compute=zero_compute,
            hardware_validated=bool(call.hardware_validated),
        )
        # The neutral allocator-processor columns are valid for hardware,
        # development proxies, and the zero-compute counterfactual.  RP2040-
        # named timing columns are populated only by attested hardware calls.
        rp2040_duration_s = (
            call.device_allocator_duration_s if call.hardware_validated else None
        )
        rp2040_choose_goal_s = (
            call.device_choose_goal_duration_s if call.hardware_validated else None
        )
        rp2040_epoch_reset_s = (
            call.algorithm_epoch_reset_duration_s
            if call.hardware_validated else None
        )
        call_rows.append({
            **dimensions,
            "call_id": provider_call_id,
            "call_group_id": call.group_id,
            "event_id": _allocator_event_id(trace_id, call, scheduler),
            "logical_robot_id": call.robot_id,
            "context_id": call.logical_context_id,
            "algorithm": algorithm,
            "virtual_compute_start_s": call.compute_start_time_s,
            "allocator_processor_duration_s": call.device_allocator_duration_s,
            "allocator_processor_primary_duration_s": (
                call.device_choose_goal_duration_s
            ),
            "allocator_processor_admission_callback_duration_s": (
                call.algorithm_epoch_reset_duration_s
            ),
            "allocator_processor_work_definition": (
                ALLOCATOR_PROCESSOR_WORK_DEFINITION
            ),
            "allocator_processor_timing_source": allocator_timing_source,
            "rp2040_device_duration_s": rp2040_duration_s,
            "virtual_compute_completion_s": call.compute_completion_time_s,
            "agx_allocator_duration_s": call.agx_allocator_duration_s,
            "device_choose_goal_us": int(
                call.device_choose_goal_duration_ns or 0
            ) // 1_000,
            "algorithm_epoch_reset_us": int(
                call.algorithm_epoch_reset_duration_ns or 0
            ) // 1_000,
            "rp2040_choose_goal_duration_s": rp2040_choose_goal_s,
            "rp2040_algorithm_epoch_reset_duration_s": rp2040_epoch_reset_s,
            "agx_choose_goal_duration_s": call.agx_choose_goal_duration_s,
            "agx_algorithm_epoch_reset_duration_s": (
                call.agx_algorithm_epoch_reset_duration_s
            ),
            "serial_roundtrip_s": call.serial_roundtrip_s,
            "host_serialization_setup_s": call.host_serialization_setup_s,
            "host_total_call_s": call.host_total_call_ns / 1_000_000_000.0,
            "timing_decomposition_schema": call.timing_decomposition_schema,
            "device_allocator_timer_scope": call.device_allocator_timer_scope,
            "serial_roundtrip_definition": call.serial_roundtrip_definition,
            "host_serialization_setup_measured": (
                call.host_serialization_setup_measured
            ),
            "psetup_transaction_s": call.psetup_transaction_s,
            "device_pre_call_setup_s": call.device_pre_call_setup_s,
            "ptime_result_transaction_s": call.ptime_result_transaction_s,
            "host_prepare_cpu_s": call.host_prepare_cpu_s,
            "active_task_count": call.active_task_count,
            "candidate_count": call.candidate_count,
            "current_x": (
                None if call.current_position is None else call.current_position[0]
            ),
            "current_y": (
                None if call.current_position is None else call.current_position[1]
            ),
            "parity_passed": call.parity_passed,
            "agx_selected_goal": call.authoritative_goal,
            "rp2040_selected_goal": device_goal,
            "agx_message_hash": call.outbound_message_sha256,
            "rp2040_message_hash": device_message_hash,
            "agx_state_hash": call.authoritative_post_state_sha256,
            "rp2040_state_hash": device_state_hash,
            "pre_state_hash": call.pre_state_sha256,
            "call_class": call.call_class,
            "trigger_reason": call.trigger_reason,
            "allocator_input_event_count": call.allocator_input_event_count,
            "recovery_invoked": call.recovery_invoked,
            "board_id": worker_board_id,
            "device_uid": call_board,
            "serial_device": call.serial_device,
            "attempt_id": call.attempt_id,
            "physical_measurement_index": call.physical_measurement_index,
            "hardware_validated": call.hardware_validated,
            "hardware_attestation": hardware_attestation,
            "hardware_attestation_sha256": call.provider_metadata.get(
                "hardware_attestation_sha256", ""
            ),
            "hardware_validation_mode": call.provider_metadata.get(
                "validation_mode", ""
            ),
            "parity_level": call.provider_metadata.get("parity_level", ""),
            "representation_hashes_differ": call.provider_metadata.get(
                "representation_hashes_differ", False
            ),
            "agx_message_projection_hash": call.provider_metadata.get(
                "agx_message_projection_sha256", ""
            ),
            "rp2040_message_projection_hash": call.provider_metadata.get(
                "device_message_projection_sha256", ""
            ),
            "agx_state_projection_hash": call.provider_metadata.get(
                "agx_state_projection_sha256", ""
            ),
            "rp2040_state_projection_hash": call.provider_metadata.get(
                "device_state_projection_sha256", ""
            ),
        })

    if any(epoch.piggybacked_pending for epoch in scheduler.epochs):
        raise RuntimeError("strict-admission architecture emitted a piggybacked epoch")
    if any(epoch.trigger_reason == "final_release_flush" for epoch in scheduler.epochs):
        raise RuntimeError("strict-admission architecture emitted a final-release flush")

    calls_by_event: dict[str, list[dict[str, Any]]] = {}
    for row in call_rows:
        if row["event_id"]:
            calls_by_event.setdefault(str(row["event_id"]), []).append(row)

    event_rows = []
    for epoch in scheduler.epochs:
        event_id = _event_id(trace_id, epoch.epoch_id)
        associated_calls = calls_by_event.get(event_id, [])
        participants = sorted({
            str(row["logical_robot_id"]) for row in associated_calls
        })
        event_completion = (
            max(float(row["virtual_compute_completion_s"]) for row in associated_calls)
            if associated_calls else None
        )
        event_rows.append({
            **dimensions,
            "event_id": event_id,
            "trigger_reason": epoch.trigger_reason,
            "trigger_class": _trigger_class(epoch),
            "event_time_s": epoch.opened_time_s,
            "pending_count": epoch.pending_depth_before,
            "oldest_pending_age_s": epoch.oldest_pending_age_s,
            "admitted_task_ids": ";".join(map(str, epoch.admitted_task_ids)),
            "admitted_task_count": epoch.admitted_count,
            # Compatibility column: piggyback admission is forbidden in the
            # strict architecture and therefore always false in new output.
            "piggybacked": False,
            "terminal_residual": epoch.terminal_residual,
            "announcement_delivery_count": len(
                epoch.announcement_delivery_time_s_by_robot
            ),
            "announcement_delivery_time_s_by_robot": dict(
                epoch.announcement_delivery_time_s_by_robot
            ),
            "participating_logical_robots": ";".join(participants),
            "associated_call_ids": ";".join(
                str(row["call_id"]) for row in associated_calls
            ),
            "virtual_event_completion_s": event_completion,
        })

    queue_rows = [
        {
            **dimensions,
            "sample_index": index,
            "sample_time_s": sample.time_s,
            "pending_depth": sample.depth,
            "oldest_pending_age_s": sample.oldest_age_s,
            "sample_event": sample.event,
        }
        for index, sample in enumerate(scheduler.queue_samples)
    ]

    movement_rows = []
    for movement in state.movement_records:
        completed = []
        if movement.completed_task_id is not None:
            completed.append(str(movement.completed_task_id))
        movement_rows.append({
            **dimensions,
            "movement_id": movement.movement_id,
            "logical_robot_id": movement.robot_id,
            "from_x": movement.source_cell[0],
            "from_y": movement.source_cell[1],
            "to_x": movement.target_cell[0],
            "to_y": movement.target_cell[1],
            "movement_start_s": movement.start_time_s,
            "movement_duration_s": movement.duration_s,
            "movement_completion_s": movement.completion_time_s,
            "robot_action_index": movement.robot_action_index,
            "movement_timing_key_sha256": movement.timing_key_sha256,
            "movement_timing_model": movement.timing_model,
            "completed_task_ids": ";".join(completed),
        })

    metrics = state.online_metrics()
    online_release_times = _positive_release_times(task_rows)
    releases_during_compute = sum(
        any(
            float(call["virtual_compute_start_s"]) < release_time
            < float(call["virtual_compute_completion_s"])
            for call in call_rows
        )
        for release_time in online_release_times
    )
    event_classes = [_trigger_class(epoch) for epoch in scheduler.epochs]
    hardware_validated = bool(
        not zero_compute
        and call_rows
        and all(call.hardware_validated for call in scheduler.allocator_calls)
    )
    allocator_work_s = sum(
        int(call.device_allocator_duration_ns or 0)
        for call in scheduler.allocator_calls
    ) / 1_000_000_000.0
    allocator_primary_work_s = sum(
        int(call.device_choose_goal_duration_ns or 0)
        for call in scheduler.allocator_calls
    ) / 1_000_000_000.0
    allocator_admission_callback_work_s = sum(
        int(call.algorithm_epoch_reset_duration_ns or 0)
        for call in scheduler.allocator_calls
    ) / 1_000_000_000.0
    if not abs(
        allocator_work_s
        - allocator_primary_work_s
        - allocator_admission_callback_work_s
    ) <= 1e-12:
        raise AssertionError("allocator processor-work decomposition is inconsistent")
    completed_task_count = sum(
        record.completed for record in state.world.target_records.values()
    )
    timing_source = _allocator_timing_source(
        zero_compute=zero_compute,
        hardware_validated=hardware_validated,
    )
    rp2040_work_s = allocator_work_s if hardware_validated else None
    rp2040_primary_work_s = (
        allocator_primary_work_s if hardware_validated else None
    )
    rp2040_admission_callback_work_s = (
        allocator_admission_callback_work_s if hardware_validated else None
    )
    all_tasks_completed = bool(metrics["all_tasks_completed"])
    algorithmic_failure_type = metrics.get("algorithmic_failure_type")
    zero_pair_id = (
        job_id[:-len("__zero")]
        if zero_compute and job_id.endswith("__zero")
        else f"{job_id}__zero"
    )
    summary = {
        "schema_version": 3,
        "trial_status": (
            "completed" if all_tasks_completed else "algorithmic_incomplete"
        ),
        "technical_status": "completed",
        "algorithmic_status": (
            "completed" if all_tasks_completed else "incomplete"
        ),
        "failure_type": None if all_tasks_completed else (
            algorithmic_failure_type or "algorithmic_incomplete"
        ),
        "completion": all_tasks_completed,
        "zero_compute_paired_id": zero_pair_id,
        **dimensions,
        **metrics,
        "completed_task_count": completed_task_count,
        "reallocation_event_count": len(event_rows),
        "arrival_driven_event_count": event_classes.count("arrival_driven"),
        "mandatory_event_count": event_classes.count("mandatory"),
        "arrival_induced_trigger_count": event_classes.count("arrival_driven"),
        "piggybacked_admission_event_count": 0,
        "piggybacked_admission_epoch_count": 0,
        "final_flush_event_count": 0,
        "final_flush_trigger_count": 0,
        "terminal_residual_event_count": event_classes.count(
            "terminal_residual"
        ),
        "terminal_residual_trigger_count": event_classes.count(
            "terminal_residual"
        ),
        "terminal_residual_admitted_task_count": sum(
            epoch.admitted_count
            for epoch in scheduler.epochs
            if epoch.terminal_residual
        ),
        "allocator_input_event_total": sum(
            call.allocator_input_event_count
            for call in scheduler.allocator_calls
        ),
        "recovery_invocation_count": sum(
            bool(call.recovery_invoked)
            for call in scheduler.allocator_calls
        ),
        "online_release_event_count": len(online_release_times),
        "release_events_during_compute_count": releases_during_compute,
        "release_events_during_compute_fraction": (
            releases_during_compute / len(online_release_times)
            if online_release_times else 0.0
        ),
        "hardware_validated": hardware_validated,
        "parity_passed": all(call.parity_passed and call.valid_for_mission for call in scheduler.allocator_calls),
        "timing_source": timing_source,
        "allocator_processor_timing_source": timing_source,
        "allocator_processor_work_definition": (
            ALLOCATOR_PROCESSOR_WORK_DEFINITION
        ),
        "allocator_processor_work_s": allocator_work_s,
        "allocator_processor_primary_work_s": allocator_primary_work_s,
        "allocator_processor_admission_callback_work_s": (
            allocator_admission_callback_work_s
        ),
        "cumulative_allocator_time_s": allocator_work_s,
        "allocator_time_aggregation": (
            "sum_of_valid_timed_allocator_transactions_processor_seconds"
        ),
        # RP2040-labelled performance fields are deliberately null for host
        # development proxies and zero-compute counterfactuals.
        "rp2040_allocator_processor_work_s": rp2040_work_s,
        "rp2040_choose_goal_processor_work_s": rp2040_primary_work_s,
        "rp2040_epoch_reset_processor_work_s": (
            rp2040_admission_callback_work_s
        ),
        "W_alloc_rp2040_s": rp2040_work_s,
        "mean_rp2040_allocator_call_time_s": (
            metrics["mean_allocator_call_time_s"]
            if hardware_validated else None
        ),
        "median_rp2040_allocator_call_time_s": (
            metrics["median_allocator_call_time_s"]
            if hardware_validated else None
        ),
        "p95_rp2040_allocator_call_time_s": (
            metrics["p95_allocator_call_time_s"]
            if hardware_validated else None
        ),
        "rp2040_processor_work_per_call_s": (
            allocator_work_s / len(call_rows)
            if hardware_validated and call_rows else None
        ),
        "rp2040_processor_work_per_reallocation_event_s": (
            allocator_work_s / len(event_rows)
            if hardware_validated and event_rows else None
        ),
        "rp2040_processor_work_per_completed_task_s": (
            allocator_work_s / completed_task_count
            if hardware_validated and completed_task_count else None
        ),
        "allocator_time_per_completed_task_s": (
            allocator_work_s / completed_task_count
            if completed_task_count else 0.0
        ),
        "allocator_processor_work_per_call_s": (
            allocator_work_s / len(call_rows) if call_rows else 0.0
        ),
        "allocator_processor_work_per_reallocation_event_s": (
            allocator_work_s / len(event_rows) if event_rows else 0.0
        ),
        "allocator_processor_work_per_completed_task_s": (
            allocator_work_s / completed_task_count
            if completed_task_count else 0.0
        ),
        "processor_capacity_fraction": (
            allocator_work_s / (len(state.robots) * state.mission_elapsed_time_s)
            if state.robots and all_tasks_completed
            and state.mission_elapsed_time_s > 0.0 else 0.0
        ),
        "processor_capacity_fraction_definition": (
            "allocator_processor_work_divided_by_robot_count_times_causal_mission_elapsed"
        ),
        "rp2040_performance_reported": hardware_validated,
        "causal_compute_duration_excludes_transport": True,
        "physical_measurement_order_serializes_virtual_time": False,
    }

    provenance = {
        "schema_version": 1,
        "created_at": _utc_now(),
        **dict(campaign_identity),
        **dimensions,
        "git": _git(repo_root),
        "python": sys.version,
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        "logical_cores": os.cpu_count(),
        "movement_timing_model": state.movement_timing_model,
        "movement_timing_seed": state.movement_timing_seed,
        "movement_timing_trace_id": state.movement_timing_trace_id,
        "allocator_processor_work_definition": (
            ALLOCATOR_PROCESSOR_WORK_DEFINITION
        ),
        "allocator_processor_timing_source": timing_source,
        "rp2040_performance_reported": hardware_validated,
        "scenario_manifest": str(Path(scenario_manifest).resolve()),
        "release_manifest": str(Path(release_manifest).resolve()),
    }

    destinations = {
        "trial_summary.json": summary,
        "task_events.csv": task_rows,
        "allocator_calls.csv": call_rows,
        "reallocation_events.csv": event_rows,
        "pending_queue_samples.csv": queue_rows,
        "movement_events.csv": movement_rows,
        "run_provenance.json": provenance,
    }
    if not call_rows or not event_rows:
        raise RuntimeError("causal mission produced an empty allocator/event table")
    for name, value in destinations.items():
        path = output / name
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path}")
        if name.endswith(".json"):
            _atomic_json(path, value)
        else:
            _atomic_csv(
                path,
                value,
                empty_fieldnames=(
                    (
                        "movement_id", "logical_robot_id", "from_x", "from_y",
                        "to_x", "to_y", "movement_start_s", "movement_duration_s",
                        "movement_completion_s", "robot_action_index",
                        "movement_timing_key_sha256", "movement_timing_model",
                        "completed_task_ids",
                    )
                    if name == "movement_events.csv" else (
                        "sample_index", "sample_time_s", "pending_depth",
                        "oldest_pending_age_s", "sample_event",
                    ) if name == "pending_queue_samples.csv" else None
                ),
            )
    return summary


__all__ = ["run_causal_manifest_job"]
