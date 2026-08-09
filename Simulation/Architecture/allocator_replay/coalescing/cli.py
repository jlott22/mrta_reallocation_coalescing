"""Active command line for the reallocation-coalescing HIL study."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from allocator_replay.host.deployment import deploy, load_build
from allocator_replay.host.discovery import common_compatibility, discover
from allocator_replay.host.emulator import LoopbackReplayDevice
from allocator_replay.host.transport import SerialReplayDevice

from .build import (
    build_device_bundle,
    latest_device_build,
    validate_built_imports,
    verify_device_build,
)
from .campaign import CoalescingCampaignRunner
from .config import CampaignConfig, load_campaign_config
from .manifests import load_all_pairs
from .preflight import build_device_binding, run_preflight, verify_preflight
from .report import campaign_status, rebuild_report
from .schedule import campaign_root, prepare_campaign, verify_campaign


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _ports(values: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    requested: Iterable[str] | str = "auto" if values == ["auto"] else values
    devices, failures = discover(requested)
    ports = [item.port for item in devices]
    if not ports:
        raise RuntimeError(
            "no compatible MicroPython Pololus discovered"
            + (f": {failures}" if failures else "")
        )
    return ports, failures


def _open(ports: list[str]) -> list[SerialReplayDevice]:
    result: list[SerialReplayDevice] = []
    try:
        for port in ports:
            result.append(SerialReplayDevice(port))
        return result
    except Exception:
        for item in result:
            item.close()
        raise


def _close(devices: list[Any]) -> None:
    for item in devices:
        try:
            item.close()
        except Exception:
            pass


def _config(args: argparse.Namespace) -> CampaignConfig:
    return load_campaign_config(args.config)


def _discover(args: argparse.Namespace) -> None:
    requested = "auto" if args.ports == ["auto"] else args.ports
    devices, failures = discover(requested)
    _print({"devices": [item.as_dict() for item in devices], "failures": failures})


def _build(args: argparse.Namespace) -> None:
    compatibility = args.compatibility
    detected: list[dict[str, Any]] = []
    if args.ports:
        requested = "auto" if args.ports == ["auto"] else args.ports
        devices, failures = discover(requested)
        detected.extend(failures)
        detected.extend(item.as_dict() for item in devices)
        compatibility = common_compatibility(devices)
    manifest = build_device_bundle(
        compatibility=compatibility,
        optimize=args.optimize,
        compile_mpy=not args.source_only,
    )
    validate_built_imports(Path(str(manifest["output"])))
    _print({"build": manifest, "detected_devices": detected})


def _deploy(args: argparse.Namespace) -> None:
    ports, failures = _ports(args.ports)
    if args.build:
        root = Path(args.build).resolve()
        manifest = verify_device_build(root, require_compiled=True)
    else:
        root, manifest = latest_device_build(compiled=True)
    if manifest.get("contains_bayesian") is not False:
        raise RuntimeError("refusing to deploy a legacy Bayesian/Top-K bundle")
    _print(
        {
            "deployments": deploy(ports, build_root=root),
            "discovery_failures": failures,
        }
    )


def _preflight(args: argparse.Namespace) -> None:
    root = (
        load_build(Path(args.build).resolve())[0]
        if args.build
        else latest_device_build(compiled=True)[0]
    )
    ports, failures = _ports(args.ports)
    devices = _open(ports)
    try:
        report = run_preflight(devices, root)
        report["discovery_failures"] = failures
        _print(report)
    finally:
        _close(devices)


def _validate(args: argparse.Namespace) -> None:
    config = _config(args)
    pairs = load_all_pairs(config)
    _print(
        {
            "valid": True,
            "config": config.as_dict(),
            "condition_count": len(config.conditions()),
            "paired_manifest_count": len(pairs),
            "paired_manifest_ids": [item.paired_manifest_id for item in pairs.values()],
        }
    )


def _prepare(args: argparse.Namespace) -> None:
    config = _config(args)
    ports, failures = _ports(args.ports)
    devices = _open(ports)
    try:
        report = verify_preflight(devices)
        binding = report["device_binding"]
        root = prepare_campaign(
            config, campaign_id=args.campaign, device_binding=binding
        )
        _print(
            {
                "campaign_root": str(root),
                "status": campaign_status(root),
                "device_binding_sha256": binding["device_binding_sha256"],
                "discovery_failures": failures,
            }
        )
    finally:
        _close(devices)


def _dry_run(args: argparse.Namespace) -> None:
    config = _config(args)
    campaign_id = args.campaign or (config.campaign_id + "_dry_run")
    manifest = build_device_bundle(compile_mpy=False)
    build_root = Path(str(manifest["output"]))
    validate_built_imports(build_root)
    devices = [
        LoopbackReplayDevice(f"mock-{index + 1}", build_root=build_root)
        for index in range(args.devices)
    ]
    try:
        binding = build_device_binding(
            devices,
            build_root,
            execution_mode="software_loopback_dry_run",
            require_compiled=False,
        )
        root = prepare_campaign(
            config, campaign_id=campaign_id, device_binding=binding
        )
        state = CoalescingCampaignRunner(
            root, config, devices, execution_mode="software_loopback_dry_run"
        ).run()
        report = rebuild_report(root)
        _print({"campaign_root": str(root), "status": state["status"], "report": report})
    finally:
        _close(devices)


def _run(args: argparse.Namespace) -> None:
    config = _config(args)
    ports, failures = _ports(args.ports)
    devices = _open(ports)
    try:
        preflight = verify_preflight(devices)
        binding = preflight["device_binding"]
        root = prepare_campaign(
            config, campaign_id=args.campaign, device_binding=binding
        )
        state = CoalescingCampaignRunner(
            root, config, devices, execution_mode="serial_hardware"
        ).run()
        report = rebuild_report(root)
        _print(
            {
                "campaign_root": str(root),
                "status": state["status"],
                "report": report,
                "discovery_failures": failures,
            }
        )
    finally:
        _close(devices)


def _status(args: argparse.Namespace) -> None:
    config = _config(args)
    root = campaign_root(config, args.campaign)
    _print(campaign_status(root))


def _report(args: argparse.Namespace) -> None:
    config = _config(args)
    root = campaign_root(config, args.campaign)
    verify_campaign(root, config)
    _print({"campaign_root": str(root), **rebuild_report(root)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m allocator_replay",
        description="Unrestricted Collaborative Visit HIL for reallocation coalescing",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    discovery = commands.add_parser("discover", help="auto-discover motor-free replay boards")
    discovery.add_argument("--ports", nargs="+", default=["auto"])
    discovery.set_defaults(handler=_discover)

    build = commands.add_parser("build-device", help="build collaborative-only device modules")
    build.add_argument("--ports", nargs="+", default=None)
    build.add_argument("--compatibility", default="1.24")
    build.add_argument("--optimize", type=int, default=0)
    build.add_argument("--source-only", action="store_true")
    build.set_defaults(handler=_build)

    deployment = commands.add_parser("deploy", help="deploy replay modules without touching main.py")
    deployment.add_argument("--ports", nargs="+", default=["auto"])
    deployment.add_argument("--build", default=None)
    deployment.set_defaults(handler=_deploy)

    preflight = commands.add_parser("preflight", help="probe all six unrestricted allocators")
    preflight.add_argument("--ports", nargs="+", default=["auto"])
    preflight.add_argument("--build", default=None)
    preflight.set_defaults(handler=_preflight)

    for name, handler, help_text in (
        ("hil-validate", _validate, "validate config and every paired manifest hash"),
        ("hil-status", _status, "show resumable campaign state"),
        ("hil-report", _report, "rebuild tidy HIL timing reports"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", required=True)
        command.add_argument("--campaign", default=None)
        command.set_defaults(handler=handler)

    prepare = commands.add_parser(
        "hil-prepare",
        help="freeze a schedule against a live preflighted hardware identity",
    )
    prepare.add_argument("--config", required=True)
    prepare.add_argument("--campaign", default=None)
    prepare.add_argument("--ports", nargs="+", default=["auto"])
    prepare.set_defaults(handler=_prepare)

    dry = commands.add_parser("hil-dry-run", help="run the full serial protocol in software")
    dry.add_argument("--config", required=True)
    dry.add_argument("--campaign", default=None)
    dry.add_argument("--devices", type=int, choices=(1, 2, 3), default=1)
    dry.set_defaults(handler=_dry_run)

    run = commands.add_parser("hil-run", help="run/resume on preflighted RP2040 boards")
    run.add_argument("--config", required=True)
    run.add_argument("--campaign", default=None)
    run.add_argument("--ports", nargs="+", default=["auto"])
    run.set_defaults(handler=_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except Exception as exc:
        parser.exit(1, f"error: {type(exc).__name__}: {exc}\n")
    return 0
