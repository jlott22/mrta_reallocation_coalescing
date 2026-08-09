"""Generate immutable post-rate-calibration stage configs without source edits."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from study.manifests import canonical_json_bytes, generate_manifest_set


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _write_immutable(path: Path, value: Any) -> None:
    data = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != data:
        raise FileExistsError(f"refusing to replace different generated config: {path}")
    if not path.exists():
        path.write_bytes(data)


def _require_inside_repo(path: Path, repo_root: Path, label: str) -> Path:
    resolved = path.resolve()
    root = repo_root.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"{label} must be inside the standalone repository")
    return resolved


def prepare_selected_manifest(
    rate_report_path: Path,
    rate_config_path: Path,
    repo_root: Path,
) -> tuple[dict[str, Any], Path]:
    report = _load(rate_report_path)
    if report.get("report_kind") != "causal_rate_calibration" or report.get("passed") is not True:
        raise ValueError("rate calibration report is not a passing causal report")
    selected = report.get("reviewed_selection")
    if not isinstance(selected, dict) or set(selected) != {"low", "medium", "high"}:
        raise ValueError("reviewed low/medium/high rate selection is required")
    rates = {name: float(selected[name]["rate_per_s"]) for name in ("low", "medium", "high")}
    if not rates["low"] < rates["medium"] < rates["high"]:
        raise ValueError("selected rates are not strictly ordered")
    base = _load(rate_config_path)
    selection_hash = hashlib.sha256(canonical_json_bytes({"rates": rates})).hexdigest()[:10]
    manifest = {
        "manifest_set_id": f"collaborative_visit_g19_t50_n25_causal_calibration_v1_{selection_hash}",
        "root": "study/generated/manifests",
        "master_seed": int(base["manifest"].get("master_seed", 20260808)),
        "final_campaign_master_seed": int(
            base["manifest"].get("final_campaign_master_seed", 2026080905)
        ),
        "trace_count": 25,
        "grid_size": 19,
        "robot_count": 4,
        "task_count": 50,
        "initial_task_count": 8,
        "arrival_loads": rates,
        "arrival_rate_units": "tasks_per_mission_second",
        "design_status": "reviewed_native_rate_selection_pending_timeout_and_variance",
    }
    root = generate_manifest_set({"manifest": manifest}, repo_root)
    return manifest, root


def _hardware(base: dict[str, Any]) -> dict[str, Any]:
    hardware = dict(base["hardware"])
    hardware["development_override"] = False
    return hardware


def timeout_config(
    rate_report_path: Path,
    rate_config_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    manifest, _ = prepare_selected_manifest(rate_report_path, rate_config_path, repo_root)
    base = _load(rate_config_path)
    return {
        "schema_version": 1,
        "manifest": manifest,
        "campaign": {
            "campaign_id": f"agx_causal_timeout_calibration_{manifest['manifest_set_id'][-10:]}",
            "stage": "calibrate_timeout",
            "output_root": f"study/output/agx_causal_timeout_calibration_{manifest['manifest_set_id'][-10:]}",
            "algorithms": ["CBAA", "ACBBA", "PI", "HIPC"],
            "loads": ["low", "medium", "high"],
            "policies": [
                {"policy_id": "eager_b1", "mode": "eager", "batch_size": 1},
                {"policy_id": "bounded_b4_w2", "mode": "bounded", "batch_size": 4, "max_pending_age_s": 2.0},
                {"policy_id": "bounded_b4_w5", "mode": "bounded", "batch_size": 4, "max_pending_age_s": 5.0},
                {"policy_id": "bounded_b4_w10", "mode": "bounded", "batch_size": 4, "max_pending_age_s": 10.0},
                {"policy_id": "bounded_b4_w20", "mode": "bounded", "batch_size": 4, "max_pending_age_s": 20.0}
            ],
            "trace_limit": 5,
            "schedule_seed": 2026081003,
            "max_technical_retries": 1,
            "runner_factory": "study.causal.worker:run_causal_job",
            "required_gate_paths": [
                "study/native_gates/environment/native_environment_report.json",
                "study/native_gates/preflight/native_preflight_report.json",
                "study/native_gates/smoke/causal_smoke_report.json",
                str(rate_report_path.resolve().relative_to(repo_root)),
            ],
            "require_clean_source": True,
            "notes": "3 loads x 4 algorithms x (Eager + W=2/5/10/20) x 5 traces = 300 causal missions."
        },
        "hardware": _hardware(base),
    }


def variance_config(
    timeout_config_path: Path,
    timeout_report_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    base = _load(timeout_config_path)
    report = _load(timeout_report_path)
    if report.get("report_kind") != "causal_timeout_calibration" or report.get("passed") is not True:
        raise ValueError("timeout calibration report is not a passing causal report")
    timeout = report.get("reviewed_timeout_s")
    if not isinstance(timeout, (int, float)) or float(timeout) <= 0.0:
        raise ValueError("reviewed timeout selection is required before variance pilot")
    suffix = base["manifest"]["manifest_set_id"][-10:]
    return {
        "schema_version": 1,
        "manifest": base["manifest"],
        "campaign": {
            "campaign_id": f"agx_causal_variance_v1_{suffix}",
            "stage": "variance",
            "output_root": f"study/output/agx_causal_variance_v1_{suffix}",
            "algorithms": ["CBAA", "ACBBA", "PI", "HIPC"],
            "loads": ["low", "medium", "high"],
            "policies": [
                {"policy_id": "eager_b1", "mode": "eager", "batch_size": 1},
                {"policy_id": "count_b4", "mode": "count", "batch_size": 4}
            ],
            "trace_limit": 10,
            "schedule_seed": 2026081004,
            "max_technical_retries": 1,
            "runner_factory": "study.causal.worker:run_causal_job",
            "required_gate_paths": [
                "study/native_gates/environment/native_environment_report.json",
                "study/native_gates/preflight/native_preflight_report.json",
                "study/native_gates/smoke/causal_smoke_report.json",
                "study/native_gates/calibration/rate_calibration_report.json",
                str(timeout_report_path.resolve().relative_to(repo_root)),
            ],
            "require_clean_source": True,
            "reviewed_timeout_s_for_later_freeze": float(timeout),
            "notes": "3 loads x 4 algorithms x Eager/B4 x 10 traces = 240 causal missions."
        },
        "hardware": _hardware(base),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    timeout = sub.add_parser("timeout")
    timeout.add_argument("--rate-report", type=Path, required=True)
    timeout.add_argument("--rate-config", type=Path, required=True)
    timeout.add_argument("--repo-root", type=Path, default=Path("."))
    timeout.add_argument("--output", type=Path, required=True)
    variance = sub.add_parser("variance")
    variance.add_argument("--timeout-config", type=Path, required=True)
    variance.add_argument("--timeout-report", type=Path, required=True)
    variance.add_argument("--repo-root", type=Path, default=Path("."))
    variance.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.repo_root.resolve()
    if args.command == "timeout":
        config = timeout_config(args.rate_report.resolve(), args.rate_config.resolve(), root)
    else:
        config = variance_config(args.timeout_config.resolve(), args.timeout_report.resolve(), root)
    output = _require_inside_repo(args.output, root, "generated stage config")
    _write_immutable(output, config)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
