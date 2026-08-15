"""Persistent, motor-free allocator runtime used by authoritative HIL.

Legacy replay retains one active context and restores snapshots between
calls.  Causal mission mode explicitly retains four isolated native runtime
objects on one board.  Only the selected object's ``choose_goal`` is timed;
context creation, selection, and environmental delta application remain
outside the allocator timing boundary.
"""

import gc

from replay_codec import decode_value, encode_value
from replay_random import Random
from replay_robot import (
    ALGORITHM_PREFIXES,
    EXCLUDED_ATTRS,
    ReplayRobot,
    outbound_messages,
)


SECTIONS = (
    "robot_attrs",
    "views",
    "cfg",
    "belief",
    "allocator_attrs",
)

# A full Bayesian DGA population contains 30 complete team plans.  Encoding
# that list as one JSON value can exhaust controller heap after choose_goal()
# has already succeeded.  These prefixes make each plan an independent
# streamed result field while remaining ordinary dga_* robot attributes to the
# host-side snapshot code.
DGA_STREAMED_ATTRIBUTES = (
    ("dga_population", "dga_replay_population_"),
    ("dga_received_solutions", "dga_replay_received_solutions_"),
    (
        "dga_received_solution_pool",
        "dga_replay_received_solution_pool_",
    ),
)

RNG_STREAM_PREFIX = "dga_rng_replay_rng_"

# Generated memory-optimized Bayesian allocators reconstruct these reusable
# candidate buffers from grid size.  They are implementation workspaces, not
# logical allocator state, and must not be copied to the host or restored into
# the desktop simulator allocator.
TRANSIENT_ALLOCATOR_ATTRIBUTES = (
    "_candidate_scan_ids",
    "_candidate_ranked_ids",
    "_candidate_probabilities",
    "_candidate_distances",
    "_active_candidate_cache",
)


def _has_algorithm_prefix(name):
    """MicroPython 1.24 does not accept a tuple in ``str.startswith``."""
    for prefix in ALGORITHM_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


def _delete_attribute(value, name):
    try:
        delattr(value, name)
    except AttributeError:
        pass


def _restore_streamed_rng(robot):
    """Rebuild MT19937 state without a 625-item temporary tuple.

    The output worker sends the 624 uint32 words as small bytearray fields.
    State setup is untimed.  An unsigned array is filled directly from those
    fields and adopted by the ordinary ``Random`` implementation, avoiding
    both CPython-only ``Random.__new__`` and a 624/625-pointer temporary.
    """

    from array import array

    length_name = RNG_STREAM_PREFIX + "state_length"
    if not hasattr(robot, length_name):
        return
    length = int(getattr(robot, length_name))
    if length != 624:
        raise ValueError("invalid streamed RNG state length")
    index = int(getattr(robot, RNG_STREAM_PREFIX + "index"))
    chunk_count = int(getattr(robot, RNG_STREAM_PREFIX + "chunk_count"))
    # MicroPython array multiplication is unsupported, and append growth can
    # temporarily over-allocate or fragment the small controller heap.
    # Constructing from an exact-size zero byte buffer yields precisely
    # ``length`` compact uint32 slots without a pointer-list initializer.
    state = array("I", bytearray(length * 4))
    state_index = 0
    for chunk_index in range(chunk_count):
        name = RNG_STREAM_PREFIX + ("%03d" % chunk_index)
        if not hasattr(robot, name):
            raise ValueError("incomplete streamed RNG state: " + name)
        chunk = getattr(robot, name)
        if len(chunk) % 4:
            raise ValueError("invalid streamed RNG byte count")
        for offset in range(0, len(chunk), 4):
            if state_index >= length:
                raise ValueError("streamed RNG state is too long")
            state[state_index] = (
                int(chunk[offset])
                | (int(chunk[offset + 1]) << 8)
                | (int(chunk[offset + 2]) << 16)
                | (int(chunk[offset + 3]) << 24)
            )
            state_index += 1
        _delete_attribute(robot, name)
    if state_index != length:
        raise ValueError("streamed RNG state is incomplete")
    robot.dga_rng = Random(None, state, index)
    _delete_attribute(robot, length_name)
    _delete_attribute(robot, RNG_STREAM_PREFIX + "index")
    _delete_attribute(robot, RNG_STREAM_PREFIX + "chunk_count")


