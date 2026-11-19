import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync } from 'node:child_process';
import { runReview, permissionReply, chooseReviewModel, eligibleReviewModel, acpErrorReason } from
  '../.agents/skills/crouton-factory-codex/scripts/local-factory-acp-review.mjs';

function fixture(t, behavior = '') {
  const cwd = mkdtempSync(join(tmpdir(), 'factory-review-test-'));
  t.after(() => rmSync(cwd, { recursive: true, force: true }));
  const git = (...args) => execFileSync('git', args, { cwd, encoding: 'utf8' }).trim();
  git('init', '-q');
  git('config', 'user.email', 'test@example.invalid');
  git('config', 'user.name', 'Protocol test');
  // Isolate test fixtures from user/repository hooks; no delivery hook is bypassed.
  git('config', 'core.hooksPath', join(cwd, 'unused-hooks'));
  writeFileSync(join(cwd, 'source.txt'), 'reviewed source\n');
  git('add', 'source.txt');
  git('commit', '-qm', 'Test fixture');
  const fake = join(cwd, 'fake-acp.mjs');
  writeFileSync(fake, `
import { createInterface } from 'node:readline';
import { writeFileSync } from 'node:fs';
const behavior = ${JSON.stringify(behavior)};
const reply = (id, result) => console.log(JSON.stringify({jsonrpc:'2.0',id,result}));
const failRpc = (id, error) => console.log(JSON.stringify({jsonrpc:'2.0',id,error}));
const config = model => ({configOptions:[{id:'model',currentValue:model,options:[{value:'opencode/live-review-free'}]}]});
const permission = JSON.parse(process.env.OPENCODE_CONFIG_CONTENT).permission;
if (permission.edit !== 'deny' || permission.bash !== 'deny') process.exit(9);
createInterface({input:process.stdin}).on('line', line => {
 const m = JSON.parse(line);
 if(m.method==='initialize') reply(m.id,{protocolVersion:1});
 if(m.method==='session/new') reply(m.id,{sessionId:'actual-session',...config('other')});
 if(m.method==='session/set_config_option') reply(m.id,config(behavior==='wrong-model'?'other':m.params.value));
 if(m.method==='session/prompt') {
   if(behavior==='prompt-error') return failRpc(m.id,{code:-32603,message:'Free usage limit reached',data:{service:'session',errorName:'provider.free-limit'}});
   if(behavior==='exit') process.exit(2);
   if(behavior==='timeout') return;
   if(behavior==='mutate') writeFileSync('source.txt','changed');
   console.log(JSON.stringify({method:'session/update',params:{sessionId:'actual-session',update:{sessionUpdate:'agent_message_chunk',content:{type:'text',text:behavior==='empty'?'Incomplete review':'Findings checked.\\nFACTORY_GATE_READY'}}}}));
   reply(m.id,{stopReason:behavior==='cancelled'?'cancelled':'end_turn'});
 }
});`);
  git('add', 'fake-acp.mjs');
  git('commit', '-qm', 'Add protocol peer');
  return { cwd, head: git('rev-parse', 'HEAD'), base: 'HEAD', output: join(cwd, '.git', 'proof'),
    model: 'opencode/live-review-free', issue: '1', pr: '2', reviewer: 'independent-test',
    context: 'Synthetic protocol evidence only; not an actual model review.',
    command: [process.execPath, fake], timeoutMs: 2000 };
}

test('completed protocol preserves real session/model/response evidence', async t => {
  const options = fixture(t);
  assert.equal(await runReview(options), 'FACTORY_GATE_READY');
  const status = JSON.parse(readFileSync(join(options.output, 'status.json')));
  assert.equal(status.session, 'actual-session');
  assert.equal(status.model, 'opencode/live-review-free');
  assert.equal(status.head, options.head);
  assert.equal(status.state, 'complete');
  assert.match(readFileSync(join(options.output, 'report.md'), 'utf8'), /FACTORY_GATE_READY$/);
});

for (const behavior of ['wrong-model', 'empty', 'cancelled', 'mutate', 'exit', 'timeout']) {
  test(`fails closed for ${behavior}`, async t => {
    const options = fixture(t, behavior);
    if (behavior === 'timeout') options.timeoutMs = 200;
    await assert.rejects(runReview(options));
    assert.equal(JSON.parse(readFileSync(join(options.output, 'status.json'))).state, 'blocked');
  });
}

test('surfaces the structured ACP error cause in the blocked status', async t => {
  const options = fixture(t, 'prompt-error');
  await assert.rejects(runReview(options), /provider\.free-limit/);
  const status = JSON.parse(readFileSync(join(options.output, 'status.json')));
  assert.equal(status.state, 'blocked');
  assert.equal(status.reason, 'ACP session/prompt failed (code -32603 provider.free-limit)');
});

test('acpErrorReason stays bounded when a peer omits structured error data', () => {
  assert.equal(acpErrorReason('session/prompt', {}), 'ACP session/prompt failed (unknown code)');
  assert.equal(acpErrorReason('initialize', { code: 42 }),
    'ACP initialize failed (code 42)');
});

test('rejects permission requests and unsupported client methods', () => {
  assert.deepEqual(permissionReply({ id: 9, method: 'session/request_permission',
    params: { options: [{kind:'allow_once',optionId:'yes'},{kind:'reject_once',optionId:'no'}] } }),
  {jsonrpc:'2.0',id:9,result:{outcome:{outcome:'selected',optionId:'no'}}});
  assert.equal(permissionReply({id:1,method:'fs/write_text_file'}).error.code, -32601);
  assert.equal(permissionReply({id:2,method:'session/request_permission',params:{options:[]}})
    .error.code, -32601);
});

const advertised = (currentValue, values) => ({configOptions:[{id:'model',currentValue,
  options:values.map(value => ({value}))}]});
test('live free selection survives retired defaults and ignores paid models', () => {
  const session = advertised('paid/provider', ['paid/provider','opencode/new-free','openrouter/new:free']);
  assert.equal(chooseReviewModel(session), 'opencode/new-free');
  assert.equal(chooseReviewModel(advertised('openrouter/new:free', ['openrouter/new:free','opencode/new-free'])), 'openrouter/new:free');
  assert.throws(() => chooseReviewModel(session, 'retired/model'), /not advertised/);
  assert.equal(chooseReviewModel(session, 'paid/provider'), 'paid/provider');
  assert.throws(() => chooseReviewModel(advertised('paid/provider', ['paid/provider'])), /no policy-eligible/);
  assert.throws(() => chooseReviewModel({}), /no policy-eligible/);
  assert.equal(eligibleReviewModel('untrusted/name-free'), false);
});
test('automatic protocol selection records actual live model without override', async t => {
  const options = fixture(t); delete options.model;
  assert.equal(await runReview(options), 'FACTORY_GATE_READY');
  const choice = JSON.parse(readFileSync(join(options.output, 'model-choice.json')));
  assert.equal(choice.model, 'opencode/live-review-free');
  assert.equal(choice.explicit, false);
});
