# Model comparison — 2026-11-04 18:15

Suites: router, tools · runs per case: 3

| Model | Router | Tools | Errors | p50 | p95 | Avg cost / call | Run cost |
|---|---|---|---|---|---|---|---|
| openai / gpt-6-luna | 100% | 100% | 0% | 1.65 s | 2.53 s | $0.000053 | $0.0073 |

Router / Tools = share of cases fully correct. Errors = calls that failed or returned no usable decision. Costs use list prices in `erranderpy/core/llm/pricing.py` (DeepSeek at peak price).

## Failed cases

### openai / gpt-6-luna

All cases passed.
