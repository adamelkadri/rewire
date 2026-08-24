# Roadmap and current status

Rewire is built incrementally. A phase is done when it is implemented, tested,
documented and demonstrable by a command. This page is the honest record of
what exists.

Legend: **done** · *in progress* · planned

| Phase | Capability | Status |
|-------|-----------|--------|
| 0 | Project foundation: packaging, settings, logging, errors, preflight, CI | **done** |
| 1 | API change detection (OpenAPI diff, breaking-change classification) | **done** |
| 2 | Repository analysis (AST index, symbols, imports, usages) | **done** |
| 3 | Impact analysis (change × repo → ranked affected locations) | **done** |
| 4 | First coding agent (tool-restricted, produces candidate patches only) | **done** |
| 5 | Docker sandbox (isolated execution, resource limits, verification) | **done** |
| 6 | Agent repair loop (sandbox feedback → bounded retries) | **done** |
| 7 | End-to-end MVP (`rewire migrate`) | **done** |
| 8 | Evaluation framework (datasets, metrics, published results) | **done** |
| 9 | Model comparison across providers | **done** |
| 10 | Agent ablations (AST vs text, repair on/off, context strategies) | planned |
| 11 | GitHub integration (branch, PR, never auto-merge) | planned |
| 12 | Automatic change monitoring | planned |
| 13 | HTTP API and background jobs | planned |
| 14 | Observability | planned |
| 15 | Web dashboard | planned |
| 16 | Security hardening review | planned |
| 17 | Performance and cost optimisation | planned |
| 18 | Portfolio and research write-up | planned |

Milestones: **0–3** core intelligence · **4–7** working agent · **8–10** measured
agent · **11–18** production product.

## What Phase 9 delivers

- `rewire eval models --model openai:gpt-4o --model openai:gpt-4o-mini …` — the
  same ten cases, the same prompts, the same tools and the same repair budget for
  every model, graded by the same hidden contract tests. The only thing that
  differs between columns is the model.
- Provider selection decoupled from configuration: `build_provider_for` builds
  any supported provider/model pair from one settings object, so temperature,
  timeout and retry budget are held constant across the comparison by
  construction rather than by discipline.
- **A 95% Wilson confidence interval on every rate, and an exact paired sign test
  between every pair of models.** The report prints "not distinguishable from
  chance" instead of a ranking whenever the test says so (ADR-037).
- **Agreement structure, not just a ranking** (ADR-039): the cases *no* model
  solved, reported as Rewire's ceiling rather than the model's, and the cases
  *every* model solved, which separate nothing and are excluded from the paired
  test.
- Per-model overclaim rate, denominated in patches Rewire vouched for — the
  number that answers whether verification or the model is the thing to fix.
- A requested model with no API key is reported as skipped with the reason and
  the environment variable that would fix it, never dropped (ADR-038).
- A crashed model is recorded the same way and the models that already ran are
  kept, along with a partial results file written after each model.

**Measured, four models, ten cases each, one run per model:**

| Model | Correct | 95% CI | Vouched for | Overclaimed | Cost |
|-------|---------|--------|-------------|-------------|------|
| `gpt-4o` | **6/10** | 31–83% | 8 | 3 | $0.19 |
| `gpt-4o-mini` | **4/10** | 17–69% | 5 | 2 | $0.02 |
| `gpt-4.1` | **6/10** | 31–83% | 7 | 2 | $0.15 |
| `gpt-4.1-mini` | **5/10** | 24–76% | 5 | 1 | $0.04 |

All six pairwise comparisons are inconclusive; the largest split is 2–0 on two
disagreements (p = 0.50). Nothing here separates these models.

What the run does establish is the pooled overclaim rate: across all four models
Rewire vouched for 25 patches and 8 were wrong — **32% (17–52%)**, with every
individual model between 20% and 40%. A better model did not buy a more
trustworthy verdict, which points the next round of work at verification rather
than at model selection.

