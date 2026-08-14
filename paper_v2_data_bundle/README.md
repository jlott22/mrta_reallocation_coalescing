# Paper-V2 latency and mechanism data bundle

This is a read-only descriptive export from the immutable August 2026 MRTA
campaigns.  No simulation or hardware experiment was rerun.  The primary
mission-performance outcome is online-task release-to-completion latency: time
from an online task becoming available until it is physically completed or
searched.  Mission elapsed time is secondary.

The primary computation outcomes are provider-specific allocator processor
work and allocator-call count.  AGX and RP2040 work are never pooled inside a
primary policy-versus-Eager estimate.  Processor work is summed over calls and
logical robots and must not be interpreted as mission wall delay.

## Core semantics

- A logical allocation epoch is a scheduler admission/mandatory trigger.
- A call is one logical robot's `choose_goal` invocation.  Calls can continue
  after the epoch's expected first calls close it.
- `first_round=true` is the first valid call by each raw expected robot.  The
  hardware epoch format omits the expected set; initial/admission epochs infer
  four robots, while local mandatory epochs infer the first associated robot.
- `post_closure=true` means call completion is more than 1e-09 s after
  the first-expected-call epoch close; no such call is discarded.
- Any-source assignment includes a physical-service fallback when a robot
  reaches an admitted task before an allocator owns it.  That source is
  identified when first assignment and completion have the same timestamp;
  allocator-only timestamps remain empty in those cases.
- Queue event statistics (one observation per admission epoch) remain separate
  from task-weighted release-to-admission waiting.
- Virtual compute-union fields are interval diagnostics, not a formal critical
  path or a direct attribution of mission delay.
- Raw movement schedules can retain in-flight edges completing after mission
  termination.  Step, trajectory, final-position, and semantic-event fields
  include only edges completed by the mission/horizon timestamp.
- Quantiles use Type-7 linear interpolation.  Seconds retain Python float
  round-trip precision.  Missing numerics are empty, never silently zero.
- Trace ID—not task, call, or epoch—is the environmental replication unit.  No
  task/call/epoch p-values or confidence intervals are included.

The bounded B=4/W=5 policy is checked against scheduler pending age at
admission.  Release-to-assignment or release-to-completion may exceed five
seconds without being a policy violation.

## Reproducibility

Exporter command:

```bash
/usr/bin/python /home/agxorin/mrta_reallocation_coalescing/build_paper_v2_data_bundle.py --repository-root /home/agxorin/mrta_reallocation_coalescing --raw-campaign-root /home/agxorin/mrta_reallocation_coalescing --publication-bundle /home/agxorin/mrta_reallocation_coalescing/publication/aug14_final_v1 --output-dir /home/agxorin/mrta_reallocation_coalescing/paper_v2_data_bundle
```

The script discovers campaign roots from the existing publication source
labels and supports both `allocation_epochs.csv` and hardware
`reallocation_events.csv`.  It preserves retained algorithmic noncompletions,
excludes recovered historical technical attempts from scientific trial counts,
hashes all selected raw evidence, and reconciles the new reconstruction with
`publication/aug14_final_v1` at a 1e-09 s numerical tolerance.

The prompt named `ARCHITECTURE_CLARIFICATION.md`; that filename is absent in
the repository.  The retained `docs/SIMULATION_ARCHITECTURE.md` in the frozen
worktree is the architecture semantic source and the discrepancy is reported
in the missing-data audit.

The optional full-call export is excluded by default.  `export_manifest.json`
contains sizes, row/column counts, and SHA-256 hashes for all bundle files
except the manifest's own mathematically self-referential hash.
