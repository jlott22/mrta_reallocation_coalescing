# AGX Orin + four-RP2040 native runbook

## Purpose and stop rule

Follow this order from a clean clone on the AGX Orin. Do not skip, manually edit
a report to PASS, or launch the full campaign before design freeze. A command
that fails is a gate failure; inspect its machine-readable evidence and correct
the underlying condition.

The clone command below targets the published `main` branch. Record its exact
commit before starting the scientific gates and do not mix outputs produced by
different commits.

No physical hardware validation has been performed on the development machine.
The first AGX execution establishes the real evidence.

## 1. Hardware and software prerequisites

Required hardware:

- one NVIDIA Jetson AGX Orin;
- exactly four intended RP2040/Pololu boards, each with a data-capable USB
  connection and stable unique identity; and
- enough free storage for calibration, 1,500 causal missions, 1,500
  zero-compute conditions, failed attempts, and analysis (the environment gate
  requires at least 5 GiB free).

Required host software:

- Linux/JetPack with `python3` 3.10 or newer;
- Git;
- access to `nvpmodel`, `jetson_clocks`, and `lsusb` for provenance;
- permission to read/write the four serial devices; and
- Python packages in
  `Simulation/Architecture/allocator_replay/requirements-host.txt`
  (`pyserial`, `mpremote`, and compatible `mpy-cross`).

The boards must already run the intended MicroPython firmware. The preparation
script deploys the motor-free replay modules; it does not flash RP2040 firmware,
start motors/sensors, or replace `main.py`.

## 2. Clone and create the Python environment

Run from the parent directory in which the repository should live:

```bash
git clone --branch main \
  https://github.com/jlott22/mrta_reallocation_coalescing.git \
  mrta_reallocation_coalescing
cd mrta_reallocation_coalescing
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r Simulation/Architecture/allocator_replay/requirements-host.txt
git status --short
git rev-parse HEAD
```

`git status --short` must be empty before scientific gates. Local bindings,
native gate reports, generated manifests, and raw outputs use contained ignored
paths; do not edit tracked source after the environment gate begins. If source
must change, treat it as a new campaign version and repeat every gate.

Optional pre-hardware software verification:

```bash
python -m unittest discover -s known_visit_sim/tests -v
python -m unittest discover -s study/tests -v
PYTHONPATH="$PWD/Simulation/Architecture${PYTHONPATH:+:$PYTHONPATH}" \
  python -m unittest discover -s Tests/HIL/AllocatorReplay -v
```

## 3. Identify four stable ports

Connect all four boards and prefer stable by-ID links:

```bash
ls -l /dev/serial/by-id/
lsusb
```

Choose exactly four distinct `/dev/serial/by-id/...` paths. If the account lacks
serial permission, add it to the distribution's serial group (commonly
`dialout`), log out/in, and verify access before continuing. Do not run the
campaign as root merely to bypass an unresolved ownership problem.

Select worker cores if cores 0-3 are not suitable. They must be four distinct
online logical cores and four workers must fit within 75% of the detected
logical cores:

```bash
export CAUSAL_CORE_AFFINITIES=0,1,2,3
```

## 4. Build, deploy, discover, and seal board bindings

Set the ports in the intended A/B/C/D order and run the preparation command:

```bash
PORTS=/dev/serial/by-id/<A>,/dev/serial/by-id/<B>,/dev/serial/by-id/<C>,/dev/serial/by-id/<D>
bash scripts/agx_prepare_rp2040_boards.sh "$PORTS"
```

This command:

- installs/checks host dependencies;
- builds compiled device modules;
- deploys them to all four explicit ports;
- queries live UID, firmware, build, module-set, MicroPython, CPU-frequency,
  and timer evidence; and
- writes the effective local binding to
  `configs/local/agx_board_bindings.json`.

It also writes
`study/native_gates/device_build/device_build_deployment.json`. Identity files
are immutable: a different rerun is refused rather than overwriting earlier
evidence. Verify that the four UIDs are distinct and that all boards report the
same intended build/module set:

```bash
python -m json.tool configs/local/agx_board_bindings.json
python -m json.tool study/native_gates/device_build/device_build_deployment.json
```

Do not hand-replace hashes with placeholders from
`configs/agx_board_bindings.example.json`.

## 5. Record the AGX environment

The first run deliberately writes a FAIL report so the power/clock state can be
reviewed without an automatic system change:

