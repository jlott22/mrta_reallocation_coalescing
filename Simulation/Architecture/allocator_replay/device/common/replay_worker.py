"""Motor-free serial worker for allocator-call replay.

This module deliberately imports no Pololu motor, sensor, or robot program.
Start it from the MicroPython REPL with:

    import replay_worker; replay_worker.main()
"""

import gc
import sys

try:
    import json
except ImportError:  # pragma: no cover - MicroPython
    import ujson as json

try:
    import binascii
except ImportError:  # pragma: no cover - MicroPython
    import ubinascii as binascii

try:
    import machine
except ImportError:  # pragma: no cover - desktop emulator
    machine = None

from replay_fingerprint import logical_sha256
from replay_codec import (
    clear_object_classes,
    decode_value,
    encode_value,
    iter_json_chunks,
    register_object_class,
)
from replay_robot import (
    ReplayRobot,
    outbound_messages,
    restore_allocator,
    snapshot_authoritative,
    snapshot_mutable,
)
from replay_persistent import PersistentRuntimeSlot, ReplayPersistentRuntime
from replay_runtime import ticks_diff, ticks_us


PROTOCOL = "AR1"
ALGORITHM_CLASSES = {
    "CBAA": "CBAAAllocator",
    "ACBBA": "ACBBAAllocator",
    "PI": "PIAllocator",
    "HIPC": "HIPCAllocator",
    "DMCHBA": "DMCHBAAllocator",
    "DGA": "DGAAllocator",
}
_ACTIVE_ALLOCATOR_MODULE = None


class TypedArrayView:
    def __init__(self, values, typecode):
        self.values = values
        self.typecode = typecode

    def __len__(self):
        return len(self.values)

    def __iter__(self):
        return iter(self.values)


def _typed_post_arrays(post, expected):
    typecodes = expected.get("post_array_typecodes", {})
    for section in ("robot_attrs", "allocator_attrs"):
        section_types = typecodes.get(section, {})
        values = post[section]
        for name, typecode in section_types.items():
            value = values.get(name)
            if (
                value is not None
                and value.__class__.__name__ == "array"
            ):
                values[name] = TypedArrayView(value, typecode)
    return post


def _write(*fields):
    sys.stdout.write("|".join(str(field) for field in fields) + "\n")
    try:
        sys.stdout.flush()
    except AttributeError:
        pass


def _crc32(payload):
    return int(binascii.crc32(payload)) & 0xFFFFFFFF


def _crc32_update(payload, previous):
    """Incrementally update CRC32 on CPython and MicroPython."""

    try:
        return int(binascii.crc32(payload, previous)) & 0xFFFFFFFF
    except TypeError:  # pragma: no cover - older MicroPython fallback
        value = int(previous) ^ 0xFFFFFFFF
        for item in payload:
            value ^= int(item)
            for _ in range(8):
                value = (
                    (value >> 1) ^ 0xEDB88320
                    if value & 1
                    else value >> 1
                )
        return int(value ^ 0xFFFFFFFF) & 0xFFFFFFFF


def _mem_free():
    function = getattr(gc, "mem_free", None)
    return int(function()) if callable(function) else -1


def _device_uid():
    if machine is None:
        return "desktop-emulator"
    raw = machine.unique_id()
    return binascii.hexlify(raw).decode("ascii")


def _frequency():
    if machine is None:
        return 0
    function = getattr(machine, "freq", None)
    return int(function()) if callable(function) else 0


def _build_id():
    try:
        import replay_build

        return replay_build.BUILD_ID
    except Exception:
        return "unknown"


def _implementation():
    implementation = getattr(sys, "implementation", None)
    if implementation is None:
        return "unknown"
    name = getattr(implementation, "name", "unknown")
    version = getattr(implementation, "version", ())
    return "{}-{}".format(name, ".".join(str(item) for item in version[:3]))


def _firmware_sha256():
    try:
        import hashlib
    except ImportError:  # pragma: no cover - MicroPython
        import uhashlib as hashlib
    try:
        import os

        platform_details = "|".join(str(item) for item in os.uname())
    except Exception:
        platform_details = getattr(sys, "platform", "unknown")
    implementation = getattr(sys, "implementation", None)
    payload = "{}|{}|{}|{}".format(
        getattr(implementation, "name", "unknown"),
        getattr(implementation, "version", ()),
        getattr(implementation, "_mpy", 0),
        platform_details,
    ).encode("utf-8")
    digest = hashlib.sha256(payload)
    try:
        return digest.hexdigest()
    except AttributeError:  # pragma: no cover - MicroPython
        return binascii.hexlify(digest.digest()).decode("ascii")


def _module_set_sha256(module_files):
    try:
        import hashlib
    except ImportError:  # pragma: no cover - MicroPython
        import uhashlib as hashlib
    digest = hashlib.sha256()
    for filename in sorted(module_files):
        digest.update(filename.encode("utf-8"))
        digest.update(b"\0")
        try:
            handle = open(filename, "rb")
        except OSError:
            return "missing:" + filename
        try:
            while True:
                chunk = handle.read(4096)
                if not chunk:
                    break
                digest.update(chunk)
        finally:
            handle.close()
    try:
        return digest.hexdigest()
    except AttributeError:  # pragma: no cover - MicroPython
        return binascii.hexlify(digest.digest()).decode("ascii")


