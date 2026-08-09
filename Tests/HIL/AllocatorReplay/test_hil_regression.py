from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from allocator_replay.cli import build_parser
from allocator_replay.hil.bridge import AuthoritativeBridge, JsonlJournal
from allocator_replay.hil.regression import (
    DEFAULT_REGRESSION_GATES,
    HilRegressionRunner,
    RegressionBridge,
    RegressionGateComplete,
    regression_status,
    select_regression_gates,
)
from allocator_replay.host.transport import DeviceIdentity


def _identity(device_id: str) -> DeviceIdentity:
    return DeviceIdentity(
        port=f"TEST:{device_id}",
        device_id=device_id,
        build_id="test-build",
        implementation="test",
        frequency_hz=125_000_000,
        heap_free=32_000,
        firmware_sha256="test-firmware",
    )


class RegressionBridgeTests(unittest.TestCase):
    def test_safe_stop_occurs_only_after_target_and_subsequent_returned(self) -> None:
        gate = DEFAULT_REGRESSION_GATES[0]
        device = SimpleNamespace(identity=_identity("device-a"))
        with tempfile.TemporaryDirectory() as directory:
            journal_path = Path(directory) / "gate.jsonl"
            bridge = RegressionBridge(
                device=device,
                condition=gate.condition,
                trial_id=gate.trial_id,
                run_generation=3,
                journal=JsonlJournal(journal_path),
                gate=gate,
            )
            bridge.calls_by_robot[gate.robot_id] = gate.call_index

            def accepted_parent(self, allocator, robot):
                del allocator
                rid = str(robot.rid)
                index = self.calls_by_robot.get(rid, 0)
                self.calls_by_robot[rid] = index + 1
                self.accepted_call_count += 1
                return index, rid

            with patch.object(
                AuthoritativeBridge,
                "call",
                accepted_parent,
            ):
                target = bridge.call(None, SimpleNamespace(rid=gate.robot_id))
                self.assertEqual(target, (gate.call_index, gate.robot_id))
                self.assertTrue(bridge.target_fixture_id)
                self.assertFalse(bridge.target_simulator_progress_confirmed)
                self.assertFalse(bridge.subsequent_fixture_id)

                subsequent = bridge.call(None, SimpleNamespace(rid="00"))
                self.assertEqual(subsequent, (0, "00"))
                self.assertTrue(bridge.target_simulator_progress_confirmed)
                self.assertTrue(bridge.subsequent_fixture_id)
                self.assertFalse(bridge.subsequent_simulator_progress_confirmed)

                with self.assertRaises(RegressionGateComplete):
                    bridge.call(None, SimpleNamespace(rid="01"))

            self.assertTrue(bridge.passed)
            self.assertEqual(bridge.accepted_call_count, 2)
            rows = [
                json.loads(line)
                for line in journal_path.read_text(encoding="utf-8").splitlines()
            ]
        self.assertEqual(
            [row["stage"] for row in rows],
            [
                "target_response_applied_to_proxy_state",
                "target_response_consumed_by_simulator",
                "subsequent_response_applied_to_proxy_state",
                "subsequent_response_consumed_by_simulator",
            ],
        )

    def test_normal_trial_completion_can_prove_last_subsequent_call(self) -> None:
        gate = DEFAULT_REGRESSION_GATES[2]
        device = SimpleNamespace(identity=_identity("device-b"))
        with tempfile.TemporaryDirectory() as directory:
            bridge = RegressionBridge(
                device=device,
                condition=gate.condition,
                trial_id=gate.trial_id,
                run_generation=1,
                journal=JsonlJournal(Path(directory) / "gate.jsonl"),
                gate=gate,
            )
            bridge.target_fixture_id = "target"
            bridge.target_simulator_progress_confirmed = True
            bridge.subsequent_fixture_id = "subsequent"
            self.assertFalse(bridge.passed)
            bridge.confirm_normal_trial_completion()
            self.assertTrue(bridge.passed)


class RegressionScheduleTests(unittest.TestCase):
    def _root(self, directory: str) -> Path:
        root = Path(directory)
        (root / "journals").mkdir()
        gates = []
        for gate in DEFAULT_REGRESSION_GATES[:2]:
            gates.append(
                {
                    **gate.manifest_row(),
                    "status": "pending",
                    "attempt_generation": 0,
                    "device_id": "",
                    "failure_reason": "",
                    "result": {},
                }
            )
        value = {
            "schema": 2,
            "run_id": "unit",
            "status": "prepared",
            "gates": gates,
            "devices": {},
        }
        (root / "manifest.json").write_text(
            json.dumps(value),
            encoding="utf-8",
        )
        (root / "schedule.json").write_text(
            json.dumps(value),
            encoding="utf-8",
        )
        return root

    def test_runner_resumes_running_gate_and_journals_separate_results(self) -> None:
        class Device:
            def __init__(self, device_id: str) -> None:
                self.identity = _identity(device_id)

            def hello(self):
                return self.identity

            def restart_clean_worker(self):
                return self.identity

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            schedule = json.loads(
                (root / "schedule.json").read_text(encoding="utf-8")
            )
            schedule["gates"][0]["status"] = "running"
            schedule["gates"][0]["attempt_generation"] = 1
            (root / "schedule.json").write_text(
                json.dumps(schedule),
                encoding="utf-8",
            )

            def pass_gate(root, manifest, gate, generation, device):
                del root, manifest
                return {
                    "gate_id": gate.gate_id,
                    "device_id": device.identity.device_id,
                    "run_generation": generation,
                    "target_fixture_id": "target",
                    "subsequent_fixture_id": "subsequent",
                    "wall_seconds": 1.25,
                    "status": "passed",
                }

            runner = HilRegressionRunner(
                root,
                [Device("one"), Device("two")],
            )
            with patch(
                "allocator_replay.hil.regression.run_regression_trial",
                side_effect=pass_gate,
            ):
                result = runner.run()

            self.assertEqual(result["status"], "passed")
            self.assertEqual(
                {gate["status"] for gate in result["gates"]},
                {"passed"},
            )
            # The interrupted gate advances to a new, distinguishable generation.
            interrupted = next(
                gate
                for gate in result["gates"]
                if gate["gate_id"] == DEFAULT_REGRESSION_GATES[0].gate_id
            )
            self.assertEqual(interrupted["attempt_generation"], 2)
            status = regression_status(root)
            self.assertEqual(status["counts"], {"passed": 2})
            self.assertTrue(
                all(item["target_fixture_id"] == "target" for item in status["gates"])
            )


class RegressionCliTests(unittest.TestCase):
    def test_selection_and_cli_support_one_or_many_gates(self) -> None:
        chosen = select_regression_gates(
            [
                DEFAULT_REGRESSION_GATES[3].gate_id,
                DEFAULT_REGRESSION_GATES[0].gate_id,
            ]
        )
        self.assertEqual(
            [gate.gate_id for gate in chosen],
            [
                DEFAULT_REGRESSION_GATES[0].gate_id,
                DEFAULT_REGRESSION_GATES[3].gate_id,
            ],
        )
        args = build_parser().parse_args(
            [
                "hil-regression-gate",
                "--ports",
                "COM12",
                "COM13",
                "--gates",
                chosen[0].gate_id,
                chosen[1].gate_id,
            ]
        )
        self.assertEqual(args.command, "hil-regression-gate")
        self.assertEqual(args.ports, ["COM12", "COM13"])
        self.assertEqual(args.gates, [gate.gate_id for gate in chosen])

    def test_unknown_gate_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown regression gate"):
            select_regression_gates(["not-a-gate"])


if __name__ == "__main__":
    unittest.main()
