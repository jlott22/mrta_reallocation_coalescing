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
from allocator_replay.host.transport import (  # noqa: E402
    compact_causal_events,
)
from allocator_replay.hil.persistent import event_batches as hil_event_batches  # noqa: E402


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
            all(
                len(canonical_json_bytes(compact_causal_events(batch)))
                <= 768
                for batch in batches
            )
        )

    def test_full_fifty_cell_bundle_uses_compact_chunkable_wire_form(self) -> None:
        cells = [
            {"x": index % 19, "y": index // 19}
            for index in range(50)
        ]
        event = {
            "kind": "allocator_message",
            "payload": {
                "type": "acbba_entry",
                "sender": ROBOT_IDS[1],
                "x": 1,
                "y": 1,
                "winner": ROBOT_IDS[1],
                "bid": -1.0,
                "timestamp": 4,
                "order": 0,
                "bundle_cells": cells,
                "bundle_size": len(cells),
            },
        }

        self.assertGreater(len(canonical_json_bytes([event])), 768)
        batches = persistent_event_batches([event])
        hil_batches = hil_event_batches([event])
        compact = compact_causal_events(batches[0])
        self.assertLessEqual(len(canonical_json_bytes(compact)), 768)
        self.assertEqual(batches, [[event]])
        self.assertEqual(hil_batches, [[event]])

    def test_repeated_full_bundle_is_shared_in_resident_input_queue(self) -> None:
        cells = [
            {"x": index % 19, "y": index // 19}
            for index in range(50)
        ]
        config = {
            "mission": "collaborative",
            "algorithm": "ACBBA",
            "robot_id": ROBOT_IDS[0],
            "robot_ids": list(ROBOT_IDS),
            "grid_size": 19,
            "max_targets": 50,
        }
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(
            config,
            {
                "robot_id": ROBOT_IDS[0],
                "robot_ids": list(ROBOT_IDS),
                "active_tasks": cells,
            },
        )
        runtime.begin_call_setup()
        for order in (0, 1):
            runtime.apply_delta(
                {
                    "events": [
                        {
                            "kind": "allocator_message",
                            "payload": {
                                "type": "acbba_entry",
                                "sender": ROBOT_IDS[1],
                                "x": order,
                                "y": 0,
                                "winner": ROBOT_IDS[1],
                                "bid": -1.0 - order,
                                "timestamp": 4 + order,
                                "order": order,
                                "bundle_cells": cells,
                                "bundle_size": len(cells),
                            },
                        }
                    ]
                }
            )

        first = runtime.pending_allocator_events[0][1]["bundle_cells"]
        second = runtime.pending_allocator_events[1][1]["bundle_cells"]
        self.assertIs(first, second)
        self.assertEqual(len(first), 50)
        self.assertIsInstance(first[0], tuple)
        runtime.choose_goal()
        self.assertEqual(runtime.pending_message_sequences, {})

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


class InboundConsensusRepairTests(unittest.TestCase):
    """Regressions for ordered peer messages drained inside W_alloc.

    CausalBoardSession stages every inbound allocator message before the
    authoritative checkpoint, then the resident runtime drains that FIFO at
    the start of ``choose_goal``.  The desktop algorithms repair their local
    retained path after *each* changed peer message.  These tests observe that
    same boundary directly, before the later allocator solve can mask an
    invalid path by repairing or refilling it.
    """

    @staticmethod
    def _runtime(algorithm: str):
        cells = ((1, 1), (2, 1), (3, 1))
        config = {
            "mission": "collaborative",
            "algorithm": algorithm,
            "robot_id": ROBOT_IDS[0],
            "robot_ids": list(ROBOT_IDS),
            "grid_size": 19,
            "all_tasks": [list(cell) for cell in cells],
            "active_tasks": [list(cell) for cell in cells],
            "commitment_horizon": 3,
            "seed": 73,
        }
        initial = {
            "robot_id": ROBOT_IDS[0],
            "robot_ids": list(ROBOT_IDS),
            "pos": [0, 0],
            "all_tasks": [list(cell) for cell in cells],
            "active_tasks": [list(cell) for cell in cells],
            "peer_positions": {
                ROBOT_IDS[1]: [0, 6],
                ROBOT_IDS[2]: [0, 12],
                ROBOT_IDS[3]: [0, 18],
            },
            "target_p": [1.0] * len(cells),
        }
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(config, initial)
        slots = [runtime.state.slot_for_cell(cell) for cell in cells]
        return runtime, cells, slots

    @staticmethod
    def _observe_after_each_inbound_message(runtime, slots, events):
        """Capture state immediately after each W_alloc message callback."""

        observed = []
        handle_message = runtime.allocator.handle_message

        def record(message):
            changed = handle_message(message)
            observed.append(
                {
                    "path": list(runtime.allocator.path),
                    "owners": [
                        int(runtime.state.claim_owner[slot])
                        for slot in slots
                    ],
                }
            )
            return changed

        runtime.allocator.handle_message = record
        try:
            runtime.begin_call_setup()
            runtime.apply_delta({"events": copy.deepcopy(events)})
            # This invokes the staged FIFO inside the same W_alloc transaction
            # used by a causal board call.  Assertions below intentionally use
            # ``observed`` rather than the post-solve state.
            runtime.choose_goal()
        finally:
            runtime.allocator.handle_message = handle_message
        return observed

    def test_acbba_releases_lost_bundle_suffix_before_next_peer_message(self) -> None:
        runtime, cells, slots = self._runtime("ACBBA")
        runtime.allocator.path = list(slots)
        for index, slot in enumerate(slots, start=1):
            runtime.state.set_claim(slot, runtime.state.robot_index, -10.0 - index, index)

        # First, peer 01 wins the head of our retained bundle.  Before the
        # next relay is processed, ACBBA must release the entire dependent
        # suffix; otherwise Table-1 evaluates the relay against stale local
        # ownership.  This is the minimal form of the smoke failure's ordered
        # peer bundle burst.
        events = [
            {
                "kind": "allocator_message",
                "payload": {
                    "type": "acbba_entry",
                    "sender": ROBOT_IDS[1],
                    "x": cells[0][0],
                    "y": cells[0][1],
                    "winner": ROBOT_IDS[1],
                    "bid": 0.0,
                    "timestamp": 10,
                    "order": 0,
                    "bundle_cells": [list(cells[0])],
                    "bundle_size": 1,
                },
            },
            {
                "kind": "allocator_message",
                "payload": {
                    "type": "acbba_entry",
                    "sender": ROBOT_IDS[1],
                    "x": cells[1][0],
                    "y": cells[1][1],
                    "winner": ROBOT_IDS[0],
                    "bid": -12.0,
                    "timestamp": 2,
                    "order": 1,
                    "bundle_cells": [list(cells[0])],
                    "bundle_size": 1,
                },
            },
        ]

        observed = self._observe_after_each_inbound_message(
            runtime, slots, events
        )

        self.assertEqual(observed[0]["path"], [])
        self.assertEqual(observed[0]["owners"], [1, -1, -1])
        self.assertEqual(observed[1]["path"], [])
        self.assertEqual(observed[1]["owners"], [1, -1, -1])

    def test_pi_repairs_lost_path_item_before_next_peer_clear(self) -> None:
        runtime, cells, slots = self._runtime("PI")
        runtime.allocator.path = list(slots)
        for index, slot in enumerate(slots, start=1):
            runtime.state.set_claim(slot, runtime.state.robot_index, float(index), index)

        # PI differs from ACBBA: a lost path item is removed while its valid
        # suffix remains.  The following clear is intentionally a second FIFO
        # event, so it detects a regression that postpones the repair until
        # the final allocator solve.
        events = [
            {
                "kind": "allocator_message",
                "payload": {
                    "type": "pi_entry",
                    "sender": ROBOT_IDS[1],
                    "x": cells[0][0],
                    "y": cells[0][1],
                    "owner": ROBOT_IDS[1],
                    "significance": 0.0,
                    "timestamp": 10,
                    "order": 0,
                    "path_cells": [list(cells[0])],
                    "path_size": 1,
                },
            },
            {
                "kind": "allocator_message",
                "payload": {
                    "type": "pi_clear_path",
                    "sender": ROBOT_IDS[1],
                    "timestamp": 11,
                    "path_cells": [],
                    "path_size": 0,
                },
            },
        ]

        observed = self._observe_after_each_inbound_message(
            runtime, slots, events
        )

        self.assertEqual(observed[0]["path"], slots[1:])
        self.assertEqual(observed[0]["owners"], [1, 0, 0])
        self.assertEqual(observed[1]["path"], slots[1:])
        self.assertEqual(observed[1]["owners"], [-1, 0, 0])


class StagedEpochTimingTests(unittest.TestCase):
    def test_admission_is_non_destructive_and_compatibility_timing_is_zero(self) -> None:
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

        runtime.choose_goal()
        retained_path = list(runtime.allocator.path)
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

        self.assertEqual(runtime.algorithm_epoch_reset_time_us(), 0)
        self.assertEqual(runtime.allocator.path, retained_path)
        self.assertEqual(len(runtime.pending_allocator_events), 2)
        runtime.choose_goal()
        self.assertEqual(runtime.pending_allocator_events, [])
        self.assertEqual(runtime.allocator.path, retained_path)


if __name__ == "__main__":
    unittest.main()
