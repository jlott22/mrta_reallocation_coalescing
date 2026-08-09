from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from study.causal.analysis import _require_canonical_schedule_artifact
from study.causal.freeze import ANALYSIS_VERSION
from study.causal.outputs import _integer
from study.causal.reports import full_campaign_report
from study.manifests import canonical_json_bytes, sha256_file


class FullCausalReportIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        self.campaign = self.repo / "study" / "output" / "full"
        self.analysis = self.campaign / "analysis"
        self.analysis.mkdir(parents=True)
        self.config = SimpleNamespace(
            stage="full",
            development_override=False,
            output_root=self.campaign,
            repo_root=self.repo,
            config_sha256="a" * 64,
            manifest_index_sha256="b" * 64,
        )
        self.source = {
            "git_head": "c" * 40,
            "git_dirty": False,
            "source_tree_sha256": "d" * 64,
        }
        self.raw_sentinel = self.campaign / "causal" / "completed" / "job" / "allocator_calls.csv"
        self.raw_sentinel.parent.mkdir(parents=True)
        self.raw_sentinel.write_text("call_id\ncall_1\n", encoding="utf-8")
        paired = self.analysis / "paired_eager_effects_trial_level.csv"
        with paired.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=("board_id", "negative_D_alloc_flag"),
            )
            writer.writeheader()
            writer.writerow({"board_id": "board_0", "negative_D_alloc_flag": False})

        causal_execution = {
            "report_kind": "causal_campaign_execution",
            "passed": True,
            "zero_compute": False,
            "hardware_validated": True,
            "planned_jobs": 1,
            "completed_jobs": 1,
            "technical_attempt_failures": 1,
            "schedule_sha256": "e" * 64,
            "config_sha256": self.config.config_sha256,
            "manifest_index_sha256": self.config.manifest_index_sha256,
            "source_identity": self.source,
        }
        zero_execution = dict(causal_execution) | {
            "zero_compute": True,
            "hardware_validated": False,
            "technical_attempt_failures": 2,
            "schedule_sha256": "f" * 64,
        }
        (self.campaign / "campaign_execution_report.json").write_bytes(
            canonical_json_bytes(causal_execution)
        )
        (self.campaign / "zero_compute_execution_report.json").write_bytes(
            canonical_json_bytes(zero_execution)
        )
        self.causal_identity = self._identity(False)
        self.zero_identity = self._identity(True)
        metadata = {
            "analysis_version": ANALYSIS_VERSION,
            "trial_is_the_independent_replicate": True,
            "causal_valid_trial_count": 1,
            "excluded_trial_count": 0,
            "algorithmically_incomplete_trial_count": 0,
            "zero_compute_job_coverage_complete": True,
            "zero_compute_pair_coverage_complete": True,
            "input_identity": {
                "validation_mode": "frozen_full_campaign_semantic_revalidation",
                "causal": self.causal_identity,
                "zero_compute": self.zero_identity,
            },
            "analysis_output_sha256": {paired.name: sha256_file(paired)},
        }
        (self.analysis / "analysis_metadata.json").write_bytes(
            canonical_json_bytes(metadata)
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _identity(self, zero_compute: bool) -> dict[str, object]:
        report_name = (
            "zero_compute_execution_report.json"
            if zero_compute
            else "campaign_execution_report.json"
        )
        return {
            "config_sha256": self.config.config_sha256,
            "manifest_index_sha256": self.config.manifest_index_sha256,
            "git_head": self.source["git_head"],
            "source_tree_sha256": self.source["source_tree_sha256"],
            "execution_report_sha256": sha256_file(self.campaign / report_name),
            "planned_job_count": 1,
            "design_id": "design_v1",
            "required_output_sha256_by_job": {
                "job": {"allocator_calls.csv": sha256_file(self.raw_sentinel)}
            },
        }

    def _current_identity(self, zero_compute: bool) -> dict[str, object]:
        identity = dict(self.zero_identity if zero_compute else self.causal_identity)
        identity["required_output_sha256_by_job"] = {
            "job": {"allocator_calls.csv": sha256_file(self.raw_sentinel)}
        }
        return identity

    def _report(self) -> dict[str, object]:
        block = SimpleNamespace(jobs=[SimpleNamespace(job_id="job")])

        def schedule_summary(_config: object, _blocks: object, *, zero_compute: bool) -> dict[str, str]:
            return {"schedule_sha256": ("f" if zero_compute else "e") * 64}

        def raw_rows(_config: object, *, zero_compute: bool):
            return ([{"job_id": "job"}], self._current_identity(zero_compute))

        with (
            patch("study.causal.orchestrator._git_identity", return_value=self.source),
            patch("study.causal.orchestrator._validate_full_freeze", return_value={"design_id": "design_v1"}),
            patch("study.causal.schedule.plan_paired_blocks", return_value=[block]),
            patch("study.causal.schedule.validate_schedule", side_effect=schedule_summary),
            patch("study.causal.analysis._validated_campaign_rows", side_effect=raw_rows),
        ):
            report, _ = full_campaign_report(
                self.campaign, self.analysis, self.config
            )
        return report

    def test_full_report_revalidates_current_raw_outputs_and_counts_both_arms(self) -> None:
        report = self._report()
        self.assertTrue(report["passed"])
        self.assertTrue(report["raw_outputs_revalidated_against_analysis_inputs"])
        self.assertEqual(1, report["causal_technical_attempt_failures"])
        self.assertEqual(2, report["zero_compute_technical_attempt_failures"])
        self.assertEqual(3, report["technical_attempt_failures"])

        self.raw_sentinel.write_text("call_id\ntampered\n", encoding="utf-8")
        report = self._report()
        self.assertFalse(report["passed"])
        self.assertFalse(report["raw_outputs_revalidated_against_analysis_inputs"])

    def test_integer_validation_preserves_64_bit_paired_seed(self) -> None:
        seed = 6_173_009_043_561_456_367
        self.assertEqual(seed, _integer(seed, "runtime_seed"))
        self.assertEqual(seed, _integer(str(seed), "runtime_seed"))

    def test_analysis_rejects_a_mutated_immutable_schedule(self) -> None:
        path = self.campaign / "causal_schedule.json"
        expected = {"schema_version": 1, "jobs": ["job"]}
        path.write_bytes(canonical_json_bytes(expected))
        _require_canonical_schedule_artifact(path, expected, kind="causal")
        path.write_bytes(canonical_json_bytes(expected | {"jobs": ["tampered"]}))
        with self.assertRaisesRegex(ValueError, "canonical frozen plan"):
            _require_canonical_schedule_artifact(path, expected, kind="causal")

    def test_full_report_fails_cleanly_on_malformed_retry_count(self) -> None:
        path = self.campaign / "zero_compute_execution_report.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["technical_attempt_failures"] = None
        path.write_bytes(canonical_json_bytes(value))
        report = self._report()
        self.assertFalse(report["passed"])
        self.assertFalse(report["technical_failure_counts_valid"])
        self.assertIsNone(report["technical_attempt_failures"])


if __name__ == "__main__":
    unittest.main()
