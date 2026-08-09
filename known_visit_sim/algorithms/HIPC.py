from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from known_visit_sim.algorithms.base import AllocatorBase, timed_candidate_filter
from known_visit_sim.core.types import AllocationDecision, Cell


class HIPCAllocator(AllocatorBase):
    """Known-target allocator behavior and communication hooks."""

    name = "HIPC"

    # Keep bundle depth matched to the other bundle allocators for fair comparison.
    BUNDLE_SIZE = 3

    def recover_stalled_allocation(self, robot: Any) -> bool:
        """Locally expire peer assignments after the shared stall timeout."""
        self._ensure_hipc_state(robot)
        counts = getattr(robot, "hipc_bad_prediction_count", {}) or {}
        for peer_id in self._safe_peer_positions(robot):
            peer_key = self._rid_key(peer_id)
            if peer_key != self._rid_key(robot.rid):
                counts[peer_key] = max(int(counts.get(peer_key, 0)), self.BAD_PRED_LIMIT)
        setattr(robot, "hipc_bad_prediction_count", counts)
        self._reset_path_state(robot)
        return True

    # Same reward scaling as CBAA/AuctionGreedy.
    REWARD_FACTOR = 5.0
    NO_WINNER = None
    NO_BID = -1.0e18
    EPS = 1.0e-9

    NO_TIME = -1.0e18

    # Prediction-pruning parameters.
    BAD_PRED_LIMIT = 3

    # A first-task-cell mismatch this small is considered "close enough" for grid robots.
    # Use 0 for strict exact-cell prediction, 1 for neighboring-cell tolerance.
    PREDICTION_TOLERANCE_CELLS = 0

    # ------------------------------------------------------------------
    # Main allocator entry point
    # ------------------------------------------------------------------

    def choose_goal(self, robot: Any) -> AllocationDecision:
        goal = self.pick_goal(robot)
        mode = "known_visit"

        self._ensure_hipc_state(robot)

        return AllocationDecision(
            goal=goal,
            debug={
                "alg": self.name,
                "mode": mode,
                "hipc_path": self._get_path(robot),
                "hipc_bundle": self._get_bundle(robot),
                "hipc_trigger": getattr(robot, "hipc_last_reallocation_trigger", None),
                "hipc_team_size": int(getattr(robot, "hipc_last_team_size", 1)),
                "hipc_candidate_count": int(getattr(robot, "hipc_last_candidate_count", 0)),
                "hipc_bundle_size": self._planning_horizon(robot, self.BUNDLE_SIZE),
                "hipc_candidate_count_before_filter": int(getattr(robot, "candidate_count_before_filter", 0)),
                "hipc_candidate_count_after_filter": int(getattr(robot, "candidate_count_after_filter", 0)),
                "hipc_max_candidate_cells": getattr(robot, "max_candidate_cells", None),
                "hipc_dropped_peers": sorted(str(rid) for rid in getattr(robot, "hipc_dropped_peers", set())),
                "hipc_bad_prediction_count": dict(getattr(robot, "hipc_bad_prediction_count", {})),
                "hipc_claims_known": self._count_known_claims(robot),
                "hipc_pending_snapshot": bool(getattr(robot, "hipc_pending_snapshot", False)),
            },
        )

    # ------------------------------------------------------------------
    # Known-target HIPC allocation
    # ------------------------------------------------------------------

    def pick_goal(self, robot: Any) -> Optional[Cell]:
        self._ensure_hipc_state(robot)
        self._clear_invalid_or_completed_cells(robot)

        trigger = "collision_avoidance" if self._collision_activation_trigger(robot) else None
        setattr(robot, "hipc_last_reallocation_trigger", trigger)
        if trigger is not None:
            self._release_own_bundle_for_replan(robot)

        self._repair_bundle_after_consensus(robot)

        # HIPC builds my bundle by planning for a local
        # team neighborhood, then extracting this robot's portion of that plan.
        self._build_bundle(robot)

        path = self._get_path(robot)
        if not path:
            return None
        return path[0]

    def _build_bundle(self, robot: Any) -> None:
        """Build this robot's HIPC bundle from a local team-level TAA."""

        self._ensure_hipc_state(robot)

        candidates = self._candidate_cells(robot)
        team_agents = self._hipc_team_agents(robot)
        team_plan = self._run_local_team_taa(robot, team_agents, candidates)

        rid_key = self._rid_key(robot.rid)
        bundle_size = self._planning_horizon(robot, self.BUNDLE_SIZE)
        new_path = team_plan.get(rid_key, [])[:bundle_size]

        setattr(robot, "hipc_last_team_size", len(team_agents))
        setattr(robot, "hipc_last_candidate_count", len(candidates))
        setattr(robot, "hipc_last_predicted_team_plan", team_plan)
        setattr(robot, "hipc_last_predicted_peer_first_task", {
            str(rid): path[0]
            for rid, path in team_plan.items()
            if str(rid) != rid_key and path
        })

        self._replace_own_bundle_if_changed(robot, new_path)

    @timed_candidate_filter
    def _candidate_cells(self, robot: Any) -> List[Cell]:
        """Return every valid unsearched cell, ordered by probability and distance."""

        grid_size = self._grid_size(robot)
        origin = self._robot_pos(robot)
        cells: List[Tuple[float, int, Cell]] = []

        for y in range(grid_size):
            for x in range(grid_size):
                cell = (x, y)
                if not self._valid_task_cell(robot, cell):
                    continue

                probability = float(self._target_probability(robot, cell))
                distance = self.manhattan(origin[0], origin[1], x, y)
                cells.append((-probability, distance, cell))

        cells.sort(key=lambda item: (item[0], item[1], item[2]))
        return self._filter_candidate_cells(robot, [cell for _, _, cell in cells])

    def _hipc_team_agents(self, robot: Any) -> Dict[str, Cell]:
        """
        Return the local HIPC planning neighborhood as rid -> predicted position.

        Dropped peers are excluded from local team planning, but their received
        consensus claims are still respected by the HIPC winner/bid tables.
        """

        self._ensure_hipc_state(robot)

        team: Dict[str, Cell] = {self._rid_key(robot.rid): self._robot_pos(robot)}
        dropped: Set[str] = set()

        bad_counts = getattr(robot, "hipc_bad_prediction_count", {}) or {}
        peer_positions = self._safe_peer_positions(robot)
        peer_ids = set(str(rid) for rid in peer_positions.keys())

        for peer_id in sorted(peer_ids):
            if peer_id == self._rid_key(robot.rid):
                continue

            if int(bad_counts.get(peer_id, 0)) >= self.BAD_PRED_LIMIT:
                dropped.add(peer_id)
                continue

            reference = self._peer_reference_cell(peer_id, peer_positions)
            if reference is None:
                dropped.add(peer_id)
                continue

            team[peer_id] = reference

        setattr(robot, "hipc_dropped_peers", dropped)
        return team

    def _run_local_team_taa(
        self,
        robot: Any,
        team_agents: Dict[str, Cell],
        candidates: List[Cell],
    ) -> Dict[str, List[Cell]]:
        """
        Greedy local team-level TAA.

        At each step, select the best robot-cell pair across predictable agents.
        This is HIPC's local implicit team-planning step. Only this robot's own
        portion of the result will later be claimed/transmitted.
        """

        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        team_ids = set(team_agents.keys())

        plan: Dict[str, List[Cell]] = {rid: [] for rid in team_agents}
        endpoint: Dict[str, Cell] = dict(team_agents)
        assigned_cells: Set[Cell] = set()

        bundle_size = self._planning_horizon(robot, self.BUNDLE_SIZE)
        max_assignments = max(1, len(team_agents) * bundle_size)

        for _ in range(max_assignments):
            best_rid: Optional[str] = None
            best_cell: Optional[Cell] = None
            best_score = self.NO_BID

            for rid in sorted(team_agents.keys()):
                if len(plan[rid]) >= bundle_size:
                    continue

                reference = endpoint[rid]

                for cell in candidates:
                    if cell in assigned_cells:
                        continue

                    known_winner = winner_by_cell.get(cell, self.NO_WINNER)
                    if known_winner is not self.NO_WINNER and self._rid_key(known_winner) not in team_ids:
                        # Respect claims from peers outside the planning neighborhood.
                        continue

                    score = self._bid_from_reference(robot, cell, reference)
                    known_bid = float(winning_bid_by_cell.get(cell, self.NO_BID))

                    # If a non-self known winner has a stronger local bid, avoid planning
                    # through that task unless the predicted owner is that winner.
                    if known_winner is not self.NO_WINNER:
                        known_key = self._rid_key(known_winner)
                        if known_key != rid and score < known_bid - self.EPS:
                            continue

                    if self._better_team_choice(rid, cell, score, best_rid, best_cell, best_score):
                        best_rid = rid
                        best_cell = cell
                        best_score = score

            if best_rid is None or best_cell is None:
                break

            plan[best_rid].append(best_cell)
            endpoint[best_rid] = best_cell
            assigned_cells.add(best_cell)

        return plan

    def _better_team_choice(
        self,
        rid: str,
        cell: Cell,
        score: float,
        best_rid: Optional[str],
        best_cell: Optional[Cell],
        best_score: float,
    ) -> bool:
        if best_cell is None or best_rid is None:
            return True
        if score > best_score + self.EPS:
            return True
        if abs(score - best_score) <= self.EPS:
            return (str(rid), cell) < (str(best_rid), best_cell)
        return False

    def _replace_own_bundle_if_changed(self, robot: Any, new_path: List[Cell]) -> None:
        """Replace this robot's own claimed bundle if the HIPC TAA changed it."""

        self._ensure_hipc_state(robot)

        old_path = self._get_path(robot)
        bundle_size = self._planning_horizon(robot, self.BUNDLE_SIZE)
        normalized_new = self._normalize_cell_list(list(new_path))[:bundle_size]

        if tuple(old_path) == tuple(normalized_new):
            return

        released = set(getattr(robot, "hipc_pending_releases", set()) or set())
        released.update(cell for cell in old_path if cell not in normalized_new)
        setattr(robot, "hipc_pending_releases", released)

        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)

        # Release old local self-claims. Other agents learn releases via future
        # bundle snapshots, matching the lightweight HIPC messaging style.
        for cell in old_path:
            if self._same_robot_id(winner_by_cell.get(cell), robot.rid):
                winner_by_cell[cell] = self.NO_WINNER
                winning_bid_by_cell[cell] = self.NO_BID
                bid_time_by_cell[cell] = self.NO_TIME

        setattr(robot, "hipc_path", [])
        setattr(robot, "hipc_bundle", [])

        prefix: List[Cell] = []
        for cell in normalized_new:
            if not self._valid_task_cell(robot, cell):
                continue

            bid = self._append_bid_for_prefix(robot, prefix, cell)
            if not self._can_claim(robot, cell, bid):
                continue

            self._insert_claim(robot, cell, len(self._get_path(robot)), bid)
            prefix.append(cell)

        setattr(robot, "hipc_pending_snapshot", True)

    def _append_bid_for_prefix(self, robot: Any, prefix: List[Cell], cell: Cell) -> float:
        current_distance = self._route_distance(robot, prefix)
        candidate_distance = self._route_distance(robot, prefix + [cell])
        marginal_distance = max(0.0, candidate_distance - current_distance)
        return self._probability_adjusted_score(robot, marginal_distance, cell)

    # ------------------------------------------------------------------
    # HIPC communication hooks
    # ------------------------------------------------------------------

    def build_hipc_messages(self, robot: Any) -> List[dict]:
        """Known-target allocator behavior and communication hooks."""

        self._ensure_hipc_state(robot)

        if not bool(getattr(robot, "hipc_pending_snapshot", False)):
            return []

        path = self._get_path(robot)
        current_signature = self._bundle_signature(robot)
        previous_signature = getattr(robot, "hipc_last_sent_signature", None)
        if current_signature == previous_signature:
            setattr(robot, "hipc_pending_snapshot", False)
            return []

        if not path:
            setattr(robot, "hipc_pending_snapshot", False)
            setattr(robot, "hipc_last_sent_signature", current_signature)
            setattr(robot, "hipc_pending_releases", set())
            if previous_signature is None:
                return []
            return [{
                "type": "hipc_clear_bundle",
                "alg": "HIPC",
                "sender": robot.rid,
                "timestamp": self._next_bid_time(robot),
                "bundle_cells": [],
                "bundle_size": 0,
            }]

        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)
        bundle_cells = [{"x": cell[0], "y": cell[1]} for cell in path]

        messages: List[dict] = []
        for order, cell in enumerate(path):
            if not self._same_robot_id(winner_by_cell.get(cell), robot.rid):
                continue

            messages.append({
                "type": "hipc_entry",
                "alg": "HIPC",
                "sender": robot.rid,
                "x": cell[0],
                "y": cell[1],
                "winner": robot.rid,
                "bid": float(winning_bid_by_cell.get(cell, self.NO_BID)),
                "timestamp": float(bid_time_by_cell.get(cell, self.NO_TIME)),
                "order": order,
                "bundle_cells": list(bundle_cells),
                "bundle_size": len(path),
            })

        setattr(robot, "hipc_pending_snapshot", False)
        setattr(robot, "hipc_last_sent_signature", current_signature)
        setattr(robot, "hipc_pending_releases", set())
        return messages

    def build_acbba_messages(self, robot: Any) -> List[dict]:
        return self.build_hipc_messages(robot)

    def build_cbaa_messages(self, robot: Any) -> List[dict]:
        return self.build_hipc_messages(robot)

    def make_messages(self, robot: Any) -> List[dict]:
        return self.build_hipc_messages(robot)

    def get_outbound_messages(self, robot: Any) -> List[dict]:
        return self.build_hipc_messages(robot)

    def build_hipc_message(self, robot: Any) -> List[dict]:
        return self.build_hipc_messages(robot)

    def build_acbba_message(self, robot: Any) -> List[dict]:
        return self.build_hipc_messages(robot)

    def build_cbaa_message(self, robot: Any) -> List[dict]:
        return self.build_hipc_messages(robot)

    def make_message(self, robot: Any) -> List[dict]:
        return self.build_hipc_messages(robot)

    def make_hipc_message(self, robot: Any) -> List[dict]:
        return self.build_hipc_messages(robot)

    def make_acbba_message(self, robot: Any) -> List[dict]:
        return self.build_hipc_messages(robot)

    def make_cbaa_message(self, robot: Any) -> List[dict]:
        return self.build_hipc_messages(robot)

    def get_outbound_message(self, robot: Any) -> List[dict]:
        return self.build_hipc_messages(robot)

    def on_collision_avoidance_activated(self, robot: Any) -> bool:
        return True

    def handle_hipc_message(self, robot: Any, message: Any) -> None:
        if not isinstance(message, dict):
            return
        if message.get("type") not in {"hipc_entry", "hipc_clear_bundle"}:
            return

        self._ensure_hipc_state(robot)
        self._update_prediction_quality_from_message(robot, message)

        self._merge_hipc_entry(robot, message)

    def handle_acbba_message(self, robot: Any, message: Any) -> None:
        if isinstance(message, dict) and message.get("type") in {"hipc_entry", "hipc_clear_bundle"}:
            self.handle_hipc_message(robot, message)

    def handle_cbaa_message(self, robot: Any, message: Any) -> None:
        self.handle_acbba_message(robot, message)

    def receive_message(self, robot: Any, message: Any) -> None:
        self.handle_acbba_message(robot, message)

    def on_message(self, robot: Any, message: Any) -> None:
        self.handle_acbba_message(robot, message)

    def process_message(self, robot: Any, message: Any) -> None:
        self.handle_acbba_message(robot, message)

    def _merge_hipc_entry(self, robot: Any, message: Dict[str, Any]) -> None:
        if message.get("type") == "hipc_clear_bundle":
            bundle_cells = self._parse_bundle_cells(message.get("bundle_cells", []))
            if bundle_cells is not None:
                self._clear_sender_claims_not_in_bundle(robot, message.get("sender"), bundle_cells)
                self._repair_bundle_after_consensus(robot)
                self._sync_current_goal_after_message(robot)
            return

        parsed = self._parse_hipc_entry(message)
        if parsed is None:
            return

        cell, received_winner, received_bid, received_time, bundle_cells = parsed

        if bundle_cells is not None:
            self._clear_sender_claims_not_in_bundle(robot, message.get("sender"), bundle_cells)

        if not self._in_bounds(robot, cell):
            return

        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)

        if not self._valid_task_cell(robot, cell):
            winner_by_cell[cell] = self.NO_WINNER
            winning_bid_by_cell[cell] = self.NO_BID
            bid_time_by_cell[cell] = self.NO_TIME
            self._repair_bundle_after_consensus(robot)
            self._sync_current_goal_after_message(robot)
            return

        local_winner = winner_by_cell.get(cell, self.NO_WINNER)
        local_bid = float(winning_bid_by_cell.get(cell, self.NO_BID))
        local_time = float(bid_time_by_cell.get(cell, self.NO_TIME))

        should_update = False
        if self._same_robot_id(received_winner, message.get("sender")) and self._same_robot_id(local_winner, message.get("sender")):
            should_update = received_time >= local_time - self.EPS
        elif received_bid > local_bid + self.EPS:
            should_update = True
        elif abs(received_bid - local_bid) <= self.EPS:
            if local_winner is self.NO_WINNER or self._robot_id_less(received_winner, local_winner):
                should_update = True

        if should_update:
            winner_by_cell[cell] = received_winner
            winning_bid_by_cell[cell] = float(received_bid)
            bid_time_by_cell[cell] = float(received_time)

        self._repair_bundle_after_consensus(robot)
        self._sync_current_goal_after_message(robot)

    def _parse_hipc_entry(
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
            bundle_cells = self._parse_bundle_cells(message.get("bundle_cells", None))
            return (int(x), int(y)), winner, bid, timestamp, bundle_cells
        except Exception:
            return None

    def _parse_bundle_cells(self, raw: Any) -> Optional[set[Cell]]:
        if raw is None or not isinstance(raw, list):
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

    def _clear_sender_claims_not_in_bundle(self, robot: Any, sender: Any, bundle_cells: set[Cell]) -> None:
        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)

        for cell, winner in list(winner_by_cell.items()):
            if cell not in bundle_cells and self._same_robot_id(winner, sender):
                winner_by_cell[cell] = self.NO_WINNER
                winning_bid_by_cell[cell] = self.NO_BID
                bid_time_by_cell[cell] = self.NO_TIME

    # ------------------------------------------------------------------
    # Imperfect-SA prediction quality
    # ------------------------------------------------------------------

    def _update_prediction_quality_from_message(self, robot: Any, message: Dict[str, Any]) -> None:
        sender = message.get("sender")
        if sender is None or self._same_robot_id(sender, robot.rid):
            return

        sender_key = self._rid_key(sender)
        actual_first = self._first_bundle_cell_from_message(message)
        if actual_first is None:
            return

        signature = self._bundle_signature_from_message(message)
        seen = getattr(robot, "hipc_seen_peer_bundle_signature", {}) or {}
        if seen.get(sender_key) == signature:
            return
        seen[sender_key] = signature
        setattr(robot, "hipc_seen_peer_bundle_signature", seen)

        predicted = getattr(robot, "hipc_last_predicted_peer_first_task", {}) or {}
        predicted_first = predicted.get(sender_key)
        if predicted_first is None:
            return

        if self.manhattan(predicted_first[0], predicted_first[1], actual_first[0], actual_first[1]) <= self.PREDICTION_TOLERANCE_CELLS:
            self._record_good_prediction(robot, sender_key)
        else:
            self._record_bad_prediction(robot, sender_key)

    def _record_bad_prediction(self, robot: Any, peer_key: str) -> None:
        counts = getattr(robot, "hipc_bad_prediction_count", {}) or {}
        counts[peer_key] = int(counts.get(peer_key, 0)) + 1
        setattr(robot, "hipc_bad_prediction_count", counts)

    def _record_good_prediction(self, robot: Any, peer_key: str) -> None:
        counts = getattr(robot, "hipc_bad_prediction_count", {}) or {}
        current = int(counts.get(peer_key, 0))
        if current > 0:
            counts[peer_key] = current - 1
        else:
            counts[peer_key] = 0
        setattr(robot, "hipc_bad_prediction_count", counts)

    def _first_bundle_cell_from_message(self, message: Dict[str, Any]) -> Optional[Cell]:
        bundle_cells_raw = message.get("bundle_cells", None)
        try:
            if isinstance(bundle_cells_raw, list) and bundle_cells_raw:
                first = bundle_cells_raw[0]
                if isinstance(first, dict):
                    return int(first["x"]), int(first["y"])
                x, y = first
                return int(x), int(y)

            # Fallback: only use the message cell as the first task if order==0.
            if int(message.get("order", -1)) == 0:
                if "cell" in message:
                    x, y = message["cell"]
                else:
                    x, y = message["x"], message["y"]
                return int(x), int(y)
        except Exception:
            return None
        return None

    def _bundle_signature_from_message(self, message: Dict[str, Any]) -> Tuple[Any, ...]:
        bundle_cells_raw = message.get("bundle_cells", None)
        cells: List[Cell] = []
        if isinstance(bundle_cells_raw, list):
            for item in bundle_cells_raw:
                try:
                    if isinstance(item, dict):
                        cells.append((int(item["x"]), int(item["y"])))
                    else:
                        x, y = item
                        cells.append((int(x), int(y)))
                except Exception:
                    continue

        if cells:
            return tuple(cells)

        try:
            if "cell" in message:
                x, y = message["cell"]
            else:
                x, y = message["x"], message["y"]
            return ((int(x), int(y)), int(message.get("order", -1)))
        except Exception:
            return tuple()

    # ------------------------------------------------------------------
    # HIPC local state / helper methods
    # ------------------------------------------------------------------

    def _ensure_cbaa_state(self, robot: Any) -> None:
        """Compatibility shim for simulator helper naming conventions."""
        self._ensure_hipc_state(robot)

    def _ensure_hipc_state(self, robot: Any) -> None:
        self._ensure_path_state(robot)

        if not hasattr(robot, "hipc_bad_prediction_count") or getattr(robot, "hipc_bad_prediction_count") is None:
            setattr(robot, "hipc_bad_prediction_count", {})

        if not hasattr(robot, "hipc_last_predicted_peer_first_task") or getattr(robot, "hipc_last_predicted_peer_first_task") is None:
            setattr(robot, "hipc_last_predicted_peer_first_task", {})

        if not hasattr(robot, "hipc_last_predicted_team_plan") or getattr(robot, "hipc_last_predicted_team_plan") is None:
            setattr(robot, "hipc_last_predicted_team_plan", {})

        if not hasattr(robot, "hipc_seen_peer_bundle_signature") or getattr(robot, "hipc_seen_peer_bundle_signature") is None:
            setattr(robot, "hipc_seen_peer_bundle_signature", {})

        if not hasattr(robot, "hipc_dropped_peers") or getattr(robot, "hipc_dropped_peers") is None:
            setattr(robot, "hipc_dropped_peers", set())

        if not hasattr(robot, "hipc_last_team_size"):
            setattr(robot, "hipc_last_team_size", 1)

        if not hasattr(robot, "hipc_last_candidate_count"):
            setattr(robot, "hipc_last_candidate_count", 0)

        if not hasattr(robot, "hipc_last_collision_active"):
            setattr(robot, "hipc_last_collision_active", False)

        if not hasattr(robot, "hipc_last_reallocation_trigger"):
            setattr(robot, "hipc_last_reallocation_trigger", None)

    def _ensure_path_state(self, robot: Any) -> None:
        """Create HIPC path-consensus state."""

        if not hasattr(robot, "hipc_winner_by_cell") or getattr(robot, "hipc_winner_by_cell") is None:
            setattr(robot, "hipc_winner_by_cell", {})

        if not hasattr(robot, "hipc_winning_bid_by_cell") or getattr(robot, "hipc_winning_bid_by_cell") is None:
            setattr(robot, "hipc_winning_bid_by_cell", {})

        if not hasattr(robot, "hipc_bid_time_by_cell") or getattr(robot, "hipc_bid_time_by_cell") is None:
            setattr(robot, "hipc_bid_time_by_cell", {})

        if not hasattr(robot, "hipc_bundle") or getattr(robot, "hipc_bundle") is None:
            setattr(robot, "hipc_bundle", [])

        if not hasattr(robot, "hipc_path") or getattr(robot, "hipc_path") is None:
            setattr(robot, "hipc_path", [])


        if not hasattr(robot, "hipc_bid_counter"):
            setattr(robot, "hipc_bid_counter", 0)

        if not hasattr(robot, "hipc_pending_snapshot"):
            setattr(robot, "hipc_pending_snapshot", False)

        if not hasattr(robot, "hipc_last_sent_signature"):
            setattr(robot, "hipc_last_sent_signature", None)
        if not hasattr(robot, "hipc_pending_releases"):
            setattr(robot, "hipc_pending_releases", set())

    def _reset_cbaa_state(self, robot: Any) -> None:
        self._reset_path_state(robot)

    def _reset_path_state(self, robot: Any) -> None:
        setattr(robot, "hipc_winner_by_cell", {})
        setattr(robot, "hipc_winning_bid_by_cell", {})
        setattr(robot, "hipc_bid_time_by_cell", {})
        setattr(robot, "hipc_bundle", [])
        setattr(robot, "hipc_path", [])
        setattr(robot, "hipc_bid_counter", 0)
        setattr(robot, "hipc_pending_snapshot", False)
        setattr(robot, "hipc_last_sent_signature", None)
        # Preserve prediction-quality counts across local plan resets.
        setattr(robot, "hipc_last_predicted_peer_first_task", {})
        setattr(robot, "hipc_last_predicted_team_plan", {})
        setattr(robot, "hipc_seen_peer_bundle_signature", {})
        setattr(robot, "hipc_dropped_peers", set())
        setattr(robot, "hipc_last_team_size", 1)
        setattr(robot, "hipc_last_candidate_count", 0)
        setattr(robot, "hipc_last_reallocation_trigger", None)


    def _consensus_maps(self, robot: Any) -> Tuple[Dict[Cell, Any], Dict[Cell, float]]:
        self._ensure_hipc_state(robot)
        return getattr(robot, "hipc_winner_by_cell"), getattr(robot, "hipc_winning_bid_by_cell")

    def _bid_time_map(self, robot: Any) -> Dict[Cell, float]:
        self._ensure_hipc_state(robot)
        return getattr(robot, "hipc_bid_time_by_cell")

    def _get_bundle(self, robot: Any) -> List[Cell]:
        self._ensure_hipc_state(robot)
        return self._normalize_cell_list(getattr(robot, "hipc_bundle", []))

    def _get_path(self, robot: Any) -> List[Cell]:
        self._ensure_hipc_state(robot)
        return self._normalize_cell_list(getattr(robot, "hipc_path", []))

    def _release_own_bundle_for_replan(self, robot: Any) -> None:
        """Release the stored local bundle so normal HIPC planning can rebuild it."""

        self._ensure_hipc_state(robot)
        old_path = self._get_path(robot)
        if not old_path:
            return

        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)

        for cell in old_path:
            if self._same_robot_id(winner_by_cell.get(cell), robot.rid):
                winner_by_cell[cell] = self.NO_WINNER
                winning_bid_by_cell[cell] = self.NO_BID
                bid_time_by_cell[cell] = self.NO_TIME

        setattr(robot, "hipc_path", [])
        setattr(robot, "hipc_bundle", [])
        setattr(robot, "hipc_pending_snapshot", True)

    def _collision_activation_trigger(self, robot: Any) -> bool:
        """Return True only on the rising edge of a collision-avoidance flag."""

        active = self._collision_active(robot)
        previous = bool(getattr(robot, "hipc_last_collision_active", False))
        setattr(robot, "hipc_last_collision_active", active)
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
        self._ensure_hipc_state(robot)
        table = getattr(robot, "hipc_winner_by_cell", {}) or {}
        return sum(1 for winner in table.values() if winner is not self.NO_WINNER)

    def _clear_invalid_or_completed_cells(self, robot: Any) -> None:
        self._ensure_hipc_state(robot)
        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)

        cells = set(winner_by_cell.keys()) | set(winning_bid_by_cell.keys()) | set(bid_time_by_cell.keys())
        for cell in cells:
            if not self._valid_task_cell(robot, cell):
                winner_by_cell[cell] = self.NO_WINNER
                winning_bid_by_cell[cell] = self.NO_BID
                bid_time_by_cell[cell] = self.NO_TIME

        self._repair_bundle_after_consensus(robot)

    def _repair_bundle_after_consensus(self, robot: Any) -> None:
        self._ensure_hipc_state(robot)
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
        path = self._get_path(robot)
        bundle = self._get_bundle(robot)
        if index < 0 or index >= len(path):
            return

        suffix = path[index:]
        released = set(getattr(robot, "hipc_pending_releases", set()) or set())
        released.update(suffix)
        setattr(robot, "hipc_pending_releases", released)
        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)

        for cell in suffix:
            if self._same_robot_id(winner_by_cell.get(cell), robot.rid):
                winner_by_cell[cell] = self.NO_WINNER
                winning_bid_by_cell[cell] = self.NO_BID
                bid_time_by_cell[cell] = self.NO_TIME

        setattr(robot, "hipc_path", path[:index])
        setattr(robot, "hipc_bundle", bundle[:index])
        setattr(robot, "hipc_pending_snapshot", True)

    def _route_distance(self, robot: Any, path: List[Cell]) -> float:
        if not path:
            return 0.0

        distance = 0
        previous = self._robot_pos(robot)
        for cell in path:
            distance += self.manhattan(previous[0], previous[1], cell[0], cell[1])
            previous = cell

        return float(distance)

    def _bid_from_reference(self, robot: Any, cell: Cell, reference: Cell) -> float:
        distance = self.manhattan(cell[0], cell[1], reference[0], reference[1])
        return self._probability_adjusted_score(robot, distance, cell)

    def _insert_claim(self, robot: Any, cell: Cell, insertion_index: int, bid: float) -> None:
        self._ensure_hipc_state(robot)
        path = self._get_path(robot)
        bundle = self._get_bundle(robot)
        index = max(0, min(int(insertion_index), len(path)))

        path.insert(index, cell)
        bundle.insert(index, cell)

        winner_by_cell, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)
        winner_by_cell[cell] = robot.rid
        winning_bid_by_cell[cell] = float(bid)
        bid_time_by_cell[cell] = self._next_bid_time(robot)

        setattr(robot, "hipc_path", path)
        setattr(robot, "hipc_bundle", bundle)

    def _sync_current_goal_after_message(self, robot: Any) -> None:
        previous_goal = self._current_goal(robot)
        path = self._get_path(robot)
        if previous_goal is not None and not path and hasattr(robot, "current_goal"):
            setattr(robot, "current_goal", None)

    def _next_bid_time(self, robot: Any) -> float:
        self._ensure_hipc_state(robot)
        counter = int(getattr(robot, "hipc_bid_counter", 0)) + 1
        setattr(robot, "hipc_bid_counter", counter)
        return float(counter)

    def _bundle_signature(self, robot: Any) -> Tuple[Tuple[Cell, float, float], ...]:
        path = self._get_path(robot)
        _, winning_bid_by_cell = self._consensus_maps(robot)
        bid_time_by_cell = self._bid_time_map(robot)
        return tuple(
            (
                cell,
                float(winning_bid_by_cell.get(cell, self.NO_BID)),
                float(bid_time_by_cell.get(cell, self.NO_TIME)),
            )
            for cell in path
        )

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

    def _safe_peer_positions(self, robot: Any) -> Dict[str, Cell]:
        raw = getattr(robot, "peer_positions", {}) or {}
        return self._normalize_peer_cell_dict(raw)

    def _normalize_peer_cell_dict(self, values: Any) -> Dict[str, Cell]:
        result: Dict[str, Cell] = {}
        if not isinstance(values, dict):
            return result
        for rid, cell in values.items():
            try:
                if cell is None:
                    continue
                x, y = cell
                result[str(rid)] = (int(x), int(y))
            except Exception:
                continue
        return result

    def _peer_reference_cell(
        self,
        peer_id: str,
        peer_positions: Dict[str, Cell],
    ) -> Optional[Cell]:
        if peer_id in peer_positions:
            return peer_positions[peer_id]
        return None

    def _rid_key(self, rid: Any) -> str:
        return str(rid)


Allocator = HIPCAllocator
