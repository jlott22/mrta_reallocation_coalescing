from __future__ import annotations

import ast
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from allocator_replay.config.study import (
    ALGORITHMS,
    BAYESIAN_SIM_ROOT,
    COLLABORATIVE_SIM_ROOT,
    DEVICE_BUILD_ROOT,
    REPOSITORY_ROOT,
)


COMMON_SOURCE = Path(__file__).resolve().parent / "common"
NATIVE_SOURCE = Path(__file__).resolve().parent / "native"
PHYSICAL_SOURCE = Path(__file__).resolve().parent / "physical"
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
NATIVE_COLLABORATIVE_MODULES = (
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
NATIVE_BAYESIAN_MODULES = (
    "dmchba_complete.py",
)
PHYSICAL_MODULES = ("adapter.py", "factory.py")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _module_set_sha256(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(4096), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


class _MicroPythonTransform(ast.NodeTransformer):
    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.annotation = None
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        self.generic_visit(node)
        node.returns = None
        return node

    def visit_AsyncFunctionDef(
        self,
        node: ast.AsyncFunctionDef,
    ) -> ast.AsyncFunctionDef:
        self.generic_visit(node)
        node.returns = None
        return node

    def visit_AnnAssign(
        self,
        node: ast.AnnAssign,
    ) -> ast.Assign | ast.Pass:
        self.generic_visit(node)
        if node.value is None:
            return ast.copy_location(ast.Pass(), node)
        return ast.copy_location(
            ast.Assign(targets=[node.target], value=node.value),
            node,
        )

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if (
            isinstance(node.op, ast.Mult)
            and isinstance(node.left, ast.Call)
            and isinstance(node.left.func, ast.Name)
            and node.left.func.id == "array"
            and len(node.left.args) >= 2
        ):
            # MicroPython's array type does not implement sequence
            # multiplication. Build the repeated initializer list first.
            node.left.args[1] = ast.BinOp(
                left=node.left.args[1],
                op=ast.Mult(),
                right=node.right,
            )
            return ast.copy_location(node.left, node)
        return node

    def visit_Assign(self, node: ast.Assign) -> ast.Assign:
        self.generic_visit(node)
        # CPython accepts any iterable for extended/slice assignment, while
        # MicroPython 1.24 requires a concrete tuple or list.  Both simulator
        # DGA implementations reverse route segments with
        # ``route[start:end] = reversed(route[start:end])``.
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].slice, ast.Slice)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "reversed"
        ):
            node.value = ast.Call(
                func=ast.Name(id="list", ctx=ast.Load()),
                args=[node.value],
                keywords=[],
            )
        return node

    def visit_Expr(self, node: ast.Expr) -> ast.Expr | ast.For:
        self.generic_visit(node)
        call = node.value
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "extend"
            and len(call.args) == 1
            and isinstance(call.args[0], ast.GeneratorExp)
        ):
            # MicroPython 1.24's array.extend requires a buffer object; unlike
            # CPython it rejects both generators and ordinary lists.  Emit
            # append loops, which are compatible and avoid a temporary array.
            generator = call.args[0]
            body: list[ast.stmt] = [
                ast.Expr(
                    value=ast.Call(
                        func=ast.Attribute(
                            value=call.func.value,
                            attr="append",
                            ctx=ast.Load(),
                        ),
                        args=[generator.elt],
                        keywords=[],
                    )
                )
            ]
            for comprehension in reversed(generator.generators):
                for condition in reversed(comprehension.ifs):
                    body = [
                        ast.If(
                            test=condition,
                            body=body,
                            orelse=[],
                        )
                    ]
                body = [
                    ast.For(
                        target=comprehension.target,
                        iter=comprehension.iter,
                        body=body,
                        orelse=[],
                        type_comment=None,
                    )
                ]
            return ast.copy_location(
                body[0],
                node,
            )
        return node


