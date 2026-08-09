"""Explicitly freeze a reviewed causal experimental design.

This command never chooses rates or a timeout by optimizing policy outcomes.
It consumes reviewed native calibration reports, requires an explicit operator
acceptance flag, generates a new immutable final manifest/config, and seals all
input hashes into a design-freeze record.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from study.manifests import (
    canonical_json_bytes,
    generate_manifest_set,
    sha256_file,
    validate_manifest_set,
)

from .gates import GateError, validate_gate_set
from .model import PRIMARY_ALGORITHMS, load_causal_config


ANALYSIS_VERSION = "causal-analysis-v1"
REQUIRED_GATE_KINDS = (
    "native_environment_check",
    "rp2040_parity_preflight",
    "causal_hardware_smoke",
    "causal_rate_calibration",
    "causal_timeout_calibration",
    "causal_variance_pilot",
)


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"cannot read JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise GateError(f"expected JSON object: {path}")
    return value


def _write_immutable(path: Path, value: Any) -> None:
    data = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != data:
        raise FileExistsError(f"refusing to replace a different frozen artifact: {path}")
    if not path.exists():
        path.write_bytes(data)


def _git_head(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise GateError("cannot determine Git HEAD")
    return completed.stdout.strip()


def _source_hash(repo_root: Path) -> str:
    digest = hashlib.sha256()
    roots = (repo_root / "known_visit_sim", repo_root / "study", repo_root / "Simulation")
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if any(part in {"output", "generated", "builds", "__pycache__"} for part in path.parts):
                continue
            relative = path.relative_to(repo_root).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            data = path.read_bytes()
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
    return digest.hexdigest()


def _manifest_content_signatures(root: Path, index: dict[str, Any]) -> set[str]:
    signatures: set[str] = set()
    for entry in index["entries"]:
        raw = _load(root / entry["path"])
        if entry["manifest_kind"] == "scenario":
            content = {
                "kind": "scenario",
                "grid_size": raw.get("grid_size"),
                "robot_starts": raw.get("robot_starts"),
                "tasks": raw.get("tasks"),
            }
        else:
            content = {
                "kind": "release",
                "load_id": raw.get("load_id"),
                "rate_per_s": raw.get("rate_per_s"),
                "tasks": raw.get("tasks"),
            }
        signatures.add(hashlib.sha256(canonical_json_bytes(content)).hexdigest())
    return signatures


def _require_gate_identity(
    base: Any,
    gates: dict[str, Any],
    *,
    git_head: str,
    source_tree_sha256: str,
) -> None:
    """Bind every reviewed gate to this source and physical board cohort."""

    if set(gates) != set(REQUIRED_GATE_KINDS):
        raise GateError("design freeze requires exactly the six declared native gate kinds")
    environment = gates["native_environment_check"].report
    preflight = gates["rp2040_parity_preflight"].report
    smoke = gates["causal_hardware_smoke"].report
    rate = gates["causal_rate_calibration"].report
    timeout = gates["causal_timeout_calibration"].report
    variance = gates["causal_variance_pilot"].report
    if environment.get("git", {}).get("head") != git_head:
        raise GateError("environment gate Git commit differs from freeze source")
    if environment.get("source_tree_sha256") != source_tree_sha256:
        raise GateError("environment gate source hash differs from freeze source")
    if preflight.get("repository_commit") != git_head:
        raise GateError("preflight Git commit differs from freeze source")
    if (
        preflight.get("config_sha256") != environment.get("config_sha256")
        or preflight.get("manifest_sha256")
        != environment.get("manifest_index_sha256")
    ):
        raise GateError("environment and preflight do not bind the same config/manifest")

    expected_boards = {board.board_id: board for board in base.boards}
    expected_uids = {board.expected_device_uid: board for board in base.boards}
    if len(expected_boards) != 4 or len(expected_uids) != 4:
        raise GateError("freeze requires four unique board labels and device UIDs")
    preflight_boards = preflight.get("boards")
    if not isinstance(preflight_boards, list) or {
        str(row.get("device_id")) for row in preflight_boards
    } != set(expected_uids):
        raise GateError("preflight board UID cohort differs from frozen bindings")
    for row in preflight_boards:
        board = expected_uids[str(row["device_id"])]
        expected = {
            "build_id": board.expected_build_id,
            "firmware_sha256": board.expected_firmware_sha256,
            "module_set_sha256": board.expected_module_set_sha256,
        }
        for name, value in expected.items():
            if row.get(name) != value:
                raise GateError(f"preflight {name} differs for {board.board_id}")

    environment_bindings = environment.get("serial_bindings")
    if not isinstance(environment_bindings, list) or {
        str(row.get("board_id")) for row in environment_bindings
    } != set(expected_boards):
        raise GateError("environment board labels differ from frozen bindings")
    for row in environment_bindings:
        board = expected_boards[str(row["board_id"])]
        for name, value in board.to_dict().items():
            if row.get(name) != value:
                raise GateError(f"environment board binding differs: {board.board_id}/{name}")

    identity_reports = {
        "smoke": smoke.get("input_identity"),
        "rate": rate.get("input_identity"),
        "timeout": timeout.get("input_identity"),
        "variance": variance.get("input_identity"),
    }
    for name, identity in identity_reports.items():
        if not isinstance(identity, dict):
            raise GateError(f"{name} gate lacks sealed input identity")
        if identity.get("git_head") != git_head:
            raise GateError(f"{name} gate Git commit differs from freeze source")
        if identity.get("source_tree_sha256") != source_tree_sha256:
            raise GateError(f"{name} gate source hash differs from freeze source")
        if identity.get("hardware_binding_sha256") != base.hardware_binding_sha256:
            raise GateError(f"{name} gate hardware binding differs from freeze cohort")

    smoke_identity = identity_reports["smoke"]
    rate_identity = identity_reports["rate"]
    timeout_identity = identity_reports["timeout"]
    variance_identity = identity_reports["variance"]
    if smoke_identity.get("manifest_index_sha256") != environment.get("manifest_index_sha256"):
        raise GateError("smoke manifest differs from environment/preflight")
    if rate_identity.get("manifest_index_sha256") != smoke_identity.get("manifest_index_sha256"):
        raise GateError("rate sweep and smoke do not use the same provisional manifest")
    if timeout_identity.get("manifest_index_sha256") != base.manifest_index_sha256:
        raise GateError("timeout calibration manifest differs from freeze base")
    if variance_identity.get("manifest_index_sha256") != base.manifest_index_sha256:
        raise GateError("variance calibration manifest differs from freeze base")
    if variance_identity.get("config_sha256") != base.config_sha256:
        raise GateError("variance calibration did not use the selected freeze-base config")

    expected_device_rows = {
        board_id: {
            "expected_device_uid": board.expected_device_uid,
            "expected_device_build_id": board.expected_build_id,
            "expected_device_firmware_sha256": board.expected_firmware_sha256,
            "expected_device_module_set_sha256": board.expected_module_set_sha256,
        }
        for board_id, board in expected_boards.items()
    }
    for name, identity in identity_reports.items():
        if identity.get("boards") not in (expected_device_rows, [
            board.to_dict() for board in base.boards
        ]):
            raise GateError(f"{name} gate board identities differ from freeze cohort")


def freeze_design(
    base_config_path: Path,
    repo_root: Path,
    gate_paths: list[Path],
    output_dir: Path,
    *,
    accept_reviewed_proposals: bool,
) -> tuple[Path, Path, Path]:
    if not accept_reviewed_proposals:
        raise GateError("design freeze requires --accept-reviewed-proposals")
    repo_root = repo_root.resolve()
    output_dir = output_dir.resolve()
    if output_dir == repo_root or repo_root not in output_dir.parents:
        raise GateError("design-freeze output directory must be inside the repository")
    base = load_causal_config(base_config_path, repo_root)
    gates = validate_gate_set(gate_paths, required_kinds=REQUIRED_GATE_KINDS)
    rate_report = gates["causal_rate_calibration"].report
    timeout_report = gates["causal_timeout_calibration"].report
    variance_report = gates["causal_variance_pilot"].report
    selection = rate_report.get("reviewed_selection")
    if not isinstance(selection, dict) or set(selection) != {"low", "medium", "high"}:
        raise GateError("rate report lacks reviewed low/medium/high selection")
    rates: dict[str, float] = {}
    for name in ("low", "medium", "high"):
        entry = selection[name]
        if not isinstance(entry, dict) or not isinstance(entry.get("rate_per_s"), (int, float)):
            raise GateError(f"invalid reviewed rate selection for {name}")
        rates[name] = float(entry["rate_per_s"])
    if not (0.0 < rates["low"] < rates["medium"] < rates["high"]):
        raise GateError("reviewed rates must be positive and ordered low < medium < high")
    rate_proposal = rate_report.get("selection_proposal")
    reviewed_rate_ids = {
        name: selection[name].get("load_id") for name in ("low", "medium", "high")
    }
    if rate_proposal is not None and (
        not isinstance(rate_proposal, dict)
        or set(rate_proposal) != {"low", "medium", "high"}
        or not all(isinstance(rate_proposal[name], dict) for name in rate_proposal)
    ):
        raise GateError("rate report selection proposal is malformed")
    proposed_rate_ids = (
        None
        if rate_proposal is None
        else {
            name: rate_proposal[name].get("load_id")
            for name in ("low", "medium", "high")
        }
    )
    rate_justification = rate_report.get("reviewed_selection_justification")
    if reviewed_rate_ids != proposed_rate_ids and not (
        isinstance(rate_justification, str) and rate_justification.strip()
    ):
        raise GateError("reviewed rates override the proposal without justification")
    base_rates = {
        str(name): float(value)
        for name, value in base.raw["manifest"].get("arrival_loads", {}).items()
    }
    if base_rates != rates:
        raise GateError("freeze-base manifest rates differ from reviewed rate selection")
    timeout_s = timeout_report.get("reviewed_timeout_s")
    if not isinstance(timeout_s, (int, float)) or float(timeout_s) <= 0.0:
        raise GateError("timeout report lacks a positive reviewed_timeout_s")
    if float(base.raw["campaign"].get("reviewed_timeout_s_for_later_freeze", -1.0)) != float(timeout_s):
        raise GateError("freeze-base timeout differs from reviewed timeout selection")
    timeout_justification = timeout_report.get("reviewed_timeout_justification")
    if timeout_s != timeout_report.get("selection_proposal_s") and not (
        isinstance(timeout_justification, str) and timeout_justification.strip()
    ):
        raise GateError("reviewed timeout overrides the proposal without justification")
    trace_count = variance_report.get("reviewed_trace_count")
    if (
        not isinstance(trace_count, int)
        or isinstance(trace_count, bool)
        or trace_count not in {25, 50}
    ):
        raise GateError(
            "variance report reviewed_trace_count must be a predeclared 25 or 50"
        )
    trace_proposal = variance_report.get("trace_count_proposal")
    trace_justification = variance_report.get(
        "reviewed_trace_count_justification"
    )
    if trace_count != trace_proposal and not (
        isinstance(trace_justification, str) and trace_justification.strip()
    ):
        raise GateError(
            "a final trace count that differs from the variance proposal lacks "
            "a scientific justification"
        )
    calibration_seed = int(base.raw["manifest"].get("master_seed", 20260808))
    final_seed_raw = base.raw["manifest"].get("final_campaign_master_seed")
    if isinstance(final_seed_raw, bool) or not isinstance(final_seed_raw, int):
        raise GateError("base calibration config lacks a predeclared final_campaign_master_seed")
    final_seed = int(final_seed_raw)
    if final_seed == calibration_seed:
        raise GateError("final campaign master seed must differ from every calibration cohort seed")
    # The design identity is derived from reviewed scientific choices and all
    # hardware/native gate bytes. Re-running with identical evidence is stable.
    evidence = {
        kind: {"path": str(gate.path), "sha256": gate.sha256, "kind": kind}
        for kind, gate in sorted(gates.items())
    }
    identity_material = {
        "schema_version": 1,
        "rates": rates,
        "timeout_s": float(timeout_s),
        "trace_count": trace_count,
        "calibration_master_seed": calibration_seed,
        "final_campaign_master_seed": final_seed,
        "gate_sha256": {kind: gate.sha256 for kind, gate in sorted(gates.items())},
        "git_head": _git_head(repo_root),
        "source_tree_sha256": _source_hash(repo_root),
        "analysis_version": ANALYSIS_VERSION,
    }
    if environment_binding := gates["native_environment_check"].report.get(
        "hardware_binding_sha256"
    ):
        if environment_binding != base.hardware_binding_sha256:
            raise GateError("environment gate hardware binding differs from freeze base")
    else:
        raise GateError("environment gate lacks hardware binding identity")
    _require_gate_identity(
        base,
        gates,
        git_head=identity_material["git_head"],
        source_tree_sha256=identity_material["source_tree_sha256"],
    )
    design_digest = hashlib.sha256(canonical_json_bytes(identity_material)).hexdigest()
    design_id = f"causal_design_{design_digest[:12]}"
    manifest_id = f"collaborative_visit_g19_t50_n{trace_count}_{design_id}"
    manifest_config = {
        "manifest": {
            "manifest_set_id": manifest_id,
            "root": "study/generated/manifests",
            "master_seed": final_seed,
            "calibration_master_seed": calibration_seed,
            "trace_count": trace_count,
            "grid_size": 19,
            "robot_count": 4,
            "task_count": 50,
            "initial_task_count": 8,
            "arrival_loads": rates,
            "arrival_rate_units": "tasks_per_mission_second",
            "design_status": "native_causal_reviewed_and_frozen",
        }
    }
    manifest_root = generate_manifest_set(manifest_config, repo_root)
    manifest_index_hash = sha256_file(manifest_root / "manifest_index.json")
    calibration_index = validate_manifest_set(base.manifest_root)
    final_index = validate_manifest_set(manifest_root)
    calibration_hashes = {
        str(entry["sha256"]) for entry in calibration_index["entries"]
    }
    final_hashes = {str(entry["sha256"]) for entry in final_index["entries"]}
    overlapping_manifest_hashes = sorted(calibration_hashes & final_hashes)
    if overlapping_manifest_hashes:
        raise GateError(
            "final evaluation cohort overlaps calibration manifest bytes; "
            "use a disjoint predeclared master seed"
        )
    overlapping_content_signatures = sorted(
        _manifest_content_signatures(base.manifest_root, calibration_index)
        & _manifest_content_signatures(manifest_root, final_index)
    )
    if overlapping_content_signatures:
        raise GateError(
            "final evaluation cohort repeats calibration scenario/release content"
        )
    full_config = {
        "schema_version": 1,
        "manifest": manifest_config["manifest"],
        "campaign": {
            "campaign_id": f"agx_full_{design_id}",
            "stage": "full",
            "output_root": f"study/output/agx_full_{design_id}",
            "algorithms": list(PRIMARY_ALGORITHMS),
            "loads": ["low", "medium", "high"],
            "policies": [
                {"policy_id": "eager_b1", "mode": "eager", "batch_size": 1},
                {"policy_id": "count_b2", "mode": "count", "batch_size": 2},
                {"policy_id": "count_b4", "mode": "count", "batch_size": 4},
                {"policy_id": "count_b8", "mode": "count", "batch_size": 8},
                {
                    "policy_id": f"bounded_b4_w{str(float(timeout_s)).replace('.', 'p')}",
                    "mode": "bounded",
                    "batch_size": 4,
                    "max_pending_age_s": float(timeout_s),
                },
            ],
            "schedule_seed": 2026080904,
            "max_technical_retries": 1,
            "runner_factory": "study.causal.worker:run_causal_job",
            "required_gate_paths": [
                str(gate.path.relative_to(repo_root)) for gate in gates.values()
            ],
            "design_freeze_path": str((output_dir / "design_freeze.json").relative_to(repo_root)),
            "require_exactly_four_workers": True,
            "require_clean_source": True,
        },
        "hardware": {
            "development_override": False,
            "core_affinities": list(base.core_affinities),
            "boards": [board.to_dict() for board in base.boards],
            "bindings_file_sha256": base.hardware_binding_sha256,
            "provider_factory": base.provider_factory,
            "call_timeout_s": base.device_timeout_seconds,
        },
    }
    config_path = output_dir / "agx_full_causal_frozen.json"
    freeze_path = output_dir / "design_freeze.json"
    markdown_path = output_dir / "FINAL_DESIGN_FREEZE.md"
    _write_immutable(config_path, full_config)
    frozen_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if freeze_path.is_file():
        existing_freeze = _load(freeze_path)
        if existing_freeze.get("design_id") == design_id:
            # Repeating an identical accepted freeze is idempotent.  Preserve
            # the original audit timestamp so immutable bytes remain stable.
            frozen_at = str(existing_freeze.get("frozen_at", frozen_at))
    freeze = {
        "schema_version": 1,
        "report_kind": "final_design_freeze",
        "passed": True,
        "hardware_validated": True,
        "design_id": design_id,
        "frozen_at": frozen_at,
        "research_design": {
            "mission": "Collaborative Visit",
            "grid_size": 19,
            "robot_count": 4,
            "task_count": 50,
            "initial_task_count": 8,
            "algorithms": list(PRIMARY_ALGORITHMS),
            "loads_tasks_per_mission_second": rates,
            "policies": full_config["campaign"]["policies"],
            "paired_trace_count": trace_count,
            "calibration_master_seed": calibration_seed,
            "final_campaign_master_seed": final_seed,
            "calibration_and_final_manifest_hash_overlap_count": 0,
            "calibration_and_final_content_overlap_count": 0,
            "causal_trial_count": 4 * 3 * 5 * trace_count,
            "rate_review_justification": rate_justification,
            "timeout_review_justification": timeout_justification,
            "trace_count_review_justification": trace_justification,
        },
        "manifest_set_id": manifest_id,
        "manifest_index_sha256": manifest_index_hash,
        "hardware_binding_sha256": base.hardware_binding_sha256,
        "full_config_path": str(config_path.relative_to(repo_root)),
        "full_config_sha256": hashlib.sha256(canonical_json_bytes(full_config)).hexdigest(),
        "device_build_id_by_board": {
            board.board_id: board.expected_build_id for board in base.boards
        },
        "device_uid_by_board": {
            board.board_id: board.expected_device_uid for board in base.boards
        },
        "device_module_set_sha256_by_board": {
            board.board_id: board.expected_module_set_sha256 for board in base.boards
        },
        "device_firmware_sha256_by_board": {
            board.board_id: board.expected_firmware_sha256 for board in base.boards
        },
        "code_commit": identity_material["git_head"],
        "source_tree_sha256": identity_material["source_tree_sha256"],
        "analysis_version": ANALYSIS_VERSION,
        "sealed_gates": evidence,
        "values_will_not_be_regenerated_or_recalibrated_by_full_runner": True,
    }
    _write_immutable(freeze_path, freeze)
    markdown = f"""# Final Causal Experimental Design Freeze

