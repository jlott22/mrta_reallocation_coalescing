"""Stable semantic fingerprints shared by CPython and MicroPython.

The format deliberately normalizes floating-point state to six significant
digits, leaving one guard digit below binary32 precision so equivalent values
do not fail only because they fall on opposite sides of a rounding boundary.
Goals, integer state, RNG state, collection structure, and messages remain
exact; floating allocator state tolerates the expected binary32 storage used
by the controller ports.  Encoded ``array('d')`` and ``array('f')`` values are
therefore equivalent when their logical elements match.
"""

try:
    import hashlib
except ImportError:  # pragma: no cover - MicroPython
    import uhashlib as hashlib

try:
    import struct
except ImportError:  # pragma: no cover - MicroPython
    import ustruct as struct


def _sha256():
    return hashlib.sha256()


def _update_length(digest, number):
    digest.update(str(int(number)).encode("ascii"))
    digest.update(b":")


def _key_bytes(value):
    digest = _sha256()
    _feed(digest, value)
    raw = digest.digest()
    return raw


def _feed(digest, value):
    if value is None:
        digest.update(b"N")
        return
    if value is True:
        digest.update(b"T")
        return
    if value is False:
        digest.update(b"F")
        return
    if isinstance(value, int):
        digest.update(b"I")
        encoded = str(value).encode("ascii")
        _update_length(digest, len(encoded))
        digest.update(encoded)
        return
    if isinstance(value, float):
        digest.update(b"R")
        if value != value:
            encoded = b"nan"
        elif value == float("inf"):
            encoded = b"inf"
        elif value == -float("inf"):
            encoded = b"-inf"
        else:
            encoded = ("{:.6g}".format(value)).encode("ascii")
        _update_length(digest, len(encoded))
        digest.update(encoded)
        return
    if isinstance(value, str):
        digest.update(b"S")
        encoded = value.encode("utf-8")
        _update_length(digest, len(encoded))
        digest.update(encoded)
        return
    if isinstance(value, (bytes, bytearray)):
        digest.update(b"B")
        _update_length(digest, len(value))
        digest.update(bytes(value))
        return
    if isinstance(value, (list, tuple)):
        digest.update(b"L")
        _update_length(digest, len(value))
        for item in value:
            _feed(digest, item)
        return
    if isinstance(value, dict):
        if value.get("@") == "object":
            digest.update(b"O")
            _feed(digest, value.get("class"))
            _feed(digest, value.get("attrs"))
            return
        if (
            value.get("@") == "array"
            and value.get("typecode") in ("d", "f")
        ):
            digest.update(b"A")
            _feed(digest, value.get("v", []))
            return
        digest.update(b"M")
        keys = list(value)
        keys.sort(key=_key_bytes)
        _update_length(digest, len(keys))
        for key in keys:
            _feed(digest, key)
            _feed(digest, value[key])
        return
    raise TypeError("unsupported fingerprint value: " + type(value).__name__)


def semantic_sha256(value):
    digest = _sha256()
    _feed(digest, value)
    try:
        return digest.hexdigest()
    except AttributeError:  # pragma: no cover - MicroPython uhashlib
        try:
            import binascii
        except ImportError:
            import ubinascii as binascii
        return binascii.hexlify(digest.digest()).decode("ascii")


def _logical_key_bytes(value):
    digest = _sha256()
    _feed_logical(digest, value)
    return digest.digest()


