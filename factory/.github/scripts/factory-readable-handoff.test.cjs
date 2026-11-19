const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const { mkdtempSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');

// The functions under test silence gh output, so stubs record to a file.
function traceFile() {
  return join(mkdtempSync(join(tmpdir(), 'factory-handoff-')), 'trace.log');
}

const source = readFileSync('.github/scripts/free-model-factory-worker-primitives.sh', 'utf8');

function extract(name) {
  const start = source.indexOf(`${name}() {`);
  assert.notEqual(start, -1, `${name} missing`);
  return source.slice(start, source.indexOf('\n}\n', start) + 3);
}

const helper = extract('post_readable_handoff');
const SECRET_KEYS = [
  'FACTORY_WORKER',
  'FACTORY_WORKER_48_APP_ID',
  'FACTORY_WORKER_48_INSTALLATION_ID',
  'FACTORY_WORKER_48_APP_PRIVATE_KEY',
  'FACTORY_WORKER_APP_MINT_STUB_TOKEN',
  'FACTORY_WORKER_APP_MAPPING',
  'TRUSTED_WORKER_APP_HELPER',
  'TRUSTED_WORKER_APP_MAPPING',
  'GH_TOKEN',
];

function run(extraEnv = {}, { xtrace = false } = {}) {
  const env = { ...process.env, ...extraEnv };
  for (const key of SECRET_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(extraEnv, key)) delete env[key];
  }
  env.WORKER = env.FACTORY_WORKER || '11';
  env.DISPLAY = 'Mark Cordova';
  env.MODEL = 'test-model';
  env.PR_REBASE_TOKEN = 'shared-token';
  env.GH_TOKEN = 'workflow-token';
  env.TRACE = traceFile();
  const result = spawnSync('bash', ['-c', `
    set -Eeuo pipefail
    log() { printf 'log:%s\\n' "$*" >&2; }
    gh() {
      if [[ "$1" == issue && "$2" == comment ]]; then
        printf 'comment-token:%s\\n' "\${GH_TOKEN}" >> "$TRACE"
        printf 'comment-number:%s\\n' "$3" >> "$TRACE"
        if [[ "$4" == --body-file ]]; then
          printf 'comment-body:%s\\n' "$(tr '\\n' '|' < "$5")" >> "$TRACE"
        fi
        return 0
      fi
      printf 'unexpected-gh:%s\\n' "$*" >&2
      exit 9
    }
    ${helper}
    ${xtrace ? 'set -x' : ''}
    post_readable_handoff implementation 3142 abcdef1234567890 'Opened from issue #3135.'
    # Ambient token for markers must still be the workflow token.
    printf 'ambient:%s\\n' "\$GH_TOKEN" >&2
    # Simulate a trusted marker comment path: never use the App token.
    gh issue comment 3142 --body '<!-- comic-pile-factory-claim-released-v3:pr-3142:worker:1:reason -->'
  `], { encoding: 'utf8', env });
  let trace = '';
  try { trace = readFileSync(env.TRACE, 'utf8'); } catch { trace = ''; }
  return { status: result.status, stderr: `${result.stderr}${trace}`, log: result.stderr };
}

test('worker 48 with all secrets posts readable handoff with App token', () => {
  const result = run({
    FACTORY_WORKER: '48',
    FACTORY_WORKER_48_APP_ID: '123',
    FACTORY_WORKER_48_INSTALLATION_ID: '456',
    FACTORY_WORKER_48_APP_PRIVATE_KEY: 'TEST-ONLY-NOT-A-KEY',
    FACTORY_WORKER_APP_MINT_STUB_TOKEN: 'install-token',
  });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stderr, /comment-token:install-token/);
  assert.match(result.log, /^::add-mask::install-token$/m);
  assert.match(result.stderr, /comment-number:3142/);
  assert.match(result.stderr, /Factory handoff · implementation/);
  assert.doesNotMatch(result.stderr, /comment-body:.*<!--/);
  // Marker comment uses ambient workflow token, never the App token.
  assert.match(result.stderr, /comment-token:workflow-token/);
  assert.match(result.stderr, /ambient:workflow-token/);
});

test('worker 48 missing any secret posts no readable handoff', () => {
  const result = run({ FACTORY_WORKER: '48' });
  assert.equal(result.status, 0, result.stderr);
  assert.doesNotMatch(result.stderr, /::add-mask::/);
  assert.doesNotMatch(result.stderr, /comment-token:install-token/);
  assert.doesNotMatch(result.stderr, /Factory handoff/);
  assert.match(result.stderr, /comment-token:workflow-token/);
});

test('other workers never post readable App handoffs', () => {
  for (const worker of ['11', '46', '72']) {
    const result = run({
      FACTORY_WORKER: worker,
      FACTORY_WORKER_48_APP_ID: '123',
      FACTORY_WORKER_48_INSTALLATION_ID: '456',
      FACTORY_WORKER_48_APP_PRIVATE_KEY: 'TEST-ONLY-NOT-A-KEY',
      FACTORY_WORKER_APP_MINT_STUB_TOKEN: 'install-token',
    });
    assert.equal(result.status, 0, `${worker} ${result.stderr}`);
    assert.doesNotMatch(result.stderr, /::add-mask::/);
    assert.doesNotMatch(result.stderr, /comment-token:install-token/);
    assert.doesNotMatch(result.stderr, /Factory handoff/);
    assert.match(result.stderr, /comment-token:workflow-token/);
  }
});

