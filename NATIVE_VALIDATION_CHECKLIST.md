# Native validation checklist

> **Corrected-rerun checklist.** Any older filled copy of this checklist is
> pre-correction evidence. New validation must additionally demonstrate strict
> non-piggyback admission, dynamic admitted-only target registration,
> non-destructive state retention, and the inclusive allocator-transaction
> timer described below.

Campaign/version: ____________________  Date: ____________________

Operator: ____________________________  AGX Git commit: ____________________

This checklist is an operator index to machine-readable evidence, not a
substitute for it. Mark exactly one of PASS/FAIL and record the report path or
hash. Any required FAIL means the data are not publication-valid.

## A. Repository and AGX environment

- [ ] PASS  [ ] FAIL — Clean committed standalone repository; no donor path,
  symlink, or runtime dependency. Evidence: ______________________________
- [ ] PASS  [ ] FAIL — AGX Orin model, JetPack/L4T, kernel, Python, logical CPU
  count, and Git/source identity recorded. Evidence: ______________________
- [ ] PASS  [ ] FAIL — Three distinct selected affinities exist; exactly three
  workers fit within 75% of logical cores. Evidence: _______________________
- [ ] PASS  [ ] FAIL — OMP/OpenBLAS/MKL/NumExpr/vecLib/BLIS thread count is one
  for workers. Evidence: _________________________________________________
- [ ] PASS  [ ] FAIL — `nvpmodel` and `jetson_clocks --show` succeeded; chosen
  power/clock/governor policy was reviewed and explicitly accepted without a
  silent script-side change. Evidence/rationale: __________________________
- [ ] PASS  [ ] FAIL — Temperatures, memory, and at least 5 GiB free storage
  recorded and acceptable. Evidence: _____________________________________
- [ ] PASS  [ ] FAIL — Three stable by-ID serial paths exist and are readable/
  writable. Evidence: ____________________________________________________
- [ ] PASS  [ ] FAIL — Config, manifest index, hardware binding, source-tree,
  and Git identities agree. Evidence: ____________________________________

Environment JSON: `study/native_gates/environment/native_environment_report.json`

Overall environment gate: [ ] PASS  [ ] FAIL

## B. Required twenty-item RP2040 preflight

1. [ ] PASS  [ ] FAIL — Three unique intended RP2040 boards detected.
   Evidence: _____________________________________________________________
2. [ ] PASS  [ ] FAIL — Each board identity is stable across live queries.
   Evidence: _____________________________________________________________
3. [ ] PASS  [ ] FAIL — Firmware, build, and module-set hashes match the sealed
   deployment. Evidence: _________________________________________________
4. [ ] PASS  [ ] FAIL — MicroPython/native runtime version is correct; no
   virtual/loopback identity is accepted. Evidence: _______________________
5. [ ] PASS  [ ] FAIL — Device timer unit, resolution, monotonicity, and
   wraparound handling are verified. Evidence: ____________________________
6. [ ] PASS  [ ] FAIL — Four logical contexts can be created and reset.
   Evidence: _____________________________________________________________
7. [ ] PASS  [ ] FAIL — CBAA, ACBBA, PI, and HIPC load on every board.
   Evidence: _____________________________________________________________
8. [ ] PASS  [ ] FAIL — Online task-set growth works.
   Evidence: _____________________________________________________________
9. [ ] PASS  [ ] FAIL — Context state persists across calls within a mission.
   Evidence: _____________________________________________________________
10. [ ] PASS  [ ] FAIL — Same-time calls freeze all pre-call views; physical
    request order does not alter semantics or virtual completion times.
    Evidence: ____________________________________________________________
11. [ ] PASS  [ ] FAIL — Native and AGX selected goals match known cases.
    Evidence: ____________________________________________________________
12. [ ] PASS  [ ] FAIL — Ordered outbound messages and complete behavioral
    state signatures match at the sealed parity level.
    Evidence: ____________________________________________________________
13. [ ] PASS  [ ] FAIL — Duplicate group/call/event IDs are rejected.
    Evidence: ____________________________________________________________
14. [ ] PASS  [ ] FAIL — Contexts and heap/module state reset between missions.
    Evidence: ____________________________________________________________
