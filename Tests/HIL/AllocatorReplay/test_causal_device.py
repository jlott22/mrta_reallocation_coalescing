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
    DEVICE_ALLOCATOR_TIMER_SCOPE,
    DecisionSignature,
    DeterministicVirtualDevice,
    FrozenCall,
    MissionBinding,
    ParityFailure,
    REQUIRED_HARDWARE_WORKERS,
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
from allocator_replay.coalescing.build import (  # noqa: E402
    build_device_bundle,
)
from allocator_replay.capture.codec import canonical_json_bytes  # noqa: E402
from allocator_replay.host.emulator import LoopbackReplayDevice  # noqa: E402
from allocator_replay.host.transport import _compact_causal_events  # noqa: E402
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
        devices = [
            DeterministicVirtualDevice(f"board-{index}")
            for index in range(REQUIRED_HARDWARE_WORKERS)
        ]
        try:
            bindings = bind_hardware_workers(devices)
            self.assertEqual(
                [item.worker_index for item in bindings],
                list(range(REQUIRED_HARDWARE_WORKERS)),
            )
            self.assertEqual([item.board_id for item in bindings], sorted(item.board_id for item in bindings))
            with self.assertRaisesRegex(
                BoardBindingError, f"exactly {REQUIRED_HARDWARE_WORKERS}"
            ):
                bind_hardware_workers(devices[:2])
            duplicate = DeterministicVirtualDevice("board-0")
            with self.assertRaisesRegex(BoardBindingError, "duplicate"):
                bind_hardware_workers(devices[:-1] + [duplicate])
            duplicate.close()
        finally:
            for item in devices:
                item.close()

    def test_explicit_mapping_is_by_identity_not_serial_order(self) -> None:
        devices = [
            DeterministicVirtualDevice(f"id-{index}")
            for index in range(REQUIRED_HARDWARE_WORKERS)
        ]
        try:
            mapping = {
                index: f"id-{REQUIRED_HARDWARE_WORKERS - 1 - index}"
                for index in range(REQUIRED_HARDWARE_WORKERS)
            }
            bindings = bind_hardware_workers(devices, explicit_mapping=mapping)
            self.assertEqual(
                [item.board_id for item in bindings],
                [
                    f"id-{REQUIRED_HARDWARE_WORKERS - 1 - index}"
                    for index in range(REQUIRED_HARDWARE_WORKERS)
                ],
            )
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
            for index in range(REQUIRED_HARDWARE_WORKERS)
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

    def test_cbaa_admission_is_deferred_and_non_destructive(self) -> None:
        config, pre_state, event = CausalLoopbackProtocolTests._inputs(
            "CBAA", ROBOT_IDS[0]
        )
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, copy.deepcopy(pre_state))
        slot = runtime.state.slot_for_cell((1, 1))
        runtime.allocator.path = [slot]
        runtime.state.set_claim(slot, runtime.state.robot_index, -2.0)

        observed = []
        original_hook = runtime.allocator.on_allocation_epoch

        def observe_hook(reason, admitted, epoch_index=None):
            observed.append((reason, tuple(admitted), epoch_index))
            return original_hook(reason, admitted, epoch_index)

        runtime.allocator.on_allocation_epoch = observe_hook
        runtime.apply_delta({"events": [copy.deepcopy(event)]})

        # Receipt/decoding and registry ingestion are PSETUP work.  The
        # allocator-specific hook is queued for the timed transaction.
        self.assertEqual(observed, [])
        self.assertEqual(runtime.allocator.path, [slot])
        self.assertEqual(runtime.state.claim_owner[slot], runtime.state.robot_index)

        decision = runtime.choose_goal()

        self.assertEqual(len(observed), 1)
        self.assertEqual(decision.goal, (1, 1))
        self.assertEqual(runtime.algorithm_epoch_reset_time_us(), 0)
        self.assertEqual(runtime.allocator.path, [slot])
        self.assertEqual(runtime.state.claim_owner[slot], runtime.state.robot_index)
        self.assertEqual(runtime.call_class(), "full_allocation_solve")

    def test_cbaa_retained_bid_is_not_recomputed_after_movement(self) -> None:
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
        # A retained CBAA claim keeps its auction-time value. Movement must
        # not create a new bid or a stale/new-bid feedback loop.
        self.assertEqual(runtime.state.claim_value[slot], -2.0)
        self.assertEqual(messages, [])

    def test_acbba_refresh_matching_last_sent_is_not_rebroadcast(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "ACBBA", ROBOT_IDS[0]
        )
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, copy.deepcopy(pre_state))
        cells = ((1, 1), (3, 3), (5, 5))
        slots = [runtime.state.slot_for_cell(cell) for cell in cells]
        runtime.allocator.path = list(slots)
        runtime._pre_choose_path = list(slots)
        runtime._post_choose_path = list(slots)
        runtime.synchronized_authoritative_state = True
        runtime.behavior_last_sent = []
        for index, (slot, cell) in enumerate(zip(slots, cells), start=1):
            runtime.state.set_claim(
                slot, runtime.state.robot_index, -float(index), index
            )
            message = {
                "type": "acbba_entry",
                "sender": ROBOT_IDS[0],
                "x": cell[0],
                "y": cell[1],
                "winner": ROBOT_IDS[0],
                "bid": -float(index),
                "timestamp": index,
            }
            runtime.state.queue_message(message)
            runtime.behavior_last_sent.append(
                {
                    "cell": [cell[0], cell[1]],
                    "owner": ROBOT_IDS[0],
                    "value": -float(index),
                    "timestamp": index,
                }
            )

        messages = runtime.drain_messages()

        self.assertEqual(messages, [])

    def test_collision_mechanism_survives_allocation_epoch_snapshot(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "PI", ROBOT_IDS[0]
        )
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, copy.deepcopy(pre_state))
        runtime.last_call_had_epoch_reallocation = True
        runtime.allocator.last_call_path = "collision_replan"

        post_state = runtime.snapshot_minimal()

        behavior = post_state["allocator_attrs"][
            "native_collaborative_resume"
        ]["behavior"]
        self.assertEqual(behavior["call_mechanism"], "collision_replan")

    def test_hipc_unobserved_peers_are_not_reported_as_dropped(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "HIPC", ROBOT_IDS[0]
        )
        pre_state["views"]["peer_positions"] = {}
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, pre_state)

        self.assertEqual(runtime.allocator._team_indices(), [0])
        self.assertEqual(runtime.allocator.dropped_peers, [])

    def test_hipc_equal_score_uses_desktop_xy_tie_break(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "HIPC", ROBOT_IDS[0]
        )
        tasks = [[1, 4], [6, 7], [9, 4]]
        config["all_tasks"] = tasks
        pre_state["views"]["all_tasks"] = tasks
        pre_state["views"]["active_tasks"] = tasks
        pre_state["views"]["target_p"] = [1.0] * len(tasks)
        pre_state["views"]["peer_positions"] = {}
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, pre_state)

        runtime.choose_goal()

        path = [
            runtime.state.decode_cell(runtime.state.targets[slot])
            for slot in runtime.allocator.path
        ]
        self.assertEqual(path, [(1, 4), (6, 7), (9, 4)])

    def test_hipc_invalid_first_item_replans_unbounded_valid_suffix(self) -> None:
        tasks = [
            (7, 4),
            (3, 5),
            (1, 4),
            (7, 6),
            (7, 3),
            (5, 0),
            (3, 0),
            (6, 3),
        ]
        invalid = (3, 0)
        active = [cell for cell in tasks if cell != invalid]
        old_path = [invalid, (3, 5), (1, 4)]
        config = {
            "mission": "collaborative",
            "algorithm": "HIPC",
            "robot_ids": list(ROBOT_IDS),
            "grid_size": 8,
            "all_tasks": tasks,
            "active_tasks": active,
            "max_candidate_cells": None,
            "seed": 73,
            "commitment_horizon": 3,
        }
        pre_state = {
            "robot_attrs": {
                "rid": ROBOT_IDS[0],
                "robot_id": ROBOT_IDS[0],
                "pos": (2, 7),
                "grid_size": 8,
                "hipc_path": old_path,
                "hipc_bundle": old_path,
                "hipc_winner_by_cell": {
                    invalid: ROBOT_IDS[0],
                    (3, 5): ROBOT_IDS[0],
                    (1, 4): ROBOT_IDS[0],
                    (7, 3): ROBOT_IDS[3],
                    (6, 3): ROBOT_IDS[1],
                },
                "hipc_winning_bid_by_cell": {
                    invalid: -14.0,
                    (3, 5): -1.0,
                    (1, 4): -6.0,
                    (7, 3): -7.0,
                    (6, 3): -13.0,
                },
                "hipc_bid_time_by_cell": {
                    invalid: 1.0,
                    (3, 5): 2.0,
                    (1, 4): 3.0,
                    (7, 3): 4.0,
                    (6, 3): 5.0,
                },
                "hipc_pending_snapshot": False,
                "hipc_last_sent_signature": None,
                "hipc_bid_counter": 3,
                "hipc_last_collision_active": False,
                "collision_avoidance_active": False,
                "hipc_bad_prediction_count": {},
                "hipc_dropped_peers": set(),
                "hipc_last_predicted_peer_first_task": {},
                "hipc_seen_peer_bundle_signature": {},
            },
            "views": {
                "all_tasks": tasks,
                "active_tasks": active,
                "searched": {invalid},
                "local_searched": {invalid},
                "target_p": {cell: 1.0 for cell in tasks},
                "peer_positions": {
                    ROBOT_IDS[1]: (4, 6),
                    ROBOT_IDS[2]: (6, 5),
                    ROBOT_IDS[3]: (6, 6),
                },
            },
            "cfg": dict(config),
            "belief": {},
            "allocator_attrs": {},
        }
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, pre_state)

        runtime.choose_goal()
        messages = runtime.drain_messages()

        path = [
            runtime.state.decode_cell(runtime.state.targets[slot])
            for slot in runtime.allocator.path
        ]
        self.assertEqual(path, [(3, 5), (1, 4)])
        self.assertEqual(messages, [])
        self.assertGreaterEqual(runtime.allocator.bid_counter, 3)
        # The invalid, already searched cell was never admitted into the
        # resident registry merely because it appeared in historical state.
        self.assertIsNone(runtime.state.slot_for_cell(invalid))
        for cell in path:
            slot = runtime.state.slot_for_cell(cell)
            self.assertEqual(
                runtime.state.claim_owner[slot], runtime.state.robot_index
            )

    def test_hipc_lost_middle_item_releases_owned_suffix_before_replan(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "HIPC", ROBOT_IDS[0]
        )
        cells = ((1, 1), (3, 3), (5, 5))
        pre_state["robot_attrs"].update(
            {
                "hipc_path": list(cells),
                "hipc_bundle": list(cells),
                "hipc_winner_by_cell": {
                    cells[0]: ROBOT_IDS[0],
                    cells[1]: ROBOT_IDS[1],
                    cells[2]: ROBOT_IDS[0],
                },
                "hipc_winning_bid_by_cell": {
                    cells[0]: -1.0,
                    cells[1]: -2.0,
                    cells[2]: -3.0,
                },
                "hipc_bid_time_by_cell": {
                    cells[0]: 1.0,
                    cells[1]: 2.0,
                    cells[2]: 3.0,
                },
                "hipc_pending_snapshot": False,
                "hipc_last_sent_signature": None,
                "hipc_bid_counter": 3,
            }
        )
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, pre_state)
        runtime.allocator._team_plan = lambda candidates: {}

        runtime.choose_goal()

        middle = runtime.state.slot_for_cell(cells[1])
        suffix = runtime.state.slot_for_cell(cells[2])
        self.assertEqual(runtime.state.claim_owner[middle], 1)
        self.assertEqual(runtime.state.claim_owner[suffix], -1)

    def test_hipc_restored_valid_prefix_preserves_bid_timestamp(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "HIPC", ROBOT_IDS[0]
        )
        kept = (1, 1)
        invalid = (3, 3)
        pre_state["views"]["active_tasks"] = {kept, (5, 5)}
        pre_state["views"]["searched"] = {invalid}
        pre_state["views"]["local_searched"] = {invalid}
        pre_state["robot_attrs"].update(
            {
                "hipc_path": [kept, invalid],
                "hipc_bundle": [kept, invalid],
                "hipc_winner_by_cell": {
                    kept: ROBOT_IDS[0],
                    invalid: ROBOT_IDS[0],
                },
                "hipc_winning_bid_by_cell": {kept: -1.0, invalid: -2.0},
                "hipc_bid_time_by_cell": {kept: 1.0, invalid: 2.0},
                "hipc_pending_snapshot": False,
                "hipc_last_sent_signature": None,
                "hipc_bid_counter": 2,
            }
        )
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, pre_state)
        kept_slot = runtime.state.slot_for_cell(kept)
        runtime.allocator._team_plan = lambda candidates: {
            runtime.state.robot_index: [kept_slot]
        }

        runtime.choose_goal()
        messages = runtime.drain_messages()

        self.assertEqual(runtime.allocator.path, [kept_slot])
        self.assertEqual(runtime.state.claim_epoch[kept_slot], 1)
        self.assertEqual(runtime.allocator.bid_counter, 2)
        # Reset-time registry projection already removed the invalid,
        # unadmitted suffix.  An unchanged retained prefix is not rebroadcast.
        self.assertEqual(messages, [])

    def test_pi_equal_cost_inclusion_uses_desktop_xy_tie_break(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "PI", ROBOT_IDS[0]
        )
        tasks = [[1, 4], [6, 7], [9, 4]]
        config["all_tasks"] = tasks
        pre_state["views"]["all_tasks"] = tasks
        pre_state["views"]["active_tasks"] = tasks
        pre_state["views"]["target_p"] = [1.0] * len(tasks)
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, pre_state)

        runtime.choose_goal()

        path = [
            runtime.state.decode_cell(runtime.state.targets[slot])
            for slot in runtime.allocator.path
        ]
        self.assertEqual(path, [(1, 4), (9, 4), (6, 7)])

    def test_pi_choose_clears_invalid_peer_claims(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "PI", ROBOT_IDS[0]
        )
        invalid = (3, 3)
        pre_state["views"]["searched"] = {invalid}
        pre_state["robot_attrs"].update(
            {
                "pi_owner_by_cell": {invalid: ROBOT_IDS[1]},
                "pi_significance_by_cell": {invalid: 2.0},
                "pi_time_by_cell": {invalid: 4.0},
                "pi_path": [],
            }
        )
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, pre_state)
        slot = runtime.state.slot_for_cell(invalid)
        self.assertEqual(runtime.state.claim_owner[slot], 1)

        runtime.choose_goal()

        self.assertEqual(runtime.state.claim_owner[slot], -1)

    def test_pi_ignores_unadmitted_invalid_claim_history(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "PI", ROBOT_IDS[0]
        )
        invalid = (3, 3)
        config["all_tasks"] = [invalid]
        pre_state["cfg"] = dict(config)
        pre_state["views"].update(
            {
                "all_tasks": [invalid],
                "active_tasks": [],
                "searched": {invalid},
                "local_searched": {invalid},
                "target_p": {invalid: 1.0},
            }
        )
        pre_state["robot_attrs"].update(
            {
                "pi_owner_by_cell": {invalid: ROBOT_IDS[1]},
                "pi_significance_by_cell": {invalid: 2.0},
                "pi_time_by_cell": {invalid: 4.0},
                "pi_path": [],
                "pi_pending_snapshot": False,
                "pi_last_sent_signature": None,
                "pi_time_counter": 0,
            }
        )
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, pre_state)

        runtime.choose_goal()
        messages = runtime.drain_messages()

        self.assertIsNone(runtime.state.slot_for_cell(invalid))
        self.assertEqual(messages, [])
        self.assertEqual(runtime.allocator.time_counter, 0)

    def test_pi_invalid_claim_suppresses_initialized_empty_snapshot(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "PI", ROBOT_IDS[0]
        )
        invalid = (3, 3)
        config["all_tasks"] = [invalid]
        pre_state["cfg"] = dict(config)
        pre_state["views"].update(
            {
                "all_tasks": [invalid],
                "active_tasks": [],
                "searched": {invalid},
                "local_searched": {invalid},
                "target_p": {invalid: 1.0},
            }
        )
        pre_state["robot_attrs"].update(
            {
                "pi_owner_by_cell": {invalid: ROBOT_IDS[1]},
                "pi_significance_by_cell": {invalid: 2.0},
                "pi_time_by_cell": {invalid: 4.0},
                "pi_path": [],
                "pi_pending_snapshot": False,
                "pi_last_sent_signature": (),
                "pi_time_counter": 0,
            }
        )
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, pre_state)

        runtime.choose_goal()
        messages = runtime.drain_messages()

        self.assertEqual(messages, [])
        self.assertEqual(runtime.allocator.time_counter, 0)

    def test_pi_unbounded_path_incorporates_valid_local_claims(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "PI", ROBOT_IDS[0]
        )
        path_cell = (1, 1)
        stale_cell = (3, 3)
        config["commitment_horizon"] = 1
        pre_state["cfg"] = dict(config)
        pre_state["robot_attrs"].update(
            {
                "pi_owner_by_cell": {
                    path_cell: ROBOT_IDS[0],
                    stale_cell: ROBOT_IDS[0],
                },
                "pi_significance_by_cell": {
                    path_cell: 2.0,
                    stale_cell: 4.0,
                },
                "pi_time_by_cell": {path_cell: 1.0, stale_cell: 2.0},
                "pi_path": [path_cell],
                "pi_pending_snapshot": False,
                "pi_last_sent_signature": None,
                "pi_time_counter": 2,
            }
        )
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, pre_state)

        runtime.choose_goal()
        messages = runtime.drain_messages()

        stale_slot = runtime.state.slot_for_cell(stale_cell)
        self.assertEqual(
            runtime.state.claim_owner[stale_slot], runtime.state.robot_index
        )
        self.assertEqual(len(runtime.allocator.path), 3)
        self.assertIn(stale_slot, runtime.allocator.path)
        self.assertEqual(len(messages), 3)
        self.assertTrue(all(item["path_size"] == 3 for item in messages))

    def test_cbaa_equal_bid_uses_desktop_xy_tie_break(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "CBAA", ROBOT_IDS[0]
        )
        tasks = [[1, 4], [4, 1]]
        config["all_tasks"] = tasks
        pre_state["views"]["all_tasks"] = tasks
        pre_state["views"]["active_tasks"] = tasks
        pre_state["views"]["target_p"] = [1.0] * len(tasks)
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, pre_state)

        decision = runtime.choose_goal()

        self.assertEqual(decision.goal, (1, 4))

    def test_acbba_equal_bid_uses_desktop_xy_tie_break(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "ACBBA", ROBOT_IDS[0]
        )
        tasks = [[0, 1], [1, 4], [6, 7], [9, 4]]
        config["all_tasks"] = tasks
        pre_state["views"]["all_tasks"] = tasks
        pre_state["views"]["active_tasks"] = tasks
        pre_state["views"]["target_p"] = [1.0] * len(tasks)
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, pre_state)

        runtime.choose_goal()

        path = [
            runtime.state.decode_cell(runtime.state.targets[slot])
            for slot in runtime.allocator.path
        ]
        self.assertEqual(path, [(0, 1), (1, 4), (9, 4), (6, 7)])

    def test_acbba_no_time_sentinel_survives_rp2040_float_rounding(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "ACBBA", ROBOT_IDS[0]
        )
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, pre_state)

        normalized = runtime._normalize_last_sent(
            {
                (1, 1): (
                    "acbba_entry",
                    None,
                    -1.0e18,
                    -999999984306749440,
                )
            }
        )

        self.assertEqual(normalized[0]["timestamp"], -1_000_000_000_000_000_000)

    def test_compact_causal_events_preserve_effect_and_reduce_payload(self) -> None:
        config, pre_state, _ = CausalLoopbackProtocolTests._inputs(
            "PI", ROBOT_IDS[0]
        )
        path = [(1, 1), (3, 3), (5, 5)]
        events = []
        for snapshot in range(14):
            sender = ROBOT_IDS[1 + snapshot % 3]
            for order, cell in enumerate(path):
                events.append(
                    {
                        "kind": "allocator_message",
                        "payload": {
                            "type": "pi_entry",
                            "sender": sender,
                            "x": cell[0],
                            "y": cell[1],
                            "owner": sender,
                            "significance": float(order + 1),
                            "timestamp": float(snapshot + order + 1),
                            "order": order,
                            "path_cells": [
                                {"x": item[0], "y": item[1]}
                                for item in path
                            ],
                            "path_size": len(path),
                        },
                    }
                )
        compact = _compact_causal_events(events)
        self.assertLess(
            len(canonical_json_bytes(compact)),
            len(canonical_json_bytes(events)) // 2,
        )

        verbose_runtime = create_persistent_runtime(config)
        compact_runtime = create_persistent_runtime(config)
        verbose_runtime.reset_trial(config, copy.deepcopy(pre_state))
        compact_runtime.reset_trial(config, copy.deepcopy(pre_state))
        verbose_runtime.apply_delta({"events": copy.deepcopy(events)})
        compact_runtime.apply_delta({"events": copy.deepcopy(compact)})

        self.assertEqual(
            compact_runtime.snapshot_minimal(),
            verbose_runtime.snapshot_minimal(),
        )

    def test_admission_hook_runs_after_psetup_without_resetting_state(self) -> None:
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
                original_hook = runtime.allocator.on_allocation_epoch

                def inspect_then_integrate(reason, admitted, epoch_index=None):
                    observed["path"] = list(runtime.allocator.path)
                    observed["owner"] = int(runtime.state.claim_owner[slot])
                    observed["pending"] = list(runtime.authoritative_message_seed)
                    observed["position"] = runtime.state.decode_cell(
                        runtime.state.position
                    )
                    return original_hook(reason, admitted, epoch_index)

                runtime.allocator.on_allocation_epoch = inspect_then_integrate
                runtime.apply_delta(
                    {
                        "set": {"robot_attrs": {"pos": [2, 0]}},
                        "events": [copy.deepcopy(event)],
                    }
                )

                # Generic checkpoint synchronization happens in PSETUP, but
                # allocator-specific input integration is deferred until the
                # timed choose transaction.
                self.assertEqual(observed, {})
                self.assertEqual(runtime.state.decode_cell(runtime.state.position), (2, 0))
                self.assertEqual(runtime.allocator.path, [slot])
                self.assertEqual(runtime.state.claim_owner[slot], runtime.state.robot_index)

                decision = runtime.choose_goal()

                self.assertEqual(observed["path"], [slot])
                self.assertEqual(observed["owner"], runtime.state.robot_index)
                self.assertEqual(observed["pending"], [{"type": "stale"}])
                self.assertEqual(observed["position"], (2, 0))
                self.assertEqual(decision.goal, (1, 1))
                self.assertEqual(runtime.algorithm_epoch_reset_time_us(), 0)
                self.assertEqual(runtime.allocator.path[0], slot)
                self.assertEqual(
                    runtime.state.claim_owner[slot], runtime.state.robot_index
                )
                if hasattr(runtime.allocator, "bid_counter"):
                    self.assertGreaterEqual(runtime.allocator.bid_counter, 19)
                if hasattr(runtime.allocator, "time_counter"):
                    self.assertGreaterEqual(runtime.allocator.time_counter, 23)
                if algorithm == "HIPC":
                    self.assertEqual(
                        runtime.allocator.bad_prediction_count,
                        {ROBOT_IDS[1]: 2},
                    )

    def test_completed_admission_is_active_during_epoch_then_finally_inactive(self) -> None:
        config, pre_state, event = CausalLoopbackProtocolTests._inputs(
            "HIPC", ROBOT_IDS[0]
        )
        admitted = (3, 3)
        pre_state["views"]["active_tasks"] = {(1, 1), (5, 5)}
        pre_state["views"]["searched"] = {admitted}
        event["payload"]["admitted_cells"] = [admitted]
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, copy.deepcopy(pre_state))
        self.assertIsNone(runtime.state.slot_for_cell(admitted))
        observed: dict[str, bool] = {}
        original_hook = runtime.allocator.on_allocation_epoch

        def inspect_then_integrate(reason, cells, epoch_index=None):
            slot = runtime.state.slot_for_cell(admitted)
            self.assertIsNotNone(slot)
            observed["active"] = runtime.state.is_active(slot)
            observed["candidate"] = runtime.state.is_candidate(slot)
            return original_hook(reason, cells, epoch_index)

        runtime.allocator.on_allocation_epoch = inspect_then_integrate

        runtime.apply_delta(
            {"set": copy.deepcopy(pre_state), "events": [copy.deepcopy(event)]}
        )

        # The shell registry learns the announced cell in PSETUP but the
        # algorithm hook is part of the subsequent timed transaction.
        slot = runtime.state.slot_for_cell(admitted)
        self.assertIsNotNone(slot)
        self.assertEqual(observed, {})
        self.assertFalse(runtime.state.is_active(slot))
        runtime.choose_goal()

        self.assertTrue(observed["active"])
        self.assertTrue(observed["candidate"])
        self.assertFalse(runtime.state.is_active(slot))
        self.assertFalse(runtime.state.is_candidate(slot))


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
                        all(
                            item.algorithm_epoch_reset_us == 0
                            and item.metadata[
                                "device_allocator_timer_scope"
                            ] == DEVICE_ALLOCATOR_TIMER_SCOPE
                            for item in measured
                        )
                    )
                    self.assertTrue(
                        all(not item.metadata["hardware_valid"] for item in measured)
                    )
                finally:
                    session.close()
                    device.close()

    def test_large_persistent_event_batch_is_streamed_outside_header(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            device = LoopbackReplayDevice(
                "causal-large-events", build_root=self.build_root
            )
            binding = bind_hardware_workers(
                [device], development_override=True
            )[0]
            session = CausalBoardSession(
                binding, lock_root=Path(temporary)
            )
            headers = []
            original_load_header = device._load_header

            def capture_header(value):
                headers.append(copy.deepcopy(value))
                return original_load_header(value)

            device._load_header = capture_header
            try:
                algorithm = "PI"
                config, pre_state, event = self._inputs(
                    algorithm, ROBOT_IDS[0]
                )
                session.begin_mission(
                    MissionBinding(
                        "trial-large-events",
                        "condition-large-events",
                        algorithm,
                        73,
                        ROBOT_IDS,
                        config,
                        {
                            robot_id: self._inputs(algorithm, robot_id)[1]
                            for robot_id in ROBOT_IDS
                        },
                    )
                )
                padding = [
                    {
                        "kind": "diagnostic_noop",
                        "payload": {"index": index, "padding": "x" * 256},
                    }
                    for index in range(32)
                ]
                call = FrozenCall(
                    call_id="call-large-events",
                    group_id="group-large-events",
                    trial_id="trial-large-events",
                    logical_robot_id=ROBOT_IDS[0],
                    algorithm=algorithm,
                    virtual_start_s=10.0,
                    device_setup={
                        "pre_state": pre_state,
                        "events": [event, *padding],
                    },
                    authoritative=self._authoritative_signature(
                        algorithm, ROBOT_IDS[0]
                    ),
                    agx_allocator_time_us=1,
                )

                measured = session.measure_group((call,))

                self.assertTrue(measured[0].parity_ok)
                setup_header = headers[-1]
                self.assertNotIn("events", setup_header)
                self.assertLess(len(json.dumps(setup_header)), 1024)
            finally:
                session.close()
                device.close()

    def test_automated_preflight_exercises_all_checks_but_labels_loopback_pending(self) -> None:
        manifest = json.loads(
            (self.build_root / "manifest.json").read_text(encoding="utf-8")
        )
        devices = [
            LoopbackReplayDevice(f"preflight-{index}", build_root=self.build_root)
            for index in range(REQUIRED_HARDWARE_WORKERS)
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
            for index in range(REQUIRED_HARDWARE_WORKERS)
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

    def test_reconnect_identity_allows_timer_resolution_jitter(self) -> None:
        fingerprint = self.native_fingerprints()[0]
        jittered = replace(
            fingerprint,
            timer_resolution_us=fingerprint.timer_resolution_us + 10,
        )
        non_monotonic = replace(fingerprint, timer_monotonic=False)

        self.assertEqual(fingerprint.reconnect_key(), jittered.reconnect_key())
        self.assertNotEqual(fingerprint.reconnect_key(), non_monotonic.reconnect_key())

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
        devices = [
            DeterministicVirtualDevice(f"virtual-{index}")
            for index in range(REQUIRED_HARDWARE_WORKERS)
        ]
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
