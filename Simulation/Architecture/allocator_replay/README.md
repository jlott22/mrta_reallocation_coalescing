# Multi-Pololu Motionless Allocator Replay

This subsystem captures real allocator-call state from both simulators and
replays those calls on one or more USB-connected Pololus without initializing
motors or sensors. It is independent of the normal robot programs:

- `Hardware/Algorithms/Pololu_*.py` files are never imported, edited, deployed, or
  overwritten.
- neither simulator repository is modified; capture uses external wrappers.
- deployment uploads only `replay_*.mpy` modules and never writes `main.py`.
- every physical result is tied to `machine.unique_id()`, build hash, fixture
  hash, and append-only attempt journal.

## Study grid

The held-out Bayesian cohort is trials 500–549 from seed `20260727`. The
collaborative cohort regenerates seed `20311176`, verifies trials 0–99 against
the existing scenario list, then takes continued draws 100–149.

Bayesian uses nine Top-K levels: `K=1`, 1%, 3%, 5%, 10%, 25%, 50%, 75%, and
100%. Their exact cell limits are 1, 4, 11, 18, 36, 90, 181, 271, and 361.
Collaborative visit uses eight levels: `K=1`, `K=2`, 5%, 10%, 25%, 50%, 75%,
and 100%, with exact target limits 1, 2, 3, 5, 13, 25, 38, and 50. Across six
algorithms this is 102 conditions: 54 Bayesian and 48 collaborative.

All 50 cohort scenarios are captured so later phases do not require a new
scenario draw. The initial hardware pilot replays the first 25 Bayesian trials
(500–524) and first 10 collaborative trials (100–109) in every condition.
Those exact trial IDs are sealed into the campaign schedule.

Each fixture contains complete pre-call robot, allocator, belief, cache, peer,
and RNG state; expected goal/messages; complete expected post-call state; call
classification; and simulator timing/risk metadata. Fixtures are deterministic
gzip JSONL shards under `Results/HIL/AllocatorReplay/Traces/`.

Before device transfer, the host verifies sealed shard, source, configuration,
and allocator-port hashes. It removes host-only expected-state duplication,
binary-packs arrays and RNG state, and streams state attributes in small,
independently CRC-checked parts. The controller decodes each part directly into
native allocator state. Transport and JSON duplication therefore cannot be
misclassified as allocator memory use; if the native logical state itself
cannot fit, repeated attempts still become `memory_unusable`.

## Host setup

From the repository root:

```powershell
$env:PYTHONPATH=(Resolve-Path "Simulation/Architecture").Path
python -m pip install -r Simulation/Architecture/allocator_replay/requirements-host.txt
python -m allocator_replay capture
```

`capture` defaults to 75% of logical CPU cores and resumes complete 50-trial
conditions. It regenerates any partial development trace.

## Hardware handoff

Connect one, two, three, or more Pololus, then run:

```powershell
python -m allocator_replay discover --ports auto
python -m allocator_replay build-device --ports auto
python -m allocator_replay deploy --ports auto
python -m allocator_replay preflight --ports auto
python -m allocator_replay run --ports auto
```

Discovery interrupts the current MicroPython program to a friendly REPL and
identifies boards by `machine.unique_id()`. Build detection requires every
board to use the same MicroPython major/minor version. Preflight verifies exact
firmware and deployed-module-set hashes, interpreter, CPU frequency, free heap,
double-array support, motor and sensor inactivity, parity smoke fixtures for
all 12 mission/algorithm ports, and cross-device timing medians within 5%.

The hardware campaign begins only after the exact connected device set passes
preflight. One board owns an entire mission × algorithm × Top-K condition.
Available boards dynamically claim the predicted-longest remaining condition.
Within a condition, simulator-predicted expensive calls run first.

The default `run` command is the 25/10-trial pilot. Later phases can select a
non-overlapping window without repeating pilot trials, for example:

```powershell
python -m allocator_replay run --ports auto `
  --campaign hardware_expansion `
  --bayesian-trial-start 25 --bayesian-trials 25 `
  --collaborative-trial-start 10 --collaborative-trials 40
```

Use these commands in another terminal:

```powershell
python -m allocator_replay status
python -m allocator_replay report
```

If a board disconnects, its condition pauses and remains pinned to that unique
device. Reconnect the same board and rerun the campaign command to resume. To
move a paused condition deliberately:

```powershell
python -m allocator_replay reassign `
  --condition collaborative_cbaa_topk_100_k50 `
  --device-id <destination-unique-id>
