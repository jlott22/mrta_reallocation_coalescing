"""Strict configuration model for the coalescing HIL campaign."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
ALLOCATORS = ("CBAA", "ACBBA", "PI", "HIPC", "DMCHBA", "DGA")
CORE_ALLOCATORS = ("CBAA", "ACBBA", "PI", "HIPC")
REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_RESULTS_ROOT = REPOSITORY_ROOT / "results" / "hil_reallocation_coalescing"
SAFE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)


def _slug(value: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")
    if not text:
        raise ValueError("identifier must contain an alphanumeric character")
    return text


def safe_id_component(value: str, label: str = "identifier") -> str:
    """Validate one user-controlled filesystem component without rewriting it.

    Rejection (instead of lossy slugging) makes a value such as ``../pilot``
    visibly invalid and prevents two distinct command lines from silently
    selecting the same resumable campaign directory.
    """

    text = str(value)
    if (
        text in {"", ".", ".."}
        or text != text.strip()
        or text.endswith(".")
        or text.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
        or SAFE_ID_PATTERN.fullmatch(text) is None
    ):
        raise ValueError(
            f"{label} must be one SAFE_ID path component "
            "([A-Za-z0-9][A-Za-z0-9._-]*)"
        )
    return text


def _inside_repository(path: Path, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(REPOSITORY_ROOT.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} must be inside the new repository: {resolved}") from exc
    return resolved


def _forbidden_legacy_field(value: Any, path: str = "config") -> None:
    """Reject legacy factors before they can enter an active campaign."""

    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if "top_k" in normalized or "topk" in normalized:
                raise ValueError(f"{path}.{key}: Top-K is not part of this study")
            if normalized == "mission" and str(item).lower() != "collaborative":
                raise ValueError(f"{path}.{key}: only Collaborative Visit is supported")
            _forbidden_legacy_field(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _forbidden_legacy_field(item, f"{path}[{index}]")
    elif isinstance(value, str) and value.strip().lower() == "bayesian":
        raise ValueError(f"{path}: Bayesian conditions are quarantined")


@dataclass(frozen=True)
class PolicySpec:
    """One task-arrival admission policy.

    ``count`` has no age timeout.  The deterministic trace-end flush used by
    motionless replay prevents an incomplete final batch from disappearing;
    the simulator's mission scheduler remains authoritative for mission runs.
    """

    policy: str
    batch_size: int
    max_wait_s: float | None = None
    policy_id: str = ""

    def __post_init__(self) -> None:
        policy = str(self.policy).strip().lower()
        if policy not in {"eager", "count", "bounded"}:
            raise ValueError(f"unknown policy: {self.policy}")
        batch_size = int(self.batch_size)
        if batch_size < 1:
            raise ValueError("batch_size must be at least one")
        wait = None if self.max_wait_s is None else float(self.max_wait_s)
        if wait is not None and wait <= 0:
            raise ValueError("max_wait_s must be positive when supplied")
        if policy == "eager":
            if batch_size != 1:
                raise ValueError("eager policy requires batch_size=1")
            if wait is not None:
                raise ValueError("eager policy does not use max_wait_s")
        elif policy == "count" and wait is not None:
            raise ValueError("count policy does not use max_wait_s")
        elif policy == "bounded" and wait is None:
            raise ValueError("bounded policy requires max_wait_s")
        generated_id = (
            "eager_b1"
            if policy == "eager"
            else (
                f"count_b{batch_size}"
                if policy == "count"
                else f"bounded_b{batch_size}_w{wait:g}s"
            )
        )
        object.__setattr__(self, "policy", policy)
        object.__setattr__(self, "batch_size", batch_size)
        object.__setattr__(self, "max_wait_s", wait)
        object.__setattr__(self, "policy_id", _slug(self.policy_id or generated_id))

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "PolicySpec":
        return cls(
            policy=str(value.get("policy", value.get("name", ""))),
            batch_size=int(value.get("batch_size", value.get("B", 0))),
            max_wait_s=value.get("max_wait_s", value.get("W_s")),
            policy_id=str(value.get("policy_id", value.get("id", ""))),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "policy_id": self.policy_id,
            "batch_size": self.batch_size,
            "max_wait_s": self.max_wait_s,
        }


@dataclass(frozen=True)
class HilCondition:
    allocator: str
    arrival_load: str
    policy: PolicySpec
    manifest_set_id: str

    def __post_init__(self) -> None:
        allocator = str(self.allocator).upper()
        if allocator not in ALLOCATORS:
            raise ValueError(f"unsupported allocator: {allocator}")
        object.__setattr__(self, "allocator", allocator)
        object.__setattr__(self, "arrival_load", _slug(self.arrival_load))
        object.__setattr__(self, "manifest_set_id", _slug(self.manifest_set_id))

    @property
    def condition_id(self) -> str:
        return "__".join(
            (
                "collaborative",
                self.allocator.lower(),
                self.arrival_load,
                self.policy.policy_id,
                self.manifest_set_id,
                "unrestricted",
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "mission": "collaborative",
            "allocator": self.allocator,
            "arrival_load": self.arrival_load,
            **self.policy.as_dict(),
            "manifest_set_id": self.manifest_set_id,
            "candidate_mode": "unrestricted",
            "max_candidate_cells": None,
        }


@dataclass(frozen=True)
class CampaignConfig:
    source_path: Path
    campaign_id: str
    manifest_root: Path
    manifest_set_id: str
    results_root: Path
    allocators: tuple[str, ...]
    arrival_loads: tuple[str, ...]
    arrival_rates_tasks_per_s: tuple[tuple[str, float], ...]
    policies: tuple[PolicySpec, ...]
    trace_ids: tuple[str, ...]
    allocator_rounds_per_epoch: int
    timeout_seconds: float

    def conditions(self) -> tuple[HilCondition, ...]:
        return tuple(
            HilCondition(allocator, load, policy, self.manifest_set_id)
            for allocator in self.allocators
            for load in self.arrival_loads
            for policy in self.policies
        )

    def arrival_rate(self, load_id: str) -> float:
        return dict(self.arrival_rates_tasks_per_s)[str(load_id)]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "study_id": "mrta_reallocation_coalescing",
            "campaign_id": self.campaign_id,
            "manifest_root": str(self.manifest_root),
            "manifest_set_id": self.manifest_set_id,
            "results_root": str(self.results_root),
            "allocators": list(self.allocators),
            "arrival_loads": list(self.arrival_loads),
            "arrival_rates_tasks_per_s": dict(self.arrival_rates_tasks_per_s),
            "policies": [item.as_dict() for item in self.policies],
            "trace_ids": list(self.trace_ids),
            "allocator_rounds_per_epoch": self.allocator_rounds_per_epoch,
            "timeout_seconds": self.timeout_seconds,
            "mission": "collaborative",
            "candidate_mode": "unrestricted",
            "max_candidate_cells": None,
        }


def _unique(values: Iterable[str], label: str) -> tuple[str, ...]:
    result = tuple(values)
    if not result:
        raise ValueError(f"{label} must not be empty")
    if len(set(result)) != len(result):
        raise ValueError(f"{label} contains duplicates")
    return result


def load_campaign_config(path: str | Path) -> CampaignConfig:
    source = Path(path).resolve()
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("campaign config must be a JSON object")
    _forbidden_legacy_field(raw)
    if int(raw.get("schema_version", SCHEMA_VERSION)) != SCHEMA_VERSION:
        raise ValueError("unsupported HIL campaign config schema_version")
    study = str(raw.get("study_id", "mrta_reallocation_coalescing"))
    if study != "mrta_reallocation_coalescing":
        raise ValueError(f"wrong study_id: {study}")

    def resolve_repo_path(value: str | Path, label: str) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = REPOSITORY_ROOT / candidate
        return _inside_repository(candidate, label)

    manifest_root = resolve_repo_path(raw["manifest_root"], "manifest_root")
    results_root = resolve_repo_path(
        raw.get("results_root", DEFAULT_RESULTS_ROOT), "results_root"
    )
    manifest_set_id = _slug(
        str(raw.get("manifest_set_id", manifest_root.name))
    )
    if _slug(manifest_root.name) != manifest_set_id:
        raise ValueError(
            "manifest_set_id must match the generated manifest directory name"
        )
    allocators = _unique(
        (str(item).upper() for item in raw.get("allocators", CORE_ALLOCATORS)),
        "allocators",
    )
    unknown = sorted(set(allocators) - set(ALLOCATORS))
    if unknown:
        raise ValueError("unknown allocators: " + ", ".join(unknown))
    loads = _unique(
        (_slug(item) for item in raw.get("arrival_loads", ())),
        "arrival_loads",
    )
    raw_rates = raw.get("arrival_rates_tasks_per_s")
    if not isinstance(raw_rates, dict):
        raise ValueError("arrival_rates_tasks_per_s must explicitly map every load")
    normalized_rates = {_slug(key): float(value) for key, value in raw_rates.items()}
    if set(normalized_rates) != set(loads):
        raise ValueError("arrival_rates_tasks_per_s keys must equal arrival_loads")
    if any(value <= 0 for value in normalized_rates.values()):
        raise ValueError("arrival rates must be positive")
    rates = tuple((load, normalized_rates[load]) for load in loads)
    policies = tuple(
        PolicySpec.from_mapping(item) for item in raw.get("policies", ())
    )
    if not policies:
        raise ValueError("policies must not be empty")
    if len({item.policy_id for item in policies}) != len(policies):
        raise ValueError("policy_id values must be unique")
    trace_ids = _unique(
        (_slug(item) for item in raw.get("trace_ids", ())),
        "trace_ids",
    )
    rounds = int(raw.get("allocator_rounds_per_epoch", 1))
    if rounds < 1:
        raise ValueError("allocator_rounds_per_epoch must be at least one")
    timeout = float(raw.get("timeout_seconds", 30.0))
    if timeout <= 0:
        raise ValueError("timeout_seconds must be positive")
    return CampaignConfig(
        source_path=source,
        campaign_id=_slug(raw.get("campaign_id", source.stem)),
        manifest_root=manifest_root,
        manifest_set_id=manifest_set_id,
        results_root=results_root,
        allocators=allocators,
        arrival_loads=loads,
        arrival_rates_tasks_per_s=rates,
        policies=policies,
        trace_ids=trace_ids,
        allocator_rounds_per_epoch=rounds,
        timeout_seconds=timeout,
    )
