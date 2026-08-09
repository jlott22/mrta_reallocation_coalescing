from __future__ import annotations

import csv
import hashlib
import json
import random
from pathlib import Path
from typing import Iterable

from allocator_replay.config.study import (
    BAYESIAN_SEED,
    BAYESIAN_TRIAL_IDS,
    COHORT_ROOT,
    COLLABORATIVE_SEED,
    COLLABORATIVE_TRIAL_IDS,
    GRID_SIZE,
    REPOSITORY_ROOT,
    START_POSITIONS,
    cohort_path,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_seed(*parts: object) -> int:
    payload = "|".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _weighted_without_replacement(
    rng: random.Random,
    population: list[tuple[int, int]],
    weights: list[float],
    count: int,
) -> list[tuple[int, int]]:
    available = list(population)
    remaining_weights = list(weights)
    selected: list[tuple[int, int]] = []
    while available and len(selected) < count:
        chosen = rng.choices(available, weights=remaining_weights, k=1)[0]
        index = available.index(chosen)
        selected.append(available.pop(index))
        remaining_weights.pop(index)
    return selected


def _atomic_csv(
    path: Path,
    comments: Iterable[str],
    header: list[str],
    rows: Iterable[list[object]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        for comment in comments:
            handle.write(f"# {comment}\n")
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    temporary.replace(path)


def generate_bayesian(path: Path | None = None) -> Path:
    output = path or cohort_path("bayesian")
    rng = random.Random(BAYESIAN_SEED)
    starts = set(START_POSITIONS.values())
    cells = [(x, y) for x in range(GRID_SIZE) for y in range(GRID_SIZE)]
    rows: list[list[object]] = []
    repairs: list[int] = []
    for trial_id in BAYESIAN_TRIAL_IDS:
        target = (rng.randrange(GRID_SIZE), rng.randrange(GRID_SIZE))
        while target in starts:
            repairs.append(trial_id)
            target = (rng.randrange(GRID_SIZE), rng.randrange(GRID_SIZE))
        candidates = [cell for cell in cells if cell != target]
        weights = [
            1.0 / (1.0 + abs(cell[0] - target[0]) + abs(cell[1] - target[1]))
            for cell in candidates
        ]
        clue_rng = random.Random(
            _stable_seed(
                BAYESIAN_SEED,
                f"d1_c4_g{GRID_SIZE}",
                f"ep{trial_id}",
                f"obj{target}",
            )
        )
        clues = _weighted_without_replacement(clue_rng, candidates, weights, 4)
        rows.append(
            [
                trial_id,
                target[0],
                target[1],
                *[coordinate for clue in clues for coordinate in clue],
            ]
        )
    header = ["episode", "object_x", "object_y"]
    for clue_index in range(1, 5):
        header.extend((f"clue{clue_index}_x", f"clue{clue_index}_y"))
    _atomic_csv(
        output,
        (
            "scenario_set=allocator_replay_held_out",
            "mission=bayesian_clue_search",
            f"base_seed={BAYESIAN_SEED}",
            "condition=distribution=1,clues_per_object=4,grid_size=19",
            f"target_start_repair_trials={','.join(map(str, repairs)) or 'none'}",
        ),
        header,
        rows,
    )
    return output


def _collaborative_rows(count: int) -> list[list[object]]:
    starts = set(START_POSITIONS.values())
    eligible = [
        (x, y)
        for y in range(GRID_SIZE)
        for x in range(GRID_SIZE)
        if (x, y) not in starts
    ]
    rng = random.Random(COLLABORATIVE_SEED)
    return [
        [trial_id, *[coordinate for cell in rng.sample(eligible, 50) for coordinate in cell]]
        for trial_id in range(count)
    ]


def _read_noncomment_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(
            csv.DictReader(
                line
                for line in handle
                if line.strip() and not line.lstrip().startswith("#")
            )
        )


def generate_collaborative(path: Path | None = None) -> Path:
    output = path or cohort_path("collaborative")
    all_rows = _collaborative_rows(150)
    prior = (
        REPOSITORY_ROOT
        / "Results"
        / "Simulation"
        / "Sensitivity"
        / "scenarios"
        / "known_targets_g19_t50_n100.csv"
    )
    if prior.exists():
        expected = _read_noncomment_csv(prior)
        header = ["trial_id"]
        for target_index in range(1, 51):
            header.extend((f"target{target_index}_x", f"target{target_index}_y"))
        regenerated = [
            {name: str(value) for name, value in zip(header, row)}
            for row in all_rows[:100]
        ]
        if expected != regenerated:
            raise RuntimeError(
                "collaborative seed continuation does not reproduce trials 0-99"
            )
    rows = all_rows[
        COLLABORATIVE_TRIAL_IDS[0] : COLLABORATIVE_TRIAL_IDS[-1] + 1
    ]
    header = ["trial_id"]
    for target_index in range(1, 51):
        header.extend((f"target{target_index}_x", f"target{target_index}_y"))
    _atomic_csv(
        output,
        (
            "scenario_set=allocator_replay_held_out",
            "mission=collaborative_visit",
            f"seed={COLLABORATIVE_SEED}",
            "source_stream_rows=100-149",
            "grid_size=19,num_targets=50,num_robots=4,layout=edge_even",
        ),
        header,
        rows,
    )
    return output


def generate_all() -> dict[str, object]:
    COHORT_ROOT.mkdir(parents=True, exist_ok=True)
    bayesian = generate_bayesian()
    collaborative = generate_collaborative()
    manifest = {
        "schema": 1,
        "cohorts": {
            "bayesian": {
                "path": str(bayesian.resolve()),
                "sha256": sha256_file(bayesian),
                "trial_ids": list(BAYESIAN_TRIAL_IDS),
                "seed": BAYESIAN_SEED,
            },
            "collaborative": {
                "path": str(collaborative.resolve()),
                "sha256": sha256_file(collaborative),
                "trial_ids": list(COLLABORATIVE_TRIAL_IDS),
                "seed": COLLABORATIVE_SEED,
            },
        },
    }
    manifest_path = COHORT_ROOT / "manifest.json"
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest_path)
    return manifest
