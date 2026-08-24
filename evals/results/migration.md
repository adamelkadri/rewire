# Migration benchmark

- dataset: `evals/datasets/migration` (10 case(s))
- model: `openai` / `gpt-4o`
- generated: 2026-08-24T12:22:21+00:00
- wall clock: 1084s

Every patch is graded by a contract test injected after the patch is applied
and never present in the repository the agent could read. **Verified** is what
Rewire claimed; **correct** is what the hidden test found. The gap between them
is the rate at which Rewire's own verification was fooled.

| Arm | Attempts | Correct | Verified | Overclaimed | Underclaimed | Repaired | Tokens | Cost |
|---|---|---|---|---|---|---|---|---|
| no-repair | 1 | **5/10** (50%) | 4 | 0 | 0 | 0 | 69178 | $0.14 |
| repair | 3 | **7/10** (70%) | 7 | 1 | 0 | 5 | 155859 | $0.30 |

Repair moved the proven success rate from **50%** (5/10) to **70%** (7/10).

## no-repair

_one attempt, no feedback from the sandbox_

| Tag | Correct |
|---|---|
| change:enum-removed | 0/1 |
| change:field-removed | 1/1 |
| change:field-renamed | 3/5 |
| change:required-added | 0/1 |
| change:response-renamed | 0/1 |
| change:unrelated | 1/1 |
| difficulty:direction | 0/1 |
| difficulty:name-collision | 0/1 |
| difficulty:partial | 0/1 |
| difficulty:spread | 1/1 |
| limitation:nothing-to-match | 0/1 |
| shape:multi-module | 1/2 |
| shape:negative | 1/1 |
| shape:raw-http | 1/1 |
| shape:single-module | 2/5 |
| shape:wrapper | 0/1 |

| Case | Expect | Status | Verified | Correct | Attempts | Tokens | Note |
|---|---|---|---|---|---|---|---|
| `01-request-field-renamed` | migrate | verified | yes | yes | 1 | 12707 | the hidden contract test passed |
| `02-rename-across-modules` | migrate | verified | yes | yes | 1 | 11663 | the hidden contract test passed |
| `03-request-field-removed` | migrate | verified | yes | yes | 1 | 10223 | the hidden contract test passed |
| `04-response-field-renamed` | migrate | no_patch | no | - | 1 | 4531 | no patch was produced, so there was nothing to grade |
| `05-enum-value-removed` | migrate | unverified | no | **no** | 1 | 7713 | the hidden contract test did not pass: the patch broke checks that passed before it: tests |
| `06-raw-http-client` | migrate | verified | yes | yes | 1 | 8552 | the hidden contract test passed |
| `07-required-field-added` | migrate | no_affected_code | no | - | 0 | 0 | no patch was produced, so there was nothing to grade |
| `08-wrapper-and-tests` | migrate | unverified | no | **no** | 1 | 7634 | the hidden contract test did not pass: the patch broke checks that passed before it: tests |
| `09-unrelated-change` | no_op | no_affected_code | no | - | 0 | 0 | no patch was produced, so there was nothing to grade |
| `10-partially-migrated` | migrate | unverified | no | **no** | 1 | 6155 | the hidden contract test did not pass: the patch broke checks that passed before it: tests |

## repair

_up to three attempts, each told why the last one failed_

| Tag | Correct |
|---|---|
| change:enum-removed | 0/1 |
| change:field-removed | 1/1 |
| change:field-renamed | 4/5 |
| change:required-added | 0/1 |
| change:response-renamed | 1/1 |
| change:unrelated | 1/1 |
| difficulty:direction | 1/1 |
| difficulty:name-collision | 0/1 |
| difficulty:partial | 1/1 |
| difficulty:spread | 1/1 |
| limitation:nothing-to-match | 0/1 |
| shape:multi-module | 2/2 |
| shape:negative | 1/1 |
| shape:raw-http | 1/1 |
| shape:single-module | 3/5 |
| shape:wrapper | 0/1 |

| Case | Expect | Status | Verified | Correct | Attempts | Tokens | Note |
|---|---|---|---|---|---|---|---|
| `01-request-field-renamed` | migrate | verified | yes | yes | 2 | 19082 | the hidden contract test passed |
| `02-rename-across-modules` | migrate | verified | yes | yes | 2 | 23148 | the hidden contract test passed |
| `03-request-field-removed` | migrate | verified | yes | yes | 1 | 10012 | the hidden contract test passed |
| `04-response-field-renamed` | migrate | verified | yes | yes | 2 | 18203 | the hidden contract test passed |
| `05-enum-value-removed` | migrate | verified | yes | **no** | 2 | 18331 | the hidden contract test did not pass: the patch broke checks that passed before it: tests |
| `06-raw-http-client` | migrate | verified | yes | yes | 1 | 15647 | the hidden contract test passed |
| `07-required-field-added` | migrate | no_affected_code | no | - | 0 | 0 | no patch was produced, so there was nothing to grade |
| `08-wrapper-and-tests` | migrate | unverified | no | **no** | 3 | 33075 | the hidden contract test did not pass: the patch broke checks that passed before it: tests |
| `09-unrelated-change` | no_op | no_affected_code | no | - | 0 | 0 | no patch was produced, so there was nothing to grade |
| `10-partially-migrated` | migrate | verified | yes | yes | 2 | 18361 | the hidden contract test passed |