def _feed_logical(digest, value):
    """Hash encoded fixture values and live replay objects identically.

    Unlike ``semantic_sha256``, this function normalizes the tagged transport
    representation to its logical value.  It lets the device verify large
    arrays and packed plans without allocating a second encoded copy after the
    timed allocator call.
    """
    if isinstance(value, dict) and "@" in value:
        tag = value.get("@")
        if tag == "float":
            mapped = {
                "nan": float("nan"),
                "inf": float("inf"),
                "-inf": -float("inf"),
            }[value["v"]]
            _feed_logical(digest, mapped)
            return
        if tag in ("bytes", "bytearray"):
            try:
                import binascii
            except ImportError:  # pragma: no cover - MicroPython
                import ubinascii as binascii
            _feed_logical(digest, binascii.a2b_base64(value["v"]))
            return
        if tag in ("tuple", "set"):
            marker = b"L" if tag == "tuple" else b"E"
            items = list(value["v"])
            if tag == "set":
                items.sort(key=_logical_key_bytes)
            digest.update(marker)
            _update_length(digest, len(items))
            for item in items:
                _feed_logical(digest, item)
            return
        if tag == "dict":
            pairs = list(value["v"])
            pairs.sort(key=lambda pair: _logical_key_bytes(pair[0]))
            digest.update(b"M")
            _update_length(digest, len(pairs))
            for key, item in pairs:
                _feed_logical(digest, key)
                _feed_logical(digest, item)
            return
        if tag == "array":
            typecode = value.get("typecode", "")
            digest.update(b"A")
            digest.update(
                ("f" if typecode in ("d", "f") else typecode).encode("ascii")
            )
            _feed_logical(digest, value.get("v", []))
            return
        if tag == "rng":
            digest.update(b"G")
            _feed_logical(digest, value["v"])
            return
        if tag == "cellmap":
            digest.update(b"C")
            _feed_logical(digest, int(value["grid_size"]))
            _feed_logical(digest, bool(value["numeric"]))
            _feed_logical(digest, value["v"])
            return
        if tag == "object":
            digest.update(b"O")
            _feed_logical(digest, value.get("class"))
            _feed_logical(digest, value.get("attrs"))
            return
    if value is None or isinstance(value, (bool, int, float, str, bytes, bytearray)):
        _feed(digest, value)
        return
    class_name = value.__class__.__name__
    if class_name in ("array", "TypedArrayView"):
        typecode = getattr(value, "typecode", "")
        digest.update(b"A")
        digest.update(
            ("f" if typecode in ("d", "f") else typecode).encode("ascii")
        )
        digest.update(b"L")
        _update_length(digest, len(value))
        for item in value:
            _feed_logical(digest, item)
        return
    if class_name == "CellIndexedMap":
        digest.update(b"C")
        _feed_logical(digest, int(value.grid_size))
        _feed_logical(digest, bool(value._numeric))
        keys = list(value)
        keys.sort(key=_logical_key_bytes)
        digest.update(b"M")
        _update_length(digest, len(keys))
        for key in keys:
            _feed_logical(digest, key)
            _feed_logical(digest, value[key])
        return
    if (
        class_name == "Random"
        and callable(getattr(value, "getstate", None))
    ):
        digest.update(b"G")
        _feed_logical(digest, value.getstate())
        return
    if isinstance(value, (list, tuple)):
        digest.update(b"L")
        _update_length(digest, len(value))
        for item in value:
            _feed_logical(digest, item)
        return
    if isinstance(value, set):
        items = list(value)
        items.sort(key=_logical_key_bytes)
        digest.update(b"E")
        _update_length(digest, len(items))
        for item in items:
            _feed_logical(digest, item)
        return
    if isinstance(value, dict):
        keys = list(value)
        keys.sort(key=_logical_key_bytes)
        digest.update(b"M")
        _update_length(digest, len(keys))
        for key in keys:
            _feed_logical(digest, key)
            _feed_logical(digest, value[key])
        return
    attributes = {}
    if hasattr(value, "__dict__"):
        attributes.update(value.__dict__)
    for cls in type(value).__mro__:
        slots = getattr(cls, "__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            if name not in attributes and hasattr(value, name):
                attributes[name] = getattr(value, name)
    if attributes:
        digest.update(b"O")
        _feed_logical(digest, class_name)
        _feed_logical(digest, attributes)
        return
    raise TypeError("unsupported logical fingerprint value: " + class_name)


def logical_sha256(value):
    digest = _sha256()
    _feed_logical(digest, value)
    try:
        return digest.hexdigest()
    except AttributeError:  # pragma: no cover - MicroPython uhashlib
        try:
            import binascii
        except ImportError:
            import ubinascii as binascii
        return binascii.hexlify(digest.digest()).decode("ascii")
