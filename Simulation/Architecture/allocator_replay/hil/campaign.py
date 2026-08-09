from __future__ import annotations

import importlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from allocator_replay.capture.tracer import (
    ALGORITHM_CLASSES,
    ALGORITHM_MODULES,
    _import_environment,
)
from allocator_replay.config.study import (
    BAYESIAN_SIM_ROOT,
    COLLABORATIVE_SIM_ROOT,
    Condition,
)
from allocator_replay.hil.bridge import (
    AuthoritativeBridge,
    HilConditionStop,
    JsonlJournal,
    make_proxy_allocator,
)
from allocator_replay.hil.manifest import (
    collaborative_scenario_for,
    load_manifest,
    save_schedule,
)
from allocator_replay.hil.memory import require_safe_commit
from allocator_replay.hil.watchdog import (
    AdaptiveWatchdogPolicy,
    HilTrialFailure,
    TrialWatchdog,
)
from allocator_replay.host.transport import ReplayTransportError


COLLABORATIVE_RUN_SEED = 839_171


def _condition(job: dict[str, Any]) -> Condition:
    return Condition(
        mission=str(job["mission"]),
        algorithm=str(job["algorithm"]),
        top_k_level=str(job["top_k_level"]),
        top_k_rate=float(job["top_k_rate"]),
        top_k_cells=int(job["top_k_cells"]),
    )


def _scenario(
    manifest: dict[str, Any],
    condition: Condition,
    trial_id: int,
):
    if condition.mission == "bayesian":
        if str(BAYESIAN_SIM_ROOT) not in sys.path:
            sys.path.insert(0, str(BAYESIAN_SIM_ROOT))
        from benchmark_sim.core.scenario_loader import load_scenarios

        path = Path(manifest["scenario_sources"]["bayesian"]["path"])
        scenarios = load_scenarios(path, max_trials=None)
    else:
        if str(COLLABORATIVE_SIM_ROOT) not in sys.path:
            sys.path.insert(0, str(COLLABORATIVE_SIM_ROOT))
        from known_visit_sim.core.scenario_loader import load_scenarios

        path = collaborative_scenario_for(manifest, trial_id)
        starts = {(0, 0), (0, 6), (0, 12), (0, 18)}
        scenarios = load_scenarios(path, 19, starts, None)
    try:
        return path, next(
            scenario for scenario in scenarios if int(scenario.trial_id) == trial_id
        )
    except StopIteration as exc:
        raise ValueError(
            f"scenario source does not contain {condition.mission} trial {trial_id}"
        ) from exc


def _decision_type(mission: str) -> type:
    if mission == "bayesian":
        from benchmark_sim.core.types import AllocationDecision
    else:
        from known_visit_sim.core.types import AllocationDecision
    return AllocationDecision


