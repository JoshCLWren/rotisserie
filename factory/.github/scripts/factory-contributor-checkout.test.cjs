const assert = require('node:assert/strict');
const { readFileSync, mkdtempSync, rmSync, writeFileSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { execFileSync, spawnSync } = require('node:child_process');
const { test } = require('node:test');
const source = readFileSync('.github/scripts/free-model-factory-worker-primitives.sh', 'utf8');
const checkout = source.match(/^checkout_target\(\) \{\n.*?^\}/ms)[0];

for (const collision of [false, true]) {
  test(`credentialed checkout rejects fork with base branch collision=${collision}`, () => {
    const dir = mkdtempSync(join(tmpdir(), 'factory-fork-'));
    const git = (...args) => execFileSync('git', args, { cwd: dir, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
    try {
      git('init', '-b', 'main');
      git('config', 'user.name', 'Contributor');
      git('config', 'user.email', 'contributor@example.com');
      writeFileSync(join(dir, 'file'), 'main');
      git('add', '.'); git('commit', '-m', 'Main');
      const main = git('rev-parse', 'HEAD');
      git('clone', '--bare', '.', join(dir, 'origin.git'));
      writeFileSync(join(dir, '.git/info/exclude'), 'origin.git\n');
      git('remote', 'add', 'origin', join(dir, 'origin.git'));
      git('fetch', 'origin');
      writeFileSync(join(dir, 'file'), 'contributor');
      git('add', 'file'); git('commit', '-m', 'Contributor fix');
      const head = git('rev-parse', 'HEAD');
      git('push', 'origin', 'HEAD:refs/pull/3897/head');
      if (collision) git('push', 'origin', `${main}:refs/heads/fix/issue-3893`);
      git('reset', '--hard', main);
      const pr = { head: { sha: head, repo: { full_name: 'contributor/comic-pile' } },
        base: { repo: { full_name: 'JoshCLWren/comic-pile' } } };
      const result = spawnSync('bash', ['-c', `set -Eeuo pipefail
        ${checkout}
        gh() { printf '%s' "$TEST_PR"; }
        log() { :; }
        checkout_target pr 3897 fix/issue-3893
      `], { cwd: dir, encoding: 'utf8', env: { ...process.env,
        GITHUB_REPOSITORY: 'JoshCLWren/comic-pile', TEST_PR: JSON.stringify(pr), TEST_HEAD: head } });
      assert.notEqual(result.status, 0);
      assert.equal(git('rev-parse', 'HEAD'), main);
      assert.equal(readFileSync(join(dir, 'file'), 'utf8'), 'main');
    } finally { rmSync(dir, { recursive: true, force: true }); }
  });
}

test('unavailable PR head fails instead of silently reviewing main', () => {
  const result = spawnSync('bash', ['-c', `set -Eeuo pipefail
    ${checkout}
    git() { :; }
    gh() { return 1; }
    log() { :; }
    checkout_target pr 3897 fix/issue-3893
  `], { encoding: 'utf8', env: { ...process.env, GITHUB_REPOSITORY: 'JoshCLWren/comic-pile' } });
  assert.notEqual(result.status, 0);
});

const push = source.match(/^factory_git_push\(\) \{\n.*?^\}/ms)[0];
test('fork repair pushes to fork and restores the base remote without leaking its token', () => {
  const result = spawnSync('bash', ['-c', `set -Eeuo pipefail
    ${push}
    FACTORY_PUSH_REPOSITORY=contributor/comic-pile
    GITHUB_REPOSITORY=JoshCLWren/comic-pile
    current_url=https://github.com/JoshCLWren/comic-pile.git
    git() {
      case "$1 $2" in
        'remote get-url') printf '%s' "$current_url" ;;
        'remote set-url') current_url="$4" ;;
        'push origin')
          test "$current_url" = https://x-access-token:test-token@github.com/contributor/comic-pile.git
          test "$3" = HEAD:fix/issue-3893 ;;
        *) exit 9 ;;
      esac
    }
    factory_git_push pat test-token origin HEAD:fix/issue-3893
    test "$current_url" = https://github.com/JoshCLWren/comic-pile.git
  `], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  assert.doesNotMatch(result.stdout + result.stderr, /test-token/);
});

const worker = readFileSync('.github/scripts/free-model-factory-worker.sh', 'utf8');
const snapshot = worker.match(/^review_snapshot_failure_reason\(\) \{\n.*?^\}/ms)[0];
for (const [observed, expected, owner, reason] of [
  ['head', 'head', true, ''],
  ['', 'head', true, 'semantic-review-head-read-failed'],
  ['new', 'head', true, 'semantic-review-head-changed'],
  ['head', 'head', false, 'semantic-review-lease-lost'],
]) {
  test(`review snapshot distinguishes ${reason || 'unchanged head'}`, () => {
    const result = spawnSync('bash', ['-c', `${snapshot}
      current_owner_is_self() { ${owner ? 'return 0' : 'return 1'}; }
      review_snapshot_failure_reason "$1" "$2" 3897
    `, 'test', observed, expected], { encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, reason);
  });
}

const generation = source.match(/^pr_no_diff_generation_fields\(\) \{\n.*?^\}/ms)[0];
for (const conflicted of [false, true]) {
  test(`pre-session failure pins the live head and conflict=${conflicted}`, () => {
    const result = spawnSync('bash', ['-c', `${generation}
      git() { printf '%s' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa; }
      gh() { printf '%s' "$TEST_META"; }
      pr_no_diff_generation_fields 3897 factory:review 1
    `], { encoding: 'utf8', env: { ...process.env, TEST_META: JSON.stringify({
      headRefOid: 'b'.repeat(40), mergeable: conflicted ? 'CONFLICTING' : 'MERGEABLE',
    }) } });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, `:sha=${'b'.repeat(40)}:stage=factory:review:conflicted=${Number(conflicted)}`);
  });
}
