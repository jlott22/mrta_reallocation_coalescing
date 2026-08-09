from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from allocator_replay.capture.tracer import _import_environment
from allocator_replay.config.study import (
    RESULTS_ROOT,
    Condition,
    conditions,
)
from allocator_replay.hil.bridge import (
    AuthoritativeBridge,
    HilConditionStop,
    JsonlJournal,
    make_proxy_allocator,
)
from allocator_replay.hil.campaign import (
    COLLABORATIVE_RUN_SEED,
    _decision_type,
    _scenario,
)
from allocator_replay.hil.manifest import (
    BAYESIAN_SCENARIOS,
    _collaborative_scenarios,
    implementation_provenance,
    load_manifest,
    save_schedule,
    sha256_file,
    verify_campaign_provenance,
)
from allocator_replay.hil.memory import require_safe_commit
from allocator_replay.host.transport import ReplayTransportError


HIL_REGRESSION_ROOT = RESULTS_ROOT / "RegressionRuns"
REGRESSION_SCHEMA = 2


@dataclass(frozen=True)
class RegressionGate:
    gate_id: str
    mission: str
    algorithm: str
    top_k_level: str
    trial_id: int
    robot_id: str
    call_index: int

    @property
    def condition(self) -> Condition:
        matches = [
            condition
            for condition in conditions(self.mission)
            if (
                condition.algorithm == self.algorithm
                and condition.top_k_level == self.top_k_level
            )
        ]
        if len(matches) != 1:
            raise ValueError(
                f"gate {self.gate_id} does not identify exactly one condition"
            )
        return matches[0]

    def manifest_row(self) -> dict[str, Any]:
        condition = self.condition
        return {
            **asdict(self),
            "condition_id": condition.condition_id,
            "top_k_rate": condition.top_k_rate,
            "top_k_cells": condition.top_k_cells,
        }


DEFAULT_REGRESSION_GATES = (
    RegressionGate(
        "cbaa_50_t32_r03_c64",
        "bayesian",
        "CBAA",
        "50%",
        32,
        "03",
        64,
    ),
    RegressionGate(
        "acbba_25_t32_r03_c33",
        "bayesian",
        "ACBBA",
        "25%",
        32,
        "03",
        33,
    ),
    RegressionGate(
        "acbba_50_t07_r01_c02",
        "bayesian",
        "ACBBA",
        "50%",
        7,
        "01",
        2,
    ),
    RegressionGate(
        "acbba_75_t32_r00_c38",
        "bayesian",
        "ACBBA",
        "75%",
        32,
        "00",
        38,
    ),
    RegressionGate(
        "pi_50_t50_r01_c17",
        "bayesian",
        "PI",
        "50%",
        50,
        "01",
        17,
    ),
    RegressionGate(
        "hipc_25_t50_r03_c26",
        "bayesian",
        "HIPC",
        "25%",
        50,
        "03",
        26,
    ),
    RegressionGate(
        "hipc_50_t32_r00_c40",
        "bayesian",
        "HIPC",
        "50%",
        32,
        "00",
        40,
    ),
)
REGRESSION_GATES = {gate.gate_id: gate for gate in DEFAULT_REGRESSION_GATES}


def regression_root(
    run_id: str,
    *,
    results_root: Path | None = None,
) -> Path:
    base = (results_root or HIL_REGRESSION_ROOT).resolve()
    root = (base / run_id).resolve()
    if root == base or base not in root.parents:
        raise ValueError("regression run escaped the dedicated results directory")
    return root


