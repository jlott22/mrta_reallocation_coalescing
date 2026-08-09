"""Resumable motionless HIL campaign runner."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from allocator_replay.host.transport import ReplayTransportError

from .config import CampaignConfig, HilCondition, PolicySpec
from .epochs import build_admission_epochs
from .io import append_jsonl, atomic_json, load_json
from .manifests import PairedTrace, canonical_sha256, load_all_pairs
from .build import verify_device_build
from .preflight import build_device_binding, verified_device_row
from .runtime import PersistentEpochSession
from .schedule import verify_campaign


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity(device: Any) -> Any:
    return device.identity or device.hello()


def _condition(row: dict[str, Any]) -> HilCondition:
    return HilCondition(
        allocator=row["allocator"],
        arrival_load=row["arrival_load"],
        policy=PolicySpec(
            row["policy"], row["batch_size"], row.get("max_wait_s"), row["policy_id"]
        ),
        manifest_set_id=row["manifest_set_id"],
    )


class CoalescingCampaignRunner:
    def __init__(
        self,
        root: Path,
        config: CampaignConfig,
        devices: list[Any],
        *,
        execution_mode: str,
    ) -> None:
        if not devices:
            raise ValueError("at least one replay device is required")
        self.root = Path(root)
        self.config = config
        preliminary = verify_campaign(self.root, config)
        scheduled_binding = preliminary["device_binding"]
        if scheduled_binding.get("execution_mode") != execution_mode:
            raise RuntimeError(
                "execution mode differs from immutable campaign device binding"
            )
        current_binding = build_device_binding(
            devices,
            Path(str(scheduled_binding["build_root"])),
            execution_mode=execution_mode,
            require_compiled=execution_mode == "serial_hardware",
        )
        self.schedule = verify_campaign(
            self.root, config, device_binding=current_binding
        )
        self.device_binding = current_binding
        self.build_manifest = verify_device_build(
            Path(str(current_binding["build_root"])),
            require_compiled=execution_mode == "serial_hardware",
        )
        self.state_path = self.root / "state.json"
        self.journal_path = self.root / "journal" / "attempts.jsonl"
        self.state = load_json(self.state_path)
        self.devices = {str(_identity(item).device_id): item for item in devices}
        self.device_order = tuple(sorted(self.devices))
        self.device_provenance = {
            str(item["device_id"]): dict(item)
            for item in current_binding["devices"]
        }
        self.execution_mode = execution_mode
        self.pairs = load_all_pairs(config)
        self.assignment_index = 0

    def _save(self) -> None:
        self.state["updated_at"] = _now()
        atomic_json(self.state_path, self.state)

    def _append(self, row: dict[str, Any]) -> None:
        sealed = dict(row)
        sealed["record_sha256"] = canonical_sha256(sealed)
        append_jsonl(self.journal_path, sealed)

    def _device(self, trial_state: dict[str, Any]) -> Any:
        pinned = trial_state.get("device_id")
        if pinned:
            if pinned not in self.devices:
                raise ReplayTransportError(
                    f"trial is pinned to disconnected device {pinned}; reconnect it"
                )
            return self.devices[pinned]
        device_id = self.device_order[self.assignment_index % len(self.device_order)]
        self.assignment_index += 1
        trial_state["device_id"] = device_id
        return self.devices[device_id]

    def _base_row(
        self,
        condition: HilCondition,
        trace: PairedTrace,
        generation: int,
        device_id: str,
    ) -> dict[str, Any]:
        device = self.device_provenance[device_id]
        return {
            "schema_version": 1,
            "campaign_id": self.schedule["campaign_id"],
            "schedule_sha256": self.schedule["schedule_sha256"],
            "execution_mode": self.execution_mode,
            "device_binding_sha256": self.device_binding[
                "device_binding_sha256"
            ],
            "device_id": device_id,
            "device_build_id": device["build_id"],
            "device_module_set_sha256": device["module_set_sha256"],
            "device_source_bundle_sha256": device["source_bundle_sha256"],
            "device_firmware_sha256": device["firmware_sha256"],
            "device_implementation": device["implementation"],
            "device_frequency_hz": device["frequency_hz"],
            **condition.as_dict(),
            "trace_id": trace.trace_id,
            "arrival_rate_tasks_per_s": trace.arrival_rate_tasks_per_s,
            "paired_manifest_id": trace.paired_manifest_id,
            "paired_manifest_sha256": trace.paired_manifest_sha256,
            "run_generation": generation,
            "journaled_at": _now(),
        }

    def _run_trial(
        self,
        condition: HilCondition,
        trace: PairedTrace,
        trial_state: dict[str, Any],
    ) -> None:
        device = self._device(trial_state)
        generation = int(trial_state.get("generation", 0)) + 1
        trial_state.update(status="running", generation=generation, last_error="")
        self._save()
        restart = getattr(device, "restart_clean_worker", None)
        if callable(restart):
            restart()
        live = verified_device_row(device, self.build_manifest)
        live_identity = {key: value for key, value in live.items() if key != "port"}
        expected_identity = self.device_provenance.get(str(live["device_id"]))
        if (
            str(live["device_id"]) != str(trial_state["device_id"])
            or live_identity != expected_identity
        ):
            raise RuntimeError(
                "device build/module/firmware identity changed before trial"
            )
        session = PersistentEpochSession(
            device, condition, trace, generation, self.config.timeout_seconds
        )
        base = self._base_row(
            condition, trace, generation, session.device_id
        )
        self._append({**base, "record_type": "trial_started"})
        epochs = build_admission_epochs(trace.tasks, condition.policy)
        try:
            session.begin()
            for epoch in epochs:
                session.admit(epoch.task_ids)
                for round_index in range(self.config.allocator_rounds_per_epoch):
                    for robot_id in session.robot_ids:
                        result = session.call(
                            robot_id,
                            epoch_index=epoch.epoch_index,
                            round_index=round_index,
                            trigger_reason=epoch.trigger_reason,
                        )
                        self._append(
                            {
                                **base,
                                "record_type": "allocator_call",
                                **epoch.as_dict(),
                                **result.metrics,
                            },
                        )
            self._append(
                {
                    **base,
                    "record_type": "trial_completed",
                    "allocation_epoch_count": len(epochs),
                    "allocator_call_count": (
                        len(epochs)
                        * len(session.robot_ids)
                        * self.config.allocator_rounds_per_epoch
                    ),
                },
            )
            trial_state["status"] = "completed"
            trial_state["last_error"] = ""
            trial_state["completed_device_identity"] = dict(
                self.device_provenance[session.device_id]
            )
        finally:
            session.close()

    def run(self) -> dict[str, Any]:
        self.state["status"] = "running"
        self._save()
        technical_failures = 0
        try:
            for condition_row in self.schedule["conditions"]:
                condition = _condition(condition_row)
                condition_state = self.state["conditions"][condition.condition_id]
                condition_state["status"] = "running"
                for trial_row in condition_row["trials"]:
                    pair_id = trial_row["paired_manifest_id"]
                    trial_state = condition_state["trials"][pair_id]
                    if trial_state["status"] == "completed":
                        continue
                    trace = self.pairs[(condition.arrival_load, trial_row["trace_id"])]
                    try:
                        self._run_trial(condition, trace, trial_state)
                    except ReplayTransportError as exc:
                        trial_state["status"] = "pending"
                        trial_state["last_error"] = f"{type(exc).__name__}: {exc}"
                        self.state["status"] = "paused_transport"
                        self._save()
                        raise
                    except Exception as exc:
                        technical_failures += 1
                        trial_state["status"] = "technical_failed"
                        trial_state["last_error"] = f"{type(exc).__name__}: {exc}"
                        self._append(
                            {
                                **self._base_row(
                                    condition,
                                    trace,
                                    int(trial_state["generation"]),
                                    str(trial_state.get("device_id")),
                                ),
                                "record_type": "trial_failed",
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            },
                        )
                    self._save()
                statuses = {
                    item["status"] for item in condition_state["trials"].values()
                }
                condition_state["status"] = (
                    "completed" if statuses == {"completed"} else "completed_with_failures"
                )
                self._save()
        finally:
            for device in self.devices.values():
                try:
                    device.exit()
                except Exception:
                    pass
        self.state["status"] = (
            "completed" if technical_failures == 0 else "completed_with_failures"
        )
        self._save()
        return self.state
