from __future__ import annotations

import array
import importlib
import json
import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from allocator_replay.capture.codec import decode_value, encode_value
from allocator_replay.config.study import DEVICE_BUILD_ROOT
from allocator_replay.device.build import build_device_bundle, validate_built_imports
from allocator_replay.device.common.replay_random import Random
from allocator_replay.host.preflight import verify_source_safety


class CodecAndBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = build_device_bundle()
        cls.root = Path(str(cls.manifest["output"]))

    def test_codec_round_trip_preserves_rng_and_non_string_keys(self) -> None:
        rng = random.Random(419)
        value = {
            (3, 10): {None, "robot", 7},
            "rng": rng,
            "bytes": b"\x00\xff",
        }
        decoded = decode_value(encode_value(value))
        self.assertEqual(decoded[(3, 10)], {None, "robot", 7})
        self.assertEqual(decoded["bytes"], b"\x00\xff")
        self.assertEqual(
            [decoded["rng"].random() for _ in range(5)],
            [rng.random() for _ in range(5)],
        )

    def test_micropython_rng_matches_cpython(self) -> None:
        for seed in (0, 1, 1009, 20260727, 2**70 + 13):
            expected = random.Random(seed)
            actual = Random(seed)
            self.assertEqual(
                [actual.getrandbits(17) for _ in range(20)],
                [expected.getrandbits(17) for _ in range(20)],
            )

    def test_compact_rng_restore_preserves_exact_continuation(self) -> None:
        expected = random.Random(20260727)
        for _ in range(317):
            expected.getrandbits(29)
        _, internal, _ = expected.getstate()
        words = array.array("I", internal[:624])

        actual = Random(None, words, internal[624])

        self.assertIs(actual._state, words)
        self.assertEqual(len(actual._state), 624)
        for bits in (1, 17, 32, 63, 5, 31):
            self.assertEqual(
                actual.getrandbits(bits),
                expected.getrandbits(bits),
            )
        self.assertEqual(
            [actual.random() for _ in range(12)],
            [expected.random() for _ in range(12)],
        )
        self.assertEqual(actual.getstate(), expected.getstate())

    def test_streamed_rng_restore_is_bounded_and_exact(self) -> None:
        expected = random.Random(20311176)
        for _ in range(913):
            expected.random()
        _, internal, _ = expected.getstate()

        class Robot:
            pass

        robot = Robot()
        prefix = "dga_rng_replay_rng_"
        setattr(robot, prefix + "state_length", 624)
        setattr(robot, prefix + "index", internal[624])
        setattr(robot, prefix + "chunk_count", 26)
        for chunk_index in range(26):
            start = chunk_index * 24
            stop = min(624, start + 24)
            raw = bytearray()
            for word in internal[start:stop]:
                raw.extend(int(word).to_bytes(4, "little"))
            setattr(robot, prefix + f"{chunk_index:03d}", raw)

        sys.path.insert(0, str(self.root))
        try:
            persistent = importlib.import_module("replay_persistent")
            persistent._restore_streamed_rng(robot)
        finally:
            sys.path.remove(str(self.root))

        self.assertEqual(robot.dga_rng._state.__class__.__name__, "array")
        self.assertEqual(len(robot.dga_rng._state), 624)
        self.assertFalse(hasattr(robot, prefix + "state_length"))
        self.assertEqual(
            [robot.dga_rng.getrandbits(23) for _ in range(40)],
            [expected.getrandbits(23) for _ in range(40)],
        )

    def test_bundle_compiles_imports_and_is_motor_free(self) -> None:
        validate_built_imports(self.root)
        safety = verify_source_safety(self.root)
        self.assertTrue(safety["passed"], safety)
        self.assertFalse(safety["main_module_present"])
        self.assertFalse((self.root / "main.py").exists())
        self.assertFalse((self.root / "main.mpy").exists())
        self.assertFalse((self.root / "fingerprint.mpy").exists())
        manifest = json.loads(
            (self.root / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertTrue(manifest["safety"]["overwrites_main_py"] is False)
        self.assertTrue((self.root / "replay_worker.mpy").exists())
        for source in self.root.glob("replay_*.py"):
            self.assertNotIn(
                "from __future__ import",
                source.read_text(encoding="utf-8"),
                source.name,
            )

    def test_dga_segment_reverse_is_micropython_compatible(self) -> None:
        for name in ("replay_b_dga.py", "replay_c_dga.py"):
            source = (self.root / name).read_text(encoding="utf-8")
            self.assertNotIn("= reversed(", source, name)
            self.assertIn("= list(reversed(", source, name)

    def test_dga_array_extend_is_micropython_compatible(self) -> None:
        source = (
            self.root / "replay_b_dga_optimized.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            "cells.extend((self._pack_cell(cell, grid_size) for cell in route))",
            source,
        )
        self.assertIn(
            "for cell in route:\n"
            "                cells.append(self._pack_cell(cell, grid_size))",
            source,
        )

    def test_persistent_snapshot_avoids_tuple_startswith(self) -> None:
        source = (
            self.root / "replay_persistent.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn(".startswith(ALGORITHM_PREFIXES)", source)

    def test_persistent_rng_restore_avoids_cpython_new(self) -> None:
        persistent_source = (
            self.root / "replay_persistent.py"
        ).read_text(encoding="utf-8")
        codec_source = (
            self.root / "replay_codec.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("Random.__new__(Random)", persistent_source)
        self.assertIn(
            'state = array("I", bytearray(length * 4))',
            persistent_source,
        )
        self.assertNotIn("state.append(", persistent_source)
        self.assertNotIn(
            "tuple(int(item) for item in state_values)",
            codec_source,
        )

    def test_object_slot_encoding_does_not_require_cpython_mro(self) -> None:
        source = (self.root / "replay_codec.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("type(value).__mro__", source)
        self.assertIn(
            'getattr(value_type, "__mro__", (value_type,))',
            source,
        )

    def test_type_opaque_micropython_arrays_never_guess_float32(self) -> None:
        """RP2040 MicroPython arrays have no public typecode/itemsize."""

        class array:
            def __init__(self, values):
                self._values = list(values)

            def __iter__(self):
                return iter(self._values)

            def __len__(self):
                return len(self._values)

            def __getitem__(self, index):
                return self._values[index]

        sys.path.insert(0, str(self.root))
        try:
            codec = importlib.import_module("replay_codec")
            # Exercise either side of the DGA logical-array chunk boundary,
            # either side of the 384-byte raw transport boundary for uint16,
            # and the exact 361-cell candidate-workspace length that failed.
            for length in (1, 2, 3, 47, 48, 49, 191, 192, 193, 361):
                with self.subTest(length=length):
                    values = list(range(length))
                    encoded = codec.encode_value(array(values))
                    self.assertIsInstance(encoded, list)
                    self.assertEqual(encoded, values)
                    self.assertEqual(decode_value(encoded), values)
        finally:
            sys.path.remove(str(self.root))

    def test_large_cellmap_json_stream_never_calls_copying_items(self) -> None:
        """A 361-cell result is encoded directly in bounded wire chunks."""

        sys.path.insert(0, str(self.root))
        try:
            codec = importlib.import_module("replay_codec")
            memory = importlib.import_module("replay_memory")
            mapping = memory.CellIndexedMap(19, numeric=False)
            expected = {}
            for cell_id in range(361):
                cell = (cell_id % 19, cell_id // 19)
                signature = (
                    str(cell_id % 4),
                    float(cell_id) / 7.0,
                )
                mapping[cell] = signature
                expected[cell] = signature

            original_items = memory.CellIndexedMap.items

            def fail_if_copied(_mapping):
                raise AssertionError("CellIndexedMap.items() copied output")

            memory.CellIndexedMap.items = fail_if_copied
            try:
                chunks = list(
                    codec.iter_json_chunks(
                        mapping,
                        encode_replay_value=True,
                        chunk_size=192,
                    )
                )
            finally:
                memory.CellIndexedMap.items = original_items

            self.assertGreater(len(chunks), 20)
            self.assertLessEqual(max(len(chunk) for chunk in chunks), 192)
            encoded = json.loads(b"".join(chunks).decode("utf-8"))
            decoded = codec.decode_value(encoded)
            self.assertEqual(dict(decoded.items()), expected)
        finally:
            sys.path.remove(str(self.root))

    def test_generated_bayesian_scratch_is_prepared_but_not_snapshotted(
        self,
    ) -> None:
        """CBAA-family packed arrays are workspaces, not logical state."""

        transient = {
            "_candidate_scan_ids",
            "_candidate_ranked_ids",
            "_candidate_probabilities",
            "_candidate_distances",
            "_active_candidate_cache",
        }
        classes = {
            "CBAA": ("replay_b_cbaa", "CBAAAllocator"),
            "ACBBA": ("replay_b_acbba", "ACBBAAllocator"),
            "PI": ("replay_b_pi", "PIAllocator"),
            "HIPC": ("replay_b_hipc", "HIPCAllocator"),
        }
        robot = SimpleNamespace(
            grid_size=19,
            cfg=SimpleNamespace(grid_size=19),
        )
        sys.path.insert(0, str(self.root))
        try:
            persistent = importlib.import_module("replay_persistent")
            for algorithm, (module_name, class_name) in classes.items():
                with self.subTest(algorithm=algorithm):
                    module = importlib.import_module(module_name)
                    allocator = getattr(module, class_name)()
                    persistent._prepare_replay_state(allocator, robot)
                    self.assertEqual(
                        len(allocator._candidate_scan_ids),
                        361,
                    )
                    self.assertEqual(
                        len(allocator._candidate_ranked_ids),
                        361,
                    )
                    self.assertEqual(
                        len(allocator._candidate_probabilities),
                        361,
                    )
                    self.assertEqual(
                        len(allocator._candidate_distances),
                        361,
                    )
                    snapshot = persistent._allocator_snapshot(allocator)
                    self.assertFalse(
                        transient.intersection(snapshot),
                        snapshot,
                    )
        finally:
            sys.path.remove(str(self.root))
