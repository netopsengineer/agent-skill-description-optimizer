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
- **Dependabot** (`.github/dependabot.yml`) opens cooldown-gated PRs daily for four
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

## Temporary dependency risk acceptance

Both required dependency scanners use the same empty-by-default policy in
[`.github/advisory-exceptions.json`](.github/advisory-exceptions.json). This is an
escape hatch for an explicitly reviewed, short-lived **no-fix** case. Prefer a
canonical dependency or lockfile update whenever a fixed release is available.
Neither Dependabot nor the mechanical auto-fixer may create or extend an acceptance.
The privileged auto-fix consumer prevents direct protected-file writes and verifies paths against the actual PR file tree
before obtaining its write token, and refuses policy, scanner, workflow, packaging,
new-file, symlink, or mode changes. Ordinary existing-file formatting and lockfile
repairs remain eligible. This guard does not prove that arbitrary proposed source
changes are mechanically generated. Both PR and weekly dependency audits run in
a separate fresh job from tests and pre-commit hooks.

Each entry must identify one exact GHSA advisory, `PyPI` package, and version
already in `uv.lock`, plus an exclusive UTC `expiresAt`, `reason`, `noFixReason`,
and a credential-free HTTPS `upstream` reference. Accepting a vulnerability is a
maintainer risk decision; a green accepted-risk result does not mean it is fixed.
The current policy contains no accepted risks.

The PR and weekly checks run `scripts/audit-dependencies.sh`:

1. Run the pinned `uv audit` once without local ignore configuration and with
   `--locked`; preserve its JSON, diagnostic output, and numeric exit status
2. Run the official, digest-pinned OSV CLI once against the repository recursively,
   with a read-only source mount, an explicitly empty config, and all-package JSON
3. Validate each original report independently against the same exact-scope policy
4. Fail on operational errors, missing/incomplete/malformed results, unknown
   findings, unrelated packages or versions, available fixes, stale or expired
   entries
5. Report every accepted risk prominently, including its expiry and upstream
   reference, and retain raw reports as the `dependency-audit-*` CI artifact

uv's adverse package statuses retain their native warning behavior and remain
visible outside the vulnerability-exception mechanism. The existing
`UV_MALWARE_CHECK=1` enforcement during environment sync remains unchanged.

There is no advisory-wide ignore flag, severity floor, second filtered scan, or
blanket `continue-on-error` gate. Unknown scanner-format changes fail closed and
require a tested adapter update. The official OSV CLI digest has a single owner in
[`.github/security-scanner/Dockerfile`](.github/security-scanner/Dockerfile), which
Dependabot maintains under the existing review and required-check policy.

The offline policy contract runs in the normal `prek` gate. To reproduce both
online scanners locally, install Docker and run:

```bash
uv sync --locked
bash scripts/audit-dependencies.sh
```

Reports are written under `${RUNNER_TEMP:-${TMPDIR:-/tmp}}/optimizer-dependency-audit`.
Use the raw reports to investigate a failure; never change a scanner exit code or
weaken a required check to make an update merge.
