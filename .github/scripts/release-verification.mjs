import {appendFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {setTimeout as sleep} from 'node:timers/promises';
import {GitHub} from './dependency-queue.mjs';

// Missing/pending evidence may wait; terminal failures and API errors fail closed.
// Never substitute another SHA, credential, or PR run for this main event.
export async function ready(api, sha, {timeoutMs=0, intervalMs=15000,
  now=Date.now, wait=sleep}={}) {
  const deadline=now()+timeoutMs;
  for (;;) {
    const main=await api.get('git/ref/heads/main');
    if (!main.object?.sha) throw new Error('Missing main identity');
    if (main.object.sha!==sha) return false;
    const body=await api.get(`actions/workflows/validate.yml/runs?branch=main&head_sha=${sha}&per_page=100`);
    if (!Array.isArray(body.workflow_runs)) throw new Error('Missing validation evidence');
    const latest=body.workflow_runs.filter(run=>
      run.repository?.full_name===api.repository && run.head_repository?.full_name===api.repository &&
      run.head_branch==='main' && run.head_sha===sha && run.path==='.github/workflows/validate.yml' &&
      ['push','schedule','workflow_dispatch','repository_dispatch'].includes(run.event))
      .sort((a,b)=>b.run_number-a.run_number || (b.run_attempt??1)-(a.run_attempt??1))[0];
    if (latest?.status==='completed') {
      if (latest.conclusion!=='success') throw new Error(`Main validation ended ${latest.conclusion}`);
      // Close the main-advance race while reading workflow evidence.
      return (await api.get('git/ref/heads/main')).object?.sha===sha;
    }
    if (now()>=deadline) {
      if (timeoutMs===0) return false;
      throw new Error('Timed out waiting for exact-main validation; periodic reconciliation will retry');
    }
    await wait(Math.min(intervalMs,deadline-now()));
  }
}
if (process.argv[1] && import.meta.url===pathToFileURL(process.argv[1]).href) {
  const api=new GitHub(process.env.REPOSITORY,process.env.GH_TOKEN);
  const allowed=await ready(api,process.env.SHA,{timeoutMs:600000});
  appendFileSync(process.env.GITHUB_OUTPUT,`ready=${allowed}\n`);
  console.log(allowed ? 'Exact main commit passed validation; release eligibility may be evaluated.' :
    'Main advanced; this event will not release a stale commit.');
}
