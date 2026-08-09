"""Persistent device session with an allocator-only timing boundary."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from allocator_replay.capture.codec import decode_value

from .config import HilCondition
from .manifests import PairedTrace


EMPTY_SECTIONS = ("robot_attrs", "views", "cfg", "belief", "allocator_attrs")


def empty_state() -> dict[str, dict[str, Any]]:
    return {name: {} for name in EMPTY_SECTIONS}


def _identity(device: Any) -> Any:
    return device.identity or device.hello()


@dataclass
class CallResult:
    goal: tuple[int, int] | None
    messages: list[dict[str, Any]]
    metrics: dict[str, Any]


class PersistentEpochSession:
    """Keep one controller VM alive while multiplexing four robot contexts."""

    def __init__(
        self,
        device: Any,
        condition: HilCondition,
        trace: PairedTrace,
        generation: int,
        timeout_seconds: float,
    ) -> None:
        self.device = device
        self.condition = condition
        self.trace = trace
        self.generation = int(generation)
        self.timeout_seconds = float(timeout_seconds)
        self.robot_ids = tuple(item["robot_id"] for item in trace.robot_starts)
        self.starts = {
            item["robot_id"]: [int(item["x"]), int(item["y"])]
            for item in trace.robot_starts
        }
        self.all_cells = [
            [int(item["x"]), int(item["y"])] for item in trace.tasks
        ]
        self.cells_by_task = {
            str(item["task_id"]): [int(item["x"]), int(item["y"])]
            for item in trace.tasks
        }
        self.active_task_ids: list[str] = []
        self.pending_admission_task_ids: list[str] = []
        self.epoch_admission_task_ids: dict[int, tuple[str, ...]] = {}
        self.epoch_trigger_reasons: dict[int, str] = {}
        self.last_epoch_by_robot: dict[str, int] = {
            rid: -1 for rid in self.robot_ids
        }
        self.context_state = {rid: empty_state() for rid in self.robot_ids}
        # Internal envelopes retain the emitting epoch so a context switch can
        # reset old consensus before, but current-epoch consensus after, the
        # visible-set-growth hook.
        self.pending_messages: dict[str, list[dict[str, Any]]] = {
            rid: [] for rid in self.robot_ids
        }
        self.sequence = {rid: 0 for rid in self.robot_ids}
        self.started = False

    @property
    def device_id(self) -> str:
        return str(_identity(self.device).device_id)

    def begin(self) -> None:
        if self.started:
            return
        trial_key = (
            f"{self.condition.condition_id}/{self.trace.paired_manifest_id}/"
            f"generation_{self.generation}"
        )
        self.device.begin_persistent_trial(
            {
                "schema": 1,
                "trial_key": trial_key,
                "condition_id": self.condition.condition_id,
                "mission": "collaborative",
                "algorithm": self.condition.allocator,
                "arrival_load": self.condition.arrival_load,
                "policy": self.condition.policy.policy,
                "batch_size": self.condition.policy.batch_size,
                "max_wait_s": self.condition.policy.max_wait_s,
                "paired_manifest_id": self.trace.paired_manifest_id,
                "run_generation": self.generation,
                "grid_size": self.trace.grid_size,
                "robot_ids": list(self.robot_ids),
                "all_tasks": self.all_cells,
                "max_candidate_cells": None,
                "candidate_mode": "unrestricted",
                "seed": self.trace.runtime_seed,
                "commitment_horizon": 3,
            }
        )
        self.started = True

    def close(self) -> None:
        if not self.started:
            return
        try:
            self.device.end_persistent_trial()
        finally:
            self.started = False

    def admit(self, task_ids: list[str] | tuple[str, ...]) -> None:
        for task_id in task_ids:
            if task_id not in self.cells_by_task:
                raise ValueError(f"unknown admitted task: {task_id}")
            if task_id not in self.active_task_ids:
                self.active_task_ids.append(task_id)
                self.pending_admission_task_ids.append(task_id)

    def _allocation_epoch_event(
        self,
        robot_id: str,
        epoch_index: int,
        trigger_reason: str,
    ) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
        """Return this robot's once-per-epoch visible-set-growth event.

        Admissions are associated with the first call of an epoch.  The same
        immutable metadata is then delivered to each robot on its first round
        for that epoch.  Later consensus rounds do not re-run the reset hook.
        """

        epoch_index = int(epoch_index)
        if epoch_index < 0:
            raise ValueError("allocation epoch index must be non-negative")
        trigger_reason = str(trigger_reason)
        if not trigger_reason:
            raise ValueError("allocation epoch trigger reason is required")

        if epoch_index not in self.epoch_admission_task_ids:
            latest = max(self.epoch_admission_task_ids, default=-1)
            if epoch_index <= latest:
                raise ValueError("allocation epochs must be introduced in order")
            admitted = tuple(self.pending_admission_task_ids)
            self.pending_admission_task_ids = []
            self.epoch_admission_task_ids[epoch_index] = admitted
            self.epoch_trigger_reasons[epoch_index] = trigger_reason
        else:
            if self.pending_admission_task_ids:
                raise ValueError("tasks were admitted after an allocation epoch began")
            admitted = self.epoch_admission_task_ids[epoch_index]
            if self.epoch_trigger_reasons[epoch_index] != trigger_reason:
                raise ValueError("allocation epoch trigger reason changed between calls")

        previous = self.last_epoch_by_robot[robot_id]
        if epoch_index < previous:
            raise ValueError("robot allocation epochs must be called in order")
        if epoch_index == previous:
            return None, admitted

        payload = {
            "epoch_index": epoch_index,
            "trigger_reason": trigger_reason,
            "admitted_task_ids": list(admitted),
            "admitted_cells": [
                list(self.cells_by_task[task_id]) for task_id in admitted
            ],
        }
        return {"kind": "allocation_epoch", "payload": payload}, admitted

    def _setup(
        self,
        robot_id: str,
        fixture_id: str,
        allocation_epoch_event: dict[str, Any] | None,
    ) -> dict[str, Any]:
        prior = self.context_state[robot_id]
        active_cells = [self.cells_by_task[item] for item in self.active_task_ids]
        self.sequence[robot_id] += 1
        state = empty_state()
        state["robot_attrs"] = {
            "rid": robot_id,
            "robot_id": robot_id,
            "pos": self.starts[robot_id],
            "grid_size": self.trace.grid_size,
            "sequence": self.sequence[robot_id],
        }
        state["views"] = {
            "all_tasks": self.all_cells,
            "active_tasks": active_cells,
            "peer_positions": {
                rid: cell for rid, cell in self.starts.items() if rid != robot_id
            },
            "target_p": [1.0 for _ in self.all_cells],
        }
        state["cfg"] = {
            "robot_ids": list(self.robot_ids),
            "all_tasks": self.all_cells,
            "max_candidate_cells": None,
            "candidate_mode": "unrestricted",
            "commitment_horizon": 3,
        }
        state["allocator_attrs"] = dict(prior.get("allocator_attrs", {}))
        pending = self.pending_messages[robot_id]
        events: list[dict[str, Any]] = []
        if allocation_epoch_event is not None:
            current_epoch = int(
                allocation_epoch_event["payload"]["epoch_index"]
            )
            # Old-round traffic belongs to the consensus state invalidated by
            # task-set growth.  Traffic already emitted in this epoch must be
            # applied after the reset so sequential robot calls can converge.
            events.extend(
                {
                    "kind": "allocator_message",
                    "payload": envelope["payload"],
                }
                for envelope in pending
                if int(envelope["epoch_index"]) < current_epoch
            )
            events.append(allocation_epoch_event)
            events.extend(
                {
                    "kind": "allocator_message",
                    "payload": envelope["payload"],
                }
                for envelope in pending
                if int(envelope["epoch_index"]) >= current_epoch
            )
        else:
            events.extend(
                {
                    "kind": "allocator_message",
                    "payload": envelope["payload"],
                }
                for envelope in pending
            )
        self.pending_messages[robot_id] = []
        return {
            "schema": 1,
            "fixture_id": fixture_id,
            "condition_id": self.condition.condition_id,
            "mission": "collaborative",
            "algorithm": self.condition.allocator,
            "context_id": robot_id,
            "setup_mode": "restore",
            "deleted": {},
            "events": events,
            "resume_state": {},
            "pre_state": state,
        }

    def call(
        self,
        robot_id: str,
        *,
        epoch_index: int,
        round_index: int,
        trigger_reason: str,
    ) -> CallResult:
        self.begin()
        fixture_id = (
            f"{self.condition.condition_id}/{self.trace.paired_manifest_id}/"
            f"g{self.generation}/epoch_{epoch_index:04d}/round_{round_index:02d}/"
            f"robot_{robot_id}"
        )
        attempt_id = fixture_id + ":" + self.device_id
        epoch_event, admitted_task_ids = self._allocation_epoch_event(
            robot_id, epoch_index, trigger_reason
        )
        setup = self._setup(robot_id, fixture_id, epoch_event)
        total_started = time.perf_counter_ns()
        if callable(getattr(self.device, "prepare_persistent_call", None)):
            setup_started = time.perf_counter_ns()
            self.device.prepare_persistent_call(setup, attempt_id)
            setup_host_us = max(0, (time.perf_counter_ns() - setup_started) // 1000)
            run_started = time.perf_counter_ns()
            result = self.device.run_persistent_ready(attempt_id, self.timeout_seconds)
            timed_output_host_us = max(0, (time.perf_counter_ns() - run_started) // 1000)
        else:
            setup_host_us = None
            result = self.device.execute_persistent(setup, attempt_id, self.timeout_seconds)
            timed_output_host_us = None
        total_host_us = max(0, (time.perf_counter_ns() - total_started) // 1000)
        if result.get("status") != "completed":
            raise RuntimeError(
                f"device allocator call failed: {result.get('failure_type', 'unknown')}"
            )
        if epoch_event is not None:
            # Do not mark delivery before the authoritative call completes;
            # a transport retry must receive the same idempotent event.
            self.last_epoch_by_robot[robot_id] = int(epoch_index)
        before = int(result.get("candidate_count_before", 0))
        after = int(result.get("candidate_count_after", 0))
        if before != after:
            raise RuntimeError(
                f"candidate restriction detected on device: before={before}, after={after}"
            )
        post = result.get("post_state")
        if not isinstance(post, dict):
            raise RuntimeError("persistent device returned no resumable state")
        self.context_state[robot_id] = {
            name: dict(post.get(name, {})) for name in EMPTY_SECTIONS
        }
        raw_messages = result.get("messages", [])
        decoded_messages = [decode_value(item) for item in raw_messages]
        messages = [item for item in decoded_messages if isinstance(item, dict)]
        for message in messages:
            sender = str(message.get("sender", robot_id))
            for receiver in self.robot_ids:
                if receiver != sender:
                    self.pending_messages[receiver].append(
                        {
                            "epoch_index": int(epoch_index),
                            "round_index": int(round_index),
                            "payload": message,
                        }
                    )
        raw_goal = decode_value(result.get("goal"))
        goal = None
        if raw_goal is not None:
            try:
                goal = int(raw_goal[0]), int(raw_goal[1])
            except (TypeError, ValueError, IndexError) as exc:
                raise RuntimeError(f"invalid device goal: {raw_goal!r}") from exc
        allocator_us = int(result.get("allocator_time_us", 0))
        if allocator_us < 0:
            raise RuntimeError("device allocator timer returned a negative duration")
        metrics = {
            "fixture_id": fixture_id,
            "attempt_id": attempt_id,
            "device_id": self.device_id,
            "robot_id": robot_id,
            "epoch_index": int(epoch_index),
            "round_index": int(round_index),
            "trigger_reason": trigger_reason,
            "allocation_epoch_hook_invoked": epoch_event is not None,
            "epoch_admitted_task_ids": list(admitted_task_ids),
            "epoch_admitted_cells": [
                list(self.cells_by_task[task_id])
                for task_id in admitted_task_ids
            ],
            "device_allocator_time_us": allocator_us,
            "allocator_time_us": allocator_us,
            "device_candidate_enumeration_time_us": int(
                result.get("candidate_filter_time_us", 0)
            ),
            "host_setup_transport_us": setup_host_us,
            "host_timed_and_output_roundtrip_us": timed_output_host_us,
            "host_total_call_us": total_host_us,
            "host_nonallocator_overhead_us": max(0, total_host_us - allocator_us),
            "candidate_count_before": before,
            "candidate_count_after": after,
            "candidate_mode": "unrestricted",
            "call_class": str(result.get("call_class", "unknown")),
            "heap_free_before": result.get("heap_free_before"),
            "heap_free_after": result.get("heap_free_after"),
            "goal": None if goal is None else list(goal),
            "message_count": len(messages),
        }
        return CallResult(goal=goal, messages=messages, metrics=metrics)
