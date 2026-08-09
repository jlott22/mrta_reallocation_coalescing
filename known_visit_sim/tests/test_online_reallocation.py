from __future__ import annotations

import unittest

from known_visit_sim.algorithms.registry import load_allocator_class
from known_visit_sim.comms.message import Message
from known_visit_sim.comms.models import IdealModel
from known_visit_sim.config import SimConfig, edge_even_start_positions, generate_robot_ids
from known_visit_sim.core.reallocation import (
    ReallocationPolicy,
    TaskState,
    normalize_release_times,
)
from known_visit_sim.core.scheduler import AsyncTrialRunner
from known_visit_sim.core.types import TrialScenario


ALGORITHMS = ("CBAA", "ACBBA", "PI", "HIPC", "DMCHBA", "DGA")


def config(robot_count: int = 1, **overrides) -> SimConfig:
    ids = generate_robot_ids(robot_count)
    values = dict(
        grid_size=5,
        robot_ids=ids,
        start_positions=edge_even_start_positions(5, ids),
        comm_delay_s=0.0,
        comm_delay_jitter_s=0.0,
        collision_intent_settle_s=0.0,
        async_initial_spread_s=0.0,
        async_step_jitter_s=0.0,
        debug_max_events=5_000,
    )
    values.update(overrides)
    return SimConfig(**values)


def new_state(
    scenario: TrialScenario,
    releases,
    policy: ReallocationPolicy,
    algorithm: str = "CBAA",
):
    return AsyncTrialRunner(
        config(), load_allocator_class(algorithm), IdealModel(), seed=17
    ).new_online_trial(scenario, releases, policy)


