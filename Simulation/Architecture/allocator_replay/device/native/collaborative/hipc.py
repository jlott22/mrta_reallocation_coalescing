"""Local implicit team planner for native collaborative visits."""

from .base import NativeAllocatorBase


class HIPCAllocator(NativeAllocatorBase):
    name = "HIPC"
    BAD_PRED_LIMIT = 3

    def __init__(self, state):
        NativeAllocatorBase.__init__(self, state)
        self.bid_counter = 0
        self.bad_prediction_count = {}
        self.dropped_peers = []
        self.last_predicted_peer_first_task = {}
        self.seen_peer_bundle_signature = {}

    def _next_bid_time(self):
        self.bid_counter += 1
        return self.bid_counter

    def on_allocation_epoch(self, reason, admitted_cells, epoch_index=None):
        reset = NativeAllocatorBase.on_allocation_epoch(
            self, reason, admitted_cells, epoch_index
        )
        if admitted_cells:
            self.bid_counter = 0
            # Desktop HIPC deliberately preserves prediction-quality counts,
            # but clears the current prediction/bundle protocol checkpoint.
            self.dropped_peers = []
            self.last_predicted_peer_first_task = {}
            self.seen_peer_bundle_signature = {}
        return reset

    def _team_indices(self):
        state = self.state
        team = []
        dropped = []
        for index, robot_id in enumerate(state.robot_ids):
            if not state.peer_position_valid[index]:
                continue
            if (
                index != state.robot_index
                and int(self.bad_prediction_count.get(str(robot_id), 0))
                >= self.BAD_PRED_LIMIT
            ):
                dropped.append(str(robot_id))
                continue
            team.append(index)
        self.dropped_peers = sorted(dropped)
        return team

    def _team_plan(self, candidates):
        state = self.state
        team = self._team_indices()
        plans = {}
        endpoints = {}
        for index in team:
            plans[index] = []
            endpoints[index] = int(state.peer_positions[index])

        assigned = set()
        maximum = max(1, len(team) * state.commitment_horizon)
        for _ in range(maximum):
            best = None
            for owner in team:
                if len(plans[owner]) >= state.commitment_horizon:
                    continue
                for slot in candidates:
                    if slot in assigned:
                        continue
                    known_owner = int(state.claim_owner[slot])
                    if known_owner >= 0 and known_owner not in team:
                        continue
                    score = self.score_from(endpoints[owner], slot)
                    known_value = float(state.claim_value[slot])
                    if (
                        known_owner >= 0
                        and known_owner != owner
                        and score < known_value - self.EPS
                    ):
                        continue
                    cell = state.decode_cell(state.targets[slot])
                    key = (
                        -score,
                        state.robot_id_key(state.robot_ids[owner]),
                        int(cell[0]),
                        int(cell[1]),
                    )
                    if best is None or key < best[0]:
                        best = (key, owner, slot, score)
            if best is None:
                break
            _, owner, slot, _ = best
            plans[owner].append(slot)
            endpoints[owner] = state.targets[slot]
            assigned.add(slot)
        self.last_predicted_peer_first_task = {}
        for owner, path in plans.items():
            if owner == state.robot_index or not path:
                continue
            cell = state.decode_cell(state.targets[path[0]])
            self.last_predicted_peer_first_task[str(state.robot_ids[owner])] = [
                int(cell[0]), int(cell[1])
            ]
        return plans

    def choose(self):
        state = self.state
        starting_path = list(self.path)
        for slot in range(len(state.targets)):
            if state.claim_owner[slot] >= 0 and not state.is_candidate(slot):
                state.clear_claim(slot)
        trigger = None
        if self.collision_rising():
            self.release_own_path("hipc_entry")
            trigger = "collision_replan"

        self.clean_path(require_ownership=True)
        repaired_path = self.path != starting_path
        candidates = self.candidates(always_rank=True)
        plans = self._team_plan(candidates)
        new_path = plans.get(state.robot_index, [])[: state.commitment_horizon]
        changed = new_path != self.path or repaired_path
        if changed:
            old_path = list(self.path)
            for slot in old_path:
                if (
                    slot not in new_path
                    and state.claim_owner[slot] == state.robot_index
                ):
                    state.clear_claim(slot)
                    state.queue_message(
                        self.claim_message(
                            "hipc_entry", slot, -1, self.NO_VALUE, True
                        )
                    )
            accepted = []
            previous = state.position
            for slot in new_path:
                distance = state.distance(previous, state.targets[slot])
                bid = -state.adjusted_cost(distance, slot)
                if self.owner_wins(slot, bid, higher_is_better=True):
                    state.set_claim(
                        slot,
                        state.robot_index,
                        bid,
                        self._next_bid_time(),
                    )
                    accepted.append(slot)
                    previous = state.targets[slot]
            self.path = accepted
            for slot in self.path:
                state.queue_message(
                    self.claim_message(
                        "hipc_entry",
                        slot,
                        state.robot_index,
                        state.claim_value[slot],
                    )
                )

        if changed:
            self.last_call_path = trigger or "team_plan_changed"
        elif self.path:
            self.last_call_path = trigger or "team_plan_retained"
        else:
            self.last_call_path = trigger or "no_team_assignment"
        return self.goal_cell()

    def handle_message(self, message):
        return self.parse_claim_message(
            message, ("hipc_entry", "acbba_entry", "cbaa_entry")
        )

    def export_resume(self):
        result = NativeAllocatorBase.export_resume(self)
        result.update(
            {
                "bid_counter": int(self.bid_counter),
                "bad_prediction_count": dict(self.bad_prediction_count),
                "dropped_peers": list(self.dropped_peers),
                "last_predicted_peer_first_task": dict(
                    self.last_predicted_peer_first_task
                ),
                "seen_peer_bundle_signature": dict(
                    self.seen_peer_bundle_signature
                ),
            }
        )
        return result

    def restore_resume(self, resume):
        NativeAllocatorBase.restore_resume(self, resume)
        if not isinstance(resume, dict):
            return
        self.bid_counter = int(resume.get("bid_counter", 0))
        self.bad_prediction_count = dict(
            resume.get("bad_prediction_count", {}) or {}
        )
        self.dropped_peers = sorted(
            str(item) for item in (resume.get("dropped_peers", ()) or ())
        )
        self.last_predicted_peer_first_task = dict(
            resume.get("last_predicted_peer_first_task", {}) or {}
        )
        self.seen_peer_bundle_signature = dict(
            resume.get("seen_peer_bundle_signature", {}) or {}
        )
