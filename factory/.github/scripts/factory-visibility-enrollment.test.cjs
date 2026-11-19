const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const visibility = require('./factory-visibility.cjs');
const { reconcileMissingPrLabels } = visibility;
const { BLOCKED_LABELS, hasManualOnlyDirective } = visibility._test;

const sha = 'a'.repeat(40);
const MANUAL_ONLY = '<!-- factory-execution:manual-only -->';
const pr = (number = 1) => ({
  number, state: 'open', draft: false, user: { login: 'JoshCLWren' },
  body: '',
  head: { sha, repo: { full_name: 'JoshCLWren/comic-pile' } },
});

async function run({
  prs = [pr()],
  labels = [],
  comments = [],
  event = 'schedule',
  action = 'opened',
  liveByNumber,
  getError,
  snapshot = null,
} = {}) {
  const writes = [];
  const reads = [];
  const gets = [];
  const labelReads = [];
  let repoLabelReads = 0;
  // REST response doubles: every read now consumes `result.data`, and label
  // reads are paginated issue REST endpoints rather than github.paginate.
  const issues = {
    listLabelsForRepo: async () => {
      repoLabelReads += 1;
      return { data: [], headers: {}, status: 200 };
    },
    createLabel: async () => ({ data: {}, headers: {}, status: 201 }),
    listLabelsOnIssue: async ({ issue_number }) => {
      labelReads.push(issue_number);
      return { data: labels.map(name => ({ name })), headers: {}, status: 200 };
    },
    listComments: async () => ({ data: comments, headers: {}, status: 200 }),
    async setLabels(input) { writes.push(input); return { data: {}, headers: {}, status: 200 }; },
  };
  const pulls = {
    async list(params) {
      reads.push(params);
      return { data: prs.slice((params.page - 1) * 100, params.page * 100), headers: {}, status: 200 };
    },
    async get(params) {
      gets.push(params);
      if (getError) throw getError;
      const item = prs.find(candidate => candidate.number === params.pull_number) || prs[0];
      const overlay = liveByNumber?.[params.pull_number];
      return {
        data: overlay ? { ...item, ...overlay } : { ...item },
        headers: {},
        status: 200,
      };
    },
  };
  const github = { rest: { issues, pulls } };
  await reconcileMissingPrLabels({ github, openPullRequests: snapshot, context: {
    eventName: event, repo: { owner: 'JoshCLWren', repo: 'comic-pile' },
    payload: { pull_request: prs[0], action },
  } });
  return { writes, reads, gets, labelReads, repoLabelReads };
}


test('fresh Neon PR inventory skips all GitHub enumeration when enrollment is complete', async () => {
  const labels = [{ name: 'factory' }, { name: 'factory:review' }, { name: 'factory:unowned' }];
  const { reads, gets, writes, repoLabelReads } = await run({
    snapshot: [{ number: 1, labels, headRefOid: sha, headRefName: 'factory/42-1', isDraft: false }],
    prs: [{ ...pr(1), labels }],
  });
  assert.deepEqual(reads, []);
  assert.deepEqual(gets, []);
  assert.deepEqual(writes, []);
  assert.equal(repoLabelReads, 0);
});

test('incomplete Neon PR inventory checks live GitHub before modifying labels', async () => {
  const { reads, gets, writes } = await run({
    snapshot: [{ number: 1, labels: [], headRefOid: sha, headRefName: 'factory/42-1', isDraft: false }],
    prs: [pr(1)],
    labels: [],
  });
  assert.deepEqual(reads, [], 'Neon supplies the candidate inventory');
  assert.equal(gets.length, 1, 'live PR must still authorize mutation');
  assert.equal(writes.length, 1);
});

test('open event enrolls a manual PR and preserves unrelated labels atomically', async () => {
  const { writes } = await run({ labels: ['bug'], event: 'pull_request_target', action: 'opened' });
  assert.equal(writes.length, 1);
  assert.deepEqual(new Set(writes[0].labels),
    new Set(['bug', 'factory', 'factory:review', 'factory:unowned']));
});

test('pull_request_target enrolls the payload PR without listing pulls', async () => {
  // Regression: PR events supplied the payload as a bare array while the loop
  // expected `{ data }`, so every non-edited PR event crashed before labels ran.
  const { writes, reads } = await run({ labels: ['bug'], event: 'pull_request_target', action: 'synchronize' });
  assert.equal(writes.length, 1);
  assert.deepEqual(reads, []);
  assert.equal(writes[0].issue_number, 1);
  assert.ok(writes[0].labels.includes('factory:review'));
});

