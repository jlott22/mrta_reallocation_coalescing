"""Single-task consensus auction for native collaborative visits."""

from .base import NativeAllocatorBase


class CBAAAllocator(NativeAllocatorBase):
    name = "CBAA"

    def choose(self):
        state = self.state
        self.clean_path(require_ownership=True)
        if self.path:
            self.last_call_path = "cached_goal"
            return self.goal_cell()

        candidates = self.candidates()
        best_slot = None
        best_bid = self.NO_VALUE
        for slot in candidates:
            bid = self.score_from(state.position, slot)
            if not self.owner_wins(slot, bid, higher_is_better=True):
                continue
            if (
                best_slot is None
                or bid > best_bid + self.EPS
                or (
                    abs(bid - best_bid) <= self.EPS
                    and state.targets[slot] < state.targets[best_slot]
                )
            ):
                best_slot = slot
                best_bid = bid

        if best_slot is None:
            self.last_call_path = "no_claimable_candidate"
            return self.goal_cell()

        state.set_claim(best_slot, state.robot_index, best_bid)
        self.path = [best_slot]
        state.queue_message(
            self.claim_message("cbaa_entry", best_slot, state.robot_index, best_bid)
        )
        self.last_call_path = "allocated"
        return self.goal_cell()

    def handle_message(self, message):
        changed = self.parse_claim_message(message, ("cbaa_entry",))
        if changed:
            self.clean_path(require_ownership=True)
        return changed
