"""Memory-bounded DMCHBA assignment for native collaborative visits."""

from array import array

from .base import NativeAllocatorBase


class DMCHBAAllocator(NativeAllocatorBase):
    name = "DMCHBA"
    PSEUDOTASK_COST = 1.0e9

    def __init__(self, state):
        NativeAllocatorBase.__init__(self, state)
        self.last_assignment_signature = None
        self.last_matrix_size = 0

    def _team(self):
        state = self.state
        return [
            index
            for index in range(len(state.robot_ids))
            if state.peer_position_valid[index]
        ]

    def _signature(self):
        state = self.state
        peers = []
        for index in self._team():
            peers.append((index, int(state.peer_positions[index])))
        return (int(state.task_revision), tuple(peers))

    def choose(self):
        state = self.state
        self.clean_path()
        trigger = None
        if self.collision_rising():
            self.path = []
            trigger = "collision_replan"
        signature = self._signature()
        if not self.path and signature != self.last_assignment_signature:
            trigger = trigger or "path_empty"
        if trigger is not None:
            self._assign()
            self.last_assignment_signature = signature
            self.last_call_path = trigger
        elif self.path:
            self.last_call_path = "committed_path_retained"
        else:
            self.last_call_path = "no_assignment_retained"
        return self.goal_cell()

    def _assign(self):
        state = self.state
        tasks = self.candidates()
        team = self._team()
        if not tasks or not team:
            self.path = []
            self.last_matrix_size = 0
            return

        clones_per_agent = (len(tasks) + len(team) - 1) // len(team)
        rows = []
        for owner in team:
            for clone_index in range(clones_per_agent):
                rows.append(
                    (
                        owner,
                        int(state.peer_positions[owner]),
                        clone_index,
                    )
                )
        matrix_size = len(rows)
        columns = list(tasks)
        while len(columns) < matrix_size:
            columns.append(-1)
        assignment = self._hungarian(rows, columns)
        self.last_matrix_size = matrix_size

        mine = []
        for row_index, column_index in enumerate(assignment):
            if column_index < 0 or column_index >= len(columns):
                continue
            slot = columns[column_index]
            if slot < 0:
                continue
            owner, _, _ = rows[row_index]
            if owner == state.robot_index:
                mine.append(slot)
        self.path = self._nearest_order(mine)[: state.commitment_horizon]

    def _cost(self, rows, columns, row_index, column_index):
        state = self.state
        slot = columns[column_index]
        if slot < 0:
            return self.PSEUDOTASK_COST + column_index
        _, position, clone_index = rows[row_index]
        distance = state.distance(position, state.targets[slot])
        cost = state.adjusted_cost(distance, slot)
        # Deterministic only; it is intentionally too small to change normal
        # distance/reward ordering.
        return cost + 1.0e-7 * (
            int(state.targets[slot]) + clone_index * 0.001 + row_index * 0.000001
        )

    def _hungarian(self, rows, columns):
        """Return row-to-column assignment without materializing the matrix."""

        count = len(rows)
        if count == 0:
            return []
        u = array("f", [0.0] * (count + 1))
        v = array("f", [0.0] * (count + 1))
        matched_row = array("h", [0] * (count + 1))
        way = array("h", [0] * (count + 1))

        for row_number in range(1, count + 1):
            matched_row[0] = row_number
            column0 = 0
            minimum = array("f", [float("inf")] * (count + 1))
            used = bytearray(count + 1)
            while True:
                used[column0] = 1
                active_row = int(matched_row[column0])
                delta = float("inf")
                next_column = 0
                for column in range(1, count + 1):
                    if used[column]:
                        continue
                    reduced = (
                        self._cost(
                            rows,
                            columns,
                            active_row - 1,
                            column - 1,
                        )
                        - float(u[active_row])
                        - float(v[column])
                    )
                    if reduced < float(minimum[column]):
                        minimum[column] = reduced
                        way[column] = column0
                    candidate = float(minimum[column])
                    if candidate < delta:
                        delta = candidate
                        next_column = column
                for column in range(count + 1):
                    if used[column]:
                        matched = int(matched_row[column])
                        u[matched] = float(u[matched]) + delta
                        v[column] = float(v[column]) - delta
                    else:
                        minimum[column] = float(minimum[column]) - delta
                column0 = next_column
                if matched_row[column0] == 0:
                    break
            while True:
                previous = int(way[column0])
                matched_row[column0] = matched_row[previous]
                column0 = previous
                if column0 == 0:
                    break

        assignment = array("h", [-1] * count)
        for column in range(1, count + 1):
            row_number = int(matched_row[column])
            if row_number:
                assignment[row_number - 1] = column - 1
        return assignment

    def _nearest_order(self, slots):
        state = self.state
        remaining = list(slots)
        result = []
        previous = state.position
        while remaining:
            best = min(
                remaining,
                key=lambda slot: (
                    state.adjusted_cost(
                        state.distance(previous, state.targets[slot]), slot
                    ),
                    -float(state.probability[slot]),
                    int(state.targets[slot]),
                ),
            )
            result.append(best)
            remaining.remove(best)
            previous = state.targets[best]
        return result

    def minimal_state(self):
        result = NativeAllocatorBase.minimal_state(self)
        result["matrix_size"] = int(self.last_matrix_size)
        return result

    def export_resume(self):
        result = NativeAllocatorBase.export_resume(self)
        result["last_assignment_signature"] = (
            self.last_assignment_signature
        )
        result["last_matrix_size"] = int(self.last_matrix_size)
        return result

    def restore_resume(self, resume):
        NativeAllocatorBase.restore_resume(self, resume)
        signature = resume.get("last_assignment_signature")
        if isinstance(signature, list):
            peers = signature[1] if len(signature) > 1 else []
            signature = (
                int(signature[0]),
                tuple(tuple(item) for item in peers),
            )
        self.last_assignment_signature = signature
        self.last_matrix_size = int(
            resume.get("last_matrix_size", 0)
        )
