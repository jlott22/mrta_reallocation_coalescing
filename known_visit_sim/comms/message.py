from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


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
        return self.category == "collision_intent"


def topic_for(rid: str, category: str) -> str:
    return f"robot/{rid}/{category}"
