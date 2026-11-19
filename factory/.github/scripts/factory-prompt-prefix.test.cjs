const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');

const files = ['free-model-factory-worker-primitives.sh', 'free-model-factory-worker.sh',
  'nvidia-factory-worker.sh', 'omniroute-factory-worker.sh', 'factory-fork-execute.sh'];
function render(file, suffix) {
  const source = readFileSync(`.github/scripts/${file}`, 'utf8');
  const assignment = source.match(/^\s*prompt=".*"$/m)[0];
  const vars = ['WORKER', 'WORKER_ID', 'SOURCE', 'MODEL', 'RUNTIME_MODEL', 'target', 'mission',
    'REQUESTED_PR', 'EXPECTED_HEAD', 'RUNNER_TEMP', 'PROVIDER', 'CALL_SIGN'];
  const result = spawnSync('bash', ['-c', `${assignment}\nprintf '%s' "$prompt"`], {
    encoding: 'utf8', env: Object.fromEntries(vars.map(key => [key, `${key}-${suffix}`])),
  });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout;
}
for (const file of files) {
  test(`${file}: changing run identity cannot break the stable policy prefix`, () => {
    const first = render(file, 'first');
    const second = render(file, 'second');
    const boundary = first.indexOf(' Run context:');
    assert.ok(boundary > (file.startsWith('factory-fork') ? 300 : 400), `stable prefix too short: ${boundary}`);
    assert.equal(first.slice(0, boundary), second.slice(0, boundary));
    assert.notEqual(first.slice(boundary), second.slice(boundary));
    assert.match(first.slice(0, boundary), /Do not commit or push|do not post comments, commit, push/);
    assert.match(first.slice(0, boundary), /focused validation|run tests/);
    if (!file.startsWith('factory-fork')) {
      assert.match(first.slice(0, boundary), /AGENTS\.md/);
      assert.match(first.slice(0, boundary), /alter automation schedules/);
      assert.ok(first.endsWith('mission-first'));
    }
  });
}
