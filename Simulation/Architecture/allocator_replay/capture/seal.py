from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable

from allocator_replay.capture.cohorts import sha256_file
from allocator_replay.capture.tracer import (
    _source_hash,
    simulator_source_paths,
)
from allocator_replay.config.study import (
    TRACE_ROOT,
    Condition,
    minimum_capture_trial_count,
    trace_condition_root,
)


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def seal_traces(selected: Iterable[Condition]) -> dict[str, object]:
    entries: dict[str, object] = {}
    for condition in selected:
        root = trace_condition_root(condition)
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("status") != "complete"
            or int(manifest.get("trial_count", -1))
            < minimum_capture_trial_count(condition)
        ):
            raise RuntimeError(
                f"cannot seal incomplete trace {condition.condition_id}"
            )
        source_paths = simulator_source_paths(condition)
        recorded_source_hash = manifest.get("simulator_source_sha256", "")
        full_source_hash = _source_hash(source_paths)
        legacy_paths = [
            path
            for path in source_paths
            if not (
                condition.mission == "bayesian"
                and condition.algorithm == "DGA"
                and path.name == "DGA_optimized.py"
            )
        ]
        if recorded_source_hash not in {
            full_source_hash,
            _source_hash(legacy_paths),
        }:
            raise RuntimeError(
                f"simulator source changed during {condition.condition_id}"
            )
        manifest["simulator_source_sha256"] = full_source_hash
        manifest["condition"]["top_k_level"] = condition.top_k_level
        manifest["simulator_source_files"] = {
            str(path.resolve()): sha256_file(path)
            for path in source_paths
        }
        for trial in manifest["trials"]:
            trace = root / trial["trace"]
            trial["file_sha256"] = sha256_file(trace)
        manifest["sealed"] = True
        _atomic_json(manifest_path, manifest)
        entries[condition.condition_id] = {
            "manifest": str(manifest_path.resolve()),
            "manifest_sha256": sha256_file(manifest_path),
            "fixture_count": manifest["fixture_count"],
            "trial_count": manifest["trial_count"],
            "cohort_sha256": manifest["cohort_sha256"],
            "simulator_source_sha256": full_source_hash,
        }
    catalog = {
        "schema": 1,
        "sealed": True,
        "condition_count": len(entries),
        "conditions": entries,
    }
    catalog_path = TRACE_ROOT / "catalog.json"
    _atomic_json(catalog_path, catalog)
    return {
        "path": str(catalog_path.resolve()),
        "sha256": sha256_file(catalog_path),
        "condition_count": len(entries),
    }
