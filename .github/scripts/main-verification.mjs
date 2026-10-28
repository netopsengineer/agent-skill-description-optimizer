import { appendFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

// Only successful runs of this workflow on trusted main can deduplicate a
// scheduled/event-driven check. PR, pending, skipped and failed runs never count.
export function verifiedMain(runs, repository, sha, workflow) {
  const latest = runs.filter((run) =>
    run.repository?.full_name === repository &&
    run.head_repository?.full_name === repository &&
    run.head_branch === 'main' && run.head_sha === sha &&
    run.path === `.github/workflows/${workflow}` &&
    ['push', 'schedule', 'workflow_dispatch', 'repository_dispatch'].includes(run.event) &&
    run.status === 'completed')
    .sort((a, b) => b.run_number - a.run_number)[0];
  return latest?.conclusion === 'success';
}

export async function required({ event, schedule, repository, sha, workflow, token, fetchImpl = fetch }) {
  if (!['schedule', 'repository_dispatch'].includes(event)) return true;
  // Preserve the original daily full gate even when main has not changed.
  if (event === 'schedule' && schedule === '17 3 * * *') return true;
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]*\/[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(repository) ||
      !/^[a-f0-9]{40}$/.test(sha) || !/^[a-z-]+\.yml$/.test(workflow)) {
    throw new Error('Invalid trusted verification identity');
  }
  try {
    const url = `https://api.github.com/repos/${repository}/actions/workflows/${workflow}/runs?branch=main&head_sha=${sha}&per_page=100`;
    const response = await fetchImpl(url, {
      headers: { Accept: 'application/vnd.github+json', Authorization: `Bearer ${token}` },
      signal: AbortSignal.timeout(30000), redirect: 'error',
    });
    // Missing metadata must cause the full gate, not a false success. Do not
    // request broader token permissions or retry using another credential.
    if (!response.ok) return true;
    const body = await response.json();
    if (!Array.isArray(body.workflow_runs)) return true;
    return !verifiedMain(body.workflow_runs, repository, sha, workflow);
  } catch {
    return true;
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const run = await required({ event: process.env.EVENT, schedule: process.env.SCHEDULE, repository: process.env.REPOSITORY,
    sha: process.env.SHA, workflow: process.env.WORKFLOW, token: process.env.GH_TOKEN });
  appendFileSync(process.env.GITHUB_OUTPUT, `required=${run}\n`);
  console.log(run ? `Full verification required for ${process.env.SHA}` :
    `Successful trusted-main verification already exists for ${process.env.SHA}`);
}
