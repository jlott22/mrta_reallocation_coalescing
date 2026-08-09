from __future__ import annotations

import math
import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable


EVENT_CAP = 8_000
NO_MOVEMENT_EVENTS = 128
NO_PROGRESS_EVENTS = 1_024
REPEATED_STATE_COUNT = 8
REPEATED_STATE_WINDOW = 128
SHORT_CONFIRMATION_EVENTS = 64
PROGRESS_CONFIRMATION_EVENTS = 128


class HilTrialFailure(RuntimeError):
    def __init__(self, reason: str, diagnostics: dict[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.diagnostics = diagnostics


@dataclass
class AdaptiveWatchdogPolicy:
    no_movement_events: int = NO_MOVEMENT_EVENTS
    no_progress_events: int = NO_PROGRESS_EVENTS
    repeated_state_count: int = REPEATED_STATE_COUNT
    repeated_state_window: int = REPEATED_STATE_WINDOW
    event_cap: int = EVENT_CAP
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, int]:
        with self.lock:
            return {
                "no_movement_events": self.no_movement_events,
                "no_progress_events": self.no_progress_events,
                "repeated_state_count": self.repeated_state_count,
                "repeated_state_window": self.repeated_state_window,
                "event_cap": self.event_cap,
            }

    def raise_threshold(self, detector: str, observed: int) -> dict[str, Any]:
        with self.lock:
            if detector == "deadlock_no_movement":
                old = self.no_movement_events
                self.no_movement_events = min(
                    self.event_cap - 1,
                    max(old + 1, int(math.ceil(observed * 1.25))),
                )
                new = self.no_movement_events
            elif detector == "deadlock_no_mission_progress":
                old = self.no_progress_events
                self.no_progress_events = min(
                    self.event_cap - 1,
                    max(old + 1, int(math.ceil(observed * 1.25))),
                )
                new = self.no_progress_events
            else:
                old = self.repeated_state_count
                self.repeated_state_count = max(
                    old + 1,
                    int(math.ceil(observed * 1.25)),
                )
                new = self.repeated_state_count
            return {
                "detector": detector,
                "old_threshold": old,
                "new_threshold": new,
                "observed_streak": observed,
                "policy": self.snapshot_unlocked(),
            }

    def snapshot_unlocked(self) -> dict[str, int]:
        return {
            "no_movement_events": self.no_movement_events,
            "no_progress_events": self.no_progress_events,
            "repeated_state_count": self.repeated_state_count,
            "repeated_state_window": self.repeated_state_window,
            "event_cap": self.event_cap,
        }


class TrialWatchdog:
    def __init__(
        self,
        mission: str,
        policy: AdaptiveWatchdogPolicy,
        on_adjustment: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.mission = mission
        self.policy = policy
        self.on_adjustment = on_adjustment
        self.last_movement_event = 0
        self.last_progress_event = 0
        self.last_progress_token: tuple[Any, ...] | None = None
        self.signatures: deque[tuple[Any, ...]] = deque(
            maxlen=policy.repeated_state_window
        )
        self.reasons: Counter[str] = Counter()
        self.warning: dict[str, Any] | None = None

    def _progress_token(self, state: Any, result: Any) -> tuple[Any, ...]:
        world = state.world
        if self.mission == "bayesian":
            return (
                int(world.unique_cells_searched()),
                bool(world.first_clue_time_s is not None),
                bool(world.target_found_time_s is not None),
            )
        completed = sum(
            int(record.completed) for record in world.target_records.values()
        )
        return (completed,)

    @staticmethod
    def _pending_signature(robot: Any) -> tuple[Any, ...]:
        pending = getattr(robot, "pending_actions", ())
        if not pending:
            return ()
        action = pending[0]
        return (
            getattr(action, "kind", None),
            getattr(action, "target", None),
            getattr(action, "heading", None),
        )

    def _state_signature(self, state: Any, progress: tuple[Any, ...]) -> tuple[Any, ...]:
        robots = tuple(
            (
                str(rid),
                tuple(robot.pos),
                tuple(robot.heading),
                robot.current_goal,
                self._pending_signature(robot),
            )
            for rid, robot in sorted(state.robots.items())
        )
        return progress, robots

    def _diagnostics(self, state: Any, reason: str) -> dict[str, Any]:
        event = int(state.events_processed)
        return {
            "reason": reason,
            "events_processed": event,
            "clock_s": float(state.clock_s),
            "events_since_movement": event - self.last_movement_event,
            "events_since_mission_progress": event - self.last_progress_event,
            "event_reasons": dict(self.reasons),
            "policy": self.policy.snapshot(),
            "robots": {
                str(rid): {
                    "position": list(robot.pos),
                    "heading": list(robot.heading),
                    "goal": robot.current_goal,
                    "last_event": getattr(robot, "last_event", ""),
                }
                for rid, robot in sorted(state.robots.items())
            },
        }

    def _start_warning(self, reason: str, event: int, observed: int) -> None:
        confirmation = (
            PROGRESS_CONFIRMATION_EVENTS
            if reason == "deadlock_no_mission_progress"
            else SHORT_CONFIRMATION_EVENTS
        )
        self.warning = {
            "reason": reason,
            "started_at": event,
            "observed": observed,
            "deadline": event + confirmation,
        }

    def __call__(self, state: Any, robot: Any, result: Any) -> None:
        event = int(state.events_processed)
        self.reasons[str(getattr(result, "reason", "unknown"))] += 1
        moved = bool(getattr(result, "moved", False))
        if moved:
            self.last_movement_event = event

        progress = self._progress_token(state, result)
        progressed = self.last_progress_token is None or progress != self.last_progress_token
        if progressed:
            self.last_progress_token = progress
            self.last_progress_event = event

        signature = self._state_signature(state, progress)
        self.signatures.append(signature)
        repeat_count = sum(item == signature for item in self.signatures)

        if event >= self.policy.event_cap:
            raise HilTrialFailure(
                "event_cap_8000",
                self._diagnostics(state, "event_cap_8000"),
            )

        no_movement = event - self.last_movement_event
        no_progress = event - self.last_progress_event
        if self.warning is not None:
            reason = str(self.warning["reason"])
            current_observed = (
                no_movement
                if reason == "deadlock_no_movement"
                else no_progress
                if reason == "deadlock_no_mission_progress"
                else repeat_count
            )
            self.warning["observed"] = max(
                int(self.warning["observed"]), current_observed
            )
            warning_resolved = (
                moved
                if reason == "deadlock_no_movement"
                else progressed
            )
            if warning_resolved:
                observed = int(self.warning["observed"])
                adjustment = self.policy.raise_threshold(reason, observed)
                adjustment["event"] = event
                if self.on_adjustment is not None:
                    self.on_adjustment(adjustment)
                self.warning = None
            elif event >= int(self.warning["deadline"]):
                raise HilTrialFailure(reason, self._diagnostics(state, reason))
            return

        policy = self.policy.snapshot()
        if no_movement >= policy["no_movement_events"]:
            self._start_warning("deadlock_no_movement", event, no_movement)
        elif no_progress >= policy["no_progress_events"]:
            self._start_warning(
                "deadlock_no_mission_progress", event, no_progress
            )
        elif (
            repeat_count >= policy["repeated_state_count"]
            and no_progress >= policy["repeated_state_count"]
        ):
            self._start_warning("deadlock_repeated_state", event, repeat_count)
