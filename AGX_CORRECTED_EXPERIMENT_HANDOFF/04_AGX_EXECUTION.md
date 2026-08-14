# AGX execution

Use a fresh clone of `main`. Keep the checkout clean during campaigns and set
numerical-library thread counts to one. The campaign layer already provides
immutable manifests, content validation, resumable jobs, and non-overwriting
promotion.

Start work in the background:

```bash
cd mrta_reallocation_coalescing
bash AGX_CORRECTED_EXPERIMENT_HANDOFF/scripts/start_background.sh
```

If four prepared boards are present:

```bash
START_HARDWARE=YES bash AGX_CORRECTED_EXPERIMENT_HANDOFF/scripts/start_background.sh
```

The simulation supervisor runs the engineering-only 8-job AGX smoke, Round 1
causal, Round 1 zero-time, checkpoint validation, Round 2 causal, then Round 2
zero-time. The hardware supervisor runs the engineering-only 8-mission RP2040
smoke before the fixed 96-mission subset. The checkpoint reports failures and
invariant violations but does not create an allowlist or stop the complete
Round 2 matrix. Technical corruption must be repaired/resumed; algorithmic
noncompletion remains data.

Monitor `study/output/corrected_experiment_supervisor_v2/` and each v2
campaign's analysis directory. The v2 supervisor has its own locks, logs, PID
files, and completion marker, so it cannot inherit stale supervisor state from
the aborted v1 execution. A lost terminal or disconnected AI session must not
stop the background process. Use the PID files to prove whether a supervisor is
alive before starting another one.
