#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import shlex
from pathlib import Path

REPO_ROOT = Path(os.environ.get("DCTA_REPO_ROOT", Path(__file__).resolve().parents[3])).expanduser().resolve()
ROOT = Path("results/known_target_visit_core_500")
COMBINED_ROOT = ROOT / "combined"
RUNNER = Path("known_visit_sim/tests/known_visit_horizon/run_known_visit_horizon_trial.py")
SCENARIO = Path("scenarios/known_visit_g19_t10_n500.csv")
ALGORITHMS = ["CBAA", "ACBBA", "PI", "HIPC", "DMCHBA", "DGA"]
HORIZON = 8
TRIALS = 500
WEIGHTS = {"DGA": 5, "DMCHBA": 4, "HIPC": 2, "PI": 2, "ACBBA": 2, "CBAA": 1}
CONDITIONS = [
    ("ideal_1_0", "ideal", "1.0", ""),
    *[(f"bernoulli_drop_{str(v).replace('.', '_')}", "bernoulli", f"drop_{v}", str(v))
      for v in (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7)],
    *[(f"gilbert_elliott_pGG_{str(v).replace('.', '_')}_pBB_{str(round(1-v, 2)).replace('.', '_')}",
       "gilbert_elliott", f"pGG_{v}_pBB_{round(1-v, 2)}", str(v))
      for v in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)],
    *[(f"rayleigh_style_sens_neg{str(abs(v)).replace('.', '_')}", "rayleigh_style", f"sens_{v}", str(v))
      for v in (-32.58, -37.79, -42.16, -46.04, -49.17, -52.15, -56.04, -59.4)],
]


def quote(value: object) -> str:
    return shlex.quote(str(value))


def jobs() -> list[dict]:
    result = []
    for algorithm in ALGORITHMS:
        for folder, model, label, level in CONDITIONS:
            run_id = f"{algorithm.lower()}_{folder}"
            out_dir = ROOT / "raw" / algorithm.lower() / folder
            args = [
                "${PYTHON_BIN:-python3}", str(RUNNER),
                "--scenario-file", str(SCENARIO), "--num-trials", str(TRIALS),
                "--max-trials", str(TRIALS), "--num-targets", "10",
                "--grid-size", "19", "--num-robots", "4",
                "--algorithm", algorithm, "--comm-model", model,
                "--commitment-horizon", str(HORIZON), "--seed", "0",
                "--out-dir", str(out_dir), "--condition-id", run_id,
                "--study-type", "core", "--suite-name", "known_target_visit_core_500",
            ]
            if level:
                args.extend(["--comm-level", level])
            result.append({"algorithm": algorithm, "folder": folder, "model": model,
                           "label": label, "level": level, "run_id": run_id,
                           "out_dir": str(out_dir), "args": args,
                           "weight": WEIGHTS[algorithm]})
    return result


def assign(all_jobs: list[dict], cores: int) -> tuple[list[list[dict]], list[int]]:
    parts = [[] for _ in range(cores)]
    loads = [0] * cores
    for job in sorted(all_jobs, key=lambda x: (-x["weight"], x["algorithm"], x["folder"])):
        index = min(range(cores), key=lambda i: loads[i])
        parts[index].append(job)
        loads[index] += job["weight"]
    return parts, loads


def write_manifest(all_jobs: list[dict]) -> None:
    out = REPO_ROOT / COMBINED_ROOT / "manifest.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["algorithm", "folder", "model", "label", "level", "run_id", "out_dir"]
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for job in all_jobs:
            writer.writerow({key: job[key] for key in fields})


def write_workers(parts: list[list[dict]]) -> None:
    directory = REPO_ROOT / ROOT / "parts"
    directory.mkdir(parents=True, exist_ok=True)
    for index, part in enumerate(parts):
        lines = ["#!/usr/bin/env bash", "set -u", "set -o pipefail",
                 f'cd "{REPO_ROOT}" || exit 1',
                 f'echo "Starting known-visit core-500 worker {index} at $(date -Is)"']
        for job in part:
            out = job["out_dir"]
            cmd = " ".join(arg if str(arg).startswith("${PYTHON_BIN") else quote(arg) for arg in job["args"])
            lines.extend([
                "", f'echo "RUN {job["run_id"]}"', f"mkdir -p {quote(out)}",
                f"if [[ -f {quote(out + '/_COMPLETE.txt')} ]]; then",
                f'  echo "Skipping completed: {job["run_id"]}"', "else",
                f"  rm -f {quote(out + '/_FAILED.txt')}",
                f"  {cmd} 2>&1 | tee {quote(out + '/run.log')}",
                "  STATUS=${PIPESTATUS[0]}", '  if [[ "$STATUS" -ne 0 ]]; then',
                f'    printf "status=%s\\nfailed_at=%s\\n" "$STATUS" "$(date -Is)" > {quote(out + "/_FAILED.txt")}',
                f'    echo "ERROR: {job["run_id"]} failed; continuing worker {index}"',
                "  fi", "fi",
            ])
        lines.append(f'echo "Finished known-visit core-500 worker {index} at $(date -Is)"')
        path = directory / f"known_visit_core500_worker_{index}.sh"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        path.chmod(0o755)