test('local factory lease is preserved while its missing review stage is enrolled', async () => {
  const { writes } = await run({ labels: ['factory:local', 'bug'] });
  assert.equal(writes.length, 1);
  assert.deepEqual(new Set(writes[0].labels),
    new Set(['bug', 'factory', 'factory:review', 'factory:local']));
});

test('complete local lease stays untouched until local session releases it', async () => {
  const { writes } = await run({ labels: ['factory', 'factory:local', 'factory:review'] });
  assert.deepEqual(writes, []);
});

test('mentioning a manual-only marker in documentation does not opt out a PR', async () => {
  const inline = { ...pr(), body: 'To opt out, use `<!-- factory-execution:manual-only -->`.' };
  const documented = { ...pr(), body: 'Example:\n```markdown\n<!-- factory-execution:manual-only -->\n```' };
  for (const item of [inline, documented]) {
    const { writes } = await run({ prs: [item] });
    assert.equal(writes.length, 1);
    assert.ok(writes[0].labels.includes('factory:review'));
  }
});

test('a standalone manual-only directive opts out even with factory labels', async () => {
  const explicit = { ...pr(), body: 'Operator-only work.\n<!-- factory-execution:manual-only -->\n' };
  const { writes } = await run({ prs: [explicit], labels: ['factory'] });
  assert.deepEqual(writes, []);
});

test('a standalone manual-only line opts out of first-time enrollment', async () => {
  const hold = { ...pr(4512), body: 'P0 auth outage rollback.\n\nmanual-only\n' };
  const { writes } = await run({
    prs: [hold],
    labels: [],
    event: 'pull_request_target',
    action: 'opened',
  });
  assert.deepEqual(writes, []);
});

test('a standalone manual-only line is not re-enrolled after humans strip labels', async () => {
  const hold = { ...pr(4512), body: 'Hold this PR.\nmanual-only\n' };
  const { writes } = await run({
    prs: [hold],
    labels: [],
    event: 'pull_request_target',
    action: 'unlabeled',
  });
  assert.deepEqual(writes, []);
});

test('manual-only PR with empty factory labels is not enrolled as factory:unowned', async () => {
  const hold = {
    ...pr(3971),
    body: `${MANUAL_ONLY}\nHold this factory PR.\n`,
  };
  const { writes } = await run({ prs: [hold], labels: ['ralph-status:blocked'] });
  assert.deepEqual(writes, []);
});

test('unlabeled pull_request_target skips enroll when live body has the marker', async () => {
  const hold = { ...pr(3970), body: `${MANUAL_ONLY}\nOperator hold.\n` };
  const { writes, reads, gets } = await run({
    prs: [hold],
    labels: [],
    event: 'pull_request_target',
    action: 'unlabeled',
  });
  assert.deepEqual(reads, []);
  assert.equal(gets.length, 1);
  assert.equal(gets[0].pull_number, 3970);
  assert.deepEqual(writes, []);
});

test('enroll skips when payload body is stale but live fetch has the marker', async () => {
  const stale = { ...pr(3971), body: null };
  const { writes, gets } = await run({
    prs: [stale],
    labels: [],
    event: 'pull_request_target',
    action: 'unlabeled',
    liveByNumber: { 3971: { body: `${MANUAL_ONLY}\nHold this factory PR.\n` } },
  });
  assert.equal(gets.length, 1);
  assert.deepEqual(writes, []);
});

test('ralph-status:blocked alone skips enroll without a manual-only marker', async () => {
  const { writes, gets } = await run({
    prs: [{ ...pr(3970), body: 'Ordinary operator-held PR.' }],
    labels: ['ralph-status:blocked'],
    event: 'pull_request_target',
    action: 'unlabeled',
  });
  assert.equal(gets.length, 1);
  assert.deepEqual(writes, []);
});

test('enroll skips every claim-policy BLOCKED_LABEL without a marker', async () => {
  for (const label of [...BLOCKED_LABELS].sort()) {
    const { writes } = await run({
      prs: [{ ...pr(), body: 'No hold marker in this body.' }],
      labels: [label],
      event: 'pull_request_target',
      action: 'unlabeled',
    });
    assert.deepEqual(writes, [], label);
  }
});

