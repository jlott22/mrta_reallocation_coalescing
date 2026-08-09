# Decisions to review before the paper campaign

Only choices that materially affect interpretation or deployment are listed
here. The implementation does not require redesign for any of them.

## 1. Mission elapsed time is a model-based parallel estimate

The copied simulator's event clock excluded allocator runtime. The canonical
`mission_elapsed_time_s` now adds a four-processor allocator critical-path
estimate: within each logical epoch/timestamp, repeated calls are summed per
robot and the maximum robot duration is added. Team-serial compute is preserved
separately. This is more deployment-relevant than adding all four robots'
compute, but it is not a measured physical wall clock and compute duration is
not fed back into robot/event scheduling. Consequently task latencies remain on
the simulator event-time axis and exclude allocator duration. This limitation
must be stated in the paper.

## 2. Releases use the absolute simulator execution-time axis

Every condition receives the same predetermined absolute release timestamps.
The axis covers movement, turns, waits, replans, and modeled communication, but
not measured allocator duration. This maximizes exogenous pairing and avoids an
algorithm's host timing changing its input trace. It also means a release is not
modeled as occurring partway through a long allocator call. This is negligible
for the primary four in most pilots but material for heavy DGA.

## 3. Initial visible count is 8, not 12

Twelve tasks gave more distinct eventual winning first assignees, but would
make 24% of the fixed mission static. Eight gives two initial tasks per robot,
calls all four robots at the initial epoch, and leaves 42 online arrivals. The
study does not guarantee that consensus leaves one distinct initial winner per
robot in every algorithm/trace.

## 4. Calibrated parameters are common, not algorithm-tuned

The chosen rates are 0.075/0.30/1.20 tasks per mission-second; policies are
Eager B=1, Count B=2/4/8, and bounded B=4/W=5 s. Low-load batching sometimes
increases allocator calls or cumulative compute. Those results are scientifically
valid and must not be dropped.

## 5. Twenty-five traces target the primary mechanism

The n=10 variance pilot supports 25 paired traces for epoch reduction and most
medium/high compute effects. It does not promise precise estimates for small
mission-time or high-load latency differences. Treat the paired trial—not its
50 tasks—as the replicate, use paired Friedman/Wilcoxon-Holm only after the full
run, and report uncertainty that spans zero without a significance claim.

## 6. AGX allocator timing is measured under controlled concurrency

The launcher caps workers at 75% of logical cores, forces numerical libraries
to one thread, and deterministically balances condition order. It does not pin
CPU affinity or lock AGX clocks/thermal mode. Record `nvpmodel`, clock, and
thermal conditions during the real run; a lower `--max-workers` timing
sensitivity run is advisable if allocator-time conclusions are central.

## 7. HIL validates embedded compute, not a moving mission

The RP2040 replay is deliberately motor-free. It replays arrival/admission
epochs into persistent four-robot contexts and measures device `choose_goal()`
time separately from serial/host overhead. It does not emulate motion,
completion, invalid-goal, or robot-idle epochs and therefore cannot provide HIL
mission latency or elapsed time. Simulation and HIL join by sealed manifest and
device-build identity.

## 8. Physical validation remains outstanding

No robot was connected. Loopback, protocol, build, preflight, resume, and report
tests passed, but the first real run must explicitly select the four intended
ports, deploy the generated bundle, pass the online-growth preflight for all six
allocators, and confirm `hardware_validated=true` before any HIL value is used in
the paper.

## USER REVIEW REQUIRED

- Accept the mission-time critical-path estimate and event-time release axis, or
  require a causal compute-delay simulator extension before the full campaign.
- Confirm that 8 initial tasks and 25 paired traces match the paper's intended
  scope despite the documented limitations.
- Choose and record the AGX power/clock/thermal mode; consider a lower-worker
  timing sensitivity run.
- Run and inspect the required physical RP2040 preflight before citing HIL data.
