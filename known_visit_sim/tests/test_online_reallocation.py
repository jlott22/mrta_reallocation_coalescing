from __future__ import annotations

import unittest

from known_visit_sim.algorithms.base import AllocatorBase
from known_visit_sim.algorithms.registry import load_allocator_class
from known_visit_sim.comms.message import ENVIRONMENT_SENDER, Message
from known_visit_sim.comms.models import BernoulliModel, IdealModel
from known_visit_sim.config import SimConfig, edge_even_start_positions, generate_robot_ids
from known_visit_sim.core.reallocation import (
    ReallocationPolicy,
    TaskState,
    normalize_release_times,
)
from known_visit_sim.core.robot import PendingAction
from known_visit_sim.core.scheduler import AsyncTrialRunner
from known_visit_sim.core.types import AllocationDecision, TrialScenario


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


def run_causal_allocator_call(robot, now_s: float, reason: str | None = None):
    """Run one public causal allocator transaction without advancing motion."""

    trigger = reason or robot.causal_allocation_reason()
    if trigger is None:
        raise AssertionError("robot has no causal allocation reason")
    prepared = robot.prepare_causal_allocation(now_s, trigger)
    staged = robot.stage_causal_allocation(prepared)
    robot.complete_causal_allocation(now_s, staged)
    return prepared, staged


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

    def test_eager_opens_one_exact_single_task_epoch_per_release(self) -> None:
        scenario = TrialScenario(2, [(1, 2), (2, 2), (3, 2)])
        state = new_state(scenario, [0.0, 1.0, 1.0], ReallocationPolicy.eager())
        scheduler = state.reallocation_scheduler
        epoch = scheduler.release_due(1.0)
        arrivals = [
            item for item in scheduler.epochs
            if item.trigger_reason == "task_arrival_eager"
        ]
        self.assertEqual([item.admitted_count for item in arrivals], [1, 1])
        self.assertTrue(all(item.opened_time_s == 1.0 for item in arrivals))
        self.assertIs(epoch, arrivals[-1])
        # World admission and robot knowledge are distinct causal boundaries.
        self.assertEqual(state.robots["00"].active_tasks, set())
        state.bus.pump(1.0)
        self.assertEqual(state.robots["00"].active_tasks, set(scenario.targets))

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

    def test_count_policy_has_no_final_release_flush(self) -> None:
        scenario = TrialScenario(4, [(1, 2), (2, 2), (3, 2)])
        state = new_state(scenario, [0.0, 1.0, 2.0], ReallocationPolicy.count(8))
        scheduler = state.reallocation_scheduler
        self.assertIsNone(scheduler.release_due(1.0))
        self.assertIsNone(scheduler.release_due(2.0))
        self.assertEqual(scheduler.pending_count, 2)
        self.assertEqual(scheduler.unreleased_count, 0)
        self.assertNotIn(
            "final_release_flush", [epoch.trigger_reason for epoch in scheduler.epochs]
        )

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

    def test_mandatory_event_never_piggybacks_pending_tasks(self) -> None:
        scenario = TrialScenario(6, [(1, 2), (2, 2), (3, 2), (4, 2)])
        state = new_state(
            scenario, [0.0, 1.0, 2.0, 100.0], ReallocationPolicy.count(8)
        )
        scheduler = state.reallocation_scheduler
        scheduler.release_due(1.0)
        scheduler.release_due(2.0)
        epoch = scheduler.mandatory_event(2.5, "task_completion")
        self.assertTrue(epoch.mandatory)
        self.assertFalse(epoch.piggybacked_pending)
        self.assertEqual(epoch.trigger_reason, "task_completion")
        self.assertEqual(epoch.admitted_count, 0)
        self.assertEqual(scheduler.pending_count, 2)
        self.assertEqual(
            [
                state.world.target_records[cell].admission_time_s
                for cell in scenario.targets[1:3]
            ],
            [None, None],
        )

    def test_terminal_residual_waits_for_physical_completion_of_all_prior_work(self) -> None:
        first, residual_a, residual_b = (1, 2), (2, 2), (3, 2)
        state = new_state(
            TrialScenario(25, [first, residual_a, residual_b]),
            [0.0, 1.0, 2.0],
            ReallocationPolicy.count(8),
        )
        scheduler = state.reallocation_scheduler
        scheduler.release_due(1.0)
        self.assertIsNone(scheduler.release_due(2.0))
        self.assertEqual(scheduler.pending_count, 2)

        # Assignment/ownership is not physical progress and cannot open the tail.
        state.world.record_assignment("00", [first], 2.5)
        self.assertIsNone(scheduler.maybe_admit_terminal_residual(2.5))
        self.assertEqual(scheduler.pending_count, 2)

        state.world.record_target_visit("00", first, 3.0)
        epoch = scheduler.maybe_admit_terminal_residual(3.0)
        self.assertIsNotNone(epoch)
        self.assertEqual(epoch.trigger_reason, "terminal_residual")
        self.assertTrue(epoch.terminal_residual)
        self.assertEqual(epoch.admitted_count, 2)
        self.assertEqual(scheduler.pending_count, 0)

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
        run_causal_allocator_call(robot, 2.1, "consensus/internal")
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
        self.assertNotIn("final_release_flush", reasons)
        self.assertNotIn("terminal_residual", reasons)
        self.assertEqual(scheduler.pending_count, 2)
        self.assertEqual(max(sample.depth for sample in scheduler.queue_samples), 4)


