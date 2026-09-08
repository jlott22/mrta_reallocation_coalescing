from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import tempfile
import unittest
from pathlib import Path

from study.causal.analysis import analyze, friedman_test, holm_adjust, wilcoxon_signed_rank
from study.causal.calibration import (
    summarize_rate_calibration,
    summarize_timeout_calibration,
    summarize_variance_pilot,
)
from study.causal.gates import GateError, validate_gate
from study.causal.model import PUBLICATION_WORKER_COUNT, load_causal_config
from study.causal.orchestrator import _claim_board_lock, _release_board_lock
from study.causal.schedule import plan_paired_blocks, validate_schedule
from study.manifests import canonical_json_bytes, generate_manifest_set


SHA_A = "a" * 64
SHA_B = "b" * 64


class CausalStudyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest = {
            "manifest_set_id": "causal_test_manifests",
            "root": "study/generated/manifests",
            "master_seed": 17,
            "trace_count": 5,
            "grid_size": 19,
            "robot_count": 4,
            "task_count": 50,
            "initial_task_count": 8,
            "arrival_loads": {"low": 0.1, "medium": 0.5, "high": 1.0},
        }
        generate_manifest_set({"manifest": self.manifest}, self.root)
        self.config_path = self.root / "configs" / "test.json"
        self.config_path.parent.mkdir(parents=True)
        self.config = self._config()
        self.config_path.write_bytes(canonical_json_bytes(self.config))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _boards(self, count: int = PUBLICATION_WORKER_COUNT) -> list[dict]:
        return [
            {
                "board_id": f"board_{index}",
                "serial_device": f"/dev/serial/by-id/test-{index}",
                "expected_device_uid": f"uid_{index}",
                "expected_build_id": "test_build_v1",
                "expected_firmware_sha256": f"{index + 1:064x}",
                "expected_module_set_sha256": f"{index + 11:064x}",
            }
            for index in range(count)
        ]

    def _config(self) -> dict:
        return {
            "schema_version": 1,
            "manifest": self.manifest,
            "campaign": {
                "campaign_id": "causal_test",
                "stage": "development",
                "output_root": "study/output/causal_test",
                "algorithms": ["CBAA", "ACBBA", "PI", "HIPC"],
                "loads": ["low", "medium", "high"],
                "policies": [
                    {"policy_id": "eager_b1", "mode": "eager", "batch_size": 1},
                    {"policy_id": "count_b2", "mode": "count", "batch_size": 2},
                    {"policy_id": "count_b4", "mode": "count", "batch_size": 4},
                    {"policy_id": "count_b8", "mode": "count", "batch_size": 8},
                    {"policy_id": "bounded_b4_w5", "mode": "bounded", "batch_size": 4, "max_pending_age_s": 5.0},
                ],
                "schedule_seed": 44,
                "max_technical_retries": 1,
            },
            "hardware": {
                "boards": self._boards(),
                "core_affinities": list(range(PUBLICATION_WORKER_COUNT)),
                "development_override": True,
                "provider_factory": "study.causal.worker:create_timing_provider",
            },
        }

    def test_paired_blocks_remain_on_one_board_and_are_balanced(self) -> None:
        config = load_causal_config(self.config_path, self.root)
        blocks = plan_paired_blocks(config)
        self.assertEqual(4 * 3 * 5, len(blocks))
        self.assertEqual(300, sum(len(block.jobs) for block in blocks))
        board_counts = {}
        orders = set()
        for block in blocks:
            self.assertEqual(1, len({job.board_id for job in block.jobs}))
            self.assertEqual(block.board.board_id, block.jobs[0].board_id)
            self.assertEqual(5, len({job.policy.policy_id for job in block.jobs}))
            board_counts[block.board.board_id] = board_counts.get(block.board.board_id, 0) + 1
            orders.add(tuple(job.policy.policy_id for job in block.jobs))
        self.assertLessEqual(max(board_counts.values()) - min(board_counts.values()), 1)
        self.assertEqual(5, len(orders))
        summary = validate_schedule(config, blocks)
        self.assertEqual(60, summary["block_count"])
        self.assertEqual(300, summary["job_count"])

    def test_schedule_is_byte_deterministic_and_zero_pair_ids_are_distinct(self) -> None:
        config = load_causal_config(self.config_path, self.root)
        first = plan_paired_blocks(config)
        second = plan_paired_blocks(config)
        self.assertEqual(first, second)
        zeros = plan_paired_blocks(config, zero_compute=True)
        for causal, zero in zip(first, zeros, strict=True):
            self.assertEqual(causal.block_id, zero.block_id)
            self.assertEqual(
                [job.job_id + "__zero" for job in causal.jobs],
                [job.job_id for job in zero.jobs],
            )

    def test_continuation_excludes_only_sealed_complete_pairs(self) -> None:
        original_config = load_causal_config(self.config_path, self.root)
        original = plan_paired_blocks(original_config)
        excluded = original[0]
        prior_root = self.root / "study" / "output" / "prior"
        rows = []
        for job in excluded.jobs:
            marker = prior_root / "causal" / "completed" / job.job_id / "completion.json"
            marker.parent.mkdir(parents=True)
            summary = marker.parent / "trial_summary.json"
            summary.write_bytes(canonical_json_bytes({"job_id": job.job_id}))
            marker.write_bytes(canonical_json_bytes({
                "job_id": job.job_id,
                "required_output_sha256": {
                    "trial_summary.json": hashlib.sha256(
                        summary.read_bytes()
                    ).hexdigest(),
                },
            }))
            rows.append({
                "job_id": job.job_id,
                "completion_json_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
            })
        manifest = {
            "schema_version": 1,
            "report_kind": "hardware_continuation_predecessor_manifest",
            "prior_output_root": "study/output/prior",
            "completed_job_count": len(rows),
            "completed_jobs": rows,
        }
        manifest_path = self.root / "configs" / "prior.json"
        manifest_path.write_bytes(canonical_json_bytes(manifest))
        config_value = self._config()
        config_value["campaign"]["continuation"] = {
            "completion_manifest": "configs/prior.json",
            "completion_manifest_sha256": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
            "excluded_completed_blocks": [excluded.block_id],
        }
        self.config_path.write_bytes(canonical_json_bytes(config_value))

        continuation = load_causal_config(self.config_path, self.root)
        remaining = plan_paired_blocks(continuation)
        self.assertNotIn(excluded.block_id, {block.block_id for block in remaining})
        self.assertEqual(295, sum(len(block.jobs) for block in remaining))
        original_orders = {
            block.block_id: tuple(job.policy.policy_id for job in block.jobs)
            for block in original
        }
        self.assertTrue(all(
            tuple(job.policy.policy_id for job in block.jobs)
            == original_orders[block.block_id]
            for block in remaining
        ))

        marker = prior_root / "causal" / "completed" / rows[0]["job_id"] / "completion.json"
        marker.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "marker changed"):
            load_causal_config(self.config_path, self.root)

    def test_exact_job_allowlist_supports_partial_blocks_and_seals_successes(self) -> None:
        original_config = load_causal_config(self.config_path, self.root)
        original = plan_paired_blocks(original_config)
        original_jobs = [job for block in original for job in block.jobs]
        retry_jobs = (original[0].jobs[0], original[1].jobs[1])
        retry_ids = {job.job_id for job in retry_jobs}
        completed_ids = [job.job_id for job in original_jobs if job.job_id not in retry_ids]

        checkpoint = self.root / "corrected_hardware_v9_v10_progress"
        checkpoint.mkdir()
        failures_path = checkpoint / "terminal_failures.csv"
        with failures_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["job_id"])
            writer.writeheader()
            writer.writerows({"job_id": value} for value in sorted(retry_ids))
        completions_path = checkpoint / "completion_records.jsonl.gz"
        with gzip.open(completions_path, "wt", encoding="utf-8") as handle:
            for job_id in completed_ids:
                handle.write(json.dumps({"job_id": job_id}) + "\n")

        def digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        export = {
            "bundle_id": "corrected_hardware_v9_v10_progress",
            "planned_trials": len(original_jobs),
            "completed_trials": len(completed_ids),
            "remaining_terminal_failed_trials": len(retry_ids),
            "files": {
                "terminal_failures.csv": {"sha256": digest(failures_path)},
                "completion_records.jsonl.gz": {"sha256": digest(completions_path)},
            },
        }
        export_path = checkpoint / "export_manifest.json"
        export_path.write_bytes(canonical_json_bytes(export))
        config_value = self._config()
        config_value["campaign"]["job_allowlist"] = {
            "path": "corrected_hardware_v9_v10_progress/terminal_failures.csv",
            "sha256": digest(failures_path),
            "predecessor_export_manifest": (
                "corrected_hardware_v9_v10_progress/export_manifest.json"
            ),
            "predecessor_export_manifest_sha256": digest(export_path),
            "expected_planned_jobs": len(original_jobs),
            "expected_completed_jobs": len(completed_ids),
            "expected_retry_jobs": len(retry_ids),
        }
        self.config_path.write_bytes(canonical_json_bytes(config_value))

        retry_config = load_causal_config(self.config_path, self.root)
        retry_blocks = plan_paired_blocks(retry_config)
        scheduled = [job for block in retry_blocks for job in block.jobs]
        self.assertEqual(retry_ids, {job.job_id for job in scheduled})
        original_identity = {job.job_id: job.identity() for job in original_jobs}
        self.assertTrue(
            all(
                job.identity() == original_identity[job.job_id]
                for job in scheduled
            )
        )
        summary = validate_schedule(retry_config, retry_blocks)
        self.assertEqual(2, summary["allowlisted_job_count"])
        self.assertEqual(len(completed_ids), summary["sealed_completed_job_count"])

        with failures_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{completed_ids[0]}\n")
        with self.assertRaisesRegex(ValueError, "allowlist SHA-256 mismatch"):
            load_causal_config(self.config_path, self.root)

    def test_priority_trace_blocks_run_first_without_changing_balance(self) -> None:
        config_value = self._config()
        config_value["campaign"]["priority_trace_count"] = 1
        self.config_path.write_bytes(canonical_json_bytes(config_value))
        config = load_causal_config(self.config_path, self.root)
        blocks = plan_paired_blocks(config)
        priority_count = 4 * 3
        self.assertEqual(
            {"trace_0000"},
            {block.trace_id for block in blocks[:priority_count]},
        )
        self.assertNotIn(
            "trace_0000",
            {block.trace_id for block in blocks[priority_count:]},
        )
        summary = validate_schedule(config, blocks)
        self.assertEqual(60, summary["block_count"])

    def test_native_config_fails_closed_on_board_and_path_errors(self) -> None:
        bad = self._config()
        bad["hardware"]["development_override"] = False
        bad["hardware"]["boards"] = self._boards(PUBLICATION_WORKER_COUNT - 1)
        bad["hardware"]["core_affinities"] = list(
            range(PUBLICATION_WORKER_COUNT - 1)
        )
        self.config_path.write_bytes(canonical_json_bytes(bad))
        with self.assertRaisesRegex(
            ValueError, f"exactly {PUBLICATION_WORKER_COUNT}"
        ):
            load_causal_config(self.config_path, self.root)
        bad = self._config()
        bad["campaign"]["output_root"] = "../donor"
        self.config_path.write_bytes(canonical_json_bytes(bad))
        with self.assertRaisesRegex(ValueError, "escapes"):
            load_causal_config(self.config_path, self.root)

    def test_causal_campaign_rejects_out_of_design_algorithms(self) -> None:
        bad = self._config()
        bad["campaign"]["algorithms"].append("DGA")
        self.config_path.write_bytes(canonical_json_bytes(bad))
        with self.assertRaisesRegex(ValueError, "must be exactly"):
            load_causal_config(self.config_path, self.root)

    def test_external_binding_bytes_are_part_of_effective_config_hash(self) -> None:
        config = self._config()
        bindings = {"schema_version": 1, "boards": config["hardware"].pop("boards"), "core_affinities": config["hardware"].pop("core_affinities")}
        path = self.root / "configs" / "local" / "bindings.json"
        path.parent.mkdir()
        path.write_bytes(canonical_json_bytes(bindings))
        config["hardware"]["bindings_file"] = "configs/local/bindings.json"
        self.config_path.write_bytes(canonical_json_bytes(config))
        first = load_causal_config(self.config_path, self.root)
        bindings["boards"][0]["serial_device"] = "/dev/serial/by-id/changed"
        path.write_bytes(canonical_json_bytes(bindings))
        second = load_causal_config(self.config_path, self.root)
        self.assertNotEqual(first.config_sha256, second.config_sha256)
        self.assertNotEqual(first.hardware_binding_sha256, second.hardware_binding_sha256)

    def test_inline_frozen_config_preserves_sealed_binding_identity(self) -> None:
        config = self._config()
        config["hardware"]["development_override"] = False
        config["hardware"]["boards"] = config["hardware"]["boards"][:PUBLICATION_WORKER_COUNT]
        config["hardware"]["core_affinities"] = list(range(PUBLICATION_WORKER_COUNT))
        config["hardware"]["bindings_file_sha256"] = SHA_A
        self.config_path.write_bytes(canonical_json_bytes(config))
        loaded = load_causal_config(self.config_path, self.root)
        self.assertEqual(SHA_A, loaded.hardware_binding_sha256)
        config["hardware"]["bindings_file_sha256"] = "not-a-digest"
        self.config_path.write_bytes(canonical_json_bytes(config))
        with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
            load_causal_config(self.config_path, self.root)

    def test_board_lock_prevents_cross_worker_use(self) -> None:
        board = load_causal_config(self.config_path, self.root).boards[0]
        descriptor, path = _claim_board_lock(self.root, board, 0)
        try:
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                _claim_board_lock(self.root, board, 1)
        finally:
            _release_board_lock(descriptor, path)
        descriptor, path = _claim_board_lock(self.root, board, 1)
        _release_board_lock(descriptor, path)

    def test_development_gate_cannot_satisfy_scientific_freeze(self) -> None:
        path = self.root / "gate.json"
        path.write_bytes(canonical_json_bytes({
            "schema_version": 1,
            "report_kind": "native_environment_check",
            "scientifically_valid": False,
            "development_only_pass": True,
        }))
        with self.assertRaises(GateError):
            validate_gate(path)

    def test_friedman_and_wilcoxon_known_cases(self) -> None:
        identical = friedman_test([[1.0, 1.0, 1.0] for _ in range(8)])
        self.assertEqual(0.0, identical["statistic"])
        self.assertEqual(1.0, identical["p_value"])
        separated = friedman_test([[1.0, 2.0, 3.0] for _ in range(10)])
        self.assertGreater(separated["statistic"], 15.0)
        self.assertLess(separated["p_value"], 0.001)
        signed = wilcoxon_signed_rank([1.0] * 10)
        self.assertAlmostEqual(2 / 1024, signed["p_value"])
        self.assertEqual([0.03, 0.04, 0.04], holm_adjust([0.01, 0.04, 0.02]))

    def _native_rows(self, rates: dict[str, float], traces: int = 5) -> list[dict]:
        rows = []
        for algorithm in ("CBAA", "ACBBA", "PI", "HIPC"):
            for load_index, load in enumerate(rates):
                for trace in range(traces):
                    for policy in ("eager_b1", "count_b4"):
                        board_index = (
                            ("CBAA", "ACBBA", "PI", "HIPC").index(algorithm)
                            + load_index + trace
                        ) % PUBLICATION_WORKER_COUNT
                        rows.append({
                            "algorithm": algorithm,
                            "arrival_load": load,
                            "trace_id": f"trace_{trace:04d}",
                            "policy_id": policy,
                            "hardware_validated": True,
                            "parity_passed": True,
                            "all_tasks_completed": True,
                            "rp2040_allocator_processor_work_s": 10.0 + (1000 if load == "low" else 0),
                            "agx_allocator_processor_work_s": 5.0,
                            "mission_elapsed_time_s": 100.0,
                            "median_release_to_first_assignment_latency_s": load_index + 0.2,
                            "median_release_to_completion_latency_s": 4.0,
                            "p95_release_to_completion_latency_s": 8.0,
                            "allocation_epoch_count": 20,
                            "arrival_driven_event_count": 10,
                            "mandatory_event_count": 10,
                            "allocator_call_count": 80,
                            "mean_pending_queue_depth": load_index,
                            "max_pending_queue_depth": load_index + 1,
                            "config_sha256": "c" * 64,
                            "manifest_index_sha256": "d" * 64,
                            "hardware_binding_sha256": "e" * 64,
                            "git_head": "f" * 40,
                            "source_tree_sha256": "1" * 64,
                            "campaign_execution_report_sha256": "4" * 64,
                            "schedule_sha256": "5" * 64,
                            "completion_marker_sha256": hashlib.sha256(
                                f"completion:{algorithm}:{load}:{trace}:{policy}".encode()
                            ).hexdigest(),
                            "trial_summary_sha256": hashlib.sha256(
                                f"summary:{algorithm}:{load}:{trace}:{policy}".encode()
                            ).hexdigest(),
                            "required_output_sha256": {
                                "trial_summary.json": hashlib.sha256(
                                    f"raw:{algorithm}:{load}:{trace}:{policy}".encode()
                                ).hexdigest()
                            },
                            "board_id": f"board_{board_index}",
                            "expected_device_uid": f"uid_{board_index}",
                            "expected_device_build_id": "build_v1",
                            "expected_device_firmware_sha256": "2" * 64,
                            "expected_device_module_set_sha256": "3" * 64,
                            "_path": "synthetic",
                        })
        return rows

    def test_rate_regime_proposal_does_not_optimize_processor_work(self) -> None:
        rates = {"low": 0.03, "mid1": 0.15, "mid2": 0.3, "high": 1.2}
        report = summarize_rate_calibration(
            self._native_rows(rates),
            rates,
            reviewed_selection={"low": "low", "medium": "mid2", "high": "high"},
            reviewed_selection_justification=(
                "Synthetic fixture deliberately exercises a reviewed override."
            ),
        )
        self.assertTrue(report["passed"])
        self.assertEqual(0.03, report["selection_proposal"]["low"]["rate_per_s"])
        self.assertFalse(report["regime_definition"]["selection_uses_processor_work"])
        self.assertEqual(0.3, report["reviewed_selection"]["medium"]["rate_per_s"])
        with self.assertRaisesRegex(ValueError, "requires a nonempty scientific justification"):
            summarize_rate_calibration(
                self._native_rows(rates),
                rates,
                reviewed_selection={
                    "low": "low", "medium": "mid2", "high": "high"
                },
            )

    def test_variance_keeps_trial_as_replicate_and_requires_review(self) -> None:
        rates = {"low": 0.1, "medium": 0.5, "high": 1.0}
        report = summarize_variance_pilot(self._native_rows(rates, traces=10))
        self.assertTrue(report["passed"])
        self.assertTrue(report["paired_trial_is_the_replicate"])
        self.assertIsNone(report["reviewed_trace_count"])
        reviewed = summarize_variance_pilot(
            self._native_rows(rates, traces=10),
            reviewed_trace_count=25,
            reviewed_trace_count_justification=(
                "Synthetic fixture exercises an explicitly documented review override."
            ),
        )
        self.assertEqual(25, reviewed["reviewed_trace_count"])
        with self.assertRaisesRegex(ValueError, "predeclared choices"):
            summarize_variance_pilot(
                self._native_rows(rates, traces=10), reviewed_trace_count=1
            )

    def _timeout_rows(self) -> list[dict]:
        rows = self._native_rows({"low": 0.1, "medium": 0.5, "high": 1.0})
        eager = [row for row in rows if row["policy_id"] == "eager_b1"]
        templates = [row for row in rows if row["policy_id"] == "count_b4"]
        bounded = []
        for timeout in (2.0, 5.0, 10.0, 20.0):
            for source in templates:
                row = dict(source)
                row["policy_id"] = f"bounded_b4_w{int(timeout)}"
                row["policy_max_pending_age_s"] = timeout
                row["rp2040_allocator_processor_work_s"] = 9.0
                identity = (
                    f"{row['algorithm']}:{row['arrival_load']}:"
                    f"{row['trace_id']}:{row['policy_id']}"
                )
                row["completion_marker_sha256"] = hashlib.sha256(
                    f"completion:{identity}".encode()
                ).hexdigest()
                row["trial_summary_sha256"] = hashlib.sha256(
                    f"summary:{identity}".encode()
                ).hexdigest()
                row["required_output_sha256"] = {
                    "trial_summary.json": hashlib.sha256(identity.encode()).hexdigest()
                }
                bounded.append(row)
        for row in eager:
            row["policy_max_pending_age_s"] = None
        return eager + bounded

    def test_timeout_requires_exact_crossed_matrix_and_ignores_incomplete_work(self) -> None:
        rows = self._timeout_rows()
        incomplete = next(
            row for row in rows
            if row["policy_id"] == "bounded_b4_w5"
            and row["arrival_load"] == "medium"
        )
        incomplete["all_tasks_completed"] = False
        incomplete["rp2040_allocator_processor_work_s"] = 0.001
        incomplete["mission_elapsed_time_s"] = None
        report = summarize_timeout_calibration(
            rows,
            reviewed_timeout_s=5.0,
            reviewed_timeout_justification=(
                "Synthetic fixture deliberately exercises a reviewed override."
            ),
        )
        self.assertTrue(report["passed"])
        w5 = next(row for row in report["by_timeout_candidate"] if row["timeout_s"] == 5.0)
        self.assertEqual(59, w5["complete_eager_pair_count"])
        self.assertGreater(w5["median_medium_high_percent_processor_work_saved_vs_eager"], 0.0)
        with self.assertRaisesRegex(ValueError, "requires a nonempty scientific justification"):
            summarize_timeout_calibration(rows, reviewed_timeout_s=5.0)
        with self.assertRaisesRegex(ValueError, "complete crossed paired matrix"):
            summarize_timeout_calibration(rows[:-1])

    def test_variance_refuses_to_freeze_from_censored_pairs(self) -> None:
        rows = self._native_rows(
            {"low": 0.1, "medium": 0.5, "high": 1.0}, traces=10
        )
        rows[0]["all_tasks_completed"] = False
        rows[0]["mission_elapsed_time_s"] = None
        report = summarize_variance_pilot(rows)
        self.assertFalse(report["passed"])
        self.assertIsNone(report["trace_count_proposal"])
        with self.assertRaisesRegex(ValueError, "censored/incomplete"):
            summarize_variance_pilot(rows, reviewed_trace_count=25)

    def test_rate_matrix_rejects_a_missing_algorithm_policy_trace_cell(self) -> None:
        rates = {"r0": 0.03, "r1": 0.075, "r2": 0.15}
        rows = self._native_rows(rates)
        with self.assertRaisesRegex(ValueError, "complete crossed paired matrix"):
            summarize_rate_calibration(rows[:-1], rates)

    def test_analysis_bundle_is_immutable_and_includes_step_deltas(self) -> None:
        causal_root = self.root / "causal"
        zero_root = self.root / "zero"
        output = self.root / "analysis"
        common = {
            "algorithm": "CBAA",
            "arrival_load": "medium",
            "trace_id": "trace_0000",
            "scenario_sha256": "1" * 64,
            "release_sha256": "2" * 64,
            "runtime_seed": 17,
            "config_sha256": "3" * 64,
            "manifest_index_sha256": "4" * 64,
            "hardware_binding_sha256": "5" * 64,
            "git_head": "6" * 40,
            "source_tree_sha256": "7" * 64,
            "board_id": "board_0",
            "expected_device_uid": "uid_0",
            "expected_device_build_id": "build_v1",
            "expected_device_firmware_sha256": "8" * 64,
            "expected_device_module_set_sha256": "9" * 64,
            "technical_status": "completed",
            "all_tasks_completed": True,
            "hardware_validated": True,
            "parity_passed": True,
            "agx_allocator_processor_work_s": 2.0,
            "median_release_to_first_assignment_latency_s": 2.0,
            "p95_release_to_first_assignment_latency_s": 3.0,
            "median_release_to_completion_latency_s": 8.0,
            "p95_release_to_completion_latency_s": 12.0,
            "mission_elapsed_time_s": 100.0,
            "allocation_epoch_count": 10,
            "arrival_driven_event_count": 4,
            "mandatory_event_count": 5,
            "piggybacked_admission_event_count": 1,
            "timeout_trigger_count": 0,
            "batch_threshold_trigger_count": 1,
            "final_flush_event_count": 1,
            "allocator_call_count": 40,
            "processor_capacity_fraction": 0.01,
            "completed_task_count": 50,
        }
        policies = (
            ("eager_b1", "eager", 1, 10.0, 50, 100),
            ("count_b4", "count", 4, 8.0, 46, 94),
        )
        for policy_id, mode, batch, work, max_steps, team_steps in policies:
            row = dict(common) | {
                "policy_id": policy_id,
                "policy_mode": mode,
                "policy_batch_size": batch,
                "policy_max_pending_age_s": None,
                "rp2040_allocator_processor_work_s": work,
                "max_robot_steps": max_steps,
                "total_team_steps": team_steps,
                "zero_compute": False,
            }
            zero = dict(row) | {
                "zero_compute": True,
                "hardware_validated": False,
                "mission_elapsed_time_s": 99.0,
            }
            for root, value in ((causal_root, row), (zero_root, zero)):
                path = root / policy_id / "trial_summary.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(canonical_json_bytes(value))
        paths = analyze(causal_root, zero_root, output)
        with paths["paired"].open(newline="", encoding="utf-8") as handle:
            paired = list(csv.DictReader(handle))
        b4 = next(row for row in paired if row["policy_id"] == "count_b4")
        self.assertEqual(-4.0, float(b4["paired_change_max_robot_steps"]))
        self.assertEqual(-6.0, float(b4["paired_change_total_team_steps"]))
        analyze(causal_root, zero_root, output)
        changed = causal_root / "count_b4" / "trial_summary.json"
        value = json.loads(changed.read_text(encoding="utf-8"))
        value["max_robot_steps"] = 45
        changed.write_bytes(canonical_json_bytes(value))
        with self.assertRaisesRegex(FileExistsError, "different analysis bundle"):
            analyze(causal_root, zero_root, output)


if __name__ == "__main__":
    unittest.main()
