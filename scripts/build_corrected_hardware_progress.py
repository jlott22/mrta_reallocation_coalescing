#!/usr/bin/env python3
"""Build a compact, fail-closed snapshot of corrected RP2040 progress.

The raw v9/v10 roots contain hundreds of megabytes of per-call diagnostics.
This exporter retains every successful trial summary and completion seal, a
complete terminal/attempt failure audit, schedules/provenance, and hashes that
locate the omitted raw artifacts without copying those artifacts into Git.
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
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Iterable


V9_ID = "corrected_hardware_core_96_v9"
V10_ID = "corrected_hardware_core_96_v10_continuation"
V9_COMPLETED = 26
V10_COMPLETED = 56
V10_TERMINAL_FAILED = 14
V10_ATTEMPT_FAILURES = 34
PLANNED_JOBS = 96
V9_MANIFEST_SHA256 = "03a90d93e1709ba5421100a40413f7b398dfdd9beb2c0e3d86cd7f6a5ab2a3fb"
EXPECTED_IDENTITIES = {
    V9_ID: {
        "git_head": "3fe1f578987088fc8f044741bd046ca4e5488267",
        "source_tree_sha256": "4dc06c3d1edcae1722ad3295e412fa863ced136415528bb840cd0b0c421724fe",
    },
    V10_ID: {
        "git_head": "a3490a7748d852af5b05f1df3caa1f86a44dda17",
        "source_tree_sha256": "895b1584cf51aa2adf31aa5c197f1e24514af08667b507b055c39955e85d77d6",
    },
}
SCALAR_METRICS = (
    "mission_elapsed_time_s",
    "host_program_runtime_s",
    "allocator_call_count",
    "allocator_processor_work_s",
    "mean_allocator_call_time_s",
    "median_allocator_call_time_s",
    "p95_allocator_call_time_s",
    "max_allocator_call_time_s",
    "completed_task_count",
    "messages_sent_total",
    "logical_message_payload_bytes_sent_total",
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


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with _gzip_text(path) as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
            count += 1
    return count


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
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _jobs(schedule: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = [job for block in schedule["blocks"] for job in block["jobs"]]
    result = {str(row["job_id"]): row for row in rows}
    if len(result) != len(rows):
        raise ValueError("schedule contains duplicate job IDs")
    return result


def _failure_category(message: str) -> str:
    if "parity failed" in message:
        return "parity"
    if "MemoryError" in message or "memory failure" in message:
        return "memory"
    if "chunk sequence mismatch" in message:
        return "result_chunk_sequence"
    if "no protocol response" in message:
        return "timeout_or_no_response"
    return "other"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    repo = args.repo_root.resolve()
    output = (args.output_dir or repo / "corrected_hardware_v9_v10_progress").resolve()
    if output.exists():
        existing = {path.name for path in output.iterdir()}
        if existing - {"README.md"}:
            raise FileExistsError(f"refusing to overwrite existing export: {output}")
    output.mkdir(parents=True, exist_ok=True)
    readme_template = (
        Path(__file__).resolve().parents[1]
        / "corrected_hardware_v9_v10_progress"
        / "README.md"
    )
    readme_output = output / "README.md"
    if not readme_output.exists():
        shutil.copyfile(readme_template, readme_output)

    roots = {campaign: repo / "study" / "output" / campaign for campaign in (V9_ID, V10_ID)}
    schedules = {campaign: _json(root / "causal_schedule.json") for campaign, root in roots.items()}
    schedule_jobs = {campaign: _jobs(schedule) for campaign, schedule in schedules.items()}
    for campaign, expected in EXPECTED_IDENTITIES.items():
        identity = schedules[campaign]["source_identity"]
        if identity.get("relevant_dirty") is not False:
            raise ValueError(f"{campaign}: schedule source is dirty")
        for key, value in expected.items():
            if identity.get(key) != value:
                raise ValueError(f"{campaign}: unexpected {key}")
    if len(schedule_jobs[V9_ID]) != PLANNED_JOBS or len(schedule_jobs[V10_ID]) != 70:
        raise ValueError("unexpected v9/v10 schedule size")

    audit_path = repo / "AGX_CORRECTED_EXPERIMENT_HANDOFF" / "audit" / "hardware_v9_completed_26.json"
    if _sha256(audit_path) != V9_MANIFEST_SHA256:
        raise ValueError("v9 completion manifest hash mismatch")
    audit = _json(audit_path)
    audit_jobs = {row["job_id"]: row for row in audit["completed_jobs"]}
    if len(audit_jobs) != V9_COMPLETED:
        raise ValueError("v9 completion manifest does not contain 26 unique jobs")

    records: list[dict[str, Any]] = []
    raw_rows: list[dict[str, Any]] = []
    latest_completion = ""
    for campaign, expected_count in ((V9_ID, V9_COMPLETED), (V10_ID, V10_COMPLETED)):
        completed_root = roots[campaign] / "causal" / "completed"
        job_dirs = sorted(path for path in completed_root.iterdir() if path.is_dir())
        if len(job_dirs) != expected_count:
            raise ValueError(f"{campaign}: expected {expected_count} completed jobs, found {len(job_dirs)}")
        completed_ids = {path.name for path in job_dirs}
        if campaign == V9_ID and completed_ids != set(audit_jobs):
            raise ValueError("v9 completed directories differ from sealed manifest")
        for job_dir in job_dirs:
            job_id = job_dir.name
            if job_id not in schedule_jobs[campaign]:
                raise ValueError(f"{campaign}: completed job not scheduled: {job_id}")
            completion_path = job_dir / "completion.json"
            completion = _json(completion_path)
            if completion.get("job_id") != job_id:
                raise ValueError(f"{campaign}/{job_id}: completion identity mismatch")
            if campaign == V9_ID and _sha256(completion_path) != audit_jobs[job_id]["completion_json_sha256"]:
                raise ValueError(f"{campaign}/{job_id}: sealed completion hash mismatch")
            sealed = completion.get("required_output_sha256", {})
            for filename, expected_hash in sorted(sealed.items()):
                artifact = job_dir / filename
                actual_hash = _sha256(artifact)
                if actual_hash != expected_hash:
                    raise ValueError(f"{campaign}/{job_id}: artifact hash mismatch: {filename}")
                raw_rows.append({
                    "campaign_id": campaign,
                    "record_kind": "completed_artifact",
                    "job_id": job_id,
                    "relative_path": str(artifact.relative_to(repo)),
                    "bytes": artifact.stat().st_size,
                    "sha256": actual_hash,
                })
            summary = _json(job_dir / "trial_summary.json")
            for key, value in EXPECTED_IDENTITIES[campaign].items():
                if summary.get(key) != value:
                    raise ValueError(f"{campaign}/{job_id}: unexpected {key}")
            if summary.get("hardware_validated") is not True or summary.get("technical_status") != "completed":
                raise ValueError(f"{campaign}/{job_id}: trial is not a completed hardware result")
            latest_completion = max(latest_completion, str(completion.get("completed_at", "")))
            records.append({
                "campaign_id": campaign,
                "job_id": job_id,
                "summary": summary,
                "completion": completion,
                "schedule": schedule_jobs[campaign][job_id],
            })

    v9_ids = {row["job_id"] for row in records if row["campaign_id"] == V9_ID}
    v10_ids = {row["job_id"] for row in records if row["campaign_id"] == V10_ID}
    if v9_ids & set(schedule_jobs[V10_ID]):
        raise ValueError("v10 schedule reruns a sealed v9 success")
    if set(schedule_jobs[V9_ID]) != v9_ids | set(schedule_jobs[V10_ID]):
        raise ValueError("v9 successes plus v10 continuation do not cover the 96-job design")

    event_rows = [
        json.loads(line)
        for line in (roots[V10_ID] / "campaign_events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    terminal_events = [row for row in event_rows if row.get("type") == "job" and row.get("status") == "failed"]
    terminal_ids = {str(row["job_id"]) for row in terminal_events}
    if len(terminal_events) != V10_TERMINAL_FAILED or len(terminal_ids) != V10_TERMINAL_FAILED:
        raise ValueError("unexpected v10 terminal failure count")
    if terminal_ids & v10_ids or terminal_ids | v10_ids != set(schedule_jobs[V10_ID]):
        raise ValueError("v10 completions and terminal failures do not partition its schedule")

    failure_events = {
        str(row["attempt_dir"]): row
        for row in event_rows
        if row.get("type") == "attempt_failure"
    }
    attempt_rows: list[dict[str, Any]] = []
    for path in sorted((roots[V10_ID] / "causal" / "attempts").glob("*/attempt_*/failure.json")):
        raw_hash = _sha256(path)
        failure = _json(path)
        attempt_dir = str(path.parent)
        event = failure_events.get(attempt_dir)
        if event is None:
            raise ValueError(f"missing attempt-failure event for {path}")
        message = str(failure.get("message", ""))
        diagnostics = failure.get("diagnostics") or {}
        attempt_rows.append({
            "campaign_id": V10_ID,
            "job_id": path.parents[1].name,
            "attempt": path.parent.name,
            "worker_index": event.get("worker_index"),
            "recorded_at": event.get("recorded_at"),
            "terminal_failure": path.parents[1].name in terminal_ids,
            "category": _failure_category(message),
            "message": message,
            "call_id": diagnostics.get("call_id", ""),
            "group_id": diagnostics.get("group_id", ""),
            "cleanup_failure": failure.get("cleanup_failure", ""),
            "raw_failure_bytes": path.stat().st_size,
            "raw_failure_sha256": raw_hash,
        })
        raw_rows.append({
            "campaign_id": V10_ID,
            "record_kind": "attempt_failure",
            "job_id": path.parents[1].name,
            "relative_path": str(path.relative_to(repo)),
            "bytes": path.stat().st_size,
            "sha256": raw_hash,
        })
    if len(attempt_rows) != V10_ATTEMPT_FAILURES:
        raise ValueError("unexpected v10 attempt failure count")

    _write_jsonl(
        output / "trial_summaries.jsonl.gz",
        ({"campaign_id": row["campaign_id"], "job_id": row["job_id"], "summary": row["summary"]} for row in records),
    )
    _write_jsonl(
        output / "completion_records.jsonl.gz",
        ({"campaign_id": row["campaign_id"], **row["completion"]} for row in records),
    )
    fields = sorted({key for row in records for key in row["summary"]})
    with _gzip_text(output / "trial_level.csv.gz") as handle:
        writer = csv.DictWriter(handle, fieldnames=["campaign_id", "job_id"] + fields)
        writer.writeheader()
        for row in records:
            writer.writerow({
                "campaign_id": row["campaign_id"],
                "job_id": row["job_id"],
                **{key: _csv_value(value) for key, value in row["summary"].items()},
            })

    attempt_fields = list(attempt_rows[0])
    with (output / "attempt_failures.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=attempt_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(attempt_rows)
    terminal_fields = ["job_id", "algorithm", "arrival_load", "policy_id", "trace_id", "worker_index", "recorded_at", "attempt_failures", "failure_categories"]
    attempts_by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in attempt_rows:
        attempts_by_job[row["job_id"]].append(row)
    with (output / "terminal_failures.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=terminal_fields, lineterminator="\n")
        writer.writeheader()
        for event in sorted(terminal_events, key=lambda row: row["job_id"]):
            job_id = str(event["job_id"])
            job = schedule_jobs[V10_ID][job_id]
            failures = attempts_by_job[job_id]
            writer.writerow({
                "job_id": job_id,
                "algorithm": job["algorithm"],
                "arrival_load": job["load_id"],
                "policy_id": job["policy"]["policy_id"],
                "trace_id": job["trace_id"],
                "worker_index": event["worker_index"],
                "recorded_at": event["recorded_at"],
                "attempt_failures": len(failures),
                "failure_categories": ";".join(sorted({row["category"] for row in failures})),
            })

    status_by_job = {job_id: "completed_v9" for job_id in v9_ids}
    status_by_job.update({job_id: "completed_v10" for job_id in v10_ids})
    status_by_job.update({job_id: "terminal_failure_v10" for job_id in terminal_ids})
    coverage: dict[tuple[str, str, str], Counter[str]] = defaultdict(Counter)
    for job_id, job in schedule_jobs[V9_ID].items():
        coverage[(job["algorithm"], job["load_id"], job["policy"]["policy_id"])][status_by_job[job_id]] += 1
    coverage_fields = ["algorithm", "arrival_load", "policy_id", "planned", "completed_v9", "completed_v10", "completed_total", "terminal_failure_v10"]
    with (output / "matrix_coverage.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=coverage_fields, lineterminator="\n")
        writer.writeheader()
        for key, counts in sorted(coverage.items()):
            writer.writerow({
                "algorithm": key[0], "arrival_load": key[1], "policy_id": key[2],
                "planned": sum(counts.values()),
                "completed_v9": counts["completed_v9"],
                "completed_v10": counts["completed_v10"],
                "completed_total": counts["completed_v9"] + counts["completed_v10"],
                "terminal_failure_v10": counts["terminal_failure_v10"],
            })

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        s = row["summary"]
        groups[(s["algorithm"], s["arrival_load"], s["policy_id"])].append(s)
    condition_fields = ["algorithm", "arrival_load", "policy_id", "metric", "n", "median", "min", "max"]
    with (output / "condition_summaries.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=condition_fields, lineterminator="\n")
        writer.writeheader()
        for key, rows in sorted(groups.items()):
            for metric in SCALAR_METRICS:
                values = [number for row in rows if (number := _number(row.get(metric))) is not None]
                writer.writerow({
                    "algorithm": key[0], "arrival_load": key[1], "policy_id": key[2],
                    "metric": metric, "n": len(values),
                    "median": median(values) if values else "",
                    "min": min(values) if values else "",
                    "max": max(values) if values else "",
                })

    with _gzip_text(output / "raw_artifact_index.csv.gz") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(raw_rows[0]))
        writer.writeheader()
        writer.writerows(sorted(raw_rows, key=lambda row: (row["campaign_id"], row["job_id"], row["relative_path"])))

    provenance = output / "provenance"
    provenance.mkdir()
    for campaign, root in roots.items():
        for name in ("causal_schedule.json", "campaign_events.jsonl", "LIVE_TRACKER.md"):
            source = root / name
            if source.exists():
                shutil.copy2(source, provenance / f"{campaign}__{name}")
    shutil.copy2(audit_path, provenance / audit_path.name)

    complete_blocks = Counter()
    block_policies: dict[str, set[str]] = defaultdict(set)
    for row in records:
        job = row["schedule"]
        block_policies[job["block_id"]].add(job["policy"]["policy_id"])
    for policies in block_policies.values():
        complete_blocks["complete" if policies == {"eager_b1", "count_b4"} else "partial"] += 1
    categories = Counter(row["category"] for row in attempt_rows)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "bundle_id": "corrected_hardware_v9_v10_progress",
        "snapshot_date": "2026-09-07",
        "scope": "compact RP2040 progress snapshot; raw per-call streams remain local and are hash-indexed",
        "planned_trials": PLANNED_JOBS,
        "completed_trials": len(records),
        "remaining_terminal_failed_trials": len(terminal_ids),
        "completed_v9": len(v9_ids),
        "completed_v10": len(v10_ids),
        "complete_eager_count_blocks": complete_blocks["complete"],
        "partial_eager_count_blocks": complete_blocks["partial"],
        "attempt_failure_count": len(attempt_rows),
        "attempt_failure_categories": dict(sorted(categories.items())),
        "latest_successful_completion_at": latest_completion,
        "campaign_source_identities": EXPECTED_IDENTITIES,
        "v9_completion_manifest_sha256": V9_MANIFEST_SHA256,
    }
    generated = sorted(path for path in output.rglob("*") if path.is_file())
    manifest["files"] = {
        str(path.relative_to(output)): {"bytes": path.stat().st_size, "sha256": _sha256(path)}
        for path in generated
    }
    (output / "export_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
