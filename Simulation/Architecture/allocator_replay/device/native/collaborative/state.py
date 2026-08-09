"""Compact persistent trial state for collaborative-visit allocators."""

from array import array

from .compat import NativeRandom


def value_from(source, names, default=None):
    if source is None:
        return default
    for name in names:
        if isinstance(source, dict):
            if name in source:
                return source[name]
        elif hasattr(source, name):
            return getattr(source, name)
    return default


def normalized_robot_id(value):
    return str(value)


class CollaborativeState:
    """State held on one Pololu for the lifetime of one robot trial.

    Cells are encoded as ``y * grid_size + x``.  Target membership and
    consensus values are parallel compact arrays, so the common case stores
    fifty targets without dictionaries of ``(x, y)`` tuples.
    """

    DEFAULT_MAX_TARGETS = 50

    def __init__(self, config, initial_state):
        self.configure(config, initial_state)

    def configure(self, config, initial_state):
        self.grid_size = int(
            value_from(initial_state, ("grid_size",), value_from(config, ("grid_size",), 19))
        )
        if self.grid_size <= 0 or self.grid_size * self.grid_size > 65535:
            raise ValueError("grid_size must fit an unsigned 16-bit encoded cell")

        rid = value_from(
            initial_state,
            ("robot_id", "rid"),
            value_from(config, ("robot_id", "rid"), "00"),
        )
        self.robot_id = normalized_robot_id(rid)
        robot_ids = value_from(
            initial_state,
            ("robot_ids", "team_ids"),
            value_from(config, ("robot_ids", "team_ids"), [self.robot_id]),
        )
        self.robot_ids = [normalized_robot_id(item) for item in robot_ids]
        if self.robot_id not in self.robot_ids:
            self.robot_ids.append(self.robot_id)
        self.robot_ids.sort(key=self.robot_id_key)
        self.robot_index_by_id = {}
        for index, item in enumerate(self.robot_ids):
            self.robot_index_by_id[item] = index
        self.robot_index = self.robot_index_by_id[self.robot_id]

        position = value_from(initial_state, ("pos", "position"), (0, 0))
        self.position = self.encode_cell(position)
        self.peer_positions = array("H", [0] * len(self.robot_ids))
        self.peer_position_valid = bytearray(len(self.robot_ids))
        self.peer_positions[self.robot_index] = self.position
        self.peer_position_valid[self.robot_index] = 1

        raw_targets = value_from(
            initial_state,
            ("active_tasks", "targets", "known_targets", "target_cells"),
            value_from(config, ("active_tasks", "targets", "known_targets", "target_cells"), []),
        )
        encoded_targets = self._normalize_cell_collection(raw_targets)
        encoded_targets.sort()
        unique_targets = []
        previous = -1
        for encoded in encoded_targets:
            if encoded != previous:
                unique_targets.append(encoded)
                previous = encoded

        max_targets = int(
            value_from(config, ("max_targets",), self.DEFAULT_MAX_TARGETS)
        )
        if len(unique_targets) > max_targets:
            raise ValueError("collaborative native runtime target capacity exceeded")

        self.targets = array("H", unique_targets)
        self.slot_by_cell = {}
        for slot, encoded in enumerate(self.targets):
            self.slot_by_cell[int(encoded)] = slot
        count = len(self.targets)
        self.active = bytearray([1] * count)
        self.probability = array("f", [1.0] * count)
        self.claim_owner = array("h", [-1] * count)
        self.claim_value = array("f", [-1.0e18] * count)
        self.claim_epoch = array("I", [0] * count)

        self.max_candidate_cells = value_from(
            config,
            ("max_candidate_cells", "top_k_cells", "candidate_limit"),
            None,
        )
        if isinstance(self.max_candidate_cells, str):
            if self.max_candidate_cells.lower() == "all":
                self.max_candidate_cells = None
            else:
                self.max_candidate_cells = int(self.max_candidate_cells)
        if self.max_candidate_cells is not None:
            self.max_candidate_cells = int(self.max_candidate_cells)
            if self.max_candidate_cells <= 0:
                raise ValueError("max_candidate_cells must be positive or all")

        self.commitment_horizon = int(
            value_from(config, ("commitment_horizon",), 3) or 3
        )
        if self.commitment_horizon <= 0:
            raise ValueError("commitment_horizon must be positive")

        seed = int(value_from(initial_state, ("rng_seed", "seed"), value_from(config, ("rng_seed", "seed"), 1009)))
        seed += self.robot_index
        self.rng = NativeRandom(seed)
        self.collision_active = bool(
            value_from(initial_state, ("collision_active", "collision_avoidance_active"), False)
        )
        self.event_counter = 0
        self.task_revision = 0
        self.outbox = []
        self.filter_time_us = 0
        self.filter_invocations = 0
        self.candidate_filter_time_us_samples = []
        self.candidate_count_before = 0
        self.candidate_count_after = 0
        self.last_event = "trial_reset"
        self.current_goal = None

        self._replace_active(raw_targets, mark_revision=False)
        completed = value_from(
            initial_state,
            ("completed_tasks", "visited_targets", "searched"),
            [],
        )
        self.complete_cells(completed, mark_revision=False)
        self._load_probabilities(value_from(initial_state, ("target_p", "probabilities"), None))
        self.update_peer_positions(
            value_from(initial_state, ("peer_positions", "team_positions"), {})
        )

    @staticmethod
    def robot_id_key(value):
        text = str(value)
        try:
            return (0, int(text))
        except ValueError:
            return (1, text)

    def encode_cell(self, cell):
        if isinstance(cell, dict):
            x = int(cell.get("x"))
            y = int(cell.get("y"))
        elif isinstance(cell, int):
            encoded = int(cell)
            if encoded < 0 or encoded >= self.grid_size * self.grid_size:
                raise ValueError("encoded cell out of bounds")
            return encoded
        else:
            x = int(cell[0])
            y = int(cell[1])
        if x < 0 or y < 0 or x >= self.grid_size or y >= self.grid_size:
            raise ValueError("cell out of bounds")
        return y * self.grid_size + x

    def decode_cell(self, encoded):
        encoded = int(encoded)
        return (encoded % self.grid_size, encoded // self.grid_size)

    def _normalize_cell_collection(self, values):
        if values is None:
            return []
        if isinstance(values, dict):
            # Mapping cells to truth/probability is accepted at the boundary.
            values = [key for key, active in values.items() if active]
        result = []
        for value in values:
            try:
                if isinstance(value, str):
                    clean = value.strip().strip("()[]")
                    if "," in clean:
                        parts = clean.split(",")
                        value = (int(parts[0]), int(parts[1]))
                    else:
                        value = int(clean)
                result.append(self.encode_cell(value))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        return result

    def _replace_active(self, cells, mark_revision=True):
        encoded = self._normalize_cell_collection(cells)
        wanted = set(encoded)
        unknown = [cell for cell in wanted if cell not in self.slot_by_cell]
        if unknown:
            raise ValueError("active task not present in immutable trial target list")
        changed = False
        for slot, cell in enumerate(self.targets):
            next_value = 1 if int(cell) in wanted else 0
            if self.active[slot] != next_value:
                self.active[slot] = next_value
                changed = True
                if not next_value:
                    self.clear_claim(slot)
        if changed and mark_revision:
            self.task_revision += 1
            self.last_event = "active_tasks_replaced"

    def complete_cells(self, cells, mark_revision=True):
        changed = False
        for encoded in self._normalize_cell_collection(cells):
            slot = self.slot_by_cell.get(encoded)
            if slot is not None and self.active[slot]:
                self.active[slot] = 0
                self.clear_claim(slot)
                changed = True
        if changed and mark_revision:
            self.task_revision += 1
            self.last_event = "target_completed"

    def activate_cells(self, cells):
        changed = False
        for encoded in self._normalize_cell_collection(cells):
            slot = self.slot_by_cell.get(encoded)
            if slot is None:
                raise ValueError("activated task not present in immutable trial target list")
            if not self.active[slot]:
                self.active[slot] = 1
                changed = True
        if changed:
            self.task_revision += 1
            self.last_event = "target_activated"

    def _load_probabilities(self, values):
        if values is None:
            return
        if isinstance(values, (list, tuple, array)) and len(values) == len(self.targets):
            for slot, value in enumerate(values):
                self.probability[slot] = max(0.0, float(value))
            return
        if not isinstance(values, dict):
            return
        for raw_cell, raw_probability in values.items():
            try:
                encoded = self.encode_cell(raw_cell)
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            slot = self.slot_by_cell.get(encoded)
            if slot is not None:
                self.probability[slot] = max(0.0, float(raw_probability))

    def update_probabilities(self, values):
        self._load_probabilities(values)
        self.last_event = "probabilities_updated"

    def update_position(self, cell):
        self.position = self.encode_cell(cell)
        self.peer_positions[self.robot_index] = self.position
        self.peer_position_valid[self.robot_index] = 1
        self.last_event = "position_updated"

    def update_peer_positions(self, values):
        if not isinstance(values, dict):
            return
        for rid, cell in values.items():
            index = self.robot_index_by_id.get(normalized_robot_id(rid))
            if index is None:
                continue
            try:
                encoded = self.encode_cell(cell)
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            self.peer_positions[index] = encoded
            self.peer_position_valid[index] = 1
        self.peer_positions[self.robot_index] = self.position
        self.peer_position_valid[self.robot_index] = 1

    def set_collision(self, active):
        self.collision_active = bool(active)
        self.last_event = "collision_updated"

    def active_slots(self):
        return [slot for slot, active in enumerate(self.active) if active]

    def is_active(self, slot):
        return 0 <= int(slot) < len(self.active) and bool(self.active[int(slot)])

    def slot_for_cell(self, cell):
        try:
            return self.slot_by_cell.get(self.encode_cell(cell))
        except (TypeError, ValueError, IndexError, KeyError):
            return None

    def position_for_robot(self, index):
        if 0 <= index < len(self.robot_ids) and self.peer_position_valid[index]:
            return int(self.peer_positions[index])
        return None

    def distance(self, first, second):
        ax, ay = self.decode_cell(first)
        bx, by = self.decode_cell(second)
        return abs(ax - bx) + abs(ay - by)

    def normalized_probability(self, slot):
        maximum = 0.0
        for index, active in enumerate(self.active):
            if active and self.probability[index] > maximum:
                maximum = float(self.probability[index])
        if maximum <= 0.0:
            maximum = 1.0
        value = float(self.probability[int(slot)]) / maximum
        if value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return value

    def adjusted_cost(self, distance, slot):
        return float(distance) + 8.0 * (1.0 - self.normalized_probability(slot))

    def clear_claim(self, slot):
        slot = int(slot)
        self.claim_owner[slot] = -1
        self.claim_value[slot] = -1.0e18
        self.claim_epoch[slot] = 0

    def set_claim(self, slot, owner, value, epoch=None):
        slot = int(slot)
        self.claim_owner[slot] = int(owner)
        self.claim_value[slot] = float(value)
        if epoch is None:
            epoch = self.event_counter
        self.claim_epoch[slot] = max(0, int(epoch))

    def owner_index(self, rid):
        return self.robot_index_by_id.get(normalized_robot_id(rid), -1)

    def owner_id(self, index):
        index = int(index)
        if 0 <= index < len(self.robot_ids):
            return self.robot_ids[index]
        return None

    def queue_message(self, message):
        self.outbox.append(message)

    def drain_messages(self):
        messages = self.outbox
        self.outbox = []
        return messages

    def begin_allocator_call(self):
        self.filter_time_us = 0
        self.filter_invocations = 0
        self.candidate_count_before = 0
        self.candidate_count_after = 0
        del self.candidate_filter_time_us_samples[:]
        self.event_counter += 1

    def export_resume(self):
        claims = []
        for slot in range(len(self.targets)):
            owner = int(self.claim_owner[slot])
            if owner >= 0:
                claims.append(
                    [
                        int(self.targets[slot]),
                        self.owner_id(owner),
                        float(self.claim_value[slot]),
                        int(self.claim_epoch[slot]),
                    ]
                )
        peers = []
        for index, rid in enumerate(self.robot_ids):
            if self.peer_position_valid[index]:
                peers.append([rid, int(self.peer_positions[index])])
        return {
            "version": 1,
            "grid_size": int(self.grid_size),
            "robot_id": self.robot_id,
            "robot_ids": list(self.robot_ids),
            "targets": [int(cell) for cell in self.targets],
            "active": [
                int(self.targets[slot]) for slot in self.active_slots()
            ],
            "probability": [
                float(value) for value in self.probability
            ],
            "claims": claims,
            "position": int(self.position),
            "peer_positions": peers,
            "collision_active": bool(self.collision_active),
            "event_counter": int(self.event_counter),
            "task_revision": int(self.task_revision),
            "rng_state": int(self.rng.state),
            "current_goal": self.current_goal,
        }

    def restore_resume(self, resume):
        if not isinstance(resume, dict):
            return
        if int(resume.get("version", 0)) != 1:
            raise ValueError("unsupported collaborative resume version")
        if int(resume.get("grid_size", self.grid_size)) != self.grid_size:
            raise ValueError("collaborative resume grid size mismatch")
        expected_targets = [int(cell) for cell in self.targets]
        actual_targets = [int(cell) for cell in resume.get("targets", ())]
        if actual_targets != expected_targets:
            raise ValueError("collaborative resume target set mismatch")

        self._replace_active(resume.get("active", ()), mark_revision=False)
        probabilities = resume.get("probability", ())
        if len(probabilities) == len(self.probability):
            for slot, value in enumerate(probabilities):
                self.probability[slot] = max(0.0, float(value))
        for slot in range(len(self.targets)):
            self.clear_claim(slot)
        for entry in resume.get("claims", ()):
            try:
                encoded, owner_id, value, epoch = entry
                slot = self.slot_by_cell.get(int(encoded))
                owner = self.owner_index(owner_id)
                if slot is not None and owner >= 0 and self.active[slot]:
                    self.set_claim(slot, owner, value, epoch)
            except (TypeError, ValueError):
                continue

        try:
            self.position = self.encode_cell(int(resume["position"]))
        except (KeyError, TypeError, ValueError):
            pass
        self.peer_position_valid = bytearray(len(self.robot_ids))
        for rid, encoded in resume.get("peer_positions", ()):
            index = self.owner_index(rid)
            if index >= 0:
                try:
                    self.peer_positions[index] = self.encode_cell(int(encoded))
                    self.peer_position_valid[index] = 1
                except (TypeError, ValueError):
                    pass
        self.peer_positions[self.robot_index] = self.position
        self.peer_position_valid[self.robot_index] = 1
        self.collision_active = bool(
            resume.get("collision_active", self.collision_active)
        )
        self.event_counter = int(
            resume.get("event_counter", self.event_counter)
        )
        self.task_revision = int(
            resume.get("task_revision", self.task_revision)
        )
        self.rng.state = int(
            resume.get("rng_state", self.rng.state)
        ) & 0xFFFFFFFF
        goal = resume.get("current_goal")
        self.current_goal = None if goal is None else self.encode_cell(int(goal))

    def snapshot_minimal(self):
        active_cells = []
        for slot in self.active_slots():
            active_cells.append(list(self.decode_cell(self.targets[slot])))
        peer_positions = {}
        for index, rid in enumerate(self.robot_ids):
            if self.peer_position_valid[index]:
                peer_positions[rid] = list(self.decode_cell(self.peer_positions[index]))
        return {
            "robot_id": self.robot_id,
            "position": list(self.decode_cell(self.position)),
            "active_tasks": active_cells,
            "active_task_count": len(active_cells),
            "peer_positions": peer_positions,
            "task_revision": int(self.task_revision),
            "rng_state": int(self.rng.state),
            "current_goal": None
            if self.current_goal is None
            else list(self.decode_cell(self.current_goal)),
        }
