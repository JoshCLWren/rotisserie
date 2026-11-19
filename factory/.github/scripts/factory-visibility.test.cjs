const assert = require('node:assert/strict');
const { mkdtempSync, writeFileSync, copyFileSync, rmSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const test = require('node:test');

const reconcile = require('./factory-visibility.cjs');

function loadVisibilityFixture({ expected = [], retired = [] } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'factory-visibility-'));
  try {
    const lockPath = join(dir, 'factory-expected-workers.json');
    const rosterPath = join(dir, 'free-model-factories.tsv');
    writeFileSync(lockPath, JSON.stringify({
      schema_version: 1,
      expected_workers: expected,
      retired_workers: retired,
    }));
    writeFileSync(rosterPath, expected.map((id) => `${id}\tmodel\tworker`).join('\n') + (expected.length ? '\n' : ''));
    writeFileSync(join(dir, 'factory-expected-workers.cjs'), `'use strict';
const fs = require('node:fs');
const lock = JSON.parse(fs.readFileSync(${JSON.stringify(lockPath)}, 'utf8'));
const SCHEDULED_WORKER_IDS = [1, 2, 3, 4, 5];
function loadExpectedWorkerIds() {
  return {
    expectedWorkers: new Set(lock.expected_workers.map(Number)),
    retiredWorkers: new Set(lock.retired_workers.map(Number)),
    rosterWorkers: new Set(lock.expected_workers.map(Number)),
    scheduledWorkerIds: new Set(SCHEDULED_WORKER_IDS),
  };
}
module.exports = {
  SCHEDULED_WORKER_IDS,
  defaultLockPath: () => ${JSON.stringify(lockPath)},
  defaultRosterPath: () => ${JSON.stringify(rosterPath)},
  loadExpectedWorkerIds,
  isExpectedWorkerId: (id) => new Set(lock.expected_workers.map(Number)).has(Number(id)),
  readRosterWorkerIds: () => new Set(lock.expected_workers.map(Number)),
  readLock: () => ({ expected: new Set(lock.expected_workers.map(Number)), retired: new Set(lock.retired_workers.map(Number)), schemaVersion: 1 }),
};
`);
    copyFileSync('.github/scripts/factory-visibility.cjs', join(dir, 'factory-visibility.cjs'));
    return require(join(dir, 'factory-visibility.cjs'));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}
const {
  ownerFor,
  reconcileLabels,
  withRetry,
  trusted,
  performedViaUntrustedApp,
  TRUSTED_APP_SLUGS,
  restCall,
  restRequestLog,
  restRequestCounts,
  clearRestRequestLog,
  currentLabels,
  STAGE_LABELS,
  DERIVED_LABELS,
  MERGE_CONFLICT_LABEL,
  mergeConflictState,
  derivedLabelsFor,
  EXPECTED_WORKER_IDS,
} = reconcile._test;

function contextFor(eventName, payload) {
  return {
    eventName,
    payload,
    repo: { owner: 'JoshCLWren', repo: 'comic-pile' },
  };
}

function githubFor({
  labels = [],
  comments = [],
  setLabels,
  updateLabel,
  createLabel,
  commentIssueNumbers,
  extraDefinitions = [],
} = {}) {
  const workerDefinitions = Object.fromEntries(
    Array.from({ length: 16 }, (_, index) => [
      `factory:${index + 1}`,
      ['0366D6', `Current next-action owner is ComicPile Factory ${index + 1}`],
    ]),
  );
  const definitions = [
    ...Object.entries({
      factory: ['5319E7', 'Work owned or produced by an autonomous ComicPile factory'],
      ...workerDefinitions,
      'factory:local': ['0366D6', 'Current next-action owner is the local OpenCode factory'],
      'factory:unowned': ['BFDADC', 'Factory work has no current next-action owner'],
      'factory:building': ['FBCA04', 'A factory is actively implementing or repairing this work'],
      'factory:review': ['D4C5F9', 'The exact current head needs review or re-review'],
      'factory:changes-requested': ['D73A4A', 'Actionable review findings currently block progress'],
      'factory:ci': ['1D76DB', 'Review passed and required exact-head checks are being verified'],
      'factory:ready': ['0E8A16', 'All exact-head factory merge gates are satisfied'],
      'factory:blocked': ['B60205', 'A genuine human, credential, or external blocker remains'],
    }).map(([name, [color, description]]) => ({ name, color, description })),
    ...extraDefinitions,
  ];
  const api = {
    listLabelsForRepo: async () => ({ data: definitions, headers: {}, status: 200 }),
    listLabelsOnIssue: async () => ({ data: labels.map(name => ({ name })), headers: {}, status: 200 }),
    listComments: async params => {
      if (commentIssueNumbers) commentIssueNumbers.push(params.issue_number);
      return { data: comments, headers: {}, status: 200 };
    },
    createLabel: async params => {
      if (createLabel) createLabel(params);
      return { data: {}, headers: {}, status: 201 };
    },
    updateLabel: async params => {
      if (updateLabel) updateLabel(params);
      return { data: {}, headers: {}, status: 200 };
    },
    setLabels: async params => {
      if (setLabels) setLabels(params);
      return { data: {}, headers: {}, status: 200 };
    },
  };
  return {
    // Reads and writes now go through direct REST requests (each page and every
    // retry attempt is recorded), so the doubles return REST response objects.
    graphql: async () => ({
      repository: { pullRequest: { reviewThreads: { nodes: [], pageInfo: { hasNextPage: false, endCursor: null } } } },
    }),
    rest: {
      issues: api,
      pulls: {
        listReviews: async () => ({ data: [], headers: {}, status: 200 }),
        get: async ({ pull_number }) => ({ data: { number: pull_number, state: 'open', body: '', head: { sha: 'a'.repeat(40), ref: 'factory/1-1-x', repo: { full_name: 'JoshCLWren/comic-pile' } } } }),
      },
      checks: { listForRef: async () => ({ data: { check_runs: [] } }) },
    },
  };
}

test('canonical local worker tokens map to the local owner', () => {
  assert.equal(ownerFor('local'), 'factory:local');
  assert.equal(ownerFor('local-opencode'), 'factory:local');
  assert.equal(ownerFor('local-opencode-debian'), 'factory:local');
});

test('fixed-model worker tokens map from the roster, not a numeric ceiling', () => {
  const { ownerFor } = loadVisibilityFixture({
    expected: [11, 46, 51, 75, 101],
    retired: [6, 32, 49, 56, 71, 72, 73, 74, 76, 77, 78, 93, 95],
  })._test;
  assert.equal(ownerFor('opencode-free-model-factory-46'), 'factory:46');
  assert.equal(ownerFor('opencode-free-model-factory-51'), 'factory:51');
  assert.equal(ownerFor('opencode-free-model-factory-74'), 'factory:unowned');
  assert.equal(ownerFor('opencode-free-model-factory-75'), 'factory:75');
  assert.equal(ownerFor('opencode-free-model-factory-95'), 'factory:unowned');
  // Active above-old-ceiling worker comes from the hermetic roster fixture.
  assert.equal(ownerFor('opencode-free-model-factory-101'), 'factory:101');
  assert.equal(ownerFor('opencode-free-model-factory-56'), 'factory:unowned');
  assert.equal(ownerFor('opencode-free-model-factory-78'), 'factory:unowned');
  assert.equal(ownerFor('opencode-omniroute-factory-95'), 'factory:unowned');
  assert.equal(ownerFor('opencode-nvidia-factory-11'), 'factory:11');
  // Off-roster / retired ids stay unowned even when historically in 6..71.
  assert.equal(ownerFor('opencode-nvidia-factory-6'), 'factory:unowned');
  assert.equal(ownerFor('opencode-free-model-factory-32'), 'factory:unowned');
  assert.equal(ownerFor('opencode-free-model-factory-49'), 'factory:unowned');
  assert.equal(ownerFor('opencode-free-model-factory-71'), 'factory:unowned');
  assert.equal(ownerFor('opencode-free-model-factory-72'), 'factory:unowned');
  assert.equal(ownerFor('opencode-free-model-factory-73'), 'factory:unowned');
  assert.equal(ownerFor('opencode-free-model-factory-76'), 'factory:unowned');
  // Retired roster worker stays unowned even when named by a stale marker.
  assert.equal(ownerFor('opencode-free-model-factory-77'), 'factory:unowned');
});

