"""Complete memory-bounded Bayesian DMCHBA for native Pololu execution.

This is the shared implementation selected by both authoritative HIL and the
future physical adapter.  It preserves the benchmark DMCHBA state machine and
matching-by-clone assignment, but represents controller state in fixed, packed
buffers:

* probabilities and normalized penalties are flat arrays;
* candidate cells are unsigned 16-bit cell IDs;
* the assignment-input signature stores packed candidate bytes rather than a
  tuple object for every cell;
* Hungarian uses reusable O(N) typed workspaces and computes costs on demand.

The inner Hungarian loop uses a deterministic fixed-point form of the shared
normalized objective.  This avoids allocating millions of temporary float
objects on MicroPython.  It is a native numeric representation choice, not a
different allocator strategy.
"""

from array import array

try:
    from time import ticks_diff, ticks_us
except ImportError:  # CPython desktop tests
    from time import perf_counter_ns

    def ticks_us():
        return perf_counter_ns() // 1000

    def ticks_diff(new, old):
        return new - old

try:
    from replay_types import AllocationDecision
except ImportError:  # package import during desktop tests
    from allocator_replay.device.common.replay_types import AllocationDecision


REPLAY_NATIVE_DMCHBA = True


class DMCHBAAllocator:
    """Native, implicit-coordination DMCHBA allocator."""

    name = "DMCHBA"

    COMMITMENT_HORIZON = 3
    PROBABILITY_ALPHA = 8.0

    # One unit is 0.000001 of the normalized benchmark cost. Exact reduced
    # cost ties are resolved by strict comparisons and ascending traversal.
    # This is the stable native tie order and avoids tiny float temporaries in
    # the controller's O(N^3) inner loop.
    COST_SCALE = 1000000
    PSEUDOTASK_COST = 500000000
    HUNGARIAN_INFINITY = 530000000

    def __init__(self, config=None):
        self.config = dict(config or {})
        self.grid_size = max(1, int(self.config.get("grid_size", 19)))
        self.cell_capacity = self.grid_size * self.grid_size
        self.candidate_capacity = self._configured_candidate_capacity(
            self.cell_capacity
        )
        self.team_capacity = self._configured_team_capacity()
        self.matrix_capacity = (
            self.candidate_capacity + self.team_capacity - 1
        )

        self._probability = array(
            "f", [0.0] * self.cell_capacity
        )
        self._penalty = array(
            "I", [0] * self.cell_capacity
        )
        self._searched = bytearray(self.cell_capacity)
        self._valid = bytearray(self.cell_capacity)
        self._candidate_ids = array(
            "H", [0] * self.candidate_capacity
        )
        self._assigned_ids = array(
            "H", [0] * self.candidate_capacity
        )
        self._team_positions = array(
            "H", [0] * self.team_capacity
        )
        self._team_ids = []

        workspace_size = self.matrix_capacity + 1
        self._h_u = array("q", [0] * workspace_size)
        self._h_v = array("q", [0] * workspace_size)
        self._h_minimum = array("q", [0] * workspace_size)
        self._h_match = array("H", [0] * workspace_size)
        self._h_way = array("H", [0] * workspace_size)
        self._h_used = bytearray(workspace_size)
        self._h_assignment = array(
            "h", [-1] * self.matrix_capacity
        )

        self._normalizer = 1.0
        self._position_id = 0
        self._robot_id = "00"
        self._clue_signature = ()
        self._coverage_mode = False
        self._collision_active = False
        self._band_y_min = 0
        self._band_y_max = self.grid_size - 1
        self._commitment_horizon = self.COMMITMENT_HORIZON
        self._candidate_count = 0
        self._candidate_count_before = 0
        self._prepared = False

    # ------------------------------------------------------------------
    # Persistent replay integration
    # ------------------------------------------------------------------

    def replay_snapshot_allocator_attrs(self):
        """Scratch arrays are reconstructed from environment state at setup."""

        return {}

    def replay_restore_allocator_attrs(self, values):
        """Retain native workspaces and ignore legacy simulator scratch."""

        # Historical simulator snapshots can contain its reusable Hungarian
        # arrays. DMCHBA's logical state lives on the robot, so neither those
        # desktop arrays nor an empty native snapshot should replace the
        # constructor-created controller workspaces.
        del values

    def prepare_replay_state(self, robot):
        """Build compact native views outside the timed choose_goal region."""

        grid_size = int(getattr(robot, "grid_size", self.grid_size))
        if grid_size <= 0 or grid_size * grid_size > 65535:
            raise ValueError("DMCHBA grid must fit an unsigned 16-bit cell ID")

        candidate_capacity = self._candidate_limit(robot, grid_size)
        team_entries = self._team_entries(robot, grid_size)
        team_capacity = max(
            self._configured_team_capacity(),
            len(team_entries),
        )
        if (
            grid_size != self.grid_size
            or candidate_capacity != self.candidate_capacity
            or team_capacity > self.team_capacity
        ):
            self._resize(grid_size, candidate_capacity, team_capacity)

        self._robot_id = self._canonical_rid(
            getattr(robot, "rid", "00")
        )
        self._position_id = self._encode_cell(
            getattr(robot, "pos", (0, 0))
        )
        self._load_probabilities(getattr(robot, "target_p", {}))
        self._load_validity(robot)
        self._load_team(team_entries)
        self._clue_signature = self._normalize_clues(
            getattr(robot, "known_clues", ())
        )

        cfg = getattr(robot, "cfg", None)
        self._coverage_mode = (
            str(getattr(cfg, "trial_mode", "clue_search"))
            == "coverage"
        )
        self._collision_active = self._read_collision(robot)
        self._commitment_horizon = int(
            getattr(
                cfg,
                "commitment_horizon",
                self.COMMITMENT_HORIZON,
            )
            or self.COMMITMENT_HORIZON
        )
        if self._commitment_horizon <= 0:
            raise ValueError("commitment_horizon must be positive")
        self._set_band(robot)
        self._prepared = True
        self._compact_replay_searched_view(robot)

    def _compact_replay_searched_view(self, robot):
        """Keep the resident context close to the physical grid footprint.

        The desktop robot exposes one Belief.searched set through three
        properties.  State transfer restores those aliases exactly, but the
        native allocator only needs a cell-indexed searched bitmap between
        calls.  Replacing the coordinate set after it has been consumed
        mirrors the bytearray grid used by the physical Pololu program and
        prevents untimed simulator state from crowding the timed allocator.
        """

        views = getattr(robot, "_views", None)
        if not isinstance(views, dict):
            return
        compact = bytearray(self._searched)
        views["searched"] = compact
        views["local_searched"] = compact
        belief = getattr(robot, "belief", None)
        if belief is not None:
            setattr(belief, "searched", compact)

    def initialize(self, robot):
        # ReplayPersistentRuntime calls prepare_replay_state immediately after
        # initialize.  Keeping initialize allocation-free prevents duplicate
        # setup work.
        del robot

    # ------------------------------------------------------------------
    # Complete allocator interface
    # ------------------------------------------------------------------

    def choose_goal(self, robot):
        if not self._prepared:
            # Direct unit use remains safe.  HIL/physical runtime setup invokes
            # this before the timer, so campaign timings never include it.
            self.prepare_replay_state(robot)
        self._ensure_state(robot)

        if not self._clue_signature and not self._coverage_mode:
            goal = self._next_serpentine_goal()
            mode = "serpentine_pre_clue"
            trigger = None
        else:
            self._drop_invalid_path(robot)
            trigger, candidates_ready, signature = (
                self._post_clue_trigger(robot)
            )
            if trigger is not None:
                if not candidates_ready:
                    self._prepare_candidates(robot)
                    signature = self._assignment_signature()
                self._run_assignment(robot, trigger, signature)
            path = self._path(robot)
            goal = path[0] if path else None
            mode = (
                "dmchba_coverage"
                if self._coverage_mode
                else "dmchba_post_clue"
            )

        return AllocationDecision(
            goal=goal,
            debug={
                "alg": self.name,
                "mode": mode,
                "dmchba_trigger": trigger,
                "dmchba_path_len": len(self._path(robot)),
                "dmchba_assigned_count": int(
                    getattr(robot, "dmchba_last_assigned_count", 0)
                ),
                "dmchba_committed_count": int(
                    getattr(robot, "dmchba_last_committed_count", 0)
                ),
                "dmchba_commitment_horizon": int(
                    self._commitment_horizon
                ),
                "dmchba_candidate_count": int(
                    getattr(robot, "dmchba_last_candidate_count", 0)
                ),
                "dmchba_candidate_count_before_filter": int(
                    getattr(robot, "candidate_count_before_filter", 0)
                ),
                "dmchba_candidate_count_after_filter": int(
                    getattr(robot, "candidate_count_after_filter", 0)
                ),
                "dmchba_max_candidate_cells": int(
                    self.candidate_capacity
                ),
                "dmchba_team_size": int(
                    getattr(robot, "dmchba_last_team_size", 0)
                ),
                "dmchba_matrix_n": int(
                    getattr(robot, "dmchba_last_matrix_n", 0)
                ),
                "dmchba_evaluates_all_candidates": (
                    self.candidate_capacity >= self.cell_capacity
                ),
                "dmchba_allocator_messages": False,
                "dmchba_native_packed": True,
            },
        )

    def handle_message(self, robot, message):
        del robot, message
        return None

    def handle_dmchba_message(self, robot, message):
        del robot, message
        return None

    def on_message(self, robot, message):
        del robot, message
        return None

    def process_message(self, robot, message):
        del robot, message
        return None

    def on_collision_avoidance_activated(self, robot):
        setattr(robot, "collision_avoidance_active", True)

    # ------------------------------------------------------------------
    # Trigger state
    # ------------------------------------------------------------------

    def _ensure_state(self, robot):
        if not hasattr(robot, "dmchba_path"):
            robot.dmchba_path = []
        if not hasattr(robot, "dmchba_clue_signature"):
            robot.dmchba_clue_signature = None
        if not hasattr(robot, "dmchba_last_collision_active"):
            robot.dmchba_last_collision_active = False
        if not hasattr(robot, "dmchba_last_assignment_signature"):
            robot.dmchba_last_assignment_signature = None
        if not hasattr(robot, "dmchba_last_committed_count"):
            robot.dmchba_last_committed_count = len(
                self._path(robot)
            )

    def _post_clue_trigger(self, robot):
        previous_clue = getattr(robot, "dmchba_clue_signature", None)
        if previous_clue is None:
            robot.dmchba_clue_signature = self._clue_signature
            robot.dmchba_path = []
            return "clue_changed", False, None
        if previous_clue != self._clue_signature:
            # Match the benchmark cadence: later clues update the shared
            # belief but do not discard an active commitment.
            robot.dmchba_clue_signature = self._clue_signature

        previous_collision = bool(
            getattr(robot, "dmchba_last_collision_active", False)
        )
        robot.dmchba_last_collision_active = self._collision_active
        if self._collision_active and not previous_collision:
            robot.dmchba_path = []
            return "collision_avoidance", False, None

        if not self._path(robot):
            self._prepare_candidates(robot)
            signature = self._assignment_signature()
            if (
                signature
                != getattr(
                    robot,
                    "dmchba_last_assignment_signature",
                    None,
                )
            ):
                return "path_exhausted", True, signature
            return None, True, signature
        return None, False, None

    # ------------------------------------------------------------------
    # Packed candidate filter and signature
    # ------------------------------------------------------------------

    def _prepare_candidates(self, robot):
        started = ticks_us()
        try:
            capacity = self.candidate_capacity
            retained = 0
            before = 0
            ranked = False
            for cell_id in range(self.cell_capacity):
                if not self._valid[cell_id]:
                    continue
                before += 1
                if retained < capacity:
                    self._candidate_ids[retained] = cell_id
                    retained += 1
                    continue
                if not ranked:
                    self._sort_retained_candidates(retained)
                    ranked = True
                if self._candidate_precedes(
                    cell_id,
                    self._candidate_ids[retained - 1],
                ):
                    index = retained - 1
                    while (
                        index > 0
                        and self._candidate_precedes(
                            cell_id,
                            self._candidate_ids[index - 1],
                        )
                    ):
                        self._candidate_ids[index] = (
                            self._candidate_ids[index - 1]
                        )
                        index -= 1
                    self._candidate_ids[index] = cell_id

            if ranked:
                count = capacity
            else:
                count = retained
            self._candidate_count_before = before
            self._candidate_count = count
            robot.candidate_count_before_filter = int(before)
            robot.candidate_count_after_filter = int(count)
            robot.max_candidate_cells = int(capacity)
            return count
        finally:
            counters = getattr(robot, "counters", None)
            samples = getattr(
                counters,
                "candidate_filter_time_us_samples",
                None,
            )
            if samples is not None:
                samples.append(
                    max(0, int(ticks_diff(ticks_us(), started)))
                )

    def _sort_retained_candidates(self, count):
        for source_index in range(1, count):
            cell_id = self._candidate_ids[source_index]
            index = source_index
            while (
                index > 0
                and self._candidate_precedes(
                    cell_id,
                    self._candidate_ids[index - 1],
                )
            ):
                self._candidate_ids[index] = (
                    self._candidate_ids[index - 1]
                )
                index -= 1
            self._candidate_ids[index] = cell_id

    def _candidate_precedes(self, left_id, right_id):
        left_probability = self._probability[left_id]
        right_probability = self._probability[right_id]
        if left_probability != right_probability:
            return left_probability > right_probability
        left_distance = self._distance_ids(
            self._position_id, left_id
        )
        right_distance = self._distance_ids(
            self._position_id, right_id
        )
        if left_distance != right_distance:
            return left_distance < right_distance
        left_x = left_id % self.grid_size
        right_x = right_id % self.grid_size
        if left_x != right_x:
            return left_x < right_x
        return left_id // self.grid_size < right_id // self.grid_size

    def _assignment_signature(self):
        packed = bytearray(self._candidate_count * 2)
        offset = 0
        for index in range(self._candidate_count):
            cell_id = int(self._candidate_ids[index])
            packed[offset] = cell_id & 255
            packed[offset + 1] = (cell_id >> 8) & 255
            offset += 2
        team = []
        for index, rid in enumerate(self._team_ids):
            team.append((rid, self._decode_cell(self._team_positions[index])))
        return (
            self._clue_signature,
            packed,
            tuple(team),
        )

    # ------------------------------------------------------------------
    # Matching-by-clone Hungarian assignment
    # ------------------------------------------------------------------

    def _run_assignment(self, robot, reason, signature):
        task_count = self._candidate_count
        team_size = len(self._team_ids)
        robot.dmchba_last_reassignment_reason = reason
        robot.dmchba_last_assignment_signature = signature
        robot.dmchba_last_candidate_count = int(task_count)
        robot.dmchba_last_team_size = int(team_size)
        robot.dmchba_last_assigned_count = 0
        robot.dmchba_last_committed_count = 0
        robot.dmchba_last_matrix_n = 0

        if not task_count or not team_size:
            robot.dmchba_path = []
            return

        clones_per_agent = (
            task_count + team_size - 1
        ) // team_size
        matrix_n = clones_per_agent * team_size
        if matrix_n > self.matrix_capacity:
            raise MemoryError(
                "DMCHBA matrix exceeds preallocated native workspace"
            )

        self._solve_virtual(
            task_count,
            clones_per_agent,
            matrix_n,
        )
        assigned_count = 0
        for row_index in range(matrix_n):
            column = int(self._h_assignment[row_index])
            if column < 0 or column >= task_count:
                continue
            agent_index = row_index // clones_per_agent
            if self._team_ids[agent_index] != self._robot_id:
                continue
            self._assigned_ids[assigned_count] = (
                self._candidate_ids[column]
            )
            assigned_count += 1

        path = self._ordered_prefix(assigned_count)
        robot.dmchba_path = path
        robot.dmchba_last_assigned_count = int(assigned_count)
        robot.dmchba_last_committed_count = len(path)
        robot.dmchba_last_matrix_n = int(matrix_n)
        robot.dmchba_clones_per_agent = int(clones_per_agent)
        robot.dmchba_pseudotask_count = int(
            matrix_n - task_count
        )
        robot.dmchba_native_workspace_capacity = int(
            self.matrix_capacity
        )

    def _solve_virtual(self, task_count, clones_per_agent, matrix_n):
        n = matrix_n
        u = self._h_u
        v = self._h_v
        minimum = self._h_minimum
        match = self._h_match
        way = self._h_way
        used = self._h_used
        assignment = self._h_assignment
        infinity = self.HUNGARIAN_INFINITY

        for index in range(n + 1):
            u[index] = 0
            v[index] = 0
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
                way[column] = 0

            while True:
                used[column0] = 1
                matched_row = int(match[column0])
                row_index = matched_row - 1
                agent_index = row_index // clones_per_agent
                delta = infinity
                next_column = 0

                for column in range(1, n + 1):
                    if used[column]:
                        continue
                    task_index = column - 1
                    if task_index < task_count:
                        cost = self._virtual_real_cost(
                            agent_index,
                            task_index,
                        )
                    else:
                        cost = (
                            self.PSEUDOTASK_COST + task_index
                        )
                    reduced = (
                        cost
                        - int(u[matched_row])
                        - int(v[column])
                    )
                    if reduced < minimum[column]:
                        minimum[column] = reduced
                        way[column] = column0
                    if minimum[column] < delta:
                        delta = int(minimum[column])
                        next_column = column

                for column in range(n + 1):
                    if used[column]:
                        matched = int(match[column])
                        u[matched] = int(u[matched]) + delta
                        v[column] = int(v[column]) - delta
                    else:
                        minimum[column] = (
                            int(minimum[column]) - delta
                        )
                column0 = next_column
                if match[column0] == 0:
                    break

            while True:
                previous_column = int(way[column0])
                match[column0] = match[previous_column]
                column0 = previous_column
                if column0 == 0:
                    break

        for column in range(1, n + 1):
            matched = int(match[column])
            if matched:
                assignment[matched - 1] = column - 1

    def _virtual_real_cost(self, agent_index, task_index):
        """Return the fixed primary cost (exposed for parity tests)."""

        cell_id = int(self._candidate_ids[task_index])
        distance = self._distance_ids(
            int(self._team_positions[agent_index]),
            cell_id,
        )
        return (
            distance * self.COST_SCALE
            + int(self._penalty[cell_id])
        )

    def _ordered_prefix(self, assigned_count):
        ordered = []
        reference = self._position_id
        remaining = assigned_count
        while (
            remaining > 0
            and len(ordered) < self._commitment_horizon
        ):
            best_index = 0
            best_id = int(self._assigned_ids[0])
            best_cost = self._route_cell_cost(reference, best_id)
            for index in range(1, remaining):
                cell_id = int(self._assigned_ids[index])
                cost = self._route_cell_cost(reference, cell_id)
                if cost < best_cost:
                    best_index = index
                    best_id = cell_id
                    best_cost = cost
                elif cost == best_cost:
                    distance = self._distance_ids(
                        reference, cell_id
                    )
                    best_distance = self._distance_ids(
                        reference, best_id
                    )
                    if (
                        distance < best_distance
                        or (
                            distance == best_distance
                            and self._cell_xy_precedes(
                                cell_id, best_id
                            )
                        )
                    ):
                        best_index = index
                        best_id = cell_id
                        best_cost = cost
            ordered.append(self._decode_cell(best_id))
            reference = best_id
            for index in range(best_index, remaining - 1):
                self._assigned_ids[index] = (
                    self._assigned_ids[index + 1]
                )
            remaining -= 1
        return ordered

    def _route_cell_cost(self, reference, cell_id):
        return (
            self._distance_ids(reference, cell_id)
            * self.COST_SCALE
            + int(self._penalty[cell_id])
        )

    # ------------------------------------------------------------------
    # Untimed compact environment preparation
    # ------------------------------------------------------------------

    def _load_probabilities(self, values):
        for index in range(self.cell_capacity):
            self._probability[index] = 0.0

        if hasattr(values, "items"):
            for cell, probability in values.items():
                try:
                    cell_id = self._encode_cell(cell)
                    self._probability[cell_id] = max(
                        0.0, float(probability)
                    )
                except (TypeError, ValueError, IndexError, KeyError):
                    continue
        else:
            loaded_flat = False
            try:
                if len(values) == self.cell_capacity and (
                    not values
                    or not isinstance(values[0], (list, tuple))
                ):
                    for index in range(self.cell_capacity):
                        self._probability[index] = max(
                            0.0, float(values[index])
                        )
                    loaded_flat = True
            except (TypeError, ValueError, IndexError):
                loaded_flat = False
            if not loaded_flat:
                try:
                    for y, row in enumerate(values):
                        if y >= self.grid_size:
                            break
                        for x, probability in enumerate(row):
                            if x >= self.grid_size:
                                break
                            self._probability[
                                y * self.grid_size + x
                            ] = max(0.0, float(probability))
                except (TypeError, ValueError):
                    pass

        maximum = 0.0
        for value in self._probability:
            if value > maximum:
                maximum = float(value)
        if maximum <= 0.0 or maximum == float("inf"):
            maximum = 1.0
        self._normalizer = maximum
        alpha_scale = self.PROBABILITY_ALPHA * self.COST_SCALE
        for index in range(self.cell_capacity):
            normalized = float(self._probability[index]) / maximum
            if normalized < 0.0:
                normalized = 0.0
            elif normalized > 1.0:
                normalized = 1.0
            self._penalty[index] = int(
                alpha_scale * (1.0 - normalized) + 0.5
            )

    def _load_validity(self, robot):
        for index in range(self.cell_capacity):
            self._searched[index] = 0
            self._valid[index] = 1
        self._mark_collection(
            getattr(robot, "searched", ()),
            self._searched,
        )
        for index in range(self.cell_capacity):
            if self._searched[index]:
                self._valid[index] = 0
        for name in (
            "known_obstacles",
            "obstacles",
            "blocked",
            "blocked_cells",
        ):
            self._mark_collection(
                getattr(robot, name, ()),
                self._valid,
                clear=True,
            )

    def _mark_collection(self, values, target, clear=False):
        mark = 0 if clear else 1
        if values is None:
            return
        if hasattr(values, "items"):
            source = values.items()
            for cell, active in source:
                if not active:
                    continue
                try:
                    target[self._encode_cell(cell)] = mark
                except (TypeError, ValueError, IndexError, KeyError):
                    continue
            return
        try:
            if len(values) == self.grid_size:
                is_grid = True
                for row in values:
                    try:
                        if len(row) != self.grid_size:
                            is_grid = False
                            break
                    except TypeError:
                        is_grid = False
                        break
                if is_grid:
                    for y, row in enumerate(values):
                        offset = y * self.grid_size
                        for x, active in enumerate(row):
                            if active:
                                target[offset + x] = mark
                    return
            if len(values) == self.cell_capacity and (
                not values
                or not isinstance(values[0], (list, tuple, set))
            ):
                for index, active in enumerate(values):
                    if active:
                        target[index] = mark
                return
        except (TypeError, IndexError):
            pass
        try:
            for item in values:
                try:
                    target[self._encode_cell(item)] = mark
                    continue
                except (TypeError, ValueError, IndexError, KeyError):
                    pass
                try:
                    y = len(item)
                except TypeError:
                    continue
                if y == self.grid_size:
                    # Matrix forms are handled above. Reaching this branch
                    # means a malformed mixed collection; ignore it instead of
                    # accidentally mapping every row onto y=0.
                    continue
        except TypeError:
            return

    def _load_team(self, entries):
        self._team_ids = []
        for index, entry in enumerate(entries):
            self._team_ids.append(entry[0])
            self._team_positions[index] = entry[1]

    def _team_entries(self, robot, grid_size):
        own_id = self._canonical_rid(getattr(robot, "rid", "00"))
        positions = {own_id: self._encode_cell_for_size(
            getattr(robot, "pos", (0, 0)), grid_size
        )}
        peers = getattr(robot, "peer_positions", {})
        if hasattr(peers, "items"):
            for rid, cell in peers.items():
                key = self._canonical_rid(rid)
                if key == own_id:
                    continue
                try:
                    positions[key] = self._encode_cell_for_size(
                        cell, grid_size
                    )
                except (TypeError, ValueError, IndexError, KeyError):
                    continue
        entries = list(positions.items())
        entries.sort(key=lambda item: self._rid_key(item[0]))
        return entries

    def _normalize_clues(self, values):
        clues = set()
        for value in values or ():
            try:
                clues.add(self._decode_cell(self._encode_cell(value)))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        return tuple(sorted(clues))

    def _set_band(self, robot):
        cfg = getattr(robot, "cfg", None)
        robot_ids = [
            str(rid)
            for rid in getattr(cfg, "robot_ids", ())
        ]
        rid = str(getattr(robot, "rid", ""))
        if not robot_ids or rid not in robot_ids:
            self._band_y_min = 0
            self._band_y_max = self.grid_size - 1
            return
        robot_count = len(robot_ids)
        index = robot_ids.index(rid)
        rows, extra = divmod(self.grid_size, robot_count)
        start = index * rows + min(index, extra)
        height = rows + (1 if index < extra else 0)
        self._band_y_min = start
        self._band_y_max = start + height - 1

    def _read_collision(self, robot):
        for name in (
            "collision_avoidance_active",
            "avoidance_active",
            "collision_active",
            "blocked_by_collision",
            "collision_blocked",
            "needs_collision_replan",
            "collision_replan",
        ):
            if bool(getattr(robot, name, False)):
                return True
        state = str(getattr(robot, "collision_state", "")).lower()
        return state in (
            "active",
            "avoid",
            "avoiding",
            "blocked",
            "replan",
        )

    def _resize(self, grid_size, candidate_capacity, team_capacity):
        self.grid_size = int(grid_size)
        self.cell_capacity = self.grid_size * self.grid_size
        self.candidate_capacity = max(
            1, min(int(candidate_capacity), self.cell_capacity)
        )
        self.team_capacity = max(1, int(team_capacity))
        self.matrix_capacity = (
            self.candidate_capacity + self.team_capacity - 1
        )
        self._probability = array(
            "f", [0.0] * self.cell_capacity
        )
        self._penalty = array("I", [0] * self.cell_capacity)
        self._searched = bytearray(self.cell_capacity)
        self._valid = bytearray(self.cell_capacity)
        self._candidate_ids = array(
            "H", [0] * self.candidate_capacity
        )
        self._assigned_ids = array(
            "H", [0] * self.candidate_capacity
        )
        self._team_positions = array(
            "H", [0] * self.team_capacity
        )
        size = self.matrix_capacity + 1
        self._h_u = array("q", [0] * size)
        self._h_v = array("q", [0] * size)
        self._h_minimum = array("q", [0] * size)
        self._h_match = array("H", [0] * size)
        self._h_way = array("H", [0] * size)
        self._h_used = bytearray(size)
        self._h_assignment = array(
            "h", [-1] * self.matrix_capacity
        )

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _configured_candidate_capacity(self, cell_capacity):
        value = self.config.get(
            "top_k_cells",
            self.config.get("max_candidate_cells", cell_capacity),
        )
        if value is None or str(value).lower() == "all":
            return cell_capacity
        return max(1, min(int(value), cell_capacity))

    def _configured_team_capacity(self):
        robot_ids = self.config.get("robot_ids")
        if robot_ids:
            return max(1, len(robot_ids))
        return max(1, int(self.config.get("robot_count", 4)))

    def _candidate_limit(self, robot, grid_size):
        cfg = getattr(robot, "cfg", None)
        value = getattr(cfg, "max_candidate_cells", None)
        if value is None:
            value = self.config.get(
                "top_k_cells",
                self.config.get("max_candidate_cells"),
            )
        capacity = grid_size * grid_size
        if value is None or str(value).lower() == "all":
            return capacity
        return max(1, min(int(value), capacity))

    def _path(self, robot):
        result = []
        for value in getattr(robot, "dmchba_path", ()) or ():
            try:
                result.append(
                    self._decode_cell(self._encode_cell(value))
                )
            except (TypeError, ValueError, IndexError, KeyError):
                continue
        return result

    def _drop_invalid_path(self, robot):
        kept = []
        for cell in self._path(robot):
            cell_id = self._encode_cell(cell)
            if self._valid[cell_id]:
                kept.append(cell)
        if kept != getattr(robot, "dmchba_path", []):
            robot.dmchba_path = kept
            robot.dmchba_last_committed_count = len(kept)

    def _next_serpentine_goal(self):
        current_x, current_y = self._decode_cell(self._position_id)
        if current_y < self._band_y_min:
            current_y = self._band_y_min
        elif current_y > self._band_y_max:
            current_y = self._band_y_max
        passed = False
        for y in range(self._band_y_min, self._band_y_max + 1):
            if (y - self._band_y_min) % 2:
                x_values = range(self.grid_size - 1, -1, -1)
            else:
                x_values = range(self.grid_size)
            for x in x_values:
                if not passed:
                    if x == current_x and y == current_y:
                        passed = True
                    continue
                cell_id = y * self.grid_size + x
                if not self._searched[cell_id]:
                    return x, y
        for y in range(self._band_y_min, self._band_y_max + 1):
            if (y - self._band_y_min) % 2:
                x_values = range(self.grid_size - 1, -1, -1)
            else:
                x_values = range(self.grid_size)
            for x in x_values:
                if x == current_x and y == current_y:
                    return None
                cell_id = y * self.grid_size + x
                if not self._searched[cell_id]:
                    return x, y
        return None

    def _encode_cell(self, cell):
        return self._encode_cell_for_size(cell, self.grid_size)

    @staticmethod
    def _encode_cell_for_size(cell, grid_size):
        x = int(cell[0])
        y = int(cell[1])
        if not (0 <= x < grid_size and 0 <= y < grid_size):
            raise ValueError("cell is outside DMCHBA grid")
        return y * grid_size + x

    def _decode_cell(self, cell_id):
        value = int(cell_id)
        return value % self.grid_size, value // self.grid_size

    def _distance_ids(self, first, second):
        first_x = int(first) % self.grid_size
        first_y = int(first) // self.grid_size
        second_x = int(second) % self.grid_size
        second_y = int(second) // self.grid_size
        return abs(first_x - second_x) + abs(first_y - second_y)

    def _cell_xy_precedes(self, left, right):
        left_x = int(left) % self.grid_size
        right_x = int(right) % self.grid_size
        if left_x != right_x:
            return left_x < right_x
        return int(left) // self.grid_size < int(right) // self.grid_size

    @staticmethod
    def _canonical_rid(value):
        text = str(value)
        try:
            number = int(text)
            if 0 <= number <= 9:
                return "0" + str(number)
            return str(number)
        except (TypeError, ValueError):
            return text

    @staticmethod
    def _rid_key(value):
        text = str(value)
        try:
            return 0, int(text)
        except (TypeError, ValueError):
            return 1, text

    def workspace_payload_bytes(self):
        """Return packed scratch payload for structural memory tests."""

        arrays = (
            self._probability,
            self._penalty,
            self._candidate_ids,
            self._assigned_ids,
            self._team_positions,
            self._h_u,
            self._h_v,
            self._h_minimum,
            self._h_match,
            self._h_way,
            self._h_assignment,
        )
        total = sum(len(values) * values.itemsize for values in arrays)
        return (
            total
            + len(self._searched)
            + len(self._valid)
            + len(self._h_used)
        )