test('enroll skips when the live PR body cannot be read', async () => {
  const { writes, gets } = await run({
    prs: [{ ...pr(), body: 'Looks enrollable from the payload.' }],
    event: 'pull_request_target',
    action: 'unlabeled',
    getError: Object.assign(new Error('Not Found'), { status: 404 }),
  });
  assert.equal(gets.length, 1);
  assert.deepEqual(writes, []);
});

test('enroll skips when live GET omits a readable body string', async () => {
  const { writes } = await run({
    prs: [{ ...pr(), body: 'Looks enrollable from the payload.' }],
    event: 'pull_request_target',
    action: 'unlabeled',
    liveByNumber: { 1: { body: null } },
  });
  assert.deepEqual(writes, []);
});

test('enrollment BLOCKED_LABELS matches factory_work_policy.py', () => {
  const source = fs.readFileSync(path.join(__dirname, 'factory_work_policy.py'), 'utf8');
  const match = source.match(/^BLOCKED_LABELS = \{([^}]+)\}/m);
  assert.ok(match, 'factory_work_policy.BLOCKED_LABELS must remain parseable');
  const fromPolicy = [...match[1].matchAll(/'([^']+)'/g)].map(item => item[1]).sort();
  assert.deepEqual([...BLOCKED_LABELS].sort(), fromPolicy);
});

test('hasManualOnlyDirective stays in lockstep with Python is_manual_only', () => {
  const { spawnSync } = require('node:child_process');
  const repoRoot = path.join(__dirname, '..', '..');
  const cases = [
    'manual-only',
    'P0 rollback.\n\nmanual-only\n',
    '<!-- factory-execution:manual-only -->',
    'The manual-only marker is reserved for human gates.',
    '`manual-only`',
    '```\nmanual-only\n```\nOrdinary work.',
    'To opt out, use `<!-- factory-execution:manual-only -->`.',
  ];
  for (const body of cases) {
    const py = spawnSync('python3', [
      '-c',
      'import sys; sys.path.insert(0, "scripts"); from factory_eligibility import is_manual_only; print("1" if is_manual_only(sys.stdin.read()) else "0")',
    ], { input: body, encoding: 'utf8', cwd: repoRoot });
    assert.equal(py.status, 0, py.stderr);
    assert.equal(
      hasManualOnlyDirective(body),
      py.stdout.trim() === '1',
      body,
    );
  }
});

test('clean scheduled sweep performs one PR list read and no other GitHub API work', async () => {
  const factoryLabels = [{ name: 'factory' }, { name: 'factory:ready' }, { name: 'factory:unowned' }];
  const { writes, reads, gets, labelReads, repoLabelReads } = await run({
    prs: [{ ...pr(1), labels: factoryLabels }, { ...pr(2), labels: factoryLabels }],
    labels: ['factory', 'factory:ready', 'factory:unowned'],
  });
  assert.equal(reads.length, 1, 'a short clean list needs only one REST request');
  assert.deepEqual(gets, []);
  assert.deepEqual(labelReads, []);
  assert.equal(repoLabelReads, 0);
  assert.deepEqual(writes, []);
});

test('scheduled sweep only inspects PRs actually missing the factory label', async () => {
  const { writes, reads, gets, labelReads, repoLabelReads } = await run({
    prs: [
      { ...pr(1), labels: [{ name: 'factory' }, { name: 'factory:review' }, { name: 'factory:unowned' }] },
      { ...pr(2), labels: [{ name: 'bug' }] },
    ],
    labels: ['bug'],
  });
  assert.equal(reads.length, 1);
  assert.deepEqual(gets.map(request => request.pull_number), [2]);
  assert.deepEqual(labelReads, [2, 2]);
  assert.equal(repoLabelReads, 1, 'label definitions are checked once when a repair is needed');
  assert.deepEqual(writes.map(write => write.issue_number), [2]);
});

test('scheduled sweep repairs partially labeled PRs while skipping complete PRs', async () => {
  const { writes, gets } = await run({
    prs: [
      { ...pr(1), labels: [{ name: 'factory' }, { name: 'factory:review' }, { name: 'factory:unowned' }] },
      { ...pr(2), labels: [{ name: 'factory' }, { name: 'factory:local' }] },
    ],
    labels: ['factory', 'factory:local'],
  });
  assert.deepEqual(gets.map(request => request.pull_number), [2]);
  assert.deepEqual(writes.map(write => write.issue_number), [2]);
  assert.ok(writes[0].labels.includes('factory:review'));
});

