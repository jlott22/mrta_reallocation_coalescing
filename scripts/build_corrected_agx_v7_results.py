#!/usr/bin/env python3
"""Export corrected-v7 AGX raw campaigns into a compact canonical dataset.

This packages raw campaign outputs; it does not perform statistical analysis
or create paper figures. The four source roots are explicit and allowlisted.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import math
import shutil
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Iterable


CAMPAIGNS = (
    ("round1", "causal", "corrected_round1_causal_v7", 0, 24),
    ("round1", "zero_compute", "corrected_round1_zero_v7", 0, 24),
    ("round2", "causal", "corrected_round2_causal_v7", 25, 49),
    ("round2", "zero_compute", "corrected_round2_zero_v7", 25, 49),
)
EXPECTED_SOURCE_COMMIT = "598485103ee270a27d8b684c0082e46e2ad53802"
EXPECTED_SOURCE_TREE = "49b978dd91f3e30388d913f090454ec645459f246d8daa4e7e754127a13bfc7c"
EXPECTED_JOBS_PER_CAMPAIGN = 1500
SCALAR_METRICS = (
    "mission_elapsed_time_s",
    "simulated_execution_time_s",
    "W_alloc_agx_s",
    "W_alloc_rp2040_s",
    "allocator_call_count",
    "allocation_epoch_count",
    "messages_sent_total",
    "messages_delivered_total",
    "logical_message_payload_bytes_sent_total",
    "movement_time_s",
    "completed_task_count",
    "mean_release_to_completion_latency_s",
    "median_release_to_completion_latency_s",
    "p95_release_to_completion_latency_s",
    "mean_release_to_first_current_goal_latency_s",
    "p95_release_to_first_current_goal_latency_s",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _gzip_text(path: Path) -> io.TextIOWrapper:
    raw = path.open("wb")
    compressed = gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0)
    return io.TextIOWrapper(compressed, encoding="utf-8", newline="")


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return value


def _number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _write_combined_csv(
    output: Path,
    records: list[dict[str, Any]],
    source_name: str,
) -> int:
    headers: list[str] = []
    seen: set[str] = set()
    row_count = 0
    for record in records:
        source = record["job_dir"] / source_name
        with source.open(newline="", encoding="utf-8") as handle:
            reader = csv.reader(handle)
            source_headers = next(reader)
        for name in source_headers:
            if name not in seen:
                seen.add(name)
                headers.append(name)

    prefix = ["campaign_id", "round", "timing_arm", "job_id"]
    with _gzip_text(output) as handle:
        writer = csv.DictWriter(handle, fieldnames=prefix + headers, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            source = record["job_dir"] / source_name
            with source.open(newline="", encoding="utf-8") as source_handle:
                for row in csv.DictReader(source_handle):
                    writer.writerow(
                        {
                            **row,
                            "campaign_id": record["campaign_id"],
                            "round": record["round"],
                            "timing_arm": record["arm"],
                            "job_id": record["job_id"],
                        }
                    )
                    row_count += 1
    return row_count


def _write_jsonl(output: Path, records: Iterable[dict[str, Any]]) -> int:
    count = 0
    with _gzip_text(output) as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--output-dir", type=Path, help="New compact export directory")
    parser.add_argument(
        "--campaign-root",
        action="append",
        default=[],
        metavar="CAMPAIGN_ID=PATH",
        help="Required raw campaign root; provide each of the four allowlisted IDs",
    )
    args = parser.parse_args()
    repo = args.repo_root.resolve()

    overrides: dict[str, Path] = {}
    expected_campaign_ids = {item[2] for item in CAMPAIGNS}
    for value in args.campaign_root:
        campaign_id, separator, raw_path = value.partition("=")
        if not separator or campaign_id not in expected_campaign_ids:
            raise ValueError(f"invalid --campaign-root override: {value}")
        if campaign_id in overrides:
            raise ValueError(f"duplicate campaign-root override: {campaign_id}")
        candidate = Path(raw_path)
        overrides[campaign_id] = candidate.resolve() if candidate.is_absolute() else (
            repo / candidate
        ).resolve()
    missing_campaign_ids = expected_campaign_ids - set(overrides)
    if missing_campaign_ids:
        raise ValueError(
            "explicit --campaign-root values are required for: "
            + ", ".join(sorted(missing_campaign_ids))
        )

    output = (args.output_dir or repo / "corrected_agx_v7_results").resolve()
    if output.exists():
        existing = {path.name for path in output.iterdir()}
        if existing - {"README.md"}:
            raise FileExistsError(f"refusing to overwrite existing export: {output}")
    output.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    campaign_rows: list[dict[str, Any]] = []
    latest_completion = ""
    for round_id, arm, campaign_id, trace_min, trace_max in CAMPAIGNS:
        root = overrides[campaign_id]
        completed = root / "completed"
        job_dirs = sorted(path for path in completed.iterdir() if path.is_dir())
        if len(job_dirs) != EXPECTED_JOBS_PER_CAMPAIGN:
            raise ValueError(f"{campaign_id}: expected 1500 jobs, found {len(job_dirs)}")
        algorithmic_complete = 0
        for job_dir in job_dirs:
            completion = _json(job_dir / "completion.json")
            summary = _json(job_dir / "trial_summary.json")
            job = _json(job_dir / "job.json")
            trace_number = int(str(completion["trace_id"]).split("_")[-1])
            if not trace_min <= trace_number <= trace_max:
                raise ValueError(f"{campaign_id}: out-of-round trace {completion['trace_id']}")
            if completion.get("git_head") != EXPECTED_SOURCE_COMMIT:
                raise ValueError(f"{campaign_id}: unexpected source commit")
            if completion.get("source_tree_sha256") != EXPECTED_SOURCE_TREE:
                raise ValueError(f"{campaign_id}: unexpected source tree")
            if summary.get("timing_provider") not in {"HostMeasuredTimingProvider", "ZeroComputeTimingProvider"}:
                raise ValueError(f"{campaign_id}: non-AGX timing provider")
            if summary.get("device_allocator_hardware_validated") is not False:
                raise ValueError(f"{campaign_id}: hardware-validated trial is forbidden")
            expected_arm = "HostMeasuredTimingProvider" if arm == "causal" else "ZeroComputeTimingProvider"
            if summary.get("timing_provider") != expected_arm:
                raise ValueError(f"{campaign_id}: timing-arm mismatch")
            algorithmic_complete += int(summary.get("algorithmic_status") == "completed")
            latest_completion = max(latest_completion, str(completion.get("completed_at", "")))
            records.append(
                {
                    "round": round_id,
                    "arm": arm,
                    "campaign_id": campaign_id,
                    "job_id": str(completion["job_id"]),
                    "job_dir": job_dir,
                    "completion": completion,
                    "summary": summary,
                    "job": job,
                }
            )
        campaign_rows.append(
            {
                "campaign_id": campaign_id,
                "round": round_id,
                "timing_arm": arm,
                "trace_min": trace_min,
                "trace_max": trace_max,
                "technical_completed": len(job_dirs),
                "algorithmic_completed": algorithmic_complete,
                "algorithmic_incomplete": len(job_dirs) - algorithmic_complete,
                "source_commit": EXPECTED_SOURCE_COMMIT,
                "source_tree_sha256": EXPECTED_SOURCE_TREE,
            }
        )

    # Full, lossless scalar/nested trial summaries and completion seals.
    summary_count = _write_jsonl(
        output / "trial_summaries.jsonl.gz",
        (
            {
                "campaign_id": row["campaign_id"],
                "round": row["round"],
                "timing_arm": row["arm"],
                "job_id": row["job_id"],
                "summary": row["summary"],
            }
            for row in records
        ),
    )
    completion_count = _write_jsonl(
        output / "completion_records.jsonl.gz",
        (
            {
                "campaign_id": row["campaign_id"],
                "round": row["round"],
                "timing_arm": row["arm"],
                **row["completion"],
            }
            for row in records
        ),
    )

    # Flat trial table retains every summary field for spreadsheet/R use.
    summary_fields = sorted({key for row in records for key in row["summary"]})
    identity = ["campaign_id", "round", "timing_arm", "job_id"]
    with _gzip_text(output / "trial_level.csv.gz") as handle:
        writer = csv.DictWriter(handle, fieldnames=identity + summary_fields)
        writer.writeheader()
        for row in records:
            writer.writerow(
                {
                    **{key: row[key if key != "timing_arm" else "arm"] for key in identity},
                    **{key: _csv_value(value) for key, value in row["summary"].items()},
                }
            )

    epoch_rows = _write_combined_csv(output / "allocation_epoch_level.csv.gz", records, "allocation_epochs.csv")
    task_rows = _write_combined_csv(output / "task_level.csv.gz", records, "task_events.csv")

    # Coverage and descriptive condition summaries across the full 50 traces.
    groups: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        summary = row["summary"]
        groups[(row["arm"], summary["algorithm"], summary["arrival_load"], summary["policy_id"])].append(row)
    coverage_fields = [
        "timing_arm", "algorithm", "arrival_load", "policy_id", "planned_trials",
        "technical_completed", "algorithmic_completed", "algorithmic_incomplete",
    ]
    with (output / "matrix_coverage.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=coverage_fields, lineterminator="\n")
        writer.writeheader()
        for key, rows in sorted(groups.items()):
            complete = sum(r["summary"].get("algorithmic_status") == "completed" for r in rows)
            writer.writerow(dict(zip(coverage_fields[:4], key)) | {
                "planned_trials": 50,
                "technical_completed": len(rows),
                "algorithmic_completed": complete,
                "algorithmic_incomplete": len(rows) - complete,
            })

    condition_fields = coverage_fields[:4] + ["metric", "n", "median", "min", "max"]
    with (output / "condition_summaries.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=condition_fields, lineterminator="\n")
        writer.writeheader()
        for key, rows in sorted(groups.items()):
            for metric in SCALAR_METRICS:
                values = [number for row in rows if (number := _number(row["summary"].get(metric))) is not None]
                writer.writerow(dict(zip(condition_fields[:4], key)) | {
                    "metric": metric,
                    "n": len(values),
                    "median": median(values) if values else "",
                    "min": min(values) if values else "",
                    "max": max(values) if values else "",
                })

    # Exact causal/zero pairs use round + job_id, avoiding the old cross-root conflict.
    paired: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in records:
        paired[(row["round"], row["job_id"])][row["arm"]] = row["summary"]
    pair_fields = ["round", "job_id", "algorithm", "arrival_load", "policy_id"]
    for metric in SCALAR_METRICS:
        pair_fields.extend((f"causal_{metric}", f"zero_compute_{metric}", f"causal_minus_zero_{metric}"))
    pair_count = 0
    with _gzip_text(output / "causal_zero_pairs.csv.gz") as handle:
        writer = csv.DictWriter(handle, fieldnames=pair_fields)
        writer.writeheader()
        for (round_id, job_id), arms in sorted(paired.items()):
            if set(arms) != {"causal", "zero_compute"}:
                raise ValueError(f"unpaired AGX job: {round_id}/{job_id}")
            causal, zero = arms["causal"], arms["zero_compute"]
            row: dict[str, Any] = {
                "round": round_id,
                "job_id": job_id,
                "algorithm": causal["algorithm"],
                "arrival_load": causal["arrival_load"],
                "policy_id": causal["policy_id"],
            }
            for metric in SCALAR_METRICS:
                c_value, z_value = causal.get(metric), zero.get(metric)
                c_number, z_number = _number(c_value), _number(z_value)
                row[f"causal_{metric}"] = _csv_value(c_value)
                row[f"zero_compute_{metric}"] = _csv_value(z_value)
                row[f"causal_minus_zero_{metric}"] = (
                    c_number - z_number if c_number is not None and z_number is not None else ""
                )
            writer.writerow(row)
            pair_count += 1

    # Audit index: all local AGX artifacts are listed; required scientific files
    # carry the immutable hashes already sealed by completion.json.
    with _gzip_text(output / "raw_artifact_index.csv.gz") as handle:
        fields = ["campaign_id", "round", "timing_arm", "job_id", "filename", "bytes", "sealed_sha256"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in records:
            sealed = row["completion"].get("required_output_sha256", {})
            for path in sorted(p for p in row["job_dir"].iterdir() if p.is_file()):
                writer.writerow({
                    "campaign_id": row["campaign_id"],
                    "round": row["round"],
                    "timing_arm": row["arm"],
                    "job_id": row["job_id"],
                    "filename": path.name,
                    "bytes": path.stat().st_size,
                    "sealed_sha256": sealed.get(path.name, ""),
                })

    provenance_dir = output / "provenance"
    provenance_dir.mkdir()
    for _, _, campaign_id, _, _ in CAMPAIGNS:
        source_root = repo / "study" / "output" / campaign_id
        for source in sorted((source_root / "provenance").glob("*.json")):
            shutil.copy2(source, provenance_dir / f"{campaign_id}__{source.name}")
        shutil.copy2(source_root / "campaign_events.jsonl", provenance_dir / f"{campaign_id}__campaign_events.jsonl")

    with (output / "campaign_coverage.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(campaign_rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(campaign_rows)

    manifest = {
        "schema_version": 1,
        "bundle_id": "corrected_agx_v7_results",
        "scope": "AGX-only simulations; no RP2040 campaign artifacts",
        "source_commit": EXPECTED_SOURCE_COMMIT,
        "source_tree_sha256": EXPECTED_SOURCE_TREE,
        "latest_trial_completed_at": latest_completion,
        "campaign_count": len(CAMPAIGNS),
        "trial_count": summary_count,
        "completion_record_count": completion_count,
        "paired_causal_zero_count": pair_count,
        "allocation_epoch_row_count": epoch_rows,
        "task_event_row_count": task_rows,
        "campaigns": campaign_rows,
    }
    generated_files = sorted(path for path in output.rglob("*") if path.is_file())
    manifest["files"] = {
        str(path.relative_to(output)): {"bytes": path.stat().st_size, "sha256": _sha256(path)}
        for path in generated_files
    }
    (output / "export_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
