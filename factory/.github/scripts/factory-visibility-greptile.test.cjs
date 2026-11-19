const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const reconcile = require('./factory-visibility.cjs');
const {
  trusted,
  isRepairRoutingReviewer,
  unresolvedReviewerBotThreads,
  greptileThreadState,
  greptileSummaryFindings,
  isGreptileAck,
  isTrustedResponder,
  summaryFindingId,
  REPAIR_ROUTING_REVIEWER_BOTS,
  TRUSTED_APP_SLUGS,
} = reconcile._test;

const HEAD = 'a'.repeat(40);
const OLD_HEAD = 'b'.repeat(40);
let nextId = 1;

function comment(login, body = 'finding', extra = {}) {
  const id = nextId++;
  return { databaseId: id, url: `https://github.com/x/pull/100#discussion_r${id}`, author: { login }, body, createdAt: '2026-10-18T15:00:00Z', ...extra };
}

// The wrapper posts outcomes as github-actions[bot]: GraphQL actor type Bot.
function actions(body, extra = {}) {
  return comment('github-actions', body, { ...extra, author: { login: 'github-actions', __typename: 'Bot' } });
}

// GraphQL-shaped thread: firstComment + recent window (oldest first).
function thread({ login = 'greptile-apps', isResolved = false, isOutdated = false, replies = [], first, totalCount } = {}) {
  const head = first || comment(login, '**Bug** here');
  const nodes = [head, ...replies];
  return {
    id: `PRRT_${head.databaseId}`,
    isResolved,
    isOutdated,
    firstComment: { nodes: [{ databaseId: head.databaseId, url: head.url, author: head.author }] },
    recent: { totalCount: totalCount ?? nodes.length, nodes },
  };
}

function summaryBody({ commit = HEAD, findings = [] } = {}) {
  const items = findings.map((f, i) => `${i + 1}. <img alt="P1" src="https://x/p1.svg" align="top">&nbsp;**${f.title}** ${f.discussionId ? `<a href="https://github.com/JoshCLWren/comic-pile/pull/100#discussion_r${f.discussionId}">▶</a>` : ''}`);
  return [
    'Author text mentioning `<!-- greptile_comment -->` inline.',
    '<!-- greptile_comment -->',
    '<h2>Findings</h2>',
    '',
    ...items,
    '',
    `<sub>Reviews (3) · Last reviewed commit: ["fix"](https://github.com/joshclwren/comic-pile/commit/${commit}) · Reviewed by Greptile</sub>`,
    '<!-- /greptile_comment -->',
  ].join('\n');
}

function githubFor({
  labels = ['factory', 'factory:review', 'factory:unowned'],
  threads = [],
  threadPages,
  liveHead = HEAD,
  body = '',
  checkRuns = [],
  reviews = [],
  issueComments = [],
  fullComments = {},
  setLabels,
  graphqlCalls,
  pullCalls,
} = {}) {
  const pages = threadPages || [{ nodes: threads }];
  const api = {
    listLabelsForRepo: async () => ({ data: [], headers: {}, status: 200 }),
    listLabelsOnIssue: async () => ({ data: labels.map(name => ({ name })), headers: {}, status: 200 }),
    listComments: async () => ({ data: issueComments, headers: {}, status: 200 }),
    createLabel: async () => ({ data: {}, headers: {}, status: 201 }),
    updateLabel: async () => ({ data: {}, headers: {}, status: 200 }),
    setLabels: async params => {
      if (setLabels) setLabels(params);
      return { data: {}, headers: {}, status: 200 };
    },
  };
  return {
    graphql: async (query, variables) => {
      if (graphqlCalls) graphqlCalls.push({ query, variables });
      if (variables.id) {
        const nodes = fullComments[variables.id];
        return { node: { comments: { nodes: nodes || null, pageInfo: { hasNextPage: false, endCursor: null } } } };
      }
      const index = variables.after ? Number(variables.after.slice('cursor-'.length)) : 0;
      const page = pages[index];
      const hasNextPage = index + 1 < pages.length || page.hasNextPage === true;
      const pageInfo = 'pageInfo' in page
        ? page.pageInfo
        : { hasNextPage, endCursor: hasNextPage ? `cursor-${index + 1}` : null };
      return {
        repository: {
          pullRequest: {
            reviewThreads: { nodes: page.nodes, ...(pageInfo === undefined ? {} : { pageInfo }) },
          },
        },
      };
    },
    rest: {
      issues: api,
      checks: { listForRef: async () => ({ data: { check_runs: checkRuns } }) },
      pulls: {
        listReviews: async ({ page }) => ({
          data: page === 1 ? reviews : [], headers: {}, status: 200,
        }),
        get: async (params) => {
          if (pullCalls) pullCalls.push(params);
          return {
            data: {
              number: params.pull_number,
              state: 'open',
              body,
              head: { sha: liveHead, ref: 'factory/49-3154-greptile', repo: { full_name: 'JoshCLWren/comic-pile' } },
            },
          };
        },
      },
    },
  };
}

