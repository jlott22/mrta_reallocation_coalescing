from __future__ import annotations

import array
import base64
import gzip
import hashlib
import importlib
import json
import math
import random
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator


def _sort_key(value: Any) -> str:
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
    return "9" + repr(encode_value(value))


def encode_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return {"@": "float", "v": "nan"}
        if math.isinf(value):
            return {"@": "float", "v": "inf" if value > 0 else "-inf"}
        return value
    if isinstance(value, bytes):
        return {"@": "bytes", "v": base64.b64encode(value).decode("ascii")}
    if isinstance(value, bytearray):
        return {"@": "bytearray", "v": base64.b64encode(value).decode("ascii")}
    if isinstance(value, array.array):
        return {
            "@": "array",
            "typecode": value.typecode,
            "v": [encode_value(item) for item in value],
        }
    if isinstance(value, random.Random):
        return {"@": "rng", "v": encode_value(value.getstate())}
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
        items = sorted(value.items(), key=lambda item: _sort_key(item[0]))
        return {
            "@": "dict",
            "v": [
                [encode_value(key), encode_value(item_value)]
                for key, item_value in items
            ],
        }
    if value.__class__.__name__ == "CellIndexedMap":
        return {
            "@": "cellmap",
            "grid_size": int(value.grid_size),
            "numeric": bool(value._numeric),
            "v": encode_value(dict(value.items())),
        }
    attributes: dict[str, Any] = {}
    if hasattr(value, "__dict__"):
        attributes.update(vars(value))
    for cls in type(value).__mro__:
        slots = getattr(cls, "__slots__", ())
        if isinstance(slots, str):
            slots = (slots,)
        for name in slots:
            if name not in attributes and hasattr(value, name):
                attributes[name] = getattr(value, name)
    if attributes:
        return {
            "@": "object",
            "module": value.__class__.__module__,
            "class": value.__class__.__name__,
            "attrs": encode_value(attributes),
        }
    raise TypeError(f"unsupported fixture value: {type(value).__name__}")


def decode_value(value: Any) -> Any:
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
    if tag == "bytes":
        return base64.b64decode(value["v"])
    if tag == "bytearray":
        return bytearray(base64.b64decode(value["v"]))
    if tag == "array":
        return array.array(value["typecode"], [decode_value(item) for item in value["v"]])
    if tag == "arraybin":
        result = array.array(value["typecode"])
        result.frombytes(base64.b64decode(value["v"]))
        if value.get("byteorder", sys.byteorder) != sys.byteorder:
            result.byteswap()
        return result
    if tag == "rng":
        rng = random.Random()
        rng.setstate(decode_value(value["v"]))
        return rng
    if tag == "tuple":
        return tuple(decode_value(item) for item in value["v"])
    if tag == "set":
        return set(decode_value(item) for item in value["v"])
    if tag == "dict":
        return {
            decode_value(key): decode_value(item_value)
            for key, item_value in value["v"]
        }
    if tag == "cellmap":
        return decode_value(value["v"])
    if tag == "object":
        module = importlib.import_module(value["module"])
        cls = getattr(module, value["class"])
        instance = cls.__new__(cls)
        for name, item in decode_value(value["attrs"]).items():
            setattr(instance, name, item)
        return instance
    raise ValueError(f"unknown fixture tag: {tag}")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def fixture_hash(fixture: dict[str, Any]) -> str:
    without_hash = {key: value for key, value in fixture.items() if key != "fixture_sha256"}
    return hashlib.sha256(canonical_json_bytes(without_hash)).hexdigest()


def write_trace(path: Path, fixtures: Iterable[dict[str, Any]]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    count = 0
    digest = hashlib.sha256()
    with temporary.open("wb") as raw_handle:
        gzip_handle = gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_handle,
            compresslevel=6,
            mtime=0,
        )
        handle = gzip_handle
        for fixture in fixtures:
            fixture["fixture_sha256"] = fixture_hash(fixture)
            line = canonical_json_bytes(fixture) + b"\n"
            handle.write(line)
            digest.update(line)
            count += 1
        gzip_handle.close()
    temporary.replace(path)
    return {"fixture_count": count, "content_sha256": digest.hexdigest()}


def read_trace(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                fixture = json.loads(line)
                if fixture_hash(fixture) != fixture.get("fixture_sha256"):
                    raise ValueError(f"fixture hash mismatch in {path}")
                yield fixture