def _restore_packed_plan(robot, name):
    """Reassemble one memory-optimized Bayesian DGA plan."""

    marker_name = name + "_packed"
    if not hasattr(robot, marker_name):
        return None
    from array import array
    from replay_b_dga_optimized import _PackedPlan

    chunk_count_name = name + "_cells_chunk_count"
    chunk_count = int(getattr(robot, chunk_count_name))
    cells = array("H")
    for chunk_index in range(chunk_count):
        chunk_name = name + "_cells_" + ("%03d" % chunk_index)
        if not hasattr(robot, chunk_name):
            raise ValueError(
                "incomplete streamed packed-plan state: " + chunk_name
            )
        chunk = getattr(robot, chunk_name)
        for item in chunk:
            cells.append(int(item))
        _delete_attribute(robot, chunk_name)
    lengths_name = name + "_lengths"
    team_ids_name = name + "_team_ids"
    grid_size_name = name + "_grid_size"
    plan = _PackedPlan(
        cells,
        getattr(robot, lengths_name),
        getattr(robot, team_ids_name),
        int(getattr(robot, grid_size_name)),
    )
    for helper in (
        marker_name,
        chunk_count_name,
        lengths_name,
        team_ids_name,
        grid_size_name,
    ):
        _delete_attribute(robot, helper)
    return plan


def _restore_streamed_dga(robot):
    """Reassemble plan lists after a host-side robot context switch."""

    for attribute, prefix in DGA_STREAMED_ATTRIBUTES:
        count_name = prefix + "count"
        if not hasattr(robot, count_name):
            continue
        count = int(getattr(robot, count_name))
        values = []
        for index in range(count):
            name = prefix + ("%03d" % index)
            if hasattr(robot, name):
                values.append(getattr(robot, name))
                _delete_attribute(robot, name)
                continue
            packed = _restore_packed_plan(robot, name)
            if packed is None:
                raise ValueError(
                    "incomplete streamed DGA state: " + name
                )
            values.append(packed)
        _delete_attribute(robot, count_name)
        setattr(robot, attribute, values)


def _stream_dga_attribute(target, name, value):
    """Expose one large DGA sequence as count plus per-plan references."""

    prefix = None
    for attribute, candidate in DGA_STREAMED_ATTRIBUTES:
        if name == attribute:
            prefix = candidate
            break
    if prefix is None or not isinstance(value, (list, tuple)):
        return False
    target[prefix + "count"] = int(len(value))
    for index, item in enumerate(value):
        # The worker encodes each raw plan immediately before sending that one
        # field. Keeping references here avoids holding 30 encoded plans plus
        # the resident native population at the same time.
        target[prefix + ("%03d" % index)] = item
    return True


def _raw_attributes(value, excluded=()):
    result = {}
    for name, item in value.__dict__.items():
        if name in excluded:
            continue
        if not callable(item):
            result[name] = item
    return result


def _prepare_replay_state(allocator, robot):
    """Refresh native scratch state outside the timed allocator boundary."""

    ensure_candidates = getattr(
        allocator,
        "_ensure_candidate_workspace",
        None,
    )
    if callable(ensure_candidates):
        grid_size = getattr(robot, "grid_size", None)
        if grid_size is None:
            grid_size = getattr(robot.cfg, "grid_size", 19)
        ensure_candidates(int(grid_size) * int(grid_size))
    prepare = getattr(allocator, "prepare_replay_state", None)
    if callable(prepare):
        prepare(robot)