test('head-contributor markers resolve roster workers above the old ceiling', () => {
  const { workerFrom, ownerFor } = loadVisibilityFixture({
    expected: [101],
    retired: [77, 93, 72],
  })._test;
  const body = '<!-- comic-pile-factory-head-contributor-v1:pr-7:head-' + 'a'.repeat(40) + ':worker-101:epoch-2 -->';
  assert.equal(workerFrom(body), '101');
  assert.equal(ownerFor(workerFrom(body)), 'factory:101');
  assert.equal(ownerFor('worker-93'), 'factory:unowned');
  assert.equal(ownerFor('72'), 'factory:unowned');
  assert.equal(ownerFor('77'), 'factory:unowned');
});

test('legacy model-specific label metadata is normalized', async () => {
  const updates = [];
  const dynamicWorker = String([...EXPECTED_WORKER_IDS][0]);
  const github = githubFor({
    extraDefinitions: [{
      name: `factory:${dynamicWorker}`,
      color: '5319e7',
      description: `Fixed-model Factory ${dynamicWorker}: legacy model-specific description`,
    }],
    updateLabel: async input => updates.push(input),
  });

  await reconcile({ github, context: contextFor('workflow_dispatch', {}) });

  const update = updates.find(candidate => candidate.name === `factory:${dynamicWorker}`);
  assert.ok(update);
  assert.equal(update.description, `Current next-action owner is ComicPile Factory ${dynamicWorker}`);
});

test('label reconciliation replaces both groups with one atomic call', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['bug', 'factory', 'factory:building', 'factory:unowned'],
    setLabels: async input => calls.push(input),
  });
  await reconcileLabels(github, contextFor('workflow_dispatch', {}), 12, {
    owner: 'factory:local',
    stage: 'factory:review',
  });
  assert.equal(calls.length, 1);
  assert.deepEqual(
    new Set(calls[0].labels),
    new Set(['bug', 'factory', 'factory:local', 'factory:review']),
  );
});

test('unchanged label reconciliation avoids a write regardless of label order', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory:review', 'bug', 'factory:local', 'factory'],
    setLabels: async input => calls.push(input),
  });
  await reconcileLabels(github, contextFor('workflow_dispatch', {}), 12, {
    owner: 'factory:local',
    stage: 'factory:review',
  });
  assert.deepEqual(calls, []);
});

test('merge-conflict is derived observability, never a stage or blocker', () => {
  assert.equal(MERGE_CONFLICT_LABEL, 'factory:merge-conflict');
  assert.deepEqual([...DERIVED_LABELS], [MERGE_CONFLICT_LABEL]);
  assert.ok(!STAGE_LABELS.includes(MERGE_CONFLICT_LABEL));
  assert.ok(!reconcile._test.BLOCKED_LABELS.has(MERGE_CONFLICT_LABEL));
  // The label name must never satisfy the owner predicate, so no reconciliation
  // can mistake it for a next-action lease.
  assert.equal(ownerFor('merge-conflict'), 'factory:unowned');
  assert.equal(reconcile._test.isOwnerLabel(MERGE_CONFLICT_LABEL), false);
});

test('mergeConflictState reads only GitHub mergeability', () => {
  // REST shape: boolean mergeable plus mergeable_state.
  assert.equal(mergeConflictState({ mergeable: false, mergeable_state: 'dirty' }), 'conflict');
  assert.equal(mergeConflictState({ mergeable: true, mergeable_state: 'clean' }), 'clean');
  assert.equal(mergeConflictState({ mergeable: true, mergeable_state: 'behind' }), 'clean');
  assert.equal(mergeConflictState({ mergeable: true, mergeable_state: 'blocked' }), 'clean');
  assert.equal(mergeConflictState({ mergeable: true, mergeable_state: 'unstable' }), 'clean');
  // `gh`/GraphQL shape: mergeable string plus mergeStateStatus.
  assert.equal(mergeConflictState({ mergeable: 'CONFLICTING', mergeStateStatus: 'DIRTY' }), 'conflict');
  assert.equal(mergeConflictState({ mergeable: 'MERGEABLE', mergeStateStatus: 'CLEAN' }), 'clean');
  assert.equal(mergeConflictState({ mergeable: 'UNKNOWN', mergeStateStatus: 'DIRTY' }), 'conflict');
  // Unresolved mergeability is never guessed.
  assert.equal(mergeConflictState({ mergeable: null, mergeable_state: 'unknown' }), 'unknown');
  assert.equal(mergeConflictState({ mergeable: 'UNKNOWN', mergeStateStatus: 'UNKNOWN' }), 'unknown');
  assert.equal(mergeConflictState({}), 'unknown');
});

test('derived labels are added and cleared in the same atomic label write', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['bug', 'factory', 'factory:review', 'factory:unowned'],
    setLabels: async input => calls.push(input),
  });
  await reconcileLabels(github, contextFor('workflow_dispatch', {}), 12, {
    owner: 'factory:unowned',
    stage: 'factory:review',
    derivedLabels: [MERGE_CONFLICT_LABEL],
  });
  assert.equal(calls.length, 1);
  assert.deepEqual(
    new Set(calls[0].labels),
    new Set(['bug', 'factory', 'factory:review', 'factory:unowned', MERGE_CONFLICT_LABEL]),
  );
});

test('resolved mergeability clears the derived label without touching stages', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:review', 'factory:47', MERGE_CONFLICT_LABEL, 'infrastructure'],
    setLabels: async input => calls.push(input),
  });
  await reconcileLabels(github, contextFor('workflow_dispatch', {}), 12, {
    owner: 'factory:47',
    stage: 'factory:review',
    derivedLabels: [],
  });
  assert.equal(calls.length, 1);
  assert.deepEqual(
    new Set(calls[0].labels),
    new Set(['factory', 'factory:review', 'factory:47', 'infrastructure']),
  );
});

test('unresolved mergeability preserves the derived label instead of guessing', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:review', 'factory:unowned', MERGE_CONFLICT_LABEL],
    setLabels: async input => calls.push(input),
  });
  // Review/comment routing never reads live mergeability, so it must inherit it.
  await reconcileLabels(github, contextFor('workflow_dispatch', {}), 12, {
    owner: 'factory:unowned',
    stage: 'factory:review',
    derivedLabels: derivedLabelsFor({}),
  });
  assert.deepEqual(calls, []);
  await reconcileLabels(github, contextFor('workflow_dispatch', {}), 12, {
    owner: 'factory:unowned',
    stage: 'factory:review',
  });
  assert.deepEqual(calls, []);
});