function greptileReview(overrides = {}) {
  return {
    id: 1,
    author_association: 'NONE',
    user: { login: 'greptile-apps[bot]', type: 'Bot' },
    state: 'COMMENTED',
    commit_id: HEAD,
    body: '',
    ...overrides,
  };
}

function reviewContext(review, { headSha = HEAD } = {}) {
  return {
    eventName: 'pull_request_review',
    repo: { owner: 'JoshCLWren', repo: 'comic-pile' },
    payload: {
      action: 'submitted',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        number: 100,
        head: { ref: 'factory/49-3154-greptile', sha: headSha, repo: { full_name: 'JoshCLWren/comic-pile' } },
        labels: [{ name: 'factory' }, { name: 'factory:review' }, { name: 'factory:unowned' }],
      },
      review,
    },
  };
}

function editedContext(body, { changes = { body: { from: 'old' } } } = {}) {
  return {
    eventName: 'pull_request_target',
    repo: { owner: 'JoshCLWren', repo: 'comic-pile' },
    payload: {
      action: 'edited',
      changes,
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: {
        number: 100,
        body,
        head: { ref: 'factory/49-3154-greptile', sha: HEAD, repo: { full_name: 'JoshCLWren/comic-pile' } },
        labels: [{ name: 'factory' }, { name: 'factory:review' }],
      },
    },
  };
}

async function runWith(context, options = {}) {
  const calls = [];
  const graphqlCalls = [];
  const pullCalls = [];
  const github = githubFor({ ...options, setLabels: async input => calls.push(input), graphqlCalls, pullCalls });
  await reconcile({ github, context });
  return { calls, graphqlCalls, pullCalls };
}

const run = (review, options = {}) => runWith(reviewContext(review, options), options);
const routed = calls => calls.length === 1 && calls[0].labels.includes('factory:changes-requested');

test('reviewer-bot allowlist is separate from marker trust', () => {
  assert.deepEqual([...REPAIR_ROUTING_REVIEWER_BOTS], ['greptile-apps[bot]']);
  assert.equal(isRepairRoutingReviewer(greptileReview()), true);
  // Greptile is a routing signal only; it must never become a trusted marker author.
  assert.equal(trusted(greptileReview()), false);
  assert.equal(trusted({ ...greptileReview(), body: '<!-- comic-pile-factory-ready-v1:x -->' }), false);
  assert.equal(TRUSTED_APP_SLUGS.has('greptile-apps'), false);
  assert.equal(isRepairRoutingReviewer({ user: { login: 'greptile-apps' } }), false);
  assert.equal(isRepairRoutingReviewer({ user: { login: 'coderabbitai[bot]' } }), false);
  assert.equal(trusted({ author_association: 'NONE', user: { login: 'coderabbitai[bot]' } }), false);
});

function rabbitReview({ commit = HEAD, body = '', user = { login: 'coderabbitai[bot]', type: 'Bot' } } = {}) {
  return { id: 3, user, state: 'COMMENTED', commit_id: commit, body, submitted_at: '2026-11-02T17:12:03Z' };
}

test('Retired CodeRabbit inline and summary findings never route the factory', async () => {
  const review = rabbitReview({ body: 'Outside diff range comments (1)' });
  const { calls, pullCalls } = await run(review, {
    reviews: [review],
    threads: [thread({ login: 'coderabbitai' })],
  });
  assert.equal(calls.length, 0);
  // Only the live PR read is necessary; no CodeRabbit REST review scan.
  assert.equal(pullCalls.length, 1);
});

