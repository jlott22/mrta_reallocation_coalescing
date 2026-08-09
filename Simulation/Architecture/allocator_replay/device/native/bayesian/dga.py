"""Full native DGA search with compact MicroPython-compatible plans."""

from array import array

from .scoring import manhattan


POPULATION_SIZE = 30
ITERATIONS_PER_TRIGGER = 25
ELITE_COUNT = 2
CROSSOVER_RATE = 0.7
MUTATION_RATE = 0.3
MIN_SUM_TIE_WEIGHT = 0.05


class CompactRandom:
    """Small persistent PRNG suitable for one generator per physical robot."""

    def __init__(self, seed=1):
        value = int(seed) & 0xFFFFFFFF
        self.state = value if value else 0x6D2B79F5

    def _next(self):
        value = self.state
        value ^= (value << 13) & 0xFFFFFFFF
        value ^= value >> 17
        value ^= (value << 5) & 0xFFFFFFFF
        self.state = value & 0xFFFFFFFF
        return self.state

    def random(self):
        return self._next() / 4294967296.0

    def randrange(self, start, stop=None):
        if stop is None:
            stop = int(start)
            start = 0
        start = int(start)
        stop = int(stop)
        if stop <= start:
            raise ValueError("empty range")
        return start + self._next() % (stop - start)

    def choice(self, values):
        return values[self.randrange(len(values))]

    def shuffle(self, values):
        for index in range(len(values) - 1, 0, -1):
            selected = self.randrange(index + 1)
            values[index], values[selected] = values[selected], values[index]

    def sample(self, values, count):
        pool = list(values)
        result = []
        for _ in range(int(count)):
            index = self.randrange(len(pool))
            result.append(pool.pop(index))
        return result


def _rid_key(value):
    text = str(value)
    try:
        return 0, int(text)
    except Exception:
        return 1, text