def _allocator_class(fixture):
    global _ACTIVE_ALLOCATOR_MODULE
    prefix = "b" if fixture["mission"] == "bayesian" else "c"
    module_name = "replay_{}_{}".format(
        prefix,
        fixture["algorithm"].lower(),
    )
    if _ACTIVE_ALLOCATOR_MODULE != module_name:
        keep = {module_name}
        if module_name == "replay_b_dga":
            keep.add("replay_b_dga_optimized")
        for loaded_name in list(sys.modules):
            if (
                loaded_name.startswith("replay_b_")
                or loaded_name.startswith("replay_c_")
            ) and loaded_name not in keep:
                try:
                    del sys.modules[loaded_name]
                except KeyError:
                    pass
        clear_object_classes()
        gc.collect()
        _ACTIVE_ALLOCATOR_MODULE = module_name
    module = __import__(module_name)
    for name in ("_PackedPlan", "_PackedScore"):
        cls = getattr(module, name, None)
        if cls is not None:
            register_object_class(
                cls,
                name,
                (
                    ("cells", "lengths", "team_ids", "grid_size")
                    if name == "_PackedPlan"
                    else ("plan", "fitness", "ordinal")
                ),
            )
    if fixture["mission"] == "bayesian" and fixture["algorithm"] == "DGA":
        optimized = __import__("replay_b_dga_optimized")
        for name in ("_PackedPlan", "_PackedScore"):
            cls = getattr(optimized, name, None)
            if cls is not None:
                register_object_class(
                    cls,
                    name,
                    (
                        ("cells", "lengths", "team_ids", "grid_size")
                        if name == "_PackedPlan"
                        else ("plan", "fitness", "ordinal")
                    ),
                )
    return getattr(
        module,
        ALGORITHM_CLASSES[fixture["algorithm"]],
    )


def _load_allocator(fixture):
    return _allocator_class(fixture)()


def _persistent_runtime(config):
    """Use the exact factory shared with a future physical control wrapper."""
    if str(config.get("mission", "")).lower() == "bayesian":
        # Preserve the memory-optimized DGA alias and codec registrations.
        _allocator_class(config)
    module = __import__("replay_physical_factory")
    return module.create_complete_runtime(config)


def _run_fixture(fixture, attempt_id, timed_callback=None):
    result = {
        "attempt_id": attempt_id,
        "fixture_id": fixture["fixture_id"],
        "condition_id": fixture["condition_id"],
        "device_id": _device_uid(),
        "status": "failed",
        "failure_type": "",
    }
    allocator = None
    robot = None
    allocator_finished = False
    stage = "fixture_header"
    try:
        pre_state = fixture.pop("pre_state")
        expected = fixture["expected"]
        expected_mutable_hash = expected.get(
            "post_mutable_state_sha256"
        )
        if expected_mutable_hash is None:
            full_post = expected["post_state"]
            expected_mutable_hash = logical_sha256(
                {
                    "robot_attrs": full_post["robot_attrs"],
                    "allocator_attrs": full_post["allocator_attrs"],
                }
            )
        stage = "allocator_construct"
        allocator = _load_allocator(fixture)
        stage = "robot_construct"
        robot = ReplayRobot(pre_state)
        stage = "allocator_restore"
        restore_allocator(allocator, pre_state)
        del pre_state
        gc.collect()
        result["heap_free_before"] = _mem_free()
        filter_start = len(robot.counters.candidate_filter_time_us_samples)
        stage = "allocator_choose_goal"
        started = ticks_us()
        decision = allocator.choose_goal(robot)
        elapsed = max(0, ticks_diff(ticks_us(), started))
        filter_samples = robot.counters.candidate_filter_time_us_samples[
            filter_start:
        ]
        filter_us = sum(filter_samples)
        result["allocator_time_us"] = elapsed
        result["candidate_filter_calls"] = len(filter_samples)
        result["candidate_filter_time_us"] = filter_us
        result["allocator_exclusive_time_us"] = max(0, elapsed - filter_us)
        result["heap_free_after"] = _mem_free()
        allocator_finished = True
        if timed_callback is not None:
            timed_callback(
                {
                    "attempt_id": attempt_id,
                    "fixture_id": fixture["fixture_id"],
                    "allocator_time_us": elapsed,
                    "candidate_filter_calls": len(filter_samples),
                    "candidate_filter_time_us": filter_us,
                    "allocator_exclusive_time_us": max(
                        0,
                        elapsed - filter_us,
                    ),
                    "heap_free_before": result["heap_free_before"],
                    "heap_free_after": result["heap_free_after"],
                }
            )
        stage = "post_state"
        post = _typed_post_arrays(
            snapshot_mutable(robot, allocator, expected),
            expected,
        )
        post_hash = logical_sha256(post)
        result["candidate_count_before"] = int(
            getattr(robot, "candidate_count_before_filter", 0) or 0
        )
        result["candidate_count_after"] = int(
            getattr(robot, "candidate_count_after_filter", 0) or 0
        )
        stage = "outbound_messages"
        direct_messages = list(getattr(robot, "published_messages", []) or [])
        messages = direct_messages + outbound_messages(allocator, robot)
        message_hash = logical_sha256(messages)
        expected_goal = decode_value(expected["goal"])
        expected_messages_hash = expected.get("messages_sha256")
        if expected_messages_hash is None:
            expected_messages_hash = logical_sha256(expected["messages"])
        goal_ok = decision.goal == expected_goal
        state_ok = post_hash == expected_mutable_hash
        messages_ok = message_hash == expected_messages_hash
        result["goal"] = encode_value(decision.goal)
        result["goal_match"] = goal_ok
        result["post_mutable_state_sha256"] = post_hash
        result["state_match"] = state_ok
        result["messages_sha256"] = message_hash
        result["messages_match"] = messages_ok
        if not state_ok:
            result["robot_attr_sha256"] = {
                name: logical_sha256(value)
                for name, value in post["robot_attrs"].items()
            }
            result["allocator_attr_sha256"] = {
                name: logical_sha256(value)
                for name, value in post["allocator_attrs"].items()
            }
        if goal_ok and state_ok and messages_ok:
            result["status"] = "completed"
        else:
            result["failure_type"] = "parity_failure"
    except MemoryError as exc:
        result["failure_type"] = (
            "verification_memory_error"
            if allocator_finished
            else "memory_error"
        )
        result["error"] = repr(exc)
        result["heap_free_after"] = _mem_free()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        result["failure_type"] = "device_exception"
        if stage == "robot_construct":
            try:
                import replay_robot

                stage += ":" + str(replay_robot.RESTORE_STAGE)
            except Exception:
                pass
        result["error"] = "{}: {}: {!r}".format(
            stage,
            type(exc).__name__,
            exc,
        )
        result["heap_free_after"] = _mem_free()
    return result


