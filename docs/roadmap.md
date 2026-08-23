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
| 4 | First coding agent (tool-restricted, produces candidate patches only) | planned |
| 5 | Docker sandbox (isolated execution, resource limits, verification) | planned |
| 6 | Agent repair loop (sandbox feedback → bounded retries) | planned |
| 7 | End-to-end MVP (`rewire migrate`) | planned |
| 8 | Evaluation framework (datasets, metrics, published results) | planned |
| 9 | Model comparison across providers | planned |
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
- `SandboxSettings` and `AgentSettings` are validated but unread; their defaults
  are informed guesses that Phase 5 and Phase 6 will have to justify with
  measurements.
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
