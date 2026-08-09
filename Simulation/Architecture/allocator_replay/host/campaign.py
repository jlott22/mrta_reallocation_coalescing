from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from allocator_replay.capture.codec import read_trace
from allocator_replay.config.study import (
    CALL_TIMEOUT_SECONDS,
    BAYESIAN_SIM_ROOT,
    CAMPAIGN_ROOT,
    COLLABORATIVE_SIM_ROOT,
    COMMITMENT_HORIZON,
    GRID_SIZE,
    INITIAL_HARDWARE_TRIAL_COUNTS,
    REPOSITORY_ROOT,
    ROBOT_IDS,
    SIMULATION_SEED,
    TIMEOUT_CONFIRMATION_ATTEMPTS,
    Condition,
    conditions,
    minimum_capture_trial_count,
    trace_condition_root,
)
from allocator_replay.host.deployment import load_build
from allocator_replay.host.transport import (
    ReplayMemoryError,
    ReplayTimeout,
    ReplayTransportError,
    SerialReplayDevice,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _condition_lookup(condition_id: str) -> Condition:
    return next(item for item in conditions() if item.condition_id == condition_id)


def _allocator_source_paths(condition: Condition) -> list[Path]:
    root = (
        BAYESIAN_SIM_ROOT / "benchmark_sim" / "algorithms"
        if condition.mission == "bayesian"
        else COLLABORATIVE_SIM_ROOT / "known_visit_sim" / "algorithms"
    )
    paths = [root / f"{condition.algorithm}.py", root / "base.py"]
    memory = root / "memory_optimized.py"
    if (
        condition.mission == "bayesian"
        and condition.algorithm in {"CBAA", "ACBBA", "PI", "HIPC"}
    ):
        paths.append(memory)
    optimized = root / "DGA_optimized.py"
    if condition.mission == "bayesian" and condition.algorithm == "DGA":
        paths.append(optimized)
    return paths


def _trace_manifest(condition: Condition) -> tuple[Path, dict[str, Any]]:
    path = trace_condition_root(condition) / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(
            f"missing trace for {condition.condition_id}; run capture first"
        )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (
        manifest.get("status") != "complete"
        or int(manifest.get("trial_count", -1))
        < minimum_capture_trial_count(condition)
    ):
        raise RuntimeError(
            f"{condition.condition_id} does not have the required "
            f"{minimum_capture_trial_count(condition)}-trial trace"
        )
    if not manifest.get("sealed"):
        raise RuntimeError(
            f"{condition.condition_id} has not been source/trace sealed"
        )
    recorded_condition = manifest.get("condition", {})
    if (
        "top_k_level" not in recorded_condition
        and recorded_condition
    ):
        recorded_condition = {
            **recorded_condition,
            "top_k_level": condition.top_k_level,
        }
    expected_condition = {
        "condition_id": condition.condition_id,
        "mission": condition.mission,
        "algorithm": condition.algorithm,
        "top_k_level": condition.top_k_level,
        "top_k_rate": condition.top_k_rate,
        "top_k_cells": condition.top_k_cells,
    }
    if recorded_condition != expected_condition:
        raise RuntimeError(
            f"trace configuration mismatch for {condition.condition_id}"
        )
    cohort_file = Path(manifest["cohort_file"])
    if _sha256(cohort_file) != manifest["cohort_sha256"]:
        raise RuntimeError(f"cohort hash mismatch: {cohort_file}")
    for trial in manifest["trials"]:
        trace = path.parent / trial["trace"]
        if _sha256(trace) != trial.get("file_sha256"):
            raise RuntimeError(f"trace shard hash mismatch: {trace}")
    return path, manifest


def _condition_entry(
    condition: Condition,
    *,
    trial_start_index: int,
    trial_count: int,
) -> dict[str, Any]:
    path, manifest = _trace_manifest(condition)
    captured_trials = sorted(
        manifest["trials"],
        key=lambda item: int(item["trial_id"]),
    )
    if trial_start_index < 0 or trial_count < 1:
        raise ValueError(
            "trial windows require a nonnegative start and positive count"
        )
    selected_trials = captured_trials[
        trial_start_index : trial_start_index + trial_count
    ]
    if len(selected_trials) != trial_count:
        raise RuntimeError(
            f"{condition.condition_id} has {len(captured_trials)} captured trials; "
            f"cannot select [{trial_start_index}, "
            f"{trial_start_index + trial_count})"
        )
    selected_trial_ids = [
        int(item["trial_id"])
        for item in selected_trials
    ]
    configuration = {
        "mission": condition.mission,
        "algorithm": condition.algorithm,
        "top_k_level": condition.top_k_level,
        "top_k_rate": condition.top_k_rate,
        "top_k_cells": condition.top_k_cells,
        "grid_size": GRID_SIZE,
        "robot_ids": list(ROBOT_IDS),
        "commitment_horizon": COMMITMENT_HORIZON,
        "simulation_seed": SIMULATION_SEED,
        "trial_ids": selected_trial_ids,
    }
    configuration_sha256 = hashlib.sha256(
        json.dumps(
            configuration,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
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
        "trial_start_index": trial_start_index,
        "trial_count": trial_count,
        "trial_ids": selected_trial_ids,
        "captured_trial_count": int(manifest["trial_count"]),
        "fixture_total": sum(
            int(item["fixture_count"])
            for item in selected_trials
        ),
        "fixtures_completed": 0,
        "estimated_simulator_ns": int(
            sum(
                int(item.get("simulator_allocator_time_ns", 0))
                for item in selected_trials
            )
        ),
        "trace_manifest": str(path.resolve()),
        "trace_manifest_sha256": _sha256(path),
        "cohort_sha256": manifest["cohort_sha256"],
        "simulator_source_sha256": manifest["simulator_source_sha256"],
        "configuration": configuration,
        "configuration_sha256": configuration_sha256,
        "started_at": "",
        "finished_at": "",
        "failure_fixture_id": "",
    }


def create_campaign(
    campaign_id: str,
    *,
    selected: Iterable[Condition] | None = None,
    build_root: Path | None = None,
    trial_windows: dict[str, tuple[int, int]] | None = None,
) -> Path:
    root = CAMPAIGN_ROOT / campaign_id
    schedule_path = root / "schedule.json"
    if schedule_path.exists():
        return root
    build_path, build_manifest = load_build(build_root)
    chosen = list(selected or conditions())
    windows = trial_windows or {
        mission: (0, count)
        for mission, count in INITIAL_HARDWARE_TRIAL_COUNTS.items()
    }
    entries = {
        condition.condition_id: _condition_entry(
            condition,
            trial_start_index=windows[condition.mission][0],
            trial_count=windows[condition.mission][1],
        )
        for condition in chosen
    }
    provenance = build_manifest.get("source_provenance", {})
    for condition in chosen:
        manifest = json.loads(
            Path(entries[condition.condition_id]["trace_manifest"]).read_text(
                encoding="utf-8"
            )
        )
        captured_files = manifest.get("simulator_source_files")
        if not captured_files:
            raise RuntimeError(
                f"{condition.condition_id} is not source-sealed; rerun capture "
                "without --force after trace generation completes"
            )
        for source_string, captured_hash in captured_files.items():
            source = Path(source_string)
            if not source.exists() or _sha256(source) != captured_hash:
                raise RuntimeError(
                    f"captured simulator source changed: {source}"
                )
        for source in _allocator_source_paths(condition):
            source_string = str(source.resolve())
            if provenance.get(source_string) != _sha256(source):
                raise RuntimeError(
                    f"device port provenance does not match {source}"
                )
    state = {
        "schema": 1,
        "campaign_id": campaign_id,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "status": "ready",
        "build_id": build_manifest["build_id"],
        "build_manifest": str((build_path / "manifest.json").resolve()),
        "build_manifest_sha256": _sha256(build_path / "manifest.json"),
        "study_configuration_sha256": _sha256(
            REPOSITORY_ROOT
            / "Simulation"
            / "Architecture"
            / "allocator_replay"
            / "config"
            / "study.py"
        ),
        "timeout_seconds": CALL_TIMEOUT_SECONDS,
        "confirmation_attempts": TIMEOUT_CONFIRMATION_ATTEMPTS,
        "trial_windows": {
            mission: {
                "start_index": start,
                "trial_count": count,
            }
            for mission, (start, count) in windows.items()
        },
        "devices": {},
        "conditions": entries,
    }
    root.mkdir(parents=True, exist_ok=True)
    (root / "journals").mkdir(exist_ok=True)
    _atomic_json(schedule_path, state)
    return root


def _load_fixtures(entry: dict[str, Any]) -> list[dict[str, Any]]:
    manifest_path = Path(entry["trace_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent
    selected_trial_ids = {
        int(trial_id)
        for trial_id in entry.get(
            "trial_ids",
            [item["trial_id"] for item in manifest["trials"]],
        )
    }
    fixtures: list[dict[str, Any]] = []
    for trial in manifest["trials"]:
        if int(trial["trial_id"]) not in selected_trial_ids:
            continue
        fixtures.extend(read_trace(root / trial["trace"]))
    fixtures.sort(
        key=lambda fixture: (
            float(fixture.get("risk_score", 0.0)),
            fixture["fixture_id"],
        ),
        reverse=True,
    )
    return fixtures


class CampaignRunner:
    def __init__(
        self,
        campaign_root: Path,
        devices: Iterable[SerialReplayDevice],
    ) -> None:
        self.root = campaign_root.resolve()
        self.schedule_path = self.root / "schedule.json"
        self.state: dict[str, Any] = json.loads(
            self.schedule_path.read_text(encoding="utf-8")
        )
        self.devices = list(devices)
        self.lock = threading.RLock()
        self.attempts: dict[str, dict[str, Any]] = {}
        self.accepted_fixtures: set[str] = set()
        self._load_journals()
        self._register_devices()

    def _journal_path(self, device_id: str) -> Path:
        return self.root / "journals" / f"{_safe_name(device_id)}.jsonl"

    def _load_journals(self) -> None:
        for path in (self.root / "journals").glob("*.jsonl"):
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    self.attempts[record["attempt_id"]] = record
                    if record.get("accepted"):
                        self.accepted_fixtures.add(record["fixture_id"])
        for entry in self.state["conditions"].values():
            prefix = entry["condition_id"] + "/"
            entry["fixtures_completed"] = sum(
                fixture_id.startswith(prefix)
                for fixture_id in self.accepted_fixtures
            )

    def _register_devices(self) -> None:
        now = _utc_now()
        for device in self.devices:
            identity = device.identity or device.hello()
            self.state["devices"][identity.device_id] = {
                "device_id": identity.device_id,
                "port": identity.port,
                "build_id": identity.build_id,
                "implementation": identity.implementation,
                "frequency_hz": identity.frequency_hz,
                "heap_free_at_start": identity.heap_free,
                "firmware_sha256": identity.firmware_sha256,
                "status": "ready",
                "current_condition_id": "",
                "last_seen_at": now,
                "fixtures_completed": 0,
                "failures": 0,
            }
        self.state["status"] = "running"
        self._save()

    def _save(self) -> None:
        self.state["updated_at"] = _utc_now()
        _atomic_json(self.schedule_path, self.state)

    def _append(self, device_id: str, record: dict[str, Any]) -> None:
        record["journaled_at"] = _utc_now()
        path = self._journal_path(device_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.attempts[record["attempt_id"]] = record
        if record.get("accepted"):
            self.accepted_fixtures.add(record["fixture_id"])

    def _claim(self, device_id: str) -> str | None:
        with self.lock:
            # A disconnected device's condition remains pinned.  When that
            # same unique ID returns, it resumes before new work is assigned.
            pinned = [
                entry
                for entry in self.state["conditions"].values()
                if entry["pinned_device_id"] == device_id
                and entry["status"] in {"paused", "running"}
            ]
            if pinned:
                entry = pinned[0]
            else:
                candidates = [
                    entry
                    for entry in self.state["conditions"].values()
                    if entry["status"] == "pending"
                    and not entry["pinned_device_id"]
                ]
                if not candidates:
                    return None
                # Longest-predicted processing time first gives stable,
                # balanced finishing times as devices become available.
                entry = max(
                    candidates,
                    key=lambda item: (
                        int(item["estimated_simulator_ns"]),
                        int(item["fixture_total"]),
                        item["condition_id"],
                    ),
                )
                entry["pinned_device_id"] = device_id
            if device_id not in entry["device_ids"]:
                entry["device_ids"].append(device_id)
            entry["mixed_device"] = len(entry["device_ids"]) > 1
            entry["status"] = "running"
            entry["started_at"] = entry["started_at"] or _utc_now()
            device = self.state["devices"][device_id]
            device["status"] = "running"
            device["current_condition_id"] = entry["condition_id"]
            self._save()
            return str(entry["condition_id"])

    @staticmethod
    def _base_record(
        identity,
        fixture: dict[str, Any],
        attempt_id: str,
        repetition: int,
    ) -> dict[str, Any]:
        return {
            "schema": 1,
            "attempt_id": attempt_id,
            "condition_id": fixture["condition_id"],
            "fixture_id": fixture["fixture_id"],
            "fixture_sha256": fixture["fixture_sha256"],
            "repetition_id": repetition,
            "mission": fixture["mission"],
            "algorithm": fixture["algorithm"],
            "top_k_level": fixture.get("top_k_level")
            or _condition_lookup(fixture["condition_id"]).top_k_level,
            "top_k_rate": fixture["top_k_rate"],
            "top_k_cells": fixture["top_k_cells"],
            "trial_id": fixture["trial_id"],
            "robot_id": fixture["robot_id"],
            "call_index": fixture["call_index"],
            "call_class": fixture["call_class"],
            "device_id": identity.device_id,
            "port": identity.port,
            "build_id": identity.build_id,
            "frequency_hz": identity.frequency_hz,
            "accepted": False,
        }

    def _execute_fixture(
        self,
        device: SerialReplayDevice,
        fixture: dict[str, Any],
        *,
        _transport_retry_depth: int = 0,
    ) -> tuple[bool, str]:
        identity = device.identity or device.hello()
        fixture_key = fixture["fixture_sha256"][:16]
        results: list[dict[str, Any]] = []
        for repetition in range(1, TIMEOUT_CONFIRMATION_ATTEMPTS + 1):
            base_attempt_id = (
                f"{fixture['condition_id']}:{fixture_key}:"
                f"{identity.device_id}:r{repetition}"
            )
            attempt_id = base_attempt_id
            existing = self.attempts.get(attempt_id)
            transport_retry = 0
            while (
                existing is not None
                and existing.get("outcome") == "transport_error"
            ):
                transport_retry += 1
                attempt_id = f"{base_attempt_id}:transport_retry{transport_retry}"
                existing = self.attempts.get(attempt_id)
            if existing is not None:
                record = existing
            else:
                record = self._base_record(
                    identity,
                    fixture,
                    attempt_id,
                    repetition,
                )
                record["host_started_at"] = _utc_now()
                try:
                    result = device.execute(
                        fixture,
                        attempt_id,
                        CALL_TIMEOUT_SECONDS,
                    )
                    record.update(result)
                    record["outcome"] = (
                        "completed"
                        if result.get("status") == "completed"
                        else str(result.get("failure_type") or "device_failure")
                    )
                except ReplayMemoryError as exc:
                    record.update(
                        {
                            "status": "failed",
                            "failure_type": "memory_error",
                            "outcome": "memory_error",
                            "error": str(exc),
                            "timing_attempt_counted": False,
                            "allocator_time_us": None,
                            "candidate_filter_time_us": None,
                            "allocator_exclusive_time_us": None,
                        }
                    )
                except ReplayTimeout:
                    record.update(
                        {
                            "status": "failed",
                            "failure_type": "timing_timeout",
                            "outcome": "timing_timeout",
                            "allocator_time_us": None,
                            "candidate_filter_time_us": None,
                            "allocator_exclusive_time_us": None,
                        }
                    )
                    # Interrupting occurs after the acknowledged 30-second
                    # timing attempt and outside the measured region.
                    device.interrupt()
                except ReplayTransportError as exc:
                    record.update(
                        {
                            "status": "failed",
                            "failure_type": "transport_error",
                            "outcome": "transport_error",
                            "error": str(exc),
                            "timing_attempt_counted": False,
                        }
                    )
                    with self.lock:
                        self._append(identity.device_id, record)
                    if _transport_retry_depth < 2:
                        try:
                            device.hello()
                        except ReplayTransportError:
                            raise exc
                        return self._execute_fixture(
                            device,
                            fixture,
                            _transport_retry_depth=_transport_retry_depth + 1,
                        )
                    raise
                record["host_finished_at"] = _utc_now()
                record.setdefault("timing_attempt_counted", True)
                if (
                    repetition == 1
                    and record.get("outcome") == "completed"
                ):
                    record["accepted"] = True
                with self.lock:
                    self._append(identity.device_id, record)
            results.append(record)
            if repetition == 1 and record.get("outcome") == "completed":
                return True, ""
            if repetition == 1:
                continue
            # Once a first attempt fails, always collect all three results.
        outcomes = [str(record.get("outcome", "")) for record in results]
        counts = {outcome: outcomes.count(outcome) for outcome in set(outcomes)}
        if counts.get("timing_timeout", 0) >= 2:
            return False, "timing_unusable_30s"
        if counts.get("memory_error", 0) >= 2:
            return False, "memory_unusable"
        if (
            counts.get("parity_failure", 0)
            + counts.get("verification_memory_error", 0)
            >= 2
        ):
            return False, "parity_invalid"
        if counts.get("device_exception", 0) >= 2:
            return False, "device_error"
        return False, "unstable_device_failure"

    def _finish_condition(
        self,
        device_id: str,
        condition_id: str,
        *,
        classification: str,
        failure_fixture_id: str = "",
    ) -> None:
        with self.lock:
            entry = self.state["conditions"][condition_id]
            entry["status"] = "complete" if classification == "hardware_feasible_30s" else "stopped"
            entry["classification"] = classification
            entry["finished_at"] = _utc_now()
            entry["failure_fixture_id"] = failure_fixture_id
            device = self.state["devices"][device_id]
            device["status"] = "ready"
            device["current_condition_id"] = ""
            if classification != "hardware_feasible_30s":
                device["failures"] += 1
            self._save()

    def _pause_condition(
        self,
        device_id: str,
        condition_id: str,
        error: str,
    ) -> None:
        with self.lock:
            entry = self.state["conditions"][condition_id]
            entry["status"] = "paused"
            entry["pause_reason"] = error
            device = self.state["devices"][device_id]
            device["status"] = "disconnected"
            device["current_condition_id"] = condition_id
            device["last_error"] = error
            self._save()

    def _worker(self, device: SerialReplayDevice) -> None:
        identity = device.identity or device.hello()
        device_id = identity.device_id
        while True:
            condition_id = self._claim(device_id)
            if condition_id is None:
                with self.lock:
                    self.state["devices"][device_id]["status"] = "idle"
                    self._save()
                return
            entry = self.state["conditions"][condition_id]
            try:
                fixtures = _load_fixtures(entry)
                for fixture in fixtures:
                    if fixture["fixture_id"] in self.accepted_fixtures:
                        continue
                    passed, classification = self._execute_fixture(device, fixture)
                    if not passed:
                        self._finish_condition(
                            device_id,
                            condition_id,
                            classification=classification,
                            failure_fixture_id=fixture["fixture_id"],
                        )
                        break
                    with self.lock:
                        entry["fixtures_completed"] += 1
                        device_state = self.state["devices"][device_id]
                        device_state["fixtures_completed"] += 1
                        device_state["last_seen_at"] = _utc_now()
                        self._save()
                else:
                    if entry["fixtures_completed"] != entry["fixture_total"]:
                        raise RuntimeError(
                            f"journal count mismatch for {condition_id}: "
                            f"{entry['fixtures_completed']} != {entry['fixture_total']}"
                        )
                    self._finish_condition(
                        device_id,
                        condition_id,
                        classification="hardware_feasible_30s",
                    )
            except ReplayTransportError as exc:
                self._pause_condition(device_id, condition_id, str(exc))
                return
            except Exception as exc:
                self._pause_condition(
                    device_id,
                    condition_id,
                    f"host_error: {type(exc).__name__}: {exc}",
                )
                return

    def run(self) -> dict[str, Any]:
        threads = [
            threading.Thread(
                target=self._worker,
                args=(device,),
                name=f"allocator-replay-{(device.identity or device.hello()).device_id}",
                daemon=False,
            )
            for device in self.devices
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with self.lock:
            states = {
                entry["status"] for entry in self.state["conditions"].values()
            }
            if states <= {"complete", "stopped"}:
                self.state["status"] = "complete"
            elif "paused" in states:
                self.state["status"] = "paused"
            else:
                self.state["status"] = "incomplete"
            self._save()
        return self.state


def reassign_condition(
    campaign_root: Path,
    condition_id: str,
    new_device_id: str,
) -> dict[str, Any]:
    schedule = campaign_root / "schedule.json"
    state = json.loads(schedule.read_text(encoding="utf-8"))
    if condition_id not in state["conditions"]:
        raise KeyError(condition_id)
    entry = state["conditions"][condition_id]
    old_device_id = entry.get("pinned_device_id", "")
    if entry["status"] not in {"paused", "pending"}:
        raise RuntimeError("only paused or pending conditions can be reassigned")
    entry["pinned_device_id"] = new_device_id
    if new_device_id not in entry["device_ids"]:
        entry["device_ids"].append(new_device_id)
    entry["mixed_device"] = len(entry["device_ids"]) > 1
    entry["status"] = "paused"
    entry.setdefault("reassignments", []).append(
        {
            "at": _utc_now(),
            "from_device_id": old_device_id,
            "to_device_id": new_device_id,
        }
    )
    state["updated_at"] = _utc_now()
    _atomic_json(schedule, state)
    return entry
