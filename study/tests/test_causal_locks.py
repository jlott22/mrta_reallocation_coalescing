from __future__ import annotations

import os
import queue
import signal
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from study.causal.locks import LockRecoveryError, native_lease_path, recover_stale_locks
from study.causal.model import load_causal_config
from study.causal.orchestrator import _handle_worker_sigterm, _worker_entry
from study.causal.schedule import plan_paired_blocks
from study.manifests import canonical_json_bytes, generate_manifest_set


class CausalLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        manifest = {
            "manifest_set_id": "lock_test_manifests",
            "root": "study/generated/manifests",
            "master_seed": 919,
            "trace_count": 1,
            "grid_size": 19,
            "robot_count": 4,
            "task_count": 50,
            "initial_task_count": 8,
            "arrival_loads": {"low": 0.1},
        }
        generate_manifest_set({"manifest": manifest}, self.root)
        boards = [
            {
                "board_id": f"board_{index}",
                "serial_device": f"/dev/serial/by-id/test-{index}",
                "expected_device_uid": f"uid_{index}",
                "expected_build_id": "lock_test_build",
                "expected_firmware_sha256": f"{index + 1:064x}",
                "expected_module_set_sha256": f"{index + 11:064x}",
            }
            for index in range(4)
        ]
        raw = {
            "schema_version": 1,
            "manifest": manifest,
            "campaign": {
                "campaign_id": "lock_test",
                "stage": "development",
                "output_root": "study/output/lock_test",
                "algorithms": ["CBAA", "ACBBA", "PI", "HIPC"],
                "loads": ["low"],
                "policies": [
                    {"policy_id": "eager_b1", "mode": "eager", "batch_size": 1}
                ],
                "schedule_seed": 31,
                "max_technical_retries": 0,
            },
            "hardware": {
                "boards": boards,
                "core_affinities": [0, 1, 2, 3],
                "development_override": True,
                "provider_factory": "study.causal.worker:create_timing_provider",
                "simulated_duration_us": 100,
            },
        }
        self.config_path = self.root / "configs" / "lock_test.json"
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_bytes(canonical_json_bytes(raw))
        self.config = load_causal_config(self.config_path, self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _output_lock(
        self,
        board_index: int,
        pid: int,
        *,
        hostname: str | None = None,
        uid: str | None = None,
    ) -> Path:
        board = self.config.boards[board_index]
        path = self.config.output_root / "board_locks" / f"{board.board_id}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes({
            "schema_version": 2,
            "lock_kind": "causal_output_board",
            "board_id": board.board_id,
            "expected_device_uid": board.expected_device_uid if uid is None else uid,
            "serial_device": board.serial_device,
            "worker_index": board_index,
            "pid": pid,
            "hostname": socket.gethostname() if hostname is None else hostname,
            "claimed_at": "2026-08-09T00:00:00Z",
        }))
        return path

    def _native_lock(
        self,
        board_index: int,
        pid: int,
        *,
        hostname: str | None = None,
        uid: str | None = None,
    ) -> Path:
        board = self.config.boards[board_index]
        path = native_lease_path(self.root, board.expected_device_uid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json_bytes({
            "schema_version": 1,
            "board_id": board.expected_device_uid if uid is None else uid,
            "pid": pid,
            "hostname": socket.gethostname() if hostname is None else hostname,
            "token": "dead-owner-token",
            "created_unix_s": 1.0,
        }))
        return path

    def test_recovery_removes_only_exact_configured_dead_locks(self) -> None:
        output_lock = self._output_lock(0, 101)
        native_lock = self._native_lock(0, 102)
        unrelated = self.config.output_root / "board_locks" / "unconfigured.lock"
        unrelated.write_text("must remain", encoding="utf-8")
        with patch("study.causal.locks._pid_status", return_value=("dead", "test proof")):
            report = recover_stale_locks(self.config)
        self.assertFalse(output_lock.exists())
        self.assertFalse(native_lock.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(2, report["present_stale_lock_count"])
        self.assertEqual(2, len(report["removed_locks"]))
        self.assertFalse(report["used_directory_enumeration"])
        self.assertFalse(report["used_glob_deletion"])

    def test_live_lock_refuses_before_deleting_other_stale_lock(self) -> None:
        stale = self._output_lock(0, 101)
        live = self._native_lock(1, 202)

        def status(pid: int) -> tuple[str, str]:
            return ("alive", "test process exists") if pid == 202 else ("dead", "test proof")

        with patch("study.causal.locks._pid_status", side_effect=status):
            with self.assertRaisesRegex(LockRecoveryError, "status=alive"):
                recover_stale_locks(self.config)
        self.assertTrue(stale.exists())
        self.assertTrue(live.exists())

    def test_foreign_host_lock_refuses_before_deletion(self) -> None:
        stale = self._output_lock(0, 101)
        foreign = self._native_lock(1, 102, hostname="another-agx")
        with patch("study.causal.locks._pid_status", return_value=("dead", "test proof")):
            with self.assertRaisesRegex(LockRecoveryError, "foreign host"):
                recover_stale_locks(self.config)
        self.assertTrue(stale.exists())
        self.assertTrue(foreign.exists())

    def test_malformed_or_wrong_uid_lock_refuses_before_deletion(self) -> None:
        for case in ("malformed", "wrong_uid"):
            with self.subTest(case=case):
                stale = self._output_lock(0, 101)
                blocked = self._native_lock(1, 102)
                if case == "malformed":
                    blocked.write_text("not-json", encoding="utf-8")
                    message = "malformed lock JSON"
                else:
                    blocked = self._native_lock(1, 102, uid="wrong_uid")
                    message = "UID does not match"
                with patch("study.causal.locks._pid_status", return_value=("dead", "test proof")):
                    with self.assertRaisesRegex(LockRecoveryError, message):
                        recover_stale_locks(self.config)
                self.assertTrue(stale.exists())
                self.assertTrue(blocked.exists())
                stale.unlink()
                blocked.unlink()

    def test_current_pid_is_not_considered_dead(self) -> None:
        from study.causal.locks import _pid_status

        status, _detail = _pid_status(os.getpid())
        self.assertIn(status, {"alive", "unknown"})
        self.assertNotEqual("dead", status)

    def test_worker_sigterm_unwinds_provider_lease_and_output_lock(self) -> None:
        # Import the repository implementation, not anything installed globally.
        repository_architecture = (
            Path(__file__).resolve().parents[2] / "Simulation" / "Architecture"
        )
        if str(repository_architecture) not in sys.path:
            sys.path.insert(0, str(repository_architecture))
        from allocator_replay.causal import BoardLease

        lease_path = native_lease_path(
            self.root, self.config.boards[0].expected_device_uid
        )
        closed = []

        def provider_factory(**_kwargs):
            lease = BoardLease(
                self.config.boards[0].expected_device_uid,
                self.root / "study" / "native_device_leases",
            ).acquire()

            class Provider:
                def open(self) -> None:
                    pass

                def close(self) -> None:
                    lease.release()
                    closed.append(True)

            return Provider()

        def interrupted_runner(**_kwargs) -> None:
            _handle_worker_sigterm(signal.SIGTERM, None)

        results: queue.Queue = queue.Queue()
        block = next(
            item for item in plan_paired_blocks(self.config)
            if item.worker_index == 0
        )
        with (
            patch("study.causal.orchestrator._set_affinity", return_value={"supported": False}),
            patch(
                "study.causal.orchestrator._import_callable",
                side_effect=[provider_factory, interrupted_runner],
            ),
        ):
            _worker_entry(
                str(self.config_path), str(self.root), 0, [block],
                {"git_head": "0" * 40, "source_tree_sha256": "1" * 64,
                 "relevant_dirty": False},
                results, False,
            )

        output_lock = (
            self.config.output_root / "board_locks"
            / f"{self.config.boards[0].board_id}.lock"
        )
        self.assertEqual([True], closed)
        self.assertFalse(lease_path.exists())
        self.assertFalse(output_lock.exists())
        events = []
        while not results.empty():
            events.append(results.get_nowait())
        self.assertEqual(["worker_ready", "worker_terminated"], [
            event["type"] for event in events
        ])
        attempt_root = (
            self.config.output_root / "causal" / "attempts"
            / block.jobs[0].job_id / "attempt_0001"
        )
        self.assertTrue((attempt_root / "job.json").is_file())
        self.assertFalse((attempt_root / "failure.json").exists())


if __name__ == "__main__":
    unittest.main()