15. [ ] PASS  [ ] FAIL — Response/attempt IDs cannot cross workers or trials;
    stale responses are rejected. Evidence: ______________________________
16. [ ] PASS  [ ] FAIL — Device allocator time covers ordered allocator input
    application, admission/completion hooks, targeted recovery when requested,
    and goal selection as one transaction. USB, wire decoding, generic PSETUP
    state synchronization, explicit pre-call GC, outbound extraction, and
    result serialization are excluded. The legacy epoch-reset component is
    zero.
    Evidence: ____________________________________________________________
17. [ ] PASS  [ ] FAIL — Disconnect causes fail-closed active-trial termination
    and no duration is accepted. Evidence: ________________________________
18. [ ] PASS  [ ] FAIL — Reconnection is allowed only between missions after
    exact UID/build/firmware/module revalidation. Evidence: _______________
19. [ ] PASS  [ ] FAIL — Resume rejects a different board/device build.
    Evidence: ____________________________________________________________
20. [ ] PASS  [ ] FAIL — Campaign cannot mark data hardware-valid when any
    preflight requirement fails. Evidence: ________________________________

Preflight JSON: `study/native_gates/preflight/native_preflight_report.json`

All 20 checks PASS: [ ] YES  [ ] NO

`hardware_valid=true`, `hardware_validated=true`, `passed=true`: [ ] YES [ ] NO

Overall preflight gate: [ ] PASS  [ ] FAIL

## C. Native timing and attestation spot check

- [ ] PASS  [ ] FAIL — At least one call per algorithm/board has a sealed
  hardware attestation bound to board label, UID, build, firmware, module set,
  mission, group, call, attempt, and context. Evidence: ____________________
- [ ] PASS  [ ] FAIL — `rp2040_device_duration_s` alone determines virtual
  compute completion `t+d`; serial/setup/AGX timings do not. Evidence: ______
- [ ] PASS  [ ] FAIL — `host_serialization_setup_measured=false` and its schema-2
  value is zero; no claim of separately measured host serialization is made.
  Evidence: _____________________________________________________________
- [ ] PASS  [ ] FAIL — Serial round trip equals PSETUP transaction plus PTIME/
  result transaction wall times and is excluded from causal compute.
  Evidence: _____________________________________________________________
- [ ] PASS  [ ] FAIL — Explicit pre-call GC is outside the allocator timer;
  natural GC during allocator input handling, recovery, or goal selection
  remains included.
  Evidence: _____________________________________________________________
- [ ] PASS  [ ] FAIL — Trial setup contains no future-task universe; each
  resident context registers a new task only from its delivered admission
  event. Evidence: ______________________________________________________
- [ ] PASS  [ ] FAIL — Admission preserves current goal/claims/path, CBAA sees
  the full admitted pool, and ACBBA/PI/HIPC have no bundle cap.
  Evidence: _____________________________________________________________
- [ ] PASS  [ ] FAIL — A retained CBAA claim keeps its auction-time bid after
  position/movement deltas; desktop/native parity shows no movement-only bid
  refresh or outbound rebroadcast.
  Evidence: _____________________________________________________________

## D. Causal smoke

- [ ] PASS  [ ] FAIL — 32 planned smoke missions accounted for across all four
  algorithms, Eager/B4, two loads, two traces, three boards/workers.
- [ ] PASS  [ ] FAIL — Second invocation resumed and revalidated completed jobs
  without recomputing/overwriting them.
- [ ] PASS  [ ] FAIL — Every promoted call has clean parity and exact board/
  context binding; no stale or cross-board state.
- [ ] PASS  [ ] FAIL — Same-time groups use independent virtual completion;
  releases during compute occur where expected; other robots continue.
- [ ] PASS  [ ] FAIL — No assignment predates release; movement completion
  arithmetic and final mission elapsed arithmetic are exact.
- [ ] PASS  [ ] FAIL — Three pinned workers remained mapped one-to-one to three
  boards with no hidden numerical oversubscription.

Smoke report: `study/native_gates/smoke/causal_smoke_report.json`

Overall smoke gate: [ ] PASS  [ ] FAIL

## E. Calibration and review

- [ ] PASS  [ ] FAIL — Rate calibration accounts for 280 native-timed missions
  and processor work was excluded from the rate-selection pressure index.
