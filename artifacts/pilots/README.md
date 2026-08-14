# Corrected pilot evidence retained in Git

This directory contains only compact evidence from the 2026-08-14 pilot run on
the corrected strict-bound, message-only, non-destructive architecture.
Pre-correction pilot summaries were deleted and must not be restored or pooled
with this evidence.

- `corrected_rate_summary.csv`: rate-screen aggregates for Eager and Count B4.
  Rates 0.6 and 1.2 include two independent confirmation traces in addition to
  the three screening traces.
- `corrected_policy_summary.csv`: three-load policy-screen aggregates for
  Eager, Count B2/B4/B8, and Bounded B4 with W=2/5/10/20.
- `corrected_pilot_selection.json`: machine-readable selected loads, policies,
  trial staging, and RP trace count.
- `corrected_zero_diagnostic_summary.csv`: ACBBA at the selected high load
  under zero-time execution; all 24 jobs completed.

Every CSV row is an unweighted aggregate across algorithms and paired traces.
Mission-time and completion-latency means use successful missions only;
completion counts remain explicit, and allocator-work/call/message means retain
all technically valid trials, including algorithmically incomplete outcomes.

Regenerate these files after rerunning the three corrected pilot campaigns:

```text
python scripts/analyze_corrected_pilot.py
```

Selected values:

- loads: 0.075, 0.30, 0.60 tasks per mission-second;
- policies: Eager, Count B2/B4/B8, Bounded B4/W10;
- AGX: 25 traces in Round 1 plus 25 in Round 2 for every condition;
- RP2040: four traces for each selected hardware condition; and
- Round 2: never restricted by the informational verification checkpoint.

Raw `study/output` directories are machine-local. Abandoned pre-fix diagnostic
outputs are not evidence and should be removed during the AGX clean-start
procedure.
