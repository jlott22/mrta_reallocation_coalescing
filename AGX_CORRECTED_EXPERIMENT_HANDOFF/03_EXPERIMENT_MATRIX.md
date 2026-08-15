# Experiment matrix and core ownership

## AGX statistical matrix

Each treatment has 60 conditions:

`4 algorithms x 3 loads x 5 policies`.

| Phase | Trace IDs | Jobs |
|---|---|---:|
| Round 1 host-causal | 0000-0024 | 1,500 |
| Round 1 zero-time | 0000-0024 | 1,500 |
| Round 2 host-causal | 0025-0049 | 1,500 |
| Round 2 zero-time | 0025-0049 | 1,500 |
| Total | 50 per condition/treatment | 6,000 |

Host-causal and zero-time use byte-identical scenario/release manifests and
paired seeds. Round 2 runs every condition. Its only exclusion is the already
completed first 25 trace IDs.

## RP2040 matrix

`4 algorithms x 3 loads x 2 policies x 4 traces = 96 missions`.

The hardware policies are Eager and Count B4. The RP subset remains four
traces; it is not expanded to 25 or 50. Brief preflight and targeted late-call
probes are engineering evidence, but there is no long standalone hardware
smoke. Trace 0000 is prioritized inside this 96-mission schedule, so every
successful long mission is retained as publication-core data.

Every hardware mission still models four logical robots. Their complete frozen
checkpoints are reconstructed one at a time through a single resident native
runtime per board call, outside the allocator timing boundary. This common
constraint applies to all four algorithms and does not cap the task pool or
path/bundle horizon.

## AGX cores

| Cores | Owner |
|---|---|
| 0-3 | Four RP2040 workers/boards, one worker per core |
| 4-8 | Five rolling simulation workers |
| 9 | Background supervisor/tracker |
| 10 | Checkpoint QA/analysis when needed |
| 11 | OS and operational reserve |

The nine compute cores are exactly 0-8: four RP2040 workers on 0-3 and five
AGX-only simulation workers on 4-8. Do not add simulation workers to cores
9-11. The simulation scheduler balances conditions across the five-worker pool;
cores are not permanently tied to algorithms.
