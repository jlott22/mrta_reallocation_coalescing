from __future__ import annotations

import copy
import hashlib
import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COMMON_DEVICE_ROOT = (
    REPOSITORY_ROOT
    / "Simulation"
    / "Architecture"
    / "allocator_replay"
    / "device"
    / "common"
)
SIMULATOR_ROOT = (
    REPOSITORY_ROOT / "Simulation" / "Architecture" / "simulator"
)
for import_root in (COMMON_DEVICE_ROOT, SIMULATOR_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from allocator_replay.capture.codec import encode_value  # noqa: E402
from allocator_replay.device.build import build_device_bundle  # noqa: E402
from allocator_replay.device.native.bayesian.dmchba_complete import (  # noqa: E402
    DMCHBAAllocator,
)
from benchmark_sim.algorithms.DMCHBA import (  # noqa: E402
    DMCHBAAllocator as ReferenceDMCHBAAllocator,
)
from replay_persistent import (  # noqa: E402
    PersistentRuntimeSlot,
    ReplayPersistentRuntime,
)


GRID_CELLS = [(x, y) for y in range(3) for x in range(3)]
UNIQUE_TASKS = {(1, 0), (0, 1), (2, 1), (1, 2)}
ROW_MAJOR_PROBABILITIES = {
    cell: (index + 1) / 10.0
    for index, cell in enumerate(GRID_CELLS)
}
WORKSPACE_NAMES = (
    "_probability",
    "_penalty",
    "_searched",
    "_valid",
    "_candidate_ids",
    "_assigned_ids",
    "_team_positions",
    "_h_u",
    "_h_v",
    "_h_minimum",
    "_h_match",
    "_h_way",
    "_h_used",
    "_h_assignment",
)


def _config(*, top_k_cells: int | None = None) -> dict:
    return {
        "mission": "bayesian",
        "algorithm": "DMCHBA",
        "grid_size": 3,
        "robot_ids": ["00", "01"],
        "top_k_cells": top_k_cells,
    }


def _robot(
    *,
    probabilities: dict[tuple[int, int], float] | None = None,
    valid_tasks: set[tuple[int, int]] | None = UNIQUE_TASKS,
    top_k_cells: int | None = None,
) -> SimpleNamespace:
    searched = (
        set()
        if valid_tasks is None
        else set(GRID_CELLS) - set(valid_tasks)
    )
    return SimpleNamespace(
        rid="00",
        pos=(0, 0),
        heading=(1, 0),
        grid_size=3,
        robot_ids=["00", "01"],
        peer_positions={"01": (2, 2)},
        known_clues={(1, 1)},
        searched=searched,
        known_obstacles=set(),
        obstacles=set(),
        blocked=set(),
        blocked_cells=set(),
        target_p=dict(probabilities or ROW_MAJOR_PROBABILITIES),
        current_goal=None,
        collision_avoidance_active=False,
        collision_state="",
        cfg=SimpleNamespace(
            max_candidate_cells=top_k_cells,
            commitment_horizon=None,
            trial_mode="clue_search",
            robot_ids=["00", "01"],
        ),
        counters=SimpleNamespace(
            candidate_filter_time_us_samples=[],
            candidate_filter_time_ns_samples=[],
        ),
    )


def _encoded_section(values: dict) -> dict:
    return {
        name: encode_value(value)
        for name, value in values.items()
    }


def _sectioned_state(
    *,
    probabilities: dict[tuple[int, int], float] | None = None,
    valid_tasks: set[tuple[int, int]] | None = UNIQUE_TASKS,
    top_k_cells: int | None = None,
) -> dict:
    robot = _robot(
        probabilities=probabilities,
        valid_tasks=valid_tasks,
        top_k_cells=top_k_cells,
    )
    return {
        "robot_attrs": _encoded_section(
            {
                "rid": robot.rid,
                "pos": robot.pos,
                "heading": robot.heading,
                "grid_size": robot.grid_size,
                "robot_ids": robot.robot_ids,
                "current_goal": robot.current_goal,
                "collision_avoidance_active": (
                    robot.collision_avoidance_active
                ),
                "collision_state": robot.collision_state,
            }
        ),
        "views": _encoded_section(
            {
                "peer_positions": robot.peer_positions,
                "known_clues": robot.known_clues,
                "searched": robot.searched,
                "target_p": robot.target_p,
                "known_obstacles": robot.known_obstacles,
                "obstacles": robot.obstacles,
                "blocked": robot.blocked,
                "blocked_cells": robot.blocked_cells,
            }
        ),
        "cfg": _encoded_section(
            {
                "max_candidate_cells": top_k_cells,
                "commitment_horizon": None,
                "trial_mode": "clue_search",
                "robot_ids": ["00", "01"],
            }
        ),
        "belief": {},
        "allocator_attrs": {},
    }


def _workspace_ids(allocator: DMCHBAAllocator) -> dict[str, int]:
    return {
        name: id(getattr(allocator, name))
        for name in WORKSPACE_NAMES
    }


class CompleteNativeDMCHBATests(unittest.TestCase):
    def test_unique_assignment_matches_reference_semantics(self) -> None:
        reference_robot = _robot()
        native_robot = _robot()
        reference = ReferenceDMCHBAAllocator()
        native = DMCHBAAllocator(_config())

        expected = reference.choose_goal(reference_robot)
        native.prepare_replay_state(native_robot)
        actual = native.choose_goal(native_robot)

        self.assertEqual(expected.goal, (0, 1))
        self.assertEqual(actual.goal, expected.goal)
        self.assertEqual(
            reference_robot.dmchba_path,
            [(0, 1), (1, 0)],
        )
        self.assertEqual(
            native_robot.dmchba_path,
            reference_robot.dmchba_path,
        )
        for key in (
            "alg",
            "mode",
            "dmchba_trigger",
            "dmchba_assigned_count",
            "dmchba_committed_count",
            "dmchba_commitment_horizon",
            "dmchba_candidate_count",
            "dmchba_candidate_count_before_filter",
            "dmchba_candidate_count_after_filter",
            "dmchba_team_size",
            "dmchba_matrix_n",
            "dmchba_allocator_messages",
        ):
            self.assertEqual(actual.debug[key], expected.debug[key], key)
        self.assertEqual(actual.debug["dmchba_trigger"], "clue_changed")
        self.assertEqual(native_robot.dmchba_clones_per_agent, 2)
        self.assertEqual(native_robot.dmchba_pseudotask_count, 0)
        self.assertTrue(actual.debug["dmchba_native_packed"])

    def test_trigger_sequence_retains_commitment_until_event(self) -> None:
        robot = _robot()
        allocator = DMCHBAAllocator(_config())

        def choose():
            allocator.prepare_replay_state(robot)
            return allocator.choose_goal(robot)

        first = choose()
        self.assertEqual(first.debug["dmchba_trigger"], "clue_changed")
        self.assertEqual(robot.dmchba_path, [(0, 1), (1, 0)])
        self.assertEqual(
            len(robot.counters.candidate_filter_time_us_samples),
            1,
        )

        unchanged = choose()
        self.assertIsNone(unchanged.debug["dmchba_trigger"])
        self.assertEqual(robot.dmchba_path, [(0, 1), (1, 0)])
        self.assertEqual(
            len(robot.counters.candidate_filter_time_us_samples),
            1,
        )

        robot.known_clues.add((0, 2))
        later_clue = choose()
        self.assertIsNone(later_clue.debug["dmchba_trigger"])
        self.assertEqual(robot.dmchba_path, [(0, 1), (1, 0)])
        self.assertEqual(
            len(robot.counters.candidate_filter_time_us_samples),
            1,
        )

        robot.collision_avoidance_active = True
        collision = choose()
        self.assertEqual(
            collision.debug["dmchba_trigger"],
            "collision_avoidance",
        )
        self.assertEqual(robot.dmchba_path, [(0, 1), (1, 0)])
        self.assertEqual(
            len(robot.counters.candidate_filter_time_us_samples),
            2,
        )

        collision_still_active = choose()
        self.assertIsNone(
            collision_still_active.debug["dmchba_trigger"]
        )
        self.assertEqual(
            len(robot.counters.candidate_filter_time_us_samples),
            2,
        )

        robot.searched.update(robot.dmchba_path)
        exhausted = choose()
        self.assertEqual(
            exhausted.debug["dmchba_trigger"],
            "path_exhausted",
        )
        self.assertEqual(exhausted.goal, (1, 2))
        self.assertEqual(robot.dmchba_path, [(1, 2)])
        self.assertEqual(exhausted.debug["dmchba_candidate_count"], 2)
        self.assertEqual(exhausted.debug["dmchba_matrix_n"], 2)
        self.assertEqual(
            len(robot.counters.candidate_filter_time_us_samples),
            3,
        )

    def test_topk_uses_shared_probability_distance_cell_order(self) -> None:
        probabilities = {
            (0, 0): 0.1,
            (1, 0): 0.8,
            (2, 0): 0.2,
            (0, 1): 0.3,
            (1, 1): 0.5,
            (2, 1): 0.4,
            (0, 2): 0.2,
            (1, 2): 0.7,
            (2, 2): 0.6,
        }
        robot = _robot(
            probabilities=probabilities,
            valid_tasks=None,
            top_k_cells=3,
        )
        allocator = DMCHBAAllocator(_config(top_k_cells=3))
        allocator.prepare_replay_state(robot)

        decision = allocator.choose_goal(robot)
        candidates = [
            allocator._decode_cell(allocator._candidate_ids[index])
            for index in range(allocator._candidate_count)
        ]

        self.assertEqual(candidates, [(1, 0), (1, 2), (2, 2)])
        self.assertEqual(robot.candidate_count_before_filter, 9)
        self.assertEqual(robot.candidate_count_after_filter, 3)
        self.assertEqual(decision.goal, (1, 0))
        self.assertEqual(robot.dmchba_path, [(1, 0)])
        self.assertEqual(robot.dmchba_clones_per_agent, 2)
        self.assertEqual(robot.dmchba_pseudotask_count, 1)
        self.assertEqual(decision.debug["dmchba_matrix_n"], 4)

    def test_pclear_restore_keeps_constructor_workspaces_and_logical_state(
        self,
    ) -> None:
        config = _config()
        constructed: list[
            tuple[DMCHBAAllocator, dict[str, int]]
        ] = []

        def runtime_factory(trial_config):
            def allocator_factory():
                allocator = DMCHBAAllocator(trial_config)
                constructed.append(
                    (allocator, _workspace_ids(allocator))
                )
                return allocator

            return ReplayPersistentRuntime(allocator_factory)

        slot = PersistentRuntimeSlot(runtime_factory)
        slot.begin_trial(config)
        slot.prepare(
            "00",
            "restore",
            copy.deepcopy(_sectioned_state()),
        )
        first_allocator, constructor_ids = constructed[-1]
        self.assertIs(slot.runtime.allocator, first_allocator)
        self.assertEqual(
            _workspace_ids(first_allocator),
            constructor_ids,
        )
        compact_searched = slot.runtime.robot._views["searched"]
        self.assertIsInstance(compact_searched, bytearray)
        self.assertEqual(len(compact_searched), 9)
        self.assertIs(
            compact_searched,
            slot.runtime.robot._views["local_searched"],
        )
        self.assertIs(
            compact_searched,
            slot.runtime.robot.belief.searched,
        )

        # An ordinary same-context delta must be able to prepare from the
        # compact resident bitmap without losing searched-cell validity.
        before_valid = bytes(first_allocator._valid)
        delta = {
            section: {}
            for section in (
                "robot_attrs",
                "views",
                "cfg",
                "belief",
                "allocator_attrs",
            )
        }
        delta["robot_attrs"]["pos"] = encode_value((0, 0))
        slot.prepare("00", "delta", delta)
        self.assertEqual(bytes(first_allocator._valid), before_valid)
        self.assertIsInstance(
            slot.runtime.robot._views["searched"],
            bytearray,
        )

        first = slot.runtime.choose_goal()
        self.assertEqual(first.goal, (0, 1))
        self.assertEqual(slot.runtime.drain_messages(), [])
        snapshot = copy.deepcopy(slot.runtime.snapshot_minimal())
        self.assertEqual(snapshot["allocator_attrs"], {})
        self.assertTrue(
            {
                "dmchba_path",
                "dmchba_clue_signature",
                "dmchba_last_collision_active",
                "dmchba_last_assignment_signature",
                "dmchba_last_reassignment_reason",
            }.issubset(snapshot["robot_attrs"])
        )
        snapshot_text = repr(snapshot["allocator_attrs"]).lower()
        self.assertNotIn("workspace", snapshot_text)
        self.assertNotIn("_h_", snapshot_text)

        expected = slot.runtime.choose_goal()
        self.assertEqual(expected.goal, (0, 1))
        self.assertEqual(
            slot.runtime.call_class(),
            "cached_or_maintenance",
        )

        restored_state = _sectioned_state()
        restored_state["robot_attrs"].update(
            snapshot["robot_attrs"]
        )
        restored_state["allocator_attrs"].update(
            snapshot["allocator_attrs"]
        )
        slot.clear_context()
        slot.prepare(
            "00",
            "restore",
            copy.deepcopy(restored_state),
        )

        restored_allocator, restored_constructor_ids = constructed[-1]
        self.assertIs(slot.runtime.allocator, restored_allocator)
        self.assertEqual(
            _workspace_ids(restored_allocator),
            restored_constructor_ids,
        )
        restored = slot.runtime.choose_goal()
        self.assertEqual(restored.goal, expected.goal)
        self.assertIsNone(restored.debug["dmchba_trigger"])
        self.assertEqual(
            slot.runtime.robot.dmchba_path,
            [(0, 1), (1, 0)],
        )
        self.assertEqual(
            slot.runtime.call_class(),
            "cached_or_maintenance",
        )
        self.assertEqual(
            slot.runtime.timing_counters()
            .candidate_filter_time_us_samples,
            [],
        )

    def test_device_build_and_factory_select_complete_native_port(
        self,
    ) -> None:
        manifest = build_device_bundle(compile_mpy=False)
        build_root = Path(str(manifest["output"]))
        native_source = (
            REPOSITORY_ROOT
            / "Simulation"
            / "Architecture"
            / "allocator_replay"
            / "device"
            / "native"
            / "bayesian"
            / "dmchba_complete.py"
        ).resolve()
        expected_hash = hashlib.sha256(
            native_source.read_bytes()
        ).hexdigest()

        self.assertEqual(
            manifest["source_provenance"][str(native_source)],
            expected_hash,
        )
        self.assertTrue(
            (build_root / "replay_native_b_dmchba.py").exists()
        )

        module_names = (
            "replay_physical_factory",
            "replay_native_b_dmchba",
        )
        saved_modules = {
            name: sys.modules.pop(name, None)
            for name in module_names
        }
        sys.path.insert(0, str(build_root))
        try:
            importlib.invalidate_caches()
            factory = importlib.import_module(
                "replay_physical_factory"
            )
            native_module = importlib.import_module(
                "replay_native_b_dmchba"
            )
            runtime = factory.create_complete_runtime(_config())
            runtime.reset_trial(
                _config(),
                copy.deepcopy(_sectioned_state()),
            )

            self.assertIs(
                runtime.allocator.__class__,
                native_module.DMCHBAAllocator,
            )
            self.assertEqual(
                runtime.allocator.__class__.__module__,
                "replay_native_b_dmchba",
            )
            self.assertTrue(
                runtime.choose_goal().debug["dmchba_native_packed"]
            )
        finally:
            sys.path.remove(str(build_root))
            for name in module_names:
                sys.modules.pop(name, None)
                if saved_modules[name] is not None:
                    sys.modules[name] = saved_modules[name]

    def test_workspace_is_linear_fixed_count_and_reused(self) -> None:
        configs = (
            {
                "grid_size": 19,
                "robot_ids": ["00", "01", "02", "03"],
                "top_k_cells": 90,
            },
            {
                "grid_size": 19,
                "robot_ids": ["00", "01", "02", "03"],
                "top_k_cells": 180,
            },
            {
                "grid_size": 19,
                "robot_ids": ["00", "01", "02", "03"],
                "top_k_cells": 361,
            },
        )
        small, medium, full = [
            DMCHBAAllocator(config)
            for config in configs
        ]

        self.assertEqual(
            (
                small.matrix_capacity,
                medium.matrix_capacity,
                full.matrix_capacity,
            ),
            (93, 183, 364),
        )
        small_bytes = small.workspace_payload_bytes()
        medium_bytes = medium.workspace_payload_bytes()
        full_bytes = full.workspace_payload_bytes()
        self.assertEqual(
            (medium_bytes - small_bytes) * (361 - 180),
            (full_bytes - medium_bytes) * (180 - 90),
        )
        self.assertLess(full_bytes, 32 * 1024)

        workspace_size = full.matrix_capacity + 1
        for name in (
            "_h_u",
            "_h_v",
            "_h_minimum",
            "_h_match",
            "_h_way",
            "_h_used",
        ):
            self.assertEqual(len(getattr(full, name)), workspace_size)
        self.assertEqual(
            len(full._h_assignment),
            full.matrix_capacity,
        )
        self.assertEqual(len(full._candidate_ids), 361)
        self.assertEqual(len(full._assigned_ids), 361)
        for forbidden in (
            "cost_matrix",
            "base_costs",
            "clone_rows",
            "columns",
        ):
            self.assertFalse(hasattr(full, forbidden), forbidden)

        robot = SimpleNamespace(
            **vars(_robot(valid_tasks=None)),
        )
        robot.grid_size = 19
        robot.pos = (0, 0)
        robot.peer_positions = {
            "01": (0, 6),
            "02": (0, 12),
            "03": (0, 18),
        }
        robot.known_clues = {(1, 1)}
        robot.searched = set()
        robot.known_obstacles = set()
        robot.obstacles = set()
        robot.blocked = set()
        robot.blocked_cells = set()
        robot.target_p = [1.0 / 361.0] * 361
        robot.cfg = SimpleNamespace(
            max_candidate_cells=361,
            commitment_horizon=3,
            trial_mode="clue_search",
            robot_ids=["00", "01", "02", "03"],
        )
        before = _workspace_ids(full)
        full.prepare_replay_state(robot)
        robot.pos = (1, 0)
        full.prepare_replay_state(robot)
        self.assertEqual(_workspace_ids(full), before)


if __name__ == "__main__":
    unittest.main()
