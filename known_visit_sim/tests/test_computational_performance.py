from __future__ import annotations

import unittest

from known_visit_sim.algorithms.CBAA import CBAAAllocator
from known_visit_sim.comms.models import make_comm_model
from known_visit_sim.config import SimConfig
from known_visit_sim.core.scheduler import AsyncTrialRunner
from known_visit_sim.core.types import TrialScenario
from known_visit_sim.metrics.summary import build_rows


class ComputationalPerformanceTests(unittest.TestCase):
    def _state(self):
        cfg = SimConfig(
            grid_size=3,
            robot_ids=["00"],
            start_positions={"00": (0, 0)},
            start_headings={"00": (1, 0)},
        )
        runner = AsyncTrialRunner(
            cfg,
            CBAAAllocator,
            make_comm_model("ideal", None),
            seed=0,
        )
        return runner.new_trial(
            TrialScenario(trial_id=7, targets=[(2, 2)])
        )

    def test_choose_goal_records_separate_allocator_and_filter_times(self) -> None:
        state = self._state()
        robot = state.robots["00"]

        robot.step(0.0, state.planner)

        self.assertEqual(len(robot.counters.allocator_time_ns_samples), 1)
        self.assertEqual(len(robot.counters.allocator_solve_time_ns_samples), 1)
        self.assertGreaterEqual(
            len(robot.counters.candidate_filter_time_ns_samples),
            1,
        )
        self.assertLessEqual(
            robot.counters.allocator_solve_time_ns_samples[0],
            robot.counters.allocator_time_ns_samples[0],
        )

    def test_metric_rows_export_separate_timing_columns(self) -> None:
        state = self._state()
        counters = state.robots["00"].counters
        counters.allocator_time_ns_samples = [2_000_000, 5_000_000]
        counters.allocator_solve_time_ns_samples = [1_000_000, 3_000_000]
        counters.candidate_filter_time_ns_samples = [1_000_000, 2_000_000]

        _, system, robots, _ = build_rows(
            state,
            algorithm="CBAA",
            comm_model="ideal",
            comm_level="ideal",
            scenario_file="scenario.csv",
        )

        row = robots[0]
        self.assertEqual(row["allocator_calls"], 2)
        self.assertAlmostEqual(row["allocator_time_ms_total"], 7.0)
        self.assertAlmostEqual(row["allocator_solve_time_ms_total"], 4.0)
        self.assertEqual(row["candidate_filter_calls"], 2)
        self.assertAlmostEqual(row["candidate_filter_time_ms_total"], 3.0)
        self.assertAlmostEqual(system["allocator_time_ms_team_total"], 7.0)
        self.assertAlmostEqual(system["allocator_time_ms_team_max"], 5.0)
        self.assertAlmostEqual(system["allocator_solve_time_ms_team_total"], 4.0)
        self.assertAlmostEqual(system["allocator_solve_time_ms_team_max"], 3.0)
        self.assertAlmostEqual(system["candidate_filter_time_ms_team_total"], 3.0)
        self.assertAlmostEqual(system["candidate_filter_time_ms_team_max"], 2.0)


if __name__ == "__main__":
    unittest.main()
