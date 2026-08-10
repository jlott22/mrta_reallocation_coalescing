"""Regression contracts for bounded causal RP2040 event staging.

These tests isolate the host/device setup contract that prevents a complete
allocator-message burst from being reconstructed beside four resident native
contexts on an RP2040.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
ARCHITECTURE = ROOT / "Simulation" / "Architecture"
if str(ARCHITECTURE) not in sys.path:
    sys.path.insert(0, str(ARCHITECTURE))

from allocator_replay.capture.codec import canonical_json_bytes  # noqa: E402
from allocator_replay.causal.session import (  # noqa: E402
    CausalBoardSession,
    persistent_event_batches,
)
from allocator_replay.device.native.collaborative import (  # noqa: E402
    create_persistent_runtime,
)
from allocator_replay.device.native.collaborative import (  # noqa: E402
    runtime as native_runtime_module,
)


ROBOT_IDS = ("robot_0", "robot_1", "robot_2", "robot_3")
STATE_SECTIONS = (
    "robot_attrs",
    "views",
    "cfg",
    "belief",
    "allocator_attrs",
)


def empty_state() -> dict[str, dict]:
    return {section: {} for section in STATE_SECTIONS}


def full_state(robot_id: str) -> dict[str, dict]:
    return {
        "robot_attrs": {
            "rid": robot_id,
            "cbaa_current_task": None,
            "cbaa_winner_by_cell": {},
            "cbaa_winning_bid_by_cell": {},
            "cbaa_pending_deltas": {},
            "cbaa_last_sent_signatures": {},
        },
        "views": {
            "all_tasks": [[1, 1], [2, 2]],
            "active_tasks": [[1, 1], [2, 2]],
        },
        "cfg": {"robot_ids": list(ROBOT_IDS)},
        "belief": {},
        "allocator_attrs": {},
    }


def allocator_event(sender: str, x: int) -> dict:
    return {
        "kind": "allocator_message",
        "payload": {
            "type": "cbaa_entry",
            "sender": sender,
            "x": x,
            "y": 1,
            "winner": sender,
            "bid": -float(x),
        },
    }


def setup(events: list[dict]) -> dict:
    return {
        "schema": 1,
        "fixture_id": "causal/trial/call",
        "condition_id": "condition-cbaa",
        "mission": "collaborative",
        "algorithm": "CBAA",
        "context_id": ROBOT_IDS[0],
        "setup_mode": "causal_context",
        "deleted": {},
        "events": copy.deepcopy(events),
        "resume_state": {},
        "pre_state": full_state(ROBOT_IDS[0]),
    }


class RecordingDevice:
    def __init__(self) -> None:
        self.calls: list[tuple[dict, str]] = []

    def prepare_persistent_call(self, value: dict, attempt_id: str) -> dict:
        self.calls.append((copy.deepcopy(value), attempt_id))
        index = len(self.calls)
        return {
            "psetup_transaction_us": index * 1_000,
            "device_pre_call_setup_us": index * 10,
            "host_prepare_cpu_us": index * 100,
        }


def bare_session(*, prior_calls: int) -> tuple[CausalBoardSession, RecordingDevice]:
    """Construct only the state used by the private staging helper."""

    device = RecordingDevice()
    session = object.__new__(CausalBoardSession)
    session.device = device
    session.context_call_count = {ROBOT_IDS[0]: prior_calls}
    return session, device


class PersistentEventBatchTests(unittest.TestCase):
    def test_events_remain_ordered_and_atomic_within_bound(self) -> None:
        events = [allocator_event(ROBOT_IDS[index], index) for index in range(1, 4)]

        batches = persistent_event_batches(events)

        self.assertEqual(batches, [[event] for event in events])
        self.assertTrue(all(len(batch) == 1 for batch in batches))
        self.assertTrue(
            all(len(canonical_json_bytes(batch)) <= 768 for batch in batches)
        )

    def test_oversized_atomic_event_is_rejected_before_device_io(self) -> None:
        oversized = {
            "kind": "allocator_message",
            "payload": {"sender": ROBOT_IDS[1], "body": "x" * 900},
        }

        with self.assertRaisesRegex(
            ValueError,
            "event exceeds bounded setup payload",
        ):
            persistent_event_batches([oversized])


class CausalPersistentStageTests(unittest.TestCase):
    def test_existing_context_stages_events_before_authoritative_checkpoint(self) -> None:
        events = [
            allocator_event(ROBOT_IDS[1], 1),
            allocator_event(ROBOT_IDS[2], 2),
        ]
        session, device = bare_session(prior_calls=7)
        original = setup(events)

        metrics = session._prepare_persistent_stages(original, "attempt-existing")

        self.assertEqual(len(device.calls), 3)
        first, second, checkpoint = [item[0] for item in device.calls]
        self.assertEqual(first["events"], [events[0]])
        self.assertEqual(second["events"], [events[1]])
        self.assertEqual(first["pre_state"], empty_state())
        self.assertEqual(second["pre_state"], empty_state())
        self.assertTrue(first["begin_call_setup"])
        self.assertFalse(second["begin_call_setup"])
        self.assertEqual(checkpoint["pre_state"], original["pre_state"])
        self.assertEqual(checkpoint["events"], [])
        self.assertFalse(checkpoint["begin_call_setup"])
        self.assertEqual(
            [attempt_id for _, attempt_id in device.calls],
            ["attempt-existing"] * 3,
        )
        self.assertEqual(metrics["device_pre_call_setup_us"], 60)
        self.assertEqual(metrics["host_prepare_cpu_us"], 600)
        self.assertEqual(metrics["psetup_transaction_us"], 6_000)

    def test_first_context_bootstraps_state_before_ordered_event_stages(self) -> None:
        events = [
            allocator_event(ROBOT_IDS[1], 1),
            allocator_event(ROBOT_IDS[2], 2),
        ]
        session, device = bare_session(prior_calls=0)
        original = setup(events)

        metrics = session._prepare_persistent_stages(original, "attempt-first")

        self.assertEqual(len(device.calls), 3)
        bootstrap, first, second = [item[0] for item in device.calls]
        self.assertEqual(bootstrap["pre_state"], original["pre_state"])
        self.assertEqual(bootstrap["events"], [])
        self.assertTrue(bootstrap["begin_call_setup"])
        self.assertEqual(first["pre_state"], empty_state())
        self.assertEqual(second["pre_state"], empty_state())
        self.assertEqual(first["events"], [events[0]])
        self.assertEqual(second["events"], [events[1]])
        self.assertFalse(first["begin_call_setup"])
        self.assertFalse(second["begin_call_setup"])
        self.assertEqual(metrics["device_pre_call_setup_us"], 60)
        self.assertEqual(metrics["host_prepare_cpu_us"], 600)
        self.assertEqual(metrics["psetup_transaction_us"], 6_000)

    def test_eventless_existing_context_uses_one_fresh_setup(self) -> None:
        session, device = bare_session(prior_calls=2)
        original = setup([])

        metrics = session._prepare_persistent_stages(original, "attempt-empty")

        self.assertEqual(len(device.calls), 1)
        only = device.calls[0][0]
        self.assertEqual(only["pre_state"], original["pre_state"])
        self.assertEqual(only["events"], [])
        self.assertTrue(only["begin_call_setup"])
        self.assertEqual(metrics["device_pre_call_setup_us"], 10)
        self.assertEqual(metrics["host_prepare_cpu_us"], 100)
        self.assertEqual(metrics["psetup_transaction_us"], 1_000)


class StagedEpochTimingTests(unittest.TestCase):
    def test_epoch_reset_time_accumulates_across_staged_transactions(self) -> None:
        config = {
            "mission": "collaborative",
            "algorithm": "CBAA",
            "robot_ids": list(ROBOT_IDS),
            "grid_size": 19,
            "all_tasks": [[1, 1], [2, 2]],
            "active_tasks": [[1, 1], [2, 2]],
            "seed": 17,
        }
        initial = {
            "rid": ROBOT_IDS[0],
            "robot_ids": list(ROBOT_IDS),
            "all_tasks": [[1, 1], [2, 2]],
            "active_tasks": [[1, 1], [2, 2]],
        }
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, initial)
        events = [
            {
                "kind": "allocation_epoch",
                "payload": {
                    "epoch_index": 0,
                    "trigger_reason": "initial",
                    "admitted_cells": [[1, 1]],
                },
            },
            {
                "kind": "allocation_epoch",
                "payload": {
                    "epoch_index": 1,
                    "trigger_reason": "arrival_eager",
                    "admitted_cells": [[2, 2]],
                },
            },
        ]

        original_ticks_us = native_runtime_module.ticks_us
        samples = iter((1_000, 1_011, 2_000, 2_023))
        native_runtime_module.ticks_us = lambda: next(samples)
        try:
            # Prove that the logical-call boundary clears stale timing once,
            # while each following transport stage preserves the accumulated
            # policy-induced allocator work.
            runtime.pending_algorithm_epoch_reset_us = 999
            runtime.begin_call_setup()
            runtime.apply_delta({"events": [events[0]]})
            runtime.apply_delta({"events": [events[1]]})
            runtime.apply_delta(
                {
                    "set": empty_state(),
                    "events": [],
                }
            )
        finally:
            native_runtime_module.ticks_us = original_ticks_us

        self.assertEqual(runtime.algorithm_epoch_reset_time_us(), 34)


if __name__ == "__main__":
    unittest.main()
