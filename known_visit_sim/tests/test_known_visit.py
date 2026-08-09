from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from known_visit_sim.algorithms.registry import load_allocator_class
from known_visit_sim.algorithms.base import AllocatorBase
from known_visit_sim.comms.models import BernoulliModel, IdealModel
from known_visit_sim.config import SimConfig, edge_even_start_positions, generate_robot_ids
from known_visit_sim.core.scenario_loader import load_scenarios
from known_visit_sim.core.scheduler import AsyncTrialRunner
from known_visit_sim.core.types import AllocationDecision, TrialScenario
from known_visit_sim.core.world import World
from known_visit_sim.generate_scenarios import generate
from known_visit_sim.metrics.export import write_outputs
from known_visit_sim.metrics.summary import build_rows, gini
from known_visit_sim.tests.known_visit_horizon.run_known_visit_horizon_trial import make_comm_model


ALGORITHMS = ("CBAA", "ACBBA", "PI", "HIPC", "DMCHBA", "DGA", "AuctionGreedy")
ALLOWED_TOPICS = {
    "state", "collision_intent", "cbaa_entry", "acbba_entry", "pi_entry",
    "pi_clear_path", "hipc_entry", "hipc_clear_bundle", "dga_entry",
}


def config(grid_size: int = 5, robot_count: int = 2, **overrides) -> SimConfig:
    ids = generate_robot_ids(robot_count)
    values = dict(
        grid_size=grid_size,
        robot_ids=ids,
        start_positions=edge_even_start_positions(grid_size, ids),
        comm_delay_s=0.0,
        comm_delay_jitter_s=0.0,
        collision_intent_settle_s=0.0,
        debug_max_events=5_000,
    )
    values.update(overrides)
    return SimConfig(**values)


class GeneratorTests(unittest.TestCase):
    def test_targets_are_deterministic_unique_in_bounds_and_not_starts(self) -> None:
        first = generate(19, 8, 10, 4, 1234)
        self.assertEqual(first, generate(19, 8, 10, 4, 1234))
        starts = set(edge_even_start_positions(19, generate_robot_ids(4)).values())
        for targets in first:
            self.assertEqual(len(targets), len(set(targets)))
            self.assertTrue(all(0 <= x < 19 and 0 <= y < 19 for x, y in targets))
            self.assertTrue(set(targets).isdisjoint(starts))

    def test_scenario_loader_rejects_start_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.csv"
            path.write_text("trial_id,target1_x,target1_y\n0,0,0\n")
            with self.assertRaisesRegex(ValueError, "overlaps"):
                load_scenarios(path, 5, {(0, 0)})


