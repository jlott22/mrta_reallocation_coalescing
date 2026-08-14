# RP2040/Pololu reallocation-coalescing HIL

This subsystem is the motor-free embedded-compute companion to the online
Collaborative Visit simulation. The corrected experiment selects CBAA, ACBBA,
PI, and HIPC with unrestricted locally known candidates. The runtime retains
legacy DMCHBA/DGA support for old fixtures, but those algorithms are outside the
current comparison and must not enter a rerun configuration. Bayesian/Top-K
modules are not included in a device bundle, and the deployed worker rejects
its legacy one-shot allocator path.

## What the replay measures

For a causal trial, the host keeps one persistent MicroPython VM and four
logical robot contexts. Trial setup does not reveal a complete 50-task
universe. The resident target registry starts with only delivered admissions
and appends a coordinate when its reliable environment announcement reaches
that logical robot.

The host stages already decoded allocator events in causal order. Admission
hooks, peer-consensus messages, allocator-native task-completion repair, and a
robot-requested recovery action execute as the first part of the next timed
allocator call. The same timer then covers goal selection, bundle/path repair,
and bidding. State/context setup, USB transport, wire decoding, snapshots,
outbound extraction, result serialization, journaling, and explicit pre-call
garbage collection remain outside the allocator timer.

Admission is idempotent and non-destructive: it cannot clear a valid goal,
claim, bundle, or path. The legacy epoch-reset timing component is zero. CBAA
bids over every locally known active task while retaining one current task;
ACBBA, PI, and HIPC have no bundle-size cap. Every accepted physical call still
verifies candidate counts, result/message/mechanism parity, and post-state
parity against the authoritative host call.

CBAA retains the auction-time value of a valid current claim while the robot
moves. Neither the host nor native runtime may recompute or rebroadcast that
value solely because position changed. A new bid requires a real claim
lifecycle event such as outbid, completion, invalidation, targeted recovery
release, or selection of another task.

The standalone release-trace HIL commands below are retained for protocol and
historical motionless replay. They do not contain the full causal stream of
peer/completion/recovery events and therefore are not, by themselves, the
hardware arm of the corrected experiment. Hardware timings for the rerun must
come from the causal simulation provider and fresh output roots.

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
and duplicate-epoch idempotence. Current-study evidence must cover the four
primary allocators.

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

The supplied pilot and selected standalone HIL campaign are archived
pre-correction configurations. DMCHBA and DGA remain technically selectable,
which is precisely why a fresh current-study configuration must explicitly
allow only CBAA, ACBBA, PI, and HIPC.
