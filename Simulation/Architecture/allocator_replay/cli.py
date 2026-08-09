from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from allocator_replay.capture.runner import capture_all
from allocator_replay.config.study import (
    CAMPAIGN_ROOT,
    INITIAL_HARDWARE_TRIAL_COUNTS,
    Condition,
    conditions,
)
from allocator_replay.device.build import build_device_bundle, validate_built_imports
from allocator_replay.host.campaign import (
    CampaignRunner,
    create_campaign,
    reassign_condition,
)
from allocator_replay.host.deployment import deploy, load_build
from allocator_replay.host.discovery import (
    common_compatibility,
    discover,
)
from allocator_replay.host.preflight import PREFLIGHT_PATH, run_preflight
from allocator_replay.host.report import rebuild_reports
from allocator_replay.host.status import campaign_status
from allocator_replay.host.transport import SerialReplayDevice
from allocator_replay.hil.campaign import HilCampaignRunner
from allocator_replay.hil.manifest import (
    HIL_ROOT,
    prepare_campaign,
    verify_campaign_provenance,
)
from allocator_replay.hil.report import rebuild_hil_reports
from allocator_replay.hil.regression import (
    DEFAULT_REGRESSION_GATES,
    HilRegressionRunner,
    prepare_regression_run,
    regression_root,
    regression_status,
    select_regression_gates,
    verify_regression_run,
)
from allocator_replay.hil.status import hil_status


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _ports(values: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    requested: Iterable[str] | str = (
        "auto" if values == ["auto"] else values
    )
    devices, failures = discover(requested)
    ports = [device.port for device in devices]
    if not ports:
        raise RuntimeError(
            "no compatible MicroPython Pololus discovered"
            + (f": {failures}" if failures else "")
        )
    return ports, failures


def _latest_campaign() -> Path:
    candidates = [
        path
        for path in CAMPAIGN_ROOT.iterdir()
        if path.is_dir() and (path / "schedule.json").exists()
    ] if CAMPAIGN_ROOT.exists() else []
    if not candidates:
        raise FileNotFoundError("no allocator replay campaign exists")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _campaign(value: str | None) -> Path:
    if value is None:
        return _latest_campaign()
    path = Path(value)
    if path.exists():
        return path.resolve()
    candidate = CAMPAIGN_ROOT / value
    if candidate.exists():
        return candidate.resolve()
    raise FileNotFoundError(value)


def _hil_campaign(value: str | None) -> Path:
    if value is not None:
        path = Path(value)
        if path.exists():
            return path.resolve()
        candidate = HIL_ROOT / value
        if candidate.exists():
            return candidate.resolve()
        raise FileNotFoundError(value)
    candidates = [
        path
        for path in HIL_ROOT.iterdir()
        if path.is_dir() and (path / "schedule.json").exists()
    ] if HIL_ROOT.exists() else []
    if not candidates:
        raise FileNotFoundError("no authoritative HIL campaign exists")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _selected(args: argparse.Namespace) -> list[Condition]:
    chosen = conditions(args.mission)
    condition_set = getattr(args, "condition_set", "all")
    if condition_set == "baseline":
        chosen = [
            item
            for item in chosen
            if item.top_k_level
            in {"5%", "10%", "25%", "50%", "75%", "100%"}
        ]
    elif condition_set == "low-k":
        chosen = [
            item
            for item in chosen
            if item.top_k_level
            not in {"5%", "10%", "25%", "50%", "75%", "100%"}
        ]
    if getattr(args, "algorithm", None):
        chosen = [item for item in chosen if item.algorithm == args.algorithm]
    if getattr(args, "top_k_rate", None) is not None:
        chosen = [
            item
            for item in chosen
            if abs(item.top_k_rate - args.top_k_rate) < 1e-12
        ]
    if getattr(args, "top_k_cells", None) is not None:
        chosen = [
            item
            for item in chosen
            if item.top_k_cells == args.top_k_cells
        ]
    if getattr(args, "top_k_level", None):
        chosen = [
            item
            for item in chosen
            if item.top_k_level == args.top_k_level
        ]
    if not chosen:
        raise RuntimeError("condition filters selected no work")
    return chosen


def _open_devices(ports: list[str]) -> list[SerialReplayDevice]:
    devices: list[SerialReplayDevice] = []
    try:
        for port in ports:
            devices.append(SerialReplayDevice(port))
        return devices
    except Exception:
        for device in devices:
            device.close()
        raise


def _close_devices(devices: list[SerialReplayDevice]) -> None:
    for device in devices:
        try:
            device.exit()
        except Exception:
            pass
        device.close()


def _verify_preflight(devices: list[SerialReplayDevice]) -> None:
    if not PREFLIGHT_PATH.exists():
        raise RuntimeError("run preflight before starting a hardware campaign")
    report = json.loads(PREFLIGHT_PATH.read_text(encoding="utf-8"))
    if not report.get("passed"):
        raise RuntimeError("the latest hardware preflight did not pass")
    current = {
        (device.identity or device.hello()).device_id: (
            (device.identity or device.hello()).build_id,
            (device.identity or device.hello()).frequency_hz,
            (device.identity or device.hello()).firmware_sha256,
        )
        for device in devices
    }
    prior = {
        item["device_id"]: (
            item["build_id"],
            item["frequency_hz"],
            item["firmware_sha256"],
        )
        for item in report["devices"]
    }
    if current != prior:
        raise RuntimeError(
            "connected device IDs/builds/frequencies differ from the passed preflight"
        )


def _capture(args: argparse.Namespace) -> None:
    _print(
        capture_all(
            mission=args.mission,
            selected=_selected(args),
            workers=args.workers,
            max_trials=args.max_trials,
            force=args.force,
        )
    )


def _build(args: argparse.Namespace) -> None:
    compatibility = args.compatibility
    discovery_result: list[dict[str, object]] = []
    if args.ports:
        requested = "auto" if args.ports == ["auto"] else args.ports
        devices, failures = discover(requested)
        if failures:
            discovery_result.extend(failures)
        compatibility = common_compatibility(devices)
        discovery_result.extend(device.as_dict() for device in devices)
    manifest = build_device_bundle(
        compatibility=compatibility,
        optimize=args.optimize,
        compile_mpy=not args.source_only,
    )
    root, _ = load_build(Path(str(manifest["output"])))
    validate_built_imports(root)
    _print({"build": manifest, "detected_devices": discovery_result})


def _discover(args: argparse.Namespace) -> None:
    requested = "auto" if args.ports == ["auto"] else args.ports
    devices, failures = discover(requested)
    _print(
        {
            "devices": [device.as_dict() for device in devices],
            "failures": failures,
        }
    )


def _deploy(args: argparse.Namespace) -> None:
    requested = "auto" if args.ports == ["auto"] else args.ports
    discovered, failures = discover(requested)
    if not discovered:
        raise RuntimeError("no compatible MicroPython Pololus discovered")
    build_root, manifest = load_build(
        Path(args.build).resolve() if args.build else None
    )
    incompatible = [
        device.port
        for device in discovered
        if device.compatibility != manifest["compatibility"]
    ]
    if incompatible:
        raise RuntimeError(
            "device build compatibility mismatch on: "
            + ", ".join(incompatible)
        )
    ports = [device.port for device in discovered]
    results = deploy(
        ports,
        build_root=build_root,
    )
    _print({"deployments": results, "discovery_failures": failures})


def _preflight(args: argparse.Namespace) -> None:
    ports, failures = _ports(args.ports)
    devices = _open_devices(ports)
    try:
        result = run_preflight(
            devices,
            build_root=Path(args.build).resolve() if args.build else None,
            calibration_repetitions=args.repetitions,
        )
        result["discovery_failures"] = failures
        _print(result)
    finally:
        _close_devices(devices)


def _run(args: argparse.Namespace) -> None:
    ports, failures = _ports(args.ports)
    devices = _open_devices(ports)
    try:
        _verify_preflight(devices)
        if args.campaign:
            root = create_campaign(
                args.campaign,
                selected=_selected(args),
                build_root=Path(args.build).resolve() if args.build else None,
                trial_windows={
                    "bayesian": (
                        args.bayesian_trial_start,
                        args.bayesian_trials,
                    ),
                    "collaborative": (
                        args.collaborative_trial_start,
                        args.collaborative_trials,
                    ),
                },
            )
        else:
            campaign_id = "hardware_" + datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%SZ"
            )
            root = create_campaign(
                campaign_id,
                selected=_selected(args),
                build_root=Path(args.build).resolve() if args.build else None,
                trial_windows={
                    "bayesian": (
                        args.bayesian_trial_start,
                        args.bayesian_trials,
                    ),
                    "collaborative": (
                        args.collaborative_trial_start,
                        args.collaborative_trials,
                    ),
                },
            )
        state = CampaignRunner(root, devices).run()
        report = rebuild_reports(root)
        _print(
            {
                "campaign_root": str(root),
                "status": state["status"],
                "report": report,
                "discovery_failures": failures,
            }
        )
    finally:
        _close_devices(devices)


