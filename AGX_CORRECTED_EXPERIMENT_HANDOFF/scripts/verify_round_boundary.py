#!/usr/bin/env python3
"""Produce a concise, non-filtering checkpoint before corrected Round 2."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, action="append", required=True)
    parser.add_argument("--round2-config", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    report: dict[str, object] = {
        "checkpoint_role": "informational_only_no_round2_filtering",
        "round1": [],
        "round2_design_checks": [],
    }
    for config_path in args.config:
        cfg = _load(config_path)
        root = args.repo_root / cfg["campaign"]["output_root"]
        completed = root / "completed"
        report["round1"].append(
            {
                "campaign_id": cfg["campaign"]["campaign_id"],
                "expected_jobs": 1500,
                "completed_directories": len(list(completed.glob("*"))) if completed.exists() else 0,
                "analysis_present": (root / "analysis").exists(),
            }
        )

    for config_path in args.round2_config:
        cfg = _load(config_path)
        campaign = cfg["campaign"]
        exclusions = campaign.get("job_exclusions", [])
        excluded_trace_count = sum(len(item.get("trace_ids", [])) for item in exclusions)
        expected_excluded_jobs = len(campaign["algorithms"]) * len(campaign["loads"]) * len(campaign["policies"]) * excluded_trace_count
        report["round2_design_checks"].append(
            {
                "campaign_id": campaign["campaign_id"],
                "has_job_allowlist": "job_allowlist" in campaign,
                "condition_count": len(campaign["algorithms"]) * len(campaign["loads"]) * len(campaign["policies"]),
                "trace_limit": campaign["trace_limit"],
                "excluded_trace_count": excluded_trace_count,
                "excluded_job_count": expected_excluded_jobs,
                "declared_excluded_job_count": campaign.get("expected_excluded_job_count"),
            }
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))

    # Only a malformed design stops Round 2. Outcomes and incomplete missions do not.
    malformed = any(
        item["has_job_allowlist"]
        or item["condition_count"] != 60
        or item["trace_limit"] != 50
        or item["excluded_trace_count"] != 25
        or item["excluded_job_count"] != 1500
        or item["declared_excluded_job_count"] != 1500
        for item in report["round2_design_checks"]
    )
    return 2 if malformed else 0


if __name__ == "__main__":
    raise SystemExit(main())
