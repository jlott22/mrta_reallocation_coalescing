from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Sequence


def write_csv(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for name in row:
            if name not in seen:
                seen.add(name)
                fieldnames.append(name)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(out_dir: str | Path, trial_rows: Sequence[dict], system_rows: Sequence[dict],
                  robot_rows: Sequence[dict], target_rows: Sequence[dict], config: dict) -> None:
    out = Path(out_dir)
    write_csv(out / "trial_summary.csv", trial_rows)
    write_csv(out / "system_performance.csv", system_rows)
    write_csv(out / "robot_performance.csv", robot_rows)
    write_csv(out / "target_performance.csv", target_rows)
    (out / "config_used.json").write_text(json.dumps(config, indent=2, default=str))
