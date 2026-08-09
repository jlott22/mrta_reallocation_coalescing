from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from allocator_replay.capture.codec import read_trace
from allocator_replay.device.build import build_device_bundle
from allocator_replay.hil.bridge import make_proxy_allocator
from allocator_replay.hil.campaign import HilCampaignRunner
from allocator_replay.hil.manifest import (
    BAYESIAN_MIN_POST_CLUE_CALLS,
    _sample,
    invalidate_campaign,
    prepare_campaign,
    verify_campaign_provenance,
)
from allocator_replay.hil.report import rebuild_hil_reports
from allocator_replay.hil.watchdog import HilTrialFailure
from allocator_replay.host.emulator import LoopbackReplayDevice


class AuthoritativeProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        manifest = build_device_bundle()
        cls.build_root = Path(str(manifest["output"]))

    def _fixture(self) -> dict:
        trace_root = (
            Path("Results")
            / "HIL"
            / "AllocatorReplay"
            / "Traces"
            / "collaborative"
            / "collaborative_cbaa_topk_fixed_k1"
        )
        trace = next(iter(sorted(trace_root.glob("trial_*.jsonl.gz"))))
        captured = next(read_trace(trace))
        return {
            "schema": 1,
            "fixture_id": captured["fixture_id"] + "/authoritative-test",
            "condition_id": captured["condition_id"],
            "mission": captured["mission"],
            "algorithm": captured["algorithm"],
            "pre_state": captured["pre_state"],
        }

    def test_chunked_loopback_returns_actual_state_and_timing(self) -> None:
        device = LoopbackReplayDevice("hil-loopback", build_root=self.build_root)
        try:
            result = device.execute_authoritative(
                self._fixture(),
                "hil-attempt-1",
                30.0,
            )
        finally:
            device.close()
        self.assertEqual(result["status"], "completed")
        self.assertIn("goal", result)
        self.assertIn("messages", result)
        self.assertIn("post_state", result)
        self.assertIn("robot_attrs", result["post_state"])
        self.assertGreaterEqual(result["allocator_time_us"], 0)
        self.assertGreaterEqual(result["candidate_filter_time_us"], 0)
        self.assertEqual(
            result["allocator_exclusive_time_us"],
            max(
                0,
                result["allocator_time_us"]
                - result["candidate_filter_time_us"],
            ),
        )

    def test_pre_timing_device_failure_is_not_transport_disconnect(self) -> None:
        fixture = self._fixture()
        fixture["pre_state"]["robot_attrs"]["rid"] = {
            "@": "dict",
            "v": [["@", "invalid-test-tag"]],
        }
        device = LoopbackReplayDevice(
            "hil-pre-timing-failure",
            build_root=self.build_root,
        )
        try:
            result = device.execute_authoritative(
                fixture,
                "hil-attempt-pre-timing-failure",
                30.0,
            )
        finally:
            device.close()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_type"], "device_exception")
        self.assertIn("robot_construct", result["error"])
        self.assertNotIn("allocator_time_us", result)


