"""One-board causal timing provider with four persistent logical contexts."""

from __future__ import annotations

import copy
import hashlib
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from allocator_replay.capture.codec import canonical_json_bytes, decode_value

from .binding import BoardFingerprint, BoardLease, StableBoardBinding
from .errors import (
    BoardBindingError,
    CausalTimingError,
    DeviceCallError,
    ParityFailure,
    SessionStateError,
    StaleReplyError,
)
from .parity import projected_parity
from .types import (
    DecisionSignature,
    FrozenCall,
    MeasuredCall,
    MissionBinding,
    PRIMARY_ALGORITHMS,
    semantic_hash,
    validate_same_time_group,
)


PERSISTENT_EVENT_BATCH_BYTES = 768


def persistent_event_batches(
    events: Sequence[Mapping[str, Any]],
    max_bytes: int = PERSISTENT_EVENT_BATCH_BYTES,
) -> list[list[Mapping[str, Any]]]:
    """Return one bounded PSETUP stage per ordered callback event.

    An allocator callback is atomic and cannot be split safely.  Reject an
    unexpectedly large event on the host instead of reconstructing an
    unbounded event queue beside four resident contexts on the controller.
    """

    if max_bytes < 2:
        raise ValueError("max_bytes must fit an empty JSON array")
    batches: list[list[Mapping[str, Any]]] = []
    for event in events:
        batch = [event]
        encoded_size = len(canonical_json_bytes(batch))
        if encoded_size > max_bytes:
            raise ValueError(
                "persistent callback event exceeds bounded setup payload: "
                f"{encoded_size} > {max_bytes} bytes"
            )
        batches.append(batch)
    return batches


def _empty_persistent_state() -> dict[str, dict[str, Any]]:
    return {
        section: {}
        for section in (
            "robot_attrs",
            "views",
            "cfg",
            "belief",
            "allocator_attrs",
        )
    }