class CommunicationAndWorldTests(unittest.TestCase):
    def test_blocked_goal_quarantine_escalates_without_waiting_to_reallocate(self) -> None:
        cfg = config(replan_delay_s=0.3)
        state = AsyncTrialRunner(
            cfg, load_allocator_class("CBAA"), IdealModel(), 4
        ).new_trial(TrialScenario(0, [(2, 2)]))
        robot = state.robots["00"]
        durations = []
        for now in (10.0, 20.0, 40.0, 100.0):
            robot._now = now
            robot.current_goal = (2, 2)
            robot._blocked_goal_failures[(2, 2)] = 1
            result = robot._maybe_temporarily_invalidate_blocked_goal((2, 2))
            self.assertIsNotNone(result)
            durations.append(robot._temporary_invalid_task_until[(2, 2)] - now)
            self.assertEqual(result.time_cost_s, cfg.replan_delay_s)
        self.assertEqual(durations, [5.0, 15.0, 45.0, 120.0])
        self.assertEqual(robot.counters.blocked_task_quarantines, 4)
        self.assertEqual(robot.counters.blocked_task_quarantine_time_s, 185.0)
        self.assertEqual(robot.counters.maximum_quarantine_level, 4)

    def test_stagnation_detector_reports_no_goal_diagnostics(self) -> None:
        class NoGoalAllocator(AllocatorBase):
            name = "NoGoal"

            def choose_goal(self, robot):
                return AllocationDecision(goal=None, debug={"reason": "test"})

        cfg = config(debug_max_events=100, debug_max_stagnant_events=8)
        runner = AsyncTrialRunner(cfg, NoGoalAllocator, IdealModel(), 1)
        with self.assertRaisesRegex(RuntimeError, r"Stagnation detected.*event_reasons.*no_goal"):
            runner.run_trial(TrialScenario(99, [(2, 2)]))

    def test_horizon_runner_applies_communication_levels_to_the_right_fields(self) -> None:
        ge = make_comm_model("gilbert_elliott", "0.75")
        self.assertAlmostEqual(ge.p_good_to_good, 0.95)
        self.assertAlmostEqual(ge.p_bad_to_bad, 0.85)
        self.assertAlmostEqual(ge.stationary_delivery_prob, 0.75)
        self.assertAlmostEqual(ge.state_correlation, 0.8)
        rayleigh = make_comm_model("rayleigh_style", "-50.66")
        self.assertEqual(rayleigh.sensitivity_dbm, -50.66)
        self.assertEqual(rayleigh.tx_power_dbm, 30.0)

    def test_state_infers_completion_but_does_not_create_world_visit(self) -> None:
        cfg = config()
        scenario = TrialScenario(0, [(2, 2)])
        state = AsyncTrialRunner(cfg, load_allocator_class("CBAA"), IdealModel(), 2).new_trial(scenario)
        sender, receiver = state.robots["00"], state.robots["01"]
        sender.pos = (2, 2)
        sender._last_published_state_pos = None
        sender.publish_state()
        state.bus.pump(1.0)
        self.assertNotIn((2, 2), receiver.active_tasks)
        self.assertFalse(state.world.target_records[(2, 2)].completed)
        self.assertNotIn((2, 2), state.world.visits)

    def test_dropped_state_can_leave_stale_task_and_enable_duplicate_visit(self) -> None:
        cfg = config()
        scenario = TrialScenario(0, [(2, 2)])
        state = AsyncTrialRunner(cfg, load_allocator_class("CBAA"), BernoulliModel(1.0), 3).new_trial(scenario)
        sender, receiver = state.robots["00"], state.robots["01"]
        sender.pos = (2, 2)
        state.world.record_target_visit("00", (2, 2), 2.0)
        sender._complete_task_locally((2, 2), "local_target_visit")
        sender._last_published_state_pos = None
        sender.publish_state()
        sender.publish_collision_intent((2, 3))
        state.bus.pump(1.0)
        self.assertIn((2, 2), receiver.active_tasks)
        self.assertEqual(receiver._collision_peer_intents["00"], (2, 3))
        state.world.record_target_visit("01", (2, 2), 6.0)
        self.assertEqual(state.world.target_records[(2, 2)].duplicate_visits, 1)

    def test_world_truth_records_first_finder_time_and_duplicates(self) -> None:
        world = World(5, TrialScenario(7, [(2, 2)]))
        self.assertEqual(world.record_target_visit("00", (2, 2), 4.5), (True, True))
        self.assertEqual(world.record_target_visit("01", (2, 2), 8.0), (True, False))
        record = world.target_records[(2, 2)]
        self.assertEqual(record.first_completion_time_s, 4.5)
        self.assertEqual(record.first_found_by, "00")
        self.assertEqual(record.duplicate_visits, 1)
        self.assertTrue(world.all_targets_completed())

    def test_world_termination_does_not_require_local_consensus(self) -> None:
        cfg = config()
        scenario = TrialScenario(0, [(1, 0)])
        state = AsyncTrialRunner(cfg, load_allocator_class("AuctionGreedy"), BernoulliModel(1.0), 9).run_trial(scenario)
        self.assertTrue(state.done)
        self.assertTrue(state.world.all_targets_completed())
        stale_non_finders = [
            robot for robot in state.robots.values()
            if robot.counters.targets_found == 0 and (1, 0) in robot.active_tasks
        ]
        self.assertTrue(stale_non_finders)


