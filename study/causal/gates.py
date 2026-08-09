"""Fail-closed gate records for native causal execution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from study.manifests import sha256_file


class GateError(RuntimeError):
    """Raised when scientific execution prerequisites are not sealed and valid."""


@dataclass(frozen=True)
class ValidatedGate:
    path: Path
    sha256: str
    kind: str
    report: dict[str, Any]


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"cannot read gate {path}: {error}") from error
    if not isinstance(value, dict):
        raise GateError(f"gate must be a JSON object: {path}")
    return value


def validate_gate(path: Path | str, expected_kind: str | None = None) -> ValidatedGate:
    resolved = Path(path).resolve()
    report = _load(resolved)
    if report.get("schema_version") != 1:
        raise GateError(f"unsupported gate schema: {resolved}")
    kind = report.get("report_kind") or report.get("gate_kind")
    if not isinstance(kind, str) or not kind:
        raise GateError(f"gate lacks report_kind: {resolved}")
    if expected_kind is not None and kind != expected_kind:
        raise GateError(f"expected {expected_kind!r}, found {kind!r}: {resolved}")
    passed = report.get("passed")
    if passed is None:
        passed = report.get("scientifically_valid")
    if passed is not True:
        raise GateError(f"gate did not pass scientifically: {resolved}")
    if report.get("development_only_pass") is True:
        raise GateError(f"development-only evidence cannot satisfy a scientific gate: {resolved}")
    if kind in {"rp2040_parity_preflight", "causal_hardware_smoke"}:
        if report.get("hardware_validated") is not True:
            raise GateError(f"{kind} lacks real-hardware validation: {resolved}")
    return ValidatedGate(resolved, sha256_file(resolved), kind, report)


def validate_gate_set(
    paths: Iterable[Path | str],
    *,
    required_kinds: Iterable[str] = (),
) -> dict[str, ValidatedGate]:
    gates: dict[str, ValidatedGate] = {}
    for path in paths:
        gate = validate_gate(path)
        if gate.kind in gates:
            raise GateError(f"duplicate gate kind: {gate.kind}")
        gates[gate.kind] = gate
    missing = sorted(set(required_kinds) - set(gates))
    if missing:
        raise GateError(f"missing required gates: {', '.join(missing)}")
    return gates


def verify_sealed_gate(gate: dict[str, Any], repo_root: Path) -> ValidatedGate:
    raw_path = gate.get("path")
    digest = gate.get("sha256")
    if not isinstance(raw_path, str) or not isinstance(digest, str):
        raise GateError("sealed gate entry requires path and sha256")
    path = Path(raw_path)
    if not path.is_absolute():
        path = (repo_root / path).resolve()
    else:
        path = path.resolve()
    if path != repo_root and repo_root not in path.parents:
        raise GateError("sealed gate path escapes repository")
    if sha256_file(path) != digest:
        raise GateError(f"sealed gate hash mismatch: {path}")
    expected_kind = gate.get("kind")
    return validate_gate(path, expected_kind if isinstance(expected_kind, str) else None)
