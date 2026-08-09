from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from allocator_replay.config.study import ALGORITHMS
from allocator_replay.device.build import build_device_bundle
from allocator_replay.host.emulator import LoopbackReplayDevice
from allocator_replay.host.preflight import (
    run_persistent_checks,
    run_preflight,
)
from allocator_replay.host.transport import (
    ReplayAllocatorMemoryError,
    ReplayStateSetupError,
    ReplayTimeout,
    ReplayTransportError,
)


class _PersistentDevice:
    def __init__(
        self,
        device_id: str,
        *,
        allocator_time_us: int = 100,
        resource_algorithm: str | None = None,
        setup_failure_algorithm: str | None = None,
        invalid_algorithm: str | None = None,
        omit_dga_stream: bool = False,
    ) -> None:
        self.device_id = device_id
        self.allocator_time_us = allocator_time_us
        self.resource_algorithm = resource_algorithm
        self.setup_failure_algorithm = setup_failure_algorithm
        self.invalid_algorithm = invalid_algorithm
        self.omit_dga_stream = omit_dga_stream
        self.config = None
        self.setup = None
        self.events: list[tuple] = []

    def begin_persistent_trial(self, config):
        self.config = dict(config)
        self.events.append(
            ("begin", config["mission"], config["algorithm"])
        )

    def prepare_persistent_call(self, setup, attempt_id):
        self.events.append(
            (
                "prepare",
                self.config["mission"],
                self.config["algorithm"],
                setup["setup_mode"],
                setup["context_id"],
            )
        )
        if self.config["algorithm"] == self.setup_failure_algorithm:
            raise ReplayStateSetupError("scripted setup failure")
        self.setup = dict(setup)

    def run_persistent_ready(self, attempt_id, timeout_seconds):
        del attempt_id, timeout_seconds
        self.events.append(
            (
                "run",
                self.config["mission"],
                self.config["algorithm"],
                self.setup["setup_mode"],
                self.setup["context_id"],
            )
        )
        if (
            self.config["algorithm"] == self.resource_algorithm
            and self.setup["setup_mode"] == "restore"
        ):
            raise ReplayAllocatorMemoryError("scripted allocator memory limit")
        total = self.allocator_time_us
        if self.config["algorithm"] == self.invalid_algorithm:
            total = -1
        if self.setup["setup_mode"] == "restore":
            goal = [1, 0]
        else:
            goal = (
                [1, 0]
                if self.config["mission"] == "bayesian"
                else [2, 1]
            )
        post_state = {
            "robot_attrs": {},
            "views": {},
            "cfg": {},
            "belief": {},
            "allocator_attrs": {},
        }
        if (
            self.config["mission"] == "bayesian"
            and self.config["algorithm"] == "DGA"
            and not self.omit_dga_stream
        ):
            post_state["robot_attrs"] = {
                "dga_rng_replay_rng_state_length": 624,
                "dga_replay_population_000_packed": True,
            }
        return {
            "status": "completed",
            "failure_type": "",
            "goal": goal,
            "messages": [{"type": "claim", "goal": goal}],
            "post_state": post_state,
            "resume_state": {},
            "allocator_time_us": total,
            "candidate_filter_time_us": 20,
            "allocator_exclusive_time_us": max(0, total - 20),
            "candidate_filter_calls": 1,
            "candidate_count_before": 3,
            "candidate_count_after": 1,
            "heap_free_before": 40000,
            "heap_free_after": 39000,
            "call_class": "full_allocation_solve",
        }

    def end_persistent_trial(self):
        self.events.append(
            ("end", self.config["mission"], self.config["algorithm"])
        )
        self.config = None
        self.setup = None

    def interrupt(self):
        self.events.append(("interrupt",))


class _IsolatingPersistentDevice(_PersistentDevice):
    def __init__(
        self,
        device_id: str,
        *,
        restarted_device_id: str | None = None,
    ) -> None:
        super().__init__(device_id)
        self.restarted_device_id = restarted_device_id or device_id
        self.restart_count = 0

    def restart_clean_worker(self):
        self.events.append(("isolate", self.restart_count))
        self.restart_count += 1
        return _identity(self.restarted_device_id)