class AllocatorAndOutputTests(unittest.TestCase):
    def test_all_allocators_complete_ideal_and_degraded_and_emit_only_allowed_topics(self) -> None:
        scenario = TrialScenario(0, [(2, 0), (2, 4)])
        for algorithm in ALGORITHMS:
            for model in (IdealModel(), BernoulliModel(0.1)):
                with self.subTest(algorithm=algorithm, model=model.name):
                    invalid_goals = []

                    def check_active_goals(state, _robot, _result) -> None:
                        invalid_goals.extend(
                            (robot.rid, robot.current_goal)
                            for robot in state.robots.values()
                            if robot.current_goal is not None
                            and robot.current_goal not in robot.active_tasks
                        )

                    state = AsyncTrialRunner(
                        config(), load_allocator_class(algorithm), model, 42
                    ).run_trial(scenario, on_step=check_active_goals)
                    self.assertTrue(state.world.all_targets_completed())
                    self.assertTrue(set(state.bus.counters.sent_by_topic).issubset(ALLOWED_TOPICS))
                    self.assertEqual(invalid_goals, [])

    def test_preserved_multi_task_caps_and_dga_defaults(self) -> None:
        self.assertEqual(load_allocator_class("ACBBA").BUNDLE_SIZE, 3)
        self.assertEqual(load_allocator_class("PI").BUNDLE_SIZE, 3)
        self.assertEqual(load_allocator_class("HIPC").BUNDLE_SIZE, 3)
        self.assertEqual(load_allocator_class("DMCHBA").COMMITMENT_HORIZON, 3)
        dga = load_allocator_class("DGA")
        self.assertEqual(dga.COMMITMENT_HORIZON, 3)
        self.assertEqual(dga.POPULATION_SIZE, 30)
        self.assertEqual(dga.DGA_ITERATIONS_PER_TRIGGER, 25)

    def test_dga_sends_full_prefix_and_empty_owner_clear(self) -> None:
        state = AsyncTrialRunner(
            config(), load_allocator_class("DGA"), IdealModel(), 19
        ).new_trial(TrialScenario(0, [(2, 0), (2, 4)]))
        robot = state.robots["00"]
        allocator = robot.allocator
        plan = {"00": [(1, 0), (2, 0)], "01": []}

        allocator._queue_dga_deltas(robot, plan, 12.0)
        messages = allocator.build_dga_messages(robot)

        self.assertEqual([msg["owner"] for msg in messages], ["00", "00", "01"])
        self.assertEqual([(msg["order"], msg["x"], msg["y"]) for msg in messages[:2]], [(0, 1, 0), (1, 2, 0)])
        self.assertTrue(messages[2]["removed"])
        self.assertEqual(messages[2]["path_size"], 0)

    def test_hipc_clear_bundle_clears_sender_claims(self) -> None:
        state = AsyncTrialRunner(
            config(), load_allocator_class("HIPC"), IdealModel(), 20
        ).new_trial(TrialScenario(0, [(2, 0), (2, 4)]))
        receiver = state.robots["01"]
        receiver.allocator._ensure_hipc_state(receiver)
        receiver.hipc_winner_by_cell[(2, 0)] = "00"
        receiver.hipc_winning_bid_by_cell[(2, 0)] = 3.0
        receiver.hipc_bid_time_by_cell[(2, 0)] = 1.0

        receiver._deliver_allocator_payload({
            "type": "hipc_clear_bundle",
            "sender": "00",
            "timestamp": 2.0,
            "bundle_cells": [],
            "bundle_size": 0,
        })

        self.assertIsNone(receiver.hipc_winner_by_cell[(2, 0)])
        self.assertEqual(receiver.hipc_winning_bid_by_cell[(2, 0)], receiver.allocator.NO_BID)

    def test_dmchba_preserves_local_suffix_and_replans_external_invalidation(self) -> None:
        cfg = config(grid_size=7, commitment_horizon=3)
        scenario = TrialScenario(0, [(2, 0), (3, 0), (4, 0), (5, 0)])

        local_state = AsyncTrialRunner(
            cfg, load_allocator_class("DMCHBA"), IdealModel(), 17
        ).new_trial(scenario)
        local_robot = local_state.robots["00"]
        first = local_robot.allocator.choose_goal(local_robot)
        initial_path = list(local_robot.dmchba_path)
        self.assertGreaterEqual(len(initial_path), 2)

        local_robot._complete_task_locally(initial_path[0], "local_target_visit")
        second = local_robot.allocator.choose_goal(local_robot)
        self.assertIsNone(second.debug["dmchba_trigger"])
        self.assertEqual(local_robot.dmchba_path, initial_path[1:])
        self.assertEqual(second.goal, initial_path[1])

        peer_state = AsyncTrialRunner(
            cfg, load_allocator_class("DMCHBA"), IdealModel(), 18
        ).new_trial(scenario)
        peer_robot = peer_state.robots["00"]
        peer_robot.allocator.choose_goal(peer_robot)
        preserved_path = list(peer_robot.dmchba_path)
        unrelated_cell = next(iter(peer_robot.active_tasks - set(preserved_path)))
        peer_robot._complete_task_locally(unrelated_cell, "peer_state_at_target")
        preserved = peer_robot.allocator.choose_goal(peer_robot)
        self.assertIsNone(preserved.debug["dmchba_trigger"])
        self.assertEqual(peer_robot.dmchba_path, preserved_path)

        invalidated_cell = peer_robot.dmchba_path[-1]
        peer_robot._complete_task_locally(invalidated_cell, "peer_state_at_target")
        replanned = peer_robot.allocator.choose_goal(peer_robot)
        self.assertEqual(
            replanned.debug["dmchba_trigger"], "external_task_invalidated_path"
        )
        self.assertNotIn(invalidated_cell, peer_robot.dmchba_path)

    def test_metrics_and_all_output_files(self) -> None:
        cfg = config(condition_id="smoke")
        state = AsyncTrialRunner(
            cfg, load_allocator_class("CBAA"), IdealModel(), 5
        ).run_trial(TrialScenario(11, [(2, 0), (2, 4)]))
        trial, system, robots, targets = build_rows(
            state, "CBAA", "ideal", "1.0", "paired.csv"
        )
        self.assertTrue(system["all_targets_visited"])
        self.assertIn("max_robot_steps", system)
        self.assertIn("task_cell_revisits_total", system)
        self.assertEqual(
            system["task_cell_revisits_total"], system["duplicate_target_visits"]
        )
        self.assertIn("workload_gini_targets_found", system)
        self.assertIn("events_processed", system)
        self.assertIn("stall_recoveries_total", system)
        self.assertEqual(sum(row["targets_found"] for row in robots), 2)
        self.assertTrue(all("steps_total" in row for row in robots))
        self.assertTrue(all("task_cell_revisits" in row for row in robots))
        self.assertTrue(all("stall_recoveries" in row for row in robots))
        self.assertTrue(all(row["first_completion_time_s"] != "" for row in targets))
        self.assertAlmostEqual(gini([0, 2]), 0.5)
        with tempfile.TemporaryDirectory() as tmp:
            write_outputs(tmp, [trial], [system], robots, targets, {})
            for name in (
                "trial_summary.csv", "system_performance.csv",
                "robot_performance.csv", "target_performance.csv", "config_used.json",
            ):
                self.assertTrue((Path(tmp) / name).is_file())
            with (Path(tmp) / "robot_performance.csv").open(newline="") as handle:
                fields = csv.DictReader(handle).fieldnames or []
            self.assertIn("steps_total", fields)
            self.assertIn("targets_found", fields)


if __name__ == "__main__":
    unittest.main()