```bash
bash scripts/agx_native_environment_check.sh
```

The nonzero exit is expected on this first invocation. Read both:

```text
study/native_gates/environment/native_environment_report.json
study/native_gates/environment/NATIVE_ENVIRONMENT_REPORT.md
```

Confirm at minimum:

- the model is AGX Orin and the JetPack/L4T/kernel/Python identity is plausible;
- the selected cores exist and are distinct;
- four workers fit within the 75%-of-logical-cores safety cap;
- all serial paths exist and are readable/writable;
- the repository is clean and manifest/config hashes match;
- storage is sufficient;
- `nvpmodel -q` and `jetson_clocks --show` succeeded; and
- the recorded governor/frequency/temperature/memory state is suitable and can
  be held consistently for every native stage.

The script never invokes `sudo` and never changes clocks or power mode. If a
different mode or fixed-clock policy is needed, make that explicit operational
decision outside the study script, allow the system to stabilize, then rerun
the report. When the recorded state is deliberately accepted:

```bash
ACCEPT_RECORDED_POWER_CLOCK_STATE=YES \
  bash scripts/agx_native_environment_check.sh
```

The environment report must show `scientifically_valid: true`.

## 6. Run the twenty-check native preflight

```bash
bash scripts/agx_rp2040_preflight.sh
```

Inspect:

```text
study/native_gates/preflight/native_preflight_report.json
study/native_gates/preflight/NATIVE_PREFLIGHT_REPORT.md
```

The JSON must show `hardware_valid`, `hardware_validated`, and `passed` true,
with all twenty named checks PASS. A loopback/virtual report is never sufficient.
Keep the boards connected to the same ports after this point.

## 7. Run and review the causal smoke

```bash
bash scripts/agx_causal_smoke.sh
```

The script runs the 32-mission smoke and then invokes the orchestrator a second
time to prove content-validated resume. Review:

```text
study/native_gates/smoke/causal_smoke_report.json
study/native_gates/smoke/NATIVE_CAUSAL_SMOKE_REPORT.md
study/output/agx_causal_smoke_v1/campaign_execution_report.json
```

Require all four algorithms, both policies, both provisional loads, multiple
traces, and all four boards; exact worker/core binding; completed missions;
clean parity; no cross-board or stale-context evidence; valid compute/movement
arithmetic; and a successful no-recompute resume.

## 8. Calibrate arrival rates and review the proposal

Run the full seven-rate sweep:

```bash
bash scripts/agx_causal_calibrate_rates.sh
```

Read:

```text
study/native_gates/calibration/rate_calibration_report.json
study/native_gates/calibration/NATIVE_RATE_CALIBRATION_REPORT.md
```

The first report is a proposal and should require review. Inspect every
algorithm, completion rate, pending-depth/overlap pressure, task latency,
events, and processor work. Select common low/medium/high *load IDs* from the
candidate IDs (`rate_003`, `rate_0075`, `rate_015`, `rate_03`, `rate_06`,
`rate_12`, `rate_24`). Do not select rates to make B4 favorable.

Seal the three reviewed IDs by rerunning the summarizer through the same script;
the 280 completed missions resume rather than rerun:

```bash
REVIEW_LOW_LOAD=rate_0075 \
REVIEW_MEDIUM_LOAD=rate_03 \
REVIEW_HIGH_LOAD=rate_12 \
  bash scripts/agx_causal_calibrate_rates.sh
```

The values above are syntax examples, **not predetermined final selections**.
Use only the IDs supported by the native report. Verify that
`review_required_before_freeze` is false and the reviewed pressure ordering is
strictly increasing. If the reviewed triplet differs from the generated
proposal, also set `REVIEW_RATE_JUSTIFICATION` to a concise scientific reason;
an unexplained override is rejected.

## 9. Calibrate and seal bounded timeout W

Generate the reviewed-rate stage and run Eager plus W=2/5/10/20 s:

```bash
bash scripts/agx_causal_calibrate_timeout.sh
```

Read:

```text
study/native_gates/calibration/timeout_calibration_report.json
study/native_gates/calibration/NATIVE_TIMEOUT_CALIBRATION_REPORT.md
study/native_gates/calibration/timeout_stage_config.json
```

Choose one candidate that is eligible under the documented common-W rule, then
seal it without rerunning completed conditions:

```bash
REVIEW_TIMEOUT_S=5 bash scripts/agx_causal_calibrate_timeout.sh
```