def _run_authoritative(fixture, attempt_id, timed_callback=None):
    """Run one hardware-authoritative call without a desktop expected result."""
    result = {
        "attempt_id": attempt_id,
        "fixture_id": fixture["fixture_id"],
        "condition_id": fixture["condition_id"],
        "device_id": _device_uid(),
        "status": "failed",
        "failure_type": "",
    }
    allocator = None
    robot = None
    stage = "fixture_header"
    try:
        pre_state = fixture.pop("pre_state")
        stage = "allocator_construct"
        allocator = _load_allocator(fixture)
        stage = "robot_construct"
        robot = ReplayRobot(pre_state)
        stage = "allocator_restore"
        restore_allocator(allocator, pre_state)
        del pre_state
        gc.collect()
        heap_before = _mem_free()
        filter_start = len(robot.counters.candidate_filter_time_us_samples)
        stage = "allocator_choose_goal"
        started = ticks_us()
        decision = allocator.choose_goal(robot)
        elapsed = max(0, ticks_diff(ticks_us(), started))
        filter_samples = robot.counters.candidate_filter_time_us_samples[
            filter_start:
        ]
        filter_us = sum(filter_samples)
        timed = {
            "attempt_id": attempt_id,
            "fixture_id": fixture["fixture_id"],
            "allocator_time_us": elapsed,
            "candidate_filter_calls": len(filter_samples),
            "candidate_filter_time_us": filter_us,
            "allocator_exclusive_time_us": max(0, elapsed - filter_us),
            "heap_free_before": heap_before,
            "heap_free_after": _mem_free(),
        }
        result.update(timed)
        if timed_callback is not None:
            timed_callback(dict(timed))
        result["candidate_count_before"] = int(
            getattr(robot, "candidate_count_before_filter", 0) or 0
        )
        result["candidate_count_after"] = int(
            getattr(robot, "candidate_count_after_filter", 0) or 0
        )
        stage = "outbound_messages"
        direct_messages = list(getattr(robot, "published_messages", []) or [])
        messages = direct_messages + outbound_messages(allocator, robot)
        stage = "post_state"
        result["goal"] = encode_value(decision.goal)
        result["messages"] = encode_value(messages)
        result["post_state"] = snapshot_authoritative(robot, allocator)
        result["status"] = "completed"
    except MemoryError as exc:
        result["failure_type"] = "memory_error"
        result["error"] = repr(exc)
        if "heap_free_after" not in result:
            result["heap_free_after"] = _mem_free()
        else:
            result["heap_free_after_postprocess_failure"] = _mem_free()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        result["failure_type"] = "device_exception"
        result["error"] = "{}: {}: {!r}".format(
            stage,
            type(exc).__name__,
            exc,
        )
        if "heap_free_after" not in result:
            result["heap_free_after"] = _mem_free()
        else:
            result["heap_free_after_postprocess_failure"] = _mem_free()
    return result


def _send_result(result):
    # MicroPython's ujson does not consistently accept CPython's ``separators``
    # keyword.  Whitespace is harmless because the JSON is base64 framed.
    payload = json.dumps(result).encode("utf-8")
    encoded = binascii.b2a_base64(payload).decode("ascii").strip()
    _write(PROTOCOL, "RESULT", encoded, _crc32(payload))


