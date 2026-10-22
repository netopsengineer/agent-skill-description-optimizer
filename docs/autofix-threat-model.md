# Auto-fix trust boundary

## Threat model

A pull-request job is untrusted, including its workflow, dependencies, installation
scripts, hook configuration, test code, artifacts, and claimed results. A compromised
hook could previously upload arbitrary replacement bytes under the expected artifact
name. Binding that artifact to a Validate run and restricting its file paths did not
prove its contents were mechanical fixes. The Contents-write App could then commit
those bytes and the existing bot approval flow could make them eligible for merge.

The replacement treats a failed Validate run only as a wake-up signal. It neither
produces nor downloads an auto-fix artifact. No PR-supplied patch, filename list,
configuration, executable, or dependency is accepted as an instruction to the writer.

Trusted inputs are GitHub's authenticated metadata, the immutable base commit that
contains the running workflow, its pinned Actions, and its locked formatter/tools.
Compromise of GitHub, the trusted base, runner image, or those trusted tool releases
is outside this boundary. The ordinary validation jobs still execute PR code with
read-only permissions, never with the auto-fix App credential.

## Derivation and scope

1. Check out only `github.workflow_sha`, with no persisted credentials. Disable the
   setup-uv cache and install only the base-locked development tools, without
   installing the project or reading a PR lockfile. No PR checkout or hook run occurs.
2. Read the current Validate run and workflow from GitHub. Require the exact workflow
   ID and path, completed failed `pull_request` event, same numeric repository ID,
   latest triggering attempt, and both actor IDs to be Dependabot's stable ID.
3. Resolve exactly one current PR from GitHub's commit-to-PR association, rather
   than the often-empty event PR array. Require it to be open, non-draft, unmerged,
   authored by Dependabot, in this repository, targeting the default branch, and
   still at the exact triggering SHA and `dependabot/` branch. Human pushes and
   human reruns cannot inherit the bot's write path. Stop on an auto-fix head commit.
4. Read complete Git trees and immutable blobs through the API. Only existing regular
   `100644` Python files in `src/skill_optimizer/`, `tests/`, and the entry-point shim
   may be transformed, and their eligible path sets must match the trusted base.
   Reject new/deleted eligible files, symlinks, executable modes, malformed or duplicate
   paths, partial trees, bad Git object hashes, and excessive input/output sizes.
5. Feed blob bytes only to the absolute base-installed Ruff binary over stdin. Use
   the explicit base `pyproject.toml`, safe lint fixes and formatting, no cache, a
   scrubbed environment, and a subprocess timeout. Source content is never imported,
   installed, or executed. Formatter diagnostics are captured, not emitted as
   workflow commands. Python itself runs in isolated mode.
6. Store only those independently derived replacements in a local plan. Recheck the
   run and PR before completing preparation. Empty plans or any error never reach
   the token-minting step. The plan is not a downloaded artifact.
7. Mint the existing App's Contents-write token only when fixes exist. Recheck the
   current run and PR immediately before publishing. Use GitHub's single
   `createCommitOnBranch` mutation with `expectedHeadOid`, which atomically rejects
   a moved head, including a reset to an ancestor. No force update or retry occurs.

The allowlist excludes every workflow, script, hook configuration, `CODEOWNERS`,
Dependabot configuration, security policy, packaging file, lockfile, and document.
Those controls cannot be rewritten by this formatter even if a PR supplies a forged
artifact or names them in source text. Existing required checks and review rules
remain authoritative; an auto-fix commit does not bypass them.

The branch-head comparison is atomic. PR state and run-attempt metadata are checked
immediately before the mutation but GitHub does not atomically compare those fields
with a commit. Closing a PR in that narrow interval can leave a mechanical commit on
its branch; it cannot cause a write to a different branch or head. API errors,
including GraphQL errors in an HTTP 200 response, fail closed without blind retries.
API redirects are refused, so an Authorization header is not forwarded elsewhere.

## Intentional maintenance tradeoff

Automatic lock regeneration, arbitrary pre-commit hooks, Markdown rewriting, and
PR-selected tool versions are no longer eligible. Dependabot still updates its
lockfiles and tool pins, and all ordinary checks remain enabled. Base-pinned Ruff
can repair only the rules it knows: a new formatter release's migration or a genuine
code failure may still need an explicitly reviewed maintenance PR. Using a new
PR-selected package in the writer would recreate the trust boundary we removed.

The Dependabot approval flow trusts its stable sender IDs, rechecks the current PR
before and after reviewing an exact commit, and requests auto-merge only with a
matching head. If the head changes during review it dismisses that stale approval. The auto-fix App sender remains safe only while every
writer using that identity for Dependabot branches observes this constrained model.
This design does not establish trust in arbitrary changes made by other App writers.

## Regression evidence

`tests/test_autofix_policy_guard.py` exercises provenance, object validation,
protected paths, actual formatter nonexecution, empty plans, failure paths, and
atomic expected-head writes using offline API fixtures. The normal required gate
runs these tests and enforces the repository's unchanged coverage threshold.

The implementation follows GitHub's guidance on
[privileged workflow triggers](https://docs.github.com/en/actions/reference/security/secure-use)
and uses its
[atomic commit mutation](https://docs.github.com/en/graphql/reference/commits#createcommitonbranch).

## Post-merge reconciliation

A merge performed with `GITHUB_TOKEN` does not dispatch ordinary push-triggered
workflows. Validate and Release retain their push and manual triggers and also run
daily, at 03:17 and 03:47 UTC respectively, to reconcile a missed main run. These
schedules are eventual recovery, not a guarantee of ordering or a new release gate.
Release retains its existing idempotence, permissions, artifact checks, and PyPI
publishing condition; no new credentials or repository settings are required.
