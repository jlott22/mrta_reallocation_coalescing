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

    def recover_stalled_allocation(self, payload=None):
        """Expire peer predictions while preserving this robot's bundle."""

        del payload
        state = self.state
        changed = False
        for index, robot_id in enumerate(state.robot_ids):
            if index == state.robot_index or not state.peer_position_valid[index]:
                continue
            key = str(robot_id)
            previous = int(self.bad_prediction_count.get(key, 0))
            if previous < self.BAD_PRED_LIMIT:
                self.bad_prediction_count[key] = self.BAD_PRED_LIMIT
                changed = True
        if changed:
            self.last_call_path = "stalled_peer_predictions_expired"
        return changed

    def on_task_completed(self, cell, reason="", local=False):
        """Preserve a locally executed suffix; release external-loss suffixes."""

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
                self.path = self.path[1:]
            else:
                for released in self.path[index:]:
                    if state.claim_owner[released] == state.robot_index:
                        state.clear_claim(released)
                self.path = self.path[:index]
        elif state.claim_owner[slot] >= 0:
            state.clear_claim(slot)
        self.last_call_path = "task_completion_repair"
        return invalidated_goal

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
        maximum = len(candidates)
        for _ in range(maximum):
            best = None
            for owner in team:
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

    def _truncate_invalid_suffix(self):
        """Apply desktop HIPC's first-bad-item bundle repair rule."""

        state = self.state
        first_bad = None
        for index, slot in enumerate(self.path):
            if (
                not state.is_candidate(slot)
                or state.claim_owner[slot] != state.robot_index
            ):
                first_bad = index
                break
        if first_bad is None:
            return False
        # HIPC bundle entries are causally dependent.  Losing one item drops
        # the complete suffix, not just that item; retained self claims in the
        # suffix must be released before the local team plan is rebuilt.
        for slot in self.path[first_bad:]:
            if state.claim_owner[slot] == state.robot_index:
                state.clear_claim(slot)
        self.path = self.path[:first_bad]
        return True

    def _repair_bundle_after_consensus(self):
        """Repair a lost local bundle before the next inbound HIPC entry.

        Desktop HIPC performs its suffix repair in the receive handler.  The
        native runtime drains a burst of peer callbacks before ``choose()``,
        so deferring this work until ``choose`` lets a later callback inspect
        stale self-owned cells and preserve old bid timestamps.
        """

        return self._truncate_invalid_suffix()

    def _sync_current_goal_after_message(self):
        """Mirror the desktop handler's conservative invalid-goal repair."""

        if self.state.current_goal is not None and not self.path:
            self.state.current_goal = None

    def choose(self):
        state = self.state
        for slot in range(len(state.targets)):
            if state.claim_owner[slot] >= 0 and not state.is_candidate(slot):
                state.clear_claim(slot)
        trigger = None
        if self.collision_rising():
            self.release_own_path("hipc_entry")
            trigger = "collision_replan"

        repaired_path = self._truncate_invalid_suffix()
        candidates = self.candidates(always_rank=True)
        plans = self._team_plan(candidates)
        new_path = plans.get(state.robot_index, [])
        if (
            state.is_admission_allocation()
            and self.path
            and state.current_goal == int(state.targets[self.path[0]])
            and state.is_candidate(self.path[0])
        ):
            retained_head = self.path[0]
            new_path = [retained_head] + [
                slot for slot in new_path if slot != retained_head
            ]
        plan_changed = new_path != self.path
        changed = plan_changed or repaired_path
        if plan_changed:
            old_path = list(self.path)
            common_prefix = 0
            while (
                common_prefix < len(old_path)
                and common_prefix < len(new_path)
                and old_path[common_prefix] == new_path[common_prefix]
                and state.claim_owner[old_path[common_prefix]]
                == state.robot_index
                and state.is_candidate(old_path[common_prefix])
            ):
                common_prefix += 1
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
            # Preserve the unchanged executing prefix exactly, including its
            # bids and protocol timestamps.  Only the changed suffix is newly
            # allocated; this is essential when admission extends a bundle.
            accepted = list(new_path[:common_prefix])
            previous = (
                state.position
                if not accepted
                else state.targets[accepted[-1]]
            )
            for slot in new_path[common_prefix:]:
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
        if not isinstance(message, dict) or message.get("type") not in (
            "hipc_entry",
            "hipc_clear_bundle",
        ):
            return False
        state = self.state
        sender = state.owner_index(message.get("sender"))
        if sender < 0 or sender == state.robot_index:
            return False

        self._update_prediction_quality(message, sender)
        changed = False
        repaired = False
        bundle = message.get("bundle_cells")
        if isinstance(bundle, list):
            included = set()
            for item in bundle:
                try:
                    cell = (
                        (item["x"], item["y"])
                        if isinstance(item, dict)
                        else item
                    )
                    slot = state.slot_for_cell(cell)
                    if slot is not None:
                        included.add(slot)
                except (KeyError, TypeError, ValueError):
                    continue
            for slot in range(len(state.targets)):
                if state.claim_owner[slot] == sender and slot not in included:
                    state.clear_claim(slot)
                    changed = True
            # A bundle declaration can invalidate this robot's dependent
            # suffix before the entry in the same message is reconciled.
            repaired = self._repair_bundle_after_consensus()

        if message.get("type") == "hipc_clear_bundle":
            self._sync_current_goal_after_message()
            if changed or repaired:
                self.last_call_path = "message_updated_consensus"
            return changed or repaired

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
            self._sync_current_goal_after_message()
            return changed or repaired
        if slot is None:
            self._sync_current_goal_after_message()
            return changed or repaired
        if not state.is_candidate(slot):
            # The desktop receiver clears an entry that has become invalid or
            # completed, then repairs any local suffix that depended on it.
            if state.claim_owner[slot] >= 0:
                state.clear_claim(slot)
                changed = True
            repaired = self._repair_bundle_after_consensus() or repaired
            self._sync_current_goal_after_message()
            if changed or repaired:
                self.last_call_path = "message_updated_consensus"
            return changed or repaired

        local_owner = int(state.claim_owner[slot])
        local_bid = float(state.claim_value[slot])
        local_time = int(state.claim_epoch[slot])
        should_update = False
        if incoming_owner == sender and local_owner == sender:
            should_update = incoming_time >= local_time
        elif incoming_bid > local_bid + self.EPS:
            should_update = True
        elif abs(incoming_bid - local_bid) <= self.EPS:
            should_update = local_owner < 0 or (
                incoming_owner >= 0 and incoming_owner < local_owner
            )
        if should_update:
            if incoming_owner < 0:
                state.clear_claim(slot)
            else:
                state.set_claim(
                    slot, incoming_owner, incoming_bid, incoming_time
                )
            changed = True
        # Match the desktop receive path: later queued callbacks must observe
        # the repaired bundle rather than a stale self-owned suffix.
        repaired = self._repair_bundle_after_consensus() or repaired
        self._sync_current_goal_after_message()
        if changed or repaired:
            self.last_call_path = "message_updated_consensus"
        return changed or repaired

    def _update_prediction_quality(self, message, sender):
        state = self.state
        actual_first = None
        raw_bundle = message.get("bundle_cells")
        signature = []
        if isinstance(raw_bundle, list):
            for item in raw_bundle:
                try:
                    cell = (
                        (int(item["x"]), int(item["y"]))
                        if isinstance(item, dict)
                        else (int(item[0]), int(item[1]))
                    )
                    signature.append([cell[0], cell[1]])
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
            if signature:
                actual_first = tuple(signature[0])
        if actual_first is None:
            try:
                if int(message.get("order", -1)) == 0:
                    actual_first = (
                        int(message["x"]), int(message["y"])
                    )
                    signature = [[actual_first[0], actual_first[1]], 0]
            except (KeyError, TypeError, ValueError):
                return
        if actual_first is None:
            return

        sender_id = str(state.robot_ids[sender])
        if self.seen_peer_bundle_signature.get(sender_id) == signature:
            return
        self.seen_peer_bundle_signature[sender_id] = signature
        predicted = self.last_predicted_peer_first_task.get(sender_id)
        if predicted is None:
            return
        try:
            distance = abs(int(predicted[0]) - actual_first[0]) + abs(
                int(predicted[1]) - actual_first[1]
            )
        except (TypeError, ValueError, IndexError):
            return
        current = int(self.bad_prediction_count.get(sender_id, 0))
        if distance <= 0:
            self.bad_prediction_count[sender_id] = max(0, current - 1)
        else:
            self.bad_prediction_count[sender_id] = current + 1

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