def _status(args: argparse.Namespace) -> None:
    _print(campaign_status(_campaign(args.campaign)))


def _report(args: argparse.Namespace) -> None:
    root = _campaign(args.campaign)
    _print({"campaign_root": str(root), **rebuild_reports(root)})


def _reassign(args: argparse.Namespace) -> None:
    root = _campaign(args.campaign)
    _print(reassign_condition(root, args.condition, args.device_id))


def _hil_prepare(args: argparse.Namespace) -> None:
    campaign_id = args.campaign or (
        "pololu_authoritative_" + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
    )
    root = prepare_campaign(
        campaign_id,
        build_root=Path(args.build).resolve() if args.build else None,
    )
    _print(
        {
            "campaign_root": str(root),
            "status": hil_status(root),
        }
    )


def _hil_run(args: argparse.Namespace) -> None:
    root = _hil_campaign(args.campaign)
    ports, failures = _ports(args.ports)
    devices = _open_devices(ports)
    try:
        identities = [device.identity or device.hello() for device in devices]
        verify_campaign_provenance(
            root,
            device_build_ids=[
                str(identity.build_id) for identity in identities
            ],
        )
        _verify_preflight(devices)
        state = HilCampaignRunner(root, devices).run()
        _print(
            {
                "campaign_root": str(root),
                "status": state["status"],
                "report": rebuild_hil_reports(root),
                "discovery_failures": failures,
            }
        )
    finally:
        _close_devices(devices)