`5` is a syntax example only. Allowed candidates are 2, 5, 10, and 20 s. Do not
tune W by algorithm. If the reviewed eligible W differs from the generated
proposal, also set `REVIEW_TIMEOUT_JUSTIFICATION`; the reason is sealed into
the calibration report and final design freeze.

## 10. Run the variance/n pilot and seal n

```bash
bash scripts/agx_causal_variance_pilot.sh
```

Read:

```text
study/native_gates/calibration/variance_calibration_report.json
study/native_gates/calibration/NATIVE_VARIANCE_REPORT.md
study/native_gates/calibration/variance_stage_config.json
```

Review all algorithm/load paired effects and the exploratory n=25 precision
diagnostic. Seal the supported count explicitly:

```bash
REVIEW_TRACE_COUNT=25 bash scripts/agx_causal_variance_pilot.sh
```

Use 25 only if supported. If the report recommends 50, record that deviation
and seal 50 instead of silently retaining 25. The only predeclared final
choices are 25 and 50. If the reviewed choice differs from the report's
proposal, supply a nonempty scientific rationale explicitly:

```bash
REVIEW_TRACE_COUNT=50 \
REVIEW_TRACE_COUNT_JUSTIFICATION="State why the reviewed protocol overrides the pilot proposal" \
  bash scripts/agx_causal_variance_pilot.sh
```

The freeze gate rejects arbitrary smaller values and rejects an unexplained
override.

## 11. Freeze the reviewed experimental design

Re-read the environment, preflight, smoke, rate, timeout, and variance reports.
Confirm the repository, manifests, board cohort, and device build did not change.
Then:

```bash
ACCEPT_REVIEWED_DESIGN=YES \
  bash scripts/agx_freeze_experimental_design.sh
```

Review the generated files under:

```text
study/frozen/native_causal_v1/
```

At minimum inspect `design_freeze.json`, `FINAL_DESIGN_FREEZE.md`, and
`agx_full_causal_frozen.json`. Confirm final rates, W, n, algorithm/policy list,
independent final seed, manifest/release hashes, source/commit, analysis version,
and four UID/build/firmware/module identities. Do not edit frozen JSON.

## 12. Run/resume the full causal campaign

```bash
bash scripts/agx_run_full_causal_campaign.sh
```

The target n=25 run contains 1,500 missions. Exactly four workers run at a time,
each fixed to its core and board. It is safe to rerun the same command after a
normal interruption: completed jobs are hashed and semantically revalidated,
then skipped. Failed attempts remain in the attempts tree; bounded technical
retries are explicit. Algorithmic incompletions remain scientific outcomes.

Monitor the machine without modifying campaign files. Useful read-only checks:

```bash
ps -eo pid,psr,pcpu,pmem,cmd | grep -E 'study.causal|run_causal_trials'
watch -n 10 'find study/output -name completion.json | wc -l'
```

Do not start a second orchestrator against the same four boards. UID leases
will refuse it, but avoiding concurrent operators keeps diagnostics clear.

Review the causal execution report in the frozen config's `campaign.output_root`.
It must account for every planned job, retry, technical failure, and algorithmic
outcome and must report native hardware validation.

## 13. Run/resume zero-compute counterfactuals

Only after the causal run is complete:

```bash
bash scripts/agx_run_zero_compute_counterfactuals.sh
```

This stage uses the frozen condition identities and zero allocator duration. It
does not replay stored call sequences and does not need to preserve native-run
decisions or trajectories. Reinvoke the same command to resume. Verify exact
condition coverage in `zero_compute_execution_report.json`.

## 14. Analyze and generate the final report

```bash
bash scripts/agx_analyze_full_causal_campaign.sh
```

The analyzer must use the frozen full config, revalidate every promoted causal
and zero job plus its hashes, require exact condition coverage, and write
trial-level statistics/figure tables and:

```text
<campaign-output-root>/analysis/
<campaign-output-root>/full_campaign_report.json
<campaign-output-root>/FULL_CAMPAIGN_REPORT.md
```

Before using results in the paper, confirm the final report binds the design
freeze, source, config, manifest, schedules, execution reports, board cohort,
and analysis input hashes. It must expose incomplete/excluded conditions and
complete zero-pair coverage. Negative allocation effects must be flagged rather
than clipped.

## 15. Resume and recovery rules