class HilManifestAndReportTests(unittest.TestCase):
    def test_prepare_is_deterministic_and_has_full_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "hil"
            with patch("allocator_replay.hil.manifest.HIL_ROOT", root):
                first = prepare_campaign("test")
                first_value = json.loads(
                    (first / "manifest.json").read_text(encoding="utf-8")
                )
                second = prepare_campaign("test")
                second_value = json.loads(
                    (second / "manifest.json").read_text(encoding="utf-8")
                )
                implementation = first_value["implementation"]
                verified = verify_campaign_provenance(
                    first,
                    build_root=Path(
                        implementation["device_build"]["manifest_path"]
                    ).parent,
                    device_build_ids=[
                        implementation["device_build"]["build_id"],
                    ],
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "connected_device_builds",
                ):
                    verify_campaign_provenance(
                        first,
                        build_root=Path(
                            implementation["device_build"]["manifest_path"]
                        ).parent,
                        device_build_ids=["wrong-build"],
                    )
        self.assertEqual(first_value["selected_trials"], second_value["selected_trials"])
        self.assertEqual(first_value["condition_count"], 96)
        self.assertEqual(first_value["mission_run_count"], 1680)
        self.assertEqual(len(first_value["selected_trials"]["bayesian"]), 25)
        self.assertEqual(len(first_value["selected_trials"]["collaborative"]), 10)
        self.assertEqual(first_value["schema"], 2)
        implementation = first_value["implementation"]
        self.assertEqual(
            implementation["implementation_id"],
            "pololu_native_persistent_hil_v2",
        )
        self.assertEqual(
            implementation["device_build"]["build_id"],
            json.loads(
                Path(
                    implementation["device_build"]["manifest_path"]
                ).read_text(encoding="utf-8")
            )["build_id"],
        )
        self.assertEqual(len(implementation["host_source"]["sha256"]), 64)
        self.assertEqual(
            len(implementation["simulator_sources"]["bayesian"]["sha256"]),
            64,
        )
        self.assertTrue(all(verified["checks"].values()))
        eligibility = first_value["trial_eligibility"]["bayesian"]
        self.assertEqual(eligibility["required_clue_condition_count"], 36)
        self.assertGreaterEqual(eligibility["eligible_trial_count"], 25)
        selected_evidence = eligibility["selected_trial_evidence"]
        self.assertEqual(len(selected_evidence), 25)
        for trial_id in first_value["selected_trials"]["bayesian"]:
            evidence = selected_evidence[str(trial_id)]
            self.assertEqual(evidence["clue_condition_count"], 36)
            self.assertGreaterEqual(
                evidence["minimum_post_clue_allocator_calls"],
                BAYESIAN_MIN_POST_CLUE_CALLS,
            )

    def test_sampling_rejects_insufficient_eligible_trials(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot sample 25"):
            _sample(list(range(24)), 25, 20260727)

    def test_report_excludes_interrupted_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "journals").mkdir()
            (root / "reports").mkdir()
            schedule = {
                "campaign_id": "report-test",
                "jobs": [
                    {
                        "condition_id": "bayesian_cbaa_topk_fixed_k1",
                        "mission": "bayesian",
                        "algorithm": "CBAA",
                        "top_k_level": "K=1",
                        "top_k_rate": 1 / 361,
                        "top_k_cells": 1,
                        "status": "complete",
                        "stopped_reason": "",
                        "trial_ids": [1],
                        "completed_trials": [1],
                        "historical_system_csv": "",
                    }
                ],
            }
            (root / "schedule.json").write_text(
                json.dumps(schedule),
                encoding="utf-8",
            )
            manifest = {
                **schedule,
                "schema": 2,
                "implementation": {
                    "implementation_id": "test-native-persistent-v2",
                    "device_build": {
                        "build_id": "build-test",
                        "manifest_sha256": "build-manifest-hash",
                        "source_bundle_sha256": "source-bundle-hash",
                        "deployed_module_set_sha256": "module-set-hash",
                    },
                    "host_source": {"sha256": "host-source-hash"},
                    "simulator_sources": {
                        "bayesian": {"sha256": "sim-source-hash"}
                    },
                },
                "scenario_sources": {
                    "bayesian": {"sha256": "scenario-hash"}
                },
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            rows = [
                {
                    "record_type": "call_attempt",
                    "condition_id": "bayesian_cbaa_topk_fixed_k1",
                    "mission": "bayesian",
                    "algorithm": "CBAA",
                    "top_k_level": "K=1",
                    "top_k_rate": 1 / 361,
                    "top_k_cells": 1,
                    "trial_id": 1,
                    "run_generation": 1,
                    "robot_id": "00",
                    "fixture_id": "old",
                    "accepted": True,
                    "allocator_time_us": 999,
                    "candidate_filter_time_us": 0,
                    "allocator_exclusive_time_us": 999,
                    "candidate_filter_calls": 0,
                    "device_id": "dev",
                },
                {
                    "record_type": "call_attempt",
                    "condition_id": "bayesian_cbaa_topk_fixed_k1",
                    "mission": "bayesian",
                    "algorithm": "CBAA",
                    "top_k_level": "K=1",
                    "top_k_rate": 1 / 361,
                    "top_k_cells": 1,
                    "trial_id": 1,
                    "run_generation": 2,
                    "robot_id": "00",
                    "fixture_id": "new",
                    "accepted": True,
                    "allocator_time_us": 10,
                    "candidate_filter_time_us": 2,
                    "allocator_exclusive_time_us": 8,
                    "candidate_filter_calls": 1,
                    "device_id": "dev",
                },
                {
                    "record_type": "trial_complete",
                    "condition_id": "bayesian_cbaa_topk_fixed_k1",
                    "trial_id": 1,
                    "run_generation": 2,
                    "scenario_file": "scenario.csv",
                    "scenario_sha256": "hash",
                    "trial_status": "completed",
                    "total_team_steps": 12,
                    "max_steps_any_robot": 4,
                    "events_processed": 20,
                },
            ]
            journal = root / "journals" / "dev.jsonl"
            journal.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            summary = rebuild_hil_reports(root)
            self.assertEqual(summary["accepted_calls"], 1)
            with (root / "reports" / "system_trial_metrics.csv").open(
                encoding="utf-8"
            ) as handle:
                system = list(__import__("csv").DictReader(handle))
            self.assertEqual(system[0]["allocator_time_max_us"], "10.0")
            self.assertEqual(
                system[0]["implementation_id"],
                "test-native-persistent-v2",
            )
            self.assertEqual(system[0]["campaign_build_id"], "build-test")
            self.assertEqual(
                system[0]["simulator_source_sha256"],
                "sim-source-hash",
            )
            self.assertEqual(system[0]["scenario_sha256"], "hash")

    def test_report_omits_excluded_condition_from_all_core_csvs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "journals").mkdir()
            jobs = []
            records = []
            for condition_id, excluded in (
                ("bayesian_cbaa_topk_fixed_k1", True),
                ("bayesian_cbaa_topk_003_k11", False),
            ):
                jobs.append({
                    "condition_id": condition_id,
                    "mission": "bayesian",
                    "algorithm": "CBAA",
                    "top_k_level": "K=1" if excluded else "3%",
                    "top_k_rate": 1 / 361 if excluded else 0.03,
                    "top_k_cells": 1 if excluded else 11,
                    "status": "excluded" if excluded else "complete",
                    "excluded_from_core": excluded,
                    "trial_ids": [1],
                    "completed_trials": [1],
                    "stopped_reason": "",
                })
                records.extend([
                    {
                        "record_type": "call_attempt",
                        "condition_id": condition_id,
                        "mission": "bayesian",
                        "algorithm": "CBAA",
                        "top_k_level": "K=1" if excluded else "3%",
                        "top_k_rate": 1 / 361 if excluded else 0.03,
                        "top_k_cells": 1 if excluded else 11,
                        "trial_id": 1,
                        "run_generation": 1,
                        "robot_id": "00",
                        "fixture_id": condition_id,
                        "accepted": True,
                        "allocator_time_us": 10,
                        "candidate_filter_time_us": 2,
                        "allocator_exclusive_time_us": 8,
                        "candidate_filter_calls": 1,
                        "device_id": "dev",
                    },
                    {
                        "record_type": "trial_complete",
                        "condition_id": condition_id,
                        "trial_id": 1,
                        "run_generation": 1,
                        "scenario_file": "scenario.csv",
                        "scenario_sha256": "hash",
                        "trial_status": "completed",
                        "total_team_steps": 4,
                        "max_steps_any_robot": 1,
                        "events_processed": 4,
                    },
                ])
            schedule = {"campaign_id": "excluded", "jobs": jobs}
            for name in ("manifest.json", "schedule.json"):
                (root / name).write_text(json.dumps(schedule), encoding="utf-8")
            (root / "journals" / "dev.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in records),
                encoding="utf-8",
            )
            summary = rebuild_hil_reports(root)
            self.assertEqual(summary["excluded_conditions"], 1)
            self.assertEqual(summary["condition_rows"], 1)
            with (root / "reports" / "raw_call_attempts.csv").open(
                encoding="utf-8"
            ) as handle:
                raw = list(__import__("csv").DictReader(handle))
            self.assertEqual(len(raw), 1)
            self.assertEqual(raw[0]["condition_id"], "bayesian_cbaa_topk_003_k11")

    def test_invalidation_preserves_raw_attempts_but_rejects_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "journals").mkdir()
            (root / "reports").mkdir()
            job = {
                "condition_id": "bayesian_cbaa_topk_fixed_k1",
                "mission": "bayesian",
                "algorithm": "CBAA",
                "top_k_level": "K=1",
                "top_k_rate": 1 / 361,
                "top_k_cells": 1,
                "status": "complete",
                "stopped_reason": "",
                "trial_ids": [1],
                "completed_trials": [1],
                "historical_system_csv": "",
            }
            value = {
                "schema": 2,
                "campaign_id": "invalid-test",
                "jobs": [job],
                "scenario_sources": {
                    "bayesian": {"sha256": "scenario-hash"}
                },
                "implementation": {
                    "implementation_id": "native-v2",
                    "device_build": {"build_id": "build-v2"},
                },
            }
            for name in ("manifest.json", "schedule.json"):
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            attempts = [
                {
                    "record_type": "call_attempt",
                    "condition_id": job["condition_id"],
                    "mission": "bayesian",
                    "algorithm": "CBAA",
                    "top_k_level": "K=1",
                    "top_k_rate": 1 / 361,
                    "top_k_cells": 1,
                    "trial_id": 1,
                    "run_generation": 1,
                    "robot_id": "00",
                    "fixture_id": "fixture",
                    "accepted": True,
                    "allocator_time_us": 10,
                    "candidate_filter_time_us": 2,
                    "allocator_exclusive_time_us": 8,
                    "candidate_filter_calls": 1,
                    "device_id": "dev",
                },
                {
                    "record_type": "trial_complete",
                    "condition_id": job["condition_id"],
                    "trial_id": 1,
                    "run_generation": 1,
                    "scenario_file": "scenario.csv",
                    "scenario_sha256": "scenario-hash",
                    "trial_status": "completed",
                    "total_team_steps": 12,
                    "max_steps_any_robot": 4,
                    "events_processed": 20,
                },
            ]
            (root / "journals" / "dev.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in attempts),
                encoding="utf-8",
            )
            journal_before = (
                root / "journals" / "dev.jsonl"
            ).read_bytes()
            invalidate_campaign(
                root,
                reason_code="invalid_harness_architecture",
                explanation="Diagnostic-only legacy harness.",
                superseded_by="v2",
            )
            summary = rebuild_hil_reports(root)
            self.assertFalse(summary["analysis_valid"])
            self.assertEqual(summary["raw_attempts"], 1)
            self.assertEqual(summary["accepted_calls"], 0)
            self.assertEqual(
                (root / "journals" / "dev.jsonl").read_bytes(),
                journal_before,
            )
            with (root / "reports" / "raw_call_attempts.csv").open(
                encoding="utf-8"
            ) as handle:
                raw = list(__import__("csv").DictReader(handle))
            self.assertEqual(raw[0]["accepted"], "True")
            self.assertEqual(raw[0]["accepted_for_analysis"], "False")
            self.assertEqual(raw[0]["analysis_valid"], "False")
            with (root / "reports" / "condition_metrics.csv").open(
                encoding="utf-8"
            ) as handle:
                conditions = list(__import__("csv").DictReader(handle))
            self.assertEqual(conditions[0]["representative"], "False")
            self.assertEqual(
                conditions[0]["invalidation_reason"],
                "invalid_harness_architecture",
            )

    def test_proxy_goal_is_hardware_authoritative(self) -> None:
        class DesktopAllocator:
            def choose_goal(self, robot):
                raise AssertionError("desktop choose_goal must not execute")

        class Decision:
            def __init__(self, goal, debug=None):
                self.goal = goal
                self.debug = debug or {}

        class Bridge:
            def call(self, allocator, robot):
                return (3, 4)

            def take_messages(self, robot_id):
                return []

        proxy = make_proxy_allocator(DesktopAllocator, Bridge(), Decision)
        decision = proxy().choose_goal(SimpleNamespace(rid="00"))
        self.assertEqual(decision.goal, (3, 4))

    def test_scheduler_supports_one_two_and_three_devices(self) -> None:
        for worker_count in (1, 2, 3, 5):
            with self.subTest(worker_count=worker_count):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / "journals").mkdir()
                    schedule = {
                        "campaign_id": f"workers-{worker_count}",
                        "status": "pending",
                        "jobs": [
                            {
                                "condition_id": f"condition-{index}",
                                "mission": "bayesian",
                                "algorithm": "CBAA",
                                "top_k_level": "K=1",
                                "top_k_rate": 1 / 361,
                                "top_k_cells": 1,
                                "trial_ids": [index],
                                "completed_trials": [],
                                "status": "pending",
                                "device_id": "",
                                "stopped_reason": "",
                            }
                            for index in range(6)
                        ],
                    }
                    for name in ("manifest.json", "schedule.json"):
                        (root / name).write_text(
                            json.dumps(schedule),
                            encoding="utf-8",
                        )
                    devices = [
                        SimpleNamespace(
                            identity=SimpleNamespace(
                                device_id=f"device-{index}"
                            )
                        )
                        for index in range(worker_count)
                    ]

                    def complete(
                        root,
                        manifest,
                        job,
                        trial_id,
                        generation,
                        device,
                        watchdog_policy=None,
                    ):
                        return {"trial_id": trial_id}

                    with patch(
                        "allocator_replay.hil.campaign._run_trial",
                        side_effect=complete,
                    ):
                        result = HilCampaignRunner(root, devices).run()
                    self.assertEqual(result["status"], "complete")
                    self.assertTrue(
                        all(job["status"] == "complete" for job in result["jobs"])
                    )

    def test_scheduler_shards_whole_trials_across_five_devices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "journals").mkdir()
            job = {
                "condition_id": "bayesian_pi_topk_003_k11",
                "mission": "bayesian",
                "algorithm": "PI",
                "top_k_level": "3%",
                "top_k_rate": 0.03,
                "top_k_cells": 11,
                "trial_ids": list(range(15)),
                "completed_trials": [],
                "trial_work_estimates": {
                    str(index): 100 - index for index in range(15)
                },
                "status": "pending",
                "device_id": "",
                "stopped_reason": "",
            }
            schedule = {
                "campaign_id": "five-worker-sharding",
                "status": "pending",
                "jobs": [job],
            }
            for name in ("manifest.json", "schedule.json"):
                (root / name).write_text(json.dumps(schedule), encoding="utf-8")
            devices = [
                SimpleNamespace(
                    identity=SimpleNamespace(device_id=f"device-{index}")
                )
                for index in range(5)
            ]
            assignments = []

            def complete(
                root,
                manifest,
                job,
                trial_id,
                generation,
                device,
                watchdog_policy=None,
            ):
                assignments.append((trial_id, device.identity.device_id))
                time.sleep(0.01)
                return {"trial_id": trial_id}

            with patch(
                "allocator_replay.hil.campaign._run_trial",
                side_effect=complete,
            ):
                result = HilCampaignRunner(root, devices).run()
            self.assertEqual(result["jobs"][0]["status"], "complete")
            self.assertEqual({trial for trial, _ in assignments}, set(range(15)))
            self.assertGreaterEqual(len({device for _, device in assignments}), 4)

    def test_idle_workers_wait_for_bayesian_to_release_collaborative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "journals").mkdir()
            schedule = {
                "campaign_id": "phase-release-workers",
                "status": "pending",
                "phase": "bayesian",
                "jobs": [
                    {
                        "condition_id": "bayesian_pi_topk_003_k11",
                        "mission": "bayesian",
                        "algorithm": "PI",
                        "top_k_level": "3%",
                        "top_k_rate": 0.03,
                        "top_k_cells": 11,
                        "trial_ids": [1],
                        "completed_trials": [],
                        "status": "pending",
                        "device_id": "",
                        "stopped_reason": "",
                    },
                    {
                        "condition_id": "collaborative_cbaa_topk_005_k18",
                        "mission": "collaborative",
                        "algorithm": "CBAA",
                        "top_k_level": "5%",
                        "top_k_rate": 0.05,
                        "top_k_cells": 18,
                        "trial_ids": list(range(8)),
                        "completed_trials": [],
                        "status": "pending",
                        "device_id": "__after_bayesian_hold__",
                        "stopped_reason": "",
                    },
                ],
            }
            for name in ("manifest.json", "schedule.json"):
                (root / name).write_text(json.dumps(schedule), encoding="utf-8")
            devices = [
                SimpleNamespace(
                    identity=SimpleNamespace(device_id=f"device-{index}")
                )
                for index in range(5)
            ]
            collaborative_devices = set()

            def complete(
                root,
                manifest,
                job,
                trial_id,
                generation,
                device,
                watchdog_policy=None,
            ):
                if job["mission"] == "bayesian":
                    time.sleep(0.05)
                else:
                    collaborative_devices.add(device.identity.device_id)
                    time.sleep(0.02)
                return {"trial_id": trial_id}

            with patch(
                "allocator_replay.hil.campaign._run_trial",
                side_effect=complete,
            ):
                result = HilCampaignRunner(root, devices).run()
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["phase"], "collaborative")
            self.assertGreaterEqual(len(collaborative_devices), 4)

    def test_watchdog_failure_continues_to_next_trial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "journals").mkdir()
            schedule = {
                "campaign_id": "trial-failure-continuation",
                "status": "pending",
                "jobs": [{
                    "condition_id": "bayesian_pi_topk_003_k11",
                    "mission": "bayesian",
                    "algorithm": "PI",
                    "top_k_level": "3%",
                    "top_k_rate": 0.03,
                    "top_k_cells": 11,
                    "trial_ids": [1, 2],
                    "completed_trials": [],
                    "status": "pending",
                    "device_id": "",
                    "stopped_reason": "",
                }],
            }
            for name in ("manifest.json", "schedule.json"):
                (root / name).write_text(json.dumps(schedule), encoding="utf-8")
            device = SimpleNamespace(
                identity=SimpleNamespace(device_id="device-0")
            )

            def run(
                root,
                manifest,
                job,
                trial_id,
                generation,
                device,
                watchdog_policy=None,
            ):
                if trial_id == 1:
                    raise HilTrialFailure(
                        "deadlock_repeated_state", {"events_processed": 72}
                    )
                return {"trial_id": trial_id}

            with patch(
                "allocator_replay.hil.campaign._run_trial", side_effect=run
            ):
                result = HilCampaignRunner(root, [device]).run()
            job = result["jobs"][0]
            self.assertEqual(job["completed_trials"], [2])
            self.assertEqual(job["failed_trials"][0]["trial_id"], 1)
            self.assertEqual(job["status"], "completed_with_trial_failures")


if __name__ == "__main__":
    unittest.main()
