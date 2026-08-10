"""Summarize native causal rate, timeout, and variance pilots.

Selections are based on declared workload-regime/latency criteria and remain
review-required.  Nothing in this module selects the condition with the most
favorable coalescing compute effect.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from study.manifests import canonical_json_bytes, sha256_file

from .model import PUBLICATION_WORKER_COUNT, CausalConfig, load_causal_config


PRIMARY_ALGORITHMS = {"CBAA", "ACBBA", "PI", "HIPC"}
ALLOWED_FINAL_TRACE_COUNTS = frozenset({25, 50})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def load_trial_summaries(
    root: Path,
    config: CausalConfig,
) -> list[dict[str, Any]]:
    from .orchestrator import (
        _completion_state,
        _git_identity,
        validate_campaign_gates,
    )
    from .outputs import validate_causal_outputs
    from .schedule import plan_paired_blocks, validate_schedule

    expected_root = config.output_root / "causal" / "completed"
    if root.resolve() != expected_root.resolve():
        raise ValueError("calibration input root differs from configured campaign output")
    if config.development_override or config.stage not in {
        "calibrate_rates", "calibrate_timeout", "variance"
    }:
        raise ValueError("native calibration requires a non-development calibration config")
    source = _git_identity(config.repo_root)
    validate_campaign_gates(config, source, False)
    blocks = plan_paired_blocks(config, zero_compute=False)
    schedule_summary = validate_schedule(config, blocks, zero_compute=False)
    jobs = [job for block in blocks for job in block.jobs]
    rows: list[dict[str, Any]] = []
    campaign_root = config.output_root
    execution_path = campaign_root / "campaign_execution_report.json"
    execution = _load(execution_path)
    if (
        execution.get("report_kind") != "causal_campaign_execution"
        or execution.get("passed") is not True
        or execution.get("hardware_validated") is not True
        or execution.get("zero_compute") is not False
    ):
        raise ValueError("calibration campaign execution report is not a passing native causal run")
    expected_report = {
        "planned_jobs": len(jobs),
        "completed_jobs": len(jobs),
        "pending_jobs": 0,
        "conflicting_jobs": 0,
        "schedule_sha256": schedule_summary["schedule_sha256"],
        "config_sha256": config.config_sha256,
        "manifest_index_sha256": config.manifest_index_sha256,
        "hardware_binding_sha256": config.hardware_binding_sha256,
        "source_identity": source,
    }
    for field, expected in expected_report.items():
        if execution.get(field) != expected:
            raise ValueError(f"calibration execution report {field} mismatch")
    expected_ids = {job.job_id for job in jobs}
    observed_ids = {
        path.name for path in root.iterdir() if path.is_dir()
    } if root.is_dir() else set()
    if observed_ids != expected_ids:
        raise ValueError("calibration promoted directory set differs from planned schedule")
    for job in jobs:
        if _completion_state(config, job, source) != "completed":
            raise ValueError(f"calibration job failed resume revalidation: {job.job_id}")
        directory = root / job.job_id
        validation = validate_causal_outputs(config, job, directory)
        path = directory / "trial_summary.json"
        row = _load(path)
        provenance_path = path.parent / "run_provenance.json"
        provenance = _load(provenance_path)
        for field in (
            "config_sha256", "manifest_index_sha256", "hardware_binding_sha256",
            "board_id", "worker_index", "core_id", "git_head",
            "source_tree_sha256", "expected_device_uid",
            "expected_device_build_id", "expected_device_firmware_sha256",
            "expected_device_module_set_sha256",
        ):
            if field in row and field in provenance and row[field] != provenance[field]:
                raise ValueError(f"summary/provenance {field} mismatch: {path}")
            if field not in row and field in provenance:
                row[field] = provenance[field]
        row["_path"] = str(path)
        row["_provenance_path"] = str(provenance_path)
        row["campaign_execution_report_sha256"] = sha256_file(execution_path)
        row["schedule_sha256"] = execution.get("schedule_sha256")
        row["completion_marker_sha256"] = sha256_file(
            directory / "completion.json"
        )
        row["trial_summary_sha256"] = sha256_file(path)
        row["required_output_sha256"] = validation["required_output_sha256"]
        rows.append(row)
    if not rows:
        raise ValueError(f"no trial_summary.json files found under {root}")
    if execution.get("completed_jobs") != len(rows) or execution.get("planned_jobs") != len(rows):
        raise ValueError("calibration completed-summary count differs from execution plan")
    return rows


ALIASES: dict[str, tuple[str, ...]] = {
    "rp2040_work": (
        "rp2040_allocator_processor_work_s", "total_rp2040_allocator_work_s",
        "cumulative_device_allocator_time_s",
    ),
    "agx_work": (
        "agx_allocator_processor_work_s", "total_agx_allocator_work_s",
        "cumulative_agx_allocator_time_s",
    ),
    "assign_median": (
        "median_release_to_first_assignment_latency_s",
        "median_release_to_assignment_latency_s",
    ),
    "completion_median": ("median_release_to_completion_latency_s",),
    "completion_p95": ("p95_release_to_completion_latency_s",),
    "mission": ("mission_elapsed_time_s", "causal_mission_elapsed_time_s"),
    "epochs": ("allocation_epoch_count", "reallocation_event_count"),
    "arrival_epochs": ("arrival_induced_trigger_count", "arrival_driven_event_count"),
    "mandatory_epochs": ("mandatory_trigger_count", "mandatory_event_count"),
    "max_pending": ("max_pending_queue_depth", "max_pending_depth"),
    "mean_pending": ("mean_pending_queue_depth", "mean_pending_depth"),
    "calls": ("allocator_call_count",),
    "batch_epochs": ("batch_threshold_trigger_count",),
    "overlap_fraction": ("release_events_during_compute_fraction",),
}


def _number(row: Mapping[str, Any], key: str, default: float | None = None) -> float | None:
    for name in ALIASES.get(key, (key,)):
        value = row.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = float(value)
            if math.isfinite(value):
                return value
    return default


def _mean(rows: Iterable[Mapping[str, Any]], key: str) -> float | None:
    values = [value for row in rows if (value := _number(row, key)) is not None]
    return statistics.fmean(values) if values else None


def _median(rows: Iterable[Mapping[str, Any]], key: str) -> float | None:
    values = [value for row in rows if (value := _number(row, key)) is not None]
    return statistics.median(values) if values else None


def _identity(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    try:
        return (
            str(row["algorithm"]),
            str(row.get("arrival_load", row.get("load_id"))),
            str(row["trace_id"]),
            str(row["policy_id"]),
        )
    except KeyError as error:
        raise ValueError(f"trial summary lacks dimension {error}") from error


def _validate_native_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        key = _identity(row)
        if key in seen:
            raise ValueError(f"duplicate trial summary dimensions: {key}")
        seen.add(key)
        if row.get("hardware_validated") is not True:
            raise ValueError(f"trial is not real-hardware validated: {row['_path']}")
        if row.get("parity_passed") is not True:
            raise ValueError(f"trial lacks fail-closed parity pass: {row['_path']}")
        if _number(row, "rp2040_work") is None:
            raise ValueError(f"trial lacks RP2040 processor work: {row['_path']}")
        if row.get("all_tasks_completed") is True and _number(row, "mission") is None:
            raise ValueError(f"trial lacks causal mission elapsed time: {row['_path']}")
    common_fields = (
        "config_sha256", "manifest_index_sha256", "git_head", "source_tree_sha256",
        "campaign_execution_report_sha256", "schedule_sha256",
    )
    common: dict[str, Any] = {}
    for field in common_fields:
        values = {row.get(field) for row in rows}
        if len(values) != 1 or None in values or "" in values:
            raise ValueError(f"native calibration rows do not share one {field}")
        common[field] = next(iter(values))
    board_ids = {str(row.get("board_id", "")) for row in rows}
    if "" in board_ids or len(board_ids) != PUBLICATION_WORKER_COUNT:
        raise ValueError(
            "native calibration must use exactly "
            f"{PUBLICATION_WORKER_COUNT} recorded boards"
        )
    device_rows: dict[str, dict[str, str]] = {}
    for board_id in sorted(board_ids):
        group = [row for row in rows if str(row.get("board_id")) == board_id]
        identity: dict[str, str] = {}
        for field in (
            "expected_device_uid", "expected_device_build_id",
            "expected_device_firmware_sha256", "expected_device_module_set_sha256",
        ):
            values = {str(row.get(field, "")) for row in group}
            if len(values) != 1 or "" in values:
                raise ValueError(f"board {board_id} has inconsistent {field}")
            identity[field] = next(iter(values))
        device_rows[board_id] = identity
    if (
        len({value["expected_device_uid"] for value in device_rows.values()})
        != PUBLICATION_WORKER_COUNT
    ):
        raise ValueError("native calibration board UIDs are not unique")
    common["boards"] = device_rows
    common["hardware_binding_sha256"] = next(iter({
        row.get("hardware_binding_sha256") for row in rows
    })) if len({row.get("hardware_binding_sha256") for row in rows}) == 1 else None
    if common["hardware_binding_sha256"] in {None, ""}:
        raise ValueError("native calibration lacks one hardware binding hash")
    for field in (
        "completion_marker_sha256", "trial_summary_sha256", "required_output_sha256"
    ):
        if any(field not in row or not row[field] for row in rows):
            raise ValueError(f"native calibration rows lack sealed {field}")
    common["completion_marker_sha256_by_condition"] = {
        "|".join(_identity(row)): row["completion_marker_sha256"] for row in rows
    }
    common["trial_summary_sha256_by_condition"] = {
        "|".join(_identity(row)): row["trial_summary_sha256"] for row in rows
    }
    common["required_output_sha256_by_condition"] = {
        "|".join(_identity(row)): row["required_output_sha256"] for row in rows
    }
    return common


def _validate_crossed_matrix(
    rows: list[dict[str, Any]],
    *,
    loads: set[str],
    traces: int,
    policy_ids: set[str],
) -> None:
    algorithms = {str(row["algorithm"]) for row in rows}
    observed_loads = {
        str(row.get("arrival_load", row.get("load_id"))) for row in rows
    }
    observed_traces = {str(row["trace_id"]) for row in rows}
    observed_policies = {str(row["policy_id"]) for row in rows}
    if algorithms != PRIMARY_ALGORITHMS:
        raise ValueError(f"calibration requires exactly the four primary algorithms: {sorted(algorithms)}")
    if observed_loads != loads:
        raise ValueError(f"calibration load set mismatch: {sorted(observed_loads)} != {sorted(loads)}")
    if len(observed_traces) != traces:
        raise ValueError(f"calibration requires exactly {traces} common paired traces")
    if observed_policies != policy_ids:
        raise ValueError("calibration policy set differs from its declared matrix")
    expected = {
        (algorithm, load, trace, policy)
        for algorithm in PRIMARY_ALGORITHMS
        for load in loads
        for trace in observed_traces
        for policy in policy_ids
    }
    observed = {_identity(row) for row in rows}
    if observed != expected:
        missing = sorted(expected - observed)[:5]
        extra = sorted(observed - expected)[:5]
        raise ValueError(
            f"calibration is not a complete crossed paired matrix; missing={missing}, extra={extra}"
        )


def _summarize_groups(
    rows: list[dict[str, Any]],
    grouping: Callable[[dict[str, Any]], tuple[str, ...]],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[grouping(row)].append(row)
    output: list[dict[str, Any]] = []
    for key, group in sorted(grouped.items()):
        completed = [row for row in group if row.get("all_tasks_completed") is True]
        output.append({
            "group": list(key),
            "trial_count": len(group),
            "completion_count": len(completed),
            "completion_rate": len(completed) / len(group),
            "continuous_metric_trial_count": len(completed),
            "mean_rp2040_processor_work_s": _mean(completed, "rp2040_work"),
            "mean_agx_processor_work_s": _mean(completed, "agx_work"),
            "median_trial_assignment_latency_s": _median(completed, "assign_median"),
            "median_trial_completion_latency_s": _median(completed, "completion_median"),
            "median_trial_p95_completion_latency_s": _median(completed, "completion_p95"),
            "median_mission_elapsed_time_s": _median(completed, "mission"),
            "median_epoch_count": _median(completed, "epochs"),
            "median_arrival_event_count": _median(completed, "arrival_epochs"),
            "median_mandatory_event_count": _median(completed, "mandatory_epochs"),
            "median_allocator_call_count": _median(completed, "calls"),
            "median_mean_pending_depth": _median(completed, "mean_pending"),
            "median_max_pending_depth": _median(completed, "max_pending"),
        })
    return output


def summarize_rate_calibration(
    rows: list[dict[str, Any]],
    rates: Mapping[str, float],
    *,
    reviewed_selection: Mapping[str, str] | None = None,
    reviewed_selection_justification: str | None = None,
) -> dict[str, Any]:
    input_identity = _validate_native_rows(rows)
    policies = {str(row["policy_id"]) for row in rows}
    eager = {value for value in policies if "eager" in value.lower()}
    count_b4 = {
        value for value in policies
        if "b4" in value.lower() and "bounded" not in value.lower()
    }
    if len(policies) != 2 or len(eager) != 1 or len(count_b4) != 1:
        raise ValueError("rate calibration requires exactly Eager and unbounded count-B4")
    _validate_crossed_matrix(
        rows, loads=set(rates), traces=5, policy_ids=policies
    )
    load_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        load_rows[str(row.get("arrival_load", row.get("load_id")))].append(row)
    missing = sorted(set(rates) - set(load_rows))
    if missing:
        raise ValueError(f"rate calibration missing loads: {', '.join(missing)}")
    regime_rows: list[dict[str, Any]] = []
    eligible: list[tuple[str, float, dict[str, Any]]] = []
    for load_id, rate in sorted(rates.items(), key=lambda item: item[1]):
        group = load_rows[load_id]
        completed_group = [row for row in group if row.get("all_tasks_completed") is True]
        b4_group = [
            row for row in completed_group if str(row["policy_id"]) in count_b4
        ]
        completion_rate = sum(row.get("all_tasks_completed") is True for row in group) / len(group)
        b4_mean_pending = _median(b4_group, "mean_pending")
        b4_max_pending = _median(b4_group, "max_pending")
        b4_batch_events = _median(b4_group, "batch_epochs")
        overlap_fraction = _median(completed_group, "overlap_fraction")
        pressure_index = sum(
            value or 0.0
            for value in (
                b4_mean_pending,
                None if b4_batch_events is None else b4_batch_events / 42.0,
                overlap_fraction,
            )
        )
        summary = {
            "load_id": load_id,
            "rate_per_s": float(rate),
            "trial_count": len(group),
            "completion_rate": completion_rate,
            "median_max_pending_depth": _median(group, "max_pending"),
            "median_mean_pending_depth": _median(group, "mean_pending"),
            "continuous_metric_trial_count": len(completed_group),
            "median_assignment_latency_s": _median(completed_group, "assign_median"),
            "median_completion_latency_s": _median(completed_group, "completion_median"),
            "median_mission_elapsed_time_s": _median(completed_group, "mission"),
            "median_arrival_events": _median(completed_group, "arrival_epochs"),
            "median_rp2040_processor_work_s": _median(completed_group, "rp2040_work"),
            "median_b4_mean_pending_depth": b4_mean_pending,
            "median_b4_max_pending_depth": b4_max_pending,
            "median_b4_batch_threshold_events": b4_batch_events,
            "median_fraction_online_releases_during_compute": _median(
                completed_group, "overlap_fraction"
            ),
            "predeclared_relative_arrival_pressure_index": pressure_index,
            "healthy": completion_rate >= 0.95,
        }
        regime_rows.append(summary)
        if summary["healthy"]:
            eligible.append((load_id, float(rate), summary))
    if len(eligible) < 3:
        proposal = None
        rationale = "Fewer than three rates met the predeclared >=95% completion health criterion."
    else:
        # Use rate ordering only after confirming that measured queue/overlap
        # pressure separates the endpoints.  Processor work is never part of
        # this calibration index.  The operator must still inspect every
        # per-algorithm row and explicitly review the common selection.
        low = eligible[0]
        high = eligible[-1]
        low_pressure = float(low[2]["predeclared_relative_arrival_pressure_index"])
        high_pressure = float(high[2]["predeclared_relative_arrival_pressure_index"])
        if high_pressure <= low_pressure or not eligible[1:-1]:
            proposal = None
            rationale = (
                "Healthy rates did not show increasing queue/compute-overlap pressure; "
                "do not label low/medium/high without redesigning the rate sweep."
            )
        else:
            target_pressure = (low_pressure + high_pressure) / 2.0
            medium = min(
                eligible[1:-1],
                key=lambda item: abs(
                    float(item[2]["predeclared_relative_arrival_pressure_index"])
                    - target_pressure
                ),
            )
            proposal = {
                "low": {"load_id": low[0], "rate_per_s": low[1]},
                "medium": {"load_id": medium[0], "rate_per_s": medium[1]},
                "high": {"load_id": high[0], "rate_per_s": high[1]},
            }
            rationale = (
                "Proposal uses the lowest/highest healthy rates only when measured pressure "
                "increases, and chooses the intermediate rate nearest the pressure midpoint. "
                "The index uses B4 pending depth, batch events per 42 online tasks, and the "
                "fraction of releases during compute; processor-work outcomes are excluded."
            )
    reviewed: dict[str, dict[str, Any]] | None = None
    review_justification = ""
    if reviewed_selection is not None:
        if set(reviewed_selection) != {"low", "medium", "high"}:
            raise ValueError("reviewed selection must define low, medium, and high")
        reviewed = {}
        for name, load_id in reviewed_selection.items():
            if load_id not in rates:
                raise ValueError(f"reviewed rate load ID not present: {load_id}")
            row = next(item for item in regime_rows if item["load_id"] == load_id)
            if not row["healthy"]:
                raise ValueError(f"reviewed rate is not healthy under declared criterion: {load_id}")
            reviewed[name] = {"load_id": load_id, "rate_per_s": float(rates[load_id])}
        if not (reviewed["low"]["rate_per_s"] < reviewed["medium"]["rate_per_s"] < reviewed["high"]["rate_per_s"]):
            raise ValueError("reviewed low/medium/high rates must be strictly ordered")
        reviewed_pressure = [
            float(next(
                item for item in regime_rows
                if item["load_id"] == reviewed_selection[name]
            )["predeclared_relative_arrival_pressure_index"])
            for name in ("low", "medium", "high")
        ]
        if not (reviewed_pressure[0] < reviewed_pressure[1] < reviewed_pressure[2]):
            raise ValueError(
                "reviewed low/medium/high rates do not have strictly increasing measured pressure"
            )
        review_justification = (
            reviewed_selection_justification.strip()
            if isinstance(reviewed_selection_justification, str)
            else ""
        )
        proposed_loads = (
            None
            if proposal is None
            else {name: proposal[name]["load_id"] for name in ("low", "medium", "high")}
        )
        if dict(reviewed_selection) != proposed_loads and not review_justification:
            raise ValueError(
                "a reviewed rate selection that differs from (or lacks) the "
                "proposal requires a nonempty scientific justification"
            )
    expected_minimum = len(rates) * 4 * 2 * 5
    return {
        "schema_version": 1,
        "report_kind": "causal_rate_calibration",
        "generated_at": _utc_now(),
        "passed": len(rows) == expected_minimum and all(row["hardware_validated"] is True for row in rows),
        "hardware_validated": True,
        "input_identity": input_identity,
        "expected_minimum_trials": expected_minimum,
        "observed_trials": len(rows),
        "regime_definition": {
            "healthy_completion_rate_minimum": 0.95,
            "selection_uses_processor_work": False,
            "pressure_index": (
                "median B4 mean pending depth + median B4 batch-threshold events/42 "
                "+ median fraction of online releases strictly inside compute intervals"
            ),
            "low": "lowest-rate healthy endpoint with lower observed pressure",
            "medium": "healthy intermediate rate nearest the measured pressure midpoint",
            "high": "highest-rate healthy endpoint with greater observed pressure",
            "labels_are_relative_to_this_candidate_sweep": True,
            "operator_must_review_per_algorithm_diagnostics": True,
        },
        "by_load": regime_rows,
        "by_algorithm_load_policy": _summarize_groups(
            rows,
            lambda row: (
                str(row["algorithm"]), str(row.get("arrival_load", row.get("load_id"))), str(row["policy_id"])
            ),
        ),
        "selection_proposal": proposal,
        "proposal_rationale": rationale,
        "reviewed_selection": reviewed,
        "reviewed_selection_justification": review_justification or None,
        "review_required_before_freeze": reviewed is None,
    }


def summarize_timeout_calibration(
    rows: list[dict[str, Any]],
    *,
    reviewed_timeout_s: float | None = None,
    reviewed_timeout_justification: str | None = None,
) -> dict[str, Any]:
    input_identity = _validate_native_rows(rows)
    candidates = sorted({
        float(row["policy_max_pending_age_s"])
        for row in rows
        if row.get("policy_max_pending_age_s") is not None
    })
    if not candidates:
        raise ValueError("timeout calibration contains no bounded-W candidate")
    expected = {2.0, 5.0, 10.0, 20.0}
    if not expected.issubset(candidates):
        raise ValueError("timeout calibration must include W=2,5,10,20 seconds")
    policies = {str(row["policy_id"]) for row in rows}
    eager = {value for value in policies if "eager" in value.lower()}
    if len(eager) != 1 or len(policies) != 5 or set(candidates) != expected:
        raise ValueError("timeout calibration requires Eager plus exactly four bounded W candidates")
    timeout_by_policy: dict[str, set[float | None]] = defaultdict(set)
    for row in rows:
        raw_timeout = row.get("policy_max_pending_age_s")
        timeout_by_policy[str(row["policy_id"])].add(
            None if raw_timeout is None else float(raw_timeout)
        )
    if timeout_by_policy[next(iter(eager))] != {None}:
        raise ValueError("Eager rows must not carry a timeout")
    bounded_values = []
    for policy_id in policies - eager:
        if len(timeout_by_policy[policy_id]) != 1 or None in timeout_by_policy[policy_id]:
            raise ValueError(f"bounded policy has inconsistent timeout: {policy_id}")
        bounded_values.extend(timeout_by_policy[policy_id])
    if set(bounded_values) != expected or len(bounded_values) != 4:
        raise ValueError("each timeout candidate must map to exactly one policy")
    loads = {str(row.get("arrival_load", row.get("load_id"))) for row in rows}
    if len(loads) != 3:
        raise ValueError("timeout calibration requires exactly three selected arrival loads")
    _validate_crossed_matrix(rows, loads=loads, traces=5, policy_ids=policies)
    summaries = _summarize_groups(
        rows,
        lambda row: (
            str(row["algorithm"]), str(row.get("arrival_load", row.get("load_id"))), str(row["policy_id"])
        ),
    )
    # Predeclared common-W rule: first require healthy execution and retain a
    # nonnegative median processor-work saving versus Eager over medium/high
    # paired trials; then choose the candidate with the smallest low-load
    # trial-p95 completion latency.  Work is a feasibility constraint, never
    # an objective to maximize.
    by_w: dict[float, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("policy_max_pending_age_s") is not None:
            by_w[float(row["policy_max_pending_age_s"])].append(row)
    eager_by_block = {
        (
            str(row["algorithm"]),
            str(row.get("arrival_load", row.get("load_id"))),
            str(row["trace_id"]),
        ): row
        for row in rows
        if str(row["policy_id"]) in eager
    }
    candidate_summaries: list[dict[str, Any]] = []
    scored: list[tuple[float, float]] = []
    for timeout, group in sorted(by_w.items()):
        paired_savings: list[float] = []
        medium_high_savings: list[float] = []
        complete_pair_count = 0
        for row in group:
            block = (
                str(row["algorithm"]),
                str(row.get("arrival_load", row.get("load_id"))),
                str(row["trace_id"]),
            )
            eager_row = eager_by_block[block]
            if not (
                row.get("all_tasks_completed") is True
                and eager_row.get("all_tasks_completed") is True
            ):
                continue
            complete_pair_count += 1
            eager_work = _number(eager_row, "rp2040_work")
            bounded_work = _number(row, "rp2040_work")
            if eager_work not in {None, 0.0} and bounded_work is not None:
                saving = 100.0 * (eager_work - bounded_work) / eager_work
                paired_savings.append(saving)
                if block[1] in {"medium", "high"}:
                    medium_high_savings.append(saving)
        low_group = [
            row for row in group
            if str(row.get("arrival_load", row.get("load_id"))) == "low"
            and row.get("all_tasks_completed") is True
        ]
        completion_rate = sum(
            row.get("all_tasks_completed") is True for row in group
        ) / len(group)
        low_p95 = _median(low_group, "completion_p95")
        medium_high_median_saving = (
            statistics.median(medium_high_savings)
            if medium_high_savings else None
        )
        eligible_candidate = bool(
            completion_rate >= 0.95
            and complete_pair_count / len(group) >= 0.95
            and medium_high_median_saving is not None
            and medium_high_median_saving >= 0.0
            and low_p95 is not None
        )
        candidate_summaries.append({
            "timeout_s": timeout,
            "trial_count": len(group),
            "completion_rate": completion_rate,
            "complete_eager_pair_count": complete_pair_count,
            "complete_eager_pair_rate": complete_pair_count / len(group),
            "median_low_load_trial_p95_completion_latency_s": low_p95,
            "median_medium_high_percent_processor_work_saved_vs_eager": medium_high_median_saving,
            "median_all_load_percent_processor_work_saved_vs_eager": (
                statistics.median(paired_savings) if paired_savings else None
            ),
            "eligible_under_predeclared_rule": eligible_candidate,
        })
        if eligible_candidate:
            assert low_p95 is not None
            scored.append((low_p95, timeout))
    proposal = None if not scored else min(scored)[1]
    review_justification = ""
    if reviewed_timeout_s is not None:
        reviewed_timeout_s = float(reviewed_timeout_s)
        if reviewed_timeout_s not in candidates:
            raise ValueError("reviewed timeout was not one of the calibrated candidates")
        reviewed_row = next(
            row for row in candidate_summaries
            if row["timeout_s"] == reviewed_timeout_s
        )
        if not reviewed_row["eligible_under_predeclared_rule"]:
            raise ValueError("reviewed timeout does not satisfy the predeclared health/work constraint")
        review_justification = (
            reviewed_timeout_justification.strip()
            if isinstance(reviewed_timeout_justification, str)
            else ""
        )
        if reviewed_timeout_s != proposal and not review_justification:
            raise ValueError(
                "a reviewed timeout that differs from (or lacks) the proposal "
                "requires a nonempty scientific justification"
            )
    expected_minimum = 3 * 4 * 5 * 5  # 3 loads, 4 algorithms, Eager+4 W, 5 traces
    return {
        "schema_version": 1,
        "report_kind": "causal_timeout_calibration",
        "generated_at": _utc_now(),
        "passed": len(rows) == expected_minimum,
        "hardware_validated": True,
        "input_identity": input_identity,
        "expected_minimum_trials": expected_minimum,
        "observed_trials": len(rows),
        "candidate_timeout_s": candidates,
        "by_algorithm_load_policy": summaries,
        "by_timeout_candidate": candidate_summaries,
        "selection_proposal_s": proposal,
        "proposal_rule": (
            "Among candidates with >=95% completion and nonnegative median RP2040-work saving "
            "versus Eager over medium/high paired trials, choose the smallest median low-load "
            "trial-p95 completion latency. Work is a feasibility constraint, not maximized."
        ),
        "reviewed_timeout_s": reviewed_timeout_s,
        "reviewed_timeout_justification": review_justification or None,
        "review_required_before_freeze": reviewed_timeout_s is None,
    }


def _paired(rows: list[dict[str, Any]], policy_match: str = "b4") -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        key = (
            str(row["algorithm"]), str(row.get("arrival_load", row.get("load_id"))), str(row["trace_id"])
        )
        groups[key][str(row["policy_id"]).lower()] = row
    output: list[dict[str, Any]] = []
    for key, policies in sorted(groups.items()):
        eager = next((row for name, row in policies.items() if "eager" in name), None)
        condition = next((row for name, row in policies.items() if policy_match in name and "bounded" not in name), None)
        if eager is None or condition is None:
            continue
        if not (
            eager.get("all_tasks_completed") is True
            and condition.get("all_tasks_completed") is True
        ):
            continue
        eager_work = _number(eager, "rp2040_work")
        condition_work = _number(condition, "rp2040_work")
        if eager_work is None or condition_work is None:
            continue
        output.append({
            "algorithm": key[0],
            "load_id": key[1],
            "trace_id": key[2],
            "delta_rp2040_work_s": condition_work - eager_work,
            "percent_rp2040_work_saved": (
                100.0 * (eager_work - condition_work) / eager_work if eager_work != 0.0 else None
            ),
            "delta_assignment_latency_s": (
                None
                if _number(condition, "assign_median") is None
                or _number(eager, "assign_median") is None
                else _number(condition, "assign_median")
                - _number(eager, "assign_median")
            ),
            "delta_completion_latency_s": (
                None
                if _number(condition, "completion_median") is None
                or _number(eager, "completion_median") is None
                else _number(condition, "completion_median")
                - _number(eager, "completion_median")
            ),
            "delta_p95_completion_latency_s": (
                None
                if _number(condition, "completion_p95") is None
                or _number(eager, "completion_p95") is None
                else _number(condition, "completion_p95")
                - _number(eager, "completion_p95")
            ),
            "delta_mission_elapsed_s": (
                None
                if _number(condition, "mission") is None
                or _number(eager, "mission") is None
                else _number(condition, "mission") - _number(eager, "mission")
            ),
            "delta_event_count": (
                None
                if _number(condition, "epochs") is None
                or _number(eager, "epochs") is None
                else _number(condition, "epochs") - _number(eager, "epochs")
            ),
        })
    return output


def summarize_variance_pilot(
    rows: list[dict[str, Any]],
    *,
    reviewed_trace_count: int | None = None,
    reviewed_trace_count_justification: str | None = None,
) -> dict[str, Any]:
    input_identity = _validate_native_rows(rows)
    policies = {str(row["policy_id"]) for row in rows}
    eager = {value for value in policies if "eager" in value.lower()}
    count_b4 = {
        value for value in policies
        if "b4" in value.lower() and "bounded" not in value.lower()
    }
    loads = {str(row.get("arrival_load", row.get("load_id"))) for row in rows}
    if len(eager) != 1 or len(count_b4) != 1 or len(policies) != 2 or len(loads) != 3:
        raise ValueError("variance pilot requires exactly Eager/B4 across three loads")
    _validate_crossed_matrix(rows, loads=loads, traces=10, policy_ids=policies)
    pairs = _paired(rows)
    if not pairs:
        raise ValueError("variance pilot has no Eager/B4 pairs")
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in pairs:
        groups[(row["algorithm"], row["load_id"])].append(row)
    metrics = (
        "delta_rp2040_work_s", "percent_rp2040_work_saved", "delta_assignment_latency_s",
        "delta_completion_latency_s", "delta_p95_completion_latency_s", "delta_mission_elapsed_s",
        "delta_event_count",
    )
    group_rows: list[dict[str, Any]] = []
    adequate_cells = 0
    complete_pair_coverage = len(pairs) / (3 * 4 * 10)
    for (algorithm, load_id), group in sorted(groups.items()):
        summary: dict[str, Any] = {"algorithm": algorithm, "load_id": load_id, "paired_n": len(group)}
        cell_adequate = True
        for metric in metrics:
            values = [float(row[metric]) for row in group if row[metric] is not None]
            mean = statistics.fmean(values) if values else None
            sd = statistics.stdev(values) if len(values) >= 2 else None
            # Exploratory projection: n=10 pilot SD carried forward to n=25
            # with a two-sided t critical value for df=24. The report labels
            # this approximation and still requires explicit review.
            projected_halfwidth = None if sd is None else 2.064 * sd / math.sqrt(25)
            summary[f"{metric}_mean"] = mean
            summary[f"{metric}_sample_sd"] = sd
            summary[f"{metric}_projected_n25_95pct_halfwidth"] = projected_halfwidth
            if metric in {"delta_rp2040_work_s", "delta_completion_latency_s"}:
                if (
                    mean is None
                    or projected_halfwidth is None
                    or projected_halfwidth >= abs(mean)
                ):
                    cell_adequate = False
        summary["n25_directional_precision_adequate"] = cell_adequate
        adequate_cells += int(cell_adequate)
        group_rows.append(summary)
    adequacy_rate = adequate_cells / len(group_rows)
    proposal_n = (
        25 if complete_pair_coverage == 1.0 and adequacy_rate >= 0.75
        else (50 if complete_pair_coverage == 1.0 else None)
    )
    if reviewed_trace_count is not None:
        if (
            isinstance(reviewed_trace_count, bool)
            or not isinstance(reviewed_trace_count, int)
            or reviewed_trace_count not in ALLOWED_FINAL_TRACE_COUNTS
        ):
            raise ValueError(
                "reviewed trace count must be one of the predeclared choices: 25 or 50"
            )
        if complete_pair_coverage != 1.0:
            raise ValueError(
                "cannot freeze trace count from censored/incomplete variance pairs"
            )
        justification = (
            reviewed_trace_count_justification.strip()
            if isinstance(reviewed_trace_count_justification, str)
            else ""
        )
        if reviewed_trace_count != proposal_n and not justification:
            raise ValueError(
                "a reviewed trace count that differs from the variance proposal "
                "requires a nonempty scientific justification"
            )
    else:
        justification = ""
    expected_minimum = 3 * 4 * 2 * 10
    return {
        "schema_version": 1,
        "report_kind": "causal_variance_pilot",
        "generated_at": _utc_now(),
        "passed": len(rows) == expected_minimum and complete_pair_coverage == 1.0,
        "hardware_validated": True,
        "input_identity": input_identity,
        "expected_minimum_trials": expected_minimum,
        "observed_trials": len(rows),
        "paired_trial_is_the_replicate": True,
        "complete_pair_count": len(pairs),
        "expected_complete_pair_count": 3 * 4 * 10,
        "complete_pair_coverage": complete_pair_coverage,
        "group_summaries": group_rows,
        "n25_directional_precision_adequacy_rate": adequacy_rate,
        "trace_count_proposal": proposal_n,
        "proposal_rule": (
            "Retain n=25 when at least 75% of algorithm/load cells have projected n=25 "
            "95% half-width no larger than the observed absolute mean for both primary "
            "work and completion-latency effects; otherwise recommend 50."
        ),
        "precision_projection_is_exploratory": True,
        "precision_projection_method": (
            "pilot sample SD (n=10) times t_0.975,df24=2.064 divided by sqrt(25); "
            "zero effects and missing SD are directionally inadequate"
        ),
        "reviewed_trace_count": reviewed_trace_count,
        "reviewed_trace_count_justification": justification or None,
        "allowed_final_trace_counts": sorted(ALLOWED_FINAL_TRACE_COUNTS),
        "review_required_before_freeze": reviewed_trace_count is None,
    }


def write_report(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    current = canonical_json_bytes(report)
    if output.exists() and output.read_bytes() != current:
        prior = output.read_bytes()
        history = output.parent / "history"
        history.mkdir(parents=True, exist_ok=True)
        archived = history / f"{output.stem}_{hashlib.sha256(prior).hexdigest()[:12]}{output.suffix}"
        if not archived.exists():
            shutil.copy2(output, archived)
    output.write_bytes(current)


def _parse_reviewed_rates(values: list[str] | None) -> dict[str, str] | None:
    if not values:
        return None
    parsed: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise ValueError("--review-rate values must be low=LOAD, medium=LOAD, or high=LOAD")
        name, load = item.split("=", 1)
        parsed[name] = load
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    rate = sub.add_parser("rates")
    rate.add_argument("--input-root", type=Path, required=True)
    rate.add_argument("--config", type=Path, required=True)
    rate.add_argument("--review-rate", action="append")
    rate.add_argument("--review-rate-justification")
    rate.add_argument("--output", type=Path, required=True)
    timeout = sub.add_parser("timeout")
    timeout.add_argument("--input-root", type=Path, required=True)
    timeout.add_argument("--config", type=Path, required=True)
    timeout.add_argument("--review-timeout-s", type=float)
    timeout.add_argument("--review-timeout-justification")
    timeout.add_argument("--output", type=Path, required=True)
    variance = sub.add_parser("variance")
    variance.add_argument("--input-root", type=Path, required=True)
    variance.add_argument("--config", type=Path, required=True)
    variance.add_argument("--review-trace-count", type=int)
    variance.add_argument("--review-trace-count-justification")
    variance.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_model = load_causal_config(args.config, Path("."))
    rows = load_trial_summaries(args.input_root, config_model)
    if args.command == "rates":
        rates = config_model.raw["manifest"]["arrival_loads"]
        report = summarize_rate_calibration(
            rows,
            rates,
            reviewed_selection=_parse_reviewed_rates(args.review_rate),
            reviewed_selection_justification=args.review_rate_justification,
        )
    elif args.command == "timeout":
        report = summarize_timeout_calibration(
            rows,
            reviewed_timeout_s=args.review_timeout_s,
            reviewed_timeout_justification=args.review_timeout_justification,
        )
    else:
        report = summarize_variance_pilot(
            rows,
            reviewed_trace_count=args.review_trace_count,
            reviewed_trace_count_justification=(
                args.review_trace_count_justification
            ),
        )
    write_report(report, args.output)
    print(args.output.resolve())
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