def _run_trial(
    root: Path,
    manifest: dict[str, Any],
    job: dict[str, Any],
    trial_id: int,
    generation: int,
    device: Any,
    watchdog_policy: AdaptiveWatchdogPolicy | None = None,
) -> dict[str, Any]:
    require_safe_commit()
    condition = _condition(job)
    cfg, _, comm, runner_class, base_class, _ = _import_environment(condition)
    scenario_path, scenario = _scenario(manifest, condition, trial_id)
    identity = device.identity or device.hello()
    journal = JsonlJournal(root / "journals" / f"{identity.device_id}.jsonl")
    bridge = AuthoritativeBridge(
        device=device,
        condition=condition,
        trial_id=trial_id,
        run_generation=generation,
        journal=journal,
    )
    proxy = make_proxy_allocator(base_class, bridge, _decision_type(condition.mission))
    seed = (
        trial_id * 1009
        if condition.mission == "bayesian"
        else COLLABORATIVE_RUN_SEED + trial_id * 1009
    )
    started = time.time()
    policy = watchdog_policy or AdaptiveWatchdogPolicy()

    def record_adjustment(adjustment: dict[str, Any]) -> None:
        journal.append(
            {
                "schema": 1,
                "record_type": "watchdog_threshold_adjustment",
                "campaign_mode": "pololu_authoritative_hil",
                "condition_id": condition.condition_id,
                "mission": condition.mission,
                "algorithm": condition.algorithm,
                "top_k_level": condition.top_k_level,
                "trial_id": trial_id,
                "run_generation": generation,
                "device_id": identity.device_id,
                **adjustment,
                "journaled_at": time.time(),
            }
        )

    watchdog = TrialWatchdog(
        condition.mission,
        policy,
        on_adjustment=record_adjustment,
    )
    try:
        cfg.debug_max_events = policy.event_cap
        if hasattr(cfg, "debug_max_stagnant_events"):
            cfg.debug_max_stagnant_events = policy.event_cap
        try:
            state = runner_class(
                cfg=cfg,
                allocator_cls=proxy,
                comm_model=comm,
                seed=seed,
            ).run_trial(scenario, on_step=watchdog)
        except HilTrialFailure as exc:
            journal.append(
                {
                    "schema": 1,
                    "record_type": "trial_failed",
                    "campaign_mode": "pololu_authoritative_hil",
                    "condition_id": condition.condition_id,
                    "mission": condition.mission,
                    "algorithm": condition.algorithm,
                    "top_k_level": condition.top_k_level,
                    "top_k_rate": condition.top_k_rate,
                    "top_k_cells": condition.top_k_cells,
                    "trial_id": trial_id,
                    "run_generation": generation,
                    "device_id": identity.device_id,
                    "build_id": identity.build_id,
                    "frequency_hz": identity.frequency_hz,
                    "scenario_file": str(scenario_path.resolve()),
                    "trial_status": "failed",
                    "failure_reason": exc.reason,
                    "events_processed": int(
                        exc.diagnostics.get("events_processed", 0)
                    ),
                    "allocator_call_count": bridge.accepted_call_count,
                    "wall_seconds": time.time() - started,
                    "watchdog_diagnostics": exc.diagnostics,
                    "journaled_at": time.time(),
                }
            )
            raise
    finally:
        try:
            bridge.close()
        except ReplayTransportError:
            # The incomplete generation remains in the append-only journal.
            # A resumed campaign starts the trial again with a new generation.
            pass
    robot_steps = {
        str(rid): int(robot.counters.steps_total)
        for rid, robot in state.robots.items()
    }
    row = {
        "schema": 1,
        "record_type": "trial_complete",
        "campaign_mode": "pololu_authoritative_hil",
        "condition_id": condition.condition_id,
        "mission": condition.mission,
        "algorithm": condition.algorithm,
        "top_k_level": condition.top_k_level,
        "top_k_rate": condition.top_k_rate,
        "top_k_cells": condition.top_k_cells,
        "trial_id": trial_id,
        "run_generation": generation,
        "device_id": identity.device_id,
        "build_id": identity.build_id,
        "frequency_hz": identity.frequency_hz,
        "scenario_file": str(scenario_path.resolve()),
        "scenario_sha256": (
            manifest["scenario_sources"]["bayesian"]["sha256"]
            if condition.mission == "bayesian"
            else manifest["scenario_sources"]["collaborative"][str(trial_id)]["sha256"]
        ),
        "trial_status": "completed",
        "total_team_steps": sum(robot_steps.values()),
        "max_steps_any_robot": max(robot_steps.values()) if robot_steps else 0,
        "robot_steps": robot_steps,
        "events_processed": int(state.events_processed),
        "allocator_call_count": bridge.accepted_call_count,
        "wall_seconds": time.time() - started,
        "journaled_at": time.time(),
    }
    journal.append(row)
    return row


def _weight(job: dict[str, Any]) -> float:
    factors = {
        "CBAA": 1.0,
        "ACBBA": 2.0,
        "PI": 1.8,
        "HIPC": 2.2,
        "DMCHBA": 4.0,
        "DGA": 6.0,
    }
    mission = 2.0 if job["mission"] == "collaborative" else 1.0
    return mission * factors[job["algorithm"]] * max(1, int(job["top_k_cells"])) ** 2


TERMINAL_JOB_STATUSES = {
    "complete",
    "completed_with_trial_failures",
    "stopped",
    "excluded",
}


def _failed_trial_ids(job: dict[str, Any]) -> set[int]:
    result: set[int] = set()
    for item in job.get("failed_trials", []):
        result.add(int(item["trial_id"] if isinstance(item, dict) else item))
    return result


def _trial_weight(job: dict[str, Any], trial_id: int) -> float:
    estimates = job.get("trial_work_estimates", {})
    value = estimates.get(str(trial_id)) if isinstance(estimates, dict) else None
    if value is not None:
        return float(value)
    return _weight(job)


