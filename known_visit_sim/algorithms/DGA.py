from __future__ import annotations

import math
import random
import hashlib
from copy import deepcopy
from typing import Any, Dict, List, Optional, Set, Tuple

from known_visit_sim.algorithms.base import AllocatorBase, timed_candidate_filter
from known_visit_sim.core.types import AllocationDecision, Cell


class DGAAllocator(AllocatorBase):
    """Known-target allocator behavior and communication hooks."""

    name = "DGA"

    POPULATION_SIZE = 30
    DGA_ITERATIONS_PER_TRIGGER = 25
    COMMITMENT_HORIZON = 3
    MAX_CANDIDATE_CELLS = None
    REWARD_FACTOR = 5.0
    MIN_SUM_TIE_WEIGHT = 0.05
    CROSSOVER_RATE = 0.7
    MUTATION_RATE = 0.3
    ELITE_COUNT = 2

    BAD_PRED_LIMIT = 3
    PREDICTION_TOLERANCE_CELLS = 0
    EPS = 1.0e-9

    def recover_stalled_allocation(self, robot: Any) -> bool:
        """Locally expire peer assignments after the shared stall timeout."""
        self._ensure_dga_state(robot)
        counts = getattr(robot, "dga_bad_prediction_count", {}) or {}
        for peer_id in self._safe_peer_positions(robot):
            peer_key = self._rid_key(peer_id)
            if peer_key != self._rid_key(robot.rid):
                counts[peer_key] = max(int(counts.get(peer_key, 0)), self.BAD_PRED_LIMIT)
        setattr(robot, "dga_bad_prediction_count", counts)
        self._reset_path_state(robot)
        return True

    def choose_goal(self, robot: Any) -> AllocationDecision:
        goal = self.pick_goal(robot)
        mode = "known_visit"
        trigger = getattr(robot, "dga_last_reallocation_trigger", None)

        self._ensure_dga_state(robot)

        return AllocationDecision(
            goal=goal,
            debug={
                "alg": self.name,
                "mode": mode,
                "dga_trigger": trigger,
                "dga_path": self._get_path(robot),
                "dga_best_fitness": getattr(robot, "dga_best_fitness", math.inf),
                "dga_generation": int(getattr(robot, "dga_generation", 0)),
                "dga_population_size": len(getattr(robot, "dga_population", []) or []),
                "dga_iterations_per_trigger": self.DGA_ITERATIONS_PER_TRIGGER,
                "dga_commitment_horizon": self._planning_horizon(robot, self.COMMITMENT_HORIZON),
                "dga_candidate_count": int(getattr(robot, "dga_last_candidate_count", 0)),
                "dga_candidate_count_before_filter": int(getattr(robot, "candidate_count_before_filter", 0)),
                "dga_candidate_count_after_filter": int(getattr(robot, "candidate_count_after_filter", 0)),
                "dga_max_candidate_cells": getattr(robot, "max_candidate_cells", None),
                "dga_team_size": int(getattr(robot, "dga_last_team_size", 1)),
                "dga_pending_snapshot": bool(getattr(robot, "dga_pending_snapshot", False)),
                "dga_pending_deltas": len(getattr(robot, "dga_pending_deltas", []) or []),
            },
        )

    def pick_goal(self, robot: Any) -> Optional[Cell]:
        self._ensure_dga_state(robot)
        self._clear_invalid_or_completed_cells(robot)

        trigger = None
        if self._collision_activation_trigger(robot):
            trigger = "collision_avoidance"
            setattr(robot, "dga_path", [])
        elif bool(getattr(robot, "dga_received_better_solution", False)):
            trigger = "received_better_solution"
        elif not self._get_path(robot):
            trigger = "path_empty"

        setattr(robot, "dga_last_reallocation_trigger", trigger)
        if trigger is not None:
            self._run_dga(robot, trigger)
            setattr(robot, "dga_received_better_solution", False)

        path = self._get_path(robot)
        if not path:
            return None
        return path[0]

    # ------------------------------------------------------------------
    # DGA search
    # ------------------------------------------------------------------

    def _run_dga(self, robot: Any, trigger: str) -> None:
        candidates = self._candidate_cells(robot)
        team_agents = self._dga_team_agents(robot)
        setattr(robot, "dga_last_candidate_count", len(candidates))
        setattr(robot, "dga_last_team_size", len(team_agents))

        if not candidates or not team_agents:
            setattr(robot, "dga_path", [])
            setattr(robot, "dga_best_plan", {self._rid_key(robot.rid): []})
            setattr(robot, "dga_best_fitness", math.inf)
            return

        population = self._prepare_population(robot, team_agents, candidates)
        for _ in range(max(0, int(self.DGA_ITERATIONS_PER_TRIGGER))):
            population = self._next_generation(robot, population, team_agents, candidates)
            setattr(robot, "dga_generation", int(getattr(robot, "dga_generation", 0)) + 1)

        scored = self._rank_population(robot, population, team_agents, candidates)
        best_plan, best_fitness = scored[0]
        self._commit_best_plan(robot, best_plan, best_fitness, trigger)
        setattr(robot, "dga_population", [deepcopy(plan) for plan, _ in scored[: self.POPULATION_SIZE]])

    def _prepare_population(
        self,
        robot: Any,
        team_agents: Dict[str, Cell],
        candidates: List[Cell],
    ) -> List[Dict[str, List[Cell]]]:
        population: List[Dict[str, List[Cell]]] = []
        current = getattr(robot, "dga_population", []) or []
        received = (getattr(robot, "dga_received_solutions", []) or []) + (
            getattr(robot, "dga_received_solution_pool", []) or []
        )

        for plan in current + received:
            repaired = self._repair_plan(robot, plan, team_agents, candidates)
            if repaired is not None:
                population.append(repaired)

        population.append(self._greedy_seed(robot, team_agents, candidates))
        preserved = self._current_path_seed(robot, team_agents, candidates)
        if preserved is not None:
            population.append(preserved)

        while len(population) < max(1, int(self.POPULATION_SIZE)):
            population.append(self._random_balanced_seed(robot, team_agents, candidates))

        ranked = self._rank_population(robot, population, team_agents, candidates)
        return [plan for plan, _ in ranked[: self.POPULATION_SIZE]]

    def _next_generation(
        self,
        robot: Any,
        population: List[Dict[str, List[Cell]]],
        team_agents: Dict[str, Cell],
        candidates: List[Cell],
    ) -> List[Dict[str, List[Cell]]]:
        ranked = self._rank_population(robot, population, team_agents, candidates)
        elite_count = max(0, min(int(self.ELITE_COUNT), len(ranked), int(self.POPULATION_SIZE)))
        next_population = [deepcopy(plan) for plan, _ in ranked[:elite_count]]
        rng = self._rng(robot)

        while len(next_population) < max(1, int(self.POPULATION_SIZE)):
            parent_a = self._tournament_select(robot, ranked)
            parent_b = self._tournament_select(robot, ranked)
            if rng.random() < float(self.CROSSOVER_RATE):
                child = self._crossover(robot, parent_a, parent_b, team_agents, candidates)
            else:
                child = deepcopy(parent_a)

            if rng.random() < float(self.MUTATION_RATE):
                child = self._mutate(robot, child, team_agents, candidates)

            repaired = self._repair_plan(robot, child, team_agents, candidates)
            if repaired is not None:
                next_population.append(repaired)

        return next_population

    def _rank_population(
        self,
        robot: Any,
        population: List[Dict[str, List[Cell]]],
        team_agents: Dict[str, Cell],
        candidates: List[Cell],
    ) -> List[Tuple[Dict[str, List[Cell]], float]]:
        scored: List[Tuple[Dict[str, List[Cell]], float]] = []
        for plan in population:
            repaired = self._repair_plan(robot, plan, team_agents, candidates)
            if repaired is None:
                continue
            scored.append((repaired, self._fitness(robot, repaired, team_agents)))

        if not scored:
            seed = self._greedy_seed(robot, team_agents, candidates)
            scored.append((seed, self._fitness(robot, seed, team_agents)))

        scored.sort(key=lambda item: (item[1], self._plan_signature(item[0])))
        return scored

    def _fitness(self, robot: Any, plan: Dict[str, List[Cell]], team_agents: Dict[str, Cell]) -> float:
        route_costs: List[float] = []
        for rid in sorted(team_agents.keys(), key=self._robot_id_key):
            route_costs.append(self._route_cost(robot, team_agents[rid], plan.get(rid, [])))

        if not route_costs:
            return math.inf

        max_route_cost = max(route_costs)
        total_route_cost = sum(route_costs)
        fitness = max_route_cost + float(self.MIN_SUM_TIE_WEIGHT) * total_route_cost
        if not math.isfinite(fitness):
            return math.inf
        return float(fitness)

    def _route_cost(self, robot: Any, start: Cell, path: List[Cell]) -> float:
        cost = 0.0
        previous = start
        for cell in path:
            distance = self.manhattan(previous[0], previous[1], cell[0], cell[1])
            edge_cost = self._probability_adjusted_cost(robot, distance, cell)
            if math.isfinite(edge_cost):
                cost += edge_cost
            previous = cell
        return float(cost) if math.isfinite(cost) else math.inf

    # ------------------------------------------------------------------
    # Seeds, crossover, mutation, and repair
    # ------------------------------------------------------------------

    def _greedy_seed(
        self,
        robot: Any,
        team_agents: Dict[str, Cell],
        candidates: List[Cell],
    ) -> Dict[str, List[Cell]]:
        plan = {rid: [] for rid in team_agents}
        for cell in candidates:
            best_rid = min(
                team_agents.keys(),
                key=lambda rid: (
                    self._append_cost(robot, team_agents[rid], plan[rid], cell),
                    len(plan[rid]),
                    self._robot_id_key(rid),
                ),
            )
            plan[best_rid].append(cell)
        return self._nearest_neighbor_order(robot, plan, team_agents)

    def _random_balanced_seed(
        self,
        robot: Any,
        team_agents: Dict[str, Cell],
        candidates: List[Cell],
    ) -> Dict[str, List[Cell]]:
        rng = self._rng(robot)
        team_ids = sorted(team_agents.keys(), key=self._robot_id_key)
        cells = list(candidates)
        rng.shuffle(cells)
        plan = {rid: [] for rid in team_agents}
        for index, cell in enumerate(cells):
            plan[team_ids[index % len(team_ids)]].append(cell)
        return self._nearest_neighbor_order(robot, plan, team_agents)

    def _current_path_seed(
        self,
        robot: Any,
        team_agents: Dict[str, Cell],
        candidates: List[Cell],
    ) -> Optional[Dict[str, List[Cell]]]:
        rid_key = self._rid_key(robot.rid)
        if rid_key not in team_agents:
            return None

        current_path = [cell for cell in self._get_path(robot) if cell in set(candidates)]
        if not current_path:
            return None

        plan = self._greedy_seed(robot, team_agents, candidates)
        used = set(current_path)
        plan[rid_key] = current_path + [cell for cell in plan.get(rid_key, []) if cell not in used]
        for peer_id in list(plan.keys()):
            if peer_id == rid_key:
                continue
            plan[peer_id] = [cell for cell in plan[peer_id] if cell not in used]
        return self._repair_plan(robot, plan, team_agents, candidates)

    def _crossover(
        self,
        robot: Any,
        parent_a: Dict[str, List[Cell]],
        parent_b: Dict[str, List[Cell]],
        team_agents: Dict[str, Cell],
        candidates: List[Cell],
    ) -> Dict[str, List[Cell]]:
        rng = self._rng(robot)
        child = {rid: [] for rid in team_agents}

        for rid in sorted(team_agents.keys(), key=self._robot_id_key):
            route_a = list(parent_a.get(rid, []))
            route_b = list(parent_b.get(rid, []))
            if not route_a:
                child[rid].extend(route_b[: len(route_b) // 2])
                continue
            if not route_b:
                child[rid].extend(route_a[: len(route_a) // 2])
                continue

            a_start = rng.randrange(0, len(route_a))
            a_end = rng.randrange(a_start + 1, len(route_a) + 1)
            b_start = rng.randrange(0, len(route_b))
            b_end = rng.randrange(b_start + 1, len(route_b) + 1)
            child[rid].extend(route_a[a_start:a_end])
            child[rid].extend(route_b[b_start:b_end])

        return self._repair_plan(robot, child, team_agents, candidates) or child

    def _mutate(
        self,
        robot: Any,
        plan: Dict[str, List[Cell]],
        team_agents: Dict[str, Cell],
        candidates: List[Cell],
    ) -> Dict[str, List[Cell]]:
        rng = self._rng(robot)
        mutated = deepcopy(plan)
        team_ids = sorted(team_agents.keys(), key=self._robot_id_key)
        if not team_ids:
            return mutated

        op = rng.choice(("move", "swap", "reinsert", "reverse", "clean"))
        if op == "move":
            sources = [rid for rid in team_ids if mutated.get(rid)]
            if sources:
                src = rng.choice(sources)
                dst = rng.choice(team_ids)
                cell = mutated[src].pop(rng.randrange(len(mutated[src])))
                insert_at = rng.randrange(len(mutated.get(dst, [])) + 1)
                mutated.setdefault(dst, []).insert(insert_at, cell)
        elif op == "swap":
            sources = [rid for rid in team_ids if mutated.get(rid)]
            if len(sources) >= 2:
                a, b = rng.sample(sources, 2)
                ia = rng.randrange(len(mutated[a]))
                ib = rng.randrange(len(mutated[b]))
                mutated[a][ia], mutated[b][ib] = mutated[b][ib], mutated[a][ia]
        elif op == "reinsert":
            rid = rng.choice(team_ids)
            if len(mutated.get(rid, [])) >= 2:
                cell = mutated[rid].pop(rng.randrange(len(mutated[rid])))
                mutated[rid].insert(rng.randrange(len(mutated[rid]) + 1), cell)
        elif op == "reverse":
            rid = rng.choice(team_ids)
            route = mutated.get(rid, [])
            if len(route) >= 3:
                start = rng.randrange(0, len(route) - 1)
                end = rng.randrange(start + 2, len(route) + 1)
                route[start:end] = reversed(route[start:end])
        else:
            for rid in team_ids:
                mutated[rid] = [cell for cell in mutated.get(rid, []) if self._valid_task_cell(robot, cell)]

        return self._repair_plan(robot, mutated, team_agents, candidates) or mutated

    def _repair_plan(
        self,
        robot: Any,
        plan: Any,
        team_agents: Dict[str, Cell],
        candidates: List[Cell],
    ) -> Optional[Dict[str, List[Cell]]]:
        if not isinstance(plan, dict):
            return None

        candidate_set = set(candidates)
        repaired: Dict[str, List[Cell]] = {rid: [] for rid in team_agents}
        seen: Set[Cell] = set()

        for rid in sorted(team_agents.keys(), key=self._robot_id_key):
            for raw in plan.get(rid, []) or []:
                cell = self._normalize_cell(raw)
                if cell is None or cell in seen or cell not in candidate_set:
                    continue
                if not self._valid_task_cell(robot, cell):
                    continue
                repaired[rid].append(cell)
                seen.add(cell)

        missing = [cell for cell in candidates if cell not in seen]
        for cell in missing:
            rid = min(
                repaired.keys(),
                key=lambda key: (
                    self._append_cost(robot, team_agents[key], repaired[key], cell),
                    len(repaired[key]),
                    self._robot_id_key(key),
                ),
            )
            repaired[rid].append(cell)
            seen.add(cell)

        return repaired

    def _nearest_neighbor_order(
        self,
        robot: Any,
        plan: Dict[str, List[Cell]],
        team_agents: Dict[str, Cell],
    ) -> Dict[str, List[Cell]]:
        ordered = {rid: [] for rid in team_agents}
        for rid, start in team_agents.items():
            remaining = list(plan.get(rid, []))
            previous = start
            while remaining:
                best = min(
                    remaining,
                    key=lambda cell: (
                        self._edge_cost(robot, previous, cell),
                        -self._target_probability(robot, cell),
                        cell,
                    ),
                )
                ordered[rid].append(best)
                remaining.remove(best)
                previous = best
        return ordered

    def _append_cost(self, robot: Any, start: Cell, route: List[Cell], cell: Cell) -> float:
        previous = route[-1] if route else start
        return self._edge_cost(robot, previous, cell)

    def _edge_cost(self, robot: Any, previous: Cell, cell: Cell) -> float:
        distance = self.manhattan(previous[0], previous[1], cell[0], cell[1])
        return self._probability_adjusted_cost(robot, distance, cell)

    def _tournament_select(
        self,
        robot: Any,
        ranked: List[Tuple[Dict[str, List[Cell]], float]],
        size: int = 3,
    ) -> Dict[str, List[Cell]]:
        rng = self._rng(robot)
        sample_size = max(1, min(int(size), len(ranked)))
        contenders = rng.sample(ranked, sample_size)
        contenders.sort(key=lambda item: (item[1], self._plan_signature(item[0])))
        return deepcopy(contenders[0][0])

    def _commit_best_plan(
        self,
        robot: Any,
        plan: Dict[str, List[Cell]],
        fitness: float,
        trigger: str,
    ) -> None:
        previous_plan = getattr(robot, "dga_best_plan", {}) or {}
        previous_signature = self._plan_signature(previous_plan)
        new_signature = self._plan_signature(plan)
        rid_key = self._rid_key(robot.rid)
        commitment_horizon = self._planning_horizon(robot, self.COMMITMENT_HORIZON)
        new_path = list(plan.get(rid_key, []))[:commitment_horizon]

        setattr(robot, "dga_best_plan", deepcopy(plan))
        setattr(robot, "dga_best_solution", deepcopy(plan))
        setattr(robot, "dga_best_fitness", float(fitness))
        setattr(robot, "dga_path", new_path)
        setattr(robot, "dga_last_reallocation_trigger", trigger)
        if new_signature != previous_signature:
            self._queue_dga_deltas(robot, plan, float(fitness))
            setattr(robot, "dga_last_assignment_signature", new_signature)

        peer_first = {
            str(rid): route[0]
            for rid, route in plan.items()
            if str(rid) != rid_key and route
        }
        setattr(robot, "dga_last_predicted_peer_first_task", peer_first)

    # ------------------------------------------------------------------
    # Candidate cells and local team construction
    # ------------------------------------------------------------------

    @timed_candidate_filter
    def _candidate_cells(self, robot: Any) -> List[Cell]:
        grid_size = self._grid_size(robot)
        origin = self._robot_pos(robot)
        cells: List[Tuple[float, int, Cell]] = []

        for y in range(grid_size):
            for x in range(grid_size):
                cell = (x, y)
                if not self._valid_task_cell(robot, cell):
                    continue
                probability = float(self._target_probability(robot, cell))
                distance = self.manhattan(origin[0], origin[1], x, y)
                cells.append((-probability, distance, cell))

        cells.sort(key=lambda item: (item[0], item[1], item[2]))
        ordered = [cell for _, _, cell in cells]
        return self._filter_candidate_cells(robot, ordered)

    def _dga_team_agents(self, robot: Any) -> Dict[str, Cell]:
        self._ensure_dga_state(robot)
        team: Dict[str, Cell] = {self._rid_key(robot.rid): self._robot_pos(robot)}
        dropped: Set[str] = set()
        bad_counts = getattr(robot, "dga_bad_prediction_count", {}) or {}
        peer_positions = self._safe_peer_positions(robot)

        for peer_id in sorted(str(rid) for rid in peer_positions.keys()):
            if peer_id == self._rid_key(robot.rid):
                continue
            if int(bad_counts.get(peer_id, 0)) >= self.BAD_PRED_LIMIT:
                dropped.add(peer_id)
                continue
            reference = self._peer_reference_cell(peer_id, peer_positions)
            if reference is None:
                dropped.add(peer_id)
                continue
            team[peer_id] = reference

        setattr(robot, "dga_dropped_peers", dropped)
        return team

    def _queue_dga_deltas(self, robot: Any, plan: Dict[str, List[Cell]], fitness: float) -> None:
        """Queue one-cell DGA messages that fully describe each owner prefix.

        DGA messages remain one path position per published message, matching the
        other allocation protocols' cell-level drop granularity. For a new
        solution id, every committed prefix cell is sent, not just positions
        changed from an older solution. If an owner has an empty prefix, one
        explicit clear message is sent so receivers do not carry forward stale
        cells from that owner.
        """

        solution_id = self._solution_id(plan, int(getattr(robot, "dga_generation", 0)), fitness)
        generation = int(getattr(robot, "dga_generation", 0))
        timestamp = self._next_delta_time(robot)
        last_sent = getattr(robot, "dga_last_sent_signatures", {}) or {}
        pending: List[dict] = []

        for owner in sorted(plan.keys(), key=self._robot_id_key):
            commitment_horizon = self._planning_horizon(robot, self.COMMITMENT_HORIZON)
            prefix = self._normalize_cell_list(plan.get(owner, []))[:commitment_horizon]
            sent_key = self._delta_signature_key(solution_id, owner)
            already_sent_for_solution = sent_key in last_sent
            previous_prefix = tuple(last_sent.get(sent_key, tuple()) or tuple())
            path_signature = self._path_signature(prefix)
            if already_sent_for_solution and previous_prefix == path_signature:
                continue

            if not prefix:
                pending.append({
                    "type": "dga_entry",
                    "alg": "DGA",
                    "sender": robot.rid,
                    "solution_id": solution_id,
                    "generation": generation,
                    "fitness": float(fitness),
                    "owner": str(owner),
                    "order": 0,
                    "path_size": 0,
                    "timestamp": float(timestamp),
                    "x": None,
                    "y": None,
                    "removed": True,
                })
                continue

            for order, cell in enumerate(prefix):
                payload = {
                    "type": "dga_entry",
                    "alg": "DGA",
                    "sender": robot.rid,
                    "solution_id": solution_id,
                    "generation": generation,
                    "fitness": float(fitness),
                    "owner": str(owner),
                    "order": int(order),
                    "path_size": int(len(prefix)),
                    "timestamp": float(timestamp),
                    "x": int(cell[0]),
                    "y": int(cell[1]),
                    "removed": False,
                }
                pending.append(payload)

        setattr(robot, "dga_pending_deltas", pending)
        setattr(robot, "dga_pending_snapshot", bool(pending))

    # ------------------------------------------------------------------
    # DGA communication hooks
    # ------------------------------------------------------------------

    def build_dga_messages(self, robot: Any) -> List[dict]:
        self._ensure_dga_state(robot)
        pending = getattr(robot, "dga_pending_deltas", []) or []
        if not pending:
            setattr(robot, "dga_pending_snapshot", False)
            return []

        messages = [dict(message) for message in pending]
        last_sent = getattr(robot, "dga_last_sent_signatures", {}) or {}
        for message in messages:
            key = self._delta_signature_key(message.get("solution_id"), message.get("owner"))
            owner = str(message.get("owner"))
            plan = getattr(robot, "dga_best_plan", {}) or {}
            commitment_horizon = self._planning_horizon(robot, self.COMMITMENT_HORIZON)
            last_sent[key] = self._path_signature(self._normalize_cell_list(plan.get(owner, []))[:commitment_horizon])
        setattr(robot, "dga_last_sent_signatures", last_sent)
        setattr(robot, "dga_pending_snapshot", False)
        setattr(robot, "dga_pending_deltas", [])
        setattr(robot, "dga_last_sent_signature", self._plan_signature(getattr(robot, "dga_best_plan", {}) or {}))
        return messages

    def make_messages(self, robot: Any) -> List[dict]:
        return self.build_dga_messages(robot)

    def get_outbound_messages(self, robot: Any) -> List[dict]:
        return self.build_dga_messages(robot)

    def build_acbba_messages(self, robot: Any) -> List[dict]:
        return self.build_dga_messages(robot)

    def build_cbaa_messages(self, robot: Any) -> List[dict]:
        return self.build_dga_messages(robot)

    def build_dga_message(self, robot: Any) -> List[dict]:
        return self.build_dga_messages(robot)

    def make_message(self, robot: Any) -> List[dict]:
        return self.build_dga_messages(robot)

    def get_outbound_message(self, robot: Any) -> List[dict]:
        return self.build_dga_messages(robot)

    def build_acbba_message(self, robot: Any) -> List[dict]:
        return self.build_dga_messages(robot)

    def build_cbaa_message(self, robot: Any) -> List[dict]:
        return self.build_dga_messages(robot)

    def receive_message(self, robot: Any, message: Any) -> None:
        self.handle_dga_message(robot, message)

    def on_message(self, robot: Any, message: Any) -> None:
        self.handle_dga_message(robot, message)

    def process_message(self, robot: Any, message: Any) -> None:
        self.handle_dga_message(robot, message)

    def handle_dga_message(self, robot: Any, message: Any) -> None:
        if not isinstance(message, dict) or message.get("type") != "dga_entry":
            return
        if self._same_robot_id(message.get("sender"), robot.rid):
            return

        self._ensure_dga_state(robot)
        parsed = self._parse_dga_entry(robot, message)
        if parsed is None:
            return

        sender, solution_id, generation, received_fitness, owner, order, cell, path_size, removed, timestamp = parsed
        if not self._store_received_entry(
            robot,
            sender,
            solution_id,
            generation,
            received_fitness,
            owner,
            order,
            cell,
            path_size,
            removed,
            timestamp,
        ):
            return

        path = self._owner_path_from_received(robot, sender, solution_id, owner)
        self._update_prediction_quality_from_entry(robot, sender, solution_id, owner, path)
        plan = self._reconstruct_received_solution(robot, sender, solution_id)
        if not plan:
            return

        received_fitness = float(message.get("fitness", math.inf))
        local_fitness = float(getattr(robot, "dga_best_fitness", math.inf))
        local_generation = int(getattr(robot, "dga_generation", 0))
        better = received_fitness < local_fitness - self.EPS
        if abs(received_fitness - local_fitness) <= self.EPS:
            better = generation > local_generation

        pool = getattr(robot, "dga_received_solution_pool", []) or []
        pool.append(plan)
        setattr(robot, "dga_received_solution_pool", pool[-self.POPULATION_SIZE :])
        setattr(robot, "dga_received_solutions", list(getattr(robot, "dga_received_solution_pool", []) or []))

        if better:
            rid_key = self._rid_key(robot.rid)
            old_prefix = tuple(self._get_path(robot))
            commitment_horizon = self._planning_horizon(robot, self.COMMITMENT_HORIZON)
            new_prefix = tuple(plan.get(rid_key, [])[:commitment_horizon])
            setattr(robot, "dga_best_plan", deepcopy(plan))
            setattr(robot, "dga_best_solution", deepcopy(plan))
            setattr(robot, "dga_best_fitness", received_fitness)
            setattr(robot, "dga_received_better_solution", True)
            if new_prefix != old_prefix and hasattr(robot, "current_goal"):
                setattr(robot, "current_goal", None)

    def handle_acbba_message(self, robot: Any, message: Any) -> None:
        self.handle_dga_message(robot, message)

    def handle_cbaa_message(self, robot: Any, message: Any) -> None:
        self.handle_dga_message(robot, message)


    def on_collision_avoidance_activated(self, robot: Any) -> bool:
        return True

    def _parse_dga_entry(
        self,
        robot: Any,
        message: Dict[str, Any],
    ) -> Optional[Tuple[str, str, int, float, str, int, Optional[Cell], int, bool, float]]:
        try:
            sender = str(message["sender"])
            solution_id = str(message["solution_id"])
            generation = int(message["generation"])
            fitness = float(message["fitness"])
            owner = str(message["owner"])
            order = int(message["order"])
            path_size = int(message["path_size"])
            removed = bool(message.get("removed", False))
            cell = None if removed else self._normalize_cell((message["x"], message["y"]))
            timestamp = float(message["timestamp"])
        except Exception:
            return None

        if (
            not solution_id
            or order < 0
            or path_size < 0
            or path_size > self._planning_horizon(robot, self.COMMITMENT_HORIZON)
            or not math.isfinite(fitness)
            or not math.isfinite(timestamp)
            or (not removed and cell is None)
        ):
            return None
        return sender, solution_id, generation, fitness, owner, order, cell, path_size, removed, timestamp

    def _store_received_entry(
        self,
        robot: Any,
        sender: str,
        solution_id: str,
        generation: int,
        fitness: float,
        owner: str,
        order: int,
        cell: Optional[Cell],
        path_size: int,
        removed: bool,
        timestamp: float,
    ) -> bool:
        received_entries = getattr(robot, "dga_received_entries", {}) or {}
        sender_entries = received_entries.setdefault(sender, {})
        solution = sender_entries.setdefault(
            solution_id,
            {
                "generation": generation,
                "fitness": fitness,
                "owners": {},
                "path_sizes": {},
                "latest_by_owner_order": {},
            },
        )

        latest = solution.setdefault("latest_by_owner_order", {})
        latest_key = (owner, int(order))
        previous = latest.get(latest_key)
        if previous is not None:
            previous_generation, previous_timestamp = previous
            if generation < previous_generation:
                return False
            if generation == previous_generation and timestamp <= previous_timestamp + self.EPS:
                return False

        solution["generation"] = max(int(solution.get("generation", generation)), generation)
        solution["fitness"] = min(float(solution.get("fitness", fitness)), fitness)
        solution.setdefault("path_sizes", {})[owner] = int(path_size)
        owner_entries = solution.setdefault("owners", {}).setdefault(owner, {})
        if removed or order >= path_size:
            owner_entries.pop(int(order), None)
        elif cell is not None:
            owner_entries[int(order)] = cell
        latest[latest_key] = (generation, timestamp)

        latest_prefix = getattr(robot, "dga_received_latest_owner_prefix", {}) or {}
        sender_prefix = latest_prefix.setdefault(sender, {})
        sender_prefix[owner] = self._owner_path_from_solution(robot, solution, owner)
        setattr(robot, "dga_received_latest_owner_prefix", latest_prefix)
        setattr(robot, "dga_received_entries", received_entries)
        return True

    def _reconstruct_received_solution(self, robot: Any, sender: str, solution_id: str) -> Dict[str, List[Cell]]:
        received_entries = getattr(robot, "dga_received_entries", {}) or {}
        solution = (received_entries.get(sender, {}) or {}).get(solution_id, {})
        owners = solution.get("owners", {}) if isinstance(solution, dict) else {}
        if not isinstance(owners, dict) or not owners:
            return {}

        plan: Dict[str, List[Cell]] = {}
        latest_prefix = (getattr(robot, "dga_received_latest_owner_prefix", {}) or {}).get(sender, {}) or {}
        for owner, route in latest_prefix.items():
            cleaned = [
                cell
                for cell in self._normalize_cell_list(route)
                if self._in_bounds(robot, cell) and not self._is_obstacle(robot, cell)
            ]
            plan[str(owner)] = cleaned

        for owner in owners.keys():
            route = self._owner_path_from_solution(robot, solution, str(owner))
            cleaned = [
                route_cell
                for route_cell in self._normalize_cell_list(route)
                if self._in_bounds(robot, route_cell) and not self._is_obstacle(robot, route_cell)
            ]
            plan[str(owner)] = cleaned
        return plan

    def _owner_path_from_received(self, robot: Any, sender: str, solution_id: str, owner: str) -> List[Cell]:
        received_entries = getattr(robot, "dga_received_entries", {}) or {}
        solution = (received_entries.get(sender, {}) or {}).get(solution_id, {})
        if not isinstance(solution, dict):
            return []
        return self._owner_path_from_solution(robot, solution, owner)

    def _owner_path_from_solution(self, robot: Any, solution: Dict[str, Any], owner: str) -> List[Cell]:
        owners = solution.get("owners", {}) if isinstance(solution, dict) else {}
        owner_entries = owners.get(owner, {}) if isinstance(owners, dict) else {}
        if not isinstance(owner_entries, dict):
            return []

        commitment_horizon = self._planning_horizon(robot, self.COMMITMENT_HORIZON)
        path_size = int((solution.get("path_sizes", {}) or {}).get(owner, commitment_horizon))
        path: List[Cell] = []
        for order in range(max(0, min(path_size, commitment_horizon))):
            cell = owner_entries.get(order)
            if cell is None:
                break
            normalized = self._normalize_cell(cell)
            if normalized is None:
                break
            path.append(normalized)
        return path

    # ------------------------------------------------------------------
    # Prediction quality
    # ------------------------------------------------------------------

    def _update_prediction_quality_from_entry(
        self,
        robot: Any,
        sender: Any,
        solution_id: str,
        owner: str,
        path: List[Cell],
    ) -> None:
        if self._same_robot_id(sender, robot.rid) or owner != self._rid_key(sender):
            return
        sender_key = self._rid_key(sender)
        if not path:
            return
        actual_first = path[0]

        seen = getattr(robot, "dga_seen_peer_plan_signature", {}) or {}
        signature = (str(solution_id), self._path_signature(path))
        if seen.get(sender_key) == signature:
            return
        seen[sender_key] = signature
        setattr(robot, "dga_seen_peer_plan_signature", seen)

        predicted = getattr(robot, "dga_last_predicted_peer_first_task", {}) or {}
        predicted_first = predicted.get(sender_key)
        if predicted_first is None:
            return

        if self.manhattan(predicted_first[0], predicted_first[1], actual_first[0], actual_first[1]) <= self.PREDICTION_TOLERANCE_CELLS:
            self._record_good_prediction(robot, sender_key)
        else:
            self._record_bad_prediction(robot, sender_key)

    def _record_bad_prediction(self, robot: Any, peer_key: str) -> None:
        counts = getattr(robot, "dga_bad_prediction_count", {}) or {}
        counts[peer_key] = int(counts.get(peer_key, 0)) + 1
        setattr(robot, "dga_bad_prediction_count", counts)

    def _record_good_prediction(self, robot: Any, peer_key: str) -> None:
        counts = getattr(robot, "dga_bad_prediction_count", {}) or {}
        current = int(counts.get(peer_key, 0))
        counts[peer_key] = max(0, current - 1)
        setattr(robot, "dga_bad_prediction_count", counts)

    # ------------------------------------------------------------------
    # Local state
    # ------------------------------------------------------------------

    def _ensure_cbaa_state(self, robot: Any) -> None:
        self._ensure_dga_state(robot)

    def _ensure_dga_state(self, robot: Any) -> None:
        self._ensure_path_state(robot)
        defaults = {
            "dga_population": [],
            "dga_received_solutions": [],
            "dga_received_solution_pool": [],
            "dga_received_entries": {},
            "dga_received_latest_owner_prefix": {},
            "dga_bad_prediction_count": {},
            "dga_last_predicted_peer_first_task": {},
            "dga_seen_peer_plan_signature": {},
            "dga_dropped_peers": set(),
            "dga_last_team_size": 1,
            "dga_last_candidate_count": 0,
            "dga_last_collision_active": False,
            "dga_last_reallocation_trigger": None,
            "dga_received_better_solution": False,
            "dga_generation": 0,
            "dga_best_fitness": math.inf,
            "dga_best_plan": {},
            "dga_best_solution": {},
            "dga_last_assignment_signature": None,
        }
        for attr, value in defaults.items():
            if not hasattr(robot, attr) or getattr(robot, attr) is None:
                setattr(robot, attr, deepcopy(value))

    def _ensure_path_state(self, robot: Any) -> None:
        if not hasattr(robot, "dga_path") or getattr(robot, "dga_path") is None:
            setattr(robot, "dga_path", [])
        if not hasattr(robot, "dga_pending_snapshot"):
            setattr(robot, "dga_pending_snapshot", False)
        if not hasattr(robot, "dga_pending_deltas") or getattr(robot, "dga_pending_deltas") is None:
            setattr(robot, "dga_pending_deltas", [])
        if not hasattr(robot, "dga_last_sent_signature"):
            setattr(robot, "dga_last_sent_signature", None)
        if not hasattr(robot, "dga_last_sent_signatures") or getattr(robot, "dga_last_sent_signatures") is None:
            setattr(robot, "dga_last_sent_signatures", {})
        if not hasattr(robot, "dga_delta_counter"):
            setattr(robot, "dga_delta_counter", 0)
        if not hasattr(robot, "dga_rng") or getattr(robot, "dga_rng") is None:
            setattr(robot, "dga_rng", random.Random(self._seed_for_robot(robot)))

    def _reset_cbaa_state(self, robot: Any) -> None:
        self._reset_path_state(robot)

    def _reset_path_state(self, robot: Any) -> None:
        setattr(robot, "dga_path", [])
        setattr(robot, "dga_population", [])
        setattr(robot, "dga_received_solutions", [])
        setattr(robot, "dga_received_solution_pool", [])
        setattr(robot, "dga_received_entries", {})
        setattr(robot, "dga_received_latest_owner_prefix", {})
        setattr(robot, "dga_best_plan", {})
        setattr(robot, "dga_best_solution", {})
        setattr(robot, "dga_best_fitness", math.inf)
        setattr(robot, "dga_pending_snapshot", False)
        setattr(robot, "dga_pending_deltas", [])
        setattr(robot, "dga_last_sent_signature", None)
        setattr(robot, "dga_last_sent_signatures", {})
        setattr(robot, "dga_last_predicted_peer_first_task", {})
        setattr(robot, "dga_seen_peer_plan_signature", {})
        setattr(robot, "dga_dropped_peers", set())
        setattr(robot, "dga_last_team_size", 1)
        setattr(robot, "dga_last_candidate_count", 0)
        setattr(robot, "dga_last_reallocation_trigger", None)
        setattr(robot, "dga_received_better_solution", False)
        setattr(robot, "dga_last_assignment_signature", None)


    def _clear_invalid_or_completed_cells(self, robot: Any) -> None:
        path = [cell for cell in self._get_path(robot) if self._valid_task_cell(robot, cell)]
        if tuple(path) != tuple(self._get_path(robot)):
            setattr(robot, "dga_path", path)

        for attr in ("dga_best_plan",):
            plan = getattr(robot, attr, {}) or {}
            cleaned = {
                str(rid): [cell for cell in self._normalize_cell_list(route) if self._valid_task_cell(robot, cell)]
                for rid, route in plan.items()
            }
            setattr(robot, attr, cleaned)
            if attr == "dga_best_plan":
                setattr(robot, "dga_best_solution", deepcopy(cleaned))

    def _get_path(self, robot: Any) -> List[Cell]:
        self._ensure_path_state(robot)
        return self._normalize_cell_list(getattr(robot, "dga_path", []))

    def _collision_activation_trigger(self, robot: Any) -> bool:
        active = self._collision_active(robot)
        previous = bool(getattr(robot, "dga_last_collision_active", False))
        setattr(robot, "dga_last_collision_active", active)
        return bool(active and not previous)

    def _collision_active(self, robot: Any) -> bool:
        for attr in (
            "collision_avoidance_active",
            "avoidance_active",
            "collision_active",
            "blocked_by_collision",
            "collision_blocked",
            "needs_collision_replan",
            "collision_replan",
        ):
            if bool(getattr(robot, attr, False)):
                return True
        state = str(getattr(robot, "collision_state", "")).lower()
        return state in {"active", "avoid", "avoiding", "blocked", "replan"}

    def _rng(self, robot: Any) -> random.Random:
        self._ensure_path_state(robot)
        return getattr(robot, "dga_rng")

    def _seed_for_robot(self, robot: Any) -> int:
        text = str(getattr(robot, "rid", "0"))
        try:
            return 1009 + int(text)
        except ValueError:
            return 1009 + sum(ord(ch) for ch in text)

    # ------------------------------------------------------------------
    # Serialization and signatures
    # ------------------------------------------------------------------

    def _next_delta_time(self, robot: Any) -> float:
        self._ensure_path_state(robot)
        counter = int(getattr(robot, "dga_delta_counter", 0)) + 1
        setattr(robot, "dga_delta_counter", counter)
        return float(counter)

    def _solution_id(self, plan: Dict[str, List[Cell]], generation: int, fitness: float) -> str:
        payload = repr((int(generation), round(float(fitness), 9), self._plan_signature(plan))).encode("utf-8")
        return hashlib.sha1(payload).hexdigest()[:16]

    def _path_signature(self, path: List[Cell]) -> Tuple[Cell, ...]:
        return tuple(self._normalize_cell_list(path))

    def _delta_signature_key(self, solution_id: Any, owner: Any) -> str:
        return str(owner)

    def _parse_path(self, raw: Any) -> List[Cell]:
        return self._normalize_cell_list(raw if isinstance(raw, list) else [])

    def _serialize_plan(self, plan: Dict[str, List[Cell]]) -> Dict[str, List[Dict[str, int]]]:
        return {
            str(rid): [{"x": int(cell[0]), "y": int(cell[1])} for cell in self._normalize_cell_list(route)]
            for rid, route in sorted(plan.items(), key=lambda item: self._robot_id_key(item[0]))
        }

    def _deserialize_plan(self, raw: Any) -> Dict[str, List[Cell]]:
        if not isinstance(raw, dict):
            return {}
        plan: Dict[str, List[Cell]] = {}
        for rid, route in raw.items():
            plan[str(rid)] = self._normalize_cell_list(route if isinstance(route, list) else [])
        return plan

    def _plan_signature(self, plan: Dict[str, List[Cell]]) -> Tuple[Tuple[str, Tuple[Cell, ...]], ...]:
        if not isinstance(plan, dict):
            return tuple()
        return tuple(
            (str(rid), tuple(self._normalize_cell_list(route)))
            for rid, route in sorted(plan.items(), key=lambda item: self._robot_id_key(item[0]))
        )

    # ------------------------------------------------------------------
    # Generic simulator helpers copied/adapted from existing allocators
    # ------------------------------------------------------------------

    def _normalize_cell(self, value: Any) -> Optional[Cell]:
        try:
            x, y = value
            return int(x), int(y)
        except Exception:
            return None

    def _normalize_cell_list(self, values: Any) -> List[Cell]:
        cells: List[Cell] = []
        if not isinstance(values, list):
            return cells
        for value in values:
            if isinstance(value, dict):
                cell = self._normalize_cell((value.get("x"), value.get("y")))
            else:
                cell = self._normalize_cell(value)
            if cell is not None and cell not in cells:
                cells.append(cell)
        return cells

    def _valid_task_cell(self, robot: Any, cell: Cell) -> bool:
        if cell is None:
            return False
        if not self._is_active_task(robot, cell):
            return False
        if not self._in_bounds(robot, cell):
            return False
        if self._is_searched(robot, cell):
            return False
        if self._is_obstacle(robot, cell):
            return False
        return True


    @staticmethod
    def manhattan(x1: int, y1: int, x2: int, y2: int) -> int:
        return abs(x1 - x2) + abs(y1 - y2)

    def _robot_pos(self, robot: Any) -> Cell:
        return self._normalize_cell(getattr(robot, "pos", (0, 0))) or (0, 0)



    def _target_probability(self, robot: Any, cell: Cell) -> float:
        target_p = getattr(robot, "target_p", {}) or {}
        if isinstance(target_p, dict):
            return float(target_p.get(cell, 0.0))
        idx_fn = getattr(robot, "idx", None)
        if callable(idx_fn):
            try:
                return float(target_p[idx_fn(cell[0], cell[1])])
            except Exception:
                return 0.0
        return 0.0

    def _is_searched(self, robot: Any, cell: Cell) -> bool:
        searched = getattr(robot, "searched", None)
        if searched is None:
            searched = getattr(robot, "local_searched", set())
        return cell in searched

    def _is_obstacle(self, robot: Any, cell: Cell) -> bool:
        for attr in ("known_obstacles", "obstacles", "blocked", "blocked_cells"):
            cells = getattr(robot, attr, None)
            if cells is not None and cell in cells:
                return True
        return False

    def _grid_size(self, robot: Any) -> int:
        grid_size = getattr(robot, "grid_size", None)
        if grid_size is not None:
            return int(grid_size)
        cfg = getattr(robot, "cfg", None)
        return int(getattr(cfg, "grid_size", 19))

    def _in_bounds(self, robot: Any, cell: Cell) -> bool:
        x, y = cell
        grid_size = self._grid_size(robot)
        return 0 <= x < grid_size and 0 <= y < grid_size

    def _safe_peer_positions(self, robot: Any) -> Dict[str, Cell]:
        raw = getattr(robot, "peer_positions", {}) or {}
        return self._normalize_peer_cell_dict(raw)

    def _normalize_peer_cell_dict(self, values: Any) -> Dict[str, Cell]:
        result: Dict[str, Cell] = {}
        if not isinstance(values, dict):
            return result
        for rid, cell in values.items():
            normalized = self._normalize_cell(cell)
            if normalized is not None:
                result[str(rid)] = normalized
        return result

    def _peer_reference_cell(self, peer_id: str, peer_positions: Dict[str, Cell]) -> Optional[Cell]:
        return peer_positions.get(peer_id)

    def _same_robot_id(self, a: Any, b: Any) -> bool:
        return self._robot_id_key(a) == self._robot_id_key(b)

    def _robot_id_less(self, a: Any, b: Any) -> bool:
        return self._robot_id_key(a) < self._robot_id_key(b)

    def _robot_id_key(self, rid: Any) -> Tuple[int, Any]:
        text = str(rid)
        try:
            return 0, int(text)
        except ValueError:
            return 1, text

    def _rid_key(self, rid: Any) -> str:
        return str(rid)


Allocator = DGAAllocator