- [ ] PASS  [ ] FAIL — Common low/medium/high load IDs are healthy, strictly
  ordered by rate and observed pressure, reviewed across all algorithms, and
  explicitly sealed. Selected: low ______ medium ______ high ______
- [ ] PASS  [ ] FAIL — Timeout calibration accounts for 300 missions; one common
  eligible W was reviewed rather than algorithm-tuned. W: ______ s
- [ ] PASS  [ ] FAIL — Variance pilot accounts for 240 missions; trial is the
  replicate and the n recommendation was reviewed. Frozen n: ______
- [ ] PASS  [ ] FAIL — Calibration seed/cohort (`20260808`) is distinct from the
  final cohort seed (`2026080905`) with zero signature overlap.

Rate report: `study/native_gates/calibration/rate_calibration_report.json`

Timeout report: `study/native_gates/calibration/timeout_calibration_report.json`

Variance report: `study/native_gates/calibration/variance_calibration_report.json`

Overall calibration gate: [ ] PASS  [ ] FAIL

## F. Design freeze

- [ ] PASS  [ ] FAIL — Operator reviewed and accepted all six exact gate kinds.
- [ ] PASS  [ ] FAIL — Final rates, W, n, algorithms, five policies, scenario/
  release hashes, source/commit, analysis version, schedule inputs, and four
  board/build identities are sealed.
- [ ] PASS  [ ] FAIL — Frozen config is non-development, clean-source, exact-four
  workers/boards, and was not manually edited.

Design ID: ____________________  Freeze SHA-256: __________________________

Freeze report: `study/frozen/native_causal_v1/FINAL_DESIGN_FREEZE.md`

Overall freeze gate: [ ] PASS  [ ] FAIL

## G. Full causal and zero-compute campaigns

- [ ] PASS  [ ] FAIL — Causal execution report accounts for every frozen job,
  retained technical attempt, retry, and algorithmic outcome.
- [ ] PASS  [ ] FAIL — Every promoted causal job revalidates hashes/semantics and
  reports native hardware validity plus parity.
- [ ] PASS  [ ] FAIL — All five policies in every paired block used the same
  board; algorithms, loads, traces, and policy order are balanced.
- [ ] PASS  [ ] FAIL — Algorithmic incomplete/unfavorable outcomes are retained
  and not converted into technical retries or dropped.
- [ ] PASS  [ ] FAIL — Zero-compute execution accounts for exactly the frozen
  condition set and preserves exogenous identity without replaying call lists.
- [ ] PASS  [ ] FAIL — Causal/zero pairing coverage is exact; missing and extra
  pair counts are zero.

Planned causal jobs: ______  Promoted: ______  Algorithmically complete: ______

Retained technical attempts: ______  Permanently failed jobs: ______

Planned zero jobs: ______  Promoted: ______  Exact complete pairs: ______

Overall execution gate: [ ] PASS  [ ] FAIL

## H. Analysis and reviewer package

- [ ] PASS  [ ] FAIL — Analysis revalidated frozen config, design, schedules,
  execution reports, completion markers, and all raw output hashes.
- [ ] PASS  [ ] FAIL — Trial-level complete blocks are the inferential units;
  task rows are descriptive only.
- [ ] PASS  [ ] FAIL — Friedman, conditional Wilcoxon/Holm, paired effects, and
  deterministic paired confidence intervals are reported without fabricated
  significance.
- [ ] PASS  [ ] FAIL — Outcome/exclusion audit, completion rates, zero-pair
  coverage, and negative D_alloc flags are present.
- [ ] PASS  [ ] FAIL — Figure/table data and final report bind the frozen
  analysis version and input hashes.
- [ ] PASS  [ ] FAIL — Claims say RP2040-timed causal virtual mission, not four
  physical processors per mission or physical robot mission elapsed time.

Final report: `<campaign-output-root>/FULL_CAMPAIGN_REPORT.md`

Overall analysis gate: [ ] PASS  [ ] FAIL

## Final authorization

All required sections PASS: [ ] YES  [ ] NO

Approved for publication analysis: [ ] YES  [ ] NO

Operator signature/date: _________________________________________________

Reviewer signature/date: _________________________________________________

Deviations and disposition:

___________________________________________________________________________

___________________________________________________________________________