def select_regression_gates(values: Iterable[str] | None) -> list[RegressionGate]:
    requested = list(values or ())
    if not requested or requested == ["all"]:
        return list(DEFAULT_REGRESSION_GATES)
    if "all" in requested:
        raise ValueError("'all' cannot be combined with named regression gates")
    unknown = sorted(set(requested) - set(REGRESSION_GATES))
    if unknown:
        raise ValueError(
            "unknown regression gate(s): "
            + ", ".join(unknown)
            + "; choose from "
            + ", ".join(REGRESSION_GATES)
        )
    # Preserve the canonical order and silently collapse duplicate CLI values.
    selected = set(requested)
    return [gate for gate in DEFAULT_REGRESSION_GATES if gate.gate_id in selected]


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def prepare_regression_run(
    run_id: str,
    gates: Iterable[RegressionGate],
    *,
    build_root: Path | None = None,
    results_root: Path | None = None,
) -> Path:
    selected = list(gates)
    if not selected:
        raise ValueError("a regression run requires at least one gate")
    root = regression_root(run_id, results_root=results_root)

    gate_rows = [gate.manifest_row() for gate in selected]
    if len({row["gate_id"] for row in gate_rows}) != len(gate_rows):
        raise ValueError("regression gate IDs must be unique")

    if (root / "manifest.json").exists():
        existing = load_manifest(root, "manifest.json")
        if existing.get("gates") != gate_rows:
            raise ValueError(
                "existing regression run has a different immutable gate set"
            )
        expected = implementation_provenance(build_root)["device_build"]
        actual = existing.get("implementation", {}).get("device_build", {})
        if (
            actual.get("build_id") != expected["build_id"]
            or actual.get("manifest_sha256") != expected["manifest_sha256"]
        ):
            raise ValueError(
                "existing regression run is bound to a different device build"
            )
        return root

    if root.exists():
        raise ValueError(
            f"regression directory exists without a manifest: {root}"
        )
    if not BAYESIAN_SCENARIOS.exists():
        raise FileNotFoundError(BAYESIAN_SCENARIOS)

    collaborative = _collaborative_scenarios()
    collaborative_ids = sorted(
        {
            gate.trial_id
            for gate in selected
            if gate.mission == "collaborative"
        }
    )
    missing = [
        trial_id
        for trial_id in collaborative_ids
        if trial_id not in collaborative
    ]
    if missing:
        raise ValueError(
            "collaborative scenario source is missing trials "
            + ", ".join(str(value) for value in missing)
        )

    now = datetime.now(timezone.utc).isoformat()
    manifest = {
        "schema": REGRESSION_SCHEMA,
        "mode": "pololu_authoritative_hil_regression_gate",
        "run_id": run_id,
        "status": "prepared",
        "created_at": now,
        "implementation": implementation_provenance(build_root),
        "scenario_sources": {
            "bayesian": {
                "path": str(BAYESIAN_SCENARIOS.resolve()),
                "sha256": sha256_file(BAYESIAN_SCENARIOS),
            },
            "collaborative": {
                str(trial_id): collaborative[trial_id]
                for trial_id in collaborative_ids
            },
        },
        "gates": gate_rows,
    }
    schedule = {
        "schema": REGRESSION_SCHEMA,
        "mode": manifest["mode"],
        "run_id": run_id,
        "status": "prepared",
        "created_at": now,
        "gates": [
            {
                **row,
                "status": "pending",
                "attempt_generation": 0,
                "device_id": "",
                "failure_reason": "",
                "result": {},
            }
            for row in gate_rows
        ],
        "devices": {},
    }
    root.mkdir(parents=True, exist_ok=False)
    (root / "journals").mkdir()
    _atomic_json(root / "manifest.json", manifest)
    _atomic_json(root / "schedule.json", schedule)
    return root


class RegressionGateComplete(RuntimeError):
    """Intentional stop at the first allocator entry after the proof call."""


