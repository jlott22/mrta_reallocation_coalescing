from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class RobotCounters:
    rid: str
    steps_total: int = 0
    unique_cells_contributed: int = 0
    system_revisits_by_robot: int = 0
    targets_found: int = 0
    task_cell_revisits: int = 0
    task_cell_replans: int = 0
    path_replans: int = 0
    collision_prevention_events: int = 0
    blocked_task_quarantines: int = 0
    blocked_task_quarantine_time_s: float = 0.0
    maximum_quarantine_level: int = 0
    allocator_time_ns_samples: List[int] = field(default_factory=list)
    allocator_solve_time_ns_samples: List[int] = field(default_factory=list)
    candidate_filter_time_ns_samples: List[int] = field(default_factory=list)


@dataclass
class MessageCounters:
    sent_by_robot: Dict[str, int] = field(default_factory=dict)
    protected_sent_by_robot: Dict[str, int] = field(default_factory=dict)
    unprotected_sent_by_robot: Dict[str, int] = field(default_factory=dict)
    core_sent_by_robot: Dict[str, int] = field(default_factory=dict)
    allocation_sent_by_robot: Dict[str, int] = field(default_factory=dict)
    sent_by_topic: Dict[str, int] = field(default_factory=dict)
    sent_by_robot_topic: Dict[str, Dict[str, int]] = field(default_factory=dict)
    delivered_to_robot: Dict[str, int] = field(default_factory=dict)
    dropped_to_robot: Dict[str, int] = field(default_factory=dict)
    delivered_total: int = 0
    protected_delivered_total: int = 0
    unprotected_delivered_total: int = 0
    dropped_total: int = 0
    sent_total: int = 0
    protected_sent_total: int = 0
    unprotected_sent_total: int = 0
    core_sent_total: int = 0
    allocation_sent_total: int = 0

    def sent(self, rid: str, topic: str, protected: bool, core: bool) -> None:
        self.sent_total += 1
        self.sent_by_robot[rid] = self.sent_by_robot.get(rid, 0) + 1
        self.sent_by_topic[topic] = self.sent_by_topic.get(topic, 0) + 1
        topics = self.sent_by_robot_topic.setdefault(rid, {})
        topics[topic] = topics.get(topic, 0) + 1
        if protected:
            self.protected_sent_total += 1
            self.protected_sent_by_robot[rid] = self.protected_sent_by_robot.get(rid, 0) + 1
        else:
            self.unprotected_sent_total += 1
            self.unprotected_sent_by_robot[rid] = self.unprotected_sent_by_robot.get(rid, 0) + 1
        target = self.core_sent_by_robot if core else self.allocation_sent_by_robot
        target[rid] = target.get(rid, 0) + 1
        if core:
            self.core_sent_total += 1
        else:
            self.allocation_sent_total += 1

    def delivered(self, rid: str, protected: bool = False) -> None:
        self.delivered_total += 1
        self.delivered_to_robot[rid] = self.delivered_to_robot.get(rid, 0) + 1
        if protected:
            self.protected_delivered_total += 1
        else:
            self.unprotected_delivered_total += 1

    def dropped(self, rid: str) -> None:
        self.dropped_total += 1
        self.dropped_to_robot[rid] = self.dropped_to_robot.get(rid, 0) + 1