Three cases — `04-response-field-renamed`, `05-enum-value-removed`,
`07-required-field-added` — were solved by no model. Case 04 is the sharpest:
three of four models produced a patch Rewire vouched for and the hidden contract
test rejected. Wall clock 1725s, total spend $0.40.

## What Phase 9 explicitly does not deliver

- **Still ten cases, and now four columns.** More models do not make a small
  dataset larger. Every pairwise comparison here has single-digit disagreement
  counts, which is why they mostly come back inconclusive.
- **One run per model.** Phase 8 established that two runs of the same
  configuration mostly agree; this phase did not repeat that per model, so
  none of these numbers has a variance estimate.
- No Anthropic or OpenRouter results, because this project has no key for either.
  The machinery is provider-agnostic and the report names the gap; filling it is
  one environment variable and a rerun.
- No cost-per-success or latency-per-case optimisation. The cost column is
  reported, not acted on; Phase 17 is where that becomes work.
- No prompt or tool variation between models. Every model gets the prompt tuned
  against gpt-4o in Phase 6, which is a confound this phase does not remove.

## What Phase 8 delivers

- `rewire eval migrate` — the whole pipeline run over a labelled dataset, in two
  arms that differ only in repair budget, with results published to
  `evals/results/migration.{json,md}`.
- **Grading by tests the agent never sees.** Each case ships a `hidden/`
  contract test injected into the sandbox copy after the patch is applied. An
  agent handed the repository's own suite can always pass it by editing the
  assertion; it cannot edit a file that is not there.
- Three numbers per arm, never one: **correct** (the hidden test accepted the
  patch), **verified** (Rewire said so), and **overclaimed** (Rewire said so and
  was wrong). The gap between the first two is the rate at which Rewire's own
  verification was fooled — a number no self-graded benchmark can report.
- Ten cases across distinct change kinds and repository shapes: field renamed,
  field removed, response field renamed, enum value removed, required field
  added, raw HTTP with no SDK, a wrapper whose parameter shares the field's
  name, a partially-migrated repository, a rename spread across three modules,
  and a negative case where the correct action is to do nothing.
- A dataset that is itself tested: every case's visible tests must pass before
  migration and every case's hidden tests must **fail** before it. A hidden test
  that already passes grades nothing and would silently award a success to a
  patch that changed nothing.
- Per-tag breakdowns, per-case cost and token counts, and a partial results file
  written after every case so a killed run keeps what it paid for.

**Measured, two independent runs of all ten cases:**

| Arm | Correct | Verified | Overclaimed | Repaired |
|-----|---------|----------|-------------|----------|
| repair off (1 attempt) | **4/10 (40%)** | 4–5 | 1–2 | 0 |
| repair on (3 attempts) | **6/10 (60%)** | 8 | 3 | 3–4 |

Both runs produced the same headline rates, and nine of ten cases reached the
same outcome in both. The one that moved changed between two failure modes, not
into success.

The number worth reading is the third column. Rewire's sandbox vouched for eight
patches and only five were real migrations. The three that were not passed the
repository's visible tests by changing them: renaming the wrapper's *public
Python parameter* to match the wire field, deleting the logic that could not be
migrated and replacing the assertion with a comment, and dropping the field
entirely while changing the test to assert its absence. None is distinguishable
from a legitimate test update by inspecting the diff, which is the entire
argument for hidden tests.

## What Phase 8 explicitly does not deliver

- **Ten cases is not 120.** The dataset is small, hand-written by the same
  person who wrote the tool, and generated from templates. It measures whether
  the loop works on distinct shapes of problem; it does not estimate performance
  on real repositories.
- One model. Comparing providers is Phase 9.
- One dimension of ablation — repair on and off. Context strategies and
  AST-versus-text are Phase 10.
- No statistical treatment. Ten cases, two runs; the difference between arms is
  not tested for significance and should not be quoted as if it were.

## What Phase 7 delivers

- `rewire migrate ./repo --old old.yaml --new new.yaml` — the whole pipeline in
  one command: spec diff, index, impact, agent, sandbox, repair.