def _strip_annotations(text: str) -> str:
    tree = _MicroPythonTransform().visit(ast.parse(text))
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _native_module(source: Path, *, output_prefix: str) -> str:
    """Flatten one package-relative native module for a Pololu filesystem."""

    tree = ast.parse(source.read_text(encoding="utf-8"))
    filtered: list[ast.stmt] = []
    for node in tree.body:
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
        ):
            continue
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.level != 1 or not node.module:
                raise ValueError(
                    f"unsupported native relative import in {source}: "
                    f"level={node.level} module={node.module!r}"
                )
            node.level = 0
            node.module = f"{output_prefix}_{node.module}"
        filtered.append(node)
    tree.body = filtered
    tree = _MicroPythonTransform().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


def _base_module(source: Path) -> str:
    tree = ast.parse(source.read_text(encoding="utf-8"))
    allocator_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AllocatorBase"
    )
    return _strip_annotations(
        "from math import isfinite\n"
        "from replay_runtime import timed_candidate_filter\n"
        "from replay_types import AllocationDecision, Cell\n\n"
        + ast.unparse(allocator_class)
        + "\n"
    )


def _mapping_helpers() -> str:
    return r'''

def _cim_get(self, key, default=None):
    try:
        return self[key]
    except KeyError:
        return default


def _cim_items(self):
    return [(key, self[key]) for key in self]


def _cim_keys(self):
    return list(iter(self))


def _cim_values(self):
    return [self[key] for key in self]


def _cim_update(self, values=(), **kwargs):
    source = values.items() if hasattr(values, "items") else values
    for key, value in source:
        self[key] = value
    for key, value in kwargs.items():
        self[key] = value


def _cim_setdefault(self, key, default=None):
    try:
        return self[key]
    except KeyError:
        self[key] = default
        return default


def _cim_pop(self, key, default=...):
    try:
        value = self[key]
    except KeyError:
        if default is ...:
            raise
        return default
    del self[key]
    return value


def _cim_contains(self, key):
    try:
        self[key]
        return True
    except (KeyError, TypeError, ValueError):
        return False


CellIndexedMap.get = _cim_get
CellIndexedMap.items = _cim_items
CellIndexedMap.keys = _cim_keys
CellIndexedMap.values = _cim_values
CellIndexedMap.update = _cim_update
CellIndexedMap.setdefault = _cim_setdefault
CellIndexedMap.pop = _cim_pop
CellIndexedMap.__contains__ = _cim_contains
'''


def _micropython_mixin_overrides(algorithm: str) -> str:
    """Replace zero-argument super() calls unsupported by this firmware."""
    if algorithm == "CBAA":
        return '''
def _mp_ensure_cbaa_state(self, robot):
    CBAAReferenceAllocator._ensure_cbaa_state(self, robot)
    self._optimize_cell_maps(robot)

def _mp_reset_cbaa_state(self, robot):
    CBAAReferenceAllocator._reset_cbaa_state(self, robot)
    self._optimize_cell_maps(robot)

CBAAAllocator._ensure_cbaa_state = _mp_ensure_cbaa_state
CBAAAllocator._reset_cbaa_state = _mp_reset_cbaa_state
'''
    if algorithm == "ACBBA":
        return '''
def _mp_ensure_acbba_state(self, robot):
    ACBBAReferenceAllocator._ensure_acbba_state(self, robot)
    self._optimize_cell_maps(robot)

def _mp_reset_acbba_state(self, robot, preserve_deltas=False):
    ACBBAReferenceAllocator._reset_acbba_state(
        self, robot, preserve_deltas=preserve_deltas
    )
    self._optimize_cell_maps(robot)

ACBBAAllocator._ensure_acbba_state = _mp_ensure_acbba_state
ACBBAAllocator._reset_acbba_state = _mp_reset_acbba_state
'''
    if algorithm == "HIPC":
        return '''
def _mp_ensure_hipc_state(self, robot):
    HIPCReferenceAllocator._ensure_hipc_state(self, robot)
    self._optimize_cell_maps(robot)

def _mp_reset_path_state(self, robot):
    HIPCReferenceAllocator._reset_path_state(self, robot)
    self._optimize_cell_maps(robot)

HIPCAllocator._ensure_hipc_state = _mp_ensure_hipc_state
HIPCAllocator._reset_path_state = _mp_reset_path_state
'''
    if algorithm == "PI":
        return '''
def _mp_ensure_pi_state(self, robot):
    PIReferenceAllocator._ensure_pi_state(self, robot)
    self._optimize_cell_maps(robot)

def _mp_reset_pi_state(self, robot):
    PIReferenceAllocator._reset_pi_state(self, robot)
    self._optimize_cell_maps(robot)

def _mp_build_pi_bundle(self, robot):
    self._active_candidate_cache = self._packed_candidate_cells(robot)
    try:
        PIReferenceAllocator._build_bundle(self, robot)
    finally:
        self._active_candidate_cache = None

PIAllocator._ensure_pi_state = _mp_ensure_pi_state
PIAllocator._reset_pi_state = _mp_reset_pi_state
PIAllocator._build_bundle = _mp_build_pi_bundle
'''
    return ""


