from __future__ import annotations

import hashlib
import importlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from time import perf_counter_ns
from typing import Any

from allocator_replay.capture.codec import encode_value, write_trace
from allocator_replay.capture.cohorts import sha256_file
from allocator_replay.capture.state import (
    classify_call,
    expected_messages_from_state,
    snapshot,
    state_fingerprint,
)
from allocator_replay.config.study import (
    BAYESIAN_SIM_ROOT,
    COLLABORATIVE_SIM_ROOT,
    COMMITMENT_HORIZON,
    GRID_SIZE,
    ROBOT_IDS,
    SIMULATION_SEED,
    START_HEADINGS,
    START_POSITIONS,
    Condition,
    cohort_path,
    minimum_capture_trial_count,
    trace_condition_root,
)


ALGORITHM_MODULES = {
    "bayesian": {
        name: f"benchmark_sim.algorithms.{name}"
        for name in ("CBAA", "ACBBA", "PI", "HIPC", "DMCHBA", "DGA")
    },
    "collaborative": {
        name: f"known_visit_sim.algorithms.{name}"
        for name in ("CBAA", "ACBBA", "PI", "HIPC", "DMCHBA", "DGA")
    },
}
ALGORITHM_CLASSES = {
    "CBAA": "CBAAAllocator",
    "ACBBA": "ACBBAAllocator",
    "PI": "PIAllocator",
    "HIPC": "HIPCAllocator",
    "DMCHBA": "DMCHBAAllocator",
    "DGA": "DGAAllocator",
}


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _source_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.resolve()).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def simulator_source_paths(condition: Condition) -> list[Path]:
    sim_root = (
        BAYESIAN_SIM_ROOT / "benchmark_sim"
        if condition.mission == "bayesian"
        else COLLABORATIVE_SIM_ROOT / "known_visit_sim"
    )
    paths = [
        sim_root / "algorithms" / f"{condition.algorithm}.py",
        sim_root / "algorithms" / "base.py",
        sim_root / "core" / "robot.py",
    ]
    memory_source = sim_root / "algorithms" / "memory_optimized.py"
    if memory_source.exists() and condition.algorithm in {
        "CBAA",
        "ACBBA",
        "PI",
        "HIPC",
    }:
        paths.append(memory_source)
    optimized_dga_source = sim_root / "algorithms" / "DGA_optimized.py"
    if (
        condition.mission == "bayesian"
        and condition.algorithm == "DGA"
        and optimized_dga_source.exists()
    ):
        paths.append(optimized_dga_source)
    return paths


def _import_environment(condition: Condition):
    if condition.mission == "bayesian":
        if str(BAYESIAN_SIM_ROOT) not in sys.path:
            sys.path.insert(0, str(BAYESIAN_SIM_ROOT))
        from benchmark_sim.comms.models import make_comm_model
        from benchmark_sim.config import SimConfig
        from benchmark_sim.core.scenario_loader import load_scenarios
        from benchmark_sim.core.scheduler import AsyncTrialRunner

        cfg = SimConfig(
            trial_mode="clue_search",
            grid_size=GRID_SIZE,
            robot_ids=list(ROBOT_IDS),
            start_positions=dict(START_POSITIONS),
            start_headings=dict(START_HEADINGS),
            robot_start_layout="edge_even",
            condition_id=condition.condition_id,
            debug_max_events=100_000,
            commitment_horizon=COMMITMENT_HORIZON,
            max_candidate_cells=condition.top_k_cells,
            study_profile="topk_filter",
            top_k_rate=condition.top_k_rate,
        )
        scenarios = load_scenarios(str(cohort_path("bayesian")), max_trials=None)
        comm = make_comm_model("ideal", None)
    else:
        if str(COLLABORATIVE_SIM_ROOT) not in sys.path:
            sys.path.insert(0, str(COLLABORATIVE_SIM_ROOT))
        from known_visit_sim.comms.models import make_comm_model
        from known_visit_sim.config import SimConfig
        from known_visit_sim.core.scheduler import AsyncTrialRunner
        from known_visit_sim.core.scenario_loader import load_scenarios

        cfg = SimConfig(
            grid_size=GRID_SIZE,
            robot_ids=list(ROBOT_IDS),
            start_positions=dict(START_POSITIONS),
            start_headings=dict(START_HEADINGS),
            robot_start_layout="edge_even",
            condition_id=condition.condition_id,
            debug_max_events=100_000,
            debug_max_stagnant_events=20_000,
            commitment_horizon=COMMITMENT_HORIZON,
            max_candidate_cells=condition.top_k_cells,
        )
        scenarios = load_scenarios(
            cohort_path("collaborative"),
            GRID_SIZE,
            set(START_POSITIONS.values()),
            None,
        )
        comm = make_comm_model("ideal", None)
    module_name = ALGORITHM_MODULES[condition.mission][condition.algorithm]
    module = importlib.import_module(module_name)
    allocator_class = getattr(module, ALGORITHM_CLASSES[condition.algorithm])
    return cfg, scenarios, comm, AsyncTrialRunner, allocator_class, module