- `--apply`, the first thing in Rewire that writes to the user's repository, and
  the three refusals that govern it:
  - an **unverified** patch is never written, and there is no override flag;
  - nothing is written into a **dirty** Git working tree without `--allow-dirty`,
    and nothing at all outside a Git repository;
  - nothing is written if a file **changed** between verification and writing.
- The dirty-tree check runs *before* the model is called, so a refusal costs
  milliseconds rather than an agent run and two container runs.
- Seven terminal statuses, four of them successes. "The spec moved and nothing
  here uses it" exits zero, because that is what most runs will say once Phase 12
  watches specifications automatically.
- `migration.json` per run — status, attempts, verdicts, tokens, cost, files
  written — recorded for every run including the ones where nothing happened,
  because that is the dataset Phase 8 aggregates.
- Read-only Git inspection in `gitio/`, which Phase 11 extends to branches and
  pull requests.

**Demonstrated live, end to end:** a repository where the renamed field also
appears as a dict key in a file the impact analysis does not rank. The first
attempt missed it, the sandbox caught it, the second attempt fixed it, and the
verified patch was written to a clean checkout — three files, 21 524 tokens,
$0.044, 75.5s. The repository's tests pass afterwards when run independently,
and `git diff` shows exactly the three files and nothing else.

## What Phase 7 explicitly does not deliver

- **No branch, no commit, no pull request.** `--apply` writes into the working
  tree and stops. Phase 11 does the rest.
- No measured success rate. Still one hand-made case; Phase 8 is the benchmark.
- No concurrency, no resume: a killed run leaves its traces but cannot be
  continued.
- Nothing reads `migration.json` back yet.

## What Phase 6 delivers

- `rewire propose ./repo --old old.yaml --new new.yaml --repair` — propose,
  verify, feed the failure back, and try again, up to `--max-attempts`.
- A per-attempt table showing what each attempt changed, what the sandbox said,
  and what it cost, so "repair helped" is visible rather than asserted.
- Retries driven by evidence: only `REGRESSED` and `ERRORED` are repairable.
  `INCONCLUSIVE` stops immediately, because nothing measured the patch and
  rewriting it cannot change that.
- Early stops that are real outcomes, each reported: the agent proposed the same
  patch twice, proposed nothing, or the shared token budget ran out.
- Each attempt starts from the original files with a fresh patch builder, so a
  mistaken edit can be replaced rather than only added to.
- Failing check output and the previous diff reach the agent inside the same
  untrusted-data envelope as every tool result, and truncated.
- `verification.json` written beside each attempt's trace.
- `--max-attempts 1` is repair switched off, which is the comparison Phase 10
  needs.

**Measured on one case, three runs per arm:** with `--max-attempts 1` the
migration verified **0/3** times; with `--max-attempts 3` it verified **3/3**,
every one of them on the second attempt. The case is a rename where the old
field name also appears as a dict key in a third file that the impact analysis
does not rank highly; the first attempt consistently misses it and the test
suite consistently catches it.

Read that number carefully. It is one hand-made case, one model, three runs an
arm, and the repair prompt was reworded *after* watching this exact case fail.
It shows that feedback changes the outcome on a case built to need feedback. It
does not show a success rate, and no success rate should be quoted until
Phase 8 measures one across a dataset nobody tuned against.

## What Phase 6 explicitly does not deliver

- **No measured success rate.** The runs below are a handful of executions of
  one hand-made case against one model. That is a demonstration, not a
  benchmark, and the benchmark is Phase 8.
- No flake detection. A test that fails intermittently produces `REGRESSED` and
  sends the agent to fix a bug its patch did not cause.
- No cross-attempt learning: attempt three is told about attempt two, not about
  attempt one.
- No partial credit. A patch that fixes four of five call sites scores the same
  as one that fixes none.

## What Phase 5 delivers

- `rewire verify ./repo` — runs the repository's own checks in a container and
  reports what they prove, before an agent is involved at all.
- `rewire propose ... --verify` — proposes a patch, then executes the checks
  against it and exits non-zero unless the verdict is `VERIFIED`.
