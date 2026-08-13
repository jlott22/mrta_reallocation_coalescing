"""Configuration-driven, resumable campaign launcher.

Each job runs in a fresh attempt directory.  A successful attempt is validated
and atomically moved under ``completed/``; completed outputs are never replaced.
The default child-process contract is documented in ``study/README.md``.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import csv
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import platform
import re
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from study.manifests import (
    DEFAULT_MANIFEST_ROOT,
    canonical_json_bytes,
    generate_manifest_set,
    load_config,
    sha256_file,
    validate_manifest_set,
)


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

ALLOWED_TRIGGER_REASONS = {
    "task_arrival_eager",
    "batch_threshold",
    "age_timeout",
    "final_release_flush",
    "initial_allocation",
    "robot_idle",
    "task_completion",
    "invalid_goal",
    "consensus/internal",
    "consensus",
    "internal",
    "stalled_recovery",
    "other",
}

ARRIVAL_TRIGGER_REASONS = {
    "task_arrival_eager", "batch_threshold", "age_timeout", "final_release_flush",
}

REQUIRED_SUMMARY_INTEGER_METRICS = {
    "max_robot_steps",
    "total_team_steps",
    "allocator_call_count",
    "allocation_epoch_count",
    "arrival_induced_trigger_count",
    "mandatory_trigger_count",
    "max_pending_queue_depth",
}

REQUIRED_SUMMARY_FLOAT_METRICS = {
    "movement_time_s",
    "simulated_execution_time_s",
    "cumulative_allocator_time_s",
    "host_program_runtime_s",
    "mean_allocator_call_time_s",
    "median_allocator_call_time_s",
    "p95_allocator_call_time_s",
    "max_allocator_call_time_s",
    "allocator_time_per_completed_task_s",
    "mean_release_to_first_assignment_latency_s",
    "median_release_to_first_assignment_latency_s",
    "p95_release_to_first_assignment_latency_s",
    "mean_release_to_completion_latency_s",
    "median_release_to_completion_latency_s",
    "p95_release_to_completion_latency_s",
    "mean_pending_queue_depth",
    "mean_pending_age_s",
    "max_pending_age_s",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def worker_cap(logical_cores: int | None = None) -> int:
    """Return the exact floor(75%-of-logical-cores) process cap."""

    cores = logical_cores if logical_cores is not None else (os.cpu_count() or 1)
    if isinstance(cores, bool) or not isinstance(cores, int) or cores <= 0:
        raise ValueError("logical core count must be a positive integer")
    return math.floor(0.75 * cores)


def effective_workers(requested: int | None, logical_cores: int | None = None) -> int:
    cap = worker_cap(logical_cores)
    if cap < 1:
        raise RuntimeError("campaign execution requires at least two detected logical cores")
    if requested is None:
        return cap
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        raise ValueError("requested workers must be a positive integer")
    return min(requested, cap)


def _safe_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ValueError(f"{name} must match {SAFE_ID.pattern}")
    return value


def _git_metadata(
    repo_root: Path, ignored_dirty_roots: Iterable[Path | str] = ()
) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args], cwd=repo_root, text=True, capture_output=True,
                check=True, timeout=15,
            )
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None

    status = run("status", "--porcelain=v1", "--untracked-files=all")
    ignored = tuple(
        str(Path(root)).replace("\\", "/").strip("./") + "/"
        for root in ignored_dirty_roots
        if str(root).strip("./\\")
    )
    status_lines = None if status is None else status.splitlines()
    relevant_status: list[str] | None = status_lines
    if status_lines is not None and ignored:
        relevant_status = []
        for line in status_lines:
            path_text = line[3:].replace("\\", "/") if len(line) >= 4 else line
            if " -> " in path_text:
                path_text = path_text.split(" -> ", 1)[1]
            path_text = path_text.strip('"')
            if any(path_text == root[:-1] or path_text.startswith(root) for root in ignored):
                continue
            relevant_status.append(line)
    return {
        "commit": run("rev-parse", "HEAD"),
        "branch": run("branch", "--show-current"),
        "describe": run("describe", "--always", "--dirty", "--tags"),
        "dirty": None if relevant_status is None else bool(relevant_status),
        "status_porcelain": relevant_status,
        "ignored_generated_status_roots": list(ignored),
    }


def _source_files(repo_root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    transient_study_roots = {
        "__pycache__",
        "frozen",
        "generated",
        "native_device_leases",
        "native_gates",
        "output",
    }
    for source_root_name in ("known_visit_sim", "study"):
        source_root = repo_root / source_root_name
        if not source_root.is_dir():
            continue
        source_paths: list[Path] = []
        for directory, names, filenames in os.walk(source_root, topdown=True):
            names[:] = sorted(
                name
                for name in names
                if name != "__pycache__"
                and not (
                    source_root_name == "study"
                    and Path(directory) == source_root
                    and name in transient_study_roots
                )
            )
            source_paths.extend(
                Path(directory) / filename
                for filename in sorted(filenames)
                if filename.endswith(".py")
            )
        for path in sorted(source_paths):
            relative = path.relative_to(repo_root)
            files[relative.as_posix()] = sha256_file(path)
    return files


def _source_tree_hash(repo_root: Path) -> str:
    files = _source_files(repo_root)
    material = {
        "identity_schema_version": 1,
        "files": files,
    }
    return hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def _source_identity(
    repo_root: Path, ignored_dirty_roots: Iterable[Path | str] = ()
) -> dict[str, Any]:
    """Fingerprint executable study source, including dirty/untracked files.

    HEAD alone is insufficient for development pilots.  The content tree hash
    is deterministic and deliberately excludes generated manifests/results.
    """

    git = _git_metadata(repo_root, ignored_dirty_roots)
    files = _source_files(repo_root)
    material = {"identity_schema_version": 1, "files": files}
    return {
        "identity_schema_version": 1,
        "git_head": git["commit"],
        "git_branch": git["branch"],
        "git_describe": git["describe"],
        "git_dirty": git["dirty"],
        "git_status_porcelain": git["status_porcelain"],
        "ignored_generated_status_roots": git["ignored_generated_status_roots"],
        "source_tree_sha256": hashlib.sha256(canonical_json_bytes(material)).hexdigest(),
        "source_files_sha256": files,
    }


class ProcessController:
    """Track child process groups and terminate them on campaign cancellation."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[Any]] = set()

    def register(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            self._processes.add(process)
            cancelled = self.cancelled.is_set()
        if cancelled:
            self._terminate(process)

    def unregister(self, process: subprocess.Popen[Any]) -> None:
        with self._lock:
            self._processes.discard(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[Any], force: bool = False) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif force:
                process.kill()
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            pass

    def cancel_all(self, grace_s: float = 2.0) -> None:
        self.cancelled.set()
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            self._terminate(process)
        deadline = time.monotonic() + max(0.0, grace_s)
        while any(process.poll() is None for process in processes) and time.monotonic() < deadline:
            time.sleep(0.02)
        for process in processes:
            self._terminate(process, force=True)

    @property
    def active_count(self) -> int:
        with self._lock:
            return sum(process.poll() is None for process in self._processes)


def _machine_metadata() -> dict[str, Any]:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "logical_cores": os.cpu_count(),
    }


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(canonical_json_bytes(value))
    os.replace(temporary, path)


