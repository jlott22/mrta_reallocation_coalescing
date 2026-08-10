"""Reviewer-facing native smoke, calibration, and full-campaign reports."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from study.manifests import canonical_json_bytes, sha256_file

from .freeze import ANALYSIS_VERSION
from .model import PUBLICATION_WORKER_COUNT, CausalConfig, load_causal_config


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json_bytes(value)
    if path.exists() and path.read_bytes() != data:
        prior = path.read_bytes()
        history = path.parent / "history"
        history.mkdir(parents=True, exist_ok=True)
        archived = history / (
            f"{path.stem}_{hashlib.sha256(prior).hexdigest()[:12]}{path.suffix}"
        )
        if not archived.exists():
            shutil.copy2(path, archived)
    path.write_bytes(data)


def _write_text_with_history(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    if path.exists() and path.read_bytes() != data:
        prior = path.read_bytes()
        history = path.parent / "history"
        history.mkdir(parents=True, exist_ok=True)
        archived = history / (
            f"{path.stem}_{hashlib.sha256(prior).hexdigest()[:12]}{path.suffix}"
        )
        if not archived.exists():
            shutil.copy2(path, archived)
    path.write_bytes(data)


def build_smoke_report(
    campaign_root: Path,
    config: CausalConfig,
) -> dict[str, Any]:
    from .orchestrator import _completion_state, _git_identity
    from .schedule import plan_paired_blocks

    if config.stage != "smoke" or config.development_override:
        raise ValueError("native smoke report requires the non-development smoke config")
    if campaign_root.resolve() != config.output_root.resolve():
        raise ValueError("smoke report root differs from configured campaign output")
    source = _git_identity(config.repo_root)
    execution = _load(campaign_root / "campaign_execution_report.json")
    completed = campaign_root / "causal" / "completed"
    jobs = [
        job for block in plan_paired_blocks(config, zero_compute=False)
        for job in block.jobs
    ]
    expected_ids = {job.job_id for job in jobs}
    observed_ids = {
        path.name for path in completed.iterdir() if path.is_dir()
    } if completed.is_dir() else set()
    exact_promoted_set = observed_ids == expected_ids
    semantically_valid_jobs = {
        job.job_id for job in jobs
        if _completion_state(config, job, source) == "completed"
    }
    summaries = [
        _load(completed / job.job_id / "trial_summary.json")
        for job in jobs if job.job_id in semantically_valid_jobs
    ]
    all_events = _events(campaign_root / "campaign_events.jsonl")
    invocation_id = execution.get("invocation_id")
    resume_events = [
        row for row in all_events if row.get("invocation_id") == invocation_id
    ]
    ready = [row for row in resume_events if row.get("type") == "worker_ready"]
    ready_bindings = {(row.get("worker_index"), row.get("board_id")) for row in ready}
    skipped = {row.get("job_id") for row in resume_events if row.get("status") == "skipped_completed"}
    executed_or_failed = [
        row for row in resume_events
        if row.get("type") in {"attempt_failure", "worker_fatal"}
        or row.get("status") in {"completed", "failed", "conflict", "technical_failure"}
    ]
    releases_during_compute = 0
    context_ids: set[str] = set()
    call_rows_total = 0
    promoted_semantic_markers = 0
    all_task_timing_valid = True
    all_movement_timing_valid = True
    for directory in sorted(completed.iterdir()) if completed.is_dir() else []:
        if not directory.is_dir():
            continue
        calls = _csv(directory / "allocator_calls.csv")
        tasks = _csv(directory / "task_events.csv")
        movements = _csv(directory / "movement_events.csv")
        marker = _load(directory / "completion.json")
        promoted_semantic_markers += int(
            marker.get("semantic_validation", {}).get("valid") is True
        )
        call_rows_total += len(calls)
        context_ids.update(row["logical_robot_id"] for row in calls)
        releases = [float(row["release_time_s"]) for row in tasks if float(row["release_time_s"]) > 0.0]
        for call in calls:
            start = float(call["virtual_compute_start_s"])
            end = float(call["virtual_compute_completion_s"])
            releases_during_compute += sum(start < release < end for release in releases)
        all_task_timing_valid = all_task_timing_valid and all(
            float(row["first_assignment_time_s"]) >= float(row["release_time_s"])
            and float(row["completion_time_s"]) >= float(row["release_time_s"])
            for row in tasks
            if row["first_assignment_time_s"] and row["completion_time_s"]
        )
        movement_completions = {
            round(float(row["movement_completion_s"]), 9) for row in movements
        }
        all_movement_timing_valid = all_movement_timing_valid and all(
            row.get("completion_mode") in {"stationary_service", "current_cell_service"}
            or round(float(row["completion_time_s"]), 9) in movement_completions
            for row in tasks if row["completion_time_s"]
        )
    source_identity = execution.get("source_identity", {})
    expected_identity = {
        "config_sha256": execution.get("config_sha256"),
        "manifest_index_sha256": execution.get("manifest_index_sha256"),
        "hardware_binding_sha256": execution.get("hardware_binding_sha256"),
        "git_head": source_identity.get("git_head"),
        "source_tree_sha256": source_identity.get("source_tree_sha256"),
        "boards": execution.get("board_bindings"),
        "schedule_sha256": execution.get("schedule_sha256"),
    }
    checks = {
        "execution_completed": execution.get("passed") is True,
        "all_planned_missions_complete": execution.get("completed_jobs") == execution.get("planned_jobs"),
        "exact_planned_promoted_directory_set": exact_promoted_set,
        "all_promoted_jobs_revalidated": semantically_valid_jobs == expected_ids,
        "exact_publication_worker_count": (
            execution.get("worker_count") == PUBLICATION_WORKER_COUNT
        ),
        "unique_publication_worker_board_bindings": (
            len(ready_bindings) == PUBLICATION_WORKER_COUNT
            and len({item[1] for item in ready_bindings})
            == PUBLICATION_WORKER_COUNT
        ),
        "all_missions_scientifically_complete": bool(summaries) and all(row.get("all_tasks_completed") is True for row in summaries),
        "all_calls_parity_clean": bool(summaries) and all(row.get("parity_passed") is True for row in summaries),
        "hardware_validated": bool(summaries) and all(row.get("hardware_validated") is True for row in summaries),
        "four_logical_contexts_observed": len(context_ids) == 4,
        "causal_calls_observed": call_rows_total > 0,
        "release_during_compute_observed": releases_during_compute > 0,
        "latest_invocation_resume_revalidated_and_skipped_all": (
            bool(invocation_id)
            and skipped == expected_ids
            and not executed_or_failed
            and len(ready) == PUBLICATION_WORKER_COUNT
        ),
        "no_technical_failures": execution.get("technical_attempt_failures") == 0,
        "all_promoted_outputs_semantically_validated": (
            promoted_semantic_markers == len(summaries)
        ),
        "tasks_never_assigned_or_completed_before_release": all_task_timing_valid,
        "task_completion_respects_movement_completion": all_movement_timing_valid,
        "source_and_binding_identity_present": all(
            expected_identity.get(name) is not None
            and expected_identity.get(name) != ""
            and expected_identity.get(name) != []
            for name in (
                "config_sha256", "manifest_index_sha256", "git_head",
                "source_tree_sha256", "boards", "schedule_sha256",
            )
        ),
        "latest_invocation_fixed_hash_seed": bool(ready) and all(
            row.get("python_hash_seed") == "0" for row in ready
        ),
        "latest_invocation_single_thread_libraries": bool(ready) and all(
            set(row.get("thread_environment", {}).values()) == {"1"}
            for row in ready
        ),
    }
    passed = all(checks.values())
    return {
        "schema_version": 1,
        "report_kind": "causal_hardware_smoke",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "passed": passed,
        "hardware_validated": passed,
        "campaign_root": str(campaign_root),
        "campaign_execution_sha256": sha256_file(campaign_root / "campaign_execution_report.json"),
        "checks": checks,
        "mission_count": len(summaries),
        "call_count": call_rows_total,
        "release_events_strictly_inside_compute_intervals": releases_during_compute,
        "logical_context_ids": sorted(context_ids),
        "worker_board_bindings": sorted([list(item) for item in ready_bindings], key=lambda item: item[0]),
        "resume_skipped_job_count": len(skipped),
        "resume_invocation_id": invocation_id,
        "input_identity": expected_identity,
        "note": "Failure of release_during_compute_observed means this smoke did not exercise that boundary and must be expanded; it is not auto-waived.",
    }


def calibration_markdown(report: dict[str, Any]) -> str:
    kind = report.get("report_kind", "unknown")
    reviewed = (
        report.get("reviewed_selection")
        or report.get("reviewed_timeout_s")
        or report.get("reviewed_trace_count")
    )
    justification = (
        report.get("reviewed_selection_justification")
        or report.get("reviewed_timeout_justification")
        or report.get("reviewed_trace_count_justification")
    )
    return f"""# Native Calibration Report