test('Greptile COMMENTED review with an open finding routes to changes-requested', async () => {
  const { calls, graphqlCalls, pullCalls } = await run(greptileReview(), { threads: [thread()] });
  assert.equal(pullCalls.length, 1);
  assert.equal(graphqlCalls.length, 1);
  assert.equal(graphqlCalls[0].variables.number, 100);
  assert.ok(routed(calls));
  assert.equal(calls[0].issue_number, 100);
  assert.ok(!calls[0].labels.includes('factory:review'));
  assert.ok(calls[0].labels.includes('factory:unowned'));
});

test('Greptile REST login suffix is also matched in GraphQL thread authors', async () => {
  const { calls } = await run(greptileReview(), { threads: [thread({ login: 'greptile-apps[bot]' })] });
  assert.ok(routed(calls));
});

test('Greptile findings beyond the first thread page still route', async () => {
  const { calls, graphqlCalls } = await run(greptileReview(), {
    threadPages: [
      { nodes: Array.from({ length: 100 }, () => thread({ isResolved: true })) },
      { nodes: [thread()] },
    ],
  });
  assert.equal(graphqlCalls.length, 2);
  assert.equal(graphqlCalls[0].variables.after, null);
  assert.equal(graphqlCalls[1].variables.after, 'cursor-1');
  assert.ok(routed(calls));
});

test('truncated thread pagination with no findings seen fails closed instead of skipping', async () => {
  for (const truncated of [
    { nodes: [thread({ isResolved: true })], pageInfo: { hasNextPage: true, endCursor: null } },
    { nodes: [thread({ isResolved: true })], pageInfo: undefined },
    { nodes: null, pageInfo: { hasNextPage: false, endCursor: null } },
  ]) {
    const calls = [];
    const github = githubFor({ threadPages: [truncated], setLabels: async input => calls.push(input) });
    await assert.rejects(
      reconcile({ github, context: reviewContext(greptileReview()) }),
      /review-thread pagination was incomplete/,
    );
    assert.equal(calls.length, 0);
  }
});

test('truncated thread pagination still routes when an open Greptile finding was already seen', async () => {
  const { calls } = await run(greptileReview(), {
    threadPages: [{ nodes: [thread()], pageInfo: { hasNextPage: true, endCursor: null } }],
  });
  assert.ok(routed(calls));
});

test('thread counting reports incomplete when the page limit is exhausted', async () => {
  const github = githubFor({
    threadPages: [{ nodes: [thread({ isResolved: true })] }, { nodes: [thread()] }],
  });
  const context = reviewContext(greptileReview());
  assert.deepEqual(
    await unresolvedReviewerBotThreads(github, context, 100, { pageLimit: 1 }),
    { count: 0, complete: false },
  );
  assert.deepEqual(
    await unresolvedReviewerBotThreads(github, context, 100),
    { count: 1, complete: true },
  );
});

test('routing is state-based: a stale payload still routes from live open findings', async () => {
  // A replaced or late run carries an old review payload; routing reads the
  // live PR head and threads instead of trusting the payload.
  const { calls, pullCalls } = await run(greptileReview({ commit_id: OLD_HEAD }), {
    headSha: OLD_HEAD,
    threads: [thread()],
  });
  assert.equal(pullCalls.length, 1);
  assert.ok(routed(calls));
});

test('a non-Greptile review (e.g. a worker thread reply) also recomputes routing', async () => {
  const reply = { author_association: 'NONE', user: { login: 'github-actions[bot]' }, state: 'COMMENTED', performed_via_github_app: { slug: 'github-actions' } };
  assert.ok(routed((await run(reply, { threads: [thread()] })).calls));
  assert.equal((await run(reply, { threads: [] })).calls.length, 0);
});

test('resolved, outdated, or other-author threads do not route', async () => {
  const { calls } = await run(greptileReview(), {
    threads: [
      thread({ isResolved: true }),
      thread({ isOutdated: true }),
      thread({ login: 'some-human' }),
      thread({ login: 'pr-agent[bot]' }),
      thread({ login: 'other-reviewer' }),
    ],
  });
  assert.equal(calls.length, 0);
});

