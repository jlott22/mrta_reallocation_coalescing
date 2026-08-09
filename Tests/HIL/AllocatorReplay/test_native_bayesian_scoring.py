from __future__ import annotations

import unittest

from allocator_replay.device.native.bayesian import (
    ACBBAInsertionCore,
    CBAACore,
    HIPCCore,
    NormalizedProbabilityScorer,
    PICore,
)


class NativeBayesianScoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.probabilities = {
            (0, 0): 0.10,
            (1, 0): 0.40,
            (2, 0): 0.20,
        }
        self.scorer = NormalizedProbabilityScorer(
            self.probabilities, grid_size=3
        )

    def test_normalization_cache_tracks_full_and_delta_belief_changes(self) -> None:
        self.assertEqual(self.scorer.maximum, 0.40)
        self.assertAlmostEqual(
            self.scorer.normalized_probability((0, 0)), 0.25
        )

        self.scorer.apply_updates({(2, 0): 0.80})
        self.assertEqual(self.scorer.maximum, 0.80)
        self.assertAlmostEqual(
            self.scorer.normalized_probability((1, 0)), 0.50
        )

        # Lowering the maximum invalidates and lazily rescans the map.
        self.scorer.apply_updates({(2, 0): 0.05})
        self.assertEqual(self.scorer.maximum, 0.40)
        self.assertEqual(
            self.scorer.normalized_probability((1, 0)), 1.0
        )

    def test_cbaa_uses_normalized_probability_cost(self) -> None:
        core = CBAACore(self.scorer)
        # The maximum-probability cell has no probability penalty.
        self.assertEqual(core.bid((0, 0), (1, 0)), -1.0)
        # p=.1/max=.4, so penalty is 8*(1-.25)=6.
        self.assertEqual(core.bid((0, 0), (0, 0)), -6.0)

    def test_acbba_scores_marginal_distance_with_shared_objective(self) -> None:
        core = ACBBAInsertionCore(self.scorer)
        index, bid = core.best_insertion(
            (0, 0), [(2, 0)], (1, 0)
        )
        self.assertEqual(index, 0)
        # Inserting (1,0) before (2,0) adds no route distance and the cell has
        # normalized probability one.
        self.assertEqual(bid, 0.0)
        self.assertEqual(
            core.bid_from_reference((0, 0), (1, 0)), -1.0
        )

    def test_pi_and_hipc_consume_the_same_scorer(self) -> None:
        pi = PICore(self.scorer)
        hipc = HIPCCore(self.scorer)
        self.assertEqual(
            pi.insertion_cost((0, 0), [], (1, 0), 0),
            1.0,
        )
        self.assertEqual(
            hipc.bid_from_reference((0, 0), (1, 0)),
            -1.0,
        )


if __name__ == "__main__":
    unittest.main()
