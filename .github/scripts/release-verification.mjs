import {appendFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {GitHub} from './dependency-queue.mjs';
import {verifiedMain} from './main-verification.mjs';

export async function ready(api, sha) {
  const main = await api.get('git/ref/heads/main');
  if (main.object?.sha !== sha) return false;
  const runs = await api.get(`actions/workflows/validate.yml/runs?branch=main&head_sha=${sha}&per_page=100`);
  if (!Array.isArray(runs.workflow_runs)) throw new Error('Missing validation evidence');
  return verifiedMain(runs.workflow_runs, api.repository, sha, 'validate.yml');
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const api = new GitHub(process.env.REPOSITORY, process.env.GH_TOKEN);
  const allowed = await ready(api, process.env.SHA);
  appendFileSync(process.env.GITHUB_OUTPUT, `ready=${allowed}\n`);
  console.log(allowed ? 'Exact main commit passed validation; release eligibility may be evaluated.' :
    'Waiting for successful validation of current main; completion will wake this workflow.');
}
