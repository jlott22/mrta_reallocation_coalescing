"""Software-valid tests for the causal RP2040 timing/parity boundary."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ARCHITECTURE = ROOT / "Simulation" / "Architecture"
if str(ARCHITECTURE) not in sys.path:
    sys.path.insert(0, str(ARCHITECTURE))
COMMON_DEVICE = (
    ARCHITECTURE / "allocator_replay" / "device" / "common"
)
if str(COMMON_DEVICE) not in sys.path:
    sys.path.insert(0, str(COMMON_DEVICE))

from allocator_replay.causal import (  # noqa: E402
    BoardBindingError,
    BoardFingerprint,
    BoardLeaseError,
    BoardLease,
    CausalBoardSession,
    CausalPreflightRecorder,
    DecisionSignature,
    DeterministicVirtualDevice,
    FrozenCall,
    MissionBinding,
    ParityFailure,
    SessionStateError,
    SimulatedDurationProvider,
    StaleReplyError,
    ZeroDurationProvider,
    bind_hardware_workers,
    bind_single_hardware_worker,
    load_and_verify_preflight,
    projected_parity,
    run_native_preflight,
    semantic_hash,
    verify_preflight_report,
    write_preflight_reports,
)
from allocator_replay.device.common.replay_persistent import (  # noqa: E402
    PersistentRuntimeSlot,
)
from allocator_replay.device.native.collaborative import (  # noqa: E402
    create_persistent_runtime,
)
from allocator_replay.device.native.collaborative import runtime as native_runtime_module  # noqa: E402
from allocator_replay.coalescing.build import (  # noqa: E402
    build_device_bundle,
)
from allocator_replay.host.emulator import LoopbackReplayDevice  # noqa: E402
from known_visit_sim.algorithms.registry import load_allocator_class  # noqa: E402
from known_visit_sim.comms.models import IdealModel  # noqa: E402
from known_visit_sim.config import (  # noqa: E402
    SimConfig,
    edge_even_start_positions,
    generate_robot_ids,
)
from known_visit_sim.core.reallocation import ReallocationPolicy  # noqa: E402
from known_visit_sim.core.scheduler import AsyncTrialRunner  # noqa: E402
from known_visit_sim.core.types import TrialScenario  # noqa: E402


ROBOT_IDS = ("robot_0", "robot_1", "robot_2", "robot_3")


def state(robot_id: str, value: int = 0) -> dict:
    return {
        "robot_attrs": {"rid": robot_id, "value": value},
        "views": {"active_tasks": [[1, 1], [2, 2]]},
        "cfg": {"robot_ids": list(ROBOT_IDS)},
        "belief": {},
        "allocator_attrs": {"round": value},
    }


def mission(trial_id: str = "trial-1", algorithm: str = "CBAA") -> MissionBinding:
    return MissionBinding(
        trial_id=trial_id,
        condition_id=f"condition-{algorithm.lower()}",
        algorithm=algorithm,
        seed=17,
        robot_ids=ROBOT_IDS,
        trial_config={"grid_size": 19, "all_tasks": [[1, 1], [2, 2]]},
        initial_context_states={item: state(item) for item in ROBOT_IDS},
    )


def result_for(
    robot_id: str,
    value: int,
    duration_us: int,
    *,
    goal: tuple[int, int] = (1, 1),
    message_suffix: str = "ok",
) -> dict:
    post = state(robot_id, value + 1)
    messages = [{"sender": robot_id, "payload": message_suffix}]
    return {
        "status": "completed",
        "goal": list(goal),
        "messages": messages,
        "post_state": post,
        "allocator_time_us": duration_us,
        "candidate_count_before": 2,
        "candidate_count_after": 2,
        "call_class": "full_allocation_solve",
        "heap_free_before": 10000,
        "heap_free_after": 9900,
    }


def frozen(
    robot_id: str,
    call_id: str,
    group_id: str,
    value: int,
    duration_us: int,
    *,
    start_s: float = 20.0,
    trial_id: str = "trial-1",
    goal: tuple[int, int] = (1, 1),
    message_suffix: str = "ok",
) -> FrozenCall:
    expected = result_for(
        robot_id,
        value,
        duration_us,
        goal=goal,
        message_suffix=message_suffix,
    )
    return FrozenCall(
        call_id=call_id,
        group_id=group_id,
        trial_id=trial_id,
        logical_robot_id=robot_id,
        algorithm="CBAA",
        virtual_start_s=start_s,
        device_setup={"pre_state": state(robot_id, value)},
        authoritative=DecisionSignature.from_result(expected),
        agx_allocator_time_us=duration_us // 2,
    )


class _Factory:
    def __init__(self, durations: dict[str, int] | None = None) -> None:
        self.durations = durations or {
            "robot_0": 100_000,
            "robot_1": 200_000,
            "robot_2": 300_000,
            "robot_3": 400_000,
        }
        self.mutate_between = None
        self.goal_override: tuple[int, int] | None = None
        self.message_override: str | None = None
        self.stale_attempt = False

    def __call__(self, setup, prior, attempt_id):
        del prior
        robot_id = setup["context_id"]
        value = int(setup["pre_state"]["robot_attrs"]["value"])
        if robot_id == "robot_0" and self.mutate_between is not None:
            self.mutate_between()
        result = result_for(
            robot_id,
            value,
            self.durations[robot_id],
            goal=self.goal_override or (1, 1),
            message_suffix=self.message_override or "ok",
        )
        result["attempt_id"] = "stale-attempt" if self.stale_attempt else attempt_id
        return result


class CausalSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.lock_root = Path(self.temporary.name)
        self.factory = _Factory()
        self.device = DeterministicVirtualDevice("board-a", result_factory=self.factory)
        self.binding = bind_hardware_workers(
            [self.device], development_override=True
        )[0]
        self.session = CausalBoardSession(self.binding, lock_root=self.lock_root)

    def tearDown(self) -> None:
        self.session.close()
        self.device.close()
        self.temporary.cleanup()

    def test_same_time_measurement_order_does_not_serialize_virtual_time(self) -> None:
        self.session.begin_mission(mission())
        calls = (
            frozen("robot_0", "call-a", "group-1", 0, 100_000),
            frozen("robot_1", "call-b", "group-1", 0, 200_000),
        )
        measured = self.session.measure_group(calls)
        self.assertEqual(self.device.prepare_order, ["robot_0", "robot_1"])
        self.assertAlmostEqual(measured[0].virtual_completion_s, 20.1)
        self.assertAlmostEqual(measured[1].virtual_completion_s, 20.2)
        self.assertNotAlmostEqual(measured[1].virtual_completion_s, 20.3)
        self.assertEqual(measured[0].device_duration_s, 0.1)
        self.assertGreaterEqual(measured[0].serial_roundtrip_us, 0)
        self.assertGreaterEqual(measured[0].host_serialization_setup_us, 0)
        self.assertEqual(measured[0].agx_allocator_time_us, 50_000)
        self.assertEqual(
            measured[0].serial_roundtrip_us,
            measured[0].psetup_transaction_us
            + measured[0].ptime_result_transaction_us,
        )
        self.assertEqual(measured[0].host_serialization_setup_us, 0)
        self.assertFalse(
            measured[0].metadata["host_serialization_setup_measured"]
        )
        self.assertFalse(measured[0].metadata["hardware_valid"])
        self.assertNotIn("hardware_attestation", measured[0].metadata)

    def test_every_input_is_detached_before_first_physical_request(self) -> None:
        self.session.begin_mission(mission())
        mutable_b = state("robot_1", 0)
        calls = [
            frozen("robot_0", "call-a", "group-frozen", 0, 100_000),
            FrozenCall(
                call_id="call-b",
                group_id="group-frozen",
                trial_id="trial-1",
                logical_robot_id="robot_1",
                algorithm="CBAA",
                virtual_start_s=20.0,
                device_setup={"pre_state": mutable_b},
                authoritative=DecisionSignature.from_result(
                    result_for("robot_1", 0, 200_000)
                ),
                agx_allocator_time_us=10,
            ),
        ]
        self.factory.mutate_between = lambda: mutable_b["robot_attrs"].__setitem__(
            "value", 999
        )
        measured = self.session.measure_group(calls)
        self.assertEqual(mutable_b["robot_attrs"]["value"], 999)
        self.assertEqual(
            measured[1].device_post_state["robot_attrs"]["value"], 1
        )

    def test_four_contexts_are_isolated_persistent_and_reset(self) -> None:
        self.session.begin_mission(mission())
        first = self.session.measure_group(
            (frozen("robot_2", "c1", "g1", 0, 300_000),)
        )[0]
        self.assertEqual(first.device_post_state["robot_attrs"]["value"], 1)
        self.assertEqual(self.session.context_call_count["robot_2"], 1)
        self.assertEqual(self.session.context_call_count["robot_0"], 0)
        self.assertEqual(
            self.session.context_state["robot_0"]["robot_attrs"]["value"], 0
        )
        self.session.end_mission()
        self.session.begin_mission(mission("trial-2"))
        self.assertTrue(all(value == 0 for value in self.session.context_call_count.values()))
        self.assertTrue(
            all(
                value["robot_attrs"]["value"] == 0
                for value in self.session.context_state.values()
            )
        )
        self.assertGreaterEqual(self.device.reboot_count, 2)

    def test_goal_parity_fails_closed_and_duration_is_not_accepted(self) -> None:
        self.session.begin_mission(mission())
        self.factory.goal_override = (9, 9)
        with self.assertRaises(ParityFailure) as caught:
            self.session.measure_group(
                (frozen("robot_0", "bad", "bad-group", 0, 100_000),)
            )
        self.assertIn("goal", caught.exception.diagnostics["mismatches"])
        self.assertEqual(self.session.last_failure["accepted_duration_count"], 0)
        with self.assertRaisesRegex(SessionStateError, "invalidated"):
            self.session.measure_group(
                (frozen("robot_1", "later", "later-group", 0, 200_000),)
            )

    def test_message_and_state_hashes_are_fail_closed(self) -> None:
        self.session.begin_mission(mission())
        self.factory.message_override = "different"
        with self.assertRaises(ParityFailure) as caught:
            self.session.measure_group(
                (frozen("robot_0", "bad-msg", "bad-msg-group", 0, 100_000),)
            )
        self.assertIn("message_sha256", caught.exception.diagnostics["mismatches"])

    def test_duplicate_group_and_call_ids_are_rejected_before_io(self) -> None:
        self.session.begin_mission(mission())
        self.session.measure_group(
            (frozen("robot_0", "unique", "same-group", 0, 100_000),)
        )
        order = list(self.device.prepare_order)
        with self.assertRaisesRegex(SessionStateError, "duplicate group"):
            self.session.measure_group(
                (frozen("robot_1", "another", "same-group", 0, 200_000),)
            )
        self.assertEqual(self.device.prepare_order, order)

    def test_stale_reply_is_rejected_and_invalidates_trial(self) -> None:
        self.session.begin_mission(mission())
        self.factory.stale_attempt = True
        with self.assertRaises(StaleReplyError):
            self.session.measure_group(
                (frozen("robot_0", "stale", "stale-group", 0, 100_000),)
            )
        self.assertFalse(self.session.valid)

    def test_disconnect_fails_closed_and_reconnect_revalidates_build(self) -> None:
        self.session.begin_mission(mission())
        self.device.disconnect()
        with self.assertRaisesRegex(Exception, "disconnected"):
            self.session.measure_group(
                (frozen("robot_0", "drop", "drop-group", 0, 100_000),)
            )
        with self.assertRaises(Exception):
            self.session.end_mission()
        wrong = DeterministicVirtualDevice(
            "board-a",
            result_factory=self.factory,
            firmware_sha256="different-firmware",
        )
        with self.assertRaises(BoardBindingError):
            self.session.validate_reconnection(wrong)
        replacement = DeterministicVirtualDevice(
            "board-a", result_factory=self.factory, port="VIRTUAL:renumbered"
        )
        fingerprint = self.session.validate_reconnection(replacement)
        self.assertEqual(fingerprint.device_id, "board-a")
        self.assertEqual(fingerprint.port, "VIRTUAL:renumbered")
        self.assertTrue(self.session.valid)
        self.session.begin_mission(mission("trial-after-reconnect"))
        wrong.close()


class BoardBindingAndLeaseTests(unittest.TestCase):
    def test_production_binding_requires_exactly_four_and_stable_ids(self) -> None:
        devices = [DeterministicVirtualDevice(f"board-{index}") for index in range(4)]
        try:
            bindings = bind_hardware_workers(devices)
            self.assertEqual([item.worker_index for item in bindings], [0, 1, 2, 3])
            self.assertEqual([item.board_id for item in bindings], sorted(item.board_id for item in bindings))
            with self.assertRaisesRegex(BoardBindingError, "exactly four"):
                bind_hardware_workers(devices[:3])
            duplicate = DeterministicVirtualDevice("board-0")
            with self.assertRaisesRegex(BoardBindingError, "duplicate"):
                bind_hardware_workers(devices[:3] + [duplicate])
            duplicate.close()
        finally:
            for item in devices:
                item.close()

    def test_explicit_mapping_is_by_identity_not_serial_order(self) -> None:
        devices = [DeterministicVirtualDevice(f"id-{index}") for index in range(4)]
        try:
            mapping = {0: "id-3", 1: "id-1", 2: "id-0", 3: "id-2"}
            bindings = bind_hardware_workers(devices, explicit_mapping=mapping)
            self.assertEqual([item.board_id for item in bindings], ["id-3", "id-1", "id-0", "id-2"])
        finally:
            for item in devices:
                item.close()

    def test_two_workers_cannot_lease_one_board(self) -> None:
        device = DeterministicVirtualDevice("exclusive")
        binding = bind_hardware_workers([device], development_override=True)[0]
        with tempfile.TemporaryDirectory() as temporary:
            first = CausalBoardSession(binding, lock_root=Path(temporary))
            try:
                with self.assertRaises(BoardLeaseError):
                    CausalBoardSession(binding, lock_root=Path(temporary))
            finally:
                first.close()
                device.close()

    def test_worker_can_lease_configured_uid_before_open_validation(self) -> None:
        device = DeterministicVirtualDevice("preleased")
        binding = bind_hardware_workers([device], development_override=True)[0]
        with tempfile.TemporaryDirectory() as temporary:
            lease = BoardLease("preleased", Path(temporary)).acquire()
            session = CausalBoardSession(
                binding, lock_root=Path(temporary), lease=lease
            )
            session.close()
            self.assertFalse(lease.acquired)
        device.close()

    def test_build_mismatch_fails_before_binding(self) -> None:
        device = DeterministicVirtualDevice("build-check", build_id="actual")
        try:
            with self.assertRaisesRegex(BoardBindingError, "build mismatch"):
                bind_hardware_workers(
                    [device],
                    development_override=True,
                    expected_build_id="expected",
                )
        finally:
            device.close()

    def test_firmware_mismatch_fails_before_global_binding(self) -> None:
        devices = [
            DeterministicVirtualDevice(
                f"firmware-{index}", firmware_sha256="actual-firmware"
            )
            for index in range(4)
        ]
        try:
            with self.assertRaisesRegex(BoardBindingError, "firmware hash mismatch"):
                bind_hardware_workers(
                    devices,
                    expected_firmware_sha256="expected-firmware",
                )
        finally:
            for item in devices:
                item.close()

    def test_one_worker_revalidates_only_its_preflight_sealed_board(self) -> None:
        device = DeterministicVirtualDevice(
            "sealed-uid",
            build_id="sealed-build",
            firmware_sha256="sealed-firmware",
            module_set_sha256="sealed-modules",
        )
        try:
            binding = bind_single_hardware_worker(
                device,
                worker_index=2,
                expected_device_id="sealed-uid",
                expected_build_id="sealed-build",
                expected_module_set_sha256="sealed-modules",
                expected_firmware_sha256="sealed-firmware",
            )
            self.assertEqual(binding.worker_index, 2)
            self.assertEqual(binding.board_id, "sealed-uid")
            self.assertEqual(binding.fingerprint.timer_unit, "us")
            with self.assertRaisesRegex(BoardBindingError, "opened board"):
                bind_single_hardware_worker(
                    device,
                    worker_index=2,
                    expected_device_id="another-board",
                    expected_build_id="sealed-build",
                    expected_module_set_sha256="sealed-modules",
                )
        finally:
            device.close()


class NativeFourContextSlotTests(unittest.TestCase):
    def test_four_native_runtime_objects_reside_and_reset_independently(self) -> None:
        config = {
            "mission": "collaborative",
            "algorithm": "CBAA",
            "robot_ids": list(ROBOT_IDS),
            "grid_size": 19,
            "all_tasks": [[1, 1], [2, 2]],
            "active_tasks": [[1, 1], [2, 2]],
            "seed": 17,
            "logical_context_count": 4,
        }
        slot = PersistentRuntimeSlot(create_persistent_runtime)
        slot.begin_trial(config)
        runtime_ids = {}
        for robot_id in ROBOT_IDS:
            slot.prepare(
                robot_id,
                "causal_context",
                state(robot_id),
                events=[],
            )
            runtime_ids[robot_id] = id(slot.runtime)
            slot.runtime.choose_goal()
        self.assertEqual(len(slot.contexts), 4)
        self.assertEqual(len(set(runtime_ids.values())), 4)
        self.assertTrue(all(item.call_index == 1 for item in slot.contexts.values()))

        slot.prepare("robot_0", "causal_context", state("robot_0"), events=[])
        self.assertEqual(id(slot.runtime), runtime_ids["robot_0"])
        slot.runtime.choose_goal()
        self.assertEqual(slot.contexts["robot_0"].call_index, 2)
        self.assertEqual(slot.contexts["robot_1"].call_index, 1)

        slot.clear_context("robot_1")
        self.assertNotIn("robot_1", slot.contexts)
        self.assertIn("robot_0", slot.contexts)
        slot.end_trial()
        self.assertEqual(slot.contexts, {})

    def test_cbaa_epoch_reset_is_separately_timed_known_answer(self) -> None:
        config, pre_state, event = CausalLoopbackProtocolTests._inputs(
            "CBAA", ROBOT_IDS[0]
        )
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, copy.deepcopy(pre_state))
        slot = runtime.state.slot_for_cell((1, 1))
        runtime.allocator.path = [slot]
        runtime.state.set_claim(slot, runtime.state.robot_index, -2.0)

        original_ticks_us = native_runtime_module.ticks_us
        samples = iter((10_000, 10_037))
        native_runtime_module.ticks_us = lambda: next(samples)
        try:
            runtime.apply_delta({"events": [copy.deepcopy(event)]})
        finally:
            native_runtime_module.ticks_us = original_ticks_us

        self.assertEqual(runtime.algorithm_epoch_reset_time_us(), 37)
        self.assertEqual(runtime.allocator.path, [])
        self.assertEqual(runtime.state.claim_owner[slot], -1)

    def test_cbaa_refresh_matching_last_sent_is_not_rebroadcast(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "CBAA", ROBOT_IDS[0]
        )
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, copy.deepcopy(pre_state))
        slot = runtime.state.slot_for_cell((5, 5))
        runtime.state.update_position((5, 6))
        runtime.state.set_claim(slot, runtime.state.robot_index, -2.0)
        runtime.allocator.path = [slot]
        runtime.behavior_last_sent = [
            {"cell": [5, 5], "owner": ROBOT_IDS[0], "value": -1.0}
        ]
        runtime.synchronized_authoritative_state = True

        decision = runtime.choose_goal()
        messages = runtime.drain_messages()

        self.assertEqual(decision.goal, (5, 5))
        self.assertEqual(runtime.state.claim_value[slot], -1.0)
        self.assertEqual(messages, [])

    def test_epoch_reset_times_populated_resident_state_before_checkpoint(self) -> None:
        reset_state = {
            "CBAA": {
                "cbaa_current_task": None,
                "cbaa_winner_by_cell": {},
                "cbaa_winning_bid_by_cell": {},
                "cbaa_pending_deltas": {},
                "cbaa_last_sent_signatures": {},
            },
            "ACBBA": {
                "acbba_path": [],
                "acbba_winner_by_cell": {},
                "acbba_winning_bid_by_cell": {},
                "acbba_bid_time_by_cell": {},
                "acbba_pending_deltas": {},
                "acbba_pending_snapshot": False,
                "acbba_last_sent_signatures": {},
                "acbba_bid_counter": 0,
            },
            "PI": {
                "pi_path": [],
                "pi_owner_by_cell": {},
                "pi_significance_by_cell": {},
                "pi_time_by_cell": {},
                "pi_pending_snapshot": False,
                "pi_last_sent_signature": [],
                "pi_time_counter": 0,
            },
            "HIPC": {
                "hipc_path": [],
                "hipc_winner_by_cell": {},
                "hipc_winning_bid_by_cell": {},
                "hipc_bid_time_by_cell": {},
                "hipc_pending_snapshot": False,
                "hipc_last_sent_signature": [],
                "hipc_bid_counter": 0,
                "hipc_bad_prediction_count": {ROBOT_IDS[1]: 2},
                "hipc_dropped_peers": [],
                "hipc_last_predicted_peer_first_task": {},
                "hipc_seen_peer_bundle_signature": {},
            },
        }
        for algorithm in ("CBAA", "ACBBA", "PI", "HIPC"):
            with self.subTest(algorithm=algorithm):
                config, pre_state, event = CausalLoopbackProtocolTests._inputs(
                    algorithm, ROBOT_IDS[0]
                )
                runtime = create_persistent_runtime(config)
                runtime.reset_trial(config, copy.deepcopy(pre_state))
                slot = runtime.state.slot_for_cell((1, 1))
                runtime.state.set_claim(slot, runtime.state.robot_index, -3.0, 7)
                runtime.allocator.path = [slot]
                if hasattr(runtime.allocator, "bid_counter"):
                    runtime.allocator.bid_counter = 19
                if hasattr(runtime.allocator, "time_counter"):
                    runtime.allocator.time_counter = 23
                if algorithm == "HIPC":
                    runtime.allocator.bad_prediction_count = {ROBOT_IDS[1]: 2}
                    runtime.allocator.dropped_peers = [ROBOT_IDS[2]]
                    runtime.allocator.last_predicted_peer_first_task = {
                        ROBOT_IDS[1]: [3, 3]
                    }
                    runtime.allocator.seen_peer_bundle_signature = {
                        ROBOT_IDS[1]: ((3, 3),)
                    }
                runtime.authoritative_message_seed = [{"type": "stale"}]
                runtime.authoritative_pending_snapshot = True
                runtime.authoritative_last_sent = {"stale": True}
                runtime.behavior_last_sent = [
                    {"cell": [1, 1], "owner": ROBOT_IDS[0], "value": -3.0}
                ]

                observed: dict[str, object] = {}
                original_reset = runtime.allocator.on_allocation_epoch

                def inspect_then_reset(reason, admitted, epoch_index=None):
                    observed["path"] = list(runtime.allocator.path)
                    observed["owner"] = int(runtime.state.claim_owner[slot])
                    observed["pending"] = list(runtime.authoritative_message_seed)
                    return original_reset(reason, admitted, epoch_index)

                runtime.allocator.on_allocation_epoch = inspect_then_reset
                checkpoint = copy.deepcopy(pre_state)
                checkpoint["robot_attrs"].update(reset_state[algorithm])
                original_ticks_us = native_runtime_module.ticks_us
                samples = iter((20_000, 20_037))
                native_runtime_module.ticks_us = lambda: next(samples)
                try:
                    runtime.apply_delta(
                        {
                            "set": checkpoint,
                            "events": [copy.deepcopy(event)],
                        }
                    )
                finally:
                    native_runtime_module.ticks_us = original_ticks_us

                self.assertEqual(observed["path"], [slot])
                self.assertEqual(observed["owner"], runtime.state.robot_index)
                self.assertEqual(observed["pending"], [{"type": "stale"}])
                self.assertEqual(runtime.algorithm_epoch_reset_time_us(), 37)
                self.assertEqual(runtime.allocator.path, [])
                self.assertEqual(runtime.state.claim_owner[slot], -1)
                self.assertEqual(runtime.behavior_last_sent, [])
                if hasattr(runtime.allocator, "bid_counter"):
                    self.assertEqual(runtime.allocator.bid_counter, 0)
                if hasattr(runtime.allocator, "time_counter"):
                    self.assertEqual(runtime.allocator.time_counter, 0)
                if algorithm == "HIPC":
                    self.assertEqual(
                        runtime.allocator.bad_prediction_count,
                        {ROBOT_IDS[1]: 2},
                    )
                    self.assertEqual(runtime.allocator.dropped_peers, [])
                    self.assertEqual(
                        runtime.allocator.last_predicted_peer_first_task, {}
                    )
                    self.assertEqual(
                        runtime.allocator.seen_peer_bundle_signature, {}
                    )


class CausalLoopbackProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        manifest = build_device_bundle(compile_mpy=True)
        cls.build_root = Path(str(manifest["output"]))

    @staticmethod
    def _inputs(algorithm: str, robot_id: str) -> tuple[dict, dict, dict]:
        tasks = [[1, 1], [3, 3], [5, 5]]
        config = {
            "mission": "collaborative",
            "algorithm": algorithm,
            "robot_ids": list(ROBOT_IDS),
            "grid_size": 19,
            "all_tasks": tasks,
            "max_candidate_cells": None,
            "seed": 73,
            "commitment_horizon": 3,
        }
        pre_state = {
            "robot_attrs": {
                "rid": robot_id,
                "robot_id": robot_id,
                "pos": [0, 0],
                "grid_size": 19,
            },
            "views": {
                "all_tasks": tasks,
                "active_tasks": tasks,
                "peer_positions": {
                    peer: [0, 0] for peer in ROBOT_IDS if peer != robot_id
                },
                "target_p": [1.0] * len(tasks),
            },
            "cfg": dict(config),
            "belief": {},
            "allocator_attrs": {},
        }
        event = {
            "kind": "allocation_epoch",
            "payload": {
                "epoch_index": 0,
                "trigger_reason": "initial_tasks",
                "admitted_cells": tasks,
            },
        }
        return config, pre_state, event

    @classmethod
    def _authoritative_signature(
        cls, algorithm: str, robot_id: str
    ) -> DecisionSignature:
        config, pre_state, event = cls._inputs(algorithm, robot_id)
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, copy.deepcopy(pre_state))
        runtime.apply_delta({"events": [copy.deepcopy(event)]})
        decision = runtime.choose_goal()
        messages = runtime.drain_messages()
        post_state = runtime.snapshot_minimal()
        _, after = runtime.candidate_counts()
        return DecisionSignature.from_result(
            {
                "goal": decision.goal,
                "messages": messages,
                "post_state": post_state,
                "candidate_count_after": after,
                "call_class": runtime.call_class(),
            }
        )

    def test_primary_algorithms_use_split_timer_and_four_resident_contexts(self) -> None:
        for algorithm in ("CBAA", "ACBBA", "PI", "HIPC"):
            with self.subTest(algorithm=algorithm), tempfile.TemporaryDirectory() as temporary:
                device = LoopbackReplayDevice(
                    f"causal-{algorithm.lower()}", build_root=self.build_root
                )
                binding = bind_hardware_workers(
                    [device], development_override=True
                )[0]
                session = CausalBoardSession(
                    binding, lock_root=Path(temporary)
                )
                try:
                    config, _, _ = self._inputs(algorithm, ROBOT_IDS[0])
                    initial = {
                        robot_id: self._inputs(algorithm, robot_id)[1]
                        for robot_id in ROBOT_IDS
                    }
                    session.begin_mission(
                        MissionBinding(
                            f"trial-{algorithm.lower()}",
                            f"condition-{algorithm.lower()}",
                            algorithm,
                            73,
                            ROBOT_IDS,
                            config,
                            initial,
                        )
                    )
                    calls = []
                    for robot_id in ROBOT_IDS:
                        _, pre_state, event = self._inputs(algorithm, robot_id)
                        calls.append(
                            FrozenCall(
                                call_id=f"call-{algorithm}-{robot_id}",
                                group_id=f"group-{algorithm}",
                                trial_id=f"trial-{algorithm.lower()}",
                                logical_robot_id=robot_id,
                                algorithm=algorithm,
                                virtual_start_s=10.0,
                                device_setup={
                                    "pre_state": pre_state,
                                    "events": [event],
                                },
                                authoritative=self._authoritative_signature(
                                    algorithm, robot_id
                                ),
                                agx_allocator_time_us=1,
                            )
                        )
                    measured = session.measure_group(tuple(calls))
                    self.assertEqual(len(measured), 4)
                    self.assertTrue(all(item.parity_ok for item in measured))
                    self.assertTrue(
                        all(
                            item.virtual_completion_s
                            == 10.0 + item.device_duration_s
                            for item in measured
                        )
                    )
                    self.assertEqual(
                        len(device.serial.persistent_slot.contexts), 4
                    )
                    self.assertEqual(device.serial.context_clear_count, 0)
                    self.assertTrue(
                        all(
                            item.serial_roundtrip_us
                            == item.psetup_transaction_us
                            + item.ptime_result_transaction_us
                            for item in measured
                        )
                    )
                    self.assertTrue(
                        all(
                            item.device_pre_call_setup_us is not None
                            and item.device_pre_call_setup_us >= 0
                            for item in measured
                        )
                    )
                    self.assertTrue(
                        all(
                            item.device_allocator_time_us
                            == item.device_choose_goal_us
                            + item.algorithm_epoch_reset_us
                            for item in measured
                        )
                    )
                    self.assertTrue(
                        all(not item.metadata["hardware_valid"] for item in measured)
                    )
                finally:
                    session.close()
                    device.close()

    def test_automated_preflight_exercises_all_checks_but_labels_loopback_pending(self) -> None:
        manifest = json.loads(
            (self.build_root / "manifest.json").read_text(encoding="utf-8")
        )
        devices = [
            LoopbackReplayDevice(f"preflight-{index}", build_root=self.build_root)
            for index in range(4)
        ]
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                report = run_native_preflight(
                    devices,
                    lock_root=root / "locks",
                    expected_build_id=manifest["build_id"],
                    expected_module_set_sha256=manifest[
                        "deployed_module_set_sha256"
                    ],
                    expected_firmware_sha256="loopback-firmware",
                    reconnect_factory=lambda fingerprint: LoopbackReplayDevice(
                        fingerprint.device_id, build_root=self.build_root
                    ),
                    native_hardware=False,
                    json_path=root / "preflight.json",
                    text_path=root / "preflight.txt",
                )
                statuses = {
                    item["check_id"]: item["status"]
                    for item in report["checks"]
                }
                self.assertEqual(report["summary"], {"passed": 19, "failed": 0, "pending": 1})
                self.assertEqual(statuses["native_runtime"], "PENDING")
                self.assertTrue(
                    all(
                        status == "PASS"
                        for check_id, status in statuses.items()
                        if check_id != "native_runtime"
                    )
                )
                self.assertFalse(report["hardware_valid"])
                self.assertTrue((root / "preflight.json").is_file())
                self.assertIn(
                    "Hardware-valid: FAIL",
                    (root / "preflight.txt").read_text(encoding="utf-8"),
                )
        finally:
            for device in devices:
                device.close()

    def test_primary_algorithms_pass_longitudinal_delayed_admission(self) -> None:
        robot_ids = generate_robot_ids(4)
        config = SimConfig(
            grid_size=7,
            robot_ids=robot_ids,
            start_positions=edge_even_start_positions(7, robot_ids),
            comm_delay_s=0.0,
            comm_delay_jitter_s=0.0,
            collision_intent_settle_s=0.0,
            async_initial_spread_s=0.0,
            async_step_mean_s=1.0,
            async_step_jitter_s=0.0,
            async_min_delay_s=1.0,
            async_max_delay_s=1.0,
            debug_max_events=50_000,
            debug_max_stagnant_events=10_000,
        )
        for algorithm_index, algorithm in enumerate(
            ("CBAA", "ACBBA", "PI", "HIPC"), start=1
        ):
            with self.subTest(algorithm=algorithm), tempfile.TemporaryDirectory() as temporary:
                device = LoopbackReplayDevice(
                    f"causal-longitudinal-{algorithm.lower()}",
                    build_root=self.build_root,
                )
                session = CausalBoardSession(
                    bind_hardware_workers(
                        [device], development_override=True
                    )[0],
                    lock_root=Path(temporary),
                )
                try:
                    state = AsyncTrialRunner(
                        config,
                        load_allocator_class(algorithm),
                        IdealModel(),
                        seed=5,
                        timing_provider=session,
                    ).run_online_trial(
                        TrialScenario(
                            99_020 + algorithm_index,
                            [(3, 1), (5, 5)],
                        ),
                        [0.0, 0.5],
                        ReallocationPolicy.eager(),
                    )
                    self.assertTrue(state.done)
                    self.assertTrue(state.world.all_targets_completed())
                    self.assertGreater(
                        len(state.reallocation_scheduler.allocator_calls), 20
                    )
                finally:
                    session.close()
                    device.close()


class SimulatedProviderTests(unittest.TestCase):
    def test_simulated_and_zero_providers_share_independent_group_semantics(self) -> None:
        calls = (
            frozen("robot_0", "a", "g", 0, 100_000, start_s=5.0),
            frozen("robot_1", "b", "g", 0, 200_000, start_s=5.0),
        )
        provider = SimulatedDurationProvider({"a": 20_000, "b": 70_000})
        provider.begin_mission(mission())
        measured = provider.measure_group(calls)
        self.assertEqual([item.virtual_completion_s for item in measured], [5.02, 5.07])
        self.assertTrue(all(not item.metadata["hardware_valid"] for item in measured))
        provider.end_mission()
        zero = ZeroDurationProvider()
        zero.begin_mission(mission())
        self.assertEqual(
            [item.virtual_completion_s for item in zero.measure_group(calls)],
            [5.0, 5.0],
        )

    def test_structural_core_call_adapter(self) -> None:
        @dataclass
        class CoreCall:
            call_id: str
            group_id: str
            trial_id: str
            robot_id: str
            algorithm: str
            virtual_start_s: float
            pre_state: dict
            authoritative_goal: tuple[int, int]
            candidate_count: int
            message_sha256: str
            post_state_sha256: str
            call_type: str
            agx_duration_ns: int

        reference = frozen("robot_0", "adapt", "adapt-group", 0, 100_000)
        call = CoreCall(
            "adapt",
            "adapt-group",
            "trial-1",
            "robot_0",
            "CBAA",
            2.0,
            state("robot_0"),
            reference.authoritative.goal,
            reference.authoritative.active_candidate_count,
            reference.authoritative.message_sha256,
            reference.authoritative.post_state_sha256,
            reference.authoritative.call_class,
            12_000,
        )
        provider = ZeroDurationProvider()
        provider.begin_mission(mission())
        measured = provider.measure_group((call,))[0]
        self.assertEqual(measured.robot_id, "robot_0")
        self.assertEqual(measured.agx_allocator_time_us, 12)

    def test_exact_causal_core_dataclasses_are_structurally_compatible(self) -> None:
        from known_visit_sim.core.timing import (
            DecisionSignature as CoreSignature,
            FrozenAllocatorCall as CoreCall,
            MissionTimingBinding as CoreMission,
        )

        reference = frozen("robot_0", "core", "core-group", 0, 100_000)
        core_signature = CoreSignature(
            goal=reference.authoritative.goal,
            active_candidate_count=reference.authoritative.active_candidate_count,
            message_sha256=reference.authoritative.message_sha256,
            post_state_sha256=reference.authoritative.post_state_sha256,
            call_class=reference.authoritative.call_class,
        )
        call = CoreCall(
            call_id="core",
            group_id="core-group",
            trial_id="trial-1",
            logical_robot_id="robot_0",
            algorithm="CBAA",
            virtual_start_s=3.0,
            device_setup={"pre_state": state("robot_0")},
            authoritative=core_signature,
            agx_allocator_time_us=42,
        )
        binding = CoreMission(
            "trial-1",
            "condition-cbaa",
            "CBAA",
            17,
            ROBOT_IDS,
            {},
            {item: state(item) for item in ROBOT_IDS},
        )
        provider = ZeroDurationProvider()
        provider.begin_mission(binding)
        measured = provider.measure_group((call,))[0]
        self.assertEqual(measured.virtual_completion_s, 3.0)
        self.assertEqual(measured.authoritative, reference.authoritative)


class CrossImplementationProjectionTests(unittest.TestCase):
    @staticmethod
    def desktop_state() -> dict:
        return {
            "robot_attrs": {
                "rid": "00",
                "pos": (0, 0),
                "grid_size": 19,
                "cbaa_current_task": (1, 0),
                "cbaa_winner_by_cell": {(1, 0): "00"},
                "cbaa_winning_bid_by_cell": {(1, 0): -1.0},
                "collision_avoidance_active": False,
            },
            "views": {"active_tasks": {(1, 0), (1, 6)}},
            "cfg": {"grid_size": 19},
            "belief": {},
            "allocator_attrs": {},
        }

    @staticmethod
    def native_state(*, owner: str = "00") -> dict:
        return {
            "robot_attrs": {},
            "views": {},
            "cfg": {},
            "belief": {},
            "allocator_attrs": {
                "native_collaborative_resume": {
                    "algorithm": "CBAA",
                    "state": {
                        "grid_size": 19,
                        "robot_id": "00",
                        "position": 0,
                        "active": [1, 115],
                        "claims": [[1, owner, -1.0, 2]],
                        "collision_active": False,
                    },
                    "allocator": {"path": [1]},
                }
            },
        }

    def test_enriched_native_messages_and_compact_state_match_shared_semantics(self) -> None:
        agx_messages = [
            {
                "type": "cbaa_entry",
                "sender": "00",
                "x": 1,
                "y": 0,
                "winner": "00",
                "bid": -1.0,
            }
        ]
        device_messages = [
            {
                **agx_messages[0],
                "owner": "00",
                "value": -1.0,
                "significance": -1.0,
                "timestamp": 2,
                "released": False,
            }
        ]
        result = projected_parity(
            algorithm="CBAA",
            authoritative_messages=agx_messages,
            device_messages=device_messages,
            authoritative_post_state=self.desktop_state(),
            device_post_state=self.native_state(),
        )
        self.assertTrue(result["message_match"])
        self.assertTrue(result["state_match"])

    def test_different_owner_remains_a_fail_closed_state_mismatch(self) -> None:
        result = projected_parity(
            algorithm="CBAA",
            authoritative_messages=[],
            device_messages=[],
            authoritative_post_state=self.desktop_state(),
            device_post_state=self.native_state(owner="01"),
        )
        self.assertFalse(result["state_match"])

    @staticmethod
    def desktop_acbba_state() -> dict:
        return {
            "robot_attrs": {
                "rid": "00",
                "pos": (0, 0),
                "grid_size": 19,
                "acbba_path": [(1, 0)],
                "acbba_winner_by_cell": {(1, 0): "00"},
                "acbba_winning_bid_by_cell": {(1, 0): -1.0},
                "acbba_bid_time_by_cell": {(1, 0): 4.0},
                "acbba_bid_counter": 4,
                "acbba_pending_deltas": {},
                "acbba_pending_snapshot": False,
                "acbba_last_sent_signatures": {
                    (1, 0): ((1, 0), "00", -1.0, 4.0)
                },
                "acbba_last_collision_active": False,
                "acbba_last_reallocation_trigger": None,
                "collision_avoidance_active": False,
                "last_allocation_epoch_index": 2,
                "last_allocation_epoch_reason": "task_arrival_eager",
                "last_allocation_epoch_admitted": [(1, 6)],
            },
            "views": {
                "active_tasks": {(1, 0), (1, 6)},
                "target_p": {(1, 0): 1.0, (1, 6): 1.0},
                "peer_positions": {"01": (0, 6)},
            },
            "cfg": {
                "grid_size": 19,
                "robot_ids": ["00", "01"],
                "all_tasks": [(1, 0), (1, 6)],
            },
            "belief": {},
            "allocator_attrs": {},
        }

    @staticmethod
    def native_acbba_state() -> dict:
        return {
            "robot_attrs": {},
            "views": {},
            "cfg": {},
            "belief": {},
            "allocator_attrs": {
                "native_collaborative_resume": {
                    "algorithm": "ACBBA",
                    "state": {
                        "grid_size": 19,
                        "robot_id": "00",
                        "robot_ids": ["00", "01"],
                        "position": 0,
                        "peer_positions": [["00", 0], ["01", 114]],
                        "targets": [1, 115],
                        "active": [1, 115],
                        "probability": [1.0, 1.0],
                        "claims": [[1, "00", -1.0, 4]],
                        "collision_active": False,
                        "event_counter": 4,
                        "last_allocation_epoch_index": 2,
                        "last_allocation_epoch_reason": "task_arrival_eager",
                        "last_allocation_epoch_admitted": [115],
                    },
                    "allocator": {
                        "path": [1],
                        "last_collision_active": False,
                        "last_call_path": "bundle_retained",
                        "bid_counter": 4,
                    },
                    "behavior": {
                        "pending_messages": [],
                        "pending_snapshot": False,
                        "last_sent": [
                            {
                                "cell": [1, 0],
                                "owner": "00",
                                "value": -1.0,
                                "timestamp": 4,
                            }
                        ],
                        "protocol_counter": 4,
                    },
                }
            },
        }

    def acbba_projection(self, native: dict | None = None) -> dict:
        return projected_parity(
            algorithm="ACBBA",
            authoritative_messages=[],
            device_messages=[],
            authoritative_post_state=self.desktop_acbba_state(),
            device_post_state=native or self.native_acbba_state(),
            authoritative_call_class="candidate_filter_only",
            device_call_class="candidate_filter_only",
        )

    def test_persistent_timing_and_collision_fields_are_in_state_projection(self) -> None:
        self.assertTrue(self.acbba_projection()["state_match"])
        mutations = (
            lambda resume: resume["allocator"].__setitem__("last_collision_active", True),
            lambda resume: resume["allocator"].__setitem__("last_call_path", "collision_replan"),
            lambda resume: resume["allocator"].__setitem__("bid_counter", 5),
            lambda resume: resume["state"]["claims"][0].__setitem__(3, 5),
            lambda resume: resume["state"].__setitem__("last_allocation_epoch_index", 3),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                native = self.native_acbba_state()
                resume = native["allocator_attrs"]["native_collaborative_resume"]
                mutate(resume)
                self.assertFalse(self.acbba_projection(native)["state_match"])

    def test_pending_and_last_sent_protocol_state_are_not_omitted(self) -> None:
        native = self.native_acbba_state()
        behavior = native["allocator_attrs"]["native_collaborative_resume"]["behavior"]
        behavior["pending_snapshot"] = True
        self.assertFalse(self.acbba_projection(native)["state_match"])
        native = self.native_acbba_state()
        behavior = native["allocator_attrs"]["native_collaborative_resume"]["behavior"]
        behavior["last_sent"][0]["timestamp"] = 5
        self.assertFalse(self.acbba_projection(native)["state_match"])

    def test_protocol_message_timestamp_and_bundle_metadata_are_fail_closed(self) -> None:
        agx = [{
            "type": "acbba_entry",
            "sender": "00",
            "x": 1,
            "y": 0,
            "winner": "00",
            "bid": -1.0,
            "timestamp": 4,
            "order": 0,
            "bundle_cells": [{"x": 1, "y": 0}],
            "bundle_size": 1,
        }]
        device = copy.deepcopy(agx)
        equal = projected_parity(
            algorithm="ACBBA",
            authoritative_messages=agx,
            device_messages=device,
            authoritative_post_state=self.desktop_acbba_state(),
            device_post_state=self.native_acbba_state(),
            authoritative_call_class="candidate_filter_only",
            device_call_class="candidate_filter_only",
        )
        self.assertTrue(equal["message_match"])
        device[0]["timestamp"] = 5
        changed = projected_parity(
            algorithm="ACBBA",
            authoritative_messages=agx,
            device_messages=device,
            authoritative_post_state=self.desktop_acbba_state(),
            device_post_state=self.native_acbba_state(),
            authoritative_call_class="candidate_filter_only",
            device_call_class="candidate_filter_only",
        )
        self.assertFalse(changed["message_match"])

    def test_hipc_prediction_state_mutation_is_fail_closed(self) -> None:
        desktop = self.desktop_acbba_state()
        native = self.native_acbba_state()
        desktop_robot = desktop["robot_attrs"]
        native_resume = native["allocator_attrs"]["native_collaborative_resume"]
        replacements = (
            ("acbba_path", "hipc_path"),
            ("acbba_winner_by_cell", "hipc_winner_by_cell"),
            ("acbba_winning_bid_by_cell", "hipc_winning_bid_by_cell"),
            ("acbba_bid_time_by_cell", "hipc_bid_time_by_cell"),
            ("acbba_bid_counter", "hipc_bid_counter"),
            ("acbba_pending_snapshot", "hipc_pending_snapshot"),
            ("acbba_last_collision_active", "hipc_last_collision_active"),
            ("acbba_last_reallocation_trigger", "hipc_last_reallocation_trigger"),
        )
        for old, new in replacements:
            desktop_robot[new] = desktop_robot.pop(old)
        desktop_robot["hipc_last_sent_signature"] = (((1, 0), -1.0, 4.0),)
        desktop_robot.pop("acbba_pending_deltas")
        desktop_robot.pop("acbba_last_sent_signatures")
        desktop_robot["hipc_bad_prediction_count"] = {}
        desktop_robot["hipc_dropped_peers"] = set()
        desktop_robot["hipc_last_predicted_peer_first_task"] = {}
        desktop_robot["hipc_seen_peer_bundle_signature"] = {}
        native_resume["algorithm"] = "HIPC"
        native_resume["allocator"].pop("bid_counter")
        native_resume["behavior"]["last_sent"][0].pop("owner")
        native_resume["behavior"]["bad_prediction_count"] = {}
        arguments = dict(
            algorithm="HIPC",
            authoritative_messages=[],
            device_messages=[],
            authoritative_post_state=desktop,
            device_post_state=native,
            authoritative_call_class="candidate_filter_only",
            device_call_class="candidate_filter_only",
        )
        self.assertTrue(projected_parity(**arguments)["state_match"])
        desktop_robot["hipc_bad_prediction_count"] = {"01": 1}
        self.assertFalse(projected_parity(**arguments)["state_match"])
        desktop_robot["hipc_bad_prediction_count"] = {}
        native_resume["behavior"]["protocol_counter"] = 5
        self.assertFalse(projected_parity(**arguments)["state_match"])


class PreflightReportTests(unittest.TestCase):
    @staticmethod
    def native_fingerprints() -> list[BoardFingerprint]:
        return [
            BoardFingerprint(
                device_id=f"native-{index}",
                port=f"/dev/serial/by-id/native-{index}",
                build_id="sealed-build",
                firmware_sha256="firmware",
                module_set_sha256="modules",
                implementation="MicroPython-1.24-native",
                frequency_hz=125_000_000,
                timer_unit="us",
                timer_resolution_us=1,
                timer_monotonic=True,
                timer_wraparound_safe=True,
            )
            for index in range(4)
        ]

    def test_zero_timer_resolution_cannot_be_hardware_valid(self) -> None:
        fingerprints = self.native_fingerprints()
        fingerprints[0] = replace(fingerprints[0], timer_resolution_us=0)
        recorder = CausalPreflightRecorder(native_hardware=True)
        recorder.fingerprints = fingerprints
        for check_id in tuple(recorder._checks):
            recorder.pass_check(check_id, claimed=True)
        report = recorder.report()
        self.assertFalse(report["hardware_valid"])
        self.assertFalse(report["passed"])
        self.assertFalse(report["hardware_validated"])

    def test_machine_and_human_reports_are_sealed_and_fail_closed(self) -> None:
        recorder = CausalPreflightRecorder(native_hardware=True)
        recorder.fingerprints = self.native_fingerprints()
        for check_id in tuple(recorder._checks):
            recorder.pass_check(check_id, known_answer=True)
        report = recorder.report()
        self.assertTrue(report["hardware_valid"])
        self.assertEqual(report["report_kind"], "rp2040_parity_preflight")
        self.assertTrue(report["passed"])
        self.assertTrue(report["hardware_validated"])
        self.assertEqual(verify_preflight_report(report)["report_sha256"], report["report_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            json_path, text_path = write_preflight_reports(
                report, root / "preflight.json", root / "preflight.txt"
            )
            loaded = load_and_verify_preflight(json_path)
            self.assertTrue(loaded["hardware_valid"])
            self.assertIn("Hardware-valid: PASS", text_path.read_text(encoding="utf-8"))
            corrupted = json.loads(json_path.read_text(encoding="utf-8"))
            corrupted["checks"][0]["status"] = "FAIL"
            with self.assertRaisesRegex(BoardBindingError, "hash mismatch"):
                verify_preflight_report(corrupted)

    def test_pending_or_virtual_evidence_can_never_be_hardware_valid(self) -> None:
        recorder = CausalPreflightRecorder(native_hardware=False)
        devices = [DeterministicVirtualDevice(f"virtual-{index}") for index in range(4)]
        try:
            recorder.bind_boards(devices)
            report = recorder.report()
            self.assertFalse(report["hardware_valid"])
            with self.assertRaisesRegex(BoardBindingError, "failed or pending"):
                verify_preflight_report(report)
        finally:
            for item in devices:
                item.close()


if __name__ == "__main__":
    unittest.main()
