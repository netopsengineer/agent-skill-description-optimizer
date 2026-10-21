<!-- markdownlint-disable MD041 -->

## Summary

<!-- What does this PR change, and why? -->

## Conventional Commit title

This repo squash-merges PRs, so **the PR title becomes the release-relevant
commit message** and is linted by the `pr-title` check. Use a Conventional
Commit title, e.g.:

- `feat: add X` (minor release)
- `fix: correct Y` (patch release)
- `ci:` / `chore:` / `docs:` / `refactor:` / `test:` (no bump by themselves)

## Checklist

- [ ] The PR title is a valid Conventional Commit.
- [ ] The local gate passes: `uv run prek run --all-files` (see `AGENTS.md`).
- [ ] No changes to `src/skill_optimizer/**`, `optimize_description_v2.py`, or
      `tests/**` unless intended.

## Dependency release impact

For dependency, bootstrap or packaging updates, complete the
[release-impact check](../docs/dependency-release-impact.md):

- Changed dependency and advisory/fixed-version evidence:
- Affected surface: runtime/bundled, source build, or CI/development/bootstrap:
- Published version/tag and actual wheel/source-archive evidence compared:
- Candidate build/install evidence:
- Accumulated commits and read-only semantic-release decision:
- Release decision: no distribution needed / new distribution needed / blocked:
- If a release would be triggered by merge, approval and expected contents:

For other changes, state why this section is not applicable.
