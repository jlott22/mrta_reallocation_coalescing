# Copied-source baseline

Before dynamic-arrival changes, the copied Collaborative Visit simulator passed
all 17 source unit tests on Python 3.13.14.

A deterministic ideal-communication smoke trial used scenario 0 from
`scenarios/known_visit_g19_t10_n500.csv`, seed 9137, and unrestricted
candidates. All six allocators completed all 10 tasks:

| Allocator | Team steps |
| --- | ---: |
| CBAA | 82 |
| ACBBA | 62 |
| PI | 68 |
| HIPC | 61 |
| DMCHBA | 92 |
| DGA | 80 |

The raw copied-source outputs are under
`artifacts/baseline_source_equivalence/`. Host allocator durations are retained
for instrumentation checks but are not deterministic equivalence fields.
