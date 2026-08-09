"""Deterministic admission epochs used by motionless HIL replay."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .config import PolicySpec


@dataclass(frozen=True)
class AdmissionEpoch:
    epoch_index: int
    time_s: float
    trigger_reason: str
    task_ids: tuple[str, ...]
    pending_count_before: int
    oldest_pending_age_s: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "epoch_index": self.epoch_index,
            "time_s": self.time_s,
            "trigger_reason": self.trigger_reason,
            "task_ids": list(self.task_ids),
            "admitted_task_count": len(self.task_ids),
            "pending_count_before": self.pending_count_before,
            "oldest_pending_age_s": self.oldest_pending_age_s,
        }


def _online_groups(tasks: Iterable[dict[str, Any]]) -> list[tuple[float, list[str]]]:
    groups: list[tuple[float, list[str]]] = []
    for task in sorted(
        (item for item in tasks if not item["initially_visible"]),
        key=lambda item: (float(item["release_time_s"]), str(item["task_id"])),
    ):
        released = float(task["release_time_s"])
        if groups and groups[-1][0] == released:
            groups[-1][1].append(str(task["task_id"]))
        else:
            groups.append((released, [str(task["task_id"])]))
    return groups


def build_admission_epochs(
    tasks: Iterable[dict[str, Any]], policy: PolicySpec
) -> tuple[AdmissionEpoch, ...]:
    """Build a reproducible replay plan without changing mission semantics.

    This plan is intentionally limited to arrival-induced epochs.  It is used
    for the motionless embedded-compute replay and software validation, not as
    a replacement for the simulator's mandatory robot-idle/completion epochs.
    Simultaneous releases are admitted together because they are already
    pending when a trigger at that absolute timestamp is serviced.
    """

    values = list(tasks)
    initial = tuple(
        sorted(str(item["task_id"]) for item in values if item["initially_visible"])
    )
    epochs: list[AdmissionEpoch] = []
    if initial:
        epochs.append(
            AdmissionEpoch(0, 0.0, "initial_tasks", initial, len(initial), 0.0)
        )
    pending: list[tuple[str, float]] = []
    groups = _online_groups(values)
    group_index = 0

    def admit(now: float, reason: str) -> None:
        nonlocal pending
        oldest = max(0.0, now - min(released for _, released in pending))
        task_ids = tuple(task_id for task_id, _ in pending)
        epochs.append(
            AdmissionEpoch(
                len(epochs), now, reason, task_ids, len(pending), oldest
            )
        )
        pending = []

    while group_index < len(groups):
        release_time, task_ids = groups[group_index]
        if pending and policy.max_wait_s is not None:
            deadline = pending[0][1] + policy.max_wait_s
            if deadline < release_time:
                admit(deadline, "age_timeout")
                continue
        pending.extend((task_id, release_time) for task_id in task_ids)
        group_index += 1
        if len(pending) >= policy.batch_size:
            reason = "task_arrival_eager" if policy.policy == "eager" else "batch_threshold"
            admit(release_time, reason)

    if pending:
        if policy.max_wait_s is not None:
            admit(pending[0][1] + policy.max_wait_s, "age_timeout")
        else:
            # A pure count condition needs an explicit end-of-trace flush so
            # fewer than B remaining releases are measured and never lost.
            admit(max(released for _, released in pending), "trace_end_flush")
    return tuple(epochs)
