from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from known_visit_sim.algorithms.base import AllocatorBase, timed_candidate_filter
from known_visit_sim.core.types import AllocationDecision, Cell


class ACBBAAllocator(AllocatorBase):
    """Known-target allocator behavior and communication hooks."""

    name = "ACBBA"

    BUNDLE_SIZE = 3
    REWARD_FACTOR = 5.0
    NO_WINNER = None
    NO_BID = -1.0e18
    EPS = 1.0e-9
    EPS_TIME = 1.0e-9
    NO_TIME = -1.0e18

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
                "acbba_path": self._get_path(robot),
                "acbba_bundle": self._get_bundle(robot),
                "acbba_trigger": getattr(robot, "acbba_last_reallocation_trigger", None),
                "acbba_claims_known": self._count_known_claims(robot),
                "acbba_pending_snapshot": bool(getattr(robot, "acbba_pending_snapshot", False)),
                "acbba_bundle_size": self._planning_horizon(robot, self.BUNDLE_SIZE),
                "acbba_candidate_count_before_filter": int(getattr(robot, "candidate_count_before_filter", 0)),
                "acbba_candidate_count_after_filter": int(getattr(robot, "candidate_count_after_filter", 0)),
                "acbba_max_candidate_cells": getattr(robot, "max_candidate_cells", None),
            },
        )

    # ------------------------------------------------------------------
    # Known-target ACBBA allocation
    # ------------------------------------------------------------------

    def pick_goal(self, robot: Any) -> Optional[Cell]:
        self._ensure_acbba_state(robot)
        self._clear_invalid_or_completed_cells(robot)

        trigger = "collision_avoidance" if self._collision_activation_trigger(robot) else None
        setattr(robot, "acbba_last_reallocation_trigger", trigger)
        if trigger is not None:
            self._release_own_bundle_for_replan(robot)

        self._repair_bundle_after_consensus(robot)

        # Insert new tasks until the bundle is full or no viable task remains.
        self._build_bundle(robot)

        path = self._get_path(robot)
        if not path:
            return None

        return path[0]

    def _build_bundle(self, robot: Any) -> None:
        """Greedily insert tasks until the path/bundle reaches BUNDLE_SIZE."""

        self._ensure_acbba_state(robot)
        path = self._get_path(robot)
        bundle = self._get_bundle(robot)

        changed = False

        bundle_size = self._planning_horizon(robot, self.BUNDLE_SIZE)
        candidates = self._candidate_cells(robot)

        while len(path) < bundle_size:
            best_cell: Optional[Cell] = None
            best_index = 0
            best_bid = self.NO_BID

            for cell in candidates:
                if cell in path or cell in bundle:
                    continue

                if not self._valid_task_cell(robot, cell):
                    continue

                insertion_index, my_bid = self._best_insertion_bid(robot, path, cell)

                if not self._can_claim(robot, cell, my_bid):
                    continue

                if self._better_insertion_choice(
                    cell,
                    insertion_index,
                    my_bid,
                    best_cell,
                    best_index,
                    best_bid,
                ):
                    best_cell = cell
                    best_index = insertion_index
                    best_bid = my_bid

            if best_cell is None:
                break

            self._insert_claim(robot, best_cell, best_index, best_bid)
            path = self._get_path(robot)
            bundle = self._get_bundle(robot)
            changed = True

        if changed:
            setattr(robot, "acbba_pending_snapshot", True)

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

    def _route_distance(self, robot: Any, path: List[Cell]) -> float:
        """Return route distance robot.pos -> path[0] -> path[1] -> ..."""

        if not path:
            return 0.0

        distance = 0
        previous = self._robot_pos(robot)
        for cell in path:
            distance += self.manhattan(previous[0], previous[1], cell[0], cell[1])
            previous = cell

        return float(distance)

    def _best_insertion_bid(self, robot: Any, path: List[Cell], cell: Cell) -> Tuple[int, float]:
        """Return the best insertion index and bid for adding cell to path."""

        current_distance = self._route_distance(robot, path)
        best_index = 0
        best_bid = self.NO_BID

        for insertion_index in range(len(path) + 1):
            candidate_path = path[:insertion_index] + [cell] + path[insertion_index:]
            marginal_distance = self._route_distance(robot, candidate_path) - current_distance
            marginal_distance = max(0.0, marginal_distance)
            bid = self._probability_adjusted_score(robot, marginal_distance, cell)

            if bid > best_bid + self.EPS:
                best_index = insertion_index
                best_bid = bid
            elif abs(bid - best_bid) <= self.EPS and insertion_index < best_index:
                best_index = insertion_index
                best_bid = bid

        return best_index, best_bid

    def _better_insertion_choice(
        self,
        cell: Cell,
        insertion_index: int,
        bid: float,
        best_cell: Optional[Cell],
        best_index: int,
        best_bid: float,
    ) -> bool:
        """Tie-safe comparison for route-insertion bundle construction."""

        if best_cell is None:
            return True

        if bid > best_bid + self.EPS:
            return True

        if abs(bid - best_bid) <= self.EPS:
            if cell != best_cell:
                return cell < best_cell
            return insertion_index < best_index

        return False

    def _bid_from_reference(self, robot: Any, cell: Cell, reference: Cell) -> float:
        distance = self.manhattan(cell[0], cell[1], reference[0], reference[1])
        return self._probability_adjusted_score(robot, distance, cell)

    def _append_claim(self, robot: Any, cell: Cell, bid: float) -> None:
        self._insert_claim(robot, cell, len(self._get_path(robot)), bid)

    def _insert_claim(self, robot: Any, cell: Cell, insertion_index: int, bid: float) -> None:
        self._ensure_acbba_state(robot)

        path = self._get_path(robot)
        bundle = self._get_bundle(robot)
        index = max(0, min(int(insertion_index), len(path)))

        path.insert(index, cell)
        bundle.insert(index, cell)

        bid_time = self._next_bid_time(robot)
        self._set_table_entry(robot, cell, robot.rid, float(bid), bid_time, queue=True)

        setattr(robot, "acbba_path", path)
        setattr(robot, "acbba_bundle", bundle)

    def _repair_bundle_after_consensus(self, robot: Any) -> None:
        """
        Enforce CBBA/ACBBA suffix release.

        If this robot loses task k in its bundle/path, then task k and all later
        tasks are no longer valid because their bids depended on the earlier
        bundle prefix.
        """

        self._ensure_acbba_state(robot)

        path = self._get_path(robot)
        if not path:
            return

        winner_by_cell, _ = self._consensus_maps(robot)

        first_bad_index: Optional[int] = None
        for idx, cell in enumerate(path):
            if not self._valid_task_cell(robot, cell):
                first_bad_index = idx
                break

            if not self._same_robot_id(winner_by_cell.get(cell), robot.rid):
                first_bad_index = idx
                break

        if first_bad_index is not None:
            self._truncate_bundle_from(robot, first_bad_index)

    def _truncate_bundle_from(self, robot: Any, index: int) -> None:
        self._ensure_acbba_state(robot)

        path = self._get_path(robot)
        bundle = self._get_bundle(robot)

        if index < 0 or index >= len(path):
            return

        suffix = path[index:]

        winner_by_cell, _ = self._consensus_maps(robot)

        for cell in suffix:
            if self._same_robot_id(winner_by_cell.get(cell), robot.rid):
                self._set_table_entry(robot, cell, self.NO_WINNER, self.NO_BID, self.NO_TIME, queue=True)

        setattr(robot, "acbba_path", path[:index])
        setattr(robot, "acbba_bundle", bundle[:index])
        setattr(robot, "acbba_pending_snapshot", True)

    # ------------------------------------------------------------------
    # ACBBA communication hooks
    # ------------------------------------------------------------------

    def build_acbba_messages(self, robot: Any) -> List[dict]:
        """Known-target allocator behavior and communication hooks."""

        self._ensure_acbba_state(robot)

        if bool(getattr(robot, "acbba_pending_snapshot", False)):
            self._queue_current_bundle_entries(robot)
            setattr(robot, "acbba_pending_snapshot", False)
            setattr(robot, "acbba_last_sent_signature", self._bundle_signature(robot))

        pending = getattr(robot, "acbba_pending_deltas", {}) or {}
        if not pending:
            return []

        last_sent = getattr(robot, "acbba_last_sent_signatures", {}) or {}
        path = self._get_path(robot)
        ordered_cells = [cell for cell in path if cell in pending]
        ordered_cells.extend(sorted(cell for cell in pending if cell not in set(ordered_cells)))

        messages: List[dict] = []
        for cell in ordered_cells:
            payload = dict(pending[cell])
            signature = self._message_signature(payload)
            if self._same_signature(last_sent.get(cell), signature):
                continue
            messages.append(payload)
            last_sent[cell] = signature

        setattr(robot, "acbba_pending_deltas", {})
        setattr(robot, "acbba_last_sent_signatures", last_sent)
        return messages

    # Generic aliases used by different simulator wiring conventions.
    def build_cbaa_messages(self, robot: Any) -> List[dict]:
        return self.build_acbba_messages(robot)

    def make_messages(self, robot: Any) -> List[dict]:
        return self.build_acbba_messages(robot)

    def get_outbound_messages(self, robot: Any) -> List[dict]:
        return self.build_acbba_messages(robot)

    def build_acbba_message(self, robot: Any) -> List[dict]:
        return self.build_acbba_messages(robot)

    def build_cbaa_message(self, robot: Any) -> List[dict]:
        return self.build_acbba_messages(robot)

    def make_message(self, robot: Any) -> List[dict]:
        return self.build_acbba_messages(robot)

    def make_acbba_message(self, robot: Any) -> List[dict]:
        return self.build_acbba_messages(robot)

    def make_cbaa_message(self, robot: Any) -> List[dict]:
        return self.build_acbba_messages(robot)

    def get_outbound_message(self, robot: Any) -> List[dict]:
        return self.build_acbba_messages(robot)

    def on_collision_avoidance_activated(self, robot: Any) -> bool:
        return True

    def handle_acbba_message(self, robot: Any, message: Any) -> None:
        """
        Apply the ACBBA Table 1 asynchronous deconfliction protocol.

        Incoming acbba_entry messages may be direct claims/releases from the
        sender or rebroadcasted third-party table information. `sender` is the
        transmitting robot; `winner` is the believed task winner.
        """

        if not isinstance(message, dict):
            return

        if message.get("type") != "acbba_entry":
            return

        sender = message.get("sender")
        if sender is None or self._same_robot_id(sender, robot.rid):
            return

        self._ensure_acbba_state(robot)

        parsed = self._parse_acbba_entry(message)
        if parsed is None:
            return

        cell, received_winner, received_bid, received_time, bundle_cells = parsed

        if bundle_cells is not None and self._same_robot_id(received_winner, sender):
            stale_changed = self._clear_sender_claims_not_in_bundle(robot, sender, bundle_cells)
            if stale_changed:
                self._repair_bundle_after_consensus(robot)
                self._sync_current_goal_after_message(robot)

        if not self._in_bounds(robot, cell):
            return

        if not self._valid_task_cell(robot, cell):
            changed = self._set_table_entry(robot, cell, self.NO_WINNER, self.NO_BID, self.NO_TIME, queue=True)
            if changed:
                self._repair_bundle_after_consensus(robot)
            self._sync_current_goal_after_message(robot)
            return

        changed = self._apply_table1_decision(
            robot,
            cell,
            sender,
            received_winner,
            float(received_bid),
            float(received_time),
        )
        if changed:
            self._repair_bundle_after_consensus(robot)
        self._sync_current_goal_after_message(robot)

    def _apply_table1_decision(
        self,
        robot: Any,
        cell: Cell,
        sender: Any,
        received_winner: Any,
        received_bid: float,
        received_time: float,
    ) -> bool:
        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)
        local_winner = winner_by_cell.get(cell, self.NO_WINNER)
        local_bid = float(winning_bid_by_cell.get(cell, self.NO_BID))
        local_time = float(bid_time_by_cell.get(cell, self.NO_TIME))

        receiver = robot.rid

        if self._same_robot_id(received_winner, sender):
            if self._same_robot_id(local_winner, receiver):
                if self._bid_gt(received_bid, local_bid):
                    return self._action_update_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
                if self._bid_eq(received_bid, local_bid) and self._robot_id_less(received_winner, local_winner):
                    return self._action_update_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
                if self._bid_lt(received_bid, local_bid):
                    return self._action_update_time_and_rebroadcast(robot, cell)
                return self._action_leave_and_rebroadcast(robot, cell)

            if self._same_robot_id(local_winner, sender):
                if self._time_gt(received_time, local_time):
                    return self._action_update_no_rebroadcast(robot, cell, received_winner, received_bid, received_time)
                return self._action_leave_no_rebroadcast(robot, cell)

            if local_winner is self.NO_WINNER:
                return self._action_update_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)

            if self._bid_gt(received_bid, local_bid) and self._time_gte(received_time, local_time):
                return self._action_update_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
            if self._bid_lt(received_bid, local_bid) and self._time_lte(received_time, local_time):
                return self._action_leave_and_rebroadcast(robot, cell)
            if self._bid_eq(received_bid, local_bid):
                return self._action_leave_and_rebroadcast(robot, cell)
            if self._bid_lt(received_bid, local_bid) and self._time_gt(received_time, local_time):
                return self._action_reset_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
            if self._bid_gt(received_bid, local_bid) and self._time_lt(received_time, local_time):
                return self._action_reset_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
            return self._action_leave_and_rebroadcast(robot, cell)

        if self._same_robot_id(received_winner, receiver):
            if self._same_robot_id(local_winner, receiver):
                if self._time_eq(received_time, local_time):
                    return self._action_leave_no_rebroadcast(robot, cell)
                return self._action_leave_and_rebroadcast(robot, cell)
            if self._same_robot_id(local_winner, sender):
                return self._action_reset_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
            return self._action_leave_and_rebroadcast(robot, cell)

        if received_winner is not self.NO_WINNER:
            if self._same_robot_id(local_winner, receiver):
                if self._bid_gt(received_bid, local_bid):
                    return self._action_update_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
                if self._bid_eq(received_bid, local_bid) and self._robot_id_less(received_winner, local_winner):
                    return self._action_update_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
                if self._bid_lt(received_bid, local_bid):
                    return self._action_update_time_and_rebroadcast(robot, cell)
                return self._action_leave_and_rebroadcast(robot, cell)

            if self._same_robot_id(local_winner, sender):
                if self._time_gte(received_time, local_time):
                    return self._action_update_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
                if self._time_lt(received_time, local_time):
                    return self._action_reset_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
                return self._action_leave_and_rebroadcast(robot, cell)

            if self._same_robot_id(local_winner, received_winner):
                if self._time_gt(received_time, local_time):
                    return self._action_update_no_rebroadcast(robot, cell, received_winner, received_bid, received_time)
                return self._action_leave_no_rebroadcast(robot, cell)

            if local_winner is self.NO_WINNER:
                return self._action_update_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)

            if self._bid_gt(received_bid, local_bid) and self._time_gte(received_time, local_time):
                return self._action_update_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
            if self._bid_lt(received_bid, local_bid) and self._time_lte(received_time, local_time):
                return self._action_leave_and_rebroadcast(robot, cell)
            if self._bid_eq(received_bid, local_bid):
                return self._action_leave_and_rebroadcast(robot, cell)
            if self._bid_lt(received_bid, local_bid) and self._time_gt(received_time, local_time):
                return self._action_reset_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
            if self._bid_gt(received_bid, local_bid) and self._time_lt(received_time, local_time):
                return self._action_reset_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
            return self._action_leave_and_rebroadcast(robot, cell)

        if self._same_robot_id(local_winner, receiver):
            return self._action_leave_and_rebroadcast(robot, cell)
        if self._same_robot_id(local_winner, sender):
            return self._action_update_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
        if local_winner is self.NO_WINNER:
            return self._action_leave_no_rebroadcast(robot, cell)
        if self._time_gt(received_time, local_time):
            return self._action_update_and_rebroadcast(robot, cell, received_winner, received_bid, received_time)
        return self._action_leave_and_rebroadcast(robot, cell)

    def _action_update_and_rebroadcast(
        self,
        robot: Any,
        cell: Cell,
        winner: Any,
        bid: float,
        timestamp: float,
    ) -> bool:
        changed = self._set_table_entry(robot, cell, winner, bid, timestamp, queue=False)
        self._queue_acbba_delta(robot, cell, winner, bid, timestamp)
        return changed

    def update_and_rebroadcast(
        self,
        robot: Any,
        cell: Cell,
        winner: Any,
        bid: float,
        timestamp: float,
    ) -> bool:
        return self._action_update_and_rebroadcast(robot, cell, winner, bid, timestamp)

    def _action_update_no_rebroadcast(
        self,
        robot: Any,
        cell: Cell,
        winner: Any,
        bid: float,
        timestamp: float,
    ) -> bool:
        return self._set_table_entry(robot, cell, winner, bid, timestamp, queue=False)

    def update_no_rebroadcast(
        self,
        robot: Any,
        cell: Cell,
        winner: Any,
        bid: float,
        timestamp: float,
    ) -> bool:
        return self._action_update_no_rebroadcast(robot, cell, winner, bid, timestamp)

    def _action_leave_and_rebroadcast(self, robot: Any, cell: Cell) -> bool:
        self._queue_local_consensus_delta(robot, cell)
        return False

    def leave_and_rebroadcast(self, robot: Any, cell: Cell) -> bool:
        return self._action_leave_and_rebroadcast(robot, cell)

    def _action_leave_no_rebroadcast(self, robot: Any, cell: Cell) -> bool:
        return False

    def leave_no_rebroadcast(self, robot: Any, cell: Cell) -> bool:
        return self._action_leave_no_rebroadcast(robot, cell)

    def _action_reset_and_rebroadcast(
        self,
        robot: Any,
        cell: Cell,
        incoming_winner: Any,
        incoming_bid: float,
        incoming_timestamp: float,
    ) -> bool:
        changed = self._set_table_entry(robot, cell, self.NO_WINNER, self.NO_BID, self.NO_TIME, queue=False)
        self._queue_acbba_delta(robot, cell, incoming_winner, incoming_bid, incoming_timestamp)
        return changed

    def reset_and_rebroadcast(
        self,
        robot: Any,
        cell: Cell,
        incoming_winner: Any,
        incoming_bid: float,
        incoming_timestamp: float,
    ) -> bool:
        return self._action_reset_and_rebroadcast(robot, cell, incoming_winner, incoming_bid, incoming_timestamp)

    def _action_update_time_and_rebroadcast(self, robot: Any, cell: Cell) -> bool:
        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)
        local_winner = winner_by_cell.get(cell, self.NO_WINNER)
        local_bid = float(winning_bid_by_cell.get(cell, self.NO_BID))
        if not self._same_robot_id(local_winner, robot.rid):
            return self._action_leave_and_rebroadcast(robot, cell)

        timestamp = self._next_bid_time(robot)
        previous_time = float(bid_time_by_cell.get(cell, self.NO_TIME))
        bid_time_by_cell[cell] = timestamp
        self._queue_acbba_delta(robot, cell, robot.rid, local_bid, timestamp, include_bundle_metadata=True)
        return abs(previous_time - timestamp) > self.EPS_TIME

    def update_time_and_rebroadcast(self, robot: Any, cell: Cell) -> bool:
        return self._action_update_time_and_rebroadcast(robot, cell)

    # Aliases for receiver wiring.
    def handle_cbaa_message(self, robot: Any, message: Any) -> None:
        self.handle_acbba_message(robot, message)

    def receive_message(self, robot: Any, message: Any) -> None:
        self.handle_acbba_message(robot, message)

    def on_message(self, robot: Any, message: Any) -> None:
        self.handle_acbba_message(robot, message)

    def process_message(self, robot: Any, message: Any) -> None:
        self.handle_acbba_message(robot, message)

    def _parse_acbba_entry(
        self,
        message: Dict[str, Any],
    ) -> Optional[Tuple[Cell, Any, float, float, Optional[set[Cell]]]]:
        try:
            if "cell" in message:
                x, y = message["cell"]
            else:
                x, y = message["x"], message["y"]

            winner = self._normalize_winner(message.get("winner", self.NO_WINNER))
            bid = float(message.get("bid", self.NO_BID))
            timestamp = float(message.get("timestamp", message.get("bid_time", self.NO_TIME)))

            bundle_cells_raw = message.get("bundle_cells", None)
            bundle_cells: Optional[set[Cell]] = None
            if isinstance(bundle_cells_raw, list):
                parsed_cells: set[Cell] = set()
                for item in bundle_cells_raw:
                    if isinstance(item, dict):
                        parsed_cells.add((int(item["x"]), int(item["y"])))
                    else:
                        bx, by = item
                        parsed_cells.add((int(bx), int(by)))
                bundle_cells = parsed_cells

            return (int(x), int(y)), winner, bid, timestamp, bundle_cells
        except Exception:
            return None

    def _clear_sender_claims_not_in_bundle(self, robot: Any, sender: Any, bundle_cells: set[Cell]) -> bool:
        winner_by_cell, _ = self._consensus_maps(robot)
        changed = False

        for cell, winner in list(winner_by_cell.items()):
            if cell not in bundle_cells and self._same_robot_id(winner, sender):
                changed = self._set_table_entry(
                    robot,
                    cell,
                    self.NO_WINNER,
                    self.NO_BID,
                    self.NO_TIME,
                    queue=True,
                ) or changed
        return changed

    # ------------------------------------------------------------------
    # ACBBA local state helpers
    # ------------------------------------------------------------------

    def _ensure_cbaa_state(self, robot: Any) -> None:
        """Compatibility shim for inherited helper methods."""
        self._ensure_acbba_state(robot)

    def _ensure_acbba_state(self, robot: Any) -> None:
        if not hasattr(robot, "acbba_winner_by_cell") or getattr(robot, "acbba_winner_by_cell") is None:
            setattr(robot, "acbba_winner_by_cell", {})

        if not hasattr(robot, "acbba_winning_bid_by_cell") or getattr(robot, "acbba_winning_bid_by_cell") is None:
            setattr(robot, "acbba_winning_bid_by_cell", {})

        if not hasattr(robot, "acbba_bid_time_by_cell") or getattr(robot, "acbba_bid_time_by_cell") is None:
            setattr(robot, "acbba_bid_time_by_cell", {})

        if not hasattr(robot, "acbba_bundle") or getattr(robot, "acbba_bundle") is None:
            setattr(robot, "acbba_bundle", [])

        if not hasattr(robot, "acbba_path") or getattr(robot, "acbba_path") is None:
            setattr(robot, "acbba_path", [])


        if not hasattr(robot, "acbba_bid_counter"):
            setattr(robot, "acbba_bid_counter", 0)

        if not hasattr(robot, "acbba_pending_snapshot"):
            setattr(robot, "acbba_pending_snapshot", False)

        if not hasattr(robot, "acbba_last_sent_signature"):
            setattr(robot, "acbba_last_sent_signature", None)

        if not hasattr(robot, "acbba_pending_deltas") or getattr(robot, "acbba_pending_deltas") is None:
            setattr(robot, "acbba_pending_deltas", {})

        if not hasattr(robot, "acbba_last_sent_signatures") or getattr(robot, "acbba_last_sent_signatures") is None:
            setattr(robot, "acbba_last_sent_signatures", {})

        if not hasattr(robot, "acbba_last_collision_active"):
            setattr(robot, "acbba_last_collision_active", False)

        if not hasattr(robot, "acbba_last_reallocation_trigger"):
            setattr(robot, "acbba_last_reallocation_trigger", None)

    def _reset_cbaa_state(self, robot: Any) -> None:
        self._reset_acbba_state(robot)

    def _reset_acbba_state(self, robot: Any, preserve_deltas: bool = False) -> None:
        pending_deltas = dict(getattr(robot, "acbba_pending_deltas", {}) or {}) if preserve_deltas else {}
        last_sent_signatures = (
            dict(getattr(robot, "acbba_last_sent_signatures", {}) or {}) if preserve_deltas else {}
        )
        setattr(robot, "acbba_winner_by_cell", {})
        setattr(robot, "acbba_winning_bid_by_cell", {})
        setattr(robot, "acbba_bid_time_by_cell", {})
        setattr(robot, "acbba_bundle", [])
        setattr(robot, "acbba_path", [])
        setattr(robot, "acbba_bid_counter", 0)
        setattr(robot, "acbba_pending_snapshot", False)
        setattr(robot, "acbba_last_sent_signature", None)
        setattr(robot, "acbba_pending_deltas", pending_deltas)
        setattr(robot, "acbba_last_sent_signatures", last_sent_signatures)
        setattr(robot, "acbba_last_reallocation_trigger", None)


    def _consensus_maps(self, robot: Any) -> Tuple[Dict[Cell, Any], Dict[Cell, float]]:
        self._ensure_acbba_state(robot)
        return getattr(robot, "acbba_winner_by_cell"), getattr(robot, "acbba_winning_bid_by_cell")

    def _bid_time_map(self, robot: Any) -> Dict[Cell, float]:
        self._ensure_acbba_state(robot)
        return getattr(robot, "acbba_bid_time_by_cell")

    def _set_table_entry(
        self,
        robot: Any,
        cell: Cell,
        winner: Any,
        bid: float,
        timestamp: float,
        queue: bool = False,
        include_bundle_metadata: bool = False,
    ) -> bool:
        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)

        normalized_winner = self._normalize_winner(winner)
        normalized_bid = self.NO_BID if normalized_winner is self.NO_WINNER else float(bid)
        normalized_time = self.NO_TIME if normalized_winner is self.NO_WINNER else float(timestamp)

        previous_winner = winner_by_cell.get(cell, self.NO_WINNER)
        previous_bid = float(winning_bid_by_cell.get(cell, self.NO_BID))
        previous_time = float(bid_time_by_cell.get(cell, self.NO_TIME))
        changed = not (
            self._same_robot_id(previous_winner, normalized_winner)
            and abs(previous_bid - normalized_bid) <= self.EPS
            and abs(previous_time - normalized_time) <= self.EPS_TIME
        )

        winner_by_cell[cell] = normalized_winner
        winning_bid_by_cell[cell] = normalized_bid
        bid_time_by_cell[cell] = normalized_time

        if queue and changed:
            self._queue_acbba_delta(
                robot,
                cell,
                normalized_winner,
                normalized_bid,
                normalized_time,
                include_bundle_metadata=include_bundle_metadata,
            )

        return changed

    def _queue_current_bundle_entries(self, robot: Any) -> None:
        path = self._get_path(robot)
        if not path:
            return

        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)

        for cell in path:
            if not self._same_robot_id(winner_by_cell.get(cell), robot.rid):
                continue
            self._queue_acbba_delta(
                robot,
                cell,
                robot.rid,
                float(winning_bid_by_cell.get(cell, self.NO_BID)),
                float(bid_time_by_cell.get(cell, self.NO_TIME)),
                include_bundle_metadata=True,
            )

    def _queue_local_consensus_delta(self, robot: Any, cell: Cell) -> None:
        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)
        local_winner = winner_by_cell.get(cell, self.NO_WINNER)
        include_bundle = self._same_robot_id(local_winner, robot.rid) and cell in self._get_path(robot)
        self._queue_acbba_delta(
            robot,
            cell,
            local_winner,
            float(winning_bid_by_cell.get(cell, self.NO_BID)),
            float(bid_time_by_cell.get(cell, self.NO_TIME)),
            include_bundle_metadata=include_bundle,
        )

    def _queue_acbba_delta(
        self,
        robot: Any,
        cell: Cell,
        winner: Any,
        bid: float,
        timestamp: float,
        include_bundle_metadata: bool = False,
    ) -> None:
        self._ensure_acbba_state(robot)
        normalized_winner = self._normalize_winner(winner)
        normalized_bid = self.NO_BID if normalized_winner is self.NO_WINNER else float(bid)
        normalized_time = self.NO_TIME if normalized_winner is self.NO_WINNER else float(timestamp)

        payload: Dict[str, Any] = {
            "type": "acbba_entry",
            "sender": robot.rid,
            "x": cell[0],
            "y": cell[1],
            "winner": normalized_winner,
            "bid": normalized_bid,
            "timestamp": normalized_time,
        }

        if include_bundle_metadata and self._same_robot_id(normalized_winner, robot.rid):
            path = self._get_path(robot)
            if cell in path:
                bundle_cells = [{"x": bundle_cell[0], "y": bundle_cell[1]} for bundle_cell in path]
                payload["order"] = path.index(cell)
                payload["bundle_cells"] = bundle_cells
                payload["bundle_size"] = len(path)

        pending = getattr(robot, "acbba_pending_deltas", {}) or {}
        last_sent = getattr(robot, "acbba_last_sent_signatures", {}) or {}
        signature = self._message_signature(payload)
        if self._same_signature(last_sent.get(cell), signature):
            pending.pop(cell, None)
        else:
            pending[cell] = payload
        setattr(robot, "acbba_pending_deltas", pending)

    def _message_signature(self, payload: Dict[str, Any]) -> Tuple[Cell, Any, float, float]:
        cell = (int(payload["x"]), int(payload["y"]))
        winner = self._normalize_winner(payload.get("winner", self.NO_WINNER))
        bid = self.NO_BID if winner is self.NO_WINNER else float(payload.get("bid", self.NO_BID))
        timestamp = self.NO_TIME if winner is self.NO_WINNER else float(payload.get("timestamp", self.NO_TIME))
        return cell, self._signature_winner(winner), bid, timestamp

    def _same_signature(self, left: Any, right: Any) -> bool:
        if not isinstance(left, tuple) or len(left) != 4:
            return False
        if not isinstance(right, tuple) or len(right) != 4:
            return False
        return (
            left[0] == right[0]
            and left[1] == right[1]
            and abs(float(left[2]) - float(right[2])) <= self.EPS
            and abs(float(left[3]) - float(right[3])) <= self.EPS_TIME
        )

    def _signature_winner(self, winner: Any) -> Any:
        normalized = self._normalize_winner(winner)
        if normalized is self.NO_WINNER:
            return self.NO_WINNER
        return str(normalized)

    def _get_bundle(self, robot: Any) -> List[Cell]:
        self._ensure_acbba_state(robot)
        return self._normalize_cell_list(getattr(robot, "acbba_bundle", []))

    def _get_path(self, robot: Any) -> List[Cell]:
        self._ensure_acbba_state(robot)
        return self._normalize_cell_list(getattr(robot, "acbba_path", []))

    def _release_own_bundle_for_replan(self, robot: Any) -> None:
        """Release the stored local bundle so normal ACBBA bidding can rebuild it."""

        self._ensure_acbba_state(robot)
        path = self._get_path(robot)
        if not path:
            return

        self._truncate_bundle_from(robot, 0)

    def _collision_activation_trigger(self, robot: Any) -> bool:
        """Return True only on the rising edge of a collision-avoidance flag."""

        active = self._collision_active(robot)
        previous = bool(getattr(robot, "acbba_last_collision_active", False))
        setattr(robot, "acbba_last_collision_active", active)
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
                cells.append((int(x), int(y)))
            except Exception:
                continue

        return cells

    def _count_known_claims(self, robot: Any) -> int:
        self._ensure_acbba_state(robot)
        table = getattr(robot, "acbba_winner_by_cell", {}) or {}
        return sum(1 for winner in table.values() if winner is not self.NO_WINNER)

    def _clear_invalid_or_completed_cells(self, robot: Any) -> None:
        self._ensure_acbba_state(robot)

        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)

        cells = set(winner_by_cell.keys()) | set(winning_bid_by_cell.keys()) | set(bid_time_by_cell.keys())

        for cell in cells:
            if not self._valid_task_cell(robot, cell):
                self._set_table_entry(robot, cell, self.NO_WINNER, self.NO_BID, self.NO_TIME, queue=True)

        # Then enforce suffix release if any invalid cell was in this robot's path.
        self._repair_bundle_after_consensus(robot)

    def _sync_current_goal_after_message(self, robot: Any) -> None:
        previous_goal = self._current_goal(robot)
        path = self._get_path(robot)
        if previous_goal is not None and not path and hasattr(robot, "current_goal"):
            setattr(robot, "current_goal", None)

    def _next_bid_time(self, robot: Any) -> float:
        self._ensure_acbba_state(robot)
        counter = int(getattr(robot, "acbba_bid_counter", 0)) + 1
        setattr(robot, "acbba_bid_counter", counter)
        return float(counter)

    def _bundle_signature(self, robot: Any) -> Tuple[Tuple[Cell, float, float], ...]:
        path = self._get_path(robot)
        _, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)

        signature: List[Tuple[Cell, float, float]] = []
        for cell in path:
            signature.append((
                cell,
                float(winning_bid_by_cell.get(cell, self.NO_BID)),
                float(bid_time_by_cell.get(cell, self.NO_TIME)),
            ))

        return tuple(signature)

    def _robot_pos(self, robot: Any) -> Cell:
        x, y = getattr(robot, "pos")
        return int(x), int(y)

    def _can_claim(self, robot: Any, cell: Cell, my_bid: float) -> bool:
        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        current_winner = winner_by_cell.get(cell, self.NO_WINNER)
        current_bid = winning_bid_by_cell.get(cell, self.NO_BID)

        if current_winner is self.NO_WINNER:
            return True

        if self._same_robot_id(current_winner, robot.rid):
            return True

        if my_bid > current_bid + self.EPS:
            return True

        if abs(my_bid - current_bid) <= self.EPS:
            return self._robot_id_less(robot.rid, current_winner)

        return False

    def _bid_gt(self, left: float, right: float) -> bool:
        return float(left) > float(right) + self.EPS

    def _bid_lt(self, left: float, right: float) -> bool:
        return float(left) < float(right) - self.EPS

    def _bid_eq(self, left: float, right: float) -> bool:
        return abs(float(left) - float(right)) <= self.EPS

    def _time_gt(self, left: float, right: float) -> bool:
        return float(left) > float(right) + self.EPS_TIME

    def _time_lt(self, left: float, right: float) -> bool:
        return float(left) < float(right) - self.EPS_TIME

    def _time_eq(self, left: float, right: float) -> bool:
        return abs(float(left) - float(right)) <= self.EPS_TIME

    def _time_gte(self, left: float, right: float) -> bool:
        return float(left) >= float(right) - self.EPS_TIME

    def _time_lte(self, left: float, right: float) -> bool:
        return float(left) <= float(right) + self.EPS_TIME

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

    def _normalize_winner(self, winner: Any) -> Any:
        if winner in (None, "", "None", "none", "null", -1, "-1"):
            return self.NO_WINNER
        return winner

    def _same_robot_id(self, a: Any, b: Any) -> bool:
        if a is self.NO_WINNER or b is self.NO_WINNER:
            return a is self.NO_WINNER and b is self.NO_WINNER
        return self._robot_id_key(a) == self._robot_id_key(b)

    def _robot_id_less(self, a: Any, b: Any) -> bool:
        return self._robot_id_key(a) < self._robot_id_key(b)

    def _robot_id_key(self, rid: Any) -> Tuple[int, Any]:
        text = str(rid)
        try:
            return 0, int(text)
        except ValueError:
            return 1, text


Allocator = ACBBAAllocator