def _transform_memory(source: Path, base_module: str) -> str:
    text = source.read_text(encoding="utf-8")
    text = text.replace("from __future__ import annotations\n", "")
    text = text.replace(
        "from collections.abc import Iterator, MutableMapping\n",
        "",
    )
    text = "\n".join(
        line for line in text.splitlines() if not line.startswith("from typing import")
    )
    text = text.replace(
        "from benchmark_sim.algorithms.base import timed_candidate_filter",
        f"from {base_module} import timed_candidate_filter",
    )
    text = text.replace("Cell = Tuple[int, int]", "Cell = tuple")
    text = text.replace(
        "class CellIndexedMap(MutableMapping):",
        "class CellIndexedMap:",
    )
    return _strip_annotations(text + _mapping_helpers())


def _transform_algorithm(
    source: Path,
    *,
    mission_prefix: str,
    base_module: str,
) -> str:
    text = source.read_text(encoding="utf-8")
    text = text.replace("from __future__ import annotations\n", "")
    text = "\n".join(
        line for line in text.splitlines() if not line.startswith("from typing import")
    )
    package = "benchmark_sim" if mission_prefix == "b" else "known_visit_sim"
    text = text.replace(
        f"from {package}.algorithms.base import AllocatorBase, timed_candidate_filter",
        f"from {base_module} import AllocatorBase, timed_candidate_filter",
    )
    text = text.replace(
        f"from {package}.algorithms.memory_optimized import ",
        "from replay_memory import ",
    )
    text = text.replace(
        f"from {package}.core.types import AllocationDecision, Cell",
        "from replay_types import AllocationDecision, Cell",
    )
    text = text.replace(
        f"from {package}.core.types import AllocationDecision",
        "from replay_types import AllocationDecision",
    )
    text = text.replace(
        f"from {package}.core.types import Cell",
        "from replay_types import Cell",
    )
    text = text.replace("Cell = Tuple[int, int]", "Cell = tuple")
    text = text.replace("import random", "import replay_random as random")
    text = text.replace("import hashlib", "import replay_hashlib as hashlib")
    text = text.replace(
        "from copy import deepcopy",
        "from replay_compat import deepcopy",
    )
    text = text.replace(
        "from functools import cmp_to_key",
        "from replay_compat import cmp_to_key",
    )
    if mission_prefix == "b":
        text = text.replace(
            "from benchmark_sim.algorithms.DGA_optimized import DGAOptimizedAllocator",
            "from replay_b_dga_optimized import DGAOptimizedAllocator",
        )
        text = text.replace(
            "from benchmark_sim.algorithms.DGA import DGAReferenceAllocator",
            "from replay_b_dga import DGAReferenceAllocator",
        )
    if mission_prefix == "b":
        text += _micropython_mixin_overrides(source.stem)
    return _strip_annotations(text)


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _compile(source: Path, *, compatibility: str, optimize: int) -> Path:
    executable = shutil.which("mpy-cross")
    if executable is None:
        raise RuntimeError("mpy-cross is not installed or not on PATH")
    command = [
        executable,
        "-c",
        compatibility,
        f"-O{optimize}",
        str(source),
    ]
    result = subprocess.run(
        command,
        cwd=source.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"mpy-cross failed for {source.name}: {result.stdout}{result.stderr}"
        )
    output = source.with_suffix(".mpy")
    if not output.exists():
        raise RuntimeError(f"mpy-cross did not create {output}")
    return output