- A baseline-and-patched comparison, so a regression means the patch caused it
  and a repository that was already failing is not blamed on the agent.
- Check detection from the repository's own configuration: pytest when there are
  tests, ruff and mypy when they are configured, byte-compilation always.
- Five check statuses, because "no linter", "linter missing from the image" and
  "linter failed" are three different things and none of them is a pass.
- A container with no network, no capabilities, a read-only root filesystem, a
  non-root user, and hard memory, CPU and process ceilings — with integration
  tests that try to break each one and assert the attempt fails.
- Host-enforced timeouts followed by `docker rm -f`, so a container that ignores
  its own limits is still killed and still leaves partial output.
- A scripted runner that makes the whole pipeline testable without Docker.

**Measured on a real repository:** a patch renaming a request field in the
source but not in the test that asserts on it is reported `REGRESSED` with
`tests` named as the regression; the same patch including the test is reported
`VERIFIED`. Both run in a container proven, by test, to have no network access.

## What Phase 5 explicitly does not deliver

- **No repair of its own.** A failing check ends a Phase 5 run;
  `propose --repair` reaches Phase 6's loop for that.
- No reproducibility. `pip install` resolves whatever is current, so a patch
  verified today may verify differently next month. Phase 8 needs pinned images
  per benchmark case.
- No caching. Every run stages the repository and reinstalls its dependencies
  from scratch, which dominates the wall clock on small repositories.
- Python only. Check detection understands pyproject, requirements, pytest, ruff
  and mypy, and nothing else.
- No sandbox for the install step's build backend, which executes untrusted code
  with network access under container confinement. See ADR-026.

## What Phase 4 delivers

- `rewire propose ./repo --old old.yaml --new new.yaml` — runs the deterministic
  pipeline, then hands the agent the findings and eight read-and-propose tools.
- A provider abstraction with Anthropic and OpenAI adapters, plus a scripted
  provider that makes the loop fully testable offline.
- Exact-string edits with Rewire computing the unified diff, verified to apply
  cleanly with `git apply`.
- A state machine whose terminal success state is `CANDIDATE`, and a
  `verified` property that is unconditionally `False`.
- Hard budgets on iterations, tool calls, tokens and files, each a stop
  condition rather than a hint.
- Per-run JSONL traces with every prompt, tool call, token and cost, flushed
  per event so a killed run still leaves a usable trace.
- Token accounting and cost estimation, where an unpriced model yields `None`
  rather than zero.

## What Phase 4 explicitly does not deliver

- **No verification of its own.** Phase 4 executes nothing and writes nothing.
  `rewire propose --verify` reaches Phase 5's sandbox for that; without the
  flag, a candidate patch still has no evidence behind it whatsoever.
- No repair loop of its own: one attempt, no feedback. `--repair` (Phase 6)
  adds it.
- No measured success rate. The agent has been run against one hand-made case;
  that is a demo, not a benchmark.

## What Phase 3 delivers

- `rewire impact ./repo --old old.yaml --new new.yaml` — joins detected changes
  to the code they affect, ranked by confidence, with `--explain` showing the
  evidence behind every score.
- A log-odds confidence model whose signals are stored on each location, so a
  ranking can be audited rather than trusted.
- Direction-aware matching: a request field is written, a response field is
  read, and disagreement is strong evidence the name refers to something else.
- Call-graph proximity, so a test or service layer one hop from the SDK is found
  even though it imports no client library.
- `rewire eval impact` — precision, recall and F1 against labelled ground truth
  at two granularities, writing `evals/results/latest.{json,md}` with the
  configuration that produced them.
- Five labelled benchmark cases, including a negative case that expects nothing.

**Current measured result: precision 1.000, recall 1.000, F1 1.000 at both
location and file granularity, over 9 expected locations in 5 cases.** That is a
statement about five hand-written cases, not about real-world accuracy; see the
debt below.

## What Phase 3 explicitly does not deliver

- No file is modified. Phase 3 reports; Phase 4 edits.
- No LLM involvement anywhere in the pipeline so far.
- No cross-language analysis: Python only, as in Phase 2.
- No fitted weights. Every number in the confidence model is a hand-assigned
  prior.

