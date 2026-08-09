#!/usr/bin/env python3
from __future__ import annotations

import csv
import argparse
import os
import shlex
from pathlib import Path

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = Path(os.environ.get("DCTA_REPO_ROOT", DEFAULT_REPO_ROOT)).expanduser().resolve()
PYTHON_BIN = "${PYTHON_BIN:-python3}"
RUNNER_REL = Path("known_visit_sim/tests/known_visit_horizon/run_known_visit_horizon_trial.py")
SCENARIO_FILE = Path("scenarios/known_visit_10target_300.csv")
ROOT = Path("results/sensitivity_known_target_visit_horizon_300")
GRID_SIZE = 19
NUM_ROBOTS = 4
NUM_TARGETS = 10
NUM_TRIALS = 300
MAX_TRIALS = 300
SEED = 0
DEFAULT_CORES = 9

HORIZONS = [1, 2, 3, 5, 8, 12]
ALGORITHMS = ["ACBBA", "PI", "HIPC", "DMCHBA", "DGA"]
CONDITIONS = [
    ("ideal", "ideal", ""),
    ("bernoulli_025", "bernoulli", "0.25"),
    ("ge_075", "gilbert_elliott", "0.75"),
    ("rayleigh_m50_66", "rayleigh_style", "-50.66"),
]
WEIGHTS = {"DGA": 4, "DMCHBA": 4, "HIPC": 2, "PI": 2, "ACBBA": 2}


def q(x) -> str:
    return shlex.quote(str(x))


def build_jobs():
    jobs = []
    for horizon in HORIZONS:
        setting = f"h{horizon}"
        for alg in ALGORITHMS:
            for comm_label, model, level in CONDITIONS:
                run_id = f"{alg.lower()}_{setting}_{comm_label}"
                out_dir = ROOT / "raw" / setting / model / run_id
                args = [
                    PYTHON_BIN,
                    str(RUNNER_REL),
                    "--scenario-file", str(SCENARIO_FILE),
                    "--generate-scenarios-if-missing",
                    "--num-trials", str(NUM_TRIALS),
                    "--max-trials", str(MAX_TRIALS),
                    "--num-targets", str(NUM_TARGETS),
                    "--grid-size", str(GRID_SIZE),
                    "--num-robots", str(NUM_ROBOTS),
                    "--algorithm", alg,
                    "--comm-model", model,
                    "--commitment-horizon", str(horizon),
                    "--seed", str(SEED),
                    "--out-dir", str(out_dir),
                    "--condition-id", run_id,
                ]
                if level:
                    args += ["--comm-level", level]
                jobs.append({
                    "setting": setting,
                    "run_id": run_id,
                    "algorithm": alg,
                    "comm_label": comm_label,
                    "comm_model": model,
                    "comm_level": level,
                    "horizon": horizon,
                    "scenario_file": str(SCENARIO_FILE),
                    "grid_size": GRID_SIZE,
                    "num_robots": NUM_ROBOTS,
                    "num_targets": NUM_TARGETS,
                    "max_trials": MAX_TRIALS,
                    "out_dir": str(out_dir),
                    "args": args,
                    "weight": WEIGHTS.get(alg, 1),
                })
    return jobs


def assign_jobs(jobs, num_parts):
    parts = [[] for _ in range(num_parts)]
    loads = [0 for _ in range(num_parts)]
    for job in sorted(jobs, key=lambda j: (-j["weight"], j["algorithm"], j["setting"], j["comm_label"])):
        idx = min(range(num_parts), key=lambda i: loads[i])
        parts[idx].append(job)
        loads[idx] += job["weight"]
    return parts, loads