```

That condition is permanently marked mixed-device in reports.

## Timing and classifications

The replay worker measures only `choose_goal()` with `ticks_us()` and emits a
CRC-protected `TIMED` acknowledgement immediately when that call returns.
Fixture decode, parity hashing, messages, garbage collection, serial traffic,
and journaling are outside the measured interval. It records:

- complete allocator time, including candidate filtering;
- cumulative nested candidate-filter time;
- allocator-exclusive time (`complete - filter`);
- free heap before and after the call.

An acknowledged call that does not return in 30 seconds is interrupted and
confirmed twice. At least two timeouts classify only that condition as
`timing_unusable_30s`. Reproducible memory and parity failures become
`memory_unusable` and `parity_invalid`. USB errors do not count as timing
attempts. A condition is `hardware_feasible_30s` only if every captured call
passes parity and returns below 30 seconds.

Reports contain raw attempts plus robot-trial, system-trial, and condition CSVs.
Total, mean, median, p95, and maximum are reported separately for allocator,
filter, and allocator-exclusive time. Stopped conditions retain raw attempts
but have blank representative summary statistics.

## Pololu-authoritative live simulation

The HIL workflow is separate from offline capture/replay. Whenever the live
simulator reaches `choose_goal()`, the Pololu is authoritative:

```text
simulator environment state -> Pololu choose_goal()
Pololu goal/messages/state   -> simulator continues
```

The computer still simulates movement, sensing, and communications. It never
runs a shadow desktop allocator. Each board holds one robot allocator context
at a time; switching among the four simulated robots restores compact
allocator-owned state outside the timer. The 30-second watchdog begins only
after the board acknowledges that setup is complete, and ends as soon as
`choose_goal()` returns. Output streaming, USB, and journaling are also
outside the timer.

Each condition starts in a freshly soft-reset raw-REPL VM that bypasses
`main.py` and imports only the replay worker. A failed allocator attempt is
also retried from a fresh VM. This prevents code, interned strings, and heap
fragmentation from previously tested allocators from changing the next
allocator's memory budget. Logical robot contexts remain resumable within a
condition through bounded state restoration, while no allocator module state
is carried between conditions. The reset path never initializes motors or
sensors.

Bayesian uses 25 fixed clue-qualified historical trials from 0-499 at `K=1`,
1%, 3%, 5%, 10%, 25%, 50%, 75%, and 100%. Collaborative visit uses ten fixed
historical trials from 0-99 at `K=1`, `K=2`, 5%, 10%, 25%, 50%, 75%, and
100%. The same trial IDs are reused for all six algorithms and every Top-K
level: 102 conditions and 1,830 mission runs. Bayesian eligibility requires a
clue plus at least three post-clue allocator calls in every available
historical condition.

For a new study, build first and then prepare its immutable campaign against
that exact build:

```powershell
python -m allocator_replay build-device --ports auto
python -m allocator_replay hil-prepare `
  --campaign <new-campaign-name> `
  --build Simulation/Architecture/allocator_replay/builds/micropython_1_24_o0
```

The manifest freezes the device build, deployed module set, host HIL source,
both simulator source trees, scenario files, and historical evidence hashes.
`hil-run` refuses invalidated campaigns, source drift, scenario drift, or a
different build on either board.

After deployment and persistent preflight, start or resume:

```powershell
python -m allocator_replay hil-run --ports auto `
  --campaign pololu_native_persistent_v7
```

Status and reports use dedicated commands:

```powershell
python -m allocator_replay hil-status `
  --campaign pololu_native_persistent_v7
python -m allocator_replay hil-report `
  --campaign pololu_native_persistent_v7
```

Each accepted call records hardware allocator, candidate-filter, and
allocator-exclusive microseconds, filter invocation counts, candidate counts,
heap, call class, setup/timing/output phase, device/build identity, and run
generation. Completed trials also record wall time, total team steps, and
maximum steps by any robot. Raw-call, robot-trial, system-trial, and condition
CSVs carry the frozen provenance identifiers.

If a host stops mid-trial, that trial restarts from its historical scenario
with a new generation. Partial attempts remain in append-only journals but are
excluded from representative summaries. Reproducible allocator timeouts and
allocator-memory failures stop only the affected condition. Setup and output
failures have separate labels and are never reported as allocator feasibility.