class HilCampaignRunner:
    def __init__(self, root: Path, devices: list[Any]) -> None:
        self.root = root.resolve()
        self.devices = devices
        self.lock = threading.Lock()
        self.schedule = load_manifest(self.root)
        self.manifest = load_manifest(self.root, "manifest.json")
        self.schedule["status"] = "running"
        self.schedule.setdefault("devices", {})
        # A process restart invalidates all in-memory trial leases.  Incomplete
        # generations remain in journals and are restarted with a new generation.
        self.schedule["active_trials"] = {}
        self.policies: dict[str, AdaptiveWatchdogPolicy] = {}
        for job in self.schedule["jobs"]:
            if job["status"] == "running":
                job["status"] = "pending"
            self.policies[job["condition_id"]] = AdaptiveWatchdogPolicy()
        save_schedule(self.root, self.schedule)

    def _release_collaborative_if_ready(self) -> None:
        bayesian_ready = all(
            job["status"] in TERMINAL_JOB_STATUSES
            for job in self.schedule["jobs"]
            if job["mission"] == "bayesian"
        )
        if not bayesian_ready:
            return
        changed = False
        for job in self.schedule["jobs"]:
            if (
                job["mission"] == "collaborative"
                and job.get("device_id") == "__after_bayesian_hold__"
            ):
                job["device_id"] = ""
                changed = True
        if changed:
            self.schedule["phase"] = "collaborative"

    def _active_for_condition(self, condition_id: str) -> int:
        return sum(
            assignment.get("condition_id") == condition_id
            for assignment in self.schedule.get("active_trials", {}).values()
        )

    def _claim(self, device_id: str) -> tuple[dict[str, Any], int, int] | None:
        with self.lock:
            self._release_collaborative_if_ready()
            active = self.schedule.setdefault("active_trials", {})
            candidates: list[tuple[dict[str, Any], int]] = []
            for job in self.schedule["jobs"]:
                if job["status"] not in {"pending", "running"}:
                    continue
                pin = str(job.get("device_id") or "")
                if pin and pin != device_id:
                    continue
                completed = {int(item) for item in job["completed_trials"]}
                failed = _failed_trial_ids(job)
                for trial_value in job["trial_ids"]:
                    trial_id = int(trial_value)
                    key = f"{job['condition_id']}:{trial_id}"
                    if trial_id not in completed | failed and key not in active:
                        candidates.append((job, trial_id))
            if not candidates:
                return None

            bayesian = [item for item in candidates if item[0]["mission"] == "bayesian"]
            if bayesian:
                highest_cells = max(int(item[0]["top_k_cells"]) for item in bayesian)
                highest = [
                    item for item in bayesian
                    if int(item[0]["top_k_cells"]) == highest_cells
                ]
                if not any(
                    self._active_for_condition(item[0]["condition_id"])
                    for item in highest
                ):
                    candidates = highest

            job, trial_id = max(
                candidates,
                key=lambda item: (
                    _trial_weight(item[0], item[1]),
                    int(item[0]["top_k_cells"]),
                    item[0]["condition_id"],
                    -item[1],
                ),
            )
            job["status"] = "running"
            generations = job.setdefault("trial_generations", {})
            generation = int(generations.get(str(trial_id), 0)) + 1
            generations[str(trial_id)] = generation
            devices = job.setdefault("device_ids", [])
            if device_id not in devices:
                devices.append(device_id)
            key = f"{job['condition_id']}:{trial_id}"
            active[key] = {
                "condition_id": job["condition_id"],
                "trial_id": trial_id,
                "run_generation": generation,
                "device_id": device_id,
                "claimed_at": datetime.now(timezone.utc).isoformat(),
                "predicted_work": _trial_weight(job, trial_id),
            }
            self.schedule["devices"][device_id] = {
                "status": "running",
                "condition_id": job["condition_id"],
                "trial_id": trial_id,
                "run_generation": generation,
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
            }
            save_schedule(self.root, self.schedule)
            return job, trial_id, generation

    def _release_assignment(self, job: dict[str, Any], trial_id: int) -> None:
        key = f"{job['condition_id']}:{trial_id}"
        self.schedule.setdefault("active_trials", {}).pop(key, None)

    def _refresh_job_status(self, job: dict[str, Any]) -> None:
        if job["status"] in {"stopped", "excluded"}:
            return
        completed = {int(item) for item in job["completed_trials"]}
        failed = _failed_trial_ids(job)
        planned = {int(item) for item in job["trial_ids"]}
        if planned <= completed | failed:
            job["status"] = (
                "completed_with_trial_failures" if failed else "complete"
            )
            return
        job["status"] = (
            "running"
            if self._active_for_condition(job["condition_id"])
            else "pending"
        )

    def _save_trial(self, job: dict[str, Any], trial_id: int) -> None:
        with self.lock:
            self._release_assignment(job, trial_id)
            completed = set(int(item) for item in job["completed_trials"])
            completed.add(trial_id)
            job["completed_trials"] = sorted(completed)
            self._refresh_job_status(job)
            save_schedule(self.root, self.schedule)

    def _fail_trial(
        self,
        job: dict[str, Any],
        trial_id: int,
        generation: int,
        device_id: str,
        reason: str,
    ) -> None:
        with self.lock:
            self._release_assignment(job, trial_id)
            failures = job.setdefault("failed_trials", [])
            if not any(int(item["trial_id"]) == trial_id for item in failures):
                failures.append(
                    {
                        "trial_id": trial_id,
                        "run_generation": generation,
                        "device_id": device_id,
                        "reason": reason,
                    }
                )
            self._refresh_job_status(job)
            save_schedule(self.root, self.schedule)

    def _stop(self, job: dict[str, Any], reason: str) -> None:
        with self.lock:
            job["status"] = "stopped"
            job["stopped_reason"] = reason
            save_schedule(self.root, self.schedule)

    def _worker(self, device: Any) -> None:
        identity = device.identity or device.hello()
        restart_before_claim = False
        while True:
            if restart_before_claim:
                try:
                    restart = getattr(
                        device,
                        "restart_clean_worker",
                        None,
                    )
                    restarted = (
                        restart()
                        if callable(restart)
                        else identity
                    )
                    if (
                        restarted.device_id != identity.device_id
                        or (
                            getattr(identity, "build_id", None)
                            is not None
                            and getattr(restarted, "build_id", None)
                            != identity.build_id
                        )
                    ):
                        raise ReplayTransportError(
                            "device identity/build changed after clean restart"
                        )
                except ReplayTransportError:
                    with self.lock:
                        self.schedule["devices"].setdefault(
                            identity.device_id,
                            {},
                        )["status"] = "disconnected"
                        save_schedule(self.root, self.schedule)
                    return
            claim = self._claim(identity.device_id)
            if claim is None:
                # Other workers may currently own every available trial.  Stay
                # available until those trials finish because their completion
                # can expose more work (most importantly, releasing the held
                # collaborative phase after the final Bayesian trial).
                with self.lock:
                    work_in_flight = bool(
                        self.schedule.get("active_trials", {})
                    )
                if work_in_flight:
                    time.sleep(0.05)
                    continue
                break
            job, trial_id, generation = claim
            restart_before_claim = True
            try:
                require_safe_commit()
                _run_trial(
                    self.root,
                    self.manifest,
                    job,
                    trial_id,
                    generation,
                    device,
                    self.policies[job["condition_id"]],
                )
                self._save_trial(job, trial_id)
            except HilTrialFailure as exc:
                self._fail_trial(
                    job,
                    trial_id,
                    generation,
                    identity.device_id,
                    exc.reason,
                )
            except HilConditionStop as exc:
                with self.lock:
                    self._release_assignment(job, trial_id)
                self._stop(job, exc.reason)
            except ReplayTransportError:
                with self.lock:
                    self._release_assignment(job, trial_id)
                    job["status"] = "pending"
                    self.schedule["devices"][identity.device_id]["status"] = "disconnected"
                    save_schedule(self.root, self.schedule)
                return
            except Exception as exc:
                with self.lock:
                    self._release_assignment(job, trial_id)
                journal = JsonlJournal(
                    self.root / "journals" / f"{identity.device_id}.jsonl"
                )
                journal.append(
                    {
                        "schema": 1,
                        "record_type": "condition_error",
                        "condition_id": job["condition_id"],
                        "device_id": identity.device_id,
                        "error": f"{type(exc).__name__}: {exc}",
                        "journaled_at": time.time(),
                    }
                )
                self._stop(job, "simulator_or_host_failure")

    def run(self) -> dict[str, Any]:
        with ThreadPoolExecutor(max_workers=len(self.devices)) as executor:
            futures = [executor.submit(self._worker, device) for device in self.devices]
            for future in futures:
                future.result()
        with self.lock:
            statuses = {job["status"] for job in self.schedule["jobs"]}
            if statuses <= TERMINAL_JOB_STATUSES:
                self.schedule["status"] = "complete"
            elif "running" not in statuses:
                self.schedule["status"] = "paused"
            save_schedule(self.root, self.schedule)
            return self.schedule
