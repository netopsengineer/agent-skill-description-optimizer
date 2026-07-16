# Skill Description Optimizer (no API key)

[![Built for Claude Code](https://img.shields.io/badge/Built_for-Claude_Code-D97757?style=for-the-badge&logo=claude&logoColor=white&labelColor=1a1a1a)](https://docs.claude.com/en/docs/claude-code)
[![Release](https://img.shields.io/github/actions/workflow/status/netopsengineer/agent-skill-description-optimizer/release.yml?branch=main&style=for-the-badge&logo=semanticrelease&logoColor=white&label=Release&labelColor=1a1a1a)](https://github.com/netopsengineer/agent-skill-description-optimizer/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-3fb950?style=for-the-badge&labelColor=1a1a1a)](https://opensource.org/licenses/MIT)

Optimizes the `description:` frontmatter of a Claude Code / Agent **skill** - the
text that decides whether Claude invokes the skill. Both halves of the loop
(evaluation **and** improvement) run entirely through the `claude -p` CLI, so **no
`ANTHROPIC_API_KEY` is required** - it reuses the auth the CLI already has.

## Why this exists

Skill-creator's own `run_eval.py` already evaluates triggering with `claude -p` (no
key). The *only* part of its optimization loop that needs an API key is the
**improver** (`improve_description.py` / `run_loop.py`), which calls the `anthropic`
Python SDK. This project replaces that piece: **both** the evaluation and the
improvement go through `claude -p`.

## Relationship to skill-creator

This is a re-implementation of skill-creator's optimization loop, intended to be
usable in its place without an API key. Where it matches and where it deliberately
differs:

| Behavior                                    | This tool                                         | skill-creator              |
|---------------------------------------------|---------------------------------------------------|----------------------------|
| Trigger detection (stream tool-call intent) | **Equivalent** logic (see note)                   | `run_eval.py`              |
| Improver transport                          | `claude -p`                                       | `anthropic` SDK            |
| Improver avoids repeating past attempts     | Yes                                               | Yes                        |
| 1024-char limit with shorten retry          | Yes                                               | Yes                        |
| Early-exit when all train queries pass      | Yes                                               | Yes                        |
| Held-out selection (avoid overfitting)      | Yes - mean accuracy, ε tie-break on weakest model | Yes - test pass count      |
| Multiple eval models                        | Yes (`--models haiku,sonnet,opus`)                | Single `--model`           |
| Train/test split                            | Seeded shuffle, stratified                        | Seeded shuffle, stratified |
| Live HTML report                            | Yes (`--report` / `--results-dir`)                | Yes                        |

Trigger detection is **equivalent**, not byte-identical: the streamed path is a
faithful port of skill-creator's tool-intent detection, but the non-streaming
`assistant` fallback here intentionally searches the serialized tool input more broadly
(matching the injected command anywhere in it) than upstream's field-specific matching -
functionally equivalent and arguably more robust.

### Drop-in for skill-creator's optimization step

It accepts skill-creator's `run_loop.py` flags (`--model`, `--max-iterations`,
`--holdout`, `--runs-per-query`, `--num-workers`, `--trigger-threshold`, `--verbose`),
makes `--out` optional, and prints a JSON object containing `best_description` to
**stdout** (progress goes to stderr) - so skill-creator's Step 3/4 invocation works
against it unchanged. From another repository, the canonical invocation is
project-targeted `uv` (always the pinned 3.14 interpreter); agent/headless callers pass
`--report none` so stdout stays a clean JSON capture and no browser opens:

```bash
uv run --project /ABSOLUTE/PATH/TO/agent-skill-description-optimizer \
  optimize-skill-description \
  --eval-set /ABSOLUTE/PATH/TO/eval.json --skill-path /ABSOLUTE/PATH/TO/skill \
  --model MODEL_ID --report none --max-iterations 5 --verbose
# then read `best_description` from the JSON it prints to stdout
```

For direct execution without `uv`, name a 3.14+ interpreter explicitly
(`python3.14 optimize_description_v2.py ...`); an arbitrary `python3` is unsupported and
exits 1 with a `Requires Python >=3.14; ...` message rather than a traceback.

The auto-refreshing **HTML report** is ported too: `--report auto` (the default) opens
a live, self-refreshing report in your browser, `--report none` disables it (recommended
for agents), and `--results-dir <dir>` collects `results.json`, `report.html`, and
`logs/` under a timestamped subdirectory. Both halves of the loop still run through
`claude -p`.

## Requirements

- The `claude` CLI, logged in (run `claude` once interactively if unsure). No API key.
  It is checked at startup: a missing or non-executable CLI exits 1 with a one-line
  message, not a mid-run traceback.
- Python **3.14+** and [`uv`](https://docs.astral.sh/uv/).
- For triggering to be measurable, the skill's tasks should be ones Claude would
  actually consult a skill for (see [Caveats](#caveats)).

## Install

The CLI is installable from PyPI:

Via `uv`:

```bash
uv tool install agent-skill-description-optimizer --python '>=3.14'
```

Via `uvx` for a temporary environment or CI:

```bash
uvx --python '>=3.14' --from agent-skill-description-optimizer \
  optimize-skill-description --help
```

Via `pip`:

```bash
pip install agent-skill-description-optimizer
```

Install from source:

```bash
uv sync
```

This installs the `optimize-skill-description` console script. The tool can be run
three equivalent ways:

```bash
uv run optimize-skill-description --skill-path ... --eval-set ... --out ...
uv run python -m skill_optimizer       --skill-path ... --eval-set ... --out ...
uv run python optimize_description_v2.py --skill-path ... --eval-set ... --out ...
```

## How it works

1. Reads the skill's current description from `SKILL.md`.
2. **Evaluates** it: injects the description as a temporary slash-command into a
   throwaway temp project, runs each eval query through `claude -p`, and detects -
   from the streamed tool-call intent - whether the model decided to invoke it.
   Runs across one or more models, `--repeats` times each.
3. **Improves** it: sends the current description + the failing queries to an
   improver model via `claude -p --effort high`, which returns a rewritten
   description as JSON.
4. Splits the eval set into **train / held-out test** (stratified by
   `should_trigger`), iterates, and keeps the description with the best **held-out**
   score - selecting on held-out rather than train avoids overfitting the eval set.
   `--holdout 0` disables the split (skill-creator's semantics): every query trains
   and selection falls back to the **train** score. A positive holdout needs at least
   two queries in each `should_trigger` class and must leave a train and a test member
   in each, or the run is rejected up front.

## Eval set format

A JSON array (a `{"queries": [...]}` or `{"evals": [...]}` wrapper is also accepted -
exactly one of those keys, with unrelated metadata alongside it ignored):

```json
[
  {"query": "my imports are a mess in billing.py, sort them and drop the unused ones", "should_trigger": true},
  {"query": "format this json blob so it's readable", "should_trigger": false}
]
```

Each item must be an object with a string `query` and a boolean `should_trigger`
(integers and truthy values are **not** coerced); item order, duplicate queries, and
extra item keys are preserved. A missing or unreadable file, an empty set, a malformed
root, or a bad item type is rejected up front with an `Invalid eval set: ...` message,
before any evaluation runs.

Aim for 8-10 of each. The valuable `should_trigger: false` cases are **near-misses** -
queries that share keywords with the skill but actually need something else - not
obviously-irrelevant queries.

## Usage

```bash
# Minimal: tune one skill against an eval set, on Sonnet.
uv run optimize-skill-description \
  --skill-path /path/to/skills/my-skill \
  --eval-set my-skill-queries.json \
  --out runs/my-skill

# Robust across models, disable a competing installed plugin during eval,
# and write the winner back into SKILL.md (creates SKILL.md.bak).
uv run optimize-skill-description \
  --skill-path /path/to/skills/my-skill \
  --eval-set my-skill-queries.json \
  --out runs/my-skill \
  --models haiku,sonnet,opus \
  --disable-plugin some-plugin@some-marketplace \
  --iterations 3 \
  --write
```

The chosen description is reported at the end and saved in `report.json`. Without
`--write`, nothing touches your skill - you just get the recommendation.

### Key flags

| Flag                | Default    | Notes                                                                                                                                                                                     |
|---------------------|------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `--skill-path`      | (required) | Dir containing `SKILL.md`.                                                                                                                                                                |
| `--eval-set`        | (required) | JSON eval set (see format above).                                                                                                                                                         |
| `--out`             | temp dir   | Where run artifacts are written; defaults to a temporary directory.                                                                                                                       |
| `--models`          | `sonnet`   | Comma list; aliases `haiku`/`sonnet`/`opus` or full model ids. Optimizes on the **mean** accuracy across them but reports the **min** so a winner can't tank the weakest model.           |
| `--improver-model`  | `opus`     | Model that rewrites the description.                                                                                                                                                      |
| `--model`           | (none)     | Single model for **both** eval and improver (skill-creator compat); `--models`/`--improver-model` override it.                                                                            |
| `--description`     | (none)     | Override the starting description instead of reading `SKILL.md`'s.                                                                                                                        |
| `--improver-effort` | `high`     | Passed as `--effort`; use `none` to omit.                                                                                                                                                 |
| `--repeats`         | `3`        | Runs per (query, model). More = less noise, more cost.                                                                                                                                    |
| `--iterations`      | `5`        | Improve->re-eval rounds, inclusive `0`-`50` (`0` = baseline-only, no proposals). Stops early once all train queries pass.                                                                 |
| `--threshold`       | `0.5`      | Trigger-rate at/above which a query counts as "triggered".                                                                                                                                |
| `--test-frac`       | `0.4`      | Held-out fraction (stratified by class), in `[0, 1)`. `0` disables the holdout and selects on train; a positive value needs >=2 queries per class and leaves a member in each.            |
| `--seed`            | `42`       | RNG seed for the stratified split. Fixed for reproducibility and echoed in the output JSON, so a run reproduces from its own record; vary it to check split robustness.                   |
| `--select-epsilon`  | `0.05`     | Held-out mean differences within this band count as ties, broken by the weakest-model accuracy. `0` = strict mean-only selection.                                                         |
| `--max-desc-chars`  | `1024`     | Hard character budget. An over-budget candidate can never be selected, and `--write` refuses an over-budget winner.                                                                       |
| `--report`          | `auto`     | HTML report: `auto` (temp file, opened in a browser), `none` to disable, or an explicit output path.                                                                                      |
| `--dry-run`         | off        | Validate inputs (eval set, skill, holdout split, `claude` availability) and print the run plan as JSON to stdout with an estimated `claude -p` call count, then exit spending no tokens.  |
| `--results-dir`     | (none)     | Save `results.json`, `report.html`, and `logs/` under a timestamped subdirectory here. Mutually exclusive with `--out`.                                                                   |
| `--disable-plugin`  | (none)     | Repeatable. Disables a **plugin-provided** skill (by plugin id) during eval so it can't out-compete the injected candidate. Does not affect standalone user/project skills - see Caveats. |
| `--write`           | off        | Write the best description into `SKILL.md` (backs up to `SKILL.md.bak`).                                                                                                                  |
| `--version`         | -          | Print the installed package version and exit.                                                                                                                                             |

Skill-creator aliases are accepted for the matching flags: `--max-iterations`
(`--iterations`), `--holdout` (`--test-frac`), `--runs-per-query` (`--repeats`),
`--num-workers` (`--workers`), `--trigger-threshold` (`--threshold`). The
`best_description` is also printed as a JSON object to stdout.

The `haiku`/`sonnet`/`opus` aliases are **source-pinned** convenience defaults that
currently resolve to `claude-haiku-4-5-20251001`, `claude-sonnet-5`, and
`claude-opus-4-8`; any other value is passed through as a full model id unchanged. The
alias targets track a moving lineup, so for reproducible runs pass an explicit full
`--model`/`--models`/`--improver-model` id rather than relying on the aliases.

### Output

The best description is reported on stderr, printed as JSON to **stdout**, and
written (with full history) to the artifact dir:

- `baseline.json` - full-set eval of the current description.
- `iterN_prompt.txt`, `iterN_proposal.json`, `iterN_eval.json`,
  `iterN_improve.json` (raw improver transcript), plus `iterN_improve_retry.json` when
  a slot's first attempt failed retryably and was retried - per iteration. The raw
  transcripts are the **only** place raw improver stdout/stderr and return codes are
  kept; public output never carries them.
- `report.json` - baseline vs best description, held-out scores, full history.

The stdout JSON is a **superset** of skill-creator's `run_loop` envelope
(`best_description`, `best_score`, `exit_reason`, `iterations_run`, `is_best` per
history entry, char counts, ...) plus this tool's own keys (`best_test_mean`,
`select_epsilon`, per-model accuracy, `seed`, `estimated_claude_calls`, ...) - nothing
from the old shape is removed. `--dry-run` instead prints a `{"dry_run": true, ...}`
plan object (same estimate) and exits without spending tokens. Two
of the shared keys carry different **semantics**, though: `iterations_run` counts the
improve rounds actually entered - excluding the baseline, and *including* a round whose
improver retries were exhausted (which produces no `history` row) - whereas skill-creator
reports `len(history)`; and `history` numbers the baseline as `iteration` 0 here, where
skill-creator starts at 1. Consumers that only read `best_description` are unaffected. The
score strings (`best_score`, history `train_passed/train_total`, ...) use a
**judged-query** denominator (`k/N` with `(+u unjudged)` when probes errored), so a
transient CLI failure reads as "unjudged", not as a miss - a deliberate deviation from
skill-creator's total-query denominator.

#### Improver failures and retries

An improver call that **times out**, returns **unparseable JSON**, or is still over the
character budget **after the shorten retry** is a *retryable* failure: the slot is
retried once, and if the second attempt also fails retryably the slot is recorded and
the loop continues to the next iteration (the last verified best is preserved). A
**completed non-zero exit** or launch-budget exhaustion is *fatal* and aborts the run
without emitting a success envelope: the CLI reports it as a single `stderr` line (exit
1, empty stdout, no traceback - so a stdout-parsing caller fails legibly), while the raw
child return code/stderr stay in the per-iteration transcript. Any other unclassified
error propagates unchanged. Retry work is bounded: at most 50 proposal slots and 200
improver child processes per run.

Every result (stdout, `report.json`, live report) always carries an
`improver_failed_iterations` list - `[]` when nothing failed. Each entry is
`{"iteration": N, "attempt_count": 2, "errors": [{"attempt", "kind", "message"}, ...]}`,
where `kind` is one of `timeout` / `invalid_output` / `length_limit` and `message` is a
fixed, non-sensitive template (raw stdout/stderr/paths never appear here).

With `--report auto` (default) a live, self-refreshing `report.html` opens in your
browser and updates after each iteration; `--results-dir <dir>` additionally writes
`results.json`, `report.html`, and per-iteration `logs/` under a timestamped
subdirectory.

## Project layout

```text
src/skill_optimizer/     # the package
  skill_md.py            # SKILL.md frontmatter parse / write
  interpreter.py         # pure trigger-decision state machine over stream-json
  evaluation.py          # claude -p transport, aggregation, concurrent eval
  improver.py            # improver prompt + claude -p call
  selection.py           # train/test split, model resolution, candidate selection
  cli.py                 # argparse + orchestration
  models.py              # shared types (EvalConfig, TypedDicts) and aliases
optimize_description_v2.py  # entry/compat shim re-exporting the package
tests/                   # pytest suite + recorded stream-json fixtures
```

## Development

```bash
uv run pytest          # test suite (characterization + unit + integration)
uv run pyright         # strict type checking
uv run ruff check .    # lint
uv run pydoclint src/skill_optimizer optimize_description_v2.py  # docstring checks
```

Integration tests exercise the real subprocess/stream-parsing transport against a
fake `claude` binary and recorded `claude -p` streams in `tests/fixtures/` - no live
CLI calls or API key needed to run the suite.

## Caveats

These are what the evals taught us - read them before trusting a score:

- **Triggering is a proxy.** We inject the description as a slash-command and watch
  whether the model decides to call it. It's a consistent *relative* signal for
  comparing descriptions; it is not a perfect predictor of real installed-plugin
  triggering.
- **A non-skill tool first reads as no-trigger.** Detection keys on the *first*
  tool the model reaches for; if a substantive query makes it run `Bash` (or
  another tool) before the skill, that run scores as a no-trigger. This matches
  skill-creator's own detection - design eval queries with it in mind.
- **Capable models under-trigger by design.** Claude only consults a skill for tasks
  it can't easily do itself. Simple, one-step queries often won't trigger *any*
  skill no matter how good the description is - and for skills that wrap tools the
  model already knows well, the achievable trigger rate has a low ceiling that
  description tuning cannot raise. Make queries substantive, and judge results with
  that ceiling in mind.
- **Disable competing skills.** If the skill you're tuning is already installed, its
  real description will compete with the injected candidate and pollute results.
  `--disable-plugin <id@marketplace>` handles this **only for plugin-provided skills**
  (it writes `enabledPlugins: {<id>: false}` into `--settings`, keyed by plugin id). A
  standalone **user/project** skill of the same name (e.g. under `~/.claude/skills/`) is
  not a plugin and is unaffected; isolate it from discovery separately for the run
  (move it aside) and restore it afterward. This tool never mutates a user skill
  directory for you.
- **Cost.** Total `claude -p` calls ≈ `queries x models x repeats x (iterations + 1)` -
  one full-set evaluation per baseline/candidate (train and held-out views are sliced
  from it, not re-evaluated). An unjudgeable probe (timeout/CLI error) is retried at
  most once, so real counts run slightly above this estimate - not a 2x multiplier.
  Start small (`--models sonnet --repeats 2 --iterations 1`) to sanity-check before
  scaling up.

## Contributing

Local setup, commit conventions, the release process, and CI/CD - see
[CONTRIBUTING.md](CONTRIBUTING.md).
