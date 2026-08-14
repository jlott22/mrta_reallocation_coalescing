from __future__ import annotations

from pathlib import Path
import copy
import sys
import unittest

ARCHITECTURE = (
    Path(__file__).resolve().parents[3]
    / "Simulation"
    / "Architecture"
)
if str(ARCHITECTURE) not in sys.path:
    sys.path.insert(0, str(ARCHITECTURE))

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
                expected_after = (
                    8
                    if algorithm in ("CBAA", "ACBBA", "PI", "HIPC")
                    else 5
                )
                self.assertEqual(after, expected_after)
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

    def test_cbaa_movement_does_not_rebid_retained_task(self) -> None:
        runtime = create_persistent_runtime(_config("CBAA"))
        runtime.reset_trial({}, _state(3))
        first = runtime.choose_goal()
        runtime.drain_messages()
        slot = runtime.allocator.path[0]
        bid = float(runtime.state.claim_value[slot])

        runtime.apply_delta({"sequence": 1, "pos": [0, 1]})
        second = runtime.choose_goal()

        self.assertEqual(second.goal, first.goal)
        self.assertAlmostEqual(runtime.state.claim_value[slot], bid, places=6)
        self.assertEqual(runtime.drain_messages(), [])

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
        # Primary experimental allocators always see the full admitted pool;
        # a legacy top-k option cannot reintroduce residual tasks.
        self.assertEqual(runtime.candidate_counts(), (3, 3))

    def test_incremental_insertion_and_pi_significance_match_route_definition(self) -> None:
        runtime = create_persistent_runtime(_config("PI"))
        initial = _state(8)
        initial["target_p"] = {
            tuple(cell): 0.25 + index * 0.1
            for index, cell in enumerate(initial["active_tasks"])
        }
        runtime.reset_trial({}, initial)
        allocator = runtime.allocator
        path = [0, 3, 5, 2]
        slot = 7

        base_cost = allocator.route_cost(path)
        brute_costs = []
        for index in range(len(path) + 1):
            candidate = list(path)
            candidate.insert(index, slot)
            brute_costs.append(
                max(0.0, allocator.route_cost(candidate) - base_cost)
            )
        expected_index = min(
            range(len(brute_costs)), key=lambda index: brute_costs[index]
        )
        actual_index, actual_delta = allocator.best_insertion(path, slot)
        self.assertEqual(actual_index, expected_index)
        self.assertAlmostEqual(actual_delta, brute_costs[expected_index])

        base_distance = allocator.route_distance(path)
        brute_distances = []
        for index in range(len(path) + 1):
            candidate = list(path)
            candidate.insert(index, slot)
            brute_distances.append(
                max(
                    0.0,
                    allocator.route_distance(candidate) - base_distance,
                )
            )
        expected_distance_index = min(
            range(len(brute_distances)),
            key=lambda index: brute_distances[index],
        )
        distance_index, distance_delta = allocator.best_distance_insertion(
            path, slot
        )
        self.assertEqual(distance_index, expected_distance_index)
        self.assertAlmostEqual(
            distance_delta, brute_distances[expected_distance_index]
        )

        allocator.path = list(path)
        allocator._refresh_local_significance()
        full_cost = allocator.route_cost(path)
        for index, item in enumerate(path):
            without = path[:index] + path[index + 1 :]
            expected = max(0.0, full_cost - allocator.route_cost(without))
            # Native claims use a compact single-precision array on the RP.
            self.assertAlmostEqual(
                runtime.state.claim_value[item], expected, places=6
            )

    def test_future_all_tasks_are_not_registered_until_admission(self) -> None:
        config = _config("ACBBA")
        config["all_tasks"] = [[1, 1], [9, 9]]
        initial = _state(1)
        initial["all_tasks"] = [[1, 1], [9, 9]]
        runtime = create_persistent_runtime(config)
        runtime.reset_trial({}, initial)

        self.assertIsNone(runtime.state.slot_for_cell((9, 9)))
        runtime.choose_goal()
        retained_path = list(runtime.allocator.path)
        retained_counter = runtime.allocator.bid_counter

        runtime.begin_call_setup()
        runtime.apply_delta(
            {
                "events": [
                    {
                        "kind": "allocation_epoch",
                        "payload": {
                            "epoch_index": 0,
                            "trigger_reason": "arrival_bound",
                            "admitted_cells": [[9, 9]],
                        },
                    }
                ]
            }
        )

        self.assertIsNotNone(runtime.state.slot_for_cell((9, 9)))
        self.assertEqual(runtime.allocator.path, retained_path)
        self.assertEqual(runtime.allocator.bid_counter, retained_counter)
        self.assertEqual(runtime.algorithm_epoch_reset_time_us(), 0)
        runtime.choose_goal()
        self.assertEqual(len(runtime.allocator.path), 2)
        self.assertEqual(runtime.allocator.path[0], retained_path[0])

    def test_primary_bundle_allocators_are_not_capped_at_three(self) -> None:
        initial = _state(8)
        initial["peer_positions"] = {}
        for algorithm in ("ACBBA", "PI", "HIPC"):
            with self.subTest(algorithm=algorithm):
                config = _config(algorithm, limit=2)
                config["robot_ids"] = ["00"]
                runtime = create_persistent_runtime(config)
                runtime.reset_trial({}, initial)

                runtime.choose_goal()

                self.assertEqual(len(runtime.allocator.path), 8)
                self.assertEqual(runtime.candidate_counts(), (8, 8))
                messages = runtime.drain_messages()
                field = (
                    "path_cells" if algorithm == "PI" else "bundle_cells"
                )
                snapshots = [
                    message[field]
                    for message in messages
                    if isinstance(message.get(field), list)
                ]
                self.assertEqual(len(snapshots), 8)
                self.assertTrue(
                    all(item is snapshots[0] for item in snapshots[1:])
                )

    def test_consensus_completion_and_recovery_wait_for_choose(self) -> None:
        config = _config("CBAA")
        config["robot_ids"] = ["00", "01"]
        runtime = create_persistent_runtime(config)
        runtime.reset_trial({}, _state(2))
        first = runtime.choose_goal().goal
        runtime.drain_messages()
        slot = runtime.state.slot_for_cell(first)

        runtime.begin_call_setup()
        runtime.apply_delta(
            {
                "events": [
                    {
                        "kind": "allocator_task_completed",
                        "payload": {
                            "cell": list(first),
                            "reason": "service",
                            "local": True,
                        },
                    },
                    {"kind": "allocator_recovery", "payload": {}},
                ]
            }
        )

        self.assertTrue(runtime.state.is_active(slot))
        self.assertEqual(len(runtime.pending_allocator_events), 2)
        decision = runtime.choose_goal()
        self.assertFalse(runtime.state.is_active(slot))
        self.assertNotEqual(decision.goal, first)
        self.assertTrue(decision.debug["recovery_requested"])
        self.assertEqual(runtime.pending_allocator_events, [])

    def test_recovery_expires_only_local_blockage_without_forged_release(self) -> None:
        for algorithm in ("CBAA", "ACBBA", "PI"):
            with self.subTest(algorithm=algorithm):
                config = _config(algorithm)
                config["robot_ids"] = ["00", "01"]
                runtime = create_persistent_runtime(config)
                runtime.reset_trial({}, _state(3))
                blocking_value = 0.0 if algorithm == "PI" else 1.0e6
                for slot in runtime.state.active_slots():
                    runtime.state.set_claim(slot, 1, blocking_value, 10)
                runtime.allocator.path = []
                runtime.state.current_goal = None

                runtime.begin_call_setup()
                runtime.apply_delta(
                    {
                        "events": [
                            {"kind": "allocator_recovery", "payload": {}}
                        ]
                    }
                )
                decision = runtime.choose_goal()
                messages = runtime.drain_messages()

                owners = [
                    int(runtime.state.claim_owner[slot])
                    for slot in runtime.state.active_slots()
                ]
                self.assertTrue(decision.debug["recovery_applied"])
                self.assertEqual(owners.count(runtime.state.robot_index), 1)
                self.assertEqual(owners.count(1), 2)
                self.assertFalse(
                    any(bool(message.get("released")) for message in messages)
                )

    def test_hipc_recovery_preserves_head_and_expires_peer_prediction(self) -> None:
        config = _config("HIPC")
        config["robot_ids"] = ["00", "01"]
        runtime = create_persistent_runtime(config)
        runtime.reset_trial({}, _state(3))
        head = runtime.state.active_slots()[0]
        runtime.allocator.path = [head]
        runtime.state.set_claim(head, runtime.state.robot_index, -2.0, 1)
        runtime.state.current_goal = int(runtime.state.targets[head])

        runtime.begin_call_setup()
        runtime.apply_delta(
            {"events": [{"kind": "allocator_recovery", "payload": {}}]}
        )
        decision = runtime.choose_goal()

        self.assertTrue(decision.debug["recovery_applied"])
        self.assertEqual(runtime.allocator.path[0], head)
        self.assertEqual(len(runtime.allocator.path), 3)
        self.assertGreaterEqual(
            runtime.allocator.bad_prediction_count["01"],
            runtime.allocator.BAD_PRED_LIMIT,
        )

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
