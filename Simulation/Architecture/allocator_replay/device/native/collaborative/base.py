"""Shared compact operations for native collaborative allocators."""

from .compat import ticks_diff, ticks_us


class NativeAllocatorBase:
    name = "BASE"
    NO_VALUE = -1.0e18
    EPS = 1.0e-9

    def __init__(self, state):
        self.state = state
        self.path = []
        self.last_collision_active = False
        self.last_call_path = "initial"

    def candidates(self, always_rank=False):
        started = ticks_us()
        try:
            state = self.state
            slots = state.active_slots()
            state.candidate_count_before = len(slots)
            limit = state.max_candidate_cells
            if always_rank or (limit is not None and limit < len(slots)):
                origin = state.position
                slots.sort(
                    key=lambda slot: (
                        -float(state.probability[slot]),
                        state.distance(origin, state.targets[slot]),
                        int(state.targets[slot]),
                    )
                )
            if limit is not None and limit < len(slots):
                slots = slots[:limit]
            state.candidate_count_after = len(slots)
            return slots
        finally:
            elapsed = max(0, ticks_diff(ticks_us(), started))
            self.state.filter_time_us += elapsed
            self.state.filter_invocations += 1
            self.state.candidate_filter_time_us_samples.append(elapsed)

    def collision_rising(self):
        active = bool(self.state.collision_active)
        rising = active and not self.last_collision_active
        self.last_collision_active = active
        return rising

    def score_from(self, encoded_position, slot):
        distance = self.state.distance(encoded_position, self.state.targets[slot])
        return -self.state.adjusted_cost(distance, slot)

    def route_cost(self, path, start=None):
        state = self.state
        previous = state.position if start is None else int(start)
        cost = 0.0
        for slot in path:
            cost += state.adjusted_cost(
                state.distance(previous, state.targets[slot]), slot
            )
            previous = state.targets[slot]
        return cost

    def route_distance(self, path, start=None):
        state = self.state
        previous = state.position if start is None else int(start)
        distance = 0
        for slot in path:
            distance += state.distance(previous, state.targets[slot])
            previous = state.targets[slot]
        return float(distance)

    def best_insertion(self, path, slot):
        base_cost = self.route_cost(path)
        best_index = 0
        best_delta = float("inf")
        for index in range(len(path) + 1):
            candidate = list(path)
            candidate.insert(index, slot)
            delta = max(0.0, self.route_cost(candidate) - base_cost)
            if delta < best_delta - self.EPS:
                best_index = index
                best_delta = delta
            elif abs(delta - best_delta) <= self.EPS and index < best_index:
                best_index = index
        return best_index, best_delta

    def best_distance_insertion(self, path, slot):
        """Return insertion index and pure marginal travel distance."""

        base_distance = self.route_distance(path)
        best_index = 0
        best_delta = float("inf")
        for index in range(len(path) + 1):
            candidate = list(path)
            candidate.insert(index, slot)
            delta = max(
                0.0, self.route_distance(candidate) - base_distance
            )
            if delta < best_delta - self.EPS:
                best_index = index
                best_delta = delta
            elif abs(delta - best_delta) <= self.EPS and index < best_index:
                best_index = index
        return best_index, best_delta

    def owner_wins(self, slot, proposed_value, higher_is_better=True):
        state = self.state
        owner = int(state.claim_owner[slot])
        if owner < 0 or owner == state.robot_index:
            return True
        known = float(state.claim_value[slot])
        if higher_is_better:
            if proposed_value > known + self.EPS:
                return True
        elif proposed_value < known - self.EPS:
            return True
        if abs(proposed_value - known) <= self.EPS:
            return state.robot_index < owner
        return False

    def clean_path(self, require_ownership=False):
        cleaned = []
        for slot in self.path:
            if not self.state.is_active(slot):
                continue
            if require_ownership and self.state.claim_owner[slot] != self.state.robot_index:
                break
            cleaned.append(slot)
        self.path = cleaned

    def release_own_path(self, message_type=None):
        state = self.state
        for slot in self.path:
            if state.claim_owner[slot] == state.robot_index:
                if message_type:
                    state.queue_message(
                        self.claim_message(message_type, slot, -1, self.NO_VALUE, True)
                    )
                state.clear_claim(slot)
        self.path = []

    def claim_message(self, message_type, slot, owner, value, released=False):
        cell = self.state.decode_cell(self.state.targets[slot])
        return {
            "type": message_type,
            "sender": self.state.robot_id,
            "x": int(cell[0]),
            "y": int(cell[1]),
            "winner": self.state.owner_id(owner),
            "owner": self.state.owner_id(owner),
            "value": float(value),
            "bid": float(value),
            "significance": float(value),
            "timestamp": int(self.state.event_counter),
            "released": bool(released),
        }

    def parse_claim_message(self, message, accepted_types, lower_is_better=False):
        if not isinstance(message, dict) or message.get("type") not in accepted_types:
            return False
        if str(message.get("sender")) == self.state.robot_id:
            return False
        try:
            slot = self.state.slot_for_cell((message["x"], message["y"]))
            if slot is None or not self.state.is_active(slot):
                return False
            owner_id = message.get("owner", message.get("winner"))
            owner = self.state.owner_index(owner_id)
            released = bool(message.get("released", False)) or owner < 0
            value = float(
                message.get(
                    "significance",
                    message.get("bid", message.get("value", self.NO_VALUE)),
                )
            )
            epoch = int(message.get("timestamp", message.get("epoch", 0)))
        except (KeyError, TypeError, ValueError):
            return False

        state = self.state
        local_owner = int(state.claim_owner[slot])
        local_value = float(state.claim_value[slot])
        local_epoch = int(state.claim_epoch[slot])
        if released:
            if local_owner == self.state.owner_index(message.get("sender")) or epoch >= local_epoch:
                state.clear_claim(slot)
                return True
            return False

        accept = local_owner < 0 or local_owner == owner
        if not accept:
            if lower_is_better:
                accept = value < local_value - self.EPS
            else:
                accept = value > local_value + self.EPS
            if abs(value - local_value) <= self.EPS:
                accept = owner < local_owner
            if epoch > local_epoch and owner == local_owner:
                accept = True
        if accept:
            state.set_claim(slot, owner, value, epoch)
            return True
        return False

    def goal_cell(self):
        if not self.path:
            self.state.current_goal = None
            return None
        encoded = int(self.state.targets[self.path[0]])
        self.state.current_goal = encoded
        return self.state.decode_cell(encoded)

    def handle_message(self, message):
        return False

    def minimal_state(self):
        return {
            "path": [
                list(self.state.decode_cell(self.state.targets[slot]))
                for slot in self.path
                if self.state.is_active(slot)
            ],
            "call_path": self.last_call_path,
        }

    def export_resume(self):
        return {
            "path": [
                int(self.state.targets[slot])
                for slot in self.path
                if 0 <= slot < len(self.state.targets)
            ],
            "last_collision_active": bool(self.last_collision_active),
            "last_call_path": self.last_call_path,
        }

    def restore_resume(self, resume):
        if not isinstance(resume, dict):
            return
        path = []
        for encoded in resume.get("path", ()):
            slot = self.state.slot_by_cell.get(int(encoded))
            if slot is not None and self.state.is_active(slot):
                path.append(slot)
        self.path = path
        self.last_collision_active = bool(
            resume.get("last_collision_active", False)
        )
        self.last_call_path = str(
            resume.get("last_call_path", "restored")
        )
