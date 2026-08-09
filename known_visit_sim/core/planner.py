from __future__ import annotations

import heapq
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .types import Cell, Heading, DIRS4, in_bounds, manhattan, quarter_turns


class AStarPlanner:
    def __init__(
        self,
        grid_size: int,
        move_cost: float = 1.0,
        turn_cost: float = 0.3,
        visited_step_penalty: float = 4.0,
        reward_factor: float = 5.0,
        min_step_cost: float = 0.01,
    ) -> None:
        self.grid_size = grid_size
        self.move_cost = move_cost
        self.turn_cost = turn_cost
        self.visited_step_penalty = visited_step_penalty
        self.reward_factor = reward_factor
        self.min_step_cost = min_step_cost

    def plan(
        self,
        start: Cell,
        heading: Heading,
        goal: Cell,
        target_p: Dict[Cell, float],
        searched: Set[Cell],
        blocked: Set[Cell] | None = None,
    ) -> List[Cell]:
        blocked = blocked or set()
        if start == goal:
            return [start]
        if goal in blocked:
            return []

        frontier: List[Tuple[float, int, Cell, Heading]] = []
        tie = 0
        heapq.heappush(frontier, (0.0, tie, start, heading))
        came_from: Dict[Cell, Optional[Cell]] = {start: None}
        cost_so_far: Dict[Cell, float] = {start: 0.0}
        dir_so_far: Dict[Cell, Heading] = {start: heading}

        while frontier:
            _, _, cur, cur_dir = heapq.heappop(frontier)
            if cur == goal:
                break
            for dx, dy in DIRS4:
                nxt = (cur[0] + dx, cur[1] + dy)
                if not in_bounds(nxt, self.grid_size):
                    continue
                if nxt in blocked and nxt != goal:
                    continue
                step_dir = (dx, dy)
                turns = quarter_turns(cur_dir, step_dir)
                base_cost = self.move_cost + self.turn_cost * turns
                if nxt in searched:
                    base_cost += self.visited_step_penalty
                reward_bonus = self.reward_factor * target_p.get(nxt, 0.0)
                reward_bonus = min(max(0.0, reward_bonus), max(0.0, base_cost - self.min_step_cost))
                step_cost = max(self.min_step_cost, base_cost - reward_bonus)
                new_cost = cost_so_far[cur] + step_cost
                if nxt not in cost_so_far or new_cost < cost_so_far[nxt]:
                    cost_so_far[nxt] = new_cost
                    came_from[nxt] = cur
                    dir_so_far[nxt] = step_dir
                    tie += 1
                    priority = new_cost + manhattan(nxt, goal)
                    heapq.heappush(frontier, (priority, tie, nxt, step_dir))

        if goal not in came_from:
            return []
        path: List[Cell] = []
        cur: Optional[Cell] = goal
        while cur is not None:
            path.append(cur)
            cur = came_from[cur]
        path.reverse()
        return path
