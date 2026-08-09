"""Local implicit team planner for native collaborative visits."""

from .base import NativeAllocatorBase


class HIPCAllocator(NativeAllocatorBase):
    name = "HIPC"

    def _team_indices(self):
        state = self.state
        return [
            index
            for index in range(len(state.robot_ids))
            if state.peer_position_valid[index]
        ]

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
                    key = (
                        -score,
                        state.robot_id_key(state.robot_ids[owner]),
                        int(state.targets[slot]),
                    )
                    if best is None or key < best[0]:
                        best = (key, owner, slot, score)
            if best is None:
                break
            _, owner, slot, _ = best
            plans[owner].append(slot)
            endpoints[owner] = state.targets[slot]
            assigned.add(slot)
        return plans

    def choose(self):
        state = self.state
        trigger = None
        if self.collision_rising():
            self.release_own_path("hipc_entry")
            trigger = "collision_replan"

        self.clean_path(require_ownership=True)
        candidates = self.candidates(always_rank=True)
        plans = self._team_plan(candidates)
        new_path = plans.get(state.robot_index, [])[: state.commitment_horizon]
        changed = new_path != self.path
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
                    state.set_claim(slot, state.robot_index, bid)
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
