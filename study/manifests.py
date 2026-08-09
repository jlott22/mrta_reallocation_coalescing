"""Generate and validate explicit, paired scenario and release manifests.

The spatial, arrival, and runtime random streams are derived independently with
SHA-256.  In particular, changing an arrival rate cannot change task locations,
robot starts, or the underlying sequence of exponential variates.  A release
trace is an absolute mission-time schedule and never depends on allocator state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
DEFAULT_MANIFEST_ROOT = Path("study/generated/manifests")


def canonical_json_bytes(value: Any) -> bytes:
    """Return the single canonical on-disk representation used for hashing."""

    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_seed(master_seed: int, stream: str, trace_id: str) -> int:
    """Derive a platform-independent 64-bit seed without Python's salted hash."""

    material = f"mrta-reallocation-coalescing:v1:{master_seed}:{stream}:{trace_id}".encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def edge_even_robot_starts(grid_size: int, robot_count: int) -> list[dict[str, Any]]:
    if grid_size <= 0 or robot_count <= 0 or robot_count > grid_size:
        raise ValueError("edge-even starts require 1 <= robot_count <= grid_size")
    if robot_count == 1:
        ys = [(grid_size - 1) // 2]
    else:
        ys = [round(index * (grid_size - 1) / (robot_count - 1)) for index in range(robot_count)]
    return [
        {
            "robot_id": f"{index:02d}",
            "x": 0,
            "y": y,
            "heading_x": 1,
            "heading_y": 0,
        }
        for index, y in enumerate(ys)
    ]


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _manifest_settings(config: dict[str, Any]) -> dict[str, Any]:
    settings = dict(config.get("manifest", {}))
    required = ("manifest_set_id", "master_seed", "trace_count", "grid_size", "robot_count",
                "task_count", "initial_task_count", "arrival_loads")
    missing = [name for name in required if name not in settings]
    if missing:
        raise ValueError(f"manifest config missing: {', '.join(missing)}")
    _positive_int(settings["trace_count"], "trace_count")
    _positive_int(settings["grid_size"], "grid_size")
    _positive_int(settings["robot_count"], "robot_count")
    _positive_int(settings["task_count"], "task_count")
    _positive_int(settings["initial_task_count"], "initial_task_count")
    if settings["initial_task_count"] > settings["task_count"]:
        raise ValueError("initial_task_count cannot exceed task_count")
    if not isinstance(settings["master_seed"], int) or isinstance(settings["master_seed"], bool):
        raise ValueError("master_seed must be an integer")
    if not settings["manifest_set_id"] or Path(settings["manifest_set_id"]).name != settings["manifest_set_id"]:
        raise ValueError("manifest_set_id must be one safe path component")
    loads = settings["arrival_loads"]
    if not isinstance(loads, dict) or not loads:
        raise ValueError("arrival_loads must be a non-empty object")
    for load_id, rate in loads.items():
        if not load_id or Path(load_id).name != load_id:
            raise ValueError(f"unsafe arrival load id: {load_id!r}")
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0 or not math.isfinite(rate):
            raise ValueError(f"arrival rate for {load_id!r} must be finite and positive")
    starts = edge_even_robot_starts(settings["grid_size"], settings["robot_count"])
    if settings["task_count"] > settings["grid_size"] ** 2 - len(starts):
        raise ValueError("task_count exceeds non-start grid cells")
    return settings


def _write_immutable_json(path: Path, value: Any) -> str:
    data = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise FileExistsError(f"refusing to overwrite a different manifest: {path}")
    else:
        path.write_bytes(data)
    return sha256_bytes(data)


def _scenario(master_seed: int, trace_index: int, grid_size: int, robot_count: int,
              task_count: int, initial_task_count: int) -> dict[str, Any]:
    trace_id = f"trace_{trace_index:04d}"
    spatial_seed = stable_seed(master_seed, "spatial", trace_id)
    runtime_seed = stable_seed(master_seed, "runtime", trace_id)
    starts = edge_even_robot_starts(grid_size, robot_count)
    occupied = {(robot["x"], robot["y"]) for robot in starts}
    eligible = [
        (x, y)
        for y in range(grid_size)
        for x in range(grid_size)
        if (x, y) not in occupied
    ]
    locations = random.Random(spatial_seed).sample(eligible, task_count)
    tasks = [
        {
            "task_id": f"task_{index:04d}",
            "x": x,
            "y": y,
            "initially_visible": index <= initial_task_count,
        }
        # The copied simulator/world uses one-based target numbering.  Keep
        # manifests identical to that convention so IDs need no translation.
        for index, (x, y) in enumerate(locations, start=1)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_kind": "scenario",
        "trace_id": trace_id,
        "runtime_seed": runtime_seed,
        "spatial_seed": spatial_seed,
        "grid_size": grid_size,
        "robot_start_layout": "edge_even",
        "robot_starts": starts,
        "tasks": tasks,
    }


def _unit_exponential_draws(master_seed: int, trace_id: str, count: int) -> tuple[int, list[float]]:
    arrival_seed = stable_seed(master_seed, "arrival", trace_id)
    rng = random.Random(arrival_seed)
    # Draw Exp(rate=1) once. Every load scales this same sequence, strengthening
    # pairing across load as well as across algorithms and policies.
    draws = [-math.log1p(-rng.random()) for _ in range(count)]
    return arrival_seed, draws


def _release_trace(scenario: dict[str, Any], scenario_sha256: str, master_seed: int,
                   load_id: str, rate_per_s: float) -> dict[str, Any]:
    initial_count = sum(bool(task["initially_visible"]) for task in scenario["tasks"])
    online_count = len(scenario["tasks"]) - initial_count
    arrival_seed, unit_draws = _unit_exponential_draws(master_seed, scenario["trace_id"], online_count)
    release_times = [0.0] * initial_count
    absolute_time = 0.0
    for draw in unit_draws:
        absolute_time += draw / float(rate_per_s)
        release_times.append(round(absolute_time, 9))
    tasks = [
        {
            "task_id": task["task_id"],
            "x": task["x"],
            "y": task["y"],
            "release_time_s": release_time,
            "initially_visible": task["initially_visible"],
        }
        for task, release_time in zip(scenario["tasks"], release_times, strict=True)
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "manifest_kind": "release_trace",
        "trace_id": scenario["trace_id"],
        "load_id": load_id,
        "runtime_seed": scenario["runtime_seed"],
        "arrival_seed": arrival_seed,
        "arrival_model": {
            "name": "exponential_interarrival_absolute_time",
            "rate_per_s": float(rate_per_s),
            "time_origin_s": 0.0,
            "shared_unit_rate_draws_across_loads": True,
        },
        "initial_task_count": initial_count,
        "scenario_sha256": scenario_sha256,
        "tasks": tasks,
    }


def generate_manifest_set(config: dict[str, Any], repo_root: Path | str = ".") -> Path:
    """Generate a set, refusing to replace any file whose content differs."""

    settings = _manifest_settings(config)
    root_setting = Path(settings.get("root", DEFAULT_MANIFEST_ROOT))
    set_root = Path(repo_root).resolve() / root_setting / settings["manifest_set_id"]
    entries: list[dict[str, Any]] = []
    for trace_index in range(settings["trace_count"]):
        scenario = _scenario(
            settings["master_seed"], trace_index, settings["grid_size"],
            settings["robot_count"], settings["task_count"], settings["initial_task_count"],
        )
        scenario_rel = Path("scenarios") / f"{scenario['trace_id']}.json"
        scenario_hash = _write_immutable_json(set_root / scenario_rel, scenario)
        entries.append({
            "path": scenario_rel.as_posix(), "sha256": scenario_hash,
            "manifest_kind": "scenario", "trace_id": scenario["trace_id"],
        })
        for load_id, rate in sorted(settings["arrival_loads"].items()):
            release = _release_trace(scenario, scenario_hash, settings["master_seed"], load_id, rate)
            release_rel = Path("releases") / load_id / f"{scenario['trace_id']}.json"
            release_hash = _write_immutable_json(set_root / release_rel, release)
            entries.append({
                "path": release_rel.as_posix(), "sha256": release_hash,
                "manifest_kind": "release_trace", "trace_id": scenario["trace_id"],
                "load_id": load_id,
            })
    index = {
        "schema_version": SCHEMA_VERSION,
        "manifest_kind": "manifest_index",
        "manifest_set_id": settings["manifest_set_id"],
        "generation": {
            "master_seed": settings["master_seed"],
            "trace_count": settings["trace_count"],
            "grid_size": settings["grid_size"],
            "robot_count": settings["robot_count"],
            "task_count": settings["task_count"],
            "initial_task_count": settings["initial_task_count"],
            "arrival_loads": settings["arrival_loads"],
            "spatial_arrival_runtime_rng_streams_are_separate": True,
        },
        "entries": sorted(entries, key=lambda entry: entry["path"]),
    }
    _write_immutable_json(set_root / "manifest_index.json", index)
    validate_manifest_set(set_root)
    return set_root


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def validate_manifest_set(set_root: Path | str) -> dict[str, Any]:
    """Validate byte hashes plus scenario/release semantic pairing."""

    root = Path(set_root).resolve()
    index = _load_json(root / "manifest_index.json")
    if index.get("schema_version") != SCHEMA_VERSION or index.get("manifest_kind") != "manifest_index":
        raise ValueError("unsupported manifest index")
    scenarios: dict[str, tuple[dict[str, Any], str]] = {}
    releases: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for entry in index.get("entries", []):
        relative = Path(entry["path"])
        path = (root / relative).resolve()
        if root not in path.parents or relative.is_absolute():
            raise ValueError(f"manifest index contains unsafe path: {relative}")
        if entry["path"] in seen_paths:
            raise ValueError(f"duplicate manifest index path: {entry['path']}")
        seen_paths.add(entry["path"])
        actual_hash = sha256_file(path)
        if actual_hash != entry["sha256"]:
            raise ValueError(f"SHA256 mismatch for {entry['path']}")
        value = _load_json(path)
        if value.get("manifest_kind") != entry["manifest_kind"]:
            raise ValueError(f"manifest kind mismatch for {entry['path']}")
        if value.get("trace_id") != entry["trace_id"]:
            raise ValueError(f"trace ID mismatch for {entry['path']}")
        if entry["manifest_kind"] == "scenario":
            scenarios[entry["trace_id"]] = (value, actual_hash)
        else:
            releases.append(value)
    for release in releases:
        if release["trace_id"] not in scenarios:
            raise ValueError(f"release has no scenario: {release['trace_id']}")
        scenario, scenario_hash = scenarios[release["trace_id"]]
        if release["scenario_sha256"] != scenario_hash:
            raise ValueError(f"release scenario hash mismatch: {release['trace_id']}/{release['load_id']}")
        if release["runtime_seed"] != scenario["runtime_seed"]:
            raise ValueError("paired runtime seeds differ")
        scenario_tasks = [(t["task_id"], t["x"], t["y"], t["initially_visible"]) for t in scenario["tasks"]]
        release_tasks = [(t["task_id"], t["x"], t["y"], t["initially_visible"]) for t in release["tasks"]]
        if scenario_tasks != release_tasks:
            raise ValueError(f"scenario/release tasks differ: {release['trace_id']}/{release['load_id']}")
        task_ids = [task["task_id"] for task in release["tasks"]]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task IDs must be unique")
        times = [task["release_time_s"] for task in release["tasks"]]
        if times != sorted(times) or any(time < 0 for time in times):
            raise ValueError("release times must be nonnegative and ordered")
        for task in release["tasks"]:
            if bool(task["initially_visible"]) != (task["release_time_s"] == 0.0):
                raise ValueError("initial visibility and release time disagree")
    generation = index.get("generation", {})
    expected_scenarios = generation.get("trace_count")
    expected_releases = expected_scenarios * len(generation.get("arrival_loads", {}))
    if len(scenarios) != expected_scenarios or len(releases) != expected_releases:
        raise ValueError("manifest index is incomplete")
    return index


def load_config(path: Path | str) -> dict[str, Any]:
    return _load_json(Path(path))


def iter_pairs(set_root: Path | str) -> Iterable[tuple[Path, Path, dict[str, Any]]]:
    root = Path(set_root).resolve()
    index = validate_manifest_set(root)
    scenario_entries = {
        entry["trace_id"]: entry for entry in index["entries"] if entry["manifest_kind"] == "scenario"
    }
    for release_entry in index["entries"]:
        if release_entry["manifest_kind"] != "release_trace":
            continue
        scenario_entry = scenario_entries[release_entry["trace_id"]]
        yield root / scenario_entry["path"], root / release_entry["path"], release_entry


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(args.config)
    settings = _manifest_settings(config)
    root = args.repo_root.resolve() / Path(settings.get("root", DEFAULT_MANIFEST_ROOT)) / settings["manifest_set_id"]
    if args.validate_only:
        validate_manifest_set(root)
        print(f"validated immutable manifest set: {root}")
    else:
        generated = generate_manifest_set(config, args.repo_root)
        print(f"generated and validated immutable manifest set: {generated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
