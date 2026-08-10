"""Read-only AGX Orin environment/provenance inspection."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from study.manifests import canonical_json_bytes, sha256_file

from .model import PUBLICATION_WORKER_COUNT, CausalConfig, load_causal_config
from .freeze import _source_hash


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip().strip("\x00")
    # Some Jetson virtual thermal zones intermittently return EAGAIN through
    # the text decoder as a TypeError (the raw read yields ``None``).  A
    # missing instantaneous sensor sample is provenance data, not a reason to
    # crash the environment gate.
    except (OSError, TypeError):
        return None


def _run(command: list[str], timeout_s: float = 5.0) -> dict[str, Any]:
    executable = shutil.which(command[0])
    if executable is None:
        return {"command": command, "available": False, "returncode": None, "stdout": "", "stderr": ""}
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        return {
            "command": command,
            "available": True,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "command": command,
            "available": True,
            "returncode": None,
            "stdout": "",
            "stderr": f"{type(error).__name__}: {error}",
        }


def _git(repo_root: Path) -> dict[str, Any]:
    head = _run(["git", "-C", str(repo_root), "rev-parse", "HEAD"])
    status = _run(["git", "-C", str(repo_root), "status", "--porcelain=v1", "--untracked-files=all"])
    return {
        "head": head["stdout"] if head["returncode"] == 0 else None,
        "dirty": bool(status["stdout"]) if status["returncode"] == 0 else None,
        "status_porcelain": status["stdout"],
        "head_probe": head,
        "status_probe": status,
    }


def _thermal() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    root = Path("/sys/class/thermal")
    if not root.is_dir():
        return rows
    for zone in sorted(root.glob("thermal_zone*")):
        raw = _read_text(zone / "temp")
        try:
            celsius = float(raw) / 1000.0 if raw is not None else None
        except ValueError:
            celsius = None
        rows.append({
            "zone": zone.name,
            "type": _read_text(zone / "type"),
            "temperature_c": celsius,
        })
    return rows


def _cpu_state(core_ids: Iterable[int]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for core_id in core_ids:
        root = Path(f"/sys/devices/system/cpu/cpu{core_id}/cpufreq")
        rows.append({
            "core_id": core_id,
            "governor": _read_text(root / "scaling_governor"),
            "current_khz": _read_text(root / "scaling_cur_freq"),
            "min_khz": _read_text(root / "scaling_min_freq"),
            "max_khz": _read_text(root / "scaling_max_freq"),
        })
    return rows


def inspect_environment(
    config: CausalConfig,
    *,
    allow_non_agx: bool = False,
    accept_recorded_power_clock_state: bool = False,
) -> dict[str, Any]:
    """Collect provenance without changing clocks, power modes, or permissions."""

    logical_cores = os.cpu_count() or 0
    model = _read_text(Path("/proc/device-tree/model"))
    l4t = _read_text(Path("/etc/nv_tegra_release"))
    machine = platform.machine()
    is_linux = sys.platform.startswith("linux")
    is_agx_orin = bool(model and "AGX Orin" in model)
    board_rows: list[dict[str, Any]] = []
    for binding in config.boards:
        device = Path(binding.serial_device)
        exists = device.exists()
        board_rows.append({
            **binding.to_dict(),
            "serial_path_exists": exists,
            "serial_readable": os.access(device, os.R_OK) if exists else False,
            "serial_writable": os.access(device, os.W_OK) if exists else False,
            "identity_verified": False,
            "note": "Identity/build verification is performed by RP2040 preflight, not this read-only check.",
        })
    disk = shutil.disk_usage(config.repo_root)
    git = _git(config.repo_root)
    nvpmodel = _run(["nvpmodel", "-q"])
    jetson_clocks = _run(["jetson_clocks", "--show"])
    checks = {
        "linux": is_linux,
        "agx_orin_model": is_agx_orin,
        "at_least_four_logical_cores": logical_cores >= 4,
        "publication_workers_within_75_percent_logical_core_cap": (
            PUBLICATION_WORKER_COUNT <= math.floor(0.75 * logical_cores)
        ),
        "publication_worker_affinities_are_distinct": (
            len(config.core_affinities) == PUBLICATION_WORKER_COUNT
            and len(set(config.core_affinities)) == PUBLICATION_WORKER_COUNT
        ),
        "affinities_exist": all(0 <= core < logical_cores for core in config.core_affinities),
        "exact_publication_board_bindings": (
            len(config.boards) == PUBLICATION_WORKER_COUNT
        ),
        "serial_paths_present_and_rw": (
            len(board_rows) == PUBLICATION_WORKER_COUNT
            and all(
            row["serial_path_exists"] and row["serial_readable"] and row["serial_writable"]
            for row in board_rows
            )
        ),
        "repository_clean": git["dirty"] is False,
        "manifest_index_hash_matches_config_load": (
            sha256_file(config.manifest_root / "manifest_index.json") == config.manifest_index_sha256
        ),
        "minimum_free_storage_5_gib": disk.free >= 5 * 1024**3,
        "nvpmodel_probe_succeeded": nvpmodel["returncode"] == 0,
        "jetson_clocks_probe_succeeded": jetson_clocks["returncode"] == 0,
        "recorded_power_clock_state_explicitly_accepted": bool(
            accept_recorded_power_clock_state
        ),
    }
    scientifically_valid = all(checks.values())
    development_pass = allow_non_agx and all(
        value for name, value in checks.items()
        if name not in {
            "linux", "agx_orin_model", "serial_paths_present_and_rw",
            "exact_publication_board_bindings", "nvpmodel_probe_succeeded",
            "jetson_clocks_probe_succeeded",
            "recorded_power_clock_state_explicitly_accepted",
        }
    )
    return {
        "schema_version": 1,
        "report_kind": "native_environment_check",
        "generated_at": utc_now(),
        "config_path": str(config.path),
        "config_sha256": config.config_sha256,
        "hardware_binding_sha256": config.hardware_binding_sha256,
        "manifest_index_path": str(config.manifest_root / "manifest_index.json"),
        "manifest_index_sha256": config.manifest_index_sha256,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": machine,
            "python": sys.version,
            "logical_cpu_count": logical_cores,
            "agx_model": model,
            "l4t_release": l4t,
        },
        "selected_core_affinities": list(config.core_affinities),
        "selected_cpu_state": _cpu_state(config.core_affinities),
        "thermal_zones": _thermal(),
        "memory": _run(["free", "-b"]),
        "storage": {
            "path": str(config.repo_root),
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "df": _run(["df", "-B1", str(config.repo_root)]),
        },
        "nvpmodel": nvpmodel,
        "jetson_clocks": jetson_clocks,
        "power_clock_state_operator_accepted": bool(
            accept_recorded_power_clock_state
        ),
        "lsusb": _run(["lsusb"]),
        "serial_bindings": board_rows,
        "git": git,
        "source_tree_sha256": _source_hash(config.repo_root),
        "checks": checks,
        "scientifically_valid": scientifically_valid,
        "development_only_pass": bool(development_pass and not scientifically_valid),
        "hardware_validation_performed": False,
        "system_settings_changed": False,
        "operator_note": (
            "This command is read-only. If the recorded nvpmodel/governor/clock state is not fixed, "
            "choose and document a mode before timing; this tool never invokes sudo or changes it."
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    status = "PASS" if report["scientifically_valid"] else "FAIL"
    checks = "\n".join(
        f"- [{'x' if passed else ' '}] {name}: {'PASS' if passed else 'FAIL'}"
        for name, passed in report["checks"].items()
    )
    boards = "\n".join(
        f"- `{row['board_id']}` -> `{row['serial_device']}`; path/RW="
        f"{row['serial_path_exists']}/{row['serial_readable']}/{row['serial_writable']}; "
        "identity pending preflight"
        for row in report["serial_bindings"]
    )
    platform_row = report["platform"]
    return f"""# Native AGX Environment Report