class RegressionBridge(AuthoritativeBridge):
    """Authoritative bridge with a safe, simulator-progress-aware stop point."""

    def __init__(self, *args: Any, gate: RegressionGate, **kwargs: Any) -> None:
        self.gate = gate
        self.target_fixture_id = ""
        self.subsequent_fixture_id = ""
        self.target_simulator_progress_confirmed = False
        self.subsequent_simulator_progress_confirmed = False
        self._stop_at_next_allocator_entry = False
        super().__init__(*args, **kwargs)

    def _checkpoint(self, stage: str, fixture_id: str) -> None:
        self.journal.append(
            {
                "schema": REGRESSION_SCHEMA,
                "record_type": "regression_gate_checkpoint",
                "campaign_mode": "pololu_authoritative_hil_regression_gate",
                "gate_id": self.gate.gate_id,
                "condition_id": self.condition.condition_id,
                "trial_id": self.trial_id,
                "run_generation": self.run_generation,
                "device_id": self.identity.device_id,
                "stage": stage,
                "fixture_id": fixture_id,
                "accepted_call_count": self.accepted_call_count,
                "journaled_at": time.time(),
            }
        )

    def call(self, allocator: Any, robot: Any):
        rid = str(robot.rid)
        call_index = self.calls_by_robot.get(rid, 0)
        if self._stop_at_next_allocator_entry:
            self.subsequent_simulator_progress_confirmed = True
            self._checkpoint(
                "subsequent_response_consumed_by_simulator",
                self.subsequent_fixture_id,
            )
            raise RegressionGateComplete(
                f"{self.gate.gate_id} reached its safe stop point"
            )

        if self.target_fixture_id and not self.target_simulator_progress_confirmed:
            self.target_simulator_progress_confirmed = True
            self._checkpoint(
                "target_response_consumed_by_simulator",
                self.target_fixture_id,
            )

        goal = super().call(allocator, robot)
        fixture_id = (
            f"{self.condition.condition_id}/trial_{self.trial_id:03d}/"
            f"generation_{self.run_generation}/robot_{rid}/"
            f"call_{call_index:05d}"
        )
        is_target = (
            rid == self.gate.robot_id and call_index == self.gate.call_index
        )
        if is_target:
            self.target_fixture_id = fixture_id
            self._checkpoint("target_response_applied_to_proxy_state", fixture_id)
        elif self.target_simulator_progress_confirmed and not self.subsequent_fixture_id:
            self.subsequent_fixture_id = fixture_id
            self._stop_at_next_allocator_entry = True
            self._checkpoint(
                "subsequent_response_applied_to_proxy_state",
                fixture_id,
            )
        return goal

    def confirm_normal_trial_completion(self) -> None:
        if self.subsequent_fixture_id:
            self.subsequent_simulator_progress_confirmed = True
            self._checkpoint(
                "trial_completed_after_subsequent_response",
                self.subsequent_fixture_id,
            )

    @property
    def passed(self) -> bool:
        return bool(
            self.target_fixture_id
            and self.target_simulator_progress_confirmed
            and self.subsequent_fixture_id
            and self.subsequent_simulator_progress_confirmed
        )


def _result_row(
    *,
    gate: RegressionGate,
    bridge: RegressionBridge,
    identity: Any,
    generation: int,
    status: str,
    wall_seconds: float,
    scenario_path: Path,
    scenario_sha256: str,
    stop_mode: str,
    error: str = "",
) -> dict[str, Any]:
    return {
        "schema": REGRESSION_SCHEMA,
        "record_type": "regression_gate_result",
        "campaign_mode": "pololu_authoritative_hil_regression_gate",
        "gate_id": gate.gate_id,
        "condition_id": gate.condition.condition_id,
        "mission": gate.mission,
        "algorithm": gate.algorithm,
        "top_k_level": gate.top_k_level,
        "trial_id": gate.trial_id,
        "target_robot_id": gate.robot_id,
        "target_call_index": gate.call_index,
        "target_fixture_id": bridge.target_fixture_id,
        "subsequent_fixture_id": bridge.subsequent_fixture_id,
        "target_simulator_progress_confirmed": (
            bridge.target_simulator_progress_confirmed
        ),
        "subsequent_simulator_progress_confirmed": (
            bridge.subsequent_simulator_progress_confirmed
        ),
        "accepted_call_count": bridge.accepted_call_count,
        "run_generation": generation,
        "device_id": identity.device_id,
        "port": identity.port,
        "build_id": identity.build_id,
        "frequency_hz": identity.frequency_hz,
        "scenario_file": str(scenario_path.resolve()),
        "scenario_sha256": scenario_sha256,
        "status": status,
        "stop_mode": stop_mode,
        "accepted_for_analysis": False,
        "error": error,
        "wall_seconds": wall_seconds,
        "journaled_at": time.time(),
    }