def _send_timed(result):
    payload = json.dumps(result).encode("utf-8")
    encoded = binascii.b2a_base64(payload).decode("ascii").strip()
    _write(PROTOCOL, "TIMED", encoded, _crc32(payload))


def _send_authoritative_result(result):
    """Chunk a live post-state response; transfer is outside timed execution."""
    payload = json.dumps(result).encode("utf-8")
    _write(PROTOCOL, "ABEGIN", len(payload), _crc32(payload))
    sequence = 0
    for offset in range(0, len(payload), 384):
        chunk = payload[offset:offset + 384]
        encoded = binascii.b2a_base64(chunk).decode("ascii").strip()
        _write(PROTOCOL, "ADATA", sequence, encoded, _crc32(chunk))
        sequence += 1
    _write(PROTOCOL, "AEND", sequence)


def _send_persistent_failure(
    attempt_id,
    failure_type,
    error,
    heap_free_before=None,
    heap_free_after=None,
    elapsed_until_failure_us=None,
):
    raw = str(error).encode("utf-8")
    encoded = binascii.b2a_base64(raw).decode("ascii").strip()
    if (
        heap_free_before is not None
        and heap_free_after is not None
        and elapsed_until_failure_us is not None
    ):
        _write(
            PROTOCOL,
            "PFAIL",
            attempt_id,
            failure_type,
            encoded,
            int(heap_free_before),
            int(heap_free_after),
            int(elapsed_until_failure_us),
        )
        return
    _write(
        PROTOCOL,
        "PFAIL",
        attempt_id,
        failure_type,
        encoded,
    )


def _persistent_counters(runtime):
    method = getattr(runtime, "timing_counters", None)
    if callable(method):
        return method()
    counters = getattr(runtime, "counters", None)
    if counters is not None:
        return counters
    robot = getattr(runtime, "robot", None)
    return getattr(robot, "counters", None)


def _persistent_candidate_counts(runtime):
    method = getattr(runtime, "candidate_counts", None)
    if callable(method):
        return method()
    robot = getattr(runtime, "robot", runtime)
    return (
        int(getattr(robot, "candidate_count_before_filter", 0) or 0),
        int(getattr(robot, "candidate_count_after_filter", 0) or 0),
    )


def _persistent_field_measure(value, encode_replay_value):
    length = 0
    checksum = 0
    for chunk in iter_json_chunks(
        value,
        encode_replay_value=encode_replay_value,
    ):
        length += len(chunk)
        checksum = _crc32_update(chunk, checksum)
    return length, checksum


def _send_persistent_field(
    index,
    kind,
    section,
    name,
    value,
    encode_replay_value=False,
):
    """Encode and transfer one value with bounded controller allocations.

    The first traversal computes the existing whole-field length and CRC. The
    second emits independently checked chunks. Neither traversal constructs
    the encoded replay container or complete JSON field in controller RAM.
    """

    payload_length, payload_crc = _persistent_field_measure(
        value,
        encode_replay_value,
    )
    encoded_name = binascii.b2a_base64(
        str(name).encode("utf-8")
    ).decode("ascii").strip()
    _write(
        PROTOCOL,
        "PFIELD",
        index,
        kind,
        section,
        encoded_name,
        payload_length,
        payload_crc,
    )
    sequence = 0
    for chunk in iter_json_chunks(
        value,
        encode_replay_value=encode_replay_value,
    ):
        encoded = binascii.b2a_base64(chunk).decode("ascii").strip()
        _write(
            PROTOCOL,
            "PFDATA",
            index,
            sequence,
            encoded,
            _crc32(chunk),
        )
        sequence += 1
    _write(PROTOCOL, "PFEND", index, sequence)


_PERSISTENT_RNG_WORDS_PER_CHUNK = 24
_PERSISTENT_ARRAY_ITEMS_PER_CHUNK = 48


def _is_streamed_rng(section, name, value):
    return (
        section == "robot_attrs"
        and name == "dga_rng"
        and value.__class__.__name__ == "Random"
        and hasattr(value, "_state")
        and hasattr(value, "_index")
    )


def _is_streamed_packed_plan(section, name, value):
    return (
        section == "robot_attrs"
        and name.startswith("dga_replay_")
        and value.__class__.__name__ == "_PackedPlan"
        and hasattr(value, "cells")
        and hasattr(value, "lengths")
        and hasattr(value, "team_ids")
        and hasattr(value, "grid_size")
    )


def _chunk_count(length, size):
    return (int(length) + int(size) - 1) // int(size)


def _persistent_state_field_count(
    section,
    name,
    value,
    state_values_encoded,
):
    if state_values_encoded:
        return 1
    if _is_streamed_rng(section, name, value):
        return 3 + _chunk_count(
            len(value._state),
            _PERSISTENT_RNG_WORDS_PER_CHUNK,
        )
    if _is_streamed_packed_plan(section, name, value):
        return 5 + _chunk_count(
            len(value.cells),
            _PERSISTENT_ARRAY_ITEMS_PER_CHUNK,
        )
    return 1