class DGACore:
    """Persistent 30-by-25 DGA using 16-bit encoded target cells.

    Each plan is a list of per-robot ``array('H')`` routes.  At a 19x19 grid,
    one complete 361-cell plan has about 722 bytes of route payload instead of
    hundreds of Python ``(x, y)`` tuples.
    """

    POPULATION_SIZE = POPULATION_SIZE
    ITERATIONS_PER_TRIGGER = ITERATIONS_PER_TRIGGER
    ELITE_COUNT = ELITE_COUNT
    CROSSOVER_RATE = CROSSOVER_RATE
    MUTATION_RATE = MUTATION_RATE
    MIN_SUM_TIE_WEIGHT = MIN_SUM_TIE_WEIGHT

    def __init__(
        self,
        scorer,
        grid_size=19,
        seed=1,
        population_size=POPULATION_SIZE,
        iterations=ITERATIONS_PER_TRIGGER,
    ):
        self.scorer = scorer
        self.grid_size = int(grid_size)
        self.population_size = int(population_size)
        self.iterations = int(iterations)
        self.rng = CompactRandom(seed)
        self.population = []
        self.team_ids = []
        self.generation = 0
        self.best_fitness = float("inf")
        self.best_plan = []
        self.last_mutation = None

    def reset(self, seed=None):
        if seed is not None:
            self.rng = CompactRandom(seed)
        self.population = []
        self.team_ids = []
        self.generation = 0
        self.best_fitness = float("inf")
        self.best_plan = []
        self.last_mutation = None

    def evolve(
        self,
        team_positions,
        candidates,
        own_id=None,
        current_path=None,
        received_plans=None,
    ):
        ids = sorted([str(rid) for rid in team_positions], key=_rid_key)
        positions = [
            _normalize_cell(_mapping_value_by_text_key(team_positions, rid))
            for rid in ids
        ]
        encoded_candidates = self._encode_candidates(candidates)
        if not ids or not encoded_candidates:
            self.population = []
            self.team_ids = ids
            self.best_plan = [array("H") for _ in ids]
            self.best_fitness = 0.0
            return self.result()

        population = self._prepare_population(
            ids,
            positions,
            encoded_candidates,
            own_id,
            current_path,
            received_plans,
        )
        for _ in range(max(0, self.iterations)):
            population = self._next_generation(
                population, positions, encoded_candidates
            )
            self.generation += 1

        ranked = self._rank(population, positions)
        self.population = [
            self._copy_plan(item[1])
            for item in ranked[: self.population_size]
        ]
        self.team_ids = ids
        self.best_fitness = ranked[0][0]
        self.best_plan = self._copy_plan(ranked[0][1])
        return self.result()

    def result(self):
        return {
            "plan": self.decode_plan(self.best_plan),
            "fitness": self.best_fitness,
            "generation": self.generation,
            "population_size": len(self.population),
            "iterations_per_trigger": self.iterations,
        }

    def decode_plan(self, plan):
        decoded = {}
        for index, rid in enumerate(self.team_ids):
            route = plan[index] if index < len(plan) else ()
            decoded[rid] = [self.decode_cell(code) for code in route]
        return decoded

    def encode_cell(self, cell):
        cell = _normalize_cell(cell)
        if cell is None:
            raise ValueError("invalid cell")
        x, y = cell
        if not (0 <= x < self.grid_size and 0 <= y < self.grid_size):
            raise ValueError("cell outside grid")
        return y * self.grid_size + x

    def decode_cell(self, code):
        code = int(code)
        return code % self.grid_size, code // self.grid_size

    def crossover(self, parent_a, parent_b):
        """Apply the simulator's random contiguous-segment crossover."""

        child = [array("H") for _ in self.team_ids]
        for index in range(len(self.team_ids)):
            route_a = parent_a[index]
            route_b = parent_b[index]
            if not route_a:
                child[index].extend(route_b[: len(route_b) // 2])
                continue
            if not route_b:
                child[index].extend(route_a[: len(route_a) // 2])
                continue
            a_start = self.rng.randrange(0, len(route_a))
            a_end = self.rng.randrange(a_start + 1, len(route_a) + 1)
            b_start = self.rng.randrange(0, len(route_b))
            b_end = self.rng.randrange(b_start + 1, len(route_b) + 1)
            child[index].extend(route_a[a_start:a_end])
            child[index].extend(route_b[b_start:b_end])
        return child

    def mutate(self, plan, valid_codes=None, operation=None):
        """Apply one of the simulator's five mutation strategies."""

        mutated = self._copy_plan(plan)
        if not mutated:
            return mutated
        operation = operation or self.rng.choice(
            ("move", "swap", "reinsert", "reverse", "clean")
        )
        self.last_mutation = operation
        populated = [
            index for index in range(len(mutated)) if len(mutated[index])
        ]

        if operation == "move" and populated:
            source = self.rng.choice(populated)
            destination = self.rng.randrange(len(mutated))
            cell = mutated[source].pop(self.rng.randrange(len(mutated[source])))
            insert_at = self.rng.randrange(len(mutated[destination]) + 1)
            mutated[destination].insert(insert_at, cell)
        elif operation == "swap" and len(populated) >= 2:
            first, second = self.rng.sample(populated, 2)
            first_index = self.rng.randrange(len(mutated[first]))
            second_index = self.rng.randrange(len(mutated[second]))
            value = mutated[first][first_index]
            mutated[first][first_index] = mutated[second][second_index]
            mutated[second][second_index] = value
        elif operation == "reinsert":
            route_index = self.rng.randrange(len(mutated))
            route = mutated[route_index]
            if len(route) >= 2:
                cell = route.pop(self.rng.randrange(len(route)))
                route.insert(self.rng.randrange(len(route) + 1), cell)
        elif operation == "reverse":
            route_index = self.rng.randrange(len(mutated))
            route = mutated[route_index]
            if len(route) >= 3:
                start = self.rng.randrange(0, len(route) - 1)
                end = self.rng.randrange(start + 2, len(route) + 1)
                # MicroPython rejects assigning ``reversed(...)`` directly to
                # an array/list slice.  In-place swaps express the same move.
                left = start
                right = end - 1
                while left < right:
                    route[left], route[right] = route[right], route[left]
                    left += 1
                    right -= 1
        elif operation == "clean" and valid_codes is not None:
            for route_index in range(len(mutated)):
                cleaned = array("H")
                for code in mutated[route_index]:
                    if valid_codes[code]:
                        cleaned.append(code)
                mutated[route_index] = cleaned
        return mutated

    def _prepare_population(
        self,
        ids,
        positions,
        candidates,
        own_id,
        current_path,
        received_plans,
    ):
        candidate_mask = self._candidate_mask(candidates)
        source = self._remap_existing(ids)
        if received_plans:
            for plan in received_plans:
                source.append(self._encode_external_plan(plan, ids))
        population = []
        for plan in source:
            population.append(
                self._repair(plan, positions, candidates, candidate_mask)
            )
        population.append(self._greedy_seed(positions, candidates))

        if own_id is not None and current_path:
            preserved = self._current_path_seed(
                ids, positions, candidates, own_id, current_path
            )
            if preserved is not None:
                population.append(preserved)
        while len(population) < max(1, self.population_size):
            population.append(self._random_seed(positions, candidates))
        ranked = self._rank(population, positions)
        self.team_ids = list(ids)
        return [
            self._copy_plan(item[1])
            for item in ranked[: self.population_size]
        ]

    def _next_generation(self, population, positions, candidates):
        ranked = self._rank(population, positions)
        elite_count = min(
            max(0, self.ELITE_COUNT), len(ranked), self.population_size
        )
        result = [
            self._copy_plan(ranked[index][1])
            for index in range(elite_count)
        ]
        candidate_mask = self._candidate_mask(candidates)
        while len(result) < max(1, self.population_size):
            first = self._tournament(ranked)
            second = self._tournament(ranked)
            if self.rng.random() < self.CROSSOVER_RATE:
                child = self.crossover(first, second)
            else:
                child = self._copy_plan(first)
            if self.rng.random() < self.MUTATION_RATE:
                child = self.mutate(child, candidate_mask)
            result.append(
                self._repair(child, positions, candidates, candidate_mask)
            )
        return result

    def _rank(self, population, positions):
        ranked = [
            (self._fitness(plan, positions), plan)
            for plan in population
        ]
        # Nested arrays compare lexicographically on CPython but not on every
        # MicroPython build.  Stable input order is the native exact-tie rule.
        ranked.sort(key=lambda item: item[0])
        return ranked

    def _fitness(self, plan, positions):
        route_costs = [
            self._route_cost(positions[index], plan[index])
            for index in range(len(positions))
        ]
        if not route_costs:
            return float("inf")
        return max(route_costs) + self.MIN_SUM_TIE_WEIGHT * sum(route_costs)

    def _route_cost(self, origin, route):
        total = 0.0
        previous = origin
        for code in route:
            cell = self.decode_cell(code)
            total += self.scorer.cost(manhattan(previous, cell), cell)
            previous = cell
        return total

    def _append_cost(self, position, route, code):
        previous = self.decode_cell(route[-1]) if route else position
        cell = self.decode_cell(code)
        return self.scorer.cost(manhattan(previous, cell), cell)

    def _greedy_seed(self, positions, candidates):
        plan = [array("H") for _ in positions]
        for code in candidates:
            owner = min(
                range(len(positions)),
                key=lambda index: (
                    self._append_cost(positions[index], plan[index], code),
                    len(plan[index]),
                    index,
                ),
            )
            plan[owner].append(code)
        return self._nearest_neighbor(plan, positions)

    def _random_seed(self, positions, candidates):
        shuffled = list(candidates)
        self.rng.shuffle(shuffled)
        plan = [array("H") for _ in positions]
        for index, code in enumerate(shuffled):
            plan[index % len(plan)].append(code)
        return self._nearest_neighbor(plan, positions)

    def _nearest_neighbor(self, plan, positions):
        result = [array("H") for _ in positions]
        for route_index in range(len(plan)):
            remaining = list(plan[route_index])
            previous = positions[route_index]
            while remaining:
                best_index = min(
                    range(len(remaining)),
                    key=lambda index: (
                        self.scorer.cost(
                            manhattan(
                                previous, self.decode_cell(remaining[index])
                            ),
                            self.decode_cell(remaining[index]),
                        ),
                        -self.scorer.raw_probability(
                            self.decode_cell(remaining[index])
                        ),
                        remaining[index],
                    ),
                )
                code = remaining.pop(best_index)
                result[route_index].append(code)
                previous = self.decode_cell(code)
        return result

    def _repair(self, plan, positions, candidates, candidate_mask):
        repaired = [array("H") for _ in positions]
        seen = bytearray(self.grid_size * self.grid_size)
        for route_index in range(min(len(plan), len(repaired))):
            for raw_code in plan[route_index]:
                code = int(raw_code)
                if (
                    0 <= code < len(candidate_mask)
                    and candidate_mask[code]
                    and not seen[code]
                ):
                    repaired[route_index].append(code)
                    seen[code] = 1
        for code in candidates:
            if seen[code]:
                continue
            owner = min(
                range(len(repaired)),
                key=lambda index: (
                    self._append_cost(
                        positions[index], repaired[index], code
                    ),
                    len(repaired[index]),
                    index,
                ),
            )
            repaired[owner].append(code)
            seen[code] = 1
        return repaired

    def _tournament(self, ranked):
        count = min(3, len(ranked))
        contenders = self.rng.sample(ranked, count)
        contenders.sort(key=lambda item: item[0])
        return self._copy_plan(contenders[0][1])

    def _current_path_seed(
        self, ids, positions, candidates, own_id, current_path
    ):
        own_id = str(own_id)
        if own_id not in ids:
            return None
        candidate_mask = self._candidate_mask(candidates)
        codes = []
        seen = set()
        for cell in current_path:
            try:
                code = self.encode_cell(cell)
            except ValueError:
                continue
            if candidate_mask[code] and code not in seen:
                codes.append(code)
                seen.add(code)
        if not codes:
            return None
        plan = self._greedy_seed(positions, candidates)
        own_index = ids.index(own_id)
        own_route = array("H", codes)
        for code in plan[own_index]:
            if code not in seen:
                own_route.append(code)
        plan[own_index] = own_route
        for index in range(len(plan)):
            if index == own_index:
                continue
            plan[index] = array(
                "H", [code for code in plan[index] if code not in seen]
            )
        return self._repair(
            plan, positions, candidates, candidate_mask
        )

    def _remap_existing(self, ids):
        if not self.population:
            return []
        old_index = {
            rid: index for index, rid in enumerate(self.team_ids)
        }
        remapped = []
        for plan in self.population:
            new_plan = []
            for rid in ids:
                if rid in old_index and old_index[rid] < len(plan):
                    new_plan.append(
                        array("H", plan[old_index[rid]])
                    )
                else:
                    new_plan.append(array("H"))
            remapped.append(new_plan)
        return remapped

    def _encode_external_plan(self, plan, ids):
        encoded = []
        for rid in ids:
            route = array("H")
            values = _mapping_value_by_text_key(plan, rid, ())
            for cell in values:
                try:
                    route.append(self.encode_cell(cell))
                except ValueError:
                    pass
            encoded.append(route)
        return encoded

    def _encode_candidates(self, candidates):
        result = []
        seen = bytearray(self.grid_size * self.grid_size)
        for cell in candidates:
            try:
                code = self.encode_cell(cell)
            except ValueError:
                continue
            if not seen[code]:
                result.append(code)
                seen[code] = 1
        return result

    def _candidate_mask(self, candidates):
        result = bytearray(self.grid_size * self.grid_size)
        for code in candidates:
            result[int(code)] = 1
        return result

    @staticmethod
    def _copy_plan(plan):
        return [array("H", route) for route in plan]


def _normalize_cell(value):
    try:
        return int(value[0]), int(value[1])
    except Exception:
        return None


def _mapping_value_by_text_key(mapping, text_key, default=None):
    if text_key in mapping:
        return mapping[text_key]
    for key in mapping:
        if str(key) == text_key:
            return mapping[key]
    return default
