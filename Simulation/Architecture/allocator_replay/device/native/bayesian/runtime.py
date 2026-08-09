"""Persistent facade for native Bayesian allocator cores.

Complete consensus/message adapters can wrap these cores without changing the
facade used by HIL and physical control loops.  The diagnostic local mode is
deliberately opt-in so it cannot be mistaken for the full CBAA/ACBBA/PI/HIPC
state machines.
"""

from .acbba import ACBBAInsertionCore
from .cbaa import CBAACore
from .dga import DGACore
from .dmchba import DMCHBACore
from .hipc import HIPCCore
from .pi import PICore
from .scoring import NormalizedProbabilityScorer, manhattan


CONSENSUS_ALGORITHMS = ("CBAA", "ACBBA", "PI", "HIPC")


class PersistentBayesianRuntime:
    def __init__(self, config):
        self.config = {}
        self.algorithm = ""
        self.state = {}
        self.messages = []
        self.scorer = None
        self.core = None
        self.adapter = None
        self.allow_local_core = False
        self.path = []
        self.last_decision = None
        self.reset_trial(config, {})

    def reset_trial(self, config, initial_state):
        self.config = dict(config or {})
        self.algorithm = str(
            self.config.get("algorithm", self.config.get("allocator", ""))
        ).upper()
        self.allow_local_core = bool(
            self.config.get("allow_diagnostic_local_core", False)
        )
        self.adapter = self.config.get("allocator_adapter")
        self.state = {}
        self.messages = []
        self.path = []
        self.last_decision = None
        self._merge_state(initial_state or {})
        grid_size = int(
            self.state.get(
                "grid_size", self.config.get("grid_size", 19)
            )
        )
        probabilities = self.state.get(
            "target_p", self.state.get("probabilities", {})
        )
        self.scorer = NormalizedProbabilityScorer(
            probabilities=probabilities,
            grid_size=grid_size,
            alpha=self.config.get("probability_alpha", 8.0),
        )
        self.core = self._make_core(grid_size)
        if self.adapter is not None:
            metadata = self.adapter.reset_trial(
                self.config, initial_state or {}
            )
        else:
            metadata = {}
        result = {
            "algorithm": self.algorithm,
            "grid_size": grid_size,
            "probability_alpha": self.scorer.alpha,
            "persistent": True,
            "complete_adapter": self.adapter is not None,
        }
        if isinstance(metadata, dict):
            result.update(metadata)
        return result

    def apply_delta(self, delta):
        delta = delta or {}
        if self.adapter is not None:
            self.adapter.apply_delta(delta)
        probability_map = delta.get("target_p")
        if probability_map is not None:
            self.scorer.replace_probabilities(
                probability_map,
                delta.get("grid_size", self.scorer.grid_size),
            )
        updates = delta.get(
            "target_p_updates", delta.get("probability_updates")
        )
        if updates:
            self.scorer.apply_updates(updates)
        self._merge_state(delta)

        searched_add = delta.get("searched_add", ())
        if searched_add:
            searched = self.state.setdefault("searched", set())
            if not isinstance(searched, set):
                searched = set(searched)
                self.state["searched"] = searched
            for cell in searched_add:
                searched.add(_cell(cell))
            self.path = [
                cell for cell in self.path if cell not in searched
            ]
        searched_remove = delta.get("searched_remove", ())
        if searched_remove:
            searched = self.state.setdefault("searched", set())
            for cell in searched_remove:
                searched.discard(_cell(cell))

    def choose_goal(self):
        if self.adapter is not None:
            decision = self.adapter.choose_goal()
            self.last_decision = decision
            return decision
        if self.algorithm in CONSENSUS_ALGORITHMS and not self.allow_local_core:
            raise RuntimeError(
                self.algorithm
                + " requires its complete native consensus/message adapter; "
                + "set allow_diagnostic_local_core only for scorer diagnostics"
            )

        if not self._clue_seen():
            goal = self._serpentine_goal()
            decision = {
                "goal": goal,
                "mode": "serpentine_pre_clue",
                "algorithm": self.algorithm,
            }
        else:
            candidates = self._candidate_cells()
            if self.algorithm == "CBAA":
                goal, bid = self.core.best_candidate(
                    self._position(), candidates
                )
                decision = self._decision(goal, {"bid": bid})
            elif self.algorithm in ("ACBBA", "HIPC"):
                goal, detail = self._choose_insertion(candidates)
                decision = self._decision(goal, detail)
            elif self.algorithm == "PI":
                goal, detail = self._choose_pi(candidates)
                decision = self._decision(goal, detail)
            elif self.algorithm == "DMCHBA":
                assignments = self.core.assign(
                    self._team_positions(), candidates
                )
                rid = str(self.state.get("rid", "0"))
                self.path = list(assignments.get(rid, []))
                decision = self._decision(
                    self.path[0] if self.path else None,
                    {
                        "matrix_size": self.core.last_matrix_size,
                        "workspace_bytes": self.core.workspace.payload_bytes(),
                    },
                )
            elif self.algorithm == "DGA":
                rid = str(self.state.get("rid", "0"))
                result = self.core.evolve(
                    self._team_positions(),
                    candidates,
                    own_id=rid,
                    current_path=self.path,
                    received_plans=self.state.pop(
                        "dga_received_plans", None
                    ),
                )
                self.path = list(result["plan"].get(rid, []))[: int(
                    self.config.get("commitment_horizon", 3)
                )]
                decision = self._decision(
                    self.path[0] if self.path else None, result
                )
            else:
                raise ValueError("unsupported Bayesian algorithm: " + self.algorithm)
        self.last_decision = decision
        return decision

    def drain_messages(self):
        if self.adapter is not None:
            return self.adapter.drain_messages()
        messages = self.messages
        self.messages = []
        return messages

    def snapshot_minimal(self):
        if self.adapter is not None:
            return self.adapter.snapshot_minimal()
        snapshot = {
            "algorithm": self.algorithm,
            "rid": self.state.get("rid"),
            "pos": self._position(),
            "path": list(self.path),
            "last_decision": self.last_decision,
            "probability_maximum": self.scorer.maximum,
        }
        if self.algorithm == "DGA":
            snapshot.update(
                {
                    "dga_generation": self.core.generation,
                    "dga_best_fitness": self.core.best_fitness,
                    "dga_population_size": len(self.core.population),
                    "dga_rng_state": self.core.rng.state,
                }
            )
        return snapshot

    def _make_core(self, grid_size):
        if self.algorithm == "CBAA":
            return CBAACore(self.scorer)
        if self.algorithm == "ACBBA":
            return ACBBAInsertionCore(self.scorer)
        if self.algorithm == "PI":
            return PICore(self.scorer)
        if self.algorithm == "HIPC":
            return HIPCCore(self.scorer)
        if self.algorithm == "DMCHBA":
            return DMCHBACore(
                self.scorer,
                self.config.get("commitment_horizon", 3),
            )
        if self.algorithm == "DGA":
            return DGACore(
                self.scorer,
                grid_size=grid_size,
                seed=self.config.get("seed", 1),
                population_size=self.config.get(
                    "population_size", 30
                ),
                iterations=self.config.get("iterations", 25),
            )
        raise ValueError("unsupported Bayesian algorithm: " + self.algorithm)

    def _merge_state(self, values):
        for key, value in values.items():
            if key not in (
                "target_p_updates",
                "probability_updates",
                "searched_add",
                "searched_remove",
            ):
                self.state[key] = value

    def _decision(self, goal, detail):
        return {
            "goal": goal,
            "mode": self.algorithm.lower() + "_post_clue",
            "algorithm": self.algorithm,
            "detail": detail,
        }

    def _choose_insertion(self, candidates):
        horizon = int(self.config.get("commitment_horizon", 3))
        path = [
            cell for cell in self.path if cell in candidates
        ]
        while len(path) < horizon:
            best = None
            for cell in candidates:
                if cell in path:
                    continue
                index, bid = self.core.best_insertion(
                    self._position(), path, cell
                )
                candidate = (bid, tuple(-value for value in cell), -index)
                if best is None or candidate > best[0]:
                    best = (candidate, cell, index, bid)
            if best is None:
                break
            path.insert(best[2], best[1])
        self.path = path
        return (
            path[0] if path else None,
            {"path": list(path)},
        )

    def _choose_pi(self, candidates):
        horizon = int(self.config.get("commitment_horizon", 3))
        path = [cell for cell in self.path if cell in candidates]
        while len(path) < horizon:
            best = None
            for cell in candidates:
                if cell in path:
                    continue
                for index in range(len(path) + 1):
                    cost = self.core.insertion_cost(
                        self._position(), path, cell, index
                    )
                    choice = (cost, cell, index)
                    if best is None or choice < best:
                        best = choice
            if best is None:
                break
            path.insert(best[2], best[1])
        self.path = path
        return path[0] if path else None, {"path": list(path)}

    def _candidate_cells(self):
        size = self.scorer.grid_size
        searched = set(self.state.get("searched", ()))
        obstacles = set(
            self.state.get(
                "known_obstacles", self.state.get("obstacles", ())
            )
        )
        cells = []
        for y in range(size):
            for x in range(size):
                cell = (x, y)
                if cell not in searched and cell not in obstacles:
                    cells.append(cell)
        limit = self.config.get(
            "max_candidate_cells", self.config.get("top_k")
        )
        if limit is None or str(limit).lower() == "all":
            return cells
        limit = int(limit)
        cells.sort(
            key=lambda cell: (
                -self.scorer.raw_probability(cell),
                manhattan(self._position(), cell),
                cell,
            )
        )
        return cells[:limit]

    def _team_positions(self):
        team = {str(self.state.get("rid", "0")): self._position()}
        for rid, position in self.state.get("peer_positions", {}).items():
            cell = _cell(position)
            if cell is not None:
                team[str(rid)] = cell
        return team

    def _position(self):
        return _cell(self.state.get("pos", (0, 0))) or (0, 0)

    def _clue_seen(self):
        return bool(
            self.state.get(
                "known_clues", self.state.get("clues", ())
            )
        )

    def _serpentine_goal(self):
        size = self.scorer.grid_size
        position = self._position()
        searched = set(self.state.get("searched", ()))
        ordered = []
        for y in range(size):
            xs = range(size) if y % 2 == 0 else range(size - 1, -1, -1)
            for x in xs:
                ordered.append((x, y))
        try:
            start = ordered.index(position) + 1
        except ValueError:
            start = 0
        for offset in range(len(ordered)):
            cell = ordered[(start + offset) % len(ordered)]
            if cell not in searched:
                return cell
        return None


def create_persistent_runtime(config):
    return PersistentBayesianRuntime(config)


def _cell(value):
    try:
        return int(value[0]), int(value[1])
    except Exception:
        return None
