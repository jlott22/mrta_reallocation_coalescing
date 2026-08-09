"""Native worker adapters joining causal simulation to one timing provider."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from .model import BoardBinding, CausalConfig, CausalJob


def _allocator_replay_imports(repo_root: Path) -> dict[str, Any]:
    architecture = repo_root / "Simulation" / "Architecture"
    if str(architecture) not in sys.path:
        sys.path.insert(0, str(architecture))
    causal = importlib.import_module("allocator_replay.causal")
    transport = importlib.import_module("allocator_replay.host.transport")
    return {
        "CausalBoardSession": causal.CausalBoardSession,
        "BoardLease": causal.BoardLease,
        "ZeroDurationProvider": causal.ZeroDurationProvider,
        "SimulatedDurationProvider": causal.SimulatedDurationProvider,
        "bind_single_hardware_worker": causal.bind_single_hardware_worker,
        "SerialReplayDevice": transport.SerialReplayDevice,
    }


class _OwnedPhysicalSession:
    """Close both the causal lease/session and its serial transport."""

    def __init__(self, session: Any, device: Any) -> None:
        self.session = session
        self.device = device

    def __getattr__(self, name: str) -> Any:
        return getattr(self.session, name)

    def open(self) -> "_OwnedPhysicalSession":
        self.session.open()
        return self

    def close(self) -> None:
        try:
            self.session.close()
        finally:
            self.device.close()


def create_timing_provider(
    *,
    config: CausalConfig,
    board: BoardBinding,
    zero_compute: bool,
) -> Any:
    """Create one worker-owned provider; hardware is never faked implicitly."""

    imports = _allocator_replay_imports(config.repo_root)
    if zero_compute:
        return imports["ZeroDurationProvider"]()
    if config.development_override:
        duration = config.raw.get("hardware", {}).get("simulated_duration_us")
        if duration is None:
            raise RuntimeError(
                "development_override requires explicit hardware.simulated_duration_us; "
                "this output is never hardware-valid"
            )
        return imports["SimulatedDurationProvider"](int(duration))
    # Claim the immutable UID globally within this checkout before opening the
    # serial endpoint.  This prevents two concurrent campaign invocations from
    # double-opening one board even when their output roots differ.
    lease = imports["BoardLease"](
        board.expected_device_uid,
        config.repo_root / "study" / "native_device_leases",
    ).acquire()
    device = None
    try:
        device = imports["SerialReplayDevice"](board.serial_device)
        binding = imports["bind_single_hardware_worker"](
            device,
            worker_index=config.boards.index(board),
            expected_device_id=board.expected_device_uid,
            expected_build_id=board.expected_build_id,
            expected_module_set_sha256=board.expected_module_set_sha256,
            expected_firmware_sha256=board.expected_firmware_sha256,
        )
        session = imports["CausalBoardSession"](
            binding,
            lock_root=config.repo_root / "study" / "native_device_leases",
            lease=lease,
            timeout_seconds=config.device_timeout_seconds,
        )
    except BaseException:
        if device is not None:
            device.close()
        lease.release()
        raise
    assert device is not None
    return _OwnedPhysicalSession(session, device)


def run_causal_job(
    *,
    config: CausalConfig,
    job: CausalJob,
    attempt_dir: Path,
    timing_provider: Any,
    worker_metadata: Mapping[str, Any],
) -> None:
    """Invoke the simulator's in-process causal mission adapter.

    Keeping this call in the long-lived worker process is important: the worker
    owns one serial session for its full block sequence while the board resets
    its four allocator contexts between missions.
    """

    if job.worker_index < 0 or job.worker_index >= len(config.boards):
        raise RuntimeError("job worker index is outside configured board bindings")
    board = config.boards[job.worker_index]
    if board.board_id != job.board_id:
        raise RuntimeError("job board identity differs from immutable worker binding")
    try:
        runner_module = importlib.import_module("known_visit_sim.run_causal_trials")
    except ModuleNotFoundError as error:
        raise RuntimeError("causal simulator runner module is not installed") from error
    run_one = getattr(runner_module, "run_causal_manifest_job", None)
    if not callable(run_one):
        raise RuntimeError("known_visit_sim.run_causal_trials lacks run_causal_manifest_job")
    run_one(
        scenario_manifest=job.scenario_path,
        release_manifest=job.release_path,
        scenario_sha256=job.scenario_sha256,
        release_sha256=job.release_sha256,
        algorithm=job.algorithm,
        arrival_load=job.load_id,
        trace_id=job.trace_id,
        job_id=job.job_id,
        block_id=job.block_id,
        policy=job.policy.to_dict(),
        runtime_seed=job.runtime_seed,
        timing_provider=timing_provider,
        zero_compute=job.zero_compute,
        output_dir=attempt_dir,
        campaign_identity={
            "config_sha256": config.config_sha256,
            "manifest_index_sha256": config.manifest_index_sha256,
            "hardware_binding_sha256": config.hardware_binding_sha256,
            "expected_device_uid": board.expected_device_uid,
            "expected_device_build_id": board.expected_build_id,
            "expected_device_firmware_sha256": board.expected_firmware_sha256,
            "expected_device_module_set_sha256": board.expected_module_set_sha256,
            **dict(worker_metadata),
        },
    )
