from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from allocator_replay.capture.codec import (
    canonical_json_bytes,
    decode_value,
    encode_value,
)
from allocator_replay.capture.state import (
    ALGORITHM_PREFIXES,
    OUTBOUND_METHODS,
    classify_call,
    snapshot,
)
from allocator_replay.config.study import CALL_TIMEOUT_SECONDS
from allocator_replay.host.transport import (
    ReplayAllocatorError,
    ReplayAllocatorMemoryError,
    ReplayMemoryError,
    ReplayOutputSerializationError,
    ReplayStateSetupError,
    ReplayTimeout,
    ReplayTransportError,
)
from allocator_replay.hil.persistent import (
    empty_state,
    event_batches,
    state_delta,
)


class HilConditionStop(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class JsonlJournal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def append(self, row: dict[str, Any]) -> None:
        payload = json.dumps(row, sort_keys=True, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(payload + "\n")
            handle.flush()
            os.fsync(handle.fileno())


def _fixture_sha(value: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _valid_goal(goal: Any, grid_size: int) -> bool:
    if goal is None:
        return True
    return (
        isinstance(goal, tuple)
        and len(goal) == 2
        and all(isinstance(item, int) and 0 <= item < grid_size for item in goal)
    )


def _normalize_goal(goal: Any) -> Any:
    if isinstance(goal, list) and len(goal) == 2:
        try:
            return int(goal[0]), int(goal[1])
        except (TypeError, ValueError):
            return goal
    return goal


TIMED_FAILURE_FIELDS = (
    "heap_free_before",
    "heap_free_after",
    "elapsed_until_failure_us",
)


def _timed_failure_result(exc: BaseException) -> dict[str, int]:
    method = getattr(exc, "timed_failure_diagnostics", None)
    if callable(method):
        return dict(method())
    result: dict[str, int] = {}
    for name in TIMED_FAILURE_FIELDS:
        value = getattr(exc, name, None)
        if value is not None:
            result[name] = int(value)
    return result


def _timeout_result(exc: ReplayTimeout) -> dict[str, int | float]:
    method = getattr(exc, "timeout_diagnostics", None)
    if callable(method):
        return dict(method())
    result: dict[str, int | float] = {}
    for name in (
        "heap_free_at_ready",
        "host_elapsed_us",
        "timeout_seconds",
    ):
        value = getattr(exc, name, None)
        if value is not None:
            result[name] = value
    return result


def _mutable_robot_name(name: str) -> bool:
    return name.startswith(ALGORITHM_PREFIXES) or name in {
        "candidate_count_before_filter",
        "candidate_count_after_filter",
        "max_candidate_cells",
        "_allocation_probability_source",
    }


@dataclass
class AuthoritativeBridge:
    device: Any
    condition: Any
    trial_id: int
    run_generation: int
    journal: JsonlJournal

    def __post_init__(self) -> None:
        self.calls_by_robot: dict[str, int] = {}
        self.pending_messages: dict[str, list[dict[str, Any]]] = {}
        self.accepted_call_count = 0
        self._persistent_started = False
        self._active_context: str | None = None
        self._context_states: dict[str, dict[str, Any]] = {}
        self._context_resume: dict[str, dict[str, Any]] = {}
        self._pending_events: dict[str, list[dict[str, Any]]] = {}

    @property
    def identity(self):
        return self.device.identity or self.device.hello()

    def take_messages(self, robot_id: str) -> list[dict[str, Any]] | None:
        return self.pending_messages.pop(str(robot_id), None)

    def _record(
        self,
        *,
        fixture: dict[str, Any],
        repetition: int,
        outcome: str,
        result: dict[str, Any] | None = None,
        error: str = "",
        accepted: bool = False,
    ) -> None:
        identity = self.identity
        row = {
            "schema": 1,
            "record_type": "call_attempt",
            "campaign_mode": "pololu_authoritative_hil",
            "mission": self.condition.mission,
            "algorithm": self.condition.algorithm,
            "condition_id": self.condition.condition_id,
            "top_k_level": self.condition.top_k_level,
            "top_k_rate": self.condition.top_k_rate,
            "top_k_cells": self.condition.top_k_cells,
            "trial_id": self.trial_id,
            "run_generation": self.run_generation,
            "robot_id": fixture["robot_id"],
            "call_index": fixture["call_index"],
            "fixture_id": fixture["fixture_id"],
            "fixture_sha256": fixture["fixture_sha256"],
            "attempt_id": (
                f"{fixture['fixture_id']}:{identity.device_id}:"
                f"g{self.run_generation}:r{repetition}"
            ),
            "repetition_id": repetition,
            "device_id": identity.device_id,
            "port": identity.port,
            "build_id": identity.build_id,
            "frequency_hz": identity.frequency_hz,
            "status": "completed" if accepted else "failed",
            "outcome": outcome,
            "accepted": accepted,
            "accepted_for_analysis": False,
            "error": error,
            "journaled_at": time.time(),
        }
        if result:
            for field in (
                "allocator_time_us",
                "candidate_filter_time_us",
                "allocator_exclusive_time_us",
                "candidate_filter_calls",
                "candidate_count_before",
                "candidate_count_after",
                "heap_free_before",
                "heap_free_after",
                "elapsed_until_failure_us",
                "heap_free_at_ready",
                "host_elapsed_us",
                "timeout_seconds",
                "call_class",
            ):
                row[field] = result.get(field)
        self.journal.append(row)

    def _record_phase(
        self,
        fixture: dict[str, Any],
        repetition: int,
        phase: str,
        status: str,
        outcome: str,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> None:
        identity = self.identity
        row = {
            "schema": 1,
            "record_type": "call_phase",
            "campaign_mode": "pololu_authoritative_hil_persistent",
            "condition_id": self.condition.condition_id,
            "mission": self.condition.mission,
            "algorithm": self.condition.algorithm,
            "trial_id": self.trial_id,
            "run_generation": self.run_generation,
            "robot_id": fixture["robot_id"],
            "call_index": fixture["call_index"],
            "fixture_id": fixture["fixture_id"],
            "repetition_id": repetition,
            "device_id": identity.device_id,
            "phase": phase,
            "phase_status": status,
            "outcome": outcome,
            "error": error,
            "journaled_at": time.time(),
        }
        if result:
            for name in (
                "allocator_time_us",
                "candidate_filter_time_us",
                "allocator_exclusive_time_us",
                "candidate_filter_calls",
                "candidate_count_before",
                "candidate_count_after",
                "heap_free_before",
                "heap_free_after",
                "elapsed_until_failure_us",
                "heap_free_at_ready",
                "host_elapsed_us",
                "timeout_seconds",
                "call_class",
            ):
                if name in result:
                    row[name] = result[name]
        self.journal.append(row)

    def queue_event(
        self,
        robot_id: str,
        kind: str,
        payload: Any | None = None,
        *,
        receiver: str = "",
    ) -> None:
        event = {"kind": kind}
        if payload is not None:
            event["payload"] = encode_value(payload)
        if receiver:
            event["receiver"] = receiver
        self._pending_events.setdefault(str(robot_id), []).append(event)

    def _begin_persistent(self) -> None:
        if self._persistent_started:
            return
        self.device.begin_persistent_trial(
            {
                "schema": 1,
                "trial_key": (
                    f"{self.condition.condition_id}/trial_{self.trial_id:03d}/"
                    f"generation_{self.run_generation}"
                ),
                "condition_id": self.condition.condition_id,
                "mission": self.condition.mission,
                "algorithm": self.condition.algorithm,
                "top_k_level": self.condition.top_k_level,
                "top_k_rate": self.condition.top_k_rate,
                "top_k_cells": self.condition.top_k_cells,
                "trial_id": self.trial_id,
                "run_generation": self.run_generation,
            }
        )
        self._persistent_started = True
        self._active_context = None

    def _prepare_persistent_setup(
        self,
        setup: dict[str, Any],
        attempt_id: str,
    ) -> None:
        """Restore state, then deliver queued callbacks in bounded batches.

        Large simulator message queues must not be embedded in the setup
        header.  A real robot receives and applies one radio message at a
        time; staging the callbacks after state restore gives the multiplexed
        HIL controller the same bounded transient footprint.  Every stage is
        outside the timed ``choose_goal`` region.
        """

        pending = list(setup.get("events", ()))
        state_setup = dict(setup)
        state_setup["events"] = []
        self.device.prepare_persistent_call(state_setup, attempt_id)
        for index, batch in enumerate(event_batches(pending)):
            self.device.prepare_persistent_call(
                {
                    "schema": int(setup.get("schema", 1)),
                    "fixture_id": (
                        str(setup["fixture_id"])
                        + f"/event_batch_{index:04d}"
                    ),
                    "condition_id": setup["condition_id"],
                    "mission": setup["mission"],
                    "algorithm": setup["algorithm"],
                    "context_id": setup["context_id"],
                    "setup_mode": "delta",
                    "deleted": {},
                    "events": batch,
                    "resume_state": {},
                    "pre_state": empty_state(),
                },
                attempt_id,
            )

    def close(self) -> None:
        if not self._persistent_started:
            return
        try:
            self.device.end_persistent_trial()
        finally:
            self._persistent_started = False
            self._active_context = None

    def _restart_after_failed_attempt(
        self,
        *,
        interrupt_running: bool,
    ) -> None:
        """Give every confirmation attempt a clean native VM heap."""

        expected = self.identity
        self._persistent_started = False
        self._active_context = None
        interrupt = getattr(self.device, "interrupt", None)
        if interrupt_running and callable(interrupt):
            interrupt()
        restarted = self.device.restart_clean_worker()
        if (
            restarted.device_id != expected.device_id
            or (
                getattr(expected, "build_id", None) is not None
                and getattr(restarted, "build_id", None)
                != expected.build_id
            )
        ):
            raise ReplayTransportError(
                "device identity/build changed during attempt recovery"
            )

    def _recover_attempt(
        self,
        fixture: dict[str, Any],
        repetition: int,
        trigger: str,
    ) -> None:
        try:
            self._restart_after_failed_attempt(
                interrupt_running=trigger == "allocator_timeout",
            )
        except Exception as exc:
            self._record_phase(
                fixture,
                repetition,
                "recovery",
                "failed",
                "transport_error",
                error=f"after {trigger}: {type(exc).__name__}: {exc}",
            )
            if isinstance(exc, ReplayTransportError):
                raise
            raise ReplayTransportError(
                f"worker recovery failed after {trigger}: {exc}"
            ) from exc
        self._record_phase(
            fixture,
            repetition,
            "recovery",
            "completed",
            "worker_restarted",
        )

    @staticmethod
    def _apply_minimal(robot: Any, allocator: Any, pre, returned) -> None:
        returned_robot = returned.get("robot_attrs", {})
        for name in list(pre["robot_attrs"]):
            if _mutable_robot_name(name) and name not in returned_robot:
                try:
                    delattr(robot, name)
                except AttributeError:
                    pass
        for name, value in returned_robot.items():
            if _mutable_robot_name(name):
                setattr(robot, name, decode_value(value))
        returned_allocator = returned.get("allocator_attrs", {})
        for name in list(pre["allocator_attrs"]):
            if name not in returned_allocator:
                try:
                    delattr(allocator, name)
                except AttributeError:
                    pass
        for name, value in returned_allocator.items():
            setattr(allocator, name, decode_value(value))

    def _call_persistent(self, allocator: Any, robot: Any):
        rid = str(robot.rid)
        call_index = self.calls_by_robot.get(rid, 0)
        self.calls_by_robot[rid] = call_index + 1
        pre = snapshot(robot, allocator)
        fixture = {
            "schema": 1,
            "fixture_id": (
                f"{self.condition.condition_id}/trial_{self.trial_id:03d}/"
                f"generation_{self.run_generation}/robot_{rid}/"
                f"call_{call_index:05d}"
            ),
            "condition_id": self.condition.condition_id,
            "mission": self.condition.mission,
            "algorithm": self.condition.algorithm,
            "robot_id": rid,
            "call_index": call_index,
            "pre_state": pre,
        }
        fixture["fixture_sha256"] = _fixture_sha(fixture)
        failures: list[str] = []
        events = list(self._pending_events.get(rid, []))
        accepted_result = None
        accepted_goal = None
        accepted_messages = []
        successful_confirmations: list[
            tuple[int, dict[str, Any], Any, list[dict[str, Any]]]
        ] = []
        for repetition in range(1, 4):
            attempt_id = (
                f"{fixture['fixture_id']}:{self.identity.device_id}:"
                f"g{self.run_generation}:r{repetition}"
            )
            try:
                self._begin_persistent()
                if (
                    repetition == 1
                    and self._active_context == rid
                    and rid in self._context_states
                ):
                    changed, deleted = state_delta(
                        self._context_states[rid],
                        pre,
                    )
                    mode = "delta"
                    setup_state = changed
                else:
                    mode = "restore"
                    setup_state = pre
                    deleted = {}
                setup = {
                    "schema": 1,
                    "fixture_id": fixture["fixture_id"] + "/setup",
                    "condition_id": self.condition.condition_id,
                    "mission": self.condition.mission,
                    "algorithm": self.condition.algorithm,
                    "context_id": rid,
                    "setup_mode": mode,
                    "deleted": deleted,
                    "events": events,
                    "resume_state": (
                        self._context_resume.get(rid, {})
                        if mode == "restore"
                        else {}
                    ),
                    "pre_state": setup_state,
                }
                self._prepare_persistent_setup(setup, attempt_id)
                self._record_phase(
                    fixture,
                    repetition,
                    "setup",
                    "completed",
                    "ready_to_time",
                )
                result = self.device.run_persistent_ready(
                    attempt_id,
                    CALL_TIMEOUT_SECONDS,
                )
                self._record_phase(
                    fixture,
                    repetition,
                    "timing",
                    "completed",
                    "completed",
                    result=result,
                )
                self._record_phase(
                    fixture,
                    repetition,
                    "output",
                    "completed",
                    "completed",
                )
            except ReplayTimeout as exc:
                failures.append("allocator_timeout")
                timeout_result = _timeout_result(exc)
                self._record_phase(
                    fixture,
                    repetition,
                    "timing",
                    "failed",
                    "allocator_timeout",
                    result=timeout_result,
                    error=str(exc),
                )
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome="timeout",
                    result=timeout_result,
                    error=str(exc),
                )
                self._recover_attempt(
                    fixture,
                    repetition,
                    "allocator_timeout",
                )
                continue
            except ReplayStateSetupError as exc:
                failures.append("state_setup_failure")
                self._record_phase(
                    fixture,
                    repetition,
                    "setup",
                    "failed",
                    "state_setup_failure",
                    error=str(exc),
                )
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome="state_setup_failure",
                    error=str(exc),
                )
                self._recover_attempt(
                    fixture,
                    repetition,
                    "state_setup_failure",
                )
                continue
            except ReplayAllocatorMemoryError as exc:
                failures.append("allocator_memory_failure")
                failure_result = _timed_failure_result(exc)
                self._record_phase(
                    fixture,
                    repetition,
                    "timing",
                    "failed",
                    "allocator_memory_failure",
                    result=failure_result,
                    error=str(exc),
                )
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome="allocator_memory_failure",
                    result=failure_result,
                    error=str(exc),
                )
                self._recover_attempt(
                    fixture,
                    repetition,
                    "allocator_memory_failure",
                )
                continue
            except ReplayAllocatorError as exc:
                failures.append("allocator_failure")
                failure_result = _timed_failure_result(exc)
                self._record_phase(
                    fixture,
                    repetition,
                    "timing",
                    "failed",
                    "allocator_failure",
                    result=failure_result,
                    error=str(exc),
                )
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome="allocator_failure",
                    result=failure_result,
                    error=str(exc),
                )
                self._recover_attempt(
                    fixture,
                    repetition,
                    "allocator_failure",
                )
                continue
            except ReplayOutputSerializationError as exc:
                failures.append("output_serialization_failure")
                timed = getattr(exc, "timed_result", None)
                if timed:
                    self._record_phase(
                        fixture,
                        repetition,
                        "timing",
                        "completed",
                        "completed",
                        result=timed,
                    )
                self._record_phase(
                    fixture,
                    repetition,
                    "output",
                    "failed",
                    "output_serialization_failure",
                    error=str(exc),
                )
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome="output_serialization_failure",
                    result=timed,
                    error=str(exc),
                )
                self._recover_attempt(
                    fixture,
                    repetition,
                    "output_serialization_failure",
                )
                continue
            except ReplayTransportError as exc:
                self._record_phase(
                    fixture,
                    repetition,
                    "transport",
                    "failed",
                    "transport_error",
                    error=str(exc),
                )
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome="transport_error",
                    error=str(exc),
                )
                raise
            try:
                goal = _normalize_goal(decode_value(result["goal"]))
                messages = decode_value(result["messages"])
                returned = result["post_state"]
                if not _valid_goal(goal, int(robot.grid_size)):
                    raise ValueError(f"invalid goal {goal!r}")
                if not isinstance(messages, list) or not all(
                    isinstance(item, dict) for item in messages
                ):
                    raise ValueError("messages must be a list of mappings")
                if not isinstance(returned, dict):
                    raise ValueError("minimal state must be a mapping")
            except Exception as exc:
                failures.append("invalid_output")
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome="invalid_output",
                    result=result,
                    error=str(exc),
                )
                self._recover_attempt(
                    fixture,
                    repetition,
                    "invalid_output",
                )
                continue
            if not failures:
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome="completed",
                    result=result,
                    accepted=True,
                )
                accepted_result = result
                accepted_goal = goal
                accepted_messages = messages
                break
            successful_confirmations.append(
                (repetition, result, goal, messages)
            )
            if repetition < 3:
                self._recover_attempt(
                    fixture,
                    repetition,
                    "confirmation_completed",
                )

        reason = None
        if failures.count("allocator_timeout") >= 2:
            reason = "timing_unusable_30s"
        elif failures.count("allocator_memory_failure") >= 2:
            reason = "memory_unusable"
        elif failures.count("state_setup_failure") >= 2:
            reason = "hardware_state_setup_failure"
        elif failures.count("output_serialization_failure") >= 2:
            reason = "hardware_output_serialization_failure"
        elif failures.count("invalid_output") >= 2:
            reason = "hardware_invalid_output"
        elif accepted_result is None and not successful_confirmations:
            reason = "hardware_call_failed"

        if reason is not None:
            for repetition, result, _, _ in successful_confirmations:
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome="confirmation_completed",
                    result=result,
                    accepted=False,
                )
            raise HilConditionStop(
                reason,
                f"{fixture['fixture_id']} failed: {failures}",
            )

        if accepted_result is None:
            (
                selected_repetition,
                accepted_result,
                accepted_goal,
                accepted_messages,
            ) = successful_confirmations[0]
            for repetition, result, _, _ in successful_confirmations:
                selected = repetition == selected_repetition
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome=(
                        "completed"
                        if selected
                        else "confirmation_completed"
                    ),
                    result=result,
                    accepted=selected,
                )
        returned = accepted_result["post_state"]
        self._apply_minimal(robot, allocator, pre, returned)
        # Cache the same canonical encoding that the next desktop snapshot
        # will produce.  Device output is intentionally streamed in resident
        # iteration order so it never sorts/copies a large CellIndexedMap.
        # Keeping that raw wire representation here would make the next
        # state_delta compare it with host-sorted encode_value output and
        # falsely resend the complete unchanged map.
        post = snapshot(robot, allocator)
        self._context_states[rid] = post
        self._context_resume[rid] = dict(
            accepted_result.get("resume_state", {})
        )
        self._active_context = rid
        self._pending_events[rid] = []
        call_class = str(
            accepted_result.get("call_class") or "unknown"
        )
        self.journal.append(
            {
                "schema": 1,
                "record_type": "call_classification",
                "fixture_id": fixture["fixture_id"],
                "run_generation": self.run_generation,
                "call_class": call_class,
            }
        )
        self.pending_messages[rid] = accepted_messages
        self.accepted_call_count += 1
        return accepted_goal

    def call(self, allocator: Any, robot: Any):
        if callable(getattr(self.device, "begin_persistent_trial", None)):
            return self._call_persistent(allocator, robot)
        rid = str(robot.rid)
        call_index = self.calls_by_robot.get(rid, 0)
        self.calls_by_robot[rid] = call_index + 1
        pre = snapshot(robot, allocator)
        fixture = {
            "schema": 1,
            "fixture_id": (
                f"{self.condition.condition_id}/trial_{self.trial_id:03d}/"
                f"generation_{self.run_generation}/robot_{rid}/"
                f"call_{call_index:05d}"
            ),
            "condition_id": self.condition.condition_id,
            "mission": self.condition.mission,
            "algorithm": self.condition.algorithm,
            "robot_id": rid,
            "call_index": call_index,
            "pre_state": pre,
        }
        fixture["fixture_sha256"] = _fixture_sha(fixture)
        failures: list[str] = []
        accepted_result: dict[str, Any] | None = None
        accepted_goal = None
        accepted_messages: list[dict[str, Any]] = []
        for repetition in range(1, 4):
            attempt_id = (
                f"{fixture['fixture_id']}:{self.identity.device_id}:"
                f"g{self.run_generation}:r{repetition}"
            )
            try:
                result = self.device.execute_authoritative(
                    fixture,
                    attempt_id,
                    CALL_TIMEOUT_SECONDS,
                )
            except ReplayTimeout as exc:
                failures.append("timeout")
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome="timeout",
                    error=str(exc),
                )
                try:
                    self.device.interrupt()
                except ReplayTransportError:
                    pass
                continue
            except ReplayMemoryError as exc:
                failures.append("memory_error")
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome="memory_error",
                    error=str(exc),
                )
                continue
            except ReplayTransportError as exc:
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome="transport_error",
                    error=str(exc),
                )
                raise
            if result.get("status") != "completed":
                failure = str(result.get("failure_type") or "device_failure")
                failures.append(failure)
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome=failure,
                    result=result,
                    error=str(result.get("error", "")),
                )
                continue
            try:
                goal = _normalize_goal(decode_value(result["goal"]))
                messages = decode_value(result["messages"])
                post = result["post_state"]
                if not _valid_goal(goal, int(robot.grid_size)):
                    raise ValueError(f"invalid goal {goal!r}")
                if not isinstance(messages, list) or not all(
                    isinstance(item, dict) for item in messages
                ):
                    raise ValueError("messages must be a list of mappings")
                if not all(
                    key in post for key in ("robot_attrs", "allocator_attrs")
                ):
                    raise ValueError("missing post-state sections")
            except Exception as exc:
                failures.append("invalid_output")
                self._record(
                    fixture=fixture,
                    repetition=repetition,
                    outcome="invalid_output",
                    result=result,
                    error=str(exc),
                )
                continue
            self._record(
                fixture=fixture,
                repetition=repetition,
                outcome="completed",
                result=result,
                accepted=True,
            )
            accepted_result = result
            accepted_goal = goal
            accepted_messages = messages
            break
        if accepted_result is None:
            timeout_count = failures.count("timeout")
            memory_count = failures.count("memory_error")
            if timeout_count >= 2:
                reason = "timing_unusable_30s"
            elif memory_count >= 2:
                reason = "memory_unusable"
            elif failures.count("invalid_output") >= 2:
                reason = "hardware_invalid_output"
            else:
                reason = "hardware_call_failed"
            raise HilConditionStop(
                reason,
                f"{fixture['fixture_id']} failed: {failures}",
            )
        post_mutable = accepted_result["post_state"]
        returned_robot = post_mutable["robot_attrs"]
        for name in list(pre["robot_attrs"]):
            if _mutable_robot_name(name) and name not in returned_robot:
                try:
                    delattr(robot, name)
                except AttributeError:
                    pass
        for name, value in returned_robot.items():
            if _mutable_robot_name(name):
                setattr(robot, name, decode_value(value))
        returned_allocator = post_mutable["allocator_attrs"]
        for name in list(pre["allocator_attrs"]):
            if name not in returned_allocator:
                try:
                    delattr(allocator, name)
                except AttributeError:
                    pass
        for name, value in returned_allocator.items():
            setattr(allocator, name, decode_value(value))
        post = dict(pre)
        post["robot_attrs"] = dict(pre["robot_attrs"])
        post["robot_attrs"].update(returned_robot)
        post["allocator_attrs"] = returned_allocator
        call_class = classify_call(
            self.condition.algorithm,
            pre,
            post,
            int(accepted_result.get("candidate_filter_calls") or 0),
        )
        # Append a small classification amendment instead of rewriting the
        # append-only attempt record.
        self.journal.append(
            {
                "schema": 1,
                "record_type": "call_classification",
                "fixture_id": fixture["fixture_id"],
                "run_generation": self.run_generation,
                "call_class": call_class,
            }
        )
        self.pending_messages[rid] = accepted_messages
        self.accepted_call_count += 1
        return accepted_goal