class OnlineIntegrationTests(unittest.TestCase):
    def test_allocator_snapshot_has_no_world_or_future_task_universe(self) -> None:
        initial, future = (1, 2), (4, 4)
        state = new_state(
            TrialScenario(27, [initial, future]),
            [0.0, 100.0],
            ReallocationPolicy.eager(),
        )
        robot = state.robots["00"]
        self.assertFalse(hasattr(robot, "world"))
        before = robot.causal_allocator_snapshot()
        self.assertNotIn("all_tasks", before["cfg"])
        self.assertEqual(before["views"]["active_tasks"]["v"], [])

        state.bus.pump(0.0)
        after = robot.causal_allocator_snapshot()
        encoded_active = after["views"]["active_tasks"]["v"]
        self.assertEqual(len(encoded_active), 1)
        self.assertNotIn(str(list(future)), str(after))

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
        # Admission is reliable, but it still crosses the configured message
        # boundary before becoming robot knowledge.
        self.assertTrue(all(cell in robot.searched for robot in state.robots.values()))
        state.bus.pump(5.0)

        self.assertTrue(all(cell not in robot.searched for robot in state.robots.values()))
        self.assertEqual(state.world.visits[cell].total_visits, visits_before)
        self.assertEqual(state.world.target_records[cell].total_visits, 0)

    def test_task_knowledge_requires_authenticated_reliable_admission_message(self) -> None:
        cell = (2, 2)
        state = AsyncTrialRunner(
            config(robot_count=2, comm_delay_s=0.25),
            load_allocator_class("CBAA"),
            # Admission bypasses even a 100% lossy evaluated radio model.
            BernoulliModel(1.0),
            seed=13,
        ).new_online_trial(
            TrialScenario(23, [cell]), [5.0], ReallocationPolicy.eager()
        )
        receiver = state.robots["01"]

        # A robot cannot forge the reserved environment control message.
        with self.assertRaisesRegex(ValueError, "reserved environment"):
            state.bus.publish(
                "00", "robot/00/task_admission",
                {"admitted_cells": [list(cell)]}, 4.0,
            )
        receiver.receive_message(
            Message(
                "00", "robot/00/task_admission",
                {"epoch_index": 999, "admitted_cells": [list(cell)]},
                created_at_s=4.0, delivered_at_s=4.0,
            )
        )
        self.assertNotIn(cell, receiver.active_tasks)

        state.reallocation_scheduler.release_due(5.0)
        self.assertNotIn(cell, receiver.active_tasks)
        self.assertEqual(state.bus.pump(5.249), ())
        delivered = state.bus.pump(5.25)
        self.assertEqual(set(delivered), {"00", "01"})
        self.assertIn(cell, receiver.active_tasks)
        record = state.world.target_records[cell]
        self.assertEqual(record.knowledge_receipt_time_s_by_robot["01"], 5.25)
        self.assertEqual(
            state.reallocation_scheduler.epochs[-1]
            .announcement_delivery_time_s_by_robot["01"],
            5.25,
        )

    def test_peer_state_never_completes_a_task_but_explicit_completion_does(self) -> None:
        cell = (2, 2)
        state = AsyncTrialRunner(
            config(robot_count=2), load_allocator_class("CBAA"), IdealModel(), seed=13
        ).new_online_trial(
            TrialScenario(26, [cell]), [5.0], ReallocationPolicy.eager()
        )
        state.reallocation_scheduler.release_due(5.0)
        state.bus.pump(5.0)
        receiver = state.robots["01"]

        receiver.receive_message(
            Message(
                "00", "robot/00/state", {"loc": list(cell)},
                created_at_s=5.0, delivered_at_s=6.0,
            )
        )
        self.assertIn(cell, receiver.active_tasks)
        self.assertFalse(state.world.target_records[cell].completed)

        receiver.receive_message(
            Message(
                "00", "robot/00/task_completion", {"cell": list(cell)},
                created_at_s=6.0, delivered_at_s=6.1,
            )
        )
        self.assertNotIn(cell, receiver.active_tasks)
        # Peer knowledge cannot fabricate a physical world visit.
        self.assertFalse(state.world.target_records[cell].completed)

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
                self.assertNotIn(
                    "task_completion",
                    [
                        epoch.trigger_reason
                        for epoch in state.reallocation_scheduler.epochs
                    ],
                )

    def test_no_pre_release_assignment_or_completion_and_timestamp_order(self) -> None:
        scenario = TrialScenario(10, [(1, 2), (2, 2)])
        state = new_state(scenario, [0.0, 3.0], ReallocationPolicy.count(4))
        future = state.world.target_records[(2, 2)]
        self.assertEqual(future.state, TaskState.UNRELEASED)
        self.assertEqual(state.world.record_target_visit("00", (2, 2), 1.0), (False, False))
        self.assertIsNone(future.first_completion_time_s)
        state.reallocation_scheduler.release_due(3.0)
        self.assertEqual(future.state, TaskState.PENDING)
        self.assertIsNone(future.admission_time_s)

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

    def test_cbaa_movement_does_not_rebid_retained_task(self) -> None:
        state = new_state(
            TrialScenario(111, [(2, 2)]),
            [0.0],
            ReallocationPolicy.eager(),
        )
        state.bus.pump(0.0)
        robot = state.robots["00"]
        run_causal_allocator_call(robot, 0.0)
        goal = robot.current_goal
        self.assertIsNotNone(goal)
        bid = robot.cbaa_winning_bid_by_cell[goal]

        robot.pos = (0, 1)
        decision = robot.allocator.choose_goal(robot)
        messages = robot.allocator.build_cbaa_messages(robot)

        self.assertEqual(decision.goal, goal)
        self.assertEqual(robot.cbaa_winning_bid_by_cell[goal], bid)
        self.assertEqual(messages, [])

    def test_rapid_eager_arrivals_queue_context_for_every_epoch(self) -> None:
        scenario = TrialScenario(12, [(1, 2), (2, 2)])
        state = new_state(scenario, [0.1, 0.2], ReallocationPolicy.eager())
        scheduler = state.reallocation_scheduler
        first = scheduler.release_due(0.1)
        second = scheduler.release_due(0.2)
        # Epoch existence is environment truth; allocator context appears only
        # once its authenticated announcement is delivered.
        self.assertFalse(scheduler.has_queued_context("00"))
        state.bus.pump(0.2)
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
        self.assertEqual(state.online_metrics()["allocator_call_count"], 1)

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
        self.assertGreater(
            metrics["logical_allocation_payload_bytes_sent_total"], 0
        )
        self.assertEqual(
            metrics["logical_message_payload_bytes_sent_total"],
            sum(metrics["logical_payload_bytes_sent_by_topic"].values()),
        )
        self.assertTrue(all(row["assignment_events"] >= 1 for row in state.task_rows()))

    def test_task_completions_never_open_global_epochs(self) -> None:
        scenario = TrialScenario(21, [(1, 2), (2, 2)])
        state = AsyncTrialRunner(
            config(), load_allocator_class("CBAA"), IdealModel(), seed=10
        ).run_online_trial(scenario, [0.0, 0.0], ReallocationPolicy.eager())
        self.assertNotIn(
            "task_completion",
            [epoch.trigger_reason for epoch in state.reallocation_scheduler.epochs],
        )

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