def _allocator_snapshot(allocator):
    """Return logical allocator state without native transient workspaces."""

    snapshot = getattr(
        allocator,
        "replay_snapshot_allocator_attrs",
        None,
    )
    if callable(snapshot):
        values = snapshot()
        if not isinstance(values, dict):
            raise TypeError(
                "replay_snapshot_allocator_attrs must return a mapping"
            )
        return values
    return _raw_attributes(
        allocator,
        excluded=TRANSIENT_ALLOCATOR_ATTRIBUTES,
    )


class ReplayPersistentRuntime:
    """Adapter for generated replay allocators.

    Native allocator modules may replace this adapter by exporting
    ``create_persistent_runtime(config)``.  That factory must return an object
    with the same five public methods.
    """

    snapshot_values_encoded = False

    def __init__(self, allocator_factory):
        self._allocator_factory = allocator_factory
        self.allocator = None
        self.robot = None
        self.config = None

    def reset_trial(self, config, initial_state):
        self.config = config
        allocator_values = initial_state.pop("allocator_attrs")
        self.allocator = self._allocator_factory()
        self.robot = ReplayRobot(initial_state)
        _restore_streamed_rng(self.robot)
        _restore_streamed_dga(self.robot)
        # target_p is deliberately transmitted once, in views.  The belief
        # alias prevents a second 361-cell map from existing during restore.
        if (
            "target_p" in self.robot._views
            and not hasattr(self.robot.belief, "target_p")
        ):
            self.robot.belief.target_p = self.robot._views["target_p"]
        decoded_allocator_values = {
            name: decode_value(value)
            for name, value in allocator_values.items()
        }
        restore_allocator = getattr(
            self.allocator,
            "replay_restore_allocator_attrs",
            None,
        )
        if callable(restore_allocator):
            # Native allocators keep constructor-created typed workspaces and
            # restore only their explicitly declared logical state. Removing
            # every __dict__ entry here would destroy those workspaces during
            # PCLEAR/reconnect recovery.
            restore_allocator(decoded_allocator_values)
        else:
            # Generated legacy allocators historically treated their complete
            # __dict__ as the authoritative logical snapshot.
            for name in list(self.allocator.__dict__):
                delattr(self.allocator, name)
            for name, value in decoded_allocator_values.items():
                setattr(self.allocator, name, value)
        if not allocator_values:
            initialize = getattr(self.allocator, "initialize", None)
            if callable(initialize):
                initialize(self.robot)
        _prepare_replay_state(self.allocator, self.robot)
        return {"context_ready": True}

    def _section_target(self, section):
        if section == "robot_attrs":
            return self.robot
        if section == "views":
            return self.robot._views
        if section == "cfg":
            return self.robot.cfg
        if section == "belief":
            return self.robot.belief
        if section == "allocator_attrs":
            return self.allocator
        raise ValueError("unknown persistent state section: " + str(section))

    @staticmethod
    def _set(target, name, encoded):
        value = decode_value(encoded)
        if isinstance(target, dict):
            target[name] = value
        else:
            setattr(target, name, value)

    @staticmethod
    def _delete(target, name):
        if isinstance(target, dict):
            target.pop(name, None)
        else:
            try:
                delattr(target, name)
            except AttributeError:
                pass

    def apply_delta(self, delta):
        changed = delta.get("set", delta)
        deleted = delta.get("delete", {})
        for section in SECTIONS:
            target = self._section_target(section)
            for name in deleted.get(section, ()):
                self._delete(target, name)
            for name, value in changed.get(section, {}).items():
                self._set(target, name, value)
        if any(
            prefix + "count" in changed.get("robot_attrs", {})
            for _, prefix in DGA_STREAMED_ATTRIBUTES
        ):
            _restore_streamed_dga(self.robot)
        if RNG_STREAM_PREFIX + "state_length" in changed.get(
            "robot_attrs",
            {},
        ):
            _restore_streamed_rng(self.robot)
        if "target_p" in changed.get("views", {}):
            self.robot.belief.target_p = self.robot._views["target_p"]
        # Messages belong to one allocator invocation, not the resident
        # context.  Clearing them is setup work and therefore untimed.
        self.robot.published_messages = []
        for event in delta.get("events", ()):
            self._apply_event(event)
        _prepare_replay_state(self.allocator, self.robot)

    def _apply_event(self, event):
        kind = event.get("kind")
        if kind == "allocator_message":
            payload = decode_value(event.get("payload", {}))
            receiver_name = event.get("receiver")
            receiver = getattr(self.allocator, receiver_name, None)
            if callable(receiver):
                receiver(self.robot, payload)
                return
            category = payload.get("type") if isinstance(payload, dict) else None
            primary = {
                "cbaa_entry": "handle_cbaa_message",
                "acbba_entry": "handle_acbba_message",
                "pi_entry": "handle_pi_message",
                "pi_clear_path": "handle_pi_message",
                "hipc_entry": "handle_hipc_message",
                "hipc_clear_bundle": "handle_hipc_message",
                "dga_entry": "handle_dga_message",
                "dmchba_entry": "handle_dmchba_message",
            }.get(category)
            names = (
                (primary, "handle_cbaa_message", "handle_acbba_message")
                if primary
                else ("handle_cbaa_message", "handle_acbba_message")
            )
            for name in names:
                receiver = getattr(self.allocator, name, None)
                if callable(receiver):
                    receiver(self.robot, payload)
                    return
            return
        callback = getattr(self.allocator, str(kind), None)
        if not callable(callback):
            return
        if kind == "on_observation":
            # Current study allocators do not consume Observation fields, but
            # the event is retained as a small attribute bag for parity with
            # physical-style allocator callbacks.
            callback(self.robot, _Bag(decode_value(event.get("payload", {}))))
        else:
            callback(self.robot)

    def choose_goal(self):
        counters = self.robot.counters
        self._filter_start = len(
            counters.candidate_filter_time_us_samples
        )
        self._pre_class_state = self._class_state()
        decision = self.allocator.choose_goal(self.robot)
        self._last_filter_calls = len(
            counters.candidate_filter_time_us_samples
        ) - self._filter_start
        self._post_class_state = self._class_state()
        return decision

    def drain_messages(self):
        direct = self.robot.published_messages or []
        self.robot.published_messages = []
        generated = outbound_messages(self.allocator, self.robot)
        if not direct:
            return generated
        if generated:
            direct.extend(generated)
        return direct

    def snapshot_minimal(self):
        """Return only allocator-owned state; never echo environment maps."""
        robot_excluded = (
            "counters",
            "published_messages",
            "_views",
            "cfg",
            "belief",
        ) + EXCLUDED_ATTRS
        robot_attrs = {}
        for name, value in self.robot.__dict__.items():
            if name in robot_excluded:
                continue
            if not (
                _has_algorithm_prefix(name)
                or name in (
                    "candidate_count_before_filter",
                    "candidate_count_after_filter",
                    "max_candidate_cells",
                    "_allocation_probability_normalizer",
                    "_allocation_probability_belief_revision",
                )
            ):
                continue
            if _stream_dga_attribute(robot_attrs, name, value):
                continue
            if any(
                name.startswith(prefix)
                for _, prefix in DGA_STREAMED_ATTRIBUTES
            ):
                # Stream helper attributes are removed during restore.  This
                # guard prevents an incomplete legacy helper from being
                # echoed alongside the authoritative reconstructed list.
                continue
            robot_attrs[name] = value
        return {
            "robot_attrs": robot_attrs,
            "views": {},
            "cfg": {},
            "belief": {},
            "allocator_attrs": _allocator_snapshot(self.allocator),
        }

    def timing_counters(self):
        return self.robot.counters

    def candidate_counts(self):
        return (
            int(
                getattr(
                    self.robot,
                    "candidate_count_before_filter",
                    0,
                )
                or 0
            ),
            int(
                getattr(
                    self.robot,
                    "candidate_count_after_filter",
                    0,
                )
                or 0
            ),
        )

    def _class_state(self):
        names = (
            "dga_generation",
            "dga_last_reallocation_trigger",
            "dmchba_last_assignment_signature",
            "dmchba_last_reassignment_reason",
            "acbba_path",
            "pi_path",
        )
        return {
            name: getattr(self.robot, name, None)
            for name in names
        }

    def call_class(self):
        algorithm = str(self.config.get("algorithm", "")).upper()
        before = getattr(self, "_pre_class_state", {})
        after = getattr(self, "_post_class_state", {})
        if algorithm == "DGA" and any(
            before.get(name) != after.get(name)
            for name in (
                "dga_generation",
                "dga_last_reallocation_trigger",
            )
        ):
            return "full_allocation_solve"
        if algorithm == "DMCHBA" and any(
            before.get(name) != after.get(name)
            for name in (
                "dmchba_last_assignment_signature",
                "dmchba_last_reassignment_reason",
            )
        ):
            return "full_allocation_solve"
        if algorithm == "HIPC" and self._last_filter_calls:
            return "full_allocation_solve"
        path_name = {
            "ACBBA": "acbba_path",
            "PI": "pi_path",
        }.get(algorithm)
        if path_name and before.get(path_name) != after.get(path_name):
            return "partial_bundle_refill"
        if self._last_filter_calls:
            return "candidate_filter_only"
        return "cached_or_maintenance"