def _rng_chunk_bytes(state, start, stop):
    raw = bytearray((int(stop) - int(start)) * 4)
    output_index = 0
    for state_index in range(int(start), int(stop)):
        word = int(state[state_index]) & 0xFFFFFFFF
        raw[output_index] = word & 0xFF
        raw[output_index + 1] = (word >> 8) & 0xFF
        raw[output_index + 2] = (word >> 16) & 0xFF
        raw[output_index + 3] = (word >> 24) & 0xFF
        output_index += 4
    return raw


def _persistent_state_fields(
    section,
    name,
    value,
    state_values_encoded,
):
    """Yield bounded logical fields for one persistent state value."""

    if state_values_encoded:
        yield name, value, False
        return
    if _is_streamed_rng(section, name, value):
        prefix = name + "_replay_rng_"
        state = value._state
        chunks = _chunk_count(
            len(state),
            _PERSISTENT_RNG_WORDS_PER_CHUNK,
        )
        yield prefix + "state_length", int(len(state)), False
        yield prefix + "index", int(value._index), False
        yield prefix + "chunk_count", int(chunks), False
        for chunk_index in range(chunks):
            start = chunk_index * _PERSISTENT_RNG_WORDS_PER_CHUNK
            stop = min(
                len(state),
                start + _PERSISTENT_RNG_WORDS_PER_CHUNK,
            )
            yield (
                prefix + ("%03d" % chunk_index),
                _rng_chunk_bytes(state, start, stop),
                True,
            )
        return
    if _is_streamed_packed_plan(section, name, value):
        chunks = _chunk_count(
            len(value.cells),
            _PERSISTENT_ARRAY_ITEMS_PER_CHUNK,
        )
        yield name + "_packed", True, False
        yield name + "_grid_size", int(value.grid_size), False
        yield name + "_team_ids", value.team_ids, True
        yield name + "_lengths", value.lengths, True
        yield name + "_cells_chunk_count", int(chunks), False
        for chunk_index in range(chunks):
            start = chunk_index * _PERSISTENT_ARRAY_ITEMS_PER_CHUNK
            stop = min(
                len(value.cells),
                start + _PERSISTENT_ARRAY_ITEMS_PER_CHUNK,
            )
            yield (
                name + "_cells_" + ("%03d" % chunk_index),
                value.cells[start:stop],
                True,
            )
        return
    yield name, value, True


def _send_persistent_result(
    attempt_id,
    goal,
    messages,
    state,
    state_values_encoded=True,
):
    sections = (
        "robot_attrs",
        "views",
        "cfg",
        "belief",
        "allocator_attrs",
    )
    sectioned = any(section in state for section in sections)
    state_count = 0
    if sectioned:
        for section in sections:
            for name, value in state.get(section, {}).items():
                state_count += _persistent_state_field_count(
                    section,
                    name,
                    value,
                    state_values_encoded,
                )
    resume_count = 0 if sectioned else len(state)
    field_count = 1 + len(messages) + state_count + resume_count
    _write(PROTOCOL, "PRESULT_BEGIN", attempt_id, field_count)
    field_index = 0
    _send_persistent_field(
        field_index,
        "goal",
        "-",
        "-",
        goal,
        True,
    )
    field_index += 1
    for message_index, message in enumerate(messages):
        _send_persistent_field(
            field_index,
            "message",
            "-",
            message_index,
            message,
            True,
        )
        messages[message_index] = None
        message = None
        gc.collect()
        field_index += 1
    for section in sections:
        for name, value in state.get(section, {}).items() if sectioned else ():
            for (
                wire_name,
                wire_value,
                encode_replay_value,
            ) in _persistent_state_fields(
                section,
                name,
                value,
                state_values_encoded,
            ):
                _send_persistent_field(
                    field_index,
                    "state",
                    section,
                    wire_name,
                    wire_value,
                    encode_replay_value,
                )
                wire_value = None
                gc.collect()
                field_index += 1
    if not sectioned:
        for name, value in state.items():
            _send_persistent_field(
                field_index,
                "resume",
                "-",
                name,
                value,
            )
            field_index += 1
    _write(PROTOCOL, "PRESULT_END", attempt_id, field_index)