Design ID: `{design_id}`
Status: **FROZEN / PASS**
Git commit: `{freeze['code_commit']}`
Source hash: `{freeze['source_tree_sha256']}`

## Fixed factors

- Algorithms: {', '.join(PRIMARY_ALGORITHMS)}
- Rates (tasks/mission-second): low={rates['low']}, medium={rates['medium']}, high={rates['high']}
- Policies: Eager/B1, B2, B4, B8, bounded B4/W={float(timeout_s)} s
- Paired traces: {trace_count}
- Causal missions: {freeze['research_design']['causal_trial_count']}
- Workers/boards: exactly four, one stable RP2040 binding per worker

Manifest index SHA-256: `{manifest_index_hash}`
Full config SHA-256: `{freeze['full_config_sha256']}`

Every input gate and its byte hash is recorded in `design_freeze.json`. The full
launcher validates those bytes and refuses to recalibrate or regenerate values.
"""
    if markdown_path.exists() and markdown_path.read_text(encoding="utf-8") != markdown:
        raise FileExistsError(f"refusing to replace a different frozen report: {markdown_path}")
    if not markdown_path.exists():
        markdown_path.write_text(markdown, encoding="utf-8", newline="\n")
    return config_path, freeze_path, markdown_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-config", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--gate", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--accept-reviewed-proposals", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.repo_root.resolve()
    paths = freeze_design(
        args.base_config,
        root,
        [path.resolve() for path in args.gate],
        args.output_dir.resolve(),
        accept_reviewed_proposals=args.accept_reviewed_proposals,
    )
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
