# AGENTS.md

Agent execution contract for this repo. Human explanation is canonical in `README.md`;
reference it rather than duplicating it, and keep operator narration out of this file.

## Purpose

Optimize a skill's `description:` frontmatter (the text that gates skill invocation).
Evaluation and improvement both run through `claude -p`, so no `ANTHROPIC_API_KEY` is
required. Entry point: `optimize_description_v2.py` (shim over the `src/skill_optimizer/`
package). Runtime: Python standard library only; interpreter floor Python 3.14+.

## Invoke

Canonical cross-repo call (project-targeted `uv` pins the 3.14 interpreter):

```bash
uv run --project /ABSOLUTE/PATH/TO/agent-skill-description-optimizer \
  optimize-skill-description \
  --eval-set /ABSOLUTE/PATH/TO/eval.json \
  --skill-path /ABSOLUTE/PATH/TO/skill \
  --model MODEL_ID --report none --max-iterations 5 --verbose
```

1. Pass `--report none` for every headless run: stdout stays a single JSON object and no
   browser opens. Mandatory for a stdout-parsing caller.
2. Parse stdout as JSON; read `best_description`.
3. Apply it to the skill's `SKILL.md` frontmatter, or pass `--write` to apply it
   automatically (backs up to `SKILL.md.bak`; refuses an over-`--max-desc-chars` winner).
4. Treat stderr as progress only — tail it, never parse it.

Equivalent in-repo forms: `uv run optimize-skill-description ...`, `uv run python -m
skill_optimizer ...`, `uv run python optimize_description_v2.py ...`. Direct execution
requires a named 3.14+ interpreter (`python3.14 optimize_description_v2.py ...`). Full
flag list: `README.md`.

## Preconditions

Checked at startup, before any token spend or artifact write. Each failure exits 1 with a
one-line stderr message and empty stdout (a stdout-parsing caller fails legibly, never on
a `JSONDecodeError` or mid-run traceback):

| Condition                                | Failure message prefix                           |
|------------------------------------------|--------------------------------------------------|
| `claude` on `PATH` and executable        | `claude CLI not found or not executable:`        |
| Interpreter is Python >=3.14             | `Requires Python >=3.14;`                        |
| Eval set well-formed                     | `Invalid eval set:`                              |
| Holdout split satisfiable                | `Invalid holdout split:`                         |
| `--out` and `--results-dir` not both set | `--out and --results-dir are mutually exclusive` |
| `--iterations` in `[0, 50]`              | `--iterations must be between 0 and 50`          |

## Dry run

Add `--dry-run` to run all Preconditions, print a `{"dry_run": true, ...}` plan object to
stdout, and exit 0 without spending tokens or writing artifacts. Decide from
`estimated_claude_calls` (upper bound):

- Within budget → re-invoke without `--dry-run`.
- Over budget → reduce `--models`, `--repeats`, or `--iterations`, or raise the budget.

A real run also emits `estimated_claude_calls` and `seed` in its output JSON and logs one
`Plan: ...` line to stderr at startup.

## Output

- stdout: exactly one JSON object — a superset of skill-creator's `run_loop` envelope.
- Required read: `best_description`.
- Also present: `best_score`, `best_test_mean`, `seed`, `estimated_claude_calls`,
  `improver_failed_iterations`, `history` (each entry has `is_best`), plus every
  `run_loop` key. `--dry-run` emits the plan object instead (no `best_description`).
- Artifacts under `--out` (else a temp dir): `baseline.json`, and per iteration
  `iterN_prompt.txt`, `iterN_proposal.json`, `iterN_eval.json`, `iterN_improve.json`
  (raw improver transcript — the only place raw child stdout/stderr/returncode is kept),
  plus `iterN_improve_retry.json` when a slot retried.

## Failure modes

| Mode                                                                       | Behavior                                                                                                                      | Recovery                      |
|----------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------|-------------------------------|
| Transient probe (timeout / CLI hiccup)                                     | Retried once; if still undecided, scored `unjudged` and excluded from denominators — never a miss                             | none                          |
| Improver retryable (timeout / unparseable JSON / over-limit after shorten) | Retried once; a second failure is recorded in `improver_failed_iterations` and the loop continues from the last verified best | inspect `iterN_improve*.json` |
| Improver fatal (nonzero `claude` exit / launch-budget exhaustion)          | Exit 1, one stderr line, empty stdout, no envelope                                                                            | inspect `iterN_improve.json`  |

