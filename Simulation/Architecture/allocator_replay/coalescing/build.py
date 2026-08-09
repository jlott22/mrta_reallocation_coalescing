"""Build a motor-free device bundle containing only collaborative allocators."""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from .config import REPOSITORY_ROOT


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
COMMON_SOURCE = PACKAGE_ROOT / "device" / "common"
NATIVE_SOURCE = PACKAGE_ROOT / "device" / "native" / "collaborative"
PHYSICAL_SOURCE = PACKAGE_ROOT / "device" / "physical"
DEVICE_BUILD_ROOT = PACKAGE_ROOT / "builds"
COMMON_MODULES = (
    "replay_fingerprint.py",
    "replay_types.py",
    "replay_runtime.py",
    "replay_compat.py",
    "replay_random.py",
    "replay_hashlib.py",
    "replay_codec.py",
    "replay_robot.py",
    "replay_persistent.py",
    "replay_worker.py",
)
NATIVE_MODULES = (
    "compat.py",
    "state.py",
    "base.py",
    "cbaa.py",
    "acbba.py",
    "pi.py",
    "hipc.py",
    "dmchba.py",
    "dga.py",
    "runtime.py",
)
PHYSICAL_MODULES = ("adapter.py", "factory.py")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _module_set_sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


class _MicroPythonTransform(ast.NodeTransformer):
    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.annotation = None
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        self.generic_visit(node)
        node.returns = None
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AsyncFunctionDef:
        self.generic_visit(node)
        node.returns = None
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.Assign | ast.Pass:
        self.generic_visit(node)
        if node.value is None:
            return ast.copy_location(ast.Pass(), node)
        return ast.copy_location(ast.Assign(targets=[node.target], value=node.value), node)

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if (
            isinstance(node.op, ast.Mult)
            and isinstance(node.left, ast.Call)
            and isinstance(node.left.func, ast.Name)
            and node.left.func.id == "array"
            and len(node.left.args) >= 2
        ):
            node.left.args[1] = ast.BinOp(
                left=node.left.args[1], op=ast.Mult(), right=node.right
            )
            return ast.copy_location(node.left, node)
        return node


def _strip_annotations(text: str) -> str:
    tree = _MicroPythonTransform().visit(ast.parse(text))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _native_module(source: Path) -> str:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    filtered: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.level != 1 or not node.module:
                raise ValueError(f"unsupported relative import in {source}")
            node.level = 0
            node.module = "replay_native_c_" + node.module
        filtered.append(node)
    tree.body = filtered
    tree = _MicroPythonTransform().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _compile(source: Path, compatibility: str, optimize: int) -> Path:
    executable = shutil.which("mpy-cross")
    if executable is None:
        raise RuntimeError("mpy-cross is required for a deployable device build")
    completed = subprocess.run(
        [executable, "-c", compatibility, f"-O{optimize}", str(source)],
        cwd=source.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"mpy-cross failed for {source.name}: {completed.stdout}{completed.stderr}"
        )
    target = source.with_suffix(".mpy")
    if not target.is_file():
        raise RuntimeError(f"mpy-cross did not create {target}")
    return target


