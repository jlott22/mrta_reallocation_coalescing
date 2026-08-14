from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


ENVIRONMENT_SENDER = "__environment__"
TASK_ADMISSION_TOPIC = "task_admission"
TASK_COMPLETION_TOPIC = "task_completion"
PROTECTED_MESSAGE_TOPICS = {"collision_intent", TASK_ADMISSION_TOPIC}


@dataclass(frozen=True)
class Message:
    sender: str
    topic: str
    payload: Dict[str, Any]
    created_at_s: float
    delivered_at_s: float = 0.0

    @property
    def category(self) -> str:
        return self.topic.rstrip("/").split("/")[-1]

    @property
    def protected(self) -> bool:
        # Task announcements are part of the experiment's reliable control
        # plane.  Losing one would turn task discovery into an additional
        # communication treatment instead of measuring admission coalescing.
        # Peer task-completion reports intentionally remain droppable.
        return self.category in PROTECTED_MESSAGE_TOPICS


def topic_for(rid: str, category: str) -> str:
    return f"robot/{rid}/{category}"


def environment_topic_for(category: str) -> str:
    return f"environment/{category}"