def _run_persistent(slot, attempt_id):
    """Run only choose_goal in the timed region and stream output afterward."""
    runtime = slot.runtime
    if runtime is None:
        _send_persistent_failure(
            attempt_id,
            "state_setup_failure",
            "no active persistent context",
        )
        return
    counters = _persistent_counters(runtime)
    samples = (
        getattr(counters, "candidate_filter_time_us_samples", None)
        if counters is not None
        else None
    )
    if samples is not None:
        try:
            del samples[:]
        except Exception:
            while samples:
                samples.pop()
    heap_before = _mem_free()
    started = ticks_us()
    try:
        decision = runtime.choose_goal()
        elapsed = max(0, ticks_diff(ticks_us(), started))
    except MemoryError as exc:
        elapsed_until_failure = max(
            0,
            ticks_diff(ticks_us(), started),
        )
        heap_after = _mem_free()
        _send_persistent_failure(
            attempt_id,
            "allocator_memory_failure",
            repr(exc),
            heap_before,
            heap_after,
            elapsed_until_failure,
        )
        return
    except Exception as exc:
        elapsed_until_failure = max(
            0,
            ticks_diff(ticks_us(), started),
        )
        heap_after = _mem_free()
        _send_persistent_failure(
            attempt_id,
            "allocator_failure",
            "{}: {!r}".format(type(exc).__name__, exc),
            heap_before,
            heap_after,
            elapsed_until_failure,
        )
        return
    heap_after = _mem_free()
    reported = decision if isinstance(decision, dict) else {}
    elapsed = int(reported.get("allocator_time_us", elapsed))
    filter_us = int(
        reported.get(
            "candidate_filter_time_us",
            sum(samples) if samples is not None else 0,
        )
    )
    filter_calls = int(
        reported.get(
            "candidate_filter_calls",
            len(samples) if samples is not None else 0,
        )
    )
    measured_before, measured_after = _persistent_candidate_counts(runtime)
    before_count = int(
        reported.get("candidate_count_before", measured_before)
    )
    after_count = int(
        reported.get("candidate_count_after", measured_after)
    )
    heap_before = int(reported.get("heap_free_before", heap_before) or -1)
    heap_after = int(reported.get("heap_free_after", heap_after) or -1)
    exclusive = int(
        reported.get(
            "allocator_exclusive_time_us",
            max(0, elapsed - filter_us),
        )
    )
    class_method = getattr(runtime, "call_class", None)
    call_class = str(
        reported.get(
            "call_class",
            reported.get(
                "call_path",
                (
                    class_method()
                    if callable(class_method)
                    else (
                        "candidate_filter_only"
                        if filter_calls
                        else "cached_or_maintenance"
                    )
                ),
            ),
        )
    )
    if samples is not None:
        try:
            del samples[:]
        except Exception:
            while samples:
                samples.pop()
    # This frame is emitted before messages, snapshots, JSON, or USB result
    # transfer.  The host's allocator deadline ends upon receiving it.
    _write(
        PROTOCOL,
        "PTIMED",
        attempt_id,
        elapsed,
        filter_us,
        exclusive,
        filter_calls,
        heap_before,
        heap_after,
        before_count,
        after_count,
        call_class,
    )
    try:
        # Native search temporaries are no longer part of the timed allocator
        # result. Reclaim them before constructing messages or streamed state;
        # the recorded post-call heap value above remains the pre-GC metric.
        gc.collect()
        goal = (
            decision.get("goal")
            if isinstance(decision, dict) and "goal" in decision
            else getattr(decision, "goal", decision)
        )
        # AllocationDecision.debug and native decision detail are diagnostic,
        # not authoritative output state. Release that wrapper before
        # constructing messages and snapshots on a low-heap call.
        decision = None
        reported = None
        gc.collect()
        messages = runtime.drain_messages()
        state = runtime.snapshot_minimal()
        if not isinstance(messages, list):
            raise TypeError("drain_messages must return a list")
        if not isinstance(state, dict):
            raise TypeError("snapshot_minimal must return a mapping")
        _send_persistent_result(
            attempt_id,
            goal,
            messages,
            state,
            bool(
                getattr(
                    runtime,
                    "snapshot_values_encoded",
                    True,
                )
            ),
        )
    except Exception as exc:
        _send_persistent_failure(
            attempt_id,
            "output_serialization_failure",
            "{}: {!r}".format(type(exc).__name__, exc),
        )


def _apply_part(fixture, meta, raw):
    section = meta["section"]
    name = meta["name"]
    kind = meta["kind"]
    reset = meta["reset"]
    target = fixture["pre_state"][section]
    payload = json.loads(raw.decode("utf-8"))
    if kind == "value":
        target[name] = decode_value(payload)
        return
    if kind == "list_items":
        if reset:
            target[name] = []
        target[name].extend(decode_value(payload))
        return
    if kind == "set_items":
        if reset:
            target[name] = set()
        # Decode and insert one item at a time so the bounded wire batch does
        # not become a second complete list beside the resident set.
        for item in payload:
            target[name].add(decode_value(item))
        return
    if kind == "dict_items":
        if reset:
            target[name] = {}
        for key, value in payload:
            target[name][decode_value(key)] = decode_value(value)
        return
    if kind == "cellmap_items":
        if reset:
            from replay_memory import CellIndexedMap

            target[name] = CellIndexedMap(
                int(payload["grid_size"]),
                numeric=bool(payload["numeric"]),
            )
        for key, value in payload["items"]:
            target[name][decode_value(key)] = decode_value(value)
        return
    if kind == "array_bytes":
        from array import array

        chunk = array(
            payload["typecode"],
            binascii.a2b_base64(payload["v"]),
        )
        native_order = getattr(sys, "byteorder", "little")
        if payload.get("byteorder", native_order) != native_order:
            chunk.byteswap()
        if reset:
            target[name] = array(payload["typecode"])
        target[name].extend(chunk)
        return
    raise ValueError("unknown part kind")


