from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
ARCH = ROOT / "Simulation" / "Architecture"
if str(ARCH) not in sys.path:
    sys.path.insert(0, str(ARCH))

from allocator_replay.coalescing.build import (  # noqa: E402
    build_device_bundle,
    validate_built_imports,
)
from allocator_replay.coalescing.cli import build_parser  # noqa: E402
from allocator_replay.coalescing.config import (  # noqa: E402
    ALLOCATORS,
    HilCondition,
    PolicySpec,
    load_campaign_config,
)
from allocator_replay.coalescing.epochs import build_admission_epochs  # noqa: E402
from allocator_replay.coalescing.manifests import load_all_pairs  # noqa: E402
from allocator_replay.coalescing.preflight import (  # noqa: E402
    build_device_binding,
    run_preflight,
    verify_preflight,
)
from allocator_replay.coalescing.runtime import PersistentEpochSession  # noqa: E402
from allocator_replay.causal.session import (  # noqa: E402
    DEVICE_ALLOCATOR_TIMER_SCOPE,
)
from allocator_replay.coalescing.schedule import campaign_root  # noqa: E402
from allocator_replay.host import discovery  # noqa: E402
from allocator_replay.device.native.collaborative import (  # noqa: E402
    create_persistent_runtime,
)
from allocator_replay.host.emulator import LoopbackReplayDevice  # noqa: E402


PILOT_CONFIG = ROOT / "configs" / "hil_pilot.json"


class CoalescingConfigTests(unittest.TestCase):
    def test_active_cli_has_no_legacy_campaign_selectors(self) -> None:
        parser = build_parser()
        help_text = parser.format_help().lower()
        self.assertNotIn("top-k", help_text)
        self.assertNotIn("bayesian", help_text)
        choices = next(
            action.choices
            for action in parser._actions
            if getattr(action, "choices", None)
        )
        self.assertNotIn("capture", choices)
        self.assertIn("hil-dry-run", choices)

    def test_generated_pairs_are_byte_verified(self) -> None:
        config = load_campaign_config(PILOT_CONFIG)
        pairs = load_all_pairs(config)
        self.assertEqual(set(pairs), {("medium", "trace_0000")})
        pair = pairs[("medium", "trace_0000")]
        self.assertEqual(len(pair.tasks), 50)
        self.assertEqual(len(pair.robot_starts), 4)
        self.assertEqual(
            len({(item["x"], item["y"]) for item in pair.tasks}), 50
        )
        self.assertTrue(pair.paired_manifest_sha256)

    def test_campaign_override_is_exactly_one_safe_component(self) -> None:
        config = load_campaign_config(PILOT_CONFIG)
        for unsafe in (
            "../escape",
            "..\\escape",
            "nested/name",
            "..",
            ".",
            "CON",
            "trailing.",
        ):
            with self.subTest(unsafe=unsafe):
                with self.assertRaisesRegex(ValueError, "SAFE_ID"):
                    campaign_root(config, unsafe)
        safe = campaign_root(config, "focused-safe_id.v3")
        self.assertEqual(safe.parent, config.results_root.resolve())
        self.assertEqual(safe.name, "focused-safe_id.v3")

    def test_legacy_top_k_is_rejected(self) -> None:
        temporary = ROOT / "Tests" / "HIL" / "AllocatorReplay" / "_bad_hil_config.json"
        value = json.loads(PILOT_CONFIG.read_text(encoding="utf-8"))
        value["top_k_cells"] = 5
        temporary.write_text(json.dumps(value), encoding="utf-8")
        try:
            with self.assertRaisesRegex(ValueError, "Top-K"):
                load_campaign_config(temporary)
        finally:
            temporary.unlink(missing_ok=True)


class AdmissionEpochTests(unittest.TestCase):
    @staticmethod
    def tasks() -> list[dict]:
        return [
            {"task_id": "initial", "initially_visible": True, "release_time_s": 0.0},
            {"task_id": "a", "initially_visible": False, "release_time_s": 1.0},
            {"task_id": "b", "initially_visible": False, "release_time_s": 2.0},
            {"task_id": "c", "initially_visible": False, "release_time_s": 3.0},
            {"task_id": "d", "initially_visible": False, "release_time_s": 4.0},
            {"task_id": "e", "initially_visible": False, "release_time_s": 10.0},
        ]

    def test_eager_count_and_final_flush(self) -> None:
        eager = build_admission_epochs(self.tasks(), PolicySpec("eager", 1))
        self.assertEqual([item.trigger_reason for item in eager[1:]], ["task_arrival_eager"] * 5)
        count = build_admission_epochs(self.tasks(), PolicySpec("count", 4))
        self.assertEqual(count[1].task_ids, ("a", "b", "c", "d"))
        self.assertEqual(count[2].trigger_reason, "trace_end_flush")
        self.assertEqual(count[2].task_ids, ("e",))

    def test_bounded_timeout(self) -> None:
        epochs = build_admission_epochs(self.tasks(), PolicySpec("bounded", 8, 5.0))
        self.assertEqual(epochs[1].trigger_reason, "age_timeout")
        self.assertEqual(epochs[1].time_s, 6.0)
        self.assertEqual(epochs[2].time_s, 15.0)


