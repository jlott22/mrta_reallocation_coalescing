# Paper-V2 bundle build report

- Git commit: `bb751db5062a41abee4f2e1e87512fe6520554d2`
- Exporter command: `/usr/bin/python /home/agxorin/mrta_reallocation_coalescing/build_paper_v2_data_bundle.py --repository-root /home/agxorin/mrta_reallocation_coalescing --raw-campaign-root /home/agxorin/mrta_reallocation_coalescing --publication-bundle /home/agxorin/mrta_reallocation_coalescing/publication/aug14_final_v1 --output-dir /home/agxorin/mrta_reallocation_coalescing/paper_v2_data_bundle`
- Optional full-call export: `not created`
- Validation checks: 35 passed, 0 failed
- Reconciliation mismatches: 0
- Current unpacked bundle size before final manifest: 355,189,840 bytes

## Raw roots and campaigns

- `agx_causal_traces_25_49`: `/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_aug14_v1/fix_worktree/study/output/agx_n50_extension_causal_1500_v1`
- `agx_primary_traces_0_24`: `/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_primary_1396_v1`
- `arrival_trace_0`: `/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_arrival_verify_24_v1`
- `arrival_traces_1_2`: `/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_aug14_v1/fix_worktree/study/output/agx_deadline_arrival_verify_traces_1_2_v2`
- `hardware_bounded`: `/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_hardware_bounded_8_v1`
- `hardware_core`: `/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_hardware_core_96_v1`
- `timeout_trace_0`: `/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_timeout_verify_24_v1`
- `timeout_traces_1_2`: `/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_aug14_v1/fix_worktree/study/output/agx_deadline_timeout_verify_traces_1_2_v2`
- `zero_compute_traces_0_7`: `/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_aug14_v1/fix_worktree/study/output/agx_deadline_zero_compute_480_scheduler_v2`
- `zero_compute_traces_25_49`: `/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_aug14_v1/fix_worktree/study/output/agx_n50_extension_zero_compute_1500_v1`
- `zero_compute_traces_8_24`: `/home/agxorin/mrta_reallocation_coalescing/study/output/agx_deadline_aug14_v1/fix_worktree/study/output/agx_deadline_zero_compute_traces_8_24_v2`

## Output counts

- `core/call_epoch_trial_summary.csv`: 240,816 rows, 43 columns
- `core/causal_zero_pairs_extended.csv`: 3,000 rows, 90 columns
- `core/cbaa_divergence_summary.csv`: 56 rows, 24 columns
- `core/cbaa_liveness_summary.csv`: 1,500 rows, 43 columns
- `core/hardware_call_complexity_summary.csv`: 1,494 rows, 28 columns
- `core/hardware_environment.csv`: 4 rows, 25 columns
- `core/matrix_coverage.csv`: 200 rows, 20 columns
- `core/policy_pairwise_same_provider_extended.csv`: 11,712 rows, 150 columns
- `core/policy_vs_eager_all_pairs_audit.csv`: 4,800 rows, 175 columns
- `core/policy_vs_eager_same_provider_extended.csv`: 4,664 rows, 175 columns
- `core/raw_artifact_index.csv`: 6,144 rows, 47 columns
- `core/task_admission_reason_summary.csv`: 23,860 rows, 52 columns
- `core/trial_mechanism_summary.csv`: 6,000 rows, 471 columns
- `core/verification_trial_mechanism_summary.csv`: 144 rows, 478 columns
- `detail/cbaa_divergence_events.csv.gz`: 5,808 rows, 20 columns
- `detail/epoch_level_causal_part01.csv.gz`: 667,500 rows, 65 columns
- `detail/epoch_level_verification.csv.gz`: 38,374 rows, 65 columns
- `detail/epoch_level_zero_part01.csv.gz`: 229,707 rows, 65 columns
- `detail/hardware_call_level.csv.gz`: 97,814 rows, 60 columns
- `detail/representative_case_events.csv.gz`: 47,311 rows, 44 columns
- `detail/task_timing_decomposition_causal.csv.gz`: 150,000 rows, 67 columns
- `detail/task_timing_decomposition_zero.csv.gz`: 150,000 rows, 67 columns
- `detail/task_vs_eager_same_provider.csv.gz`: 233,200 rows, 43 columns
- `validation/missing_data_audit.csv`: 12 rows, 8 columns
- `validation/publication_reconciliation.csv`: 27 rows, 8 columns
- `validation/reconciliation_mismatches.csv`: 0 rows, 12 columns
- `validation/validation_checks.csv`: 35 rows, 6 columns

## Missing/non-derivable evidence

- task and trial assignment diagnostics: repeated_same_owner_assignment_count; distinct_owner_change_count; unique_owner_count; final_assigned_owner — task_events retains first owner plus aggregate assignment/reassignment counts, not the owner history
- trajectory counters: turn_count; replan_count; blocked_intent_collision_count; quarantine_count — robot counter internals were not included in promoted CSV/JSON artifacts
- allocator internals: bundle_reset_count; task_goal_change_count — no explicit algorithm-neutral event was retained; values are not inferred from algorithm names
- message mechanism: inbound_message_count; outbound_message_count; buffered_message_count — message payload hashes are retained per call but message event counts/timestamps are not
- compute buffering: maximum_compute_buffering_delay_s — release/admission overlap with virtual compute is derivable, but a causal delay attribution is not recorded
- task release geometry: minimum_robot_distance_at_release — robots may be in transit at release and continuous within-edge positions were not retained
- hardware physical concurrency: physical host wall start/end; cross-worker overlap counts — physical measurement order is retained per board, but host-wall timestamps are not
- hardware protocol diagnostics: protocol_retry_count; device_timeout; result_serialization_duration_s — these subfields were not retained separately in promoted call records
- hardware environment: baud_rate; AGX power mode; jetson_clocks; thermals — no retained evidence artifact records these fields
- noncompletion event loop: total_processed_event_count; stagnation_event_count — the summary retains configured horizons and final semantic streams, not the scheduler's processed-event counter
- CBAA repeated signatures: repeated_goal_robot_call_signature_frequency — goal and state hashes are retained per call, but the full progress signature used internally is not
- architecture filename: ARCHITECTURE_CLARIFICATION.md — the named file is absent; retained docs/SIMULATION_ARCHITECTURE.md is used and the discrepancy is documented

## Scientific limitations

The raw traces do not retain a unified scheduler/message event log, complete
assignment-owner histories, cross-worker host-wall measurement timestamps, or
all robot diagnostic counters.  The exporter therefore labels reconstructed
semantic comparisons and virtual interval unions as diagnostics rather than a
formal critical-path proof.  Algorithmic noncompletions remain in denominators
and never receive an imputed mission elapsed time.

See `validation_checks.csv`, `publication_reconciliation.csv`,
`reconciliation_mismatches.csv`, and `missing_data_audit.csv` for machine-readable details.