## Defaults and reproducibility

- The train/test split is seeded (`--seed`, default `42`) and the seed is echoed in the
  output JSON, so a run reproduces from its own record. Vary `--seed` to test whether a
  score depends on one split.
- With only the documented flags, defaults match skill-creator's `run_loop`
  (`--num-workers 10`, `--holdout 0.4`, `--max-iterations 5`). Exception: `--timeout` is
  `90` (upstream `30`) so the retry/tri-state path does not score a slow probe as a miss.

## Eval set format

`[{"query": <str>, "should_trigger": <bool>}, ...]`. Constraints:

- `query` is a string; `should_trigger` is a bool (no int/truthy coercion).
- A `{"queries": [...]}` or `{"evals": [...]}` wrapper is accepted (exactly one key).
- For `--holdout > 0`: at least 2 queries per `should_trigger` class, leaving at least one
  train and one test member in each.
- Make `should_trigger: false` cases near-misses and queries substantive; trivial
  one-step queries under-trigger regardless of description quality.

## Use in place of skill-creator's run_loop

skill-creator hardcodes `python -m scripts.run_loop` and will not route here on its own.
Run this instead; it accepts run_loop's flags (`--model`, `--max-iterations`, `--holdout`,
`--runs-per-query`, `--num-workers`, `--trigger-threshold`, `--verbose`) and prints
`best_description` to stdout, so the surrounding workflow is unchanged. To wire it into
another project, add to that project's `AGENTS.md`:

```text
For skill `description:` optimization, run
`uv run --project /ABSOLUTE/PATH/TO/agent-skill-description-optimizer
optimize-skill-description --report none` (reads `best_description` from stdout JSON)
instead of skill-creator's `scripts/run_loop.py` — no API key required.
```

## Validation gates

Run before declaring any change complete; all must pass:

```bash
uv sync
uv run pytest
uv run pyright
uv run ruff check .
uv run pydoclint src/skill_optimizer optimize_description_v2.py
uvx bandit -c pyproject.toml -r src optimize_description_v2.py
sourcery review src/skill_optimizer optimize_description_v2.py tests
```

`uv run pytest` enforces **100% line + branch coverage** (`--cov` is wired into
`addopts`; the floor is `[tool.coverage.report] fail_under = 100`). Every statement and
branch is either exercised or covered by a justified exclusion in `[tool.coverage.report]
exclude_lines` — never a bare pragma. Use `uv run pytest --no-cov` for a partial
file/selector run where a sub-100 result is expected.

`ruff check .` includes flake8-bandit (`S`) rules (`[tool.ruff.lint] extend-select`);
`bandit` itself still runs separately (also wired as a pre-commit/prek hook) because its
`S404`-equivalent check (`B404`, flagging the bare `import subprocess`) needs ruff's
unstable `--preview` flag to reproduce — everything else the two tools agree on. Tests
are exempted from both (assert is normal pytest idiom, and integration tests spawn real
subprocesses per the Invoke forms above) via `per-file-ignores`/`exclude_dirs`.

## Packaging invariants

- Backend is `uv_build`; import package is `src/skill_optimizer`
  (`module-name = "skill_optimizer"`, `module-root = "src"`); `[project].version` is
  static.
- `--version` prints `importlib.metadata.version("agent-skill-description-optimizer")`
  (falls back to `0.0.0+unknown` from an uninstalled source checkout).
- All four invocations must work: `optimize-skill-description`, `python -m
  skill_optimizer`, `python optimize_description_v2.py`, and `python3.14
  optimize_description_v2.py` from a clean checkout (the shim inserts `src/` on
  `sys.path`).
- `uv build --no-sources` must produce an sdist + wheel; the wheel contains
  `skill_optimizer` (with `py.typed`) and NO top-level `optimize_description_v2` module.

## End-to-end test (live `claude` CLI)

Distinct from `tests/` (mocked `claude` binary via `SKILL_OPTIMIZER_CLAUDE_BIN`, no
tokens, run via `uv run pytest`). This section drives the real `claude` CLI, a real
Python 3.14 interpreter, and a real package build against a checked-in fixture, to catch
regressions the mock cannot reach: real subprocess/env behavior, real triggering
against a live model, real interpreter resolution, real packaging. Mandatory before
packaging a release or publishing; recommended after any change to `cli.py`,
`evaluation.py`, `improver.py`, `skill_md.py`, `pyproject.toml`, or the packaging config.
Not run on every commit: it spends real tokens (~50-90 `claude -p` calls total across
the gates below) and takes several minutes.

