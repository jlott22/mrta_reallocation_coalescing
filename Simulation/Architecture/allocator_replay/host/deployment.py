from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterable

from allocator_replay.config.study import DEVICE_BUILD_ROOT


def latest_build() -> Path:
    candidates = [
        path
        for path in DEVICE_BUILD_ROOT.glob("micropython_*")
        if (path / "manifest.json").exists()
    ]
    if not candidates:
        raise FileNotFoundError(
            "no device build exists; run `python -m allocator_replay build-device`"
        )
    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_build(build_root: Path | None = None) -> tuple[Path, dict[str, object]]:
    root = (build_root or latest_build()).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)
    return root, json.loads(manifest_path.read_text(encoding="utf-8"))


def deploy(
    ports: Iterable[str],
    *,
    build_root: Path | None = None,
) -> list[dict[str, object]]:
    executable = shutil.which("mpremote")
    if executable is None:
        raise RuntimeError(
            "mpremote is required; install allocator_replay/requirements-host.txt"
        )
    root, manifest = load_build(build_root)
    modules = sorted(root.glob("*.mpy"))
    if not modules:
        raise RuntimeError(f"no compiled modules in {root}")
    if any(module.name == "main.mpy" for module in modules):
        raise RuntimeError("replay build must never contain main.mpy")
    results: list[dict[str, object]] = []
    for port in ports:
        uploaded: list[str] = []
        for module in modules:
            command = [
                executable,
                "connect",
                str(port),
                "fs",
                "cp",
                str(module),
                f":{module.name}",
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"deploy failed on {port} for {module.name}: "
                    f"{completed.stdout}{completed.stderr}"
                )
            uploaded.append(module.name)
        results.append(
            {
                "port": str(port),
                "build_id": manifest["build_id"],
                "uploaded": uploaded,
                "main_py_changed": False,
            }
        )
    return results
