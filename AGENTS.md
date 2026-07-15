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
sourcery review src/skill_optimizer optimize_description_v2.py tests
```

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
