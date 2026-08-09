from __future__ import annotations

import unittest

from allocator_replay.device.native.bayesian import (
    create_persistent_runtime as create_bayesian_runtime,
)
from allocator_replay.device.physical import PhysicalAllocatorAdapter


class _Counters:
    def __init__(self) -> None:
        self.candidate_filter_time_us_samples = []


class _CompleteRuntime:
    def __init__(self, config) -> None:
        self.config = config
        self.reset_calls = 0
        self.deltas = []
        self.counters = _Counters()
        self.messages = []
        self.choice_count = 0

    def reset_trial(self, config, initial_state):
        self.reset_calls += 1
        self.state = initial_state
        return {"complete_adapter": True, "motor_free": True}

    def apply_delta(self, delta):
        self.deltas.append(delta)

    def choose_goal(self):
        self.choice_count += 1
        self.counters.candidate_filter_time_us_samples.extend((7, 11))
        self.messages.append({"type": "claim", "cell": (2, 1)})
        return {"goal": (2, 1), "source": "complete"}

    def drain_messages(self):
        messages = self.messages
        self.messages = []
        return messages

    def snapshot_minimal(self):
        return {"choice_count": self.choice_count}

    def timing_counters(self):
        return self.counters

    def candidate_counts(self):
        return 20, 5

    def call_class(self):
        return "full_allocation_solve"


class _Clock:
    def __init__(self, values):
        self.values = iter(values)

    def ticks_us(self):
        return next(self.values)

    @staticmethod
    def ticks_diff(new, old):
        return new - old