class _TimeoutPersistentDevice(_PersistentDevice):
    def __init__(self, device_id: str) -> None:
        super().__init__(device_id)
        self.hello_failures = 1
        self.restart_count = 0

    def run_persistent_ready(self, attempt_id, timeout_seconds):
        if (
            self.config["algorithm"] == "DMCHBA"
            and self.setup["setup_mode"] == "restore"
        ):
            raise ReplayTimeout("scripted timing limit")
        return super().run_persistent_ready(attempt_id, timeout_seconds)

    def hello(self):
        self.events.append(("hello",))
        if self.hello_failures:
            self.hello_failures -= 1
            raise ReplayTransportError("scripted stale worker")
        return _identity(self.device_id)

    def start_worker(self):
        self.events.append(("restart",))
        self.restart_count += 1
        return _identity(self.device_id)


class _DgaContextRestoreFailureDevice(_PersistentDevice):
    def __init__(self, device_id: str) -> None:
        super().__init__(device_id)
        self.dga_restore_count = 0

    def prepare_persistent_call(self, setup, attempt_id):
        super().prepare_persistent_call(setup, attempt_id)
        if (
            self.config["mission"] == "bayesian"
            and self.config["algorithm"] == "DGA"
            and setup["setup_mode"] == "restore"
        ):
            self.dga_restore_count += 1
            if self.dga_restore_count == 2:
                raise ReplayStateSetupError(
                    "scripted streamed RNG restore failure"
                )


class _RunPreflightDevice:
    def __init__(self, device_id: str, build_id: str, module_hash: str) -> None:
        self.identity = SimpleNamespace(
            port="COM-test",
            device_id=device_id,
            build_id=build_id,
            implementation="micropython-test",
            frequency_hz=125_000_000,
            heap_free=100_000,
            firmware_sha256="firmware",
        )
        self.module_hash = module_hash
        self.start_count = 0
        self.exit_count = 0
        self.execute_count = 0

    def hello(self):
        return self.identity

    def check(self):
        return {
            "double_array": True,
            "motors_initialized": False,
            "sensors_initialized": False,
            "heap_free": 100_000,
            "actual_module_set_sha256": self.module_hash,
            "expected_module_set_sha256": self.module_hash,
        }

    def start_worker(self):
        self.start_count += 1
        return self.identity

    def exit(self):
        self.exit_count += 1

    def execute(self, *args, **kwargs):
        self.execute_count += 1
        raise AssertionError("legacy full-snapshot replay must not run")


def _identity(device_id: str):
    return SimpleNamespace(device_id=device_id)


def _checks(*device_ids: str):
    return {
        device_id: {
            "motors_initialized": False,
            "sensors_initialized": False,
        }
        for device_id in device_ids
    }