@dataclasses.dataclass(frozen=True)
class CampaignJob:
    job_id: str
    condition_id: str
    trace_id: str
    algorithm: str
    load_id: str
    policy_id: str
    policy_mode: str
    batch_size: int
    max_pending_age_s: float | None
    runtime_seed: int
    scenario_path: Path
    release_path: Path
    scenario_sha256: str
    release_sha256: str
    git_head: str | None
    source_tree_sha256: str
    python_hash_seed: int
    fingerprint: str


@dataclasses.dataclass(frozen=True)
class JobResult:
    job_id: str
    status: str
    elapsed_s: float = 0.0
    message: str = ""


Executor = Callable[[CampaignJob, list[str], Path], int]


class OutputValidationError(ValueError):
    """Raised when a zero-exit runner produced scientifically invalid output."""


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise OutputValidationError(f"invalid JSON object {path.name}: {error}") from error
    if not isinstance(value, dict) or not value:
        raise OutputValidationError(f"{path.name} must be a nonempty JSON object")
    return value


def _read_nonempty_csv(path: Path, required_fields: set[str]) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fields = list(reader.fieldnames or [])
            rows = list(reader)
    except (OSError, csv.Error) as error:
        raise OutputValidationError(f"invalid CSV {path.name}: {error}") from error
    missing = required_fields - set(fields)
    if missing:
        raise OutputValidationError(f"{path.name} missing columns: {sorted(missing)}")
    if not rows:
        raise OutputValidationError(f"{path.name} must contain at least one data row")
    if any(None in row for row in rows):
        raise OutputValidationError(f"{path.name} contains rows wider than its header")
    return fields, rows


def _finite_number(value: Any, field: str, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OutputValidationError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise OutputValidationError(f"{field} must be finite and nonnegative")
    return number


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise OutputValidationError(f"{field} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        result = int(value)
    else:
        raise OutputValidationError(f"{field} must be an integer")
    if result < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise OutputValidationError(f"{field} must be {qualifier}")
    return result


def _csv_number(value: Any, field: str, *, optional: bool = False) -> float | None:
    if value is None or str(value).strip() == "":
        if optional:
            return None
        raise OutputValidationError(f"{field} cannot be empty")
    try:
        number = float(str(value).strip())
    except ValueError as error:
        raise OutputValidationError(f"{field} must be numeric") from error
    if not math.isfinite(number) or number < 0:
        raise OutputValidationError(f"{field} must be finite and nonnegative")
    return number


def _csv_bool(value: Any, field: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"true", "1"}:
        return True
    if normalized in {"false", "0"}:
        return False
    raise OutputValidationError(f"{field} must be a serialized boolean")


def _close(actual: float, expected: float, field: str, tolerance: float = 1e-8) -> None:
    if not math.isclose(actual, expected, rel_tol=tolerance, abs_tol=tolerance):
        raise OutputValidationError(f"{field} arithmetic mismatch: {actual} != {expected}")


def _serialized_list(value: str, field: str) -> list[Any]:
    if value.strip() == "":
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError) as error:
            raise OutputValidationError(f"{field} must serialize a list") from error
    if not isinstance(parsed, list):
        raise OutputValidationError(f"{field} must serialize a list")
    return parsed


def _nearest_rank(values: list[float], proportion: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(proportion * len(ordered)) - 1)]


def _validate_summary_dimensions(summary: dict[str, Any], job: CampaignJob) -> None:
    expected = {
        "trial_id": job.trace_id,
        "condition_id": job.condition_id,
        "algorithm": job.algorithm,
        "arrival_load": job.load_id,
        "policy_id": job.policy_id,
        "policy_mode": job.policy_mode,
        "batch_size": job.batch_size,
        "runtime_seed": job.runtime_seed,
        "scenario_sha256": job.scenario_sha256,
        "release_sha256": job.release_sha256,
        "python_hash_seed": job.python_hash_seed,
    }
    for field, expected_value in expected.items():
        if field not in summary:
            raise OutputValidationError(f"trial_summary.json missing dimension/hash {field}")
        actual = summary[field]
        if isinstance(expected_value, int):
            if isinstance(actual, bool) or not isinstance(actual, int) or actual != expected_value:
                raise OutputValidationError(f"trial summary {field} does not match job")
        elif actual != expected_value:
            raise OutputValidationError(f"trial summary {field} does not match job")
    policy_wait = summary.get("policy_max_pending_age_s")
    if job.max_pending_age_s is None:
        if policy_wait is not None:
            raise OutputValidationError("count/eager policy_max_pending_age_s must be null")
    else:
        _close(
            _finite_number(policy_wait, "policy_max_pending_age_s"),
            float(job.max_pending_age_s),
            "policy_max_pending_age_s",
        )