def run_regression_trial(
    root: Path,
    manifest: dict[str, Any],
    gate: RegressionGate,
    generation: int,
    device: Any,
) -> dict[str, Any]:
    """Run through a former failure plus one call, then stop deliberately."""

    require_safe_commit()
    condition = gate.condition
    cfg, _, comm, runner_class, base_class, _ = _import_environment(condition)
    scenario_path, scenario = _scenario(
        manifest,
        condition,
        gate.trial_id,
    )
    identity = device.identity or device.hello()
    journal = JsonlJournal(root / "journals" / f"{identity.device_id}.jsonl")
    bridge = RegressionBridge(
        device=device,
        condition=condition,
        trial_id=gate.trial_id,
        run_generation=generation,
        journal=journal,
        gate=gate,
    )
    proxy = make_proxy_allocator(
        base_class,
        bridge,
        _decision_type(condition.mission),
    )
    seed = (
        gate.trial_id * 1009
        if condition.mission == "bayesian"
        else COLLABORATIVE_RUN_SEED + gate.trial_id * 1009
    )
    started = time.time()
    stop_mode = ""
    error = ""
    try:
        runner_class(
            cfg=cfg,
            allocator_cls=proxy,
            comm_model=comm,
            seed=seed,
        ).run_trial(scenario)
        stop_mode = "trial_completed"
        bridge.confirm_normal_trial_completion()
    except RegressionGateComplete:
        stop_mode = "intentional_safe_stop"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        try:
            bridge.close()
        except ReplayTransportError:
            if not error:
                error = "ReplayTransportError: device disconnected while closing"
            raise

    if not bridge.passed:
        error = (
            f"gate target/subsequent call was not proven; "
            f"target={bridge.target_fixture_id or 'not reached'}, "
            f"subsequent={bridge.subsequent_fixture_id or 'not reached'}"
        )
        raise RuntimeError(error)

    row = _result_row(
        gate=gate,
        bridge=bridge,
        identity=identity,
        generation=generation,
        status="passed",
        wall_seconds=time.time() - started,
        scenario_path=scenario_path,
        scenario_sha256=(
            manifest["scenario_sources"]["bayesian"]["sha256"]
            if condition.mission == "bayesian"
            else manifest["scenario_sources"]["collaborative"][
                str(gate.trial_id)
            ]["sha256"]
        ),
        stop_mode=stop_mode,
    )
    journal.append(row)
    return row


