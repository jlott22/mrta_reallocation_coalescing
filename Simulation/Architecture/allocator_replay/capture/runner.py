from __future__ import annotations

import concurrent.futures
import json
import os
import time
from pathlib import Path

from allocator_replay.capture.cohorts import generate_all
from allocator_replay.capture.seal import seal_traces
from allocator_replay.capture.tracer import capture_condition
from allocator_replay.config.study import TRACE_ROOT, Condition, conditions


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _capture_one(payload: tuple[Condition, int | None, bool]) -> dict[str, object]:
    condition, max_trials, force = payload
    started = time.time()
    try:
        manifest = capture_condition(
            condition,
            max_trials=max_trials,
            force=force,
        )
        return {
            "condition_id": condition.condition_id,
            "complete": True,
            "fixture_count": manifest["fixture_count"],
            "elapsed_seconds": time.time() - started,
        }
    except Exception as exc:
        return {
            "condition_id": condition.condition_id,
            "complete": False,
            "error": repr(exc),
            "elapsed_seconds": time.time() - started,
        }


def _predicted_capture_weight(condition: Condition) -> float:
    """LPT heuristic used only to balance desktop trace-capture workers."""
    algorithm_factor = {
        "CBAA": 1.0,
        "ACBBA": 2.0,
        "PI": 1.8,
        "HIPC": 2.2,
        "DMCHBA": 4.0,
        "DGA": 6.0,
    }[condition.algorithm]
    mission_factor = 2.0 if condition.mission == "collaborative" else 1.0
    return (
        mission_factor
        * algorithm_factor
        * float(max(1, condition.top_k_cells)) ** 2
    )


def capture_all(
    *,
    mission: str | None = None,
    selected: list[Condition] | None = None,
    workers: int | None = None,
    max_trials: int | None = None,
    force: bool = False,
) -> dict[str, object]:
    cohorts = generate_all()
    selected_conditions = list(selected or conditions(mission))
    # ProcessPoolExecutor assigns the next submitted item to the next free
    # worker.  Longest-predicted-processing-time first minimizes the final
    # straggler without hard-coding conditions to cores.
    selected_conditions.sort(key=_predicted_capture_weight, reverse=True)
    worker_count = workers or max(1, int((os.cpu_count() or 1) * 0.75))
    worker_count = min(worker_count, len(selected_conditions))
    progress_path = TRACE_ROOT / "capture_progress.json"
    results: list[dict[str, object]] = []
    _atomic_json(
        progress_path,
        {
            "status": "running",
            "conditions_total": len(selected_conditions),
            "conditions_complete": 0,
            "workers": worker_count,
        },
    )
    payloads = [
        (condition, max_trials, force)
        for condition in selected_conditions
    ]
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=worker_count
    ) as executor:
        futures = [executor.submit(_capture_one, payload) for payload in payloads]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)
            _atomic_json(
                progress_path,
                {
                    "status": "running",
                    "conditions_total": len(selected_conditions),
                    "conditions_complete": sum(
                        bool(item.get("complete")) for item in results
                    ),
                    "conditions_finished": len(results),
                    "workers": worker_count,
                    "latest": result,
                },
            )
    failed = [item for item in results if not item.get("complete")]
    summary = {
        "status": "complete" if not failed else "failed",
        "conditions_total": len(selected_conditions),
        "conditions_complete": len(selected_conditions) - len(failed),
        "workers": worker_count,
        "cohorts": cohorts,
        "results": sorted(results, key=lambda item: str(item["condition_id"])),
    }
    if not failed:
        summary["trace_catalog"] = seal_traces(conditions())
    _atomic_json(progress_path, summary)
    if failed:
        raise RuntimeError(f"{len(failed)} trace-capture conditions failed")
    return summary