class CallRecorder:
    def __init__(self, condition: Condition, allocator_class: type) -> None:
        self.condition = condition
        self.allocator_class = allocator_class
        self.current_trial_id = -1
        self.calls_by_robot: dict[str, int] = defaultdict(int)
        self.fixtures: list[dict[str, Any]] = []

    def wrapper(self, original):
        recorder = self

        def traced(allocator, robot):
            rid = str(robot.rid)
            call_index = recorder.calls_by_robot[rid]
            recorder.calls_by_robot[rid] += 1
            pre = snapshot(robot, allocator)
            counters = getattr(robot, "counters", None)
            filter_samples = getattr(
                counters,
                "candidate_filter_time_ns_samples",
                [],
            )
            filter_index = len(filter_samples)
            started = perf_counter_ns()
            decision = original(allocator, robot)
            elapsed = max(0, perf_counter_ns() - started)
            nested_samples = filter_samples[filter_index:]
            post = snapshot(robot, allocator)
            messages = expected_messages_from_state(
                recorder.allocator_class,
                post,
            )
            fixture_id = (
                f"{recorder.condition.condition_id}/"
                f"trial_{recorder.current_trial_id:03d}/"
                f"robot_{rid}/call_{call_index:05d}"
            )
            recorder.fixtures.append(
                {
                    "schema": 1,
                    "fixture_id": fixture_id,
                    "condition_id": recorder.condition.condition_id,
                    "mission": recorder.condition.mission,
                    "algorithm": recorder.condition.algorithm,
                    "top_k_level": recorder.condition.top_k_level,
                    "top_k_rate": recorder.condition.top_k_rate,
                    "top_k_cells": recorder.condition.top_k_cells,
                    "trial_id": recorder.current_trial_id,
                    "robot_id": rid,
                    "call_index": call_index,
                    "pre_state": pre,
                    "expected": {
                        "goal": encode_value(decision.goal),
                        "messages": encode_value(messages),
                        # Keep the complete post-call state as well as its
                        # digest.  The digest is the fast on-device check; the
                        # state makes parity failures auditable and diffable.
                        "post_state": post,
                        "post_state_sha256": state_fingerprint(post),
                        "post_robot_attr_names": sorted(post["robot_attrs"]),
                        "post_allocator_attr_names": sorted(
                            post["allocator_attrs"]
                        ),
                    },
                    "simulator": {
                        "allocator_time_ns": elapsed,
                        "candidate_filter_time_ns": sum(nested_samples),
                        "candidate_filter_calls": len(nested_samples),
                        "candidate_count_before": int(
                            getattr(robot, "candidate_count_before_filter", 0)
                            or 0
                        ),
                        "candidate_count_after": int(
                            getattr(robot, "candidate_count_after_filter", 0)
                            or 0
                        ),
                    },
                    "call_class": classify_call(
                        recorder.condition.algorithm,
                        pre,
                        post,
                        len(nested_samples),
                    ),
                    "risk_score": float(elapsed)
                    + 1_000_000.0
                    * float(
                        getattr(robot, "candidate_count_before_filter", 0) or 0
                    ),
                }
            )
            return decision

        return traced


