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

If three prepared boards are present:

```bash
START_HARDWARE=YES bash AGX_CORRECTED_EXPERIMENT_HANDOFF/scripts/start_background.sh
```

The simulation supervisor runs Round 1 causal, Round 1 zero-time, checkpoint
validation, Round 2 causal, then Round 2 zero-time. The checkpoint reports
failures and invariant violations but does not create an allowlist or stop the
complete Round 2 matrix. Technical corruption must be repaired/resumed;
algorithmic noncompletion remains data.

Monitor `study/output/corrected_experiment_supervisor/` and each campaign's
analysis directory. A lost terminal or disconnected AI session must not stop
the background process. Use the PID files to prove whether a supervisor is
alive before starting another one.
