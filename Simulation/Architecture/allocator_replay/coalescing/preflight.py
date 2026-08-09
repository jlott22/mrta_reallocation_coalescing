"""Collaborative-only hardware safety and persistent-runtime preflight."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .build import verify_device_build
from .config import ALLOCATORS, REPOSITORY_ROOT, HilCondition, PolicySpec
from .io import atomic_json
from .manifests import PairedTrace, canonical_sha256, sha256_file
from .runtime import PersistentEpochSession


PREFLIGHT_PATH = (
    REPOSITORY_ROOT
    / "results"
    / "hil_reallocation_coalescing"
    / "device_preflight.json"
)


def _fresh_identity(device: Any) -> Any:
    """Query the running worker; never accept the host's cached identity."""

    hello = getattr(device, "hello", None)
    if not callable(hello):
        raise RuntimeError("replay device has no live HELLO operation")
    return hello()


def verified_device_row(
    device: Any, manifest: dict[str, object]
) -> dict[str, Any]:
    """Run live HELLO and CHECK and bind their answers to one build."""

    identity = _fresh_identity(device)
    check = device.check()
    device_id = str(identity.device_id)
    if str(identity.build_id) != str(manifest["build_id"]):
        raise RuntimeError(
            f"{device_id} has build {identity.build_id}, "
            f"expected {manifest['build_id']}"
        )
    if check.get("motors_initialized") or check.get("sensors_initialized"):
        raise RuntimeError(f"unsafe device state on {device_id}")
    actual = str(check.get("actual_module_set_sha256", ""))
    expected = str(check.get("expected_module_set_sha256", ""))
    built = str(manifest.get("deployed_module_set_sha256", ""))
    if (
        (bool(manifest.get("compiled")) and not actual)
        or actual != expected
        or actual != built
    ):
        raise RuntimeError(
            f"deployed module hash mismatch on {device_id}: "
            f"actual={actual!r}, worker_expected={expected!r}, build={built!r}"
        )
    return {
        "device_id": device_id,
        "port": str(identity.port),
        "build_id": str(identity.build_id),
        "module_set_sha256": actual,
        "worker_expected_module_set_sha256": expected,
        "source_bundle_sha256": str(manifest["source_bundle_sha256"]),
        "implementation": str(identity.implementation),
        "frequency_hz": int(identity.frequency_hz),
        "firmware_sha256": str(identity.firmware_sha256),
        "motors_initialized": False,
        "sensors_initialized": False,
    }


