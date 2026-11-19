const assert = require('node:assert/strict');
const { readFileSync, mkdtempSync, rmSync, writeFileSync } = require('node:fs');
const { join } = require('node:path');
const { tmpdir } = require('node:os');
const { spawnSync } = require('node:child_process');
const { test } = require('node:test');
const source = readFileSync('.github/scripts/free-model-factory-worker-primitives.sh', 'utf8');
const definition = name => source.match(new RegExp(`^${name}\\(\\) \\{\\n.*?^\\}`, 'ms'))[0];
function select({ files = [[{ filename: 'shared.tsx' }]], issue = '4719', fail = false } = {}) {
  const result = spawnSync('bash', ['-c', `set -Eeuo pipefail
    ${definition('find_overlapping_factory_pr')}
    git() { printf 'shared.tsx\\0'; }
    linked_issue_from_pr() { printf '%s' "$TEST_ISSUE"; }
    gh() {
      if [[ "$1" == pr ]]; then printf '%s' '[{"number":4727,"headRefName":"factory/54-4719-z-ai"}]';
      elif [[ "$FAIL_FILES" == true ]]; then return 1;
      else printf '%s' "$FILES_JSON"; fi
    }
    GITHUB_REPOSITORY=JoshCLWren/comic-pile
    find_overlapping_factory_pr 4719 factory/97-4719-z-ai
  `], { encoding: 'utf8', env: { ...process.env, FILES_JSON: JSON.stringify(files),
    TEST_ISSUE: issue, FAIL_FILES: String(fail) } });
  return result;
}
test('same issue plus overlapping file selects the existing PR', () => {
  const result = select(); assert.equal(result.status, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).number, 4727);
});
test('disjoint files and other issues do not suppress new work', () => {
  for (const settings of [{ files: [[{ filename: 'other.tsx' }]] }, { issue: '9999' }]) {
    const result = select(settings); assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), {});
  }
});
test('all paginated files and prior rename paths participate', () => {
  const result = select({ files: [[{ filename: 'other.tsx' }],
    [{ filename: 'renamed.tsx', previous_filename: 'shared.tsx' }]] });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(JSON.parse(result.stdout).number, 4727);
});
test('file API failure blocks persistence instead of assuming no overlap', () => {
  assert.equal(select({ fail: true }).status, 2);
  assert.equal(select({ files: 'malformed payload' }).status, 2);
});
test('prepared edits are applied to the actual existing PR before persistence', () => {
  const dir = mkdtempSync(join(tmpdir(), 'factory-overlap-'));
  function git(...args) {
    const result = spawnSync('git', args, { cwd: dir, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr); return result.stdout.trim();
  }
  try {
    git('init', '-q'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid');
    writeFileSync(join(dir, 'shared.txt'), 'base\n'); git('add', '.'); git('commit', '-qm', 'base');
    git('branch', 'existing'); git('switch', '-qc', 'new-attempt');
    writeFileSync(join(dir, 'shared.txt'), 'prepared change\n');
    const result = spawnSync('bash', ['-c', `set -Eeuo pipefail
      ${definition('redirect_issue_to_overlapping_pr')}
      find_overlapping_factory_pr() { printf '%s' '{"number":4727,"headRefName":"existing"}'; }
      current_owner_is_self() { return 1; }
      claim_unowned_pr() { return 0; }
      checkout_target() { git reset --hard HEAD >/dev/null; git switch "$3"; EXPECTED_HEAD="$(git rev-parse HEAD)"; }
      replace_labels() { :; }
      log() { printf '%s\\n' "$*" >&2; }
      run_agent() { return 99; }
      MODE=issue NUMBER=4719 BRANCH=new-attempt OWNER=factory:97
      redirect_issue_to_overlapping_pr 4719
      printf '%s %s %s' "$MODE" "$NUMBER" "$BRANCH"
    `], { cwd: dir, encoding: 'utf8', env: { ...process.env, RUNNER_TEMP: dir } });
    assert.equal(result.status, 0, result.stderr);
    assert.equal(result.stdout, 'pr 4727 existing');
    assert.equal(git('branch', '--show-current'), 'existing');
    assert.equal(readFileSync(join(dir, 'shared.txt'), 'utf8'), 'prepared change\n');
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
test('a live PR owner prevents checkout and duplicate creation', () => {
  const result = spawnSync('bash', ['-c', `set -Eeuo pipefail
    ${definition('redirect_issue_to_overlapping_pr')}
    git() { [[ "$1" == status ]] && printf changed; return 0; }
    find_overlapping_factory_pr() { printf '%s' '{"number":4727,"headRefName":"existing"}'; }
    current_owner_is_self() { return 1; }; claim_unowned_pr() { return 1; }
    checkout_target() { echo forbidden-checkout; return 99; }; log() { :; }
    BRANCH=new-attempt
    redirect_issue_to_overlapping_pr 4719
  `], { encoding: 'utf8' });
  assert.equal(result.status, 2);
  assert.doesNotMatch(result.stdout, /forbidden/);
});
test('redirection runs before lifecycle handoff and persistence checks again before commit', () => {
  const wrapper = readFileSync('.github/scripts/free-model-factory-worker.sh', 'utf8');
  assert.ok(wrapper.indexOf('redirect_issue_to_overlapping_pr "$NUMBER"') < wrapper.indexOf('pr_lifecycle='));
  const persistence = definition('_persist_issue_pr_untraced');
  assert.ok(persistence.indexOf('find_overlapping_factory_pr') < persistence.indexOf('git commit'));
});