def write_manifest(jobs):
    combined = REPO_ROOT / ROOT / "combined"
    combined.mkdir(parents=True, exist_ok=True)
    fields = ["setting", "run_id", "algorithm", "comm_label", "comm_model", "comm_level", "horizon", "scenario_file", "grid_size", "num_robots", "num_targets", "max_trials", "out_dir"]
    with (combined / "manifest.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for job in jobs:
            writer.writerow({k: job[k] for k in fields})


def write_part_scripts(parts):
    parts_dir = REPO_ROOT / ROOT / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    for idx, part_jobs in enumerate(parts):
        script_path = parts_dir / f"known_visit_horizon_core_{idx}.sh"
        lines = [
            "#!/usr/bin/env bash",
            "set -u",
            "set -o pipefail",
            f'REPO_ROOT="{REPO_ROOT}"',
            'cd "$REPO_ROOT" || exit 1',
            f'echo "Starting known-visit horizon core {idx} at $(date -Is)"',
            "",
        ]
        for job in part_jobs:
            cmd = " ".join(q(a) if not str(a).startswith("${PYTHON_BIN") else str(a) for a in job["args"])
            done_file = f'{job["out_dir"]}/_COMPLETE.txt'
            failed_file = f'{job["out_dir"]}/_FAILED.txt'
            log_file = f'{job["out_dir"]}/run.log'
            lines += [
                "",
                'echo "============================================================"',
                f'echo "RUN {job["run_id"]}"',
                'echo "============================================================"',
                f'mkdir -p {q(job["out_dir"])}',
                f'if [[ -f {q(done_file)} ]]; then',
                f'  echo "Skipping completed: {job["run_id"]}"',
                "else",
                f'  rm -f {q(failed_file)}',
                f'  echo "Command: {cmd}"',
                f'  {cmd} 2>&1 | tee {q(log_file)}',
                '  STATUS=${PIPESTATUS[0]}',
                '  if [[ "$STATUS" -ne 0 ]]; then',
                f'    echo "ERROR: failed {job["run_id"]} with status $STATUS"',
                f'    printf "status=%s\\nfailed_at=%s\\n" "$STATUS" "$(date -Is)" > {q(failed_file)}',
                f'    echo "Continuing core {idx} with its next condition"',
                "  fi",
                "fi",
            ]
        lines.append(f'echo "Finished known-visit horizon core {idx} at $(date -Is)"')
        script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        script_path.chmod(0o755)
        print(f"Wrote {script_path} ({len(part_jobs)} jobs)")


def write_start_script(num_parts):
    script_path = REPO_ROOT / ROOT / "start_all_known_visit_horizon_parts.sh"
    lines = [
        "#!/usr/bin/env bash",
        "set -u",
        "set -o pipefail",
        f'cd "{REPO_ROOT}" || exit 1',
        f'mkdir -p {ROOT}/logs',
        'echo "Starting all known-visit horizon parts at $(date -Is)"',
        'if [[ -n "${DISPLAY:-}" ]] && command -v gnome-terminal >/dev/null 2>&1; then',
        f'  gnome-terminal --title="Known-visit horizon progress (sleep inhibited)" -- bash -lc \'cd "$PWD"; systemd-inhibit --what=sleep:idle --mode=block --who="DCTA horizon study" --why="Known-visit simulation is running" python3 {ROOT}/monitor_progress.py; echo; read -rp "Press Enter to close"\' >/dev/null 2>&1 &',
        'elif [[ -n "${DISPLAY:-}" ]] && command -v xterm >/dev/null 2>&1; then',
        f'  xterm -T "Known-visit horizon progress (sleep inhibited)" -e bash -lc \'cd "$PWD"; systemd-inhibit --what=sleep:idle --mode=block --who="DCTA horizon study" --why="Known-visit simulation is running" python3 {ROOT}/monitor_progress.py; echo; read -rp "Press Enter to close"\' >/dev/null 2>&1 &',
        'else',
        f'  echo "Progress monitor: python3 {ROOT}/monitor_progress.py"',
        'fi',
    ]
    for i in range(num_parts):
        lines.append(f'./{ROOT}/parts/known_visit_horizon_core_{i}.sh > {ROOT}/logs/core_{i}.master.log 2>&1 &')
    lines += ["wait", 'echo "All known-visit horizon parts finished at $(date -Is)"']
    script_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    script_path.chmod(0o755)
    print(f"Wrote {script_path}")


def write_monitor_script(total_jobs, num_parts):
    script_path = REPO_ROOT / ROOT / "monitor_progress.py"
    source = f'''#!/usr/bin/env python3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOTAL_JOBS = {total_jobs}
TRIALS_PER_JOB = {MAX_TRIALS}
TOTAL_TRIALS = TOTAL_JOBS * TRIALS_PER_JOB
def journal_count(path):
    try:
        with path.open(encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0

def progress():
    completed_jobs = 0
    completed_trials = 0
    running = []
    failed = []
    for log in (ROOT / "raw").glob("**/run.log"):
        condition = log.parent.name
        if (log.parent / "_COMPLETE.txt").exists():
            completed_jobs += 1
            completed_trials += TRIALS_PER_JOB
            continue
        latest = journal_count(log.parent / "_trial_journal.jsonl")
        completed_trials += latest
        if (log.parent / "_FAILED.txt").exists():
            failed.append((condition, latest))
        else:
            running.append((condition, latest))
    return completed_jobs, min(completed_trials, TOTAL_TRIALS), sorted(running), sorted(failed)

while True:
    jobs, trials, running, failed = progress()
    pct = 100.0 * trials / TOTAL_TRIALS
    print("\\033[2J\\033[H", end="")
    print("Known-visit horizon sensitivity — {num_parts}-core run")
    print(f"Progress: {{pct:6.2f}}%  trials {{trials:,}}/{{TOTAL_TRIALS:,}}  complete {{jobs}}/{{TOTAL_JOBS}}  failed {{len(failed)}}")
    print("\\nActive conditions:")
    for name, done in running[-8:]:
        print(f"  {{name:<34}} {{done:>3}}/{{TRIALS_PER_JOB}}")
    if failed:
        print("\\nFailed conditions (checkpointed for retry):")
        for name, done in failed[-8:]:
            print(f"  {{name:<34}} {{done:>3}}/{{TRIALS_PER_JOB}}")
    print("\\nUpdates every 5 minutes. Ctrl-C closes only this monitor.")
    if jobs >= TOTAL_JOBS:
        print("\\nAll conditions complete.")
        break
    time.sleep(300)
'''
    script_path.write_text(source, encoding="utf-8")
    script_path.chmod(0o755)
    print(f"Wrote {script_path}")


def main():
    parser = argparse.ArgumentParser(description="Prepare known-visit horizon sensitivity workers.")
    parser.add_argument("--cores", type=int, default=DEFAULT_CORES)
    args = parser.parse_args()
    if args.cores < 1:
        parser.error("--cores must be at least 1")
    if not (REPO_ROOT / "known_visit_sim").is_dir():
        raise SystemExit(
            f"known_visit_sim package not found under {REPO_ROOT}; "
            "set DCTA_REPO_ROOT to the repository checkout"
        )
    jobs = build_jobs()
    parts, loads = assign_jobs(jobs, args.cores)
    write_manifest(jobs)
    write_part_scripts(parts)
    write_monitor_script(len(jobs), args.cores)
    write_start_script(args.cores)
    print("loads:", loads)
    print("\nNext commands:")
    print(f"  python3 {REPO_ROOT / 'known_visit_sim/tests/known_visit_horizon/make_known_visit_horizon_sensitivity.py'}")
    print(f"  ./{ROOT}/start_all_known_visit_horizon_parts.sh")
    print(f"  bash known_visit_sim/tests/known_visit_horizon/combine_known_visit_horizon_sensitivity_300.sh")


if __name__ == "__main__":
    main()
