"""Bundle-based consensus auction for native collaborative visits."""

from .base import NativeAllocatorBase


class ACBBAAllocator(NativeAllocatorBase):
    name = "ACBBA"

    def choose(self):
        state = self.state
        if self.collision_rising():
            self.release_own_path("acbba_entry")
            trigger = "collision_replan"
        else:
            trigger = None

        # CBBA suffix rule: losing one item invalidates it and all later bids.
        first_bad = None
        for index, slot in enumerate(self.path):
            if (
                not state.is_active(slot)
                or state.claim_owner[slot] != state.robot_index
            ):
                first_bad = index
                break
        if first_bad is not None:
            suffix = self.path[first_bad:]
            for slot in suffix:
                if state.claim_owner[slot] == state.robot_index:
                    state.clear_claim(slot)
                    state.queue_message(
                        self.claim_message(
                            "acbba_entry", slot, -1, self.NO_VALUE, True
                        )
                    )
            self.path = self.path[:first_bad]
            trigger = trigger or "consensus_suffix_release"

        candidates = self.candidates()
        changed = False
        horizon = state.commitment_horizon
        while len(self.path) < horizon:
            best_slot = None
            best_index = 0
            best_bid = self.NO_VALUE
            for slot in candidates:
                if slot in self.path or not state.is_active(slot):
                    continue
                insertion_index, marginal = self.best_distance_insertion(
                    self.path, slot
                )
                bid = -state.adjusted_cost(marginal, slot)
                if not self.owner_wins(slot, bid, higher_is_better=True):
                    continue
                if (
                    best_slot is None
                    or bid > best_bid + self.EPS
                    or (
                        abs(bid - best_bid) <= self.EPS
                        and (
                            state.targets[slot],
                            insertion_index,
                        )
                        < (
                            state.targets[best_slot],
                            best_index,
                        )
                    )
                ):
                    best_slot = slot
                    best_index = insertion_index
                    best_bid = bid
            if best_slot is None:
                break
            self.path.insert(best_index, best_slot)
            state.set_claim(best_slot, state.robot_index, best_bid)
            state.queue_message(
                self.claim_message(
                    "acbba_entry", best_slot, state.robot_index, best_bid
                )
            )
            changed = True

        if changed:
            self.last_call_path = trigger or "bundle_extended"
        elif self.path:
            self.last_call_path = trigger or "bundle_retained"
        else:
            self.last_call_path = trigger or "no_claimable_candidate"
        return self.goal_cell()

    def handle_message(self, message):
        changed = self.parse_claim_message(
            message, ("acbba_entry", "cbaa_entry")
        )
        if changed:
            # Repair occurs at the next authoritative allocator call.
            self.last_call_path = "message_updated_consensus"
        return changed