test('full first page keeps looking for older unlabeled PRs without re-fetching page one', async () => {
  const labeled = Array.from({ length: 100 }, (_, index) => ({
    ...pr(index + 1), labels: [{ name: 'factory' }, { name: 'factory:review' }, { name: 'factory:unowned' }],
  }));
  const { writes, reads, gets } = await run({
    prs: [...labeled, { ...pr(101), labels: [{ name: 'bug' }] }],
    labels: ['bug'],
  });
  assert.deepEqual(reads.map(request => request.page), [1, 2]);
  assert.deepEqual(gets.map(request => request.pull_number), [101]);
  assert.deepEqual(writes.map(write => write.issue_number), [101]);
});

test('PR synchronize still reconciles live merge-conflict metadata on an already-enrolled PR', async () => {
  const { writes, reads, gets } = await run({
    prs: [{ ...pr(), labels: [{ name: 'factory' }] }],
    labels: ['factory', 'factory:review', 'factory:unowned'],
    event: 'pull_request_target',
    action: 'synchronize',
    liveByNumber: { 1: { mergeable: false, mergeable_state: 'dirty' } },
  });
  assert.deepEqual(reads, []);
  assert.deepEqual(gets.map(request => request.pull_number), [1]);
  assert.equal(writes.length, 1);
  assert.ok(writes[0].labels.includes('factory:merge-conflict'));
});

test('scheduled sweep paginates all open PRs', async () => {
  const { writes, reads } = await run({ prs: [pr(1), pr(2)] });
  assert.deepEqual(writes.map(input => input.issue_number), [1, 2]);
  assert.equal(reads[0].state, 'open');
  assert.equal(reads[0].per_page, 100);
});

test('missing factory label preserves current owner and advanced stage', async () => {
  const { writes } = await run({ labels: ['factory:59', 'factory:ready', 'bug'] });
  assert.deepEqual(new Set(writes[0].labels),
    new Set(['bug', 'factory', 'factory:59', 'factory:ready']));
});

test('complete metadata is left untouched', async () => {
  const { writes } = await run({ labels: ['factory', 'factory:ci', 'factory:unowned'] });
  assert.deepEqual(writes, []);
});

test('GitHub mergeability derives factory:merge-conflict on a complete PR', async () => {
  const { writes } = await run({
    labels: ['factory', 'factory:review', 'factory:unowned'],
    liveByNumber: { 1: { mergeable: false, mergeable_state: 'dirty' } },
  });
  assert.equal(writes.length, 1);
  assert.deepEqual(new Set(writes[0].labels),
    new Set(['factory', 'factory:review', 'factory:unowned', 'factory:merge-conflict']));
});

test('a clean head clears the derived conflict label', async () => {
  const { writes } = await run({
    labels: ['factory', 'factory:review', 'factory:unowned', 'factory:merge-conflict', 'bug'],
    liveByNumber: { 1: { mergeable: true, mergeable_state: 'clean' } },
  });
  assert.equal(writes.length, 1);
  assert.deepEqual(new Set(writes[0].labels),
    new Set(['bug', 'factory', 'factory:review', 'factory:unowned']));
});

test('unresolved mergeability preserves the derived label without a write', async () => {
  for (const live of [
    { mergeable: null, mergeable_state: 'unknown' },
    { mergeable: 'UNKNOWN', mergeStateStatus: 'UNKNOWN' },
    {},
  ]) {
    const { writes } = await run({
      labels: ['factory', 'factory:review', 'factory:unowned', 'factory:merge-conflict'],
      liveByNumber: { 1: live },
    });
    assert.deepEqual(writes, [], `unknown mergeability must never erase the label: ${JSON.stringify(live)}`);
  }
});