test('merge-conflict label definition is created for repository labels', async () => {
  const created = [];
  const github = githubFor({ createLabel: async input => created.push(input) });
  await reconcile({ github, context: contextFor('workflow_dispatch', {}) });
  const definition = created.find(label => label.name === MERGE_CONFLICT_LABEL);
  assert.ok(definition, 'derived label must exist before the reconciler applies it');
  assert.match(definition.description, /merge-conflicted/);
});

test('transient GitHub failures are retried', async () => {
  let attempts = 0;
  const result = await withRetry(async () => {
    attempts += 1;
    if (attempts === 1) throw Object.assign(new Error('temporary failure'), { status: 502 });
    return 'ok';
  }, { delay: 0 });
  assert.equal(result, 'ok');
  assert.equal(attempts, 2);
});

test('primary installation quota exhaustion does not hot-loop before reset', async () => {
  const { rateLimitDelay } = reconcile._test;
  const error = Object.assign(new Error('API rate limit exceeded for installation'), {
    status: 403,
    response: { headers: { 'x-ratelimit-remaining': '0', 'x-ratelimit-reset': String(Math.ceil(Date.now() / 1000) + 3600) } },
  });
  assert.ok(rateLimitDelay(error) > 30000);
  let attempts = 0;
  await assert.rejects(withRetry(async () => {
    attempts += 1;
    throw error;
  }, { maxRateLimitWait: 30000, sleep: () => { throw Error('must not sleep'); } }), /rate limit/);
  assert.equal(attempts, 1);
});

test('429 Retry-After is honored for a bounded retry', async () => {
  let attempts = 0;
  const sleeps = [];
  const result = await withRetry(async () => {
    attempts += 1;
    if (attempts === 1) throw Object.assign(new Error('secondary rate limit'), {
      status: 429, response: { headers: { 'retry-after': '2' } },
    });
    return 'ok';
  }, { sleep: async ms => { sleeps.push(ms); } });
  assert.equal(result, 'ok');
  assert.deepEqual(sleeps, [2000]);
});

test('trusted exact-head factory PASS is labeled approved but never merge-ready', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:review', 'factory:unowned'],
    setLabels: input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('issue_comment', {
      issue: { number: 44, pull_request: { url: 'https://api.github.test/pulls/44' } },
      comment: {
        author_association: 'OWNER',
        user: { login: 'JoshCLWren' },
        body: `<!-- comic-pile-factory-review-v2:${'a'.repeat(40)}:pass -->`,
      },
    }),
  });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].labels.includes('factory:approved'));
  assert.ok(calls[0].labels.includes('factory:ci'));
  assert.ok(!calls[0].labels.includes('factory:ready'));
});

test('current semantic controller approval adds visibility without changing review stage', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:ready', 'factory:unowned'],
    setLabels: input => calls.push(input),
  });
  const head = 'a'.repeat(40);
  await reconcile({
    github,
    context: contextFor('issue_comment', {
      issue: { number: 44, pull_request: { url: 'https://api.github.test/pulls/44' } },
      comment: {
        author_association: 'NONE',
        user: { login: 'github-actions[bot]' },
        body: '<!-- comic-pile-factory-semantic-review-v1:pr-44:head-'
          + head + ':reviewer-64:producer-unknown:verdict-approve -->\n\n'
          + '### Factory semantic review, Factory 64, APPROVE',
      },
    }),
  });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].labels.includes('factory:approved'));
  assert.ok(calls[0].labels.includes('factory:ready'));
  assert.ok(!calls[0].labels.includes('factory:ci'), 'late comment must not downgrade ready');
});

test('semantic approval cannot be spoofed by wrong head, PR, actor, or prose echo', async () => {
  const head = 'a'.repeat(40);
  const marker = ({ pr = 44, sha = head, verdict = 'approve' } = {}) =>
    '<!-- comic-pile-factory-semantic-review-v1:pr-' + pr
    + ':head-' + sha + ':reviewer-82:producer-11:verdict-' + verdict + ' -->';
  for (const { body, association, actor } of [
    { body: marker({ sha: 'b'.repeat(40) }), association: 'OWNER' },
    { body: marker({ pr: 45 }), association: 'OWNER' },
    { body: marker(), association: 'NONE', actor: 'anonymous' },
    { body: 'Review notes\n' + marker(), association: 'OWNER' },
    { body: marker({ verdict: 'repair' }), association: 'OWNER' },
    { body: marker({ verdict: 'reject' }), association: 'OWNER' },
  ]) {
    const calls = [];
    const github = githubFor({
      labels: ['factory', 'factory:review', 'factory:unowned'],
      setLabels: input => calls.push(input),
    });
    await reconcile({
      github,
      context: contextFor('issue_comment', {
        issue: { number: 44, pull_request: { url: 'https://api.github.test/pulls/44' } },
        comment: {
          author_association: association,
          user: { login: actor || 'JoshCLWren' },
          body,
        },
      }),
    });
    assert.ok(calls.every(call => !call.labels.includes('factory:approved')), body);
  }
});

test('trusted human OWNER semantic approval mints factory:approved for correct head and PR', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:review', 'factory:unowned'],
    setLabels: input => calls.push(input),
  });
  const head = 'a'.repeat(40);
  await reconcile({
    github,
    context: contextFor('issue_comment', {
      issue: { number: 44, pull_request: { url: 'https://api.github.test/pulls/44' } },
      comment: {
        author_association: 'OWNER',
        user: { login: 'JoshCLWren' },
        body: '<!-- comic-pile-factory-semantic-review-v1:pr-44:head-'
          + head + ':reviewer-82:producer-unknown:verdict-approve -->\n\n'
          + '### Factory semantic review, APPROVE',
      },
    }),
  });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].labels.includes('factory:approved'));
  assert.ok(calls[0].labels.includes('factory:review'));
  assert.ok(!calls[0].labels.includes('factory:ci'), 'approval does not change review stage');
});

test('semantic review verdict is not an approval on issue-only conversations', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:building', 'factory:unowned'],
    setLabels: input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('issue_comment', {
      issue: { number: 44 },
      comment: {
        author_association: 'OWNER',
        user: { login: 'JoshCLWren' },
        body: '<!-- comic-pile-factory-semantic-review-v1:pr-44:head-'
          + 'a'.repeat(40) + ':reviewer-82:producer-11:verdict-approve -->',
      },
    }),
  });
  assert.deepEqual(calls, []);
});

test('issue comments cannot stamp a review approval onto a non-PR issue', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:building', 'factory:unowned'],
    setLabels: input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('issue_comment', {
      issue: { number: 4567 },
      comment: {
        author_association: 'OWNER',
        user: { login: 'JoshCLWren' },
        body: `<!-- comic-pile-factory-review-v2:${'a'.repeat(40)}:pass -->`,
      },
    }),
  });
  assert.deepEqual(calls, []);
});