def write_monitor(total: int, cores: int) -> None:
    path = REPO_ROOT / ROOT / "monitor_progress.py"
    path.write_text(f'''#!/usr/bin/env python3
import json
import time
from pathlib import Path
ROOT = Path(__file__).resolve().parent
TOTAL = {total}
PER_CONDITION = {TRIALS}
TOTAL_TRIALS = TOTAL * PER_CONDITION
def count(path):
    if not path.exists(): return 0
    good = 0
    for line in path.read_text(errors="replace").splitlines():
        try: json.loads(line); good += 1
        except Exception: pass
    return good
while True:
    complete = failed = trials = 0
    active = []
    for directory in (ROOT / "raw").glob("*/*"):
        if not directory.is_dir(): continue
        done = count(directory / "_trial_journal.jsonl")
        trials += done
        if (directory / "_COMPLETE.txt").exists(): complete += 1
        elif (directory / "_FAILED.txt").exists(): failed += 1
        elif (directory / "run.log").exists(): active.append((directory.parent.name + "/" + directory.name, done))
    pct = 100 * trials / TOTAL_TRIALS
    print("\\033[2J\\033[H", end="")
    print("Known-visit core-500 — {cores}-core run — fixed horizon {HORIZON}")
    print(f"Progress: {{pct:6.2f}}%  trials {{trials:,}}/{{TOTAL_TRIALS:,}}  complete {{complete}}/{{TOTAL}}  failed {{failed}}")
    print("\\nActive conditions:")
    for name, done in sorted(active)[-{cores}:]: print(f"  {{name:<58}} {{done:>3}}/{{PER_CONDITION}}")
    print("\\nTeam stall metric: stall_recoveries_total per trial")
    print("Updates every 5 minutes. Ctrl-C closes only this monitor.")
    if complete + failed >= TOTAL and not active: break
    time.sleep(300)
''', encoding="utf-8")
    path.chmod(0o755)


def write_launcher(cores: int) -> None:
    path = REPO_ROOT / ROOT / "start_all_known_visit_core500_workers.sh"
    lines = ["#!/usr/bin/env bash", "set -u", "set -o pipefail",
             f'cd "{REPO_ROOT}" || exit 1', f"mkdir -p {ROOT}/logs",
             'if [[ -n "${DISPLAY:-}" ]] && command -v gnome-terminal >/dev/null 2>&1; then',
             f'  gnome-terminal --title="Known-visit core-500 progress (sleep inhibited)" -- bash -lc \'cd "$PWD"; systemd-inhibit --what=sleep:idle --mode=block --who="DCTA core-500" --why="Known-visit core benchmark is running" python3 {ROOT}/monitor_progress.py; echo; read -rp "Press Enter to close"\' >/dev/null 2>&1 &',
             "fi", 'echo "Starting core-500 workers at $(date -Is)"']
    for index in range(cores):
        lines.append(f"./{ROOT}/parts/known_visit_core500_worker_{index}.sh > {ROOT}/logs/worker_{index}.log 2>&1 &")
    lines.extend(["wait", 'echo "All core-500 workers finished at $(date -Is)"'])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cores", type=int, default=10)
    args = parser.parse_args()
    if args.cores < 1: parser.error("--cores must be positive")
    if len(CONDITIONS) != 25: raise RuntimeError(f"expected 25 conditions, got {len(CONDITIONS)}")
    all_jobs = jobs()
    parts, loads = assign(all_jobs, args.cores)
    write_manifest(all_jobs); write_workers(parts); write_monitor(len(all_jobs), args.cores); write_launcher(args.cores)
    print(f"Prepared {len(all_jobs)} conditions, {len(all_jobs) * TRIALS} trials, loads={loads}")


if __name__ == "__main__":
    main()
