#!/usr/bin/env python3
"""Fail closed if the corrected handoff CPU topology drifts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


EXPECTED_RP2040_CORES = [0, 1, 2, 3]
EXPECTED_AGX_CORES = [4, 5, 6, 7, 8]
EXPECTED_SUPPORT_CORES = {
    "supervisor": 9,
    "checkpoint_analysis": 10,
    "os_reserve": 11,
}
ROUND_CONFIGS = (
    "round1_causal.json",
    "round1_zero.json",
    "round2_causal.json",
    "round2_zero.json",
    "agx_smoke_8.json",
)


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def _require(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise ValueError(f"{label} must be {expected!r}, found {actual!r}")


def _planned_jobs(campaign: dict[str, Any]) -> int:
    return (
        len(campaign.get("algorithms", []))
        * len(campaign.get("loads", []))
        * len(campaign.get("policies", []))
        * int(campaign.get("trace_limit", 0))
    )


def validate(repo_root: Path, *, require_hardware_bindings: bool) -> None:
    handoff = repo_root / "AGX_CORRECTED_EXPERIMENT_HANDOFF"
    matrix = _load(handoff / "experiment_matrix.json")
    agx = matrix.get("agx")
    rp2040 = matrix.get("rp2040")
    mission = matrix.get("mission")
    support = matrix.get("support_cores")
    smoke = matrix.get("engineering_smoke")
    if not all(isinstance(value, dict) for value in (agx, rp2040, mission, support, smoke)):
        raise ValueError("experiment matrix lacks required topology sections")

    _require(agx.get("simulation_cores"), EXPECTED_AGX_CORES, "AGX simulation cores")
    _require(rp2040.get("worker_cores"), EXPECTED_RP2040_CORES, "RP2040 worker cores")
    _require(support, EXPECTED_SUPPORT_CORES, "support cores")
    _require(agx.get("total_jobs"), 6000, "AGX matrix job count")
    _require(rp2040.get("total_missions"), 96, "RP2040 matrix mission count")
    _require(mission.get("robot_count"), 4, "logical robots per mission")
    _require(smoke, {"agx_jobs": 8, "rp2040_missions": 8, "inferential": False}, "engineering smoke matrix")

    compute = EXPECTED_RP2040_CORES + EXPECTED_AGX_CORES
    if len(compute) != 9 or len(set(compute)) != 9 or set(compute) != set(range(9)):
        raise ValueError("compute affinity set must be the nine disjoint cores 0-8")
    if set(compute) & set(EXPECTED_SUPPORT_CORES.values()):
        raise ValueError("compute and support affinity sets overlap")

    configs = handoff / "configs"
    for name in ROUND_CONFIGS:
        campaign = _load(configs / name).get("campaign")
        if not isinstance(campaign, dict):
            raise ValueError(f"{name} lacks a campaign object")
        _require(campaign.get("max_workers"), len(EXPECTED_AGX_CORES), f"{name} max_workers")
    agx_smoke = _load(configs / "agx_smoke_8.json").get("campaign")
    if not isinstance(agx_smoke, dict):
        raise ValueError("AGX smoke config lacks a campaign object")
    _require(_planned_jobs(agx_smoke), 8, "AGX smoke job count")
    if "Engineering-only" not in str(agx_smoke.get("notes", "")):
        raise ValueError("AGX smoke must be explicitly engineering-only")

    hardware_smoke = _load(configs / "rp2040_smoke_8.json").get("campaign")
    hardware_core = _load(configs / "hardware_core_96.json").get("campaign")
    if not isinstance(hardware_smoke, dict) or not isinstance(hardware_core, dict):
        raise ValueError("hardware configs lack campaign objects")
    _require(hardware_smoke.get("stage"), "smoke", "RP2040 smoke stage")
    _require(hardware_smoke.get("trace_limit"), 1, "RP2040 smoke trace limit")
    _require(_planned_jobs(hardware_smoke), 8, "RP2040 smoke mission count")
    _require(
        hardware_smoke.get("required_gate_paths"),
        [
            "study/native_gates/environment/native_environment_report.json",
            "study/native_gates/preflight/native_preflight_report.json",
        ],
        "RP2040 smoke gate paths",
    )
    _require(hardware_core.get("trace_limit"), 4, "RP2040 core trace limit")
    _require(_planned_jobs(hardware_core), 96, "RP2040 core mission count")
    _require(
        hardware_core.get("required_gate_paths"),
        [
            "study/native_gates/environment/native_environment_report.json",
            "study/native_gates/preflight/native_preflight_report.json",
            "study/native_gates/smoke/causal_smoke_report.json",
        ],
        "RP2040 core gate paths",
    )

    if require_hardware_bindings:
        binding = _load(repo_root / "configs/local/agx_board_bindings.json")
        boards = binding.get("boards")
        if not isinstance(boards, list) or len(boards) != len(EXPECTED_RP2040_CORES):
            raise ValueError("hardware launch requires exactly four sealed board bindings")
        _require(binding.get("core_affinities"), EXPECTED_RP2040_CORES, "bound RP2040 cores")
        identities = [row.get("expected_device_uid") for row in boards if isinstance(row, dict)]
        if len(identities) != len(EXPECTED_RP2040_CORES) or len(set(identities)) != len(identities):
            raise ValueError("hardware launch requires four distinct sealed board identities")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--require-hardware-bindings", action="store_true")
    args = parser.parse_args(argv)
    try:
        validate(args.repo_root.resolve(), require_hardware_bindings=args.require_hardware_bindings)
    except ValueError as error:
        print(f"Corrected handoff topology validation failed: {error}", file=sys.stderr)
        return 2
    print("Corrected handoff topology valid: 4 RP2040 workers + 5 AGX workers = 9 compute cores.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
