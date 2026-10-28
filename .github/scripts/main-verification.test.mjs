import assert from 'node:assert/strict';
import { test } from 'node:test';
import { required, verifiedMain } from './main-verification.mjs';
const repository = 'netopsengineer/agents-md-compiler';
const sha = 'a'.repeat(40);
const workflow = 'validate.yml';
const good = { repository: { full_name: repository }, head_repository: { full_name: repository },
  head_branch: 'main', head_sha: sha, path: `.github/workflows/${workflow}`,
  run_number: 1, event: 'push', status: 'completed', conclusion: 'success' };

test('only exact trusted successful main evidence is reusable', () => {
  assert.equal(verifiedMain([good], repository, sha, workflow), true);
  for (const patch of [{ head_sha: 'b'.repeat(40) }, { head_branch: 'dependabot/test' },
    { event: 'pull_request' }, { conclusion: 'failure' }, { conclusion: 'skipped' },
    { status: 'in_progress' }, { path: '.github/workflows/other.yml' },
    { repository: { full_name: 'attacker/repo' } }, { head_repository: null }]) {
    assert.equal(verifiedMain([{ ...good, ...patch }], repository, sha, workflow), false);
  }
});
test('PR, push and explicit manual checks never skip', async () => {
  for (const event of ['pull_request', 'push', 'workflow_dispatch']) {
    assert.equal(await required({ event, fetchImpl: () => { throw Error('must not query'); } }), true);
  }
});
test('late merges, missing evidence and failures retain full verification', async () => {
  const options = { event: 'repository_dispatch', repository, sha, workflow, token: 'test' };
  for (const response of [{ ok: false }, { ok: true, json: async () => ({}) },
    { ok: true, json: async () => ({ workflow_runs: [] }) }]) {
    assert.equal(await required({ ...options, fetchImpl: async () => response }), true);
  }
  assert.equal(await required({ ...options, fetchImpl: async () => { throw Error('network'); } }), true);
  const fetchImpl = async () => ({ ok: true, json: async () => ({ workflow_runs: [good] }) });
  assert.equal(await required({ ...options, fetchImpl }), false);
  assert.equal(await required({ ...options, sha: 'b'.repeat(40), fetchImpl }), true);
  await assert.rejects(required({ ...options, repository: '../bad' }));
});

test('daily full validation and newer failures cannot be hidden by old success', async () => {
  assert.equal(await required({ event: 'schedule', schedule: '17 3 * * *' }), true);
  assert.equal(verifiedMain([good, { ...good, run_number: 2, conclusion: 'failure' }], repository, sha, workflow), false);
});
