"""Full 30-population, 25-generation native collaborative DGA."""

from .base import NativeAllocatorBase


def copy_plan(plan):
    return [list(route) for route in plan]


class DGAAllocator(NativeAllocatorBase):
    name = "DGA"
    POPULATION_SIZE = 30
    ITERATIONS_PER_TRIGGER = 25
    ELITE_COUNT = 2
    CROSSOVER_RATE = 0.7
    MUTATION_RATE = 0.3
    MIN_SUM_TIE_WEIGHT = 0.05

    def __init__(self, state):
        NativeAllocatorBase.__init__(self, state)
        self.population = []
        self.received_pool = []
        self.received_entries = {}
        self.received_better = False
        self.best_plan = []
        self.best_fitness = float("inf")
        self.generation = 0
        self.solution_counter = 0

    def _team(self):
        state = self.state
        return [
            index
            for index in range(len(state.robot_ids))
            if state.peer_position_valid[index]
        ]

    def choose(self):
        state = self.state
        self.clean_path()
        trigger = None
        if self.collision_rising():
            self.path = []
            trigger = "collision_replan"
        elif self.received_better:
            trigger = "received_better_solution"
        elif not self.path:
            trigger = "path_empty"

        if trigger is not None:
            self._run_search(trigger)
            self.received_better = False
            self.last_call_path = trigger
        else:
            self.last_call_path = "committed_path_retained"
        return self.goal_cell()

    def _run_search(self, trigger):
        state = self.state
        candidates = self.candidates(always_rank=True)
        team = self._team()
        if not candidates or not team:
            self.path = []
            self.best_plan = [[] for _ in state.robot_ids]
            self.best_fitness = float("inf")
            return

        population = self._prepare_population(team, candidates)
        for _ in range(self.ITERATIONS_PER_TRIGGER):
            population = self._next_generation(population, team, candidates)
            self.generation += 1
        ranked = self._rank_population(population, team, candidates)
        self.best_plan = copy_plan(ranked[0][0])
        self.best_fitness = float(ranked[0][1])
        self.path = list(self.best_plan[state.robot_index])[
            : state.commitment_horizon
        ]
        self.population = [
            copy_plan(plan)
            for plan, _ in ranked[: self.POPULATION_SIZE]
        ]
        self._queue_plan_messages(trigger)

    def _blank_plan(self):
        return [[] for _ in self.state.robot_ids]

    def _prepare_population(self, team, candidates):
        population = []
        for plan in self.population + self.received_pool:
            repaired = self._repair_plan(plan, team, candidates)
            if repaired is not None:
                population.append(repaired)
        population.append(self._greedy_seed(team, candidates))
        current = self._current_path_seed(team, candidates)
        if current is not None:
            population.append(current)
        while len(population) < self.POPULATION_SIZE:
            population.append(self._random_balanced_seed(team, candidates))
        ranked = self._rank_population(population, team, candidates)
        return [
            copy_plan(plan)
            for plan, _ in ranked[: self.POPULATION_SIZE]
        ]

    def _next_generation(self, population, team, candidates):
        ranked = self._rank_population(population, team, candidates)
        next_population = [
            copy_plan(plan)
            for plan, _ in ranked[: min(self.ELITE_COUNT, len(ranked))]
        ]
        rng = self.state.rng
        while len(next_population) < self.POPULATION_SIZE:
            parent_a = self._tournament_select(ranked)
            parent_b = self._tournament_select(ranked)
            if rng.random() < self.CROSSOVER_RATE:
                child = self._crossover(parent_a, parent_b, team, candidates)
            else:
                child = copy_plan(parent_a)
            if rng.random() < self.MUTATION_RATE:
                child = self._mutate(child, team, candidates)
            repaired = self._repair_plan(child, team, candidates)
            if repaired is not None:
                next_population.append(repaired)
        return next_population

    def _rank_population(self, population, team, candidates):
        scored = []
        for plan in population:
            repaired = self._repair_plan(plan, team, candidates)
            if repaired is None:
                continue
            scored.append((repaired, self._fitness(repaired, team)))
        if not scored:
            seed = self._greedy_seed(team, candidates)
            scored.append((seed, self._fitness(seed, team)))
        scored.sort(key=lambda item: (item[1], self._signature(item[0])))
        return scored

    def _fitness(self, plan, team):
        costs = []
        state = self.state
        for owner in team:
            costs.append(
                self.route_cost(
                    plan[owner], start=state.peer_positions[owner]
                )
            )
        if not costs:
            return float("inf")
        return max(costs) + self.MIN_SUM_TIE_WEIGHT * sum(costs)

    def _greedy_seed(self, team, candidates):
        state = self.state
        plan = self._blank_plan()
        for slot in candidates:
            owner = min(
                team,
                key=lambda index: (
                    self._append_cost(
                        state.peer_positions[index], plan[index], slot
                    ),
                    len(plan[index]),
                    state.robot_id_key(state.robot_ids[index]),
                ),
            )
            plan[owner].append(slot)
        return self._nearest_order(plan, team)

    def _random_balanced_seed(self, team, candidates):
        cells = list(candidates)
        self.state.rng.shuffle(cells)
        ordered_team = sorted(
            team,
            key=lambda index: self.state.robot_id_key(
                self.state.robot_ids[index]
            ),
        )
        plan = self._blank_plan()
        for index, slot in enumerate(cells):
            plan[ordered_team[index % len(ordered_team)]].append(slot)
        return self._nearest_order(plan, team)

    def _current_path_seed(self, team, candidates):
        state = self.state
        current = [slot for slot in self.path if slot in candidates]
        if not current or state.robot_index not in team:
            return None
        plan = self._greedy_seed(team, candidates)
        used = set(current)
        plan[state.robot_index] = current + [
            slot
            for slot in plan[state.robot_index]
            if slot not in used
        ]
        for owner in team:
            if owner != state.robot_index:
                plan[owner] = [
                    slot for slot in plan[owner] if slot not in used
                ]
        return self._repair_plan(plan, team, candidates)

    def _crossover(self, parent_a, parent_b, team, candidates):
        rng = self.state.rng
        child = self._blank_plan()
        for owner in team:
            route_a = parent_a[owner]
            route_b = parent_b[owner]
            if not route_a:
                child[owner].extend(route_b[: len(route_b) // 2])
                continue
            if not route_b:
                child[owner].extend(route_a[: len(route_a) // 2])
                continue
            a_start = rng.randrange(len(route_a))
            a_end = rng.randrange(a_start + 1, len(route_a) + 1)
            b_start = rng.randrange(len(route_b))
            b_end = rng.randrange(b_start + 1, len(route_b) + 1)
            child[owner].extend(route_a[a_start:a_end])
            child[owner].extend(route_b[b_start:b_end])
        return self._repair_plan(child, team, candidates) or child

    def _mutate(self, plan, team, candidates, operation=None):
        """Apply the simulator's five practical mutation families.

        ``operation`` is an optional deterministic test hook and is not used by
        campaign code.
        """

        rng = self.state.rng
        mutated = copy_plan(plan)
        if not team:
            return mutated
        operation = operation or rng.choice(
            ("move", "swap", "reinsert", "reverse", "clean")
        )
        if operation == "move":
            sources = [owner for owner in team if mutated[owner]]
            if sources:
                source = rng.choice(sources)
                destination = rng.choice(team)
                slot = mutated[source].pop(
                    rng.randrange(len(mutated[source]))
                )
                index = rng.randrange(len(mutated[destination]) + 1)
                mutated[destination].insert(index, slot)
        elif operation == "swap":
            sources = [owner for owner in team if mutated[owner]]
            if len(sources) >= 2:
                owners = rng.sample(sources, 2)
                first = owners[0]
                second = owners[1]
                first_index = rng.randrange(len(mutated[first]))
                second_index = rng.randrange(len(mutated[second]))
                mutated[first][first_index], mutated[second][second_index] = (
                    mutated[second][second_index],
                    mutated[first][first_index],
                )
        elif operation == "reinsert":
            owner = rng.choice(team)
            if len(mutated[owner]) >= 2:
                slot = mutated[owner].pop(
                    rng.randrange(len(mutated[owner]))
                )
                mutated[owner].insert(
                    rng.randrange(len(mutated[owner]) + 1), slot
                )
        elif operation == "reverse":
            owner = rng.choice(team)
            route = mutated[owner]
            if len(route) >= 3:
                start = rng.randrange(0, len(route) - 1)
                end = rng.randrange(start + 2, len(route) + 1)
                segment = list(route[start:end])
                segment.reverse()
                # MicroPython requires a concrete list for slice assignment.
                route[start:end] = segment
        elif operation == "clean":
            for owner in team:
                mutated[owner] = [
                    slot
                    for slot in mutated[owner]
                    if self.state.is_active(slot)
                ]
        else:
            raise ValueError("unknown DGA mutation operation")
        return self._repair_plan(mutated, team, candidates) or mutated

    def _repair_plan(self, plan, team, candidates):
        if not isinstance(plan, list) or len(plan) != len(self.state.robot_ids):
            return None
        candidate_set = set(candidates)
        repaired = self._blank_plan()
        seen = set()
        for owner in team:
            route = plan[owner] if isinstance(plan[owner], list) else []
            for slot in route:
                if (
                    isinstance(slot, int)
                    and slot in candidate_set
                    and slot not in seen
                    and self.state.is_active(slot)
                ):
                    repaired[owner].append(slot)
                    seen.add(slot)
        for slot in candidates:
            if slot in seen:
                continue
            owner = min(
                team,
                key=lambda index: (
                    self._append_cost(
                        self.state.peer_positions[index],
                        repaired[index],
                        slot,
                    ),
                    len(repaired[index]),
                    self.state.robot_id_key(
                        self.state.robot_ids[index]
                    ),
                ),
            )
            repaired[owner].append(slot)
            seen.add(slot)
        return repaired

    def _nearest_order(self, plan, team):
        state = self.state
        ordered = self._blank_plan()
        for owner in team:
            remaining = list(plan[owner])
            previous = int(state.peer_positions[owner])
            while remaining:
                slot = min(
                    remaining,
                    key=lambda candidate: (
                        state.adjusted_cost(
                            state.distance(
                                previous, state.targets[candidate]
                            ),
                            candidate,
                        ),
                        -float(state.probability[candidate]),
                        int(state.targets[candidate]),
                    ),
                )
                ordered[owner].append(slot)
                remaining.remove(slot)
                previous = state.targets[slot]
        return ordered

    def _append_cost(self, start, route, slot):
        previous = self.state.targets[route[-1]] if route else start
        return self.state.adjusted_cost(
            self.state.distance(previous, self.state.targets[slot]), slot
        )

    def _tournament_select(self, ranked):
        count = min(3, len(ranked))
        contenders = self.state.rng.sample(ranked, count)
        contenders.sort(
            key=lambda item: (item[1], self._signature(item[0]))
        )
        return copy_plan(contenders[0][0])

    def _signature(self, plan):
        return tuple(tuple(route) for route in plan)

    def _queue_plan_messages(self, trigger):
        state = self.state
        self.solution_counter += 1
        solution_id = "%s-%s-%s" % (
            state.robot_id,
            self.generation,
            self.solution_counter,
        )
        for owner in self._team():
            prefix = self.best_plan[owner][: state.commitment_horizon]
            if not prefix:
                state.queue_message(
                    {
                        "type": "dga_entry",
                        "sender": state.robot_id,
                        "solution_id": solution_id,
                        "generation": int(self.generation),
                        "fitness": float(self.best_fitness),
                        "owner": state.robot_ids[owner],
                        "order": 0,
                        "path_size": 0,
                        "removed": True,
                        "trigger": trigger,
                    }
                )
                continue
            for order, slot in enumerate(prefix):
                cell = state.decode_cell(state.targets[slot])
                state.queue_message(
                    {
                        "type": "dga_entry",
                        "sender": state.robot_id,
                        "solution_id": solution_id,
                        "generation": int(self.generation),
                        "fitness": float(self.best_fitness),
                        "owner": state.robot_ids[owner],
                        "order": int(order),
                        "path_size": len(prefix),
                        "x": int(cell[0]),
                        "y": int(cell[1]),
                        "removed": False,
                        "trigger": trigger,
                    }
                )

    def handle_message(self, message):
        if (
            not isinstance(message, dict)
            or message.get("type") != "dga_entry"
            or str(message.get("sender")) == self.state.robot_id
        ):
            return False
        try:
            sender = str(message["sender"])
            solution_id = str(message["solution_id"])
            owner_id = str(message["owner"])
            owner = self.state.owner_index(owner_id)
            if owner < 0:
                return False
            path_size = int(message.get("path_size", 0))
            order = int(message.get("order", 0))
            fitness = float(message.get("fitness", float("inf")))
            generation = int(message.get("generation", 0))
            removed = bool(message.get("removed", False))
        except (KeyError, TypeError, ValueError):
            return False

        key = (sender, solution_id)
        entry = self.received_entries.get(key)
        if entry is None:
            entry = {
                "fitness": fitness,
                "generation": generation,
                "routes": {},
                "sizes": {},
            }
            self.received_entries[key] = entry
        entry["sizes"][owner] = path_size
        if removed or path_size == 0:
            entry["routes"][owner] = []
        else:
            try:
                slot = self.state.slot_for_cell((message["x"], message["y"]))
            except (KeyError, TypeError, ValueError):
                return False
            if slot is None:
                return False
            route = entry["routes"].get(owner)
            if route is None or len(route) != path_size:
                route = [-1] * path_size
                entry["routes"][owner] = route
            if 0 <= order < path_size:
                route[order] = slot

        team = self._team()
        complete = True
        for team_owner in team:
            route = entry["routes"].get(team_owner)
            expected = entry["sizes"].get(team_owner)
            if route is None or expected is None:
                complete = False
                break
            if expected and (-1 in route):
                complete = False
                break
        if not complete:
            return True

        plan = self._blank_plan()
        for team_owner in team:
            plan[team_owner] = list(entry["routes"].get(team_owner, []))
        candidates = self.state.active_slots()
        repaired = self._repair_plan(plan, team, candidates)
        if repaired is not None:
            self.received_pool.append(repaired)
            self.received_pool = self.received_pool[-self.POPULATION_SIZE :]
            if fitness < self.best_fitness - self.EPS:
                self.received_better = True
        self.received_entries.pop(key, None)
        return True

    def minimal_state(self):
        result = NativeAllocatorBase.minimal_state(self)
        result.update(
            {
                "population_size": len(self.population),
                "iterations_per_trigger": self.ITERATIONS_PER_TRIGGER,
                "generation": int(self.generation),
                "best_fitness": float(self.best_fitness),
            }
        )
        return result

    def _plan_to_cells(self, plan):
        result = []
        for route in plan:
            result.append(
                [
                    int(self.state.targets[slot])
                    for slot in route
                    if 0 <= slot < len(self.state.targets)
                ]
            )
        return result

    def _plan_from_cells(self, plan):
        if not isinstance(plan, list):
            return None
        result = self._blank_plan()
        for owner in range(min(len(plan), len(result))):
            for encoded in plan[owner]:
                slot = self.state.slot_by_cell.get(int(encoded))
                if slot is not None:
                    result[owner].append(slot)
        return result

    def export_resume(self):
        result = NativeAllocatorBase.export_resume(self)
        result.update(
            {
                "population": [
                    self._plan_to_cells(plan) for plan in self.population
                ],
                "received_pool": [
                    self._plan_to_cells(plan)
                    for plan in self.received_pool
                ],
                "received_better": bool(self.received_better),
                "best_plan": self._plan_to_cells(self.best_plan)
                if self.best_plan
                else [],
                "best_fitness": None
                if self.best_fitness == float("inf")
                else float(self.best_fitness),
                "generation": int(self.generation),
                "solution_counter": int(self.solution_counter),
            }
        )
        return result

    def restore_resume(self, resume):
        NativeAllocatorBase.restore_resume(self, resume)
        self.population = []
        for raw_plan in resume.get("population", ()):
            plan = self._plan_from_cells(raw_plan)
            if plan is not None:
                self.population.append(plan)
        self.received_pool = []
        for raw_plan in resume.get("received_pool", ()):
            plan = self._plan_from_cells(raw_plan)
            if plan is not None:
                self.received_pool.append(plan)
        best_plan = self._plan_from_cells(resume.get("best_plan", []))
        self.best_plan = best_plan or []
        best_fitness = resume.get("best_fitness")
        self.best_fitness = (
            float("inf")
            if best_fitness is None
            else float(best_fitness)
        )
        self.received_better = bool(
            resume.get("received_better", False)
        )
        self.generation = int(resume.get("generation", 0))
        self.solution_counter = int(
            resume.get("solution_counter", 0)
        )
        self.received_entries = {}