test('stale-head and untrusted PASS comments cannot mint factory:approved', async () => {
  for (const [head, association] of [
    ['b'.repeat(40), 'OWNER'],
    ['a'.repeat(40), 'NONE'],
  ]) {
    const calls = [];
    const github = githubFor({
      labels: ['factory', 'factory:review', 'factory:unowned'],
      setLabels: input => calls.push(input),
    });
    await reconcile({
      github,
      context: contextFor('issue_comment', {
        issue: { number: 44, pull_request: { url: 'https://api.github.test/pulls/44' } },
        comment: {
          author_association: association,
          user: { login: 'not-a-trusted-reviewer' },
          body: `<!-- comic-pile-factory-review-v2:${head}:pass -->`,
        },
      }),
    });
    assert.equal(calls.length, 0);
  }
});

test('actionable feedback clears approval; later CI/ready transitions preserve it', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:approved', 'factory:ci', 'factory:unowned'],
    setLabels: input => calls.push(input),
  });
  await reconcileLabels(github, contextFor('workflow_dispatch', {}), 44, {
    owner: 'factory:unowned', stage: 'factory:ready',
  });
  assert.ok(calls[0].labels.includes('factory:approved'));
  assert.ok(calls[0].labels.includes('factory:ready'));
  await reconcileLabels(github, contextFor('workflow_dispatch', {}), 44, {
    owner: 'factory:unowned', stage: 'factory:changes-requested',
  });
  assert.ok(!calls[1].labels.includes('factory:approved'));
  assert.ok(calls[1].labels.includes('factory:changes-requested'));
});

test('a new PR head invalidates old factory:approved even if an old PASS survives', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:approved', 'factory:ci', 'factory:unowned'],
    setLabels: input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('pull_request_target', {
      action: 'synchronize',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        body: 'Closes #4567',
        head: { ref: 'factory/99-4567-test', sha: 'b'.repeat(40), repo: { full_name: 'JoshCLWren/comic-pile' } },
        labels: [{ name: 'factory' }],
        number: 4568,
      },
    }),
  });
  assert.equal(calls.length, 1);
  assert.ok(!calls[0].labels.includes('factory:approved'));
  assert.ok(calls[0].labels.includes('factory:review'));
});

test('approved label is not a stage, owner, or merge gate', () => {
  assert.ok(!STAGE_LABELS.includes('factory:approved'));
  assert.ok(!DERIVED_LABELS.has('factory:approved'));
  assert.ok(!reconcile._test.BLOCKED_LABELS.has('factory:approved'));
  assert.equal(reconcile._test.isOwnerLabel('factory:approved'), false);
});

test('PR progress comments preserve an advanced review stage and local owner', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:review', 'factory:unowned'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('issue_comment', {
      issue: { number: 44, pull_request: { url: 'https://api.github.test/pulls/44' } },
      comment: {
        author_association: 'OWNER',
        user: { login: 'JoshCLWren' },
        body: '<!-- comic-pile-factory-fix-progress-v3:abc:local:123 -->',
      },
    }),
  });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].labels.includes('factory:local'));
  assert.ok(calls[0].labels.includes('factory:review'));
  assert.ok(!calls[0].labels.includes('factory:building'));
  assert.ok(!calls[0].labels.includes('factory:unowned'));
});

test('fixed-model progress comments use the durable fixed owner', async () => {
  const calls = [];
  const dynamicWorker = String([...EXPECTED_WORKER_IDS][0]);
  const github = githubFor({
    labels: ['factory', 'factory:building', 'factory:unowned'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('issue_comment', {
      issue: { number: 1089 },
      comment: {
        author_association: 'OWNER',
        user: { login: 'JoshCLWren' },
        body: `<!-- comic-pile-factory-implement-progress-v3:issue-1089:opencode-free-model-factory-${dynamicWorker}:123 -->`,
      },
    }),
  });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].labels.includes(`factory:${dynamicWorker}`));
  assert.ok(!calls[0].labels.includes('factory:unowned'));
});

test('PR synchronize events refresh owner and stage from the linked issue', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:building', 'factory:5'],
    comments: [{
      author_association: 'OWNER',
      body: '<!-- comic-pile-factory-implement-claim-v3:issue-999:local:123:attempt-1 -->',
      created_at: '2026-08-09T12:00:00Z',
      user: { login: 'JoshCLWren' },
    }],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('pull_request_target', {
      action: 'synchronize',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        body: 'Closes #999',
        head: {
          ref: 'factory/999-atomic-label-reconciliation',
          repo: { full_name: 'JoshCLWren/comic-pile' },
        },
        labels: [{ name: 'factory' }],
        number: 1000,
      },
    }),
  });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].labels.includes('factory:local'));
  assert.ok(calls[0].labels.includes('factory:review'));
  assert.ok(!calls[0].labels.includes('factory:5'));
  assert.ok(!calls[0].labels.includes('factory:building'));
});

test('PR refresh preserves a roster fixed-model PR-local owner', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:review', 'factory:49'],
    comments: [{
      author_association: 'OWNER',
      body: '<!-- comic-pile-factory-claim-released-v3:issue-1089:opencode-free-model-factory-49:123 -->',
      created_at: '2026-08-14T14:27:00Z',
      user: { login: 'JoshCLWren' },
    }],
    setLabels: async input => calls.push(input),
  });

  await reconcile({
    github,
    context: contextFor('pull_request_target', {
      action: 'synchronize',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        body: 'Closes #1089',
        head: {
          ref: 'factory/32-1089-omni',
          repo: { full_name: 'JoshCLWren/comic-pile' },
        },
        labels: [{ name: 'factory' }, { name: 'factory:49' }],
        number: 1202,
      },
    }),
  });

  assert.equal(calls.length, 0);
  // A valid preserved owner and stage require no API write.
});

test('released fixed-model PR resolves closing issue instead of worker branch number', async () => {
  const calls = [];
  const commentIssueNumbers = [];
  const github = githubFor({
    labels: ['factory', 'factory:review', 'factory:unowned'],
    comments: [{
      author_association: 'OWNER',
      body: '<!-- comic-pile-factory-claim-released-v3:issue-1089:opencode-free-model-factory-49:123 -->',
      created_at: '2026-08-14T14:27:00Z',
      user: { login: 'JoshCLWren' },
    }],
    commentIssueNumbers,
    setLabels: async input => calls.push(input),
  });

  await reconcile({
    github,
    context: contextFor('pull_request_target', {
      action: 'synchronize',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        body: 'Closes #1089',
        head: {
          ref: 'factory/32-1089-omni',
          repo: { full_name: 'JoshCLWren/comic-pile' },
        },
        labels: [{ name: 'factory' }, { name: 'factory:unowned' }],
        number: 1202,
      },
    }),
  });

  assert.deepEqual(commentIssueNumbers, [1089]);
  assert.equal(calls.length, 0);
  // Released ownership already matches, so no write is needed.
});

