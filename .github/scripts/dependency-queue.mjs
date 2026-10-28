/** Trusted control-plane code. PR objects are data, never checked out or executed. */
import {execFileSync} from 'node:child_process';
import {mkdtempSync, readFileSync, rmSync, writeFileSync, appendFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

export const BOT = 49699333;
export const REVIEWER = 41898282; // github-actions[bot], existing approval workflow.
const SHA = /^[0-9a-f]{40}$/;
const REPO = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
export function requireValue(value, message) {
  if (!value) throw new Error(message);
}
export function eligible(pr, repository, base) {
  return pr.state === 'open' && !pr.draft && !pr.merged && pr.user?.id === BOT &&
    pr.head?.repo?.full_name === repository && pr.base?.repo?.full_name === repository &&
    pr.base.ref === base && /^dependabot\/[A-Za-z0-9_./-]+$/.test(pr.head.ref) &&
    !pr.head.ref.includes('..') && !pr.head.ref.endsWith('/') &&
    SHA.test(pr.head.sha) && Number.isSafeInteger(pr.number) && pr.number > 0;
}
export function approved(reviews, sha) {
  // Only the latest approval state of the existing approval bot counts. A human
  // edit changes the head and cannot inherit this provenance via our App push.
  const latest = reviews.filter(r => r.user?.id === REVIEWER &&
    ['APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'].includes(r.state))
    .sort((a, b) => b.id - a.id)[0];
  return latest?.state === 'APPROVED' && latest.commit_id === sha;
}
export class GitHub {
  constructor(repository, token) {
    requireValue(REPO.test(repository) && !repository.split('/').some(p => ['.', '..'].includes(p)), 'Bad repository');
    this.repository = repository;
    this.token = token;
  }
  async get(path) {
    const response = await fetch(`https://api.github.com/repos/${this.repository}/${path}`, {
      redirect: 'error', headers: {Authorization: `Bearer ${this.token}`,
        Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'},
      signal: AbortSignal.timeout(30000),
    });
    requireValue(response.ok, `GitHub read failed: ${response.status}`);
    return response.json();
  }
  async pages(path) {
    const values = [];
    for (let page = 1; page <= 20; page++) {
      const result = await this.get(`${path}${path.includes('?') ? '&' : '?'}per_page=100&page=${page}`);
      requireValue(Array.isArray(result), 'Expected array');
      values.push(...result);
      if (result.length < 100) return values;
    }
    throw new Error('Refusing truncated queue');
  }
}
export async function plan(api, base) {
  requireValue(/^[A-Za-z0-9_./-]+$/.test(base) && !base.includes('..'), 'Bad base');
  const prs = (await api.pages('pulls?state=open&sort=created&direction=asc'))
    .filter(pr => eligible(pr, api.repository, base));
  // Never jump past a blocked oldest dependency update and churn newer branches.
  if (!prs.length) return null;
  const pr = await api.get(`pulls/${prs[0].number}`);
  if (!eligible(pr, api.repository, base) || !pr.auto_merge) return null;
  if (!approved(await api.pages(`pulls/${pr.number}/reviews`), pr.head.sha)) return null;
  const main = await api.get(`git/ref/heads/${base}`);
  requireValue(SHA.test(main.object?.sha), 'Bad main SHA');
  const comparison = await api.get(`compare/${pr.head.sha}...${main.object.sha}`);
  if (!['ahead', 'diverged'].includes(comparison.status) || comparison.ahead_by < 1) return null;
  return {repository: api.repository, base, number: pr.number, branch: pr.head.ref,
    head: pr.head.sha, main: main.object.sha};
}
export function samePlan(left, right) {
  return left && right && ['repository', 'base', 'number', 'branch', 'head', 'main']
    .every(key => left[key] === right[key]);
}
export function gitRunner(directory, token = '') {
  // Do not inherit system/global config, hooks, credentials, attributes drivers,
  // alternates, SSH overrides, or other Git environment from the caller.
  const env = {PATH: process.env.PATH, HOME: directory, GIT_CONFIG_NOSYSTEM: '1',
    GIT_CONFIG_GLOBAL: '/dev/null', GIT_TERMINAL_PROMPT: '0',
    GIT_AUTHOR_NAME: 'dependency-queue', GIT_AUTHOR_EMAIL: 'dependency-queue@localhost',
    GIT_COMMITTER_NAME: 'dependency-queue', GIT_COMMITTER_EMAIL: 'dependency-queue@localhost'};
  if (token) {
    env.GIT_CONFIG_COUNT = '1';
    env.GIT_CONFIG_KEY_0 = 'http.https://github.com/.extraheader';
    env.GIT_CONFIG_VALUE_0 = `AUTHORIZATION: basic ${Buffer.from(`x-access-token:${token}`).toString('base64')}`;
  }
  return (...args) => execFileSync('git', ['-c', 'core.hooksPath=/dev/null', '-c',
    'protocol.file.allow=never', '-c', 'protocol.ext.allow=never', ...args],
  {cwd: directory, env, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'], timeout: 120000}).trim();
}
export async function refresh(api, wanted, token, createGit = gitRunner) {
  requireValue(samePlan(wanted, await plan(api, wanted.base)), 'Queue changed before refresh');
  const directory = mkdtempSync(join(tmpdir(), 'dependency-queue-'));
  try {
    const git = createGit(directory);
    git('init', '--bare', '.');
    const origin = `https://github.com/${api.repository}.git`;
    git('fetch', '--no-tags', '--no-recurse-submodules', origin, wanted.head, wanted.main);
    // No worktree or PR hooks. The merge is constructed only from immutable SHAs.
    // Conflicts fail closed; no automatic conflict resolution or force push.
    const tree = git('merge-tree', '--write-tree', wanted.head, wanted.main);
    requireValue(SHA.test(tree), 'Merge did not produce one clean tree');
    const commit = git('commit-tree', tree, '-p', wanted.head, '-p', wanted.main,
      '-m', 'ci: merge main into queued dependency update');
    requireValue(SHA.test(commit), 'Bad merge commit');
    requireValue(samePlan(wanted, await plan(api, wanted.base)), 'Queue changed before push');
    // An intervening human commit is not an ancestor of this fixed two-parent
    // commit, so normal fast-forward push rejects it atomically. Never --force.
    createGit(directory, token)('push', '--porcelain', origin, `${commit}:refs/heads/${wanted.branch}`);
    const after = await api.get(`pulls/${wanted.number}`);
    requireValue(eligible(after, api.repository, wanted.base) && after.head.sha === commit,
      'Refreshed head changed; require a fresh queue pass');
    return commit;
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}
export async function main(mode) {
  const api = new GitHub(process.env.GITHUB_REPOSITORY, process.env.GH_READ_TOKEN);
  const path = join(process.env.RUNNER_TEMP, 'dependency-queue-plan.json');
  if (mode === 'plan') {
    const value = await plan(api, process.env.DEFAULT_BRANCH);
    writeFileSync(path, JSON.stringify(value));
    appendFileSync(process.env.GITHUB_OUTPUT, `refresh=${value !== null}\n`);
  } else {
    requireValue(mode === 'apply', 'Unknown mode');
    const value = JSON.parse(readFileSync(path, 'utf8'));
    requireValue(value !== null && process.env.GH_WRITE_TOKEN, 'Missing refresh plan or token');
    const commit = await refresh(api, value, process.env.GH_WRITE_TOKEN);
    console.log(`Refreshed dependency PR #${value.number} to ${commit}; required checks must run again.`);
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv[2]).catch(error => {console.error(error.message); process.exitCode = 1;});
}
