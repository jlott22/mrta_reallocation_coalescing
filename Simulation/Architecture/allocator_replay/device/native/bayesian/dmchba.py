"""Memory-bounded native DMCHBA assignment.

The original hardware program materialized an ``N x N`` Python matrix.  This
solver computes entries as the Hungarian algorithm requests them and reuses
linear work arrays.  Its memory growth is O(N), while retaining the
matching-by-clone assignment and normalized probability objective.
"""

from array import array

from .scoring import manhattan


PSEUDOTASK_COST = 1000000.0


def _rid_key(value):
    text = str(value)
    try:
        return 0, int(text)
    except Exception:
        return 1, text


class CompactHungarianWorkspace:
    def __init__(self):
        self.capacity = 0
        self.u = array("f")
        self.v = array("f")
        self.minimum = array("f")
        self.match = array("i")
        self.way = array("i")
        self.used = bytearray()
        self.assignment = array("i")

    def ensure(self, matrix_size):
        matrix_size = int(matrix_size)
        if matrix_size <= self.capacity:
            return
        size = matrix_size + 1
        self.capacity = matrix_size
        self.u = array("f", [0.0]) * size
        self.v = array("f", [0.0]) * size
        self.minimum = array("f", [0.0]) * size
        self.match = array("i", [0]) * size
        self.way = array("i", [0]) * size
        self.used = bytearray(size)
        self.assignment = array("i", [-1]) * matrix_size

    def payload_bytes(self):
        """Return the approximate reusable workspace payload."""

        # Five 32-bit arrays of n+1, one 32-bit assignment of n, and used.
        return (5 * len(self.u) + len(self.assignment)) * 4 + len(self.used)


class DMCHBACore:
    """Persistent DMCHBA scoring and virtual Hungarian workspace."""

    def __init__(self, scorer, commitment_horizon=3):
        self.scorer = scorer
        self.commitment_horizon = int(commitment_horizon)
        self.workspace = CompactHungarianWorkspace()
        self.last_matrix_size = 0
        self.last_clones_per_agent = 0

    def assign(self, team_positions, tasks):
        """Return ``robot id -> ordered committed cells``."""

        agents = sorted(team_positions.items(), key=lambda item: _rid_key(item[0]))
        tasks = [_normalize_cell(cell) for cell in tasks]
        tasks = [cell for cell in tasks if cell is not None]
        if not agents or not tasks:
            return {str(rid): [] for rid, _ in agents}

        task_count = len(tasks)
        agent_count = len(agents)
        clones_per_agent = (task_count + agent_count - 1) // agent_count
        matrix_size = clones_per_agent * agent_count
        assignment = self._solve_virtual(
            agents, tasks, clones_per_agent, matrix_size
        )

        assigned = {str(rid): [] for rid, _ in agents}
        for row_index in range(matrix_size):
            column = assignment[row_index]
            if 0 <= column < task_count:
                rid = str(agents[row_index // clones_per_agent][0])
                assigned[rid].append(tasks[column])

        positions = {str(rid): position for rid, position in agents}
        for rid in assigned:
            assigned[rid] = self.order_cells(
                positions[rid], assigned[rid], self.commitment_horizon
            )
        self.last_matrix_size = matrix_size
        self.last_clones_per_agent = clones_per_agent
        return assigned

    def order_cells(self, origin, cells, limit=None):
        remaining = []
        seen = set()
        for cell in cells:
            cell = _normalize_cell(cell)
            if cell is not None and cell not in seen:
                remaining.append(cell)
                seen.add(cell)
        ordered = []
        reference = _normalize_cell(origin) or (0, 0)
        while remaining and (limit is None or len(ordered) < int(limit)):
            best_index = 0
            best_cell = remaining[0]
            best_score = self.scorer.score(
                manhattan(reference, best_cell), best_cell
            )
            for index in range(1, len(remaining)):
                cell = remaining[index]
                score = self.scorer.score(manhattan(reference, cell), cell)
                if score > best_score:
                    best_index = index
                    best_cell = cell
                    best_score = score
                elif score == best_score:
                    best_distance = manhattan(reference, best_cell)
                    distance = manhattan(reference, cell)
                    if distance < best_distance or (
                        distance == best_distance and cell < best_cell
                    ):
                        best_index = index
                        best_cell = cell
                        best_score = score
            ordered.append(best_cell)
            remaining.pop(best_index)
            reference = best_cell
        return ordered

    def _solve_virtual(self, agents, tasks, clones_per_agent, matrix_size):
        workspace = self.workspace
        workspace.ensure(matrix_size)
        u = workspace.u
        v = workspace.v
        minimum = workspace.minimum
        match = workspace.match
        way = workspace.way
        used = workspace.used
        assignment = workspace.assignment
        infinity = float("inf")
        task_count = len(tasks)
        n = matrix_size

        for index in range(n + 1):
            u[index] = 0.0
            v[index] = 0.0
            minimum[index] = infinity
            match[index] = 0
            way[index] = 0
            used[index] = 0
            if index < n:
                assignment[index] = -1

        for row_number in range(1, n + 1):
            match[0] = row_number
            column0 = 0
            for column in range(n + 1):
                minimum[column] = infinity
                used[column] = 0

            while True:
                used[column0] = 1
                matched_row = match[column0]
                row_index = matched_row - 1
                agent_index = row_index // clones_per_agent
                agent_position = agents[agent_index][1]
                delta = infinity
                next_column = 0

                for column in range(1, n + 1):
                    if used[column]:
                        continue
                    task_index = column - 1
                    if task_index < task_count:
                        cell = tasks[task_index]
                        cost = self.scorer.cost(
                            manhattan(agent_position, cell), cell
                        )
                    else:
                        cost = PSEUDOTASK_COST + task_index
                    reduced = cost - u[matched_row] - v[column]
                    if reduced < minimum[column]:
                        minimum[column] = reduced
                        way[column] = column0
                    # Strict comparison plus ascending columns is the stable
                    # native tie breaker and requires no tiny float allocations.
                    if minimum[column] < delta:
                        delta = minimum[column]
                        next_column = column

                for column in range(n + 1):
                    if used[column]:
                        u[match[column]] += delta
                        v[column] -= delta
                    else:
                        minimum[column] -= delta
                column0 = next_column
                if match[column0] == 0:
                    break

            while True:
                previous_column = way[column0]
                match[column0] = match[previous_column]
                column0 = previous_column
                if column0 == 0:
                    break

        for column in range(1, n + 1):
            if match[column] != 0:
                assignment[match[column] - 1] = column - 1
        return assignment


def _normalize_cell(value):
    try:
        return int(value[0]), int(value[1])
    except Exception:
        return None
