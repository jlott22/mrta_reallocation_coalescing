from __future__ import annotations

import unittest
import csv
import hashlib
import itertools
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from known_visit_sim.algorithms.base import AllocatorBase
from known_visit_sim.algorithms.registry import load_allocator_class
from known_visit_sim.comms.models import IdealModel
from known_visit_sim.config import (
    SimConfig,
    edge_even_start_positions,
    generate_robot_ids,
)
from known_visit_sim.core.reallocation import (
    ReallocationPolicy,
    build_zero_compute_pair_metrics,
)
from known_visit_sim.core.scheduler import AsyncTrialRunner
from known_visit_sim.core.timing import (
    CausalTimingError,
    DEVICE_ALLOCATOR_TIMER_SCOPE,
    DeterministicTimingProvider,
    HostMeasuredTimingProvider,
    ZeroComputeTimingProvider,
)
from known_visit_sim.core.types import AllocationDecision, TrialScenario, manhattan
from known_visit_sim.run_causal_trials import (
    _positive_release_times,
    run_causal_manifest_job,
)
from study.causal.model import (
    BoardBinding,
    CausalConfig,
    CausalJob,
    PolicySpec,
)
from study.causal.outputs import validate_causal_outputs


class ProbeAllocator(AllocatorBase):
    """Small deterministic allocator that exposes message visibility in tests."""

    name = "CBAA"
    observations: list[tuple[str, float, int, tuple[tuple[int, int], ...]]] = []

    def initialize(self, robot) -> None:
        robot.cbaa_probe_messages_seen = 0

    def choose_goal(self, robot) -> AllocationDecision:
        active = tuple(sorted(robot.active_tasks))
        type(self).observations.append(
            (
                robot.rid,
                float(robot._now),
                int(robot.cbaa_probe_messages_seen),
                active,
            )
        )
        robot.publish_algorithm_message(
            "probe_entry", {"type": "probe_entry", "owner": robot.rid}
        )
        candidates = sorted(active, key=lambda cell: (manhattan(robot.pos, cell), cell))
        return AllocationDecision(
            candidates[0] if candidates else None,
            {"messages_seen": int(robot.cbaa_probe_messages_seen)},
        )

    def handle_message(self, robot, message) -> None:
        if message.category == "probe_entry":
            robot.cbaa_probe_messages_seen += 1


class StallAllocator(AllocatorBase):
    """Valid allocator implementation that never selects an active task."""

    name = "CBAA"

    def choose_goal(self, robot) -> AllocationDecision:
        return AllocationDecision(None, {"intentional_stall": True})


class EpochProbeAllocator(ProbeAllocator):
    """Expose one policy-induced allocator reset for timing-scope tests."""

    def on_allocation_epoch(self, robot, reason, admitted_tasks) -> None:
        robot.epoch_probe_count = int(
            getattr(robot, "epoch_probe_count", 0)
        ) + 1


def causal_config(robot_count: int = 1, **overrides) -> SimConfig:
    ids = generate_robot_ids(robot_count)
    values = dict(
        grid_size=5,
        robot_ids=ids,
        start_positions=edge_even_start_positions(5, ids),
        comm_delay_s=0.0,
        comm_delay_jitter_s=0.0,
        collision_intent_settle_s=0.0,
        async_initial_spread_s=0.0,
        async_step_mean_s=1.0,
        async_step_jitter_s=0.0,
        async_min_delay_s=1.0,
        async_max_delay_s=1.0,
        debug_max_events=20_000,
        debug_max_stagnant_events=5_000,
    )
    values.update(overrides)
    return SimConfig(**values)


def run_probe(
    robot_count: int,
    targets,
    releases,
    provider,
    *,
    policy: ReallocationPolicy | None = None,
):
    ProbeAllocator.observations.clear()
    return AsyncTrialRunner(
        causal_config(robot_count),
        ProbeAllocator,
        IdealModel(),
        seed=41,
        timing_provider=provider,
    ).run_online_trial(
        TrialScenario(901, list(targets)),
        list(releases),
        policy or ReallocationPolicy.eager(),
    )


