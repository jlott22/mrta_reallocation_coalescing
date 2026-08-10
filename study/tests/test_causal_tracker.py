import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from study.causal.tracker import collect_snapshot, render_markdown


class CausalTrackerTests(unittest.TestCase):
    def test_snapshot_counts_workers_and_estimates_current_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            schedule = {
                "blocks": [
                    {"jobs": [
                        {"job_id": "job_a", "worker_index": 0},
                        {"job_id": "job_b", "worker_index": 0},
                    ]},
                    {"jobs": [
                        {"job_id": "job_c", "worker_index": 1},
                        {"job_id": "job_d", "worker_index": 2},
                    ]},
                ]
            }
            (root / "causal_schedule.json").write_text(json.dumps(schedule), encoding="utf-8")
            completed = root / "causal" / "completed"
            (completed / "job_a").mkdir(parents=True)
            (completed / "job_c").mkdir()
            events = [
                {
                    "type": "worker_ready", "worker_index": 0,
                    "invocation_id": "pilot", "recorded_at": "2026-08-09T00:00:00Z",
                },
                {
                    "type": "job", "status": "completed", "job_id": "job_a",
                    "worker_index": 0, "invocation_id": "pilot",
                    "recorded_at": "2026-08-09T00:00:10Z",
                },
            ]
            (root / "campaign_events.jsonl").write_text(
                "\n".join(json.dumps(event) for event in events) + "\n",
                encoding="utf-8",
            )

            snapshot = collect_snapshot(
                root,
                now=datetime(2026, 8, 9, 0, 0, 20, tzinfo=timezone.utc),
            )

            self.assertEqual(snapshot["status"], "RUNNING")
            self.assertEqual(snapshot["completed"], 2)
            self.assertEqual(snapshot["remaining"], 2)
            self.assertEqual(snapshot["eta_s"], 40.0)
            self.assertEqual([worker["planned"] for worker in snapshot["workers"]], [2, 1, 1])
            self.assertIn("2 / 4 (50.0%)", render_markdown(snapshot, refresh_seconds=5.0))

    def test_complete_snapshot_is_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "causal_schedule.json").write_text(
                json.dumps({"blocks": [{"jobs": [{"job_id": "only", "worker_index": 0}]}]}),
                encoding="utf-8",
            )
            (root / "causal" / "completed" / "only").mkdir(parents=True)

            snapshot = collect_snapshot(root)

            self.assertEqual(snapshot["status"], "COMPLETE")
            self.assertTrue(snapshot["terminal"])
            self.assertEqual(snapshot["eta_s"], 0.0)


if __name__ == "__main__":
    unittest.main()
