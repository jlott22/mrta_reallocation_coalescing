# Pilot and design freeze

The corrected pilot screened seven rates, eight policies, and a focused
zero-time diagnostic. It also exposed and fixed a CBAA movement-rebid loop.

Selections:

- low 0.075, medium 0.30, high 0.60 tasks/s;
- Eager, Count B2/B4/B8, and Bounded B4/W10;
- eight initial tasks; and
- 25+25 paired AGX traces.

Rates 1.2 and 2.4 repeatedly produced ACBBA stagnation and compressed the
online phase. W10 was the best common bounded point: its timeout share changed
from 90.9% at low load to 51.1% at medium and 18.9% at high. In the pilot W10
reduced allocator work 1.7-4.9% relative to Eager while increasing mean
release-to-completion latency 5.1-33.5%. These are descriptive pilot values,
not final estimates.

The final analysis tests the work-versus-latency Pareto tradeoff subject to
mission completion. Do not tune a factor after Round 1 and do not drop an
allocator/policy because of an unfavorable or incomplete algorithmic outcome.
