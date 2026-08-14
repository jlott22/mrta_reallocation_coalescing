# Three-board AGX test report

> **Archived pre-correction hardware evidence.** This establishes historical
> board/transport feasibility only. It does not validate the corrected
> allocator transaction boundary, dynamic admitted-only registry, or current
> allocator state semantics; fresh physical parity/timing is required when the
> boards are available.

Date: 2026-08-10 (America/Los_Angeles)

## Outcome

Three connected Pololu 3pi+ 2040 boards are usable as the publication worker
cohort, with three physical workers and four persistent logical robot contexts
per mission. The software and physical failure-point checks pass. The canonical
paper campaign must not start yet because the controlled AGX environment gate,
clean committed source, smoke, calibration, review, and design freeze remain
required.

No motor or sensor path was initialized. The deployment report records
`main_py_changed=false` and `motors_or_sensors_initialized=false`.

## Bound hardware

| Worker | Stable device UID | Current endpoint |
|---:|---|---|
| 0 | `e4621cb30b16392f` | `/dev/ttyACM0` |
| 1 | `e4621cb30b43372f` | `/dev/ttyACM1` |
| 2 | `e4621cb30b4b372f` | `/dev/ttyACM2` |

All three boards report MicroPython 1.24.0 at 125 MHz. The sealed replay build
is `micropython_1_24_o0_coalescing_collaborative_178fdb38cac5`; its deployed
module-set SHA-256 is
`fbae8488d8639427dd8d4efecf73dbc4d3adffc37a03de19224fc49cecbee400`.

## Verification completed

- Native preflight: 20/20 PASS, 72 physical parity-valid calls,
  `hardware_valid=true`, `hardware_validated=true`, and timer resolution
  51--54 microseconds.
- Focused PI hardware trajectory: parity valid through call 115, 115 calls in
  657.0 seconds.
- Focused ACBBA hardware trajectory: parity valid through call 115, 115 calls
  in 727.3 seconds.
- Automated suites: 185/185 PASS (57 simulator/core, 48 study/campaign, 80
  HIL/device).
- Exact desktop PI trajectory: completed with parity after 409 calls.
- Representative 257 ms ACBBA duration simulation: completed all 50 tasks in
  350 calls.
- Zero-duration trace timing: CBAA 10.6 s/426 calls, PI 12.0 s/424 calls,
  HIPC 13.1 s/422 calls. ACBBA retained a technically valid
  `stagnation_horizon` outcome after 5,563 calls in 114.6 seconds.

Machine-local evidence:

- `study/native_gates/preflight_three_board_pilot_v6/preflight.json`
- `study/native_gates/device_build/device_build_deployment.json`
- `configs/local/agx_board_bindings.json`
- `study/output/agx_three_board_failure_point_probe_v1/LIVE_TRACKER.md`
- `study/output/agx_three_board_failure_point_probe_v1/checkpoint_results.json`

## Problems found and fixed

1. Large causal setup bursts exhausted RP2040 heap. The transport now compacts
   ordered message/event streams, sends bounded 384-byte parts, and avoids
   redundant whole-buffer copies.
2. Native PI/HIPC/CBAA/ACBBA state parity exposed unobserved-peer handling,
   desktop `(x,y)` tie-breaking, stale PI claims, and event/checkpoint ordering
   differences. These now have fail-closed regressions.
3. RP2040 single-precision rounding changed the ACBBA no-time sentinel. Protocol
   timestamps now normalize the sentinel to its exact canonical integer.
4. PI reported an allocation-epoch mechanism label when the authoritative
   state reported collision repair. Collision mechanism evidence now survives
   an epoch snapshot.
5. ACBBA rebroadcast an unchanged third bundle entry. Native communication now
   applies the desktop owner/bid/timestamp last-sent suppression rule.
6. An algorithmic-incomplete task row with `release_time_s=None` crashed causal
   reporting. Unreleased rows are now retained and excluded from positive
   release-time overlap metrics, so horizon outcomes remain scientific rather
   than becoming technical failures.
7. Campaign launchers now start and finalize an atomic `LIVE_TRACKER.md` with
   progress, per-worker status, measured throughput, and provisional ETA.

## Measured wall-time forecast

The failure-point probes measured approximately 5.7 seconds per PI call and
6.3 seconds per ACBBA call, including setup and result transport. Combined
with the observed 350--426 calls for representative completed missions and the
46-minute clean partial CBAA run, a normal native mission is provisionally
35--50 minutes. The central planning value is 40 minutes, or about 4.5 missions
per hour across three boards.

| Stage | Native missions | Central three-board wall time |
|---|---:|---:|
| Causal smoke | 32 | 7.1 h |
| Rate calibration | 280 | 2.6 d |
| Timeout calibration | 300 | 2.8 d |
| Variance/n pilot | 240 | 2.2 d |
| Upstream native total | 852 | 7.9 d |
| Final native, n=25 | 1,500 | 13.9 d |
| Final native, n=50 | 3,000 | 27.8 d |

The matching three-worker zero-compute arm is estimated from the measured
algorithm mix at about 5.2 hours for 1,500 conditions or 10.4 hours for 3,000.
The ACBBA zero-duration horizon is included in that mix.

Therefore the provisional continuous, unattended totals are:

- n=25: about 22 days central; roughly 19--27 days for a 35--50 minute normal
  mission range;
- n=50: about 36 days central; roughly 32--45 days for the same range.

A practical 20% allowance for retries, thermal stabilization, review stops,
and operator interruptions gives planning envelopes of approximately 26 days
for n=25 and 43 days for n=50. Calibration must run before the final n choice;
the declared variance rule still determines whether n=25 is scientifically
supported or n=50 is required.

## Required next gate

Do not start the 32-mission canonical smoke from the current dirty tree. First:

1. review and commit the tracked implementation;
2. make `jetson_clocks --show` available and record/accept the controlled AGX
   power/clock/thermal state;
3. rerun the source-bound deployment/binding and canonical preflight from that
   clean commit; and
4. run smoke with its automatically refreshed `LIVE_TRACKER.md`.