Preconditions (missing/invalid eval set, bad holdout split, `--out`+`--results-dir`
conflict, `--iterations` out of range, missing `claude` CLI) are already exercised
against a fake binary by `tests/test_cli.py`; do not re-derive them here. Re-run a
Preconditions-table row's exact command against the live CLI only when investigating a
regression specific to precondition handling.

### Fixture

- Skill: `tests/e2e/skill/SKILL.md` (`name: e2e-fixture-skill`).
- Eval set: `tests/e2e/eval_set.json` (8 queries: 4 `should_trigger: true` regex-
  debugging queries, 4 `should_trigger: false` near-misses — SQL `LIKE`, `.gitignore`
  globs, CSV parsing, JSON Schema).
- The fixture's `name:`/`description:` are deliberately weak (see the warning comment in
  the file) so a real baseline run reliably lands near `mean_accuracy=0.5` with 0%
  recall on the `should_trigger: true` queries — this is what forces Gate 3 to actually
  invoke the improver instead of exiting at iteration 0 on an already-saturated score.
  Never edit this file's `name:`/`description:`. Never run `--write` against this path
  directly — copy it to a scratch directory first.

### Gate 1 — environment sanity

```bash
which claude && claude --version
python3.14 --version
uv --version
```

Pass: all three exit 0 and print a version. If any is missing, stop and mark
**BLOCKED** (install/authenticate the `claude` CLI; install Python 3.14; install `uv`) —
nothing downstream is meaningful without this.

### Gate 2 — `--dry-run` contract

```bash
uv run optimize-skill-description \
  --skill-path tests/e2e/skill --eval-set tests/e2e/eval_set.json \
  --report none --dry-run
```

Pass, all required:

- Exit 0; stdout is exactly one JSON object.
- stdout equals, key-for-key (field order may differ): `{"dry_run": true, "skill":
  "e2e-fixture-skill", "queries": 8, "train_size": 4, "test_size": 4, "holdout": 0.4,
  "seed": 42, "models": ["claude-sonnet-5"], "improver_model": "claude-opus-4-8",
  "improver_effort": "high", "repeats": 3, "iterations": 5, "threshold": 0.5,
  "estimated_eval_calls": 144, "estimated_improver_calls": 5, "estimated_claude_calls":
  149}`.
- No new file or directory appears anywhere under the OS temp dir or the fixture's own
  directory (a dry run must not create even its default temp-dir artifacts).

Any mismatch is a regression in the dry-run contract, the cost-estimate formula, or the
stratified split. Stop; do not proceed to Gate 3.

### Gate 3 — real optimize loop (improver engagement)

```bash
SCRATCH=$(mktemp -d)
cp -r tests/e2e/skill "$SCRATCH/skill"
mkdir -p "$SCRATCH/out"
uv run optimize-skill-description \
  --skill-path "$SCRATCH/skill" --eval-set tests/e2e/eval_set.json \
  --models sonnet --repeats 2 --iterations 2 --test-frac 0.25 \
  --out "$SCRATCH/out" --report none --write --verbose
```

Pass, all required (numeric bounds allow for real-model non-determinism; the improver's
exact rationale/description wording is never asserted):

- Exit 0.
- Baseline `mean_accuracy <= 0.75` with `recall <= 0.5` on the `should_trigger: true`
  queries (last verified: `mean=0.500`, `recall=0%`). If baseline reads `1.0`, run the
  fixture-integrity check below before trusting anything else in this run.
- `$SCRATCH/out/` contains `baseline.json`, `report.json`, and at least one full
  `iter1_prompt.txt` / `iter1_proposal.json` / `iter1_eval.json` / `iter1_improve.json`
  set — proof the improver was actually invoked, which the mocked suite never does
  against a real model.
- `report.json`'s `history` has exactly one entry with `"is_best": true`, and that
  entry's `full_mean`/`test_mean` is `>=` the baseline entry's.
- The captured stdout parses as one JSON object and is byte-identical to
  `$SCRATCH/out/report.json`.
- `$SCRATCH/skill/SKILL.md.bak` exists with the fixture's original `description:`;
  `$SCRATCH/skill/SKILL.md`'s `description:` differs from it.

