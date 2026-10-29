import assert from 'node:assert/strict';
import {test} from 'node:test';
import {mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {BOT, REVIEWER, GitHub, approved, eligible, gitRunner, plan, refresh, verifyRefresh, samePlan} from './dependency-queue.mjs';

const repo = 'netopsengineer/agent-skill-description-optimizer';
const a = 'a'.repeat(40), b = 'b'.repeat(40), c = 'c'.repeat(40), d = 'd'.repeat(40);
function pr() {return {number: 1, state: 'open', draft: false, merged: false,
  user: {id: BOT}, head: {ref: 'dependabot/uv/tools', sha: a, repo: {full_name: repo}},
  base: {ref: 'main', repo: {full_name: repo}}, auto_merge: {}};}
function fixture() {
  const current = pr();
  const reviews = [{id: 1, user: {id: REVIEWER}, state: 'APPROVED', commit_id: a}];
  const api = {repository: repo, async pages(path) {return path.startsWith('pulls?') ? [current] : reviews;},
    async get(path) {
      if (path === 'pulls/1') return current;
      if (path === 'git/ref/heads/main') return {object: {sha: b}};
      if (path === `compare/${a}...${b}`) return {status: 'diverged', ahead_by: 1};
      throw new Error(`Unexpected ${path}`);
    }};
  return {api, current, reviews};
}
const wanted = {repository: repo, base: 'main', number: 1, branch: 'dependabot/uv/tools', head: a, main: b};

test('only same-repository open Dependabot branches on main are candidates', () => {
  assert.equal(eligible(pr(), repo, 'main'), true);
  for (const change of [p=>p.state='closed',p=>p.draft=true,p=>p.merged=true,
    p=>p.user.id=42,p=>p.head.repo.full_name='attacker/fork',p=>p.base.repo.full_name='attacker/fork',
    p=>p.base.ref='release',p=>p.head.ref='main',p=>p.head.ref='dependabot/../main',
    p=>p.head.ref='dependabot/x/',p=>p.head.sha='bad',p=>p.number=-1]) {
    const value=pr();change(value);assert.equal(eligible(value,repo,'main'),false);
  }
});
test('review provenance cannot launder a human revision or stale/dismissed approval', () => {
  const review={id:1,user:{id:REVIEWER},state:'APPROVED',commit_id:a};
  assert.equal(approved([review],a),true);
  assert.equal(approved([review],b),false);
  assert.equal(approved([{...review,user:{id:42}}],a),false);
  for (const state of ['CHANGES_REQUESTED','DISMISSED']) {
    assert.equal(approved([review,{...review,id:2,state}],a),false);
  }
  assert.equal(approved([{...review,state:'COMMENTED'}],a),false);
});
test('plan selects only oldest queued approved stale head, never jumps blocked oldest',async()=>{
  const {api,current,reviews}=fixture();
  assert.deepEqual(await plan(api,'main'),wanted);
  current.auto_merge=null;assert.equal(await plan(api,'main'),null);
  current.auto_merge={};reviews[0].commit_id=b;assert.equal(await plan(api,'main'),null);
  reviews[0].commit_id=a;const get=api.get;
  api.get=async path=>path.startsWith('compare/')?{status:'identical',ahead_by:0}:get(path);
  assert.equal(await plan(api,'main'),null);
  api.pages=async()=>[];assert.equal(await plan(api,'main'),null);
  await assert.rejects(()=>plan(api,'../main'),/Bad base/);
});
test('invalid main SHA and changed PR fail before writes',async()=>{
  const {api,current}=fixture();const get=api.get;
  api.get=async path=>path.startsWith('git/ref/')?{object:{sha:'bad'}}:get(path);
  await assert.rejects(()=>plan(api,'main'),/Bad main SHA/);
  current.head.repo.full_name='attacker/fork';assert.equal(await plan(api,'main'),null);
});
test('refresh creates two-parent merge and only non-force verified-branch push',async()=>{
  const {api,current}=fixture();const calls=[];
  const git=(_directory,token)=> (...args)=>{
    calls.push({token,args});
    if(args[0]==='merge-tree')return c;
    if(args[0]==='commit-tree')return d;
    if(args[0]==='push')current.head.sha=d;
    return '';
  };
  assert.equal(await refresh(api,wanted,'secret',git),d);
  assert.deepEqual(calls.find(x=>x.args[0]==='commit-tree').args,
    ['commit-tree',c,'-p',a,'-p',b,'-m','ci: merge main into queued dependency update']);
  const push=calls.find(x=>x.args[0]==='push');
  assert.deepEqual(push.args,['push','--porcelain',`https://github.com/${repo}.git`,`${d}:refs/heads/dependabot/uv/tools`]);
  assert.equal(push.token,'secret');assert.equal(calls.filter(x=>x.token).length,1);
});
test('changed plan, merge conflicts, and racing humans never force a refresh',async()=>{
  for(const mode of ['before','conflict','during','push','after']){
    const {api,current}=fixture();const calls=[];
    if(mode==='before')current.head.sha=b;
    const git=()=> (...args)=>{
      calls.push(args);
      if(args[0]==='merge-tree'){
        if(mode==='conflict')throw new Error('merge conflict');
        return c;
      }
      if(args[0]==='commit-tree'){
        if(mode==='during')current.head.sha=b;
        return d;
      }
      if(args[0]==='push'){
        if(mode==='push')throw new Error('non-fast-forward');
        current.head.sha=b;
      }
      return '';
    };
    await assert.rejects(()=>refresh(api,wanted,'secret',git));
    assert.equal(calls.some(args=>args.includes('--force')),false);
    if(['before','conflict','during'].includes(mode))assert.equal(calls.some(args=>args[0]==='push'),false);
  }
});
test('real isolated Git creates clean merge without worktree and rejects a concurrent human head',()=>{
  const dir=mkdtempSync(join(tmpdir(),'queue-test-'));
  try{
    const git=gitRunner(dir);git('init','--bare','.');
    const tree=git('mktree');
    const base=git('commit-tree',tree,'-m','base');
    const dep=git('commit-tree',tree,'-p',base,'-m','dependency');
    const main=git('commit-tree',tree,'-p',base,'-m','main');
    const mergedTree=git('merge-tree','--write-tree',dep,main);
    const merged=git('commit-tree',mergedTree,'-p',dep,'-p',main,'-m','merge');
    git('merge-base','--is-ancestor',dep,merged);
    git('merge-base','--is-ancestor',main,merged);
    const human=git('commit-tree',tree,'-p',dep,'-m','human');
    assert.throws(()=>git('merge-base','--is-ancestor',human,merged));
    assert.equal(git('rev-parse','--is-bare-repository'),'true');
  }finally{rmSync(dir,{recursive:true,force:true});}
});
test('plan comparison binds every target field',()=>{
  assert.equal(Boolean(samePlan(wanted,wanted)),true);
  for(const key of Object.keys(wanted))assert.equal(Boolean(samePlan(wanted,{...wanted,[key]:'changed'})),false);
  assert.equal(Boolean(samePlan(wanted,null)),false);
  assert.throws(()=>new GitHub('../repo','token'),/Bad repository/);
});
test('GitHub reads fail closed and paginate without truncating candidates',async()=>{
  const original=globalThis.fetch;
  try{
    const api=new GitHub(repo,'token');let seen;
    globalThis.fetch=async(url,options)=>{seen={url,options};return {ok:true,json:async()=>({ok:true})};};
    assert.deepEqual(await api.get('pulls/1'),{ok:true});
    assert.equal(seen.url,`https://api.github.com/repos/${repo}/pulls/1`);
    assert.equal(seen.options.redirect,'error');
    globalThis.fetch=async()=>({ok:false,status:403});
    await assert.rejects(()=>api.get('pulls/1'),/403/);
    api.get=async path=>path.endsWith('page=1')?Array(100).fill({}):[{}];
    assert.equal((await api.pages('pulls?state=open')).length,101);
    api.get=async()=>[];assert.deepEqual(await api.pages('pulls/1/reviews'),[]);
    api.get=async()=>({});await assert.rejects(()=>api.pages('pulls'),/Expected array/);
    api.get=async()=>Array(100).fill({});await assert.rejects(()=>api.pages('pulls'),/truncated/);
  }finally{globalThis.fetch=original;}
});

test('post-push verification tolerates only bounded original-head projection lag', async () => {
  const {api, current} = fixture(); let waits = 0;
  assert.equal(await verifyRefresh(api, wanted, d, async ms => {
    assert.equal(ms, 2000); waits++; current.head.sha = d;
  }), d);
  assert.equal(waits, 1);
  current.head.sha = a; waits = 0;
  await assert.rejects(() => verifyRefresh(api, wanted, d, async () => {waits++;}), /bounded verification/);
  assert.equal(waits, 5);
});
test('post-push verification fails immediately for races, identity changes, and API errors', async () => {
  for (const mode of ['third', 'closed', 'branch', 'number', 'main', 'denied']) {
    const {api, current} = fixture(); const get = api.get; let waits = 0;
    if (mode === 'third') current.head.sha = c;
    if (mode === 'closed') current.state = 'closed';
    if (mode === 'branch') current.head.ref = 'dependabot/other';
    if (mode === 'number') current.number = 2;
    if (mode === 'main') api.get = async path => path.startsWith('git/ref/') ? {object: {sha: c}} : get(path);
    if (mode === 'denied') api.get = async () => {throw new Error('GitHub read failed: 403');};
    await assert.rejects(() => verifyRefresh(api, wanted, d, async () => {waits++;}));
    assert.equal(waits, 0);
  }
});
test('post-push verification rejects changes arising during a projection wait', async () => {
  for (const mode of ['third', 'main']) {
    const {api, current} = fixture(); const get = api.get; let waits = 0;
    await assert.rejects(() => verifyRefresh(api, wanted, d, async () => {
      waits++;
      if (mode === 'third') current.head.sha = c;
      else api.get = async path => path.startsWith('git/ref/') ? {object: {sha: c}} : get(path);
    }), mode === 'third' ? /head changed/ : /Main changed/);
    assert.equal(waits, 1);
  }
});