test('PR refresh preserves one external owner after the linked issue is released', async () => {
  const calls = [];
  const github = githubFor({
    labels: [
      'bug',
      'factory',
      'factory:13',
      'factory:unowned',
      'factory:review',
    ],
    comments: [{
      author_association: 'OWNER',
      body: '<!-- comic-pile-factory-claim-released-v3:issue-1149:chatgpt-factory-2:123 -->',
      created_at: '2026-08-13T06:00:00Z',
      user: { login: 'JoshCLWren' },
    }],
    setLabels: async input => calls.push(input),
  });

  await reconcile({
    github,
    context: contextFor('pull_request_target', {
      action: 'synchronize',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        body: 'Closes #1149',
        head: {
          ref: 'factory/1149-omniroute',
          repo: { full_name: 'JoshCLWren/comic-pile' },
        },
        labels: [
          { name: 'factory' },
          { name: 'factory:13' },
          { name: 'factory:unowned' },
        ],
        number: 1155,
      },
    }),
  });

  assert.equal(calls.length, 1);
  const owners = calls[0].labels.filter(label => (
    label === 'factory:unowned'
    || label === 'factory:local'
    || /^factory:(?:[1-9]|[1-3][0-9]|[4-9][0-9])$/.test(label)
  ));
  assert.deepEqual(owners, ['factory:13']);
  assert.ok(calls[0].labels.includes('factory:review'));
});

test('native APPROVED review from OWNER makes no label API calls', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:ci', 'factory:43'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('pull_request_review', {
      action: 'submitted',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        head: {
          ref: 'factory/43-1386-opencode-free',
          repo: { full_name: 'JoshCLWren/comic-pile' },
        },
        labels: [{ name: 'factory' }, { name: 'factory:43' }, { name: 'factory:ci' }],
        number: 1390,
      },
      review: {
        state: 'APPROVED',
        author_association: 'OWNER',
        user: { login: 'JoshCLWren' },
      },
    }),
  });
  assert.equal(calls.length, 0);
});

test('native APPROVED review from MEMBER makes no label API calls', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:ready', 'factory:43'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('pull_request_review', {
      action: 'submitted',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        head: {
          ref: 'factory/43-1386-opencode-free',
          repo: { full_name: 'JoshCLWren/comic-pile' },
        },
        labels: [{ name: 'factory' }, { name: 'factory:43' }, { name: 'factory:ready' }],
        number: 1390,
      },
      review: {
        state: 'APPROVED',
        author_association: 'MEMBER',
        user: { login: 'some-member' },
      },
    }),
  });
  assert.equal(calls.length, 0);
});

test('native APPROVED review from COLLABORATOR makes no label API calls', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:review', 'factory:43'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('pull_request_review', {
      action: 'submitted',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        head: {
          ref: 'factory/43-1386-opencode-free',
          repo: { full_name: 'JoshCLWren/comic-pile' },
        },
        labels: [{ name: 'factory' }, { name: 'factory:43' }, { name: 'factory:review' }],
        number: 1390,
      },
      review: {
        state: 'APPROVED',
        author_association: 'COLLABORATOR',
        user: { login: 'some-collaborator' },
      },
    }),
  });
  assert.equal(calls.length, 0);
});

test('native APPROVED review from github-actions[bot] makes no label API calls', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:ci', 'factory:43'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('pull_request_review', {
      action: 'submitted',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        head: {
          ref: 'factory/43-1386-opencode-free',
          repo: { full_name: 'JoshCLWren/comic-pile' },
        },
        labels: [{ name: 'factory' }, { name: 'factory:43' }, { name: 'factory:ci' }],
        number: 1390,
      },
      review: {
        state: 'APPROVED',
        author_association: 'OWNER',
        user: { login: 'github-actions[bot]' },
      },
    }),
  });
  assert.equal(calls.length, 0);
});

test('native APPROVED review from worker App with OWNER association makes no label API calls', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:ready', 'factory:48'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('pull_request_review', {
      action: 'submitted',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        head: {
          ref: 'factory/48-3136-opencode-free',
          repo: { full_name: 'JoshCLWren/comic-pile' },
        },
        labels: [{ name: 'factory' }, { name: 'factory:48' }, { name: 'factory:ready' }],
        number: 3136,
      },
      review: {
        state: 'APPROVED',
        author_association: 'OWNER',
        user: { login: 'MarkCordova[bot]' },
        performed_via_github_app: { slug: 'mark-cordova-comic-pile', owner: { login: 'JoshCLWren' } },
      },
    }),
  });
  assert.equal(calls.length, 0);
});

test('CHANGES_REQUESTED review still sets factory:changes-requested', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:review', 'factory:43'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('pull_request_review', {
      action: 'submitted',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        head: {
          ref: 'factory/43-1386-opencode-free',
          repo: { full_name: 'JoshCLWren/comic-pile' },
        },
        labels: [{ name: 'factory' }, { name: 'factory:43' }, { name: 'factory:review' }],
        number: 1390,
      },
      review: {
        state: 'CHANGES_REQUESTED',
        author_association: 'OWNER',
        user: { login: 'JoshCLWren' },
      },
    }),
  });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].labels.includes('factory:changes-requested'));
  assert.ok(!calls[0].labels.includes('factory:review'));
  assert.ok(!calls[0].labels.includes('factory:ci'));
});

test('dismissed review resets to factory:review', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:changes-requested', 'factory:43'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('pull_request_review', {
      action: 'dismissed',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        head: {
          ref: 'factory/43-1386-opencode-free',
          repo: { full_name: 'JoshCLWren/comic-pile' },
        },
        labels: [{ name: 'factory' }, { name: 'factory:43' }, { name: 'factory:changes-requested' }],
        number: 1390,
      },
      review: {
        state: 'CHANGES_REQUESTED',
        author_association: 'OWNER',
        user: { login: 'JoshCLWren' },
      },
    }),
  });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].labels.includes('factory:review'));
  assert.ok(!calls[0].labels.includes('factory:changes-requested'));
  assert.ok(!calls[0].labels.includes('factory:ci'));
});

function enrollGithubFor({ pulls, labelsByPr, comments = [], setLabels }) {
  const api = {
    listLabelsForRepo: async () => ({ data: [], headers: {}, status: 200 }),
    createLabel: async () => ({ data: {}, headers: {}, status: 201 }),
    listLabelsOnIssue: async params => ({
      data: (labelsByPr[params.issue_number] || []).map(name => ({ name })),
      headers: {},
      status: 200,
    }),
    listComments: async () => ({ data: comments, headers: {}, status: 200 }),
    setLabels: async params => {
      if (setLabels) setLabels(params);
      return { data: {}, headers: {}, status: 200 };
    },
  };
  return {
    rest: {
      issues: api,
      pulls: {
        list: async () => ({ data: pulls, headers: {}, status: 200 }),
        get: async ({ pull_number }) => ({
          data: pulls.find(item => item.number === pull_number) || pulls[0],
          headers: {},
          status: 200,
        }),
      },
    },
  };
}

function enrollPr(number, extra = {}) {
  return {
    number,
    state: 'open',
    draft: false,
    user: { login: 'JoshCLWren' },
    body: '',
    head: { sha: 'a'.repeat(40), repo: { full_name: 'JoshCLWren/comic-pile' } },
    ...extra,
  };
}

test('enroll adds review stage without stealing a local factory lease', async () => {
  for (const labels of [
    ['factory', 'factory:local'],
    ['infrastructure', 'factory', 'factory:local'],
    ['factory:local'],
  ]) {
    const calls = [];
    const pr = enrollPr(3142);
    const github = enrollGithubFor({
      pulls: [pr],
      labelsByPr: { 3142: labels },
      setLabels: async input => calls.push(input),
    });
    for (const context of [
      contextFor('pull_request_target', { pull_request: pr }),
      contextFor('schedule', {}),
    ]) {
      await reconcile.reconcileMissingPrLabels({ github, context });
    }
    assert.equal(calls.length, 2, 'test harness returns the original labels on each read');
    for (const call of calls) {
      assert.deepEqual(new Set(call.labels),
        new Set([...labels.filter(label => label !== 'factory'), 'factory', 'factory:local', 'factory:review']));
      assert.ok(!call.labels.includes('factory:unowned'));
    }
  }
});