test('worker 48 handoff masks the installation token and never echoes it under set -x', () => {
  const result = run({
    FACTORY_WORKER: '48',
    FACTORY_WORKER_48_APP_ID: '123',
    FACTORY_WORKER_48_INSTALLATION_ID: '456',
    FACTORY_WORKER_48_APP_PRIVATE_KEY: 'TEST-ONLY-NOT-A-KEY',
    FACTORY_WORKER_APP_MINT_STUB_TOKEN: 'install-token',
  }, { xtrace: true });
  assert.equal(result.status, 0, result.stderr);
  const lines = result.log.split('\n').filter((line) => line.includes('install-token'));
  assert.deepEqual(lines, ['::add-mask::install-token'], result.log);
  // The caller's xtrace is restored after the handoff returns.
  assert.match(result.log, /^\+ printf 'ambient:%s\\n' workflow-token$/m);
});

const markerFunctions = ['release_target', 'record_pr_provenance', 'claim_issue']
  .map(extract)
  .join('\n');

test('trusted marker comments never use the worker App token, even for configured worker 48', () => {
  const env = { ...process.env };
  for (const key of SECRET_KEYS) delete env[key];
  Object.assign(env, {
    FACTORY_WORKER: '48',
    FACTORY_WORKER_48_APP_ID: '123',
    FACTORY_WORKER_48_INSTALLATION_ID: '456',
    FACTORY_WORKER_48_APP_PRIVATE_KEY: 'TEST-ONLY-NOT-A-KEY',
    FACTORY_WORKER_APP_MINT_STUB_TOKEN: 'install-token',
    GH_TOKEN: 'workflow-token',
    GITHUB_REPOSITORY: 'JoshCLWren/comic-pile',
    TRACE: traceFile(),
  });
  const result = spawnSync('bash', ['-c', `
    set -Eeuo pipefail
    WORKER=48 WORKER_ID=opencode-free-model-factory-48 OWNER=factory:48
    OWNER_RE='^factory:(unowned|local|[1-9][0-9]*)$'
    STAGE_RE='^factory:(building|review|changes-requested|ci|ready|blocked)$'
    log() { :; }
    current_stage() { printf '%s\\n' "$2"; }
    replace_labels() { :; }
    issue_has_open_factory_pr() { return 1; }
    pr_no_diff_generation_fields() { printf ':sha=abc:stage=factory:review:conflicted=0'; }
    git() { printf 'abcdef1234567890\\n'; }
    gh() {
      case "$1 $2" in
        "issue comment")
          printf 'marker-token:%s\\n' "\${GH_TOKEN}" >> "$TRACE"
          return 0 ;;
        "api repos/JoshCLWren/comic-pile/issues/3135/labels?per_page=100")
          printf '["factory","factory:48"]' ; return 0 ;;
        "api repos/JoshCLWren/comic-pile/issues/3136/labels?per_page=100")
          printf '[]' ; return 0 ;;
        "api --method") return 0 ;;
      esac
      printf 'unexpected-gh:%s\\n' "$*" >&2
      return 0
    }
    ${markerFunctions}
    release_target 3142 factory:review pr-opened-handoff pr
    release_target 3135 factory:building no-persisted-change-handoff issue
    record_pr_provenance 3142 3135
    claim_issue 3136
  `], { encoding: 'utf8', env });
  assert.equal(result.status, 0, result.stderr);
  const trace = readFileSync(env.TRACE, 'utf8');
  const tokens = [...trace.matchAll(/marker-token:(\S*)/g)].map((m) => m[1]);
  assert.ok(tokens.length >= 4, `${result.stderr}${trace}`);
  for (const token of tokens) assert.equal(token, 'workflow-token');
  assert.doesNotMatch(`${result.stderr}${trace}`, /install-token/);
});

test('only post_readable_handoff overrides GH_TOKEN with the worker App token', () => {
  const worker = readFileSync('.github/scripts/free-model-factory-worker.sh', 'utf8');
  const handoffBody = extract('post_readable_handoff');
  const stripped = source.replace(handoffBody, '');
  assert.doesNotMatch(stripped, /GH_TOKEN="\$token"/);
  assert.doesNotMatch(worker, /GH_TOKEN="\$token"/);
  for (const line of worker.split('\n')) {
    if (line.includes('post_readable_handoff ')) {
      assert.doesNotMatch(line, /<!--|comic-pile-factory-/, line);
    }
  }
  assert.match(worker, /post_readable_handoff implementation /);
  assert.match(worker, /post_readable_handoff repair /);
  assert.match(worker, /post_readable_handoff review /);
});
