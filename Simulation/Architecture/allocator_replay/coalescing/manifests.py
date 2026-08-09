"""Load and byte-verify paired spatial/release manifests."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import CampaignConfig


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _iter_index_entries(value: Any, path_hint: str = "") -> Iterable[tuple[str, str]]:
    """Accept both mapping- and row-oriented generated manifest indexes."""

    if isinstance(value, dict):
        explicit_path = value.get("path", value.get("relative_path", value.get("file")))
        explicit_sha = value.get("sha256", value.get("file_sha256"))
        if explicit_path is not None and explicit_sha is not None:
            yield str(explicit_path).replace("\\", "/"), str(explicit_sha).lower()
        for key, item in value.items():
            normalized_key = str(key).replace("\\", "/")
            if isinstance(item, str) and len(item) == 64:
                try:
                    int(item, 16)
                except ValueError:
                    pass
                else:
                    if "/" in normalized_key or normalized_key.endswith(".json"):
                        yield normalized_key, item.lower()
            elif isinstance(item, dict):
                sha = item.get("sha256", item.get("file_sha256"))
                if sha is not None and (
                    "/" in normalized_key or normalized_key.endswith(".json")
                ):
                    yield normalized_key, str(sha).lower()
            yield from _iter_index_entries(item, normalized_key)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_index_entries(item, path_hint)


def _index_hash(index: dict[str, Any], relative_path: str) -> str:
    wanted = relative_path.replace("\\", "/")
    entries = dict(_iter_index_entries(index))
    if wanted in entries:
        return entries[wanted]
    suffix_matches = [value for key, value in entries.items() if key.endswith("/" + wanted)]
    if len(set(suffix_matches)) == 1:
        return suffix_matches[0]
    raise ValueError(f"manifest_index.json does not seal {wanted}")


def _task_map(tasks: Any, *, require_release: bool) -> dict[str, dict[str, Any]]:
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("manifest tasks must be a non-empty list")
    result: dict[str, dict[str, Any]] = {}
    for item in tasks:
        if not isinstance(item, dict):
            raise ValueError("manifest task rows must be objects")
        task_id = str(item["task_id"])
        if task_id in result:
            raise ValueError(f"duplicate task_id: {task_id}")
        normalized = {
            "task_id": task_id,
            "x": int(item["x"]),
            "y": int(item["y"]),
            "initially_visible": bool(item["initially_visible"]),
        }
        if require_release:
            normalized["release_time_s"] = float(item["release_time_s"])
            if normalized["release_time_s"] < 0:
                raise ValueError(f"negative release_time_s for {task_id}")
        result[task_id] = normalized
    return result


@dataclass(frozen=True)
class PairedTrace:
    manifest_set_id: str
    trace_id: str
    load_id: str
    scenario_path: Path
    release_path: Path
    scenario_sha256: str
    release_sha256: str
    runtime_seed: int
    grid_size: int
    robot_starts: tuple[dict[str, Any], ...]
    tasks: tuple[dict[str, Any], ...]
    arrival_rate_tasks_per_s: float | None = None

    @property
    def paired_manifest_id(self) -> str:
        return f"{self.manifest_set_id}:{self.load_id}:{self.trace_id}"

    @property
    def paired_manifest_sha256(self) -> str:
        return canonical_sha256(
            {
                "scenario_sha256": self.scenario_sha256,
                "release_sha256": self.release_sha256,
            }
        )

    def schedule_row(self) -> dict[str, Any]:
        return {
            "paired_manifest_id": self.paired_manifest_id,
            "paired_manifest_sha256": self.paired_manifest_sha256,
            "manifest_set_id": self.manifest_set_id,
            "trace_id": self.trace_id,
            "arrival_load": self.load_id,
            "scenario_path": str(self.scenario_path),
            "release_path": str(self.release_path),
            "scenario_sha256": self.scenario_sha256,
            "release_sha256": self.release_sha256,
            "runtime_seed": self.runtime_seed,
            "grid_size": self.grid_size,
            "arrival_rate_tasks_per_s": self.arrival_rate_tasks_per_s,
            "robot_starts": list(self.robot_starts),
        }


def load_paired_trace(config: CampaignConfig, load_id: str, trace_id: str) -> PairedTrace:
    root = config.manifest_root
    index_path = root / "manifest_index.json"
    if not index_path.is_file():
        raise FileNotFoundError(index_path)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    scenario_rel = f"scenarios/{trace_id}.json"
    release_rel = f"releases/{load_id}/{trace_id}.json"
    scenario_path = root / Path(scenario_rel)
    release_path = root / Path(release_rel)
    if not scenario_path.is_file():
        raise FileNotFoundError(scenario_path)
    if not release_path.is_file():
        raise FileNotFoundError(release_path)
    scenario_sha = sha256_file(scenario_path)
    release_sha = sha256_file(release_path)
    if scenario_sha != _index_hash(index, scenario_rel):
        raise ValueError(f"scenario hash differs from manifest index: {scenario_rel}")
    if release_sha != _index_hash(index, release_rel):
        raise ValueError(f"release hash differs from manifest index: {release_rel}")

    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    release = json.loads(release_path.read_text(encoding="utf-8"))
    if int(scenario.get("schema_version", 0)) != 1:
        raise ValueError("unsupported scenario schema_version")
    if int(release.get("schema_version", 0)) != 1:
        raise ValueError("unsupported release schema_version")
    if scenario.get("manifest_kind", "scenario") != "scenario":
        raise ValueError("wrong scenario manifest_kind")
    if release.get("manifest_kind", "release_trace") != "release_trace":
        raise ValueError("wrong release manifest_kind")
    if str(scenario["trace_id"]) != trace_id or str(release["trace_id"]) != trace_id:
        raise ValueError("trace_id does not match selected manifest path")
    if str(release["load_id"]) != load_id:
        raise ValueError("release load_id does not match selected load")
    if int(scenario["runtime_seed"]) != int(release["runtime_seed"]):
        raise ValueError("paired runtime_seed mismatch")
    if str(release.get("scenario_sha256", "")).lower() != scenario_sha:
        raise ValueError("release trace scenario_sha256 mismatch")

    scenario_tasks = _task_map(scenario.get("tasks"), require_release=False)
    release_tasks = _task_map(release.get("tasks"), require_release=True)
    if len(scenario_tasks) != 50:
        raise ValueError(
            f"Collaborative Visit manifests require exactly 50 tasks; "
            f"found {len(scenario_tasks)}"
        )
    task_coordinates = {
        (int(item["x"]), int(item["y"])) for item in scenario_tasks.values()
    }
    if len(task_coordinates) != 50:
        raise ValueError("Collaborative Visit task coordinates must be unique")
    if set(scenario_tasks) != set(release_tasks):
        raise ValueError("paired manifests contain different task IDs")
    for task_id, spatial in scenario_tasks.items():
        dynamic = release_tasks[task_id]
        for field in ("x", "y", "initially_visible"):
            if spatial[field] != dynamic[field]:
                raise ValueError(f"paired task {task_id} differs in {field}")
        if dynamic["initially_visible"] and dynamic["release_time_s"] != 0.0:
            raise ValueError(f"initial task {task_id} must have release_time_s=0")
    initial_count = sum(item["initially_visible"] for item in release_tasks.values())
    if int(release.get("initial_task_count", initial_count)) != initial_count:
        raise ValueError("initial_task_count disagrees with task rows")

    starts = scenario.get("robot_starts")
    if not isinstance(starts, list) or len(starts) != 4:
        raise ValueError("Collaborative Visit manifests require exactly 4 robots")
    robot_starts: list[dict[str, Any]] = []
    robot_ids: set[str] = set()
    for item in starts:
        rid = str(item["robot_id"])
        if rid in robot_ids:
            raise ValueError(f"duplicate robot_id: {rid}")
        robot_ids.add(rid)
        robot_starts.append(
            {
                "robot_id": rid,
                "x": int(item["x"]),
                "y": int(item["y"]),
                "heading_x": int(item.get("heading_x", 1)),
                "heading_y": int(item.get("heading_y", 0)),
            }
        )
    grid_size = int(scenario["grid_size"])
    for task in release_tasks.values():
        if not (0 <= task["x"] < grid_size and 0 <= task["y"] < grid_size):
            raise ValueError(f"task out of bounds: {task['task_id']}")
    tasks = tuple(
        sorted(
            release_tasks.values(),
            key=lambda item: (item["release_time_s"], item["task_id"]),
        )
    )
    arrival_model = release.get("arrival_model", {})
    rate = (
        float(arrival_model["rate_per_s"])
        if isinstance(arrival_model, dict) and "rate_per_s" in arrival_model
        else None
    )
    if rate is not None and rate <= 0:
        raise ValueError("arrival_model.rate_per_s must be positive")
    return PairedTrace(
        manifest_set_id=config.manifest_set_id,
        trace_id=trace_id,
        load_id=load_id,
        scenario_path=scenario_path.resolve(),
        release_path=release_path.resolve(),
        scenario_sha256=scenario_sha,
        release_sha256=release_sha,
        runtime_seed=int(scenario["runtime_seed"]),
        grid_size=grid_size,
        robot_starts=tuple(robot_starts),
        tasks=tasks,
        arrival_rate_tasks_per_s=rate,
    )


def load_all_pairs(config: CampaignConfig) -> dict[tuple[str, str], PairedTrace]:
    return {
        (load, trace): load_paired_trace(config, load, trace)
        for load in config.arrival_loads
        for trace in config.trace_ids
    }