test('conflict derivation never consumes the stage slot', async () => {
  const { writes } = await run({
    labels: ['factory', 'factory:local'],
    liveByNumber: { 1: { mergeable: false, mergeable_state: 'dirty' } },
  });
  assert.equal(writes.length, 1);
  const applied = new Set(writes[0].labels);
  assert.ok(applied.has('factory:merge-conflict'));
  assert.ok(applied.has('factory:review'));
  assert.equal([...applied].filter(label => label === 'factory:review').length, 1);

  const advanced = await run({
    labels: ['factory', 'factory:47', 'factory:changes-requested'],
    liveByNumber: { 1: { mergeable: false, mergeable_state: 'dirty' } },
  });
  assert.equal(advanced.writes.length, 1);
  const advancedLabels = new Set(advanced.writes[0].labels);
  assert.ok(advancedLabels.has('factory:changes-requested'));
  assert.ok(advancedLabels.has('factory:merge-conflict'));
  assert.ok(!advancedLabels.has('factory:review'));
});

test('derived label never substitutes for factory enrollment metadata', async () => {
  const { writes } = await run({
    labels: ['bug', 'factory:merge-conflict'],
    liveByNumber: { 1: { mergeable: false, mergeable_state: 'dirty' } },
  });
  // The sweep still enrolls an unlabeled PR, but the derived label alone is not
  // a factory lease or stage: exactly one stage and one owner are written.
  assert.equal(writes.length, 1);
  const applied = new Set(writes[0].labels);
  assert.ok(applied.has('factory'));
  assert.ok(applied.has('factory:review'));
  assert.ok(applied.has('factory:unowned'));
});

test('dependency bots, deleted heads, drafts, and closed PRs are excluded', async () => {
  const { writes } = await run({ prs: [{ ...pr(), head: { sha, repo: null } }, { ...pr(), draft: true },
    { ...pr(), state: 'closed' }, { ...pr(), user: { login: 'dependabot[bot]' } },
    { ...pr(), user: { login: 'renovate[bot]' } }] });
  assert.deepEqual(writes, []);
});

test('only trusted current-head review verdicts recover a missing stage', async () => {
  const comment = (head, verdict, association, day) => ({
    body: `<!-- comic-pile-factory-review-v2:${head}:${verdict} -->`,
    author_association: association, created_at: `2026-09-${day}T00:00:00Z`,
  });
  const { writes } = await run({ comments: [
    comment(sha, 'pass', 'OWNER', 20),
    comment('b'.repeat(40), 'changes-required', 'OWNER', 21),
    comment(sha, 'changes-required', 'NONE', 22),
  ] });
  assert.ok(writes[0].labels.includes('factory:ci'));
});

test('current-head changes-required verdict requests repair', async () => {
  const { writes } = await run({ comments: [{
    body: `<!-- comic-pile-factory-review-v2:${sha}:changes-required -->`,
    author_association: 'OWNER', created_at: '2026-09-20T00:00:00Z',
  }] });
  assert.ok(writes[0].labels.includes('factory:changes-requested'));
});

for (const event of ['schedule', 'pull_request_target']) {
  test(`${event} enrolls a first-time fork with the normal review gates`, async () => {
    const fork = { ...pr(), body: 'Closes #3893', user: { login: 'first-timer' },
      head: { sha, ref: 'fix/issue-3893', repo: { full_name: 'first-timer/comic-pile' } } };
    const { writes } = await run({ prs: [fork], event, labels: ['bug'] });
    assert.deepEqual(new Set(writes[0].labels),
      new Set(['bug', 'factory', 'factory:review', 'factory:unowned']));
  });
}

for (const previous of ['factory:ci', 'factory:ready']) {
  test(`a contributor push invalidates ${previous} while preserving ownership`, async () => {
    const fork = { ...pr(), head: { sha, repo: { full_name: 'contributor/comic-pile' } } };
    const { writes } = await run({ prs: [fork], event: 'pull_request_target', action: 'synchronize',
      labels: ['factory', previous, 'factory:local', 'bug'] });
    assert.deepEqual(new Set(writes[0].labels),
      new Set(['bug', 'factory', 'factory:review', 'factory:local']));
  });
}