def _hil_status(args: argparse.Namespace) -> None:
    _print(hil_status(_hil_campaign(args.campaign)))


def _hil_report(args: argparse.Namespace) -> None:
    root = _hil_campaign(args.campaign)
    _print({"campaign_root": str(root), **rebuild_hil_reports(root)})


def _hil_regression_gate(args: argparse.Namespace) -> None:
    if args.list_gates:
        _print(
            {
                "gates": [
                    gate.manifest_row()
                    for gate in DEFAULT_REGRESSION_GATES
                ]
            }
        )
        return
    if args.status:
        root = regression_root(args.run_id)
        _print(
            {
                "regression_root": str(root),
                "status": regression_status(root),
            }
        )
        return
    gates = select_regression_gates(args.gates)
    build_root = Path(args.build).resolve() if args.build else None
    root = prepare_regression_run(
        args.run_id,
        gates,
        build_root=build_root,
    )
    ports, failures = _ports(args.ports)
    devices = _open_devices(ports)
    try:
        identities = [device.identity or device.hello() for device in devices]
        verify_regression_run(
            root,
            build_root=build_root,
            device_build_ids=[
                str(identity.build_id) for identity in identities
            ],
        )
        _verify_preflight(devices)
        runner = HilRegressionRunner(root, devices)
        if args.retry_failed:
            runner.retry_failed()
        runner.run()
        _print(
            {
                "regression_root": str(root),
                "status": regression_status(root),
                "discovery_failures": failures,
            }
        )
    finally:
        _close_devices(devices)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m allocator_replay",
        description="Motionless multi-Pololu allocator replay",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser("capture")
    capture.add_argument(
        "--mission",
        choices=("bayesian", "collaborative"),
        default=None,
    )
    capture.add_argument("--workers", type=int, default=None)
    capture.add_argument("--max-trials", type=int, default=None)
    capture.add_argument(
        "--algorithm",
        choices=("CBAA", "ACBBA", "PI", "HIPC", "DMCHBA", "DGA"),
    )
    capture.add_argument("--top-k-rate", type=float, default=None)
    capture.add_argument("--top-k-cells", type=int, default=None)
    capture.add_argument("--top-k-level", default=None)
    capture.add_argument("--force", action="store_true")
    capture.set_defaults(handler=_capture)

    build = subparsers.add_parser("build-device")
    build.add_argument("--ports", nargs="+", default=None)
    build.add_argument("--compatibility", default="1.24")
    build.add_argument("--optimize", type=int, default=0)
    build.add_argument("--source-only", action="store_true")
    build.set_defaults(handler=_build)

    discovery = subparsers.add_parser("discover")
    discovery.add_argument("--ports", nargs="+", default=["auto"])
    discovery.set_defaults(handler=_discover)

    deployment = subparsers.add_parser("deploy")
    deployment.add_argument("--ports", nargs="+", default=["auto"])
    deployment.add_argument("--build", default=None)
    deployment.set_defaults(handler=_deploy)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--ports", nargs="+", default=["auto"])
    preflight.add_argument("--build", default=None)
    preflight.add_argument("--repetitions", type=int, default=5)
    preflight.set_defaults(handler=_preflight)

    run = subparsers.add_parser("run")
    run.add_argument("--ports", nargs="+", default=["auto"])
    run.add_argument("--campaign", default=None)
    run.add_argument("--build", default=None)
    run.add_argument(
        "--mission",
        choices=("bayesian", "collaborative"),
        default=None,
    )
    run.add_argument("--algorithm", choices=("CBAA", "ACBBA", "PI", "HIPC", "DMCHBA", "DGA"))
    run.add_argument("--top-k-rate", type=float, default=None)
    run.add_argument("--top-k-cells", type=int, default=None)
    run.add_argument("--top-k-level", default=None)
    run.add_argument(
        "--condition-set",
        choices=("all", "baseline", "low-k"),
        default="all",
    )
    run.add_argument(
        "--bayesian-trials",
        type=int,
        default=INITIAL_HARDWARE_TRIAL_COUNTS["bayesian"],
    )
    run.add_argument("--bayesian-trial-start", type=int, default=0)
    run.add_argument(
        "--collaborative-trials",
        type=int,
        default=INITIAL_HARDWARE_TRIAL_COUNTS["collaborative"],
    )
    run.add_argument("--collaborative-trial-start", type=int, default=0)
    run.set_defaults(handler=_run)

    status = subparsers.add_parser("status")
    status.add_argument("--campaign", default=None)
    status.set_defaults(handler=_status)

    report = subparsers.add_parser("report")
    report.add_argument("--campaign", default=None)
    report.set_defaults(handler=_report)

    reassign = subparsers.add_parser("reassign")
    reassign.add_argument("--campaign", default=None)
    reassign.add_argument("--condition", required=True)
    reassign.add_argument("--device-id", required=True)
    reassign.set_defaults(handler=_reassign)

    hil_prepare = subparsers.add_parser(
        "hil-prepare",
        help="prepare fixed historical trials for Pololu-authoritative simulation",
    )
    hil_prepare.add_argument("--campaign", default=None)
    hil_prepare.add_argument(
        "--build",
        default=None,
        help="bind the immutable campaign manifest to this device build",
    )
    hil_prepare.set_defaults(handler=_hil_prepare)

    hil_run = subparsers.add_parser(
        "hil-run",
        help="run or resume a Pololu-authoritative HIL campaign",
    )
    hil_run.add_argument("--ports", nargs="+", default=["auto"])
    hil_run.add_argument("--campaign", default=None)
    hil_run.set_defaults(handler=_hil_run)

    hil_status_parser = subparsers.add_parser("hil-status")
    hil_status_parser.add_argument("--campaign", default=None)
    hil_status_parser.set_defaults(handler=_hil_status)

    hil_report = subparsers.add_parser("hil-report")
    hil_report.add_argument("--campaign", default=None)
    hil_report.set_defaults(handler=_hil_report)

    hil_regression = subparsers.add_parser(
        "hil-regression-gate",
        help=(
            "run resumable Pololu-authoritative former-failure regression gates"
        ),
    )
    hil_regression.add_argument("--ports", nargs="+", default=["auto"])
    hil_regression.add_argument(
        "--run-id",
        default="event_staging_and_output_streaming_v1",
        help="immutable regression-run directory name",
    )
    hil_regression.add_argument(
        "--gates",
        nargs="+",
        default=["all"],
        metavar="GATE",
        help="one or more named gates, or 'all'",
    )
    hil_regression.add_argument(
        "--build",
        default=None,
        help="device build used to freeze and verify regression provenance",
    )
    hil_regression.add_argument(
        "--retry-failed",
        action="store_true",
        help="explicitly reset failed gates to pending before this run",
    )
    hil_regression.add_argument(
        "--list-gates",
        action="store_true",
        help="print the built-in gate definitions without opening serial ports",
    )
    hil_regression.add_argument(
        "--status",
        action="store_true",
        help="show this regression run without opening serial ports",
    )
    hil_regression.set_defaults(handler=_hil_regression_gate)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except Exception as exc:
        parser.exit(1, f"error: {type(exc).__name__}: {exc}\n")
    return 0
