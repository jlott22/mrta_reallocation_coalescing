# Final analysis plan

The replicate is the mission trace, not a task, call, message, or epoch. Use
paired trace-level contrasts against Eager within `(allocator, load, trace)`.

Analyze the tracked corrected-v7 AGX export. For hardware, retain the fixed
96-job denominator: 82 successful missions and 14 technical failures. Analyze
v9 and v10 successful timing rows with a required `runtime_lineage` field and
report the failures before any success-conditional timing summary. Failed
attempts are not timing results, but their jobs remain in completion/failure
denominators. Report a v9-versus-v10 sensitivity because the heap repair can
reduce incidental GC inside `W_alloc`, even though allocator inputs/outputs
remain unchanged. If the optional retry is run, keep v11 as a third lineage.

Primary hierarchy:

1. completion indicator and algorithmic failure type;
2. total allocator processor work per mission;
3. per-trial mean and p95 release-to-completion latency; and
4. mission elapsed time.

Key secondary/mechanism metrics are total/max robot steps, admission epochs,
threshold/timeout/terminal-residual mix, allocator calls, per-call duration,
logical allocator-message bytes, and release-to-first-current-goal latency.

Keep AGX, zero-time, and hardware-provider results separate. Hardware timing
requires attestation; proxy/zero fields are not RP2040 performance. Always
report failure rates before success-only means and retain denominators.

Do not compare raw first-plan incorporation, bundle length, or reassignment
churn across allocators. CBAA advertises one claim, bundle allocators advertise
paths, and ACBBA/HIPC suffix repair differs from PI itemwise repair. Analyze
allocator-by-policy-by-load interactions before any pooled summary. Present a
work-latency Pareto view rather than one composite score.
