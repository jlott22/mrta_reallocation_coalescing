from __future__ import annotations

import inspect
import unittest
from array import array

from allocator_replay.device.native.bayesian import (
    DGACore,
    DMCHBACore,
    NormalizedProbabilityScorer,
)
from allocator_replay.device.native.bayesian import dga as dga_module


class NativeBayesianEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.probabilities = {
            (0, 0): 0.10,
            (1, 0): 0.40,
            (0, 1): 0.20,
            (1, 1): 0.30,
        }
        self.scorer = NormalizedProbabilityScorer(
            self.probabilities, grid_size=2
        )

    def test_dmchba_assigns_each_task_once_without_square_matrix(self) -> None:
        core = DMCHBACore(self.scorer, commitment_horizon=3)
        assigned = core.assign(
            {"0": (0, 0), "1": (1, 1)},
            list(self.probabilities),
        )
        flattened = assigned["0"] + assigned["1"]
        self.assertCountEqual(flattened, list(self.probabilities))
        self.assertEqual(core.last_matrix_size, 4)
        self.assertEqual(core.last_clones_per_agent, 2)
        self.assertLess(core.workspace.payload_bytes(), 256)
        self.assertFalse(hasattr(core, "cost_matrix"))

    def test_dga_uses_full_requested_study_configuration(self) -> None:
        core = DGACore(self.scorer, grid_size=2, seed=81)
        result = core.evolve(
            {"0": (0, 0), "1": (1, 1)},
            list(self.probabilities),
            own_id="0",
        )
        self.assertEqual(core.POPULATION_SIZE, 30)
        self.assertEqual(core.ITERATIONS_PER_TRIGGER, 25)
        self.assertEqual(result["population_size"], 30)
        self.assertEqual(result["generation"], 25)
        self.assertEqual(result["iterations_per_trigger"], 25)
        cells = result["plan"]["0"] + result["plan"]["1"]
        self.assertCountEqual(cells, list(self.probabilities))

        for plan in core.population:
            for route in plan:
                self.assertIsInstance(route, array)
                self.assertEqual(route.typecode, "H")

    def test_all_five_new_mutations_are_micropython_safe(self) -> None:
        core = DGACore(
            self.scorer,
            grid_size=2,
            seed=97,
            population_size=4,
            iterations=0,
        )
        core.team_ids = ["0", "1"]
        original = [
            array("H", [0, 1]),
            array("H", [2, 3]),
        ]
        mask = bytearray([1, 1, 1, 1])
        for operation in ("move", "swap", "reinsert", "reverse", "clean"):
            mutated = core.mutate(
                original, valid_codes=mask, operation=operation
            )
            values = [int(code) for route in mutated for code in route]
            self.assertCountEqual(values, [0, 1, 2, 3], operation)
            self.assertEqual(core.last_mutation, operation)

        # Guards the exact CPython construct that crashed the collaborative
        # generated port on MicroPython.
        source = inspect.getsource(dga_module.DGACore.mutate)
        self.assertNotIn("= reversed(", source)

    def test_crossover_uses_random_segments_not_fixed_halves(self) -> None:
        core = DGACore(
            self.scorer,
            grid_size=2,
            seed=103,
            population_size=4,
            iterations=0,
        )
        core.team_ids = ["0", "1"]
        first = [array("H", [0, 1, 2]), array("H", [3])]
        second = [array("H", [3, 2, 1]), array("H", [0])]
        child = core.crossover(first, second)
        self.assertEqual(len(child), 2)
        self.assertTrue(all(isinstance(route, array) for route in child))
        # Each non-empty parent contributes a non-empty contiguous segment.
        self.assertGreaterEqual(len(child[0]), 2)
        self.assertEqual(len(child[1]), 2)


if __name__ == "__main__":
    unittest.main()
