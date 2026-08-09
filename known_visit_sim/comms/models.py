from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, Tuple

from known_visit_sim.core.types import Cell, manhattan
from .message import Message


class CommunicationModel:
    name = "base"

    def should_deliver(
        self,
        message: Message,
        sender_pos: Cell,
        receiver_pos: Cell,
        rng: random.Random,
        link_key: Tuple[str, str],
    ) -> bool:
        raise NotImplementedError

    def level_label(self) -> str:
        return ""


@dataclass
class IdealModel(CommunicationModel):
    name: str = "ideal"

    def should_deliver(self, message: Message, sender_pos: Cell, receiver_pos: Cell, rng: random.Random, link_key: Tuple[str, str]) -> bool:
        return True

    def level_label(self) -> str:
        return "1.0"


@dataclass
class BernoulliModel(CommunicationModel):
    """Independent receiver-side drops.

    `drop_prob` is probability of dropping a non-protected message to each receiver.
    """

    drop_prob: float = 0.0
    name: str = "bernoulli"

    def should_deliver(self, message: Message, sender_pos: Cell, receiver_pos: Cell, rng: random.Random, link_key: Tuple[str, str]) -> bool:
        return rng.random() >= self.drop_prob

    def level_label(self) -> str:
        return f"drop_{self.drop_prob:g}"


@dataclass
class GilbertElliotModel(CommunicationModel):
    """Two-state burst-loss model per directed link.

    GOOD links deliver every non-protected message and BAD links drop every
    non-protected message. ``p_good_to_good`` and ``p_bad_to_bad`` independently
    control GOOD- and BAD-run persistence.

    The defaults have stationary delivery probability 0.9 and lag-one state
    correlation 0.8. Use :func:`make_comm_model` to construct a model whose
    requested communication level is the stationary delivery probability.
    """

    p_good_success: float = 1.0
    p_bad_success: float = 0.0
    p_good_to_good: float = 0.98
    p_bad_to_bad: float = 0.82
    initial_good_prob: float = 0.90
    name: str = "gilbert_elliott"
    states: Dict[Tuple[str, str], bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("p_good_to_good", self.p_good_to_good),
            ("p_bad_to_bad", self.p_bad_to_bad),
            ("initial_good_prob", self.initial_good_prob),
        ):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1], got {value}")
        self.p_good_success = 1.0
        self.p_bad_success = 0.0

    @property
    def stationary_delivery_prob(self) -> float:
        """Stationary GOOD-state probability for the directed-link chain."""
        denominator = 2.0 - self.p_good_to_good - self.p_bad_to_bad
        if denominator <= 0.0:
            return self.initial_good_prob
        return (1.0 - self.p_bad_to_bad) / denominator

    @property
    def state_correlation(self) -> float:
        """Lag-one correlation/eigenvalue of the binary link-state chain."""
        return self.p_good_to_good + self.p_bad_to_bad - 1.0

    def _state(self, link_key: Tuple[str, str], rng: random.Random) -> bool:
        if link_key not in self.states:
            self.states[link_key] = rng.random() < self.initial_good_prob
        return self.states[link_key]

    def should_deliver(self, message: Message, sender_pos: Cell, receiver_pos: Cell, rng: random.Random, link_key: Tuple[str, str]) -> bool:
        good = self._state(link_key, rng)
        success_prob = self.p_good_success if good else self.p_bad_success
        delivered = success_prob >= 1.0
        if good:
            self.states[link_key] = rng.random() < self.p_good_to_good
        else:
            self.states[link_key] = not (rng.random() < self.p_bad_to_bad)
        return delivered

    def level_label(self) -> str:
        return f"pGG_{self.p_good_to_good:g}_pBB_{self.p_bad_to_bad:g}"


@dataclass
class RayleighStyleModel(CommunicationModel):
    """Simplified path-loss + Rayleigh fading model.

    This is not an RF simulator. It mimics the benchmark-style rule where received
    power is compared with a sensitivity threshold after distance path loss and a
    random fading term.
    """

    tx_power_dbm: float = 30.0
    sensitivity_dbm: float = -65.0
    path_loss_ref_db: float = 40.0
    path_loss_exp: float = 3.0
    ref_distance: float = 1.0
    cell_size_m: float = 1.0
    name: str = "rayleigh_style"

    def should_deliver(self, message: Message, sender_pos: Cell, receiver_pos: Cell, rng: random.Random, link_key: Tuple[str, str]) -> bool:
        d_cells = max(1e-6, math.dist(sender_pos, receiver_pos))
        d_m = max(self.ref_distance, d_cells * self.cell_size_m)
        path_loss_db = self.path_loss_ref_db + 10.0 * self.path_loss_exp * math.log10(d_m / self.ref_distance)
        # Rayleigh amplitude -> exponential power gain. Fading loss is positive when gain < 1.
        power_gain = max(1e-12, rng.expovariate(1.0))
        fading_loss_db = -10.0 * math.log10(power_gain)
        received_dbm = self.tx_power_dbm - path_loss_db - fading_loss_db
        return received_dbm >= self.sensitivity_dbm

    def level_label(self) -> str:
        return f"sens_{self.sensitivity_dbm:g}"


def make_comm_model(name: str, level: float | None = None, **kwargs) -> CommunicationModel:
    norm = name.lower().replace("-", "_")
    if norm == "ideal":
        return IdealModel()
    if norm == "bernoulli":
        return BernoulliModel(drop_prob=0.0 if level is None else float(level))
    if norm in {"ge", "gilbert", "gilbert_elliott", "gilbert_elliot"}:
        # Interpret level as the stationary delivery probability.  A fixed
        # positive state correlation creates genuine success/loss bursts while
        # keeping levels comparable with Bernoulli delivery rates.
        if level is not None:
            delivery_prob = float(level)
            if not 0.0 <= delivery_prob <= 1.0:
                raise ValueError(f"Gilbert-Elliott communication level must be in [0, 1], got {delivery_prob}")
            state_correlation = float(kwargs.pop("state_correlation", 0.8))
            if not 0.0 <= state_correlation < 1.0:
                raise ValueError(
                    "Gilbert-Elliott state_correlation must be in [0, 1) "
                    f"for bursty communication, got {state_correlation}"
                )
            kwargs.setdefault(
                "p_good_to_good",
                delivery_prob + state_correlation * (1.0 - delivery_prob),
            )
            kwargs.setdefault(
                "p_bad_to_bad",
                (1.0 - delivery_prob) + state_correlation * delivery_prob,
            )
            kwargs.setdefault("initial_good_prob", delivery_prob)
        return GilbertElliotModel(**kwargs)
    if norm in {"rayleigh", "rayleigh_style", "rayleigh_fading"}:
        # Interpret level as sensitivity threshold if supplied.
        if level is not None:
            kwargs.setdefault("sensitivity_dbm", float(level))
        return RayleighStyleModel(**kwargs)
    raise ValueError(f"Unknown communication model: {name}")
