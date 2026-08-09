"""Shared persistent facade for collaborative HIL and physical wrappers."""

from .acbba import ACBBAAllocator
from .cbaa import CBAAAllocator
from .dga import DGAAllocator
from .dmchba import DMCHBAAllocator
from .hipc import HIPCAllocator
from .pi import PIAllocator
from .state import CollaborativeState, value_from

try:
    from replay_codec import decode_value
except ImportError:  # package import during desktop tests
    from allocator_replay.capture.codec import decode_value


ALLOCATORS = {
    "CBAA": CBAAAllocator,
    "ACBBA": ACBBAAllocator,
    "PI": PIAllocator,
    "HIPC": HIPCAllocator,
    "DMCHBA": DMCHBAAllocator,
    "DGA": DGAAllocator,
}
RESUME_ATTRIBUTE = "native_collaborative_resume"
DGA_POPULATION_PREFIX = "native_collaborative_dga_population_"
DGA_RECEIVED_PREFIX = "native_collaborative_dga_received_"


class NativeDecision:
    """Small worker-compatible allocation result."""

    __slots__ = ("goal", "debug")

    def __init__(self, goal, debug=None):
        self.goal = goal
        self.debug = debug or {}


def _plain_mapping(value):
    return value if isinstance(value, dict) else {}


def _flatten_initial_state(initial_state):
    """Accept both compact native state and the old snapshot section layout."""

    if not isinstance(initial_state, dict):
        return {}
    if not any(
        key in initial_state for key in ("robot_attrs", "views", "cfg")
    ):
        return dict(initial_state)
    flattened = {}
    for section in ("cfg", "views", "robot_attrs"):
        values = _plain_mapping(initial_state.get(section))
        for key, value in values.items():
            flattened[key] = value
    # Explicit top-level fields win over legacy sections.
    for key, value in initial_state.items():
        if key not in ("cfg", "views", "robot_attrs", "allocator_attrs"):
            flattened[key] = value
    return flattened


