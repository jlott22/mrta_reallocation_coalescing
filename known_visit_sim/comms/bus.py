from __future__ import annotations

import heapq
import json
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Tuple

from known_visit_sim.core.types import Cell
from known_visit_sim.metrics.counters import MessageCounters
from .message import (
    ENVIRONMENT_SENDER,
    TASK_ADMISSION_TOPIC,
    TASK_COMPLETION_TOPIC,
    Message,
    environment_topic_for,
)
from .models import CommunicationModel

CORE_MESSAGE_TOPICS = {
    "state",
    "collision_intent",
    TASK_ADMISSION_TOPIC,
    TASK_COMPLETION_TOPIC,
}


class Receiver(Protocol):
    rid: str
    pos: Cell
    def receive_message(self, message: Message) -> None: ...


@dataclass(order=True)
class PendingDelivery:
    deliver_at_s: float
    order: int
    receiver: str = field(compare=False)
    message: Message = field(compare=False)


class MessageBus:
    def __init__(self, model: CommunicationModel, delay_s: float = 0.04,
                 delay_jitter_s: float = 0.0, rng: Optional[random.Random] = None) -> None:
        self.model = model
        self.delay_s = max(0.0, delay_s)
        self.delay_jitter_s = max(0.0, delay_jitter_s)
        self.rng = rng or random.Random()
        self.receivers: Dict[str, Receiver] = {}
        self.pending: List[PendingDelivery] = []
        self.counters = MessageCounters()
        self._order = 0

    def register(self, receiver: Receiver) -> None:
        if receiver.rid == ENVIRONMENT_SENDER:
            raise ValueError(f"{ENVIRONMENT_SENDER!r} is reserved for the simulator environment")
        self.receivers[receiver.rid] = receiver

    def publish(self, sender: str, topic: str, payload: dict, now_s: float) -> None:
        if sender not in self.receivers:
            raise KeyError(f"Sender {sender} is not registered")
        message = Message(sender, topic, dict(payload), now_s)
        payload_bytes = self._logical_payload_bytes(message.payload)
        if message.category == TASK_ADMISSION_TOPIC:
            raise ValueError("task_admission is a reserved environment message")
        self.counters.sent(
            sender,
            topic=message.category,
            protected=message.protected,
            core=message.category in CORE_MESSAGE_TOPICS,
            payload_bytes=payload_bytes,
        )
        sender_pos = self.receivers[sender].pos
        for rid, receiver in self.receivers.items():
            if rid == sender:
                continue
            deliver = message.protected or self.model.should_deliver(
                message, sender_pos, receiver.pos, self.rng, (sender, rid)
            )
            if not deliver:
                self.counters.dropped(rid, payload_bytes)
                continue
            delay = self.delay_s
            if self.delay_jitter_s:
                delay = max(0.0, delay + self.rng.uniform(-self.delay_jitter_s, self.delay_jitter_s))
            self._order += 1
            heapq.heappush(self.pending, PendingDelivery(now_s + delay, self._order, rid, message))

    def publish_environment(
        self, category: str, payload: dict, now_s: float
    ) -> Tuple[str, ...]:
        """Reliably broadcast one fixed-delay environment control message.

        Environment task announcements deliberately bypass both the evaluated
        radio-loss model and delay jitter.  They still traverse the message
        queue and reach each robot only through ``receive_message``, preserving
        the agent knowledge boundary and a causal delivery timestamp.
        """

        category = str(category)
        if category != TASK_ADMISSION_TOPIC:
            raise ValueError(
                "the environment control plane currently supports only task_admission"
            )
        message = Message(
            ENVIRONMENT_SENDER,
            environment_topic_for(category),
            dict(payload),
            float(now_s),
        )
        payload_bytes = self._logical_payload_bytes(message.payload)
        self.counters.sent(
            ENVIRONMENT_SENDER,
            topic=message.category,
            protected=True,
            core=True,
            payload_bytes=payload_bytes,
        )
        recipients = tuple(sorted(self.receivers))
        for rid in recipients:
            self._order += 1
            heapq.heappush(
                self.pending,
                PendingDelivery(
                    float(now_s) + self.delay_s,
                    self._order,
                    rid,
                    message,
                ),
            )
        return recipients

    def publish_task_admission(self, payload: dict, now_s: float) -> Tuple[str, ...]:
        """Convenience wrapper for the sole environment control message."""

        return self.publish_environment(TASK_ADMISSION_TOPIC, payload, now_s)

    def next_delivery_time_s(self) -> Optional[float]:
        """Return the next exogenous delivery time without consuming it.

        Causal simulation must treat communication delivery as a timed event.
        Keeping the pending heap private is useful, but the event scheduler
        still needs this boundary so exact-zero allocator calls cannot starve
        messages that are already in flight.
        """

        if not self.pending:
            return None
        return float(self.pending[0].deliver_at_s)

    @staticmethod
    def _logical_payload_bytes(payload: dict) -> int:
        """Return deterministic compact-JSON payload bytes before wire codec.

        This is a provider-neutral communication-volume metric. Hardware wire
        bytes may be smaller because the replay transport interns/chunks large
        paths, so the two quantities must not be conflated.
        """

        return len(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                default=repr,
            ).encode("utf-8")
        )

    def pump(self, now_s: float) -> Tuple[str, ...]:
        delivered_receivers: List[str] = []
        while self.pending and self.pending[0].deliver_at_s <= now_s + 1e-12:
            item = heapq.heappop(self.pending)
            receiver = self.receivers.get(item.receiver)
            if receiver is None:
                continue
            delivered = Message(
                item.message.sender, item.message.topic, item.message.payload,
                item.message.created_at_s, item.deliver_at_s,
            )
            receiver.receive_message(delivered)
            self.counters.delivered(
                item.receiver,
                protected=delivered.protected,
                payload_bytes=self._logical_payload_bytes(delivered.payload),
            )
            delivered_receivers.append(item.receiver)
        return tuple(delivered_receivers)
