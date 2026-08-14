"""Explicit native RP2040 build, deployment, binding, and preflight commands."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from study.manifests import canonical_json_bytes

from .model import PUBLICATION_WORKER_COUNT, load_causal_config
from .locks import LockRecoveryError, recover_stale_locks


def _imports(repo_root: Path) -> dict[str, Any]:
    architecture = repo_root / "Simulation" / "Architecture"
    if str(architecture) not in sys.path:
        sys.path.insert(0, str(architecture))
    from allocator_replay import causal
    from allocator_replay.coalescing.build import build_device_bundle, verify_device_build
    from allocator_replay.host.deployment import deploy
    from allocator_replay.host.discovery import discover
    from allocator_replay.host.transport import SerialReplayDevice
    return {
        "causal": causal,
        "build_device_bundle": build_device_bundle,
        "verify_device_build": verify_device_build,
        "deploy": deploy,
        "discover": discover,
        "SerialReplayDevice": SerialReplayDevice,
    }


def _write_immutable(path: Path, value: Any) -> None:
    data = canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() != data:
        raise FileExistsError(
            f"refusing to replace different identity/provenance data: {path}; "
            "archive it explicitly before rebinding"
        )
    if not path.exists():
        path.write_bytes(data)


def _build_manifest(imports: dict[str, Any], build_root: Path | None) -> tuple[Path, dict[str, Any]]:
    if build_root is None:
        manifest = imports["build_device_bundle"](compile_mpy=True)
        root = Path(manifest["output"])
    else:
        root = build_root.resolve()
        manifest = imports["verify_device_build"](root, require_compiled=True)
    return root, dict(manifest)


def build_and_optionally_deploy(
    repo_root: Path,
    *,
    ports: list[str],
    build_root: Path | None,
    deploy_requested: bool,
    output: Path,
) -> dict[str, Any]:
    imports = _imports(repo_root)
    root, manifest = _build_manifest(imports, build_root)
    deployments: list[dict[str, Any]] = []
    if deploy_requested:
        if (
            len(ports) != PUBLICATION_WORKER_COUNT
            or len(set(ports)) != PUBLICATION_WORKER_COUNT
        ):
            raise ValueError(
                "deployment requires exactly "
                f"{PUBLICATION_WORKER_COUNT} distinct explicit ports"
            )
        deployments = imports["deploy"](ports, build_root=root)
    report = {
        "schema_version": 1,
        "report_kind": "causal_device_build_deployment",
        "build_root": str(root),
        "build_manifest": manifest,
        "deploy_requested": deploy_requested,
        "deployments": deployments,
        "motors_or_sensors_initialized": False,
        "main_py_changed": False,
    }
    _write_immutable(output, report)
    return report


def discover_bindings(
    repo_root: Path,
    *,
    ports: list[str] | None,
    build_root: Path,
    core_affinities: list[int],
    output: Path,
) -> dict[str, Any]:
    imports = _imports(repo_root)
    manifest = dict(imports["verify_device_build"](build_root.resolve(), require_compiled=True))
    selected_ports = ports
    if selected_ports is None:
        discovered, failures = imports["discover"]("auto")
        if failures:
            print(json.dumps({"skipped_or_failed_ports": failures}, indent=2), file=sys.stderr)
        selected_ports = [device.port for device in discovered]
    if (
        len(selected_ports) != PUBLICATION_WORKER_COUNT
        or len(set(selected_ports)) != PUBLICATION_WORKER_COUNT
    ):
        raise RuntimeError(
            "binding requires exactly "
            f"{PUBLICATION_WORKER_COUNT} unique intended RP2040 ports"
        )
    if (
        len(core_affinities) != PUBLICATION_WORKER_COUNT
        or len(set(core_affinities)) != PUBLICATION_WORKER_COUNT
        or min(core_affinities) < 0
    ):
        raise ValueError(
            f"exactly {PUBLICATION_WORKER_COUNT} distinct nonnegative core "
            "affinities are required"
        )
    devices = [imports["SerialReplayDevice"](port) for port in selected_ports]
    try:
        bindings = imports["causal"].bind_hardware_workers(
            devices,
            expected_build_id=str(manifest["build_id"]),
            expected_module_set_sha256=str(manifest["deployed_module_set_sha256"]),
        )
        labels = tuple(
            f"rp2040_{chr(ord('a') + index)}"
            for index in range(PUBLICATION_WORKER_COUNT)
        )
        rows = []
        timer_evidence = []
        for label, binding in zip(labels, bindings, strict=True):
            fingerprint = binding.fingerprint
            rows.append({
                "board_id": label,
                "serial_device": fingerprint.port,
                "expected_device_uid": fingerprint.device_id,
                "expected_build_id": fingerprint.build_id,
                "expected_firmware_sha256": fingerprint.firmware_sha256,
                "expected_module_set_sha256": fingerprint.module_set_sha256,
            })
            timer_evidence.append({
                "board_id": label,
                "device_uid": fingerprint.device_id,
                "implementation": fingerprint.implementation,
                "frequency_hz": fingerprint.frequency_hz,
                "timer_unit": fingerprint.timer_unit,
                "timer_resolution_us": fingerprint.timer_resolution_us,
                "timer_monotonic": fingerprint.timer_monotonic,
                "timer_wraparound_safe": fingerprint.timer_wraparound_safe,
            })
        value = {
            "schema_version": 1,
            "generated_by": "study.causal.native discover-bindings",
            "core_affinities": core_affinities,
            "build_id": manifest["build_id"],
            "module_set_sha256": manifest["deployed_module_set_sha256"],
            "boards": rows,
            "timer_evidence_at_binding": timer_evidence,
        }
        _write_immutable(output, value)
        return value
    finally:
        for device in devices:
            device.close()


def native_preflight(
    config_path: Path,
    repo_root: Path,
    json_path: Path,
    markdown_path: Path,
) -> dict[str, Any]:
    config = load_causal_config(config_path, repo_root)
    if len(config.boards) != PUBLICATION_WORKER_COUNT or config.development_override:
        raise RuntimeError(
            "native preflight requires exactly "
            f"{PUBLICATION_WORKER_COUNT} non-development bindings"
        )
    imports = _imports(repo_root)
    devices = [imports["SerialReplayDevice"](board.serial_device) for board in config.boards]
    try:
        mapping = {index: board.expected_device_uid for index, board in enumerate(config.boards)}
        build_ids = {board.expected_build_id for board in config.boards}
        module_hashes = {board.expected_module_set_sha256 for board in config.boards}
        firmware = {board.expected_device_uid: board.expected_firmware_sha256 for board in config.boards}
        if len(build_ids) != 1 or len(module_hashes) != 1:
            raise RuntimeError(
                "all primary timing boards must use one build/module set"
            )
        helper = getattr(imports["causal"], "run_native_preflight", None)
        if not callable(helper):
            raise RuntimeError(
                "installed causal device layer has no automated run_native_preflight; "
                "no check may be manually assumed PASS"
            )
        commit = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        report = helper(
            devices,
            expected_build_id=next(iter(build_ids)),
            expected_module_set_sha256=next(iter(module_hashes)),
            expected_firmware_sha256=firmware,
            explicit_mapping=mapping,
            reconnect_factory=lambda fingerprint: imports["SerialReplayDevice"](
                fingerprint.port
            ),
            repository_commit=commit,
            config_sha256=config.config_sha256,
            manifest_sha256=config.manifest_index_sha256,
            lock_root=config.repo_root / "study" / "native_device_leases",
            timeout_seconds=config.device_timeout_seconds,
            native_hardware=True,
            json_path=json_path,
            text_path=markdown_path,
        )
        verified = imports["causal"].load_and_verify_preflight(json_path)
        return verified
    finally:
        for device in devices:
            device.close()


def _ports(value: str | None) -> list[str] | None:
    if value is None or value == "auto":
        return None
    ports = [item.strip() for item in value.split(",") if item.strip()]
    return ports


def _cores(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build-deploy")
    build.add_argument(
        "--ports",
        help=f"{PUBLICATION_WORKER_COUNT} comma-separated explicit ports",
    )
    build.add_argument("--build-root", type=Path)
    build.add_argument(
        "--deploy",
        action="store_true",
        help="explicitly write compiled modules to every publication board",
    )
    build.add_argument("--output", type=Path, required=True)
    binding = sub.add_parser("discover-bindings")
    binding.add_argument("--ports", default="auto")
    binding.add_argument("--build-root", type=Path, required=True)
    binding.add_argument("--core-affinities", default="0,1,2,3")
    binding.add_argument("--output", type=Path, required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--config", type=Path, required=True)
    preflight.add_argument("--json", type=Path, required=True)
    preflight.add_argument("--markdown", type=Path, required=True)
    recovery = sub.add_parser(
        "recover-stale-locks",
        help="remove only configured local lock files whose owner PID is proven dead",
    )
    recovery.add_argument("--config", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.repo_root.resolve()
    if args.command == "build-deploy":
        ports = _ports(args.ports) or []
        report = build_and_optionally_deploy(
            root, ports=ports, build_root=args.build_root,
            deploy_requested=args.deploy, output=args.output.resolve(),
        )
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    if args.command == "discover-bindings":
        value = discover_bindings(
            root,
            ports=_ports(args.ports),
            build_root=args.build_root,
            core_affinities=_cores(args.core_affinities),
            output=args.output.resolve(),
        )
        print(json.dumps(value, indent=2, sort_keys=True))
        return 0
    if args.command == "recover-stale-locks":
        config = load_causal_config(args.config.resolve(), root)
        try:
            report = recover_stale_locks(config)
        except LockRecoveryError as error:
            print(json.dumps({
                "schema_version": 1,
                "report_kind": "causal_stale_lock_recovery",
                "passed": False,
                "error": str(error),
            }, indent=2, sort_keys=True), file=sys.stderr)
            return 2
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    report = native_preflight(
        args.config.resolve(), root, args.json.resolve(), args.markdown.resolve()
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("hardware_valid") else 2


if __name__ == "__main__":
    raise SystemExit(main())
