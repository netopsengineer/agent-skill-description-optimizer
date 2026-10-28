# Dependency coordination

Dependency updates retain all existing required checks, review requirements and
protected squash merges. The queue does not approve or merge pull requests.

## Refresh one dependency branch at a time

`Dependency queue` runs trusted main code after main pushes and every half hour as missed-event recovery. One repository-wide
concurrency group serializes refreshes. It chooses the oldest open, non-draft,
same-repository Dependabot branch targeting main. It never jumps past a blocked
oldest update to churn younger branches.

Refresh requires existing auto-merge and an approval by the existing GitHub
Actions approval bot on the exact current head. A human-modified revision cannot
inherit that approval and be laundered through an App push. The script rechecks
this provenance, head, base and queue position immediately before writing.

The existing repository-scoped App Contents capability also used by bounded
auto-fix creates an ordinary two-parent merge. A disposable bare Git repository
fetches immutable commit IDs, never checks out or executes PR files, and disables
inherited configuration and hooks. Merge conflicts stop the refresh. A normal
non-force push rejects a concurrent human commit. No new token grants, repository
settings, bypass rules, security exceptions or review changes are required.

App-attributed branch updates trigger the existing PR checks and guarded approval
workflow. The queue never marks those checks passed, approves the new revision,
or uses the App to merge protected main.

## Verify merged revisions automatically

The existing auto-merge workflow and trusted queue send a bounded repository
dispatch that wakes current-main validation. Dispatch payloads are ignored; only
the trusted default-branch commit is checked out. No workflow-run cycle is used. A half-hour schedule recovers merges that happen after the event
snapshot or runner start. Event-driven/recovery checks reuse only successful
validation of the same main commit; a newer failed run invalidates older success.
PR, push and manual runs always execute the full gates. The original daily full
validation remains mandatory even when main is unchanged, preserving fresh
vulnerability checks.

Release wakes with the same dispatch and requires successful validation of the
exact current main commit before evaluating semantic-release eligibility. PR
results, another SHA, failed runs and superseded main revisions cannot authorize
release. If validation is still running, the next queue pass retries release eligibility.
Publication remains conditional on the existing semantic-release and
PyPI gates. Tooling updates do not themselves require a package release.

GitHub scheduling can be delayed; the half-hour cron is a recovery cadence, not a
latency guarantee. Conflicts, missing provenance, API failures and failed required
checks remain visible blockers rather than being bypassed.