class PersistentPreflightTests(unittest.TestCase):
    def test_all_algorithms_restore_then_delta_on_same_context(self) -> None:
        device = _PersistentDevice("device-a")

        result = run_persistent_checks(
            [device],
            [_identity("device-a")],
            _checks("device-a"),
            calibration_repetitions=3,
        )

        self.assertTrue(result["passed"], result)
        self.assertTrue(result["motor_free"]["passed"])
        self.assertTrue(result["worker_isolation_passed"])
        self.assertTrue(
            all(
                not item["supported"]
                and not item["performed"]
                and item["passed"]
                for item in result["worker_isolation"]
            )
        )
        self.assertTrue(result["context_restore_passed"])
        self.assertEqual(len(result["context_restore"]), 1)
        self.assertGreater(
            result["context_restore"][0][
                "streamed_rng_field_count"
            ],
            0,
        )
        self.assertGreater(
            result["context_restore"][0][
                "streamed_population_field_count"
            ],
            0,
        )
        self.assertEqual(len(result["smoke"]), 2 * len(ALGORITHMS))
        for entry in result["smoke"]:
            self.assertEqual(entry["status"], "completed")
            self.assertTrue(entry["restore"]["ready_to_time"])
            if (
                entry["mission"] == "bayesian"
                and entry["algorithm"] == "DGA"
            ):
                self.assertEqual(
                    entry["delta"]["status"],
                    "replaced_by_context_restore",
                )
                self.assertTrue(entry["context_restore"]["passed"])
                self.assertTrue(
                    entry["context_restore"]["forced_pclear"]
                )
            else:
                self.assertTrue(entry["delta"]["ready_to_time"])
            self.assertEqual(entry["context_id"], "00")
        for mission in ("bayesian", "collaborative"):
            for algorithm in ALGORITHMS:
                relevant = [
                    event
                    for event in device.events
                    if len(event) >= 3
                    and event[1:3] == (mission, algorithm)
                ]
                restore_prepare = next(
                    index
                    for index, event in enumerate(relevant)
                    if event[0:4] == (
                        "prepare",
                        mission,
                        algorithm,
                        "restore",
                    )
                )
                restore_run = next(
                    index
                    for index, event in enumerate(relevant)
                    if event[0:4] == (
                        "run",
                        mission,
                        algorithm,
                        "restore",
                    )
                )
                if mission == "bayesian" and algorithm == "DGA":
                    restore_prepares = [
                        index
                        for index, event in enumerate(relevant)
                        if event[0:4] == (
                            "prepare",
                            mission,
                            algorithm,
                            "restore",
                        )
                    ]
                    restore_runs = [
                        index
                        for index, event in enumerate(relevant)
                        if event[0:4] == (
                            "run",
                            mission,
                            algorithm,
                            "restore",
                        )
                    ]
                    self.assertEqual(len(restore_prepares), 2)
                    self.assertEqual(len(restore_runs), 2)
                    self.assertLess(
                        restore_runs[0],
                        restore_prepares[1],
                    )
                    self.assertLess(
                        restore_prepares[1],
                        restore_runs[1],
                    )
                    continue
                delta_prepare = next(
                    index
                    for index, event in enumerate(relevant)
                    if event[0:4] == (
                        "prepare",
                        mission,
                        algorithm,
                        "delta",
                    )
                )
                delta_run = next(
                    index
                    for index, event in enumerate(relevant)
                    if event[0:4] == (
                        "run",
                        mission,
                        algorithm,
                        "delta",
                    )
                )
                self.assertLess(
                    restore_prepare,
                    restore_run,
                    (mission, algorithm, relevant),
                )
                self.assertLess(
                    restore_run,
                    delta_prepare,
                    (mission, algorithm, relevant),
                )
                self.assertLess(
                    delta_prepare,
                    delta_run,
                    (mission, algorithm, relevant),
                )

    def test_fresh_worker_isolates_every_smoke_and_calibration(self) -> None:
        device = _IsolatingPersistentDevice("device-a")

        result = run_persistent_checks(
            [device],
            [_identity("device-a")],
            _checks("device-a"),
            calibration_repetitions=2,
        )

        expected_restarts = 2 * len(ALGORITHMS) + 1
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["worker_isolation_passed"])
        self.assertEqual(device.restart_count, expected_restarts)
        self.assertEqual(
            len(result["worker_isolation"]),
            expected_restarts,
        )
        self.assertTrue(
            all(
                item["supported"]
                and item["attempted"]
                and item["performed"]
                and item["identity_unchanged"]
                and item["passed"]
                for item in result["worker_isolation"]
            )
        )
        self.assertEqual(
            {
                item["phase"]
                for item in result["worker_isolation"]
                if item["phase"].startswith("smoke:")
            },
            {
                f"smoke:{mission}:{algorithm}"
                for mission in ("bayesian", "collaborative")
                for algorithm in ALGORITHMS
            },
        )
        self.assertTrue(
            all(
                entry["worker_isolation"]["performed"]
                for entry in result["smoke"]
            )
        )
        self.assertTrue(
            result["calibration"][0]["worker_isolation"]["performed"]
        )
        isolate_positions = [
            index
            for index, event in enumerate(device.events)
            if event[0] == "isolate"
        ]
        begin_positions = [
            index
            for index, event in enumerate(device.events)
            if event[0] == "begin"
        ]
        self.assertEqual(len(isolate_positions), len(begin_positions))
        self.assertTrue(
            all(
                isolated < begun
                for isolated, begun in zip(
                    isolate_positions,
                    begin_positions,
                )
            )
        )

    def test_fresh_worker_identity_change_fails_closed(self) -> None:
        device = _IsolatingPersistentDevice(
            "device-a",
            restarted_device_id="device-b",
        )

        result = run_persistent_checks(
            [device],
            [_identity("device-a")],
            _checks("device-a"),
            calibration_repetitions=2,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["worker_isolation_passed"])
        self.assertFalse(result["smoke_passed"])
        self.assertFalse(result["calibration_passed"])
        self.assertFalse(
            any(event[0] == "begin" for event in device.events)
        )
        self.assertTrue(
            all(
                not item["passed"]
                and item["performed"]
                and not item["identity_unchanged"]
                and item["failure_type"] == "worker_identity_changed"
                for item in result["worker_isolation"]
            )
        )
        self.assertTrue(
            all(
                entry["status"] == "failed"
                and entry["failure_type"] == "worker_identity_changed"
                for entry in result["smoke"]
            )
        )
        calibration_attempt = result["calibration"][0]["attempts"][0]
        self.assertEqual(
            calibration_attempt["phase"],
            "worker_isolation",
        )
        self.assertEqual(
            calibration_attempt["failure_type"],
            "worker_identity_changed",
        )

    def test_allocator_resource_limit_is_deferred_only_after_ready(self) -> None:
        device = _PersistentDevice(
            "device-a",
            resource_algorithm="DMCHBA",
        )

        result = run_persistent_checks(
            [device],
            [_identity("device-a")],
            _checks("device-a"),
            calibration_repetitions=2,
        )

        self.assertTrue(result["passed"], result)
        limited = [
            entry
            for entry in result["smoke"]
            if entry["algorithm"] == "DMCHBA"
        ]
        self.assertEqual(len(limited), 2)
        for entry in limited:
            self.assertEqual(entry["status"], "resource_limited")
            self.assertTrue(entry["restore"]["setup_succeeded"])
            self.assertTrue(entry["restore"]["ready_to_time"])
            self.assertTrue(entry["restore"]["deferred_to_campaign"])

    def test_setup_failure_and_invalid_output_fail_preflight(self) -> None:
        for keyword, device in (
            (
                "setup",
                _PersistentDevice(
                    "device-a",
                    setup_failure_algorithm="PI",
                ),
            ),
            (
                "output",
                _PersistentDevice(
                    "device-a",
                    invalid_algorithm="HIPC",
                ),
            ),
        ):
            with self.subTest(keyword=keyword):
                result = run_persistent_checks(
                    [device],
                    [_identity("device-a")],
                    _checks("device-a"),
                    calibration_repetitions=2,
                )
                self.assertFalse(result["passed"])
                self.assertFalse(result["smoke_passed"])
                failures = [
                    entry
                    for entry in result["smoke"]
                    if entry.get("status") == "failed"
                ]
                self.assertTrue(failures)
                if keyword == "setup":
                    self.assertFalse(
                        failures[0]["restore"]["ready_to_time"]
                    )
                    self.assertFalse(
                        failures[0]["restore"].get(
                            "deferred_to_campaign", False
                        )
                    )
                else:
                    self.assertEqual(
                        failures[0]["restore"]["failure_type"],
                        "invalid_persistent_output",
                    )

    def test_motor_flags_and_cross_device_calibration_are_gates(self) -> None:
        first = _PersistentDevice("device-a", allocator_time_us=100)
        second = _PersistentDevice("device-b", allocator_time_us=102)
        passing = run_persistent_checks(
            [first, second],
            [_identity("device-a"), _identity("device-b")],
            _checks("device-a", "device-b"),
            calibration_repetitions=3,
        )
        self.assertTrue(passing["passed"], passing)
        self.assertTrue(passing["calibration_passed"])

        slow = _PersistentDevice("device-b", allocator_time_us=160)
        mismatched = run_persistent_checks(
            [
                _PersistentDevice("device-a", allocator_time_us=100),
                slow,
            ],
            [_identity("device-a"), _identity("device-b")],
            _checks("device-a", "device-b"),
            calibration_repetitions=3,
        )
        self.assertFalse(mismatched["passed"])
        self.assertFalse(mismatched["calibration_passed"])

        motor_checks = _checks("device-a")
        motor_checks["device-a"]["motors_initialized"] = True
        unsafe = run_persistent_checks(
            [_PersistentDevice("device-a")],
            [_identity("device-a")],
            motor_checks,
            calibration_repetitions=2,
        )
        self.assertFalse(unsafe["passed"])
        self.assertFalse(unsafe["motor_free"]["passed"])

    def test_timeout_is_deferred_only_after_worker_recovery(self) -> None:
        device = _TimeoutPersistentDevice("device-a")

        result = run_persistent_checks(
            [device],
            [_identity("device-a")],
            _checks("device-a"),
            calibration_repetitions=2,
            timeout_seconds=0.01,
        )

        self.assertTrue(result["passed"], result)
        limited = [
            entry
            for entry in result["smoke"]
            if entry["algorithm"] == "DMCHBA"
        ]
        self.assertEqual(len(limited), 2)
        self.assertGreaterEqual(device.restart_count, 1)
        for entry in limited:
            recovery = entry["restore"]["worker_recovery"]
            self.assertTrue(recovery["interrupt_succeeded"])
            self.assertTrue(recovery["worker_responsive"])
            self.assertTrue(recovery["passed"])

    def test_failed_calibration_report_has_no_nan_or_infinity(self) -> None:
        result = run_persistent_checks(
            [
                _PersistentDevice(
                    "device-a",
                    setup_failure_algorithm="CBAA",
                )
            ],
            [_identity("device-a")],
            _checks("device-a"),
            calibration_repetitions=2,
        )

        self.assertFalse(result["passed"])
        calibration = result["calibration"][0]
        self.assertIsNone(calibration["median_us"])
        self.assertIsNone(calibration["reference_median_us"])
        self.assertIsNone(calibration["deviation_fraction"])
        json.dumps(result, allow_nan=False)

    def test_missing_streamed_dga_state_fails_context_restore_gate(self) -> None:
        result = run_persistent_checks(
            [_PersistentDevice("device-a", omit_dga_stream=True)],
            [_identity("device-a")],
            _checks("device-a"),
            calibration_repetitions=2,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["context_restore_passed"])
        self.assertEqual(len(result["context_restore"]), 1)
        context = result["context_restore"][0]
        self.assertFalse(context["passed"])
        self.assertEqual(
            context["failure_type"],
            "missing_streamed_dga_state",
        )

    def test_streamed_dga_restore_failure_is_a_preflight_gate(self) -> None:
        result = run_persistent_checks(
            [_DgaContextRestoreFailureDevice("device-a")],
            [_identity("device-a")],
            _checks("device-a"),
            calibration_repetitions=2,
        )

        self.assertFalse(result["passed"])
        self.assertFalse(result["context_restore_passed"])
        context = result["context_restore"][0]
        self.assertFalse(context["passed"])
        self.assertEqual(context["status"], "failed")
        self.assertEqual(
            context["failure_type"],
            "ReplayStateSetupError",
        )
        self.assertFalse(context["setup_succeeded"])
        self.assertFalse(context["ready_to_time"])

    def test_run_preflight_gates_only_the_persistent_path(self) -> None:
        build_id = "test-build"
        module_hash = "module-hash"
        device = _RunPreflightDevice(
            "device-a",
            build_id,
            module_hash,
        )
        persistent = {
            "passed": True,
            "smoke": [{"status": "completed"}],
            "context_restore_passed": True,
            "context_restore": [{"passed": True}],
            "calibration": [{"median_us": 100.0}],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "preflight.json"
            manifest = {
                "build_id": build_id,
                "deployed_module_set_sha256": module_hash,
            }
            with (
                patch(
                    "allocator_replay.host.preflight.load_build",
                    return_value=(root, manifest),
                ),
                patch(
                    "allocator_replay.host.preflight.run_persistent_checks",
                    return_value=persistent,
                ),
                patch(
                    "allocator_replay.host.preflight.PREFLIGHT_PATH",
                    report_path,
                ),
            ):
                report = run_preflight(
                    [device],
                    build_root=root,
                    calibration_repetitions=2,
                )

        self.assertTrue(report["passed"], report)
        self.assertEqual(device.execute_count, 0)
        self.assertEqual(device.exit_count, 1)
        self.assertEqual(device.start_count, 2)
        self.assertFalse(report["legacy_replay"]["executed"])
        self.assertFalse(report["legacy_replay"]["gating"])
        self.assertTrue(report["context_restore_passed"])
        self.assertEqual(report["schema"], 3)


class PersistentPreflightLoopbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        manifest = build_device_bundle(compile_mpy=False)
        cls.build_root = Path(str(manifest["output"]))

    def test_bayesian_dga_context_is_cleared_and_stream_restored(self) -> None:
        device = LoopbackReplayDevice(
            "preflight-context-restore",
            build_root=self.build_root,
        )
        try:
            identity = device.hello()
            checks = device.check()
            clear_count_before = device.serial.context_clear_count
            result = run_persistent_checks(
                [device],
                [identity],
                {identity.device_id: checks},
                calibration_repetitions=2,
                timeout_seconds=30.0,
            )
        finally:
            device.close()

        self.assertTrue(result["passed"], result)
        self.assertTrue(result["context_restore_passed"])
        self.assertEqual(len(result["context_restore"]), 1)
        context = result["context_restore"][0]
        self.assertTrue(context["passed"])
        self.assertTrue(context["forced_pclear"])
        self.assertGreater(context["streamed_rng_field_count"], 1)
        self.assertGreater(
            context["streamed_population_field_count"],
            1,
        )
        # Twelve first-call restores, the extra DGA context restore, and two
        # calibration restores each issue PCLEAR before uploading state.
        self.assertEqual(
            device.serial.context_clear_count - clear_count_before,
            15,
        )


if __name__ == "__main__":
    unittest.main()
