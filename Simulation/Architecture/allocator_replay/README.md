# RP2040/Pololu reallocation-coalescing HIL

This subsystem is the motor-free embedded-compute companion to the online
Collaborative Visit simulation. The active command surface supports only the
six collaborative allocators (CBAA, ACBBA, PI, HIPC, DMCHBA, and DGA) with
unrestricted candidates. Bayesian/Top-K modules are not included in a device
bundle, and the deployed worker rejects its legacy one-shot allocator path;
they cannot be selected through `python -m allocator_replay`.

## What the replay measures

For every paired release trace, the host reconstructs the arrival-admission
epochs implied by Eager, Count, or Bounded `B`/`W` policy settings. The same
complete 50-task universe is resident for a trial while only admitted tasks
are marked active. One MicroPython VM remains alive for the trial and restores
the four logical robot contexts outside the timed region.
Each admission epoch carries its index, trigger reason, task IDs, and admitted
cells through the persistent protocol. The native runtime records that event
idempotently and invalidates visible-set-dependent allocator caches exactly
once per robot (on round zero), so later consensus rounds do not repeat the
reset.

The RP2040 timer encloses only `choose_goal()`. State setup, context restore,
USB transport, result serialization, and journaling are separately recorded
as host overhead. Every accepted call verifies
`candidate_count_before == candidate_count_after`.

This is a **motionless arrival-epoch allocator replay**. It validates embedded
allocator computation; it does not substitute for the simulator's movement,
task completion, or mandatory robot-idle/completion allocation epochs. Join
HIL results to simulation results through `paired_manifest_id` and
`paired_manifest_sha256`.

## Software validation

From the repository root on Windows:

```powershell
scripts/run_hil_pilot.ps1 -DryRun
```

The dry run exercises the actual chunked serial protocol against an in-process
loopback, all persistent context restore paths, resumability, reporting, and
the device/host timing split. It never claims hardware validation.

## One-time hardware setup

Install `mpy-cross`, `mpremote`, and the host requirements, connect the
Pololu/RP2040 boards, then build and deploy only the motor-free replay modules:

```powershell
$env:PYTHONPATH=(Resolve-Path "Simulation/Architecture").Path
python -m pip install -r Simulation/Architecture/allocator_replay/requirements-host.txt
python -m allocator_replay build-device --ports auto
python -m allocator_replay deploy --ports auto
python -m allocator_replay preflight --ports auto
```

Deployment never writes `main.py`, imports motors, or initializes sensors.
Safe auto-discovery probes only USB VID:PID `2e8a:0005`, Raspberry Pi's
registered Pico MicroPython CDC identity; it records and skips every other
serial device. Supplying an
exact `--ports COM12 ...` list is the operator allow-list when a board does not
enumerate with one of those known IDs. Preflight re-runs live `HELLO` and
`CHECK`, hashes the deployed module set against the selected build, binds the
firmware identity, and exercises initial visibility, later online admission,
and duplicate-epoch idempotence for all six allocators.

## Pilot and selected campaign

```powershell
scripts/run_hil_pilot.ps1
scripts/run_hil_campaign.ps1
```

Pass explicit ports when needed, for example `-Ports COM12,COM13`. Both
launchers validate every scenario/release byte hash before opening hardware.
Campaign schedules are immutable and bind Git/source/config/manifest/platform
provenance plus the exact build, module set, firmware, and device set. Every
journal row seals the same identity and report rebuilding rejects an unbound
completion. Completed trials are skipped on rerun; an interrupted trial starts
a new generation while prior journal rows remain append-only. A trial is
pinned to its original device, so reconnect that board before resuming.

Useful direct commands:

```powershell
python -m allocator_replay hil-status --config configs/hil_campaign.json
python -m allocator_replay hil-report --config configs/hil_campaign.json
```

Reports are written under `results/hil_reallocation_coalescing/<campaign>/`:

- `schedule.json`, `config_snapshot.json`, and `state.json`;
- append-only `journal/attempts.jsonl`;
- `reports/allocator_calls.csv`;
- `reports/trial_metrics.csv`;
- `reports/condition_metrics.csv` and `reports/summary.json`.

The supplied pilot is intentionally small (one medium-load paired trace,
Eager/Count-4/Bounded-4-5s, and the four core allocators). The selected HIL
campaign uses five paired traces, calibrated low/medium/high arrival rates of
0.075/0.3/1.2 tasks/s, B=1/2/4/8 plus bounded B=4, W=5s, and the four core
allocators. DMCHBA and DGA remain selectable by adding their names to a new
config; no source edit is required.