test('open-thread rule: acknowledgement and trusted answers close a finding; pushback reopens it', () => {
  const cases = [
    // #3890: Greptile accepted the owner rebuttal.
    [[comment('JoshCLWren', 'Rebuttal, no code change.', { authorAssociation: 'OWNER' }), comment('greptile-apps', 'That addresses the concern. The caps are the intended guard.')], 'acknowledged'],
    [[actions('x'), comment('greptile-apps', "<p>You\u2019re right. That finding was based on reverted code.</p>")], 'acknowledged'],
    [[actions('x'), comment('greptile-apps', 'Good catch.')], 'acknowledged'],
    // Polite pushback is not acceptance (Greptile finding on #3899).
    [[actions('x'), comment('greptile-apps', 'Thanks for the rebuttal, but the bug remains.')], 'open'],
    [[actions('x'), comment('greptile-apps', 'Good point, however the retry is still unbounded.')], 'open'],
    [[actions('x'), comment('greptile-apps', "You're right that it is capped, but it isn't bounded.")], 'open'],
    // Wrapper/owner reply after Greptile's last comment: answered, awaiting Greptile.
    [[actions('**Fixed** in abc')], 'answered'],
    [[comment('JoshCLWren', 'Rebuttal', { authorAssociation: 'OWNER' })], 'answered'],
    // Look-alike logins are never trusted without a role or the Bot actor.
    [[comment('opencode-free-model-factory-54', 'Rebuttal')], 'open'],
    [[comment('opencode-testfactory', 'Rebuttal')], 'open'],
    [[comment('github-actions', 'Rebuttal')], 'open'],
    // Untrusted replies never close a finding.
    [[comment('random-user', 'not a bug', { authorAssociation: 'NONE' })], 'open'],
    // Greptile pushed back after the answer.
    [[comment('JoshCLWren', 'no', { authorAssociation: 'OWNER' }), comment('greptile-apps', 'I still think this is broken because...')], 'open'],
    [[], 'open'],
  ];
  for (const [replies, expected] of cases) {
    const node = thread({ replies });
    const normalized = {
      isResolved: node.isResolved,
      isOutdated: node.isOutdated,
      firstComment: node.firstComment.nodes[0],
      comments: node.recent.nodes,
    };
    assert.equal(greptileThreadState(normalized), expected, JSON.stringify(replies.map(r => r.body)));
  }
  // The first Greptile comment is a finding, never an acknowledgement.
  const ackLooking = thread({ first: comment('greptile-apps', 'Thanks for the PR, but this leaks') });
  assert.equal(greptileThreadState({ firstComment: ackLooking.firstComment.nodes[0], comments: ackLooking.recent.nodes }), 'open');
  assert.equal(isGreptileAck("I don't think that addresses it"), false);
});

test('Greptile reply accepting a rebuttal does not re-route (#3890 flapping)', async () => {
  const accepted = thread({ replies: [
    comment('JoshCLWren', 'Rebuttal with evidence', { authorAssociation: 'OWNER' }),
    comment('greptile-apps', 'That addresses the concern.'),
  ] });
  const { calls } = await run(greptileReview(), { threads: [accepted] });
  assert.equal(calls.length, 0);
});

test('Greptile APPROVED with no open findings never mutates labels', async () => {
  const { calls } = await run(greptileReview({ state: 'APPROVED' }), { threads: [] });
  assert.equal(calls.length, 0);
});

test('an already-routed PR is not rewritten', async () => {
  const { calls } = await run(greptileReview(), {
    labels: ['factory', 'factory:changes-requested', 'factory:unowned'],
    threads: [thread()],
  });
  assert.equal(calls.length, 0);
});

test('manual-only PR with open Greptile findings is not labeled factory:unowned', async () => {
  const { calls } = await run(greptileReview(), {
    threads: [thread()],
    body: '<!-- factory-execution:manual-only -->\nHold this factory PR.\n',
  });
  assert.equal(calls.length, 0);
});

test('manual-only PR is not claimed by trusted CHANGES_REQUESTED routing', async () => {
  const context = reviewContext({
    author_association: 'NONE',
    user: { login: 'github-actions[bot]' },
    state: 'CHANGES_REQUESTED',
    performed_via_github_app: { slug: 'github-actions' },
  });
  context.payload.pull_request.body = '<!-- factory-execution:manual-only -->\nHold.\n';
  const { calls } = await runWith(context);
  assert.equal(calls.length, 0);
});

test('standalone manual-only line is not claimed by trusted CHANGES_REQUESTED routing', async () => {
  const context = reviewContext({
    author_association: 'NONE',
    user: { login: 'github-actions[bot]' },
    state: 'CHANGES_REQUESTED',
    performed_via_github_app: { slug: 'github-actions' },
  });
  context.payload.pull_request.body = 'Hold this PR.\nmanual-only\n';
  const { calls } = await runWith(context);
  assert.equal(calls.length, 0);
});