class PhysicalAllocatorAdapterTests(unittest.TestCase):
    def _adapter(self, created, clock=None):
        def factory(config):
            runtime = _CompleteRuntime(config)
            created.append(runtime)
            return runtime

        clock = clock or _Clock((100, 160))
        return PhysicalAllocatorAdapter(
            factory,
            ticks_us=clock.ticks_us,
            ticks_diff=clock.ticks_diff,
            heap_free=lambda: 4096,
        )

    def test_one_runtime_remains_resident_for_trial(self) -> None:
        created = []
        adapter = self._adapter(created, _Clock((100, 160, 200, 275)))
        adapter.reset_trial(
            {"algorithm": "CBAA"}, {"rid": "0", "pos": (0, 0)}
        )
        adapter.apply_delta({"set": {"robot_attrs": {"pos": (1, 0)}}})
        first = adapter.choose_goal()
        second = adapter.choose_goal()

        self.assertEqual(len(created), 1)
        self.assertIs(adapter.runtime, created[0])
        self.assertEqual(created[0].reset_calls, 1)
        self.assertEqual(created[0].choice_count, 2)
        self.assertEqual(first["goal"], second["goal"])

    def test_times_only_choose_and_reports_nested_filter_time(self) -> None:
        created = []
        adapter = self._adapter(created)
        adapter.reset_trial(
            {"algorithm": "CBAA"}, {"rid": "0", "pos": (0, 0)}
        )
        result = adapter.allocate()

        self.assertEqual(result["goal"], (2, 1))
        self.assertEqual(
            result["messages"], [{"type": "claim", "cell": (2, 1)}]
        )
        metrics = result["metrics"]
        self.assertEqual(metrics["allocator_time_us"], 60)
        self.assertEqual(metrics["candidate_filter_time_us"], 18)
        self.assertEqual(metrics["allocator_exclusive_time_us"], 42)
        self.assertEqual(metrics["candidate_filter_calls"], 2)
        self.assertEqual(metrics["candidate_count_before"], 20)
        self.assertEqual(metrics["candidate_count_after"], 5)
        self.assertEqual(metrics["call_path"], "full_allocation_solve")
        self.assertEqual(metrics["timing_source"], "physical_adapter")

    def test_physical_messages_become_replay_allocator_events(self) -> None:
        created = []
        adapter = self._adapter(created)
        adapter.reset_trial(
            {"algorithm": "CBAA"}, {"rid": "0", "pos": (0, 0)}
        )
        adapter.apply_physical_update(
            delta={"set": {"robot_attrs": {"pos": (1, 1)}}},
            events=[{"kind": "on_collision_avoidance"}],
            messages=[
                {"type": "cbaa_entry", "sender": "1"},
                {
                    "receiver": "handle_cbaa_message",
                    "payload": {"type": "cbaa_entry", "sender": "2"},
                },
            ],
        )
        update = created[0].deltas[0]
        self.assertEqual(update["set"]["robot_attrs"]["pos"], (1, 1))
        self.assertEqual(update["events"][0]["kind"], "on_collision_avoidance")
        self.assertEqual(update["events"][1]["kind"], "allocator_message")
        self.assertEqual(
            update["events"][1]["payload"]["sender"], "1"
        )
        self.assertEqual(
            update["events"][2]["receiver"], "handle_cbaa_message"
        )

    def test_prefers_timing_from_runtime_that_times_internally(self) -> None:
        class InternallyTimed(_CompleteRuntime):
            def choose_goal(self):
                return {
                    "goal": (1, 1),
                    "allocator_time_us": 51,
                    "candidate_filter_time_us": 9,
                    "candidate_filter_calls": 1,
                }

            def timing_counters(self):
                return None

        runtime = InternallyTimed({"algorithm": "DMCHBA"})
        clock = _Clock((10, 80))
        adapter = PhysicalAllocatorAdapter(
            lambda config: runtime,
            ticks_us=clock.ticks_us,
            ticks_diff=clock.ticks_diff,
            heap_free=lambda: None,
        )
        adapter.reset_trial({"algorithm": "DMCHBA"}, {})
        adapter.choose_goal()
        metrics = adapter.timing_metrics()
        self.assertEqual(metrics["allocator_time_us"], 51)
        self.assertEqual(metrics["candidate_filter_time_us"], 9)
        self.assertEqual(metrics["allocator_exclusive_time_us"], 42)
        self.assertEqual(metrics["wrapper_elapsed_us"], 70)
        self.assertEqual(metrics["timing_source"], "native_runtime")

    def test_rejects_diagnostic_or_explicitly_incomplete_consensus_core(self):
        created = []
        adapter = self._adapter(created)
        with self.assertRaisesRegex(ValueError, "diagnostic"):
            adapter.reset_trial(
                {
                    "algorithm": "CBAA",
                    "allow_diagnostic_local_core": True,
                },
                {},
            )

        class Incomplete(_CompleteRuntime):
            def reset_trial(self, config, initial_state):
                return {"complete_adapter": False}

        adapter = PhysicalAllocatorAdapter(
            lambda config: Incomplete(config)
        )
        with self.assertRaisesRegex(ValueError, "complete consensus"):
            adapter.reset_trial({"algorithm": "PI"}, {})

    def test_uses_actual_complete_bayesian_runtime_facade_unchanged(self):
        class CompleteConsensusAdapter:
            def reset_trial(self, config, state):
                self.state = state
                self.messages = [{"type": "cbaa_entry", "sender": "0"}]
                return {"port": "complete_test"}

            def apply_delta(self, delta):
                self.delta = delta

            def choose_goal(self):
                return {"goal": (3, 2), "source": "complete_consensus"}

            def drain_messages(self):
                messages = self.messages
                self.messages = []
                return messages

            def snapshot_minimal(self):
                return {"consensus": "resident"}

        consensus = CompleteConsensusAdapter()
        clock = _Clock((200, 240))
        adapter = PhysicalAllocatorAdapter(
            create_bayesian_runtime,
            ticks_us=clock.ticks_us,
            ticks_diff=clock.ticks_diff,
            heap_free=lambda: None,
        )
        metadata = adapter.reset_trial(
            {
                "algorithm": "CBAA",
                "grid_size": 4,
                "allocator_adapter": consensus,
            },
            {
                "rid": "0",
                "pos": (0, 0),
                "known_clues": [(1, 1)],
                "target_p": {(3, 2): 0.9},
            },
        )
        adapter.apply_delta({"pos": (1, 0)})
        result = adapter.allocate()

        self.assertTrue(metadata["complete_adapter"])
        self.assertEqual(result["goal"], (3, 2))
        self.assertEqual(result["decision"]["source"], "complete_consensus")
        self.assertEqual(
            result["messages"],
            [{"type": "cbaa_entry", "sender": "0"}],
        )
        self.assertEqual(consensus.delta, {"pos": (1, 0)})
        self.assertEqual(
            adapter.snapshot_minimal(), {"consensus": "resident"}
        )

    def test_failure_metrics_are_retained_and_error_is_not_swallowed(self):
        class Failing(_CompleteRuntime):
            def choose_goal(self):
                raise MemoryError("allocator heap exhausted")

        runtime = Failing({"algorithm": "DGA"})
        clock = _Clock((500, 550))
        adapter = PhysicalAllocatorAdapter(
            lambda config: runtime,
            ticks_us=clock.ticks_us,
            ticks_diff=clock.ticks_diff,
            heap_free=lambda: 1024,
        )
        adapter.reset_trial({"algorithm": "DGA"}, {})
        with self.assertRaises(MemoryError):
            adapter.choose_goal()
        metrics = adapter.timing_metrics()
        self.assertEqual(metrics["status"], "allocator_error")
        self.assertEqual(metrics["error_type"], "MemoryError")
        self.assertEqual(metrics["allocator_time_us"], 50)

    def test_end_trial_only_releases_runtime(self) -> None:
        created = []
        adapter = self._adapter(created)
        adapter.reset_trial({"algorithm": "DGA"}, {})
        adapter.end_trial()
        self.assertIsNone(adapter.runtime)
        with self.assertRaisesRegex(RuntimeError, "reset_trial"):
            adapter.apply_delta({})


if __name__ == "__main__":
    unittest.main()
