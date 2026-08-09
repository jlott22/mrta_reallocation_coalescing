from __future__ import annotations

import base64
import copy
import tempfile
import time
import unittest
import csv
import json
from pathlib import Path
from types import SimpleNamespace

from allocator_replay.capture.codec import (
    canonical_json_bytes,
    decode_value,
    encode_value,
    read_trace,
)
from allocator_replay.capture.state import snapshot
from allocator_replay.device.build import build_device_bundle
from allocator_replay.hil.bridge import (
    AuthoritativeBridge,
    HilConditionStop,
    JsonlJournal,
    _timed_failure_result,
    make_proxy_allocator,
)
from allocator_replay.hil.persistent import (
    PERSISTENT_EVENT_BATCH_BYTES,
    event_batches,
    state_delta,
)
from allocator_replay.hil.report import rebuild_hil_reports
from allocator_replay.host.emulator import LoopbackReplayDevice
from allocator_replay.host.transport import (
    ReplayAllocatorError,
    ReplayAllocatorMemoryError,
    ReplayOutputSerializationError,
    ReplayStateSetupError,
    ReplayTimeout,
    ReplayTransportError,
    SerialReplayDevice,
    project_persistent_setup,
)


SECTIONS = (
    "robot_attrs",
    "views",
    "cfg",
    "belief",
    "allocator_attrs",
)


class PersistentProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        manifest = build_device_bundle(compile_mpy=False)
        cls.build_root = Path(str(manifest["output"]))
        trace_root = (
            Path("Results")
            / "HIL"
            / "AllocatorReplay"
            / "Traces"
            / "collaborative"
            / "collaborative_cbaa_topk_fixed_k1"
        )
        trace = next(iter(sorted(trace_root.glob("trial_*.jsonl.gz"))))
        cls.captured = next(read_trace(trace))

    def _config(self) -> dict:
        captured = self.captured
        return {
            "trial_key": "persistent-test/trial-0/generation-1",
            "condition_id": captured["condition_id"],
            "mission": captured["mission"],
            "algorithm": captured["algorithm"],
        }

    def _setup(self, context_id: str = "02") -> dict:
        captured = self.captured
        return {
            "schema": 1,
            "fixture_id": "persistent-test/call-0",
            "condition_id": captured["condition_id"],
            "mission": captured["mission"],
            "algorithm": captured["algorithm"],
            "context_id": context_id,
            "setup_mode": "restore",
            "pre_state": captured["pre_state"],
            "deleted": {},
            "events": [],
            "resume_state": {},
        }

    def test_ready_then_timed_result_is_compact_and_classified(self) -> None:
        device = LoopbackReplayDevice(
            "persistent-loopback",
            build_root=self.build_root,
        )
        try:
            device.begin_persistent_trial(self._config())
            device.prepare_persistent_call(self._setup(), "attempt-1")
            runtime = device.serial.persistent_slot.runtime
            self.assertEqual(runtime.call_index, 0)
            result = device.run_persistent_ready("attempt-1", 30.0)
            self.assertEqual(runtime.call_index, 1)
            self.assertEqual(result["status"], "completed")
            self.assertIsInstance(
                decode_value(result["goal"]),
                (list, tuple),
            )
            self.assertGreaterEqual(result["allocator_time_us"], 0)
            self.assertGreaterEqual(
                result["candidate_filter_time_us"],
                0,
            )
            self.assertEqual(
                result["allocator_exclusive_time_us"],
                max(
                    0,
                    result["allocator_time_us"]
                    - result["candidate_filter_time_us"],
                ),
            )
            self.assertTrue(result["call_class"])
            self.assertTrue(
                result["resume_state"]
                or result["post_state"]["robot_attrs"]
                or result["post_state"]["allocator_attrs"]
            )
            self.assertFalse(result["post_state"]["views"])
            self.assertFalse(result["post_state"]["cfg"])
            self.assertFalse(result["post_state"]["belief"])
            device.end_persistent_trial()
        finally:
            device.close()

    def test_large_cellmap_output_streams_without_items_copy(self) -> None:
        """Exercise the exact CBAA output shape that exhausted Pololu heap."""

        class CellIndexedMap:
            def __init__(self) -> None:
                self.grid_size = 19
                self._numeric = False
                self._values = {}

            def __setitem__(self, key, value) -> None:
                self._values[key] = value

            def __getitem__(self, key):
                return self._values[key]

            def __iter__(self):
                return iter(self._values)

            def items(self):
                raise AssertionError(
                    "streamed CellIndexedMap must not call items()"
                )

        signatures = CellIndexedMap()
        expected = {}
        for cell_id in range(361):
            cell = (cell_id % 19, cell_id // 19)
            signature = (str(cell_id % 4), float(cell_id) / 7.0)
            signatures[cell] = signature
            expected[cell] = signature

        class LargeOutputRuntime:
            snapshot_values_encoded = False

            def choose_goal(self):
                return {"goal": (1, 2)}

            def drain_messages(self):
                return []

            def snapshot_minimal(self):
                return {
                    "robot_attrs": {
                        "cbaa_last_sent_signatures": signatures,
                    },
                    "views": {},
                    "cfg": {},
                    "belief": {},
                    "allocator_attrs": {},
                }

        device = LoopbackReplayDevice(
            "persistent-large-cellmap-output",
            build_root=self.build_root,
        )
        try:
            device.begin_persistent_trial(self._config())
            device.prepare_persistent_call(
                self._setup(),
                "large-cellmap",
            )
            device.serial.persistent_slot.runtime = LargeOutputRuntime()
            result = device.run_persistent_ready(
                "large-cellmap",
                30.0,
            )
            self.assertEqual(result["status"], "completed")
            returned = decode_value(
                result["post_state"]["robot_attrs"][
                    "cbaa_last_sent_signatures"
                ]
            )
            self.assertEqual(returned, expected)
        finally:
            device.close()

    def test_restore_sends_probability_map_only_once(self) -> None:
        setup = self._setup()
        setup["pre_state"] = {
            section: dict(setup["pre_state"][section])
            for section in SECTIONS
        }
        setup["pre_state"]["belief"]["target_p"] = setup["pre_state"][
            "views"
        ]["target_p"]
        projected = project_persistent_setup(setup)
        self.assertIn("target_p", projected["pre_state"]["views"])
        self.assertNotIn("target_p", projected["pre_state"]["belief"])

    def test_large_shared_searched_set_is_bounded_and_aliased(self) -> None:
        class RecordingLoopback(LoopbackReplayDevice):
            def __init__(self, *args, **kwargs) -> None:
                self.sent_parts = []
                super().__init__(*args, **kwargs)

            def _send_part(
                self,
                section,
                name,
                kind,
                reset,
                value,
            ) -> None:
                self.sent_parts.append(
                    (
                        section,
                        name,
                        kind,
                        reset,
                        len(canonical_json_bytes(value)),
                    )
                )
                super()._send_part(
                    section,
                    name,
                    kind,
                    reset,
                    value,
                )

        setup = self._setup()
        setup["pre_state"] = {
            section: dict(setup["pre_state"][section])
            for section in SECTIONS
        }
        searched = encode_value(
            {(x, y) for y in range(19) for x in range(19)}
        )
        setup["pre_state"]["views"]["searched"] = searched
        setup["pre_state"]["views"]["local_searched"] = searched
        setup["pre_state"]["belief"]["searched"] = searched
        projected = project_persistent_setup(setup)

        self.assertIn("searched", projected["pre_state"]["views"])
        self.assertNotIn(
            "local_searched",
            projected["pre_state"]["views"],
        )
        self.assertNotIn("searched", projected["pre_state"]["belief"])
        aliases = {
            (
                item["target_section"],
                item["target_name"],
                item["source_section"],
                item["source_name"],
            )
            for item in projected["state_aliases"]
        }
        self.assertEqual(
            aliases,
            {
                ("views", "local_searched", "views", "searched"),
                ("belief", "searched", "views", "searched"),
            },
        )

        device = RecordingLoopback(
            "persistent-set-stream",
            build_root=self.build_root,
        )
        try:
            device.load_fixture(projected)
            searched_parts = [
                part
                for part in device.sent_parts
                if part[0:2] == ("views", "searched")
            ]
            self.assertGreater(len(searched_parts), 1)
            self.assertEqual(
                {part[2] for part in searched_parts},
                {"set_items"},
            )
            self.assertLessEqual(
                max(part[4] for part in searched_parts),
                768,
            )
            loaded = device.serial.fixture
            restored = loaded["pre_state"]["views"]["searched"]
            self.assertEqual(
                restored,
                {(x, y) for y in range(19) for x in range(19)},
            )

            class AliasAllocator:
                pass

            slot = device.serial.worker.PersistentRuntimeSlot(
                lambda config: device.serial.worker.ReplayPersistentRuntime(
                    lambda: AliasAllocator()
                )
            )
            slot.begin_trial({"mission": "bayesian"})
            slot.prepare(
                projected["context_id"],
                projected["setup_mode"],
                loaded["pre_state"],
                projected.get("deleted", {}),
                projected.get("events", []),
                projected.get("resume_state", {}),
                projected["state_aliases"],
            )
            initial = slot.runtime.robot
            self.assertIs(
                initial._views["searched"],
                initial._views["local_searched"],
            )
            self.assertIs(
                initial._views["searched"],
                initial.belief.searched,
            )
            changed = {section: {} for section in SECTIONS}
            changed["views"]["searched"] = {(0, 0), (1, 0)}
            slot.prepare(
                projected["context_id"],
                "delta",
                changed,
                {},
                [],
                {},
                projected["state_aliases"],
            )
            self.assertIs(
                slot.runtime.robot._views["searched"],
                slot.runtime.robot._views["local_searched"],
            )
            self.assertIs(
                slot.runtime.robot._views["searched"],
                slot.runtime.robot.belief.searched,
            )
        finally:
            device.close()

    def test_restore_releases_prior_context_before_state_upload(self) -> None:
        device = LoopbackReplayDevice(
            "persistent-context-release",
            build_root=self.build_root,
        )
        try:
            device.begin_persistent_trial(self._config())
            first = device.execute_persistent(
                self._setup("02"),
                "context-02",
                30.0,
            )
            self.assertEqual(first["status"], "completed")
            old_runtime = device.serial.persistent_slot.runtime
            self.assertEqual(device.serial.context_clear_count, 1)

            device.prepare_persistent_call(
                self._setup("03"),
                "context-03",
            )
            self.assertEqual(device.serial.context_clear_count, 2)
            self.assertIsNot(
                device.serial.persistent_slot.runtime,
                old_runtime,
            )
            second = device.run_persistent_ready("context-03", 30.0)
            self.assertEqual(second["status"], "completed")
        finally:
            try:
                device.end_persistent_trial()
            finally:
                device.close()

    def test_delta_has_explicit_set_and_delete(self) -> None:
        before = {section: {} for section in SECTIONS}
        after = {section: {} for section in SECTIONS}
        before["robot_attrs"] = {"old": 1, "same": 2}
        after["robot_attrs"] = {"new": 3, "same": 2}
        changed, deleted = state_delta(before, after)
        self.assertEqual(changed["robot_attrs"], {"new": 3})
        self.assertEqual(deleted["robot_attrs"], ["old"])

    def test_event_batches_are_ordered_and_bounded(self) -> None:
        events = [
            {
                "kind": "allocator_message",
                "payload": encode_value(
                    {"sender": index, "body": "x" * 180}
                ),
            }
            for index in range(24)
        ]
        batches = event_batches(events)
        self.assertGreater(len(batches), 1)
        self.assertTrue(all(len(batch) == 1 for batch in batches))
        self.assertEqual(
            [event for batch in batches for event in batch],
            events,
        )
        self.assertTrue(
            all(
                len(canonical_json_bytes(batch))
                <= PERSISTENT_EVENT_BATCH_BYTES
                for batch in batches
            )
        )
        with self.assertRaisesRegex(
            ValueError,
            "callback event exceeds bounded setup payload",
        ):
            event_batches(
                [{"kind": "allocator_message", "payload": "x" * 900}]
            )

    def test_allocator_memory_and_output_failures_are_distinct(self) -> None:
        class MemoryRuntime:
            def choose_goal(self):
                raise MemoryError("allocator workspace")

        class ErrorRuntime:
            def choose_goal(self):
                raise ValueError("allocator invariant")

        class BadOutputRuntime:
            def choose_goal(self):
                return {"goal": [1, 1]}

            def drain_messages(self):
                return [object()]

            def snapshot_minimal(self):
                return {}

        device = LoopbackReplayDevice(
            "persistent-failures",
            build_root=self.build_root,
        )
        try:
            device.begin_persistent_trial(self._config())
            device.prepare_persistent_call(self._setup(), "memory")
            device.serial.persistent_slot.runtime = MemoryRuntime()
            with self.assertRaises(ReplayAllocatorMemoryError) as memory:
                device.run_persistent_ready("memory", 30.0)
            self.assertIsInstance(
                memory.exception.heap_free_before,
                int,
            )
            self.assertIsInstance(
                memory.exception.heap_free_after,
                int,
            )
            self.assertGreaterEqual(
                memory.exception.elapsed_until_failure_us,
                0,
            )
            self.assertEqual(
                set(memory.exception.timed_failure_diagnostics()),
                {
                    "heap_free_before",
                    "heap_free_after",
                    "elapsed_until_failure_us",
                },
            )

            device.prepare_persistent_call(self._setup(), "allocator-error")
            device.serial.persistent_slot.runtime = ErrorRuntime()
            with self.assertRaises(ReplayAllocatorError) as allocator_error:
                device.run_persistent_ready("allocator-error", 30.0)
            self.assertGreaterEqual(
                allocator_error.exception.elapsed_until_failure_us,
                0,
            )

            # Re-establish the context after the failed allocator attempt.
            device.prepare_persistent_call(self._setup(), "bad-output")
            device.serial.persistent_slot.runtime = BadOutputRuntime()
            with self.assertRaises(ReplayOutputSerializationError) as raised:
                device.run_persistent_ready("bad-output", 30.0)
            self.assertTrue(hasattr(raised.exception, "timed_result"))

            # Five-field PFAIL frames from older replay builds remain valid.
            detail = base64.b64encode(b"legacy failure").decode("ascii")
            device.serial._queue(
                "AR1",
                "PFAIL",
                "legacy",
                "allocator_failure",
                detail,
            )
            with self.assertRaises(ReplayAllocatorError) as legacy:
                device._read_protocol(
                    deadline=time.monotonic() + 1.0,
                    expected={"PTIMED"},
                )
            self.assertIsNone(legacy.exception.heap_free_before)
            self.assertIsNone(legacy.exception.heap_free_after)
            self.assertIsNone(
                legacy.exception.elapsed_until_failure_us
            )
        finally:
            device.close()

    def test_device_worker_pfail_includes_failure_diagnostics(self) -> None:
        class MemoryRuntime:
            def choose_goal(self):
                raise MemoryError("workspace")

        device = LoopbackReplayDevice(
            "worker-pfail",
            build_root=self.build_root,
        )
        worker = device.serial.worker
        frames = []
        original_write = worker._write
        worker._write = lambda *fields: frames.append(fields)
        try:
            worker._run_persistent(
                SimpleNamespace(runtime=MemoryRuntime()),
                "memory",
            )
        finally:
            worker._write = original_write
            device.close()

        self.assertTrue(frames)
        failure = frames[-1]
        self.assertEqual(failure[0:4], (
            "AR1",
            "PFAIL",
            "memory",
            "allocator_memory_failure",
        ))
        self.assertEqual(len(failure), 8)
        self.assertIsInstance(failure[5], int)
        self.assertIsInstance(failure[6], int)
        self.assertGreaterEqual(failure[7], 0)

    def test_historical_states_restore_and_delta_for_all_native_engines(
        self,
    ) -> None:
        """Exercise real captured simulator state through every v2 runtime."""

        for mission in ("bayesian", "collaborative"):
            for algorithm in ("cbaa", "acbba", "pi", "hipc", "dmchba", "dga"):
                with self.subTest(mission=mission, algorithm=algorithm):
                    suffix = (
                        "topk_005_k18"
                        if mission == "bayesian"
                        else "topk_fixed_k1"
                    )
                    trace_root = (
                        Path("Results")
                        / "HIL"
                        / "AllocatorReplay"
                        / "Traces"
                        / mission
                        / f"{mission}_{algorithm}_{suffix}"
                    )
                    trace = next(
                        iter(sorted(trace_root.glob("trial_*.jsonl.gz")))
                    )
                    fixtures = read_trace(trace)
                    if mission == "bayesian":
                        captured = next(
                            fixture
                            for fixture in fixtures
                            if decode_value(
                                fixture["pre_state"]["views"].get(
                                    "known_clues",
                                    [],
                                )
                            )
                        )
                    else:
                        captured = next(fixtures)
                    device = LoopbackReplayDevice(
                        f"historical-{mission}-{algorithm}",
                        build_root=self.build_root,
                    )
                    config = {
                        "trial_key": f"historical/{mission}/{algorithm}",
                        "condition_id": captured["condition_id"],
                        "mission": mission,
                        "algorithm": captured["algorithm"],
                        "top_k_level": captured.get("top_k_level") or "5%",
                        "top_k_rate": captured["top_k_rate"],
                        "top_k_cells": captured["top_k_cells"],
                    }
                    base = {
                        "schema": 1,
                        "condition_id": captured["condition_id"],
                        "mission": mission,
                        "algorithm": captured["algorithm"],
                        "context_id": str(captured["robot_id"]),
                        "deleted": {},
                        "events": [],
                    }
                    try:
                        device.begin_persistent_trial(config)
                        first = device.execute_persistent(
                            {
                                **base,
                                "fixture_id": (
                                    f"historical-{mission}-{algorithm}-restore"
                                ),
                                "setup_mode": "restore",
                                "pre_state": captured["pre_state"],
                                "resume_state": {},
                            },
                            "restore",
                            30.0,
                        )
                        empty_delta = {
                            section: {} for section in SECTIONS
                        }
                        second = device.execute_persistent(
                            {
                                **base,
                                "fixture_id": (
                                    f"historical-{mission}-{algorithm}-delta"
                                ),
                                "setup_mode": "delta",
                                "pre_state": empty_delta,
                                "resume_state": {},
                            },
                            "delta",
                            30.0,
                        )
                        self.assertEqual(first["status"], "completed")
                        self.assertEqual(second["status"], "completed")
                        self.assertTrue(first["call_class"])
                        self.assertTrue(second["call_class"])
                        if (
                            mission == "bayesian"
                            and algorithm in ("cbaa", "acbba", "pi", "hipc")
                        ):
                            transient = {
                                "_candidate_scan_ids",
                                "_candidate_ranked_ids",
                                "_candidate_probabilities",
                                "_candidate_distances",
                                "_active_candidate_cache",
                            }
                            self.assertFalse(
                                transient.intersection(
                                    first["post_state"]["allocator_attrs"]
                                )
                            )
                            self.assertFalse(
                                transient.intersection(
                                    second["post_state"]["allocator_attrs"]
                                )
                            )
                        if mission == "bayesian" and algorithm == "dga":
                            returned = first["post_state"]
                            robot_attrs = returned["robot_attrs"]
                            self.assertNotIn(
                                "dga_population",
                                robot_attrs,
                            )
                            population_markers = sorted(
                                name
                                for name in robot_attrs
                                if name.startswith(
                                    "dga_replay_population_"
                                )
                                and name.endswith("_packed")
                            )
                            self.assertEqual(len(population_markers), 30)
                            self.assertEqual(
                                robot_attrs[
                                    "dga_replay_population_count"
                                ],
                                30,
                            )
                            rng_chunks = sorted(
                                name
                                for name in robot_attrs
                                if name.startswith(
                                    "dga_rng_replay_rng_"
                                )
                                and name.rsplit("_", 1)[-1].isdigit()
                            )
                            self.assertGreater(len(rng_chunks), 1)
                            self.assertEqual(
                                robot_attrs[
                                    "dga_rng_replay_rng_state_length"
                                ],
                                624,
                            )
                            bounded_fields = population_markers + rng_chunks
                            # Large native state is split before USB framing;
                            # no logical field requires a multi-kilobyte heap
                            # allocation on the controller.
                            self.assertLess(
                                max(
                                    len(
                                        json.dumps(
                                            robot_attrs[name],
                                            separators=(",", ":"),
                                        )
                                    )
                                    for name in bounded_fields
                                ),
                                1_024,
                            )

                            restored = copy.deepcopy(
                                captured["pre_state"]
                            )
                            for name in list(restored["robot_attrs"]):
                                if (
                                    name.startswith("dga_")
                                    and name not in robot_attrs
                                ):
                                    del restored["robot_attrs"][name]
                            restored["robot_attrs"].update(robot_attrs)
                            restored["allocator_attrs"] = dict(
                                returned["allocator_attrs"]
                            )
                            device.end_persistent_trial()
                            device.begin_persistent_trial(config)
                            after_restore = device.execute_persistent(
                                {
                                    **base,
                                    "fixture_id": (
                                        "historical-bayesian-dga-"
                                        "context-restore"
                                    ),
                                    "setup_mode": "restore",
                                    "pre_state": restored,
                                    "resume_state": {},
                                },
                                "context-restore",
                                30.0,
                            )
                            self.assertEqual(
                                decode_value(second["goal"]),
                                decode_value(after_restore["goal"]),
                            )
                            self.assertEqual(
                                second["post_state"]["robot_attrs"],
                                after_restore["post_state"]["robot_attrs"],
                            )
                    finally:
                        try:
                            device.end_persistent_trial()
                        finally:
                            device.close()


class PersistentProxyTests(unittest.TestCase):
    def test_callbacks_are_queued_and_outbound_is_device_only(self) -> None:
        class DesktopAllocator:
            def receive_message(self, robot, message):
                raise AssertionError("desktop allocator callback executed")

            def on_observation(self, robot, observation):
                raise AssertionError("desktop allocator callback executed")

            def make_messages(self, robot):
                raise AssertionError("desktop generated outbound message")

        class Decision:
            def __init__(self, goal, debug=None):
                self.goal = goal
                self.debug = debug

        class Bridge:
            def __init__(self):
                self.events = []
                self.messages = [{"type": "native"}]

            def call(self, allocator, robot):
                return (2, 3)

            def queue_event(
                self,
                robot_id,
                kind,
                payload=None,
                *,
                receiver="",
            ):
                self.events.append((robot_id, kind, payload, receiver))

            def take_messages(self, robot_id):
                messages, self.messages = self.messages, None
                return messages

        bridge = Bridge()
        proxy_class = make_proxy_allocator(
            DesktopAllocator,
            bridge,
            Decision,
        )
        allocator = proxy_class()
        robot = SimpleNamespace(rid="00")
        allocator.receive_message(
            robot,
            {"type": "cbaa_entry", "x": 1, "y": 2},
        )
        allocator.on_observation(
            robot,
            SimpleNamespace(cell=(1, 2), searched=True),
        )
        self.assertEqual(len(bridge.events), 2)
        self.assertEqual(
            allocator.make_messages(robot),
            [{"type": "native"}],
        )
        self.assertEqual(allocator.make_messages(robot), [])
        self.assertEqual(allocator.choose_goal(robot).goal, (2, 3))


class _RestartSerial:
    """Small raw-REPL model that leaves a worker alive after Ctrl-C."""

    def __init__(self, *, busy: bool = True) -> None:
        self.is_open = True
        self.busy = busy
        self.state = "worker"
        self.buffer = bytearray()
        self.events: list[str] = []

    def _queue(self, payload: bytes) -> None:
        self.buffer.extend(payload)

    def reset_input_buffer(self) -> None:
        self.buffer.clear()

    def write(self, payload: bytes) -> int:
        if b"AR1|EXIT\n" in payload:
            self.events.append("exit")
            if self.state == "worker" and not self.busy:
                self._queue(b"AR1|BYE\n")
                self.state = "raw"
        if b"\x03" in payload:
            self.events.append("ctrl_c")
            if self.state == "worker":
                self.busy = False
                # The deployed worker acknowledges but remains in its loop.
                self._queue(b"AR1|INTERRUPTED|100000\n")
            else:
                self._queue(b">")
        if b"\x01" in payload:
            self.events.append("ctrl_a")
            self.state = "raw"
            self._queue(b"raw REPL; CTRL-B to exit\r\n>")
        if payload == b"\x04":
            self.events.append("soft_reset")
            self._queue(
                b"OK\r\nMPY: soft reboot\r\n"
                b"raw REPL; CTRL-B to exit\r\n>"
            )
        if b"import replay_worker; replay_worker.main()\x04" in payload:
            self.events.append("start_worker")
            self.state = "worker"
            self.busy = False
            self._queue(
                b"OK"
                b"AR1|READY|device-a|build-a|persistent|125000000|"
                b"100000|firmware-a\n"
            )
        return len(payload)

    def flush(self) -> None:
        return None

    def read(self, size: int = 1) -> bytes:
        if not self.buffer:
            return b""
        size = min(size, len(self.buffer))
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def readline(self) -> bytes:
        if not self.buffer:
            return b""
        try:
            end = self.buffer.index(10) + 1
        except ValueError:
            end = len(self.buffer)
        result = bytes(self.buffer[:end])
        del self.buffer[:end]
        return result

    def close(self) -> None:
        self.is_open = False


class _ScriptedPersistentDevice:
    def __init__(
        self,
        outcomes: list[str],
        *,
        recovery_fails: bool = False,
    ) -> None:
        self.identity = SimpleNamespace(
            device_id="device-a",
            port="COM-test",
            build_id="build-a",
            frequency_hz=125_000_000,
        )
        self.outcomes = list(outcomes)
        self.recovery_fails = recovery_fails
        self.run_attempts: list[str] = []
        self.interrupt_count = 0
        self.restart_count = 0
        self.begin_count = 0

    def begin_persistent_trial(self, config) -> None:
        del config
        self.begin_count += 1

    def prepare_persistent_call(self, setup, attempt_id) -> None:
        del setup, attempt_id
        if self.outcomes and self.outcomes[0] == "setup":
            self.outcomes.pop(0)
            raise ReplayStateSetupError("scripted setup failure")

    def run_persistent_ready(self, attempt_id, timeout_seconds):
        del timeout_seconds
        self.run_attempts.append(attempt_id)
        outcome = self.outcomes.pop(0)
        if outcome == "timeout":
            raise ReplayTimeout(
                "scripted timeout",
                heap_free_at_ready=98_765,
                host_elapsed_us=30_000_123,
                timeout_seconds=30.0,
            )
        if outcome == "memory":
            raise ReplayAllocatorMemoryError(
                "scripted allocator memory failure",
                heap_free_before=98_000,
                heap_free_after=1_000,
                elapsed_until_failure_us=123_456,
            )
        return {
            "attempt_id": attempt_id,
            "status": "completed",
            "allocator_time_us": 1234,
            "candidate_filter_time_us": 34,
            "allocator_exclusive_time_us": 1200,
            "candidate_filter_calls": 1,
            "candidate_count_before": 4,
            "candidate_count_after": 1,
            "heap_free_before": 98_000,
            "heap_free_after": 97_000,
            "call_class": "candidate_filter_only",
            "goal": encode_value((1, 1)),
            "messages": encode_value([]),
            "post_state": {
                "robot_attrs": {},
                "views": {},
                "cfg": {},
                "belief": {},
                "allocator_attrs": {},
            },
            "resume_state": {},
        }

    def interrupt(self) -> None:
        self.interrupt_count += 1
        if self.recovery_fails:
            raise ReplayTransportError("scripted recovery disconnect")

    def restart_clean_worker(self):
        self.restart_count += 1
        return self.identity

    def end_persistent_trial(self) -> None:
        return None


def _script_bridge(path: Path, device: _ScriptedPersistentDevice):
    bridge = AuthoritativeBridge(
        device=device,
        condition=SimpleNamespace(
            condition_id="bayesian_cbaa_topk_fixed_k1",
            mission="bayesian",
            algorithm="CBAA",
            top_k_level="K=1",
            top_k_rate=1.0 / 361.0,
            top_k_cells=1,
        ),
        trial_id=7,
        run_generation=1,
        journal=JsonlJournal(path),
    )
    robot = SimpleNamespace(
        rid="00",
        pos=(0, 0),
        heading=0,
        grid_size=3,
        current_goal=None,
        last_goal=None,
        last_event=None,
        collision_avoidance_active=False,
        collision_state=None,
        _active_peer_positions={},
        cfg=SimpleNamespace(),
        belief=SimpleNamespace(),
    )
    return bridge, robot, SimpleNamespace()


class PersistentRecoveryTests(unittest.TestCase):
    def test_context_cache_canonicalizes_streamed_cellmap_output(self):
        """An unchanged 361-cell map is not resent on a same-context call."""

        entries = []
        for cell_id in range(361):
            cell = (cell_id % 19, cell_id // 19)
            signature = (str(cell_id % 4), float(cell_id) / 7.0)
            entries.append(
                [
                    encode_value(cell),
                    encode_value(signature),
                ]
            )
        # This is the device stream's natural y-major CellIndexedMap order,
        # not capture.codec.encode_value's canonical host representation.
        streamed_cellmap = {
            "@": "cellmap",
            "grid_size": 19,
            "numeric": False,
            "v": {"@": "dict", "v": entries},
        }
        self.assertNotEqual(
            streamed_cellmap,
            encode_value(decode_value(streamed_cellmap)),
        )

        class RecordingMapDevice(_ScriptedPersistentDevice):
            def __init__(self):
                super().__init__(["success", "success"])
                self.setups = []

            def prepare_persistent_call(self, setup, attempt_id) -> None:
                self.setups.append(copy.deepcopy(setup))
                super().prepare_persistent_call(setup, attempt_id)

            def run_persistent_ready(self, attempt_id, timeout_seconds):
                result = super().run_persistent_ready(
                    attempt_id,
                    timeout_seconds,
                )
                result["post_state"]["robot_attrs"][
                    "cbaa_last_sent_signatures"
                ] = copy.deepcopy(streamed_cellmap)
                return result

        with tempfile.TemporaryDirectory() as directory:
            device = RecordingMapDevice()
            bridge, robot, allocator = _script_bridge(
                Path(directory) / "device.jsonl",
                device,
            )
            self.assertEqual(
                bridge._call_persistent(allocator, robot),
                (1, 1),
            )
            canonical_after_first = snapshot(robot, allocator)
            self.assertEqual(
                bridge._context_states[str(robot.rid)],
                canonical_after_first,
            )
            self.assertEqual(
                bridge._call_persistent(allocator, robot),
                (1, 1),
            )

        self.assertEqual(len(device.setups), 2)
        self.assertEqual(device.setups[0]["setup_mode"], "restore")
        self.assertEqual(device.setups[1]["setup_mode"], "delta")
        self.assertNotIn(
            "cbaa_last_sent_signatures",
            device.setups[1]["pre_state"]["robot_attrs"],
        )

    def test_bridge_stages_large_event_queue_after_state_restore(self):
        class RecordingDevice(_ScriptedPersistentDevice):
            def __init__(self):
                super().__init__(["success"])
                self.setups = []
                self.attempt_ids = []

            def prepare_persistent_call(self, setup, attempt_id) -> None:
                self.setups.append(copy.deepcopy(setup))
                self.attempt_ids.append(attempt_id)
                super().prepare_persistent_call(setup, attempt_id)

        with tempfile.TemporaryDirectory() as directory:
            device = RecordingDevice()
            bridge, robot, allocator = _script_bridge(
                Path(directory) / "device.jsonl",
                device,
            )
            expected = []
            for index in range(64):
                payload = {"sender": index, "body": "x" * 180}
                bridge.queue_event(
                    str(robot.rid),
                    "allocator_message",
                    payload,
                )
                expected.append(
                    {
                        "kind": "allocator_message",
                        "payload": encode_value(payload),
                    }
                )
            self.assertEqual(
                bridge._call_persistent(allocator, robot),
                (1, 1),
            )

        self.assertGreater(len(device.setups), 2)
        self.assertEqual(device.setups[0]["setup_mode"], "restore")
        self.assertEqual(device.setups[0]["events"], [])
        event_setups = device.setups[1:]
        self.assertEqual(len(event_setups), len(expected))
        self.assertTrue(
            all(setup["setup_mode"] == "delta" for setup in event_setups)
        )
        self.assertTrue(
            all(len(setup["events"]) == 1 for setup in event_setups)
        )
        self.assertTrue(
            all(
                all(not values for values in setup["pre_state"].values())
                for setup in event_setups
            )
        )
        self.assertEqual(
            [
                event
                for setup in event_setups
                for event in setup["events"]
            ],
            expected,
        )
        self.assertTrue(
            all(
                len(canonical_json_bytes(setup["events"]))
                <= PERSISTENT_EVENT_BATCH_BYTES
                for setup in event_setups
            )
        )
        combined = dict(device.setups[0])
        combined["events"] = expected

        def header_size(setup):
            projected = project_persistent_setup(setup)
            header = {
                key: value
                for key, value in projected.items()
                if key != "pre_state"
            }
            header["pre_state"] = {
                section: {} for section in SECTIONS
            }
            return len(canonical_json_bytes(header))

        # This recreates the failure shape from the hardware campaign: the
        # old monolithic restore header was tens of kilobytes even though each
        # individual radio message was small.
        self.assertGreater(header_size(combined), 15_000)
        self.assertLess(
            max(header_size(setup) for setup in device.setups),
            2_048,
        )
        self.assertEqual(len(set(device.attempt_ids)), 1)
        self.assertEqual(len(device.run_attempts), 1)

    def test_transport_timeout_retains_ready_heap_and_host_elapsed(self):
        serial = _RestartSerial(busy=False)
        device = SerialReplayDevice(
            "COM-test",
            start_worker=False,
            serial_object=serial,
        )
        device._persistent_ready_heap["attempt"] = 87_654
        with self.assertRaises(ReplayTimeout) as raised:
            device.run_persistent_ready("attempt", 0.001)
        self.assertEqual(raised.exception.heap_free_at_ready, 87_654)
        self.assertGreaterEqual(raised.exception.host_elapsed_us, 1_000)
        self.assertEqual(raised.exception.timeout_seconds, 0.001)

    def test_restart_quiesces_ctrl_c_catching_worker_and_next_condition(self):
        serial = _RestartSerial(busy=True)
        device = SerialReplayDevice(
            "COM-test",
            start_worker=False,
            serial_object=serial,
        )
        first = device.restart_clean_worker()
        self.assertEqual(first.device_id, "device-a")
        self.assertIn("ctrl_c", serial.events)
        self.assertGreaterEqual(serial.events.count("exit"), 2)

        event_count = len(serial.events)
        second = device.restart_clean_worker()
        second_events = serial.events[event_count:]
        self.assertEqual(second.device_id, "device-a")
        # An idle worker exits immediately at an ordinary condition boundary.
        self.assertEqual(second_events[0], "exit")
        self.assertNotIn("ctrl_c", second_events[:1])
        self.assertIn("soft_reset", second_events)

    def test_timeout_confirmation_reaches_r3_and_two_of_three_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.jsonl"
            device = _ScriptedPersistentDevice(
                ["timeout", "success", "timeout"]
            )
            bridge, robot, allocator = _script_bridge(path, device)
            with self.assertRaises(HilConditionStop) as raised:
                bridge._call_persistent(allocator, robot)
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(raised.exception.reason, "timing_unusable_30s")
        self.assertEqual(len(device.run_attempts), 3)
        self.assertTrue(device.run_attempts[1].endswith(":r2"))
        self.assertTrue(device.run_attempts[2].endswith(":r3"))
        attempts = [
            row for row in rows if row["record_type"] == "call_attempt"
        ]
        self.assertEqual(
            [row["repetition_id"] for row in attempts],
            [1, 3, 2],
        )
        self.assertFalse(any(row["accepted"] for row in attempts))
        timeouts = [row for row in attempts if row["outcome"] == "timeout"]
        self.assertEqual(len(timeouts), 2)
        self.assertEqual(timeouts[0]["heap_free_at_ready"], 98_765)
        self.assertEqual(timeouts[0]["host_elapsed_us"], 30_000_123)
        recoveries = [
            row
            for row in rows
            if row["record_type"] == "call_phase"
            and row["phase"] == "recovery"
        ]
        self.assertEqual(len(recoveries), 3)
        self.assertTrue(
            all(row["phase_status"] == "completed" for row in recoveries)
        )

    def test_one_timeout_two_successes_accepts_one_timing_sample(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.jsonl"
            device = _ScriptedPersistentDevice(
                ["timeout", "success", "success"]
            )
            bridge, robot, allocator = _script_bridge(path, device)
            goal = bridge._call_persistent(allocator, robot)
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(goal, (1, 1))
        self.assertEqual(len(device.run_attempts), 3)
        attempts = [
            row for row in rows if row["record_type"] == "call_attempt"
        ]
        self.assertEqual(sum(bool(row["accepted"]) for row in attempts), 1)

    def test_recovery_failure_remains_transport_not_condition_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.jsonl"
            device = _ScriptedPersistentDevice(
                ["timeout"],
                recovery_fails=True,
            )
            bridge, robot, allocator = _script_bridge(path, device)
            with self.assertRaises(ReplayTransportError):
                bridge._call_persistent(allocator, robot)
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(len(device.run_attempts), 1)
        recovery = next(
            row
            for row in rows
            if row["record_type"] == "call_phase"
            and row["phase"] == "recovery"
        )
        self.assertEqual(recovery["phase_status"], "failed")
        self.assertEqual(recovery["outcome"], "transport_error")

    def test_idle_setup_and_memory_failures_restart_without_interrupt(self):
        for first_failure in ("setup", "memory"):
            with self.subTest(first_failure=first_failure):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "device.jsonl"
                    device = _ScriptedPersistentDevice(
                        [first_failure, "success", "success"]
                    )
                    bridge, robot, allocator = _script_bridge(path, device)
                    self.assertEqual(
                        bridge._call_persistent(allocator, robot),
                        (1, 1),
                    )
                self.assertEqual(device.interrupt_count, 0)
                self.assertGreaterEqual(device.restart_count, 2)


class PersistentReportTests(unittest.TestCase):
    def test_bridge_journals_timed_failure_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device.jsonl"
            identity = SimpleNamespace(
                device_id="device-a",
                port="COM-test",
                build_id="build",
                frequency_hz=125_000_000,
            )
            bridge = AuthoritativeBridge(
                device=SimpleNamespace(identity=identity),
                condition=SimpleNamespace(
                    condition_id="condition",
                    mission="bayesian",
                    algorithm="CBAA",
                    top_k_level="K=1",
                    top_k_rate=0.001,
                    top_k_cells=1,
                ),
                trial_id=7,
                run_generation=2,
                journal=JsonlJournal(path),
            )
            fixture = {
                "robot_id": "00",
                "call_index": 3,
                "fixture_id": "condition/trial_007/robot_00/call_00003",
                "fixture_sha256": "fixture",
            }
            exc = ReplayAllocatorMemoryError(
                "memory",
                heap_free_before=12_000,
                heap_free_after=2_000,
                elapsed_until_failure_us=345_678,
            )
            diagnostics = _timed_failure_result(exc)
            bridge._record_phase(
                fixture,
                1,
                "timing",
                "failed",
                "allocator_memory_failure",
                result=diagnostics,
                error=str(exc),
            )
            bridge._record(
                fixture=fixture,
                repetition=1,
                outcome="allocator_memory_failure",
                result=diagnostics,
                error=str(exc),
            )
            rows = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(
            [row["record_type"] for row in rows],
            ["call_phase", "call_attempt"],
        )
        for row in rows:
            self.assertEqual(row["heap_free_before"], 12_000)
            self.assertEqual(row["heap_free_after"], 2_000)
            self.assertEqual(
                row["elapsed_until_failure_us"],
                345_678,
            )

    def test_report_retains_setup_timing_and_output_phases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "journals").mkdir()
            schedule = {
                "campaign_id": "phase-test",
                "jobs": [
                    {
                        "condition_id": "c",
                        "mission": "collaborative",
                        "algorithm": "CBAA",
                        "top_k_level": "K=1",
                        "top_k_rate": 0.02,
                        "top_k_cells": 1,
                        "status": "pending",
                        "stopped_reason": "",
                        "trial_ids": [0],
                        "completed_trials": [],
                    }
                ],
            }
            (root / "schedule.json").write_text(
                json.dumps(schedule),
                encoding="utf-8",
            )
            phases = [
                {
                    "record_type": "call_phase",
                    "condition_id": "c",
                    "trial_id": 0,
                    "run_generation": 1,
                    "fixture_id": "f",
                    "phase": phase,
                    "phase_status": "completed",
                    "outcome": "completed",
                }
                for phase in ("setup", "timing", "output")
            ]
            (root / "journals" / "device.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in phases),
                encoding="utf-8",
            )
            result = rebuild_hil_reports(root)
            with (root / "reports" / "raw_call_phases.csv").open(
                encoding="utf-8",
            ) as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(result["raw_phases"], 3)
        self.assertEqual(
            [row["phase"] for row in rows],
            ["setup", "timing", "output"],
        )


if __name__ == "__main__":
    unittest.main()
