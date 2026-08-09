from __future__ import annotations

import unittest

from allocator_replay.device.native.bayesian import create_persistent_runtime


class _CompleteAdapter:
    def __init__(self) -> None:
        self.deltas = []
        self.messages = [{"type": "claim", "cell": (1, 0)}]

    def reset_trial(self, config, state):
        self.config = config
        self.state = state
        return {"adapter": "complete"}

    def apply_delta(self, delta):
        self.deltas.append(delta)

    def choose_goal(self):
        return {"goal": (1, 0), "source": "complete_adapter"}

    def drain_messages(self):
        result = self.messages
        self.messages = []
        return result

    def snapshot_minimal(self):
        return {"adapter_state": "resident"}


class NativeBayesianRuntimeTests(unittest.TestCase):
    def test_consensus_algorithm_never_silently_uses_diagnostic_core(self) -> None:
        runtime = create_persistent_runtime(
            {"algorithm": "PI", "grid_size": 2}
        )
        runtime.reset_trial(
            {"algorithm": "PI", "grid_size": 2},
            {
                "rid": "0",
                "pos": (0, 0),
                "known_clues": [(1, 1)],
                "target_p": {(0, 0): 0.1, (1, 0): 0.4},
            },
        )
        with self.assertRaisesRegex(
            RuntimeError, "complete native consensus/message adapter"
        ):
            runtime.choose_goal()

    def test_complete_adapter_uses_exact_persistent_facade(self) -> None:
        adapter = _CompleteAdapter()
        runtime = create_persistent_runtime(
            {
                "algorithm": "HIPC",
                "grid_size": 2,
                "allocator_adapter": adapter,
            }
        )
        runtime.apply_delta({"pos": (0, 1)})
        self.assertEqual(
            runtime.choose_goal(),
            {"goal": (1, 0), "source": "complete_adapter"},
        )
        self.assertEqual(adapter.deltas, [{"pos": (0, 1)}])
        self.assertEqual(
            runtime.drain_messages(),
            [{"type": "claim", "cell": (1, 0)}],
        )
        self.assertEqual(
            runtime.snapshot_minimal(), {"adapter_state": "resident"}
        )

    def test_delta_updates_cached_probability_map_without_rebuilding_trial(self) -> None:
        runtime = create_persistent_runtime(
            {
                "algorithm": "CBAA",
                "grid_size": 2,
                "allow_diagnostic_local_core": True,
            }
        )
        runtime.reset_trial(
            {
                "algorithm": "CBAA",
                "grid_size": 2,
                "allow_diagnostic_local_core": True,
                "max_candidate_cells": 2,
            },
            {
                "rid": "0",
                "pos": (0, 0),
                "known_clues": [(1, 1)],
                "target_p": {
                    (0, 0): 0.1,
                    (1, 0): 0.2,
                    (0, 1): 0.3,
                    (1, 1): 0.4,
                },
            },
        )
        self.assertEqual(runtime.scorer.maximum, 0.4)
        runtime.apply_delta(
            {"target_p_updates": {(1, 0): 0.8}}
        )
        self.assertEqual(runtime.scorer.maximum, 0.8)
        decision = runtime.choose_goal()
        self.assertEqual(decision["goal"], (1, 0))
        self.assertEqual(
            runtime.snapshot_minimal()["probability_maximum"], 0.8
        )


if __name__ == "__main__":
    unittest.main()
