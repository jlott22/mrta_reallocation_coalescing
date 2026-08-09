from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from allocator_replay.capture.cohorts import (
    generate_bayesian,
    generate_collaborative,
)
from allocator_replay.config.study import (
    BAYESIAN_TRIAL_IDS,
    COLLABORATIVE_TRIAL_IDS,
    INITIAL_HARDWARE_TRIAL_COUNTS,
    cohort_path,
    conditions,
)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(
            csv.DictReader(
                line
                for line in handle
                if line.strip() and not line.startswith("#")
            )
        )


class CohortTests(unittest.TestCase):
    def test_low_k_levels_and_pilot_trial_counts(self) -> None:
        bayesian = {
            (item.top_k_level, item.top_k_cells)
            for item in conditions("bayesian")
            if item.algorithm == "CBAA"
        }
        collaborative = {
            (item.top_k_level, item.top_k_cells)
            for item in conditions("collaborative")
            if item.algorithm == "CBAA"
        }
        self.assertTrue(
            {("3%", 11), ("1%", 4), ("K=1", 1)} <= bayesian
        )
        self.assertTrue({("K=2", 2), ("K=1", 1)} <= collaborative)
        self.assertEqual(INITIAL_HARDWARE_TRIAL_COUNTS["bayesian"], 25)
        self.assertEqual(
            INITIAL_HARDWARE_TRIAL_COUNTS["collaborative"],
            10,
        )
        all_conditions = conditions()
        self.assertEqual(len(all_conditions), 102)
        self.assertEqual(
            len({item.condition_id for item in all_conditions}),
            len(all_conditions),
        )

    def test_bayesian_is_reproducible_and_held_out(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            generated = generate_bayesian(Path(directory) / "bayesian.csv")
            self.assertEqual(generated.read_bytes(), cohort_path("bayesian").read_bytes())
            ids = [int(row["episode"]) for row in _rows(generated)]
            self.assertEqual(ids, list(BAYESIAN_TRIAL_IDS))
            self.assertTrue(set(ids).isdisjoint(range(500)))

    def test_collaborative_continues_exact_seed_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            generated = generate_collaborative(Path(directory) / "collaborative.csv")
            self.assertEqual(
                generated.read_bytes(),
                cohort_path("collaborative").read_bytes(),
            )
            ids = [int(row["trial_id"]) for row in _rows(generated)]
            self.assertEqual(ids, list(COLLABORATIVE_TRIAL_IDS))
            self.assertTrue(set(ids).isdisjoint(range(100)))
