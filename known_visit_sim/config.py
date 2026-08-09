from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

Cell = Tuple[int, int]
Heading = Tuple[int, int]
EAST: Heading = (1, 0)


def generate_robot_ids(num_robots: int) -> List[str]:
    if num_robots <= 0:
        raise ValueError("num_robots must be positive")
    return [f"{index:02d}" for index in range(num_robots)]


def edge_even_start_positions(grid_size: int, robot_ids: List[str]) -> Dict[str, Cell]:
    if grid_size <= 0:
        raise ValueError("grid_size must be positive")
    if not robot_ids or len(robot_ids) > grid_size:
        raise ValueError("edge_even requires 1 <= num_robots <= grid_size")
    if len(robot_ids) == 1:
        return {robot_ids[0]: (0, (grid_size - 1) // 2)}
    return {
        rid: (0, round(index * (grid_size - 1) / (len(robot_ids) - 1)))
        for index, rid in enumerate(robot_ids)
    }


@dataclass
class SimConfig:
    grid_size: int = 19
    robot_ids: List[str] = field(default_factory=lambda: generate_robot_ids(4))
    start_positions: Dict[str, Cell] = field(default_factory=dict)
    start_headings: Dict[str, Heading] = field(default_factory=dict)
    robot_start_layout: str = "edge_even"
    condition_id: str = ""

    reward_factor: float = 5.0
    move_cost: float = 1.0
    turn_cost: float = 0.3
    visited_step_penalty: float = 4.0
    min_step_cost: float = 0.01

    async_step_mean_s: float = 1.60
    async_step_jitter_s: float = 0.10
    async_min_delay_s: float = 1.50
    async_max_delay_s: float = 1.70
    async_initial_spread_s: float = 0.10
    async_tick_span_s: float = 0.25
    turn_quarter_s: float = 0.30
    replan_delay_s: float = 0.30
    no_goal_delay_s: float = 0.50

    comm_delay_s: float = 0.04
    comm_delay_jitter_s: float = 0.01
    collision_intent_settle_s: float = 0.10
    collision_goal_backoff_max_s: float = 5.0
    collision_goal_quarantine_schedule_s: Tuple[float, ...] = (5.0, 15.0, 45.0, 120.0)
    stalled_allocation_recovery_s: float = 120.0
    debug_max_events: int = 5_000
    debug_max_stagnant_events: int = 2_000

    commitment_horizon: Optional[int] = None
    max_candidate_cells: Optional[int] = None

    def __post_init__(self) -> None:
        if self.grid_size <= 0:
            raise ValueError("grid_size must be positive")
        if not self.robot_ids or len(self.robot_ids) > self.grid_size:
            raise ValueError("edge_even requires 1 <= num_robots <= grid_size")
        if len(set(self.robot_ids)) != len(self.robot_ids):
            raise ValueError("robot_ids must be unique")
        if not self.start_positions:
            self.start_positions = edge_even_start_positions(self.grid_size, self.robot_ids)
        if not self.start_headings:
            self.start_headings = {rid: EAST for rid in self.robot_ids}
        if set(self.start_positions) != set(self.robot_ids):
            raise ValueError("start_positions must contain every robot ID exactly once")
        if set(self.start_headings) != set(self.robot_ids):
            raise ValueError("start_headings must contain every robot ID exactly once")
        if self.collision_goal_backoff_max_s <= 0:
            raise ValueError("collision_goal_backoff_max_s must be positive")
        if not self.collision_goal_quarantine_schedule_s or any(
            value <= 0 for value in self.collision_goal_quarantine_schedule_s
        ):
            raise ValueError("collision_goal_quarantine_schedule_s must contain positive durations")
        if self.stalled_allocation_recovery_s <= 0:
            raise ValueError("stalled_allocation_recovery_s must be positive")
        if self.debug_max_stagnant_events <= 0:
            raise ValueError("debug_max_stagnant_events must be positive")

    def to_dict(self) -> dict:
        return asdict(self)
