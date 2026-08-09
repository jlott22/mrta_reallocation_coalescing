from __future__ import annotations

from pathlib import Path
import copy
import sys
import unittest

from allocator_replay.device.native.collaborative import (
    create_persistent_runtime,
)
from allocator_replay.device.native.collaborative.dga import DGAAllocator

COMMON_DEVICE = (
    Path(__file__).resolve().parents[3]
    / "Simulation"
    / "Architecture"
    / "allocator_replay"
    / "device"
    / "common"
)
if str(COMMON_DEVICE) not in sys.path:
    sys.path.insert(0, str(COMMON_DEVICE))
from allocator_replay.device.common.replay_persistent import (  # noqa: E402
    PersistentRuntimeSlot,
)


ALGORITHMS = ("CBAA", "ACBBA", "PI", "HIPC", "DMCHBA", "DGA")


def _config(algorithm: str, *, limit: int | None = None) -> dict:
    return {
        "algorithm": algorithm,
        "robot_id": "00",
        "robot_ids": ["00", "01", "02", "03"],
        "grid_size": 19,
        "max_candidate_cells": limit,
        "seed": 73,
    }


def _state(count: int = 8) -> dict:
    return {
        "pos": [0, 0],
        "active_tasks": [
            [1 + index % 8, 1 + index // 8] for index in range(count)
        ],
        "peer_positions": {
            "01": [0, 6],
            "02": [0, 12],
            "03": [0, 18],
        },
    }


class NativeCollaborativeTests(unittest.TestCase):
    def test_every_allocator_uses_shared_persistent_interface(self) -> None:
        for algorithm in ALGORITHMS:
            with self.subTest(algorithm=algorithm):
                runtime = create_persistent_runtime(
                    _config(algorithm, limit=5)
                )
                metadata = runtime.reset_trial({}, _state())

                decision = runtime.choose_goal()
                messages = runtime.drain_messages()
                snapshot = runtime.snapshot_minimal()
                before, after = runtime.candidate_counts()
                samples = (
                    runtime.timing_counters()
                    .candidate_filter_time_us_samples
                )

                self.assertTrue(metadata["persistent"])
                self.assertTrue(metadata["motor_free"])
                self.assertIn(list(decision.goal), _state()["active_tasks"])
                self.assertTrue(all(sample >= 0 for sample in samples))
                self.assertEqual(before, 8)
                self.assertEqual(after, 5)
                self.assertIsInstance(messages, list)
                self.assertEqual(
                    snapshot["allocator_attrs"][
                        "native_collaborative_resume"
                    ]["algorithm"],
                    algorithm,
                )
                self.assertEqual(
                    len(
                        snapshot["allocator_attrs"][
                            "native_collaborative_resume"
                        ]["state"]["active"]
                    ),
                    8,
                )

    def test_cbaa_keeps_state_and_applies_idempotent_deltas(self) -> None:
        runtime = create_persistent_runtime(_config("CBAA"))
        runtime.reset_trial({}, _state(3))
        first = runtime.choose_goal()
        runtime.drain_messages()
        second = runtime.choose_goal()

        self.assertEqual(second.goal, first.goal)
        self.assertEqual(second.debug["call_path"], "cached_goal")
        self.assertEqual(
            len(
                runtime.timing_counters()
                .candidate_filter_time_us_samples
            ),
            0,
        )

        runtime.apply_delta(
            {"sequence": 8, "completed_tasks": [first.goal]}
        )
        runtime.apply_delta(
            {"sequence": 8, "completed_tasks": [[2, 1]]}
        )
        self.assertEqual(
            len(
                runtime.snapshot_minimal()["allocator_attrs"][
                    "native_collaborative_resume"
                ]["state"]["active"]
            ),
            2,
        )
        third = runtime.choose_goal()
        self.assertNotEqual(third.goal, first.goal)

    def test_candidate_filter_ranks_probability_then_distance(self) -> None:
        runtime = create_persistent_runtime(_config("CBAA", limit=1))
        initial = _state(3)
        initial["target_p"] = {
            (1, 1): 0.1,
            (2, 1): 0.2,
            (3, 1): 0.9,
        }
        runtime.reset_trial({}, initial)

        decision = runtime.choose_goal()

        self.assertEqual(decision.goal, (3, 1))
        self.assertEqual(runtime.candidate_counts(), (3, 1))

    def test_cbaa_peer_claim_changes_another_robot_decision(self) -> None:
        first_config = _config("CBAA")
        first = create_persistent_runtime(first_config)
        first.reset_trial({}, _state(3))
        won = first.choose_goal().goal
        messages = first.drain_messages()

        second_config = dict(first_config)
        second_config["robot_id"] = "01"
        second = create_persistent_runtime(second_config)
        second_state = _state(3)
        second_state["pos"] = [0, 0]
        second.reset_trial({}, second_state)
        second.apply_delta({"sequence": 1, "messages": messages})
        decision = second.choose_goal()

        self.assertEqual(won, (1, 1))
        self.assertNotEqual(decision.goal, won)

    def test_dmchba_uses_virtual_assignment_workspace(self) -> None:
        runtime = create_persistent_runtime(_config("DMCHBA"))
        runtime.reset_trial({}, _state(7))

        runtime.choose_goal()
        allocator_state = runtime.allocator.minimal_state()

        self.assertEqual(allocator_state["matrix_size"], 8)
        self.assertFalse(hasattr(runtime.allocator, "cost_matrix"))

    def test_dga_full_search_and_mutations_preserve_candidates(self) -> None:
        runtime = create_persistent_runtime(_config("DGA"))
        runtime.reset_trial({}, _state(8))
        engine = runtime.allocator
        self.assertIsInstance(engine, DGAAllocator)
        self.assertEqual(engine.POPULATION_SIZE, 30)
        self.assertEqual(engine.ITERATIONS_PER_TRIGGER, 25)

        candidates = engine.candidates(always_rank=True)
        team = engine._team()
        plan = engine._greedy_seed(team, candidates)
        expected = sorted(candidates)
        for operation in ("move", "swap", "reinsert", "reverse", "clean"):
            with self.subTest(operation=operation):
                mutated = engine._mutate(
                    plan, team, candidates, operation=operation
                )
                flattened = []
                for owner in team:
                    flattened.extend(mutated[owner])
                self.assertEqual(sorted(flattened), expected)
                self.assertEqual(len(flattened), len(set(flattened)))

        child = engine._crossover(plan, plan, team, candidates)
        flattened = []
        for owner in team:
            flattened.extend(child[owner])
        self.assertEqual(sorted(flattened), expected)

    def test_stationary_and_physical_wrappers_are_deterministic(self) -> None:
        config = _config("DGA", limit=6)
        stationary = create_persistent_runtime(config)
        physical = create_persistent_runtime(config)
        stationary.reset_trial({}, _state(8))
        physical.reset_trial({}, _state(8))
        delta = {
            "sequence": 1,
            "pos": [1, 0],
            "peer_positions": {"01": [1, 6]},
        }
        stationary.apply_delta(delta)
        physical.apply_delta(delta)

        stationary_decision = stationary.choose_goal()
        physical_decision = physical.choose_goal()

        self.assertEqual(
            stationary_decision.goal, physical_decision.goal
        )
        self.assertEqual(
            stationary_decision.debug["call_path"],
            physical_decision.debug["call_path"],
        )
        self.assertEqual(
            stationary.drain_messages(), physical.drain_messages()
        )
        self.assertEqual(
            stationary.snapshot_minimal(),
            physical.snapshot_minimal(),
        )

    def test_persistent_worker_slot_goal_delta_and_resume(self) -> None:
        config = _config("CBAA")
        initial = {
            "robot_attrs": {
                "rid": "00",
                "pos": (0, 0),
                "grid_size": 19,
            },
            "views": {
                "active_tasks": {(1, 1), (2, 1), (3, 1)},
                "peer_positions": {
                    "01": (0, 6),
                    "02": (0, 12),
                    "03": (0, 18),
                },
                "target_p": {
                    (1, 1): 1.0,
                    (2, 1): 1.0,
                    (3, 1): 1.0,
                },
            },
            "cfg": {
                "grid_size": 19,
                "robot_ids": ["00", "01", "02", "03"],
                "max_candidate_cells": None,
            },
            "belief": {},
            "allocator_attrs": {},
        }
        slot = PersistentRuntimeSlot(create_persistent_runtime)
        slot.begin_trial(config)
        slot.prepare("00", "restore", copy.deepcopy(initial))
        first = slot.runtime.choose_goal()
        self.assertEqual(first.goal, (1, 1))
        self.assertEqual(slot.runtime.candidate_counts(), (3, 3))
        self.assertEqual(
            len(
                slot.runtime.timing_counters()
                .candidate_filter_time_us_samples
            ),
            1,
        )
        slot.runtime.drain_messages()
        snapshot = slot.runtime.snapshot_minimal()

        slot.prepare(
            "00",
            "delta",
            {"views": {"active_tasks": {(2, 1), (3, 1)}}},
            events=[],
        )
        second = slot.runtime.choose_goal()
        self.assertEqual(second.goal, (2, 1))

        restored_state = copy.deepcopy(initial)
        restored_state["views"]["active_tasks"] = {(2, 1), (3, 1)}
        restored_state["allocator_attrs"].update(
            snapshot["allocator_attrs"]
        )
        new_slot = PersistentRuntimeSlot(create_persistent_runtime)
        new_slot.begin_trial(config)
        new_slot.prepare("00", "restore", restored_state)
        restored = new_slot.runtime.choose_goal()
        self.assertEqual(restored.goal, (2, 1))

    def test_dga_resume_keeps_population_rng_and_next_search(self) -> None:
        config = _config("DGA", limit=6)
        continuous = create_persistent_runtime(config)
        continuous.reset_trial({}, _state(8))
        continuous.choose_goal()
        continuous.drain_messages()
        snapshot = continuous.snapshot_minimal()

        restored_state = _state(8)
        restored_state["allocator_attrs"] = copy.deepcopy(
            snapshot["allocator_attrs"]
        )
        restored = create_persistent_runtime(config)
        restored.reset_trial({}, restored_state)

        self.assertEqual(
            restored.state.rng.state, continuous.state.rng.state
        )
        self.assertEqual(
            restored.allocator.generation,
            continuous.allocator.generation,
        )
        self.assertEqual(
            restored.allocator._signature(
                restored.allocator.population[0]
            ),
            continuous.allocator._signature(
                continuous.allocator.population[0]
            ),
        )

        committed = [
            continuous.state.decode_cell(
                continuous.state.targets[slot]
            )
            for slot in continuous.allocator.path
        ]
        continuous.apply_delta(
            {"sequence": 1, "completed_tasks": committed}
        )
        restored.apply_delta(
            {"sequence": 1, "completed_tasks": committed}
        )
        continuous_decision = continuous.choose_goal()
        restored_decision = restored.choose_goal()
        self.assertEqual(
            restored_decision.goal, continuous_decision.goal
        )
        self.assertEqual(
            restored.drain_messages(), continuous.drain_messages()
        )

    def test_legacy_section_state_is_accepted(self) -> None:
        runtime = create_persistent_runtime(_config("PI"))
        metadata = runtime.reset_trial(
            {},
            {
                "cfg": {"grid_size": 19},
                "robot_attrs": {"rid": "00", "pos": [0, 0]},
                "views": {
                    "active_tasks": [[1, 1], [2, 2]],
                    "peer_positions": {"01": [0, 6]},
                },
            },
        )

        self.assertEqual(metadata["target_count"], 2)
        self.assertIn(runtime.choose_goal().goal, ((1, 1), (2, 2)))

    def test_package_has_no_motor_or_sensor_initialization(self) -> None:
        package = (
            Path(__file__).resolve().parents[3]
            / "Simulation"
            / "Architecture"
            / "allocator_replay"
            / "device"
            / "native"
            / "collaborative"
        )
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in package.glob("*.py")
        ).lower()

        self.assertNotIn("motoron", source)
        self.assertNotIn("vl53", source)
        self.assertNotIn("machine.pin", source)
        self.assertNotIn("machine.uart", source)

    def test_target_capacity_is_explicit(self) -> None:
        runtime = create_persistent_runtime(_config("CBAA"))
        with self.assertRaisesRegex(ValueError, "capacity"):
            runtime.reset_trial({}, _state(51))


if __name__ == "__main__":
    unittest.main()
