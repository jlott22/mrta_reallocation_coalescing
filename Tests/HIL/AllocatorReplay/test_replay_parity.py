from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

from allocator_replay.capture.codec import read_trace
from allocator_replay.config.study import (
    DEVICE_BUILD_ROOT,
    conditions,
    trace_condition_root,
)
from allocator_replay.device.build import build_device_bundle
from allocator_replay.host.emulator import LoopbackReplayDevice


class ReplayParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        manifest = build_device_bundle()
        cls.build_root = Path(str(manifest["output"]))
        sys.path.insert(0, str(cls.build_root))
        importlib.invalidate_caches()
        cls.worker = importlib.import_module("replay_worker")

    def _fixtures(self, condition):
        traces = sorted(trace_condition_root(condition).glob("trial_*.jsonl.gz"))
        self.assertTrue(traces, condition.condition_id)
        fixtures = list(read_trace(traces[0]))
        indexes = sorted({0, len(fixtures) // 2, len(fixtures) - 1})
        return [fixtures[index] for index in indexes]

    def test_representative_calls_match_all_twelve_ports(self) -> None:
        for condition in conditions():
            if condition.top_k_rate != 0.05:
                continue
            for fixture in self._fixtures(condition):
                self.assertIn("post_state", fixture["expected"])
                result = self.worker._run_fixture(fixture, "parity-test")
                self.assertEqual(
                    result["status"],
                    "completed",
                    (fixture["fixture_id"], result),
                )
                self.assertTrue(result["goal_match"])
                self.assertTrue(result["state_match"])
                self.assertTrue(result["messages_match"])
                self.assertGreaterEqual(result["allocator_time_us"], 0)
                self.assertGreaterEqual(result["candidate_filter_time_us"], 0)
                self.assertEqual(
                    result["allocator_exclusive_time_us"],
                    max(
                        0,
                        result["allocator_time_us"]
                        - result["candidate_filter_time_us"],
                    ),
                )

    def test_ascii_chunk_protocol_round_trip(self) -> None:
        condition = next(
            item
            for item in conditions("collaborative")
            if item.algorithm == "DGA" and item.top_k_rate == 0.05
        )
        fixture = self._fixtures(condition)[1]
        device = LoopbackReplayDevice("loopback-protocol", build_root=self.build_root)
        try:
            result = device.execute(fixture, "chunked-attempt", 30.0)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["device_id"], "loopback-protocol")
        finally:
            device.exit()
            device.close()