Fixture-integrity check (only if baseline read `1.0`): run `git diff
tests/e2e/skill/SKILL.md`. If it's clean (the fixture is unmodified) and baseline still
reads `1.0`, the fixture's premise — that this exact name/description scores near
`0.5` — has silently stopped holding (e.g. a `claude` CLI/model change). Report that as
the regression; do not re-tune the fixture to paper over it.

Clean up: `rm -rf "$SCRATCH"`.

### Gate 4 — flag surface (results-dir, report, disable-plugin, aliases)

Each sub-check runs independently against the read-only fixture with `--iterations 0`
(one full-set baseline evaluation each, 8 `claude -p` calls):

```bash
uv run optimize-skill-description --skill-path tests/e2e/skill \
  --eval-set tests/e2e/eval_set.json --models sonnet --repeats 1 --iterations 0 \
  --test-frac 0 --results-dir "$(mktemp -d)"
```

Pass: exit 0; the results-dir's timestamped subdirectory contains `results.json`,
`report.html`, and `logs/{baseline.json,report.json}`.

```bash
uv run optimize-skill-description --skill-path tests/e2e/skill \
  --eval-set tests/e2e/eval_set.json --models sonnet --repeats 1 --iterations 0 \
  --test-frac 0 --disable-plugin some-plugin@some-marketplace --report none
```

Pass: exit 0 (the `--settings` JSON built from `--disable-plugin` doesn't crash eval).

```bash
uv run optimize-skill-description --skill-path tests/e2e/skill \
  --eval-set tests/e2e/eval_set.json --max-iterations 1 --holdout 0.25 \
  --runs-per-query 1 --num-workers 4 --trigger-threshold 0.6 --report none --dry-run \
  > /tmp/alias.json
uv run optimize-skill-description --skill-path tests/e2e/skill \
  --eval-set tests/e2e/eval_set.json --iterations 1 --test-frac 0.25 \
  --repeats 1 --workers 4 --threshold 0.6 --report none --dry-run \
  > /tmp/canonical.json
diff /tmp/alias.json /tmp/canonical.json
```

Pass: `diff` output is empty. Any difference is a regression in the skill-creator
alias-flag `dest=` wiring in `build_parser` (`cli.py`).

### Gate 5 — invocation forms and the Python-floor guard

Live verification of the four-invocation-form requirement already stated in Packaging
invariants (above):

```bash
uv run optimize-skill-description --version
uv run python -m skill_optimizer --version
uv run python optimize_description_v2.py --version
python3.14 optimize_description_v2.py --version
```

Pass: all four exit 0 and print a version string (the last may print `0.0.0+unknown`
from an uninstalled checkout — the documented fallback, not a failure).

```bash
command -v python3.9 python3.10 python3.11 python3.12 python3.13 2>/dev/null | head -1
```

If this prints a path, run that interpreter directly against
`optimize_description_v2.py --version`. Pass: exit 1, stdout empty, stderr exactly:

```text
Requires Python >=3.14; use uv run --project /ABSOLUTE/PATH/TO/agent-skill-description-optimizer optimize-skill-description.
```

`tests/test_entrypoint.py` only monkeypatches `sys.version_info` inside the test
interpreter's own process — it never spawns a real alternate interpreter, so this is the
one check that genuinely needs a live run. If no sub-3.14 interpreter exists on the host,
mark this check **BLOCKED** (state the missing interpreter as the dependency) — do not
fabricate a pass.

### Gate 6 — packaging and install

```bash
DIST=$(mktemp -d)
uv build --no-sources -o "$DIST"
unzip -l "$DIST"/agent_skill_description_optimizer-*-py3-none-any.whl
```

Pass: build exits 0, produces exactly one `.tar.gz` and one `.whl`; the wheel satisfies
the wheel-contents requirement in Packaging invariants (above).

```bash
uvx --from "$DIST"/agent_skill_description_optimizer-*-py3-none-any.whl \
  --python '>=3.14' optimize-skill-description --version
```

Pass: exit 0, prints a version — confirms the wheel installs and its console-script
entry point resolves, not just that its file listing looks right. Clean up: `rm -rf
"$DIST"`.

### Completion

Report each of the 6 gates above as passed / failed / **BLOCKED**, in order. Do not
report this test as complete while any gate is unresolved. A failed gate is a
regression: stop, do not package or release, and name the exact gate, command, and
observed-vs-expected difference rather than folding it into a general summary.
