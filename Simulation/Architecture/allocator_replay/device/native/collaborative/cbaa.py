"""Single-task consensus auction for native collaborative visits."""

from .base import NativeAllocatorBase


class CBAAAllocator(NativeAllocatorBase):
    name = "CBAA"

    def choose(self):
        state = self.state
        # Desktop CBAA purges every invalid table entry, including peer claims
        # outside this robot's one-cell path.  Preserve the released winner/bid
        # in the delta so downstream robots can remove that stale claim too.
        for slot in range(len(state.targets)):
            previous_owner = int(state.claim_owner[slot])
            if previous_owner < 0 or state.is_candidate(slot):
                continue
            previous_value = float(state.claim_value[slot])
            state.clear_claim(slot)
            released = self.claim_message(
                "cbaa_entry", slot, -1, self.NO_VALUE, True
            )
            released["released_winner"] = state.owner_id(previous_owner)
            released["released_bid"] = previous_value
            state.queue_message(released)
        self.clean_path(require_ownership=True)
        if self.path:
            # The desktop CBAA refreshes the retained claim after every
            # movement.  Distance, and therefore the bid, can change even
            # though the selected cell does not.  Publish that logical table
            # delta outside the timed call just as the desktop implementation
            # does; otherwise peers see different consensus traffic.
            slot = self.path[0]
            bid = self.score_from(state.position, slot)
            if abs(float(state.claim_value[slot]) - bid) > self.EPS:
                state.set_claim(slot, state.robot_index, bid)
                state.queue_message(
                    self.claim_message(
                        "cbaa_entry", slot, state.robot_index, bid
                    )
                )
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
        if not isinstance(message, dict):
            return False
        try:
            slot = self.state.slot_for_cell((message["x"], message["y"]))
        except (KeyError, TypeError, ValueError):
            return False
        if slot is None:
            return False
        previous_owner = int(self.state.claim_owner[slot])
        previous_value = float(self.state.claim_value[slot])
        changed = self.parse_claim_message(message, ("cbaa_entry",))
        if changed:
            self.clean_path(require_ownership=True)
            # CBAA is a delta-known-table protocol: an accepted peer update is
            # retransmitted by this robot with the peer winner retained and
            # this robot as sender.  This is a real observable output, not
            # bookkeeping, and is required for message parity.
            owner = int(self.state.claim_owner[slot])
            value = float(self.state.claim_value[slot])
            if owner < 0:
                forwarded = self.claim_message(
                    "cbaa_entry", slot, -1, self.NO_VALUE, True
                )
                forwarded["released_winner"] = self.state.owner_id(
                    previous_owner
                )
                forwarded["released_bid"] = previous_value
            else:
                forwarded = self.claim_message(
                    "cbaa_entry", slot, owner, value
                )
            self.state.queue_message(forwarded)
        return changed
