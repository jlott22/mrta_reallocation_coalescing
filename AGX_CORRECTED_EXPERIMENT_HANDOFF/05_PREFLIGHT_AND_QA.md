# Brief preflight and QA

Before launch:

1. Confirm a clean committed checkout and at least 12 logical cores.
2. Run the three software suites and Python compilation once.
3. Generate the fresh 50-trace manifest set with a campaign `--prepare-only`
   or dry run; confirm every config resolves the same manifest hashes.
4. Run one small AGX smoke spanning all four allocators and Eager/B4.
5. If boards are present, run only the existing environment check, native
   preflight, and a brief stress probe covering:
   - dynamic delayed admission with no future-task registry;
   - one 50-task/unbounded-path transaction;
   - local and peer completion repair;
   - retained CBAA bid across movement;
   - compact/chunked large messages;
   - timer-scope attestation and host/native parity.

Do not perform a large RP calibration. Physical boards validate semantics,
memory/transport viability, and timing scope before the fixed 96-mission subset.

For every promoted campaign require zero piggyback admissions, zero final
flushes, exact ordinary B-sized Count batches, at most one terminal residual,
message-only task knowledge, timing arithmetic consistency, and explicit
classification of incomplete missions. After Round 1, verify these properties
and then run the unrestricted Round 2 configs.
