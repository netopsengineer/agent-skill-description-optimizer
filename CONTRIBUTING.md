# Contributing

Thanks for improving the **agent-skill-description-optimizer**. This guide is how to make a
change, verify it locally, and get it merged. Day to day the automation does the
heavy lifting - open a Conventional-Commit PR and CI handles versioning,
changelogs, releases, and dependency bumps for this single package.

## Local setup

This is a Python 3.14 / [uv](https://docs.astral.sh/uv/) project with no runtime
dependencies. Get a working tree and environment with:

```bash
uv sync
```

The pre-commit hooks run through [`prek`](https://prek.j178.dev/) (a dev dependency,
so `uv sync` installs it). Install the git hooks once with `prek install -f`.

## Commit and PR conventions

- **Conventional Commits.** PRs are squash-merged, so the **PR title becomes the
  commit message** that drives releases. Use `feat:` (minor), `fix:` (patch), or a
  non-releasing type (`ci:`, `chore:`, `docs:`, `refactor:`, `test:`). The `pr-title`
  check enforces this.
- **Keep the tool code stable.** `src/skill_optimizer/**`, `optimize_description_v2.py`,
  and `tests/**` are the finished tool; change them only with intent.

## Verifying locally

The canonical validation gate lives in [`AGENTS.md`](AGENTS.md) (§Validation gates) and
is the single source of truth. CI does not maintain a separate command list - it runs the
exact same `.pre-commit-config.yaml` suite. Run it locally with one command:

```bash
uv run prek run --all-files
```

That covers lint, format, type-check, tests, secret scanning, and workflow security. See
`AGENTS.md` for the individual commands and the packaging invariants.

## CI/CD and the release process

CI is defined entirely under [`.github/`](.github/). Dependency maintenance and releases
are autonomous; human-authored PRs retain a one-approval review gate. The pipeline rests
on four automation behaviors:

- **Validate** (`.github/workflows/validate.yml`) owns the three required check contexts:
  `gate`, `deps-security`, and `pr-title`. On every PR and every push to `main` it runs the
  full `prek` gate, a dependency-advisory scan (`uv audit` + OSV-Scanner), and the
  Conventional-Commit PR-title lint.
- **Release** (`.github/workflows/release.yml`) runs on merges to `main`. It uses
  [python-semantic-release](https://python-semantic-release.readthedocs.io/) to read the
  Conventional-Commit history, bump the static `project.version`, update
  [`CHANGELOG.md`](CHANGELOG.md), tag, and cut a GitHub Release. Publishing to PyPI (OIDC
  trusted publishing with PEP 740 attestations) is gated behind the `PUBLISH_TO_PYPI` repo
  variable; while it is off, the release path still builds and validates the artifact.
- **Dependabot** (`.github/dependabot.yml`) opens cooldown-gated PRs daily for three
  ecosystems: grouped `uv` dev tools, grouped `pre-commit` hook revisions, and grouped
  `github-actions` pins. `uv-build` stays in its own PR because Dependabot cannot refresh
  its build-system range reliably inside a group.
- **Dependabot auto-merge** (`.github/workflows/dependabot-auto-merge.yml`) supplies the
  required approval only for a trusted Dependabot revision, then squash-merges it once
  Validate is green. If Validate fails on a bot PR with a mechanically fixable problem,
  **auto-fix** (`.github/workflows/auto-fix.yml`) refreshes the lockfile, applies every
  autofix hook, and pushes the result back so the checks re-run - no LLM, purely
  mechanical.

The configuration mirrors the local gate exactly, so "green locally" and "green in CI" mean
the same thing. For the agent-facing execution contract, see `AGENTS.md`.
