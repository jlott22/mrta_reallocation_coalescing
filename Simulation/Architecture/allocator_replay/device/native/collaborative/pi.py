"""Performance-impact task inclusion for native collaborative visits."""

from .base import NativeAllocatorBase


class PIAllocator(NativeAllocatorBase):
    name = "PI"
    INF = 1.0e18

    def __init__(self, state):
        NativeAllocatorBase.__init__(self, state)
        self.time_counter = 0

    def _next_time(self):
        self.time_counter += 1
        return self.time_counter

    def on_allocation_epoch(self, reason, admitted_cells, epoch_index=None):
        reset = NativeAllocatorBase.on_allocation_epoch(
            self, reason, admitted_cells, epoch_index
        )
        if admitted_cells:
            self.time_counter = 0
        return reset

    def _refresh_local_significance(self):
        state = self.state
        full_cost = self.route_cost(self.path)
        for index, slot in enumerate(self.path):
            without = self.path[:index] + self.path[index + 1 :]
            significance = max(0.0, full_cost - self.route_cost(without))
            owner = int(state.claim_owner[slot])
            previous = float(state.claim_value[slot])
            epoch = int(state.claim_epoch[slot])
            if (
                owner != state.robot_index
                or abs(previous - significance) > self.EPS
                or epoch <= 0
            ):
                epoch = self._next_time()
            state.set_claim(
                slot, state.robot_index, significance, epoch
            )

    def choose(self):
        state = self.state
        if self.collision_rising():
            self.release_own_path("pi_entry")
            trigger = "collision_replan"
        else:
            trigger = None

        kept = []
        removed = []
        for slot in self.path:
            if (
                state.is_candidate(slot)
                and state.claim_owner[slot] == state.robot_index
            ):
                kept.append(slot)
            else:
                removed.append(slot)
        if removed:
            for slot in removed:
                if state.claim_owner[slot] == state.robot_index:
                    state.clear_claim(slot)
            # PI removes only the lost item; unlike CBBA it does not discard
            # the dependent suffix.
            self.path = kept
            self._refresh_local_significance()
            trigger = trigger or "consensus_path_repair"
            # Desktop PI marks a full path snapshot pending whenever message
            # consensus repairs its retained path.  Re-emit the surviving
            # prefix (or an explicit clear) so the native output has the same
            # communication effect even when no new candidate is inserted.
            if self.path:
                for slot in self.path:
                    state.queue_message(
                        self.claim_message(
                            "pi_entry",
                            slot,
                            state.robot_index,
                            state.claim_value[slot],
                        )
                    )
            else:
                state.queue_message(
                    {
                        "type": "pi_clear_path",
                        "sender": state.robot_id,
                        "timestamp": int(state.event_counter),
                        "path_cells": [],
                        "path_size": 0,
                    }
                )

        changed = False
        horizon = state.commitment_horizon
        while len(self.path) < horizon:
            candidates = self.candidates()
            best = None
            for slot in candidates:
                if slot in self.path:
                    continue
                insertion_index, marginal = self.best_insertion(self.path, slot)
                owner = int(state.claim_owner[slot])
                known = (
                    self.INF
                    if owner < 0
                    else max(0.0, float(state.claim_value[slot]))
                )
                if not self.owner_wins(
                    slot, marginal, higher_is_better=False
                ):
                    continue
                improvement = self.INF if owner < 0 else known - marginal
                candidate = (
                    slot,
                    insertion_index,
                    marginal,
                    improvement,
                    owner < 0,
                )
                if self._better(candidate, best):
                    best = candidate
            if best is None:
                break
            slot, insertion_index, _, _, _ = best
            self.path.insert(insertion_index, slot)
            self._refresh_local_significance()
            state.queue_message(
                self.claim_message(
                    "pi_entry",
                    slot,
                    state.robot_index,
                    state.claim_value[slot],
                )
            )
            changed = True

        if changed:
            # Path insertion changes downstream marginal costs; publish the
            # complete bounded prefix rather than stale individual values.
            for slot in self.path:
                state.queue_message(
                    self.claim_message(
                        "pi_entry",
                        slot,
                        state.robot_index,
                        state.claim_value[slot],
                    )
                )
            self.last_call_path = trigger or "path_extended"
        elif self.path:
            self.last_call_path = trigger or "path_retained"
        else:
            self.last_call_path = trigger or "no_includable_candidate"
        return self.goal_cell()

    def _better(self, candidate, best):
        if best is None:
            return True
        slot, index, marginal, improvement, unclaimed = candidate
        best_slot, best_index, best_marginal, best_improvement, best_unclaimed = best
        if unclaimed != best_unclaimed:
            return unclaimed
        if not unclaimed:
            if improvement > best_improvement + self.EPS:
                return True
            if improvement < best_improvement - self.EPS:
                return False
        if marginal < best_marginal - self.EPS:
            return True
        if marginal > best_marginal + self.EPS:
            return False
        state = self.state
        probability = float(state.probability[slot])
        best_probability = float(state.probability[best_slot])
        if probability != best_probability:
            return probability > best_probability
        if index != best_index:
            return index < best_index
        return state.targets[slot] < state.targets[best_slot]

    def handle_message(self, message):
        return self.parse_claim_message(
            message,
            ("pi_entry", "acbba_entry", "cbaa_entry"),
            lower_is_better=True,
        )

    def export_resume(self):
        result = NativeAllocatorBase.export_resume(self)
        result["time_counter"] = int(self.time_counter)
        return result

    def restore_resume(self, resume):
        NativeAllocatorBase.restore_resume(self, resume)
        if isinstance(resume, dict):
            self.time_counter = int(resume.get("time_counter", 0))
