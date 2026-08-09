from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from allocator_replay.capture.codec import decode_value, encode_value
from allocator_replay.device.common.replay_fingerprint import semantic_sha256


ALGORITHM_PREFIXES = (
    "cbaa_",
    "acbba_",
    "pi_",
    "hipc_",
    "dmchba_",
    "dga_",
    "candidate_count_",
    "max_candidate_cells",
    "_allocation_probability_",
)
CORE_ATTRIBUTES = (
    "rid",
    "pos",
    "heading",
    "grid_size",
    "current_goal",
    "last_goal",
    "last_event",
    "collision_avoidance_active",
    "collision_state",
    "_active_peer_positions",
)
VIEW_NAMES = (
    "known_clues",
    "searched",
    "local_searched",
    "target_p",
    "peer_positions",
    "active_tasks",
    "known_obstacles",
    "obstacles",
    "blocked",
    "blocked_cells",
)
OUTBOUND_METHODS = (
    "make_messages",
    "get_outbound_messages",
    "build_dga_messages",
    "build_hipc_messages",
    "build_acbba_messages",
    "build_cbaa_messages",
    "make_message",
    "get_outbound_message",
    "build_dga_message",
    "build_hipc_message",
    "build_acbba_message",
    "build_cbaa_message",
)


def _supported(value: Any) -> bool:
    try:
        encode_value(value)
        return True
    except TypeError:
        return False


def _read_view(robot: Any, name: str) -> Any:
    try:
        value = getattr(robot, name)
    except (AttributeError, KeyError):
        return None
    if callable(value):
        try:
            value = value()
        except TypeError:
            return None
    return value


def _object_attributes(value: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in vars(value).items():
        if _supported(item):
            result[key] = encode_value(item)
    return result


def snapshot(robot: Any, allocator: Any | None = None) -> dict[str, Any]:
    allocator = allocator or robot.allocator
    robot_attributes: dict[str, Any] = {}
    for name, value in vars(robot).items():
        if name in {"allocator", "bus", "world", "counters", "belief", "cfg"}:
            continue
        if name in {
            "_allocation_probability_source_id",
            "_allocation_probability_belief_id",
        }:
            continue
        if (
            name in CORE_ATTRIBUTES
            or name.startswith(ALGORITHM_PREFIXES)
            or name.startswith("_allocation_probability_")
        ) and _supported(value):
            robot_attributes[name] = encode_value(value)
    for name in CORE_ATTRIBUTES:
        if name not in robot_attributes:
            value = getattr(robot, name, None)
            if _supported(value):
                robot_attributes[name] = encode_value(value)
    views: dict[str, Any] = {}
    for name in VIEW_NAMES:
        value = _read_view(robot, name)
        if value is not None and _supported(value):
            views[name] = encode_value(value)
    cfg = getattr(robot, "cfg", None)
    belief = getattr(robot, "belief", None)
    return {
        "robot_attrs": robot_attributes,
        "views": views,
        "cfg": _object_attributes(cfg) if cfg is not None else {},
        "belief": _object_attributes(belief) if belief is not None else {},
        "allocator_attrs": _object_attributes(allocator),
    }


def state_fingerprint(state: dict[str, Any]) -> str:
    return semantic_sha256(state)


class Bag:
    def __init__(self, values: dict[str, Any] | None = None) -> None:
        if values:
            self.__dict__.update(values)


class ReplayCounters:
    def __init__(self) -> None:
        self.candidate_filter_time_ns_samples: list[int] = []
        self.candidate_filter_time_us_samples: list[int] = []


class ReplayRobot:
    def __init__(self, state: dict[str, Any]) -> None:
        self.counters = ReplayCounters()
        self.published_messages: list[dict[str, Any]] = []
        self.restore(state)

    def restore(self, state: dict[str, Any]) -> None:
        for name, value in state["robot_attrs"].items():
            setattr(self, name, decode_value(value))
        self._views = {
            name: decode_value(value) for name, value in state["views"].items()
        }
        self.cfg = Bag(
            {name: decode_value(value) for name, value in state["cfg"].items()}
        )
        self.belief = Bag(
            {name: decode_value(value) for name, value in state["belief"].items()}
        )

    @property
    def known_clues(self):
        return self._views.get("known_clues", [])

    @property
    def searched(self):
        return self._views.get("searched", set())

    @property
    def local_searched(self):
        return self._views.get("local_searched", self.searched)

    @property
    def target_p(self):
        return self._views.get("target_p", {})

    @property
    def peer_positions(self):
        return self._views.get("peer_positions", {})

    @property
    def active_tasks(self):
        return self._views.get("active_tasks", set())

    @property
    def known_obstacles(self):
        return self._views.get("known_obstacles", set())

    @property
    def obstacles(self):
        return self._views.get("obstacles", set())

    @property
    def blocked(self):
        return self._views.get("blocked", set())

    @property
    def blocked_cells(self):
        return self._views.get("blocked_cells", self.blocked)

    def publish_algorithm_message(self, category: str, payload: dict[str, Any]) -> None:
        message = dict(payload)
        message.setdefault("type", category)
        self.published_messages.append(message)


def restore_allocator(allocator: Any, state: dict[str, Any]) -> None:
    allocator.__dict__.clear()
    allocator.__dict__.update(
        {
            name: decode_value(value)
            for name, value in state["allocator_attrs"].items()
        }
    )


def outbound_messages(allocator: Any, robot: ReplayRobot) -> list[dict[str, Any]]:
    for method_name in OUTBOUND_METHODS:
        method = getattr(allocator, method_name, None)
        if not callable(method):
            continue
        payloads = method(robot)
        if payloads is None:
            return []
        if isinstance(payloads, dict):
            return [payloads]
        return [payload for payload in payloads if isinstance(payload, dict)]
    return []


def expected_messages_from_state(
    allocator_class: type,
    post_state: dict[str, Any],
) -> list[dict[str, Any]]:
    robot = ReplayRobot(post_state)
    allocator = allocator_class()
    restore_allocator(allocator, post_state)
    messages = outbound_messages(allocator, robot)
    return copy.deepcopy(messages)


def classify_call(
    algorithm: str,
    pre_state: dict[str, Any],
    post_state: dict[str, Any],
    filter_calls: int,
) -> str:
    before = pre_state["robot_attrs"]
    after = post_state["robot_attrs"]
    algorithm = algorithm.upper()
    if algorithm in {"DGA", "DMCHBA"}:
        trigger_names = (
            ("dga_generation", "dga_last_reallocation_trigger")
            if algorithm == "DGA"
            else (
                "dmchba_last_assignment_signature",
                "dmchba_last_reassignment_reason",
            )
        )
        if any(before.get(name) != after.get(name) for name in trigger_names):
            return "full_allocation_solve"
    if algorithm == "HIPC" and filter_calls:
        return "full_allocation_solve"
    if algorithm in {"ACBBA", "PI"}:
        path_name = "acbba_path" if algorithm == "ACBBA" else "pi_path"
        if before.get(path_name) != after.get(path_name):
            return "partial_bundle_refill"
    if filter_calls:
        return "candidate_filter_only"
    return "cached_or_maintenance"