## What Phase 2 delivers

- `rewire analyze PATH` — a deterministic index of a repository's Python code:
  imports, classes, functions, methods, module-level variables, call sites,
  name references, environment reads, declared dependencies and entry points.
- `rewire search PATH PATTERN` — the same name looked up two ways, by parsing
  and by text search, shown side by side.
- Name resolution that follows imports, aliases, assignment chains and `self.x`
  instance attributes, so three different spellings of one SDK call all answer
  the same query.
- Graded reference kinds (keyword argument, dict key, subscript, parameter,
  attribute, name, string literal), each carrying the evidence weight Phase 3
  will turn into a confidence score.
- Two interchangeable text-search backends: ripgrep when installed, and a pure
  Python scanner when not. Both are held to the same contract and asserted to
  agree.
- Safety limits for untrusted repositories: symlinks never followed, per-file
  and total size caps, file-count cap, ignored build and dependency directories.
- A sample application fixture that calls the OpenAI SDK three different ways,
  contains an unparseable module, and hides a decoy `openai` package inside
  `.venv/`.

## What Phase 2 explicitly does not deliver

- **Python only.** No JavaScript, TypeScript, Go or Java analysis; tree-sitter
  is not yet wired in.
- No type inference, so a client obtained from a factory function is not traced.
- No cross-file call graph: a call to a locally defined wrapper is recorded, but
  not followed into the wrapper's own body.
- No index caching — every run reparses from scratch.
- Nothing yet joins a detected API change to an affected location. That is
  Phase 3.

## What Phase 1 delivers

- `rewire api-diff OLD NEW` — deterministic comparison of two OpenAPI 3.x
  documents in YAML or JSON, with table, `--json`, `--min-severity` and
  `--fail-on` output modes.
- Normalisation that collapses equivalent spellings before comparing: internal
  `$ref` inlining, path-level parameter inheritance, and OpenAPI 3.0 `nullable`
  treated as equal to 3.1's union-with-`null`.
- 36 typed change kinds across operations, parameters, request bodies and
  responses, each carrying the endpoint, field path and replacement needed to
  locate it in a repository.
- Direction-aware severity: a request/response variance table, tested for
  completeness, that grades the same structural edit differently depending on
  which way the data flows.
- Deterministic rename linking (`max_tokens` → `max_completion_tokens`) using
  token-overlap similarity gated on schema compatibility.
- Defensive loading of untrusted specs: size cap, `SafeLoader`, bounded YAML
  alias expansion, `$ref` cycle and depth limits, and hard errors for
  unresolvable references.
- Fixtures modelling four real migrations (OpenAI, Anthropic, Stripe, GitHub)
  with assertions on what a correct detector must report.

## What Phase 1 explicitly does not deliver

- Nothing that connects a change to source code — that is Phase 2/3.
- No Swagger 2.0, GraphQL, gRPC or SDK-changelog input.
- No multi-file specification support.
- No semantic reasoning about `oneOf`/`anyOf`/`allOf` compatibility.

## What Phase 0 delivers

- Installable package (`rewire`) with a `rewire` console script.
- Typed, nested, environment-driven settings with secret redaction.
- Structured logging in human and JSON modes, with a redaction processor.
- A domain exception hierarchy carrying stable machine-readable codes.
- `rewire doctor`: executable preflight checks for Python, Git, the Docker
  daemon, ripgrep, LLM credentials and data-directory writability.
- `rewire config`: effective configuration with secrets redacted.
- pytest / Ruff / mypy-strict configuration, pre-commit hooks and GitHub
  Actions CI on Python 3.12 and 3.13.
- Dockerfile and Compose stack for reproducible development.

## What Phase 0 explicitly does not deliver

- No API spec parsing or diffing.
- No repository indexing, AST analysis or search.
- No LLM integration; `REWIRE_LLM__PROVIDER` defaults to `null` and the
  provider adapters do not exist yet.