- Re-run the same stage command after interruption. Do not copy partial files
  into `completed/` or manufacture `completion.json`.
- On the AGX, `Ctrl-C` asks each child worker to unwind normally. Each worker
  closes its serial timing provider (which releases its UID-based `BoardLease`)
  and then releases its campaign-output board lock before exiting. Wait for the
  launcher to return before invoking the stage again.
- A valid promoted job is skipped only after content hashes and semantics pass.
- A corrupted or mismatched completed directory is a conflict, not an
  overwrite target. Preserve it for diagnosis.
- Disconnect during a mission is a technical failure. Reconnect the exact board
  only between missions, confirm stable by-ID path, and rerun the stage so live
  identity/build checks occur.
- If firmware/module build, board cohort, source commit, config, manifest, rate,
  W, or n changes, create a new versioned campaign/freeze and repeat upstream
  gates. Never combine data across identities.
- Do not delete failed attempts to improve reported completion. Technical retry
  history is part of the review evidence.

If the AGX lost power, the launcher was killed with `SIGKILL`, or a worker could
not finish cleanup, first use `ps` to establish that no orchestrator or causal
worker from that run remains alive. Then invoke recovery with the **exact config
that created the interrupted run**, for example the frozen full config:

```bash
python -m study.causal.native --repo-root "$PWD" recover-stale-locks \
  --config study/frozen/native_causal_v1/agx_full_causal_frozen.json
```

The recovery command derives exactly eight possible paths from that validated
config: four locks under its configured `campaign.output_root/board_locks/` and
four UID-hashed leases under `study/native_device_leases/`. It does not list a
directory, expand a glob, or remove an unconfigured filename. Before deleting
anything, it validates every present lock's JSON schema, current hostname,
configured board/serial/worker binding or immutable UID, and uses the POSIX
non-signalling PID probe to prove the owner no longer exists. If any present
lock is live, belongs to another host, is malformed, has the wrong board/UID,
changed during inspection, or has an unprovable PID state, recovery exits 2 and
deletes nothing. Preserve the diagnostic and investigate; do not manually
remove a lock merely to make the next launch proceed.

## 16. Common failures

### Fewer than four boards or duplicate UID

Check cables, by-ID paths, permissions, and board identity. Rerun preparation
and all subsequent gates only with four unique intended devices. A development
override is not publication evidence.

### Build/firmware/module mismatch

Stop. Verify the deployed build report and the actual board. Do not edit binding
hashes. Redeploy consistently and restart the versioned gate chain.

### Dirty repository or source-hash mismatch

Use `git status --short` to identify the change. Do not use a dirty override for
a scientific stage. Preserve intentional work in a new commit, then repeat the
gates because earlier source-bound evidence no longer applies.

### Parity mismatch

Preserve the attempt logs and attestation diagnostics. Do not accept its timing,
substitute a duration, or loosen parity. Diagnose AGX/native state/message/call
classification before starting a new source/build version.

### Serial timeout/disconnect/stale response

The active mission is invalid. Check USB power/cabling and system logs. Resume
only after identity/build revalidation. Repeated faults may require replacing a
board and starting a new frozen cohort.

### Environment gate fails only on power/clock acknowledgment

Review the recorded `nvpmodel` and `jetson_clocks` state. If it is scientifically
acceptable and will remain consistent, rerun with
`ACCEPT_RECORDED_POWER_CLOCK_STATE=YES`. The acknowledgment records a deliberate
choice; it does not configure the system.

### Algorithmic horizon outcome

Do not retry it as infrastructure. Retain the incomplete outcome and diagnostic
state. If it prevents the planned complete-block analysis, report the pattern
and decide whether a prospective new protocol version is required.

## 17. Reviewer package

Provide the reviewer with:

- `CAUSAL_IMPLEMENTATION_REPORT.md`, `EXPERIMENTAL_PLAN.md`, this runbook, and
  `NATIVE_VALIDATION_CHECKLIST.md`;
- native environment, preflight, smoke, rate, timeout, and variance reports;
- the final design-freeze directory;
- causal and zero schedules/execution reports and retained failure summaries;
- `FULL_CAMPAIGN_REPORT.md` plus analysis metadata/tables; and
- repository commit/status and the four board/build identities.

Do not label the reviewer package hardware-valid until every applicable item in
`NATIVE_VALIDATION_CHECKLIST.md` is marked PASS with its evidence path.