test('enroll sweep repairs local review stages and preserves active owners', async () => {
  const calls = [];
  const github = enrollGithubFor({
    pulls: [enrollPr(3153), enrollPr(3154), enrollPr(3200)],
    labelsByPr: {
      3153: ['infrastructure', 'factory', 'factory:local'],
      3154: ['factory', 'factory:local', 'factory:unowned'],
      3200: ['bug'],
    },
    setLabels: async input => calls.push(input),
  });
  await reconcile.reconcileMissingPrLabels({ github, context: contextFor('schedule', {}) });
  assert.deepEqual(calls.map(call => call.issue_number), [3153, 3154, 3200]);
  assert.deepEqual(
    new Set(calls[0].labels),
    new Set(['infrastructure', 'factory', 'factory:local', 'factory:review']),
  );
  assert.deepEqual(
    new Set(calls[1].labels),
    new Set(['factory', 'factory:local', 'factory:review']),
  );
  assert.deepEqual(
    new Set(calls[2].labels),
    new Set(['bug', 'factory', 'factory:unowned', 'factory:review']),
  );
});

test('enroll leaves manual-only PRs untouched even without factory:local', async () => {
  const calls = [];
  const manual = enrollPr(3157, {
    body: 'Infra change.\n\n<!-- factory-execution:manual-only -->\n',
  });
  const github = enrollGithubFor({
    pulls: [manual, enrollPr(3201, { body: 'ordinary PR' })],
    labelsByPr: {
      3157: ['factory', 'factory:unowned'],
      3201: ['factory', 'factory:unowned'],
    },
    setLabels: async input => calls.push(input),
  });
  await reconcile.reconcileMissingPrLabels({
    github,
    context: contextFor('pull_request_target', { pull_request: manual }),
  });
  assert.equal(calls.length, 0);
  await reconcile.reconcileMissingPrLabels({ github, context: contextFor('schedule', {}) });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].issue_number, 3201);
  assert.ok(calls[0].labels.includes('factory:review'));
});

test('worker GitHub App with OWNER association is NOT trusted for marker reconcile', () => {
  const workerAppComment = {
    author_association: 'OWNER',
    user: { login: 'mark-cordova[bot]' },
    body: '<!-- comic-pile-factory-implement-claim-v3:issue-123:mark-cordova:456 -->',
    performed_via_github_app: { slug: 'mark-cordova' },
  };
  assert.equal(trusted(workerAppComment), false);
  assert.equal(performedViaUntrustedApp(workerAppComment), true);
});

test('github-actions[bot] with NONE association IS trusted for marker reconcile', () => {
  const actionsComment = {
    author_association: 'NONE',
    user: { login: 'github-actions[bot]' },
    body: '<!-- comic-pile-factory-implement-claim-v3:issue-123:local:456 -->',
    performed_via_github_app: { slug: 'github-actions' },
  };
  assert.equal(trusted(actionsComment), true);
  assert.equal(performedViaUntrustedApp(actionsComment), false);
});

test('github-actions App slug with a non-actions login and NONE association is NOT trusted', () => {
  // Mirrors Python trusted_comment_bodies: require exact github-actions[bot] login
  // when association is not OWNER/MEMBER/COLLABORATOR. Slug alone is not enough.
  const spoof = {
    author_association: 'NONE',
    user: { login: 'not-actions[bot]' },
    body: '<!-- comic-pile-factory-implement-claim-v3:issue-123:local:456 -->',
    performed_via_github_app: { slug: 'github-actions' },
  };
  assert.equal(trusted(spoof), false);
  assert.equal(performedViaUntrustedApp(spoof), false);
});

test('github-actions[bot] with NONE and no performed_via_github_app IS trusted', () => {
  const actionsComment = {
    author_association: 'NONE',
    user: { login: 'github-actions[bot]' },
    body: '<!-- comic-pile-factory-implement-claim-v3:issue-123:local:456 -->',
  };
  assert.equal(trusted(actionsComment), true);
});

test('human OWNER comment with no App IS trusted', () => {
  const humanComment = {
    author_association: 'OWNER',
    user: { login: 'JoshCLWren' },
    body: '<!-- comic-pile-factory-implement-claim-v3:issue-123:local:456 -->',
  };
  assert.equal(trusted(humanComment), true);
  assert.equal(performedViaUntrustedApp(humanComment), false);
});

test('TRUSTED_APP_SLUGS is exactly github-actions', () => {
  assert.deepEqual([...TRUSTED_APP_SLUGS].sort(), ['github-actions']);
});

test('issue_comment reconcile rejects worker App comment even with OWNER association', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:building', 'factory:unowned'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('issue_comment', {
      issue: { number: 44, pull_request: { url: 'https://api.github.test/pulls/44' } },
      comment: {
        author_association: 'OWNER',
        user: { login: 'mark-cordova[bot]' },
        body: '<!-- comic-pile-factory-implement-progress-v3:issue-44:mark-cordova:123 -->',
        performed_via_github_app: { slug: 'mark-cordova' },
      },
    }),
  });
  // Worker App comment should be rejected, no label changes
  assert.equal(calls.length, 0);
});

function reviewPayload(review) {
  return contextFor('pull_request_review', {
    action: 'submitted',
    repository: { full_name: 'JoshCLWren/comic-pile' },
    pull_request: {
      number: 100,
      head: {
        ref: 'factory/49-3154-app-identity',
        repo: { full_name: 'JoshCLWren/comic-pile' },
      },
      labels: [{ name: 'factory' }, { name: 'factory:review' }, { name: 'factory:unowned' }],
    },
    review,
  });
}

test('pull_request_review reconcile rejects worker App CHANGES_REQUESTED review', async () => {
  // CHANGES_REQUESTED is the only state that mutates labels, so this is the
  // state that actually exercises the trusted() gate. An APPROVED review is a
  // label no-op, so asserting on APPROVED would pass even with the gate removed.
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:review', 'factory:unowned'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: reviewPayload({
      author_association: 'OWNER',
      user: { login: 'mark-cordova[bot]' },
      state: 'CHANGES_REQUESTED',
      performed_via_github_app: { slug: 'mark-cordova' },
    }),
  });
  // Worker App review must be rejected, no label changes
  assert.equal(calls.length, 0);
});

test('pull_request_review reconcile still accepts github-actions[bot] CHANGES_REQUESTED', async () => {
  // Positive control proving the negative test above is load-bearing: the same
  // payload from the trusted GITHUB_TOKEN actor does reach reconcileLabels.
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:review', 'factory:unowned'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: reviewPayload({
      author_association: 'NONE',
      user: { login: 'github-actions[bot]' },
      state: 'CHANGES_REQUESTED',
      performed_via_github_app: { slug: 'github-actions' },
    }),
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].issue_number, 100);
  assert.ok(calls[0].labels.includes('factory:changes-requested'));
});