class CausalComputeTests(unittest.TestCase):
    def test_zero_compute_known_trace_yields_to_delayed_messages(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        manifest_root = (
            repo_root
            / "study/generated/manifests/collaborative_visit_g19_t50_n25_calibrated_v2"
        )
        scenario_raw = json.loads(
            (manifest_root / "scenarios/trace_0007.json").read_text(
                encoding="utf-8"
            )
        )
        release_raw = json.loads(
            (manifest_root / "releases/high/trace_0007.json").read_text(
                encoding="utf-8"
            )
        )
        starts = scenario_raw["robot_starts"]
        robot_ids = [str(row["robot_id"]) for row in starts]
        start_positions = {
            str(row["robot_id"]): (int(row["x"]), int(row["y"]))
            for row in starts
        }
        start_headings = {
            str(row["robot_id"]): (
                int(row.get("heading_x", 1)), int(row.get("heading_y", 0))
            )
            for row in starts
        }
        tasks = release_raw["tasks"]
        targets = [(int(row["x"]), int(row["y"])) for row in tasks]
        releases = {
            cell: float(row["release_time_s"])
            for cell, row in zip(targets, tasks, strict=True)
        }
        state = AsyncTrialRunner(
            SimConfig(
                grid_size=int(scenario_raw["grid_size"]),
                robot_ids=robot_ids,
                start_positions=start_positions,
                start_headings=start_headings,
                robot_start_layout="manifest",
                condition_id="ACBBA__high__count_b4",
                comm_delay_s=0.04,
                comm_delay_jitter_s=0.0,
                commitment_horizon=None,
                max_candidate_cells=None,
            ),
            load_allocator_class("ACBBA"),
            IdealModel(),
            seed=int(scenario_raw["runtime_seed"]),
            timing_provider=ZeroComputeTimingProvider(),
        ).run_online_trial(
            TrialScenario(
                907,
                targets,
                {
                    "trace_id": "trace_0007",
                    "causal_event_horizon_events": 251_000,
                    "causal_stagnation_horizon_events": 5_500,
                },
            ),
            releases,
            ReallocationPolicy.count(4),
        )
        self.assertTrue(state.done)
        self.assertTrue(state.world.all_targets_completed())
        calls = state.reallocation_scheduler.allocator_calls
        calls_per_timestamp: dict[float, int] = {}
        for call in calls:
            calls_per_timestamp[call.mission_time_s] = (
                calls_per_timestamp.get(call.mission_time_s, 0) + 1
            )
        self.assertLess(max(calls_per_timestamp.values()), 100)
        self.assertLess(len(calls), 1_000)

    def test_predeclared_horizons_promote_algorithmic_noncompletion(self) -> None:
        def run(metadata):
            return AsyncTrialRunner(
                causal_config(1),
                StallAllocator,
                IdealModel(),
                seed=29,
                timing_provider=ZeroComputeTimingProvider(),
            ).run_online_trial(
                TrialScenario(900, [(1, 2)], metadata),
                [0.0],
                ReallocationPolicy.eager(),
            )

        event_limited = run({
            "causal_event_horizon_events": 12,
            "causal_stagnation_horizon_events": 1_000,
        })
        self.assertFalse(event_limited.done)
        self.assertFalse(event_limited.world.all_targets_completed())
        self.assertEqual(event_limited.algorithmic_failure_type, "event_horizon")
        self.assertEqual(
            event_limited.online_metrics()["algorithmic_status"], "incomplete"
        )

        stagnant = run({
            "causal_event_horizon_events": 1_000,
            "causal_stagnation_horizon_events": 4,
        })
        self.assertFalse(stagnant.done)
        self.assertEqual(
            stagnant.algorithmic_failure_type, "stagnation_horizon"
        )

    def test_same_time_group_has_independent_completions_not_serial_sum(self) -> None:
        durations = {"00": 0.40, "01": 0.55, "02": 0.37, "03": 0.61}
        provider = DeterministicTimingProvider(durations)
        state = run_probe(
            4,
            [(1, 0), (1, 1), (1, 3), (1, 4)],
            [0.0] * 4,
            provider,
        )
        first_group = provider.groups[0]
        self.assertEqual([call.logical_robot_id for call in first_group], ["00", "01", "02", "03"])
        self.assertEqual({call.virtual_start_s for call in first_group}, {0.0})
        group_id = first_group[0].group_id
        calls = [
            call for call in state.reallocation_scheduler.allocator_calls
            if call.group_id == group_id
        ]
        self.assertEqual(len(calls), 4)
        self.assertEqual(
            {call.robot_id: call.compute_completion_time_s for call in calls},
            durations,
        )
        # The last call ends at 0.61, not at the physically serialized sum 1.93.
        self.assertAlmostEqual(max(call.compute_completion_time_s for call in calls), 0.61)

    def test_same_time_calls_freeze_views_and_stage_messages(self) -> None:
        provider = DeterministicTimingProvider(
            {"00": 0.10, "01": 1.00, "02": 0.20, "03": 0.30}
        )
        state = run_probe(
            4,
            [(1, 0), (1, 1), (1, 3), (1, 4)],
            [0.0] * 4,
            provider,
        )
        initial = [item for item in ProbeAllocator.observations if item[1] == 0.0]
        self.assertEqual(len(initial), 4)
        self.assertTrue(all(messages_seen == 0 for _, _, messages_seen, _ in initial))
        # Robot 01 was still computing when the 00 result/message became
        # visible; the message was delivered only after 01's staged call.
        self.assertGreaterEqual(state.robots["01"].cbaa_probe_messages_seen, 1)

    def test_delivered_allocator_messages_are_events_for_only_the_next_call(self) -> None:
        provider = DeterministicTimingProvider({"00": 0.10, "01": 0.20})
        cfg = causal_config(2)
        AsyncTrialRunner(
            cfg,
            load_allocator_class("CBAA"),
            IdealModel(),
            seed=5,
            timing_provider=provider,
        ).run_online_trial(
            TrialScenario(902, [(2, 1), (2, 3)]),
            [0.0, 0.0],
            ReallocationPolicy.eager(),
        )
        initial = provider.groups[0]
        self.assertTrue(
            all(
                not any(
                    event["kind"] == "allocator_message"
                    for event in call.device_setup["events"]
                )
                for call in initial
            )
        )
        later = [
            call
            for group in provider.groups[1:]
            for call in group
            if any(
                event["kind"] == "allocator_message"
                for event in call.device_setup["events"]
            )
        ]
        self.assertTrue(later)

    def test_compute_blocked_external_inputs_keep_message_epoch_arrival_order(self) -> None:
        provider = DeterministicTimingProvider(
            lambda call: (
                1.0
                if call.logical_robot_id == "00"
                and call.call_id.endswith("0000001")
                else 0.1
            )
        )
        AsyncTrialRunner(
            causal_config(2),
            load_allocator_class("CBAA"),
            IdealModel(),
            seed=6,
            timing_provider=provider,
        ).run_online_trial(
            TrialScenario(904, [(4, 1), (4, 3), (2, 2)]),
            [0.0, 0.0, 0.5],
            ReallocationPolicy.eager(),
        )
        next_robot_zero_call = next(
            call
            for group in provider.groups[1:]
            for call in group
            if call.logical_robot_id == "00"
        )
        self.assertEqual(next_robot_zero_call.virtual_start_s, 1.0)
        self.assertEqual(
            [event["kind"] for event in next_robot_zero_call.device_setup["events"]],
            ["allocator_message", "allocation_epoch"],
        )

    def test_other_robot_moves_while_peer_is_compute_blocked(self) -> None:
        provider = DeterministicTimingProvider({"00": 5.0, "01": 0.1})
        state = run_probe(
            2, [(1, 0), (1, 4)], [0.0, 0.0], provider
        )
        initial_group = provider.groups[0][0].group_id
        long_call = next(
            call for call in state.reallocation_scheduler.allocator_calls
            if call.group_id == initial_group and call.robot_id == "00"
        )
        fast_move = next(
            move for move in state.movement_records if move.robot_id == "01"
        )
        self.assertAlmostEqual(long_call.compute_completion_time_s, 5.0)
        self.assertAlmostEqual(fast_move.completion_time_s, 1.1)
        self.assertLess(fast_move.completion_time_s, long_call.compute_completion_time_s)

    def test_release_during_compute_is_absolute_and_not_visible_to_inflight_call(self) -> None:
        provider = DeterministicTimingProvider(
            lambda call: 1.0 if call.call_id.endswith("0000001") else 0.0
        )
        state = run_probe(
            1, [(1, 2), (2, 2)], [0.0, 0.5], provider
        )
        future = state.world.target_records[(2, 2)]
        self.assertEqual(future.released_time_s, 0.5)
        self.assertEqual(future.admission_time_s, 0.5)
        first = provider.groups[0][0]
        self.assertEqual(first.active_task_count, 1)
        next_call = provider.groups[1][0]
        self.assertEqual(
            [event["kind"] for event in next_call.device_setup["events"]],
            ["allocation_epoch"],
        )
        self.assertEqual(
            next_call.authoritative.call_class, "full_allocation_solve"
        )
        self.assertEqual(future.first_eligible_allocator_start_time_s, 1.0)
        self.assertGreaterEqual(future.first_assignment_time_s, 1.0)

    def test_invalid_parity_result_fails_closed(self) -> None:
        class BadParityProvider(DeterministicTimingProvider):
            def measure_group(self, calls):
                rows = super().measure_group(calls)
                return tuple(replace(row, parity_passed=False) for row in rows)

        with self.assertRaisesRegex(CausalTimingError, "parity"):
            run_probe(1, [(1, 2)], [0.0], BadParityProvider({"00": 0.2}))

    def test_primary_failure_is_not_masked_by_cleanup_failure(self) -> None:
        class FailingProvider(DeterministicTimingProvider):
            def measure_group(self, calls):
                raise RuntimeError("primary measurement failure")

            def end_mission(self):
                raise RuntimeError("cleanup failure")

        with self.assertRaisesRegex(RuntimeError, "primary measurement") as caught:
            run_probe(1, [(1, 2)], [0.0], FailingProvider({"00": 0.2}))
        self.assertEqual(
            caught.exception.cleanup_failure,
            "RuntimeError: cleanup failure",
        )

    def test_true_parity_flag_cannot_hide_a_mismatched_device_result(self) -> None:
        class LyingProvider(DeterministicTimingProvider):
            def measure_group(self, calls):
                return tuple(
                    replace(row, device_goal=(4, 4), parity_passed=True)
                    for row in super().measure_group(calls)
                )

        with self.assertRaisesRegex(CausalTimingError, "device goal"):
            run_probe(1, [(1, 2)], [0.0], LyingProvider({"00": 0.2}))

    def test_board_name_and_unsealed_hardware_claim_cannot_spoof_attestation(self) -> None:
        class SpoofProvider(DeterministicTimingProvider):
            def measure_group(self, calls):
                rows = super().measure_group(calls)
                return tuple(
                    replace(
                        row,
                        board_id="RP2040-SPOOF",
                        serial_device="COM99",
                        physical_measurement_index=index,
                        metadata={
                            "hardware_valid": True,
                            "validation_mode": "rp2040_native_hardware",
                            # Deliberately no sealed hardware_attestation.
                        },
                    )
                    for index, row in enumerate(rows, start=1)
                )

        state = run_probe(
            1, [(1, 2)], [0.0], SpoofProvider({"00": 0.2})
        )
        calls = state.reallocation_scheduler.allocator_calls
        self.assertTrue(calls)
        self.assertTrue(all(call.board_id == "RP2040-SPOOF" for call in calls))
        self.assertFalse(any(call.hardware_validated for call in calls))

    def test_schema_two_timing_splits_remain_diagnostics_not_virtual_time(self) -> None:
        class SplitProvider(DeterministicTimingProvider):
            def measure_group(self, calls):
                return tuple(
                    replace(
                        row,
                        serial_roundtrip_us=30,
                        host_serialization_setup_us=0,
                        host_total_call_us=50,
                        psetup_transaction_us=11,
                        device_pre_call_setup_us=7,
                        ptime_result_transaction_us=19,
                        host_prepare_cpu_us=3,
                        metadata={
                            "timing_decomposition_schema": 2,
                            "host_serialization_setup_measured": False,
                            "device_allocator_timer_scope": (
                                DEVICE_ALLOCATOR_TIMER_SCOPE
                            ),
                            "serial_roundtrip_definition": "PSETUP plus PTIME",
                        },
                    )
                    for row in super().measure_group(calls)
                )

        state = run_probe(1, [(1, 2)], [0.0], SplitProvider({"00": 0.2}))
        call = state.reallocation_scheduler.allocator_calls[0]
        self.assertEqual(call.compute_completion_time_s, 0.2)
        self.assertEqual(call.timing_decomposition_schema, 2)
        self.assertEqual(call.serial_roundtrip_ns, 30_000)
        self.assertEqual(
            call.serial_roundtrip_ns,
            call.psetup_transaction_ns + call.ptime_result_transaction_ns,
        )
        self.assertEqual(call.device_pre_call_setup_ns, 7_000)
        self.assertEqual(call.host_serialization_setup_ns, 0)
        self.assertEqual(call.device_choose_goal_duration_ns, 200_000_000)
        self.assertEqual(call.algorithm_epoch_reset_duration_ns, 0)
        self.assertEqual(
            call.device_allocator_duration_ns,
            call.device_choose_goal_duration_ns
            + call.algorithm_epoch_reset_duration_ns,
        )
        self.assertEqual(
            call.device_allocator_timer_scope,
            DEVICE_ALLOCATOR_TIMER_SCOPE,
        )

    def test_invalid_device_allocator_component_splits_fail_closed(self) -> None:
        class BadSplitProvider(DeterministicTimingProvider):
            def __init__(self, choose_goal_us, epoch_reset_us):
                super().__init__({"00": 0.2})
                self.choose_goal_us = choose_goal_us
                self.epoch_reset_us = epoch_reset_us

            def measure_group(self, calls):
                return tuple(
                    replace(
                        row,
                        device_choose_goal_us=self.choose_goal_us,
                        algorithm_epoch_reset_us=self.epoch_reset_us,
                    )
                    for row in super().measure_group(calls)
                )

        for choose_goal_us, epoch_reset_us, message in (
            (-1, 200_001, "non-negative"),
            (199_999, 0, "must equal"),
        ):
            with self.subTest(
                choose_goal_us=choose_goal_us,
                epoch_reset_us=epoch_reset_us,
            ):
                with self.assertRaisesRegex(CausalTimingError, message):
                    run_probe(
                        1,
                        [(1, 2)],
                        [0.0],
                        BadSplitProvider(choose_goal_us, epoch_reset_us),
                    )

    def test_agx_epoch_callback_is_timed_and_composed_with_choose_goal(self) -> None:
        ticks = itertools.count(start=0, step=2_000)
        with patch(
            "known_visit_sim.core.robot.perf_counter_ns",
            side_effect=lambda: next(ticks),
        ):
            state = AsyncTrialRunner(
                causal_config(1),
                EpochProbeAllocator,
                IdealModel(),
                seed=42,
                timing_provider=HostMeasuredTimingProvider(),
            ).run_online_trial(
                TrialScenario(905, [(1, 2), (2, 2)]),
                [0.0, 0.5],
                ReallocationPolicy.eager(),
            )

        epoch_calls = [
            call
            for call in state.reallocation_scheduler.allocator_calls
            if call.agx_algorithm_epoch_reset_duration_ns > 0
        ]
        self.assertEqual(len(epoch_calls), 1)
        call = epoch_calls[0]
        self.assertEqual(call.agx_choose_goal_duration_ns, 2_000)
        self.assertEqual(call.agx_algorithm_epoch_reset_duration_ns, 2_000)
        self.assertEqual(call.agx_allocator_duration_ns, 4_000)
        self.assertEqual(
            call.agx_allocator_duration_ns,
            call.agx_choose_goal_duration_ns
            + call.agx_algorithm_epoch_reset_duration_ns,
        )
        # The host proxy consumes the same frozen decomposition, proving the
        # authoritative call boundary includes both allocator operations.
        self.assertEqual(call.device_allocator_duration_ns, 4_000)
        self.assertEqual(call.device_choose_goal_duration_ns, 2_000)
        self.assertEqual(call.algorithm_epoch_reset_duration_ns, 2_000)
        metrics = state.online_metrics()
        self.assertAlmostEqual(
            metrics["rp2040_allocator_processor_work_s"],
            metrics["rp2040_choose_goal_processor_work_s"]
            + metrics["rp2040_epoch_reset_processor_work_s"],
        )
        self.assertAlmostEqual(
            metrics["agx_allocator_processor_work_s"],
            metrics["agx_choose_goal_processor_work_s"]
            + metrics["agx_epoch_reset_processor_work_s"],
        )
        self.assertAlmostEqual(
            metrics["rp2040_epoch_reset_processor_work_s"], 0.000002
        )
        self.assertAlmostEqual(
            metrics["agx_epoch_reset_processor_work_s"], 0.000002
        )


class CausalMovementAndMetricTests(unittest.TestCase):
    def test_movement_jitter_is_paired_across_provider_and_policy_interleavings(self) -> None:
        def run_variant(provider, policy):
            return AsyncTrialRunner(
                causal_config(
                    1,
                    async_step_mean_s=1.0,
                    async_step_jitter_s=0.25,
                    async_min_delay_s=0.5,
                    async_max_delay_s=1.5,
                ),
                ProbeAllocator,
                IdealModel(),
                seed=91,
                timing_provider=provider,
            ).run_online_trial(
                TrialScenario(
                    903, [(3, 2)], {"trace_id": "paired-movement-trace"}
                ),
                [0.0],
                policy,
            )

        zero = run_variant(ZeroComputeTimingProvider(), ReallocationPolicy.eager())
        causal = run_variant(
            DeterministicTimingProvider({"00": 0.37}),
            ReallocationPolicy.count(2),
        )
        self.assertEqual(
            [move.timing_key_sha256 for move in zero.movement_records],
            [move.timing_key_sha256 for move in causal.movement_records],
        )
        self.assertEqual(
            [move.robot_action_index for move in zero.movement_records], [0, 1, 2]
        )
        for left, right in zip(
            zero.movement_records, causal.movement_records, strict=True
        ):
            self.assertAlmostEqual(left.duration_s, right.duration_s)
            self.assertEqual((left.source_cell, left.target_cell), (right.source_cell, right.target_cell))
        self.assertTrue(
            any(abs(move.duration_s - 1.0) > 1e-6 for move in zero.movement_records)
        )
        self.assertEqual(zero.movement_timing_seed, causal.movement_timing_seed)
        self.assertEqual(
            zero.movement_timing_trace_id, causal.movement_timing_trace_id
        )

    def test_final_cell_duration_is_included_and_completion_is_at_arrival(self) -> None:
        state = run_probe(
            1, [(1, 2)], [0.0], ZeroComputeTimingProvider()
        )
        movement = state.movement_records[-1]
        task = state.world.target_records[(1, 2)]
        self.assertEqual(movement.start_time_s, 0.0)
        self.assertEqual(movement.completion_time_s, 1.0)
        self.assertEqual(task.first_completion_time_s, movement.completion_time_s)
        self.assertEqual(state.mission_elapsed_time_s, 1.0)
        self.assertEqual(state.clock_s, 1.0)

    def test_release_during_movement_retains_exact_timestamp(self) -> None:
        state = run_probe(
            1,
            [(1, 2), (2, 2)],
            [0.0, 0.5],
            ZeroComputeTimingProvider(),
        )
        future = state.world.target_records[(2, 2)]
        first_move = state.movement_records[0]
        self.assertEqual((first_move.start_time_s, first_move.completion_time_s), (0.0, 1.0))
        self.assertEqual(future.released_time_s, 0.5)
        self.assertEqual(future.admission_time_s, 0.5)
        self.assertGreaterEqual(future.first_completion_time_s, 1.0)

    def test_known_answer_processor_work_capacity_and_counterfactual(self) -> None:
        causal = run_probe(
            1, [(1, 2)], [0.0], DeterministicTimingProvider({"00": 0.4})
        )
        zero = run_probe(
            1, [(1, 2)], [0.0], ZeroComputeTimingProvider()
        )
        metrics = causal.online_metrics()
        self.assertAlmostEqual(metrics["rp2040_allocator_processor_work_s"], 0.4)
        self.assertAlmostEqual(
            metrics["rp2040_choose_goal_processor_work_s"], 0.4
        )
        self.assertEqual(metrics["rp2040_epoch_reset_processor_work_s"], 0.0)
        self.assertAlmostEqual(
            metrics["rp2040_allocator_processor_work_s"],
            metrics["rp2040_choose_goal_processor_work_s"]
            + metrics["rp2040_epoch_reset_processor_work_s"],
        )
        self.assertAlmostEqual(
            metrics["agx_allocator_processor_work_s"],
            metrics["agx_choose_goal_processor_work_s"]
            + metrics["agx_epoch_reset_processor_work_s"],
        )
        self.assertAlmostEqual(metrics["mission_elapsed_time_s"], 1.4)
        self.assertAlmostEqual(metrics["median_release_to_first_assignment_latency_s"], 0.4)
        self.assertAlmostEqual(metrics["median_release_to_completion_latency_s"], 1.4)
        self.assertAlmostEqual(metrics["processor_capacity_fraction"], 0.4 / 1.4)
        paired = build_zero_compute_pair_metrics(causal, zero)
        self.assertAlmostEqual(paired["D_alloc_s"], 0.4)
        self.assertAlmostEqual(
            paired["allocation_attributable_mission_fraction"], 0.4 / 1.4
        )
        self.assertFalse(paired["negative_allocation_effect_flag"])


class CausalRawOutputTests(unittest.TestCase):
    def test_unreleased_incomplete_rows_do_not_break_release_metrics(self) -> None:
        rows = [
            {"release_time_s": 0.0},
            {"release_time_s": 2.5},
            {"release_time_s": None},
            {},
        ]

        self.assertEqual(_positive_release_times(rows), [2.5])

    def test_manifest_adapter_writes_canonical_causal_tables(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            trace_id = "trace-001"
            seed = 23
            starts = [
                {"robot_id": rid, "x": 0, "y": y, "heading_x": 1, "heading_y": 0}
                for rid, y in zip(("00", "01", "02", "03"), (0, 1, 3, 4), strict=True)
            ]
            tasks = [
                {
                    "task_id": f"task_{index:04d}",
                    "x": 1,
                    "y": y,
                    "initially_visible": True,
                }
                for index, y in enumerate((0, 1, 3, 4), start=1)
            ]
            scenario = {
                "schema_version": 1,
                "manifest_kind": "scenario",
                "trace_id": trace_id,
                "runtime_seed": seed,
                "grid_size": 5,
                "robot_starts": starts,
                "tasks": tasks,
            }
            scenario_path = root / "scenario.json"
            scenario_path.write_text(
                json.dumps(scenario, sort_keys=True) + "\n", encoding="utf-8"
            )
            scenario_hash = hashlib.sha256(scenario_path.read_bytes()).hexdigest()
            release_tasks = [
                {**task, "release_time_s": 0.0} for task in tasks
            ]
            release = {
                "schema_version": 1,
                "manifest_kind": "release_trace",
                "trace_id": trace_id,
                "runtime_seed": seed,
                "load_id": "smoke",
                "scenario_sha256": scenario_hash,
                "initial_task_count": 4,
                "tasks": release_tasks,
            }
            release_path = root / "release.json"
            release_path.write_text(
                json.dumps(release, sort_keys=True) + "\n", encoding="utf-8"
            )
            release_hash = hashlib.sha256(release_path.read_bytes()).hexdigest()
            output = root / "attempt"
            output.mkdir()
            summary = run_causal_manifest_job(
                scenario_manifest=scenario_path,
                release_manifest=release_path,
                scenario_sha256=scenario_hash,
                release_sha256=release_hash,
                algorithm="CBAA",
                arrival_load="smoke",
                trace_id=trace_id,
                job_id="job-001",
                block_id="block-001",
                policy={
                    "policy_id": "eager",
                    "mode": "eager",
                    "batch_size": 1,
                    "max_pending_age_s": None,
                },
                runtime_seed=seed,
                timing_provider=ZeroComputeTimingProvider(),
                zero_compute=True,
                output_dir=output,
                campaign_identity={
                    "config_sha256": "a" * 64,
                    "manifest_index_sha256": "b" * 64,
                    "hardware_binding_sha256": "c" * 64,
                    "worker_index": 0,
                    "core_id": 0,
                    "board_id": "board-0",
                    "serial_device": "NONE",
                    "expected_device_uid": "device-0",
                    "expected_device_build_id": "build-0",
                    "expected_device_firmware_sha256": "d" * 64,
                    "expected_device_module_set_sha256": "e" * 64,
                    "git_head": "f" * 40,
                    "source_tree_sha256": "1" * 64,
                    "relevant_source_dirty": False,
                    "hidden_library_threads": 1,
                },
            )
            self.assertTrue(summary["all_tasks_completed"])
            # The manifest adapter intentionally retains the configured
            # asynchronous initial spread and movement jitter. Exactness here
            # is checked against the emitted final completion, not 1.6 s.
            self.assertGreater(summary["mission_elapsed_time_s"], 1.5)
            required = {
                "trial_summary.json",
                "task_events.csv",
                "allocator_calls.csv",
                "reallocation_events.csv",
                "movement_events.csv",
                "run_provenance.json",
            }
            self.assertTrue(required.issubset(path.name for path in output.iterdir()))
            with (output / "task_events.csv").open(newline="", encoding="utf-8") as handle:
                tasks_out = list(csv.DictReader(handle))
            self.assertAlmostEqual(
                summary["mission_elapsed_time_s"],
                max(float(row["completion_time_s"]) for row in tasks_out),
            )
            with (output / "allocator_calls.csv").open(newline="", encoding="utf-8") as handle:
                calls = list(csv.DictReader(handle))
            self.assertEqual(len(calls), summary["allocator_call_count"])
            self.assertGreaterEqual(len(calls), 4)
            self.assertTrue(all(float(row["rp2040_device_duration_s"]) == 0.0 for row in calls))
            self.assertTrue(all(row["virtual_compute_start_s"] == row["virtual_compute_completion_s"] for row in calls))
            self.assertTrue(
                all(
                    {
                        "device_choose_goal_us",
                        "algorithm_epoch_reset_us",
                        "rp2040_choose_goal_duration_s",
                        "rp2040_algorithm_epoch_reset_duration_s",
                        "agx_choose_goal_duration_s",
                        "agx_algorithm_epoch_reset_duration_s",
                    }.issubset(row)
                    for row in calls
                )
            )
            self.assertTrue(
                all(
                    int(row["device_choose_goal_us"])
                    + int(row["algorithm_epoch_reset_us"])
                    == 0
                    for row in calls
                )
            )
            board = BoardBinding(
                "board-0", "NONE", "device-0", "build-0", "d" * 64, "e" * 64
            )
            policy_spec = PolicySpec("eager", "eager", 1, None)
            config = CausalConfig(
                path=root / "config.json",
                repo_root=Path(__file__).resolve().parents[2],
                raw={},
                config_sha256="a" * 64,
                campaign_id="test",
                stage="smoke",
                output_root=root,
                manifest_root=root,
                manifest_index_sha256="b" * 64,
                hardware_binding_sha256="c" * 64,
                algorithms=("CBAA", "ACBBA", "PI", "HIPC"),
                loads=("smoke",),
                policies=(policy_spec,),
                trace_limit=1,
                schedule_seed=1,
                boards=(board,),
                core_affinities=(0,),
                development_override=True,
                required_gate_paths=(),
                max_technical_retries=0,
                device_timeout_seconds=30.0,
                runner_factory="study.causal.worker.run_causal_job",
                provider_factory="study.causal.worker.create_timing_provider",
            )
            job = CausalJob(
                job_id="job-001",
                block_id="block-001",
                algorithm="CBAA",
                load_id="smoke",
                trace_id=trace_id,
                policy=policy_spec,
                policy_order_index=0,
                board_id="board-0",
                worker_index=0,
                core_id=0,
                scenario_path=scenario_path,
                release_path=release_path,
                scenario_sha256=scenario_hash,
                release_sha256=release_hash,
                runtime_seed=seed,
                zero_compute=True,
            )
            validation = validate_causal_outputs(config, job, output)
            self.assertTrue(validation["valid"])

            causal_output = root / "causal-attempt"
            causal_output.mkdir()
            causal_summary = run_causal_manifest_job(
                scenario_manifest=scenario_path,
                release_manifest=release_path,
                scenario_sha256=scenario_hash,
                release_sha256=release_hash,
                algorithm="CBAA",
                arrival_load="smoke",
                trace_id=trace_id,
                job_id="job-002",
                block_id="block-001",
                policy={
                    "policy_id": "eager",
                    "mode": "eager",
                    "batch_size": 1,
                    "max_pending_age_s": None,
                },
                runtime_seed=seed,
                timing_provider=DeterministicTimingProvider(
                    {"00": 0.01, "01": 0.02, "02": 0.03, "03": 0.04}
                ),
                zero_compute=False,
                output_dir=causal_output,
                campaign_identity={
                    "config_sha256": "a" * 64,
                    "manifest_index_sha256": "b" * 64,
                    "hardware_binding_sha256": "c" * 64,
                    "worker_index": 0,
                    "core_id": 0,
                    "board_id": "board-0",
                    "serial_device": "NONE",
                    "expected_device_uid": "device-0",
                    "expected_device_build_id": "build-0",
                    "expected_device_firmware_sha256": "d" * 64,
                    "expected_device_module_set_sha256": "e" * 64,
                    "git_head": "f" * 40,
                    "source_tree_sha256": "1" * 64,
                    "relevant_source_dirty": False,
                    "hidden_library_threads": 1,
                },
            )
            self.assertFalse(causal_summary["hardware_validated"])
            with (causal_output / "allocator_calls.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                causal_calls = list(csv.DictReader(handle))
            self.assertTrue(
                all(
                    int(row["device_choose_goal_us"])
                    + int(row["algorithm_epoch_reset_us"])
                    == round(float(row["rp2040_device_duration_s"]) * 1_000_000)
                    for row in causal_calls
                )
            )
            causal_job = replace(job, job_id="job-002", zero_compute=False)
            causal_validation = validate_causal_outputs(
                config, causal_job, causal_output
            )
            self.assertTrue(causal_validation["valid"])
            self.assertGreater(
                causal_validation["rp2040_allocator_processor_work_s"], 0.0
            )


if __name__ == "__main__":
    unittest.main()
