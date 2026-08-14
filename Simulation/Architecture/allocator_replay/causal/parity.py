"""Fail-closed behavioral parity for rich AGX and compact RP2040 state.

Raw serialized hashes are preferred.  Projection is used only because the two
implementations deliberately store the same logical state in different data
structures.  The projection includes every retained field that can alter a
future allocator choice, mechanism classification, or outbound message:
environment/team inputs, immutable/active tasks, active probabilities, ordered
path, consensus values and protocol timestamps, collision edge memory,
protocol clocks, epoch metadata, pending/last-sent communication state, and
HIPC prediction/drop state.  Missing non-default behavior is a mismatch.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from allocator_replay.capture.codec import decode_value

from .types import semantic_hash


NATIVE_RESUME_KEY = "native_collaborative_resume"
_TIMED_PROTOCOLS = {"ACBBA", "PI", "HIPC"}


def _cell(value: Any, grid_size: int) -> tuple[int, int]:
    value = decode_value(value)
    if isinstance(value, int):
        return int(value) % grid_size, int(value) // grid_size
    if isinstance(value, Mapping):
        return int(value["x"]), int(value["y"])
    return int(value[0]), int(value[1])


def _cells(values: Any, grid_size: int) -> list[list[int]]:
    values = decode_value(values) or []
    return [list(_cell(item, grid_size)) for item in values]


def _cell_records(values: Any, grid_size: int) -> list[list[int]]:
    result = _cells(values, grid_size)
    result.sort(key=tuple)
    return result


def _number(value: Any) -> int | float:
    value = float(value)
    return int(value) if value.is_integer() else value


def _message_cells(payload: Mapping[str, Any], name: str) -> list[list[int]]:
    values = payload.get(name, ()) or ()
    result = []
    for item in values:
        if isinstance(item, Mapping):
            result.append([int(item["x"]), int(item["y"])])
        else:
            result.append([int(item[0]), int(item[1])])
    return result


def project_messages(messages: Sequence[Any], algorithm: str) -> list[dict[str, Any]]:
    """Normalize the complete ordered outbound protocol sequence.

    Enrichment aliases (``owner``/``winner``, ``value``/``bid``) normalize to
    one representation.  Unlike the earlier projection, messages are not
    collapsed per cell: order, timestamps, releases, and bundle/path metadata
    can change a peer's later acceptance decision and therefore remain sealed.
    """

    algorithm = str(algorithm).upper()
    decoded = decode_value(list(messages))
    if not isinstance(decoded, list):
        raise ValueError("allocator messages must be a list")
    result: list[dict[str, Any]] = []
    for raw in decoded:
        if not isinstance(raw, Mapping):
            raise ValueError("allocator message is not a mapping")
        payload = raw.get("payload") if isinstance(raw.get("payload"), Mapping) else raw
        kind = str(payload.get("type", ""))
        sender = str(payload.get("sender", ""))
        if not kind or not sender:
            raise ValueError("allocator message lacks type/sender")
        clear = kind.endswith("clear_path") or kind.endswith("clear_bundle")
        item: dict[str, Any] = {
            "type": kind,
            "sender": sender,
            "clear": clear,
        }
        if clear:
            cell_field = "path_cells" if "path" in kind else "bundle_cells"
            item["cells"] = _message_cells(payload, cell_field)
            if algorithm in _TIMED_PROTOCOLS:
                if "timestamp" not in payload:
                    raise ValueError("timed clear message lacks timestamp")
                item["timestamp"] = _number(payload["timestamp"])
            result.append(item)
            continue
        if "x" not in payload or "y" not in payload:
            raise ValueError(f"unsupported allocator message shape: {kind}")
        item["cell"] = [int(payload["x"]), int(payload["y"])]
        owner = payload.get("owner", payload.get("winner"))
        value = payload.get(
            "significance", payload.get("bid", payload.get("value"))
        )
        if value is None:
            raise ValueError(f"allocator claim message has no value: {kind}")
        item["owner"] = None if owner is None else str(owner)
        item["value"] = float(value)
        item["released"] = bool(payload.get("released", owner is None))
        if item["released"]:
            released_owner = payload.get("released_winner", payload.get("released_owner"))
            released_value = payload.get("released_bid", payload.get("released_value"))
            item["released_owner"] = (
                None if released_owner is None else str(released_owner)
            )
            item["released_value"] = (
                None if released_value is None else float(released_value)
            )
        if algorithm in _TIMED_PROTOCOLS:
            if "timestamp" not in payload:
                raise ValueError("timed claim message lacks timestamp")
            item["timestamp"] = _number(payload["timestamp"])
        if algorithm == "PI":
            if "path_cells" in payload:
                item["path_cells"] = _message_cells(payload, "path_cells")
            if "order" in payload:
                item["order"] = int(payload["order"])
            if "path_size" in payload:
                item["path_size"] = int(payload["path_size"])
        elif algorithm in {"ACBBA", "HIPC"}:
            if "bundle_cells" in payload:
                item["bundle_cells"] = _message_cells(payload, "bundle_cells")
            if "order" in payload:
                item["order"] = int(payload["order"])
            if "bundle_size" in payload:
                item["bundle_size"] = int(payload["bundle_size"])
        result.append(item)
    return result


def _claims(
    *,
    owners: Mapping[Any, Any],
    values: Mapping[Any, Any],
    times: Mapping[Any, Any],
    grid_size: int,
    include_time: bool,
) -> list[dict[str, Any]]:
    result = []
    for raw_cell, owner in owners.items():
        if owner is None:
            continue
        if raw_cell not in values:
            raise ValueError("consensus owner has no corresponding value")
        item: dict[str, Any] = {
            "cell": list(_cell(raw_cell, grid_size)),
            "owner": str(owner),
            "value": float(values[raw_cell]),
        }
        if include_time:
            if raw_cell not in times:
                raise ValueError("timed consensus claim has no timestamp")
            item["timestamp"] = _number(times[raw_cell])
        result.append(item)
    result.sort(key=lambda item: tuple(item["cell"]))
    return result


def _probabilities(
    raw: Any, universe: Sequence[Sequence[int]], active: Sequence[Sequence[int]], grid_size: int
) -> list[dict[str, Any]]:
    decoded = decode_value(raw)
    active_set = {tuple(item) for item in active}
    if isinstance(decoded, Mapping):
        result = [
            {"cell": list(_cell(cell, grid_size)), "value": float(value)}
            for cell, value in decoded.items()
            if _cell(cell, grid_size) in active_set
        ]
    elif isinstance(decoded, (list, tuple)) and len(decoded) == len(universe):
        result = [
            {"cell": list(cell), "value": float(value)}
            for cell, value in zip(universe, decoded)
            if tuple(cell) in active_set
        ]
    elif decoded in (None, [], ()):
        result = [{"cell": list(cell), "value": 1.0} for cell in active]
    else:
        raise ValueError("probability state cannot be aligned to task universe")
    result.sort(key=lambda item: tuple(item["cell"]))
    return result


def _peer_positions(raw: Any, robot_id: str, grid_size: int) -> list[dict[str, Any]]:
    decoded = decode_value(raw) or {}
    if isinstance(decoded, Mapping):
        pairs = decoded.items()
    else:
        pairs = decoded
    result = []
    for rid, position in pairs:
        if str(rid) == robot_id:
            continue
        result.append({"robot_id": str(rid), "position": list(_cell(position, grid_size))})
    result.sort(key=lambda item: item["robot_id"])
    return result


def _ineligible_tasks(
    views: Mapping[str, Any], universe: Sequence[Sequence[int]], grid_size: int
) -> list[list[int]]:
    universe_set = {tuple(cell) for cell in universe}
    result: set[tuple[int, int]] = set()
    for name in (
        "searched",
        "local_searched",
        "known_obstacles",
        "obstacles",
        "blocked",
        "blocked_cells",
    ):
        for cell in _cell_records(views.get(name, ()), grid_size):
            pair = tuple(cell)
            if pair in universe_set:
                result.add(pair)
    return [list(cell) for cell in sorted(result)]


def _mechanism_category(raw: Any, call_class: str | None) -> str:
    text = str(raw or "")
    if "collision" in text:
        return "collision_repair"
    if call_class:
        return str(call_class)
    if "epoch" in text or "admission" in text:
        return "allocation_epoch"
    if "repair" in text or "release" in text:
        return "consensus_repair"
    return "normal"


def _prediction_first_tasks(raw: Any, grid_size: int) -> dict[str, list[int]]:
    decoded = decode_value(raw) or {}
    if not isinstance(decoded, Mapping):
        raise ValueError("HIPC peer prediction state must be a mapping")
    return {
        str(peer): list(_cell(cell, grid_size))
        for peer, cell in decoded.items()
    }


def _normalized_nested(raw: Any) -> Any:
    """Remove list/tuple transport differences without discarding content."""

    decoded = decode_value(raw)
    if isinstance(decoded, Mapping):
        return {
            str(key): _normalized_nested(value)
            for key, value in decoded.items()
        }
    if isinstance(decoded, (list, tuple)):
        return [_normalized_nested(value) for value in decoded]
    if isinstance(decoded, set):
        values = [_normalized_nested(value) for value in decoded]
        return sorted(values, key=lambda value: repr(value))
    return decoded


def _desktop_last_sent(robot: Mapping[str, Any], algorithm: str, grid_size: int) -> Any:
    prefix = algorithm.lower()
    if algorithm in {"CBAA", "ACBBA"}:
        raw = decode_value(robot.get(prefix + "_last_sent_signatures", {})) or {}
        result = []
        for raw_cell, signature in raw.items():
            values = list(signature)
            if len(values) < 3:
                raise ValueError("last-sent consensus signature is incomplete")
            item = {
                "cell": list(_cell(raw_cell, grid_size)),
                "owner": None if values[1] is None else str(values[1]),
                "value": float(values[2]),
            }
            if algorithm == "ACBBA":
                if len(values) < 4:
                    raise ValueError("ACBBA last-sent signature lacks timestamp")
                item["timestamp"] = _number(values[3])
            result.append(item)
        result.sort(key=lambda item: tuple(item["cell"]))
        return result
    raw = decode_value(robot.get(prefix + "_last_sent_signature", ())) or ()
    result = []
    for entry in raw:
        if len(entry) < 3:
            raise ValueError("path last-sent signature is incomplete")
        result.append(
            {
                "cell": list(_cell(entry[0], grid_size)),
                "value": float(entry[1]),
                "timestamp": _number(entry[2]),
            }
        )
    return result


def _desktop_pending(robot: Mapping[str, Any], algorithm: str) -> dict[str, Any]:
    prefix = algorithm.lower()
    messages = []
    if algorithm in {"CBAA", "ACBBA"}:
        pending = decode_value(robot.get(prefix + "_pending_deltas", {})) or {}
        messages = [payload for _, payload in pending.items()]
    return {
        "messages": project_messages(messages, algorithm),
        "snapshot_pending": bool(robot.get(prefix + "_pending_snapshot", False)),
    }


def _desktop_state(
    post_state: Mapping[str, Any], algorithm: str, call_class: str | None
) -> dict[str, Any]:
    decoded = decode_value(dict(post_state))
    robot = decoded.get("robot_attrs", {})
    views = decoded.get("views", {})
    cfg = decoded.get("cfg", {})
    grid_size = int(robot.get("grid_size", cfg.get("grid_size", 19)))
    algorithm = str(algorithm).upper()
    names = {
        "CBAA": (
            "cbaa_current_task", "cbaa_winner_by_cell", "cbaa_winning_bid_by_cell", None,
        ),
        "ACBBA": (
            "acbba_path", "acbba_winner_by_cell", "acbba_winning_bid_by_cell", "acbba_bid_time_by_cell",
        ),
        "PI": (
            "pi_path", "pi_owner_by_cell", "pi_significance_by_cell", "pi_time_by_cell",
        ),
        "HIPC": (
            "hipc_path", "hipc_winner_by_cell", "hipc_winning_bid_by_cell", "hipc_bid_time_by_cell",
        ),
    }
    if algorithm not in names:
        raise ValueError(f"no causal state projection for {algorithm}")
    path_name, owner_name, value_name, time_name = names[algorithm]
    raw_path = robot.get(path_name, ())
    if algorithm == "CBAA" and raw_path is not None:
        raw_path = [raw_path]
    owners = decode_value(robot.get(owner_name, {})) or {}
    values = decode_value(robot.get(value_name, {})) or {}
    times = decode_value(robot.get(time_name, {})) or {} if time_name else {}
    active = _cell_records(views.get("active_tasks", ()), grid_size)
    universe = _cell_records(cfg.get("all_tasks", active), grid_size)
    robot_id = str(robot.get("rid", robot.get("robot_id", "")))
    prefix = algorithm.lower()
    protocol_counter = None
    if algorithm == "ACBBA":
        protocol_counter = int(robot.get("acbba_bid_counter", 0))
    elif algorithm == "PI":
        protocol_counter = int(robot.get("pi_time_counter", 0))
    elif algorithm == "HIPC":
        protocol_counter = int(robot.get("hipc_bid_counter", 0))
    epoch_admitted = robot.get("last_allocation_epoch_admitted", ())
    result = {
        "schema": 2,
        "algorithm": algorithm,
        "robot_id": robot_id,
        "robot_ids": [str(item) for item in decode_value(cfg.get("robot_ids", ()))],
        "grid_size": grid_size,
        "position": list(_cell(robot.get("pos", (0, 0)), grid_size)),
        "peer_positions": _peer_positions(views.get("peer_positions", {}), robot_id, grid_size),
        "task_universe": universe,
        "active_tasks": active,
        "ineligible_tasks": _ineligible_tasks(views, universe, grid_size),
        "active_probabilities": _probabilities(views.get("target_p"), universe, active, grid_size),
        "path": _cells(raw_path or (), grid_size),
        "claims": _claims(
            owners=owners, values=values, times=times, grid_size=grid_size,
            include_time=algorithm in _TIMED_PROTOCOLS,
        ),
        "collision_active": bool(robot.get("collision_avoidance_active", robot.get("collision_active", False))),
        "collision_memory": (
            None if algorithm == "CBAA" else bool(robot.get(prefix + "_last_collision_active", False))
        ),
        "protocol_counter": protocol_counter,
        "epoch": {
            "index": int(robot.get("last_allocation_epoch_index", -1)),
            "reason": str(robot.get("last_allocation_epoch_reason", "")),
            "admitted": _cell_records(epoch_admitted, grid_size),
        },
        "mechanism_category": _mechanism_category(
            robot.get(prefix + "_last_reallocation_trigger")
            or robot.get("last_event"),
            call_class,
        ),
        "communication": {
            "pending": _desktop_pending(robot, algorithm),
            "last_sent": _desktop_last_sent(robot, algorithm, grid_size),
        },
    }
    if algorithm == "HIPC":
        result["hipc_prediction_state"] = {
            "bad_prediction_count": decode_value(robot.get("hipc_bad_prediction_count", {})) or {},
            "dropped_peers": sorted(str(item) for item in (decode_value(robot.get("hipc_dropped_peers", ())) or ())),
            "last_predicted_peer_first_task": _prediction_first_tasks(
                robot.get("hipc_last_predicted_peer_first_task", {}), grid_size
            ),
            "seen_peer_bundle_signature": _normalized_nested(
                robot.get("hipc_seen_peer_bundle_signature", {})
            ) or {},
        }
    return result


def _native_state(
    post_state: Mapping[str, Any], algorithm: str, call_class: str | None
) -> dict[str, Any]:
    decoded = decode_value(dict(post_state))
    allocator_attrs = decoded.get("allocator_attrs", {})
    resume = allocator_attrs.get(NATIVE_RESUME_KEY)
    if not isinstance(resume, Mapping):
        raise ValueError("native post-state has no collaborative resume record")
    state = resume.get("state", {})
    allocator = resume.get("allocator", {})
    behavior = resume.get("behavior", {})
    grid_size = int(state.get("grid_size", 19))
    algorithm = str(algorithm).upper()
    registered_targets = _cell_records(
        state.get("targets", state.get("active", ())), grid_size
    )
    active = _cell_records(state.get("active", ()), grid_size)
    # Native slots are append-only so completed learned tasks can be resumed
    # without reindexing claims.  The desktop causal projection defines its
    # task universe as the currently active allocator-visible pool, so compare
    # that behavioral universe rather than the native storage registry.
    targets = active
    active_set = {tuple(cell) for cell in active}
    owners: dict[Any, Any] = {}
    values: dict[Any, Any] = {}
    times: dict[Any, Any] = {}
    for entry in state.get("claims", ()):
        if len(entry) < 4:
            raise ValueError("native claim record lacks owner/value/timestamp")
        owners[entry[0]] = entry[1]
        values[entry[0]] = entry[2]
        times[entry[0]] = entry[3]
    robot_id = str(state.get("robot_id", ""))
    protocol_counter = None
    if algorithm == "ACBBA":
        protocol_counter = int(allocator.get("bid_counter", state.get("event_counter", 0)))
    elif algorithm in {"PI", "HIPC"}:
        protocol_counter = int(behavior.get("protocol_counter", state.get("event_counter", 0)))
    result = {
        "schema": 2,
        "algorithm": algorithm,
        "robot_id": robot_id,
        "robot_ids": [str(item) for item in state.get("robot_ids", ())],
        "grid_size": grid_size,
        "position": list(_cell(state.get("position", 0), grid_size)),
        "peer_positions": _peer_positions(state.get("peer_positions", ()), robot_id, grid_size),
        "task_universe": targets,
        "active_tasks": active,
        "ineligible_tasks": _cell_records(
            [
                cell
                for cell in state.get("unavailable", ())
                if tuple(_cell(cell, grid_size)) in active_set
            ],
            grid_size,
        ),
        "active_probabilities": _probabilities(
            state.get("probability"), registered_targets, active, grid_size
        ),
        "path": _cells(allocator.get("path", ()), grid_size),
        "claims": _claims(
            owners=owners, values=values, times=times, grid_size=grid_size,
            include_time=algorithm in _TIMED_PROTOCOLS,
        ),
        "collision_active": bool(state.get("collision_active", False)),
        "collision_memory": (
            None if algorithm == "CBAA" else bool(allocator.get("last_collision_active", False))
        ),
        "protocol_counter": protocol_counter,
        "epoch": {
            "index": int(state.get("last_allocation_epoch_index", -1)),
            "reason": str(state.get("last_allocation_epoch_reason", "")),
            "admitted": _cell_records(state.get("last_allocation_epoch_admitted", ()), grid_size),
        },
        "mechanism_category": _mechanism_category(
            behavior.get("call_mechanism", allocator.get("last_call_path")),
            call_class,
        ),
        "communication": {
            "pending": {
                "messages": project_messages(behavior.get("pending_messages", ()), algorithm),
                "snapshot_pending": bool(behavior.get("pending_snapshot", False)),
            },
            "last_sent": decode_value(behavior.get("last_sent", ())) or [],
        },
    }
    if algorithm == "HIPC":
        result["hipc_prediction_state"] = {
            "bad_prediction_count": decode_value(behavior.get("bad_prediction_count", {})) or {},
            "dropped_peers": sorted(str(item) for item in (decode_value(behavior.get("dropped_peers", ())) or ())),
            "last_predicted_peer_first_task": _prediction_first_tasks(
                behavior.get("last_predicted_peer_first_task", {}), grid_size
            ),
            "seen_peer_bundle_signature": _normalized_nested(
                behavior.get("seen_peer_bundle_signature", {})
            ) or {},
        }
    return result


def project_state(
    post_state: Mapping[str, Any], algorithm: str, call_class: str | None = None
) -> dict[str, Any]:
    decoded = decode_value(dict(post_state))
    allocator_attrs = decoded.get("allocator_attrs", {})
    if isinstance(allocator_attrs, Mapping) and NATIVE_RESUME_KEY in allocator_attrs:
        return _native_state(decoded, algorithm, call_class)
    return _desktop_state(decoded, algorithm, call_class)


def projected_parity(
    *,
    algorithm: str,
    authoritative_messages: Sequence[Any],
    device_messages: Sequence[Any],
    authoritative_post_state: Mapping[str, Any],
    device_post_state: Mapping[str, Any],
    authoritative_call_class: str | None = None,
    device_call_class: str | None = None,
) -> dict[str, Any]:
    agx_messages = project_messages(authoritative_messages, algorithm)
    device_message_projection = project_messages(device_messages, algorithm)
    agx_state = project_state(
        authoritative_post_state, algorithm, authoritative_call_class
    )
    device_state_projection = project_state(
        device_post_state, algorithm, device_call_class
    )
    return {
        "projection_schema": 2,
        "message_match": agx_messages == device_message_projection,
        "state_match": agx_state == device_state_projection,
        "agx_message_projection_sha256": semantic_hash(agx_messages),
        "device_message_projection_sha256": semantic_hash(device_message_projection),
        "agx_state_projection_sha256": semantic_hash(agx_state),
        "device_state_projection_sha256": semantic_hash(device_state_projection),
        "agx_message_projection": agx_messages,
        "device_message_projection": device_message_projection,
        "agx_state_projection": agx_state,
        "device_state_projection": device_state_projection,
    }
