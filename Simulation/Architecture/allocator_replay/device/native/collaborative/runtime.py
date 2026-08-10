"""Shared persistent facade for collaborative HIL and physical wrappers."""

from .acbba import ACBBAAllocator
from .cbaa import CBAAAllocator
from .compat import ticks_diff, ticks_us
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

_CAUSAL_MESSAGE_FIELDS = (
    "type",
    "sender",
    "x",
    "y",
    "owner",
    "winner",
    "significance",
    "bid",
    "value",
    "timestamp",
    "order",
    "path_cells",
    "path_size",
    "bundle_cells",
    "bundle_size",
    "released",
    "released_winner",
    "released_owner",
    "released_bid",
    "released_value",
    "cell",
)


def _expand_compact_message(mask, values):
    result = {}
    value_index = 0
    for field_index, name in enumerate(_CAUSAL_MESSAGE_FIELDS):
        if not (int(mask) & (1 << field_index)):
            continue
        value = values[value_index]
        value_index += 1
        if name in ("path_cells", "bundle_cells"):
            value = [
                (int(value[index]), int(value[index + 1]))
                for index in range(0, len(value), 2)
            ]
        elif name == "cell" and value is not None:
            value = (int(value[0]), int(value[1]))
        result[name] = value
    if value_index != len(values):
        raise ValueError("compact causal message field count mismatch")
    return result


def _compact_cells(values):
    if len(values) % 2:
        raise ValueError("compact causal cell sequence has odd length")
    return [
        (int(values[index]), int(values[index + 1]))
        for index in range(0, len(values), 2)
    ]


def _protocol_timestamp(value):
    """Preserve the logical no-time sentinel on single-precision ports."""

    parsed = float(value)
    if parsed <= -5.0e17:
        return -1000000000000000000
    return int(parsed)


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


def _decoded(value, default=None):
    if value is None:
        return default
    try:
        return decode_value(value)
    except (TypeError, ValueError, KeyError, IndexError):
        return default


def _mapping(value):
    value = _decoded(value, {})
    return value if isinstance(value, dict) else {}


def _sequence(value):
    value = _decoded(value, [])
    return value if isinstance(value, (list, tuple)) else []


