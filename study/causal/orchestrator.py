"""Exactly-three-worker native causal campaign orchestrator."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import multiprocessing
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from study.manifests import canonical_json_bytes, sha256_file

from .freeze import _source_hash
from .gates import GateError, verify_sealed_gate
from .locks import OUTPUT_LOCK_KIND, OUTPUT_LOCK_SCHEMA_VERSION
from .model import (
    PUBLICATION_WORKER_COUNT,
    BoardBinding,
    CausalConfig,
    CausalJob,
    PairedBlock,
    load_causal_config,
)
from .outputs import CausalOutputError, completion_fingerprint, validate_causal_outputs
from .schedule import plan_paired_blocks, validate_schedule


THREAD_ENV = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "BLIS_NUM_THREADS": "1",
}
SCIENTIFIC_STAGES = frozenset({
    "smoke",
    "calibrate_rates",
    "calibrate_timeout",
    "variance",
    "full",
    "publication_core",
    "publication_bounded",
})


class CampaignError(RuntimeError):
    pass


class _WorkerTermination(BaseException):
    """Internal control flow used to unwind a SIGTERM through worker cleanup."""


def _handle_worker_sigterm(signum: int, _frame: Any) -> None:
    raise _WorkerTermination(f"worker received signal {signum}")


def _install_worker_sigterm_handler() -> Any | None:
    """Install cooperative SIGTERM handling in an AGX/POSIX worker process."""

    if os.name != "posix" or not hasattr(signal, "SIGTERM"):
        return None
    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, _handle_worker_sigterm)
    return previous


def _block_worker_sigterm() -> Any | None:
    """Defer SIGTERM across a short resource-acquisition ownership handoff."""

    if os.name != "posix" or not hasattr(signal, "pthread_sigmask"):
        return None
    return signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})


def _restore_worker_signal_mask(previous_mask: Any | None) -> None:
    if previous_mask is not None:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _atomic_json(path: Path, value: Any) -> None:
    data = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _write_immutable(path: Path, value: Any) -> None:
    data = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise CampaignError(f"refusing to replace different immutable data: {path}")
        return
    path.write_bytes(data)


def _import_callable(spec: str) -> Callable[..., Any]:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise CampaignError(f"invalid callable specification: {spec!r}")
    value = getattr(importlib.import_module(module_name), attribute)
    if not callable(value):
        raise CampaignError(f"configured object is not callable: {spec!r}")
    return value


def _git_identity(repo_root: Path) -> dict[str, Any]:
    head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    status = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode != 0 or status.returncode != 0:
        raise CampaignError("campaign requires a valid Git checkout")
    ignored_prefixes = (
        "study/output/", "study/generated/", "study/frozen/", "results/",
        "artifacts/native_causal/",
    )
    relevant_dirty = []
    for line in status.stdout.splitlines():
        path = line[3:].replace("\\", "/") if len(line) >= 4 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if not path.startswith(ignored_prefixes):
            relevant_dirty.append(line)
    return {
        "git_head": head.stdout.strip(),
        "source_tree_sha256": _source_hash(repo_root),
        "relevant_dirty": bool(relevant_dirty),
        "relevant_status_porcelain": relevant_dirty,
        "ignored_generated_prefixes": list(ignored_prefixes),
    }


def _validate_full_freeze(config: CausalConfig, source: Mapping[str, Any]) -> dict[str, Any]:
    raw_path = config.raw["campaign"].get("design_freeze_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise GateError("full/zero campaign config lacks design_freeze_path")
    path = (config.repo_root / raw_path).resolve()
    if path != config.repo_root and config.repo_root not in path.parents:
        raise GateError("design freeze path escapes repository")
    try:
        freeze = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GateError(f"cannot read design freeze: {error}") from error
    if not isinstance(freeze, dict) or freeze.get("passed") is not True:
        raise GateError("design freeze is absent or not passing")
    if freeze.get("full_config_sha256") != config.config_sha256:
        raise GateError("current full config bytes do not match the frozen config")
    if freeze.get("manifest_index_sha256") != config.manifest_index_sha256:
        raise GateError("current manifest index does not match the frozen design")
    if freeze.get("hardware_binding_sha256") != config.hardware_binding_sha256:
        raise GateError("current hardware binding does not match the frozen design")
    if freeze.get("code_commit") != source["git_head"]:
        raise GateError("Git HEAD differs from the frozen design")
    if freeze.get("source_tree_sha256") != source["source_tree_sha256"]:
        raise GateError("source tree differs from the frozen design")
    sealed_gates = freeze.get("sealed_gates", {})
    from .freeze import REQUIRED_GATE_KINDS
    if not isinstance(sealed_gates, dict) or set(sealed_gates) != set(REQUIRED_GATE_KINDS):
        raise GateError("design freeze does not seal exactly the required native gates")
    for kind, entry in sealed_gates.items():
        if not isinstance(entry, dict) or entry.get("kind") != kind:
            raise GateError(f"sealed gate kind mismatch: {kind}")
        verify_sealed_gate(entry, config.repo_root)
    expected_builds = freeze.get("device_build_id_by_board", {})
    expected_modules = freeze.get("device_module_set_sha256_by_board", {})
    expected_firmware = freeze.get("device_firmware_sha256_by_board", {})
    expected_uids = freeze.get("device_uid_by_board", {})
    for board in config.boards:
        if expected_builds.get(board.board_id) != board.expected_build_id:
            raise GateError(f"board build binding differs from freeze: {board.board_id}")
        if expected_modules.get(board.board_id) != board.expected_module_set_sha256:
            raise GateError(f"board module-set binding differs from freeze: {board.board_id}")
        if expected_firmware.get(board.board_id) != board.expected_firmware_sha256:
            raise GateError(f"board firmware binding differs from freeze: {board.board_id}")
        if expected_uids.get(board.board_id) != board.expected_device_uid:
            raise GateError(f"board UID binding differs from freeze: {board.board_id}")
    return freeze


def validate_campaign_gates(config: CausalConfig, source: Mapping[str, Any], zero_compute: bool) -> dict[str, Any] | None:
    if config.stage in SCIENTIFIC_STAGES and config.development_override:
        raise GateError("scientific stages cannot use development_override")
    if (
        config.stage in SCIENTIFIC_STAGES
        and len(config.boards) != PUBLICATION_WORKER_COUNT
    ):
        raise GateError(
            "scientific stages require exactly "
            f"{PUBLICATION_WORKER_COUNT} boards/workers"
        )
    logical_cores = os.cpu_count() or 1
    if (
        config.stage in SCIENTIFIC_STAGES
        and PUBLICATION_WORKER_COUNT > math.floor(0.75 * logical_cores)
    ):
        raise GateError(
            "publication workers would exceed the 75%-of-logical-cores safety cap"
        )
    if config.raw["campaign"].get("require_clean_source", config.stage == "full") and source["relevant_dirty"]:
        raise GateError("relevant source tree is dirty; commit or explicitly use a development config")
    for path in config.required_gate_paths:
        from .gates import validate_gate
        gate = validate_gate(path)
        report = gate.report
        kind = report.get("report_kind")
        if kind == "native_environment_check":
            if (
                report.get("git", {}).get("head") != source["git_head"]
                or report.get("source_tree_sha256") != source["source_tree_sha256"]
                or report.get("hardware_binding_sha256")
                != config.hardware_binding_sha256
            ):
                raise GateError("environment gate belongs to another source/board cohort")
        elif kind == "rp2040_parity_preflight":
            if report.get("repository_commit") != source["git_head"]:
                raise GateError("preflight gate belongs to another Git commit")
            by_uid = {
                str(row.get("device_id")): row
                for row in report.get("boards", [])
                if isinstance(row, dict)
            }
            for board in config.boards:
                row = by_uid.get(board.expected_device_uid)
                if row is None or any((
                    row.get("build_id") != board.expected_build_id,
                    row.get("firmware_sha256") != board.expected_firmware_sha256,
                    row.get("module_set_sha256")
                    != board.expected_module_set_sha256,
                )):
                    raise GateError(
                        f"preflight board cohort differs: {board.board_id}"
                    )
        elif isinstance(report.get("input_identity"), dict):
            identity = report["input_identity"]
            if (
                identity.get("git_head") != source["git_head"]
                or identity.get("source_tree_sha256")
                != source["source_tree_sha256"]
                or identity.get("hardware_binding_sha256")
                != config.hardware_binding_sha256
            ):
                raise GateError(f"{kind} gate belongs to another source/board cohort")
    if config.stage == "full" or (zero_compute and config.stage != "development"):
        return _validate_full_freeze(config, source)
    return None


def _completion_state(
    config: CausalConfig,
    job: CausalJob,
    source: Mapping[str, Any],
) -> str:
    directory = config.output_root / ("zero_compute" if job.zero_compute else "causal") / "completed" / job.job_id
    if not directory.exists():
        return "pending"
    marker = directory / "completion.json"
    if not marker.is_file():
        return "conflict"
    try:
        completion = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "conflict"
    expected = completion_fingerprint(config, job, source)
    if completion.get("completion_fingerprint") != expected:
        return "conflict"
    try:
        validation = validate_causal_outputs(config, job, directory)
    except CausalOutputError:
        return "conflict"
    if validation["required_output_sha256"] != completion.get("required_output_sha256"):
        return "conflict"
    return "completed"


def _set_affinity(core_id: int, *, required: bool) -> dict[str, Any]:
    supported = hasattr(os, "sched_setaffinity") and hasattr(os, "sched_getaffinity")
    if not supported:
        if required:
            raise CampaignError("CPU affinity is required but unavailable")
        return {"supported": False, "requested_core": core_id, "effective_cores": None}
    try:
        os.sched_setaffinity(0, {core_id})
        effective = sorted(os.sched_getaffinity(0))
    except OSError as error:
        if required:
            raise CampaignError(f"could not pin worker to core {core_id}: {error}") from error
        return {"supported": True, "requested_core": core_id, "effective_cores": None, "error": str(error)}
    if required and effective != [core_id]:
        raise CampaignError(f"worker affinity mismatch: expected [{core_id}], got {effective}")
    return {"supported": True, "requested_core": core_id, "effective_cores": effective}


def _claim_board_lock(root: Path, board: BoardBinding, worker_index: int) -> tuple[int, Path]:
    lock_path = root / "board_locks" / f"{board.board_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise CampaignError(
            f"board lock already exists for {board.board_id}: {lock_path}; "
            "inspect the prior worker before removing a genuinely stale lock"
        ) from error
    try:
        payload = canonical_json_bytes({
            "schema_version": OUTPUT_LOCK_SCHEMA_VERSION,
            "lock_kind": OUTPUT_LOCK_KIND,
            "board_id": board.board_id,
            "expected_device_uid": board.expected_device_uid,
            "serial_device": board.serial_device,
            "worker_index": worker_index,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "claimed_at": utc_now(),
        })
        os.write(descriptor, payload)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return descriptor, lock_path


def _release_board_lock(descriptor: int, path: Path) -> None:
    os.close(descriptor)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _next_attempt(root: Path, job: CausalJob) -> Path:
    parent = root / ("zero_compute" if job.zero_compute else "causal") / "attempts" / job.job_id
    parent.mkdir(parents=True, exist_ok=True)
    existing = [path for path in parent.iterdir() if path.is_dir() and path.name.startswith("attempt_")]
    number = len(existing) + 1
    path = parent / f"attempt_{number:04d}"
    path.mkdir()
    return path


def _promote(
    config: CausalConfig,
    job: CausalJob,
    source: Mapping[str, Any],
    attempt: Path,
    validation: Mapping[str, Any],
) -> Path:
    completed = config.output_root / ("zero_compute" if job.zero_compute else "causal") / "completed" / job.job_id
    if completed.exists():
        raise CampaignError(f"completed path already exists: {completed}")
    marker = {
        "schema_version": 1,
        "completed_at": utc_now(),
        "job_id": job.job_id,
        "completion_fingerprint": completion_fingerprint(config, job, source),
        "required_output_sha256": validation["required_output_sha256"],
        "semantic_validation": dict(validation),
        "source_identity": dict(source),
    }
    _atomic_json(attempt / "completion.json", marker)
    completed.parent.mkdir(parents=True, exist_ok=True)
    os.replace(attempt, completed)
    return completed


def _worker_entry(
    config_path: str,
    repo_root: str,
    worker_index: int,
    blocks: list[PairedBlock],
    source: dict[str, Any],
    result_queue: Any,
    zero_compute: bool,
) -> None:
    config = load_causal_config(config_path, repo_root)
    board = config.boards[worker_index]
    for name, value in THREAD_ENV.items():
        os.environ[name] = value
    # The parent sets this before spawn; setting it here records/reinforces the
    # contract but cannot retroactively change this interpreter's hash secret.
    os.environ["PYTHONHASHSEED"] = "0"
    required_affinity = sys.platform.startswith("linux") and not config.development_override
    provider = None
    descriptor = -1
    lock_path: Path | None = None
    previous_sigterm_handler = _install_worker_sigterm_handler()
    try:
        affinity = _set_affinity(config.core_affinities[worker_index], required=required_affinity)
        previous_mask = _block_worker_sigterm()
        try:
            descriptor, lock_path = _claim_board_lock(config.output_root, board, worker_index)
        finally:
            _restore_worker_signal_mask(previous_mask)
        provider_factory = _import_callable(config.provider_factory)
        runner = _import_callable(config.runner_factory)
        previous_mask = _block_worker_sigterm()
        try:
            provider = provider_factory(
                config=config, board=board, zero_compute=zero_compute
            )
        finally:
            _restore_worker_signal_mask(previous_mask)
        if hasattr(provider, "open"):
            provider.open()
        result_queue.put({
            "type": "worker_ready",
            "worker_index": worker_index,
            "pid": os.getpid(),
            "board_id": board.board_id,
            "serial_device": board.serial_device,
            "core_id": config.core_affinities[worker_index],
            "affinity": affinity,
            "thread_environment": dict(THREAD_ENV),
            "python_hash_seed": os.environ.get("PYTHONHASHSEED"),
        })
        for block in blocks:
            if block.worker_index != worker_index or block.board.board_id != board.board_id:
                raise CampaignError("worker received a block bound to another board")
            for job in block.jobs:
                state = _completion_state(config, job, source)
                if state == "completed":
                    result_queue.put({"type": "job", "status": "skipped_completed", "job_id": job.job_id, "worker_index": worker_index})
                    continue
                if state == "conflict":
                    result_queue.put({"type": "job", "status": "conflict", "job_id": job.job_id, "worker_index": worker_index})
                    continue
                success = False
                for retry_index in range(config.max_technical_retries + 1):
                    attempt = _next_attempt(config.output_root, job)
                    metadata = {
                        "worker_index": worker_index,
                        "worker_pid": os.getpid(),
                        "core_id": config.core_affinities[worker_index],
                        "board_id": board.board_id,
                        "serial_device": board.serial_device,
                        "retry_index": retry_index,
                        "hidden_library_threads": 1,
                        "python_hash_seed": os.environ.get("PYTHONHASHSEED"),
                        "affinity": affinity,
                        "git_head": source["git_head"],
                        "source_tree_sha256": source["source_tree_sha256"],
                        "relevant_source_dirty": source["relevant_dirty"],
                    }
                    _atomic_json(attempt / "job.json", job.identity())
                    _atomic_json(attempt / "worker_binding.json", metadata)
                    try:
                        runner(
                            config=config,
                            job=job,
                            attempt_dir=attempt,
                            timing_provider=provider,
                            worker_metadata=metadata,
                        )
                        validation = validate_causal_outputs(config, job, attempt)
                        destination = _promote(config, job, source, attempt, validation)
                        result_queue.put({
                            "type": "job", "status": "completed", "job_id": job.job_id,
                            "worker_index": worker_index, "retry_index": retry_index,
                            "completed_dir": str(destination),
                        })
                        success = True
                        break
                    except _WorkerTermination:
                        raise
                    except BaseException as error:
                        failure = {
                            "schema_version": 1,
                            "failed_at": utc_now(),
                            "failure_class": "technical",
                            "error_type": type(error).__name__,
                            "message": str(error),
                            "traceback": traceback.format_exc(),
                            "job": job.identity(),
                            "worker": metadata,
                            "retry_index": retry_index,
                        }
                        diagnostics = getattr(error, "diagnostics", None)
                        if isinstance(diagnostics, Mapping):
                            failure["diagnostics"] = dict(diagnostics)
                        cleanup_failure = getattr(error, "cleanup_failure", None)
                        if isinstance(cleanup_failure, str):
                            failure["cleanup_failure"] = cleanup_failure
                        _atomic_json(attempt / "failure.json", failure)
                        result_queue.put({
                            "type": "attempt_failure", "status": "technical_failure",
                            "job_id": job.job_id, "worker_index": worker_index,
                            "retry_index": retry_index, "attempt_dir": str(attempt),
                            "message": str(error),
                        })
                        if retry_index < config.max_technical_retries:
                            # A failed hardware group consumes its IDs and may
                            # invalidate the session. Reopen a new, strictly
                            # identity/build-validated session for the retry;
                            # never continue a partially failed mission.
                            if provider is not None and hasattr(provider, "close"):
                                provider.close()
                            previous_mask = _block_worker_sigterm()
                            try:
                                provider = provider_factory(
                                    config=config, board=board,
                                    zero_compute=zero_compute,
                                )
                            finally:
                                _restore_worker_signal_mask(previous_mask)
                            if hasattr(provider, "open"):
                                provider.open()
                if not success:
                    result_queue.put({"type": "job", "status": "failed", "job_id": job.job_id, "worker_index": worker_index})
        result_queue.put({"type": "worker_done", "worker_index": worker_index, "pid": os.getpid()})
    except _WorkerTermination as error:
        result_queue.put({
            "type": "worker_terminated", "worker_index": worker_index,
            "pid": os.getpid(), "message": str(error),
        })
    except BaseException as error:
        result_queue.put({
            "type": "worker_fatal", "worker_index": worker_index, "pid": os.getpid(),
            "error_type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc(),
        })
        raise
    finally:
        try:
            if provider is not None and hasattr(provider, "close"):
                try:
                    provider.close()
                except Exception:
                    pass
        finally:
            try:
                if descriptor >= 0 and lock_path is not None:
                    _release_board_lock(descriptor, lock_path)
            finally:
                if previous_sigterm_handler is not None:
                    signal.signal(signal.SIGTERM, previous_sigterm_handler)


def _partition(blocks: Iterable[PairedBlock], worker_count: int) -> list[list[PairedBlock]]:
    partitions: list[list[PairedBlock]] = [[] for _ in range(worker_count)]
    for block in blocks:
        partitions[block.worker_index].append(block)
    return partitions


def _shutdown_processes(
    processes: Iterable[multiprocessing.Process],
    *,
    request_termination: bool,
    grace_seconds: float,
) -> None:
    """Join workers against one deadline, escalating only after SIGTERM grace."""

    started = [process for process in processes if process.pid is not None]
    if request_termination:
        for process in started:
            if process.is_alive():
                process.terminate()
    deadline = time.monotonic() + grace_seconds
    for process in started:
        process.join(timeout=max(0.0, deadline - time.monotonic()))
    for process in started:
        if process.is_alive():
            process.kill()
            process.join(timeout=5.0)


def _record_event(path: Path, event: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(event) | {"recorded_at": utc_now()}, sort_keys=True, ensure_ascii=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")


def _build_schedule_record(
    config: CausalConfig,
    blocks: list[PairedBlock],
    schedule_summary: Mapping[str, Any],
    source: Mapping[str, Any],
    *,
    selected_workers: int,
    zero_compute: bool,
) -> dict[str, Any]:
    """Build the canonical immutable schedule artifact used by every validator."""

    return {
        "schema_version": 1,
        "campaign_id": config.campaign_id,
        "config_sha256": config.config_sha256,
        "manifest_index_sha256": config.manifest_index_sha256,
        "hardware_binding_sha256": config.hardware_binding_sha256,
        "source_identity": dict(source),
        "board_bindings": [
            board.to_dict() for board in config.boards[:selected_workers]
        ],
        "core_affinities": list(config.core_affinities[:selected_workers]),
        "zero_compute": zero_compute,
        "summary": dict(schedule_summary),
        "blocks": [
            {
                "block_id": block.block_id,
                "board_id": block.board.board_id,
                "worker_index": block.worker_index,
                "core_id": block.core_id,
                "policy_order": [job.policy.policy_id for job in block.jobs],
                "jobs": [job.identity() for job in block.jobs],
            }
            for block in blocks
        ],
    }


def run_campaign(
    config: CausalConfig,
    *,
    zero_compute: bool = False,
    dry_run: bool = False,
    development_worker_limit: int | None = None,
) -> dict[str, Any]:
    invocation_id = (
        f"invocation_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
        f"_{os.getpid()}"
    )
    source = _git_identity(config.repo_root)
    freeze = validate_campaign_gates(config, source, zero_compute)
    blocks = plan_paired_blocks(config, zero_compute=zero_compute)
    schedule_summary = validate_schedule(config, blocks, zero_compute=zero_compute)
    if development_worker_limit is not None:
        if not config.development_override:
            raise CampaignError("worker limit override is development-only")
        if development_worker_limit <= 0 or development_worker_limit > len(config.boards):
            raise CampaignError("invalid development worker limit")
        selected_workers = development_worker_limit
        blocks = [block for block in blocks if block.worker_index < selected_workers]
    else:
        selected_workers = len(config.boards)
    if (
        not config.development_override
        and selected_workers != PUBLICATION_WORKER_COUNT
    ):
        raise CampaignError(
            "native causal campaign must launch exactly "
            f"{PUBLICATION_WORKER_COUNT} workers"
        )
    config.output_root.mkdir(parents=True, exist_ok=True)
    schedule_path = config.output_root / ("zero_compute_schedule.json" if zero_compute else "causal_schedule.json")
    schedule_record = _build_schedule_record(
        config,
        blocks,
        schedule_summary,
        source,
        selected_workers=selected_workers,
        zero_compute=zero_compute,
    )
    _write_immutable(schedule_path, schedule_record)
    provenance = {
        "schema_version": 1,
        "report_kind": "causal_campaign_provenance",
        "created_at": utc_now(),
        "campaign_id": config.campaign_id,
        "stage": config.stage,
        "zero_compute": zero_compute,
        "config_path": str(config.path),
        "config_sha256": config.config_sha256,
        "manifest_index_sha256": config.manifest_index_sha256,
        "source_identity": source,
        "design_freeze_sha256": None if freeze is None else sha256_file(
            config.repo_root / config.raw["campaign"]["design_freeze_path"]
        ),
        "schedule_sha256": schedule_summary["schedule_sha256"],
        "worker_count": selected_workers,
        "required_publication_workers": PUBLICATION_WORKER_COUNT,
        "exact_publication_worker_count": (
            selected_workers == PUBLICATION_WORKER_COUNT
        ),
        "board_bindings": [board.to_dict() for board in config.boards[:selected_workers]],
        "core_affinities": list(config.core_affinities[:selected_workers]),
        "thread_environment": THREAD_ENV,
        "python_hash_seed_for_spawned_workers": "0",
        "host": {"platform": sys.platform, "python": sys.version, "logical_cores": os.cpu_count()},
        "dry_run": dry_run,
    }
    provenance_dir = config.output_root / "provenance"
    provenance_path = provenance_dir / f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    _write_immutable(provenance_path, provenance)
    states = {
        job.job_id: _completion_state(config, job, source)
        for block in blocks for job in block.jobs
    }
    if dry_run:
        return {
            "campaign_id": config.campaign_id,
            "dry_run": True,
            "worker_count": selected_workers,
            "schedule": str(schedule_path),
            "provenance": str(provenance_path),
            "job_states": states,
        }
    partitions = _partition(blocks, len(config.boards))[:selected_workers]
    # PYTHONHASHSEED must exist before process.start(): Python initializes its
    # hash secret while each spawned interpreter starts, not inside the target.
    previous_hash_seed = os.environ.get("PYTHONHASHSEED")
    os.environ["PYTHONHASHSEED"] = "0"
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    processes: list[multiprocessing.Process] = []
    try:
        for worker_index in range(selected_workers):
            process = context.Process(
                name=f"causal-board-worker-{worker_index}",
                target=_worker_entry,
                args=(
                    str(config.path), str(config.repo_root), worker_index,
                    partitions[worker_index], dict(source), result_queue, zero_compute,
                ),
            )
            processes.append(process)
            process.start()
    except BaseException:
        _shutdown_processes(
            processes,
            request_termination=True,
            grace_seconds=max(10.0, config.device_timeout_seconds + 5.0),
        )
        raise
    finally:
        if previous_hash_seed is None:
            os.environ.pop("PYTHONHASHSEED", None)
        else:
            os.environ["PYTHONHASHSEED"] = previous_hash_seed
    events_path = config.output_root / ("zero_compute_events.jsonl" if zero_compute else "campaign_events.jsonl")
    events: list[dict[str, Any]] = []
    interrupted = False
    try:
        while any(process.is_alive() for process in processes):
            try:
                event = result_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            event = dict(event) | {"invocation_id": invocation_id}
            events.append(event)
            _record_event(events_path, event)
        while True:
            try:
                event = result_queue.get_nowait()
            except queue.Empty:
                break
            event = dict(event) | {"invocation_id": invocation_id}
            events.append(event)
            _record_event(events_path, event)
    except BaseException:
        interrupted = True
        raise
    finally:
        # All workers receive SIGTERM together.  Give the Python handler enough
        # time to emerge from one configured serial-call timeout and execute
        # provider/lease/output-lock cleanup, using one shared deadline rather
        # than multiplying the grace period by the worker count.
        grace_seconds = (
            max(10.0, config.device_timeout_seconds + 5.0)
            if interrupted else 10.0
        )
        _shutdown_processes(
            processes,
            request_termination=interrupted,
            grace_seconds=grace_seconds,
        )
    fatal = [event for event in events if event.get("type") == "worker_fatal"]
    bad_exit = [process for process in processes if process.exitcode != 0]
    jobs = [job for block in blocks for job in block.jobs]
    final_states = {job.job_id: _completion_state(config, job, source) for job in jobs}
    algorithmic_outcomes: dict[str, bool] = {}
    result_kind = "zero_compute" if zero_compute else "causal"
    for job in jobs:
        if final_states[job.job_id] != "completed":
            continue
        summary_path = (
            config.output_root / result_kind / "completed" / job.job_id
            / "trial_summary.json"
        )
        try:
            summary_value = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise CampaignError(
                f"promoted summary became unreadable: {summary_path}: {error}"
            ) from error
        algorithmic_outcomes[job.job_id] = bool(
            summary_value.get("all_tasks_completed") is True
        )
    attempt_root = config.output_root / result_kind / "attempts"
    retained_failure_paths = sorted(attempt_root.glob("*/attempt_*/failure.json"))
    retained_failures: list[dict[str, Any]] = []
    for failure_path in retained_failure_paths:
        try:
            retained_failures.append(json.loads(failure_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as error:
            raise CampaignError(f"retained failure record is unreadable: {failure_path}: {error}") from error
    report = {
        "schema_version": 1,
        "report_kind": "causal_campaign_execution",
        "generated_at": utc_now(),
        "campaign_id": config.campaign_id,
        "invocation_id": invocation_id,
        "zero_compute": zero_compute,
        "worker_count": selected_workers,
        "worker_pids": [process.pid for process in processes],
        "worker_exitcodes": [process.exitcode for process in processes],
        "planned_jobs": len(jobs),
        "completed_jobs": sum(value == "completed" for value in final_states.values()),
        "conflicting_jobs": sum(value == "conflict" for value in final_states.values()),
        "pending_jobs": sum(value == "pending" for value in final_states.values()),
        "algorithmically_completed_jobs": sum(algorithmic_outcomes.values()),
        "algorithmically_incomplete_jobs": sum(
            not value for value in algorithmic_outcomes.values()
        ),
        "algorithmic_incompletions_are_retained": True,
        "technical_attempt_failures_this_invocation": sum(
            event.get("type") == "attempt_failure" for event in events
        ),
        "technical_attempt_failures": len(retained_failures),
        "retained_failure_records": [str(path) for path in retained_failure_paths],
        "worker_fatal_events": fatal,
        "passed": not interrupted and not fatal and not bad_exit and all(value == "completed" for value in final_states.values()),
        "hardware_validated": not zero_compute and not config.development_override,
        "job_states": final_states,
        "schedule_sha256": schedule_summary["schedule_sha256"],
        "config_sha256": config.config_sha256,
        "manifest_index_sha256": config.manifest_index_sha256,
        "hardware_binding_sha256": config.hardware_binding_sha256,
        "source_identity": source,
        "board_bindings": [
            board.to_dict() for board in config.boards[:selected_workers]
        ],
        "core_affinities": list(config.core_affinities[:selected_workers]),
    }
    _atomic_json(config.output_root / ("zero_compute_execution_report.json" if zero_compute else "campaign_execution_report.json"), report)
    if not report["passed"]:
        raise CampaignError(
            f"causal campaign did not complete cleanly: completed={report['completed_jobs']}/{report['planned_jobs']}, "
            f"fatal_workers={len(fatal)}, bad_exits={len(bad_exit)}"
        )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--zero-compute", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--development-worker-limit", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_causal_config(args.config, args.repo_root)
    result = run_campaign(
        config,
        zero_compute=args.zero_compute,
        dry_run=args.dry_run,
        development_worker_limit=args.development_worker_limit,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
