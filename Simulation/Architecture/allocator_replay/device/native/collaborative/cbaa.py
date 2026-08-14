"""Single-task consensus auction for native collaborative visits."""

from .base import NativeAllocatorBase


class CBAAAllocator(NativeAllocatorBase):
    name = "CBAA"

    def recover_stalled_allocation(self, payload=None):
        """Expire only the best locally blocking remote claim."""

        del payload
        state = self.state
        self.clean_path(require_ownership=True)
        if self.path:
            return True
        blocked_slot = None
        blocked_bid = self.NO_VALUE
        for slot in self.candidates():
            bid = self.score_from(state.position, slot)
            if self.owner_wins(slot, bid, higher_is_better=True):
                return True
            if state.claim_owner[slot] < 0:
                continue
            if (
                blocked_slot is None
                or bid > blocked_bid + self.EPS
                or (
                    abs(bid - blocked_bid) <= self.EPS
                    and self._cell_precedes(slot, blocked_slot)
                )
            ):
                blocked_slot = slot
                blocked_bid = bid
        if blocked_slot is None:
            return False
        # A local lease expiry is not an observed peer release, so it creates
        # no outbound message on the remote owner's behalf.
        state.clear_claim(blocked_slot)
        self.last_call_path = "stalled_peer_claim_expired"
        return True

    def on_task_completed(self, cell, reason="", local=False):
        """Remove the completed single task without clearing other claims."""

        del reason, local
        state = self.state
        slot = state.slot_for_cell(cell)
        if slot is None:
            return False
        invalidated_goal = state.current_goal == int(state.targets[slot])
        previous_owner = int(state.claim_owner[slot])
        previous_value = float(state.claim_value[slot])
        if slot in self.path:
            self.path = [item for item in self.path if item != slot]
        if previous_owner >= 0:
            state.clear_claim(slot)
            released = self.claim_message(
                "cbaa_entry", slot, -1, self.NO_VALUE, True
            )
            released["released_winner"] = state.owner_id(previous_owner)
            released["released_bid"] = previous_value
            state.queue_message(released)
        self.last_call_path = "task_completion_repair"
        return invalidated_goal

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
            # Preserve the auction-time winning bid while executing the
            # retained task.  Movement is not a new auction and must not
            # create a same-winner bid-revision feedback loop.
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
                    and self._cell_precedes(slot, best_slot)
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

    def _cell_precedes(self, slot, other_slot):
        """Match the desktop allocator's lexicographic ``(x, y)`` tie-break."""

        state = self.state
        cell = state.decode_cell(state.targets[slot])
        other = state.decode_cell(state.targets[other_slot])
        return cell[0] < other[0] or (
            cell[0] == other[0] and cell[1] < other[1]
        )

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
