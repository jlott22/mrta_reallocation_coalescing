# Pilot evidence retained in Git

The raw pilot journals and the full generated analysis tables are deliberately ignored because they are large and machine-timing-specific. These compact tables preserve the calibration evidence; every row is an unweighted mean over successful, semantically validated trials. No pilot trial in these tables failed or left a task incomplete.

## Selected operating points

- Initial tasks: **8**. Four was too fragile. Twelve increased diversity of eventual winning first assignees, but would make almost one quarter of the fixed 50-task mission static. Eight is twice the robot count, invokes all four robots in the initial epoch, and preserves 42 online arrivals. `mean_distinct_first_assignees` is only a proxy for the eventual consensus winners; it is not a claim that robots without a winning task did no initial allocator work.
- Arrival loads: **0.075, 0.30, and 1.20 tasks per mission-second**. They represent sparse/limited coalescing, useful overlap while robots generally keep up, and strong overlap/backlog respectively. The 2.40 candidate compressed nearly all online releases into the opening phase and was rejected as less representative of an ongoing online mission.
- Bounded policy: **B=4, W=5 mission-seconds**. W=5 caps sparse-load waiting much more tightly than W=10/20 while retaining a substantial epoch reduction at medium/high load. It does not guarantee compute savings at low load; that unfavorable result is retained.
- Final trial count: **25 paired traces**. A 10-trace variance pilot showed projected n=25 intervals are narrow for the epoch mechanism and most medium/high compute effects, but not for small mission-time or high-load latency differences. The paper must report those uncertain effects as such rather than claim significance.

Absolute allocator timings differ slightly between separately executed pilot campaigns because they are host wall-clock measurements under different concurrent process loads. Parameter selection uses within-campaign paired comparisons and mechanism/queue behavior, not cross-campaign absolute timing comparisons.