- No sandbox. The settings block for it exists and is validated, but nothing
  reads it yet.
- No Git or GitHub operations, no HTTP API, no dashboard.
- No evaluation datasets or published metrics.

## Known technical debt carried out of Phase 9

- **One run per model.** Phase 8 ran its two arms twice; this phase ran four
  models once each. A model that got lucky on two cases looks two cases better,
  and nothing here would show it. Repeating each model n times is the fix and
  multiplies an already hour-long run by n.
- **The prompt is a confound.** Every model is given the repair prompt that was
  tuned against gpt-4o in Phase 6. A model that would do better with different
  wording is measured on someone else's prompt, and this phase cannot separate
  "worse model" from "worse fit to this prompt".
- **No provider outside OpenAI was actually executed.** The provider layer is
  agnostic and the Anthropic path is unit-tested, but no Anthropic or OpenRouter
  model has run this benchmark. Until one does, "compares across providers"
  describes the machinery and not the published table.
- Model prices come from a dated snapshot in `llm/pricing.py`. The report prints
  the date, and an unpriced model reports unknown rather than free, but nothing
  checks the table against the providers' current published prices.
- `eval models` writes its inner per-case partial to the same
  `evals/results/migration-partial.json` that `eval migrate` uses, so a
  comparison overwrites a migration benchmark's crash-recovery file. Both are
  scratch and both are gitignored, but the collision is untidy.
- The comparison's own partial file is written after each *model*, not each case,
  so a run killed inside a model loses that model's completed cases even though
  the inner file still holds them.

## Known technical debt carried out of Phase 8

- **The dataset is small and self-authored.** Ten cases, written by the person
  who wrote the tool, from templates. Every number it produces is a statement
  about those ten cases. Real repositories, ideally sourced from actual upstream
  migrations, are what would make it an estimate of anything.
- **Two runs is not a variance estimate.** The agent is non-deterministic. Two
  independent runs agreeing on nine of ten cases is encouraging, and is not a
  confidence interval. Repeating each case n times is the fix, and multiplies
  the cost by n.
- `07-required-field-added` is a case Rewire provably cannot do, kept
  deliberately (ADR-036). It drags the headline rate down by roughly ten points
  and should stay there until impact analysis can reason about additions.
- Hidden tests are hand-written per case, which is the expensive part of adding
  one and the reason the dataset is ten cases rather than a hundred.
- The benchmark shares one sandbox image and installs dependencies per case with
  no pinning, so a rerun months later resolves different package versions. The
  results file records the image tag but not the resolved versions.
- Cases run sequentially. Ten cases across two arms is roughly half an hour of
  wall clock, most of it dependency installation.

## Known technical debt carried out of Phase 7

- **`--apply` writes into the working tree and stops.** No branch, no commit, no
  pull request, so the "review before merge" story depends entirely on the user
  running `git diff`. Phase 11 replaces this.
- A repository with no tests can never be applied automatically, because it can
  never be `VERIFIED`. That is intended (ADR-031), but it excludes a large
  fraction of real repositories and Phase 12 will need a first-class state for
  it rather than treating it as failure.
- `MigrationRequest` carries both *what* to migrate and *how far to go*, which
  will not survive Phase 13's HTTP API — a request body should not be able to
  set `allow_dirty`.
- `migration.json` has no schema and no version field. Phase 8 will read it, at
  which point the format becomes an interface that needs both.
- The clean-tree check runs before the model *and* the content check runs before
  the write, but nothing holds a lock in between. A concurrent editor can still
  change a file Rewire is not about to rewrite, and that goes unnoticed.
- `run_migration` takes settings and does its own wiring. The Phase 13 API will
  want it to accept an already-built agent and sandbox policy instead.

## Known technical debt carried out of Phase 6

- **The repair prompt was tuned against one observed failure.** The first live
  run of this phase failed, the prompt was changed to address exactly how it
  failed, and the next run passed. That is a sample of one, tuned on itself.
  Until Phase 8 measures it across a dataset, the wording is a hypothesis.
