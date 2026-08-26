# Model comparison

- dataset: `evals/datasets/migration` (10 case(s))
- arm: `repair` — identical for every model
- generated: 2026-08-26T10:05:16+00:00
- wall clock: 1868s
- prices as of: 2026-06-24

Every model ran the same cases with the same prompts, tools and repair budget,
and every patch was graded by the same hidden contract tests. **Correct** is what
the hidden test found; **overclaim rate** is how often Rewire vouched for a patch
that test rejected — the number that says whether verification, rather than the
model, is the thing to fix.

| Model | Correct | 95% CI | Verified | Overclaimed | Overclaim rate | Tokens | Cost |
|---|---|---|---|---|---|---|---|
| `openai:gpt-4o` | **6/10** | 60% (31%-83%) | 6 | 1 | 17% | 118517 | $0.23 |
| `openai:gpt-4o-mini` | **5/10** | 50% (24%-76%) | 6 | 2 | 33% | 205129 | $0.02 |
| `openai:gpt-4.1` | **7/10** | 70% (40%-89%) | 7 | 1 | 14% | 124463 | $0.15 |
| `openai:gpt-4.1-mini` | **4/10** | 40% (17%-69%) | 5 | 2 | 40% | 160951 | $0.03 |

## Is the difference real?

Each pair is compared on the cases both ran, by an exact paired sign test over the
cases they disagreed on. Cases both models handled the same way carry no
information about which is better and are excluded. At this sample size most
differences are not separable from chance, and the test is here to say so rather
than to award a winner.

- 1-0 on 1 disagreement(s), p=1.00 - not distinguishable from chance at n=10
- 0-1 on 1 disagreement(s), p=1.00 - not distinguishable from chance at n=10
- 3-1 on 4 disagreement(s), p=0.62 - not distinguishable from chance at n=10
- 0-2 on 2 disagreement(s), p=0.50 - not distinguishable from chance at n=10
- 2-1 on 3 disagreement(s), p=1.00 - not distinguishable from chance at n=10
- 3-0 on 3 disagreement(s), p=0.25 - not distinguishable from chance at n=10

## What the models agree on

**3 case(s) no model solved:** `04-response-field-renamed`, `05-enum-value-removed`, `07-required-field-added`. These are Rewire's ceiling rather than the model's — a stronger model did not move them, so the improvement to make is in the harness.

**3 case(s) every model solved:** `01-request-field-renamed`, `06-raw-http-client`, `09-unrelated-change`. They contribute nothing to a comparison between models.

## Case by case

| Case | `openai:gpt-4o` | `openai:gpt-4o-mini` | `openai:gpt-4.1` | `openai:gpt-4.1-mini` |
|---|---|---|---|---|
| `01-request-field-renamed` | ok | ok | ok | ok |
| `02-rename-across-modules` | ok | miss | ok | miss |
| `03-request-field-removed` | ok | ok | ok | miss |
| `04-response-field-renamed` | miss | **overclaim** | **overclaim** | **overclaim** |
| `05-enum-value-removed` | **overclaim** | **overclaim** | miss | **overclaim** |
| `06-raw-http-client` | ok | ok | ok | ok |
| `07-required-field-added` | miss | miss | miss | miss |
| `08-wrapper-and-tests` | miss | miss | ok | ok |
| `09-unrelated-change` | ok | ok | ok | ok |
| `10-partially-migrated` | ok | ok | ok | miss |