def build_device_bundle(
    *,
    compatibility: str = "1.24",
    optimize: int = 0,
    compile_mpy: bool = True,
) -> dict[str, object]:
    build_family = f"micropython_{compatibility.replace('.', '_')}_o{optimize}"
    output = DEVICE_BUILD_ROOT / build_family
    output.mkdir(parents=True, exist_ok=True)
    if output.resolve().parent != DEVICE_BUILD_ROOT.resolve():
        raise RuntimeError("device build escaped the dedicated build directory")
    for pattern in ("replay_*.py", "replay_*.mpy"):
        for stale in output.glob(pattern):
            stale.unlink()
    for stale_name in ("fingerprint.py", "fingerprint.mpy", "manifest.json"):
        stale = output / stale_name
        if stale.exists():
            stale.unlink()
    for filename in COMMON_MODULES:
        source = COMMON_SOURCE / filename
        if not source.exists():
            raise FileNotFoundError(source)
        shutil.copy2(source, output / filename)
    build_module = output / "replay_build.py"

    bayes_algorithms = BAYESIAN_SIM_ROOT / "benchmark_sim" / "algorithms"
    collab_algorithms = (
        COLLABORATIVE_SIM_ROOT / "known_visit_sim" / "algorithms"
    )
    base_sources = {
        "b": bayes_algorithms / "base.py",
        "c": collab_algorithms / "base.py",
    }
    generated_sources: list[Path] = [
        *(output / name for name in COMMON_MODULES),
    ]
    source_provenance: dict[str, str] = {}
    for filename in PHYSICAL_MODULES:
        source = PHYSICAL_SOURCE / filename
        if not source.exists():
            raise FileNotFoundError(source)
        target = output / f"replay_physical_{source.stem}.py"
        _write_text(
            target,
            _strip_annotations(source.read_text(encoding="utf-8")),
        )
        generated_sources.append(target)
        source_provenance[str(source.resolve())] = _sha256(source)
    native_collaborative_root = NATIVE_SOURCE / "collaborative"
    for filename in NATIVE_COLLABORATIVE_MODULES:
        source = native_collaborative_root / filename
        if not source.exists():
            raise FileNotFoundError(source)
        target = output / f"replay_native_c_{source.stem}.py"
        _write_text(
            target,
            _native_module(source, output_prefix="replay_native_c"),
        )
        generated_sources.append(target)
        source_provenance[str(source.resolve())] = _sha256(source)
    native_bayesian_root = NATIVE_SOURCE / "bayesian"
    for filename in NATIVE_BAYESIAN_MODULES:
        source = native_bayesian_root / filename
        if not source.exists():
            raise FileNotFoundError(source)
        target = output / "replay_native_b_dmchba.py"
        _write_text(
            target,
            _native_module(source, output_prefix="replay_native_b"),
        )
        generated_sources.append(target)
        source_provenance[str(source.resolve())] = _sha256(source)
    for prefix, source in base_sources.items():
        target = output / f"replay_base_{prefix}.py"
        _write_text(target, _base_module(source))
        generated_sources.append(target)
        source_provenance[str(source.resolve())] = _sha256(source)

    memory_target = output / "replay_memory.py"
    _write_text(
        memory_target,
        _transform_memory(
            bayes_algorithms / "memory_optimized.py",
            "replay_base_b",
        ),
    )
    generated_sources.append(memory_target)
    source_provenance[
        str((bayes_algorithms / "memory_optimized.py").resolve())
    ] = _sha256(bayes_algorithms / "memory_optimized.py")

    for mission_prefix, algorithm_root in (
        ("b", bayes_algorithms),
        ("c", collab_algorithms),
    ):
        for algorithm in ALGORITHMS:
            source = algorithm_root / f"{algorithm}.py"
            target = output / f"replay_{mission_prefix}_{algorithm.lower()}.py"
            _write_text(
                target,
                _transform_algorithm(
                    source,
                    mission_prefix=mission_prefix,
                    base_module=f"replay_base_{mission_prefix}",
                ),
            )
            generated_sources.append(target)
            source_provenance[str(source.resolve())] = _sha256(source)
        if mission_prefix == "b":
            optimized_source = algorithm_root / "DGA_optimized.py"
            optimized_target = output / "replay_b_dga_optimized.py"
            _write_text(
                optimized_target,
                _transform_algorithm(
                    optimized_source,
                    mission_prefix="b",
                    base_module="replay_base_b",
                ),
            )
            generated_sources.append(optimized_target)
            source_provenance[str(optimized_source.resolve())] = _sha256(
                optimized_source
            )

    source_bundle_sha256 = _module_set_sha256(generated_sources)
    build_id = f"{build_family}_{source_bundle_sha256[:12]}"
    compiled: list[Path] = []
    deployable_modules = [
        source.with_suffix(".mpy").name
        for source in generated_sources
    ]
    deployed_module_set_sha256 = ""
    if compile_mpy:
        for source in generated_sources:
            compiled.append(
                _compile(
                    source,
                    compatibility=compatibility,
                    optimize=optimize,
                )
            )
        deployed_module_set_sha256 = _module_set_sha256(compiled)
    _write_text(
        build_module,
        "\n".join(
            (
                f'BUILD_ID = "{build_id}"',
                f'COMPATIBILITY = "{compatibility}"',
                f'SOURCE_BUNDLE_SHA256 = "{source_bundle_sha256}"',
                f'MODULE_SET_SHA256 = "{deployed_module_set_sha256}"',
                f"MODULE_FILES = {tuple(sorted(deployable_modules))!r}",
                "",
            )
        ),
    )
    generated_sources.append(build_module)
    if compile_mpy:
        compiled.append(
            _compile(
                build_module,
                compatibility=compatibility,
                optimize=optimize,
            )
        )

    manifest = {
        "schema": 1,
        "build_id": build_id,
        "output": str(output.resolve()),
        "compatibility": compatibility,
        "optimization": optimize,
        "compiled": compile_mpy,
        "source_bundle_sha256": source_bundle_sha256,
        "deployed_module_set_sha256": deployed_module_set_sha256,
        "source_provenance": source_provenance,
        "files": {
            path.name: {
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in sorted(generated_sources + compiled)
        },
        "safety": {
            "imports_existing_pololu_programs": False,
            "initializes_motors": False,
            "overwrites_main_py": False,
        },
    }
    _atomic_json(output / "manifest.json", manifest)
    return manifest


def validate_built_imports(build_root: Path) -> None:
    sys.path.insert(0, str(build_root))
    try:
        importlib.invalidate_caches()
        for prefix in ("b", "c"):
            for algorithm in ALGORITHMS:
                module_name = f"replay_{prefix}_{algorithm.lower()}"
                module = importlib.import_module(module_name)
                class_name = {
                    "CBAA": "CBAAAllocator",
                    "ACBBA": "ACBBAAllocator",
                    "PI": "PIAllocator",
                    "HIPC": "HIPCAllocator",
                    "DMCHBA": "DMCHBAAllocator",
                    "DGA": "DGAAllocator",
                }[algorithm]
                getattr(module, class_name)()
        native_collaborative = importlib.import_module(
            "replay_native_c_runtime"
        )
        getattr(native_collaborative, "create_persistent_runtime")
        native_bayesian_dmchba = importlib.import_module(
            "replay_native_b_dmchba"
        )
        getattr(native_bayesian_dmchba, "DMCHBAAllocator")
        physical_factory = importlib.import_module(
            "replay_physical_factory"
        )
        physical_adapter = importlib.import_module(
            "replay_physical_adapter"
        )
        getattr(physical_factory, "create_complete_runtime")
        getattr(physical_adapter, "PhysicalAllocatorAdapter")
    finally:
        sys.path.remove(str(build_root))