def _validate_task_output(
    path: Path,
    job: CampaignJob,
    release: dict[str, Any],
    *,
    observation_horizon_s: float | None = None,
) -> dict[str, Any]:
    required = {
        "trial_id", "task_id", "task_index", "task_x", "task_y", "state",
        "release_time_s", "admission_time_s", "first_assignment_time_s",
        "first_assigned_robot", "completion_time_s", "completing_robot",
        "assignment_events", "release_to_admission_latency_s",
        "release_to_first_assignment_latency_s", "admission_to_first_assignment_latency_s",
        "release_to_completion_latency_s", "assignment_to_completion_latency_s",
    }
    _, rows = _read_nonempty_csv(path, required)
    expected_tasks = {
        str(task["task_id"]): (index, task)
        for index, task in enumerate(release["tasks"], start=1)
    }
    if len(rows) != len(expected_tasks):
        raise OutputValidationError(
            f"task_events.csv row count {len(rows)} != manifest task count {len(expected_tasks)}"
        )
    actual_ids = [str(row["task_id"]) for row in rows]
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected_tasks):
        raise OutputValidationError("task_events.csv task IDs do not exactly match the release manifest")
    completed_count = 0
    assignment_latencies: list[float] = []
    completion_latencies: list[float] = []
    admitted_ids: set[str] = set()
    allowed_states = {"unreleased", "released", "pending", "admitted", "assigned", "completed"}
    ordered_rows = sorted(rows, key=lambda row: _integer(row["task_index"], "task_index", positive=True))
    for index, row in enumerate(ordered_rows, start=1):
        task_id = str(row["task_id"])
        expected_index, expected = expected_tasks[task_id]
        if str(row["trial_id"]) != job.trace_id:
            raise OutputValidationError(f"task {task_id} trial_id does not match job")
        row_index = _integer(row["task_index"], f"{task_id}.task_index", positive=True)
        if row_index != index or row_index != expected_index:
            raise OutputValidationError("task indices must be unique, contiguous, and one-based")
        if _integer(row["task_x"], f"{task_id}.task_x") != int(expected["x"]):
            raise OutputValidationError(f"task {task_id} x coordinate differs from manifest")
        if _integer(row["task_y"], f"{task_id}.task_y") != int(expected["y"]):
            raise OutputValidationError(f"task {task_id} y coordinate differs from manifest")
        state = str(row["state"]).strip().lower()
        if state not in allowed_states:
            raise OutputValidationError(f"task {task_id} has invalid state {state!r}")
        release_s = _csv_number(
            row["release_time_s"], f"{task_id}.release_time_s", optional=True
        )
        scheduled_release_s = float(expected["release_time_s"])
        if release_s is None:
            if state != "unreleased":
                raise OutputValidationError(
                    f"task {task_id} lacks a release timestamp but is not unreleased"
                )
            if (
                observation_horizon_s is None
                or scheduled_release_s <= observation_horizon_s + 1e-9
            ):
                raise OutputValidationError(
                    f"task {task_id} was due by the observation horizon but lacks a release timestamp"
                )
        else:
            if state == "unreleased":
                raise OutputValidationError(
                    f"task {task_id} has a release timestamp while marked unreleased"
                )
            _close(release_s, scheduled_release_s, f"{task_id}.release_time_s")
        admission_s = _csv_number(
            row["admission_time_s"], f"{task_id}.admission_time_s", optional=True
        )
        assignment_s = _csv_number(
            row["first_assignment_time_s"], f"{task_id}.first_assignment_time_s", optional=True
        )
        completion_s = _csv_number(
            row["completion_time_s"], f"{task_id}.completion_time_s", optional=True
        )
        if admission_s is not None:
            admitted_ids.add(task_id)
            assert release_s is not None
            if admission_s + 1e-9 < release_s:
                raise OutputValidationError(f"task {task_id} admitted before release")
        if assignment_s is not None:
            assert release_s is not None
            if admission_s is None or assignment_s + 1e-9 < admission_s:
                raise OutputValidationError(f"task {task_id} assigned before admission")
            assignment_latencies.append(assignment_s - release_s)
        if completion_s is not None:
            assert release_s is not None
            if assignment_s is None or completion_s + 1e-9 < assignment_s:
                raise OutputValidationError(f"task {task_id} completed before assignment")
            completion_latencies.append(completion_s - release_s)
        expected_presence = {
            "unreleased": (False, False, False),
            "released": (False, False, False),
            "pending": (False, False, False),
            "admitted": (True, False, False),
            "assigned": (True, True, False),
            "completed": (True, True, True),
        }[state]
        actual_presence = (admission_s is not None, assignment_s is not None, completion_s is not None)
        if actual_presence != expected_presence:
            raise OutputValidationError(f"task {task_id} timestamps conflict with state {state}")
        assignment_events = _integer(row["assignment_events"], f"{task_id}.assignment_events")
        if (assignment_s is None and assignment_events != 0) or (
            assignment_s is not None and assignment_events < 1
        ):
            raise OutputValidationError(f"task {task_id} assignment_events conflicts with timestamps")
        first_robot = str(row["first_assigned_robot"]).strip()
        completing_robot = str(row["completing_robot"]).strip()
        if bool(first_robot) != (assignment_s is not None):
            raise OutputValidationError(f"task {task_id} first_assigned_robot is inconsistent")
        if bool(completing_robot) != (completion_s is not None):
            raise OutputValidationError(f"task {task_id} completing_robot is inconsistent")
        derived = {
            "release_to_admission_latency_s": (
                None if admission_s is None or release_s is None else admission_s - release_s
            ),
            "release_to_first_assignment_latency_s": (
                None if assignment_s is None or release_s is None else assignment_s - release_s
            ),
            "admission_to_first_assignment_latency_s": (
                None if admission_s is None or assignment_s is None else assignment_s - admission_s
            ),
            "release_to_completion_latency_s": (
                None if completion_s is None or release_s is None else completion_s - release_s
            ),
            "assignment_to_completion_latency_s": (
                None if assignment_s is None or completion_s is None else completion_s - assignment_s
            ),
        }
        for field, expected_value in derived.items():
            actual_value = _csv_number(row[field], f"{task_id}.{field}", optional=True)
            if expected_value is None:
                if actual_value is not None:
                    raise OutputValidationError(f"task {task_id} {field} must be empty")
            else:
                assert actual_value is not None
                _close(actual_value, expected_value, f"{task_id}.{field}")
        if state == "completed":
            completed_count += 1
    return {
        "task_count": len(rows),
        "completed_count": completed_count,
        "all_tasks_completed": completed_count == len(rows),
        "admitted_ids": admitted_ids,
        "assignment_latencies": assignment_latencies,
        "completion_latencies": completion_latencies,
    }


def _validate_epoch_output(
    path: Path, summary: dict[str, Any], admitted_task_ids: set[str]
) -> dict[str, Any]:
    required = {
        "epoch_id", "opened_time_s", "trigger_reason", "mandatory", "admitted_count",
        "allocator_time_s",
    }
    fields, rows = _read_nonempty_csv(path, required)
    epoch_ids: set[int] = set()
    reasons: list[str] = []
    mandatory_count = 0
    admitted_from_epochs: set[str] = set()
    allocator_call_ids: set[int] = set()
    allocator_time_s = 0.0
    for row in rows:
        epoch_id = _integer(row["epoch_id"], "epoch_id", positive=True)
        if epoch_id in epoch_ids:
            raise OutputValidationError(f"duplicate epoch_id {epoch_id}")
        epoch_ids.add(epoch_id)
        _csv_number(row["opened_time_s"], f"epoch_{epoch_id}.opened_time_s")
        reason = str(row["trigger_reason"]).strip()
        if reason not in ALLOWED_TRIGGER_REASONS:
            raise OutputValidationError(f"epoch {epoch_id} has unknown trigger reason {reason!r}")
        reasons.append(reason)
        mandatory_count += int(_csv_bool(row["mandatory"], f"epoch_{epoch_id}.mandatory"))
        admitted_count = _integer(row["admitted_count"], f"epoch_{epoch_id}.admitted_count")
        allocator_value = _csv_number(row["allocator_time_s"], f"epoch_{epoch_id}.allocator_time_s")
        assert allocator_value is not None
        allocator_time_s += allocator_value
        if "admitted_task_ids" in fields:
            admitted = [str(value) for value in _serialized_list(
                row["admitted_task_ids"], f"epoch_{epoch_id}.admitted_task_ids"
            )]
            if len(admitted) != admitted_count or len(admitted) != len(set(admitted)):
                raise OutputValidationError(f"epoch {epoch_id} admitted count/list mismatch")
            if admitted_from_epochs.intersection(admitted):
                raise OutputValidationError("a task appears admitted in more than one epoch")
            admitted_from_epochs.update(admitted)
        if "allocator_call_ids" in fields:
            calls = [
                _integer(value, f"epoch_{epoch_id}.allocator_call_ids", positive=True)
                for value in _serialized_list(row["allocator_call_ids"], f"epoch_{epoch_id}.allocator_call_ids")
            ]
            if len(calls) != len(set(calls)) or allocator_call_ids.intersection(calls):
                raise OutputValidationError("allocator call IDs must be globally unique across epochs")
            allocator_call_ids.update(calls)
        if "closed_time_s" in fields:
            closed_s = _csv_number(
                row["closed_time_s"], f"epoch_{epoch_id}.closed_time_s", optional=True
            )
            opened_s = _csv_number(row["opened_time_s"], f"epoch_{epoch_id}.opened_time_s")
            if closed_s is not None and opened_s is not None and closed_s + 1e-9 < opened_s:
                raise OutputValidationError(f"epoch {epoch_id} closes before it opens")
    if epoch_ids != set(range(1, len(rows) + 1)):
        raise OutputValidationError("epoch IDs must be contiguous and one-based")
    if "admitted_task_ids" in fields and admitted_from_epochs != admitted_task_ids:
        raise OutputValidationError("epoch admitted task IDs disagree with task lifecycle rows")
    expected_epochs = _integer(summary["allocation_epoch_count"], "allocation_epoch_count")
    if len(rows) != expected_epochs:
        raise OutputValidationError("allocation_epoch_count disagrees with allocation_epochs.csv")
    expected_mandatory = _integer(summary["mandatory_trigger_count"], "mandatory_trigger_count")
    if mandatory_count != expected_mandatory:
        raise OutputValidationError("mandatory_trigger_count disagrees with epoch rows")
    expected_arrival = _integer(
        summary["arrival_induced_trigger_count"], "arrival_induced_trigger_count"
    )
    if sum(reason in ARRIVAL_TRIGGER_REASONS for reason in reasons) != expected_arrival:
        raise OutputValidationError("arrival_induced_trigger_count disagrees with epoch rows")
    reason_counts = summary.get("trigger_reason_counts")
    actual_reason_counts = {reason: reasons.count(reason) for reason in sorted(set(reasons))}
    if not isinstance(reason_counts, dict) or reason_counts != actual_reason_counts:
        raise OutputValidationError("trigger_reason_counts disagrees with epoch rows")
    if "allocator_call_ids" in fields:
        expected_calls = _integer(summary["allocator_call_count"], "allocator_call_count")
        if len(allocator_call_ids) != expected_calls:
            raise OutputValidationError("allocator_call_count disagrees with epoch call IDs")
    _close(
        allocator_time_s,
        _finite_number(summary["cumulative_allocator_time_s"], "cumulative_allocator_time_s"),
        "cumulative_allocator_time_s",
    )
    return {"epoch_count": len(rows)}