def capture_condition(
    condition: Condition,
    *,
    max_trials: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    output_root = trace_condition_root(condition)
    complete_path = output_root / "manifest.json"
    expected_trial_count = (
        max_trials
        if max_trials is not None
        else minimum_capture_trial_count(condition)
    )
    if complete_path.exists() and not force:
        existing = json.loads(complete_path.read_text(encoding="utf-8"))
        if (
            existing.get("status") == "complete"
            and int(existing.get("trial_count", -1)) >= expected_trial_count
        ):
            return existing
    output_root.mkdir(parents=True, exist_ok=True)
    cfg, scenarios, comm, runner_class, allocator_class, algorithm_module = (
        _import_environment(condition)
    )
    if max_trials is not None:
        scenarios = scenarios[:max_trials]
    recorder = CallRecorder(condition, allocator_class)
    original = allocator_class.choose_goal
    allocator_class.choose_goal = recorder.wrapper(original)
    trial_entries: list[dict[str, Any]] = []
    total_fixtures = 0
    total_simulator_allocator_ns = 0
    maximum_risk_score = 0.0
    try:
        for scenario in scenarios:
            recorder.current_trial_id = int(scenario.trial_id)
            recorder.calls_by_robot.clear()
            recorder.fixtures = []
            state = runner_class(
                cfg=cfg,
                allocator_cls=allocator_class,
                comm_model=comm,
                seed=SIMULATION_SEED + int(scenario.trial_id) * 1009,
            ).run_trial(scenario)
            trace_path = output_root / f"trial_{int(scenario.trial_id):03d}.jsonl.gz"
            details = write_trace(trace_path, recorder.fixtures)
            total_fixtures += int(details["fixture_count"])
            trial_simulator_ns = sum(
                int(fixture["simulator"]["allocator_time_ns"])
                for fixture in recorder.fixtures
            )
            trial_maximum_risk = max(
                (
                    float(fixture.get("risk_score", 0.0))
                    for fixture in recorder.fixtures
                ),
                default=0.0,
            )
            total_simulator_allocator_ns += trial_simulator_ns
            maximum_risk_score = max(maximum_risk_score, trial_maximum_risk)
            trial_entries.append(
                {
                    "trial_id": int(scenario.trial_id),
                    "trace": trace_path.name,
                    "fixture_count": details["fixture_count"],
                    "content_sha256": details["content_sha256"],
                    "file_sha256": sha256_file(trace_path),
                    "events_processed": int(state.events_processed),
                    "simulator_allocator_time_ns": trial_simulator_ns,
                    "maximum_risk_score": trial_maximum_risk,
                }
            )
    finally:
        allocator_class.choose_goal = original
    source_paths = simulator_source_paths(condition)
    manifest = {
        "schema": 1,
        "status": "complete",
        "condition": {
            "condition_id": condition.condition_id,
            "mission": condition.mission,
            "algorithm": condition.algorithm,
            "top_k_level": condition.top_k_level,
            "top_k_rate": condition.top_k_rate,
            "top_k_cells": condition.top_k_cells,
        },
        "cohort_file": str(cohort_path(condition.mission).resolve()),
        "cohort_sha256": sha256_file(cohort_path(condition.mission)),
        "simulator_source_sha256": _source_hash(source_paths),
        "simulator_source_files": {
            str(path.resolve()): sha256_file(path)
            for path in source_paths
        },
        "trial_count": len(trial_entries),
        "fixture_count": total_fixtures,
        "simulator_allocator_time_ns": total_simulator_allocator_ns,
        "maximum_risk_score": maximum_risk_score,
        "trials": trial_entries,
    }
    _atomic_json(complete_path, manifest)
    return manifest