### Former-failure hardware regression gates

Before a corrected full campaign is started, the focused regression command
runs the historical calls that previously exposed setup or output-memory
failures. Each gate follows the real serial-device and
`AuthoritativeBridge` path through its named target call, applies that Pololu
response to the simulator, completes one additional authoritative allocator
call, and stops at the next allocator entry. This is an intentional pass, not
a failed or partial mission. Gate call rows and checkpoints are kept outside
campaign data under `Results/HIL/AllocatorReplay/RegressionRuns/`.

List the seven built-in gates without opening a board:

```powershell
python -m allocator_replay hil-regression-gate --list-gates
```

Run or resume all gates across any number of preflighted boards:

```powershell
python -m allocator_replay hil-regression-gate `
  --ports COM12 COM13 `
  --run-id event_staging_and_output_streaming_v1 `
  --gates all `
  --build Simulation/Architecture/allocator_replay/builds/micropython_1_24_o0
```

Pass one or more gate IDs after `--gates` for a smaller run. Passed gates are
idempotently skipped when the same run ID is resumed. A USB interruption
leaves its gate pending with a new generation on resume; a reproducible
allocator/setup/output failure remains failed unless `--retry-failed` is
explicitly supplied. The immutable regression manifest binds the gate set,
scenario hashes, host/simulator source, and device build.

Use `--status` with the same run ID to inspect progress without opening either
serial port.

`pololu_authoritative_clue_qualified` is retained only as an invalidated
diagnostic campaign from the superseded per-call full-snapshot architecture.
Its journals remain auditable, but none of its timing or feasibility labels is
accepted for analysis.

`pololu_native_persistent_v2` and `pololu_native_persistent_v3` are also
invalidated diagnostic campaigns with zero accepted analysis samples. V2
exposed a context-setup batching defect before timing began. V3 fixed setup,
but revealed that running preflight and the campaign in one MicroPython VM
left DGA output without its native fresh-boot memory budget.

`pololu_native_persistent_v4` is invalidated for the DGA RNG-restore artifact:
its context restore used a CPython-only uninitialized-object path that is not
available in MicroPython. `pololu_native_integration_canary_v5` is invalidated
for the timeout-recovery artifact: each board reached the real 30-second
allocator boundary, but the host failed to return the interrupted worker to a
fresh VM for confirmation attempts.

`pololu_native_integration_canary_v6` passed its diagnostic purpose. On each
targeted 100% condition it preserved six completed calls, confirmed timeout
attempts 1-3 with three clean worker recoveries and no transport errors, and
correctly stopped only that condition as `timing_unusable_30s`. The canary is
sealed `integration_canary_only`; none of its rows are representative analysis
data. The current immutable analysis campaign is
`pololu_native_persistent_v7`, bound to build
`micropython_1_24_o0_5af608a14777`. It is a provenance-preserving
continuation containing 76 corrected or unfinished conditions and 1,387
mission runs. The same `hil-run` command safely resumes it after a host
interruption.

`pololu_native_persistent_v6` is frozen as a superseded partial campaign. Its
93 audited completed collaborative trials and 17 independently confirmed
30-second timing classifications retain their original v6 provenance. Fifteen
Bayesian typed-array host failures, Bayesian DMCHBA 10%, and unfinished work
were moved to v7; corrected source must never be resumed into v6. The
`CONTINUED_BY.json` and `CARRY_FORWARD.json` markers define the exact join
between the two append-only cohorts.

### Current hardware-validation status (2026-07-28)

The v6/v7 HIL memory classifications were subsequently traced to a
pre-timing event-batching and output-materialization heap artifact. They are
diagnostic only and are not representative native-hardware feasibility
results. The corrected build,
`micropython_1_24_o0_2539f0c4fe4d`, stages events and output in bounded chunks,
uses canonical post-call state caching, and runs each preflight allocator in a
fresh worker.

Both 125 MHz boards passed the corrected full preflight. Regression run
`event_staging_and_output_streaming_v1` then passed all seven exact historical
failure prefixes on generation 1 with zero failed call phases or failed gate
results. Every gate applied the target Pololu response to the live simulator
and completed a later authoritative hardware call.

No analysis campaign is currently running. Do not resume v6 or v7. A fresh v8
campaign will be prepared only after the final device set is connected; its
manifest will rerun the complete 102-condition, 1,830-mission matrix without
carrying forward v6/v7 timings or feasibility classifications.
