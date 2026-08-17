"""Validated data model for the native causal campaign."""

from __future__ import annotations

import hashlib
import json
import math
import re
import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from study.manifests import canonical_json_bytes, sha256_file, validate_manifest_set


SCHEMA_VERSION = 1
PUBLICATION_WORKER_COUNT = 4
PRIMARY_ALGORITHMS = ("CBAA", "ACBBA", "PI", "HIPC")
POLICY_MODES = frozenset({"eager", "count", "bounded"})
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError(f"{field} must be one safe identifier component")
    if value in {".", ".."}:
        raise ValueError(f"{field} cannot be {value!r}")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _finite_nonnegative(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise ValueError(f"{field} must be finite and nonnegative")
    return result


def _contained_path(repo_root: Path, raw: Any, field: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{field} must be a nonempty relative path")
    relative = Path(raw)
    if relative.is_absolute():
        raise ValueError(f"{field} must be relative to the repository")
    resolved = (repo_root / relative).resolve()
    if resolved != repo_root and repo_root not in resolved.parents:
        raise ValueError(f"{field} escapes the repository: {raw!r}")
    return resolved


@dataclass(frozen=True)
class PolicySpec:
    policy_id: str
    mode: str
    batch_size: int
    max_pending_age_s: float | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PolicySpec":
        policy_id = _safe_id(value.get("policy_id"), "policy_id")
        mode = str(value.get("mode", "")).lower()
        if mode not in POLICY_MODES:
            raise ValueError(f"unsupported policy mode: {mode!r}")
        batch_size = _positive_int(value.get("batch_size"), "batch_size")
        timeout_raw = value.get("max_pending_age_s")
        timeout = None if timeout_raw is None else _finite_nonnegative(
            timeout_raw, "max_pending_age_s"
        )
        if mode == "eager" and batch_size != 1:
            raise ValueError("eager policy must use batch_size=1")
        if mode == "bounded" and (timeout is None or timeout <= 0.0):
            raise ValueError("bounded policy requires a positive max_pending_age_s")
        if mode != "bounded" and timeout is not None:
            raise ValueError("only bounded policies may define max_pending_age_s")
        return cls(policy_id, mode, batch_size, timeout)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "mode": self.mode,
            "batch_size": self.batch_size,
            "max_pending_age_s": self.max_pending_age_s,
        }


@dataclass(frozen=True)
class BoardBinding:
    board_id: str
    serial_device: str
    expected_device_uid: str
    expected_build_id: str
    expected_firmware_sha256: str
    expected_module_set_sha256: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BoardBinding":
        board_id = _safe_id(value.get("board_id"), "board_id")
        serial_device = value.get("serial_device")
        if not isinstance(serial_device, str) or not serial_device:
            raise ValueError("serial_device must be a nonempty string")
        uid = _safe_id(value.get("expected_device_uid"), "expected_device_uid")
        build_id = _safe_id(value.get("expected_build_id"), "expected_build_id")
        firmware = str(value.get("expected_firmware_sha256", ""))
        modules = str(value.get("expected_module_set_sha256", ""))
        for digest, field in (
            (firmware, "expected_firmware_sha256"),
            (modules, "expected_module_set_sha256"),
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"{field} must be a lowercase SHA-256 digest")
        return cls(board_id, serial_device, uid, build_id, firmware, modules)

    def to_dict(self) -> dict[str, str]:
        return {
            "board_id": self.board_id,
            "serial_device": self.serial_device,
            "expected_device_uid": self.expected_device_uid,
            "expected_build_id": self.expected_build_id,
            "expected_firmware_sha256": self.expected_firmware_sha256,
            "expected_module_set_sha256": self.expected_module_set_sha256,
        }


@dataclass(frozen=True)
class CausalConfig:
    path: Path
    repo_root: Path
    raw: dict[str, Any]
    config_sha256: str
    campaign_id: str
    stage: str
    output_root: Path
    manifest_root: Path
    manifest_index_sha256: str
    hardware_binding_sha256: str | None
    algorithms: tuple[str, ...]
    loads: tuple[str, ...]
    policies: tuple[PolicySpec, ...]
    trace_limit: int | None
    schedule_seed: int
    boards: tuple[BoardBinding, ...]
    core_affinities: tuple[int, ...]
    development_override: bool
    required_gate_paths: tuple[Path, ...]
    max_technical_retries: int
    device_timeout_seconds: float
    runner_factory: str
    provider_factory: str
    priority_trace_count: int = 0
    excluded_completed_blocks: tuple[str, ...] = ()
    continuation_manifest_path: Path | None = None
    continuation_manifest_sha256: str | None = None


@dataclass(frozen=True)
class CausalJob:
    job_id: str
    block_id: str
    algorithm: str
    load_id: str
    trace_id: str
    policy: PolicySpec
    policy_order_index: int
    board_id: str
    worker_index: int
    core_id: int
    scenario_path: Path
    release_path: Path
    scenario_sha256: str
    release_sha256: str
    runtime_seed: int
    zero_compute: bool = False

    def identity(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "block_id": self.block_id,
            "algorithm": self.algorithm,
            "load_id": self.load_id,
            "trace_id": self.trace_id,
            "policy": self.policy.to_dict(),
            "policy_order_index": self.policy_order_index,
            "board_id": self.board_id,
            "worker_index": self.worker_index,
            "core_id": self.core_id,
            "scenario_sha256": self.scenario_sha256,
            "release_sha256": self.release_sha256,
            "runtime_seed": self.runtime_seed,
            "zero_compute": self.zero_compute,
        }


@dataclass(frozen=True)
class PairedBlock:
    block_id: str
    algorithm: str
    load_id: str
    trace_id: str
    board: BoardBinding
    worker_index: int
    core_id: int
    jobs: tuple[CausalJob, ...]


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON object {path}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _validate_primary_algorithms(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list) or not values:
        raise ValueError("campaign.algorithms must be a nonempty list")
    algorithms = tuple(_safe_id(item, "algorithm") for item in values)
    if len(algorithms) != len(set(algorithms)):
        raise ValueError("campaign.algorithms contains duplicates")
    if set(algorithms) != set(PRIMARY_ALGORITHMS):
        raise ValueError(
            "causal campaign algorithms must be exactly CBAA, ACBBA, PI, and HIPC"
        )
    return algorithms


def load_causal_config(path: Path | str, repo_root: Path | str = ".") -> CausalConfig:
    """Load a causal config and fail closed on unsafe/ambiguous settings."""

    root = Path(repo_root).resolve()
    config_path = Path(path)
    if not config_path.is_absolute():
        config_path = (root / config_path).resolve()
    if config_path != root and root not in config_path.parents:
        raise ValueError("config path must be inside the standalone repository")
    raw = _load_object(config_path)
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported causal config schema_version")
    campaign = raw.get("campaign")
    hardware = raw.get("hardware")
    manifest = raw.get("manifest")
    if not all(isinstance(section, dict) for section in (campaign, hardware, manifest)):
        raise ValueError("config requires campaign, hardware, and manifest objects")
    campaign_id = _safe_id(campaign.get("campaign_id"), "campaign_id")
    stage = _safe_id(campaign.get("stage"), "stage")
    output_root = _contained_path(root, campaign.get("output_root"), "output_root")
    manifest_set_id = _safe_id(manifest.get("manifest_set_id"), "manifest_set_id")
    manifest_parent = _contained_path(root, manifest.get("root"), "manifest.root")
    manifest_root = (manifest_parent / manifest_set_id).resolve()
    if manifest_parent != root and root not in manifest_root.parents:
        raise ValueError("manifest set escapes repository")
    index = validate_manifest_set(manifest_root)
    index_hash = sha256_file(manifest_root / "manifest_index.json")
    generated_loads = index.get("generation", {}).get("arrival_loads", {})
    loads_raw = campaign.get("loads")
    if not isinstance(loads_raw, list) or not loads_raw:
        raise ValueError("campaign.loads must be a nonempty list")
    loads = tuple(_safe_id(item, "load") for item in loads_raw)
    if len(loads) != len(set(loads)) or not set(loads).issubset(generated_loads):
        raise ValueError("campaign.loads must be unique load IDs in the manifest index")
    policies_raw = campaign.get("policies")
    if not isinstance(policies_raw, list) or not policies_raw:
        raise ValueError("campaign.policies must be a nonempty list")
    policies = tuple(PolicySpec.from_mapping(item) for item in policies_raw)
    if len({item.policy_id for item in policies}) != len(policies):
        raise ValueError("policy IDs must be unique")
    trace_limit_raw = campaign.get("trace_limit")
    trace_limit = None if trace_limit_raw is None else _positive_int(trace_limit_raw, "trace_limit")
    if trace_limit is not None and trace_limit > int(index["generation"]["trace_count"]):
        raise ValueError("trace_limit exceeds manifest trace count")
    priority_trace_count = campaign.get("priority_trace_count", 0)
    selected_trace_count = (
        int(index["generation"]["trace_count"])
        if trace_limit is None
        else trace_limit
    )
    if (
        isinstance(priority_trace_count, bool)
        or not isinstance(priority_trace_count, int)
        or priority_trace_count < 0
        or priority_trace_count > selected_trace_count
    ):
        raise ValueError(
            "priority_trace_count must be a nonnegative integer no greater than "
            "the selected trace count"
        )
    schedule_seed = campaign.get("schedule_seed")
    if isinstance(schedule_seed, bool) or not isinstance(schedule_seed, int):
        raise ValueError("campaign.schedule_seed must be an integer")
    algorithms = _validate_primary_algorithms(campaign.get("algorithms"))
    continuation = campaign.get("continuation")
    excluded_completed_blocks: tuple[str, ...] = ()
    continuation_manifest_path: Path | None = None
    continuation_manifest_sha256: str | None = None
    if continuation is not None:
        if not isinstance(continuation, dict):
            raise ValueError("campaign.continuation must be an object")
        excluded_raw = continuation.get("excluded_completed_blocks")
        if not isinstance(excluded_raw, list) or not excluded_raw:
            raise ValueError(
                "campaign.continuation.excluded_completed_blocks must be a "
                "nonempty list"
            )
        excluded_completed_blocks = tuple(
            _safe_id(value, "excluded_completed_block") for value in excluded_raw
        )
        if len(excluded_completed_blocks) != len(set(excluded_completed_blocks)):
            raise ValueError("excluded completed blocks contain duplicates")
        continuation_manifest_path = _contained_path(
            root,
            continuation.get("completion_manifest"),
            "campaign.continuation.completion_manifest",
        )
        declared_manifest_sha = str(
            continuation.get("completion_manifest_sha256", "")
        )
        if re.fullmatch(r"[0-9a-f]{64}", declared_manifest_sha) is None:
            raise ValueError(
                "campaign.continuation.completion_manifest_sha256 must be a "
                "lowercase SHA-256 digest"
            )
        actual_manifest_sha = sha256_file(continuation_manifest_path)
        if actual_manifest_sha != declared_manifest_sha:
            raise ValueError("continuation completion manifest SHA-256 mismatch")
        continuation_manifest_sha256 = actual_manifest_sha
        completion_manifest = _load_object(continuation_manifest_path)
        if completion_manifest.get("report_kind") != (
            "hardware_continuation_predecessor_manifest"
        ):
            raise ValueError("unsupported continuation completion manifest kind")
        prior_root = _contained_path(
            root,
            completion_manifest.get("prior_output_root"),
            "continuation_manifest.prior_output_root",
        )
        completed_rows = completion_manifest.get("completed_jobs")
        if not isinstance(completed_rows, list) or not completed_rows:
            raise ValueError("continuation manifest has no completed jobs")
        completed_by_block: dict[str, set[str]] = {}
        observed_job_ids: set[str] = set()
        for row in completed_rows:
            if not isinstance(row, dict):
                raise ValueError("continuation completed job rows must be objects")
            job_id = _safe_id(row.get("job_id"), "continuation job_id")
            if job_id in observed_job_ids:
                raise ValueError("continuation manifest repeats a completed job")
            observed_job_ids.add(job_id)
            marker_sha = str(row.get("completion_json_sha256", ""))
            if re.fullmatch(r"[0-9a-f]{64}", marker_sha) is None:
                raise ValueError("invalid continuation completion marker SHA-256")
            marker_path = prior_root / "causal" / "completed" / job_id / "completion.json"
            if sha256_file(marker_path) != marker_sha:
                raise ValueError(
                    f"prior completion marker changed or is absent: {job_id}"
                )
            marker = _load_object(marker_path)
            if marker.get("job_id") != job_id:
                raise ValueError("prior completion marker job ID mismatch")
            output_hashes = marker.get("required_output_sha256")
            if not isinstance(output_hashes, dict) or not output_hashes:
                raise ValueError("prior completion marker lacks output hashes")
            completion_dir = marker_path.parent
            for name, expected_output_sha in output_hashes.items():
                if (
                    not isinstance(name, str)
                    or Path(name).name != name
                    or re.fullmatch(r"[0-9a-f]{64}", str(expected_output_sha))
                    is None
                ):
                    raise ValueError("invalid prior required-output hash entry")
                if sha256_file(completion_dir / name) != expected_output_sha:
                    raise ValueError(f"prior completed output changed: {job_id}/{name}")
            try:
                block_id, policy_id = job_id.rsplit("__", 1)
            except ValueError as error:
                raise ValueError(f"invalid prior completed job ID: {job_id}") from error
            completed_by_block.setdefault(block_id, set()).add(policy_id)
        expected_policies = {policy.policy_id for policy in policies}
        if any(value != expected_policies for value in completed_by_block.values()):
            raise ValueError(
                "continuation may exclude only fully completed paired-policy blocks"
            )
        if set(excluded_completed_blocks) != set(completed_by_block):
            raise ValueError(
                "excluded completed blocks differ from the sealed prior results"
            )
        if completion_manifest.get("completed_job_count") != len(observed_job_ids):
            raise ValueError("continuation completed job count mismatch")
    binding_sha256: str | None = None
    declared_binding_sha256 = hardware.get("bindings_file_sha256")
    if declared_binding_sha256 is not None and (
        not isinstance(declared_binding_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", declared_binding_sha256) is None
    ):
        raise ValueError(
            "hardware.bindings_file_sha256 must be a lowercase SHA-256 digest"
        )
    bindings_raw = hardware.get("bindings_file")
    if bindings_raw is not None:
        bindings_path = _contained_path(root, bindings_raw, "hardware.bindings_file")
        bindings_document = _load_object(bindings_path)
        if bindings_document.get("schema_version") != 1:
            raise ValueError("unsupported hardware binding schema")
        board_values = bindings_document.get("boards")
        affinities_raw = bindings_document.get("core_affinities", hardware.get("core_affinities"))
        binding_sha256 = sha256_file(bindings_path)
        if (
            declared_binding_sha256 is not None
            and declared_binding_sha256 != binding_sha256
        ):
            raise ValueError(
                "hardware bindings_file_sha256 differs from the binding file bytes"
            )
        effective_raw = copy.deepcopy(raw)
        effective_raw["hardware"]["boards"] = board_values
        effective_raw["hardware"]["core_affinities"] = affinities_raw
        effective_raw["hardware"]["bindings_file_sha256"] = binding_sha256
        config_sha256 = hashlib.sha256(canonical_json_bytes(effective_raw)).hexdigest()
    else:
        board_values = hardware.get("boards")
        affinities_raw = hardware.get("core_affinities")
        binding_sha256 = declared_binding_sha256
        config_sha256 = hashlib.sha256(canonical_json_bytes(raw)).hexdigest()
    if not isinstance(board_values, list):
        raise ValueError("hardware.boards must be a list")
    boards = tuple(BoardBinding.from_mapping(item) for item in board_values)
    development_override = bool(hardware.get("development_override", False))
    if len(boards) != PUBLICATION_WORKER_COUNT and not development_override:
        raise ValueError(
            "native causal campaigns require exactly "
            f"{PUBLICATION_WORKER_COUNT} board bindings"
        )
    if not development_override and binding_sha256 is None:
        raise ValueError(
            "native causal configs require a sealed hardware binding SHA-256"
        )
    if not boards:
        raise ValueError("at least one board binding is required in development mode")
    if len({item.board_id for item in boards}) != len(boards):
        raise ValueError("board IDs must be unique")
    if len({item.serial_device for item in boards}) != len(boards):
        raise ValueError("serial devices must be unique")
    if len({item.expected_device_uid for item in boards}) != len(boards):
        raise ValueError("expected device UIDs must be unique")
    if not isinstance(affinities_raw, list) or len(affinities_raw) != len(boards):
        raise ValueError("hardware.core_affinities must have one entry per board")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in affinities_raw):
        raise ValueError("hardware.core_affinities must be nonnegative integers")
    affinities = tuple(affinities_raw)
    if len(set(affinities)) != len(affinities):
        raise ValueError("core affinities must be distinct")
    gates_raw = campaign.get("required_gate_paths", [])
    if not isinstance(gates_raw, list):
        raise ValueError("required_gate_paths must be a list")
    gate_paths = tuple(_contained_path(root, item, "required_gate_path") for item in gates_raw)
    retries = campaign.get("max_technical_retries", 1)
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0 or retries > 5:
        raise ValueError("max_technical_retries must be an integer from 0 through 5")
    timeout_seconds = _finite_nonnegative(
        hardware.get("call_timeout_s", 120.0), "hardware.call_timeout_s"
    )
    if timeout_seconds <= 0.0:
        raise ValueError("hardware.call_timeout_s must be positive")
    runner_factory = campaign.get("runner_factory", "study.causal.worker:run_causal_job")
    if not isinstance(runner_factory, str) or ":" not in runner_factory:
        raise ValueError("runner_factory must have module:function form")
    provider_factory = hardware.get(
        "provider_factory",
        "study.causal.worker:create_timing_provider",
    )
    if not isinstance(provider_factory, str) or ":" not in provider_factory:
        raise ValueError("hardware.provider_factory must have module:function form")
    if not development_override:
        if runner_factory != "study.causal.worker:run_causal_job":
            raise ValueError("native scientific configs must use the audited causal job runner")
        if provider_factory != "study.causal.worker:create_timing_provider":
            raise ValueError("native scientific configs must use the audited physical timing provider")
    return CausalConfig(
        path=config_path,
        repo_root=root,
        raw=raw,
        config_sha256=config_sha256,
        campaign_id=campaign_id,
        stage=stage,
        output_root=output_root,
        manifest_root=manifest_root,
        manifest_index_sha256=index_hash,
        hardware_binding_sha256=binding_sha256,
        algorithms=algorithms,
        loads=loads,
        policies=policies,
        trace_limit=trace_limit,
        priority_trace_count=priority_trace_count,
        schedule_seed=schedule_seed,
        boards=boards,
        core_affinities=affinities,
        development_override=development_override,
        required_gate_paths=gate_paths,
        max_technical_retries=retries,
        device_timeout_seconds=timeout_seconds,
        runner_factory=runner_factory,
        provider_factory=provider_factory,
        excluded_completed_blocks=excluded_completed_blocks,
        continuation_manifest_path=continuation_manifest_path,
        continuation_manifest_sha256=continuation_manifest_sha256,
    )


def fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(value))).hexdigest()
