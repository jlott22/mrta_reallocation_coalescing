try:
    import binascii
except ImportError:  # pragma: no cover - MicroPython
    import ubinascii as binascii

try:
    from array import array
except ImportError:  # pragma: no cover
    array = None

try:
    import sys
except ImportError:  # pragma: no cover
    sys = None

from replay_random import Random

OBJECT_CLASSES = {}
_JSON_STRING_BUFFER_CHARS = 64
_BASE64_RAW_CHUNK_BYTES = 96


def register_object_class(cls, name=None, constructor_fields=None):
    class_name = name or getattr(cls, "__name__", None)
    if not class_name:
        raise ValueError("object class registration requires an explicit name")
    OBJECT_CLASSES[class_name] = (cls, constructor_fields)


def clear_object_classes():
    OBJECT_CLASSES.clear()


def decode_value(value):
    if not isinstance(value, (dict, list)):
        return value
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    tag = value.get("@")
    if tag is None:
        return {key: decode_value(item) for key, item in value.items()}
    if tag == "float":
        return {
            "nan": float("nan"),
            "inf": float("inf"),
            "-inf": -float("inf"),
        }[value["v"]]
    if tag in ("bytes", "bytearray"):
        raw = binascii.a2b_base64(value["v"])
        return raw if tag == "bytes" else bytearray(raw)
    if tag == "array":
        typecode = value["typecode"]
        return array(typecode, [decode_value(item) for item in value["v"]])
    if tag == "arraybin":
        result = array(
            value["typecode"],
            binascii.a2b_base64(value["v"]),
        )
        native_order = getattr(sys, "byteorder", "little")
        if value.get("byteorder", native_order) != native_order:
            result.byteswap()
        return result
    if tag == "rng":
        rng = Random()
        rng.setstate(decode_value(value["v"]))
        return rng
    if tag == "rngbin":
        state_values = array(
            "I",
            binascii.a2b_base64(value["state"]),
        )
        native_order = getattr(sys, "byteorder", "little")
        if value.get("byteorder", native_order) != native_order:
            state_values.byteswap()
        if int(value["version"]) not in (2, 3):
            raise ValueError("unsupported random state")
        if decode_value(value["gauss"]) is not None:
            raise ValueError("cached Gaussian random state is unsupported")
        if len(state_values) != 625:
            raise ValueError("invalid random state length")
        index = int(state_values[624])
        # Keep both allocations compact while dropping the trailing index.
        # Unlike tuple(...) + setstate(...), this never creates a
        # 625-element pointer container on the controller.
        restored_state = array("I")
        for state_index in range(624):
            restored_state.append(state_values[state_index])
        state_values = None
        return Random(None, restored_state, index)
    if tag == "tuple":
        return tuple(decode_value(item) for item in value["v"])
    if tag == "set":
        return set(decode_value(item) for item in value["v"])
    if tag == "dict":
        return {
            decode_value(key): decode_value(item)
            for key, item in value["v"]
        }
    if tag == "cellmap":
        from replay_memory import CellIndexedMap

        values = decode_value(value["v"])
        return CellIndexedMap(
            int(value["grid_size"]),
            numeric=bool(value["numeric"]),
            initial=values.items(),
        )
    if tag == "object":
        registration = OBJECT_CLASSES.get(value["class"])
        if registration is None:
            raise ValueError("unregistered object class: " + value["class"])
        attributes = decode_value(value["attrs"])
        cls, constructor_fields = registration
        if constructor_fields:
            instance = cls(
                *(attributes[name] for name in constructor_fields)
            )
        else:
            instance = cls.__new__(cls)
        for name, item in attributes.items():
            setattr(instance, name, item)
        return instance
    raise ValueError("unknown fixture tag: " + str(tag))


def _sort_key(value):
    if value is None:
        return "0"
    if isinstance(value, bool):
        return "1" + str(int(value))
    if isinstance(value, int):
        return "2" + str(value)
    if isinstance(value, float):
        return "3" + repr(value)
    if isinstance(value, str):
        return "4" + value
    if isinstance(value, tuple):
        return "5(" + "|".join(_sort_key(item) for item in value) + ")"
    return "9" + repr(value)


