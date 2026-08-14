"""Fail-closed descriptive analysis for the native hardware-provider cohort.

This module deliberately serves only the non-development ``publication_core``
causal campaign.  It revalidates the promoted native outputs before emitting
trial-level and paired-to-Eager *descriptive* tables.  It neither consumes a
zero-compute arm nor performs inferential tests: hardware-provider findings
remain separate from the frozen causal-plus-zero publication analysis.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from study.manifests import canonical_json_bytes, sha256_file

from .model import CausalConfig, CausalJob, PUBLICATION_WORKER_COUNT, load_causal_config
from .orchestrator import (
    _build_schedule_record,
    _completion_state,
    _git_identity,
    validate_campaign_gates,
)
from .outputs import validate_causal_outputs
from .schedule import plan_paired_blocks, validate_schedule


ANALYSIS_VERSION = "hardware-provider-descriptive-v1"

# These are intentionally descriptive measures only.  The aliases preserve
# compatibility with the semantic output validator's accepted summary schemas.
METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "rp2040_work_s": (
        "rp2040_allocator_processor_work_s",
        "total_rp2040_allocator_work_s",
    ),
    "agx_work_s": (
        "agx_allocator_processor_work_s",
        "total_agx_allocator_work_s",
    ),
    "assignment_median_s": (
        "median_release_to_first_assignment_latency_s",
        "median_release_to_assignment_latency_s",
    ),
    "assignment_p95_s": (
        "p95_release_to_first_assignment_latency_s",
        "p95_release_to_assignment_latency_s",
    ),
    "completion_median_s": ("median_release_to_completion_latency_s",),
    "completion_p95_s": ("p95_release_to_completion_latency_s",),
    "mission_s": ("mission_elapsed_time_s", "causal_mission_elapsed_time_s"),
    "max_steps": ("max_robot_steps",),
    "team_steps": ("total_team_steps",),
    "events": ("reallocation_event_count", "allocation_epoch_count"),
    "arrival_events": ("arrival_driven_event_count", "arrival_induced_trigger_count"),
    "mandatory_events": ("mandatory_event_count", "mandatory_trigger_count"),
    "piggyback_events": ("piggybacked_admission_event_count",),
    "timeout_events": ("timeout_trigger_count",),
    "batch_events": ("batch_threshold_trigger_count",),
    "terminal_residual_events": (
        "terminal_residual_event_count",
        "terminal_residual_trigger_count",
    ),
    "final_flush_events": ("final_flush_event_count",),
    "calls": ("allocator_call_count",),
    "capacity_fraction": ("processor_capacity_fraction",),
}

PAIRED_METRICS = (
    "rp2040_work_s",
    "agx_work_s",
    "assignment_median_s",
    "assignment_p95_s",
    "completion_median_s",
    "completion_p95_s",
    "mission_s",
    "max_steps",
    "team_steps",
    "events",
    "calls",
)

LEGACY_PAIRED_DELTA_NAMES = {
    "assignment_median_s": "paired_change_assignment_latency_s",
    "assignment_p95_s": "paired_change_assignment_p95_latency_s",
    "completion_median_s": "paired_change_completion_latency_s",
    "completion_p95_s": "paired_change_completion_p95_latency_s",
    "mission_s": "paired_change_mission_elapsed_s",
    "max_steps": "paired_change_max_robot_steps",
    "team_steps": "paired_change_total_team_steps",
}


def _json_object(path: Path, *, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {description}: {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a JSON object: {path}")
    return value


def _number(row: Mapping[str, Any], metric: str) -> float | None:
    """Return a finite numeric metric, retaining absent/censored values as None."""

    for name in METRIC_ALIASES.get(metric, (metric,)):
        value = row.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if math.isfinite(number):
                return number
    return None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in {None, 0.0}:
        return None
    return numerator / denominator


def _csv_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for field in row:
            if field not in seen:
                fields.append(field)
                seen.add(field)
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(
        handle,
        fieldnames=fields,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue().encode("utf-8")


def _write_immutable_bundle(payloads: Mapping[Path, bytes]) -> None:
    conflicts = [
        path for path, data in payloads.items()
        if path.exists() and path.read_bytes() != data
    ]
    if conflicts:
        raise FileExistsError(
            "refusing to overwrite a different hardware descriptive bundle: "
            + ", ".join(str(path) for path in conflicts)
        )
    for path, data in payloads.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)


def _require_canonical_schedule_artifact(
    path: Path,
    expected_schedule: Mapping[str, Any],
) -> None:
    if not path.is_file():
        raise ValueError(f"missing immutable causal schedule: {path}")
    if path.read_bytes() != canonical_json_bytes(expected_schedule):
        raise ValueError("causal immutable schedule differs from the canonical plan")


def _require_publication_core(config: CausalConfig) -> None:
    if config.stage != "publication_core" or config.development_override:
        raise ValueError(
            "hardware descriptive analysis requires the non-development "
            "publication_core config"
        )
    if len(config.boards) != PUBLICATION_WORKER_COUNT:
        raise ValueError(
            "hardware descriptive analysis requires exactly "
            f"{PUBLICATION_WORKER_COUNT} board bindings"
        )
    eager = [policy for policy in config.policies if policy.mode == "eager"]
    if len(eager) != 1:
        raise ValueError("hardware descriptive analysis requires exactly one Eager policy")
    if len(config.policies) < 2:
        raise ValueError("hardware descriptive analysis requires an Eager comparator")


def _validate_execution_report(
    report: Mapping[str, Any],
    *,
    config: CausalConfig,
    source: Mapping[str, Any],
    jobs: Sequence[CausalJob],
    schedule_sha256: str,
) -> None:
    expected = {
        "report_kind": "causal_campaign_execution",
        "passed": True,
        "zero_compute": False,
        "campaign_id": config.campaign_id,
        "worker_count": len(config.boards),
        "planned_jobs": len(jobs),
        "completed_jobs": len(jobs),
        "pending_jobs": 0,
        "conflicting_jobs": 0,
        "hardware_validated": True,
        "schedule_sha256": schedule_sha256,
        "config_sha256": config.config_sha256,
        "manifest_index_sha256": config.manifest_index_sha256,
        "hardware_binding_sha256": config.hardware_binding_sha256,
        "source_identity": dict(source),
        "board_bindings": [board.to_dict() for board in config.boards],
        "core_affinities": list(config.core_affinities),
    }
    for field, expected_value in expected.items():
        if report.get(field) != expected_value:
            raise ValueError(
                f"causal execution report {field} mismatch: "
                f"{report.get(field)!r} != {expected_value!r}"
            )
    expected_states = {job.job_id: "completed" for job in jobs}
    if report.get("job_states") != expected_states:
        raise ValueError("causal execution report job states differ from the canonical plan")
    if report.get("algorithmic_incompletions_are_retained") is not True:
        raise ValueError("causal execution report does not retain algorithmic incompletions")
    for field in (
        "algorithmically_completed_jobs",
        "algorithmically_incomplete_jobs",
        "technical_attempt_failures",
    ):
        value = report.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"causal execution report {field} must be a nonnegative integer")


def _normalize_trial(
    job: CausalJob,
    summary: Mapping[str, Any],
    *,
    completion_sha256: str,
    summary_sha256: str,
) -> dict[str, Any]:
    if summary.get("zero_compute") is not False:
        raise ValueError(f"causal summary is not a causal-only result: {job.job_id}")
    if summary.get("hardware_validated") is not True:
        raise ValueError(f"causal summary is not hardware validated: {job.job_id}")
    if summary.get("parity_passed") is not True:
        raise ValueError(f"causal summary did not pass parity: {job.job_id}")
    completed = summary.get("all_tasks_completed")
    if not isinstance(completed, bool):
        raise ValueError(f"causal summary all_tasks_completed must be boolean: {job.job_id}")
    row: dict[str, Any] = {
        "job_id": job.job_id,
        "block_id": job.block_id,
        "algorithm": job.algorithm,
        "arrival_load": job.load_id,
        "trace_id": job.trace_id,
        "policy_id": job.policy.policy_id,
        "policy_mode": job.policy.mode,
        "policy_batch_size": job.policy.batch_size,
        "policy_max_pending_age_s": job.policy.max_pending_age_s,
        "policy_order_index": job.policy_order_index,
        "board_id": job.board_id,
        "worker_index": job.worker_index,
        "core_id": job.core_id,
        "runtime_seed": job.runtime_seed,
        "technical_completion_state": "completed",
        "technical_status_reported": summary.get("technical_status", "completed"),
        "all_tasks_completed": completed,
        "algorithmic_outcome": (
            "algorithmically_completed" if completed else "algorithmically_incomplete"
        ),
        "trial_status": (
            "technically_completed_algorithmically_completed"
            if completed
            else "technically_completed_algorithmically_incomplete"
        ),
        "hardware_validated": True,
        "parity_passed": True,
        "zero_compute": False,
        "completion_marker_sha256": completion_sha256,
        "trial_summary_sha256": summary_sha256,
    }
    for metric in METRIC_ALIASES:
        row[metric] = _number(summary, metric)
    row["rp2040_work_per_call_s"] = _ratio(row["rp2040_work_s"], row["calls"])
    row["rp2040_work_per_event_s"] = _ratio(row["rp2040_work_s"], row["events"])
    row["rp2040_work_per_arrival_event_s"] = _ratio(
        row["rp2040_work_s"], row["arrival_events"]
    )
    return row


def _pair_outcome(eager_completed: bool, condition_completed: bool) -> str:
    if eager_completed and condition_completed:
        return "both_algorithmically_completed"
    if not eager_completed and not condition_completed:
        return "both_algorithmically_incomplete"
    if not eager_completed:
        return "eager_algorithmically_incomplete"
    return "condition_algorithmically_incomplete"


def _paired_eager_rows(
    trials: Sequence[Mapping[str, Any]],
    *,
    expected_policy_ids: set[str],
    eager_policy_id: str,
) -> list[dict[str, Any]]:
    by_block: dict[tuple[str, str, str], dict[str, Mapping[str, Any]]] = {}
    for row in trials:
        key = (str(row["algorithm"]), str(row["arrival_load"]), str(row["trace_id"]))
        policies = by_block.setdefault(key, {})
        policy_id = str(row["policy_id"])
        if policy_id in policies:
            raise ValueError(f"duplicate causal condition in paired block: {key + (policy_id,)}")
        policies[policy_id] = row

    paired: list[dict[str, Any]] = []
    for key, policies in sorted(by_block.items()):
        if set(policies) != expected_policy_ids:
            raise ValueError(
                "paired block differs from the planned policy set: "
                f"{key}; observed={sorted(policies)}, expected={sorted(expected_policy_ids)}"
            )
        eager = policies[eager_policy_id]
        for policy_id in sorted(expected_policy_ids - {eager_policy_id}):
            condition = policies[policy_id]
            eager_completed = bool(eager["all_tasks_completed"])
            condition_completed = bool(condition["all_tasks_completed"])
            row: dict[str, Any] = {
                "algorithm": key[0],
                "arrival_load": key[1],
                "trace_id": key[2],
                "block_id": condition["block_id"],
                "eager_policy_id": eager_policy_id,
                "condition_policy_id": policy_id,
                "policy_id": policy_id,
                "eager_job_id": eager["job_id"],
                "condition_job_id": condition["job_id"],
                "eager_board_id": eager["board_id"],
                "condition_board_id": condition["board_id"],
                "eager_core_id": eager["core_id"],
                "condition_core_id": condition["core_id"],
                "eager_all_tasks_completed": eager_completed,
                "condition_all_tasks_completed": condition_completed,
                "pair_outcome": _pair_outcome(eager_completed, condition_completed),
                "paired_complete_for_success_metric": eager_completed and condition_completed,
                "hardware_validated": True,
                "parity_passed": True,
            }
            for metric in PAIRED_METRICS:
                eager_value = eager.get(metric)
                condition_value = condition.get(metric)
                row[f"eager_{metric}"] = eager_value
                row[f"condition_{metric}"] = condition_value
                delta = (
                    None
                    if eager_value is None or condition_value is None
                    else float(condition_value) - float(eager_value)
                )
                row[f"paired_delta_{metric}_condition_minus_eager"] = delta
                legacy_name = LEGACY_PAIRED_DELTA_NAMES.get(metric)
                if legacy_name is not None:
                    row[legacy_name] = delta
            eager_work = eager.get("rp2040_work_s")
            condition_work = condition.get("rp2040_work_s")
            row["paired_percent_rp2040_processor_work_saved"] = (
                None
                if eager_work in {None, 0.0} or condition_work is None
                else 100.0 * (float(eager_work) - float(condition_work)) / float(eager_work)
            )
            eager_mission = eager.get("mission_s")
            condition_mission = condition.get("mission_s")
            row["paired_percent_mission_time_change"] = (
                None
                if eager_mission in {None, 0.0} or condition_mission is None
                else 100.0
                * (float(condition_mission) - float(eager_mission))
                / float(eager_mission)
            )
            paired.append(row)
    return paired


def _denominators(rows: Sequence[Mapping[str, Any]]) -> dict[str, int | float]:
    planned = len(rows)
    technical_completed = sum(
        row.get("technical_completion_state") == "completed" for row in rows
    )
    algorithmic_completed = sum(row.get("all_tasks_completed") is True for row in rows)
    algorithmic_incomplete = planned - algorithmic_completed
    return {
        "planned_trial_count": planned,
        "technically_completed_trial_count": technical_completed,
        "technically_incomplete_trial_count": planned - technical_completed,
        "algorithmically_completed_trial_count": algorithmic_completed,
        "algorithmically_incomplete_trial_count": algorithmic_incomplete,
        "technical_completion_rate": (
            technical_completed / planned if planned else 0.0
        ),
        "algorithmic_completion_rate": (
            algorithmic_completed / planned if planned else 0.0
        ),
    }


def _condition_rows(trials: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in trials:
        groups[(
            str(row["algorithm"]),
            str(row["arrival_load"]),
            str(row["policy_id"]),
        )].append(row)
    result: list[dict[str, Any]] = []
    metrics = tuple(METRIC_ALIASES) + (
        "rp2040_work_per_call_s",
        "rp2040_work_per_event_s",
        "rp2040_work_per_arrival_event_s",
    )
    for (algorithm, arrival_load, policy_id), group in sorted(groups.items()):
        row: dict[str, Any] = {
            "algorithm": algorithm,
            "arrival_load": arrival_load,
            "policy_id": policy_id,
            "summary_scope": "condition",
            **_denominators(group),
            "metric_observations_retain_algorithmically_incomplete_trials": True,
        }
        for metric in metrics:
            values = [
                float(value)
                for item in group
                if (value := item.get(metric)) is not None
            ]
            row[f"{metric}_observed_n"] = len(values)
            row[f"{metric}_mean"] = statistics.fmean(values) if values else None
            row[f"{metric}_median"] = statistics.median(values) if values else None
        result.append(row)
    return result


def _outcome_rows(
    trials: Sequence[Mapping[str, Any]],
    paired: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in trials:
        groups[(
            str(row["algorithm"]),
            str(row["arrival_load"]),
            str(row["policy_id"]),
        )].append(row)
    pairs_by_condition: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in paired:
        pairs_by_condition[(
            str(row["algorithm"]),
            str(row["arrival_load"]),
            str(row["condition_policy_id"]),
        )].append(row)

    result: list[dict[str, Any]] = []
    for key, group in sorted(groups.items()):
        pair_rows = pairs_by_condition.get(key, [])
        outcomes = Counter(str(item["pair_outcome"]) for item in pair_rows)
        result.append({
            "summary_scope": "condition",
            "algorithm": key[0],
            "arrival_load": key[1],
            "policy_id": key[2],
            **_denominators(group),
            "paired_to_eager_trial_count": len(pair_rows),
            "paired_both_algorithmically_completed_count": outcomes[
                "both_algorithmically_completed"
            ],
            "paired_eager_algorithmically_incomplete_count": outcomes[
                "eager_algorithmically_incomplete"
            ],
            "paired_condition_algorithmically_incomplete_count": outcomes[
                "condition_algorithmically_incomplete"
            ],
            "paired_both_algorithmically_incomplete_count": outcomes[
                "both_algorithmically_incomplete"
            ],
            "hardware_result_status": (
                "complete_hardware_cohort"
                if all(item.get("all_tasks_completed") is True for item in group)
                else "retained_algorithmically_incomplete_trials"
            ),
        })
    pair_outcomes = Counter(str(item["pair_outcome"]) for item in paired)
    result.append({
        "summary_scope": "all_conditions",
        "algorithm": "",
        "arrival_load": "",
        "policy_id": "",
        **_denominators(trials),
        "paired_to_eager_trial_count": len(paired),
        "paired_both_algorithmically_completed_count": pair_outcomes[
            "both_algorithmically_completed"
        ],
        "paired_eager_algorithmically_incomplete_count": pair_outcomes[
            "eager_algorithmically_incomplete"
        ],
        "paired_condition_algorithmically_incomplete_count": pair_outcomes[
            "condition_algorithmically_incomplete"
        ],
        "paired_both_algorithmically_incomplete_count": pair_outcomes[
            "both_algorithmically_incomplete"
        ],
        "hardware_result_status": (
            "complete_hardware_cohort"
            if all(item.get("all_tasks_completed") is True for item in trials)
            else "retained_algorithmically_incomplete_trials"
        ),
    })
    return result


def _validate_denominators(
    trials: Sequence[Mapping[str, Any]],
    paired: Sequence[Mapping[str, Any]],
    *,
    planned_jobs: int,
    expected_pair_count: int,
) -> None:
    totals = _denominators(trials)
    if totals["planned_trial_count"] != planned_jobs:
        raise AssertionError("trial-level denominator differs from planned jobs")
    if (
        totals["technically_completed_trial_count"]
        + totals["technically_incomplete_trial_count"]
        != planned_jobs
    ):
        raise AssertionError("technical outcome denominators do not reconcile")
    if (
        totals["algorithmically_completed_trial_count"]
        + totals["algorithmically_incomplete_trial_count"]
        != planned_jobs
    ):
        raise AssertionError("algorithmic outcome denominators do not reconcile")
    if len(paired) != expected_pair_count:
        raise AssertionError("paired-to-Eager denominator differs from the paired plan")


def analyze(
    causal_root: Path,
    output_dir: Path,
    *,
    config: CausalConfig,
) -> dict[str, Path]:
    """Semantically revalidate and describe one causal-only hardware cohort.

    The function emits no p-values, confidence intervals, resampling, ranking,
    or model fitting.  Any missing or mismatched campaign identity is an error;
    it is never converted into an exclusion or an inferred result.
    """

    _require_publication_core(config)
    expected_causal_root = config.output_root / "causal" / "completed"
    expected_output_dir = config.output_root / "analysis"
    if causal_root.resolve() != expected_causal_root.resolve():
        raise ValueError("causal input root differs from the configured campaign output")
    if output_dir.resolve() != expected_output_dir.resolve():
        raise ValueError("analysis output directory differs from the configured campaign output")

    source = _git_identity(config.repo_root)
    validate_campaign_gates(config, source, zero_compute=False)
    blocks = plan_paired_blocks(config, zero_compute=False)
    schedule_summary = validate_schedule(config, blocks, zero_compute=False)
    jobs = [job for block in blocks for job in block.jobs]
    if not jobs or any(job.zero_compute for job in jobs):
        raise ValueError("hardware descriptive analysis requires a nonempty causal-only schedule")
    if len({job.job_id for job in jobs}) != len(jobs):
        raise ValueError("canonical causal schedule contains duplicate job IDs")
    if {job.board_id for job in jobs} != {board.board_id for board in config.boards}:
        raise ValueError("canonical causal schedule does not exercise the full board cohort")

    expected_schedule = _build_schedule_record(
        config,
        blocks,
        schedule_summary,
        source,
        selected_workers=len(config.boards),
        zero_compute=False,
    )
    schedule_path = config.output_root / "causal_schedule.json"
    _require_canonical_schedule_artifact(schedule_path, expected_schedule)

    report_path = config.output_root / "campaign_execution_report.json"
    execution = _json_object(report_path, description="causal execution report")
    _validate_execution_report(
        execution,
        config=config,
        source=source,
        jobs=jobs,
        schedule_sha256=str(schedule_summary["schedule_sha256"]),
    )

    expected_ids = {job.job_id for job in jobs}
    observed_ids = (
        {path.name for path in causal_root.iterdir() if path.is_dir()}
        if causal_root.is_dir()
        else set()
    )
    if observed_ids != expected_ids:
        raise ValueError(
            "promoted causal directory set differs from the canonical schedule; "
            f"missing={sorted(expected_ids - observed_ids)[:5]}, "
            f"extra={sorted(observed_ids - expected_ids)[:5]}"
        )

    trials: list[dict[str, Any]] = []
    completion_hashes: dict[str, str] = {}
    summary_hashes: dict[str, str] = {}
    required_output_hashes: dict[str, Mapping[str, str]] = {}
    for job in jobs:
        if _completion_state(config, job, source) != "completed":
            raise ValueError(f"promoted job failed semantic resume revalidation: {job.job_id}")
        directory = causal_root / job.job_id
        validation = validate_causal_outputs(config, job, directory)
        marker_path = directory / "completion.json"
        summary_path = directory / "trial_summary.json"
        completion_sha256 = sha256_file(marker_path)
        summary_sha256 = sha256_file(summary_path)
        summary = _json_object(summary_path, description="causal trial summary")
        trials.append(_normalize_trial(
            job,
            summary,
            completion_sha256=completion_sha256,
            summary_sha256=summary_sha256,
        ))
        completion_hashes[job.job_id] = completion_sha256
        summary_hashes[job.job_id] = summary_sha256
        output_hashes = validation.get("required_output_sha256")
        if not isinstance(output_hashes, Mapping):
            raise ValueError(f"semantic output validation lacks hashes: {job.job_id}")
        required_output_hashes[job.job_id] = {
            str(name): str(value) for name, value in output_hashes.items()
        }

    algorithmically_completed = sum(
        row["all_tasks_completed"] is True for row in trials
    )
    if execution.get("algorithmically_completed_jobs") != algorithmically_completed:
        raise ValueError("execution report algorithmically_completed_jobs mismatch")
    if execution.get("algorithmically_incomplete_jobs") != len(trials) - algorithmically_completed:
        raise ValueError("execution report algorithmically_incomplete_jobs mismatch")

    eager_policies = [policy.policy_id for policy in config.policies if policy.mode == "eager"]
    eager_policy_id = eager_policies[0]
    paired = _paired_eager_rows(
        trials,
        expected_policy_ids={policy.policy_id for policy in config.policies},
        eager_policy_id=eager_policy_id,
    )
    expected_pair_count = len(blocks) * (len(config.policies) - 1)
    _validate_denominators(
        trials,
        paired,
        planned_jobs=len(jobs),
        expected_pair_count=expected_pair_count,
    )
    condition_summaries = _condition_rows(trials)
    outcome_summaries = _outcome_rows(trials, paired)

    gate_hashes = {
        str(path.relative_to(config.repo_root)): sha256_file(path)
        for path in config.required_gate_paths
    }
    invariant_checks = {
        "nondevelopment_publication_core_config": True,
        "source_identity_revalidated": True,
        "required_gates_revalidated": True,
        "causal_only_schedule_validated": True,
        "canonical_schedule_matches": True,
        "execution_report_matches": True,
        "execution_used_full_publication_board_cohort": True,
        "exact_promoted_causal_directory_set": True,
        "all_promoted_outputs_semantically_revalidated": True,
        "all_promoted_outputs_hardware_validated": True,
        "all_promoted_outputs_parity_passed": True,
        "technical_denominators_reconcile": True,
        "algorithmic_denominators_reconcile": True,
        "paired_to_eager_coverage_complete": True,
        "zero_compute_input_not_used": True,
        "inferential_tests_not_performed": True,
    }
    invariants = {
        "schema_version": 1,
        "analysis_kind": "descriptive_hardware_provider",
        "passed": all(invariant_checks.values()),
        "checks": invariant_checks,
        "counts": {
            **_denominators(trials),
            "paired_to_eager_trial_count": len(paired),
            "planned_block_count": len(blocks),
            "board_count": len(config.boards),
        },
    }

    paths = {
        "trial_level": output_dir / "hardware_trial_level.csv",
        "paired_eager": output_dir / "hardware_paired_eager_descriptive.csv",
        "condition_summaries": output_dir / "hardware_condition_summaries.csv",
        "outcome_summaries": output_dir / "hardware_outcome_summaries.csv",
        "invariants": output_dir / "hardware_analysis_invariants.json",
        "metadata": output_dir / "hardware_analysis_metadata.json",
    }
    payloads: dict[Path, bytes] = {
        paths["trial_level"]: _csv_bytes(trials),
        paths["paired_eager"]: _csv_bytes(paired),
        paths["condition_summaries"]: _csv_bytes(condition_summaries),
        paths["outcome_summaries"]: _csv_bytes(outcome_summaries),
        paths["invariants"]: canonical_json_bytes(invariants),
    }
    output_hashes = {
        path.name: hashlib.sha256(data).hexdigest()
        for path, data in sorted(payloads.items(), key=lambda item: item[0].name)
    }
    metadata = {
        "schema_version": 1,
        "analysis_version": ANALYSIS_VERSION,
        "analysis_kind": "descriptive_hardware_provider",
        "descriptive_only": True,
        "inferential_tests_performed": False,
        "inferential_test_count": 0,
        "confidence_intervals_performed": False,
        "resampling_performed": False,
        "hardware_provider_results_must_remain_separate": True,
        "hardware_provider_results_are_not_the_full_causal_plus_zero_analysis": True,
        "zero_compute_input_used": False,
        "trial_is_the_independent_replicate": True,
        "task_rows_used_as_independent_replicates": False,
        "algorithmic_incompletions_are_retained": True,
        "metric_observations_retain_algorithmically_incomplete_trials_when_present": True,
        "denominators": {
            **_denominators(trials),
            "paired_to_eager_trial_count": len(paired),
            "planned_block_count": len(blocks),
        },
        "input_identity": {
            "validation_mode": "publication_core_causal_only_semantic_revalidation",
            "config_path": str(config.path.relative_to(config.repo_root)),
            "config_sha256": config.config_sha256,
            "manifest_index_sha256": config.manifest_index_sha256,
            "hardware_binding_sha256": config.hardware_binding_sha256,
            "source_identity": dict(source),
            "required_gate_sha256_by_path": gate_hashes,
            "schedule_sha256": schedule_summary["schedule_sha256"],
            "schedule_file_sha256": sha256_file(schedule_path),
            "execution_report_sha256": sha256_file(report_path),
            "completion_marker_sha256_by_job": completion_hashes,
            "trial_summary_sha256_by_job": summary_hashes,
            "required_output_sha256_by_job": required_output_hashes,
        },
        "analysis_output_sha256": output_hashes,
        "files": {name: path.name for name, path in paths.items() if name != "metadata"},
    }
    payloads[paths["metadata"]] = canonical_json_bytes(metadata)
    _write_immutable_bundle(payloads)
    return paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--causal-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_causal_config(args.config, args.repo_root)
    paths = analyze(
        args.causal_root.resolve(),
        args.output_dir.resolve(),
        config=config,
    )
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