class _Bag:
    def __init__(self, values=None):
        if values:
            for name, value in values.items():
                setattr(self, name, value)


class PersistentRuntimeSlot:
    """One-trial controller with legacy-single or causal-four contexts."""

    REQUIRED_METHODS = (
        "reset_trial",
        "apply_delta",
        "choose_goal",
        "drain_messages",
        "snapshot_minimal",
    )

    def __init__(self, runtime_factory):
        self._runtime_factory = runtime_factory
        self.trial_config = None
        self.runtime = None
        self.context_id = None
        self.contexts = {}
        self.context_capacity = 1
        self.context_residency_limit = 1
        self.allowed_context_ids = None

    def begin_trial(self, config):
        self.end_trial()
        self.trial_config = config
        requested = int(config.get("logical_context_count", 1) or 1)
        if requested not in (1, 4):
            raise ValueError(
                "persistent logical_context_count must be one or four"
            )
        self.context_capacity = requested
        residency_limit = int(
            config.get("logical_context_residency_limit", requested) or requested
        )
        if residency_limit < 1 or residency_limit > requested:
            raise ValueError(
                "logical context residency limit must be between one and "
                + str(requested)
            )
        self.context_residency_limit = residency_limit
        raw_ids = config.get("robot_ids", ())
        if requested == 4:
            ids = [str(item) for item in raw_ids]
            if len(ids) != 4 or len(set(ids)) != 4:
                raise ValueError(
                    "causal persistent trial requires four unique robot IDs"
                )
            self.allowed_context_ids = set(ids)
        else:
            self.allowed_context_ids = None

    def clear_context(self, context_id=None):
        """Release one resident robot while retaining the trial config."""

        selected = self.context_id if context_id is None else str(context_id)
        if selected is not None:
            self.contexts.pop(str(selected), None)
        self.runtime = None
        self.context_id = None

    def _new_runtime(self):
        runtime = self._runtime_factory(self.trial_config)
        missing = [
            name
            for name in self.REQUIRED_METHODS
            if not callable(getattr(runtime, name, None))
        ]
        if missing:
            raise TypeError(
                "persistent runtime missing methods: " + ", ".join(missing)
            )
        return runtime

    @staticmethod
    def _apply_state_aliases(state, aliases):
        """Restore simulator aliases without decoding duplicate containers."""

        for alias in aliases or ():
            source_section = str(alias["source_section"])
            source_name = str(alias["source_name"])
            target_section = str(alias["target_section"])
            target_name = str(alias["target_name"])
            if (
                source_section not in SECTIONS
                or target_section not in SECTIONS
            ):
                raise ValueError("invalid persistent state alias section")
            source_values = state.get(source_section, {})
            if source_name not in source_values:
                raise ValueError(
                    "persistent state alias source is missing: "
                    + source_section
                    + "."
                    + source_name
                )
            state.setdefault(target_section, {})[target_name] = (
                source_values[source_name]
            )
        return state

    def _native_state(self, state, resume):
        # Compact native snapshots are overlaid first; fresh simulator
        # environment wins for position, tasks, clues, and peer data.
        flattened = dict(resume or {})
        for section in ("cfg", "views", "robot_attrs"):
            flattened.update(state.get(section, {}))
        if state.get("allocator_attrs"):
            flattened["allocator_attrs"] = state["allocator_attrs"]

        # A checkpointed context must reconstruct the exact task registry
        # learned so far, including completed/inactive cells still present in
        # the allocator's consensus table.  ``all_tasks`` is intentionally
        # excluded because it can contain future arrivals.  Each desktop
        # primary allocator's authoritative table is the learned registry.
        algorithm = str(
            (self.trial_config or {}).get("algorithm", "")
        ).upper()
        registry_field = {
            "CBAA": "cbaa_winner_by_cell",
            "ACBBA": "acbba_winner_by_cell",
            "PI": "pi_owner_by_cell",
            "HIPC": "hipc_winner_by_cell",
        }.get(algorithm)
        path_field = {
            "CBAA": "cbaa_current_task",
            "ACBBA": "acbba_path",
            "PI": "pi_path",
            "HIPC": "hipc_path",
        }.get(algorithm)
        registry = list(flattened.get("active_tasks", ()) or ())
        registry.extend(
            flattened.get("last_allocation_epoch_admitted", ()) or ()
        )
        table = flattened.get(registry_field) if registry_field else None
        if isinstance(table, dict):
            registry.extend(table.keys())
        path = flattened.get(path_field) if path_field else None
        if algorithm == "CBAA" and path is not None:
            registry.append(path)
        elif isinstance(path, (list, tuple, set)):
            registry.extend(path)
        flattened["admitted_task_registry"] = registry
        return flattened

    @staticmethod
    def _delta_payload(
        state,
        deleted,
        events,
        flatten=True,
        advance_event_counter=None,
    ):
        payload = {
            "set": state,
            "delete": deleted or {},
            "events": events or [],
        }
        if advance_event_counter is not None:
            payload["advance_event_counter"] = bool(
                advance_event_counter
            )
        # Native compact runtimes consume physical-style named deltas.  Keep
        # the sectioned form too so generated replay adapters remain exact.
        if flatten:
            for section in ("cfg", "views", "robot_attrs"):
                payload.update(state.get(section, {}))
        return payload

    @staticmethod
    def _begin_call_setup(runtime, requested):
        if not requested:
            return
        begin = getattr(runtime, "begin_call_setup", None)
        if callable(begin):
            begin()

    @classmethod
    def _runtime_delta(
        cls,
        runtime,
        state,
        deleted,
        events,
        advance_event_counter=True,
    ):
        accepts_sectioned = bool(
            getattr(runtime, "accepts_sectioned_delta", False)
        )
        return cls._delta_payload(
            state,
            deleted,
            events,
            flatten=not accepts_sectioned,
            advance_event_counter=(
                advance_event_counter if accepts_sectioned else None
            ),
        )

    def prepare(
        self,
        context_id,
        mode,
        state,
        deleted=None,
        events=None,
        resume=None,
        aliases=None,
        begin_call_setup=True,
        end_call_setup=True,
    ):
        if self.trial_config is None:
            raise RuntimeError("persistent trial has not begun")
        if mode == "causal_context":
            if self.context_capacity != 4:
                raise RuntimeError(
                    "causal_context requires logical_context_count=4"
                )
            context_id = str(context_id)
            if (
                self.allowed_context_ids is not None
                and context_id not in self.allowed_context_ids
            ):
                raise RuntimeError("unknown causal logical context")
            state = self._apply_state_aliases(state, aliases)
            if (
                begin_call_setup
                and str(
                    (self.trial_config or {}).get(
                        "logical_context_execution", ""
                    )
                )
                == "checkpointed_time_multiplexing"
                and context_id in self.contexts
            ):
                # Each call is a complete authoritative checkpoint. Rebuild
                # even when the same robot happens to be measured twice in a
                # row; otherwise a newly admitted active task is validated
                # against the preceding call's smaller learned registry before
                # its queued admission event is staged.
                self.runtime = None
                self.context_id = None
                self.contexts = {}
                gc.collect()
            runtime = self.contexts.get(context_id)
            if runtime is None:
                if len(self.contexts) >= self.context_residency_limit:
                    # The host supplies the complete frozen checkpoint on
                    # every causal call. Evicting the previous runtime here
                    # changes neither logical state nor W_alloc; it only
                    # bounds resident SRAM used outside the timed boundary.
                    self.runtime = None
                    self.context_id = None
                    self.contexts = {}
                    gc.collect()
                runtime = self._new_runtime()
                restore_state = (
                    state
                    if isinstance(runtime, ReplayPersistentRuntime)
                    else self._native_state(state, resume)
                )
                runtime.reset_trial(self.trial_config, restore_state)
                self.contexts[context_id] = runtime
                self._begin_call_setup(runtime, begin_call_setup)
                if events:
                    runtime.apply_delta(
                        self._runtime_delta(
                            runtime,
                            {},
                            {},
                            events,
                            end_call_setup,
                        )
                    )
            else:
                # Simulator/environment fields and explicitly delivered
                # messages/events advance this isolated context. Allocator
                # state remains resident and is never copied from a context
                # sampled earlier in the same physical serial sequence.
                self._begin_call_setup(runtime, begin_call_setup)
                runtime.apply_delta(
                    self._runtime_delta(
                        runtime,
                        state,
                        deleted,
                        events,
                        end_call_setup,
                    )
                )
            self.runtime = runtime
            self.context_id = context_id
            return
        if mode == "restore":
            self.runtime = None
            self.context_id = None
            runtime = self._new_runtime()
            state = self._apply_state_aliases(state, aliases)
            restore_state = (
                state
                if isinstance(runtime, ReplayPersistentRuntime)
                else self._native_state(state, resume)
            )
            runtime.reset_trial(self.trial_config, restore_state)
            self._begin_call_setup(runtime, begin_call_setup)
            self.runtime = runtime
            self.context_id = str(context_id)
            if events:
                runtime.apply_delta(
                    self._runtime_delta(
                        runtime,
                        {},
                        {},
                        events,
                        end_call_setup,
                    )
                )
        elif mode == "delta":
            if self.runtime is None or self.context_id != str(context_id):
                raise RuntimeError(
                    "delta context is not the active persistent context"
                )
            state = self._apply_state_aliases(state, aliases)
            self._begin_call_setup(self.runtime, begin_call_setup)
            self.runtime.apply_delta(
                self._runtime_delta(
                    self.runtime,
                    state,
                    deleted,
                    events,
                    end_call_setup,
                )
            )
        else:
            raise ValueError("unknown persistent setup mode: " + str(mode))

    def end_trial(self):
        self.runtime = None
        self.context_id = None
        self.contexts = {}
        self.context_capacity = 1
        self.context_residency_limit = 1
        self.allowed_context_ids = None
        self.trial_config = None