def make_proxy_allocator(base_class: type, bridge: AuthoritativeBridge, decision_type: type):
    def choose_goal(self, robot):
        goal = bridge.call(self, robot)
        return decision_type(goal=goal, debug={"source": "pololu_authoritative"})

    def make_messages(self, robot):
        pending = bridge.take_messages(str(robot.rid))
        if pending is not None:
            return pending
        return []

    def initialize(self, robot):
        # Native initialization occurs when the context is first restored on
        # the device.  The desktop proxy must not create allocator state.
        return None

    def _mapping(value):
        if isinstance(value, dict):
            return dict(value)
        values = {}
        try:
            values.update(vars(value))
        except TypeError:
            pass
        payload = values.get("payload")
        if isinstance(payload, dict):
            result = dict(payload)
            category = values.get("category")
            if category and "type" not in result:
                result["type"] = category
            sender = values.get("sender")
            if sender is not None and "sender" not in result:
                result["sender"] = sender
            return result
        return values

    def queue_message(receiver):
        def callback(self, robot, message):
            bridge.queue_event(
                str(robot.rid),
                "allocator_message",
                _mapping(message),
                receiver=receiver,
            )
            return None

        return callback

    def on_observation(self, robot, observation):
        bridge.queue_event(
            str(robot.rid),
            "on_observation",
            _mapping(observation),
        )
        return None

    def queue_callback(kind, result=True):
        def callback(self, robot, *args, **kwargs):
            del args, kwargs
            bridge.queue_event(str(robot.rid), kind)
            return result

        return callback

    attributes = {
        "choose_goal": choose_goal,
        "make_messages": make_messages,
        "initialize": initialize,
        "on_observation": on_observation,
        "on_task_set_changed": queue_callback("on_task_set_changed"),
        "recover_stalled_allocation": queue_callback(
            "recover_stalled_allocation"
        ),
        "on_collision_avoidance_activated": queue_callback(
            "on_collision_avoidance_activated"
        ),
        "_hil_authoritative": True,
    }
    for name in OUTBOUND_METHODS:
        attributes[name] = make_messages
    for name in (
        "receive_message",
        "on_message",
        "process_message",
        "handle_message",
        "handle_acbba_message",
        "handle_cbaa_message",
        "handle_pi_message",
        "handle_hipc_message",
        "handle_dga_message",
        "handle_dmchba_message",
    ):
        attributes[name] = queue_message(name)

    return type(
        f"Hil{base_class.__name__}",
        (base_class,),
        attributes,
    )
