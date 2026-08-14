#!/usr/bin/env python3
"""Build the compact, retained evidence for the corrected-architecture pilot."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import fmean


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = REPO_ROOT / "study" / "output"
ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "pilots"
RATE_BY_ID = {
    "rate_003": 0.03,
    "rate_0075": 0.075,
    "rate_015": 0.15,
    "rate_03": 0.3,
    "rate_06": 0.6,
    "rate_12": 1.2,
    "rate_24": 2.4,
}


def load_campaign(campaign_id: str) -> list[dict]:
    root = OUTPUT_ROOT / campaign_id / "completed"
    rows = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(root.glob("*/trial_summary.json"))
    ]
    if not rows:
        raise FileNotFoundError(f"no retained summaries under {root}")
    return rows


def mean(rows: list[dict], key: str, *, successful_only: bool = False) -> float | None:
    values = [
        float(row[key])
        for row in rows
        if row.get(key) is not None
        and (not successful_only or bool(row.get("all_tasks_completed")))
    ]
    return fmean(values) if values else None


def aggregate(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row[key] for key in keys)].append(row)
    output: list[dict] = []
    for group_key, group in sorted(grouped.items()):
        completed = sum(bool(row.get("all_tasks_completed")) for row in group)
        item = dict(zip(keys, group_key))
        item.update(
            {
                "arrival_rate_tasks_per_s": RATE_BY_ID[group[0]["arrival_load"]],
                "trials": len(group),
                "completed_trials": completed,
                "incomplete_trials": len(group) - completed,
                "mean_mission_elapsed_success_s": mean(
                    group, "mission_elapsed_time_s", successful_only=True
                ),
                "mean_release_to_completion_success_s": mean(
                    group,
                    "mean_release_to_completion_latency_s",
                    successful_only=True,
                ),
                "mean_agx_allocator_processor_work_s": mean(
                    group, "agx_allocator_processor_work_s"
                ),
                "mean_allocator_calls": mean(group, "allocator_call_count"),
                "mean_allocation_epochs": mean(group, "allocation_epoch_count"),
                "mean_timeout_epochs": mean(group, "timeout_trigger_count"),
                "mean_terminal_residual_tasks": mean(
                    group, "terminal_residual_admitted_task_count"
                ),
                "mean_logical_allocation_payload_bytes": mean(
                    group, "logical_allocation_payload_bytes_sent_total"
                ),
                "piggybacked_epoch_total": sum(
                    int(row.get("piggybacked_admission_epoch_count", 0))
                    for row in group
                ),
                "final_flush_total": sum(
                    int(row.get("final_flush_trigger_count", 0)) for row in group
                ),
            }
        )
        output.append(item)
    return output


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    rate_rows = load_campaign("corrected_architecture_rate_pilot_v1")
    confirmation = load_campaign("corrected_architecture_rate_confirmation_v1")
    # The confirmation campaign contains only traces 3-4, so concatenation
    # extends rates 0.6 and 1.2 without duplicating the original three traces.
    rate_rows.extend(confirmation)
    policy_rows = load_campaign("corrected_architecture_policy_pilot_v1")
    zero_rows = load_campaign("corrected_architecture_zero_diagnostic_v1")

    rate_summary = aggregate(rate_rows, ("arrival_load", "policy_id"))
    policy_summary = aggregate(policy_rows, ("arrival_load", "policy_id"))
    zero_summary = aggregate(zero_rows, ("arrival_load", "policy_id"))
    write_csv(ARTIFACT_ROOT / "corrected_rate_summary.csv", rate_summary)
    write_csv(ARTIFACT_ROOT / "corrected_policy_summary.csv", policy_summary)
    write_csv(ARTIFACT_ROOT / "corrected_zero_diagnostic_summary.csv", zero_summary)

    bounded = {
        row["arrival_load"]: (
            None
            if not row["mean_allocation_epochs"]
            else row["mean_timeout_epochs"] / row["mean_allocation_epochs"]
        )
        for row in policy_summary
        if row["policy_id"] == "bounded_b4_w10"
    }
    selection = {
        "schema_version": 1,
        "architecture": "corrected_message_only_strict_bound_non_destructive_event_driven",
        "fresh_manifest_set_id": "collaborative_visit_g19_t50_n5_corrected_pilot_v1",
        "selected_arrival_rates_tasks_per_s": {
            "low": 0.075,
            "medium": 0.3,
            "high": 0.6,
        },
        "selected_policies": [
            "eager_b1",
            "count_b2",
            "count_b4",
            "count_b8",
            "bounded_b4_w10",
        ],
        "bounded_b4_w10_timeout_fraction_by_load": bounded,
        "agx_trials_per_condition_per_round": 25,
        "agx_total_trials_per_condition": 50,
        "rp2040_traces_per_selected_condition": 4,
        "selected_high_load_zero_diagnostic": {
            "algorithm": "ACBBA",
            "trials": len(zero_rows),
            "completed_trials": sum(
                bool(row.get("all_tasks_completed")) for row in zero_rows
            ),
        },
        "round_2_restriction": "none; pre-round-2 verification is informational",
        "excluded_from_evidence": [
            "all pre-correction pilot artifacts",
            "pre-fix/incomplete diagnostic campaigns",
        ],
    }
    (ARTIFACT_ROOT / "corrected_pilot_selection.json").write_text(
        json.dumps(selection, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"wrote {len(rate_summary)} rate rows, {len(policy_summary)} policy rows, "
        f"and {len(zero_summary)} zero-diagnostic rows"
    )


if __name__ == "__main__":
    main()