class HilRegressionRunner:
    def __init__(self, root: Path, devices: list[Any]) -> None:
        if not devices:
            raise ValueError("a regression run requires at least one device")
        self.root = root.resolve()
        self.devices = devices
        self.lock = threading.Lock()
        self.manifest = load_manifest(self.root, "manifest.json")
        self.schedule = load_manifest(self.root)
        self.schedule["status"] = "running"
        self.schedule.setdefault("devices", {})
        for gate in self.schedule["gates"]:
            if gate["status"] == "running":
                gate["status"] = "pending"
                gate["device_id"] = ""
                gate["failure_reason"] = "interrupted_before_gate_result"
        save_schedule(self.root, self.schedule)

    def retry_failed(self) -> None:
        with self.lock:
            for gate in self.schedule["gates"]:
                if gate["status"] == "failed":
                    gate["status"] = "pending"
                    gate["device_id"] = ""
                    gate["failure_reason"] = ""
                    gate["result"] = {}
            save_schedule(self.root, self.schedule)

    def _claim(self, device_id: str) -> dict[str, Any] | None:
        with self.lock:
            pending = [
                gate
                for gate in self.schedule["gates"]
                if gate["status"] == "pending"
            ]
            if not pending:
                return None
            # Later former-failure calls generally take longer to reach.
            gate = max(pending, key=lambda item: int(item["call_index"]))
            gate["status"] = "running"
            gate["device_id"] = device_id
            gate["attempt_generation"] = int(
                gate.get("attempt_generation", 0)
            ) + 1
            self.schedule["devices"][device_id] = {
                "status": "running",
                "gate_id": gate["gate_id"],
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
            }
            save_schedule(self.root, self.schedule)
            return gate

    def _finish(
        self,
        gate: dict[str, Any],
        *,
        status: str,
        result: dict[str, Any] | None = None,
        failure_reason: str = "",
    ) -> None:
        with self.lock:
            gate["status"] = status
            gate["failure_reason"] = failure_reason
            gate["result"] = dict(result or {})
            device_id = str(gate.get("device_id", ""))
            if device_id:
                self.schedule["devices"].setdefault(device_id, {})[
                    "status"
                ] = "idle"
            save_schedule(self.root, self.schedule)

    @staticmethod
    def _gate(value: dict[str, Any]) -> RegressionGate:
        return RegressionGate(
            gate_id=str(value["gate_id"]),
            mission=str(value["mission"]),
            algorithm=str(value["algorithm"]),
            top_k_level=str(value["top_k_level"]),
            trial_id=int(value["trial_id"]),
            robot_id=str(value["robot_id"]),
            call_index=int(value["call_index"]),
        )

    def _record_failure(
        self,
        device: Any,
        gate: dict[str, Any],
        error: BaseException,
    ) -> None:
        identity = device.identity or device.hello()
        JsonlJournal(
            self.root / "journals" / f"{identity.device_id}.jsonl"
        ).append(
            {
                "schema": REGRESSION_SCHEMA,
                "record_type": "regression_gate_result",
                "campaign_mode": "pololu_authoritative_hil_regression_gate",
                "gate_id": gate["gate_id"],
                "condition_id": gate["condition_id"],
                "trial_id": gate["trial_id"],
                "run_generation": gate["attempt_generation"],
                "device_id": identity.device_id,
                "port": identity.port,
                "build_id": identity.build_id,
                "status": "failed",
                "accepted_for_analysis": False,
                "error": f"{type(error).__name__}: {error}",
                "journaled_at": time.time(),
            }
        )

    def _worker(self, device: Any) -> None:
        identity = device.identity or device.hello()
        while True:
            gate_row = self._claim(identity.device_id)
            if gate_row is None:
                return
            try:
                restarted = device.restart_clean_worker()
                if (
                    restarted.device_id != identity.device_id
                    or restarted.build_id != identity.build_id
                ):
                    raise ReplayTransportError(
                        "device identity/build changed after clean restart"
                    )
                result = run_regression_trial(
                    self.root,
                    self.manifest,
                    self._gate(gate_row),
                    int(gate_row["attempt_generation"]),
                    device,
                )
            except ReplayTransportError as exc:
                with self.lock:
                    gate_row["status"] = "pending"
                    gate_row["device_id"] = ""
                    gate_row["failure_reason"] = (
                        f"transport_interrupted: {type(exc).__name__}: {exc}"
                    )
                    self.schedule["devices"].setdefault(
                        identity.device_id, {}
                    )["status"] = "disconnected"
                    save_schedule(self.root, self.schedule)
                return
            except HilConditionStop as exc:
                self._record_failure(device, gate_row, exc)
                self._finish(
                    gate_row,
                    status="failed",
                    failure_reason=exc.reason,
                )
            except Exception as exc:
                self._record_failure(device, gate_row, exc)
                self._finish(
                    gate_row,
                    status="failed",
                    failure_reason=f"{type(exc).__name__}: {exc}",
                )
            else:
                self._finish(gate_row, status="passed", result=result)

    def run(self) -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=len(self.devices)) as executor:
            futures = [
                executor.submit(self._worker, device) for device in self.devices
            ]
            for future in futures:
                future.result()
        with self.lock:
            statuses = {gate["status"] for gate in self.schedule["gates"]}
            if statuses == {"passed"}:
                self.schedule["status"] = "passed"
            elif statuses <= {"passed", "failed"}:
                self.schedule["status"] = "failed"
            else:
                self.schedule["status"] = "paused"
            save_schedule(self.root, self.schedule)
            return self.schedule


def regression_status(root: Path) -> dict[str, Any]:
    schedule = load_manifest(root)
    counts: dict[str, int] = {}
    for gate in schedule["gates"]:
        status = str(gate["status"])
        counts[status] = counts.get(status, 0) + 1
    return {
        "run_id": schedule["run_id"],
        "status": schedule["status"],
        "counts": counts,
        "gates": [
            {
                "gate_id": gate["gate_id"],
                "status": gate["status"],
                "device_id": gate.get("device_id", ""),
                "attempt_generation": gate.get("attempt_generation", 0),
                "failure_reason": gate.get("failure_reason", ""),
                "target_fixture_id": gate.get("result", {}).get(
                    "target_fixture_id", ""
                ),
                "subsequent_fixture_id": gate.get("result", {}).get(
                    "subsequent_fixture_id", ""
                ),
                "wall_seconds": gate.get("result", {}).get(
                    "wall_seconds", ""
                ),
            }
            for gate in schedule["gates"]
        ],
    }


def verify_regression_run(
    root: Path,
    *,
    build_root: Path | None = None,
    device_build_ids: Iterable[str] = (),
) -> dict[str, Any]:
    return verify_campaign_provenance(
        root,
        build_root=build_root,
        device_build_ids=list(device_build_ids),
    )