test('trusted CHANGES_REQUESTED routing is unchanged and does not query threads', async () => {
  const { calls, graphqlCalls } = await run({
    author_association: 'NONE',
    user: { login: 'github-actions[bot]' },
    state: 'CHANGES_REQUESTED',
    performed_via_github_app: { slug: 'github-actions' },
  });
  assert.ok(routed(calls));
  assert.equal(graphqlCalls.length, 0);
});

test('summary parser reads the reviewed commit and finding links, not inline mentions', () => {
  const parsed = greptileSummaryFindings(summaryBody({ findings: [{ title: 'Caps unbounded', discussionId: '42' }, { title: 'No thread' }] }));
  assert.equal(parsed.commit, HEAD);
  assert.deepEqual(parsed.findings, [
    { title: 'Caps unbounded', discussionId: '42' },
    { title: 'No thread', discussionId: null },
  ]);
  assert.equal(greptileSummaryFindings('no summary `<!-- greptile_comment -->`'), null);
});

test('summary-only Greptile re-review routes on a PR-body edit for the live head', async () => {
  const body = summaryBody({ findings: [{ title: 'Summary-only finding' }] });
  const { calls } = await runWith(editedContext(body), { body, threads: [] });
  assert.ok(routed(calls));
});

test('summary for an older commit, or with no findings, does not route', async () => {
  for (const body of [summaryBody({ commit: OLD_HEAD, findings: [{ title: 'Old' }] }), summaryBody({ findings: [] })]) {
    const { calls } = await runWith(editedContext(body), { body, threads: [] });
    assert.equal(calls.length, 0);
  }
});

test('summary findings linked to acknowledged or freshly answered threads do not route', async () => {
  const acked = thread({ replies: [comment('JoshCLWren', 'rebut', { authorAssociation: 'OWNER' }), comment('greptile-apps', "You're right.")] });
  const answered = thread({ replies: [actions('**Rebuttal**', { createdAt: '2026-10-18T16:10:00Z' })] });
  const body = summaryBody({ findings: [
    { title: 'Acked', discussionId: String(acked.firstComment.nodes[0].databaseId) },
    { title: 'Answered after review', discussionId: String(answered.firstComment.nodes[0].databaseId) },
  ] });
  const { calls } = await runWith(editedContext(body), {
    body,
    threads: [acked, answered],
    checkRuns: [{ name: 'Greptile Review', completed_at: '2026-10-18T16:05:00Z' }],
  });
  assert.equal(calls.length, 0);
});

test('summary finding re-listed after the answer (Greptile re-reviewed later) routes', async () => {
  const answered = thread({ isResolved: true, replies: [actions('**Fixed** in abc', { createdAt: '2026-10-18T16:00:00Z' })] });
  const body = summaryBody({ findings: [{ title: 'Still broken', discussionId: String(answered.firstComment.nodes[0].databaseId) }] });
  const { calls } = await runWith(editedContext(body), {
    body,
    threads: [answered],
    checkRuns: [{ name: 'Greptile Review', completed_at: '2026-10-18T16:05:00Z' }],
  });
  assert.ok(routed(calls));
});

test('PR-body edits without a body change or Greptile summary make no API calls', async () => {
  for (const context of [
    editedContext(summaryBody({ findings: [{ title: 'x' }] }), { changes: { title: { from: 'old' } } }),
    editedContext('plain body mentioning `<!-- greptile_comment -->` inline'),
  ]) {
    const { calls, graphqlCalls, pullCalls } = await runWith(context, { threads: [thread()] });
    assert.equal(calls.length + graphqlCalls.length + pullCalls.length, 0);
  }
});

test('responder trust needs a repository role or the github-actions Bot actor', () => {
  assert.equal(isTrustedResponder({ author: { login: 'github-actions', __typename: 'Bot' } }), true);
  assert.equal(isTrustedResponder({ author: { login: 'x' }, authorAssociation: 'MEMBER' }), true);
  assert.equal(isTrustedResponder({ author: { login: 'github-actions', __typename: 'User' } }), false);
  assert.equal(isTrustedResponder({ author: { login: 'opencode-free-model-factory-54', __typename: 'Bot' } }), false);
});