- **A flaky test is indistinguishable from a regression.** The baseline
  comparison removes the deterministic cases, not intermittent ones. Re-running
  a newly failing check before blaming the patch is the fix, and it is not
  implemented.
- Feedback carries only the immediately previous attempt. An agent that
  oscillates between two wrong patches is caught by the repeat check only when
  the diffs match exactly.
- The repeat check compares whole diffs. Two patches differing by a comment are
  treated as different, and each costs a full verification.
- Every attempt re-stages every edit, so repair costs close to a full run rather
  than a delta. Combined with the sandbox's lack of caching (Phase 5 debt), a
  three-attempt run reinstalls dependencies three times.
- `RepairPolicy.max_total_tokens` defaults to three times the per-task budget
  from settings, chosen because the default attempt count is three. It is
  arithmetic, not a measurement.
- Nothing writes a machine-readable record of the whole repair run. Each attempt
  gets its own trace and `verification.json`, but Phase 8 will want one file
  describing the sequence.

## Known technical debt carried out of Phase 5

- **Verification is not reproducible.** `pip install` resolves whatever is
  current, and the sandbox image is a floating tag. A patch verified today may
  verify differently next month, which is acceptable for a development tool and
  unacceptable for the published benchmark numbers in Phase 8. That phase needs
  pinned images and pinned resolutions per case.
- **Installing a repository executes its build backend**, with network access,
  inside the container. It is the weakest point in the isolation story. Confined
  and reported, but not removed — see ADR-026.
- **No caching whatsoever.** Every run re-stages the repository and reinstalls
  its dependencies. On the demo repository that is roughly 15 seconds of install
  for one second of checks, and Phase 6's repair loop multiplies it by the retry
  count. A per-repository image layer or a warm virtual environment is the fix.
- Checks are hard-coded to a Python toolchain. A repository using tox, nox,
  hatch scripts, a Makefile or a non-Python language gets byte-compilation and
  nothing else.
- `ruff` and `mypy` are installed unpinned, so their versions — and therefore
  the lint and typecheck results — can differ between the baseline and a later
  run of the same repository. The baseline comparison protects against this
  within a single run, but not across runs.
- Check output is truncated to 16 000 characters from the middle. A test suite
  that fails in a hundred places loses the middle ninety, which will matter when
  Phase 6 feeds failures back to the agent.
- The `SandboxSettings` defaults (2 GB, 2 CPUs, 512 processes, 600 seconds) are
  still informed guesses. They are now *used*, which is progress, but nothing
  has measured whether they are right for a large repository.
- `rewire verify` on a repository with no tests reports `INCONCLUSIVE` and exits
  zero, while `propose --verify` treats the same verdict as failure. Defensible
  — one is an inspection, the other is a gate — but the asymmetry is worth
  revisiting once Phase 7 has a single `migrate` entry point.

## Known technical debt carried out of Phase 2

- Reference evidence weights are hand-assigned constants, not fitted to data.
  Phase 8 should replace them with values chosen from labelled impact examples.
- Binding resolution is last-write-wins per scope. A name rebound part-way
  through a function resolves to whichever assignment the walk saw most
  recently, which is right for the common case and wrong for genuinely
  polymorphic code.
- `RepositoryIndex` is held entirely in memory and rebuilt on every command.
  `Settings.index_dir` exists for a cache that does not yet exist (Phase 17).
- Relative imports (`from . import x`) are recorded but never resolved, so calls
  through them stay unresolved. Resolving them needs a package-root inference
  step that has not been written.
- `module_path_for` does not strip source roots, so a file at `src/pkg/mod.py`
  gets the module path `src.pkg.mod` rather than `pkg.mod`. Consistent, but not
  what the interpreter would call it.

## Known technical debt carried out of Phase 4

- **The agent's own summary is unreliable and is displayed anyway.** In the
  first live run it claimed to have updated "two occurrences" in a file where
  the diff shows one. The diff is the truth and is shown alongside, but a reader
  skimming the summary could still be misled.
