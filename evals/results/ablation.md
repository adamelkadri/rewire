# Agent ablations

- dataset: `evals/datasets/migration` (10 case(s))
- model: `openai` / `gpt-4o` — identical for every arm
- repair budget: 3 attempts — identical for every arm
- generated: 2026-08-24T10:54:39+00:00
- wall clock: 1913s

Every arm ran the same cases against the same model with the same repair budget,
and every patch was graded by the same hidden contract tests. The only thing that
differs between arms is what the agent was given.

| Arm | What it lost |
|---|---|
| `full` | the shipped configuration: ranked locations and every tool |
| `no-impact-locations` | told which fields changed, not where they are used; every tool kept |
| `no-impact` | impact analysis withheld entirely, including its power to say 'nothing here' |
| `no-search` | given the ranked locations, denied the tools to look beyond them |

| Arm | Correct | 95% CI | Verified | Overclaimed | Overclaim rate | Tokens | Cost |
|---|---|---|---|---|---|---|---|
| `full` | **6/10** | 60% (31%-83%) | 7 | 2 | 29% | 142910 | $0.27 |
| `no-impact-locations` | **7/10** | 70% (40%-89%) | 7 | 1 | 14% | 97568 | $0.19 |
| `no-impact` | **7/10** | 70% (40%-89%) | 8 | 1 | 12% | 100794 | $0.19 |
| `no-search` | **5/10** | 50% (24%-76%) | 6 | 2 | 33% | 154515 | $0.29 |

## Is the difference real?

Each pair is compared on the cases both ran, by an exact paired sign test over the
cases they disagreed on. Cases both arms handled the same way carry no
information about which is better and are excluded. At this sample size most
differences are not separable from chance, and the test is here to say so rather
than to award a winner.

- 0-1 on 1 disagreement(s), p=1.00 - not distinguishable from chance at n=10
- 1-2 on 3 disagreement(s), p=1.00 - not distinguishable from chance at n=10
- 1-0 on 1 disagreement(s), p=1.00 - not distinguishable from chance at n=10
- 1-1 on 2 disagreement(s), p=1.00 - not distinguishable from chance at n=10
- 2-0 on 2 disagreement(s), p=0.50 - not distinguishable from chance at n=10
- 3-1 on 4 disagreement(s), p=0.62 - not distinguishable from chance at n=10

## What the arms agree on

**2 case(s) no arm solved:** `05-enum-value-removed`, `07-required-field-added`. No configuration of the harness reached them, so they are not a question of what the agent was given.

**4 case(s) every arm solved:** `01-request-field-renamed`, `03-request-field-removed`, `06-raw-http-client`, `10-partially-migrated`. They contribute nothing to a comparison between arms.

## Case by case

| Case | `full` | `no-impact-locations` | `no-impact` | `no-search` |
|---|---|---|---|---|
| `01-request-field-renamed` | ok | ok | ok | ok |
| `02-rename-across-modules` | ok | ok | ok | miss |
| `03-request-field-removed` | ok | ok | ok | ok |
| `04-response-field-renamed` | miss | ok | ok | miss |
| `05-enum-value-removed` | **overclaim** | miss | **overclaim** | **overclaim** |
| `06-raw-http-client` | ok | ok | ok | ok |
| `07-required-field-added` | miss | miss | miss | miss |
| `08-wrapper-and-tests` | **overclaim** | **overclaim** | ok | **overclaim** |
| `09-unrelated-change` | ok | ok | **spurious** | ok |
| `10-partially-migrated` | ok | ok | ok | ok |
