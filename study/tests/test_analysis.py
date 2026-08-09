from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from study.analysis import aggregate, condition_summaries, paired_deltas, validate_task_rows
from study.campaign import CampaignOrchestrator
from study.tests.helpers import test_config, write_config, write_valid_outputs


class AnalysisTests(unittest.TestCase):
    def test_paired_delta_signs_and_trial_level_grouping(self) -> None:
        common = {
            "technical_status": "completed", "algorithm": "CBAA", "arrival_load": "medium",
            "trial_id": "trace_0000", "policy_max_pending_age_s": None,
        }
        eager = common | {
            "job_id": "eager", "condition_id": "eager", "policy_id": "eager_b1",
            "policy_mode": "eager", "batch_size": 1,
            "cumulative_allocator_time_s": 10.0,
            "mean_release_to_first_assignment_latency_s": 2.0,
            "mean_release_to_completion_latency_s": 20.0,
            "mission_elapsed_time_s": 100.0, "max_robot_steps": 50,
            "allocation_epoch_count": 20, "allocator_call_count": 30,
            "all_tasks_completed": True,
        }
        count = common | {
            "job_id": "count", "condition_id": "count", "policy_id": "count_b4",
            "policy_mode": "count", "batch_size": 4,
            "cumulative_allocator_time_s": 6.0,
            "mean_release_to_first_assignment_latency_s": 5.0,
            "mean_release_to_completion_latency_s": 25.0,
            "mission_elapsed_time_s": 103.0, "max_robot_steps": 52,
            "allocation_epoch_count": 9, "allocator_call_count": 14,
            "all_tasks_completed": True,
        }
        paired = paired_deltas([eager, count])
        count_delta = next(row for row in paired if row["policy_id"] == "count_b4")
        self.assertEqual(4.0, count_delta["allocator_compute_saved_s"])
        self.assertEqual(40.0, count_delta["percent_allocator_computation_saved"])
        self.assertEqual(3.0, count_delta["change_assignment_latency_s"])
        self.assertEqual(5.0, count_delta["change_completion_latency_s"])
        summaries = condition_summaries([eager, count], paired)
        count_summary = next(row for row in summaries if row["policy_id"] == "count_b4")
        self.assertEqual(1, count_summary["paired_trial_count"])
        self.assertEqual(40.0, count_summary["percent_allocator_computation_saved_mean"])

    def test_end_to_end_aggregation_creates_clean_plot_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(trace_count=2, loads={"medium": 0.2})
            config_path = write_config(root, config)

            def fake_executor(job, command, attempt_dir):
                eager = job.policy_mode == "eager"
                write_valid_outputs(
                    job,
                    attempt_dir,
                    allocator_time_s=10.0 if eager else 6.0,
                    assignment_delay_s=2.0 if eager else 5.0,
                    completion_delay_s=18.0 if eager else 20.0,
                )
                return 0

            orchestrator = CampaignOrchestrator(
                config_path, root, logical_cores=22, executor=fake_executor
            )
            results = orchestrator.run()
            self.assertTrue(all(result.status == "completed" for result in results))
            paths = aggregate(orchestrator)
            with paths["paired"].open(newline="", encoding="utf-8") as handle:
                paired = list(csv.DictReader(handle))
            self.assertEqual(4, len(paired))  # eager and count for each of two paired traces
            count_rows = [row for row in paired if row["policy_id"] == "count_b2"]
            self.assertEqual({"40.0"}, {row["percent_allocator_computation_saved"] for row in count_rows})
            for name in ("figure1", "figure2", "figure3", "conditions", "trial_level"):
                self.assertTrue(paths[name].is_file(), name)

    def test_task_timestamp_order_is_enforced(self) -> None:
        class MinimalJob:
            job_id = "bad_job"

        rows = [{
            "task_id": "task_0001", "release_time_s": "5", "admission_time_s": "4",
            "first_assignment_time_s": "6", "completion_time_s": "7",
        }]
        with self.assertRaisesRegex(ValueError, "out of order"):
            validate_task_rows(rows, MinimalJob())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