def _field(value: Any, *names: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _coerce_signature(value: Any) -> DecisionSignature:
    if isinstance(value, DecisionSignature):
        return value
    if all(
        hasattr(value, name)
        for name in (
            "goal",
            "active_candidate_count",
            "message_sha256",
            "post_state_sha256",
            "call_class",
        )
    ):
        goal = getattr(value, "goal")
        return DecisionSignature(
            goal=None if goal is None else (int(goal[0]), int(goal[1])),
            active_candidate_count=int(getattr(value, "active_candidate_count")),
            message_sha256=str(getattr(value, "message_sha256")),
            post_state_sha256=str(getattr(value, "post_state_sha256")),
            call_class=str(getattr(value, "call_class")),
        )
    if isinstance(value, Mapping) and (
        "message_sha256" in value or "message_hash" in value
    ) and ("post_state_sha256" in value or "post_state_hash" in value):
        goal = value.get("goal")
        return DecisionSignature(
            goal=(None if goal is None else (int(goal[0]), int(goal[1]))),
            active_candidate_count=int(
                value.get("active_candidate_count", value.get("candidate_count", 0))
            ),
            message_sha256=str(
                value.get("message_sha256", value.get("message_hash"))
            ),
            post_state_sha256=str(
                value.get("post_state_sha256", value.get("post_state_hash"))
            ),
            call_class=str(value.get("call_class", value.get("call_type", ""))),
        )
    return DecisionSignature.from_result(value)


def coerce_frozen_call(value: Any) -> FrozenCall:
    """Adapt a structural simulator call object to the public provider type."""

    if isinstance(value, FrozenCall):
        return value
    robot_id = str(
        _field(value, "logical_robot_id", "logical_context_id", "robot_id", default="")
    )
    pre_state = _field(value, "pre_state", default=None)
    device_setup = _field(value, "device_setup", default=None)
    if device_setup is None:
        if pre_state is None:
            raise ValueError("structural causal call needs device_setup or pre_state")
        metadata = dict(_field(value, "metadata", default={}) or {})
        device_setup = {
            "pre_state": copy.deepcopy(pre_state),
            "events": copy.deepcopy(
                _field(
                    value,
                    "device_events",
                    "events",
                    default=metadata.get("device_events", []),
                )
            ),
            "deleted": copy.deepcopy(metadata.get("device_deleted", {})),
            "resume_state": copy.deepcopy(metadata.get("device_resume_state", {})),
            "setup_mode": "restore",
        }
    authoritative = _field(value, "authoritative", default=None)
    if authoritative is None:
        goal = _field(value, "authoritative_goal", "selected_goal", "goal")
        count = _field(
            value,
            "authoritative_candidate_count",
            "candidate_count",
            "active_candidate_count",
        )
        message_hash = _field(
            value,
            "message_sha256",
            "message_hash",
            "authoritative_message_sha256",
            "authoritative_message_hash",
        )
        state_hash = _field(
            value,
            "post_state_sha256",
            "post_state_hash",
            "authoritative_post_state_sha256",
            "authoritative_post_state_hash",
        )
        call_class = _field(value, "call_class", "call_type", default="")
        if count is None or message_hash is None or state_hash is None:
            raise ValueError("structural causal call lacks authoritative parity fields")
        authoritative = DecisionSignature(
            goal=None if goal is None else (int(goal[0]), int(goal[1])),
            active_candidate_count=int(count),
            message_sha256=str(message_hash),
            post_state_sha256=str(state_hash),
            call_class=str(call_class),
        )
    else:
        authoritative = _coerce_signature(authoritative)
    agx_us = _field(value, "agx_allocator_time_us", default=None)
    if agx_us is None:
        agx_ns = _field(value, "agx_duration_ns", "agx_allocator_time_ns", default=None)
        if agx_ns is not None:
            agx_us = int(agx_ns) // 1000
        else:
            agx_s = _field(value, "agx_duration_s", "agx_allocator_time_s", default=0.0)
            agx_us = round(float(agx_s) * 1_000_000)
    return FrozenCall(
        call_id=str(_field(value, "call_id", default="")),
        group_id=str(_field(value, "group_id", default="")),
        trial_id=str(_field(value, "trial_id", default="")),
        logical_robot_id=robot_id,
        algorithm=str(_field(value, "algorithm", default="")),
        virtual_start_s=float(
            _field(value, "virtual_start_s", "compute_start_s", default=0.0)
        ),
        device_setup=device_setup,
        authoritative=authoritative,
        agx_allocator_time_us=int(agx_us),
        trigger_id=str(_field(value, "trigger_id", "epoch_id", default="")),
        active_task_count=_field(value, "active_task_count", default=None),
        metadata=dict(_field(value, "metadata", default={}) or {}),
    )


def coerce_mission_binding(value: Any) -> MissionBinding:
    """Adapt a structural causal-core mission description."""

    if isinstance(value, MissionBinding):
        return value
    robot_ids = tuple(
        str(item)
        for item in _field(
            value,
            "robot_ids",
            "logical_context_ids",
            default=("robot_0", "robot_1", "robot_2", "robot_3"),
        )
    )
    raw_states = _field(
        value,
        "initial_context_states",
        "context_states",
        default=None,
    )
    if raw_states is None:
        empty = {
            "robot_attrs": {},
            "views": {},
            "cfg": {},
            "belief": {},
            "allocator_attrs": {},
        }
        states = {item: copy.deepcopy(empty) for item in robot_ids}
    else:
        states = {
            str(key): copy.deepcopy(item) for key, item in raw_states.items()
        }
    trial_config = _field(value, "trial_config", "config", default={}) or {}
    return MissionBinding(
        trial_id=str(_field(value, "trial_id", default="")),
        condition_id=str(_field(value, "condition_id", default="")),
        algorithm=str(_field(value, "algorithm", default="")),
        seed=int(_field(value, "seed", "runtime_seed", default=0)),
        robot_ids=robot_ids,
        trial_config=dict(trial_config),
        initial_context_states=states,
    )


class CausalBoardSession:
    """A timing provider owned by one simulation worker and one board.

    Four logical contexts persist as independently restored device snapshots.
    The controller can execute only one physical request at a time, so a
    same-time group is measured serially.  All requests are detached before
    the first request, all outputs are withheld until the group passes parity,
    and virtual completion uses each request's own duration from the common
    start.  Thus measurement order cannot serialize virtual compute.
    """

    def __init__(
        self,
        binding: StableBoardBinding,
        *,
        lock_root: Path,
        lease: BoardLease | None = None,
        timeout_seconds: float = 30.0,
        require_primary_algorithm: bool = True,
        require_split_transport: bool = True,
        reset_worker_between_missions: bool = True,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.binding = binding
        self.device = binding.device
        self.fingerprint = binding.fingerprint
        self.timeout_seconds = float(timeout_seconds)
        self.require_primary_algorithm = bool(require_primary_algorithm)
        self.require_split_transport = bool(require_split_transport)
        self.reset_worker_between_missions = bool(reset_worker_between_missions)
        if lease is None:
            self.lease = BoardLease(
                self.fingerprint.device_id, lock_root
            ).acquire()
        else:
            if lease.board_id != self.fingerprint.device_id or not lease.acquired:
                raise SessionStateError(
                    "pre-acquired lease must own this exact board identity"
                )
            self.lease = lease
        self.mission: MissionBinding | None = None
        self.context_state: dict[str, dict[str, Any]] = {}
        self.context_call_count: dict[str, int] = {}
        self._seen_calls: set[str] = set()
        self._seen_groups: set[str] = set()
        self._attempt_sequence = 0
        self._measurement_sequence = 0
        self._session_nonce = uuid.uuid4().hex[:12]
        self.invalid_reason: str | None = None
        self.last_failure: dict[str, Any] | None = None
        self.closed = False

    @property
    def board_id(self) -> str:
        return self.fingerprint.device_id

    @property
    def serial_device(self) -> str:
        return self.fingerprint.port

    @property
    def valid(self) -> bool:
        return not self.closed and self.invalid_reason is None

    def _assert_usable(self) -> None:
        if self.closed:
            raise SessionStateError("causal board session is closed")
        if self.invalid_reason is not None:
            raise SessionStateError(
                f"causal board session was invalidated: {self.invalid_reason}"
            )

    def _invalidate(self, exc: BaseException, detail: Mapping[str, Any] | None = None) -> None:
        self.invalid_reason = f"{type(exc).__name__}: {exc}"
        self.last_failure = {
            "failure_type": type(exc).__name__,
            "message": str(exc),
            "board_id": self.board_id,
            "serial_device": self.serial_device,
            "trial_id": None if self.mission is None else self.mission.trial_id,
            **dict(detail or {}),
        }
        interrupt = getattr(self.device, "interrupt", None)
        if callable(interrupt):
            try:
                interrupt()
            except Exception:
                pass

    def _validate_live_fingerprint(self, device: Any | None = None) -> BoardFingerprint:
        current = BoardFingerprint.inspect(
            self.device if device is None else device,
            expected_build_id=self.fingerprint.build_id,
            expected_module_set_sha256=self.fingerprint.module_set_sha256,
        )
        if current.reconnect_key() != self.fingerprint.reconnect_key():
            raise BoardBindingError(
                "board identity/build/firmware changed after worker binding"
            )
        return current

    def _clean_worker(self) -> None:
        restart = getattr(self.device, "restart_clean_worker", None)
        if callable(restart):
            restart()
        self._validate_live_fingerprint()

    def begin_mission(self, mission: Any) -> None:
        self._assert_usable()
        mission = coerce_mission_binding(mission)
        if self.mission is not None:
            raise SessionStateError("a mission is already active on this board")
        if self.require_primary_algorithm and mission.algorithm not in PRIMARY_ALGORITHMS:
            raise SessionStateError(
                f"{mission.algorithm} is optional and cannot block the primary campaign"
            )
        if self.reset_worker_between_missions:
            self._clean_worker()
        config = copy.deepcopy(dict(mission.trial_config))
        config.update(
            {
                "schema": 1,
                "trial_key": mission.trial_id,
                "condition_id": mission.condition_id,
                "mission": "collaborative",
                "algorithm": mission.algorithm,
                "robot_ids": list(mission.robot_ids),
                "seed": int(mission.seed),
                "logical_context_count": len(mission.robot_ids),
            }
        )
        try:
            self.device.begin_persistent_trial(config)
        except Exception as exc:
            self._invalidate(exc, {"stage": "begin_mission"})
            raise DeviceCallError(f"failed to initialize four device contexts: {exc}") from exc
        self.mission = mission
        self.context_state = {
            robot_id: copy.deepcopy(dict(mission.initial_context_states[robot_id]))
            for robot_id in mission.robot_ids
        }
        self.context_call_count = {robot_id: 0 for robot_id in mission.robot_ids}
        self._seen_calls.clear()
        self._seen_groups.clear()
        self._attempt_sequence = 0
        self._measurement_sequence = 0

    def _attempt_id(self, call_id: str) -> str:
        self._attempt_sequence += 1
        digest = hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:12]
        return f"causal-{self._session_nonce}-{self._attempt_sequence:08d}-{digest}"

    def _device_setup(self, call: FrozenCall, attempt_id: str) -> dict[str, Any]:
        assert self.mission is not None
        setup = copy.deepcopy(dict(call.device_setup))
        supplied_context = setup.get("context_id")
        if supplied_context is not None and str(supplied_context) != call.logical_robot_id:
            raise SessionStateError("device setup context differs from logical robot ID")
        pre_state = setup.get("pre_state")
        if pre_state is None:
            pre_state = copy.deepcopy(self.context_state[call.logical_robot_id])
        setup.update(
            {
                "schema": 1,
                "fixture_id": f"causal/{self.mission.trial_id}/{call.call_id}",
                "condition_id": self.mission.condition_id,
                "mission": "collaborative",
                "algorithm": call.algorithm,
                "context_id": call.logical_robot_id,
                # Causal mode selects one of four resident native runtimes.
                # It deliberately avoids the legacy PCLEAR/restore path.
                "setup_mode": "causal_context",
                "deleted": copy.deepcopy(setup.get("deleted", {})),
                "events": copy.deepcopy(setup.get("events", [])),
                "resume_state": copy.deepcopy(setup.get("resume_state", {})),
                "pre_state": copy.deepcopy(pre_state),
                "causal_attempt_id": attempt_id,
                "causal_group_id": call.group_id,
                "causal_trial_id": call.trial_id,
            }
        )
        return setup

    @staticmethod
    def _decode_result(result: Mapping[str, Any]) -> tuple[
        DecisionSignature, tuple[int, int] | None, tuple[Any, ...], Mapping[str, Any]
    ]:
        goal_raw = decode_value(result.get("goal"))
        goal = None if goal_raw is None else (int(goal_raw[0]), int(goal_raw[1]))
        messages_raw = decode_value(result.get("messages", []))
        if not isinstance(messages_raw, (list, tuple)):
            raise DeviceCallError("device messages result is not a sequence")
        post_state = decode_value(result.get("post_state", {}))
        if not isinstance(post_state, Mapping):
            raise DeviceCallError("device post-state result is not a mapping")
        normalized = dict(result)
        normalized["goal"] = goal
        normalized["messages"] = list(messages_raw)
        normalized["post_state"] = dict(post_state)
        signature = DecisionSignature.from_result(normalized)
        return signature, goal, tuple(messages_raw), dict(post_state)

    @staticmethod
    def _parity_diagnostics(
        call: FrozenCall, device_signature: DecisionSignature
    ) -> dict[str, Any]:
        authoritative = call.authoritative
        mismatches: dict[str, dict[str, Any]] = {}
        for name in (
            "goal",
            "active_candidate_count",
            "message_sha256",
            "post_state_sha256",
            "call_class",
        ):
            expected = getattr(authoritative, name)
            actual = getattr(device_signature, name)
            if expected != actual:
                mismatches[name] = {"agx": expected, "rp2040": actual}
        return {
            "call_id": call.call_id,
            "group_id": call.group_id,
            "trial_id": call.trial_id,
            "logical_robot_id": call.logical_robot_id,
            "authoritative": authoritative.as_dict(),
            "device": device_signature.as_dict(),
            "mismatches": mismatches,
        }

    def _prepare_persistent_stages(
        self,
        setup: Mapping[str, Any],
        attempt_id: str,
    ) -> dict[str, int | None]:
        """Prepare one logical call with bounded ordered event transactions.

        Existing contexts receive callbacks against their resident pre-hook
        state before the complete authoritative checkpoint is synchronized.
        A context's first call must bootstrap its state first, matching the
        original create-then-apply behavior.  All stages remain outside the
        timed allocator region and share one scientific attempt ID.
        """

        prepare = getattr(self.device, "prepare_persistent_call")
        context_id = str(setup["context_id"])
        events = list(setup.get("events", ()) or ())
        event_stages = persistent_event_batches(events)
        first_context_call = self.context_call_count.get(context_id, 0) == 0

        checkpoint = dict(setup)
        checkpoint["events"] = []
        stages: list[dict[str, Any]] = []

        def event_stage(
            index: int,
            batch: list[Mapping[str, Any]],
        ) -> dict[str, Any]:
            return {
                "schema": int(setup.get("schema", 1)),
                "fixture_id": (
                    str(setup["fixture_id"])
                    + f"/event_stage_{index:04d}"
                ),
                "condition_id": setup["condition_id"],
                "mission": setup["mission"],
                "algorithm": setup["algorithm"],
                "context_id": context_id,
                "setup_mode": "causal_context",
                "deleted": {},
                "events": batch,
                "resume_state": {},
                "state_aliases": [],
                "pre_state": _empty_persistent_state(),
            }

        if first_context_call:
            stages.append(checkpoint)
            stages.extend(
                event_stage(index, batch)
                for index, batch in enumerate(event_stages)
            )
        else:
            stages.extend(
                event_stage(index, batch)
                for index, batch in enumerate(event_stages)
            )
            stages.append(checkpoint)

        psetup_total = 0
        host_cpu_total = 0
        device_setup_total = 0
        device_setup_reported = True
        for index, stage in enumerate(stages):
            stage["begin_call_setup"] = index == 0
            stage["end_call_setup"] = index + 1 == len(stages)
            metrics = prepare(stage, attempt_id)
            if not isinstance(metrics, Mapping):
                device_setup_reported = False
                continue
            psetup_total += max(
                0, int(metrics.get("psetup_transaction_us", 0) or 0)
            )
            host_cpu_total += max(
                0, int(metrics.get("host_prepare_cpu_us", 0) or 0)
            )
            raw_device_setup = metrics.get("device_pre_call_setup_us")
            if raw_device_setup is None:
                device_setup_reported = False
            else:
                device_setup_total += max(0, int(raw_device_setup))
        return {
            "psetup_transaction_us": psetup_total,
            "host_prepare_cpu_us": host_cpu_total,
            "device_pre_call_setup_us": (
                device_setup_total if device_setup_reported else None
            ),
        }

    def _measure_one(
        self,
        call: FrozenCall,
        setup: Mapping[str, Any],
        attempt_id: str,
        physical_index: int,
    ) -> MeasuredCall:
        prepare = getattr(self.device, "prepare_persistent_call", None)
        run = getattr(self.device, "run_persistent_ready", None)
        total_started = time.perf_counter_ns()
        psetup_transaction_us = 0
        ptime_result_transaction_us = 0
        device_pre_call_setup_us: int | None = None
        host_prepare_cpu_us = 0
        if callable(prepare) and callable(run):
            setup_started = time.perf_counter_ns()
            prepare_metrics = self._prepare_persistent_stages(
                setup, attempt_id
            )
            psetup_transaction_us = max(
                0, (time.perf_counter_ns() - setup_started) // 1000
            )
            if isinstance(prepare_metrics, Mapping):
                psetup_transaction_us = max(
                    0,
                    int(
                        prepare_metrics.get(
                            "psetup_transaction_us",
                            psetup_transaction_us,
                        )
                    ),
                )
                raw_device_setup = prepare_metrics.get(
                    "device_pre_call_setup_us"
                )
                device_pre_call_setup_us = (
                    None
                    if raw_device_setup is None
                    else max(0, int(raw_device_setup))
                )
                host_prepare_cpu_us = max(
                    0, int(prepare_metrics.get("host_prepare_cpu_us", 0))
                )
            serial_started = time.perf_counter_ns()
            result = run(attempt_id, self.timeout_seconds)
            ptime_result_transaction_us = max(
                0, (time.perf_counter_ns() - serial_started) // 1000
            )
        else:
            if self.require_split_transport:
                raise DeviceCallError(
                    "device lacks split prepare/run protocol required to isolate setup"
                )
            execute = getattr(self.device, "execute_persistent", None)
            if not callable(execute):
                raise DeviceCallError("device has no persistent call operation")
            serial_started = time.perf_counter_ns()
            result = execute(dict(setup), attempt_id, self.timeout_seconds)
            ptime_result_transaction_us = max(
                0, (time.perf_counter_ns() - serial_started) // 1000
            )
        serial_us = psetup_transaction_us + ptime_result_transaction_us
        # The current chunked protocol interleaves host JSON/base64 work with
        # acknowledged USB transfers. It does not expose a scientifically
        # separable serialization-only wall duration, so this legacy field is
        # zero plus an explicit `measured=False` marker below rather than a
        # mislabeled PSETUP transaction.
        setup_us = 0
        total_us = max(0, (time.perf_counter_ns() - total_started) // 1000)
        if not isinstance(result, Mapping):
            raise DeviceCallError("device returned a non-mapping result")
        reply_attempt = str(result.get("attempt_id", ""))
        if reply_attempt != attempt_id:
            raise StaleReplyError(
                f"reply attempt {reply_attempt!r} does not match {attempt_id!r}"
            )
        if str(result.get("status", "")) != "completed":
            raise DeviceCallError(
                "device call failed: " + str(result.get("failure_type", "unknown"))
            )
        device_duration_us = int(result.get("allocator_time_us", -1))
        if device_duration_us < 0:
            raise DeviceCallError("device returned an invalid allocator duration")
        device_choose_goal_us = int(
            result.get("choose_goal_time_us", device_duration_us)
        )
        algorithm_epoch_reset_us = int(
            result.get("algorithm_epoch_reset_us", 0)
        )
        if (
            device_choose_goal_us < 0
            or algorithm_epoch_reset_us < 0
            or device_duration_us
            != device_choose_goal_us + algorithm_epoch_reset_us
        ):
            raise DeviceCallError(
                "device allocator duration does not equal choose_goal plus "
                "algorithm epoch reset"
            )
        before = int(result.get("candidate_count_before", -1))
        after = int(result.get("candidate_count_after", -1))
        if before < 0 or after < 0 or before != after:
            raise DeviceCallError(
                f"invalid/unrestricted candidate counts: before={before}, after={after}"
            )
        signature, goal, messages, post_state = self._decode_result(result)
        diagnostics = self._parity_diagnostics(call, signature)
        parity_metadata: dict[str, Any] = {
            "parity_level": "strict_full_logical_hash",
            "representation_hashes_differ": False,
        }
        mismatches = diagnostics["mismatches"]
        projection_fields = {"message_sha256", "post_state_sha256"}
        if mismatches and set(mismatches).issubset(projection_fields):
            authoritative_messages = call.metadata.get("authoritative_messages")
            authoritative_post_state = call.metadata.get(
                "authoritative_post_state"
            )
            if (
                isinstance(authoritative_messages, (list, tuple))
                and isinstance(authoritative_post_state, Mapping)
            ):
                try:
                    projection = projected_parity(
                        algorithm=call.algorithm,
                        authoritative_messages=authoritative_messages,
                        device_messages=messages,
                        authoritative_post_state=authoritative_post_state,
                        device_post_state=post_state,
                        authoritative_call_class=(
                            call.authoritative.call_class
                        ),
                        device_call_class=signature.call_class,
                    )
                except Exception as exc:
                    diagnostics["projection_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    if projection["message_match"]:
                        mismatches.pop("message_sha256", None)
                    if projection["state_match"]:
                        mismatches.pop("post_state_sha256", None)
                    parity_metadata = {
                        "parity_level": "shared_logical_state_and_message_effect",
                        "representation_hashes_differ": True,
                    }
                    parity_metadata.update(
                        {
                            key: projection[key]
                            for key in (
                            "agx_message_projection_sha256",
                            "device_message_projection_sha256",
                            "agx_state_projection_sha256",
                            "device_state_projection_sha256",
                        )
                        }
                    )
                    diagnostics["projection"] = projection
        if diagnostics["mismatches"]:
            diagnostics.update(
                {
                    "board_id": self.board_id,
                    "serial_device": self.serial_device,
                    "attempt_id": attempt_id,
                    "device_allocator_time_us_invalid": device_duration_us,
                    "device_choose_goal_us_invalid": device_choose_goal_us,
                    "algorithm_epoch_reset_us_invalid": (
                        algorithm_epoch_reset_us
                    ),
                    "agx_allocator_time_us": call.agx_allocator_time_us,
                    "serial_roundtrip_us": int(serial_us),
                    "host_serialization_setup_us": int(setup_us),
                    "host_total_call_us": int(total_us),
                    "psetup_transaction_us": int(psetup_transaction_us),
                    "device_pre_call_setup_us": device_pre_call_setup_us,
                    "ptime_result_transaction_us": int(
                        ptime_result_transaction_us
                    ),
                    "host_prepare_cpu_us": int(host_prepare_cpu_us),
                    "candidate_count_before": before,
                    "candidate_count_after": after,
                    "frozen_pre_state_sha256": semantic_hash(
                        setup.get("pre_state", {})
                    ),
                    "frozen_events_sha256": semantic_hash(
                        setup.get("events", [])
                    ),
                    "duration_accepted": False,
                }
            )
            raise ParityFailure(
                f"AGX/RP2040 allocator parity failed for {call.call_id}", diagnostics
            )
        completion_s = float(call.virtual_start_s) + device_duration_us / 1_000_000.0
        implementation = self.fingerprint.implementation.lower()
        port_upper = self.fingerprint.port.upper()
        native_hardware = bool(
            implementation.startswith("micropython-")
            and "loopback" not in implementation
            and not port_upper.startswith(("LOOPBACK:", "VIRTUAL:"))
            and not self.fingerprint.virtual_device
            and self.fingerprint.timer_unit == "us"
            and self.fingerprint.timer_resolution_us > 0
            and self.fingerprint.timer_monotonic
            and self.fingerprint.timer_wraparound_safe
        )
        attestation: dict[str, Any] | None = None
        attestation_sha256 = ""
        host_serialization_setup_measured = False
        device_allocator_timer_scope = (
            "choose_goal plus policy-induced on_allocation_epoch allocator "
            "callback; excludes generic PSETUP state synchronization, USB, "
            "explicit pre-call GC, and post-call result serialization; GC "
            "triggered naturally inside either measured allocator operation "
            "remains included"
        )
        serial_roundtrip_definition = (
            "PSETUP transaction wall plus PTIME/result transaction wall; "
            "includes USB/protocol and device-side work and is excluded "
            "from causal compute duration"
        )
        if native_hardware:
            # This canonical per-call record binds the accepted duration to
            # the exact live identity/build/timer fingerprint selected during
            # worker binding.  It is an integrity seal for later config/output
            # validation, not a claim of public-key remote attestation.
            attestation = {
                "schema": 1,
                "attestation_kind": "rp2040_native_allocator_measurement",
                "device_id": self.fingerprint.device_id,
                "build_id": self.fingerprint.build_id,
                "firmware_sha256": self.fingerprint.firmware_sha256,
                "module_set_sha256": self.fingerprint.module_set_sha256,
                "implementation": self.fingerprint.implementation,
                "frequency_hz": self.fingerprint.frequency_hz,
                "timer_unit": self.fingerprint.timer_unit,
                "timer_resolution_us": self.fingerprint.timer_resolution_us,
                "timer_monotonic": self.fingerprint.timer_monotonic,
                "timer_wraparound_safe": (
                    self.fingerprint.timer_wraparound_safe
                ),
                "trial_id": call.trial_id,
                "group_id": call.group_id,
                "call_id": call.call_id,
                "logical_context_id": call.logical_robot_id,
                "attempt_id": attempt_id,
                "physical_measurement_index": physical_index,
                "timing_decomposition_schema": 2,
                "device_allocator_time_us": device_duration_us,
                "device_choose_goal_us": device_choose_goal_us,
                "algorithm_epoch_reset_us": algorithm_epoch_reset_us,
                "serial_roundtrip_us": int(serial_us),
                "host_serialization_setup_us": 0,
                "host_serialization_setup_measured": (
                    host_serialization_setup_measured
                ),
                "device_allocator_timer_scope": device_allocator_timer_scope,
                "serial_roundtrip_definition": serial_roundtrip_definition,
                "psetup_transaction_us": int(psetup_transaction_us),
                "device_pre_call_setup_us": device_pre_call_setup_us,
                "ptime_result_transaction_us": int(
                    ptime_result_transaction_us
                ),
                "host_prepare_cpu_us": int(host_prepare_cpu_us),
                "host_total_call_us": int(total_us),
                "parity_level": parity_metadata["parity_level"],
                "representation_hashes_differ": parity_metadata[
                    "representation_hashes_differ"
                ],
                "agx_message_sha256": call.authoritative.message_sha256,
                "device_message_sha256": signature.message_sha256,
                "agx_post_state_sha256": (
                    call.authoritative.post_state_sha256
                ),
                "device_post_state_sha256": signature.post_state_sha256,
            }
            for projection_name in (
                "agx_message_projection_sha256",
                "device_message_projection_sha256",
                "agx_state_projection_sha256",
                "device_state_projection_sha256",
            ):
                if projection_name in parity_metadata:
                    attestation[projection_name] = parity_metadata[
                        projection_name
                    ]
            attestation_sha256 = semantic_hash(attestation)
        measurement_metadata = {
            **copy.deepcopy(dict(call.metadata)),
            **parity_metadata,
            "hardware_valid": native_hardware,
            "validation_mode": (
                "rp2040_native_hardware"
                if native_hardware else "nonphysical_causal_session"
            ),
            "timing_decomposition_schema": 2,
            "host_serialization_setup_measured": (
                host_serialization_setup_measured
            ),
            "device_allocator_timer_scope": device_allocator_timer_scope,
            "serial_roundtrip_definition": serial_roundtrip_definition,
            "device_choose_goal_us": device_choose_goal_us,
            "algorithm_epoch_reset_us": algorithm_epoch_reset_us,
            "psetup_transaction_us": int(psetup_transaction_us),
            "device_pre_call_setup_us": device_pre_call_setup_us,
            "ptime_result_transaction_us": int(
                ptime_result_transaction_us
            ),
            "host_prepare_cpu_us": int(host_prepare_cpu_us),
        }
        if attestation is not None:
            measurement_metadata.update(
                {
                    "hardware_attestation": attestation,
                    "hardware_attestation_sha256": attestation_sha256,
                }
            )
        return MeasuredCall(
            call_id=call.call_id,
            group_id=call.group_id,
            trial_id=call.trial_id,
            logical_robot_id=call.logical_robot_id,
            algorithm=call.algorithm,
            board_id=self.board_id,
            serial_device=self.serial_device,
            context_id=call.logical_robot_id,
            attempt_id=attempt_id,
            virtual_start_s=float(call.virtual_start_s),
            virtual_completion_s=completion_s,
            agx_allocator_time_us=int(call.agx_allocator_time_us),
            device_allocator_time_us=device_duration_us,
            serial_roundtrip_us=int(serial_us),
            host_serialization_setup_us=int(setup_us),
            host_total_call_us=int(total_us),
            parity_passed=True,
            authoritative=call.authoritative,
            device=signature,
            device_goal=goal,
            device_messages=messages,
            device_post_state=post_state,
            candidate_count_before=before,
            candidate_count_after=after,
            heap_free_before=(
                None if result.get("heap_free_before") is None else int(result["heap_free_before"])
            ),
            heap_free_after=(
                None if result.get("heap_free_after") is None else int(result["heap_free_after"])
            ),
            physical_measurement_index=physical_index,
            psetup_transaction_us=int(psetup_transaction_us),
            device_pre_call_setup_us=device_pre_call_setup_us,
            ptime_result_transaction_us=int(
                ptime_result_transaction_us
            ),
            host_prepare_cpu_us=int(host_prepare_cpu_us),
            device_choose_goal_us=device_choose_goal_us,
            algorithm_epoch_reset_us=algorithm_epoch_reset_us,
            metadata=measurement_metadata,
        )

    def measure_group(self, calls: Sequence[Any]) -> tuple[MeasuredCall, ...]:
        """Measure a frozen group transactionally and return only parity-valid data."""

        self._assert_usable()
        if self.mission is None:
            raise SessionStateError("begin_mission() is required before measurement")
        detached = tuple(coerce_frozen_call(item).detached_copy() for item in calls)
        validate_same_time_group(detached)
        first = detached[0]
        if first.trial_id != self.mission.trial_id:
            raise SessionStateError("same-time group belongs to another mission")
        if first.group_id in self._seen_groups:
            raise SessionStateError(f"duplicate group/event ID: {first.group_id}")
        for call in detached:
            if call.call_id in self._seen_calls:
                raise SessionStateError(f"duplicate allocator call ID: {call.call_id}")
            if call.logical_robot_id not in self.context_state:
                raise SessionStateError(
                    f"unknown logical context: {call.logical_robot_id}"
                )
            if call.algorithm != self.mission.algorithm:
                raise SessionStateError("call algorithm differs from mission binding")
        # Consume IDs before physical I/O. A failed attempt invalidates the
        # mission and cannot be silently retried under the same scientific ID.
        self._seen_groups.add(first.group_id)
        self._seen_calls.update(call.call_id for call in detached)
        prepared: list[tuple[FrozenCall, dict[str, Any], str]] = []
        for call in detached:
            attempt_id = self._attempt_id(call.call_id)
            prepared.append((call, self._device_setup(call, attempt_id), attempt_id))
        pending: list[MeasuredCall] = []
        try:
            for call, setup, attempt_id in prepared:
                self._measurement_sequence += 1
                pending.append(
                    self._measure_one(
                        call,
                        setup,
                        attempt_id,
                        self._measurement_sequence,
                    )
                )
        except Exception as exc:
            details: dict[str, Any] = {
                "group_id": first.group_id,
                "call_ids": [item.call_id for item in detached],
                "accepted_duration_count": 0,
            }
            if isinstance(exc, ParityFailure):
                details["parity"] = exc.diagnostics
            self._invalidate(exc, details)
            if isinstance(exc, CausalTimingError):
                raise
            raise DeviceCallError(f"hardware measurement group failed: {exc}") from exc
        # Publish state only after every same-time call has completed and
        # passed parity. This prevents physical call order from leaking state.
        for measured in pending:
            self.context_state[measured.context_id] = copy.deepcopy(
                dict(measured.device_post_state)
            )
            self.context_call_count[measured.context_id] += 1
        return tuple(pending)

    def end_mission(self) -> None:
        if self.mission is None:
            return
        failure: BaseException | None = None
        try:
            self.device.end_persistent_trial()
        except BaseException as exc:  # preserve cleanup failure as technical failure
            failure = exc
            self._invalidate(exc, {"stage": "end_mission"})
        finally:
            self.mission = None
            self.context_state.clear()
            self.context_call_count.clear()
            self._seen_calls.clear()
            self._seen_groups.clear()
        if failure is not None:
            raise DeviceCallError(f"device mission cleanup failed: {failure}") from failure

    def validate_reconnection(self, replacement_device: Any) -> BoardFingerprint:
        """Rebind only between missions and only to the exact sealed build/board."""

        if self.mission is not None:
            raise SessionStateError("a disconnected mission is invalid and cannot reconnect")
        current = BoardFingerprint.inspect(
            replacement_device,
            expected_build_id=self.fingerprint.build_id,
            expected_module_set_sha256=self.fingerprint.module_set_sha256,
        )
        if current.reconnect_key() != self.fingerprint.reconnect_key():
            raise BoardBindingError("reconnected endpoint is not the bound board/build")
        restart = getattr(replacement_device, "restart_clean_worker", None)
        if callable(restart):
            restart()
        revalidated = BoardFingerprint.inspect(
            replacement_device,
            expected_build_id=self.fingerprint.build_id,
            expected_module_set_sha256=self.fingerprint.module_set_sha256,
        )
        if revalidated.reconnect_key() != self.fingerprint.reconnect_key():
            raise BoardBindingError("board identity changed during reconnect validation")
        self.device = replacement_device
        self.binding = StableBoardBinding(
            self.binding.worker_index, revalidated, replacement_device
        )
        self.fingerprint = revalidated
        self.invalid_reason = None
        self.last_failure = None
        self._session_nonce = uuid.uuid4().hex[:12]
        return revalidated

    def open(self) -> "CausalBoardSession":
        """Compatibility hook; construction already acquires the board lease."""

        self._assert_usable()
        return self

    def revalidate_after_failure(self, replacement_device: Any) -> BoardFingerprint:
        """Campaign-runner alias for strict between-mission reconnection."""

        return self.validate_reconnection(replacement_device)

    def close(self) -> None:
        if self.closed:
            return
        try:
            if self.mission is not None:
                try:
                    self.end_mission()
                except CausalTimingError:
                    pass
        finally:
            self.lease.release()
            self.closed = True

    def __enter__(self) -> "CausalBoardSession":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
