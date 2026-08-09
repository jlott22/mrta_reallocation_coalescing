from __future__ import annotations

from typing import Any, Optional, Tuple

from known_visit_sim.algorithms.base import AllocatorBase
from known_visit_sim.core.types import AllocationDecision

Cell = Tuple[int, int]


class AuctionGreedyAllocator(AllocatorBase):
    """Known-target allocator behavior and communication hooks."""

    name = "AG"

    # Same reward scaling name as the robot code.
    REWARD_FACTOR = 5.0

    def choose_goal(self, robot: Any) -> AllocationDecision:
        """Known-target allocator behavior and communication hooks."""

        goal = self.pick_goal(robot)
        mode = "known_visit"

        return AllocationDecision(
            goal=goal,
            debug={
                "alg": self.name,
                "mode": mode,
            },
        )

    def pick_goal(self, robot: Any) -> Optional[Cell]:
        """
        Select a task cell we can outbid peers for using Manhattan-distance bids.

        This is the simulation equivalent of the original Auction-Greedy
        pick_goal() function.

        For a candidate cell:
            reward = target_p[cell] * REWARD_FACTOR
            my_bid = reward - distance(my_position, cell)

        For each peer:
            peer_bid = reward - distance(predicted_peer_position, cell)

        We can win if:
            my_bid > peer_bid for every peer

        Tie break:
            If bids are equal, the lower robot ID wins.
        """

        current_goal = self._current_goal(robot)

        predicted_positions = {}

        # Prefer last known peer position.
        for rid, cell in self._peer_positions(robot).items():
            if str(rid) != str(robot.rid) and rid not in predicted_positions and cell is not None:
                predicted_positions[rid] = cell

        best = None
        best_val = -1e9
        fallback_best = None
        fallback_val = -1e9

        def can_win(cell: Cell, reward: float) -> bool:
            """
            Return True if this robot locally believes it can beat all peers.

            This intentionally does not require peers to send bids. It estimates
            their possible bid using the latest received peer position.
            """

            my_bid = reward - self.manhattan(cell[0], cell[1], robot.pos[0], robot.pos[1])

            for rid, start in predicted_positions.items():
                peer_bid = reward - self.manhattan(cell[0], cell[1], start[0], start[1])

                if peer_bid > my_bid:
                    return False

                if peer_bid == my_bid and str(rid) < str(robot.rid):
                    return False

            return True

        def consider(cell: Optional[Cell]) -> None:
            """
            Evaluate one candidate cell.

            This mirrors the nested consider() helper in your robot code:
            - ignore invalid cells
            - ignore searched cells
            - keep a fallback high-reward cell
            - reject cells we cannot win
            - keep the best can-win cell
            """

            nonlocal best, best_val, fallback_best, fallback_val

            if cell is None:
                return

            if not self._is_active_task(robot, cell):
                return

            x, y = cell

            if not self._in_bounds(robot, cell):
                return

            if self._is_searched(robot, cell):
                return

            if self._is_obstacle(robot, cell):
                return

            reward = self._target_probability(robot, cell) * self.REWARD_FACTOR

            # Fallback: highest reward cell, preserving the current task cell on ties.
            if reward > fallback_val or (reward == fallback_val and cell == current_goal):
                fallback_val = reward
                fallback_best = cell

            if not can_win(cell, reward):
                return

            if reward > best_val:
                best_val = reward
                best = cell

        # First consider the cell directly ahead, as in the AG robot code.
        heading = self._heading(robot)
        if heading != (0, 0):
            consider((robot.pos[0] + heading[0], robot.pos[1] + heading[1]))

        # If no forward task cell is selected, consider left and right neighbors.
        if best is None and heading != (0, 0):
            left = (-heading[1], heading[0])
            right = (heading[1], -heading[0])

            for sx, sy in (left, right):
                side_cell = (robot.pos[0] + sx, robot.pos[1] + sy)
                consider(side_cell)

                if best == side_cell:
                    break

        # Then scan the full grid.
        grid_size = self._grid_size(robot)

        for y in range(grid_size):
            for x in range(grid_size):
                consider((x, y))

        if best is not None:
            return best

        if fallback_best is not None:
            return fallback_best

        # Final fallback: nearest unsearched cell not reserved by peers.
        unknowns = [
            (x, y)
            for y in range(grid_size)
            for x in range(grid_size)
            if self._is_active_task(robot, (x, y))
            and not self._is_searched(robot, (x, y))
            and not self._is_obstacle(robot, (x, y))
        ]

        if unknowns:
            return min(
                unknowns,
                key=lambda c: self.manhattan(c[0], c[1], robot.pos[0], robot.pos[1]),
            )

        return None


    @staticmethod
    def manhattan(x1: int, y1: int, x2: int, y2: int) -> int:
        """Calculate Manhattan distance between two grid cells."""

        return abs(x1 - x2) + abs(y1 - y2)


    def _target_probability(self, robot: Any, cell: Cell) -> float:
        """
        Read this robot's local target probability for a cell.

        The simulator should store target_p locally per robot. This helper
        supports both dictionary-style and array/list-style target_p maps.
        """

        target_p = getattr(robot, "target_p", {}) or {}

        if isinstance(target_p, dict):
            return float(target_p.get(cell, 0.0))

        idx_fn = getattr(robot, "idx", None)

        if callable(idx_fn):
            try:
                return float(target_p[idx_fn(cell[0], cell[1])])
            except Exception:
                return 0.0

        return 0.0

    def _is_searched(self, robot: Any, cell: Cell) -> bool:
        """
        Return True if this robot locally believes a cell has been searched.

        This should use local robot knowledge, not global world truth.
        """

        searched = getattr(robot, "searched", None)

        if searched is None:
            searched = getattr(robot, "local_searched", set())

        return cell in searched

    def _is_obstacle(self, robot: Any, cell: Cell) -> bool:
        """Return True if this robot locally knows the cell is blocked."""

        for attr in ("known_obstacles", "obstacles", "blocked", "blocked_cells"):
            cells = getattr(robot, attr, None)
            if cells is not None and cell in cells:
                return True

        return False

    def _peer_positions(self, robot: Any) -> dict:
        """
        Return the robot's local peer position table.

        These are normal dropped-message knowledge. If a position message was
        dropped, that peer position should not appear as updated here.
        """

        return getattr(robot, "peer_positions", {}) or {}

    def _current_goal(self, robot: Any) -> Optional[Cell]:
        """Return this robot's current task cell, if the robot shell stores one."""

        return getattr(robot, "current_goal", None)

    def _heading(self, robot: Any) -> Cell:
        """Return the robot's current cardinal heading."""

        return getattr(robot, "heading", (0, 0))

    def _grid_size(self, robot: Any) -> int:
        """Return grid size from the robot or simulator config."""

        grid_size = getattr(robot, "grid_size", None)

        if grid_size is not None:
            return int(grid_size)

        cfg = getattr(robot, "cfg", None)

        return int(getattr(cfg, "grid_size", 19))

    def _in_bounds(self, robot: Any, cell: Cell) -> bool:
        """Return True if the cell is inside the grid."""

        x, y = cell
        grid_size = self._grid_size(robot)

        return 0 <= x < grid_size and 0 <= y < grid_size