class PersistentCollaborativeRuntime:
    """One native allocator instance assigned to one robot for one trial."""

    def __init__(self, config=None):
        self.config = dict(config or {})
        self.state = None
        self.allocator = None
        self.algorithm = str(
            value_from(self.config, ("algorithm", "allocator"), "CBAA")
        ).upper()
        self.last_delta_sequence = -1
        self.call_index = 0

    def reset_trial(self, config, initial_state):
        """Start a trial and return small identity/capacity metadata."""

        merged = dict(self.config)
        if config:
            if not isinstance(config, dict):
                raise TypeError("config must be a mapping")
            merged.update(config)
        raw_initial = initial_state if isinstance(initial_state, dict) else {}
        allocator_attrs = _plain_mapping(
            raw_initial.get("allocator_attrs")
        )
        resume = allocator_attrs.get(RESUME_ATTRIBUTE)
        if isinstance(resume, dict) and resume.get("@"):
            resume = decode_value(resume)
        if isinstance(resume, dict):
            allocator_resume = _plain_mapping(resume.get("allocator"))
            if str(resume.get("algorithm", "")).upper() == "DGA":
                allocator_resume["population"] = [
                    allocator_attrs[name]
                    for name in sorted(allocator_attrs)
                    if name.startswith(DGA_POPULATION_PREFIX)
                ]
                allocator_resume["received_pool"] = [
                    allocator_attrs[name]
                    for name in sorted(allocator_attrs)
                    if name.startswith(DGA_RECEIVED_PREFIX)
                ]
                resume["allocator"] = allocator_resume
        initial = _flatten_initial_state(raw_initial)
        algorithm = str(
            value_from(
                initial,
                ("algorithm", "allocator"),
                value_from(merged, ("algorithm", "allocator"), self.algorithm),
            )
        ).upper()
        allocator_class = ALLOCATORS.get(algorithm)
        if allocator_class is None:
            raise ValueError("unknown collaborative allocator: " + algorithm)

        self.config = merged
        self.algorithm = algorithm
        state_initial = dict(initial)
        if isinstance(resume, dict):
            state_resume = _plain_mapping(resume.get("state"))
            if state_resume:
                state_initial["active_tasks"] = list(
                    state_resume.get("targets", ())
                )
                state_initial["robot_ids"] = list(
                    state_resume.get(
                        "robot_ids",
                        state_initial.get("robot_ids", ()),
                    )
                )
        self.state = CollaborativeState(merged, state_initial)
        if isinstance(resume, dict):
            self.state.restore_resume(resume.get("state"))
            # The host environment is newer than the resume record. Overlay it
            # after restoring allocator-owned state.
            if "active_tasks" in initial:
                self.state._replace_active(initial["active_tasks"])
            if "pos" in initial or "position" in initial:
                self.state.update_position(
                    value_from(initial, ("pos", "position"))
                )
            if "peer_positions" in initial:
                self.state.update_peer_positions(
                    initial["peer_positions"]
                )
            if "target_p" in initial or "probabilities" in initial:
                self.state.update_probabilities(
                    value_from(
                        initial, ("target_p", "probabilities")
                    )
                )
            collision = value_from(
                initial,
                (
                    "collision_active",
                    "collision_avoidance_active",
                ),
                None,
            )
            if collision is not None:
                self.state.set_collision(collision)
        self.allocator = allocator_class(self.state)
        if isinstance(resume, dict):
            self.allocator.restore_resume(resume.get("allocator"))
        self.last_delta_sequence = -1
        self.call_index = 0
        return {
            "mission": "collaborative_visit",
            "algorithm": self.algorithm,
            "robot_id": self.state.robot_id,
            "grid_size": int(self.state.grid_size),
            "target_capacity": int(self.state.DEFAULT_MAX_TARGETS),
            "target_count": len(self.state.targets),
            "team_size": len(self.state.robot_ids),
            "persistent": True,
            "motor_free": True,
        }

    def _require_trial(self):
        if self.state is None or self.allocator is None:
            raise RuntimeError("reset_trial must be called first")

    def apply_delta(self, delta):
        """Apply one environmental/peer delta outside the timed allocator call."""

        self._require_trial()
        if not isinstance(delta, dict):
            raise TypeError("delta must be a mapping")
        changed = delta.get("set")
        if isinstance(changed, dict):
            flattened = _flatten_initial_state(changed)
            for key, value in delta.items():
                if key not in ("set", "delete", "events"):
                    flattened[key] = value
        else:
            flattened = dict(delta)

        sequence = value_from(flattened, ("sequence", "seq"), None)
        if sequence is not None:
            sequence = int(sequence)
            if sequence <= self.last_delta_sequence:
                return

        state = self.state
        if "pos" in flattened or "position" in flattened:
            state.update_position(
                value_from(flattened, ("pos", "position"))
            )
        if (
            "peer_positions" in flattened
            or "team_positions" in flattened
        ):
            state.update_peer_positions(
                value_from(
                    flattened,
                    ("peer_positions", "team_positions"),
                    {},
                )
            )
        if "active_tasks" in flattened:
            state._replace_active(flattened["active_tasks"])
        completed = value_from(
            flattened,
            (
                "completed_tasks",
                "visited_targets",
                "target_completed",
                "completed",
            ),
            None,
        )
        if completed is not None:
            if isinstance(completed, (tuple, dict)):
                # A single (x, y) tuple/dict is one cell, while a tuple of
                # tuples is already a collection.
                if isinstance(completed, dict) or (
                    isinstance(completed, tuple)
                    and len(completed) == 2
                    and isinstance(completed[0], int)
                ):
                    completed = [completed]
            state.complete_cells(completed)
        activated = value_from(
            flattened, ("activated_tasks", "targets_activated"), None
        )
        if activated is not None:
            state.activate_cells(activated)
        probabilities = value_from(
            flattened, ("target_p", "probabilities"), None
        )
        if probabilities is not None:
            state.update_probabilities(probabilities)
        collision = value_from(
            flattened,
            (
                "collision_active",
                "collision_avoidance_active",
                "avoidance_active",
            ),
            None,
        )
        if collision is not None:
            state.set_collision(collision)

        messages = value_from(
            flattened,
            ("messages", "allocator_messages", "peer_messages"),
            [],
        )
        if isinstance(messages, dict):
            messages = [messages]
        for message in messages or []:
            payload = (
                message.get("payload")
                if isinstance(message, dict)
                and isinstance(message.get("payload"), dict)
                else message
            )
            self.allocator.handle_message(payload)

        for event in delta.get("events", ()) or ():
            if not isinstance(event, dict):
                continue
            kind = str(event.get("kind", ""))
            payload = decode_value(event.get("payload", {}))
            if kind == "allocator_message":
                self.allocator.handle_message(payload)
            elif kind in (
                "on_collision_avoidance_activated",
                "collision_avoidance",
            ):
                state.set_collision(True)

        deleted = delta.get("delete", {})
        if isinstance(deleted, dict):
            deleted_robot = deleted.get("robot_attrs", ())
            if any(
                name
                in (
                    "collision_active",
                    "collision_avoidance_active",
                    "avoidance_active",
                )
                for name in deleted_robot
            ):
                state.set_collision(False)

        state.event_counter += 1
        if sequence is not None:
            self.last_delta_sequence = sequence

    def choose_goal(self):
        """Run allocation; the shared worker supplies the outer total timer."""

        self._require_trial()
        state = self.state
        state.begin_allocator_call()
        goal = self.allocator.choose()
        self.call_index += 1
        return NativeDecision(
            None
            if goal is None
            else (int(goal[0]), int(goal[1])),
            {
                "algorithm": self.algorithm,
                "robot_id": state.robot_id,
                "call_index": int(self.call_index - 1),
                "call_path": self.allocator.last_call_path,
            },
        )

    def drain_messages(self):
        """Serialize/drain outbound allocator messages outside timed allocation."""

        self._require_trial()
        return self.state.drain_messages()

    def snapshot_minimal(self):
        """Return sectioned compact state sufficient to restore this context."""

        self._require_trial()
        resume = {
            "version": 1,
            "algorithm": self.algorithm,
            "call_index": int(self.call_index),
            "last_delta_sequence": int(self.last_delta_sequence),
            "state": self.state.export_resume(),
            "allocator": self.allocator.export_resume(),
        }
        allocator_attrs = {}
        if self.algorithm == "DGA":
            allocator_resume = resume["allocator"]
            population = allocator_resume.pop("population", [])
            received = allocator_resume.pop("received_pool", [])
            for index, plan in enumerate(population):
                allocator_attrs[
                    DGA_POPULATION_PREFIX + ("%02d" % index)
                ] = plan
            for index, plan in enumerate(received):
                allocator_attrs[
                    DGA_RECEIVED_PREFIX + ("%02d" % index)
                ] = plan
        allocator_attrs[RESUME_ATTRIBUTE] = resume
        return {
            "robot_attrs": {
                "candidate_count_before_filter": int(
                    self.state.candidate_count_before
                ),
                "candidate_count_after_filter": int(
                    self.state.candidate_count_after
                ),
                "max_candidate_cells": self.state.max_candidate_cells,
            },
            "views": {},
            "cfg": {},
            "belief": {},
            "allocator_attrs": allocator_attrs,
        }

    def timing_counters(self):
        self._require_trial()
        return self.state

    def candidate_counts(self):
        self._require_trial()
        return (
            int(self.state.candidate_count_before),
            int(self.state.candidate_count_after),
        )

    def call_class(self):
        self._require_trial()
        path = str(self.allocator.last_call_path)
        if self.algorithm in ("DGA", "DMCHBA") and path in (
            "path_empty",
            "collision_replan",
            "received_better_solution",
        ):
            return "full_allocation_solve"
        if self.algorithm == "HIPC" and self.state.filter_invocations:
            return "full_allocation_solve"
        if self.algorithm in ("ACBBA", "PI") and path in (
            "bundle_extended",
            "path_extended",
            "consensus_suffix_release",
            "consensus_path_repair",
            "collision_replan",
        ):
            return "partial_bundle_refill"
        if self.state.filter_invocations:
            return "candidate_filter_only"
        return "cached_or_maintenance"


def create_persistent_runtime(config):
    """Factory used unchanged by HIL and future physical mission wrappers."""

    return PersistentCollaborativeRuntime(config)