class PolicyTraceTests(unittest.TestCase):
    def test_release_trace_normalization_is_explicit_and_complete(self) -> None:
        scenario = TrialScenario(1, [(1, 2), (2, 2)])
        self.assertEqual(
            normalize_release_times(
                scenario, {"task_0001": 0.0, "task_0002": 3.5}
            ),
            {(1, 2): 0.0, (2, 2): 3.5},
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            normalize_release_times(scenario, {"task_0001": 0.0})

    def test_manifest_task_ids_are_preserved_and_validated(self) -> None:
        scenario = TrialScenario(
            17, [(1, 2), (2, 2)], {"task_ids": ["alpha", "omega"]}
        )
        state = new_state(
            scenario, {"alpha": 0.0, "omega": 2.0}, ReallocationPolicy.eager()
        )
        self.assertEqual(
            [record.task_id for record in state.world.target_records.values()],
            ["alpha", "omega"],
        )
        with self.assertRaisesRegex(ValueError, "unique"):
            new_state(
                TrialScenario(18, [(1, 2), (2, 2)], {"task_ids": ["x", "x"]}),
                [0.0, 1.0],
                ReallocationPolicy.eager(),
            )

    def test_eager_groups_simultaneous_releases_into_one_epoch(self) -> None:
        scenario = TrialScenario(2, [(1, 2), (2, 2), (3, 2)])
        state = new_state(scenario, [0.0, 1.0, 1.0], ReallocationPolicy.eager())
        epoch = state.reallocation_scheduler.release_due(1.0)
        self.assertEqual(epoch.trigger_reason, "task_arrival_eager")
        self.assertEqual(epoch.admitted_count, 2)
        self.assertEqual(epoch.opened_time_s, 1.0)
        self.assertTrue(all(cell in state.robots["00"].active_tasks for cell in scenario.targets))

    def test_count_threshold_admits_all_pending(self) -> None:
        cells = [
            (x, y)
            for y in range(5)
            for x in range(1, 5)
        ]
        for batch_size in (2, 4, 8):
            with self.subTest(batch_size=batch_size):
                targets = cells[: batch_size + 2]
                releases = [0.0] + [float(i) for i in range(1, batch_size + 1)] + [100.0]
                scenario = TrialScenario(3 + batch_size, targets)
                state = new_state(
                    scenario, releases, ReallocationPolicy.count(batch_size)
                )
                scheduler = state.reallocation_scheduler
                epoch = None
                for now_s in range(1, batch_size + 1):
                    epoch = scheduler.release_due(float(now_s))
                self.assertEqual(epoch.trigger_reason, "batch_threshold")
                self.assertEqual(epoch.admitted_count, batch_size)
                self.assertEqual(scheduler.pending_count, 0)

    def test_count_policy_final_release_tail_flushes_less_than_B(self) -> None:
        scenario = TrialScenario(4, [(1, 2), (2, 2), (3, 2)])
        state = new_state(scenario, [0.0, 1.0, 2.0], ReallocationPolicy.count(8))
        scheduler = state.reallocation_scheduler
        self.assertIsNone(scheduler.release_due(1.0))
        epoch = scheduler.release_due(2.0)
        self.assertEqual(epoch.trigger_reason, "final_release_flush")
        self.assertEqual(epoch.admitted_count, 2)
        self.assertEqual(scheduler.unreleased_count, 0)

    def test_bounded_policy_fires_at_exact_oldest_age_deadline(self) -> None:
        scenario = TrialScenario(5, [(1, 2), (2, 2), (3, 2)])
        state = new_state(
            scenario, [0.0, 1.0, 100.0], ReallocationPolicy.bounded(8, 5.0)
        )
        scheduler = state.reallocation_scheduler
        self.assertIsNone(scheduler.release_due(1.0))
        self.assertEqual(scheduler.next_timeout_s(), 6.0)
        self.assertIsNone(scheduler.timeout_due(5.999))
        epoch = scheduler.timeout_due(6.0)
        self.assertEqual(epoch.trigger_reason, "age_timeout")
        self.assertEqual(epoch.oldest_pending_age_s, 5.0)
        self.assertEqual(max(sample.oldest_age_s for sample in scheduler.queue_samples), 5.0)

    def test_mandatory_event_piggybacks_every_pending_task(self) -> None:
        scenario = TrialScenario(6, [(1, 2), (2, 2), (3, 2), (4, 2)])
        state = new_state(
            scenario, [0.0, 1.0, 2.0, 100.0], ReallocationPolicy.count(8)
        )
        scheduler = state.reallocation_scheduler
        scheduler.release_due(1.0)
        scheduler.release_due(2.0)
        epoch = scheduler.mandatory_event(2.5, "task_completion")
        self.assertTrue(epoch.mandatory)
        self.assertTrue(epoch.piggybacked_pending)
        self.assertEqual(epoch.trigger_reason, "task_completion")
        self.assertEqual(epoch.admitted_count, 2)
        self.assertEqual([state.world.target_records[cell].admission_time_s for cell in scenario.targets[1:3]], [2.5, 2.5])

    def test_internal_allocator_call_cannot_piggyback_pending_tasks(self) -> None:
        scenario = TrialScenario(19, [(1, 2), (2, 2), (3, 2), (4, 2)])
        state = new_state(
            scenario, [0.0, 1.0, 2.0, 100.0], ReallocationPolicy.count(4)
        )
        robot = state.robots["00"]
        robot.service_queued_allocation_epochs(0.0)
        scheduler = state.reallocation_scheduler
        scheduler.release_due(1.0)
        scheduler.release_due(2.0)
        epoch_count = len(scheduler.epochs)
        robot._now = 2.1
        robot._choose_goal_with_metrics("consensus/internal")
        self.assertEqual(scheduler.pending_count, 2)
        self.assertEqual(len(scheduler.epochs), epoch_count)

    def test_full_size_count4_trace_reaches_threshold_without_mandatory_events(self) -> None:
        cells = [
            (x, y)
            for y in range(19)
            for x in range(19)
            if x != 0
        ][:50]
        scenario = TrialScenario(20, cells)
        releases = [0.0] * 8 + [float(i) for i in range(1, 43)]
        runner = AsyncTrialRunner(
            config(grid_size=19), load_allocator_class("CBAA"), IdealModel(), seed=5
        )
        state = runner.new_online_trial(
            scenario, releases, ReallocationPolicy.count(4)
        )
        scheduler = state.reallocation_scheduler
        for now_s in range(1, 43):
            scheduler.release_due(float(now_s))
        reasons = [epoch.trigger_reason for epoch in scheduler.epochs]
        self.assertEqual(reasons.count("batch_threshold"), 10)
        self.assertEqual(reasons.count("final_release_flush"), 1)
        self.assertEqual(max(sample.depth for sample in scheduler.queue_samples), 4)


class OnlineIntegrationTests(unittest.TestCase):
    def test_admission_reopens_previously_traversed_cell_for_every_robot(self) -> None:
        cell = (2, 2)
        scenario = TrialScenario(22, [cell])
        state = AsyncTrialRunner(
            config(robot_count=2), load_allocator_class("CBAA"), IdealModel(), seed=12
        ).new_online_trial(scenario, [5.0], ReallocationPolicy.eager())
        for robot in state.robots.values():
            robot.searched.add(cell)
            state.world.record_visit(robot.rid, cell)
        visits_before = state.world.visits[cell].total_visits

        state.reallocation_scheduler.release_due(5.0)

        self.assertTrue(all(cell not in robot.searched for robot in state.robots.values()))
        self.assertEqual(state.world.visits[cell].total_visits, visits_before)
        self.assertEqual(state.world.target_records[cell].total_visits, 0)

    def test_delayed_pre_admission_state_cannot_infer_task_service(self) -> None:
        cell = (2, 2)

        def admitted_state():
            state = AsyncTrialRunner(
                config(robot_count=2),
                load_allocator_class("CBAA"),
                IdealModel(),
                seed=13,
            ).new_online_trial(
                TrialScenario(23, [cell]), [5.0], ReallocationPolicy.eager()
            )
            state.reallocation_scheduler.release_due(5.0)
            return state

        stale_state = admitted_state()
        stale_receiver = stale_state.robots["01"]
        stale_receiver.receive_message(
            Message(
                "00", "robot/00/state", {"loc": list(cell)},
                created_at_s=4.999, delivered_at_s=6.0,
            )
        )
        self.assertIn(cell, stale_receiver.active_tasks)

        boundary_state = admitted_state()
        boundary_receiver = boundary_state.robots["01"]
        boundary_receiver.receive_message(
            Message(
                "00", "robot/00/state", {"loc": list(cell)},
                created_at_s=5.0, delivered_at_s=6.0,
            )
        )
        self.assertNotIn(cell, boundary_receiver.active_tasks)

        truth_state = admitted_state()
        truth_receiver = truth_state.robots["01"]
        truth_state.world.record_assignment("00", [cell], 5.0)
        truth_state.world.record_target_visit("00", cell, 5.0)
        truth_receiver.receive_message(
            Message(
                "00", "robot/00/state", {"loc": list(cell)},
                created_at_s=4.999, delivered_at_s=6.0,
            )
        )
        self.assertNotIn(cell, truth_receiver.active_tasks)

    def test_all_algorithms_service_task_admitted_under_robot_without_path_failure(self) -> None:
        start_cell = (0, 2)
        next_cell = (1, 2)
        expected_history = [
            TaskState.RELEASED,
            TaskState.PENDING,
            TaskState.ADMITTED,
            TaskState.ASSIGNED,
            TaskState.COMPLETED,
        ]
        for algorithm in ALGORITHMS:
            with self.subTest(algorithm=algorithm):
                step_reasons = []
                state = AsyncTrialRunner(
                    config(), load_allocator_class(algorithm), IdealModel(), seed=14
                ).run_online_trial(
                    TrialScenario(24, [start_cell, next_cell]),
                    [1.0, 2.0],
                    ReallocationPolicy.eager(),
                    on_step=lambda _state, _robot, result: step_reasons.append(result.reason),
                )
                record = state.world.target_records[start_cell]
                self.assertTrue(state.done)
                self.assertNotIn("path_failed", step_reasons)
                self.assertEqual(record.assignment_events, 1)
                self.assertEqual(
                    [event.state for event in record.state_history], expected_history
                )
                self.assertEqual(
                    [
                        record.released_time_s,
                        record.admission_time_s,
                        record.first_assignment_time_s,
                        record.first_completion_time_s,
                    ],
                    [1.0, 1.0, 1.0, 1.0],
                )
                completion_epochs = [
                    epoch for epoch in state.reallocation_scheduler.epochs
                    if epoch.trigger_reason == "task_completion"
                ]
                self.assertEqual(len(completion_epochs), 1)
                self.assertEqual(completion_epochs[0].opened_time_s, 1.0)

    def test_no_pre_release_assignment_or_completion_and_timestamp_order(self) -> None:
        scenario = TrialScenario(10, [(1, 2), (2, 2)])
        state = new_state(scenario, [0.0, 3.0], ReallocationPolicy.count(4))
        future = state.world.target_records[(2, 2)]
        self.assertEqual(future.state, TaskState.UNRELEASED)
        self.assertEqual(state.world.record_target_visit("00", (2, 2), 1.0), (False, False))
        self.assertIsNone(future.first_completion_time_s)
        state.reallocation_scheduler.release_due(3.0)
        self.assertEqual(future.state, TaskState.ADMITTED)
        self.assertEqual(future.admission_time_s, 3.0)

    def test_absolute_release_event_is_not_quantized_to_robot_wakes(self) -> None:
        scenario = TrialScenario(11, [(1, 2), (2, 2)])
        runner = AsyncTrialRunner(
            config(), load_allocator_class("CBAA"), IdealModel(), seed=8
        )
        state = runner.run_online_trial(
            scenario, [0.0, 0.37], ReallocationPolicy.eager()
        )
        record = state.world.target_records[(2, 2)]
        self.assertEqual(record.released_time_s, 0.37)
        self.assertEqual(record.admission_time_s, 0.37)
        self.assertTrue(state.done)
        state.validate_online_invariants()
        times = [
            record.released_time_s,
            record.admission_time_s,
            record.first_assignment_time_s,
            record.first_completion_time_s,
        ]
        self.assertEqual(times, sorted(times))

    def test_rapid_eager_arrivals_queue_context_for_every_epoch(self) -> None:
        scenario = TrialScenario(12, [(1, 2), (2, 2)])
        state = new_state(scenario, [0.1, 0.2], ReallocationPolicy.eager())
        scheduler = state.reallocation_scheduler
        first = scheduler.release_due(0.1)
        second = scheduler.release_due(0.2)
        self.assertTrue(scheduler.has_queued_context("00"))
        state.robots["00"].service_queued_allocation_epochs(0.2)
        self.assertFalse(scheduler.has_queued_context("00"))
        self.assertEqual(len(first.allocator_call_ids), 1)
        self.assertEqual(len(second.allocator_call_ids), 1)
        self.assertEqual(first.called_robot_ids, ["00"])
        self.assertEqual(second.called_robot_ids, ["00"])

    def test_sparse_trace_fast_forwards_without_polling_cap_failure(self) -> None:
        scenario = TrialScenario(13, [(1, 2)])
        state = AsyncTrialRunner(
            config(debug_max_events=20, debug_max_stagnant_events=10),
            load_allocator_class("CBAA"),
            IdealModel(),
            seed=2,
        ).run_online_trial(scenario, [1500.125], ReallocationPolicy.eager())
        record = state.world.target_records[(1, 2)]
        self.assertTrue(state.done)
        self.assertEqual(record.released_time_s, 1500.125)
        self.assertLess(state.events_processed, 10)

    def test_six_retained_algorithms_operate_on_arrival_epochs(self) -> None:
        scenario = TrialScenario(14, [(1, 2)])
        for algorithm in ALGORITHMS:
            with self.subTest(algorithm=algorithm):
                state = AsyncTrialRunner(
                    config(), load_allocator_class(algorithm), IdealModel(), seed=3
                ).run_online_trial(scenario, [1.0], ReallocationPolicy.eager())
                record = state.world.target_records[(1, 2)]
                self.assertTrue(state.done)
                self.assertIsNotNone(record.first_assignment_time_s)
                self.assertGreaterEqual(record.first_assignment_time_s, 1.0)
                self.assertEqual(record.state, TaskState.COMPLETED)

    def test_per_call_epoch_metrics_and_mission_arithmetic(self) -> None:
        scenario = TrialScenario(15, [(1, 2), (2, 2), (3, 2)])
        state = AsyncTrialRunner(
            config(), load_allocator_class("PI"), IdealModel(), seed=4
        ).run_online_trial(
            scenario, [0.0, 0.75, 1.25], ReallocationPolicy.count(2)
        )
        metrics = state.online_metrics()
        self.assertAlmostEqual(
            metrics["mission_elapsed_time_s"],
            metrics["simulated_execution_time_s"],
        )
        self.assertNotIn("mission_elapsed_time_serial_compute_s", metrics)
        self.assertLessEqual(
            metrics["allocator_parallel_critical_path_time_s"],
            metrics["cumulative_allocator_time_s"] + 1e-12,
        )
        self.assertEqual(
            metrics["allocator_call_count"], len(state.allocator_call_rows())
        )
        self.assertEqual(
            metrics["allocation_epoch_count"], len(state.epoch_rows())
        )
        self.assertTrue(all(row["duration_ns"] >= 0 for row in state.allocator_call_rows()))
        self.assertIn("max_pending_age_s", metrics)
        self.assertTrue(all(row["assignment_events"] >= 1 for row in state.task_rows()))

    def test_one_task_completion_opens_only_one_mandatory_epoch(self) -> None:
        scenario = TrialScenario(21, [(1, 2), (2, 2)])
        state = AsyncTrialRunner(
            config(), load_allocator_class("CBAA"), IdealModel(), seed=10
        ).run_online_trial(scenario, [0.0, 0.0], ReallocationPolicy.eager())
        completion_epochs = [
            epoch for epoch in state.reallocation_scheduler.epochs
            if epoch.trigger_reason == "task_completion"
        ]
        self.assertEqual(len(completion_epochs), 1)
        self.assertEqual(completion_epochs[0].expected_robot_ids, ["00"])

    def test_dga_online_seed_is_paired_by_trace_and_changes_with_trace(self) -> None:
        scenario = TrialScenario(16, [(1, 2), (2, 2)])
        runner_a = AsyncTrialRunner(
            config(), load_allocator_class("DGA"), IdealModel(), seed=99
        )
        state_a = runner_a.new_online_trial(scenario, [0.0, 2.0], ReallocationPolicy.eager())
        runner_b = AsyncTrialRunner(
            config(), load_allocator_class("DGA"), IdealModel(), seed=99
        )
        state_b = runner_b.new_online_trial(scenario, [0.0, 2.0], ReallocationPolicy.count(4))
        runner_c = AsyncTrialRunner(
            config(), load_allocator_class("DGA"), IdealModel(), seed=99
        )
        state_c = runner_c.new_online_trial(scenario, [0.0, 3.0], ReallocationPolicy.eager())
        seed_a = state_a.robots["00"].allocator._seed_for_robot(state_a.robots["00"])
        seed_b = state_b.robots["00"].allocator._seed_for_robot(state_b.robots["00"])
        seed_c = state_c.robots["00"].allocator._seed_for_robot(state_c.robots["00"])
        self.assertEqual(seed_a, seed_b)
        self.assertNotEqual(seed_a, seed_c)


if __name__ == "__main__":
    unittest.main()
