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

Do not call `start_background.sh` again while the AGX v7 unit is active. For
four prepared boards, launch only the hardware script on cores 0-3:

```bash
taskset -c 0-3 bash AGX_CORRECTED_EXPERIMENT_HANDOFF/scripts/run_hardware_subset.sh
```

The existing simulation supervisor runs the engineering-only 8-job AGX smoke, Round 1
causal, Round 1 zero-time, checkpoint validation, Round 2 causal, then Round 2
zero-time. The hardware v10 runner starts the sealed 70-mission continuation;
the 26 successful v9 missions are excluded as complete paired blocks. The checkpoint reports failures and
invariant violations but does not create an allowlist or stop the complete
Round 2 matrix. Technical corruption must be repaired/resumed; algorithmic
noncompletion remains data.

Monitor `study/output/corrected_experiment_supervisor_v7/` for AGX and
`study/output/corrected_hardware_core_96_v10_continuation/LIVE_TRACKER.md` for hardware. The
v7 supervisor has its own locks, logs, PID
files, and completion marker, so it cannot inherit stale supervisor state from
the aborted v1/v2/v3/v4/v5/v6 executions. A lost terminal or disconnected AI
session must not stop the background process. Use the PID files to prove
whether a supervisor is alive before starting another one.
