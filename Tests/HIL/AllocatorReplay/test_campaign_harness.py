from __future__ import annotations

import copy
import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from allocator_replay.capture.codec import read_trace, write_trace
from allocator_replay.config.study import (
    DEVICE_BUILD_ROOT,
    conditions,
    trace_condition_root,
)
from allocator_replay.device.build import build_device_bundle
from allocator_replay.host.campaign import CampaignRunner
from allocator_replay.host.emulator import (
    DesktopReplayDevice,
    LoopbackReplayDevice,
)
from allocator_replay.host.report import rebuild_reports


class CampaignHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        manifest = build_device_bundle()
        cls.build_root = Path(str(manifest["output"]))
        cls.build_id = str(manifest["build_id"])
        cls.source_conditions = [
            next(
                item
                for item in conditions("bayesian")
                if item.algorithm == "CBAA" and item.top_k_rate == 0.05
            ),
            next(
                item
                for item in conditions("bayesian")
                if item.algorithm == "PI" and item.top_k_rate == 0.05
            ),
            next(
                item
                for item in conditions("collaborative")
                if item.algorithm == "DGA" and item.top_k_rate == 0.05
            ),
        ]

    def _campaign(self, root: Path, count: int = 3) -> tuple[Path, list[dict]]:
        root.mkdir(parents=True)
        (root / "journals").mkdir()
        entries = {}
        fixtures = []
        for rank, condition in enumerate(self.source_conditions[:count]):
            source = sorted(
                trace_condition_root(condition).glob("trial_*.jsonl.gz")
            )[0]
            fixture = copy.deepcopy(next(read_trace(source)))
            fixtures.append(fixture)
            trace_root = root / "fixtures" / condition.condition_id
            trace_root.mkdir(parents=True)
            details = write_trace(trace_root / "trial.jsonl.gz", [fixture])
            manifest = {
                "schema": 1,
                "status": "complete",
                "trial_count": 1,
                "fixture_count": 1,
                "trials": [
                    {
                        "trial_id": fixture["trial_id"],
                        "trace": "trial.jsonl.gz",
                        **details,
                    }
                ],
            }
            manifest_path = trace_root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            entries[condition.condition_id] = {
                "condition_id": condition.condition_id,
                "mission": condition.mission,
                "algorithm": condition.algorithm,
                "top_k_level": condition.top_k_level,
                "top_k_rate": condition.top_k_rate,
                "top_k_cells": condition.top_k_cells,
                "status": "pending",
                "classification": "",
                "pinned_device_id": "",
                "device_ids": [],
                "mixed_device": False,
                "fixture_total": 1,
                "fixtures_completed": 0,
                "estimated_simulator_ns": (count - rank) * 100,
                "trace_manifest": str(manifest_path.resolve()),
                "trace_manifest_sha256": "test",
                "cohort_sha256": "test",
                "simulator_source_sha256": "test",
                "started_at": "",
                "finished_at": "",
                "failure_fixture_id": "",
            }
        state = {
            "schema": 1,
            "campaign_id": root.name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": "ready",
            "build_id": self.build_id,
            "build_manifest": str(self.build_root / "manifest.json"),
            "build_manifest_sha256": "test",
            "timeout_seconds": 30.0,
            "confirmation_attempts": 3,
            "devices": {},
            "conditions": entries,
        }
        (root / "schedule.json").write_text(
            json.dumps(state),
            encoding="utf-8",
        )
        return root, fixtures

    def test_one_two_and_three_loopback_workers(self) -> None:
        for worker_count in (1, 2, 3):
            with self.subTest(worker_count=worker_count):
                with tempfile.TemporaryDirectory() as directory:
                    root, _ = self._campaign(
                        Path(directory) / f"campaign_{worker_count}"
                    )
                    devices = [
                        LoopbackReplayDevice(
                            f"loopback-{worker_count}-{index}",
                            build_root=self.build_root,
                        )
                        for index in range(worker_count)
                    ]
                    try:
                        state = CampaignRunner(root, devices).run()
                    finally:
                        for device in devices:
                            try:
                                device.exit()
                            except Exception:
                                pass
                            device.close()
                    self.assertEqual(state["status"], "complete")
                    for entry in state["conditions"].values():
                        self.assertEqual(
                            entry["classification"],
                            "hardware_feasible_30s",
                        )
                        self.assertEqual(len(entry["device_ids"]), 1)
                        self.assertFalse(entry["mixed_device"])
                    report = rebuild_reports(root)
                    self.assertEqual(report["accepted_calls"], 3)

    def test_confirmed_timeout_stops_only_affected_condition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, fixtures = self._campaign(Path(directory) / "timeout", count=1)
            fixture_id = fixtures[0]["fixture_id"]
            device = DesktopReplayDevice(
                "timeout-device",
                build_root=self.build_root,
                scripted_outcomes={fixture_id: ["timeout", "timeout", "timeout"]},
            )
            state = CampaignRunner(root, [device]).run()
            entry = next(iter(state["conditions"].values()))
            self.assertEqual(entry["classification"], "timing_unusable_30s")
            self.assertEqual(entry["fixtures_completed"], 0)
            rebuild_reports(root)
            with (root / "reports" / "condition_metrics.csv").open(
                newline="", encoding="utf-8"
            ) as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["representative_metrics"], "False")
            self.assertEqual(row["allocator_mean_us"], "")

    def test_disconnect_is_idempotently_resumed_on_same_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, fixtures = self._campaign(Path(directory) / "resume", count=1)
            fixture_id = fixtures[0]["fixture_id"]
            disconnected = DesktopReplayDevice(
                "stable-device-id",
                build_root=self.build_root,
                scripted_outcomes={fixture_id: ["transport_error"]},
            )
            paused = CampaignRunner(root, [disconnected]).run()
            entry = next(iter(paused["conditions"].values()))
            self.assertEqual(entry["status"], "paused")
            resumed = DesktopReplayDevice(
                "stable-device-id",
                build_root=self.build_root,
            )
            finished = CampaignRunner(root, [resumed]).run()
            entry = next(iter(finished["conditions"].values()))
            self.assertEqual(entry["classification"], "hardware_feasible_30s")
            attempts = []
            for path in (root / "journals").glob("*.jsonl"):
                attempts.extend(
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                )
            self.assertEqual(
                len({record["attempt_id"] for record in attempts}),
                len(attempts),
            )
            self.assertEqual(
                sum(record.get("outcome") == "transport_error" for record in attempts),
                1,
            )
            self.assertEqual(sum(bool(record.get("accepted")) for record in attempts), 1)