Overall scientific gate: **{status}**

Generated: `{report['generated_at']}`
Config SHA-256: `{report['config_sha256']}`
Manifest-index SHA-256: `{report['manifest_index_sha256']}`

## Platform

- Model: `{platform_row['agx_model']}`
- System/machine: `{platform_row['system']} / {platform_row['machine']}`
- Kernel: `{platform_row['release']}`
- L4T: `{platform_row['l4t_release']}`
- Logical CPUs: `{platform_row['logical_cpu_count']}`
- Selected worker cores: `{report['selected_core_affinities']}`

## Checks

{checks}

## Configured boards

{boards}

## Power and clocks

`nvpmodel -q` return code: `{report['nvpmodel']['returncode']}`

```text
{report['nvpmodel']['stdout'] or report['nvpmodel']['stderr']}
```

`jetson_clocks --show` return code: `{report['jetson_clocks']['returncode']}`

```text
{report['jetson_clocks']['stdout'] or report['jetson_clocks']['stderr']}
```

No system setting was changed. Board identity, build, firmware, timer, and parity
are deliberately deferred to the fail-closed RP2040 preflight.
"""


def write_report(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "native_environment_report.json"
    md_path = output_dir / "NATIVE_ENVIRONMENT_REPORT.md"
    current = canonical_json_bytes(report)
    if json_path.exists() and json_path.read_bytes() != current:
        prior = json_path.read_bytes()
        history = output_dir / "history"
        history.mkdir(parents=True, exist_ok=True)
        suffix = hashlib.sha256(prior).hexdigest()[:12]
        archived = history / f"native_environment_report_{suffix}.json"
        if not archived.exists():
            shutil.copy2(json_path, archived)
        if md_path.exists():
            archived_md = history / f"NATIVE_ENVIRONMENT_REPORT_{suffix}.md"
            if not archived_md.exists():
                shutil.copy2(md_path, archived_md)
    json_path.write_bytes(current)
    md_path.write_text(render_markdown(report), encoding="utf-8", newline="\n")
    return json_path, md_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--allow-non-agx-development", action="store_true")
    parser.add_argument("--accept-recorded-power-clock-state", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_causal_config(args.config, args.repo_root)
    report = inspect_environment(
        config,
        allow_non_agx=args.allow_non_agx_development,
        accept_recorded_power_clock_state=args.accept_recorded_power_clock_state,
    )
    json_path, md_path = write_report(report, args.output_dir.resolve())
    print(f"environment JSON: {json_path}")
    print(f"environment Markdown: {md_path}")
    if report["scientifically_valid"]:
        return 0
    if args.allow_non_agx_development and report["development_only_pass"]:
        print("development-only pass; NOT scientifically valid hardware evidence")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
