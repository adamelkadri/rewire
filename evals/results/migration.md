# Migration benchmark

- dataset: `evals/datasets/migration` (10 case(s))
- model: `openai` / `gpt-4o`
- generated: 2026-08-24T08:25:43+00:00
- wall clock: 778s

Every patch is graded by a contract test injected after the patch is applied
and never present in the repository the agent could read. **Verified** is what
Rewire claimed; **correct** is what the hidden test found. The gap between them
is the rate at which Rewire's own verification was fooled.

| Arm | Attempts | Correct | Verified | Overclaimed | Underclaimed | Repaired | Tokens | Cost |
|---|---|---|---|---|---|---|---|---|
| no-repair | 1 | **4/10** (40%) | 4 | 1 | 1 | 0 | 82409 | $0.16 |
| repair | 3 | **6/10** (60%) | 8 | 3 | 0 | 3 | 116621 | $0.22 |

Repair moved the proven success rate from **40%** (4/10) to **60%** (6/10).

## no-repair

_one attempt, no feedback from the sandbox_

| Tag | Correct |
|---|---|
| change:enum-removed | 0/1 |
| change:field-removed | 1/1 |
| change:field-renamed | 2/5 |
| change:required-added | 0/1 |
| change:response-renamed | 0/1 |
| change:unrelated | 1/1 |
| difficulty:direction | 0/1 |
| difficulty:name-collision | 0/1 |
| difficulty:partial | 0/1 |
| difficulty:spread | 0/1 |
| limitation:nothing-to-match | 0/1 |
| shape:multi-module | 0/2 |
| shape:negative | 1/1 |
| shape:raw-http | 1/1 |
| shape:single-module | 2/5 |
| shape:wrapper | 0/1 |

| Case | Expect | Status | Verified | Correct | Attempts | Tokens | Note |
|---|---|---|---|---|---|---|---|
| `01-request-field-renamed` | migrate | verified | yes | yes | 1 | 7902 | the hidden contract test passed |
| `02-rename-across-modules` | migrate | unverified | no | yes | 1 | 23821 | the hidden contract test passed |
| `03-request-field-removed` | migrate | verified | yes | yes | 1 | 10308 | the hidden contract test passed |
| `04-response-field-renamed` | migrate | unverified | no | **no** | 1 | 8120 | the hidden contract test did not pass: the patch broke checks that passed before it: tests |
| `05-enum-value-removed` | migrate | unverified | no | **no** | 1 | 8108 | the hidden contract test did not pass: the patch broke checks that passed before it: tests |
| `06-raw-http-client` | migrate | verified | yes | yes | 1 | 8601 | the hidden contract test passed |
| `07-required-field-added` | migrate | no_affected_code | no | - | 0 | 0 | no patch was produced, so there was nothing to grade |
| `08-wrapper-and-tests` | migrate | verified | yes | **no** | 1 | 7693 | the hidden contract test did not pass: the patch broke checks that passed before it: tests |
| `09-unrelated-change` | no_op | no_affected_code | no | - | 0 | 0 | no patch was produced, so there was nothing to grade |
| `10-partially-migrated` | migrate | unverified | no | **no** | 1 | 7856 | the hidden contract test did not pass: the patch broke checks that passed before it: tests |

## repair

_up to three attempts, each told why the last one failed_

| Tag | Correct |
|---|---|
| change:enum-removed | 0/1 |
| change:field-removed | 1/1 |
| change:field-renamed | 4/5 |
| change:required-added | 0/1 |
| change:response-renamed | 0/1 |
| change:unrelated | 1/1 |
| difficulty:direction | 0/1 |
| difficulty:name-collision | 0/1 |
| difficulty:partial | 1/1 |
| difficulty:spread | 1/1 |
| limitation:nothing-to-match | 0/1 |
| shape:multi-module | 2/2 |
| shape:negative | 1/1 |
| shape:raw-http | 1/1 |
| shape:single-module | 2/5 |
| shape:wrapper | 0/1 |

| Case | Expect | Status | Verified | Correct | Attempts | Tokens | Note |
|---|---|---|---|---|---|---|---|
| `01-request-field-renamed` | migrate | verified | yes | yes | 1 | 10454 | the hidden contract test passed |
| `02-rename-across-modules` | migrate | verified | yes | yes | 2 | 28169 | the hidden contract test passed |
| `03-request-field-removed` | migrate | verified | yes | yes | 1 | 11897 | the hidden contract test passed |
| `04-response-field-renamed` | migrate | verified | yes | **no** | 2 | 22702 | the hidden contract test did not pass: the patch broke checks that passed before it: tests |
| `05-enum-value-removed` | migrate | verified | yes | **no** | 1 | 7721 | the hidden contract test did not pass: the patch broke checks that passed before it: tests |
| `06-raw-http-client` | migrate | verified | yes | yes | 1 | 5769 | the hidden contract test passed |
| `07-required-field-added` | migrate | no_affected_code | no | - | 0 | 0 | no patch was produced, so there was nothing to grade |
| `08-wrapper-and-tests` | migrate | verified | yes | **no** | 1 | 10836 | the hidden contract test did not pass: the patch broke checks that passed before it: tests |
| `09-unrelated-change` | no_op | no_affected_code | no | - | 0 | 0 | no patch was produced, so there was nothing to grade |
| `10-partially-migrated` | migrate | verified | yes | yes | 2 | 19073 | the hidden contract test passed |