def build_device_binding(
    devices: list[Any],
    build_root: Path,
    *,
    execution_mode: str,
    require_compiled: bool,
) -> dict[str, Any]:
    """Create a canonical exact identity for a set of live replay devices."""

    if not devices:
        raise ValueError("no devices supplied for campaign binding")
    root = Path(build_root).resolve()
    manifest = verify_device_build(root, require_compiled=require_compiled)
    rows = [verified_device_row(device, manifest) for device in devices]
    ids = [row["device_id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise RuntimeError("duplicate device_id values in campaign binding")
    # Ports are recorded in the preflight report, but are deliberately not an
    # identity field: the same physical board may enumerate on a new COM port.
    identity_rows = [
        {key: value for key, value in row.items() if key != "port"}
        for row in sorted(rows, key=lambda item: item["device_id"])
    ]
    binding: dict[str, Any] = {
        "schema_version": 1,
        "execution_mode": str(execution_mode),
        "build_id": str(manifest["build_id"]),
        "source_bundle_sha256": str(manifest["source_bundle_sha256"]),
        "module_set_sha256": str(manifest["deployed_module_set_sha256"]),
        "build_manifest_sha256": sha256_file(root / "manifest.json"),
        "build_root": str(root),
        "devices": identity_rows,
    }
    binding["device_binding_sha256"] = canonical_sha256(binding)
    return binding


def _verify_binding_hash(binding: dict[str, Any]) -> None:
    claimed = str(binding.get("device_binding_sha256", ""))
    unsigned = dict(binding)
    unsigned.pop("device_binding_sha256", None)
    if not claimed or canonical_sha256(unsigned) != claimed:
        raise RuntimeError("device binding hash mismatch")


def _probe_trace() -> PairedTrace:
    cells = ((2, 2), (4, 4), (6, 6), (8, 8))
    tasks = tuple(
        {
            "task_id": f"task_{index:02d}",
            "x": x,
            "y": y,
            "initially_visible": index < 2,
            "release_time_s": 0.0 if index < 2 else float(index - 1),
        }
        for index, (x, y) in enumerate(cells)
    )
    starts = tuple(
        {
            "robot_id": f"{index:02d}",
            "x": 0,
            "y": y,
            "heading_x": 1,
            "heading_y": 0,
        }
        for index, y in enumerate((0, 6, 12, 18))
    )
    return PairedTrace(
        manifest_set_id="preflight",
        trace_id="trace_preflight",
        load_id="preflight",
        scenario_path=Path("preflight://scenario"),
        release_path=Path("preflight://release"),
        scenario_sha256="preflight",
        release_sha256="preflight",
        runtime_seed=1009,
        grid_size=19,
        robot_starts=starts,
        tasks=tasks,
    )


def _assert_probe(
    metrics: dict[str, Any],
    *,
    hook: bool,
    visible_count: int,
    require_full_solve: bool,
    require_enumeration: bool = True,
) -> None:
    if bool(metrics.get("allocation_epoch_hook_invoked")) is not hook:
        raise RuntimeError("allocation epoch hook idempotence probe failed")
    before = int(metrics.get("candidate_count_before", -1))
    after = int(metrics.get("candidate_count_after", -1))
    expected_counts = (visible_count, visible_count)
    if require_enumeration and (before, after) != expected_counts:
        raise RuntimeError(
            "unrestricted visible-set probe failed: "
            f"expected {visible_count}, received before={before}, after={after}"
        )
    if not require_enumeration and before != after:
        raise RuntimeError("duplicate epoch call applied a candidate restriction")
    if require_full_solve and metrics.get("call_class") != "full_allocation_solve":
        raise RuntimeError("online admission did not force a full allocation solve")


def run_preflight(devices: list[Any], build_root: Path) -> dict[str, Any]:
    """Re-check modules and exercise actual visible-set growth on every allocator."""

    binding = build_device_binding(
        devices,
        build_root,
        execution_mode="serial_hardware",
        require_compiled=True,
    )
    manifest = verify_device_build(Path(binding["build_root"]), require_compiled=True)
    device_rows_by_id = {row["device_id"]: row for row in binding["devices"]}
    probes: list[dict[str, Any]] = []
    trace = _probe_trace()
    initial_ids = tuple(
        item["task_id"] for item in trace.tasks if item["initially_visible"]
    )
    online_ids = tuple(
        item["task_id"] for item in trace.tasks if not item["initially_visible"]
    )
    for device in devices:
        # Re-CHECK immediately before allocator probes, after any discovery or
        # setup calls that could have changed worker state.
        current = verified_device_row(device, manifest)
        expected = device_rows_by_id.get(current["device_id"])
        current_identity = {key: value for key, value in current.items() if key != "port"}
        if current_identity != expected:
            raise RuntimeError("device identity changed during preflight")
        for algorithm in ALLOCATORS:
            restart = getattr(device, "restart_clean_worker", None)
            if callable(restart):
                restart()
            # A restart must not be allowed to swap the worker or deployed
            # module set after the initial check.
            restarted = verified_device_row(device, manifest)
            restarted_identity = {
                key: value for key, value in restarted.items() if key != "port"
            }
            if restarted_identity != expected:
                raise RuntimeError("device identity changed after clean-worker restart")
            condition = HilCondition(
                algorithm,
                "preflight",
                PolicySpec("eager", 1),
                "preflight",
            )
            session = PersistentEpochSession(device, condition, trace, 1, 30.0)
            try:
                session.begin()
                session.admit(initial_ids)
                initial = session.call(
                    "00", epoch_index=0, round_index=0, trigger_reason="initial_tasks"
                )
                _assert_probe(
                    initial.metrics,
                    hook=True,
                    visible_count=len(initial_ids),
                    require_full_solve=True,
                )
                session.admit(online_ids[:1])
                grown = session.call(
                    "00",
                    epoch_index=1,
                    round_index=0,
                    trigger_reason="task_arrival_eager",
                )
                _assert_probe(
                    grown.metrics,
                    hook=True,
                    visible_count=len(initial_ids) + 1,
                    require_full_solve=True,
                )
                duplicate = session.call(
                    "00",
                    epoch_index=1,
                    round_index=1,
                    trigger_reason="task_arrival_eager",
                )
                _assert_probe(
                    duplicate.metrics,
                    hook=False,
                    visible_count=len(initial_ids) + 1,
                    require_full_solve=False,
                    require_enumeration=False,
                )
                probes.append(
                    {
                        "device_id": restarted["device_id"],
                        "device_build_id": restarted["build_id"],
                        "device_module_set_sha256": restarted[
                            "module_set_sha256"
                        ],
                        "device_source_bundle_sha256": restarted[
                            "source_bundle_sha256"
                        ],
                        "device_firmware_sha256": restarted[
                            "firmware_sha256"
                        ],
                        "allocator": algorithm,
                        "passed": True,
                        "initial_visible_count": len(initial_ids),
                        "grown_visible_count": len(initial_ids) + 1,
                        "initial_call": initial.metrics,
                        "online_growth_call": grown.metrics,
                        "duplicate_epoch_call": duplicate.metrics,
                    }
                )
            finally:
                session.close()
    final_device_rows: list[dict[str, Any]] = []
    for device in devices:
        final_row = verified_device_row(device, manifest)
        final_identity = {
            key: value for key, value in final_row.items() if key != "port"
        }
        if final_identity != device_rows_by_id.get(final_row["device_id"]):
            raise RuntimeError("device identity changed before preflight completion")
        final_device_rows.append(final_row)
    report: dict[str, Any] = {
        "schema_version": 2,
        "study_id": "mrta_reallocation_coalescing",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "passed": len(probes) == len(devices) * len(ALLOCATORS),
        "build_id": binding["build_id"],
        "module_set_sha256": binding["module_set_sha256"],
        "source_bundle_sha256": binding["source_bundle_sha256"],
        "build_manifest_sha256": binding["build_manifest_sha256"],
        "build_root": binding["build_root"],
        "device_binding": binding,
        "candidate_mode": "unrestricted",
        "devices": final_device_rows,
        "allocator_probes": probes,
    }
    report["report_sha256"] = canonical_sha256(report)
    atomic_json(PREFLIGHT_PATH, report)
    return report


def verify_preflight(devices: list[Any]) -> dict[str, Any]:
    """Re-hash the build and live CHECK every board against the last preflight."""

    if not PREFLIGHT_PATH.is_file():
        raise RuntimeError("run the coalescing hardware preflight first")
    report = json.loads(PREFLIGHT_PATH.read_text(encoding="utf-8"))
    claimed = str(report.get("report_sha256", ""))
    unsigned = dict(report)
    unsigned.pop("report_sha256", None)
    if not claimed or canonical_sha256(unsigned) != claimed:
        raise RuntimeError("coalescing hardware preflight report hash mismatch")
    if not report.get("passed"):
        raise RuntimeError("latest coalescing hardware preflight did not pass")
    prior = report.get("device_binding")
    if not isinstance(prior, dict):
        raise RuntimeError("preflight has no sealed device binding")
    _verify_binding_hash(prior)
    root = Path(str(prior.get("build_root", ""))).resolve()
    manifest = verify_device_build(root, require_compiled=True)
    if sha256_file(root / "manifest.json") != prior.get("build_manifest_sha256"):
        raise RuntimeError("device build manifest differs from the passed preflight")
    if str(manifest["build_id"]) != str(prior.get("build_id", "")):
        raise RuntimeError("device build differs from the passed preflight")
    # build_device_binding performs fresh HELLO and CHECK calls, so a cached
    # host identity can never make stale firmware/modules pass here.
    current = build_device_binding(
        devices,
        root,
        execution_mode="serial_hardware",
        require_compiled=True,
    )
    if current != prior:
        raise RuntimeError("connected build/module/firmware set differs from preflight")
    return report
