from __future__ import annotations

import csv
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from study.causal.hardware_analysis import analyze
from study.causal.model import BoardBinding, CausalJob, PairedBlock, PolicySpec
from study.manifests import canonical_json_bytes


class HardwareAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output_root = self.root / "study" / "output" / "hardware_core"
        self.causal_root = self.output_root / "causal" / "completed"
        self.analysis_root = self.output_root / "analysis"
        self.source = {
            "git_head": "a" * 40,
            "source_tree_sha256": "b" * 64,
            "relevant_dirty": False,
            "relevant_status_porcelain": [],
            "ignored_generated_prefixes": [],
        }
        self.boards = tuple(
            BoardBinding(
                board_id=f"rp2040_{index}",
                serial_device=f"/dev/ttyACM{index}",
                expected_device_uid=f"uid_{index}",
                expected_build_id="build_v1",
                expected_firmware_sha256=f"{index + 1:064x}",
                expected_module_set_sha256=f"{index + 11:064x}",
            )
            for index in range(4)
        )
        self.policies = (
            PolicySpec("eager_b1", "eager", 1),
            PolicySpec("count_b4", "count", 4),
        )
        config_path = self.root / "configs" / "hardware_core.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("{}", encoding="utf-8")
        self.config = SimpleNamespace(
            path=config_path,
            repo_root=self.root,
            raw={"campaign": {"require_clean_source": True}},
            config_sha256="c" * 64,
            campaign_id="corrected_hardware_core_96_v1",
            stage="publication_core",
            output_root=self.output_root,
            manifest_index_sha256="d" * 64,
            hardware_binding_sha256="e" * 64,
            algorithms=("CBAA", "ACBBA", "PI", "HIPC"),
            loads=("low", "medium", "high"),
            policies=self.policies,
            boards=self.boards,
            core_affinities=(0, 1, 2, 3),
            development_override=False,
            required_gate_paths=(),
        )
        self.blocks = self._blocks()
        self.jobs = [job for block in self.blocks for job in block.jobs]
        self.schedule = {"schema_version": 1, "canonical": "test-schedule"}
        self.schedule_summary = {
            "schema_version": 1,
            "block_count": len(self.blocks),
            "job_count": len(self.jobs),
            "schedule_sha256": "f" * 64,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _blocks(self) -> list[PairedBlock]:
        blocks: list[PairedBlock] = []
        algorithms = ("CBAA", "ACBBA", "PI", "HIPC")
        for worker_index, board in enumerate(self.boards):
            jobs: list[CausalJob] = []
            block_id = f"block_{worker_index}"
            for order, policy in enumerate(self.policies):
                jobs.append(CausalJob(
                    job_id=f"{block_id}__{policy.policy_id}",
                    block_id=block_id,
                    algorithm=algorithms[worker_index],
                    load_id="medium",
                    trace_id=f"trace_{worker_index:04d}",
                    policy=policy,
                    policy_order_index=order,
                    board_id=board.board_id,
                    worker_index=worker_index,
                    core_id=worker_index,
                    scenario_path=self.root / "scenario.json",
                    release_path=self.root / "release.json",
                    scenario_sha256="1" * 64,
                    release_sha256="2" * 64,
                    runtime_seed=100 + worker_index,
                    zero_compute=False,
                ))
            blocks.append(PairedBlock(
                block_id=block_id,
                algorithm=algorithms[worker_index],
                load_id="medium",
                trace_id=f"trace_{worker_index:04d}",
                board=board,
                worker_index=worker_index,
                core_id=worker_index,
                jobs=tuple(jobs),
            ))
        return blocks

    def _summary(self, job: CausalJob, *, incomplete: bool) -> dict:
        work = 10.0 if job.policy.policy_id == "eager_b1" else 8.0
        return {
            "zero_compute": False,
            "hardware_validated": True,
            "parity_passed": True,
            "all_tasks_completed": not incomplete,
            "technical_status": "completed",
            "rp2040_allocator_processor_work_s": work,
            "agx_allocator_processor_work_s": 2.0,
            "median_release_to_first_assignment_latency_s": 1.0,
            "p95_release_to_first_assignment_latency_s": 2.0,
            "median_release_to_completion_latency_s": 4.0,
            "p95_release_to_completion_latency_s": 6.0,
            "mission_elapsed_time_s": 100.0,
            "max_robot_steps": 40,
            "total_team_steps": 150,
            "allocation_epoch_count": 12,
            "arrival_driven_event_count": 6,
            "mandatory_event_count": 6,
            "allocator_call_count": 24,
            "processor_capacity_fraction": 0.02,
        }

    def _prepare_campaign(self, *, mismatched_execution: bool = False) -> None:
        self.causal_root.mkdir(parents=True)
        (self.output_root / "causal_schedule.json").write_bytes(
            canonical_json_bytes(self.schedule)
        )
        incomplete_job_id = "block_0__count_b4"
        for job in self.jobs:
            directory = self.causal_root / job.job_id
            directory.mkdir()
            (directory / "completion.json").write_bytes(canonical_json_bytes({
                "job_id": job.job_id,
                "completion": "test",
            }))
            (directory / "trial_summary.json").write_bytes(canonical_json_bytes(
                self._summary(job, incomplete=job.job_id == incomplete_job_id)
            ))
        completed = len(self.jobs) - 1
        report = {
            "report_kind": "causal_campaign_execution",
            "passed": True,
            "zero_compute": False,
            "campaign_id": self.config.campaign_id,
            "worker_count": len(self.boards),
            "planned_jobs": len(self.jobs),
            "completed_jobs": len(self.jobs),
            "pending_jobs": 0,
            "conflicting_jobs": 0,
            "hardware_validated": True,
            "schedule_sha256": self.schedule_summary["schedule_sha256"],
            "config_sha256": self.config.config_sha256,
            "manifest_index_sha256": self.config.manifest_index_sha256,
            "hardware_binding_sha256": self.config.hardware_binding_sha256,
            "source_identity": self.source,
            "board_bindings": [board.to_dict() for board in self.boards],
            "core_affinities": [0, 1, 2, 3],
            "job_states": {job.job_id: "completed" for job in self.jobs},
            "algorithmic_incompletions_are_retained": True,
            "algorithmically_completed_jobs": completed,
            "algorithmically_incomplete_jobs": 1,
            "technical_attempt_failures": 0,
        }
        if mismatched_execution:
            report["source_identity"] = dict(self.source) | {"git_head": "f" * 40}
        (self.output_root / "campaign_execution_report.json").write_bytes(
            canonical_json_bytes(report)
        )

    def _patches(self) -> ExitStack:
        stack = ExitStack()
        stack.enter_context(patch(
            "study.causal.hardware_analysis._git_identity",
            return_value=self.source,
        ))
        stack.enter_context(patch(
            "study.causal.hardware_analysis.validate_campaign_gates",
            return_value=None,
        ))
        stack.enter_context(patch(
            "study.causal.hardware_analysis.plan_paired_blocks",
            return_value=self.blocks,
        ))
        stack.enter_context(patch(
            "study.causal.hardware_analysis.validate_schedule",
            return_value=self.schedule_summary,
        ))
        stack.enter_context(patch(
            "study.causal.hardware_analysis._build_schedule_record",
            return_value=self.schedule,
        ))
        stack.enter_context(patch(
            "study.causal.hardware_analysis._completion_state",
            return_value="completed",
        ))
        stack.enter_context(patch(
            "study.causal.hardware_analysis.validate_causal_outputs",
            return_value={"required_output_sha256": {"trial_summary.json": "9" * 64}},
        ))
        return stack

    def test_descriptive_bundle_retains_incomplete_trial_and_never_infers(self) -> None:
        self._prepare_campaign()
        with self._patches():
            paths = analyze(self.causal_root, self.analysis_root, config=self.config)
            analyze(self.causal_root, self.analysis_root, config=self.config)

        metadata = json.loads(paths["metadata"].read_text(encoding="utf-8"))
        self.assertTrue(metadata["descriptive_only"])
        self.assertFalse(metadata["inferential_tests_performed"])
        self.assertTrue(metadata["hardware_provider_results_must_remain_separate"])
        self.assertEqual(8, metadata["denominators"]["planned_trial_count"])
        self.assertEqual(1, metadata["denominators"]["algorithmically_incomplete_trial_count"])
        self.assertFalse((self.analysis_root / "friedman_tests.json").exists())
        self.assertFalse((self.analysis_root / "wilcoxon_eager_contrasts_holm.json").exists())

        with paths["trial_level"].open(newline="", encoding="utf-8") as handle:
            trials = list(csv.DictReader(handle))
        self.assertEqual(8, len(trials))
        incomplete = next(row for row in trials if row["job_id"] == "block_0__count_b4")
        self.assertEqual("algorithmically_incomplete", incomplete["algorithmic_outcome"])

        with paths["paired_eager"].open(newline="", encoding="utf-8") as handle:
            paired = list(csv.DictReader(handle))
        self.assertEqual(4, len(paired))
        incomplete_pair = next(row for row in paired if row["condition_job_id"] == "block_0__count_b4")
        self.assertEqual("condition_algorithmically_incomplete", incomplete_pair["pair_outcome"])
        self.assertEqual(-2.0, float(incomplete_pair["paired_delta_rp2040_work_s_condition_minus_eager"]))

        with paths["outcome_summaries"].open(newline="", encoding="utf-8") as handle:
            outcomes = list(csv.DictReader(handle))
        totals = next(row for row in outcomes if row["summary_scope"] == "all_conditions")
        self.assertEqual("8", totals["planned_trial_count"])
        self.assertEqual("8", totals["technically_completed_trial_count"])
        self.assertEqual("1", totals["algorithmically_incomplete_trial_count"])

    def test_execution_identity_mismatch_fails_closed_before_writing_analysis(self) -> None:
        self._prepare_campaign(mismatched_execution=True)
        with self._patches(), self.assertRaisesRegex(ValueError, "source_identity mismatch"):
            analyze(self.causal_root, self.analysis_root, config=self.config)
        self.assertFalse(self.analysis_root.exists())


if __name__ == "__main__":
    unittest.main()