class AgentBoundaryTests(unittest.TestCase):
    def test_decoded_allocator_input_is_applied_only_inside_timed_call(self) -> None:
        class InputSpyAllocator(AllocatorBase):
            name = "InputSpy"

            def initialize(self, robot) -> None:
                robot.input_was_applied = False
                robot.choose_observed_input = False

            def receive_message(self, robot, payload) -> None:
                robot.input_was_applied = payload.get("marker") == "decoded"

            def choose_goal(self, robot) -> AllocationDecision:
                robot.choose_observed_input = robot.input_was_applied
                return AllocationDecision(goal=min(robot.active_tasks))

        state = AsyncTrialRunner(
            config(), InputSpyAllocator, IdealModel(), seed=31
        ).new_trial(TrialScenario(31, [(1, 2)]))
        robot = state.robots["00"]
        run_causal_allocator_call(robot, 0.0)
        robot.input_was_applied = False
        robot.choose_observed_input = False

        robot.receive_message(
            Message(
                "peer",
                "robot/peer/cbaa_entry",
                {"type": "cbaa_entry", "x": 1, "y": 2, "marker": "decoded"},
                created_at_s=1.0,
                delivered_at_s=1.0,
            )
        )
        self.assertFalse(robot.input_was_applied)
        prepared = robot.prepare_causal_allocation(1.0, "allocator_message")
        self.assertEqual(prepared.allocator_input_count, 1)
        self.assertFalse(robot.input_was_applied)
        staged = robot.stage_causal_allocation(prepared)
        self.assertTrue(robot.input_was_applied)
        self.assertTrue(robot.choose_observed_input)
        robot.complete_causal_allocation(1.0, staged)

    def test_admission_preserves_executing_goal_and_queued_motion(self) -> None:
        initial = [(1, 0), (2, 0), (3, 0), (4, 0)]
        newly_admitted = (4, 4)
        path_attr = {
            "ACBBA": "acbba_path",
            "PI": "pi_path",
            "HIPC": "hipc_path",
        }
        for algorithm in ("CBAA", "ACBBA", "PI", "HIPC"):
            with self.subTest(algorithm=algorithm):
                state = AsyncTrialRunner(
                    config(), load_allocator_class(algorithm), IdealModel(), seed=32
                ).new_online_trial(
                    TrialScenario(32, [*initial, newly_admitted]),
                    [0.0, 0.0, 0.0, 0.0, 5.0],
                    ReallocationPolicy.eager(),
                )
                robot = state.robots["00"]
                state.bus.pump(0.0)
                run_causal_allocator_call(robot, 0.0)
                executing_goal = robot.current_goal
                retained_path = (
                    list(getattr(robot, path_attr[algorithm]))
                    if algorithm in path_attr else None
                )
                robot.pending_actions.append(
                    PendingAction(kind="move", target=(0, 1), heading=(0, 1))
                )
                queued_motion = list(robot.pending_actions)

                state.reallocation_scheduler.release_due(5.0)
                state.bus.pump(5.0)
                self.assertEqual(robot.current_goal, executing_goal)
                self.assertEqual(list(robot.pending_actions), queued_motion)
                self.assertIn(newly_admitted, robot.active_tasks)
                if retained_path is not None:
                    self.assertEqual(getattr(robot, path_attr[algorithm]), retained_path)

                # The allocator may adapt its suffix to the expanded pool, but
                # it cannot recall the executing head or pending motion.
                run_causal_allocator_call(robot, 5.0)
                self.assertEqual(robot.current_goal, executing_goal)
                self.assertEqual(list(robot.pending_actions), queued_motion)

    def test_unrelated_peer_completion_uses_allocator_repair_without_recall(self) -> None:
        tasks = [(1, 0), (2, 0), (3, 0), (4, 0), (4, 1)]
        path_attr = {
            "ACBBA": "acbba_path",
            "PI": "pi_path",
            "HIPC": "hipc_path",
        }
        for algorithm in ("CBAA", "ACBBA", "PI", "HIPC"):
            with self.subTest(algorithm=algorithm):
                state = AsyncTrialRunner(
                    config(), load_allocator_class(algorithm), IdealModel(), seed=33
                ).new_online_trial(
                    TrialScenario(33, tasks), [0.0] * len(tasks),
                    ReallocationPolicy.eager(),
                )
                robot = state.robots["00"]
                state.bus.pump(0.0)
                run_causal_allocator_call(robot, 0.0)
                executing_goal = robot.current_goal
                path = list(getattr(robot, path_attr[algorithm], ())) if algorithm in path_attr else []
                completed = path[-1] if path else min(robot.active_tasks - {executing_goal})
                robot.pending_actions.append(
                    PendingAction(kind="move", target=(0, 1), heading=(0, 1))
                )
                queued_motion = list(robot.pending_actions)

                robot.receive_message(
                    Message(
                        "peer", "robot/peer/task_completion",
                        {"cell": list(completed)}, 1.0, 1.0,
                    )
                )
                self.assertNotIn(completed, robot.active_tasks)
                self.assertEqual(robot.current_goal, executing_goal)
                self.assertEqual(list(robot.pending_actions), queued_motion)

                run_causal_allocator_call(robot, 1.0)
                self.assertEqual(robot.current_goal, executing_goal)
                self.assertEqual(list(robot.pending_actions), queued_motion)
                if algorithm in path_attr:
                    self.assertNotIn(completed, getattr(robot, path_attr[algorithm]))

    def test_recovery_deadline_is_local_progress_gated_and_advances_per_attempt(self) -> None:
        class NoGoalRecoveryAllocator(AllocatorBase):
            name = "NoGoalRecovery"

            def initialize(self, robot) -> None:
                robot.recovery_attempts = 0

            def choose_goal(self, robot) -> AllocationDecision:
                return AllocationDecision(goal=None)

            def recover_stalled_allocation(self, robot) -> bool:
                robot.recovery_attempts += 1
                return False

        state = AsyncTrialRunner(
            config(robot_count=2, stalled_allocation_recovery_s=10.0),
            NoGoalRecoveryAllocator,
            IdealModel(),
            seed=34,
        ).new_trial(TrialScenario(34, [(2, 2)]))
        robot = state.robots["00"]
        run_causal_allocator_call(robot, 0.0)
        self.assertEqual(robot.causal_control_step(0.0, state.planner).reason, "no_goal")
        self.assertEqual(robot.next_local_wake_s(), 10.0)

        robot.receive_message(
            Message(
                "01", "robot/01/state", {"loc": [4, 3]},
                created_at_s=5.0, delivered_at_s=5.0,
            )
        )
        run_causal_allocator_call(robot, 5.0)
        self.assertEqual(robot.causal_control_step(5.0, state.planner).reason, "no_goal")
        self.assertEqual(robot.next_local_wake_s(), 15.0)
        self.assertFalse(robot.recovery_due(14.999))
        self.assertTrue(robot.recovery_due(15.0))

        prepared, _ = run_causal_allocator_call(robot, 15.0, "stalled_recovery")
        self.assertTrue(prepared.recovery_requested)
        self.assertEqual(robot.recovery_attempts, 1)
        self.assertEqual(robot.causal_control_step(15.0, state.planner).reason, "no_goal")
        self.assertEqual(robot.next_local_wake_s(), 25.0)


if __name__ == "__main__":
    unittest.main()
