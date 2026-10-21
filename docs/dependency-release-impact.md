# Dependency release-impact check

A green dependency scan on `main` is not proof that published artifacts contain a
change. Classify every dependency PR before merge and record its release decision
in the PR. The pull-request template provides the checklist. Do not change release
permissions, publish flags, or security exceptions to make a check pass.

## What needs a release?

- **Runtime or bundled dependency:** inspect the published wheel's `METADATA`
  (`Requires-Dist`) and archive contents. If affected code or a dependency
  requirement ships to users, a new distribution is needed to deliver the fix.
- **Build backend or source-install requirement:** inspect the published source
  archive's `pyproject.toml`. A changed build-system range reaches source-install
  users only in a new source distribution. This is distinct from a vulnerability
  in the installed runtime.
- **CI, development, or release bootstrap only:** a workflow/lock update protects
  future development and build runs after merge. A new runtime release is not
  needed solely to change the maintainer's executable. Confirm that it is not a
  runtime dependency or bundled into the wheel.
- **Mixed change:** document each surface separately. Do not infer release impact
  from a package name, dependency group, or the PR title alone.

## Evidence to record before merge

1. Record the exact PR head SHA, current `main` SHA, latest release tag and its
   commit, and the published version/files being compared. Compare all commits
   since that release, including their bodies and breaking-change markers.
2. Record exact old/new dependency versions, advisory/fixed-version evidence, and
   the lockfile inventory used by the unsuppressed scanners. For uv, verify the
   installed executable against the lock with
   `uv run --locked python -m scripts.bootstrap_uv --installed-version "$(uv --version)"`.
3. Inspect the actual published wheel and source archive. Record runtime
   requirements, build requirements, and any bundled changed code. If published
   files are unavailable, mark this comparison blocked rather than infer parity
   from repository metadata.
4. Build a candidate with `uv build --no-sources`, inspect its wheel `METADATA` and
   source `pyproject.toml`, and exercise the documented install/entry-point checks.
   A local build is verification, not publication. Follow `AGENTS.md`'s live-CLI
   release gates before approving a release.
5. Check the pinned python-semantic-release version's **read-only**
   `semantic-release version --print` result against the proposed history. Also
   inspect the release workflow's triggers and publish condition. `ci:` and
   `chore:` do not themselves request a bump, but an earlier unreleased `fix:` or
   `feat:` can cause this merge to release accumulated changes.
6. State one decision: **no distribution needed**, **new distribution needed**, or
   **blocked pending evidence/approval**. State what a release would contain and
   whether the existing main-push workflow will create one. If publication needs
   approval, obtain it before merging a change that would trigger that publication.
7. After an approved merge, verify checks on the exact main SHA and inspect the
   Release run. If a release was approved, verify its tag/commit, published file
   hashes and installed version. A tag or green validation alone does not prove
   that PyPI accepted the expected wheel and source archive.

## Bootstrap remediation: 2026-10-21

[GHSA-hq85-5cpj-mr62](https://github.com/astral-sh/uv/security/advisories/GHSA-hq85-5cpj-mr62)
affects uv versions before 0.13.4. The repository's hardcoded 0.11.29 executable
was outside `uv.lock`, so lock-only vulnerability scans did not cover it.

The fix places `uv==0.13.4` in the non-default `bootstrap` dependency group and its
exact PyPI package in `uv.lock`. Every setup-uv step reads that lockfile; the
semantic-release hook reads and validates the same requirement. The audit verifies
the running executable before both scanners, and their existing full-inventory
checks now include uv. Missing pins, drift, incomplete scans and scanner errors
fail closed. The advisory-exception policy remains unchanged and empty.

This repairs build/CI tooling, not a bundled runtime dependency. The tool has no
third-party runtime requirements. Separately, the build backend range on main was
widened from `uv_build>=0.11.28,<0.13` in release v0.2.6 to
`uv_build>=0.11.28,<0.14`; source-install users need a later source distribution to
receive that compatibility change. A new runtime vulnerability was not established
by that range change. Record the final release decision and artifact evidence in
the remediation PR rather than assuming this documentation publishes anything.