def _json_string_parts(value):
    """Yield one JSON string without allocating its complete escaped form."""

    yield b'"'
    pending = []
    pending_length = 0
    escapes = {
        '"': '\\"',
        "\\": "\\\\",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    for character in str(value):
        escaped = escapes.get(character)
        if escaped is None:
            codepoint = ord(character)
            escaped = (
                "\\u%04x" % codepoint
                if codepoint < 0x20
                else character
            )
        pending.append(escaped)
        pending_length += len(escaped)
        if pending_length >= _JSON_STRING_BUFFER_CHARS:
            yield "".join(pending).encode("utf-8")
            pending = []
            pending_length = 0
    if pending:
        yield "".join(pending).encode("utf-8")
    yield b'"'


def _plain_json_key_parts(value):
    if isinstance(value, str):
        for part in _json_string_parts(value):
            yield part
        return
    if value is None:
        text = "null"
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        raise TypeError(
            "unsupported JSON mapping key: " + type(value).__name__
        )
    for part in _json_string_parts(text):
        yield part


def _plain_json_parts(value):
    """Yield JSON for an already replay-encoded value."""

    if value is None:
        yield b"null"
        return
    if value is True:
        yield b"true"
        return
    if value is False:
        yield b"false"
        return
    if isinstance(value, int):
        yield str(value).encode("ascii")
        return
    if isinstance(value, float):
        if value != value:
            yield b"NaN"
        elif value == float("inf"):
            yield b"Infinity"
        elif value == -float("inf"):
            yield b"-Infinity"
        else:
            yield repr(value).encode("ascii")
        return
    if isinstance(value, str):
        for part in _json_string_parts(value):
            yield part
        return
    if isinstance(value, (list, tuple)):
        yield b"["
        first = True
        for item in value:
            if first:
                first = False
            else:
                yield b","
            for part in _plain_json_parts(item):
                yield part
        yield b"]"
        return
    if isinstance(value, dict):
        yield b"{"
        first = True
        for key, item in value.items():
            if first:
                first = False
            else:
                yield b","
            for part in _plain_json_key_parts(key):
                yield part
            yield b":"
            for part in _plain_json_parts(item):
                yield part
        yield b"}"
        return
    raise TypeError(
        "unsupported already-encoded JSON value: " + type(value).__name__
    )


def _base64_parts(value, array_items=False):
    """Yield continuous base64 text using raw chunks divisible by three."""

    length = len(value)
    step = (
        48
        if array_items
        else _BASE64_RAW_CHUNK_BYTES
    )
    for start in range(0, length, step):
        raw = bytes(value[start:start + step])
        # The raw byte count for every non-final chunk is divisible by three,
        # so concatenating these independently encoded pieces is identical to
        # encoding the complete value.
        yield binascii.b2a_base64(raw).strip()


def _encoded_sequence_parts(values):
    yield b"["
    first = True
    for item in values:
        if first:
            first = False
        else:
            yield b","
        for part in _encoded_json_parts(item):
            yield part
    yield b"]"


def _encoded_mapping_parts(items):
    """Yield the replay ``dict`` tag without copying or sorting its items."""

    yield b'{"@":"dict","v":['
    first = True
    for key, item in items:
        if first:
            first = False
        else:
            yield b","
        yield b"["
        for part in _encoded_json_parts(key):
            yield part
        yield b","
        for part in _encoded_json_parts(item):
            yield part
        yield b"]"
    yield b"]}"


def _cellmap_items(value):
    """Iterate a CellIndexedMap without its compatibility ``items`` copy."""

    # Generated replay_memory intentionally implements items() as a list for
    # compatibility with allocators that index or reuse it. Calling it here
    # would duplicate the complete 361-cell map before output serialization.
    for key in value:
        yield key, value[key]


def _object_attribute_items(value):
    attributes = getattr(value, "__dict__", None)
    if attributes is not None:
        for name, item in attributes.items():
            yield name, item
    value_type = type(value)
    classes = getattr(value_type, "__mro__", (value_type,))
    for cls in classes:
        slots = getattr(cls, "__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            if (
                attributes is not None
                and name in attributes
            ):
                continue
            if hasattr(value, name):
                yield name, getattr(value, name)


def _encoded_random_parts(value):
    """Stream replay Random state without constructing its 625-item tuple."""

    state = getattr(value, "_state", None)
    index = getattr(value, "_index", None)
    if state is None or index is None:
        # This compatibility path is not used by the deployed replay Random.
        state_value = value.getstate()
        yield b'{"@":"rng","v":'
        for part in _encoded_json_parts(state_value):
            yield part
        yield b"}"
        return
    yield b'{"@":"rng","v":{"@":"tuple","v":['
    yield str(int(getattr(value, "VERSION", 3))).encode("ascii")
    yield b',{"@":"tuple","v":['
    first = True
    for item in state:
        if first:
            first = False
        else:
            yield b","
        yield str(int(item)).encode("ascii")
    if not first:
        yield b","
    yield str(int(index)).encode("ascii")
    yield b"]},null]}}"


def _encoded_json_parts(value):
    """Yield JSON with the exact logical tagging used by ``encode_value``."""

    if (
        value is None
        or isinstance(value, (bool, int, str))
    ):
        for part in _plain_json_parts(value):
            yield part
        return
    if isinstance(value, float):
        if value != value:
            marker = "nan"
        elif value == float("inf"):
            marker = "inf"
        elif value == -float("inf"):
            marker = "-inf"
        else:
            for part in _plain_json_parts(value):
                yield part
            return
        yield b'{"@":"float","v":'
        for part in _json_string_parts(marker):
            yield part
        yield b"}"
        return
    if isinstance(value, (bytes, bytearray)):
        marker = "bytes" if isinstance(value, bytes) else "bytearray"
        yield ('{"@":"' + marker + '","v":"').encode("ascii")
        for part in _base64_parts(value):
            yield part
        yield b'"}'
        return
    if value.__class__.__name__ == "array":
        typecode = getattr(value, "typecode", None)
        if typecode is None:
            for part in _encoded_sequence_parts(value):
                yield part
            return
        yield b'{"@":"arraybin","typecode":'
        for part in _json_string_parts(typecode):
            yield part
        yield b',"byteorder":'
        for part in _json_string_parts(
            getattr(sys, "byteorder", "little")
        ):
            yield part
        yield b',"v":"'
        for part in _base64_parts(value, array_items=True):
            yield part
        yield b'"}'
        return
    if isinstance(value, Random):
        for part in _encoded_random_parts(value):
            yield part
        return
    if value.__class__.__name__ == "CellIndexedMap":
        yield b'{"@":"cellmap","grid_size":'
        yield str(int(value.grid_size)).encode("ascii")
        yield b',"numeric":'
        yield b"true" if value._numeric else b"false"
        yield b',"v":'
        for part in _encoded_mapping_parts(_cellmap_items(value)):
            yield part
        yield b"}"
        return
    if isinstance(value, tuple):
        yield b'{"@":"tuple","v":'
        for part in _encoded_sequence_parts(value):
            yield part
        yield b"}"
        return
    if isinstance(value, set):
        # Set order has no logical meaning. Avoid sorted(...), which creates a
        # second pointer container as large as the resident set.
        yield b'{"@":"set","v":'
        for part in _encoded_sequence_parts(value):
            yield part
        yield b"}"
        return
    if isinstance(value, list):
        for part in _encoded_sequence_parts(value):
            yield part
        return
    if isinstance(value, dict):
        for part in _encoded_mapping_parts(value.items()):
            yield part
        return
    attributes = getattr(value, "__dict__", None)
    slots = getattr(type(value), "__slots__", ())
    if attributes or slots:
        yield b'{"@":"object","class":'
        for part in _json_string_parts(value.__class__.__name__):
            yield part
        yield b',"attrs":'
        for part in _encoded_mapping_parts(
            _object_attribute_items(value)
        ):
            yield part
        yield b"}"
        return
    raise TypeError(
        "unsupported fixture value: " + type(value).__name__
    )


def iter_json_chunks(
    value,
    encode_replay_value=False,
    chunk_size=192,
):
    """Yield bounded JSON chunks without constructing a complete document.

    ``encode_replay_value`` applies the same tagged logical representation as
    :func:`encode_value` while traversing the resident object directly.
    Otherwise ``value`` must already be JSON/replay encoded.
    """

    chunk_size = int(chunk_size)
    if chunk_size <= 0:
        raise ValueError("JSON chunk size must be positive")
    parts = (
        _encoded_json_parts(value)
        if encode_replay_value
        else _plain_json_parts(value)
    )
    buffer = bytearray()
    for part in parts:
        offset = 0
        while offset < len(part):
            room = chunk_size - len(buffer)
            take = min(room, len(part) - offset)
            buffer.extend(part[offset:offset + take])
            offset += take
            if len(buffer) == chunk_size:
                yield bytes(buffer)
                buffer = bytearray()
    if buffer:
        yield bytes(buffer)


def encode_value(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value:
            return {"@": "float", "v": "nan"}
        if value == float("inf"):
            return {"@": "float", "v": "inf"}
        if value == -float("inf"):
            return {"@": "float", "v": "-inf"}
        return value
    if isinstance(value, bytes):
        return {"@": "bytes", "v": binascii.b2a_base64(value).decode().strip()}
    if isinstance(value, bytearray):
        return {
            "@": "bytearray",
            "v": binascii.b2a_base64(bytes(value)).decode().strip(),
        }
    if value.__class__.__name__ == "array":
        typecode = getattr(value, "typecode", None)
        if typecode is None:
            # MicroPython 1.24 arrays expose neither ``typecode`` nor
            # ``itemsize``.  Guessing a type from the byte length is
            # ambiguous and previously mislabeled every Pololu array as
            # float32.  Encode the few bounded logical arrays item-by-item;
            # large transient workspaces are excluded from snapshots.
            return [encode_value(item) for item in value]
        return {
            "@": "arraybin",
            "typecode": typecode,
            "byteorder": getattr(sys, "byteorder", "little"),
            "v": binascii.b2a_base64(bytes(value)).decode().strip(),
        }
    if isinstance(value, Random):
        return {"@": "rng", "v": encode_value(value.getstate())}
    if value.__class__.__name__ == "CellIndexedMap":
        return {
            "@": "cellmap",
            "grid_size": int(value.grid_size),
            "numeric": bool(value._numeric),
            "v": encode_value(dict(value.items())),
        }
    if isinstance(value, tuple):
        return {"@": "tuple", "v": [encode_value(item) for item in value]}
    if isinstance(value, set):
        return {
            "@": "set",
            "v": [encode_value(item) for item in sorted(value, key=_sort_key)],
        }
    if isinstance(value, list):
        return [encode_value(item) for item in value]
    if isinstance(value, dict):
        items = list(value.items())
        items.sort(key=lambda item: _sort_key(item[0]))
        return {
            "@": "dict",
            "v": [
                [encode_value(key), encode_value(item)]
                for key, item in items
            ],
        }
    attributes = {}
    if hasattr(value, "__dict__"):
        attributes.update(value.__dict__)
    value_type = type(value)
    # MicroPython 1.24 class objects do not expose CPython's ``__mro__``.
    # The concrete class still exposes its own ``__slots__``, which is all
    # deployed packed-plan objects require.
    classes = getattr(value_type, "__mro__", (value_type,))
    for cls in classes:
        slots = getattr(cls, "__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            if name not in attributes and hasattr(value, name):
                attributes[name] = getattr(value, name)
    if attributes:
        return {
            "@": "object",
            "class": value.__class__.__name__,
            "attrs": encode_value(attributes),
        }
    raise TypeError("unsupported fixture value: " + type(value).__name__)