- **Recall varies between runs.** Three live runs on the same case produced
  three different sets of edits: one missed the dict-literal payload key, one
  missed nothing, one changed the test file only partially. Phase 8 needs to
  measure this rather than anecdote it.
- **The pricing table is a dated snapshot** (`PRICING_SNAPSHOT_DATE`), hand
  transcribed. It will drift. Unknown models correctly report `None`, but a
  stale *known* price is silently wrong.
- One live-model integration test would be valuable, marked `llm` and skipped
  by default. Currently the adapters are only exercised against stubs, so a
  breaking SDK change would not be caught until a real run.
- `AgentBudget.max_output_tokens` defaults to 8192; no measurement justifies
  that number.
- The agent is single-attempt and has no notion of its own confidence. It cannot
  say "I am unsure about this edit", which Phase 6's repair loop will want.

## Known technical debt carried out of Phase 3

- **The benchmark is far too small to justify its own score.** Five cases and
  nine expected locations, all written by the same person who wrote the
  analyser. A perfect result here means the obvious failure modes are handled,
  not that the analyser is accurate. Phase 8 needs cases drawn from real
  repositories and real migrations, labelled by someone other than the author.
- Every weight in `impact/scoring.py` is a hand-assigned prior. They were chosen
  to order candidates the way a reviewer would, and adjusted twice when
  measurement disagreed, but they are not fitted. Phase 8 should replace them
  with values chosen from a precision/recall curve.
- `DEFAULT_MIN_CONFIDENCE` (0.35) is likewise a guess. One benchmark false
  positive sat at 0.354, which is uncomfortably close; it was fixed by adding a
  missing signal rather than by moving the threshold, but the threshold itself
  remains unjustified by data.
- Package inference matches specification title tokens against names the
  repository uses. It links `OpenAI API` to `openai` and fails on `GitHub API`
  to `PyGithub`. `--package` exists for that, but the failure is silent.
- Rename linking is used for the `replacement` field but the *replacement* name
  is not yet searched for. A repository already partially migrated will not have
  its new-name call sites reported as related.
- Nested renames are not linked at all (Phase 1 limitation), so a change deep in
  a schema loses its replacement.

## Known technical debt carried out of Phase 1

- Severity for composition keywords (`oneOf`/`anyOf`/`allOf`) is a blanket
  "potentially breaking". Grading them properly is a subtyping problem and is
  deliberately not attempted.
- The rename threshold (0.5) and the character-similarity gate (0.8) are tuned
  against the bundled fixtures, not measured against a labelled dataset. Phase 8
  should replace those constants with values chosen from precision/recall on
  real migrations.
- `ChangeType` has 36 members and will grow. The mapping from schema findings to
  change types is table-driven, but the tables are not exhaustively asserted the
  way the severity table is.
- Response bodies are modelled with `Body.required = True` unconditionally,
  since OpenAPI has no notion of an optional response. Harmless today, but it
  means `Body` carries a field that is meaningless in one of its two uses.

## Known technical debt carried out of Phase 0

- `Settings.database_url` defaults to a `sqlite+aiosqlite` URL, but no
  SQLAlchemy engine, models or migrations exist yet. The value is currently
  configuration-only and unvalidated against a live driver (Phase 13).
- `AgentSettings` defaults are informed guesses that Phase 6 will have to
  justify with measurements. `SandboxSettings` is now read by the sandbox
  (Phase 5), but its numbers are equally unmeasured.
- The `docker.sock` mount in `docker-compose.yml` is host-root-equivalent. It is
  acceptable for local development and is called out in ADR-003; Phase 16 must
  revisit it before anything resembling a deployment.
- `docker compose run --rm rewire` relies on `group_add` to reach the Docker
  socket. The default gid 0 is correct for Docker Desktop; Linux hosts must
  set `DOCKER_GID`. This is documented but not auto-detected.
- The GitHub Actions runner warns that `actions/checkout@v4`,
  `actions/upload-artifact@v4` and `astral-sh/setup-uv@v5` target Node.js 20 and
  are being forced onto Node.js 24. Harmless today; the actions need bumping
  when their next majors land.