test('pull_request_review reconcile still ignores a native APPROVED review', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:review', 'factory:unowned'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: reviewPayload({
      author_association: 'NONE',
      user: { login: 'github-actions[bot]' },
      state: 'APPROVED',
      performed_via_github_app: { slug: 'github-actions' },
    }),
  });
  assert.equal(calls.length, 0);
});

test('enroll review-verdict scan ignores a worker App pass/verdict marker', async () => {
  // A worker App must not be able to stamp factory:changes-requested or
  // factory:ci on a head through the enroll recovery scan.
  const head = 'b'.repeat(40);
  for (const verdict of ['pass', 'changes-required']) {
    const expectedStage = verdict === 'pass' ? 'factory:ci' : 'factory:changes-requested';
    const workerCalls = [];
    const workerGithub = enrollGithubFor({
      pulls: [enrollPr(100, { head: { sha: head, repo: { full_name: 'JoshCLWren/comic-pile' } } })],
      labelsByPr: { 100: ['factory', 'factory:unowned'] },
      comments: [{
        created_at: '2026-01-01T00:00:00Z',
        author_association: 'OWNER',
        user: { login: 'mark-cordova[bot]' },
        body: `<!-- comic-pile-factory-review-v2:${head}:${verdict} -->`,
        performed_via_github_app: { slug: 'mark-cordova' },
      }],
      setLabels: async input => workerCalls.push(input),
    });
    await reconcile.reconcileMissingPrLabels({
      github: workerGithub,
      context: contextFor('schedule', {}),
    });
    assert.equal(workerCalls.length, 1, `${verdict}: worker App must not stop enrollment`);
    assert.ok(
      workerCalls[0].labels.includes('factory:review'),
      `${verdict}: worker App verdict must not set ${expectedStage}`,
    );

    // Positive control: the trusted actor's verdict marker is still honored.
    const actionsCalls = [];
    const actionsGithub = enrollGithubFor({
      pulls: [enrollPr(100, { head: { sha: head, repo: { full_name: 'JoshCLWren/comic-pile' } } })],
      labelsByPr: { 100: ['factory', 'factory:unowned'] },
      comments: [{
        created_at: '2026-01-01T00:00:00Z',
        author_association: 'NONE',
        user: { login: 'github-actions[bot]' },
        body: `<!-- comic-pile-factory-review-v2:${head}:${verdict} -->`,
        performed_via_github_app: { slug: 'github-actions' },
      }],
      setLabels: async input => actionsCalls.push(input),
    });
    await reconcile.reconcileMissingPrLabels({
      github: actionsGithub,
      context: contextFor('schedule', {}),
    });
    assert.equal(actionsCalls.length, 1, `${verdict}: trusted verdict must be honored`);
    assert.ok(actionsCalls[0].labels.includes(expectedStage));
  }
});

test('pull_request_target owner lookup ignores a worker App claim marker', async () => {
  const dynamicWorker = String([...EXPECTED_WORKER_IDS][0]);
  const pullRequest = {
    number: 100,
    state: 'open',
    draft: false,
    body: 'Refs #3131',
    head: {
      ref: `factory/${dynamicWorker}-3131-opencode-free-identity`,
      sha: 'c'.repeat(40),
      repo: { full_name: 'JoshCLWren/comic-pile' },
    },
  };
  const workerAppComment = {
    created_at: '2026-01-01T00:00:00Z',
    author_association: 'OWNER',
    user: { login: 'mark-cordova[bot]' },
    body: `<!-- comic-pile-factory-implement-claim-v3:issue-3131:opencode-free-model-factory-${dynamicWorker}:1 -->`,
    performed_via_github_app: { slug: 'mark-cordova' },
  };
  const workerCalls = [];
  const workerIssueNumbers = [];
  await reconcile({
    github: githubFor({
      labels: ['factory', 'factory:review', 'factory:unowned'],
      comments: [workerAppComment],
      commentIssueNumbers: workerIssueNumbers,
      setLabels: async input => workerCalls.push(input),
    }),
    context: contextFor('pull_request_target', {
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: pullRequest,
    }),
  });
  // Prove the linked-issue owner lookup actually consumed the worker App comment.
  assert.ok(workerIssueNumbers.includes(3131), 'owner lookup must read issue 3131 comments');
  // Untrusted worker marker cannot change an already-correct owner.
  assert.equal(workerCalls.length, 0);

  const actionsCalls = [];
  await reconcile({
    github: githubFor({
      labels: ['factory', 'factory:review', 'factory:unowned'],
      comments: [{
        ...workerAppComment,
        author_association: 'NONE',
        user: { login: 'github-actions[bot]' },
        performed_via_github_app: { slug: 'github-actions' },
      }],
      setLabels: async input => actionsCalls.push(input),
    }),
    context: contextFor('pull_request_target', {
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: pullRequest,
    }),
  });
  assert.equal(actionsCalls.length, 1);
  assert.ok(actionsCalls[0].labels.includes(`factory:${dynamicWorker}`));
});

test('pull_request_review trust: github-actions[bot] is trusted, worker App is not', () => {
  // Order-independent with #3157: native APPROVED must not assert stage/label
  // promotion here. #3157 makes APPROVED a label no-op; this contract only
  // checks who may author a trusted review signal.
  const actionsReview = {
    author_association: 'NONE',
    user: { login: 'github-actions[bot]' },
    state: 'APPROVED',
    performed_via_github_app: { slug: 'github-actions' },
  };
  const workerReview = {
    author_association: 'OWNER',
    user: { login: 'mark-cordova[bot]' },
    state: 'APPROVED',
    performed_via_github_app: { slug: 'mark-cordova' },
  };
  assert.equal(trusted(actionsReview), true);
  assert.equal(performedViaUntrustedApp(actionsReview), false);
  assert.equal(trusted(workerReview), false);
  assert.equal(performedViaUntrustedApp(workerReview), true);
});

test('request log records, reads, counts, and clears requests', async () => {
  clearRestRequestLog();
  assert.deepEqual(restRequestLog(), []);
  assert.deepEqual(restRequestCounts(), {});
  const context = contextFor('workflow_dispatch', {});
  const github = {
    rest: {
      issues: {
        listComments: async () => ({ data: [], headers: {}, status: 200 }),
      },
    },
  };
  await restCall(github, context, {
    endpoint: 'repos/o/r/issues/1/comments',
    method: 'GET',
    event: 'workflow_dispatch',
  }, async () => github.rest.issues.listComments({ owner: 'o', repo: 'r', issue_number: 1 }));
  assert.equal(restRequestLog().length, 1);
  const record = restRequestLog()[0];
  const key = `${record.installation}|${record.workflow}|${record.event}|${record.kind}|${record.endpoint}|${record.status}`;
  assert.equal(restRequestCounts()[key], 1);
  clearRestRequestLog();
  assert.deepEqual(restRequestLog(), []);
  assert.deepEqual(restRequestCounts(), {});
});

