from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from known_visit_sim.algorithms.base import AllocatorBase, timed_candidate_filter
from known_visit_sim.core.types import AllocationDecision

Cell = Tuple[int, int]


class DMCHBAAllocator(AllocatorBase):
    """Known-target allocator behavior and communication hooks."""

    name = "DMCHBA"

    REWARD_FACTOR = 5.0
    PSEUDOTASK_COST = 1.0e9
    TIE_EPS = 1.0e-9
    COMMITMENT_HORIZON = 3

    def recover_stalled_allocation(self, robot: Any) -> bool:
        self._ensure_dmchba_state(robot)
        setattr(robot, "dmchba_path", [])
        setattr(robot, "dmchba_last_assignment_signature", None)
        setattr(robot, "dmchba_stall_self_only", True)
        return True

    # ------------------------------------------------------------------
    # Main simulator entry point
    # ------------------------------------------------------------------

    def choose_goal(self, robot: Any) -> AllocationDecision:
        """Known-target allocator behavior and communication hooks."""

        self._ensure_dmchba_state(robot)
        trigger = self._task_set_trigger(robot)
        if trigger is not None:
            self._run_dmchba_assignment(robot, trigger)
        goal = self._first_path_goal(robot)
        mode = "known_visit"

        return AllocationDecision(
            goal=goal,
            debug={
                "alg": self.name,
                "mode": mode,
                "dmchba_trigger": trigger,
                "dmchba_path_len": len(self._get_path(robot)),
                "dmchba_assigned_count": int(getattr(robot, "dmchba_last_assigned_count", 0)),
                "dmchba_committed_count": int(getattr(robot, "dmchba_last_committed_count", 0)),
                "dmchba_commitment_horizon": self._planning_horizon(robot, self.COMMITMENT_HORIZON),
                "dmchba_candidate_count": int(getattr(robot, "dmchba_last_candidate_count", 0)),
                "dmchba_candidate_count_before_filter": int(getattr(robot, "candidate_count_before_filter", 0)),
                "dmchba_candidate_count_after_filter": int(getattr(robot, "candidate_count_after_filter", 0)),
                "dmchba_max_candidate_cells": getattr(robot, "max_candidate_cells", None),
                "dmchba_team_size": int(getattr(robot, "dmchba_last_team_size", 0)),
                "dmchba_matrix_n": int(getattr(robot, "dmchba_last_matrix_n", 0)),
                "dmchba_evaluates_all_candidates": getattr(robot, "max_candidate_cells", None) is None,
                "dmchba_allocator_messages": False,
                "dmchba_stall_self_only": bool(getattr(robot, "dmchba_stall_self_only", False)),
            },
        )

    # ------------------------------------------------------------------
    # Event-triggered known-target allocation
    # ------------------------------------------------------------------

    def on_task_set_changed(self, robot: Any) -> None:
        """Record when a peer completion invalidates this robot's committed path."""

        self._ensure_dmchba_state(robot)
        if getattr(robot, "last_event", None) != "peer_state_at_target":
            return

        previous = set(getattr(robot, "dmchba_task_signature", ()) or ())
        current = set(getattr(robot, "active_tasks", set()) or set())
        removed = previous - current
        if removed.intersection(self._get_path(robot)):
            setattr(robot, "dmchba_external_path_invalidated", True)

    def _task_set_trigger(self, robot: Any) -> Optional[str]:
        """Return reassignment trigger name, or None if current path should continue."""

        self._ensure_dmchba_state(robot)
        self._drop_invalid_prefix_and_cells(robot)

        task_signature = tuple(sorted(getattr(robot, "active_tasks", set()) or set()))
        previous_task_signature = getattr(robot, "dmchba_task_signature", None)
        if task_signature != previous_task_signature:
            setattr(robot, "dmchba_task_signature", task_signature)
            if bool(getattr(robot, "dmchba_external_path_invalidated", False)):
                setattr(robot, "dmchba_external_path_invalidated", False)
                setattr(robot, "dmchba_path", [])
                return "external_task_invalidated_path"
            if not self._get_path(robot):
                return "task_set_changed"

        if self._collision_activation_trigger(robot):
            setattr(robot, "dmchba_path", [])
            return "collision_avoidance"

        path = self._get_path(robot)
        if not path:
            # Avoid recomputing forever when this robot legitimately receives no
            # task near the end of a trial. Recompute only if the assignment
            # input signature has changed since the last DMCHBA solve.
            signature = self._assignment_input_signature(robot)
            previous_assignment_signature = getattr(robot, "dmchba_last_assignment_signature", None)
            if signature != previous_assignment_signature:
                return "path_exhausted"

        return None

    def _run_dmchba_assignment(self, robot: Any, reason: str) -> None:
        """
        Build the full-cell matching-by-clone problem and store this robot's path.

        Candidate generation and Hungarian assignment remain full-cell: every
        valid unsearched cell is included. Only the post-assignment committed
        execution path is capped for fairness against the other bounded
        multi-task allocators.
        """

        self._ensure_dmchba_state(robot)

        tasks = self._candidate_cells(robot)
        team_agents = self._team_agents(robot)
        assignment_signature = self._assignment_input_signature(robot)

        setattr(robot, "dmchba_last_reassignment_reason", reason)
        setattr(robot, "dmchba_last_assignment_signature", assignment_signature)
        setattr(robot, "dmchba_last_candidate_count", len(tasks))
        setattr(robot, "dmchba_last_team_size", len(team_agents))
        setattr(robot, "dmchba_last_assigned_count", 0)
        setattr(robot, "dmchba_last_committed_count", 0)
        setattr(robot, "dmchba_last_matrix_n", 0)

        if not tasks or not team_agents:
            setattr(robot, "dmchba_path", [])
            return

        agent_ids = sorted(team_agents.keys(), key=self._robot_id_key)
        num_agents = len(agent_ids)
        num_tasks = len(tasks)

        clones_per_agent = int(math.ceil(num_tasks / float(num_agents)))
        clones_per_agent = max(1, clones_per_agent)

        clone_rows: List[Tuple[str, Cell, int]] = []
        for rid in agent_ids:
            pos = team_agents[rid]
            for clone_index in range(clones_per_agent):
                clone_rows.append((rid, pos, clone_index))

        matrix_n = len(clone_rows)
        pseudotask_count = matrix_n - num_tasks
        columns: List[Optional[Cell]] = list(tasks) + [None] * pseudotask_count

        cost_matrix = self._build_cost_matrix(robot, clone_rows, columns)
        row_to_col = self._solve_assignment(cost_matrix)

        assigned_by_robot: Dict[str, List[Cell]] = {rid: [] for rid in agent_ids}
        for row_index, col_index in enumerate(row_to_col):
            if col_index is None:
                continue
            if col_index < 0 or col_index >= len(columns):
                continue

            cell = columns[col_index]
            if cell is None:
                continue

            rid, _, _ = clone_rows[row_index]
            assigned_by_robot.setdefault(rid, []).append(cell)

        my_key = self._canonical_rid(getattr(robot, "rid", ""))
        assigned = assigned_by_robot.get(my_key, [])
        ordered_path = self._order_assigned_cells(robot, assigned)
        commitment_horizon = self._planning_horizon(robot, self.COMMITMENT_HORIZON)
        committed_path = ordered_path[:commitment_horizon]

        setattr(robot, "dmchba_path", committed_path)
        setattr(robot, "dmchba_last_assigned_count", len(ordered_path))
        setattr(robot, "dmchba_last_committed_count", len(committed_path))
        setattr(robot, "dmchba_last_matrix_n", matrix_n)
        setattr(robot, "dmchba_clones_per_agent", clones_per_agent)
        setattr(robot, "dmchba_pseudotask_count", pseudotask_count)

    def _build_cost_matrix(
        self,
        robot: Any,
        clone_rows: Sequence[Tuple[str, Cell, int]],
        columns: Sequence[Optional[Cell]],
    ) -> List[List[float]]:
        """Return square Hungarian cost matrix for clone rows vs task columns."""

        grid_size = self._grid_size(robot)
        matrix: List[List[float]] = []

        for row_index, (rid, pos, clone_index) in enumerate(clone_rows):
            row: List[float] = []
            for col_index, cell in enumerate(columns):
                if cell is None:
                    # Pseudotasks fill extra clone rows. Their absolute value is
                    # irrelevant as long as it is finite and much larger than
                    # real-cell costs.
                    row.append(self.PSEUDOTASK_COST + col_index * self.TIE_EPS)
                    continue

                distance = self.manhattan(pos[0], pos[1], cell[0], cell[1])
                cost = self._probability_adjusted_cost(robot, distance, cell)

                # Tiny deterministic tie-break. This should only affect exact or
                # near-exact cost ties, not normal reward-distance decisions.
                cell_order = cell[1] * grid_size + cell[0]
                cost += self.TIE_EPS * (cell_order + clone_index * 0.001 + row_index * 0.000001)
                row.append(cost)

            matrix.append(row)

        return matrix

    def _order_assigned_cells(self, robot: Any, cells: Sequence[Cell]) -> List[Cell]:
        """
        Greedily order this robot's assigned cells by reward-distance score.

        This keeps routing consistent with the other benchmark algorithms:
            score = target_p[cell] * REWARD_FACTOR - ManhattanDistance(reference, cell)
        """

        remaining = list(dict.fromkeys(self._normalize_cell_list(cells)))
        ordered: List[Cell] = []
        reference = self._robot_pos(robot)

        while remaining:
            best_cell: Optional[Cell] = None
            best_score = -1.0e18

            for cell in remaining:
                distance = self.manhattan(reference[0], reference[1], cell[0], cell[1])
                score = self._probability_adjusted_score(robot, distance, cell)

                if best_cell is None:
                    best_cell = cell
                    best_score = score
                    continue

                if score > best_score + self.TIE_EPS:
                    best_cell = cell
                    best_score = score
                elif abs(score - best_score) <= self.TIE_EPS:
                    # Prefer closer cell on score tie, then stable cell order.
                    best_dist = self.manhattan(reference[0], reference[1], best_cell[0], best_cell[1])
                    if distance < best_dist or (distance == best_dist and cell < best_cell):
                        best_cell = cell
                        best_score = score

            if best_cell is None:
                break

            ordered.append(best_cell)
            remaining.remove(best_cell)
            reference = best_cell

        return ordered

    def _first_path_goal(self, robot: Any) -> Optional[Cell]:
        self._drop_invalid_prefix_and_cells(robot)
        path = self._get_path(robot)
        if not path:
            return None
        return path[0]

    def _drop_invalid_prefix_and_cells(self, robot: Any) -> None:
        """Remove cells from the stored path that are no longer valid locally."""

        path = self._get_path(robot)
        if not path:
            return

        kept = [cell for cell in path if self._valid_task_cell(robot, cell)]
        if kept != path:
            setattr(robot, "dmchba_path", kept)
            setattr(robot, "dmchba_last_committed_count", len(kept))

    # ------------------------------------------------------------------
    # Hungarian assignment solver
    # ------------------------------------------------------------------

    def _solve_assignment(self, cost_matrix: Sequence[Sequence[float]]) -> List[Optional[int]]:
        """
        Return row->column assignment for a min-cost assignment problem.

        Uses the pure-Python Hungarian implementation below so this allocator
        has no dependencies beyond the shared simulator base/types.
        """

        n_rows = len(cost_matrix)
        if n_rows == 0:
            return []

        return self._hungarian_fallback(cost_matrix)

    @staticmethod
    def _hungarian_fallback(cost_matrix: Sequence[Sequence[float]]) -> List[Optional[int]]:
        """
        Pure-Python O(n^3) Hungarian solver for rectangular n_rows <= n_cols.

        This is intended as a portability fallback. For the 19x19 full-cell
        simulation, scipy is strongly preferred for speed.
        """

        n = len(cost_matrix)
        if n == 0:
            return []
        m = len(cost_matrix[0])
        if m < n:
            raise ValueError("Hungarian fallback requires n_rows <= n_cols")

        u = [0.0] * (n + 1)
        v = [0.0] * (m + 1)
        p = [0] * (m + 1)
        way = [0] * (m + 1)

        for i in range(1, n + 1):
            p[0] = i
            j0 = 0
            minv = [float("inf")] * (m + 1)
            used = [False] * (m + 1)

            while True:
                used[j0] = True
                i0 = p[j0]
                delta = float("inf")
                j1 = 0

                for j in range(1, m + 1):
                    if used[j]:
                        continue
                    cur = float(cost_matrix[i0 - 1][j - 1]) - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j] = cur
                        way[j] = j0
                    if minv[j] < delta:
                        delta = minv[j]
                        j1 = j

                for j in range(0, m + 1):
                    if used[j]:
                        u[p[j]] += delta
                        v[j] -= delta
                    else:
                        minv[j] -= delta

                j0 = j1
                if p[j0] == 0:
                    break

            while True:
                j1 = way[j0]
                p[j0] = p[j1]
                j0 = j1
                if j0 == 0:
                    break

        assignment: List[Optional[int]] = [None] * n
        for j in range(1, m + 1):
            if p[j] != 0:
                assignment[p[j] - 1] = j - 1

        return assignment

    # ------------------------------------------------------------------
    # DMCHBA state and triggers
    # ------------------------------------------------------------------

    def _ensure_dmchba_state(self, robot: Any) -> None:
        if not hasattr(robot, "dmchba_path") or getattr(robot, "dmchba_path") is None:
            setattr(robot, "dmchba_path", [])
        if not hasattr(robot, "dmchba_last_collision_active"):
            setattr(robot, "dmchba_last_collision_active", False)
        if not hasattr(robot, "dmchba_last_assignment_signature"):
            setattr(robot, "dmchba_last_assignment_signature", None)
        if not hasattr(robot, "dmchba_last_committed_count"):
            setattr(robot, "dmchba_last_committed_count", len(self._get_path(robot)))
        if not hasattr(robot, "dmchba_stall_self_only"):
            setattr(robot, "dmchba_stall_self_only", False)
        if not hasattr(robot, "dmchba_external_path_invalidated"):
            setattr(robot, "dmchba_external_path_invalidated", False)

    def _get_path(self, robot: Any) -> List[Cell]:
        path = getattr(robot, "dmchba_path", []) or []
        return self._normalize_cell_list(path)

    def _collision_activation_trigger(self, robot: Any) -> bool:
        """Return True only on the rising edge of a collision-avoidance flag."""

        active = self._collision_active(robot)
        previous = bool(getattr(robot, "dmchba_last_collision_active", False))
        setattr(robot, "dmchba_last_collision_active", active)
        return bool(active and not previous)

    def _collision_active(self, robot: Any) -> bool:
        for attr in (
            "collision_avoidance_active",
            "avoidance_active",
            "collision_active",
            "blocked_by_collision",
            "collision_blocked",
            "needs_collision_replan",
            "collision_replan",
        ):
            if bool(getattr(robot, attr, False)):
                return True

        state = str(getattr(robot, "collision_state", "")).lower()
        if state in {"active", "avoid", "avoiding", "blocked", "replan"}:
            return True

        return False

    def _assignment_input_signature(self, robot: Any) -> Tuple[Any, ...]:
        """
        Compact signature of inputs used for a DMCHBA solve.

        This is used only to avoid repeated solves when this robot has no assigned
        path. It is not used to trigger reassignment while a path remains active.
        """

        tasks = tuple(self._candidate_cells(robot))
        team = tuple(sorted(self._team_agents(robot).items(), key=lambda item: self._robot_id_key(item[0])))
        return (tasks, team)

    # ------------------------------------------------------------------
    # Task set and team state
    # ------------------------------------------------------------------

    @timed_candidate_filter
    def _candidate_cells(self, robot: Any) -> List[Cell]:
        """Return all valid unsearched cells in deterministic grid order."""

        grid_size = self._grid_size(robot)
        cells: List[Cell] = []

        for y in range(grid_size):
            for x in range(grid_size):
                cell = (x, y)
                if self._valid_task_cell(robot, cell):
                    cells.append(cell)

        return self._filter_candidate_cells(robot, cells)

    def _team_agents(self, robot: Any) -> Dict[str, Cell]:
        """Return known team agent positions as canonical_rid -> cell."""

        team: Dict[str, Cell] = {self._canonical_rid(robot.rid): self._robot_pos(robot)}
        if bool(getattr(robot, "dmchba_stall_self_only", False)):
            return team

        for rid, cell in self._safe_peer_positions(robot).items():
            key = self._canonical_rid(rid)
            if key == self._canonical_rid(robot.rid):
                continue
            normalized = self._normalize_cell(cell)
            if normalized is not None:
                team[key] = normalized

        return team

    def _safe_peer_positions(self, robot: Any) -> Dict[Any, Any]:
        for attr in (
            "peer_positions",
            "known_peer_positions",
            "agent_positions",
            "robot_positions",
            "positions_by_rid",
            "team_positions",
        ):
            value = getattr(robot, attr, None)
            if isinstance(value, dict):
                return value
        return {}

    # ------------------------------------------------------------------
    # Communication hooks: DMCHBA sends no allocation-specific messages
    # ------------------------------------------------------------------

    def build_dmchba_messages(self, robot: Any) -> List[dict]:
        return []

    def make_messages(self, robot: Any) -> List[dict]:
        return []

    def get_outbound_messages(self, robot: Any) -> List[dict]:
        return []

    def make_message(self, robot: Any) -> List[dict]:
        return []

    def get_outbound_message(self, robot: Any) -> List[dict]:
        return []

    def build_cbaa_messages(self, robot: Any) -> List[dict]:
        return []

    def build_cbaa_message(self, robot: Any) -> List[dict]:
        return []

    def build_acbba_messages(self, robot: Any) -> List[dict]:
        return []

    def build_acbba_message(self, robot: Any) -> List[dict]:
        return []


    def on_collision_avoidance_activated(self, robot: Any) -> bool:
        return True

    def handle_dmchba_message(self, robot: Any, message: Any) -> None:
        return None

    def receive_message(self, robot: Any, message: Any) -> None:
        return None

    def on_message(self, robot: Any, message: Any) -> None:
        return None

    def process_message(self, robot: Any, message: Any) -> None:
        return None

    # ------------------------------------------------------------------
    # Generic simulator helpers
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Generic simulator helpers
    # ------------------------------------------------------------------

    @staticmethod
    def manhattan(x1: int, y1: int, x2: int, y2: int) -> int:
        return abs(int(x1) - int(x2)) + abs(int(y1) - int(y2))

    def _robot_pos(self, robot: Any) -> Cell:
        return self._normalize_cell(getattr(robot, "pos", (0, 0))) or (0, 0)



    def _target_probability(self, robot: Any, cell: Cell) -> float:
        target_p = getattr(robot, "target_p", {}) or {}

        if isinstance(target_p, dict):
            return float(target_p.get(cell, 0.0))

        idx_fn = getattr(robot, "idx", None)
        if callable(idx_fn):
            try:
                return float(target_p[idx_fn(cell[0], cell[1])])
            except Exception:
                pass

        # Support common 2-D list/array layouts: target_p[y][x]
        try:
            return float(target_p[cell[1]][cell[0]])
        except Exception:
            return 0.0

    def _valid_task_cell(self, robot: Any, cell: Cell) -> bool:
        if cell is None:
            return False
        if not self._is_active_task(robot, cell):
            return False
        if not self._in_bounds(robot, cell):
            return False
        if self._is_searched(robot, cell):
            return False
        if self._is_obstacle(robot, cell):
            return False
        return True

    def _is_searched(self, robot: Any, cell: Cell) -> bool:
        searched = getattr(robot, "searched", None)
        if searched is None:
            searched = getattr(robot, "local_searched", set())
        return self._cell_in_collection(robot, searched, cell)

    def _is_obstacle(self, robot: Any, cell: Cell) -> bool:
        for attr in ("known_obstacles", "obstacles", "blocked", "blocked_cells"):
            cells = getattr(robot, attr, None)
            if cells is not None and self._cell_in_collection(robot, cells, cell):
                return True
        return False

    def _cell_in_collection(self, robot: Any, collection: Any, cell: Cell) -> bool:
        if collection is None:
            return False

        if isinstance(collection, dict):
            return bool(collection.get(cell, False))

        try:
            if cell in collection:
                return True
        except Exception:
            pass

        idx_fn = getattr(robot, "idx", None)
        if callable(idx_fn):
            try:
                return bool(collection[idx_fn(cell[0], cell[1])])
            except Exception:
                pass

        try:
            return bool(collection[cell[1]][cell[0]])
        except Exception:
            return False

    def _grid_size(self, robot: Any) -> int:
        grid_size = getattr(robot, "grid_size", None)
        if grid_size is not None:
            return int(grid_size)
        cfg = getattr(robot, "cfg", None)
        return int(getattr(cfg, "grid_size", 19))

    def _in_bounds(self, robot: Any, cell: Cell) -> bool:
        x, y = cell
        grid_size = self._grid_size(robot)
        return 0 <= int(x) < grid_size and 0 <= int(y) < grid_size

    def _normalize_cell(self, value: Any) -> Optional[Cell]:
        if value is None:
            return None
        try:
            x, y = value
            return int(x), int(y)
        except Exception:
            return None

    def _normalize_cell_list(self, values: Iterable[Any]) -> List[Cell]:
        cells: List[Cell] = []
        for value in values:
            cell = self._normalize_cell(value)
            if cell is not None:
                cells.append(cell)
        return cells

    # ------------------------------------------------------------------
    # Robot ID normalization / deterministic ordering
    # ------------------------------------------------------------------

    def _canonical_rid(self, rid: Any) -> str:
        text = str(rid)
        try:
            value = int(text)
            if 0 <= value <= 9:
                return f"0{value}"
            return str(value)
        except Exception:
            return text

    def _robot_id_key(self, rid: Any) -> Tuple[int, Any]:
        text = str(rid)
        try:
            return 0, int(text)
        except ValueError:
            try:
                return 0, int(self._canonical_rid(text))
            except Exception:
                return 1, text


# Optional aliases make the file easier to load if the runner expects a generic
# class name during early integration.
Allocator = DMCHBAAllocator
