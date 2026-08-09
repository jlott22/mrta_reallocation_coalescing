from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

from known_visit_sim.core.types import Cell
from known_visit_sim.metrics.counters import MessageCounters
from .message import Message
from .models import CommunicationModel

CORE_MESSAGE_TOPICS = {"state", "collision_intent"}


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
        self.receivers[receiver.rid] = receiver

    def publish(self, sender: str, topic: str, payload: dict, now_s: float) -> None:
        if sender not in self.receivers:
            raise KeyError(f"Sender {sender} is not registered")
        message = Message(sender, topic, dict(payload), now_s)
        self.counters.sent(
            sender,
            topic=message.category,
            protected=message.protected,
            core=message.category in CORE_MESSAGE_TOPICS,
        )
        sender_pos = self.receivers[sender].pos
        for rid, receiver in self.receivers.items():
            if rid == sender:
                continue
            deliver = message.protected or self.model.should_deliver(
                message, sender_pos, receiver.pos, self.rng, (sender, rid)
            )
            if not deliver:
                self.counters.dropped(rid)
                continue
            delay = self.delay_s
            if self.delay_jitter_s:
                delay = max(0.0, delay + self.rng.uniform(-self.delay_jitter_s, self.delay_jitter_s))
            self._order += 1
            heapq.heappush(self.pending, PendingDelivery(now_s + delay, self._order, rid, message))

    def pump(self, now_s: float) -> None:
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
            self.counters.delivered(item.receiver, protected=delivered.protected)