test('restCall records each outbound request with status and headers', async () => {
  clearRestRequestLog();
  const context = contextFor('workflow_dispatch', {});
  const github = {
    rest: {
      issues: {
        listComments: async () => ({ data: [{ id: 1 }], headers: { 'x-ratelimit-remaining': '4999' }, status: 200 }),
      },
    },
  };
  const response = await restCall(github, context, {
    endpoint: 'repos/o/r/issues/1/comments',
    method: 'GET',
    event: 'workflow_dispatch',
  }, async () => github.rest.issues.listComments({ owner: 'o', repo: 'r', issue_number: 1 }));
  assert.deepEqual(response.data, [{ id: 1 }]);
  const log = restRequestLog();
  assert.equal(log.length, 1);
  assert.equal(log[0].endpoint, 'repos/o/r/issues/1/comments');
  assert.equal(log[0].status, 200);
  assert.equal(log[0].remaining, 4999);
  const key = `${log[0].installation}|${log[0].workflow}|${log[0].event}|${log[0].kind}|${log[0].endpoint}|${log[0].status}`;
  assert.equal(restRequestCounts()[key], 1);
});

test('restCall records a failed attempt then rethrows', async () => {
  clearRestRequestLog();
  const context = contextFor('workflow_dispatch', {});
  const error = Object.assign(new Error('rate limited'), {
    status: 429, response: { headers: { 'retry-after': '1' } },
  });
  const github = {
    rest: { issues: { listComments: async () => { throw error; } } },
  };
  await assert.rejects(
    restCall(github, context, {
      endpoint: 'repos/o/r/issues/1/comments',
      method: 'GET',
      event: 'workflow_dispatch',
    }, async () => github.rest.issues.listComments({})),
    /rate limited/
  );
  const log = restRequestLog();
  assert.equal(log.length, 1);
  assert.equal(log[0].status, 429);
  assert.equal(log[0].limited, true);
  assert.equal(log[0].retryAfter, 1);
});

test('currentLabels reads are recorded in the request log', async () => {
  clearRestRequestLog();
  const github = githubFor({
    labels: ['bug', 'factory'],
    setLabels: async () => {},
  });
  const context = contextFor('workflow_dispatch', {});
  const labels = await currentLabels(github, context, 12);
  assert.deepEqual(labels, new Set(['bug', 'factory']));
  const log = restRequestLog();
  assert.equal(log.length, 1);
  assert.equal(
    log[0].endpoint,
    'repos/JoshCLWren/comic-pile/issues/12/labels?per_page=100&page=1',
  );
  assert.equal(log[0].method, 'GET');
});

test('paged reads record every page request separately', async () => {
  clearRestRequestLog();
  // Two pages: the first is full, the second short, so pagination stops.
  const pages = [
    Array.from({ length: 100 }, (_, index) => ({ name: `label-${index}` })),
    [{ name: 'last-label' }],
  ];
  const github = {
    rest: { issues: { listLabelsOnIssue: async ({ page }) => ({
      data: pages[page - 1] || [],
      headers: {},
      status: 200,
    }) } },
  };
  const labels = await currentLabels(github, contextFor('workflow_dispatch', {}), 12);
  assert.equal(labels.size, 101);
  const log = restRequestLog();
  assert.equal(log.length, 2);
  assert.equal(log[0].endpoint, 'repos/JoshCLWren/comic-pile/issues/12/labels?per_page=100&page=1');
  assert.equal(log[1].endpoint, 'repos/JoshCLWren/comic-pile/issues/12/labels?per_page=100&page=2');
});

test('each retry attempt is recorded, including the failed request', async () => {
  clearRestRequestLog();
  let attempts = 0;
  const github = {
    rest: { issues: { listLabelsOnIssue: async () => {
      attempts += 1;
      if (attempts === 1) {
        throw Object.assign(new Error('secondary rate limit'), {
          status: 429, response: { headers: { 'retry-after': '1' } },
        });
      }
      return { data: [{ name: 'bug' }], headers: {}, status: 200 };
    } } },
  };
  const labels = await currentLabels(github, contextFor('workflow_dispatch', {}), 12);
  assert.deepEqual(labels, new Set(['bug']));
  const log = restRequestLog();
  assert.equal(log.length, 2);
  assert.equal(log[0].status, 429);
  assert.equal(log[0].limited, true);
  assert.equal(log[0].retryAfter, 1);
  assert.equal(log[1].status, 200);
  assert.equal(log[1].limited, false);
});

test('unlabeled PR event preserves ready parked under a live local lease', async () => {
  // Regression for PR #5393: the reconciler reverted factory:ready +
  // factory:local to factory:review + factory:unowned within seconds.
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:local', 'factory:ready'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('pull_request_target', {
      action: 'unlabeled',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        body: 'Part of #4853',
        head: {
          ref: 'factory/work-checkpoints/issue-4853/hy4-flash-free/44224727998',
          sha: 'c'.repeat(40),
          repo: { full_name: 'JoshCLWren/comic-pile' },
        },
        labels: [{ name: 'factory' }, { name: 'factory:local' }, { name: 'factory:ready' }],
        number: 5393,
      },
    }),
  });
  assert.deepEqual(calls, [], 'ready+local must not be rewritten by a label event');
});

test('PR synchronize still invalidates ready but keeps the local owner', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:local', 'factory:ready'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('pull_request_target', {
      action: 'synchronize',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        body: 'Part of #4853',
        head: {
          ref: 'factory/work-checkpoints/issue-4853/hy4-flash-free/44224727998',
          sha: 'd'.repeat(40),
          repo: { full_name: 'JoshCLWren/comic-pile' },
        },
        labels: [{ name: 'factory' }, { name: 'factory:local' }, { name: 'factory:ready' }],
        number: 5393,
      },
    }),
  });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].labels.includes('factory:local'));
  assert.ok(calls[0].labels.includes('factory:review'));
  assert.ok(!calls[0].labels.includes('factory:ready'), 'a new head invalidates ready');
  assert.ok(!calls[0].labels.includes('factory:unowned'));
});

test('off-roster review-claim marker cannot strip a live local lease or ready stage', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:local', 'factory:ready'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('issue_comment', {
      issue: { number: 5393, pull_request: { url: 'https://api.github.test/pulls/5393' } },
      comment: {
        author_association: 'OWNER',
        user: { login: 'JoshCLWren' },
        body: '<!-- comic-pile-factory-review-claim-v3:pr-5393:opencode-free-model-factory-74:123 -->',
      },
    }),
  });
  assert.deepEqual(calls, [], 'the marker must not replace the live local owner');
});

test('explicit trusted release by the local worker still hands off to unowned', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:local', 'factory:review'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('issue_comment', {
      issue: { number: 5393, pull_request: { url: 'https://api.github.test/pulls/5393' } },
      comment: {
        author_association: 'OWNER',
        user: { login: 'JoshCLWren' },
        body: '<!-- comic-pile-factory-claim-released-v3:pr-5393:local:123 -->',
      },
    }),
  });
  assert.equal(calls.length, 1);
  assert.ok(calls[0].labels.includes('factory:unowned'));
  assert.ok(!calls[0].labels.includes('factory:local'));
});

test('release marker from another worker does not strip the local lease', async () => {
  const calls = [];
  const github = githubFor({
    labels: ['factory', 'factory:local', 'factory:review'],
    setLabels: async input => calls.push(input),
  });
  await reconcile({
    github,
    context: contextFor('issue_comment', {
      issue: { number: 5393, pull_request: { url: 'https://api.github.test/pulls/5393' } },
      comment: {
        author_association: 'OWNER',
        user: { login: 'JoshCLWren' },
        body: '<!-- comic-pile-factory-claim-released-v3:pr-5393:opencode-free-model-factory-75:123 -->',
      },
    }),
  });
  assert.deepEqual(calls, [], 'a foreign release marker must not replace factory:local');
});