class SafeDiscoveryTests(unittest.TestCase):
    def test_auto_candidates_only_include_known_usb_vendors(self) -> None:
        class Port:
            def __init__(self, device: str, vid: int | None, pid: int | None) -> None:
                self.device = device
                self.description = device
                self.vid = vid
                self.pid = pid

        class Ports:
            @staticmethod
            def comports():
                return [
                    Port("COM1", 0x1234, 1),
                    Port("COM2", 0x2E8A, 5),
                    Port("COM3", 0x1FFB, 2),
                    Port("COM4", None, None),
                ]

        with patch.object(
            discovery, "_serial_dependencies", return_value=(object(), Ports)
        ):
            selected, skipped = discovery.safe_auto_ports()
        self.assertEqual([item[0] for item in selected], ["COM2"])
        self.assertEqual(
            {item["port"] for item in skipped}, {"COM1", "COM3", "COM4"}
        )
        self.assertTrue(all("not probed" in item["error"] for item in skipped))


class NativeUnrestrictedTests(unittest.TestCase):
    def test_all_six_accept_online_tasks_without_candidate_restriction(self) -> None:
        all_tasks = [[1, 1], [2, 2], [3, 3], [4, 4]]
        for algorithm in ALLOCATORS:
            with self.subTest(algorithm=algorithm):
                config = {
                    "mission": "collaborative",
                    "algorithm": algorithm,
                    "robot_id": "00",
                    "robot_ids": ["00", "01", "02", "03"],
                    "grid_size": 19,
                    "all_tasks": all_tasks,
                    "max_candidate_cells": None,
                    "seed": 7,
                }
                runtime = create_persistent_runtime(config)
                runtime.reset_trial(
                    config,
                    {
                        "robot_id": "00",
                        "robot_ids": config["robot_ids"],
                        "pos": [0, 0],
                        "all_tasks": all_tasks,
                        "active_tasks": all_tasks[:2],
                    },
                )
                runtime.choose_goal()
                self.assertEqual(runtime.candidate_counts(), (2, 2))
                resume = runtime.snapshot_minimal()
                restored = create_persistent_runtime(config)
                restored.reset_trial(
                    config,
                    {
                        "robot_id": "00",
                        "robot_ids": config["robot_ids"],
                        "pos": [0, 0],
                        "all_tasks": all_tasks,
                        "active_tasks": all_tasks,
                        "allocator_attrs": resume["allocator_attrs"],
                    },
                )
                restored.apply_delta(
                    {
                        "events": [
                            {
                                "kind": "allocation_epoch",
                                "payload": {
                                    "epoch_index": 1,
                                    "trigger_reason": "batch_threshold",
                                    "admitted_cells": all_tasks[2:],
                                },
                            }
                        ]
                    }
                )
                restored.choose_goal()
                self.assertEqual(len(restored.state.active_slots()), 4)
                before, after = restored.candidate_counts()
                self.assertEqual(before, after)
                if restored.state.filter_invocations:
                    self.assertEqual((before, after), (4, 4))
                self.assertIsNone(restored.state.max_candidate_cells)
                self.assertEqual(
                    restored.call_class(), "full_allocation_solve"
                )

    def test_cbaa_admission_retains_head_until_completion(self) -> None:
        all_tasks = [[9, 9], [1, 0]]
        config = {
            "mission": "collaborative",
            "algorithm": "CBAA",
            "robot_id": "00",
            "robot_ids": ["00", "01", "02", "03"],
            "grid_size": 19,
            "all_tasks": all_tasks,
            "max_candidate_cells": None,
            "seed": 17,
        }
        runtime = create_persistent_runtime(config)
        runtime.reset_trial(
            config,
            {
                "robot_id": "00",
                "robot_ids": config["robot_ids"],
                "pos": [0, 0],
                "all_tasks": all_tasks,
                "active_tasks": all_tasks[:1],
            },
        )
        runtime.apply_delta(
            {
                "events": [
                    {
                        "kind": "allocation_epoch",
                        "payload": {
                            "epoch_index": 0,
                            "trigger_reason": "initial_tasks",
                            "admitted_cells": all_tasks[:1],
                        },
                    }
                ]
            }
        )
        self.assertEqual(runtime.choose_goal().goal, (9, 9))
        self.assertEqual(runtime.call_class(), "full_allocation_solve")

        runtime.apply_delta(
            {
                "active_tasks": all_tasks,
                "events": [
                    {
                        "kind": "allocation_epoch",
                        "payload": {
                            "epoch_index": 1,
                            "trigger_reason": "task_arrival_eager",
                            "admitted_cells": all_tasks[1:],
                        },
                    }
                ],
            }
        )
        self.assertEqual(runtime.choose_goal().goal, (9, 9))
        before, after = runtime.candidate_counts()
        self.assertEqual(before, after)
        self.assertEqual(runtime.call_class(), "full_allocation_solve")
        self.assertEqual(runtime.state.allocation_epoch_hook_count, 2)

        # A duplicate delivery remains idempotent after a host-side context
        # switch.  It cannot recall the retained path or inflate the
        # persisted hook count, and the call index stays monotonic.
        snapshot = runtime.snapshot_minimal()
        restored = create_persistent_runtime(config)
        restored.reset_trial(
            config,
            {
                "robot_id": "00",
                "robot_ids": config["robot_ids"],
                "pos": [0, 0],
                "all_tasks": all_tasks,
                "active_tasks": all_tasks,
                "allocator_attrs": snapshot["allocator_attrs"],
            },
        )
        restored.apply_delta(
            {
                "events": [
                    {
                        "kind": "allocation_epoch",
                        "payload": {
                            "epoch_index": 1,
                            "trigger_reason": "task_arrival_eager",
                            "admitted_cells": all_tasks[1:],
                        },
                    }
                ]
            }
        )
        decision = restored.choose_goal()
        self.assertEqual(decision.goal, (9, 9))
        self.assertEqual(decision.debug["call_index"], 2)
        self.assertEqual(restored.call_class(), "cached_or_maintenance")
        self.assertEqual(restored.state.allocation_epoch_hook_count, 2)

        # Once the retained task is actually completed, the already admitted
        # pool is available to the normal CBAA solve without another release.
        restored.apply_delta(
            {
                "events": [
                    {
                        "kind": "allocator_task_completed",
                        "payload": {
                            "cell": all_tasks[0],
                            "reason": "local_service",
                            "local": True,
                        },
                    }
                ]
            }
        )
        self.assertEqual(restored.choose_goal().goal, (1, 0))
        self.assertEqual(restored.candidate_counts(), (1, 1))


class PersistentProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        manifest = build_device_bundle(compile_mpy=False)
        cls.build_root = Path(str(manifest["output"]))
        validate_built_imports(cls.build_root)

    def test_loopback_keeps_device_timer_separate(self) -> None:
        config = load_campaign_config(PILOT_CONFIG)
        trace = load_all_pairs(config)[("medium", "trace_0000")]
        condition = HilCondition("CBAA", "medium", PolicySpec("count", 4), config.manifest_set_id)
        device = LoopbackReplayDevice("focused", build_root=self.build_root)
        session = PersistentEpochSession(device, condition, trace, 1, 30.0)
        try:
            initial = tuple(item["task_id"] for item in trace.tasks if item["initially_visible"])
            online = tuple(item["task_id"] for item in trace.tasks if not item["initially_visible"])
            session.begin()
            session.admit(initial)
            first = session.call("00", epoch_index=0, round_index=0, trigger_reason="initial_tasks")
            session.admit(online[:4])
            second = session.call("01", epoch_index=1, round_index=0, trigger_reason="batch_threshold")
            for result in (first, second):
                metrics = result.metrics
                self.assertGreaterEqual(metrics["device_allocator_time_us"], 0)
                self.assertGreaterEqual(metrics["host_setup_transport_us"], 0)
                self.assertGreaterEqual(metrics["host_nonallocator_overhead_us"], 0)
                self.assertEqual(metrics["candidate_count_before"], metrics["candidate_count_after"])
                self.assertEqual(
                    metrics["resident_active_task_count"],
                    metrics["host_active_task_count"],
                )
                self.assertEqual(
                    metrics["device_allocator_timer_scope"],
                    DEVICE_ALLOCATOR_TIMER_SCOPE,
                )
        finally:
            try:
                session.close()
            finally:
                device.close()


class HardwareIdentityHardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        manifest = build_device_bundle(compile_mpy=True)
        cls.build_root = Path(str(manifest["output"]))

    def test_binding_uses_live_check_and_rejects_cached_identity_mismatch(self) -> None:
        device = LoopbackReplayDevice("binding-live", build_root=self.build_root)
        try:
            binding = build_device_binding(
                [device],
                self.build_root,
                execution_mode="serial_hardware",
                require_compiled=True,
            )
            self.assertEqual(
                binding["module_set_sha256"],
                binding["devices"][0]["module_set_sha256"],
            )
            original_check = device.check

            def stale_check():
                value = original_check()
                value["actual_module_set_sha256"] = "stale-module-set"
                return value

            device.check = stale_check
            with self.assertRaisesRegex(RuntimeError, "module hash mismatch"):
                build_device_binding(
                    [device],
                    self.build_root,
                    execution_mode="serial_hardware",
                    require_compiled=True,
                )
        finally:
            device.close()

    def test_preflight_exercises_online_growth_and_duplicate_idempotence(self) -> None:
        device = LoopbackReplayDevice("growth-preflight", build_root=self.build_root)
        with tempfile.TemporaryDirectory(
            dir=ROOT / "Tests" / "HIL" / "AllocatorReplay"
        ) as temporary:
            report_path = Path(temporary) / "preflight.json"
            try:
                with patch(
                    "allocator_replay.coalescing.preflight.PREFLIGHT_PATH",
                    report_path,
                ):
                    report = run_preflight([device], self.build_root)
                    self.assertTrue(report["passed"])
                    self.assertEqual(len(report["allocator_probes"]), len(ALLOCATORS))
                    for probe in report["allocator_probes"]:
                        self.assertEqual(
                            probe["device_module_set_sha256"],
                            report["module_set_sha256"],
                        )
                        self.assertEqual(probe["initial_visible_count"], 2)
                        self.assertEqual(probe["grown_visible_count"], 3)
                        grown = probe["online_growth_call"]
                        duplicate = probe["duplicate_epoch_call"]
                        self.assertTrue(grown["allocation_epoch_hook_invoked"])
                        self.assertEqual(grown["call_class"], "full_allocation_solve")
                        self.assertEqual(
                            grown["candidate_count_before"],
                            grown["candidate_count_after"],
                        )
                        self.assertEqual(
                            grown["resident_active_task_count"],
                            probe["grown_visible_count"],
                        )
                        if grown["candidate_filter_calls"]:
                            self.assertEqual(
                                grown["candidate_count_after"],
                                probe["grown_visible_count"],
                            )
                        self.assertFalse(
                            duplicate["allocation_epoch_hook_invoked"]
                        )
                    self.assertEqual(
                        verify_preflight([device])["report_sha256"],
                        report["report_sha256"],
                    )
            finally:
                device.close()

    def test_loopback_epoch_hook_runs_once_per_robot_not_per_round(self) -> None:
        config = load_campaign_config(PILOT_CONFIG)
        trace = load_all_pairs(config)[("medium", "trace_0000")]
        condition = HilCondition(
            "CBAA", "medium", PolicySpec("eager", 1), config.manifest_set_id
        )
        device = LoopbackReplayDevice("epoch-hook", build_root=self.build_root)
        session = PersistentEpochSession(device, condition, trace, 1, 30.0)
        try:
            initial = tuple(
                item["task_id"] for item in trace.tasks if item["initially_visible"]
            )
            online = tuple(
                item["task_id"] for item in trace.tasks if not item["initially_visible"]
            )
            session.begin()
            session.admit(initial)
            first = session.call(
                "00", epoch_index=0, round_index=0, trigger_reason="initial_tasks"
            )
            maintenance = session.call(
                "00", epoch_index=0, round_index=1, trigger_reason="initial_tasks"
            )
            peer_first = session.call(
                "01", epoch_index=0, round_index=0, trigger_reason="initial_tasks"
            )
            session.admit(online[:1])
            grown = session.call(
                "00",
                epoch_index=1,
                round_index=0,
                trigger_reason="task_arrival_eager",
            )
            grown_maintenance = session.call(
                "00",
                epoch_index=1,
                round_index=1,
                trigger_reason="task_arrival_eager",
            )

            self.assertTrue(first.metrics["allocation_epoch_hook_invoked"])
            self.assertFalse(
                maintenance.metrics["allocation_epoch_hook_invoked"]
            )
            self.assertTrue(
                peer_first.metrics["allocation_epoch_hook_invoked"]
            )
            self.assertTrue(grown.metrics["allocation_epoch_hook_invoked"])
            self.assertFalse(
                grown_maintenance.metrics["allocation_epoch_hook_invoked"]
            )
            self.assertEqual(first.metrics["call_class"], "full_allocation_solve")
            self.assertEqual(grown.metrics["call_class"], "full_allocation_solve")
            self.assertNotEqual(
                grown.metrics["call_class"], "cached_or_maintenance"
            )
            self.assertEqual(grown.metrics["epoch_admitted_task_ids"], [online[0]])
            self.assertEqual(
                grown.metrics["candidate_count_before"],
                grown.metrics["candidate_count_after"],
            )
            self.assertEqual(
                grown.metrics["resident_active_task_count"],
                len(initial) + 1,
            )
            if grown.metrics["candidate_filter_calls"]:
                self.assertEqual(
                    grown.metrics["candidate_count_after"],
                    len(initial) + 1,
                )
        finally:
            try:
                session.close()
            finally:
                device.close()


if __name__ == "__main__":
    unittest.main()
