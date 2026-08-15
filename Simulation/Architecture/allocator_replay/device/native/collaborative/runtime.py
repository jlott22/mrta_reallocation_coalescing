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
        self.pending_allocator_events = []
        self.pending_message_sequences = {}
        self.pending_admitted_cells = set()
        self.pending_post_admission_active = None
        self.pending_post_admission_unavailable = None
        self.pending_post_admission_completed = None
        self.pending_post_admission_activated = None
        self.pending_post_admission_probabilities = None
        self.pending_algorithm_epoch_reset_us = 0
        self.last_call_had_recovery = False

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

        if algorithm in ("CBAA", "ACBBA", "PI", "HIPC"):
            # The experimental allocators must see the complete locally known
            # admitted pool.  Legacy candidate and bundle caps would otherwise
            # reintroduce residual tasks independently of coalescing.
            merged["max_candidate_cells"] = None

        self.config = merged
        self.algorithm = algorithm
        state_initial = dict(initial)
        if isinstance(resume, dict):
            state_resume = _plain_mapping(resume.get("state"))
            if state_resume:
                # Let restore_resume append learned cells in their saved slot
                # order.  No future all_tasks list is required or accepted.
                state_initial["active_tasks"] = []
                state_initial["admitted_task_registry"] = []
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
            # after restoring allocator-owned state, but do not learn an
            # unknown coordinate from the checkpoint.  Its queued admission
            # event will append it after reset.
            if "active_tasks" in initial:
                active = self.state._normalize_cell_collection(
                    initial["active_tasks"]
                )
                self.state._replace_active(
                    [
                        cell
                        for cell in active
                        if cell in self.state.slot_by_cell
                    ],
                    register_unknown=False,
                )
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
            behavior = _plain_mapping(resume.get("behavior"))
            if "last_sent" in behavior:
                self.behavior_last_sent = [
                    dict(item)
                    for item in behavior.get("last_sent", ())
                    if isinstance(item, dict)
                ]
                self.behavior_last_sent_initialized = True
            self.authoritative_message_seed = [
                dict(item)
                for item in behavior.get("pending_messages", ())
                if isinstance(item, dict)
            ]
            self.authoritative_pending_snapshot = bool(
                behavior.get("pending_snapshot", False)
            )
        self._synchronize_authoritative_state(initial)
        self.last_delta_sequence = int(
            resume.get("last_delta_sequence", -1)
        ) if isinstance(resume, dict) else -1
        self.call_index = int(
            resume.get("call_index", 0)
        ) if isinstance(resume, dict) else 0
        self.epoch_reallocation_pending = False
        self.last_call_had_epoch_reallocation = False
        self.last_call_had_recovery = False
        self.pending_allocator_events = []
        self.pending_message_sequences = {}
        self.pending_admitted_cells = set()
        self.pending_post_admission_active = None
        self.pending_post_admission_unavailable = None
        self.pending_post_admission_completed = None
        self.pending_post_admission_activated = None
        self.pending_post_admission_probabilities = None
        self.pending_algorithm_epoch_reset_us = 0
        return {
            "mission": "collaborative_visit",
            "algorithm": self.algorithm,
            "robot_id": self.state.robot_id,
            "grid_size": int(self.state.grid_size),
            "target_capacity": int(self.state.max_targets),
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
        # These causal measurement fields are common to both a desktop frozen
        # checkpoint and the compact native checkpoint used by preflight. They
        # must be restored even when no desktop owner/path maps are present.
        state = self.state
        if "candidate_count_before_filter" in flattened:
            state.candidate_count_before = max(
                0, int(flattened["candidate_count_before_filter"])
            )
        if "candidate_count_after_filter" in flattened:
            state.candidate_count_after = max(
                0, int(flattened["candidate_count_after_filter"])
            )
        if owner_name not in flattened or path_name not in flattened:
            return False

        owners = _mapping(flattened.get(owner_name))
        values = _mapping(flattened.get(value_name))
        times = _mapping(flattened.get(time_name)) if time_name else {}
        if "last_allocation_epoch_index" in flattened:
            state.last_allocation_epoch_index = int(
                flattened["last_allocation_epoch_index"]
            )
        if "last_allocation_epoch_reason" in flattened:
            state.last_allocation_epoch_reason = str(
                flattened["last_allocation_epoch_reason"]
            )
        if "last_allocation_epoch_admitted" in flattened:
            admitted = state._normalize_cell_collection(
                flattened["last_allocation_epoch_admitted"]
            )
            del state.last_allocation_epoch_admitted[:]
            for encoded in admitted:
                if encoded in state.slot_by_cell:
                    state.last_allocation_epoch_admitted.append(encoded)
        if "last_event" in flattened:
            state.last_event = str(flattened["last_event"])
        if "current_goal" in flattened:
            raw_goal = _decoded(flattened.get("current_goal"), None)
            state.current_goal = (
                None
                if raw_goal is None
                else state.encode_cell(raw_goal)
            )
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

        self.pending_allocator_events = []
        self.pending_message_sequences = {}
        self.pending_admitted_cells = set()
        self.pending_post_admission_active = None
        self.pending_post_admission_unavailable = None
        self.pending_post_admission_completed = None
        self.pending_post_admission_activated = None
        self.pending_post_admission_probabilities = None
        self.pending_algorithm_epoch_reset_us = 0

    def _require_trial(self):
        if self.state is None or self.allocator is None:
            raise RuntimeError("reset_trial must be called first")

    @staticmethod
    def _cell_collection(value):
        """Normalize one cell-shaped value without decoding its coordinates."""

        if isinstance(value, dict):
            return [value]
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and isinstance(value[0], int)
        ):
            return [value]
        return value

    def _queue_allocator_event(self, kind, payload, event_counter):
        """Queue decoded allocator input without executing allocator logic."""

        kind = str(kind)
        if kind == "allocator_message" and isinstance(payload, dict):
            payload = self._intern_message_cell_sequences(payload)
        if kind == "task_announcement":
            kind = "allocation_epoch"
        if kind == "allocation_epoch":
            if not isinstance(payload, dict):
                raise TypeError("allocation epoch payload must be a mapping")
            # Decode/register shell knowledge during PSETUP, but leave the new
            # slots inactive until the ordered allocator admission hook runs in
            # W_alloc.  Thus an earlier queued peer message cannot use a task
            # before the corresponding announcement callback.
            admitted = payload.get("admitted_cells", ())
            for encoded in self.state._normalize_cell_collection(admitted):
                self.pending_admitted_cells.add(encoded)
            self.state._register_cells(admitted)
            self.pending_allocator_events.append(
                ("__allocator_admission__", payload, int(event_counter))
            )
            return
        self.pending_allocator_events.append(
            (kind, payload, int(event_counter))
        )

    def _intern_message_cell_sequences(self, payload):
        """Share repeated decoded path/bundle arrays across one call.

        PI, ACBBA, and HIPC snapshots can contain one entry per task, with the
        same full 50-cell path repeated in every entry.  Wire compaction keeps
        each event below the serial staging bound; interning here also avoids
        retaining 50 independent decoded copies beside the resident contexts.
        This is transport/state ingestion and intentionally precedes W_alloc.
        """

        result = payload
        for field in ("path_cells", "bundle_cells"):
            raw = payload.get(field)
            if not isinstance(raw, list):
                continue
            normalized = []
            flat = []
            try:
                for item in raw:
                    if isinstance(item, dict):
                        cell = (int(item["x"]), int(item["y"]))
                    else:
                        cell = (int(item[0]), int(item[1]))
                    normalized.append(cell)
                    flat.extend(cell)
            except (KeyError, TypeError, ValueError, IndexError):
                continue
            key = (field, tuple(flat))
            shared = self.pending_message_sequences.get(key)
            if shared is None:
                shared = normalized
                self.pending_message_sequences[key] = shared
            if result is payload:
                result = dict(payload)
            result[field] = shared
        return result

    def _guard_announced_unknown(self, encoded, source):
        unknown = [
            cell
            for cell in encoded
            if cell not in self.state.slot_by_cell
        ]
        unannounced = [
            cell for cell in unknown
            if cell not in self.pending_admitted_cells
        ]
        if unannounced:
            raise ValueError(
                source + " contains a task not learned through admission"
            )
        return unknown

    def apply_delta(self, delta):
        """Patch environment state and queue allocator inputs outside timing."""

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

        # Decode and retain allocator inputs in their causal order.  PSETUP
        # performs no consensus callback, recovery rule, or admission hook;
        # those operations are drained by choose_goal inside the worker timer.
        queued_event_counter = int(state.event_counter)
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
            self._queue_allocator_event(
                "allocator_message", payload, queued_event_counter
            )

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
                        self._queue_allocator_event(
                            "allocator_message",
                            payload,
                            queued_event_counter,
                        )
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
            self._queue_allocator_event(
                kind, payload, queued_event_counter
            )

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
            if self.pending_admitted_cells:
                self.pending_post_admission_probabilities = probabilities
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

        # Apply the known portion of the authoritative checkpoint now.  Newly
        # announced cells have shell registry slots but remain inactive until
        # their timed admission callback; a final small reconciliation below
        # preserves admitted-then-completed post-event ordering.
        if "active_tasks" in flattened:
            active = state._normalize_cell_collection(
                flattened["active_tasks"]
            )
            self._guard_announced_unknown(active, "active_tasks")
            known = [
                cell
                for cell in active
                if (
                    cell in state.slot_by_cell
                    and cell not in self.pending_admitted_cells
                )
            ]
            state._replace_active(
                known, register_unknown=False
            )
            if self.pending_admitted_cells:
                self.pending_post_admission_active = active
        if any(name in flattened for name in unavailable_names):
            unavailable = tuple(
                flattened.get(name, ()) for name in unavailable_names
            )
            state.replace_unavailable(*unavailable)
            if self.pending_admitted_cells:
                self.pending_post_admission_unavailable = unavailable
        if completed is not None:
            completed = self._cell_collection(completed)
            state.complete_cells(completed)
            if self.pending_admitted_cells:
                self.pending_post_admission_completed = completed
        if activated is not None:
            activated = self._cell_collection(activated)
            encoded = state._normalize_cell_collection(activated)
            self._guard_announced_unknown(encoded, "activated_tasks")
            state.activate_cells(
                [
                    cell
                    for cell in encoded
                    if (
                        cell in state.slot_by_cell
                        and cell not in self.pending_admitted_cells
                    )
                ]
            )
            if self.pending_admitted_cells:
                self.pending_post_admission_activated = activated

        # Frozen state translation and transport decoding stay outside W_alloc.
        # The queued callbacks still execute inside the timer, on top of this
        # authoritative checkpoint.  Consensus messages are idempotent under
        # the protocol timestamps/ownership rules used by the four algorithms.
        self._synchronize_authoritative_state(flattened)

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

    def _drain_allocator_events(self):
        """Execute queued callbacks as the first part of W_alloc."""

        state = self.state
        events = self.pending_allocator_events
        self.pending_allocator_events = []
        choose_counter = int(state.event_counter)
        saw_epoch = False
        allocation_reason = ""
        recovery_requested = False
        recovery_applied = False
        for kind, payload, event_counter in events:
            state.event_counter = int(event_counter)
            try:
                if kind == "allocator_call_reason":
                    if not isinstance(payload, dict):
                        raise TypeError(
                            "allocator call reason payload must be a mapping"
                        )
                    allocation_reason = str(
                        payload.get("trigger_reason", "")
                    )
                    state.active_allocation_reason = allocation_reason
                elif kind == "allocator_message":
                    self.allocator.handle_message(payload)
                elif kind in ("allocation_epoch", "__allocator_admission__"):
                    # The desktop call classifier treats the presence of an
                    # allocation-epoch callback as a full solve even when a
                    # checkpointed restore has already synchronized the same
                    # epoch metadata and the callback is idempotent.
                    epoch_index = int(payload.get("epoch_index", -1))
                    reason = str(payload.get("trigger_reason", ""))
                    admitted = payload.get("admitted_cells", ())
                    epoch_was_synchronized = (
                        epoch_index == state.last_allocation_epoch_index
                    )
                    if epoch_was_synchronized:
                        synchronized_admitted = []
                        seen_admitted = set()
                        for encoded in state._normalize_cell_collection(
                            admitted
                        ):
                            if encoded not in seen_admitted:
                                seen_admitted.add(encoded)
                                synchronized_admitted.append(encoded)
                        if (
                            reason != state.last_allocation_epoch_reason
                            or synchronized_admitted
                            != [
                                int(item)
                                for item in state.last_allocation_epoch_admitted
                            ]
                        ):
                            raise ValueError(
                                "duplicate allocation epoch metadata changed"
                            )
                        epoch_changed = False
                    else:
                        state.activate_cells(admitted)
                        for encoded in state._normalize_cell_collection(admitted):
                            slot = state.slot_by_cell.get(encoded)
                            if slot is not None:
                                state.unavailable[slot] = 0
                        epoch_changed = state.apply_allocation_epoch(
                            epoch_index, reason, admitted
                        )
                    if epoch_changed:
                        allocation_reason = reason
                        state.active_allocation_reason = reason
                        self.allocator.on_allocation_epoch(
                            reason, admitted, epoch_index
                        )
                    if epoch_changed or str(
                        self.config.get("logical_context_execution", "")
                    ) == "checkpointed_time_multiplexing":
                        saw_epoch = True
                elif kind in (
                    "recover_stalled_allocation",
                    "allocator_recovery",
                    "stalled_recovery",
                ):
                    recovery_requested = True
                    recovery_applied = bool(
                        self.allocator.recover_stalled_allocation(payload)
                    ) or recovery_applied
                elif kind == "allocator_task_completed":
                    if not isinstance(payload, dict) or "cell" not in payload:
                        raise TypeError(
                            "allocator task completion requires a cell payload"
                        )
                    completion_hook = getattr(
                        self.allocator, "on_task_completed", None
                    )
                    if callable(completion_hook):
                        completion_hook(
                            payload["cell"],
                            payload.get("reason", ""),
                            bool(payload.get("local", False)),
                        )
                    # A release created by the explicit completion callback is
                    # causally meaningful even though the task becomes
                    # inactive immediately afterward. Tag it internally so
                    # the common inactive-traffic filter retains it; the tag
                    # is removed before hashing or serialization.
                    try:
                        completed_cell = state.decode_cell(
                            state.encode_cell(payload["cell"])
                        )
                        for queued in state.outbox:
                            if (
                                isinstance(queued, dict)
                                and int(queued.get("x", -1))
                                == int(completed_cell[0])
                                and int(queued.get("y", -1))
                                == int(completed_cell[1])
                            ):
                                queued["_causal_completion_release"] = True
                    except (TypeError, ValueError, KeyError, IndexError):
                        pass
                    state.complete_cells([payload["cell"]])
                elif kind in (
                    "on_collision_avoidance_activated",
                    "collision_avoidance",
                ):
                    state.set_collision(True)
            finally:
                state.event_counter = choose_counter

        # Reconcile only after all ordered callbacks.  This covers a task that
        # was announced and completed before the same choose transaction while
        # ensuring the coordinate first entered state through the announcement.
        if self.pending_post_admission_active is not None:
            state._replace_active(
                self.pending_post_admission_active,
                register_unknown=False,
            )
        if self.pending_post_admission_unavailable is not None:
            state.replace_unavailable(
                *self.pending_post_admission_unavailable
            )
        if self.pending_post_admission_completed is not None:
            state.complete_cells(self.pending_post_admission_completed)
        if self.pending_post_admission_activated is not None:
            state.activate_cells(self.pending_post_admission_activated)
        if self.pending_post_admission_probabilities is not None:
            state.update_probabilities(
                self.pending_post_admission_probabilities
            )

        self.pending_admitted_cells = set()
        self.pending_post_admission_active = None
        self.pending_post_admission_unavailable = None
        self.pending_post_admission_completed = None
        self.pending_post_admission_activated = None
        self.pending_post_admission_probabilities = None
        self.pending_message_sequences = {}
        return (
            saw_epoch,
            recovery_requested,
            recovery_applied,
            allocation_reason,
        )

    def choose_goal(self):
        """Run the complete allocator transaction under the worker timer."""

        self._require_trial()
        state = self.state
        state.begin_allocator_call()
        self._pre_choose_path = list(self.allocator.path)
        (
            saw_epoch,
            recovery_requested,
            recovery_applied,
            allocation_reason,
        ) = (
            self._drain_allocator_events()
        )
        epoch_reallocation = bool(
            saw_epoch or self.epoch_reallocation_pending
        )
        try:
            goal = self.allocator.choose()
        finally:
            state.active_allocation_reason = ""
        self.last_call_had_epoch_reallocation = epoch_reallocation
        self.last_call_had_recovery = recovery_requested
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
                "recovery_requested": recovery_requested,
                "recovery_applied": recovery_applied,
                "trigger_reason": allocation_reason,
                "allocation_epoch_index": int(
                    state.last_allocation_epoch_index
                ),
            },
        )

    def drain_messages(self):
        """Serialize/drain outbound allocator messages outside timed allocation."""

        self._require_trial()
        generated = self.state.drain_messages()
        # A completed/inactive task has no remaining allocator-visible
        # receiver effect. Desktop pending-delta maps discard this obsolete
        # traffic; apply the same common rule to every compact hardware
        # allocator before hashes, last-sent state, or serialization.
        active_generated = []
        for message in generated:
            completion_release = False
            if isinstance(message, dict):
                completion_release = bool(
                    message.pop("_causal_completion_release", False)
                )
            if not isinstance(message, dict) or not (
                "x" in message and "y" in message
            ):
                active_generated.append(message)
                continue
            try:
                slot = self.state.slot_for_cell(
                    (int(message["x"]), int(message["y"]))
                )
            except (TypeError, ValueError, KeyError):
                slot = None
            if completion_release or (
                slot is not None and self.state.is_active(slot)
            ):
                active_generated.append(message)
        generated = active_generated
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
                    # All entries describe one immutable snapshot; sharing the
                    # list avoids O(n^2) resident references before streaming.
                    message["bundle_cells"] = path_cells
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
                        "allocator_recovery"
                        if self.last_call_had_recovery
                        else (
                            "allocation_epoch"
                            if self.last_call_had_epoch_reallocation
                            else self.allocator.last_call_path
                        )
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
        """Legacy timing component; admission ingestion is intentionally zero."""

        return 0

    def candidate_counts(self):
        self._require_trial()
        return (
            int(self.state.candidate_count_before),
            int(self.state.candidate_count_after),
        )

    def call_class(self):
        self._require_trial()
        if (
            self.last_call_had_epoch_reallocation
            or self.last_call_had_recovery
            or str(self.state.last_event) in (
                "task_admission",
                "allocation_epoch",
            )
        ):
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