def build_device_bundle(
    *, compatibility: str = "1.24", optimize: int = 0, compile_mpy: bool = True
) -> dict[str, object]:
    family = (
        f"micropython_{compatibility.replace('.', '_')}_o{optimize}_"
        "coalescing_collaborative"
    )
    if not compile_mpy:
        family += "_source"
    output = DEVICE_BUILD_ROOT / family
    output.mkdir(parents=True, exist_ok=True)
    if output.resolve().parent != DEVICE_BUILD_ROOT.resolve():
        raise RuntimeError("device build escaped allocator_replay/builds")
    for stale in list(output.glob("replay_*.py")) + list(output.glob("replay_*.mpy")):
        stale.unlink()
    manifest_path = output / "manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()

    sources: list[Path] = []
    provenance: dict[str, str] = {}
    for filename in COMMON_MODULES:
        source = COMMON_SOURCE / filename
        target = output / filename
        shutil.copy2(source, target)
        sources.append(target)
        provenance[str(source.resolve().relative_to(REPOSITORY_ROOT))] = _sha256(source)
    for filename in NATIVE_MODULES:
        source = NATIVE_SOURCE / filename
        target = output / f"replay_native_c_{source.stem}.py"
        _write(target, _native_module(source))
        sources.append(target)
        provenance[str(source.resolve().relative_to(REPOSITORY_ROOT))] = _sha256(source)
    for filename in PHYSICAL_MODULES:
        source = PHYSICAL_SOURCE / filename
        target = output / f"replay_physical_{source.stem}.py"
        _write(target, _strip_annotations(source.read_text(encoding="utf-8")))
        sources.append(target)
        provenance[str(source.resolve().relative_to(REPOSITORY_ROOT))] = _sha256(source)

    source_hash = _module_set_sha256(sources)
    build_id = f"{family}_{source_hash[:12]}"
    compiled: list[Path] = []
    if compile_mpy:
        compiled = [_compile(path, compatibility, optimize) for path in sources]
    module_hash = _module_set_sha256(compiled) if compiled else ""
    source_module_names = tuple(sorted(path.name for path in sources))
    module_names = tuple(sorted(path.with_suffix(".mpy").name for path in sources))
    build_module = output / "replay_build.py"
    _write(
        build_module,
        "\n".join(
            (
                f'BUILD_ID = "{build_id}"',
                f'COMPATIBILITY = "{compatibility}"',
                f'SOURCE_BUNDLE_SHA256 = "{source_hash}"',
                f'MODULE_SET_SHA256 = "{module_hash}"',
                f"MODULE_FILES = {module_names!r}",
                'STUDY_ID = "mrta_reallocation_coalescing"',
                'MISSION = "collaborative"',
                'CANDIDATE_MODE = "unrestricted"',
                "",
            )
        ),
    )
    sources.append(build_module)
    if compile_mpy:
        compiled.append(_compile(build_module, compatibility, optimize))

    manifest: dict[str, object] = {
        "schema": 3,
        "study_id": "mrta_reallocation_coalescing",
        "mission": "collaborative",
        "candidate_mode": "unrestricted",
        "max_candidate_cells": None,
        "contains_bayesian": False,
        "contains_top_k_conditions": False,
        "build_id": build_id,
        "output": str(output.resolve()),
        "compatibility": compatibility,
        "optimization": optimize,
        "compiled": compile_mpy,
        "source_bundle_sha256": source_hash,
        "deployed_module_set_sha256": module_hash,
        "source_module_files": list(source_module_names),
        "deployed_module_files": list(module_names),
        "source_provenance": provenance,
        "files": {
            path.name: {"sha256": _sha256(path), "bytes": path.stat().st_size}
            for path in sorted(sources + compiled)
        },
        "safety": {
            "imports_existing_repositories": False,
            "initializes_motors": False,
            "initializes_sensors": False,
            "overwrites_main_py": False,
            "legacy_allocator_modules_included": False,
            "persistent_factory_collaborative_only": True,
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def verify_device_build(
    build_root: Path, *, require_compiled: bool | None = None
) -> dict[str, object]:
    """Re-hash a build, its module set, and every source input.

    A cached manifest is evidence only after these checks pass. This catches a
    stale preflight after code edits as well as modified ``.mpy`` files in a
    previously prepared deployment directory.
    """

    root = Path(build_root).resolve()
    try:
        root.relative_to(DEVICE_BUILD_ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError(f"device build must be inside {DEVICE_BUILD_ROOT}") from exc
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(manifest.get("schema", 0)) != 3:
        raise RuntimeError("stale device build manifest schema; rebuild the bundle")
    if Path(str(manifest.get("output", ""))).resolve() != root:
        raise RuntimeError("device build manifest output path mismatch")
    if require_compiled is not None and bool(manifest.get("compiled")) != bool(
        require_compiled
    ):
        kind = "compiled" if require_compiled else "source-only"
        raise RuntimeError(f"expected a {kind} device build")
    if manifest.get("study_id") != "mrta_reallocation_coalescing":
        raise RuntimeError("device build belongs to another study")
    if manifest.get("mission") != "collaborative":
        raise RuntimeError("device build is not collaborative-only")
    if manifest.get("candidate_mode") != "unrestricted":
        raise RuntimeError("device build applies a candidate restriction")
    if manifest.get("contains_bayesian") is not False:
        raise RuntimeError("device build may contain Bayesian allocators")
    safety = manifest.get("safety", {})
    required_safety = {
        "imports_existing_repositories": False,
        "initializes_motors": False,
        "initializes_sensors": False,
        "overwrites_main_py": False,
        "legacy_allocator_modules_included": False,
        "persistent_factory_collaborative_only": True,
    }
    if any(safety.get(key) is not value for key, value in required_safety.items()):
        raise RuntimeError("device build safety declaration is incomplete or unsafe")

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise RuntimeError("device build contains no sealed file inventory")
    for name, record in files.items():
        if Path(str(name)).name != str(name) or str(name) in {".", ".."}:
            raise RuntimeError(f"unsafe build inventory filename: {name}")
        path = root / str(name)
        if not path.is_file():
            raise RuntimeError(f"sealed device module is missing: {name}")
        if _sha256(path) != str(record.get("sha256", "")):
            raise RuntimeError(f"sealed device module hash mismatch: {name}")
        if path.stat().st_size != int(record.get("bytes", -1)):
            raise RuntimeError(f"sealed device module size mismatch: {name}")
    if any(str(name).lower() in {"main.py", "main.mpy"} for name in files):
        raise RuntimeError("device build must never contain main.py/main.mpy")

    source_names = [str(item) for item in manifest.get("source_module_files", ())]
    source_paths = [root / item for item in source_names]
    if not source_paths or any(not item.is_file() for item in source_paths):
        raise RuntimeError("device build source module inventory is incomplete")
    if _module_set_sha256(source_paths) != manifest.get("source_bundle_sha256"):
        raise RuntimeError("device source bundle hash mismatch")
    deployed_names = [
        str(item) for item in manifest.get("deployed_module_files", ())
    ]
    compiled = bool(manifest.get("compiled"))
    if compiled:
        deployed_paths = [root / item for item in deployed_names]
        if not deployed_paths or any(not item.is_file() for item in deployed_paths):
            raise RuntimeError("compiled device module inventory is incomplete")
        if _module_set_sha256(deployed_paths) != manifest.get(
            "deployed_module_set_sha256"
        ):
            raise RuntimeError("compiled device module-set hash mismatch")
    elif manifest.get("deployed_module_set_sha256") not in ("", None):
        raise RuntimeError("source-only build claims a deployed module hash")

    provenance = manifest.get("source_provenance")
    if not isinstance(provenance, dict) or not provenance:
        raise RuntimeError("device build has no source provenance")
    for relative, expected in provenance.items():
        source = (REPOSITORY_ROOT / str(relative)).resolve()
        try:
            source.relative_to(REPOSITORY_ROOT.resolve())
        except ValueError as exc:
            raise RuntimeError(f"source provenance escaped repository: {relative}") from exc
        if not source.is_file() or _sha256(source) != str(expected):
            raise RuntimeError(
                f"device build is stale relative to source: {relative}; rebuild"
            )
    expected_suffix = str(manifest["source_bundle_sha256"])[:12]
    if not str(manifest.get("build_id", "")).endswith(expected_suffix):
        raise RuntimeError("device build_id does not bind the source bundle")
    return manifest


def latest_device_build(*, compiled: bool) -> tuple[Path, dict[str, object]]:
    candidates: list[tuple[Path, dict[str, object]]] = []
    for path in DEVICE_BUILD_ROOT.glob("micropython_*_coalescing_collaborative*"):
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if bool(manifest.get("compiled")) != bool(compiled):
            continue
        candidates.append((path.resolve(), manifest))
    if not candidates:
        kind = "compiled" if compiled else "source-only"
        raise FileNotFoundError(f"no {kind} coalescing device build exists")
    root, _ = max(candidates, key=lambda item: item[0].stat().st_mtime)
    return root, verify_device_build(root, require_compiled=compiled)


def validate_built_imports(build_root: Path) -> None:
    root = Path(build_root).resolve()
    sys.path.insert(0, str(root))
    imported: list[str] = []
    try:
        importlib.invalidate_caches()
        runtime = importlib.import_module("replay_native_c_runtime")
        imported.append("replay_native_c_runtime")
        factory = importlib.import_module("replay_physical_factory")
        imported.append("replay_physical_factory")
        adapter = importlib.import_module("replay_physical_adapter")
        imported.append("replay_physical_adapter")
        worker = importlib.import_module("replay_worker")
        imported.append("replay_worker")
        getattr(runtime, "create_persistent_runtime")
        getattr(factory, "create_complete_runtime")
        getattr(adapter, "PhysicalAllocatorAdapter")
        getattr(worker, "PersistentRuntimeSlot")
    finally:
        sys.path.remove(str(root))
        for name in imported:
            sys.modules.pop(name, None)