test('a long discussion that pushes Greptile out of the recent window reads every comment', async () => {
  const accepted = thread({
    replies: Array.from({ length: 20 }, (_, i) => comment(`drive-by-${i}`, 'noise')),
    totalCount: 23,
  });
  const full = [
    accepted.recent.nodes[0],
    comment('JoshCLWren', 'Rebuttal', { authorAssociation: 'OWNER' }),
    comment('greptile-apps', 'That addresses the concern.'),
    ...accepted.recent.nodes.slice(1),
  ];
  // The last-20 window holds only the trailing noise.
  accepted.recent.nodes = accepted.recent.nodes.slice(1);
  // Window shows only untrusted noise; the full thread shows an accepted answer.
  const { calls, graphqlCalls } = await run(greptileReview(), {
    threads: [accepted],
    fullComments: { [accepted.id]: full },
  });
  assert.equal(calls.length, 0);
  assert.ok(graphqlCalls.some(call => call.variables.id === accepted.id));
  // If the full read fails, the finding stays open (fail closed).
  const failed = await run(greptileReview(), { threads: [accepted] });
  assert.ok(routed(failed.calls));
});

test('summary-only finding answered by a trusted outcome comment after the review does not route', async () => {
  const id = summaryFindingId('Summary-only finding');
  assert.equal(id, 'GREPTILE_SUMMARY_summary-only-finding');
  const body = summaryBody({ findings: [{ title: 'Summary-only finding' }] });
  const answer = {
    user: { login: 'github-actions[bot]', type: 'Bot' },
    author_association: 'NONE',
    created_at: '2026-10-18T16:10:00Z',
    body: `<!-- comic-pile-factory-greptile-outcome-v1:${id}:rebutted:${HEAD}:abc -->`,
  };
  const checkRuns = [{ name: 'Greptile Review', completed_at: '2026-10-18T16:05:00Z' }];
  assert.equal((await runWith(editedContext(body), { body, threads: [], checkRuns, issueComments: [answer] })).calls.length, 0);
  // Greptile re-reviewed after the answer and still lists it.
  const late = [{ name: 'Greptile Review', completed_at: '2026-10-18T16:20:00Z' }];
  assert.ok(routed((await runWith(editedContext(body), { body, threads: [], checkRuns: late, issueComments: [answer] })).calls));
  // Untrusted look-alike comments never answer it.
  const forged = { ...answer, user: { login: 'drive-by', type: 'User' } };
  assert.ok(routed((await runWith(editedContext(body), { body, threads: [], checkRuns, issueComments: [forged] })).calls));
});

test('a thread reply (pull_request_review_comment) recomputes routing', async () => {
  const context = {
    eventName: 'pull_request_review_comment',
    repo: { owner: 'JoshCLWren', repo: 'comic-pile' },
    payload: {
      action: 'created',
      repository: { full_name: 'JoshCLWren/comic-pile' },
      pull_request: { number: 100, head: { ref: 'factory/49-3154-greptile', sha: HEAD, repo: { full_name: 'JoshCLWren/comic-pile' } } },
    },
  };
  const pushback = thread({ replies: [actions('Rebuttal'), comment('greptile-apps', 'I still think this leaks.')] });
  assert.ok(routed((await runWith(context, { threads: [pushback] })).calls));
});

test('reconciler workflow routes review and PR-body edit events in a per-PR group', () => {
  const workflow = fs.readFileSync(
    path.join(__dirname, '..', 'workflows', 'factory-issue-pr-state-reconciler.yml'),
    'utf8',
  );
  assert.match(workflow, /pull_request_review:\s*\n\s*types:\s*\[submitted, dismissed\]/);
  assert.match(workflow, /types:\s*\[opened, reopened, ready_for_review, synchronize, closed, unlabeled, edited\]/);
  assert.match(workflow, /format\('factory-greptile-routing-pr-\{0\}', github\.event\.pull_request\.number\)/);
  assert.match(workflow, /pull_request_review_comment:\s*\n\s*types:\s*\[created\]/);
  assert.match(workflow, /github\.event_name == 'pull_request_review' \|\| github\.event_name == 'pull_request_review_comment' \|\| \(github\.event_name == 'pull_request_target' && github\.event\.action == 'edited'\)/);
  assert.match(workflow, /\|\| 'factory-issue-pr-state-reconciler' \}\}\n\s*cancel-in-progress: false/);
  assert.match(workflow, /"\$EVENT_ACTION" == 'edited' \]\] && exit 0/);
});