test('enrollment keeps local+ready when the lease lands between decision and write', async () => {
  // Regression for PR #5393: the enrollment read happened while factory:local
  // was momentarily absent, so it computed unowned+review; the write-boundary
  // re-read saw the live local lease + ready and must not discard them.
  const writes = [];
  const labelReads = [
    ['factory', 'factory:review'], // stale decision read (no local lease yet)
    ['factory', 'factory:local', 'factory:ready'], // live read at the write boundary
  ];
  let readCount = 0;
  const issues = {
    listLabelsForRepo: async () => ({ data: [], headers: {}, status: 200 }),
    createLabel: async () => ({ data: {}, headers: {}, status: 201 }),
    listLabelsOnIssue: async () => ({
      data: (labelReads[Math.min(readCount++, labelReads.length - 1)]).map(name => ({ name })),
      headers: {},
      status: 200,
    }),
    listComments: async () => ({ data: [], headers: {}, status: 200 }),
    async setLabels(input) { writes.push(input); return { data: {}, headers: {}, status: 200 }; },
  };
  const pulls = {
    async list() { return { data: [pr()], headers: {}, status: 200 }; },
    async get({ pull_number }) {
      return {
        data: {
          ...pr(pull_number),
          mergeable: true,
          mergeable_state: 'clean',
          head: { sha, repo: { full_name: 'JoshCLWren/comic-pile' }, ref: 'local-factory/x' },
        },
        headers: {},
        status: 200,
      };
    },
  };
  await reconcileMissingPrLabels({
    github: { rest: { issues, pulls } },
    context: {
      eventName: 'pull_request_target',
      repo: { owner: 'JoshCLWren', repo: 'comic-pile' },
      payload: { pull_request: pr(), action: 'unlabeled' },
    },
  });
  assert.deepEqual(writes, [], 'a live local lease at the write boundary must not be reverted');
});

test('enrollment still defaults missing ownership to unowned without a local lease', async () => {
  // Control: the same stale read without any local lease keeps the old behavior.
  const writes = [];
  const issues = {
    listLabelsForRepo: async () => ({ data: [], headers: {}, status: 200 }),
    createLabel: async () => ({ data: {}, headers: {}, status: 201 }),
    listLabelsOnIssue: async () => ({
      data: ['factory', 'factory:review'].map(name => ({ name })),
      headers: {},
      status: 200,
    }),
    listComments: async () => ({ data: [], headers: {}, status: 200 }),
    async setLabels(input) { writes.push(input); return { data: {}, headers: {}, status: 200 }; },
  };
  const pulls = {
    async list() { return { data: [pr()], headers: {}, status: 200 }; },
    async get({ pull_number }) {
      return {
        data: {
          ...pr(pull_number),
          mergeable: true,
          mergeable_state: 'clean',
          head: { sha, repo: { full_name: 'JoshCLWren/comic-pile' }, ref: 'local-factory/x' },
        },
        headers: {},
        status: 200,
      };
    },
  };
  await reconcileMissingPrLabels({
    github: { rest: { issues, pulls } },
    context: {
      eventName: 'pull_request_target',
      repo: { owner: 'JoshCLWren', repo: 'comic-pile' },
      payload: { pull_request: pr(), action: 'unlabeled' },
    },
  });
  assert.equal(writes.length, 1);
  assert.ok(writes[0].labels.includes('factory:unowned'));
  assert.ok(writes[0].labels.includes('factory:review'));
});

test('enrollment push invalidates ready under a local lease but keeps the owner', async () => {
  const writes = [];
  const issues = {
    listLabelsForRepo: async () => ({ data: [], headers: {}, status: 200 }),
    createLabel: async () => ({ data: {}, headers: {}, status: 201 }),
    listLabelsOnIssue: async () => ({
      data: ['factory', 'factory:local', 'factory:ready'].map(name => ({ name })),
      headers: {},
      status: 200,
    }),
    listComments: async () => ({ data: [], headers: {}, status: 200 }),
    async setLabels(input) { writes.push(input); return { data: {}, headers: {}, status: 200 }; },
  };
  const pulls = {
    async list() { return { data: [pr()], headers: {}, status: 200 }; },
    async get({ pull_number }) {
      return {
        data: {
          ...pr(pull_number),
          mergeable: true,
          mergeable_state: 'clean',
          head: { sha, repo: { full_name: 'JoshCLWren/comic-pile' }, ref: 'local-factory/x' },
        },
        headers: {},
        status: 200,
      };
    },
  };
  await reconcileMissingPrLabels({
    github: { rest: { issues, pulls } },
    context: {
      eventName: 'pull_request_target',
      repo: { owner: 'JoshCLWren', repo: 'comic-pile' },
      payload: { pull_request: pr(), action: 'synchronize' },
    },
  });
  assert.equal(writes.length, 1);
  assert.ok(writes[0].labels.includes('factory:local'));
  assert.ok(writes[0].labels.includes('factory:review'));
  assert.ok(!writes[0].labels.includes('factory:ready'));
});
