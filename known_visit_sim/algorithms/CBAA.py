from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from known_visit_sim.algorithms.base import AllocatorBase, timed_candidate_filter
from known_visit_sim.core.types import AllocationDecision

Cell = Tuple[int, int]


class CBAAAllocator(AllocatorBase):
    """Known-target allocator behavior and communication hooks."""

    name = "CBAA"

    # Same reward scaling name/value as Auction_greedy.py.
    REWARD_FACTOR = 5.0

    NO_WINNER = None
    NO_BID = -1.0e18
    EPS = 1.0e-9

    def choose_goal(self, robot: Any) -> AllocationDecision:
        """Known-target allocator behavior and communication hooks."""

        goal = self.pick_goal(robot)
        mode = "known_visit"

        return AllocationDecision(
            goal=goal,
            debug={
                "alg": self.name,
                "mode": mode,
                "cbaa_current_task": self._get_current_task(robot),
                "cbaa_claims_known": self._count_known_claims(robot),
                "cbaa_candidate_count_before_filter": int(getattr(robot, "candidate_count_before_filter", 0)),
                "cbaa_candidate_count_after_filter": int(getattr(robot, "candidate_count_after_filter", 0)),
                "cbaa_max_candidate_cells": getattr(robot, "max_candidate_cells", None),
            },
        )

    # ------------------------------------------------------------------
    # Known-target CBAA allocation
    # ------------------------------------------------------------------

    def pick_goal(self, robot: Any) -> Optional[Cell]:
        """Known-target allocator behavior and communication hooks."""

        self._ensure_cbaa_state(robot)
        self._clear_invalid_or_completed_cells(robot)

        current = self._resolve_current_task(robot)
        if current is not None:
            # Single-assignment CBAA: keep the won cell until completed,
            # invalidated, completed, or outbid.
            return current

        best_cell: Optional[Cell] = None
        best_bid = self.NO_BID

        candidates = self._candidate_cells(robot)

        for cell in candidates:
            my_bid = self._bid(robot, cell)

            if not self._can_claim(robot, cell, my_bid):
                continue

            if self._better_new_choice(robot, cell, my_bid, best_cell, best_bid):
                best_cell = cell
                best_bid = my_bid

        if best_cell is None:
            return None

        self._claim_cell(robot, best_cell, best_bid)
        return best_cell

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

    def _bid(self, robot: Any, cell: Cell) -> float:
        """Return this robot's own CBAA bid for a task cell."""

        distance = self.manhattan(cell[0], cell[1], robot.pos[0], robot.pos[1])
        return self._probability_adjusted_score(robot, distance, cell)

    def _can_claim(self, robot: Any, cell: Cell, my_bid: float) -> bool:
        """
        Return True if this robot can claim cell under CBAA consensus state.

        Higher bid wins. Equal bids are resolved deterministically by robot id;
        lower id wins to match the convention used in Auction_greedy.py.
        """

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

    def _better_new_choice(
        self,
        robot: Any,
        cell: Cell,
        bid: float,
        best_cell: Optional[Cell],
        best_bid: float,
    ) -> bool:
        """Tie-safe comparison for selecting this robot's own best claim."""

        if best_cell is None:
            return True

        if bid > best_bid + self.EPS:
            return True

        if abs(bid - best_bid) <= self.EPS:
            # Stable deterministic cell tie-break.
            return cell < best_cell

        return False

    def _claim_cell(self, robot: Any, cell: Cell, bid: float) -> None:
        """Claim a single task and queue deltas for changed table entries."""

        self._ensure_cbaa_state(robot)
        winner_by_cell, _ = self._consensus_maps(robot)

        old_task = self._get_current_task(robot)
        if old_task is not None and old_task != cell:
            if self._same_robot_id(winner_by_cell.get(old_task), robot.rid):
                self._set_table_entry(robot, old_task, self.NO_WINNER, self.NO_BID)

        self._set_table_entry(robot, cell, robot.rid, float(bid))
        setattr(robot, "cbaa_current_task", cell)

    def _resolve_current_task(self, robot: Any) -> Optional[Cell]:
        """
        Keep the current task only if this robot still locally wins it.
        Otherwise clear it and allow a new CBAA claim.
        """

        current = self._get_current_task(robot)
        if current is None:
            return None

        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)

        if not self._valid_task_cell(robot, current):
            if self._same_robot_id(winner_by_cell.get(current), robot.rid):
                self._set_table_entry(robot, current, self.NO_WINNER, self.NO_BID)
            setattr(robot, "cbaa_current_task", None)
            return None

        if not self._same_robot_id(winner_by_cell.get(current), robot.rid):
            setattr(robot, "cbaa_current_task", None)
            return None

        return current

    def _sync_current_goal_after_message(self, robot: Any) -> None:
        """Clear simulator task cell if consensus says this robot no longer owns it."""

        previous_goal = self._current_goal(robot)
        current_task = self._resolve_current_task(robot)
        if previous_goal is not None and current_task is None and hasattr(robot, "current_goal"):
            setattr(robot, "current_goal", None)

    # ------------------------------------------------------------------
    # CBAA communication hooks
    # ------------------------------------------------------------------

    def build_cbaa_messages(self, robot: Any) -> List[dict]:
        """Known-target allocator behavior and communication hooks."""

        self._ensure_cbaa_state(robot)
        self._refresh_current_claim(robot)

        pending = getattr(robot, "cbaa_pending_deltas", {}) or {}
        last_sent = getattr(robot, "cbaa_last_sent_signatures", {}) or {}
        messages: List[dict] = []

        for cell in sorted(pending):
            payload = dict(pending[cell])
            signature = self._message_signature(payload)
            if self._same_signature(last_sent.get(cell), signature):
                continue
            messages.append(payload)
            last_sent[cell] = signature

        setattr(robot, "cbaa_pending_deltas", {})
        setattr(robot, "cbaa_last_sent_signatures", last_sent)
        return messages

    # Friendly aliases for different simulator naming conventions.
    def make_messages(self, robot: Any) -> List[dict]:
        return self.build_cbaa_messages(robot)

    def get_outbound_messages(self, robot: Any) -> List[dict]:
        return self.build_cbaa_messages(robot)

    def build_cbaa_message(self, robot: Any) -> List[dict]:
        return self.build_cbaa_messages(robot)

    def make_message(self, robot: Any) -> List[dict]:
        return self.build_cbaa_messages(robot)

    def make_cbaa_message(self, robot: Any) -> List[dict]:
        return self.build_cbaa_messages(robot)

    def get_outbound_message(self, robot: Any) -> List[dict]:
        return self.build_cbaa_messages(robot)

    def handle_cbaa_message(self, robot: Any, message: Any) -> None:
        """
        Merge a received CBAA delta-known-table entry into this robot's local
        table and queue a forwarded delta if the local table changes.

        Rules:
        - Higher bid wins.
        - Equal bid uses deterministic lower-robot-id tie-break.
        - Clear/release messages only clear matching stale ownership and do not
          erase a different stronger claim.
        - A message may be forwarded by a robot that is not the reported winner.
          The sender identifies the transmitter; the winner identifies the
          current winning robot for the cell.
        """

        if not isinstance(message, dict):
            return

        if message.get("type") != "cbaa_entry":
            return

        sender = message.get("sender")
        if sender is None or self._same_robot_id(sender, robot.rid):
            return

        self._ensure_cbaa_state(robot)
        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)

        parsed = self._parse_entry_message(message)
        if parsed is None:
            return
        cell, received_winner, received_bid = parsed

        if not self._in_bounds(robot, cell):
            return

        if not self._valid_task_cell(robot, cell):
            # Local knowledge says this task is already done/invalid.
            self._set_table_entry(robot, cell, self.NO_WINNER, self.NO_BID)
            return

        local_winner = winner_by_cell.get(cell, self.NO_WINNER)
        local_bid = winning_bid_by_cell.get(cell, self.NO_BID)

        if received_winner is self.NO_WINNER:
            released_winner = self._normalize_winner(message.get("released_winner", sender))
            released_bid = self._parse_optional_bid(message.get("released_bid", self.NO_BID))
            if self._release_matches(local_winner, local_bid, released_winner, released_bid):
                self._set_table_entry(robot, cell, self.NO_WINNER, self.NO_BID)
            self._sync_current_goal_after_message(robot)
            return

        self._clear_winner_old_claim(robot, received_winner, except_cell=cell)

        # Accept a revised self-claim from a winner already believed to own this
        # cell. This keeps a peer's changed bid from being ignored just because
        # the new bid is lower than the stale local value.
        if self._same_robot_id(local_winner, received_winner):
            self._set_table_entry(robot, cell, received_winner, float(received_bid))
            self._sync_current_goal_after_message(robot)
            return

        if received_bid > local_bid + self.EPS:
            self._set_table_entry(robot, cell, received_winner, float(received_bid))
            self._sync_current_goal_after_message(robot)
            return

        if abs(received_bid - local_bid) <= self.EPS:
            if local_winner is self.NO_WINNER or self._robot_id_less(received_winner, local_winner):
                self._set_table_entry(robot, cell, received_winner, float(received_bid))

        self._sync_current_goal_after_message(robot)

    # Friendly aliases for different simulator naming conventions.
    def receive_message(self, robot: Any, message: Any) -> None:
        self.handle_cbaa_message(robot, message)

    def on_message(self, robot: Any, message: Any) -> None:
        self.handle_cbaa_message(robot, message)

    def process_message(self, robot: Any, message: Any) -> None:
        self.handle_cbaa_message(robot, message)

    def _parse_entry_message(self, message: Dict[str, Any]) -> Optional[Tuple[Cell, Any, float]]:
        """Normalize one CBAA entry message."""

        try:
            if "cell" in message:
                x, y = message["cell"]
            else:
                x, y = message["x"], message["y"]
            winner = message.get("winner", self.NO_WINNER)
            bid = message.get("bid", self.NO_BID)
            return (int(x), int(y)), self._normalize_winner(winner), float(bid)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # CBAA local state helpers
    # ------------------------------------------------------------------

    def _ensure_cbaa_state(self, robot: Any) -> None:
        """Create CBAA tables on the robot object if missing."""

        if not hasattr(robot, "cbaa_winner_by_cell") or getattr(robot, "cbaa_winner_by_cell") is None:
            setattr(robot, "cbaa_winner_by_cell", {})

        if not hasattr(robot, "cbaa_winning_bid_by_cell") or getattr(robot, "cbaa_winning_bid_by_cell") is None:
            setattr(robot, "cbaa_winning_bid_by_cell", {})

        if not hasattr(robot, "cbaa_current_task"):
            setattr(robot, "cbaa_current_task", None)


        if not hasattr(robot, "cbaa_pending_deltas") or getattr(robot, "cbaa_pending_deltas") is None:
            setattr(robot, "cbaa_pending_deltas", {})

        if not hasattr(robot, "cbaa_last_sent_signatures") or getattr(robot, "cbaa_last_sent_signatures") is None:
            setattr(robot, "cbaa_last_sent_signatures", {})

    def _reset_cbaa_state(self, robot: Any) -> None:
        """Known-target allocator behavior and communication hooks."""

        setattr(robot, "cbaa_winner_by_cell", {})
        setattr(robot, "cbaa_winning_bid_by_cell", {})
        setattr(robot, "cbaa_current_task", None)
        setattr(robot, "cbaa_pending_deltas", {})
        setattr(robot, "cbaa_last_sent_signatures", {})


    def _consensus_maps(self, robot: Any) -> Tuple[Dict[Cell, Any], Dict[Cell, float]]:
        self._ensure_cbaa_state(robot)
        return getattr(robot, "cbaa_winner_by_cell"), getattr(robot, "cbaa_winning_bid_by_cell")

    def _set_table_entry(self, robot: Any, cell: Cell, winner: Any, bid: float) -> bool:
        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        normalized_winner = self._normalize_winner(winner)
        normalized_bid = self.NO_BID if normalized_winner is self.NO_WINNER else float(bid)

        previous_winner = winner_by_cell.get(cell, self.NO_WINNER)
        previous_bid = winning_bid_by_cell.get(cell, self.NO_BID)
        if self._same_robot_id(previous_winner, normalized_winner) and abs(float(previous_bid) - normalized_bid) <= self.EPS:
            return False

        winner_by_cell[cell] = normalized_winner
        winning_bid_by_cell[cell] = normalized_bid
        released_winner = previous_winner if normalized_winner is self.NO_WINNER else self.NO_WINNER
        released_bid = previous_bid if normalized_winner is self.NO_WINNER else self.NO_BID
        self._queue_table_delta(robot, cell, normalized_winner, normalized_bid, released_winner, released_bid)
        return True

    def _queue_table_delta(
        self,
        robot: Any,
        cell: Cell,
        winner: Any,
        bid: float,
        released_winner: Any = NO_WINNER,
        released_bid: float = NO_BID,
    ) -> None:
        self._ensure_cbaa_state(robot)
        normalized_winner = self._normalize_winner(winner)
        normalized_bid = self.NO_BID if normalized_winner is self.NO_WINNER else float(bid)
        payload = {
            "type": "cbaa_entry",
            "sender": robot.rid,
            "x": cell[0],
            "y": cell[1],
            "winner": normalized_winner,
            "bid": normalized_bid,
        }
        if normalized_winner is self.NO_WINNER:
            payload["released_winner"] = self._normalize_winner(released_winner)
            payload["released_bid"] = float(released_bid)

        signature = self._message_signature(payload)
        last_sent = getattr(robot, "cbaa_last_sent_signatures", {}) or {}
        pending = getattr(robot, "cbaa_pending_deltas", {}) or {}
        if self._same_signature(last_sent.get(cell), signature):
            pending.pop(cell, None)
        else:
            pending[cell] = payload
        setattr(robot, "cbaa_pending_deltas", pending)

    def _clear_winner_old_claim(self, robot: Any, winner_to_clear: Any, except_cell: Cell) -> None:
        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        for cell, winner in list(winner_by_cell.items()):
            if cell != except_cell and self._same_robot_id(winner, winner_to_clear):
                self._set_table_entry(robot, cell, self.NO_WINNER, self.NO_BID)

    def _refresh_current_claim(self, robot: Any) -> None:
        current = self._resolve_current_task(robot)
        if current is None:
            return

        bid = self._bid(robot, current)
        self._set_table_entry(robot, current, robot.rid, float(bid))

    def _message_signature(self, payload: Dict[str, Any]) -> Tuple[Cell, Any, float]:
        cell = (int(payload["x"]), int(payload["y"]))
        winner = self._normalize_winner(payload.get("winner", self.NO_WINNER))
        bid = self.NO_BID if winner is self.NO_WINNER else float(payload.get("bid", self.NO_BID))
        return cell, self._signature_winner(winner), bid

    def _same_signature(self, left: Any, right: Any) -> bool:
        if not isinstance(left, tuple) or len(left) != 3:
            return False
        if not isinstance(right, tuple) or len(right) != 3:
            return False
        return left[0] == right[0] and left[1] == right[1] and abs(float(left[2]) - float(right[2])) <= self.EPS

    def _signature_winner(self, winner: Any) -> Any:
        normalized = self._normalize_winner(winner)
        if normalized is self.NO_WINNER:
            return self.NO_WINNER
        return str(normalized)

    def _parse_optional_bid(self, value: Any) -> float:
        try:
            return float(value)
        except Exception:
            return self.NO_BID

    def _release_matches(self, local_winner: Any, local_bid: float, released_winner: Any, released_bid: float) -> bool:
        if local_winner is self.NO_WINNER:
            return False
        if not self._same_robot_id(local_winner, released_winner):
            return False
        if released_bid == self.NO_BID:
            return True
        return float(local_bid) <= float(released_bid) + self.EPS

    def _get_current_task(self, robot: Any) -> Optional[Cell]:
        task = getattr(robot, "cbaa_current_task", None)
        if task is None:
            return None
        try:
            x, y = task
            return int(x), int(y)
        except Exception:
            return None

    def _count_known_claims(self, robot: Any) -> int:
        if not hasattr(robot, "cbaa_winner_by_cell"):
            return 0
        table = getattr(robot, "cbaa_winner_by_cell", {}) or {}
        return sum(1 for winner in table.values() if winner is not self.NO_WINNER)

    def _clear_invalid_or_completed_cells(self, robot: Any) -> None:
        """Remove locally completed or invalid cells from the local CBAA table."""

        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        cells = set(winner_by_cell.keys()) | set(winning_bid_by_cell.keys())

        for cell in cells:
            if not self._valid_task_cell(robot, cell):
                self._set_table_entry(robot, cell, self.NO_WINNER, self.NO_BID)

        current = self._get_current_task(robot)
        if current is not None and not self._valid_task_cell(robot, current):
            setattr(robot, "cbaa_current_task", None)

    def _valid_task_cell(self, robot: Any, cell: Cell) -> bool:
        """Return True if a cell is a currently auctionable search task."""

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

    # ------------------------------------------------------------------
    # Generic simulator helpers
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Generic helpers copied/adapted from Auction_greedy.py
    # ------------------------------------------------------------------

    @staticmethod
    def manhattan(x1: int, y1: int, x2: int, y2: int) -> int:
        """Calculate Manhattan distance between two grid cells."""

        return abs(x1 - x2) + abs(y1 - y2)



    def _target_probability(self, robot: Any, cell: Cell) -> float:
        """
        Read this robot's local target probability for a cell.

        Supports both dictionary-style and array/list-style target_p maps.
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

        This uses local robot knowledge, not global world truth.
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

    # ------------------------------------------------------------------
    # Robot id normalization / tie-breaking
    # ------------------------------------------------------------------

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


# Optional aliases make the file easier to load if the runner expects a generic
# class name or the old copied class name during early integration.
Allocator = CBAAAllocator