def main():
    fixture_buffer = None
    fixture_meta = None
    fixture = None
    expected_sequence = 0
    part_buffer = None
    part_meta = None
    part_expected_sequence = 0
    persistent_slot = PersistentRuntimeSlot(_persistent_runtime)
    _write(
        PROTOCOL,
        "READY",
        _device_uid(),
        _build_id(),
        _implementation(),
        _frequency(),
        _mem_free(),
        _firmware_sha256(),
    )
    while True:
        line = sys.stdin.readline()
        if not line:
            continue
        stripped = line.strip()
        if not stripped:
            continue
        fields = stripped.split("|")
        if len(fields) < 2 or fields[0] != PROTOCOL:
            # A friendly MicroPython REPL can leave a prompt newline or input
            # echo in the serial stream when control transfers to this worker.
            # It is not a host protocol frame and must not poison the next
            # HELLO/CHECK exchange.
            continue
        command = fields[1]
        try:
            if command == "HELLO":
                _write(
                    PROTOCOL,
                    "HELLO",
                    _device_uid(),
                    _build_id(),
                    _implementation(),
                    _frequency(),
                    _mem_free(),
                    _firmware_sha256(),
                )
            elif command == "PING":
                _write(PROTOCOL, "PONG", fields[2] if len(fields) > 2 else "")
            elif command == "CHECK":
                double_array = 0
                try:
                    from array import array

                    probe = array("d", [1.25])
                    double_array = int(
                        len(probe) == 1 and abs(probe[0] - 1.25) < 1e-12
                    )
                    del probe
                except Exception:
                    double_array = 0
                # This replay worker has no motor or sensor imports.  Explicit
                # zero flags let preflight verify that invariant over serial.
                try:
                    import replay_build

                    expected_module_hash = replay_build.MODULE_SET_SHA256
                    actual_module_hash = _module_set_sha256(
                        replay_build.MODULE_FILES
                    )
                except Exception as exc:
                    expected_module_hash = "unavailable"
                    actual_module_hash = (
                        "error:" + type(exc).__name__
                    )
                _write(
                    PROTOCOL,
                    "CHECK",
                    double_array,
                    0,
                    0,
                    _mem_free(),
                    actual_module_hash,
                    expected_module_hash,
                )
            elif command == "BEGIN":
                if len(fields) != 5:
                    raise ValueError("BEGIN requires id, bytes, crc32")
                # Release the prior logical fixture before allocating the next
                # transfer buffer. This cleanup is outside all timed regions.
                fixture_buffer = None
                fixture = None
                part_buffer = None
                part_meta = None
                gc.collect()
                fixture_meta = {
                    "fixture_id": fields[2],
                    "length": int(fields[3]),
                    "crc32": int(fields[4]),
                }
                fixture_buffer = bytearray()
                expected_sequence = 0
                _write(PROTOCOL, "ACK", "BEGIN", fields[2])
            elif command == "DATA":
                if fixture_buffer is None or len(fields) != 5:
                    raise ValueError("DATA without BEGIN")
                sequence = int(fields[2])
                if sequence != expected_sequence:
                    raise ValueError("unexpected chunk sequence")
                payload = binascii.a2b_base64(fields[3])
                if _crc32(payload) != int(fields[4]):
                    raise ValueError("chunk crc mismatch")
                fixture_buffer.extend(payload)
                expected_sequence += 1
                _write(PROTOCOL, "ACK", "DATA", sequence)
            elif command == "END":
                if fixture_buffer is None or fixture_meta is None:
                    raise ValueError("END without BEGIN")
                if len(fixture_buffer) != fixture_meta["length"]:
                    raise ValueError("fixture length mismatch")
                if _crc32(fixture_buffer) != fixture_meta["crc32"]:
                    raise ValueError("fixture crc mismatch")
                fixture = json.loads(bytes(fixture_buffer).decode("utf-8"))
                if fixture["fixture_id"] != fixture_meta["fixture_id"]:
                    raise ValueError("fixture id mismatch")
                # Import and register packed replay classes before streamed
                # state parts are decoded.  No allocator instance is created.
                _allocator_class(fixture)
                fixture_buffer = None
                gc.collect()
                _write(PROTOCOL, "LOADED", fixture["fixture_id"], _mem_free())
            elif command == "PBEGIN":
                if fixture is None or len(fields) != 8:
                    raise ValueError("PBEGIN without loaded fixture")
                part_meta = {
                    "section": fields[2],
                    "name": binascii.a2b_base64(fields[3]).decode("utf-8"),
                    "kind": fields[4],
                    "reset": bool(int(fields[5])),
                    "length": int(fields[6]),
                    "crc32": int(fields[7]),
                }
                part_buffer = bytearray()
                part_expected_sequence = 0
                _write(PROTOCOL, "ACK", "PBEGIN", fields[3])
            elif command == "PDATA":
                if part_buffer is None or len(fields) != 5:
                    raise ValueError("PDATA without PBEGIN")
                sequence = int(fields[2])
                if sequence != part_expected_sequence:
                    raise ValueError("unexpected part chunk sequence")
                payload = binascii.a2b_base64(fields[3])
                if _crc32(payload) != int(fields[4]):
                    raise ValueError("part chunk crc mismatch")
                part_buffer.extend(payload)
                part_expected_sequence += 1
                _write(PROTOCOL, "ACK", "PDATA", sequence)
            elif command == "PEND":
                if part_buffer is None or part_meta is None:
                    raise ValueError("PEND without PBEGIN")
                if len(part_buffer) != part_meta["length"]:
                    raise ValueError("part length mismatch")
                if _crc32(part_buffer) != part_meta["crc32"]:
                    raise ValueError("part crc mismatch")
                _apply_part(fixture, part_meta, bytes(part_buffer))
                section = part_meta["section"]
                encoded_name = binascii.b2a_base64(
                    part_meta["name"].encode("utf-8")
                ).decode("ascii").strip()
                part_buffer = None
                part_meta = None
                gc.collect()
                _write(PROTOCOL, "PART", section, encoded_name, _mem_free())
            elif command == "RUN":
                if fixture is None or len(fields) != 3:
                    raise ValueError("RUN without loaded fixture")
                attempt_id = fields[2]
                _write(PROTOCOL, "START", attempt_id, fixture["fixture_id"])
                result = _run_fixture(
                    fixture,
                    attempt_id,
                    timed_callback=_send_timed,
                )
                _send_result(result)
            elif command == "ARUN":
                if fixture is None or len(fields) != 3:
                    raise ValueError("ARUN without loaded fixture")
                attempt_id = fields[2]
                _write(PROTOCOL, "START", attempt_id, fixture["fixture_id"])
                result = _run_authoritative(
                    fixture,
                    attempt_id,
                    timed_callback=_send_timed,
                )
                _send_authoritative_result(result)
            elif command == "PTRIAL":
                if fixture is None or len(fields) != 3:
                    raise ValueError("PTRIAL without loaded trial header")
                trial_id = fields[2]
                if str(fixture.get("trial_key")) != trial_id:
                    raise ValueError("persistent trial key mismatch")
                config = dict(fixture.get("trial_config", {}))
                for name in (
                    "mission",
                    "algorithm",
                    "condition_id",
                    "trial_key",
                ):
                    if name in fixture:
                        config[name] = fixture[name]
                persistent_slot.begin_trial(config)
                fixture_buffer = None
                fixture_meta = None
                fixture = None
                part_buffer = None
                part_meta = None
                gc.collect()
                _write(
                    PROTOCOL,
                    "PTRIAL_READY",
                    trial_id,
                    _mem_free(),
                )
            elif command == "PCLEAR":
                if len(fields) != 3:
                    raise ValueError("PCLEAR requires context id")
                persistent_slot.clear_context()
                fixture_buffer = None
                fixture_meta = None
                fixture = None
                part_buffer = None
                part_meta = None
                gc.collect()
                _write(
                    PROTOCOL,
                    "ACK",
                    "PCLEAR",
                    fields[2],
                    _mem_free(),
                )
            elif command == "PSETUP":
                if fixture is None or len(fields) != 3:
                    raise ValueError("PSETUP without loaded setup state")
                attempt_id = fields[2]
                try:
                    persistent_slot.prepare(
                        fixture["context_id"],
                        fixture["setup_mode"],
                        fixture.pop("pre_state"),
                        fixture.get("deleted", {}),
                        fixture.get("events", []),
                        fixture.get("resume_state", {}),
                        fixture.get("state_aliases", []),
                    )
                    context_id = persistent_slot.context_id
                    fixture_buffer = None
                    fixture_meta = None
                    fixture = None
                    part_buffer = None
                    part_meta = None
                    gc.collect()
                    _write(
                        PROTOCOL,
                        "PCALL_READY",
                        attempt_id,
                        context_id,
                        _mem_free(),
                    )
                except Exception as exc:
                    fixture_buffer = None
                    fixture_meta = None
                    fixture = None
                    part_buffer = None
                    part_meta = None
                    gc.collect()
                    _send_persistent_failure(
                        attempt_id,
                        "state_setup_failure",
                        "{}: {!r}".format(type(exc).__name__, exc),
                    )
            elif command == "PTIME":
                if len(fields) != 3:
                    raise ValueError("PTIME requires attempt id")
                _run_persistent(persistent_slot, fields[2])
            elif command == "PENDTRIAL":
                persistent_slot.end_trial()
                fixture_buffer = None
                fixture_meta = None
                fixture = None
                part_buffer = None
                part_meta = None
                gc.collect()
                _write(PROTOCOL, "PTRIAL_ENDED", _mem_free())
            elif command == "DROP":
                fixture_buffer = None
                fixture_meta = None
                fixture = None
                part_buffer = None
                part_meta = None
                gc.collect()
                _write(PROTOCOL, "DROPPED", _mem_free())
            elif command == "EXIT":
                persistent_slot.end_trial()
                _write(PROTOCOL, "BYE")
                return
            else:
                raise ValueError("unknown command")
        except KeyboardInterrupt:
            fixture_buffer = None
            fixture = None
            part_buffer = None
            part_meta = None
            persistent_slot.end_trial()
            gc.collect()
            _write(PROTOCOL, "INTERRUPTED", _mem_free())
        except Exception as exc:
            _write(
                PROTOCOL,
                "ERROR",
                command,
                type(exc).__name__,
                str(exc).replace("|", "/"),
            )
