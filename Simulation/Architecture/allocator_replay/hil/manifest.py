from __future__ import annotations

import csv
import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from allocator_replay.config.study import (
    ALGORITHMS,
    BAYESIAN_SIM_ROOT,
    BAYESIAN_SEED,
    COLLABORATIVE_SIM_ROOT,
    COLLABORATIVE_SEED,
    REPOSITORY_ROOT,
    Condition,
    conditions,
)
from allocator_replay.host.deployment import load_build


HIL_ROOT = REPOSITORY_ROOT / "Results" / "HIL" / "AllocatorReplay" / "ActiveCampaigns"
BAYESIAN_SCENARIOS = BAYESIAN_SIM_ROOT / "scenarios" / "final_trial_500.csv"
COLLABORATIVE_MANIFEST = (
    REPOSITORY_ROOT / "Results" / "Simulation" / "Sensitivity" / "_cv100"
    / "campaign_manifest.json"
)
BAYESIAN_TRIAL_COUNT = 500
BAYESIAN_SAMPLE_COUNT = 25
BAYESIAN_MIN_POST_CLUE_CALLS = 3
HIL_MANIFEST_SCHEMA = 2
HIL_IMPLEMENTATION_ID = "pololu_native_persistent_hil_v2"
INVALIDATION_FILENAME = "INVALID_FOR_ANALYSIS.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_tree_provenance(root: Path) -> dict[str, Any]:
    """Hash executable Python source without depending on absolute paths."""

    root = root.resolve()
    files = sorted(
        (
            path
            for path in root.rglob("*.py")
            if "__pycache__" not in path.parts and "tests" not in path.parts
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    digest = hashlib.sha256()
    entries: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        file_digest = sha256_file(path)
        entries[relative] = file_digest
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\n")
    return {
        "root": str(root),
        "sha256": digest.hexdigest(),
        "file_count": len(files),
        "files": entries,
    }


def implementation_provenance(
    build_root: Path | None = None,
) -> dict[str, Any]:
    """Return the complete, immutable implementation identity for a campaign."""

    resolved_build, build = load_build(build_root)
    build_manifest = resolved_build / "manifest.json"
    return {
        "implementation_id": HIL_IMPLEMENTATION_ID,
        "authoritative_allocator_factory": (
            "replay_physical_factory.create_complete_runtime"
        ),
        "state_model": "one_resident_robot_context_with_host_context_swaps",
        "timed_region": "device_choose_goal_only",
        "device_build": {
            "build_id": str(build["build_id"]),
            "manifest_path": str(build_manifest.resolve()),
            "manifest_sha256": sha256_file(build_manifest),
            "compatibility": str(build["compatibility"]),
            "optimization": int(build["optimization"]),
            "source_bundle_sha256": str(build["source_bundle_sha256"]),
            "deployed_module_set_sha256": str(
                build["deployed_module_set_sha256"]
            ),
        },
        "host_source": _source_tree_provenance(
            REPOSITORY_ROOT / "Simulation" / "Architecture" / "allocator_replay"
        ),
        "simulator_sources": {
            "bayesian": _source_tree_provenance(BAYESIAN_SIM_ROOT),
            "collaborative": _source_tree_provenance(COLLABORATIVE_SIM_ROOT),
        },
    }


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _sample(population: list[int] | range, count: int, seed: int) -> list[int]:
    values = list(population)
    if len(values) < count:
        raise ValueError(
            f"cannot sample {count} trials from {len(values)} eligible trials"
        )
    return sorted(random.Random(seed).sample(values, count))


def _collaborative_scenarios() -> dict[int, dict[str, str]]:
    value = json.loads(COLLABORATIVE_MANIFEST.read_text(encoding="utf-8"))
    result: dict[int, dict[str, str]] = {}
    for job in value["jobs"]:
        trial_id = int(job["trial_id"])
        path = Path(job["scenario_file"]).resolve()
        if not path.exists():
            parts = path.parts
            try:
                campaign_index = parts.index("_cv100")
            except ValueError:
                pass
            else:
                path = COLLABORATIVE_MANIFEST.parent.joinpath(
                    *parts[campaign_index + 1 :]
                ).resolve()
        item = {
            "path": str(path),
            "sha256": str(job["scenario_sha256"]),
        }
        prior = result.get(trial_id)
        if prior is not None and prior["sha256"] != item["sha256"]:
            raise ValueError(
                f"collaborative trial {trial_id} has inconsistent scenario files"
            )
        if prior is None:
            result[trial_id] = item
    if set(result) != set(range(100)):
        raise ValueError("historical collaborative manifest must contain trials 0-99")
    for trial_id, item in result.items():
        path = Path(item["path"])
        if not path.exists() or sha256_file(path) != item["sha256"]:
            raise ValueError(f"collaborative scenario hash mismatch for trial {trial_id}")
    return result


def _baseline_path(condition: Condition) -> str:
    if condition.top_k_level in {"K=1", "K=2", "1%", "3%"}:
        return ""
    rate = int(round(condition.top_k_rate * 100))
    algorithm = condition.algorithm.lower()
    if condition.mission == "bayesian":
        path = (
            REPOSITORY_ROOT / "Results" / "Simulation" / "Bayesian"
            / "primary_topk_campaign" / "combined"
            / f"{algorithm}_topk_{rate}" / "system_performance.csv"
        )
    else:
        path = (
            REPOSITORY_ROOT / "Results" / "Simulation" / "Sensitivity" / "raw"
            / "collaborative_known_target_visit" / "topk_sensitivity"
            / "multitarget_g19_r4_t50" / algorithm
            / f"topk_{rate:03d}" / "system_performance.csv"
        )
    return str(path.resolve()) if path.exists() else ""


def _field_present(value: object) -> bool:
    return str(value if value is not None else "").strip().lower() not in {
        "",
        "nan",
        "none",
        "null",
    }


def _read_unique_trials(path: Path) -> dict[int, dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    result: dict[int, dict[str, str]] = {}
    for row in rows:
        trial_id = int(row["trial_id"])
        if trial_id in result:
            raise ValueError(f"duplicate trial {trial_id} in {path}")
        result[trial_id] = row
    expected = set(range(BAYESIAN_TRIAL_COUNT))
    if set(result) != expected:
        missing = sorted(expected - set(result))
        extra = sorted(set(result) - expected)
        raise ValueError(
            f"{path} must contain Bayesian trials 0-499; "
            f"missing={missing[:10]}, extra={extra[:10]}"
        )
    return result


def _post_clue_calls(path: Path) -> dict[int, int]:
    result = {trial_id: 0 for trial_id in range(BAYESIAN_TRIAL_COUNT)}
    seen: set[int] = set()
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            trial_id = int(row["trial_id"])
            if trial_id not in result:
                raise ValueError(f"unexpected trial {trial_id} in {path}")
            result[trial_id] += int(
                float(row.get("allocator_calls_post_clue") or 0)
            )
            seen.add(trial_id)
    expected = set(range(BAYESIAN_TRIAL_COUNT))
    if seen != expected:
        missing = sorted(expected - seen)
        raise ValueError(
            f"{path} is missing computational rows for trials {missing[:10]}"
        )
    return result


def _bayesian_eligibility() -> tuple[list[int], dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    clue_counts = {
        trial_id: 0 for trial_id in range(BAYESIAN_TRIAL_COUNT)
    }
    minimum_calls = {
        trial_id: None for trial_id in range(BAYESIAN_TRIAL_COUNT)
    }
    evaluated = 0
    for condition in conditions("bayesian"):
        system_value = _baseline_path(condition)
        if not system_value:
            continue
        system_path = Path(system_value)
        computational_path = system_path.with_name(
            "computational_performance.csv"
        )
        if not computational_path.exists():
            raise FileNotFoundError(computational_path)
        system_rows = _read_unique_trials(system_path)
        post_clue_calls = _post_clue_calls(computational_path)
        evaluated += 1
        evidence.append(
            {
                "condition_id": condition.condition_id,
                "system_performance_csv": str(system_path),
                "system_performance_sha256": sha256_file(system_path),
                "computational_performance_csv": str(computational_path),
                "computational_performance_sha256": sha256_file(
                    computational_path
                ),
            }
        )
        for trial_id in range(BAYESIAN_TRIAL_COUNT):
            if _field_present(
                system_rows[trial_id].get("steps_before_first_clue")
            ):
                clue_counts[trial_id] += 1
            calls = post_clue_calls[trial_id]
            prior = minimum_calls[trial_id]
            minimum_calls[trial_id] = (
                calls if prior is None else min(prior, calls)
            )
    if evaluated == 0:
        raise ValueError("no historical Bayesian conditions were available")
    eligible = [
        trial_id
        for trial_id in range(BAYESIAN_TRIAL_COUNT)
        if clue_counts[trial_id] == evaluated
        and int(minimum_calls[trial_id] or 0)
        >= BAYESIAN_MIN_POST_CLUE_CALLS
    ]
    eligible_digest = hashlib.sha256(
        json.dumps(eligible, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return eligible, {
        "criterion": (
            "clue_found_in_every_available_historical_condition_and_"
            "minimum_post_clue_allocator_calls"
        ),
        "required_clue_condition_count": evaluated,
        "minimum_post_clue_allocator_calls_per_condition": (
            BAYESIAN_MIN_POST_CLUE_CALLS
        ),
        "eligible_trial_count": len(eligible),
        "eligible_trial_ids_sha256": eligible_digest,
        "evidence": evidence,
        "trial_evidence": {
            str(trial_id): {
                "clue_condition_count": clue_counts[trial_id],
                "minimum_post_clue_allocator_calls": int(
                    minimum_calls[trial_id] or 0
                ),
            }
            for trial_id in eligible
        },
    }


def prepare_campaign(
    campaign_id: str,
    *,
    build_root: Path | None = None,
) -> Path:
    root = (HIL_ROOT / campaign_id).resolve()
    if HIL_ROOT.resolve() not in root.parents:
        raise ValueError("HIL campaign escaped dedicated results directory")
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        if build_root is not None:
            expected = implementation_provenance(build_root)["device_build"]
            actual = existing.get("implementation", {}).get("device_build", {})
            if (
                actual.get("build_id") != expected["build_id"]
                or actual.get("manifest_sha256") != expected["manifest_sha256"]
            ):
                raise ValueError(
                    "existing HIL campaign is bound to a different device build"
                )
        return root
    if not BAYESIAN_SCENARIOS.exists():
        raise FileNotFoundError(BAYESIAN_SCENARIOS)
    collaborative = _collaborative_scenarios()
    bayesian_eligible, bayesian_eligibility = _bayesian_eligibility()
    selected = {
        "bayesian": _sample(
            bayesian_eligible,
            BAYESIAN_SAMPLE_COUNT,
            BAYESIAN_SEED,
        ),
        "collaborative": _sample(range(100), 10, COLLABORATIVE_SEED),
    }
    jobs: list[dict[str, Any]] = []
    for condition in conditions():
        # Bayesian K=1 is intentionally excluded from authoritative HIL.  Its
        # starvation-heavy paths are retained in the archived 2026-08-01 run,
        # while collaborative K=1 and K=2 remain in scope.
        if condition.mission == "bayesian" and condition.top_k_cells == 1:
            continue
        trial_ids = selected[condition.mission]
        historical_system_csv = _baseline_path(condition)
        scenario_sha = (
            sha256_file(BAYESIAN_SCENARIOS)
            if condition.mission == "bayesian"
            else ""
        )
        jobs.append(
            {
                "condition_id": condition.condition_id,
                "mission": condition.mission,
                "algorithm": condition.algorithm,
                "top_k_level": condition.top_k_level,
                "top_k_rate": condition.top_k_rate,
                "top_k_cells": condition.top_k_cells,
                "trial_ids": trial_ids,
                "scenario_file": (
                    str(BAYESIAN_SCENARIOS.resolve())
                    if condition.mission == "bayesian"
                    else ""
                ),
                "scenario_sha256": scenario_sha,
                "historical_system_csv": historical_system_csv,
                "historical_system_sha256": (
                    sha256_file(Path(historical_system_csv))
                    if historical_system_csv
                    else ""
                ),
                "status": "pending",
                "device_id": "",
                "completed_trials": [],
                "stopped_reason": "",
            }
        )
    manifest = {
        "schema": HIL_MANIFEST_SCHEMA,
        "mode": "pololu_authoritative_hil",
        "campaign_id": campaign_id,
        "status": "prepared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "implementation": implementation_provenance(build_root),
        "algorithms": list(ALGORITHMS),
        "selected_trials": selected,
        "selection_seeds": {
            "bayesian": BAYESIAN_SEED,
            "collaborative": COLLABORATIVE_SEED,
        },
        "trial_eligibility": {
            "bayesian": {
                **bayesian_eligibility,
                "selected_trial_evidence": {
                    str(trial_id): bayesian_eligibility[
                        "trial_evidence"
                    ][str(trial_id)]
                    for trial_id in selected["bayesian"]
                },
            },
            "collaborative": {
                "criterion": "historical_trial_id_0_through_99",
                "eligible_trial_count": 100,
            },
        },
        "scenario_sources": {
            "bayesian": {
                "path": str(BAYESIAN_SCENARIOS.resolve()),
                "sha256": sha256_file(BAYESIAN_SCENARIOS),
            },
            "collaborative": {
                str(key): value for key, value in sorted(collaborative.items())
                if key in selected["collaborative"]
            },
        },
        "condition_count": len(jobs),
        "mission_run_count": sum(len(job["trial_ids"]) for job in jobs),
        "jobs": jobs,
    }
    root.mkdir(parents=True, exist_ok=False)
    (root / "journals").mkdir()
    (root / "reports").mkdir()
    _atomic_json(manifest_path, manifest)
    _atomic_json(root / "schedule.json", manifest)
    return root


def invalidation_path(root: Path) -> Path:
    return root.resolve() / INVALIDATION_FILENAME


def load_invalidation(root: Path) -> dict[str, Any] | None:
    path = invalidation_path(root)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def invalidate_campaign(
    root: Path,
    *,
    reason_code: str,
    explanation: str,
    superseded_by: str = "",
    evidence: dict[str, Any] | None = None,
) -> Path:
    """Seal a campaign as diagnostic-only without modifying its raw journals."""

    root = root.resolve()
    manifest = load_manifest(root, "manifest.json")
    path = invalidation_path(root)
    if path.exists():
        return path
    marker = {
        "schema": 1,
        "status": "invalid_for_analysis",
        "campaign_id": str(manifest.get("campaign_id", root.name)),
        "invalidated_at": datetime.now(timezone.utc).isoformat(),
        "reason_code": str(reason_code),
        "explanation": str(explanation),
        "superseded_by": str(superseded_by),
        "raw_journals_preserved": True,
        "report_policy": (
            "raw attempts remain available for diagnostics; no attempt, trial, "
            "or condition is accepted as representative analysis data"
        ),
        "evidence": dict(evidence or {}),
    }
    _atomic_json(path, marker)
    return path


def verify_campaign_provenance(
    root: Path,
    *,
    build_root: Path | None = None,
    device_build_ids: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Fail closed when a prepared campaign no longer matches executable code."""

    root = root.resolve()
    invalidation = load_invalidation(root)
    if invalidation is not None:
        raise ValueError(
            "campaign is marked invalid for analysis: "
            f"{invalidation.get('reason_code', 'unspecified')}"
        )
    manifest = load_manifest(root, "manifest.json")
    if int(manifest.get("schema", 0)) < HIL_MANIFEST_SCHEMA:
        raise ValueError(
            "legacy HIL manifest has no immutable v2 implementation provenance"
        )
    recorded = manifest.get("implementation")
    if not isinstance(recorded, dict):
        raise ValueError("campaign implementation provenance is missing")
    recorded_build = recorded.get("device_build", {})
    manifest_build_path = Path(
        str(recorded_build.get("manifest_path", ""))
    )
    if build_root is None:
        if not manifest_build_path.exists():
            raise FileNotFoundError(
                "campaign-bound device build manifest is unavailable: "
                f"{manifest_build_path}"
            )
        resolved_build = manifest_build_path.parent
    else:
        resolved_build = build_root.resolve()
    current = implementation_provenance(resolved_build)
    checks = {
        "implementation_id": (
            recorded.get("implementation_id")
            == current["implementation_id"]
        ),
        "device_build_id": (
            recorded_build.get("build_id")
            == current["device_build"]["build_id"]
        ),
        "device_build_manifest": (
            recorded_build.get("manifest_sha256")
            == current["device_build"]["manifest_sha256"]
        ),
        "device_source_bundle": (
            recorded_build.get("source_bundle_sha256")
            == current["device_build"]["source_bundle_sha256"]
        ),
        "host_source": (
            recorded.get("host_source", {}).get("sha256")
            == current["host_source"]["sha256"]
        ),
        "bayesian_simulator_source": (
            recorded.get("simulator_sources", {})
            .get("bayesian", {})
            .get("sha256")
            == current["simulator_sources"]["bayesian"]["sha256"]
        ),
        "collaborative_simulator_source": (
            recorded.get("simulator_sources", {})
            .get("collaborative", {})
            .get("sha256")
            == current["simulator_sources"]["collaborative"]["sha256"]
        ),
    }
    scenario_sources = manifest.get("scenario_sources", {})
    bayesian_scenario = scenario_sources.get("bayesian", {})
    bayesian_path = Path(str(bayesian_scenario.get("path", "")))
    checks["bayesian_scenario"] = (
        bayesian_path.is_file()
        and sha256_file(bayesian_path)
        == str(bayesian_scenario.get("sha256", ""))
    )
    collaborative_ok = True
    for item in scenario_sources.get("collaborative", {}).values():
        path = Path(str(item.get("path", "")))
        if (
            not path.is_file()
            or sha256_file(path) != str(item.get("sha256", ""))
        ):
            collaborative_ok = False
            break
    checks["collaborative_scenarios"] = collaborative_ok
    expected_build_id = str(recorded_build.get("build_id", ""))
    checks["connected_device_builds"] = all(
        str(build_id) == expected_build_id for build_id in device_build_ids
    )
    failures = [key for key, passed in checks.items() if not passed]
    if failures:
        raise ValueError(
            "campaign provenance mismatch: " + ", ".join(failures)
        )
    return {
        "campaign_manifest_sha256": sha256_file(root / "manifest.json"),
        "implementation_id": current["implementation_id"],
        "build_id": expected_build_id,
        "checks": checks,
    }


def load_manifest(root: Path, name: str = "schedule.json") -> dict[str, Any]:
    # PowerShell's UTF-8 writer may add a BOM to an operational schedule.
    # Accept it without weakening JSON validation or changing manifest content.
    return json.loads((root / name).read_text(encoding="utf-8-sig"))


def save_schedule(root: Path, value: dict[str, Any]) -> None:
    value["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_json(root / "schedule.json", value)


def collaborative_scenario_for(
    manifest: dict[str, Any],
    trial_id: int,
) -> Path:
    return Path(manifest["scenario_sources"]["collaborative"][str(trial_id)]["path"])
