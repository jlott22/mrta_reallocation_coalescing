from __future__ import annotations

from math import isfinite
from typing import Any, Dict, List, Optional, Tuple

from known_visit_sim.algorithms.base import AllocatorBase, timed_candidate_filter
from known_visit_sim.core.types import AllocationDecision, Cell



class PIAllocator(AllocatorBase):
    """Known-target allocator behavior and communication hooks."""

    name = "PI"

    BUNDLE_SIZE = 3
    REWARD_FACTOR = 5.0
    NO_OWNER = None
    EPS = 1.0e-9
    INF_SIGNIFICANCE = 1.0e18
    NO_TIME = -1.0e18
    # Increase if each searched cell should carry a fixed service/inspection
    # cost in addition to travel.
    TASK_SERVICE_COST = 0.0

    # Known-target runs use the same shared cost helper as the clue/coverage
    # simulator. Because every active known target has target_p=1.0, the helper
    # reduces to pure route distance.

    # ------------------------------------------------------------------
    # Main allocator entry point
    # ------------------------------------------------------------------

    def choose_goal(self, robot: Any) -> AllocationDecision:
        goal = self.pick_goal(robot)
        mode = "known_visit"

        return AllocationDecision(
            goal=goal,
            debug={
                "alg": self.name,
                "mode": mode,
                "pi_path": self._get_path(robot),
                "pi_bundle": self._get_bundle(robot),
                "pi_trigger": getattr(robot, "pi_last_reallocation_trigger", None),
                "pi_claims_known": self._count_known_claims(robot),
                "pi_pending_snapshot": bool(getattr(robot, "pi_pending_snapshot", False)),
                "pi_route_cost": self._route_cost(robot, self._get_path(robot)),
                "pi_bundle_size": self._planning_horizon(robot, self.BUNDLE_SIZE),
                "pi_candidate_count_before_filter": int(getattr(robot, "candidate_count_before_filter", 0)),
                "pi_candidate_count_after_filter": int(getattr(robot, "candidate_count_after_filter", 0)),
                "pi_max_candidate_cells": getattr(robot, "max_candidate_cells", None),
            },
        )

    # ------------------------------------------------------------------
    # Known-target PI allocation
    # ------------------------------------------------------------------

    def pick_goal(self, robot: Any) -> Optional[Cell]:
        self._ensure_pi_state(robot)
        self._refresh_probability_normalizer(robot)
        self._clear_invalid_or_completed_cells(robot)

        trigger = "collision_avoidance" if self._collision_activation_trigger(robot) else None
        setattr(robot, "pi_last_reallocation_trigger", trigger)
        if trigger is not None:
            self._release_own_path_for_replan(robot)

        self._repair_path_after_consensus(robot)

        # PI task inclusion: refill path up to BUNDLE_SIZE using marginal
        # insertion cost and lower-significance consensus.
        self._build_bundle(robot)

        path = self._get_path(robot)
        if not path:
            return None

        return path[0]

    def _build_bundle(self, robot: Any) -> None:
        """Insert tasks until path length reaches BUNDLE_SIZE or no task is useful."""

        self._ensure_pi_state(robot)
        self._drop_local_owner_entries_not_in_path(robot)

        changed = False

        bundle_size = self._planning_horizon(robot, self.BUNDLE_SIZE)
        while len(self._get_path(robot)) < bundle_size:
            candidate = self._best_inclusion_candidate(robot)
            if candidate is None:
                break

            cell, insertion_index, marginal_cost = candidate
            self._insert_claim(robot, cell, insertion_index, marginal_cost)
            changed = True

        if changed:
            setattr(robot, "pi_pending_snapshot", True)

    def _best_inclusion_candidate(self, robot: Any) -> Optional[Tuple[Cell, int, float]]:
        """Return the best PI insertion candidate as (cell, index, marginal_cost)."""

        owner_by_cell, significance_by_cell = self._consensus_maps(robot)
        path = self._get_path(robot)
        candidates = self._candidate_cells(robot)

        best: Optional[Tuple[Cell, int, float, float]] = None
        # Tuple layout: (cell, insertion_index, marginal_cost, improvement)

        for cell in candidates:
            if cell in path:
                continue

            if not self._valid_task_cell(robot, cell):
                continue

            insertion_index, marginal_cost = self._best_insertion(robot, path, cell)
            if insertion_index is None:
                continue

            current_owner = owner_by_cell.get(cell, self.NO_OWNER)
            known_significance = float(significance_by_cell.get(cell, self.INF_SIGNIFICANCE))

            if not self._can_include(robot, current_owner, known_significance, marginal_cost):
                continue

            improvement = self._pi_improvement(known_significance, marginal_cost)
            candidate = (cell, insertion_index, marginal_cost, improvement)

            if self._better_candidate(robot, candidate, best):
                best = candidate

        if best is None:
            return None

        cell, insertion_index, marginal_cost, _ = best
        return cell, insertion_index, marginal_cost

    @timed_candidate_filter
    def _candidate_cells(self, robot: Any) -> List[Cell]:
        grid_size = self._grid_size(robot)
        cells: List[Cell] = []
        for y in range(grid_size):
            for x in range(grid_size):
                cell = (x, y)
                if self._valid_task_cell(robot, cell):
                    cells.append(cell)
        return self._filter_candidate_cells(robot, cells)

    def _can_include(
        self,
        robot: Any,
        current_owner: Any,
        known_significance: float,
        marginal_cost: float,
    ) -> bool:
        """
        PI inclusion rule.

        Unknown/unclaimed tasks start at +infinity significance, so any finite
        marginal insertion cost can include them. For already-owned tasks, this
        robot may include the task only if it can lower the task significance, or
        if it exactly ties and wins the deterministic robot-ID tie-break.
        """

        if current_owner is self.NO_OWNER:
            return True

        if self._same_robot_id(current_owner, robot.rid):
            return True

        if marginal_cost < known_significance - self.EPS:
            return True

        if abs(marginal_cost - known_significance) <= self.EPS:
            return self._robot_id_less(robot.rid, current_owner)

        return False

    def _pi_improvement(self, known_significance: float, marginal_cost: float) -> float:
        known_significance = self._finite_nonnegative(known_significance, self.INF_SIGNIFICANCE)
        marginal_cost = self._finite_nonnegative(marginal_cost, self.INF_SIGNIFICANCE)
        if known_significance >= self.INF_SIGNIFICANCE / 2:
            return self.INF_SIGNIFICANCE
        return float(known_significance - marginal_cost)

    def _better_candidate(
        self,
        robot: Any,
        candidate: Tuple[Cell, int, float, float],
        best: Optional[Tuple[Cell, int, float, float]],
    ) -> bool:
        """Stable comparison for PI task inclusion candidates."""

        if best is None:
            return True

        cell, insertion_index, marginal_cost, improvement = candidate
        best_cell, best_index, best_marginal, best_improvement = best

        candidate_inf = improvement >= self.INF_SIGNIFICANCE / 2
        best_inf = best_improvement >= self.INF_SIGNIFICANCE / 2

        if candidate_inf != best_inf:
            return candidate_inf

        if not candidate_inf:
            if improvement > best_improvement + self.EPS:
                return True
            if improvement < best_improvement - self.EPS:
                return False

        # For unclaimed tasks with infinite improvement, or finite ties, prefer
        # lower marginal route cost. This keeps route cost as the main objective.
        if marginal_cost < best_marginal - self.EPS:
            return True
        if marginal_cost > best_marginal + self.EPS:
            return False

        # Probability already affects route cost; keep it as a final stable
        # tie-breaker for otherwise equal candidates.
        p = self._target_probability(robot, cell)
        best_p = self._target_probability(robot, best_cell)
        if p > best_p + self.EPS:
            return True
        if p < best_p - self.EPS:
            return False

        if insertion_index < best_index:
            return True
        if insertion_index > best_index:
            return False

        return cell < best_cell

    def _insert_claim(self, robot: Any, cell: Cell, insertion_index: int, marginal_cost: float) -> None:
        """Insert a task into this robot's path and refresh all local PI entries."""

        self._ensure_pi_state(robot)
        path = self._get_path(robot)

        # If stale local state still says this robot owns this cell outside its
        # path, normalize by removing that stale entry before insertion.
        if cell in path:
            return

        insertion_index = max(0, min(int(insertion_index), len(path)))
        new_path = list(path)
        new_path.insert(insertion_index, cell)
        setattr(robot, "pi_path", new_path)
        setattr(robot, "pi_bundle", list(new_path))

        self._refresh_local_path_entries(robot)
        setattr(robot, "pi_pending_snapshot", True)

    def _repair_path_after_consensus(self, robot: Any) -> None:
        """
        Remove invalid or lost tasks from this robot's path.

        PI does not use CBBA/ACBBA suffix release here. If one task is lost, only
        that task is removed; remaining path tasks are kept and their
        significances are recomputed from the new path.
        """

        self._ensure_pi_state(robot)
        path = self._get_path(robot)
        if not path:
            return

        owner_by_cell, _ = self._consensus_maps(robot)

        kept: List[Cell] = []
        removed: List[Cell] = []
        for cell in path:
            if not self._valid_task_cell(robot, cell):
                removed.append(cell)
                continue

            if not self._same_robot_id(owner_by_cell.get(cell, self.NO_OWNER), robot.rid):
                removed.append(cell)
                continue

            kept.append(cell)

        if removed:
            setattr(robot, "pi_path", kept)
            setattr(robot, "pi_bundle", list(kept))
            self._clear_removed_local_entries(robot, removed)
            self._refresh_local_path_entries(robot)
            setattr(robot, "pi_pending_snapshot", True)

    def _clear_removed_local_entries(self, robot: Any, removed: List[Cell]) -> None:
        owner_by_cell, significance_by_cell = self._consensus_maps(robot)
        time_by_cell = self._time_map(robot)

        for cell in removed:
            if self._same_robot_id(owner_by_cell.get(cell, self.NO_OWNER), robot.rid):
                owner_by_cell[cell] = self.NO_OWNER
                significance_by_cell[cell] = self.INF_SIGNIFICANCE
                time_by_cell[cell] = self.NO_TIME

    def _drop_local_owner_entries_not_in_path(self, robot: Any) -> None:
        """Clear stale local owner entries that are no longer in this robot's path."""

        path_set = set(self._get_path(robot))
        owner_by_cell, significance_by_cell = self._consensus_maps(robot)
        time_by_cell = self._time_map(robot)

        cleared = False
        for cell, owner in list(owner_by_cell.items()):
            if self._same_robot_id(owner, robot.rid) and cell not in path_set:
                owner_by_cell[cell] = self.NO_OWNER
                significance_by_cell[cell] = self.INF_SIGNIFICANCE
                time_by_cell[cell] = self.NO_TIME
                cleared = True

        if cleared:
            setattr(robot, "pi_pending_snapshot", True)

    def _refresh_local_path_entries(self, robot: Any) -> None:
        """Recompute PI significance for every task currently in this robot's path."""

        self._ensure_pi_state(robot)
        path = self._get_path(robot)
        owner_by_cell, significance_by_cell = self._consensus_maps(robot)
        time_by_cell = self._time_map(robot)

        full_cost = self._route_cost(robot, path)
        for idx, cell in enumerate(path):
            without_cell = path[:idx] + path[idx + 1:]
            without_cost = self._route_cost(robot, without_cell)
            significance = self._finite_nonnegative(full_cost - without_cost, 0.0)

            old_owner = owner_by_cell.get(cell, self.NO_OWNER)
            old_sig = float(significance_by_cell.get(cell, self.INF_SIGNIFICANCE))

            owner_by_cell[cell] = robot.rid
            significance_by_cell[cell] = significance

            if (not self._same_robot_id(old_owner, robot.rid)) or abs(old_sig - significance) > self.EPS:
                time_by_cell[cell] = self._next_time(robot)
            elif cell not in time_by_cell or time_by_cell.get(cell, self.NO_TIME) == self.NO_TIME:
                # Keep an existing timestamp if the value did not change.
                time_by_cell[cell] = self._next_time(robot)

    # ------------------------------------------------------------------
    # Route-cost helpers
    # ------------------------------------------------------------------

    def _route_cost(self, robot: Any, path: List[Cell]) -> float:
        """Return nonnegative probability-discounted movement cost for the path."""

        if not path:
            return 0.0

        current = self._robot_pos(robot)
        total = 0.0
        service_cost = self._finite_nonnegative(getattr(self, "TASK_SERVICE_COST", 0.0), 0.0)
        for cell in path:
            total += self._effective_move_cost(robot, current, cell)
            total += service_cost
            current = cell

        return self._finite_nonnegative(total, self.INF_SIGNIFICANCE)

    def _effective_move_cost(self, robot: Any, start: Cell, dest: Cell) -> float:
        """
        Return distance + 8 * (1 - normalized_target_probability[dest]).
        """

        distance = self._finite_nonnegative(self.manhattan(start[0], start[1], dest[0], dest[1]), 0.0)
        if distance <= 0.0:
            return 0.0

        cost = self._probability_adjusted_cost(robot, distance, dest)
        return self._finite_nonnegative(cost, self.INF_SIGNIFICANCE)

    def _refresh_probability_normalizer(self, robot: Any) -> None:
        """Cache the max target probability used to normalize target_p to [0, 1]."""

        try:
            grid_size = self._grid_size(robot)
        except Exception:
            setattr(robot, "pi_probability_normalizer", 1.0)
            return

        max_p = 0.0
        for y in range(grid_size):
            for x in range(grid_size):
                try:
                    p = float(self._target_probability(robot, (x, y)))
                except Exception:
                    continue

                if isfinite(p) and p > max_p:
                    max_p = p

        if max_p <= self.EPS or not isfinite(max_p):
            max_p = 1.0

        setattr(robot, "pi_probability_normalizer", float(max_p))

    def _normalized_target_probability(self, robot: Any, cell: Cell) -> float:
        """Return target_p[cell] / max(target_p) clamped to [0, 1]."""

        normalizer = float(getattr(robot, "pi_probability_normalizer", 0.0) or 0.0)
        if normalizer <= self.EPS or not isfinite(normalizer):
            self._refresh_probability_normalizer(robot)
            normalizer = float(getattr(robot, "pi_probability_normalizer", 1.0) or 1.0)

        try:
            p = float(self._target_probability(robot, cell))
        except Exception:
            p = 0.0

        if not isfinite(p) or p <= 0.0:
            return 0.0

        return self._normalized_allocation_probability(robot, cell)

    def _best_insertion(self, robot: Any, path: List[Cell], cell: Cell) -> Tuple[Optional[int], float]:
        """Find insertion position with smallest probability-discounted cost increase."""

        if cell in path:
            return None, self.INF_SIGNIFICANCE

        base_cost = self._route_cost(robot, path)
        best_index: Optional[int] = None
        best_delta = self.INF_SIGNIFICANCE

        for index in range(len(path) + 1):
            candidate_path = path[:index] + [cell] + path[index:]
            delta = self._finite_nonnegative(self._route_cost(robot, candidate_path) - base_cost, self.INF_SIGNIFICANCE)

            if delta < best_delta - self.EPS:
                best_delta = delta
                best_index = index
            elif abs(delta - best_delta) <= self.EPS and best_index is not None:
                if index < best_index:
                    best_index = index

        return best_index, best_delta

    # ------------------------------------------------------------------
    # PI communication hooks
    # ------------------------------------------------------------------

    def build_pi_messages(self, robot: Any) -> List[dict]:
        """Known-target allocator behavior and communication hooks."""

        self._ensure_pi_state(robot)

        if not bool(getattr(robot, "pi_pending_snapshot", False)):
            return []

        path = self._get_path(robot)
        current_signature = self._path_signature(robot)
        if current_signature == getattr(robot, "pi_last_sent_signature", None):
            setattr(robot, "pi_pending_snapshot", False)
            return []

        owner_by_cell, significance_by_cell = self._consensus_maps(robot)
        time_by_cell = self._time_map(robot)
        path_cells = [{"x": cell[0], "y": cell[1]} for cell in path]

        if not path:
            timestamp = self._next_time(robot)
            setattr(robot, "pi_pending_snapshot", False)
            setattr(robot, "pi_last_sent_signature", current_signature)
            return [{
                "type": "pi_clear_path",
                "sender": robot.rid,
                "timestamp": timestamp,
                "path_cells": [],
                "path_size": 0,
            }]

        messages: List[dict] = []
        for order, cell in enumerate(path):
            if not self._same_robot_id(owner_by_cell.get(cell, self.NO_OWNER), robot.rid):
                continue

            significance = float(significance_by_cell.get(cell, self.INF_SIGNIFICANCE))
            timestamp = float(time_by_cell.get(cell, self.NO_TIME))

            messages.append({
                "type": "pi_entry",
                "sender": robot.rid,
                "x": cell[0],
                "y": cell[1],
                "owner": robot.rid,
                "significance": significance,
                "timestamp": timestamp,
                "order": order,
                "path_cells": list(path_cells),
                "path_size": len(path),
            })

        setattr(robot, "pi_pending_snapshot", False)
        setattr(robot, "pi_last_sent_signature", current_signature)
        return messages

    # Generic aliases used by different simulator wiring conventions.
    def build_cbaa_messages(self, robot: Any) -> List[dict]:
        return self.build_pi_messages(robot)

    def build_acbba_messages(self, robot: Any) -> List[dict]:
        return self.build_pi_messages(robot)

    def make_messages(self, robot: Any) -> List[dict]:
        return self.build_pi_messages(robot)

    def get_outbound_messages(self, robot: Any) -> List[dict]:
        return self.build_pi_messages(robot)

    def build_pi_message(self, robot: Any) -> List[dict]:
        return self.build_pi_messages(robot)

    def build_cbaa_message(self, robot: Any) -> List[dict]:
        return self.build_pi_messages(robot)

    def build_acbba_message(self, robot: Any) -> List[dict]:
        return self.build_pi_messages(robot)

    def make_message(self, robot: Any) -> List[dict]:
        return self.build_pi_messages(robot)

    def make_pi_message(self, robot: Any) -> List[dict]:
        return self.build_pi_messages(robot)

    def make_cbaa_message(self, robot: Any) -> List[dict]:
        return self.build_pi_messages(robot)

    def make_acbba_message(self, robot: Any) -> List[dict]:
        return self.build_pi_messages(robot)

    def get_outbound_message(self, robot: Any) -> List[dict]:
        return self.build_pi_messages(robot)

    def on_collision_avoidance_activated(self, robot: Any) -> bool:
        return True

    def handle_pi_message(self, robot: Any, message: Any) -> None:
        if not isinstance(message, dict):
            return

        msg_type = message.get("type")
        if msg_type not in ("pi_entry", "pi_clear_path"):
            return

        sender = message.get("sender")
        if sender is None or self._same_robot_id(sender, robot.rid):
            return

        self._ensure_pi_state(robot)

        if msg_type == "pi_clear_path":
            path_cells = self._parse_path_cells(message.get("path_cells", []))
            if path_cells is not None:
                self._clear_sender_claims_not_in_path(robot, sender, path_cells)
            self._repair_path_after_consensus(robot)
            self._sync_current_goal_after_message(robot)
            return

        parsed = self._parse_pi_entry(message)
        if parsed is None:
            return

        cell, received_owner, received_significance, received_time, path_cells = parsed

        if path_cells is not None:
            self._clear_sender_claims_not_in_path(robot, sender, path_cells)

        if not self._in_bounds(robot, cell):
            self._repair_path_after_consensus(robot)
            self._sync_current_goal_after_message(robot)
            return

        owner_by_cell, significance_by_cell = self._consensus_maps(robot)
        time_by_cell = self._time_map(robot)

        if not self._valid_task_cell(robot, cell):
            if self._same_robot_id(owner_by_cell.get(cell, self.NO_OWNER), received_owner):
                owner_by_cell[cell] = self.NO_OWNER
                significance_by_cell[cell] = self.INF_SIGNIFICANCE
                time_by_cell[cell] = self.NO_TIME
            self._repair_path_after_consensus(robot)
            self._sync_current_goal_after_message(robot)
            return

        local_owner = owner_by_cell.get(cell, self.NO_OWNER)
        local_significance = float(significance_by_cell.get(cell, self.INF_SIGNIFICANCE))
        local_time = float(time_by_cell.get(cell, self.NO_TIME))

        should_update = False

        # Same-owner update: accept newer self-report from the owner.
        if self._same_robot_id(received_owner, local_owner):
            should_update = received_time >= local_time - self.EPS
        elif local_owner is self.NO_OWNER:
            should_update = True
        elif received_significance < local_significance - self.EPS:
            should_update = True
        elif abs(received_significance - local_significance) <= self.EPS:
            should_update = self._robot_id_less(received_owner, local_owner)

        if should_update:
            owner_by_cell[cell] = received_owner
            significance_by_cell[cell] = float(received_significance)
            time_by_cell[cell] = float(received_time)

        self._repair_path_after_consensus(robot)
        self._sync_current_goal_after_message(robot)

    # Aliases for receiver wiring.
    def handle_cbaa_message(self, robot: Any, message: Any) -> None:
        self.handle_pi_message(robot, message)

    def handle_acbba_message(self, robot: Any, message: Any) -> None:
        self.handle_pi_message(robot, message)

    def receive_message(self, robot: Any, message: Any) -> None:
        self.handle_pi_message(robot, message)

    def on_message(self, robot: Any, message: Any) -> None:
        self.handle_pi_message(robot, message)

    def process_message(self, robot: Any, message: Any) -> None:
        self.handle_pi_message(robot, message)

    def _parse_pi_entry(
        self,
        message: Dict[str, Any],
    ) -> Optional[Tuple[Cell, Any, float, float, Optional[set[Cell]]]]:
        try:
            if "cell" in message:
                x, y = message["cell"]
            else:
                x, y = message["x"], message["y"]

            owner = self._normalize_owner(message.get("owner", self.NO_OWNER))
            significance = float(message.get("significance", self.INF_SIGNIFICANCE))
            timestamp = float(message.get("timestamp", message.get("time", self.NO_TIME)))
            path_cells = self._parse_path_cells(message.get("path_cells", None))

            if not isfinite(significance):
                significance = self.INF_SIGNIFICANCE

            significance = self._finite_nonnegative(significance, self.INF_SIGNIFICANCE)

            return (int(x), int(y)), owner, significance, timestamp, path_cells
        except Exception:
            return None

    def _finite_nonnegative(self, value: Any, fallback: float) -> float:
        try:
            number = float(value)
        except Exception:
            number = float(fallback)

        if not isfinite(number):
            number = float(fallback)

        if not isfinite(number):
            number = self.INF_SIGNIFICANCE

        return float(max(0.0, min(number, self.INF_SIGNIFICANCE)))

    def _parse_path_cells(self, raw: Any) -> Optional[set[Cell]]:
        if raw is None:
            return None

        if not isinstance(raw, list):
            return None

        parsed: set[Cell] = set()
        try:
            for item in raw:
                if isinstance(item, dict):
                    parsed.add((int(item["x"]), int(item["y"])))
                else:
                    x, y = item
                    parsed.add((int(x), int(y)))
            return parsed
        except Exception:
            return None

    def _clear_sender_claims_not_in_path(self, robot: Any, sender: Any, path_cells: set[Cell]) -> None:
        owner_by_cell, significance_by_cell = self._consensus_maps(robot)
        time_by_cell = self._time_map(robot)

        for cell, owner in list(owner_by_cell.items()):
            if cell not in path_cells and self._same_robot_id(owner, sender):
                owner_by_cell[cell] = self.NO_OWNER
                significance_by_cell[cell] = self.INF_SIGNIFICANCE
                time_by_cell[cell] = self.NO_TIME

    # ------------------------------------------------------------------
    # PI local state helpers
    # ------------------------------------------------------------------

    def _ensure_cbaa_state(self, robot: Any) -> None:
        """Compatibility shim for inherited helper methods."""
        self._ensure_pi_state(robot)

    def _ensure_pi_state(self, robot: Any) -> None:
        if not hasattr(robot, "pi_owner_by_cell") or getattr(robot, "pi_owner_by_cell") is None:
            setattr(robot, "pi_owner_by_cell", {})

        if not hasattr(robot, "pi_significance_by_cell") or getattr(robot, "pi_significance_by_cell") is None:
            setattr(robot, "pi_significance_by_cell", {})

        if not hasattr(robot, "pi_time_by_cell") or getattr(robot, "pi_time_by_cell") is None:
            setattr(robot, "pi_time_by_cell", {})

        if not hasattr(robot, "pi_bundle") or getattr(robot, "pi_bundle") is None:
            setattr(robot, "pi_bundle", [])

        if not hasattr(robot, "pi_path") or getattr(robot, "pi_path") is None:
            setattr(robot, "pi_path", [])


        if not hasattr(robot, "pi_time_counter"):
            setattr(robot, "pi_time_counter", 0)

        if not hasattr(robot, "pi_pending_snapshot"):
            setattr(robot, "pi_pending_snapshot", False)

        if not hasattr(robot, "pi_last_sent_signature"):
            setattr(robot, "pi_last_sent_signature", None)

        if not hasattr(robot, "pi_probability_normalizer"):
            setattr(robot, "pi_probability_normalizer", 1.0)

        if not hasattr(robot, "pi_last_collision_active"):
            setattr(robot, "pi_last_collision_active", False)

        if not hasattr(robot, "pi_last_reallocation_trigger"):
            setattr(robot, "pi_last_reallocation_trigger", None)

    def _reset_cbaa_state(self, robot: Any) -> None:
        self._reset_pi_state(robot)

    def _reset_pi_state(self, robot: Any) -> None:
        setattr(robot, "pi_owner_by_cell", {})
        setattr(robot, "pi_significance_by_cell", {})
        setattr(robot, "pi_time_by_cell", {})
        setattr(robot, "pi_bundle", [])
        setattr(robot, "pi_path", [])
        setattr(robot, "pi_time_counter", 0)
        setattr(robot, "pi_pending_snapshot", False)
        setattr(robot, "pi_last_sent_signature", None)
        setattr(robot, "pi_probability_normalizer", 1.0)
        setattr(robot, "pi_last_reallocation_trigger", None)


    def _consensus_maps(self, robot: Any) -> Tuple[Dict[Cell, Any], Dict[Cell, float]]:
        self._ensure_pi_state(robot)
        return getattr(robot, "pi_owner_by_cell"), getattr(robot, "pi_significance_by_cell")

    def _time_map(self, robot: Any) -> Dict[Cell, float]:
        self._ensure_pi_state(robot)
        return getattr(robot, "pi_time_by_cell")

    def _get_bundle(self, robot: Any) -> List[Cell]:
        self._ensure_pi_state(robot)
        return self._normalize_cell_list(getattr(robot, "pi_bundle", []))

    def _get_path(self, robot: Any) -> List[Cell]:
        self._ensure_pi_state(robot)
        return self._normalize_cell_list(getattr(robot, "pi_path", []))

    def _release_own_path_for_replan(self, robot: Any) -> None:
        """Release the stored local path so normal PI inclusion can rebuild it."""

        self._ensure_pi_state(robot)
        old_path = self._get_path(robot)
        if not old_path:
            return

        self._clear_removed_local_entries(robot, old_path)
        setattr(robot, "pi_path", [])
        setattr(robot, "pi_bundle", [])
        setattr(robot, "pi_pending_snapshot", True)

    def _collision_activation_trigger(self, robot: Any) -> bool:
        """Return True only on the rising edge of a collision-avoidance flag."""

        active = self._collision_active(robot)
        previous = bool(getattr(robot, "pi_last_collision_active", False))
        setattr(robot, "pi_last_collision_active", active)
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

    def _normalize_cell_list(self, values: Any) -> List[Cell]:
        cells: List[Cell] = []
        if not isinstance(values, list):
            return cells

        for value in values:
            try:
                x, y = value
                cell = (int(x), int(y))
                if cell not in cells:
                    cells.append(cell)
            except Exception:
                continue

        return cells

    def _count_known_claims(self, robot: Any) -> int:
        self._ensure_pi_state(robot)
        table = getattr(robot, "pi_owner_by_cell", {}) or {}
        return sum(1 for owner in table.values() if owner is not self.NO_OWNER)

    def _clear_invalid_or_completed_cells(self, robot: Any) -> None:
        self._ensure_pi_state(robot)
        owner_by_cell, significance_by_cell = self._consensus_maps(robot)
        time_by_cell = self._time_map(robot)

        cells = set(owner_by_cell.keys()) | set(significance_by_cell.keys()) | set(time_by_cell.keys())

        changed = False
        for cell in cells:
            if not self._valid_task_cell(robot, cell):
                if owner_by_cell.get(cell, self.NO_OWNER) is not self.NO_OWNER:
                    changed = True
                owner_by_cell[cell] = self.NO_OWNER
                significance_by_cell[cell] = self.INF_SIGNIFICANCE
                time_by_cell[cell] = self.NO_TIME

        self._repair_path_after_consensus(robot)
        if changed:
            setattr(robot, "pi_pending_snapshot", True)

    def _sync_current_goal_after_message(self, robot: Any) -> None:
        previous_goal = self._current_goal(robot)
        path = self._get_path(robot)
        if previous_goal is not None and not path and hasattr(robot, "current_goal"):
            setattr(robot, "current_goal", None)

    def _next_time(self, robot: Any) -> float:
        self._ensure_pi_state(robot)
        counter = int(getattr(robot, "pi_time_counter", 0)) + 1
        setattr(robot, "pi_time_counter", counter)
        return float(counter)

    def _path_signature(self, robot: Any) -> Tuple[Tuple[Cell, float, float], ...]:
        path = self._get_path(robot)
        _, significance_by_cell = self._consensus_maps(robot)
        time_by_cell = self._time_map(robot)

        signature: List[Tuple[Cell, float, float]] = []
        for cell in path:
            signature.append((
                cell,
                float(significance_by_cell.get(cell, self.INF_SIGNIFICANCE)),
                float(time_by_cell.get(cell, self.NO_TIME)),
            ))

        return tuple(signature)

    def _robot_pos(self, robot: Any) -> Cell:
        x, y = getattr(robot, "pos")
        return int(x), int(y)

    def _normalize_owner(self, owner: Any) -> Any:
        if owner in (None, "", "None", "none", "null", -1, "-1"):
            return self.NO_OWNER
        return owner

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


    @staticmethod
    def manhattan(x1: int, y1: int, x2: int, y2: int) -> int:
        return abs(x1 - x2) + abs(y1 - y2)



    def _target_probability(self, robot: Any, cell: Cell) -> float:
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
        searched = getattr(robot, "searched", None)
        if searched is None:
            searched = getattr(robot, "local_searched", set())
        return cell in searched

    def _is_obstacle(self, robot: Any, cell: Cell) -> bool:
        for attr in ("known_obstacles", "obstacles", "blocked", "blocked_cells"):
            cells = getattr(robot, attr, None)
            if cells is not None and cell in cells:
                return True
        return False

    def _current_goal(self, robot: Any) -> Optional[Cell]:
        return getattr(robot, "current_goal", None)

    def _grid_size(self, robot: Any) -> int:
        grid_size = getattr(robot, "grid_size", None)
        if grid_size is not None:
            return int(grid_size)

        cfg = getattr(robot, "cfg", None)
        return int(getattr(cfg, "grid_size", 19))

    def _in_bounds(self, robot: Any, cell: Cell) -> bool:
        x, y = cell
        grid_size = self._grid_size(robot)
        return 0 <= x < grid_size and 0 <= y < grid_size

    def _same_robot_id(self, a: Any, b: Any) -> bool:
        if a is self.NO_OWNER or b is self.NO_OWNER:
            return a is self.NO_OWNER and b is self.NO_OWNER
        return self._robot_id_key(a) == self._robot_id_key(b)

    def _robot_id_less(self, a: Any, b: Any) -> bool:
        return self._robot_id_key(a) < self._robot_id_key(b)

    def _robot_id_key(self, rid: Any) -> Tuple[int, Any]:
        text = str(rid)
        try:
            return 0, int(text)
        except ValueError:
            return 1, text


Allocator = PIAllocator
