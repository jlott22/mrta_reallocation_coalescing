"""Performance-impact task inclusion for native collaborative visits."""

from .base import NativeAllocatorBase


class PIAllocator(NativeAllocatorBase):
    name = "PI"
    INF = 1.0e18

    def __init__(self, state):
        NativeAllocatorBase.__init__(self, state)
        self.time_counter = 0
        # Communication is serialized after the timed allocator call.  Keep a
        # one-call marker for logical table changes that require a full PI
        # snapshot even when the retained path itself is unchanged.
        self.snapshot_requested = False

    def recover_stalled_allocation(self, payload=None):
        """Expire at most one locally blocking peer significance entry.

        Recovery is deliberately a lease decision local to this allocator. It
        neither clears the retained PI path nor fabricates a release message
        from the peer whose stale entry is expired.  The ordinary inclusion
        pass that follows will publish this robot's new claim if it wins.
        """

        del payload
        state = self.state
        if self.path:
            return True

        blocked = None
        for slot in self.candidates():
            insertion_index, marginal = self.best_insertion([], slot)
            owner = int(state.claim_owner[slot])
            known = (
                self.INF
                if owner < 0
                else max(0.0, float(state.claim_value[slot]))
            )
            if self.owner_wins(slot, marginal, higher_is_better=False):
                return True
            if owner < 0 or owner == state.robot_index:
                continue
            candidate = (
                slot,
                insertion_index,
                marginal,
                known - marginal,
                False,
            )
            if self._better(candidate, blocked):
                blocked = candidate

        if blocked is None:
            return False
        state.clear_claim(blocked[0])
        self.last_call_path = "stalled_peer_claim_expired"
        return True

    def _next_time(self):
        self.time_counter += 1
        return self.time_counter

    def on_task_completed(self, cell, reason="", local=False):
        """Remove only the completed PI item and retain the remaining path."""

        del reason, local
        state = self.state
        slot = state.slot_for_cell(cell)
        if slot is None:
            return False
        invalidated_goal = state.current_goal == int(state.targets[slot])
        if slot in self.path:
            self.path = [item for item in self.path if item != slot]
            if state.claim_owner[slot] == state.robot_index:
                state.clear_claim(slot)
            self._refresh_local_significance()
            self.snapshot_requested = True
        elif state.claim_owner[slot] >= 0:
            state.clear_claim(slot)
            self.snapshot_requested = True
        self.last_call_path = "task_completion_repair"
        return invalidated_goal

    def _refresh_local_significance(self):
        state = self.state
        for index, slot in enumerate(self.path):
            previous = (
                state.position
                if index == 0
                else state.targets[self.path[index - 1]]
            )
            significance = state.adjusted_cost(
                state.distance(previous, state.targets[slot]), slot
            )
            if index + 1 < len(self.path):
                following = self.path[index + 1]
                significance += state.adjusted_cost(
                    state.distance(
                        state.targets[slot], state.targets[following]
                    ),
                    following,
                )
                significance -= state.adjusted_cost(
                    state.distance(previous, state.targets[following]),
                    following,
                )
            significance = max(0.0, significance)
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

    def _repair_path_after_consensus(self):
        """Remove peer-won items before the next inbound PI entry.

        PI keeps unaffected suffix items, but their marginal significances
        depend on the retained prefix.  The desktop receiver performs this
        repair for every accepted peer update; doing it only in ``choose``
        leaves subsequent queued entries to inspect stale local ownership.
        """

        state = self.state
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
        if not removed:
            return False

        for slot in removed:
            if state.claim_owner[slot] == state.robot_index:
                state.clear_claim(slot)
        self.path = kept
        self._refresh_local_significance()
        # ``drain_messages`` emits the matching full-path snapshot after this
        # authoritative call, outside W_alloc serialization.
        self.snapshot_requested = True
        return True

    def _sync_current_goal_after_message(self):
        """Mirror the desktop PI handler's invalidation rule."""

        state = self.state
        next_goal = (
            None
            if not self.path
            else int(state.targets[self.path[0]])
        )
        if state.current_goal is not None and state.current_goal != next_goal:
            state.current_goal = None

    def choose(self):
        state = self.state
        # Desktop PI clears every invalid/completed table entry before path
        # repair, including claims owned by peers.  Keeping such a claim would
        # make the compact native consensus table observably stale even when
        # this robot's chosen path and outbound messages still match.
        invalid_claim_cleared = False
        for slot in range(len(state.targets)):
            if state.claim_owner[slot] >= 0 and not state.is_candidate(slot):
                state.clear_claim(slot)
                invalid_claim_cleared = True
        if invalid_claim_cleared:
            # Desktop PI marks its snapshot cache pending whenever any stale
            # table entry is cleared, including an entry outside an empty
            # local path.  The runtime will apply last-sent suppression and
            # emit either the current path or an explicit clear after timing.
            self.snapshot_requested = True
        if self.collision_rising():
            self.release_own_path("pi_entry")
            trigger = "collision_replan"
        else:
            trigger = None

        if self._repair_path_after_consensus():
            trigger = trigger or "consensus_path_repair"

        # Desktop PI treats its path as the complete set of local ownership
        # claims.  Clear any valid but stale self-owned table entry outside
        # that path before attempting inclusion; otherwise a full horizon can
        # leave an unreachable local claim resident indefinitely.
        stale_local_claim_cleared = False
        for slot in range(len(state.targets)):
            if (
                state.claim_owner[slot] == state.robot_index
                and slot not in self.path
            ):
                state.clear_claim(slot)
                stale_local_claim_cleared = True
        if stale_local_claim_cleared:
            self.snapshot_requested = True

        changed = False
        while True:
            candidates = self.candidates()
            best = None
            for slot in candidates:
                if slot in self.path:
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
                insertion_index, marginal = self.best_insertion(
                    self.path, slot, first_index
                )
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
        cell = state.decode_cell(state.targets[slot])
        best_cell = state.decode_cell(state.targets[best_slot])
        return cell[0] < best_cell[0] or (
            cell[0] == best_cell[0] and cell[1] < best_cell[1]
        )

    def handle_message(self, message):
        if not isinstance(message, dict):
            return False
        message_type = message.get("type")
        if message_type not in (
            "pi_entry",
            "pi_clear_path",
            "acbba_entry",
            "cbaa_entry",
        ):
            return False
        state = self.state
        sender = state.owner_index(message.get("sender"))
        if sender < 0 or sender == state.robot_index:
            return False

        changed = False
        path_cells = message.get("path_cells")
        if isinstance(path_cells, list):
            included = set()
            for item in path_cells:
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

        if message_type == "pi_clear_path":
            if changed:
                self._repair_path_after_consensus()
                self.last_call_path = "message_updated_consensus"
            self._sync_current_goal_after_message()
            return changed

        changed = self.parse_claim_message(
            message,
            ("pi_entry", "acbba_entry", "cbaa_entry"),
            lower_is_better=True,
        ) or changed
        if changed:
            self._repair_path_after_consensus()
            self.last_call_path = "message_updated_consensus"
        self._sync_current_goal_after_message()
        return changed

    def export_resume(self):
        result = NativeAllocatorBase.export_resume(self)
        result["time_counter"] = int(self.time_counter)
        return result

    def restore_resume(self, resume):
        NativeAllocatorBase.restore_resume(self, resume)
        if isinstance(resume, dict):
            self.time_counter = int(resume.get("time_counter", 0))
