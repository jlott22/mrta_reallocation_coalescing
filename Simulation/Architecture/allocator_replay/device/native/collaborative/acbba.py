"""Bundle-based consensus auction for native collaborative visits."""

from .base import NativeAllocatorBase


class ACBBAAllocator(NativeAllocatorBase):
    name = "ACBBA"

    def __init__(self, state):
        NativeAllocatorBase.__init__(self, state)
        self.bid_counter = 0

    def _next_bid_time(self):
        self.bid_counter += 1
        return self.bid_counter

    def recover_stalled_allocation(self, payload=None):
        """Expire at most one locally blocking peer claim."""

        del payload
        state = self.state
        if self.path:
            return True
        blocked = None
        for slot in self.candidates():
            insertion_index, marginal = self.best_distance_insertion([], slot)
            bid = -state.adjusted_cost(marginal, slot)
            if self.owner_wins(slot, bid, higher_is_better=True):
                return True
            if state.claim_owner[slot] < 0:
                continue
            candidate = (slot, insertion_index, bid)
            if (
                blocked is None
                or bid > blocked[2] + self.EPS
                or (
                    abs(bid - blocked[2]) <= self.EPS
                    and self._insertion_precedes(
                        slot, insertion_index, blocked[0], blocked[1]
                    )
                )
            ):
                blocked = candidate
        if blocked is None:
            return False
        state.clear_claim(blocked[0])
        self.last_call_path = "stalled_peer_claim_expired"
        return True

    def on_task_completed(self, cell, reason="", local=False):
        """Head-pop local service; retain the allocator's suffix rules."""

        state = self.state
        slot = state.slot_for_cell(cell)
        if slot is None:
            return False
        invalidated_goal = state.current_goal == int(state.targets[slot])
        if slot in self.path:
            index = self.path.index(slot)
            completed_locally = bool(local) or str(reason).startswith("local")
            if completed_locally and index == 0:
                if state.claim_owner[slot] == state.robot_index:
                    state.clear_claim(slot)
                    self._queue_claim(slot, -1, self.NO_VALUE, 0)
                self.path = self.path[1:]
            else:
                for released in self.path[index:]:
                    if state.claim_owner[released] == state.robot_index:
                        state.clear_claim(released)
                        self._queue_claim(released, -1, self.NO_VALUE, 0)
                self.path = self.path[:index]
        elif state.claim_owner[slot] >= 0:
            state.clear_claim(slot)
            self._queue_claim(slot, -1, self.NO_VALUE, 0)
        self.last_call_path = "task_completion_repair"
        return invalidated_goal

    def _queue_claim(self, slot, owner=None, value=None, epoch=None):
        state = self.state
        if owner is None:
            owner = int(state.claim_owner[slot])
        if value is None:
            value = float(state.claim_value[slot])
        if epoch is None:
            epoch = int(state.claim_epoch[slot])
        message = self.claim_message(
            "acbba_entry", slot, owner, value, owner < 0
        )
        message["timestamp"] = (
            self.NO_VALUE if owner < 0 else int(epoch)
        )
        state.queue_message(message)

    def _set_claim(self, slot, owner, value, epoch):
        state = self.state
        old = (
            int(state.claim_owner[slot]),
            float(state.claim_value[slot]),
            int(state.claim_epoch[slot]),
        )
        if owner < 0:
            state.clear_claim(slot)
        else:
            state.set_claim(slot, owner, value, epoch)
        return old != (
            int(state.claim_owner[slot]),
            float(state.claim_value[slot]),
            int(state.claim_epoch[slot]),
        )

    def choose(self):
        state = self.state
        for slot in range(len(state.targets)):
            if state.claim_owner[slot] < 0 or state.is_candidate(slot):
                continue
            state.clear_claim(slot)
            self._queue_claim(slot, -1, self.NO_VALUE, 0)
        if self.collision_rising():
            self.release_own_path("acbba_entry")
            trigger = "collision_replan"
        else:
            trigger = None

        # CBBA suffix rule: losing one item invalidates it and all later bids.
        first_bad = None
        for index, slot in enumerate(self.path):
            if (
                not state.is_candidate(slot)
                or state.claim_owner[slot] != state.robot_index
            ):
                first_bad = index
                break
        if first_bad is not None:
            suffix = self.path[first_bad:]
            for slot in suffix:
                if state.claim_owner[slot] == state.robot_index:
                    state.clear_claim(slot)
                    self._queue_claim(slot, -1, self.NO_VALUE, 0)
            self.path = self.path[:first_bad]
            trigger = trigger or "consensus_suffix_release"

        candidates = self.candidates()
        changed = False
        while True:
            best_slot = None
            best_index = 0
            best_bid = self.NO_VALUE
            for slot in candidates:
                if slot in self.path or not state.is_candidate(slot):
                    continue
                first_index = 0
                if (
                    state.is_admission_allocation()
                    and self.path
                    and state.current_goal
                    == int(state.targets[self.path[0]])
                    and state.is_candidate(self.path[0])
                ):
                    first_index = 1
                insertion_index, marginal = self.best_distance_insertion(
                    self.path, slot, first_index
                )
                bid = -state.adjusted_cost(marginal, slot)
                if not self.owner_wins(slot, bid, higher_is_better=True):
                    continue
                if (
                    best_slot is None
                    or bid > best_bid + self.EPS
                    or (
                        abs(bid - best_bid) <= self.EPS
                        and self._insertion_precedes(
                            slot,
                            insertion_index,
                            best_slot,
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
            bid_time = self._next_bid_time()
            state.set_claim(
                best_slot, state.robot_index, best_bid, bid_time
            )
            state.queue_message(
                dict(
                    self.claim_message(
                        "acbba_entry", best_slot, state.robot_index, best_bid
                    ),
                    timestamp=bid_time,
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

    def _insertion_precedes(self, slot, index, other_slot, other_index):
        """Match desktop ties: lexicographic cell first, insertion index second."""

        state = self.state
        cell = state.decode_cell(state.targets[slot])
        other = state.decode_cell(state.targets[other_slot])
        if cell[0] != other[0]:
            return cell[0] < other[0]
        if cell[1] != other[1]:
            return cell[1] < other[1]
        return index < other_index

    def handle_message(self, message):
        if not isinstance(message, dict) or message.get("type") not in (
            "acbba_entry", "cbaa_entry"
        ):
            return False
        state = self.state
        sender = state.owner_index(message.get("sender"))
        if sender < 0 or sender == state.robot_index:
            return False
        try:
            slot = state.slot_for_cell((message["x"], message["y"]))
            incoming_owner = state.owner_index(
                message.get("winner", message.get("owner"))
            )
            incoming_bid = float(
                message.get("bid", message.get("value", self.NO_VALUE))
            )
            incoming_time = int(
                float(message.get("timestamp", message.get("bid_time", 0)))
            )
        except (KeyError, TypeError, ValueError):
            return False
        if slot is None or not state.is_candidate(slot):
            return False

        changed = False
        bundle = message.get("bundle_cells")
        if isinstance(bundle, list) and incoming_owner == sender:
            included = set()
            for item in bundle:
                try:
                    cell = (
                        (item["x"], item["y"])
                        if isinstance(item, dict)
                        else item
                    )
                    included.add(state.slot_for_cell(cell))
                except (KeyError, TypeError, ValueError):
                    continue
            for other in range(len(state.targets)):
                if (
                    int(state.claim_owner[other]) == sender
                    and other not in included
                ):
                    changed = self._set_claim(
                        other, -1, self.NO_VALUE, 0
                    ) or changed
                    self._queue_claim(other, -1, self.NO_VALUE, 0)

        local_owner = int(state.claim_owner[slot])
        local_bid = float(state.claim_value[slot])
        local_time = int(state.claim_epoch[slot])
        receiver = state.robot_index

        def update(rebroadcast):
            result = self._set_claim(
                slot, incoming_owner, incoming_bid, incoming_time
            )
            if rebroadcast:
                self._queue_claim(
                    slot, incoming_owner, incoming_bid, incoming_time
                )
            return result

        def leave(rebroadcast):
            if rebroadcast:
                self._queue_claim(slot)
            return False

        def reset_and_rebroadcast():
            result = self._set_claim(slot, -1, self.NO_VALUE, 0)
            self._queue_claim(
                slot, incoming_owner, incoming_bid, incoming_time
            )
            return result

        def update_time_and_rebroadcast():
            if local_owner != receiver:
                return leave(True)
            next_time = self._next_bid_time()
            result = self._set_claim(
                slot, receiver, local_bid, next_time
            )
            self._queue_claim(slot, receiver, local_bid, next_time)
            return result

        bid_gt = incoming_bid > local_bid + self.EPS
        bid_lt = incoming_bid < local_bid - self.EPS
        bid_eq = not bid_gt and not bid_lt
        time_gt = incoming_time > local_time
        time_lt = incoming_time < local_time
        time_gte = incoming_time >= local_time
        time_lte = incoming_time <= local_time

        if incoming_owner == sender:
            if local_owner == receiver:
                if bid_gt or (bid_eq and incoming_owner < local_owner):
                    changed = update(True) or changed
                elif bid_lt:
                    changed = update_time_and_rebroadcast() or changed
                else:
                    changed = leave(True) or changed
            elif local_owner == sender:
                changed = (update(False) if time_gt else leave(False)) or changed
            elif local_owner < 0:
                changed = update(True) or changed
            elif bid_gt and time_gte:
                changed = update(True) or changed
            elif bid_lt and time_lte:
                changed = leave(True) or changed
            elif bid_eq:
                changed = leave(True) or changed
            elif (bid_lt and time_gt) or (bid_gt and time_lt):
                changed = reset_and_rebroadcast() or changed
            else:
                changed = leave(True) or changed
        elif incoming_owner == receiver:
            if local_owner == receiver:
                changed = leave(incoming_time != local_time) or changed
            elif local_owner == sender:
                changed = reset_and_rebroadcast() or changed
            else:
                changed = leave(True) or changed
        elif incoming_owner >= 0:
            if local_owner == receiver:
                if bid_gt or (bid_eq and incoming_owner < local_owner):
                    changed = update(True) or changed
                elif bid_lt:
                    changed = update_time_and_rebroadcast() or changed
                else:
                    changed = leave(True) or changed
            elif local_owner == sender:
                changed = (
                    update(True) if incoming_time >= local_time
                    else reset_and_rebroadcast()
                ) or changed
            elif local_owner == incoming_owner:
                changed = (update(False) if time_gt else leave(False)) or changed
            elif local_owner < 0:
                changed = update(True) or changed
            elif bid_gt and time_gte:
                changed = update(True) or changed
            elif bid_lt and time_lte:
                changed = leave(True) or changed
            elif bid_eq:
                changed = leave(True) or changed
            elif (bid_lt and time_gt) or (bid_gt and time_lt):
                changed = reset_and_rebroadcast() or changed
            else:
                changed = leave(True) or changed
        else:
            if local_owner == receiver:
                changed = leave(True) or changed
            elif local_owner == sender:
                changed = update(True) or changed
            elif local_owner < 0:
                changed = leave(False) or changed
            elif time_gt:
                changed = update(True) or changed
            else:
                changed = leave(True) or changed
        if changed:
            # Repair occurs at the next authoritative allocator call.
            self.last_call_path = "message_updated_consensus"
        return changed

    def export_resume(self):
        result = NativeAllocatorBase.export_resume(self)
        result["bid_counter"] = int(self.bid_counter)
        return result

    def restore_resume(self, resume):
        NativeAllocatorBase.restore_resume(self, resume)
        if isinstance(resume, dict):
            self.bid_counter = int(resume.get("bid_counter", 0))
