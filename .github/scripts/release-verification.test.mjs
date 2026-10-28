import assert from 'node:assert/strict';
import {test} from 'node:test';
import {ready} from './release-verification.mjs';
const repo='netopsengineer/agent-skill-description-optimizer',sha='a'.repeat(40);
const run={repository:{full_name:repo},head_repository:{full_name:repo},head_branch:'main',
  head_sha:sha,path:'.github/workflows/validate.yml',event:'repository_dispatch',status:'completed',
  conclusion:'success',run_number:2};
function api(main,runs){return {repository:repo,get:async path=>path==='git/ref/heads/main'?
  {object:{sha:main}}:{workflow_runs:runs}};}
test('release requires the current main SHA and genuine successful main validation',async()=>{
  assert.equal(await ready(api(sha,[run]),sha),true);
  assert.equal(await ready(api('b'.repeat(40),[run]),sha),false);
  assert.equal(await ready(api(sha,[]),sha),false);
  assert.equal(await ready(api(sha,[{...run,event:'pull_request'}]),sha),false);
  await assert.rejects(()=>ready(api(sha,[run,{...run,run_number:3,conclusion:'failure'}]),sha),/ended failure/);
  await assert.rejects(()=>ready(api(sha,null),sha),/Missing validation evidence/);
});
function clock(){let time=0;return {timeoutMs:30,intervalMs:10,now:()=>time,wait:async ms=>{time+=ms;}};}
test('same event waits for pending validation and ignores older success',async()=>{
  let calls=0;
  const a=api(sha,[]),get=a.get;
  a.get=async path=>path==='git/ref/heads/main'?get(path):{workflow_runs:
    [run,{...run,run_number:3,status:++calls<3?'in_progress':'completed'}]};
  assert.equal(await ready(a,sha,clock()),true);assert.equal(calls,3);
});
test('wait is bounded for missing or untrusted evidence',async()=>{
  for(const runs of [[],[{...run,event:'pull_request'}],[{...run,head_repository:{full_name:'fork/repo'}}]])
    await assert.rejects(()=>ready(api(sha,runs),sha,clock()),/Timed out/);
});
test('failed latest attempt and denied metadata stop immediately',async()=>{
  for(const conclusion of ['failure','cancelled','timed_out','skipped'])
    await assert.rejects(()=>ready(api(sha,[run,{...run,run_attempt:2,conclusion}]),sha,clock()),/ended/);
  let calls=0;
  await assert.rejects(()=>ready({repository:repo,get:async()=>{calls++;throw new Error('403');}},sha,clock()),/403/);
  assert.equal(calls,1);
});
test('main advance while waiting or after evidence prevents stale release',async()=>{
  for(const runs of [[],[run]]){
    let reads=0;const a=api(sha,runs),get=a.get;
    a.get=async path=>path==='git/ref/heads/main'?{object:{sha:++reads===1?sha:'b'.repeat(40)}}:get(path);
    assert.equal(await ready(a,sha,clock()),false);
  }
});