class PersistentCollaborativeRuntime:
    """One native allocator instance assigned to one robot for one trial."""

    accepts_sectioned_delta = True

    def __init__(self, config=None):
        self.config = dict(config or {})
        self.state = None
        self.allocator = None
        self.algorithm = str(
            value_from(self.config, ("algorithm", "allocator"), "CBAA")
        ).upper()
        self.last_delta_sequence = -1
        self.call_index = 0
        self.epoch_reallocation_pending = False
        self.last_call_had_epoch_reallocation = False
        self.authoritative_message_seed = []
        self.authoritative_pending_snapshot = False
        self.authoritative_last_sent = {}
        self.synchronized_authoritative_state = False
        self.behavior_last_sent = []
        self.behavior_last_sent_initialized = False
        self.pending_algorithm_epoch_reset_us = 0

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
                state_initial["all_tasks"] = list(
                    state_resume.get("targets", ())
                )
                state_initial["active_tasks"] = list(
                    state_resume.get("active", state_resume.get("targets", ()))
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
        self._synchronize_authoritative_state(initial)
        self.last_delta_sequence = int(
            resume.get("last_delta_sequence", -1)
        ) if isinstance(resume, dict) else -1
        self.call_index = int(
            resume.get("call_index", 0)
        ) if isinstance(resume, dict) else 0
        self.epoch_reallocation_pending = False
        self.last_call_had_epoch_reallocation = False
        self.pending_algorithm_epoch_reset_us = 0
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

    def _slot(self, raw_cell):
        try:
            return self.state.slot_for_cell(raw_cell)
        except (TypeError, ValueError, KeyError, IndexError):
            return None

    def _synchronize_authoritative_state(self, flattened):
        """Translate one frozen AGX pre-state into the resident compact form.

        The controller contexts remain allocated for the whole mission, but a
        live call also carries the complete frozen logical pre-state.  Using
        that state as the synchronization checkpoint prevents a compact
        protocol implementation detail (for example ACBBA Table-1 timestamp
        bookkeeping) from accumulating into a different causal trajectory.
        This is setup work and is deliberately outside the allocator timer.
        """

        if not isinstance(flattened, dict):
            return False
        names = {
            "CBAA": (
                "cbaa_current_task",
                "cbaa_winner_by_cell",
                "cbaa_winning_bid_by_cell",
                None,
                "cbaa_pending_deltas",
                None,
                "cbaa_last_sent_signatures",
                None,
            ),
            "ACBBA": (
                "acbba_path",
                "acbba_winner_by_cell",
                "acbba_winning_bid_by_cell",
                "acbba_bid_time_by_cell",
                "acbba_pending_deltas",
                "acbba_pending_snapshot",
                "acbba_last_sent_signatures",
                "acbba_bid_counter",
            ),
            "PI": (
                "pi_path",
                "pi_owner_by_cell",
                "pi_significance_by_cell",
                "pi_time_by_cell",
                None,
                "pi_pending_snapshot",
                "pi_last_sent_signature",
                "pi_time_counter",
            ),
            "HIPC": (
                "hipc_path",
                "hipc_winner_by_cell",
                "hipc_winning_bid_by_cell",
                "hipc_bid_time_by_cell",
                None,
                "hipc_pending_snapshot",
                "hipc_last_sent_signature",
                "hipc_bid_counter",
            ),
        }
        selected = names.get(self.algorithm)
        if selected is None:
            return False
        (
            path_name,
            owner_name,
            value_name,
            time_name,
            pending_name,
            pending_snapshot_name,
            last_sent_name,
            counter_name,
        ) = selected
        if owner_name not in flattened or path_name not in flattened:
            return False

        owners = _mapping(flattened.get(owner_name))
        values = _mapping(flattened.get(value_name))
        times = _mapping(flattened.get(time_name)) if time_name else {}
        state = self.state
        for slot in range(len(state.targets)):
            state.clear_claim(slot)
        for raw_cell, raw_owner in owners.items():
            if raw_owner is None:
                continue
            slot = self._slot(raw_cell)
            owner = state.owner_index(raw_owner)
            if slot is None or owner < 0:
                continue
            value = values.get(raw_cell)
            if value is None:
                raise ValueError(
                    "authoritative consensus owner lacks a value"
                )
            raw_time = times.get(raw_cell, 0)
            try:
                epoch = max(0, int(float(raw_time)))
            except (TypeError, ValueError):
                epoch = 0
            state.set_claim(slot, owner, float(value), epoch)

        raw_path = _decoded(flattened.get(path_name), None)
        if self.algorithm == "CBAA":
            raw_path = [] if raw_path is None else [raw_path]
        path = []
        for raw_cell in raw_path or []:
            slot = self._slot(raw_cell)
            # A frozen authoritative path may intentionally retain a task that
            # just became completed/blocked.  Preserve it until choose() so the
            # native allocator performs and classifies the same repair work.
            if slot is not None:
                path.append(slot)
        self.allocator.path = path

        collision_name = self.algorithm.lower() + "_last_collision_active"
        if collision_name in flattened:
            self.allocator.last_collision_active = bool(
                _decoded(flattened.get(collision_name), False)
            )
        if counter_name:
            counter_attribute = (
                "time_counter" if self.algorithm == "PI" else "bid_counter"
            )
            if hasattr(self.allocator, counter_attribute):
                setattr(
                    self.allocator,
                    counter_attribute,
                    int(_decoded(flattened.get(counter_name), 0) or 0),
                )
        if self.algorithm == "HIPC":
            self.allocator.bad_prediction_count = dict(
                _decoded(
                    flattened.get("hipc_bad_prediction_count"), {}
                ) or {}
            )
            self.allocator.dropped_peers = sorted(
                str(item)
                for item in (
                    _decoded(flattened.get("hipc_dropped_peers"), ()) or ()
                )
            )
            self.allocator.last_predicted_peer_first_task = dict(
                _decoded(
                    flattened.get(
                        "hipc_last_predicted_peer_first_task"
                    ),
                    {},
                ) or {}
            )
            self.allocator.seen_peer_bundle_signature = dict(
                _decoded(
                    flattened.get("hipc_seen_peer_bundle_signature"), {}
                ) or {}
            )

        self.authoritative_message_seed = []
        if pending_name:
            pending = _mapping(flattened.get(pending_name))
            self.authoritative_message_seed = [
                value for _, value in pending.items()
                if isinstance(value, dict)
            ]
        self.authoritative_pending_snapshot = bool(
            _decoded(
                flattened.get(pending_snapshot_name), False
                if pending_snapshot_name else False,
            )
        ) if pending_snapshot_name else False
        if last_sent_name:
            raw_last_sent = flattened.get(last_sent_name)
            try:
                self.authoritative_last_sent = (
                    None
                    if raw_last_sent is None
                    else decode_value(raw_last_sent)
                )
            except (TypeError, ValueError, KeyError, IndexError):
                self.authoritative_last_sent = None
            self.behavior_last_sent_initialized = (
                self.authoritative_last_sent is not None
            )
        else:
            self.authoritative_last_sent = {}
            self.behavior_last_sent_initialized = False
        self.behavior_last_sent = self._normalize_last_sent(
            self.authoritative_last_sent
        )
        self.synchronized_authoritative_state = True
        return True

    def _normalize_last_sent(self, raw):
        result = []
        if self.algorithm in ("CBAA", "ACBBA"):
            if not isinstance(raw, dict):
                return result
            for raw_cell, signature in raw.items():
                try:
                    values = list(signature)
                    cell = self.state.decode_cell(
                        self.state.encode_cell(raw_cell)
                    )
                    item = {
                        "cell": [int(cell[0]), int(cell[1])],
                        "owner": (
                            None
                            if values[1] is None
                            else str(values[1])
                        ),
                        "value": float(values[2]),
                    }
                    if self.algorithm == "ACBBA":
                        item["timestamp"] = _protocol_timestamp(values[3])
                    result.append(item)
                except (TypeError, ValueError, IndexError):
                    continue
            result.sort(key=lambda item: tuple(item["cell"]))
            return result
        if not isinstance(raw, (list, tuple)):
            return result
        for entry in raw:
            try:
                cell = self.state.decode_cell(
                    self.state.encode_cell(entry[0])
                )
                result.append(
                    {
                        "cell": [int(cell[0]), int(cell[1])],
                        "value": float(entry[1]),
                        "timestamp": _protocol_timestamp(entry[2]),
                    }
                )
            except (TypeError, ValueError, IndexError):
                continue
        return result

    def _record_message_behavior(self, messages):
        if self.algorithm in ("CBAA", "ACBBA"):
            by_cell = {}
            for item in self.behavior_last_sent:
                try:
                    by_cell[tuple(item["cell"])] = dict(item)
                except (TypeError, KeyError):
                    continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                try:
                    cell = (int(message["x"]), int(message["y"]))
                    owner = message.get(
                        "owner", message.get("winner")
                    )
                    value = message.get(
                        "significance",
                        message.get("bid", message.get("value")),
                    )
                    record = {
                        "cell": [cell[0], cell[1]],
                        "owner": None if owner is None else str(owner),
                        "value": float(value),
                    }
                    if self.algorithm == "ACBBA":
                        record["timestamp"] = _protocol_timestamp(
                            message["timestamp"]
                        )
                    by_cell[cell] = record
                except (KeyError, TypeError, ValueError):
                    continue
            self.behavior_last_sent = [
                by_cell[key] for key in sorted(by_cell)
            ]
        elif messages:
            result = []
            for slot in self.allocator.path:
                if self.state.claim_owner[slot] != self.state.robot_index:
                    continue
                cell = self.state.decode_cell(self.state.targets[slot])
                result.append(
                    {
                        "cell": [int(cell[0]), int(cell[1])],
                        "value": float(self.state.claim_value[slot]),
                        "timestamp": int(self.state.claim_epoch[slot]),
                    }
                )
            self.behavior_last_sent = result
            self.behavior_last_sent_initialized = True
        return messages

    def begin_call_setup(self):
        """Start one logical call that may span several bounded PSETUPs."""

        self.pending_algorithm_epoch_reset_us = 0

    def _require_trial(self):
        if self.state is None or self.allocator is None:
            raise RuntimeError("reset_trial must be called first")

    def apply_delta(self, delta):
        """Apply one environmental/peer delta outside the timed allocator call."""

        self._require_trial()
        # One setup transaction feeds exactly one subsequent allocator call.
        # Keep policy-induced allocator reset work separate from generic state
        # synchronization so the worker can add only the former to W_alloc.
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
        if "candidate_count_before_filter" in flattened:
            state.candidate_count_before = max(
                0, int(flattened["candidate_count_before_filter"])
            )
        if "candidate_count_after_filter" in flattened:
            state.candidate_count_after = max(
                0, int(flattened["candidate_count_after_filter"])
            )
        unavailable_names = (
            "searched",
            "local_searched",
            "known_obstacles",
            "obstacles",
            "blocked",
            "blocked_cells",
        )
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
        activated = value_from(
            flattened, ("activated_tasks", "targets_activated"), None
        )
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

        saw_allocation_epoch = False
        for event in delta.get("events", ()) or ():
            if isinstance(event, (list, tuple)) and event:
                tag = int(event[0])
                if tag == 0:
                    common = _expand_compact_message(event[1], event[2])
                    for row in event[3]:
                        payload = dict(common)
                        payload.update(
                            _expand_compact_message(row[0], row[1])
                        )
                        self.allocator.handle_message(payload)
                    continue
                if tag == 1:
                    kind = "allocation_epoch"
                    payload = {
                        "epoch_index": int(event[1]),
                        "trigger_reason": str(event[2]),
                        "admitted_cells": _compact_cells(event[3]),
                    }
                elif tag == 3:
                    kind = str(event[1])
                    payload = event[2]
                else:
                    raise ValueError("unknown compact causal event tag")
            else:
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("kind", ""))
                payload = decode_value(event.get("payload", {}))
            if kind == "allocator_message":
                # Replay against the resident pre-hook context.  A complete
                # frozen checkpoint is loaded below, after all ordered events,
                # so this setup-only replay cannot double-apply an update to
                # the logical choose_goal input.
                self.allocator.handle_message(payload)
            elif kind == "allocation_epoch":
                if not isinstance(payload, dict):
                    raise TypeError("allocation epoch payload must be a mapping")
                epoch_index = int(payload.get("epoch_index", -1))
                reason = str(payload.get("trigger_reason", ""))
                admitted = payload.get("admitted_cells", ())
                # The frozen checkpoint is post-hook and can already show an
                # admitted task as completed.  Replay the ordered admission
                # against resident pre-hook state first, including the
                # desktop behavior that reopens a previously traversed cell.
                state.activate_cells(admitted)
                for encoded in state._normalize_cell_collection(admitted):
                    slot = state.slot_by_cell.get(encoded)
                    if slot is not None:
                        state.unavailable[slot] = 0
                if state.apply_allocation_epoch(
                    epoch_index, reason, admitted
                ):
                    saw_allocation_epoch = True
                    reset_started = ticks_us()
                    try:
                        epoch_reallocated = bool(
                            self.allocator.on_allocation_epoch(
                                reason, admitted, epoch_index
                            )
                        )
                        if admitted:
                            # These resident protocol caches are the native
                            # counterparts of the desktop pending/last-sent
                            # structures cleared by on_allocation_epoch.  Their
                            # reset is policy-induced allocator work and is
                            # therefore inside the device reset timer.
                            self.authoritative_message_seed = []
                            self.authoritative_pending_snapshot = False
                            self.authoritative_last_sent = {}
                            self.behavior_last_sent = []
                            self.behavior_last_sent_initialized = False
                    finally:
                        self.pending_algorithm_epoch_reset_us += max(
                            0, ticks_diff(ticks_us(), reset_started)
                        )
                    self.epoch_reallocation_pending = epoch_reallocated
            elif kind in (
                "on_collision_avoidance_activated",
                "collision_avoidance",
            ):
                state.set_collision(True)

        # Environmental values in ``set`` are the authoritative post-hook
        # checkpoint.  Apply them after ordered event replay so a task that
        # was admitted and then completed before this allocator call is active
        # while its callback runs, but inactive for choose_goal.
        if "active_tasks" in flattened:
            state._replace_active(flattened["active_tasks"])
        if any(name in flattened for name in unavailable_names):
            state.replace_unavailable(
                *(flattened.get(name, ()) for name in unavailable_names)
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
        if activated is not None:
            state.activate_cells(activated)

        # The AGX snapshot is intentionally post-hook.  Event replay above
        # therefore starts from the still-resident native pre-hook context so
        # the timed callback sees and clears the real populated native state.
        # Loading the authoritative post-hook checkpoint only afterwards keeps
        # choose_goal parity fail-closed without timing USB/state translation.
        synchronized = self._synchronize_authoritative_state(flattened)
        if synchronized:
            # Messages created while replaying already-authoritative inputs are
            # setup artifacts.  The synchronized pending cache below is the
            # sole source of pre-call outbound deltas.
            state.drain_messages()
            if saw_allocation_epoch:
                self.epoch_reallocation_pending = True

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

        if bool(delta.get("advance_event_counter", True)):
            state.event_counter += 1
        if sequence is not None:
            self.last_delta_sequence = sequence

    def choose_goal(self):
        """Run allocation; the shared worker supplies the outer total timer."""

        self._require_trial()
        state = self.state
        state.begin_allocator_call()
        self._pre_choose_path = list(self.allocator.path)
        self._pre_choose_claims = [
            (
                int(state.claim_owner[slot]),
                float(state.claim_value[slot]),
                int(state.claim_epoch[slot]),
            )
            for slot in range(len(state.targets))
        ]
        epoch_reallocation = bool(self.epoch_reallocation_pending)
        goal = self.allocator.choose()
        self.last_call_had_epoch_reallocation = epoch_reallocation
        self.epoch_reallocation_pending = False
        self.call_index += 1
        self._post_choose_path = list(self.allocator.path)
        return NativeDecision(
            None
            if goal is None
            else (int(goal[0]), int(goal[1])),
            {
                "algorithm": self.algorithm,
                "robot_id": state.robot_id,
                "call_index": int(self.call_index - 1),
                "call_path": self.allocator.last_call_path,
                "allocation_epoch_reallocation": epoch_reallocation,
                "allocation_epoch_index": int(
                    state.last_allocation_epoch_index
                ),
            },
        )

    def drain_messages(self):
        """Serialize/drain outbound allocator messages outside timed allocation."""

        self._require_trial()
        generated = self.state.drain_messages()
        algorithm = self.algorithm
        if algorithm in ("PI", "HIPC"):
            path_changed = self._pre_choose_path != self._post_choose_path
            snapshot_requested = bool(
                getattr(self.allocator, "snapshot_requested", False)
            )
            if hasattr(self.allocator, "snapshot_requested"):
                self.allocator.snapshot_requested = False
            if (
                not self.authoritative_pending_snapshot
                and not path_changed
                and not generated
                and not snapshot_requested
            ):
                return self._record_message_behavior(generated)
            if self._path_signature_matches_last_sent():
                return self._record_message_behavior([])
            message_type = "pi_entry" if algorithm == "PI" else "hipc_entry"
            clear_type = (
                "pi_clear_path" if algorithm == "PI"
                else "hipc_clear_bundle"
            )
            messages = []
            path_cells = [
                {
                    "x": self.state.decode_cell(
                        self.state.targets[slot]
                    )[0],
                    "y": self.state.decode_cell(
                        self.state.targets[slot]
                    )[1],
                }
                for slot in self.allocator.path
            ]
            if not self.allocator.path:
                if algorithm == "HIPC" and not self.behavior_last_sent_initialized:
                    # Desktop HIPC initializes an empty signature without an
                    # initial clear message.  A later nonempty-to-empty change
                    # still emits the required clear.
                    self.behavior_last_sent = []
                    self.behavior_last_sent_initialized = True
                    return self._record_message_behavior([])
                if algorithm == "PI":
                    timestamp = int(self.allocator._next_time())
                else:
                    timestamp = int(self.allocator._next_bid_time())
                return self._record_message_behavior([{
                    "type": clear_type,
                    "sender": self.state.robot_id,
                    "timestamp": timestamp,
                    "path_cells": [] if algorithm == "PI" else None,
                    "bundle_cells": [] if algorithm == "HIPC" else None,
                }])
            for order, slot in enumerate(self.allocator.path):
                if self.state.claim_owner[slot] != self.state.robot_index:
                    continue
                cell = self.state.decode_cell(self.state.targets[slot])
                message = {
                    "type": message_type,
                    "sender": self.state.robot_id,
                    "x": int(cell[0]),
                    "y": int(cell[1]),
                    "timestamp": int(self.state.claim_epoch[slot]),
                    "order": order,
                }
                if algorithm == "PI":
                    message["owner"] = self.state.robot_id
                    message["significance"] = float(
                        self.state.claim_value[slot]
                    )
                    message["path_cells"] = path_cells
                    message["path_size"] = len(path_cells)
                else:
                    message["winner"] = self.state.robot_id
                    message["bid"] = float(
                        self.state.claim_value[slot]
                    )
                    message["bundle_cells"] = path_cells
                    message["bundle_size"] = len(path_cells)
                messages.append(message)
            return self._record_message_behavior(messages)

        if (
            not self.synchronized_authoritative_state
            and algorithm != "ACBBA"
        ):
            return self._record_message_behavior(generated)

        combined = list(self.authoritative_message_seed)
        combined.extend(generated)
        # Both CBAA and ACBBA expose a final delta per cell. A native choose
        # result supersedes a pre-call pending delta for that same table cell.
        by_cell = {}
        extras = []
        for message in combined:
            if not isinstance(message, dict):
                continue
            if "x" in message and "y" in message:
                by_cell[(int(message["x"]), int(message["y"]))] = message
            else:
                extras.append(message)
        messages = extras + [by_cell[key] for key in sorted(by_cell)]
        if algorithm in ("CBAA", "ACBBA"):
            # Desktop consensus allocators treat their pending tables as delta
            # caches. A refresh during choose() is suppressed when its logical
            # signature is identical to the last one sent. Apply the same
            # suppression *after* native and frozen pending deltas have been
            # coalesced per cell: a native refresh that returns to the
            # last-sent signature must also cancel an older frozen delta.
            last_sent = {}
            for item in self.behavior_last_sent:
                try:
                    last_sent[tuple(item["cell"])] = item
                except (KeyError, TypeError):
                    continue
            filtered = []
            for message in messages:
                try:
                    cell = (int(message["x"]), int(message["y"]))
                    previous = last_sent.get(cell)
                    owner = message.get("winner", message.get("owner"))
                    value = float(
                        message.get(
                            "bid",
                            message.get("significance", message.get("value")),
                        )
                    )
                    same_owner = (
                        previous is not None
                        and previous.get("owner")
                        == (None if owner is None else str(owner))
                    )
                    same_value = (
                        previous is not None
                        and abs(float(previous["value"]) - value)
                        <= self.allocator.EPS
                    )
                    same_time = True
                    if algorithm == "ACBBA":
                        same_time = (
                            previous is not None
                            and abs(
                                float(previous["timestamp"])
                                - float(_protocol_timestamp(message["timestamp"]))
                            )
                            <= self.allocator.EPS
                        )
                    if same_owner and same_value and same_time:
                        continue
                except (KeyError, TypeError, ValueError):
                    pass
                filtered.append(message)
            messages = filtered
        if algorithm == "ACBBA" and self.allocator.path:
            path_cells = []
            path_index = {}
            for order, slot in enumerate(self.allocator.path):
                cell = self.state.decode_cell(self.state.targets[slot])
                pair = [int(cell[0]), int(cell[1])]
                path_cells.append({"x": pair[0], "y": pair[1]})
                path_index[(pair[0], pair[1])] = order
            for message in messages:
                if not isinstance(message, dict):
                    continue
                try:
                    cell = (int(message["x"]), int(message["y"]))
                except (KeyError, TypeError, ValueError):
                    continue
                owner = message.get("winner", message.get("owner"))
                if (
                    str(owner) == self.state.robot_id
                    and cell in path_index
                ):
                    message["order"] = path_index[cell]
                    message["bundle_cells"] = list(path_cells)
                    message["bundle_size"] = len(path_cells)
            messages = sorted(
                messages,
                key=lambda message: (
                    0,
                    path_index[
                        (int(message["x"]), int(message["y"]))
                    ],
                )
                if (
                    isinstance(message, dict)
                    and "x" in message
                    and "y" in message
                    and (int(message["x"]), int(message["y"]))
                    in path_index
                )
                else (
                    1,
                    int(message.get("x", -1))
                    if isinstance(message, dict) else -1,
                    int(message.get("y", -1))
                    if isinstance(message, dict) else -1,
                ),
            )
        return self._record_message_behavior(messages)

    def _path_signature_matches_last_sent(self):
        """Match PI/HIPC's desktop full-path last-sent suppression rule."""

        if not self.behavior_last_sent_initialized:
            return False
        if len(self.allocator.path) != len(self.behavior_last_sent):
            return False
        for slot, previous in zip(
            self.allocator.path, self.behavior_last_sent
        ):
            try:
                cell = self.state.decode_cell(self.state.targets[slot])
                if [int(cell[0]), int(cell[1])] != list(previous["cell"]):
                    return False
                if (
                    abs(
                        float(self.state.claim_value[slot])
                        - float(previous["value"])
                    )
                    > self.allocator.EPS
                ):
                    return False
                if int(self.state.claim_epoch[slot]) != int(
                    previous["timestamp"]
                ):
                    return False
            except (KeyError, TypeError, ValueError, IndexError):
                return False
        return True

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
            "behavior": {
                "call_mechanism": (
                    self.allocator.last_call_path
                    if "collision" in str(self.allocator.last_call_path)
                    else (
                        "allocation_epoch"
                        if self.last_call_had_epoch_reallocation
                        else self.allocator.last_call_path
                    )
                ),
                "pending_messages": [],
                "pending_snapshot": False,
                "last_sent": self.behavior_last_sent,
                "protocol_counter": int(
                    getattr(
                        self.allocator,
                        "time_counter",
                        getattr(
                            self.allocator,
                            "bid_counter",
                            self.state.event_counter,
                        ),
                    )
                ),
                "bad_prediction_count": dict(
                    getattr(self.allocator, "bad_prediction_count", {})
                ),
                "dropped_peers": list(
                    getattr(self.allocator, "dropped_peers", ())
                ),
                "last_predicted_peer_first_task": dict(
                    getattr(
                        self.allocator,
                        "last_predicted_peer_first_task",
                        {},
                    )
                ),
                "seen_peer_bundle_signature": dict(
                    getattr(
                        self.allocator,
                        "seen_peer_bundle_signature",
                        {},
                    )
                ),
            },
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

    def algorithm_epoch_reset_time_us(self):
        """Policy-induced allocator callback time awaiting the next call."""

        return max(0, int(self.pending_algorithm_epoch_reset_us))

    def candidate_counts(self):
        self._require_trial()
        return (
            int(self.state.candidate_count_before),
            int(self.state.candidate_count_after),
        )

    def call_class(self):
        self._require_trial()
        if self.last_call_had_epoch_reallocation:
            return "full_allocation_solve"
        path = str(self.allocator.last_call_path)
        if self.algorithm in ("DGA", "DMCHBA") and path in (
            "path_empty",
            "collision_replan",
            "received_better_solution",
        ):
            return "full_allocation_solve"
        if self.algorithm == "HIPC" and self.state.filter_invocations:
            return "full_allocation_solve"
        # Match the authoritative mechanism definition: PI uses an observable
        # path delta; ACBBA additionally exposes its transient collision refill
        # marker even if the final bundle returns to the same value.
        if self.algorithm in ("ACBBA", "PI") and (
            getattr(self, "_pre_choose_path", ())
            != getattr(self, "_post_choose_path", ())
            or (self.algorithm == "ACBBA" and path == "collision_replan")
        ):
            return "partial_bundle_refill"
        if self.state.filter_invocations:
            return "candidate_filter_only"
        return "cached_or_maintenance"


def create_persistent_runtime(config):
    """Factory used unchanged by HIL and future physical mission wrappers."""

    return PersistentCollaborativeRuntime(config)
