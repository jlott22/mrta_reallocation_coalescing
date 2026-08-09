from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from study.manifests import generate_manifest_set, sha256_file, validate_manifest_set
from study.tests.helpers import test_config


class ManifestTests(unittest.TestCase):
    def test_generation_is_byte_deterministic_and_uses_separate_streams(self) -> None:
        config = test_config()
        with tempfile.TemporaryDirectory() as first_temp, tempfile.TemporaryDirectory() as second_temp:
            first = generate_manifest_set(config, first_temp)
            second = generate_manifest_set(config, second_temp)
            first_files = sorted(path.relative_to(first) for path in first.rglob("*.json"))
            second_files = sorted(path.relative_to(second) for path in second.rglob("*.json"))
            self.assertEqual(first_files, second_files)
            for relative in first_files:
                self.assertEqual((first / relative).read_bytes(), (second / relative).read_bytes())

            scenario = json.loads((first / "scenarios/trace_0000.json").read_text())
            low = json.loads((first / "releases/low/trace_0000.json").read_text())
            high = json.loads((first / "releases/high/trace_0000.json").read_text())
            self.assertNotEqual(scenario["spatial_seed"], low["arrival_seed"])
            self.assertEqual("task_0001", scenario["tasks"][0]["task_id"])
            self.assertEqual("task_0012", scenario["tasks"][-1]["task_id"])
            self.assertNotEqual(scenario["runtime_seed"], low["arrival_seed"])
            self.assertEqual(low["arrival_seed"], high["arrival_seed"])
            self.assertEqual(low["runtime_seed"], high["runtime_seed"])
            self.assertEqual(8, sum(task["release_time_s"] == 0 for task in low["tasks"]))
            low_online = [task["release_time_s"] for task in low["tasks"][8:]]
            high_online = [task["release_time_s"] for task in high["tasks"][8:]]
            self.assertTrue(all(a < b for a, b in zip(low_online, low_online[1:])))
            # Same exponential variates are scaled by rate: high releases sooner.
            self.assertTrue(all(high_time < low_time for high_time, low_time in zip(high_online, low_online)))

    def test_index_hashes_and_semantic_pairing_validate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = generate_manifest_set(test_config(), temporary)
            index = validate_manifest_set(root)
            scenario_entry = next(
                entry for entry in index["entries"] if entry["manifest_kind"] == "scenario"
            )
            release_entry = next(
                entry for entry in index["entries"] if entry["manifest_kind"] == "release_trace"
            )
            release = json.loads((root / release_entry["path"]).read_text())
            self.assertEqual(scenario_entry["sha256"], release["scenario_sha256"])
            self.assertEqual(release_entry["sha256"], sha256_file(root / release_entry["path"]))

    def test_hash_tampering_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = generate_manifest_set(test_config(), temporary)
            release_path = root / "releases/low/trace_0000.json"
            release_path.write_bytes(release_path.read_bytes() + b" ")
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                validate_manifest_set(root)

    def test_generation_refuses_to_overwrite_changed_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = test_config()
            root = generate_manifest_set(config, temporary)
            scenario_path = root / "scenarios/trace_0000.json"
            scenario_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                generate_manifest_set(config, temporary)


if __name__ == "__main__":
    unittest.main()
