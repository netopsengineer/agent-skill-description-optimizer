<!-- markdownlint-disable MD041 -->

## Summary

<!-- What does this PR change, and why? -->

## Conventional Commit title

This repo squash-merges PRs, so **the PR title becomes the release-relevant
commit message** and is linted by the `pr-title` check. Use a Conventional
Commit title, e.g.:

- `feat: add X` (minor release)
- `fix: correct Y` (patch release)
- `ci:` / `chore:` / `docs:` / `refactor:` / `test:` (no release)

## Checklist

- [ ] The PR title is a valid Conventional Commit.
- [ ] The local gate passes: `uv run prek run --all-files` (see `AGENTS.md`).
- [ ] No changes to `src/skill_optimizer/**`, `optimize_description_v2.py`, or
      `tests/**` unless intended.