def validate_job_outputs(job: CampaignJob, directory: Path) -> dict[str, Any]:
    """Validate output contents before promotion or resumable reuse."""

    summary = _read_json_object(directory / "trial_summary.json")
    _validate_summary_dimensions(summary, job)
    completed_value = summary.get("all_tasks_completed")
    if not isinstance(completed_value, bool):
        raise OutputValidationError("all_tasks_completed must be boolean")
    for field in REQUIRED_SUMMARY_INTEGER_METRICS:
        if field not in summary:
            raise OutputValidationError(f"trial_summary.json missing metric {field}")
        _integer(summary[field], field)
    for field in REQUIRED_SUMMARY_FLOAT_METRICS:
        if field not in summary:
            raise OutputValidationError(f"trial_summary.json missing metric {field}")
        _finite_number(summary[field], field)
    if _integer(summary["max_robot_steps"], "max_robot_steps") > _integer(
        summary["total_team_steps"], "total_team_steps"
    ):
        raise OutputValidationError("max_robot_steps cannot exceed total_team_steps")
    movement = _finite_number(summary["movement_time_s"], "movement_time_s")
    simulated = _finite_number(summary["simulated_execution_time_s"], "simulated_execution_time_s")
    allocator = _finite_number(
        summary["cumulative_allocator_time_s"], "cumulative_allocator_time_s"
    )
    allocator_parallel = _finite_number(
        summary.get("allocator_parallel_critical_path_time_s", allocator),
        "allocator_parallel_critical_path_time_s",
    )
    if allocator_parallel > allocator + 1e-9:
        raise OutputValidationError(
            "allocator parallel critical path cannot exceed cumulative allocator time"
        )
    if "mission_elapsed_time_s" not in summary:
        raise OutputValidationError(
            "trial_summary.json missing metric mission_elapsed_time_s"
        )
    mission_value = summary["mission_elapsed_time_s"]
    mission: float | None
    if completed_value:
        mission = _finite_number(mission_value, "mission_elapsed_time_s")
    elif summary.get("causal_timing_enabled") is True:
        if mission_value is not None:
            raise OutputValidationError(
                "causal algorithmic noncompletion requires null mission_elapsed_time_s"
            )
        expected_outcome = {
            "technical_status": "completed",
            "trial_status": "algorithmic_incomplete",
            "algorithmic_status": "incomplete",
        }
        for field, expected in expected_outcome.items():
            if summary.get(field) != expected:
                raise OutputValidationError(
                    f"causal algorithmic noncompletion requires {field}={expected!r}"
                )
        failure_type = summary.get("algorithmic_failure_type")
        if failure_type not in {
            "stagnation_horizon", "event_horizon", "event_queue_exhausted"
        }:
            raise OutputValidationError(
                "causal algorithmic noncompletion has an invalid failure type"
            )
        if summary.get("failure_type") != failure_type:
            raise OutputValidationError(
                "causal algorithmic noncompletion failure aliases disagree"
            )
        mission = None
    else:
        mission = _finite_number(mission_value, "mission_elapsed_time_s")
    if summary.get("causal_timing_enabled") is True:
        # In the causal scheduler, movement and per-robot allocation intervals
        # overlap on the event clock.  ``movement_time_s`` is processor-style
        # summed movement work, while mission elapsed time is the timestamp of
        # final task completion.  The legacy additive identity is therefore
        # neither defined nor scientifically valid for schema-v2 causal rows.
        if mission is not None:
            _close(mission, simulated, "mission_elapsed_time_s")
    else:
        if "other_execution_time_s" not in summary:
            raise OutputValidationError(
                "legacy trial_summary.json missing metric other_execution_time_s"
            )
        other = _finite_number(summary["other_execution_time_s"], "other_execution_time_s")
        _close(simulated, movement + other, "simulated_execution_time_s")
        assert mission is not None
        _close(mission, simulated + allocator_parallel, "mission_elapsed_time_s")
    if summary.get("causal_timing_enabled") is not True and "mission_elapsed_time_serial_compute_s" in summary:
        _close(
            _finite_number(
                summary["mission_elapsed_time_serial_compute_s"],
                "mission_elapsed_time_serial_compute_s",
            ),
            simulated + allocator,
            "mission_elapsed_time_serial_compute_s",
        )
    release = _read_json_object(job.release_path)
    task_metrics = _validate_task_output(
        directory / "task_events.csv",
        job,
        release,
        observation_horizon_s=(
            simulated
            if not completed_value and summary.get("causal_timing_enabled") is True
            else None
        ),
    )
    if completed_value != task_metrics["all_tasks_completed"]:
        raise OutputValidationError("all_tasks_completed disagrees with task lifecycle rows")
    assignments = task_metrics["assignment_latencies"]
    completions = task_metrics["completion_latencies"]
    expected_latency_metrics = {
        "mean_release_to_first_assignment_latency_s": statistics.fmean(assignments) if assignments else 0.0,
        "median_release_to_first_assignment_latency_s": statistics.median(assignments) if assignments else 0.0,
        "p95_release_to_first_assignment_latency_s": _nearest_rank(assignments, 0.95),
        "mean_release_to_completion_latency_s": statistics.fmean(completions) if completions else 0.0,
        "median_release_to_completion_latency_s": statistics.median(completions) if completions else 0.0,
        "p95_release_to_completion_latency_s": _nearest_rank(completions, 0.95),
    }
    for field, expected in expected_latency_metrics.items():
        _close(_finite_number(summary[field], field), expected, field)
    completed_count = int(task_metrics["completed_count"])
    expected_per_task = allocator / completed_count if completed_count else 0.0
    _close(
        _finite_number(
            summary["allocator_time_per_completed_task_s"],
            "allocator_time_per_completed_task_s",
        ),
        expected_per_task,
        "allocator_time_per_completed_task_s",
    )
    epoch_metrics = _validate_epoch_output(
        directory / "allocation_epochs.csv", summary, task_metrics["admitted_ids"]
    )
    return {
        "summary_schema_valid": True,
        "task_count": task_metrics["task_count"],
        "completed_task_count": completed_count,
        "epoch_count": epoch_metrics["epoch_count"],
        "all_tasks_completed": completed_value,
        "algorithmic_status": "completed" if completed_value else "incomplete",
        "algorithmic_failure_type": (
            None if completed_value else summary.get("algorithmic_failure_type")
        ),
    }