Report kind: `{kind}`
Technical coverage: **{'PASS' if report.get('passed') else 'FAIL'}**
Hardware validated: **{report.get('hardware_validated')}**
Observed missions: `{report.get('observed_trials')}`
Expected minimum: `{report.get('expected_minimum_trials')}`

Reviewed selection: `{json.dumps(reviewed, sort_keys=True)}`

Reviewed override justification: `{json.dumps(justification)}`

Predeclared proposal/rule: `{json.dumps(report.get('selection_proposal') or report.get('selection_proposal_s') or report.get('trace_count_proposal'), sort_keys=True)}` / `{report.get('proposal_rule')}`

This report treats the mission/trial as the replicate. Selection rules are
declared in the machine-readable JSON. Rates and W are not chosen to maximize
coalescing savings, and a proposal is not frozen until explicitly reviewed.
"""


def full_campaign_report(
    campaign_root: Path,
    analysis_root: Path,
    config: CausalConfig,
) -> tuple[dict[str, Any], str]:
    from .analysis import _validated_campaign_rows
    from .orchestrator import _git_identity, _validate_full_freeze
    from .schedule import plan_paired_blocks, validate_schedule

    if config.stage != "full" or config.development_override:
        raise ValueError("full report requires the frozen non-development config")
    if campaign_root.resolve() != config.output_root.resolve():
        raise ValueError("campaign report root differs from frozen config output_root")
    if analysis_root.resolve() != (config.output_root / "analysis").resolve():
        raise ValueError("analysis report root differs from frozen config")
    source = _git_identity(config.repo_root)
    freeze = _validate_full_freeze(config, source)
    execution = _load(campaign_root / "campaign_execution_report.json")
    zero = _load(campaign_root / "zero_compute_execution_report.json")
    def retained_failure_count(report_row: Mapping[str, Any]) -> int | None:
        value = report_row.get("technical_attempt_failures")
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return None
        return value

    causal_technical_failures = retained_failure_count(execution)
    zero_technical_failures = retained_failure_count(zero)
    technical_failure_counts_valid = (
        causal_technical_failures is not None
        and zero_technical_failures is not None
    )
    metadata = _load(analysis_root / "analysis_metadata.json")
    paired = _csv(analysis_root / "paired_eager_effects_trial_level.csv")
    negative = sum(row.get("negative_D_alloc_flag", "").lower() == "true" for row in paired)
    board_counts = Counter(row.get("board_id", "") for row in paired)
    expected_jobs = sum(
        len(block.jobs) for block in plan_paired_blocks(config, zero_compute=False)
    )
    causal_schedule = validate_schedule(
        config, plan_paired_blocks(config, zero_compute=False), zero_compute=False
    )
    zero_schedule = validate_schedule(
        config, plan_paired_blocks(config, zero_compute=True), zero_compute=True
    )
    causal_input = metadata.get("input_identity", {}).get("causal", {})
    zero_input = metadata.get("input_identity", {}).get("zero_compute", {})
    raw_revalidation_error: str | None = None
    current_causal_identity: dict[str, Any] | None = None
    current_zero_identity: dict[str, Any] | None = None
    try:
        _, current_causal_identity = _validated_campaign_rows(
            config, zero_compute=False
        )
        _, current_zero_identity = _validated_campaign_rows(
            config, zero_compute=True
        )
    except Exception as error:  # A malformed/tampered artifact is a report failure.
        raw_revalidation_error = f"{type(error).__name__}: {error}"
    raw_outputs_revalidated = bool(
        current_causal_identity is not None
        and current_zero_identity is not None
        and current_causal_identity == causal_input
        and current_zero_identity == zero_input
    )
    analysis_hashes = metadata.get("analysis_output_sha256", {})
    output_hashes_valid = isinstance(analysis_hashes, dict) and bool(analysis_hashes)
    if output_hashes_valid:
        for filename, expected_sha in analysis_hashes.items():
            path = analysis_root / filename
            if (
                not path.is_file()
                or path.parent.resolve() != analysis_root.resolve()
                or sha256_file(path) != expected_sha
            ):
                output_hashes_valid = False
                break
    execution_identity_valid = all((
        execution.get("report_kind") == "causal_campaign_execution",
        execution.get("passed") is True,
        execution.get("zero_compute") is False,
        execution.get("hardware_validated") is True,
        execution.get("planned_jobs") == expected_jobs,
        execution.get("completed_jobs") == expected_jobs,
        execution.get("schedule_sha256") == causal_schedule["schedule_sha256"],
        execution.get("config_sha256") == config.config_sha256,
        execution.get("manifest_index_sha256") == config.manifest_index_sha256,
        execution.get("source_identity") == source,
    ))
    zero_identity_valid = all((
        zero.get("report_kind") == "causal_campaign_execution",
        zero.get("passed") is True,
        zero.get("zero_compute") is True,
        zero.get("hardware_validated") is False,
        zero.get("planned_jobs") == expected_jobs,
        zero.get("completed_jobs") == expected_jobs,
        zero.get("schedule_sha256") == zero_schedule["schedule_sha256"],
        zero.get("config_sha256") == config.config_sha256,
        zero.get("manifest_index_sha256") == config.manifest_index_sha256,
        zero.get("source_identity") == source,
    ))
    analysis_identity_valid = all((
        metadata.get("analysis_version") == ANALYSIS_VERSION,
        metadata.get("input_identity", {}).get("validation_mode")
        == "frozen_full_campaign_semantic_revalidation",
        causal_input.get("config_sha256") == config.config_sha256,
        zero_input.get("config_sha256") == config.config_sha256,
        causal_input.get("manifest_index_sha256") == config.manifest_index_sha256,
        zero_input.get("manifest_index_sha256") == config.manifest_index_sha256,
        causal_input.get("git_head") == source["git_head"],
        zero_input.get("git_head") == source["git_head"],
        causal_input.get("source_tree_sha256") == source["source_tree_sha256"],
        zero_input.get("source_tree_sha256") == source["source_tree_sha256"],
        causal_input.get("execution_report_sha256")
        == sha256_file(campaign_root / "campaign_execution_report.json"),
        zero_input.get("execution_report_sha256")
        == sha256_file(campaign_root / "zero_compute_execution_report.json"),
        causal_input.get("planned_job_count") == expected_jobs,
        zero_input.get("planned_job_count") == expected_jobs,
        causal_input.get("design_id") == freeze.get("design_id"),
        zero_input.get("design_id") == freeze.get("design_id"),
        output_hashes_valid,
    ))
    scientific_coverage_complete = all((
        metadata.get("causal_valid_trial_count") == expected_jobs,
        metadata.get("excluded_trial_count") == 0,
        metadata.get("algorithmically_incomplete_trial_count") == 0,
        metadata.get("zero_compute_job_coverage_complete") is True,
        metadata.get("zero_compute_pair_coverage_complete") is True,
    ))
    passed = bool(
        execution_identity_valid
        and zero_identity_valid
        and analysis_identity_valid
        and raw_outputs_revalidated
        and scientific_coverage_complete
        and technical_failure_counts_valid
    )
    report = {
        "schema_version": 1,
        "report_kind": "full_causal_campaign",
        "passed": passed,
        "execution_identity_valid": execution_identity_valid,
        "zero_compute_identity_valid": zero_identity_valid,
        "analysis_identity_valid": analysis_identity_valid,
        "analysis_output_hashes_valid": output_hashes_valid,
        "raw_outputs_revalidated_against_analysis_inputs": raw_outputs_revalidated,
        "raw_output_revalidation_error": raw_revalidation_error,
        "scientific_coverage_complete": scientific_coverage_complete,
        "technical_failure_counts_valid": technical_failure_counts_valid,
        "hardware_validated": execution.get("hardware_validated") is True,
        "causal_planned": execution.get("planned_jobs"),
        "causal_completed": execution.get("completed_jobs"),
        "zero_compute_planned": zero.get("planned_jobs"),
        "zero_compute_completed": zero.get("completed_jobs"),
        "causal_technical_attempt_failures": causal_technical_failures,
        "zero_compute_technical_attempt_failures": zero_technical_failures,
        "technical_attempt_failures": (
            causal_technical_failures + zero_technical_failures
            if technical_failure_counts_valid
            else None
        ),
        "excluded_trials": metadata.get("excluded_trial_count"),
        "algorithmically_incomplete_trials": metadata.get(
            "algorithmically_incomplete_trial_count"
        ),
        "zero_compute_job_coverage_complete": metadata.get(
            "zero_compute_job_coverage_complete"
        ),
        "zero_compute_pair_coverage_complete": metadata.get(
            "zero_compute_pair_coverage_complete"
        ),
        "analysis_version": metadata.get("analysis_version"),
        "trial_level_replicate": metadata.get("trial_is_the_independent_replicate"),
        "negative_D_alloc_trial_count": negative,
        "paired_rows_by_board": dict(sorted(board_counts.items())),
        "execution_report_sha256": sha256_file(campaign_root / "campaign_execution_report.json"),
        "zero_compute_report_sha256": sha256_file(campaign_root / "zero_compute_execution_report.json"),
        "analysis_metadata_sha256": sha256_file(analysis_root / "analysis_metadata.json"),
        "design_freeze_sha256": causal_input.get("design_freeze_sha256"),
        "expected_mission_count_per_arm": expected_jobs,
    }
    markdown = f"""# Full RP2040-Timed Causal Campaign Report

