# Implementation test report

Date prepared: 2026-08-09 (America/Los_Angeles)

## Status

This report is the final local validation record for the integrated causal
tree. The final source-bound software campaign, its zero-compute arm, and both
content-validating resume runs completed on 2026-08-09. The independently
invoked automated suites passed **167/167 tests with zero failures/errors**.

No AGX Orin or physical RP2040 board was connected in the development
environment. Hardware checks are therefore **PENDING NATIVE EXECUTION**, not
PASS.

## Development environment

- OS: Windows 11 (`Windows-11-10.0.26200-SP0`)
- Architecture: AMD64
- Python: 3.13.14
- Detected logical processors: 22
- Branch at report preparation: `agent/reallocation-coalescing`
- Base/current HEAD before uncommitted causal work:
  `af7ec6a6fe64b60cc611fd1465e86c2d349d9857`
- Physical AGX/RP2040 hardware present: no
- Native timing evidence generated: no

The development host identity is diagnostic only. It cannot validate AGX
affinity/power behavior, MicroPython timing, USB behavior, or physical parity.

## Final validation commands and results

| Scope | Command | Result | Passed | Failed/errors |
|---|---|---|---:|---:|
| Python syntax/import | `python -m compileall -q known_visit_sim study Simulation/Architecture/allocator_replay Tests/HIL/AllocatorReplay` | PASS | all requested trees | 0 |
| Simulator/core | `python -m unittest discover -s known_visit_sim/tests -p 'test_*.py'` | PASS | 55 | 0 |
| Study/campaign | `python -m unittest discover -s study/tests -p 'test_*.py'` | PASS | 45 | 0 |
| Device/HIL software | `PYTHONPATH=Simulation/Architecture python -m unittest discover -s Tests/HIL/AllocatorReplay -p 'test_*.py'` (Linux syntax; equivalent environment used on Windows) | PASS | 67 | 0 |
| Shell syntax | `bash -n scripts/_agx_causal_common.sh scripts/agx_prepare_rp2040_boards.sh scripts/agx_native_environment_check.sh scripts/agx_rp2040_preflight.sh scripts/agx_causal_smoke.sh scripts/agx_causal_calibrate_rates.sh scripts/agx_causal_calibrate_timeout.sh scripts/agx_causal_variance_pilot.sh scripts/agx_freeze_experimental_design.sh scripts/agx_run_full_causal_campaign.sh scripts/agx_run_zero_compute_counterfactuals.sh scripts/agx_analyze_full_causal_campaign.sh` | PASS under Git Bash | 12 scripts | 0 |
| Causal CLI entry points | `python -m study.causal.{native,orchestrator,calibration,freeze,analysis,reports} --help` | PASS | 6 entry points | 0 |
| Patch hygiene | `git diff --check` plus conflict-marker/trailing-whitespace scan of tracked and untracked changed files | PASS | all changed files | 0 |
| Four-worker development integration | `causal_software_integration_v5`: four virtual bindings, eight causal jobs, eight zero-compute jobs, then one exact resume per arm | PASS; all 16 jobs completed and both resumes revalidated/skipped 8/8 | 32 job-state checks | 0 |
| Donor safety | Compare donor HEAD and full porcelain status to `SOURCE_AUDIT.md` | PARTIAL: all three HEADs unchanged; `topk_filter_study` and `dtca_benchmark_hardware` exactly match their recorded status. `dcta_benchmark_sim` has unrelated working-tree activity beyond the recorded snapshot and was left untouched. | 2 exact + 1 HEAD-only | 0 donor writes by this workflow |

Final totals across independently invoked test suites: **167 passed**.

Final failures/errors: **0**.

The final integration evidence is under
`study/output/causal_software_integration_v5/` and is intentionally ignored by
Git. Its effective config hash is
`72c344fa08ee32c00c97abd0b2b6cbc446aaa63821a2bb4c82d1139c9b2a580a`
and its source-tree hash is
`48cb18548f400b9696e9e43dda8beef371032dce4744530edc5a8d5d344c429d`.
The causal arm retained 5,250 call rows, 1,556 reallocation-event rows, 400 task
rows, and 2,757 movement rows. The zero-compute arm retained 3,752 call rows,
596 reallocation-event rows, 400 task rows, and 2,741 movement rows. All 16
missions completed; every development row remained
`hardware_validated=false`.

The immediate resume invocation for each arm started four workers, semantically
revalidated and skipped all eight completed jobs, executed zero jobs, and
recorded zero fatal worker events. Earlier versioned development evidence was
not overwritten: v1 exposed a timing-provider split mismatch, v2 exposed an
exact 64-bit runtime-seed validation defect, and both failed attempts remain
available. The integer validator was corrected without float conversion; v3
then passed; v4 verified the resulting behavior. A staged-content hygiene fix
removed one superfluous blank line from a Python file, so v5 was run under the
exact final staged source hash.

The read-only donor audit reproduced the recorded normalized porcelain hashes
for `topk_filter_study` (28 untracked entries) and
`dtca_benchmark_hardware` (one modified entry). `dcta_benchmark_sim` retained
its recorded HEAD but currently reports 193 entries (11 modified, 45 deleted,
137 untracked), compared with the recorded 100 (3 modified, 45 deleted, 52
untracked). Those additional analysis/result artifacts belong outside this
repository; this workflow neither created, changed, removed, nor cleaned them.

## Implemented automated coverage

