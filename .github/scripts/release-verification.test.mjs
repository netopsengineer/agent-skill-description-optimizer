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
  assert.equal(await ready(api(sha,[run,{...run,run_number:3,conclusion:'failure'}]),sha),false);
  await assert.rejects(()=>ready(api(sha,null),sha),/Missing validation evidence/);
});
