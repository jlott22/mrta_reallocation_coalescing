from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from study.campaign import (
    CampaignOrchestrator,
    OutputValidationError,
    _default_executor,
    _source_files,
    effective_workers,
    validate_job_outputs,
    worker_cap,
)
from study.tests.helpers import test_config, write_config, write_valid_outputs


class CampaignTests(unittest.TestCase):
    def test_source_files_exclude_transient_study_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tracked = root / "study" / "module.py"
            generated = root / "study" / "output" / "attempt" / "generated.py"
            tracked.parent.mkdir(parents=True)
            generated.parent.mkdir(parents=True)
            tracked.write_text("VALUE = 1\n", encoding="utf-8")
            generated.write_text("VALUE = 2\n", encoding="utf-8")

            files = _source_files(root)

            self.assertEqual({"study/module.py"}, set(files))

    def test_worker_limit_never_exceeds_three_quarters(self) -> None:
        self.assertEqual(16, worker_cap(22))
        self.assertEqual(6, worker_cap(8))
        self.assertEqual(0, worker_cap(1))
        self.assertEqual(16, effective_workers(1000, 22))
        self.assertEqual(5, effective_workers(5, 22))
        with self.assertRaises(RuntimeError):
            effective_workers(1, 1)

    def test_factorial_jobs_reuse_paired_manifest_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = write_config(root, test_config(algorithms=["CBAA", "PI"]))
            orchestrator = CampaignOrchestrator(config_path, root, logical_cores=22)
            orchestrator.prepare_manifests()
            jobs = orchestrator.plan_jobs()
            # 2 traces * 2 loads * 2 algorithms * 2 policies.
            self.assertEqual(16, len(jobs))
            same_trace_load = [
                job for job in jobs if job.trace_id == "trace_0000" and job.load_id == "low"
            ]
            self.assertEqual(4, len(same_trace_load))
            self.assertEqual(1, len({job.scenario_sha256 for job in same_trace_load}))
            self.assertEqual(1, len({job.release_sha256 for job in same_trace_load}))
            self.assertEqual(1, len({job.runtime_seed for job in same_trace_load}))

    def test_resume_skips_valid_completion_without_invoking_runner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(
                policies=[{"policy_id": "eager_b1", "mode": "eager", "batch_size": 1}],
                trace_count=1,
                loads={"low": 0.1},
            )
            config_path = write_config(root, config)
            calls = 0
            lock = threading.Lock()

            def fake_executor(job, command, attempt_dir):
                nonlocal calls
                with lock:
                    calls += 1
                write_valid_outputs(job, attempt_dir)
                return 0

            first = CampaignOrchestrator(
                config_path, root, logical_cores=22, executor=fake_executor
            ).run()
            self.assertEqual(["completed"], [result.status for result in first])
            self.assertEqual(1, calls)

            def forbidden_executor(job, command, attempt_dir):
                self.fail("valid completed job must not run again")

            second = CampaignOrchestrator(
                config_path, root, logical_cores=22, executor=forbidden_executor
            ).run()
            self.assertEqual(["skipped_completed"], [result.status for result in second])
            self.assertEqual(1, calls)

    def test_missing_required_output_is_logged_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(
                policies=[{"policy_id": "eager_b1", "mode": "eager", "batch_size": 1}],
                trace_count=1,
                loads={"low": 0.1},
            )
            config_path = write_config(root, config)

            def incomplete_executor(job, command, attempt_dir):
                (attempt_dir / "trial_summary.json").write_text("{}", encoding="utf-8")
                return 0

            orchestrator = CampaignOrchestrator(
                config_path, root, logical_cores=22, executor=incomplete_executor
            )
            result = orchestrator.run()
            self.assertEqual("failed", result[0].status)
            self.assertIn("missing/empty outputs", result[0].message)
            failures = orchestrator.output_root / "failures.jsonl"
            self.assertTrue(failures.is_file())
            self.assertIn(result[0].job_id, failures.read_text(encoding="utf-8"))

    def test_semantically_empty_outputs_are_not_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(
                policies=[{"policy_id": "eager_b1", "mode": "eager", "batch_size": 1}],
                trace_count=1,
                loads={"low": 0.1},
            )
            config_path = write_config(root, config)

            def empty_executor(job, command, attempt_dir):
                (attempt_dir / "trial_summary.json").write_text("{}\n", encoding="utf-8")
                (attempt_dir / "task_events.csv").write_text("task_id\n", encoding="utf-8")
                (attempt_dir / "allocation_epochs.csv").write_text("epoch_id\n", encoding="utf-8")
                return 0

            result = CampaignOrchestrator(
                config_path, root, logical_cores=22, executor=empty_executor
            ).run()
            self.assertEqual("failed", result[0].status)
            self.assertIn("semantic output validation failed", result[0].message)

    def test_semantic_validation_checks_tasks_epochs_and_mission_arithmetic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = write_config(root, test_config(trace_count=1, loads={"low": 0.1}))
            orchestrator = CampaignOrchestrator(config_path, root, logical_cores=22)
            orchestrator.prepare_manifests()
            job = orchestrator.plan_jobs()[0]

            valid = root / "valid"
            valid.mkdir()
            write_valid_outputs(job, valid)
            validated = validate_job_outputs(job, valid)
            self.assertTrue(validated["all_tasks_completed"])

            causal = root / "causal"
            causal.mkdir()
            write_valid_outputs(job, causal)
            causal_summary_path = causal / "trial_summary.json"
            causal_summary = json.loads(
                causal_summary_path.read_text(encoding="utf-8")
            )
            causal_summary["schema_version"] = 2
            causal_summary["causal_timing_enabled"] = True
            causal_summary.pop("other_execution_time_s")
            causal_summary["simulated_execution_time_s"] = causal_summary[
                "mission_elapsed_time_s"
            ]
            causal_summary_path.write_text(
                json.dumps(causal_summary), encoding="utf-8"
            )
            self.assertTrue(validate_job_outputs(job, causal)["all_tasks_completed"])

            task_bad = root / "task_bad"
            task_bad.mkdir()
            write_valid_outputs(job, task_bad)
            task_path = task_bad / "task_events.csv"
            task_path.write_text(
                task_path.read_text(encoding="utf-8").replace("task_0001", "task_bad", 1),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(OutputValidationError, "task IDs"):
                validate_job_outputs(job, task_bad)

            epoch_bad = root / "epoch_bad"
            epoch_bad.mkdir()
            write_valid_outputs(job, epoch_bad)
            epoch_path = epoch_bad / "allocation_epochs.csv"
            epoch_path.write_text(
                epoch_path.read_text(encoding="utf-8").replace(
                    "initial_allocation", "invented_reason", 1
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(OutputValidationError, "unknown trigger reason"):
                validate_job_outputs(job, epoch_bad)

            arithmetic_bad = root / "arithmetic_bad"
            arithmetic_bad.mkdir()
            write_valid_outputs(job, arithmetic_bad)
            summary_path = arithmetic_bad / "trial_summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["mission_elapsed_time_s"] += 1
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(OutputValidationError, "mission_elapsed_time_s"):
                validate_job_outputs(job, arithmetic_bad)

    def test_well_formed_scientific_noncompletion_is_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(
                policies=[{"policy_id": "eager_b1", "mode": "eager", "batch_size": 1}],
                trace_count=1,
                loads={"low": 0.1},
            )
            config_path = write_config(root, config)

            def noncompletion_executor(job, command, attempt_dir):
                write_valid_outputs(job, attempt_dir, all_completed=False)
                return 0

            result = CampaignOrchestrator(
                config_path, root, logical_cores=22, executor=noncompletion_executor
            ).run()
            self.assertEqual("completed", result[0].status)
            completed = root / config["campaign"]["output_root"] / "completed" / result[0].job_id
            summary = json.loads((completed / "trial_summary.json").read_text(encoding="utf-8"))
            self.assertFalse(summary["all_tasks_completed"])

    def test_resume_rehashes_outputs_and_rejects_post_completion_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(
                policies=[{"policy_id": "eager_b1", "mode": "eager", "batch_size": 1}],
                trace_count=1,
                loads={"low": 0.1},
            )
            config_path = write_config(root, config)

            def valid_executor(job, command, attempt_dir):
                write_valid_outputs(job, attempt_dir)
                return 0

            first_orchestrator = CampaignOrchestrator(
                config_path, root, logical_cores=22, executor=valid_executor
            )
            first = first_orchestrator.run()
            completed_dir = first_orchestrator.completed_dir(first_orchestrator.plan_jobs()[0])
            task_path = completed_dir / "task_events.csv"
            task_path.write_text(task_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

            def forbidden_executor(job, command, attempt_dir):
                self.fail("a conflicting completed path must never be overwritten")

            second = CampaignOrchestrator(
                config_path, root, logical_cores=22, executor=forbidden_executor
            ).run()
            self.assertEqual("failed", second[0].status)
            self.assertIn("completed path exists", second[0].message)

    def test_manifest_root_traversal_is_rejected_before_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config()
            config["manifest"]["root"] = "../escaped"
            config_path = write_config(root, config)
            with self.assertRaisesRegex(ValueError, "manifest.root escapes"):
                CampaignOrchestrator(config_path, root, logical_cores=22)
            self.assertFalse((root.parent / "escaped").exists())

    def test_source_changes_change_job_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "known_visit_sim" / "runner_piece.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            config_path = write_config(root, test_config(trace_count=1, loads={"low": 0.1}))
            first_orchestrator = CampaignOrchestrator(config_path, root, logical_cores=22)
            first_orchestrator.prepare_manifests()
            first_job = first_orchestrator.plan_jobs()[0]
            source.write_text("VALUE = 2\n", encoding="utf-8")
            second_orchestrator = CampaignOrchestrator(config_path, root, logical_cores=22)
            second_job = second_orchestrator.plan_jobs()[0]
            self.assertNotEqual(first_job.source_tree_sha256, second_job.source_tree_sha256)
            self.assertNotEqual(first_job.fingerprint, second_job.fingerprint)

    def test_schedule_is_deterministic_and_condition_balanced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(
                algorithms=["CBAA", "PI"], trace_count=2, loads={"low": 0.1, "high": 0.4}
            )
            config_path = write_config(root, config)
            first = CampaignOrchestrator(config_path, root, logical_cores=22)
            first.prepare_manifests()
            first_jobs = first.plan_jobs()
            second_jobs = CampaignOrchestrator(config_path, root, logical_cores=22).plan_jobs()
            self.assertEqual(
                [job.job_id for job in first_jobs], [job.job_id for job in second_jobs]
            )
            condition_count = len(config["campaign"]["algorithms"]) * len(
                config["campaign"]["loads"]
            ) * len(config["campaign"]["policies"])
            self.assertEqual(
                condition_count,
                len({job.condition_id for job in first_jobs[:condition_count]}),
            )

    def test_job_exclusions_are_exact_audited_and_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(
                algorithms=["CBAA", "PI"],
                trace_count=2,
                loads={"low": 0.1, "high": 0.4},
            )
            config["campaign"]["job_exclusions"] = [{
                "loads": ["low"],
                "policies": ["eager_b1"],
                "trace_ids": ["trace_0000"],
            }]
            config["campaign"]["expected_excluded_job_count"] = 2
            config_path = write_config(root, config)
            orchestrator = CampaignOrchestrator(config_path, root, logical_cores=22)
            orchestrator.prepare_manifests()
            jobs = orchestrator.plan_jobs()
            self.assertEqual(14, len(jobs))
            self.assertFalse(any(
                job.load_id == "low"
                and job.policy_id == "eager_b1"
                and job.trace_id == "trace_0000"
                for job in jobs
            ))
            self.assertEqual(
                [job.job_id for job in jobs],
                [
                    job.job_id
                    for job in CampaignOrchestrator(
                        config_path, root, logical_cores=22
                    ).plan_jobs()
                ],
            )

            config["campaign"]["expected_excluded_job_count"] = 3
            mismatch_path = write_config(root, config)
            mismatch = CampaignOrchestrator(mismatch_path, root, logical_cores=22)
            with self.assertRaisesRegex(ValueError, "exclusion count mismatch"):
                mismatch.plan_jobs()

    def test_dirty_git_tree_requires_explicit_development_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "known_visit_sim" / "piece.py"
            source.parent.mkdir(parents=True)
            source.write_text("VALUE = 1\n", encoding="utf-8")
            config = test_config(trace_count=1, loads={"low": 0.1})
            config_path = write_config(root, config)
            try:
                subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
                subprocess.run(
                    ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                     "commit", "-m", "fixture"],
                    cwd=root, check=True, capture_output=True,
                )
            except (OSError, subprocess.CalledProcessError) as error:
                self.skipTest(f"git unavailable for dirty-tree test: {error}")
            generated_output = root / config["campaign"]["output_root"] / "prior_run.txt"
            generated_output.parent.mkdir(parents=True)
            generated_output.write_text("not source\n", encoding="utf-8")
            clean_source = CampaignOrchestrator(config_path, root, logical_cores=22)
            self.assertFalse(clean_source.source_identity["git_dirty"])
            source.write_text("VALUE = 2\n", encoding="utf-8")
            orchestrator = CampaignOrchestrator(config_path, root, logical_cores=22)
            with self.assertRaisesRegex(RuntimeError, "refusing to run a dirty source tree"):
                orchestrator.run(job_limit=1)

    def test_keyboard_interrupt_terminates_child_and_preserves_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = test_config(
                policies=[{"policy_id": "eager_b1", "mode": "eager", "batch_size": 1}],
                trace_count=1,
                loads={"low": 0.1},
            )
            config_path = write_config(root, config)
            orchestrator = CampaignOrchestrator(config_path, root, logical_cores=22)
            orchestrator.command_for = lambda job, attempt_dir: [  # type: ignore[method-assign]
                sys.executable, "-c", "import time; time.sleep(30)"
            ]
            started = time.monotonic()
            with mock.patch(
                "study.campaign.concurrent.futures.as_completed", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    orchestrator.run()
            self.assertLess(time.monotonic() - started, 5.0)
            deadline = time.monotonic() + 3.0
            while orchestrator.process_controller.active_count and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(0, orchestrator.process_controller.active_count)
            attempt_root = orchestrator.output_root / "attempts"
            deadline = time.monotonic() + 3.0
            while not list(attempt_root.rglob("failure.json")) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(list(attempt_root.rglob("failure.json")))

    def test_default_executor_imports_checkout_from_repository_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo_root = Path(temporary)
            attempt_dir = repo_root / "nested" / "attempt"
            attempt_dir.mkdir(parents=True)
            (repo_root / "local_probe.py").write_text("VALUE = 'checkout-imported'\n", encoding="utf-8")
            job = SimpleNamespace(python_hash_seed=13579)
            return_code = _default_executor(
                job,
                [
                    sys.executable, "-c",
                    "import os,local_probe; print(local_probe.VALUE, os.environ['PYTHONHASHSEED'])",
                ],
                attempt_dir,
                repo_root,
            )
            self.assertEqual(0, return_code)
            self.assertEqual(
                "checkout-imported 13579", (attempt_dir / "stdout.log").read_text().strip()
            )


if __name__ == "__main__":
    unittest.main()