def _default_executor(job: CampaignJob, command: list[str], attempt_dir: Path,
                      repo_root: Path, controller: ProcessController | None = None) -> int:
    with (attempt_dir / "stdout.log").open("w", encoding="utf-8") as stdout, \
            (attempt_dir / "stderr.log").open("w", encoding="utf-8") as stderr:
        # Run from the clean-clone repository root so the local known_visit_sim
        # package is importable without requiring an editable installation.
        environment = os.environ.copy()
        # A campaign worker is one simulation process. Prevent numerical
        # libraries from quietly multiplying threads behind the process cap.
        for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            environment[variable] = "1"
        environment["PYTHONHASHSEED"] = str(job.python_hash_seed)
        popen_options: dict[str, Any] = {}
        if os.name == "posix":
            popen_options["start_new_session"] = True
        elif os.name == "nt":
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(
            command, cwd=repo_root, stdout=stdout, stderr=stderr, text=True,
            env=environment, **popen_options,
        )
        if controller is not None:
            controller.register(process)
        try:
            return process.wait()
        finally:
            if controller is not None:
                controller.unregister(process)


class CampaignOrchestrator:
    def __init__(
        self,
        config_path: Path | str,
        repo_root: Path | str = ".",
        requested_workers: int | None = None,
        logical_cores: int | None = None,
        executor: Executor | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.config_path = Path(config_path).resolve()
        self.config = load_config(self.config_path)
        self.campaign = self._validate_campaign(self.config.get("campaign"))
        self.source_identity = _source_identity(
            self.repo_root,
            (
                self.campaign["output_root"],
                self.config.get("manifest", {}).get("root", DEFAULT_MANIFEST_ROOT),
            ),
        )
        self.process_controller = ProcessController()
        configured_workers = self.campaign.get("max_workers")
        requested = requested_workers if requested_workers is not None else configured_workers
        self.logical_cores = logical_cores if logical_cores is not None else (os.cpu_count() or 1)
        self.max_workers = effective_workers(requested, self.logical_cores)
        self.hard_worker_cap = worker_cap(self.logical_cores)
        self.executor = executor or (
            lambda job, command, attempt_dir: _default_executor(
                job, command, attempt_dir, self.repo_root, self.process_controller
            )
        )
        self.log_lock = threading.Lock()
        output_setting = Path(self.campaign["output_root"])
        if output_setting.is_absolute():
            raise ValueError("campaign.output_root must be relative to the repository")
        self.output_root = (self.repo_root / output_setting).resolve()
        if self.repo_root not in self.output_root.parents:
            raise ValueError("campaign.output_root escapes the repository")
        manifest_settings = self.config["manifest"]
        manifest_root = Path(manifest_settings.get("root", DEFAULT_MANIFEST_ROOT))
        if manifest_root.is_absolute():
            raise ValueError("manifest.root must be relative to the repository")
        manifest_base = (self.repo_root / manifest_root).resolve()
        if manifest_base != self.repo_root and self.repo_root not in manifest_base.parents:
            raise ValueError("manifest.root escapes the repository")
        manifest_set_id = _safe_id(manifest_settings.get("manifest_set_id"), "manifest_set_id")
        self.manifest_root = (manifest_base / manifest_set_id).resolve()
        if self.repo_root not in self.manifest_root.parents:
            raise ValueError("resolved manifest set escapes the repository")
        self.required_outputs = tuple(self.campaign["required_outputs"])
        schedule_seed = self.campaign.get("schedule_seed")
        if schedule_seed is None:
            schedule_material = (
                f"{self.campaign['campaign_id']}:{manifest_set_id}:"
                f"{manifest_settings.get('master_seed')}"
            ).encode("utf-8")
            schedule_seed = int.from_bytes(hashlib.sha256(schedule_material).digest()[:8], "big")
        if isinstance(schedule_seed, bool) or not isinstance(schedule_seed, int):
            raise ValueError("campaign.schedule_seed must be an integer")
        self.schedule_seed = schedule_seed

    @staticmethod
    def _validate_campaign(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("config must contain a campaign object")
        campaign = dict(value)
        for key in ("campaign_id", "output_root", "algorithms", "loads", "policies", "required_outputs"):
            if key not in campaign:
                raise ValueError(f"campaign config missing: {key}")
        _safe_id(campaign["campaign_id"], "campaign_id")
        if not isinstance(campaign["algorithms"], list) or not campaign["algorithms"]:
            raise ValueError("campaign.algorithms must be non-empty")
        for algorithm in campaign["algorithms"]:
            _safe_id(algorithm, "algorithm")
        if len(set(campaign["algorithms"])) != len(campaign["algorithms"]):
            raise ValueError("campaign.algorithms contains duplicates")
        if not isinstance(campaign["loads"], list) or not campaign["loads"]:
            raise ValueError("campaign.loads must be non-empty")
        for load_id in campaign["loads"]:
            _safe_id(load_id, "load ID")
        if not isinstance(campaign["policies"], list) or not campaign["policies"]:
            raise ValueError("campaign.policies must be non-empty")
        policy_ids: set[str] = set()
        for policy in campaign["policies"]:
            if not isinstance(policy, dict):
                raise ValueError("each policy must be an object")
            policy_id = _safe_id(policy.get("policy_id"), "policy_id")
            if policy_id in policy_ids:
                raise ValueError(f"duplicate policy_id: {policy_id}")
            policy_ids.add(policy_id)
            if policy.get("mode") not in {"eager", "count", "bounded"}:
                raise ValueError(f"unsupported policy mode: {policy.get('mode')!r}")
            batch_size = policy.get("batch_size")
            if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
                raise ValueError("policy batch_size must be a positive integer")
            timeout = policy.get("max_pending_age_s")
            if policy["mode"] == "eager" and batch_size != 1:
                raise ValueError("eager policy requires batch_size=1")
            if policy["mode"] == "bounded":
                if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
                    raise ValueError("bounded policy requires positive max_pending_age_s")
            elif timeout is not None:
                raise ValueError("only bounded policy accepts max_pending_age_s")
        required = campaign["required_outputs"]
        if not isinstance(required, list) or not required:
            raise ValueError("campaign.required_outputs must be a non-empty list")
        for relative in required:
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
                raise ValueError(f"unsafe required output path: {relative!r}")
        runner = campaign.get("runner", {})
        if not isinstance(runner, dict):
            raise ValueError("campaign.runner must be an object")
        _safe_id(runner.get("module", "known_visit_sim.run_online_trials").replace(".", "_"), "runner module")
        if "allow_dirty" in campaign and not isinstance(campaign["allow_dirty"], bool):
            raise ValueError("campaign.allow_dirty must be boolean")
        exclusions = campaign.get("job_exclusions", [])
        if not isinstance(exclusions, list):
            raise ValueError("campaign.job_exclusions must be a list")
        exclusion_fields = {"algorithms", "loads", "policies", "trace_ids"}
        for rule_index, rule in enumerate(exclusions):
            if not isinstance(rule, dict) or not rule:
                raise ValueError(
                    f"campaign.job_exclusions[{rule_index}] must be a nonempty object"
                )
            unknown = set(rule) - exclusion_fields
            if unknown:
                raise ValueError(
                    "campaign.job_exclusions contains unknown fields: "
                    + ", ".join(sorted(unknown))
                )
            for field, entries in rule.items():
                if not isinstance(entries, list) or not entries:
                    raise ValueError(
                        f"campaign.job_exclusions[{rule_index}].{field} "
                        "must be a nonempty list"
                    )
                values = [_safe_id(entry, f"job exclusion {field}") for entry in entries]
                if len(values) != len(set(values)):
                    raise ValueError(
                        f"campaign.job_exclusions[{rule_index}].{field} contains duplicates"
                    )
        expected_excluded = campaign.get("expected_excluded_job_count")
        if exclusions and expected_excluded is None:
            raise ValueError(
                "campaign.expected_excluded_job_count is required with job_exclusions"
            )
        if expected_excluded is not None and (
            isinstance(expected_excluded, bool)
            or not isinstance(expected_excluded, int)
            or expected_excluded < 0
        ):
            raise ValueError(
                "campaign.expected_excluded_job_count must be a nonnegative integer"
            )
        allowlist = campaign.get("job_allowlist")
        if allowlist is not None:
            if not isinstance(allowlist, list) or not allowlist:
                raise ValueError("campaign.job_allowlist must be a nonempty list")
            allowlist_values = [
                _safe_id(job_id, "job allowlist entry") for job_id in allowlist
            ]
            if len(allowlist_values) != len(set(allowlist_values)):
                raise ValueError("campaign.job_allowlist contains duplicates")
            expected_selected = campaign.get("expected_selected_job_count")
            if (
                isinstance(expected_selected, bool)
                or not isinstance(expected_selected, int)
                or expected_selected <= 0
            ):
                raise ValueError(
                    "campaign.expected_selected_job_count must be a positive integer "
                    "with job_allowlist"
                )
            if expected_selected != len(allowlist_values):
                raise ValueError(
                    "campaign.expected_selected_job_count differs from job_allowlist length"
                )
        elif "expected_selected_job_count" in campaign:
            raise ValueError(
                "campaign.expected_selected_job_count requires job_allowlist"
            )
        return campaign

    def prepare_manifests(self) -> Path:
        generated = generate_manifest_set(self.config, self.repo_root)
        validate_manifest_set(generated)
        return generated

    def _manifest_entries(self) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
        index = validate_manifest_set(self.manifest_root)
        scenarios = {
            entry["trace_id"]: entry
            for entry in index["entries"]
            if entry["manifest_kind"] == "scenario"
        }
        releases = [
            entry for entry in index["entries"]
            if entry["manifest_kind"] == "release_trace" and entry["load_id"] in self.campaign["loads"]
        ]
        missing_loads = set(self.campaign["loads"]) - {entry["load_id"] for entry in releases}
        if missing_loads:
            raise ValueError(f"campaign loads absent from manifests: {sorted(missing_loads)}")
        trace_limit = self.campaign.get("trace_limit")
        if trace_limit is not None:
            if isinstance(trace_limit, bool) or not isinstance(trace_limit, int) or trace_limit <= 0:
                raise ValueError("campaign.trace_limit must be a positive integer")
            selected_trace_ids = sorted(scenarios)[:trace_limit]
            scenarios = {key: scenarios[key] for key in selected_trace_ids}
            releases = [entry for entry in releases if entry["trace_id"] in scenarios]
        return scenarios, releases

    def plan_jobs(self) -> list[CampaignJob]:
        scenarios, releases = self._manifest_entries()
        policies = sorted(self.campaign["policies"], key=lambda policy: policy["policy_id"])
        jobs: list[CampaignJob] = []
        for release_entry in sorted(releases, key=lambda entry: (entry["load_id"], entry["trace_id"])):
            scenario_entry = scenarios[release_entry["trace_id"]]
            release_path = self.manifest_root / release_entry["path"]
            release = json.loads(release_path.read_text(encoding="utf-8"))
            for algorithm in sorted(self.campaign["algorithms"]):
                for policy in policies:
                    condition_id = f"{algorithm}__{release_entry['load_id']}__{policy['policy_id']}"
                    job_id = f"{condition_id}__{release_entry['trace_id']}"
                    fingerprint_value = {
                        "schema_version": 2,
                        "algorithm": algorithm,
                        "load_id": release_entry["load_id"],
                        "policy": policy,
                        "trace_id": release_entry["trace_id"],
                        "runtime_seed": release["runtime_seed"],
                        "scenario_sha256": scenario_entry["sha256"],
                        "release_sha256": release_entry["sha256"],
                        "runner": self.campaign.get("runner", {}),
                        "required_outputs": self.required_outputs,
                        "git_head": self.source_identity["git_head"],
                        "source_tree_sha256": self.source_identity["source_tree_sha256"],
                        "python_hash_seed": int(release["runtime_seed"]) % (2 ** 32),
                    }
                    fingerprint = hashlib.sha256(canonical_json_bytes(fingerprint_value)).hexdigest()
                    jobs.append(CampaignJob(
                        job_id=job_id,
                        condition_id=condition_id,
                        trace_id=release_entry["trace_id"],
                        algorithm=algorithm,
                        load_id=release_entry["load_id"],
                        policy_id=policy["policy_id"],
                        policy_mode=policy["mode"],
                        batch_size=policy["batch_size"],
                        max_pending_age_s=policy.get("max_pending_age_s"),
                        runtime_seed=release["runtime_seed"],
                        scenario_path=self.manifest_root / scenario_entry["path"],
                        release_path=release_path,
                        scenario_sha256=scenario_entry["sha256"],
                        release_sha256=release_entry["sha256"],
                        git_head=self.source_identity["git_head"],
                        source_tree_sha256=self.source_identity["source_tree_sha256"],
                        python_hash_seed=int(release["runtime_seed"]) % (2 ** 32),
                        fingerprint=fingerprint,
                    ))
        exclusions = self.campaign.get("job_exclusions", [])
        excluded_job_ids: set[str] = set()
        field_attributes = {
            "algorithms": "algorithm",
            "loads": "load_id",
            "policies": "policy_id",
            "trace_ids": "trace_id",
        }
        for rule_index, rule in enumerate(exclusions):
            matched = {
                job.job_id
                for job in jobs
                if all(
                    getattr(job, field_attributes[field]) in entries
                    for field, entries in rule.items()
                )
            }
            if not matched:
                raise ValueError(
                    f"campaign.job_exclusions[{rule_index}] matched no planned jobs"
                )
            excluded_job_ids.update(matched)
        expected_excluded = self.campaign.get("expected_excluded_job_count")
        if expected_excluded is not None and len(excluded_job_ids) != expected_excluded:
            raise ValueError(
                "campaign job exclusion count mismatch: "
                f"expected {expected_excluded}, found {len(excluded_job_ids)}"
            )
        if excluded_job_ids:
            jobs = [job for job in jobs if job.job_id not in excluded_job_ids]
        allowlist = self.campaign.get("job_allowlist")
        if allowlist is not None:
            requested = set(allowlist)
            available = {job.job_id for job in jobs}
            missing = sorted(requested - available)
            if missing:
                raise ValueError(
                    "campaign.job_allowlist contains jobs absent after matrix exclusions: "
                    + ", ".join(missing)
                )
            jobs = [job for job in jobs if job.job_id in requested]
            expected_selected = self.campaign["expected_selected_job_count"]
            if len(jobs) != expected_selected:
                raise ValueError(
                    "campaign selected job count mismatch: "
                    f"expected {expected_selected}, found {len(jobs)}"
                )
        if not jobs:
            raise ValueError("campaign expands to zero jobs")
        return self._balanced_schedule(jobs)

    def _balanced_schedule(self, jobs: list[CampaignJob]) -> list[CampaignJob]:
        """Deterministically interleave conditions to reduce contention bias."""

        by_condition: dict[str, list[CampaignJob]] = {}
        for job in jobs:
            by_condition.setdefault(job.condition_id, []).append(job)
        for condition_id, condition_jobs in by_condition.items():
            condition_jobs.sort(
                key=lambda job: hashlib.sha256(
                    f"{self.schedule_seed}:{condition_id}:{job.job_id}".encode()
                ).digest()
            )
        schedule: list[CampaignJob] = []
        round_index = 0
        while any(by_condition.values()):
            active_conditions = [
                condition_id for condition_id in sorted(by_condition) if by_condition[condition_id]
            ]
            active_conditions.sort(
                key=lambda condition_id: hashlib.sha256(
                    f"{self.schedule_seed}:{round_index}:{condition_id}".encode()
                ).digest()
            )
            for condition_id in active_conditions:
                schedule.append(by_condition[condition_id].pop(0))
            round_index += 1
        return schedule

    def command_for(self, job: CampaignJob, attempt_dir: Path) -> list[str]:
        runner = self.campaign.get("runner", {})
        python_executable = str(runner.get("python_executable", sys.executable))
        module = str(runner.get("module", "known_visit_sim.run_online_trials"))
        command = [
            python_executable, "-m", module,
            "--scenario-manifest", str(job.scenario_path),
            "--release-manifest", str(job.release_path),
            "--scenario-sha256", job.scenario_sha256,
            "--release-sha256", job.release_sha256,
            "--trial-id", job.trace_id,
            "--condition-id", job.condition_id,
            "--algorithm", job.algorithm,
            "--arrival-load", job.load_id,
            "--policy-id", job.policy_id,
            "--policy", job.policy_mode,
            "--batch-size", str(job.batch_size),
            "--seed", str(job.runtime_seed),
            "--output-dir", str(attempt_dir),
        ]
        if job.max_pending_age_s is not None:
            command.extend(["--max-pending-age-s", str(job.max_pending_age_s)])
        extra_args = runner.get("extra_args", [])
        if not isinstance(extra_args, list) or not all(isinstance(value, str) for value in extra_args):
            raise ValueError("runner.extra_args must be a list of strings")
        command.extend(extra_args)
        return command

    def completed_dir(self, job: CampaignJob) -> Path:
        return self.output_root / "completed" / job.job_id

    def _completion_state(self, job: CampaignJob) -> str:
        directory = self.completed_dir(job)
        if not directory.exists():
            return "pending"
        marker = directory / "completion.json"
        if not marker.is_file():
            return "conflict"
        try:
            completion = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "conflict"
        if completion.get("job_fingerprint") != job.fingerprint:
            return "conflict"
        if completion.get("source_tree_sha256") != job.source_tree_sha256:
            return "conflict"
        if completion.get("git_head") != job.git_head:
            return "conflict"
        if completion.get("python_hash_seed") != job.python_hash_seed:
            return "conflict"
        recorded_hashes = completion.get("required_output_sha256")
        if not isinstance(recorded_hashes, dict) or set(recorded_hashes) != set(self.required_outputs):
            return "conflict"
        try:
            for relative in self.required_outputs:
                path = directory / relative
                if not path.is_file() or path.stat().st_size == 0:
                    return "conflict"
                recorded_hash = recorded_hashes.get(relative)
                if not isinstance(recorded_hash, str) or sha256_file(path) != recorded_hash:
                    return "conflict"
        except OSError:
            return "conflict"
        try:
            validate_job_outputs(job, directory)
        except (OSError, OutputValidationError, KeyError, TypeError):
            return "conflict"
        return "completed"

    def _append_jsonl(self, path: Path, value: Any) -> None:
        line = json.dumps(value, sort_keys=True, ensure_ascii=True) + "\n"
        with self.log_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

    def _attempt_dir(self, job: CampaignJob) -> Path:
        attempt_id = f"attempt_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:8]}"
        return self.output_root / "attempts" / job.job_id / attempt_id

    def _run_one(self, job: CampaignJob) -> JobResult:
        if self.process_controller.cancelled.is_set():
            return JobResult(job.job_id, "cancelled", message="campaign cancellation requested")
        state = self._completion_state(job)
        if state == "completed":
            return JobResult(job.job_id, "skipped_completed")
        if state == "conflict":
            message = "completed path exists but is invalid or belongs to a different job fingerprint"
            self._record_failure(job, None, message, None)
            return JobResult(job.job_id, "failed", message=message)
        attempt_dir = self._attempt_dir(job)
        attempt_dir.mkdir(parents=True, exist_ok=False)
        command = self.command_for(job, attempt_dir)
        started_at = utc_now()
        _atomic_json(attempt_dir / "job.json", {
            "job": dataclasses.asdict(job) | {
                "scenario_path": str(job.scenario_path),
                "release_path": str(job.release_path),
            },
            "command": command,
            "started_at": started_at,
        })
        start = time.monotonic()
        try:
            return_code = self.executor(job, command, attempt_dir)
        except Exception as error:  # preserve the failed attempt and continue other jobs
            elapsed = time.monotonic() - start
            message = f"executor raised {type(error).__name__}: {error}"
            self._record_failure(job, attempt_dir, message, None)
            return JobResult(job.job_id, "failed", elapsed, message)
        elapsed = time.monotonic() - start
        missing = [
            relative for relative in self.required_outputs
            if not (attempt_dir / relative).is_file() or (attempt_dir / relative).stat().st_size == 0
        ]
        if return_code != 0 or missing:
            message = (
                ("campaign interrupted" if self.process_controller.cancelled.is_set()
                 else f"runner exit code {return_code}")
                + (f"; missing/empty outputs: {', '.join(missing)}" if missing else "")
            )
            self._record_failure(job, attempt_dir, message, return_code)
            return JobResult(job.job_id, "failed", elapsed, message)
        try:
            validation = validate_job_outputs(job, attempt_dir)
        except (OSError, OutputValidationError, KeyError, TypeError) as error:
            message = f"semantic output validation failed: {error}"
            self._record_failure(job, attempt_dir, message, return_code)
            return JobResult(job.job_id, "failed", elapsed, message)
        current_source_hash = _source_tree_hash(self.repo_root)
        if current_source_hash != job.source_tree_sha256:
            message = "source tree changed while the job was running; refusing promotion"
            self._record_failure(job, attempt_dir, message, return_code)
            return JobResult(job.job_id, "failed", elapsed, message)
        output_hashes = {
            relative: sha256_file(attempt_dir / relative) for relative in self.required_outputs
        }
        completion = {
            "schema_version": 2,
            "status": "completed",
            "algorithmic_status": validation["algorithmic_status"],
            "algorithmic_failure_type": validation["algorithmic_failure_type"],
            "job_id": job.job_id,
            "condition_id": job.condition_id,
            "trace_id": job.trace_id,
            "job_fingerprint": job.fingerprint,
            "scenario_sha256": job.scenario_sha256,
            "release_sha256": job.release_sha256,
            "git_head": job.git_head,
            "source_tree_sha256": job.source_tree_sha256,
            "python_hash_seed": job.python_hash_seed,
            "required_output_sha256": output_hashes,
            "semantic_validation": validation,
            "started_at": started_at,
            "completed_at": utc_now(),
            "host_elapsed_s": elapsed,
            "runner_return_code": return_code,
        }
        _atomic_json(attempt_dir / "completion.json", completion)
        final_dir = self.completed_dir(job)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        if final_dir.exists():
            message = "refusing to overwrite a completed path that appeared during execution"
            self._record_failure(job, attempt_dir, message, return_code)
            return JobResult(job.job_id, "failed", elapsed, message)
        attempt_dir.rename(final_dir)
        result_status = (
            "completed" if validation["all_tasks_completed"]
            else "algorithmically_incomplete"
        )
        self._append_jsonl(self.output_root / "campaign_events.jsonl", {
            "timestamp": utc_now(), "event": result_status, "job_id": job.job_id,
            "elapsed_s": elapsed,
            "algorithmic_failure_type": validation["algorithmic_failure_type"],
        })
        return JobResult(job.job_id, result_status, elapsed)

    def _record_failure(self, job: CampaignJob, attempt_dir: Path | None,
                        message: str, return_code: int | None) -> None:
        failure = {
            "timestamp": utc_now(),
            "event": "failed",
            "job_id": job.job_id,
            "condition_id": job.condition_id,
            "trace_id": job.trace_id,
            "job_fingerprint": job.fingerprint,
            "attempt_dir": str(attempt_dir) if attempt_dir else None,
            "return_code": return_code,
            "message": message,
        }
        if attempt_dir is not None:
            _atomic_json(attempt_dir / "failure.json", failure)
        self._append_jsonl(self.output_root / "failures.jsonl", failure)
        self._append_jsonl(self.output_root / "campaign_events.jsonl", failure)

    def write_provenance(self, jobs: Iterable[CampaignJob], dry_run: bool) -> Path:
        self.output_root.mkdir(parents=True, exist_ok=True)
        job_list = list(jobs)
        config_bytes = self.config_path.read_bytes()
        run_id = f"run_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}_{uuid.uuid4().hex[:8]}"
        provenance = {
            "schema_version": 1,
            "run_id": run_id,
            "campaign_id": self.campaign["campaign_id"],
            "started_at": utc_now(),
            "dry_run": dry_run,
            "config_path": str(self.config_path),
            "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
            "config": self.config,
            "manifest_index_path": str(self.manifest_root / "manifest_index.json"),
            "manifest_index_sha256": sha256_file(self.manifest_root / "manifest_index.json"),
            "source_identity_at_campaign_start": self.source_identity,
            "machine": _machine_metadata(),
            "worker_policy": {
                "formula": "floor(0.75 * logical_cores)",
                "logical_cores": self.logical_cores,
                "hard_cap": self.hard_worker_cap,
                "effective_workers": self.max_workers,
            },
            "job_count": len(job_list),
            "schedule": {
                "method": "deterministic_condition_balanced_round_robin_v1",
                "seed": self.schedule_seed,
                "ordered_job_ids": [job.job_id for job in job_list],
                "order_sha256": hashlib.sha256(
                    canonical_json_bytes([job.job_id for job in job_list])
                ).hexdigest(),
            },
            "completed_before_run": sum(self._completion_state(job) == "completed" for job in job_list),
        }
        path = self.output_root / "provenance" / f"{run_id}.json"
        _atomic_json(path, provenance)
        return path

    def run(
        self,
        dry_run: bool = False,
        job_limit: int | None = None,
        allow_dirty: bool = False,
    ) -> list[JobResult]:
        dirty_allowed = allow_dirty or bool(self.campaign.get("allow_dirty", False))
        if not dry_run and self.source_identity["git_dirty"] is True and not dirty_allowed:
            raise RuntimeError(
                "refusing to run a dirty source tree; commit/stash changes or explicitly use "
                "--allow-dirty for a development pilot"
            )
        self.prepare_manifests()
        jobs = self.plan_jobs()
        if job_limit is not None:
            if isinstance(job_limit, bool) or not isinstance(job_limit, int) or job_limit <= 0:
                raise ValueError("job_limit must be a positive integer")
            jobs = jobs[:job_limit]
        provenance_path = self.write_provenance(jobs, dry_run)
        print(
            f"campaign={self.campaign['campaign_id']} jobs={len(jobs)} workers={self.max_workers} "
            f"hard_cap={self.hard_worker_cap} provenance={provenance_path}"
        )
        if dry_run:
            for job in jobs:
                print(f"{self._completion_state(job):>9} {job.job_id}")
            return [JobResult(job.job_id, f"dry_run_{self._completion_state(job)}") for job in jobs]
        results: list[JobResult] = []
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers)
        interrupted = False
        futures: dict[concurrent.futures.Future[JobResult], CampaignJob] = {}
        try:
            for job in jobs:
                futures[pool.submit(self._run_one, job)] = job
            total = len(futures)
            for completed_count, future in enumerate(concurrent.futures.as_completed(futures), 1):
                result = future.result()
                results.append(result)
                print(f"[{completed_count}/{total}] {result.status}: {result.job_id} {result.message}", flush=True)
        except KeyboardInterrupt:
            interrupted = True
            self.process_controller.cancel_all()
            cancelled_pending = sum(future.cancel() for future in futures)
            self._append_jsonl(self.output_root / "campaign_events.jsonl", {
                "timestamp": utc_now(),
                "event": "campaign_interrupted",
                "cancelled_pending_jobs": cancelled_pending,
                "active_processes_after_termination": self.process_controller.active_count,
            })
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            if not interrupted:
                pool.shutdown(wait=True, cancel_futures=False)
        results.sort(key=lambda result: result.job_id)
        summary = {
            "timestamp": utc_now(),
            "event": "campaign_invocation_finished",
            "counts": {
                status: sum(result.status == status for result in results)
                for status in sorted({result.status for result in results})
            },
        }
        self._append_jsonl(self.output_root / "campaign_events.jsonl", summary)
        return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--max-workers", type=int)
    parser.add_argument("--job-limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--allow-dirty", action="store_true",
        help="allow a development run from a dirty tree; exact source content is fingerprinted",
    )
    parser.add_argument(
        "--prepare-only", action="store_true",
        help="generate and validate manifests, then exit without creating campaign outputs",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    orchestrator = CampaignOrchestrator(
        config_path=args.config,
        repo_root=args.repo_root,
        requested_workers=args.max_workers,
    )
    if args.prepare_only:
        root = orchestrator.prepare_manifests()
        print(f"prepared manifests: {root}")
        return 0
    try:
        results = orchestrator.run(
            dry_run=args.dry_run,
            job_limit=args.job_limit,
            allow_dirty=args.allow_dirty,
        )
    except KeyboardInterrupt:
        print("campaign interrupted; active child processes terminated and attempts preserved", file=sys.stderr)
        return 130
    failed = sum(result.status == "failed" for result in results)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
