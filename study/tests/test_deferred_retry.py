from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.agx_prepare_deferred_retry import prepare


class DeferredRetrySelectionTests(unittest.TestCase):
    def test_selection_is_exact_hashed_and_excludes_recovered_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base.json"
            base.write_text(
                json.dumps({"manifest": {}, "campaign": {"notes": "original"}}),
                encoding="utf-8",
            )
            source = root / "source"

            def failure(job_id: str, attempt: str) -> None:
                directory = source / "attempts" / job_id / attempt
                directory.mkdir(parents=True)
                (directory / "failure.json").write_text(json.dumps({
                    "job_id": job_id,
                    "return_code": 0,
                    "message": "mission_elapsed_time_s must be a finite number",
                }), encoding="utf-8")
                (directory / "trial_summary.json").write_text(json.dumps({
                    "all_tasks_completed": False,
                    "causal_timing_enabled": True,
                    "mission_elapsed_time_s": None,
                    "algorithmic_failure_type": "stagnation_horizon",
                }), encoding="utf-8")

            failure("CBAA__low__eager_b1__trace_0001", "attempt_0001")
            failure("CBAA__low__eager_b1__trace_0001", "attempt_0002")
            failure("PI__high__count_b2__trace_0002", "attempt_0001")
            recovered = source / "completed" / "PI__high__count_b2__trace_0002"
            recovered.mkdir(parents=True)
            (recovered / "completion.json").write_text("{}", encoding="utf-8")

            config_out = root / "retry.json"
            audit_out = root / "retry.audit.json"
            prepare(
                base,
                source,
                "retry_v2",
                "study/output/retry_v2",
                config_out,
                audit_out,
            )
            config = json.loads(config_out.read_text(encoding="utf-8"))
            audit = json.loads(audit_out.read_text(encoding="utf-8"))
            selected = ["CBAA__low__eager_b1__trace_0001"]
            self.assertEqual(selected, config["campaign"]["job_allowlist"])
            self.assertEqual(1, config["campaign"]["expected_selected_job_count"])
            self.assertEqual(selected, audit["selected_job_ids"])
            self.assertEqual(2, audit["failure_record_count"])
            self.assertEqual(
                {"misclassified_algorithmic_incomplete": 2},
                audit["classifications"],
            )
            self.assertTrue(all(record["failure_sha256"] for record in audit["records"]))


if __name__ == "__main__":
    unittest.main()