Overall status: **{'PASS' if report['passed'] else 'FAIL'}**
Hardware-timed causal data: **{report['hardware_validated']}**

## Completion and integrity

- Causal missions: {report['causal_completed']} / {report['causal_planned']}
- Zero-compute counterfactuals: {report['zero_compute_completed']} / {report['zero_compute_planned']}
- Technical failed attempts retained: {report['technical_attempt_failures']}
- Excluded scientific trials retained: {report['excluded_trials']}
- Algorithmically incomplete trials retained: {report['algorithmically_incomplete_trials']}
- Exact zero-compute job coverage: {report['zero_compute_job_coverage_complete']}
- Complete causal/zero makespan-pair coverage: {report['zero_compute_pair_coverage_complete']}
- Analysis/input/output identity valid: {report['analysis_identity_valid']}
- Current promoted raw outputs revalidated: {report['raw_outputs_revalidated_against_analysis_inputs']}
- Negative `D_alloc` values retained and flagged: {report['negative_D_alloc_trial_count']}
- Trial, not task, is the replicate: {report['trial_level_replicate']}

## Board balance

```json
{json.dumps(report['paired_rows_by_board'], indent=2, sort_keys=True)}
```

RP2040 processor work, AGX processor work, USB round-trip, and host setup remain
separate. Mission elapsed is the final required task-completion timestamp; no
summed robot compute or post-hoc duration is relabeled as mission time.
"""
    return report, markdown


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--campaign-root", type=Path, required=True)
    smoke.add_argument("--config", type=Path, required=True)
    smoke.add_argument("--repo-root", type=Path, default=Path("."))
    smoke.add_argument("--json", type=Path, required=True)
    smoke.add_argument("--markdown", type=Path, required=True)
    calibration = sub.add_parser("calibration")
    calibration.add_argument("--input", type=Path, required=True)
    calibration.add_argument("--markdown", type=Path, required=True)
    full = sub.add_parser("full")
    full.add_argument("--campaign-root", type=Path, required=True)
    full.add_argument("--analysis-root", type=Path, required=True)
    full.add_argument("--config", type=Path, required=True)
    full.add_argument("--repo-root", type=Path, default=Path("."))
    full.add_argument("--json", type=Path, required=True)
    full.add_argument("--markdown", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "smoke":
        config = load_causal_config(args.config, args.repo_root)
        report = build_smoke_report(args.campaign_root.resolve(), config)
        markdown = "# Native Causal Smoke Report\n\n" + "\n".join(
            f"- [{'x' if passed else ' '}] {name}: {'PASS' if passed else 'FAIL'}"
            for name, passed in report["checks"].items()
        ) + "\n"
        _write_json(args.json.resolve(), report)
        _write_text_with_history(args.markdown.resolve(), markdown)
        return 0 if report["passed"] else 2
    if args.command == "calibration":
        report = _load(args.input.resolve())
        _write_text_with_history(
            args.markdown.resolve(), calibration_markdown(report)
        )
        return 0 if report.get("passed") else 2
    config = load_causal_config(args.config, args.repo_root)
    report, markdown = full_campaign_report(
        args.campaign_root.resolve(), args.analysis_root.resolve(), config
    )
    json_path = args.json.resolve()
    markdown_path = args.markdown.resolve()
    json_bytes = canonical_json_bytes(report)
    markdown_bytes = markdown.encode("utf-8")
    for path, data in ((json_path, json_bytes), (markdown_path, markdown_bytes)):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != data:
            raise FileExistsError(f"refusing to replace a different final report: {path}")
        if not path.exists():
            path.write_bytes(data)
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
