from __future__ import annotations

import unittest
from types import SimpleNamespace

from allocator_replay.hil.watchdog import (
    AdaptiveWatchdogPolicy,
    HilTrialFailure,
    TrialWatchdog,
)


class _World:
    first_clue_time_s = None
    target_found_time_s = None

    def __init__(self) -> None:
        self.unique = 1

    def unique_cells_searched(self) -> int:
        return self.unique


def _fixture():
    robot = SimpleNamespace(
        pos=(0, 0),
        heading=(1, 0),
        current_goal=None,
        pending_actions=[],
        last_event="idle",
    )
    state = SimpleNamespace(
        events_processed=0,
        clock_s=0.0,
        world=_World(),
        robots={"00": robot},
    )
    result = SimpleNamespace(reason="idle", moved=False)
    return state, robot, result


class HilWatchdogTests(unittest.TestCase):
    def test_no_movement_uses_confirmation_window(self) -> None:
        state, robot, result = _fixture()
        policy = AdaptiveWatchdogPolicy(
            no_movement_events=3,
            no_progress_events=100,
            repeated_state_count=100,
            event_cap=100,
        )
        watchdog = TrialWatchdog("bayesian", policy)
        failure = None
        for event in range(1, 80):
            state.events_processed = event
            try:
                watchdog(state, robot, result)
            except HilTrialFailure as exc:
                failure = exc
                break
        self.assertIsNotNone(failure)
        self.assertEqual(failure.reason, "deadlock_no_movement")
        self.assertEqual(failure.diagnostics["events_processed"], 67)

    def test_progress_during_confirmation_calibrates_online(self) -> None:
        state, robot, result = _fixture()
        adjustments = []
        policy = AdaptiveWatchdogPolicy(
            no_movement_events=3,
            no_progress_events=100,
            repeated_state_count=100,
            event_cap=100,
        )
        watchdog = TrialWatchdog(
            "bayesian", policy, on_adjustment=adjustments.append
        )
        for event in range(1, 4):
            state.events_processed = event
            watchdog(state, robot, result)
        state.events_processed = 4
        result.moved = True
        robot.pos = (1, 0)
        state.world.unique = 2
        watchdog(state, robot, result)
        self.assertEqual(len(adjustments), 1)
        self.assertGreater(policy.no_movement_events, 3)

    def test_hard_event_cap_has_no_grace(self) -> None:
        state, robot, result = _fixture()
        policy = AdaptiveWatchdogPolicy(
            no_movement_events=9_000,
            no_progress_events=9_000,
            repeated_state_count=9_000,
            event_cap=8_000,
        )
        watchdog = TrialWatchdog("bayesian", policy)
        state.events_processed = 8_000
        with self.assertRaises(HilTrialFailure) as raised:
            watchdog(state, robot, result)
        self.assertEqual(raised.exception.reason, "event_cap_8000")


if __name__ == "__main__":
    unittest.main()
