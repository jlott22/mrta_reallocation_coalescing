"""Trial-level paired analysis for the RP2040-timed causal campaign."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from study.manifests import canonical_json_bytes, sha256_file

from .model import CausalConfig, load_causal_config


METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "rp2040_work_s": ("rp2040_allocator_processor_work_s", "total_rp2040_allocator_work_s"),
    "agx_work_s": ("agx_allocator_processor_work_s", "total_agx_allocator_work_s"),
    "assignment_median_s": (
        "median_release_to_first_assignment_latency_s", "median_release_to_assignment_latency_s",
    ),
    "assignment_p95_s": (
        "p95_release_to_first_assignment_latency_s", "p95_release_to_assignment_latency_s",
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
        "terminal_residual_event_count", "terminal_residual_trigger_count",
    ),
    "final_flush_events": ("final_flush_event_count",),
    "calls": ("allocator_call_count",),
    "capacity_fraction": ("processor_capacity_fraction",),
}

TEST_METRICS = (
    "rp2040_work_s",
    "assignment_median_s",
    "completion_median_s",
    "completion_p95_s",
    "mission_s",
    "max_steps",
    "team_steps",
)


def _number(row: Mapping[str, Any], metric: str) -> float | None:
    for name in METRIC_ALIASES.get(metric, (metric,)):
        value = row.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result = float(value)
            if math.isfinite(result):
                return result
    return None


def _load_summaries(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("trial_summary.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"expected JSON object: {path}")
        value["_source_path"] = str(path)
        rows.append(value)
    if not rows:
        raise ValueError(f"no trial summaries found under {root}")
    return rows


def _require_canonical_schedule_artifact(
    path: Path,
    expected_schedule: Mapping[str, Any],
    *,
    kind: str,
) -> None:
    if not path.is_file():
        raise ValueError(f"missing immutable campaign schedule: {path}")
    if path.read_bytes() != canonical_json_bytes(expected_schedule):
        raise ValueError(
            f"{kind} immutable schedule differs from the canonical frozen plan"
        )


def _validated_campaign_rows(
    config: CausalConfig,
    *,
    zero_compute: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load exactly the promoted, planned jobs after full semantic revalidation."""

    # Imports are local to keep the pure statistical helpers lightweight and
    # to avoid creating an import cycle during orchestrator startup.
    from .orchestrator import (
        _build_schedule_record,
        _completion_state,
        _git_identity,
        _validate_full_freeze,
    )
    from .outputs import validate_causal_outputs
    from .schedule import plan_paired_blocks, validate_schedule

    if config.stage != "full" or config.development_override:
        raise ValueError("paper-facing analysis requires a frozen, non-development full config")
    source = _git_identity(config.repo_root)
    freeze = _validate_full_freeze(config, source)
    blocks = plan_paired_blocks(config, zero_compute=zero_compute)
    schedule_summary = validate_schedule(config, blocks, zero_compute=zero_compute)
    jobs = [job for block in blocks for job in block.jobs]
    kind = "zero_compute" if zero_compute else "causal"
    completed_root = config.output_root / kind / "completed"
    report_path = config.output_root / (
        "zero_compute_execution_report.json"
        if zero_compute else "campaign_execution_report.json"
    )
    schedule_path = config.output_root / (
        "zero_compute_schedule.json" if zero_compute else "causal_schedule.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"execution report is not an object: {report_path}")
    expected_report = {
        "report_kind": "causal_campaign_execution",
        "passed": True,
        "zero_compute": zero_compute,
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
        if report.get(field) != expected:
            raise ValueError(
                f"{kind} execution report {field} mismatch: "
                f"{report.get(field)!r} != {expected!r}"
            )
    if not zero_compute and report.get("hardware_validated") is not True:
        raise ValueError("causal execution report is not native-hardware validated")
    if zero_compute and report.get("hardware_validated") is not False:
        raise ValueError("zero-compute execution report is incorrectly hardware validated")
    expected_schedule = _build_schedule_record(
        config,
        blocks,
        schedule_summary,
        source,
        selected_workers=len(config.boards),
        zero_compute=zero_compute,
    )
    _require_canonical_schedule_artifact(
        schedule_path,
        expected_schedule,
        kind=kind,
    )

    expected_ids = {job.job_id for job in jobs}
    observed_ids = {
        path.name for path in completed_root.iterdir() if path.is_dir()
    } if completed_root.is_dir() else set()
    if observed_ids != expected_ids:
        raise ValueError(
            f"{kind} promoted directory set differs from frozen schedule; "
            f"missing={sorted(expected_ids - observed_ids)[:5]}, "
            f"extra={sorted(observed_ids - expected_ids)[:5]}"
        )

    rows: list[dict[str, Any]] = []
    completion_hashes: dict[str, str] = {}
    summary_hashes: dict[str, str] = {}
    required_output_hashes: dict[str, Mapping[str, str]] = {}
    for job in jobs:
        if _completion_state(config, job, source) != "completed":
            raise ValueError(f"promoted job failed resume revalidation: {job.job_id}")
        directory = completed_root / job.job_id
        validation = validate_causal_outputs(config, job, directory)
        marker_path = directory / "completion.json"
        summary_path = directory / "trial_summary.json"
        row = json.loads(summary_path.read_text(encoding="utf-8"))
        if not isinstance(row, dict):
            raise ValueError(f"trial summary is not an object: {summary_path}")
        row["_source_path"] = str(summary_path)
        row["_completion_sha256"] = sha256_file(marker_path)
        row["_trial_summary_sha256"] = sha256_file(summary_path)
        rows.append(row)
        completion_hashes[job.job_id] = row["_completion_sha256"]
        summary_hashes[job.job_id] = row["_trial_summary_sha256"]
        required_output_hashes[job.job_id] = validation["required_output_sha256"]

    identity = {
        "kind": kind,
        "config_path": str(config.path.relative_to(config.repo_root)),
        "config_sha256": config.config_sha256,
        "manifest_index_sha256": config.manifest_index_sha256,
        "hardware_binding_sha256": config.hardware_binding_sha256,
        "git_head": source["git_head"],
        "source_tree_sha256": source["source_tree_sha256"],
        "design_freeze_sha256": sha256_file(
            config.repo_root / config.raw["campaign"]["design_freeze_path"]
        ),
        "design_id": freeze.get("design_id"),
        "schedule_sha256": schedule_summary["schedule_sha256"],
        "schedule_file_sha256": sha256_file(schedule_path),
        "execution_report_sha256": sha256_file(report_path),
        "planned_job_count": len(jobs),
        "completion_marker_sha256_by_job": completion_hashes,
        "trial_summary_sha256_by_job": summary_hashes,
        "required_output_sha256_by_job": required_output_hashes,
    }
    return rows, identity


def _rank(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        average = ((cursor + 1) + end) / 2.0
        for index in order[cursor:end]:
            ranks[index] = average
        cursor = end
    return ranks


def _gamma_q(shape: float, x: float) -> float:
    """Regularized upper incomplete gamma, sufficient for chi-square tails."""

    if shape <= 0.0 or x < 0.0:
        raise ValueError("invalid gamma arguments")
    if x == 0.0:
        return 1.0
    eps = 1e-14
    tiny = 1e-300
    if x < shape + 1.0:
        term = total = 1.0 / shape
        ap = shape
        for _ in range(10000):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) < abs(total) * eps:
                break
        lower = total * math.exp(-x + shape * math.log(x) - math.lgamma(shape))
        return max(0.0, min(1.0, 1.0 - lower))
    b = x + 1.0 - shape
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for index in range(1, 10001):
        an = -index * (index - shape)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return max(0.0, min(1.0, math.exp(-x + shape * math.log(x) - math.lgamma(shape)) * h))


def friedman_test(blocks: Sequence[Sequence[float]]) -> dict[str, Any]:
    if not blocks:
        raise ValueError("Friedman test requires paired blocks")
    k = len(blocks[0])
    if k < 2 or any(len(block) != k for block in blocks):
        raise ValueError("Friedman blocks must have equal width >=2")
    n = len(blocks)
    rank_sums = [0.0] * k
    tie_sum = 0.0
    for block in blocks:
        ranks = _rank(block)
        for index, rank in enumerate(ranks):
            rank_sums[index] += rank
        counts: dict[float, int] = defaultdict(int)
        for value in block:
            counts[value] += 1
        tie_sum += sum(count**3 - count for count in counts.values() if count > 1)
    statistic = 12.0 / (n * k * (k + 1.0)) * sum(value**2 for value in rank_sums) - 3.0 * n * (k + 1.0)
    correction = 1.0 - tie_sum / (n * (k**3 - k))
    if correction <= 0.0:
        statistic = 0.0
        p_value = 1.0
    else:
        statistic /= correction
        p_value = _gamma_q((k - 1.0) / 2.0, statistic / 2.0)
    return {
        "test": "Friedman rank-sum chi-square approximation with tie correction",
        "n_blocks": n,
        "condition_count": k,
        "statistic": statistic,
        "degrees_of_freedom": k - 1,
        "p_value": p_value,
        "rank_sums": rank_sums,
    }


def wilcoxon_signed_rank(differences: Sequence[float]) -> dict[str, Any]:
    nonzero = [float(value) for value in differences if value != 0.0 and math.isfinite(value)]
    zeros = len(differences) - len(nonzero)
    if not nonzero:
        return {
            "test": "paired Wilcoxon signed-rank exact conditional signs",
            "n_nonzero": 0,
            "zero_differences": zeros,
            "w_plus": 0.0,
            "w_minus": 0.0,
            "p_value": 1.0,
        }
    abs_ranks = _rank([abs(value) for value in nonzero])
    w_plus = sum(rank for rank, value in zip(abs_ranks, nonzero, strict=True) if value > 0.0)
    total = sum(abs_ranks)
    w_minus = total - w_plus
    # Ranks are integers or half-integers. Scale by two and enumerate the
    # conditional sign distribution via dynamic programming, preserving ties.
    weights = [int(round(rank * 2.0)) for rank in abs_ranks]
    observed = int(round(w_plus * 2.0))
    distribution: dict[int, int] = {0: 1}
    for weight in weights:
        updated = dict(distribution)
        for subtotal, count in distribution.items():
            updated[subtotal + weight] = updated.get(subtotal + weight, 0) + count
        distribution = updated
    permutations = 2 ** len(weights)
    lower = sum(count for subtotal, count in distribution.items() if subtotal <= observed) / permutations
    upper = sum(count for subtotal, count in distribution.items() if subtotal >= observed) / permutations
    p_value = min(1.0, 2.0 * min(lower, upper))
    return {
        "test": "paired Wilcoxon signed-rank exact conditional signs",
        "n_nonzero": len(nonzero),
        "zero_differences": zeros,
        "w_plus": w_plus,
        "w_minus": w_minus,
        "p_value": p_value,
    }


def holm_adjust(p_values: Sequence[float]) -> list[float]:
    count = len(p_values)
    order = sorted(range(count), key=lambda index: p_values[index])
    adjusted = [0.0] * count
    running = 0.0
    for rank, index in enumerate(order):
        candidate = (count - rank) * p_values[index]
        running = max(running, candidate)
        adjusted[index] = min(1.0, running)
    return adjusted


def paired_bootstrap_mean_ci(
    differences: Sequence[float],
    *,
    confidence: float = 0.95,
    samples: int = 10000,
    seed_material: str = "",
) -> tuple[float | None, float | None]:
    values = [float(value) for value in differences if math.isfinite(value)]
    if not values:
        return None, None
    seed = int.from_bytes(hashlib.sha256(seed_material.encode()).digest()[:8], "big")
    rng = random.Random(seed)
    means = sorted(
        statistics.fmean(rng.choice(values) for _ in values)
        for _ in range(samples)
    )
    alpha = (1.0 - confidence) / 2.0
    low = means[max(0, min(samples - 1, int(math.floor(alpha * samples))))]
    high = means[max(0, min(samples - 1, int(math.ceil((1.0 - alpha) * samples)) - 1))]
    return low, high


def _csv_bytes(rows: Sequence[Mapping[str, Any]]) -> bytes:
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for name in row:
            if name not in seen:
                fields.append(name)
                seen.add(name)
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
            "refusing to overwrite a different analysis bundle: "
            + ", ".join(str(path) for path in conflicts)
        )
    for path, data in payloads.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(data)


def analyze(
    causal_root: Path,
    zero_root: Path | None,
    output_dir: Path,
    *,
    config: CausalConfig | None = None,
) -> dict[str, Path]:
    if config is None:
        causal = _load_summaries(causal_root)
        zero_rows = _load_summaries(zero_root) if zero_root is not None else []
        input_identity: dict[str, Any] = {
            "validation_mode": "development_unsealed_inputs",
            "causal_root": str(causal_root),
            "zero_compute_root": None if zero_root is None else str(zero_root),
        }
    else:
        expected_causal = config.output_root / "causal" / "completed"
        expected_zero = config.output_root / "zero_compute" / "completed"
        if output_dir.resolve() != (config.output_root / "analysis").resolve():
            raise ValueError("analysis output directory differs from frozen campaign output_root")
        if causal_root.resolve() != expected_causal.resolve():
            raise ValueError("causal input root differs from the frozen campaign output root")
        if zero_root is None or zero_root.resolve() != expected_zero.resolve():
            raise ValueError("zero-compute input root differs from the frozen campaign output root")
        causal, causal_identity = _validated_campaign_rows(
            config, zero_compute=False
        )
        zero_rows, zero_identity = _validated_campaign_rows(
            config, zero_compute=True
        )
        input_identity = {
            "validation_mode": "frozen_full_campaign_semantic_revalidation",
            "causal": causal_identity,
            "zero_compute": zero_identity,
        }
    if any(row.get("zero_compute") is True for row in causal):
        raise ValueError("causal input contains zero-compute rows")
    valid: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in causal:
        reasons: list[str] = []
        if row.get("technical_status", "completed") != "completed":
            reasons.append("technical_status_not_completed")
        if row.get("all_tasks_completed") is not True:
            reasons.append("algorithmic_mission_incomplete")
        if row.get("hardware_validated") is not True:
            reasons.append("not_hardware_validated")
        if row.get("parity_passed") is not True:
            reasons.append("parity_not_passed")
        if reasons:
            excluded.append(dict(row) | {"analysis_exclusion_reasons": reasons})
        else:
            valid.append(row)
    zero_index: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    all_zero_keys: set[tuple[str, str, str, str]] = set()
    for row in zero_rows:
        if row.get("zero_compute") is not True:
            raise ValueError("zero-compute input contains a causal-timing row")
        key = (
            str(row["algorithm"]),
            str(row.get("arrival_load", row.get("load_id"))),
            str(row["trace_id"]),
            str(row["policy_id"]),
        )
        if key in zero_index:
            raise ValueError(f"duplicate zero-compute condition: {key}")
        all_zero_keys.add(key)
        if row.get("all_tasks_completed") is True:
            zero_index[key] = row
    all_causal_keys = {
        (
            str(row["algorithm"]),
            str(row.get("arrival_load", row.get("load_id"))),
            str(row["trace_id"]),
            str(row["policy_id"]),
        )
        for row in causal
    }
    expected_complete_pair_keys = {
        (
            str(row["algorithm"]),
            str(row.get("arrival_load", row.get("load_id"))),
            str(row["trace_id"]),
            str(row["policy_id"]),
        )
        for row in valid
    }
    missing_zero_condition_keys = sorted(all_causal_keys - all_zero_keys)
    extra_zero_condition_keys = sorted(all_zero_keys - all_causal_keys)
    missing_complete_pair_keys = sorted(
        expected_complete_pair_keys - set(zero_index)
    )
    blocks: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in valid:
        key = (str(row["algorithm"]), str(row.get("arrival_load", row.get("load_id"))), str(row["trace_id"]))
        policy = str(row["policy_id"])
        if policy in blocks[key]:
            raise ValueError(f"duplicate trial-level condition: {key}/{policy}")
        blocks[key][policy] = row
    policy_ids = sorted({str(row["policy_id"]) for row in causal})
    eager_ids = [name for name in policy_ids if "eager" in name.lower()]
    if len(eager_ids) != 1:
        raise ValueError("analysis requires exactly one Eager policy")
    eager_id = eager_ids[0]
    trial_rows: list[dict[str, Any]] = []
    paired_rows: list[dict[str, Any]] = []
    for key, policies in sorted(blocks.items()):
        eager = policies.get(eager_id)
        if eager is None:
            continue
        for policy_id, row in sorted(policies.items()):
            dimensions = {
                "algorithm": key[0], "arrival_load": key[1], "trace_id": key[2],
                "policy_id": policy_id, "board_id": row.get("board_id"),
                "worker_index": row.get("worker_index"), "core_id": row.get("core_id"),
                "policy_mode": row.get("policy_mode"),
                "policy_batch_size": row.get("policy_batch_size", row.get("batch_size")),
                "policy_max_pending_age_s": row.get("policy_max_pending_age_s"),
            }
            normalized = dimensions | {metric: _number(row, metric) for metric in METRIC_ALIASES}
            work = normalized["rp2040_work_s"]
            calls = normalized["calls"]
            events = normalized["events"]
            completed_tasks = float(row.get("completed_task_count", 50))
            normalized["rp2040_work_per_call_s"] = None if calls in {None, 0.0} else work / calls
            normalized["rp2040_work_per_event_s"] = None if events in {None, 0.0} else work / events
            normalized["rp2040_work_per_completed_task_s"] = None if completed_tasks == 0.0 else work / completed_tasks
            zero = zero_index.get((key[0], key[1], key[2], policy_id))
            if zero is not None:
                for identity_field in (
                    "scenario_sha256", "release_sha256", "runtime_seed",
                    "algorithm", "arrival_load", "trace_id", "policy_id",
                    "policy_mode", "policy_batch_size", "policy_max_pending_age_s",
                    "config_sha256", "manifest_index_sha256", "board_id",
                    "hardware_binding_sha256", "git_head", "source_tree_sha256",
                    "expected_device_uid", "expected_device_build_id",
                    "expected_device_firmware_sha256",
                    "expected_device_module_set_sha256",
                ):
                    if zero.get(identity_field) != row.get(identity_field):
                        raise ValueError(
                            f"causal/zero pair differs in {identity_field}: "
                            f"{key}/{policy_id}"
                        )
            zero_mission = _number(zero, "mission_s") if zero is not None else None
            causal_mission = normalized["mission_s"]
            d_alloc = None if zero_mission is None or causal_mission is None else causal_mission - zero_mission
            normalized["zero_compute_mission_s"] = zero_mission
            normalized["D_alloc_s"] = d_alloc
            normalized["allocation_attributable_mission_fraction"] = (
                None if d_alloc is None or causal_mission == 0.0 else d_alloc / causal_mission
            )
            normalized["negative_D_alloc_flag"] = d_alloc is not None and d_alloc < 0.0
            trial_rows.append(normalized)
            eager_work = _number(eager, "rp2040_work_s")
            eager_mission = _number(eager, "mission_s")
            completion = normalized["completion_median_s"]
            eager_completion = _number(eager, "completion_median_s")
            paired = normalized | {
                "eager_policy_id": eager_id,
                "paired_percent_rp2040_processor_work_saved": (
                    None if eager_work in {None, 0.0} or work is None else 100.0 * (eager_work - work) / eager_work
                ),
                "paired_delta_rp2040_work_s_condition_minus_eager": (
                    None if eager_work is None or work is None else work - eager_work
                ),
                "paired_change_completion_latency_s": (
                    None if completion is None or eager_completion is None else completion - eager_completion
                ),
                "paired_change_assignment_latency_s": (
                    None if normalized["assignment_median_s"] is None or _number(eager, "assignment_median_s") is None
                    else normalized["assignment_median_s"] - _number(eager, "assignment_median_s")
                ),
                "paired_change_assignment_p95_latency_s": (
                    None
                    if normalized["assignment_p95_s"] is None
                    or _number(eager, "assignment_p95_s") is None
                    else normalized["assignment_p95_s"]
                    - _number(eager, "assignment_p95_s")
                ),
                "paired_change_completion_p95_latency_s": (
                    None
                    if normalized["completion_p95_s"] is None
                    or _number(eager, "completion_p95_s") is None
                    else normalized["completion_p95_s"]
                    - _number(eager, "completion_p95_s")
                ),
                "paired_change_max_robot_steps": (
                    None
                    if normalized["max_steps"] is None
                    or _number(eager, "max_steps") is None
                    else normalized["max_steps"] - _number(eager, "max_steps")
                ),
                "paired_change_total_team_steps": (
                    None
                    if normalized["team_steps"] is None
                    or _number(eager, "team_steps") is None
                    else normalized["team_steps"] - _number(eager, "team_steps")
                ),
                "paired_change_mission_elapsed_s": (
                    None
                    if causal_mission is None or eager_mission is None
                    else causal_mission - eager_mission
                ),
                "paired_percent_mission_time_change": (
                    None if eager_mission in {None, 0.0} or causal_mission is None
                    else 100.0 * (causal_mission - eager_mission) / eager_mission
                ),
            }
            paired_rows.append(paired)

    inferential: list[dict[str, Any]] = []
    contrasts: list[dict[str, Any]] = []
    by_algorithm_load: dict[tuple[str, str], list[tuple[tuple[str, str, str], dict[str, dict[str, Any]]]]] = defaultdict(list)
    for key, policies in blocks.items():
        by_algorithm_load[(key[0], key[1])].append((key, policies))
    for (algorithm, load), grouped_blocks in sorted(by_algorithm_load.items()):
        complete_policy_ids = list(policy_ids)
        if len(complete_policy_ids) < 2:
            continue
        for metric in TEST_METRICS:
            matrix: list[list[float]] = []
            used_keys: list[tuple[str, str, str]] = []
            for key, policies in sorted(grouped_blocks):
                if not all(policy in policies for policy in complete_policy_ids):
                    continue
                values = [_number(policies[policy], metric) for policy in complete_policy_ids]
                if all(value is not None for value in values):
                    matrix.append([float(value) for value in values if value is not None])
                    used_keys.append(key)
            if len(matrix) < 2:
                continue
            omnibus = friedman_test(matrix)
            inferential.append({
                "algorithm": algorithm, "arrival_load": load, "metric": metric,
                "policies": complete_policy_ids,
                "candidate_block_count": len(grouped_blocks),
                "omitted_incomplete_block_count": len(grouped_blocks) - len(matrix),
                "used_trace_ids": [item[2] for item in used_keys],
                **omnibus,
            })
            if omnibus["p_value"] < 0.05 and eager_id in complete_policy_ids:
                eager_index = complete_policy_ids.index(eager_id)
                family: list[dict[str, Any]] = []
                for policy_index, policy in enumerate(complete_policy_ids):
                    if policy == eager_id:
                        continue
                    differences = [row[policy_index] - row[eager_index] for row in matrix]
                    test = wilcoxon_signed_rank(differences)
                    low, high = paired_bootstrap_mean_ci(
                        differences,
                        seed_material=f"{algorithm}:{load}:{metric}:{policy}",
                    )
                    family.append({
                        "algorithm": algorithm, "arrival_load": load, "metric": metric,
                        "policy_id": policy, "baseline_policy_id": eager_id,
                        "paired_n": len(differences),
                        "mean_paired_difference_condition_minus_eager": statistics.fmean(differences),
                        "median_paired_difference_condition_minus_eager": statistics.median(differences),
                        "paired_mean_95pct_ci_low": low,
                        "paired_mean_95pct_ci_high": high,
                        **test,
                    })
                adjusted = holm_adjust([row["p_value"] for row in family])
                for row, value in zip(family, adjusted, strict=True):
                    row["holm_adjusted_p_value"] = value
                    row["reject_at_familywise_0p05"] = value < 0.05
                contrasts.extend(family)

    def aggregate_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[(str(row["algorithm"]), str(row["arrival_load"]), str(row["policy_id"]))].append(row)
        output: list[dict[str, Any]] = []
        metrics = (
            "paired_percent_rp2040_processor_work_saved", "paired_change_completion_latency_s",
            "paired_change_assignment_latency_s", "paired_change_assignment_p95_latency_s",
            "paired_change_completion_p95_latency_s", "paired_percent_mission_time_change",
            "paired_change_mission_elapsed_s", "paired_change_max_robot_steps",
            "paired_change_total_team_steps",
            "paired_delta_rp2040_work_s_condition_minus_eager",
            "rp2040_work_s", "agx_work_s", "arrival_events", "mandatory_events", "calls",
            "rp2040_work_per_call_s", "rp2040_work_per_event_s", "capacity_fraction",
            "D_alloc_s", "allocation_attributable_mission_fraction", "max_steps", "team_steps",
            "assignment_median_s", "assignment_p95_s", "completion_median_s", "completion_p95_s",
            "events", "piggyback_events", "timeout_events", "batch_events",
            "terminal_residual_events", "final_flush_events",
        )
        for key, group in sorted(grouped.items()):
            row: dict[str, Any] = {
                "algorithm": key[0], "arrival_load": key[1], "policy_id": key[2], "trial_n": len(group)
            }
            for metric in metrics:
                values = [float(value) for item in group if (value := item.get(metric)) is not None]
                row[f"{metric}_mean"] = statistics.fmean(values) if values else None
                row[f"{metric}_median"] = statistics.median(values) if values else None
                if metric.startswith("paired_") or metric in {"D_alloc_s", "allocation_attributable_mission_fraction"}:
                    low, high = paired_bootstrap_mean_ci(values, seed_material=f"summary:{key}:{metric}")
                    row[f"{metric}_mean_ci95_low"] = low
                    row[f"{metric}_mean_ci95_high"] = high
            output.append(row)
        return output

    summaries = aggregate_rows(paired_rows)
    figure1 = [
        {
            "algorithm": row["algorithm"], "arrival_load": row["arrival_load"], "policy_id": row["policy_id"],
            "x_change_release_to_completion_latency_s_mean": row["paired_change_completion_latency_s_mean"],
            "x_change_release_to_completion_latency_s_ci95_low": row["paired_change_completion_latency_s_mean_ci95_low"],
            "x_change_release_to_completion_latency_s_ci95_high": row["paired_change_completion_latency_s_mean_ci95_high"],
            "y_percent_rp2040_processor_work_saved_mean": row["paired_percent_rp2040_processor_work_saved_mean"],
            "y_percent_rp2040_processor_work_saved_ci95_low": row["paired_percent_rp2040_processor_work_saved_mean_ci95_low"],
            "y_percent_rp2040_processor_work_saved_ci95_high": row["paired_percent_rp2040_processor_work_saved_mean_ci95_high"],
            "bounded_policy": "bounded" in row["policy_id"].lower(),
        }
        for row in summaries
    ]
    figure2 = [
        {
            "algorithm": row["algorithm"], "arrival_load": row["arrival_load"], "policy_id": row["policy_id"],
            "total_reallocation_events_mean": row["events_mean"],
            "arrival_driven_events_mean": row["arrival_events_mean"],
            "mandatory_events_mean": row["mandatory_events_mean"],
            "piggybacked_admission_events_mean": row["piggyback_events_mean"],
            "timeout_events_mean": row["timeout_events_mean"],
            "batch_threshold_events_mean": row["batch_events_mean"],
            "terminal_residual_events_mean": row[
                "terminal_residual_events_mean"
            ],
            "final_flush_events_mean": row["final_flush_events_mean"],
            "allocator_calls_mean": row["calls_mean"],
            "rp2040_work_per_call_s_mean": row["rp2040_work_per_call_s_mean"],
            "rp2040_work_per_event_s_mean": row["rp2040_work_per_event_s_mean"],
            "rp2040_total_processor_work_s_mean": row["rp2040_work_s_mean"],
        }
        for row in summaries
    ]
    workload_dependence_b4 = [
        row for row in paired_rows
        if row.get("policy_mode") == "count"
        and float(row.get("policy_batch_size") or 0) == 4.0
    ]
    outcome_counts: dict[tuple[str, str, str], dict[str, int]] = defaultdict(
        lambda: {"planned_or_observed": 0, "algorithmically_completed": 0,
                 "algorithmically_incomplete": 0, "other_excluded": 0}
    )
    for row in causal:
        key = (
            str(row.get("algorithm")),
            str(row.get("arrival_load", row.get("load_id"))),
            str(row.get("policy_id")),
        )
        values = outcome_counts[key]
        values["planned_or_observed"] += 1
        if row.get("all_tasks_completed") is True and row.get("hardware_validated") is True and row.get("parity_passed") is True:
            values["algorithmically_completed"] += 1
        elif row.get("all_tasks_completed") is not True:
            values["algorithmically_incomplete"] += 1
        else:
            values["other_excluded"] += 1
    outcome_rows = [
        {
            "algorithm": key[0],
            "arrival_load": key[1],
            "policy_id": key[2],
            **values,
            "completion_rate": (
                values["algorithmically_completed"] / values["planned_or_observed"]
                if values["planned_or_observed"] else 0.0
            ),
            "rp2040_result_status": (
                "complete_hardware_cohort"
                if values["algorithmically_completed"] == values["planned_or_observed"]
                and values["other_excluded"] == 0
                else "retained_incomplete_or_excluded_trials"
            ),
        }
        for key, values in sorted(outcome_counts.items())
    ]
    outcome_index = {
        (row["algorithm"], row["arrival_load"], row["policy_id"]): row
        for row in outcome_rows
    }
    figure3 = [
        {
            "algorithm": row["algorithm"], "arrival_load": row["arrival_load"], "policy_id": row["policy_id"],
            "percent_mission_time_change_mean": row["paired_percent_mission_time_change_mean"],
            "processor_capacity_fraction_mean": row["capacity_fraction_mean"],
            "allocation_attributable_mission_fraction_mean": row["allocation_attributable_mission_fraction_mean"],
            "D_alloc_s_mean": row["D_alloc_s_mean"],
            "max_robot_steps_mean": row["max_steps_mean"],
            "total_team_steps_mean": row["team_steps_mean"],
            "paired_change_max_robot_steps_mean": row[
                "paired_change_max_robot_steps_mean"
            ],
            "paired_change_total_team_steps_mean": row[
                "paired_change_total_team_steps_mean"
            ],
            "paired_change_mission_elapsed_s_mean": row[
                "paired_change_mission_elapsed_s_mean"
            ],
        }
        for row in summaries
    ]
    summary_index = {
        (row["algorithm"], row["arrival_load"], row["policy_id"]): row
        for row in summaries
    }
    paper_table = []
    for key, outcome in sorted(outcome_index.items()):
        row = summary_index.get(key, {
            "algorithm": key[0],
            "arrival_load": key[1],
            "policy_id": key[2],
            "trial_n": 0,
        })
        paper_table.append(dict(row) | {
            "planned_trial_n": outcome["planned_or_observed"],
            "algorithmically_completed_trial_n": outcome["algorithmically_completed"],
            "algorithmically_incomplete_trial_n": outcome["algorithmically_incomplete"],
            "other_excluded_trial_n": outcome["other_excluded"],
            "completion_rate": outcome["completion_rate"],
            "rp2040_result_status": outcome["rp2040_result_status"],
        })
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "trial_level": output_dir / "causal_trial_level.csv",
        "paired": output_dir / "paired_eager_effects_trial_level.csv",
        "summaries": output_dir / "condition_summaries.csv",
        "friedman": output_dir / "friedman_tests.json",
        "wilcoxon": output_dir / "wilcoxon_eager_contrasts_holm.json",
        "figure1": output_dir / "figure1_compute_responsiveness.csv",
        "figure2": output_dir / "figure2_mechanism_decomposition.csv",
        "figure3": output_dir / "figure3_deployment_consequences.csv",
        "table": output_dir / "paper_summary_table.csv",
        "workload_dependence": output_dir / "workload_dependence_b4_trial_level.csv",
        "outcomes": output_dir / "trial_outcome_counts.csv",
        "excluded": output_dir / "excluded_trials_with_reasons.json",
        "metadata": output_dir / "analysis_metadata.json",
    }
    payloads: dict[Path, bytes] = {
        paths["trial_level"]: _csv_bytes(trial_rows),
        paths["paired"]: _csv_bytes(paired_rows),
        paths["summaries"]: _csv_bytes(paper_table),
        paths["figure1"]: _csv_bytes(figure1),
        paths["figure2"]: _csv_bytes(figure2),
        paths["figure3"]: _csv_bytes(figure3),
        paths["table"]: _csv_bytes(paper_table),
        paths["workload_dependence"]: _csv_bytes(workload_dependence_b4),
        paths["outcomes"]: _csv_bytes(outcome_rows),
        paths["friedman"]: canonical_json_bytes({"schema_version": 1, "tests": inferential}),
        paths["wilcoxon"]: canonical_json_bytes({"schema_version": 1, "contrasts": contrasts}),
        paths["excluded"]: canonical_json_bytes({"schema_version": 1, "trials": excluded}),
    }
    output_hashes = {
        path.name: hashlib.sha256(data).hexdigest()
        for path, data in sorted(payloads.items(), key=lambda item: item[0].name)
    }
    metadata = {
        "schema_version": 1,
        "analysis_version": "causal-analysis-v1",
        "trial_is_the_independent_replicate": True,
        "task_rows_used_as_independent_replicates": False,
        "causal_valid_trial_count": len(valid),
        "excluded_trial_count": len(excluded),
        "algorithmically_incomplete_trial_count": sum(
            row.get("all_tasks_completed") is not True for row in causal
        ),
        "algorithmic_incompletions_are_retained_in_outcome_audit": True,
        "zero_compute_trial_count": len(zero_rows),
        "zero_compute_condition_count": len(all_zero_keys),
        "zero_compute_complete_pair_count": (
            len(expected_complete_pair_keys) - len(missing_complete_pair_keys)
        ),
        "zero_compute_missing_condition_count": len(missing_zero_condition_keys),
        "zero_compute_extra_condition_count": len(extra_zero_condition_keys),
        "zero_compute_missing_complete_pair_count": len(missing_complete_pair_keys),
        "zero_compute_job_coverage_complete": (
            zero_root is not None
            and not missing_zero_condition_keys
            and not extra_zero_condition_keys
        ),
        "zero_compute_pair_coverage_complete": (
            zero_root is not None
            and not missing_zero_condition_keys
            and not extra_zero_condition_keys
            and not missing_complete_pair_keys
        ),
        "zero_compute_missing_condition_keys": missing_zero_condition_keys,
        "zero_compute_extra_condition_keys": extra_zero_condition_keys,
        "zero_compute_missing_complete_pair_keys": missing_complete_pair_keys,
        "paired_block_count": len(blocks),
        "friedman_test_count": len(inferential),
        "wilcoxon_contrast_count": len(contrasts),
        "negative_D_alloc_trials_are_flagged_not_clipped": True,
        "zero_compute_pairing_requires_exact_manifest_seed_policy_identity": True,
        "input_identity": input_identity,
        "analysis_output_sha256": output_hashes,
        "files": {
            name: path.name for name, path in paths.items() if name != "metadata"
        },
    }
    payloads[paths["metadata"]] = canonical_json_bytes(metadata)
    _write_immutable_bundle(payloads)
    return paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--causal-root", type=Path, required=True)
    parser.add_argument("--zero-compute-root", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_causal_config(args.config, args.repo_root)
    paths = analyze(
        args.causal_root.resolve(),
        args.zero_compute_root.resolve() if args.zero_compute_root else None,
        args.output_dir.resolve(),
        config=config,
    )
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