The following coverage exists in the tree and must be included in the commands
above. This section describes test intent, not an execution claim.

### Causal simulator and timing

`known_visit_sim/tests/test_causal_timing.py` covers:

- predeclared event/stagnation horizons and retained algorithmic noncompletion;
- common-start calls completing independently instead of serial duration sum;
- frozen same-time views and staged message visibility;
- delivery of buffered messages only to a later eligible call;
- arrival/epoch/message ordering while a robot is compute-busy;
- another robot moving while its peer remains compute-busy;
- exact absolute release during compute with no future-information leak;
- parity and hardware-attestation spoof/mismatch rejection;
- schema-2 timing splits excluded from virtual compute;
- action-keyed movement timing across provider/policy interleavings;
- final-cell duration and task completion at arrival;
- exact release during movement;
- analytically known processor work, capacity, makespan, and zero-compute
  counterfactual metrics; and
- canonical raw causal tables.

Related online-reallocation tests continue to cover Eager, B2/B4/B8, bounded
timeouts, mandatory piggyback, residual final flush, lifecycle timestamps, and
manifest/release semantics.

### Board/session/parity software tests

`Tests/HIL/AllocatorReplay/test_causal_device.py` covers:

- physical sampling order not serializing virtual time;
- detaching all same-time inputs before first device I/O;
- four persistent isolated contexts and reset;
- goal, message, state, count, call-class, and behavioral-projection mismatch
  failures;
- duplicate group/call/event IDs and stale reply rejection;
- disconnect invalidation and exact reconnect revalidation;
- exact-four production binding by stable identity, not port order;
- process/worker board leases and build/firmware mismatch;
- four resident native runtime objects for all primary algorithms;
- split timer boundaries and timing decomposition;
- virtual/zero providers retaining independent-completion semantics without
  being marked hardware-valid;
- cross-layer structural compatibility;
- parity sensitivity to protocol timestamps, bundle/path metadata, pending/
  last-sent state, collision/timing state, and HIPC prediction state; and
- sealed machine/human preflight reports with virtual evidence labeled pending.

### Study orchestration, calibration, gates, and analysis

`study/tests/test_causal_campaign.py` covers:

- one-board paired blocks and deterministic balanced scheduling;
- distinct, deterministic zero-pair identities;
- fail-closed native config/path validation;
- external binding bytes in the effective configuration hash;
- cross-worker board lock exclusion;
- development evidence being unable to satisfy scientific freeze;
- known-answer Friedman and Wilcoxon behavior;
- rate selection independent of processor-work optimization; and
- variance review at the trial-replicate level.

The integrated study suites additionally exercise output semantic tamper
rejection, immutable promotion/resume/retry behavior, exact calibration
matrices, gate/source/build chain binding, freeze enforcement, exact
causal/zero pairing, current raw-output revalidation in the full report,
schedule tamper rejection, and immutable paper-analysis provenance.

## Software-only integration acceptance criteria

A development campaign may use deterministic virtual duration providers only
when it is labeled `development_override`. Acceptance requires:

- four worker processes with four distinct virtual board identities;
- one persistent provider/session per worker;
- one paired block staying on one worker/board;
- semantically validated raw call/event/task/movement/provenance tables;
- exact causal compute `start + device duration` arithmetic;
- a second invocation resuming completed jobs without overwrite;
- retained diagnostic output for intentionally injected corruption/failure; and
- no `hardware_validated=true` claim anywhere in virtual evidence.

Development integration cannot satisfy the native environment, preflight,
calibration, freeze, or full-publication gates.

## Hardware-dependent validation not run

The following remain pending until the real AGX and four intended boards are
connected:

- AGX model/JetPack/power/clock/thermal/core-affinity validation;
- real by-ID/UID stability, serial permissions, and exclusive board leases;
- native build deployment and firmware/module identity on four boards;
- MicroPython timer unit/resolution/monotonicity/wraparound behavior;
- four resident physical contexts, mission resets, heap reclamation, and
  deterministic seed initialization;
- per-call device timing decomposition under USB traffic;
- native goal/message/state/call-class parity for all four algorithms;
- physical disconnect, stale-response, and reconnect behavior;
- the complete twenty-item native preflight;
- 32-mission native smoke and content-validated resume;
- 280 rate, 300 timeout, and 240 variance calibration missions;
- operator-reviewed freeze;
- the final causal and zero-compute matrices; and
- full statistical/report provenance over native results.

These checks must be recorded in generated native reports and
`NATIVE_VALIDATION_CHECKLIST.md`. Until then, the correct status is **software
validated; native hardware validation pending**.

## Final sign-off block

Local validation completed at: **2026-08-09 12:31 PDT (UTC-07:00)**

Git HEAD/source-tree hash tested:
**`af7ec6a6fe64b60cc611fd1465e86c2d349d9857` /
`48cb18548f400b9696e9e43dda8beef371032dce4744530edc5a8d5d344c429d`**

Simulator suite: **55/55 PASS**

Study suite: **45/45 PASS**

HIL/device software suite: **67/67 PASS**

Integration/resume result: **8/8 causal + 8/8 zero-compute complete; both
second invocations revalidated/skipped 8/8 with no recomputation**

Donor repository verification: **all three HEADs unchanged; two working trees
exactly unchanged; `dcta_benchmark_sim` has separately observed working-tree
activity beyond the recorded baseline and was not altered or cleaned here**

Native AGX + four RP2040 validation: **PENDING NATIVE EXECUTION**
