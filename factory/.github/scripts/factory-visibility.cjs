const {
  SCHEDULED_WORKER_IDS,
  loadExpectedWorkerIds,
} = require('./factory-expected-workers.cjs');

const {
  expectedWorkers: EXPECTED_WORKER_IDS,
  retiredWorkers: RETIRED_WORKER_IDS,
} = loadExpectedWorkerIds();

const SCHEDULED_OWNER_LABELS = SCHEDULED_WORKER_IDS.map((id) => `factory:${id}`);
const FIXED_MODEL_OWNER_LABELS = [...EXPECTED_WORKER_IDS]
  .sort((left, right) => left - right)
  .map((id) => `factory:${id}`);
// Label definitions and known owner names stay roster-derived. Retired ids are
// kept in OWNER_LABELS so historical leases can still be stripped cleanly.
const RETIRED_OWNER_LABELS = [...RETIRED_WORKER_IDS]
  .sort((left, right) => left - right)
  .map((id) => `factory:${id}`);
const WORKER_OWNER_LABELS = [...SCHEDULED_OWNER_LABELS, ...FIXED_MODEL_OWNER_LABELS];

const DEFINITIONS = {
  factory: ['5319E7', 'Work owned or produced by an autonomous ComicPile factory'],
  ...Object.fromEntries(WORKER_OWNER_LABELS.map((name) => {
    const number = name.slice('factory:'.length);
    return [name, ['0366D6', `Current next-action owner is ComicPile Factory ${number}`]];
  })),
  'factory:local': ['0366D6', 'Current next-action owner is the local OpenCode factory'],
  'factory:unowned': ['BFDADC', 'Factory work has no current next-action owner'],
  'factory:building': ['FBCA04', 'A factory is actively implementing or repairing this work'],
  'factory:review': ['D4C5F9', 'The exact current head needs review or re-review'],
  'factory:approved': ['8A63D2', 'Code review passed for this PR head; merge gates still apply'],
  'factory:changes-requested': ['D73A4A', 'Actionable review findings currently block progress'],
  'factory:ci': ['1D76DB', 'Review passed and required exact-head checks are being verified'],
  'factory:ready': ['0E8A16', 'All exact-head factory merge gates are satisfied'],
  'factory:blocked': ['B60205', 'A genuine human, credential, or external blocker remains'],
  // Derived observability label (issue #3138). It is never a stage: it stays out
  // of STAGE_LABELS, precedence, BLOCKED_LABELS, and queue selection.
  'factory:merge-conflict': ['E99695', 'GitHub reports the exact pull-request head as merge-conflicted'],
};

const OWNER_LABELS = [
  ...new Set([
    ...SCHEDULED_OWNER_LABELS,
    ...FIXED_MODEL_OWNER_LABELS,
    ...RETIRED_OWNER_LABELS,
    'factory:local',
    'factory:unowned',
  ]),
];

function isOwnerLabel(label) {
  return label === 'factory:local'
    || label === 'factory:unowned'
    || /^factory:\d+$/.test(label);
}

const STAGE_LABELS = [
  'factory:building',
  'factory:review',
  'factory:changes-requested',
  'factory:ci',
  'factory:ready',
  'factory:blocked',
];
const ADVANCED_PR_STAGES = new Set(['factory:review', 'factory:ci', 'factory:ready']);
// Review outcome is an informational second axis, never a stage or merge gate.
const APPROVAL_LABEL = 'factory:approved';
const MANUAL_ONLY_MARKER = '<!-- factory-execution:manual-only -->';
// Lockstep with factory_work_policy.BLOCKED_LABELS / issue_labels_block_reconciliation.
const BLOCKED_LABELS = new Set([
  'factory:blocked',
  'ralph-status:blocked',
  'wontfix',
  'invalid',
  'duplicate',
]);

// Derived observability labels describe GitHub-reported state; they are never
// stages and never affect precedence, blockers, or queue selection (#3138).
// The reconciler owns their presence on the exact head, so every label-set
// replacement rebuilds them from live mergeability instead of inheriting them.
const MERGE_CONFLICT_LABEL = 'factory:merge-conflict';
const DERIVED_LABELS = new Set([MERGE_CONFLICT_LABEL]);

// Derive conflict state from GitHub mergeability only. REST reports `mergeable`
// as a boolean plus `mergeable_state`; `gh`/GraphQL shapes report
// `MERGEABLE`/`CONFLICTING` plus `mergeStateStatus`. Anything still being
// computed (`UNKNOWN`/null) is unknown: never guess a conflict the API has not
// reported, and never erase a label GitHub has not yet resolved.
const MERGE_CLEAN_STATES = new Set(['CLEAN', 'BEHIND', 'BLOCKED', 'UNSTABLE', 'HAS_HOOKS']);

function mergeConflictState(pullRequest) {
  const mergeable = pullRequest?.mergeable;
  const mergeableToken = typeof mergeable === 'string' ? mergeable.toUpperCase() : mergeable;
  const mergeState = String(
    pullRequest?.mergeStateStatus ?? pullRequest?.mergeable_state ?? '',
  ).toUpperCase();
  if (mergeableToken === false || mergeableToken === 'CONFLICTING') return 'conflict';
  if (mergeState === 'DIRTY') return 'conflict';
  if (mergeableToken === true || mergeableToken === 'MERGEABLE') return 'clean';
  if (MERGE_CLEAN_STATES.has(mergeState)) return 'clean';
  return 'unknown';
}

// `null` means "GitHub has not resolved mergeability": callers must preserve the
// current derived label. An array is the exact derived-label set to apply.
function derivedLabelsFor(pullRequest) {
  const state = mergeConflictState(pullRequest);
  if (state === 'conflict') return [MERGE_CONFLICT_LABEL];
  if (state === 'clean') return [];
  return null;
}

function hasManualOnlyDirective(body) {
  // Referencing this marker in inline or fenced documentation is not an opt-out.
  // A standalone HTML comment or a standalone `manual-only` line qualifies.
  // Keep lockstep with scripts/factory_eligibility.py is_manual_only().
  const prose = String(body || '')
    .replace(/`{3}[\s\S]*?`{3}/g, '')
    .replace(/`[^`\n]*`/g, '');
  return prose.split(/\r?\n/).some((line) => {
    const trimmed = line.trim();
    return trimmed === MANUAL_ONLY_MARKER || trimmed === 'manual-only';
  });
}

function labelsBlockEnrollment(labels) {
  return [...labels].some(label => BLOCKED_LABELS.has(label));
}

const TRUSTED_ASSOCIATIONS = new Set(['OWNER', 'MEMBER', 'COLLABORATOR']);
const TRUSTED_APP_SLUGS = new Set(['github-actions']);

function performedViaUntrustedApp(comment) {
  const app = comment?.performed_via_github_app;
  if (!app || typeof app !== 'object') return false;
  const slug = app.slug;
  if (typeof slug !== 'string' || !slug) return false;
  return !TRUSTED_APP_SLUGS.has(slug);
}

function trusted(comment) {
  // Mirror Python trusted_comment_bodies: reject non-github-actions Apps even with a
  // trusted association; accept human OWNER/MEMBER/COLLABORATOR with no App; accept
  // exact github-actions[bot] regardless of association. Never trust any other App.
  if (performedViaUntrustedApp(comment)) return false;
  const association = comment?.author_association;
  if (TRUSTED_ASSOCIATIONS.has(association)) return true;
  return comment?.user?.login === 'github-actions[bot]';
}

// Reviewer bots whose inline findings route a PR to repair. This allowlist is
// deliberately separate from trusted(): a reviewer bot can never author
// factory markers, ownership, or readiness. It only proves that the bot left
// unresolved findings on the exact current head.
const REPAIR_ROUTING_REVIEWER_BOTS = new Set(['greptile-apps[bot]']);

function isRepairRoutingReviewer(review) {
  return REPAIR_ROUTING_REVIEWER_BOTS.has(review?.user?.login);
}

const REVIEW_THREAD_PAGE_LIMIT = 50;

// REST/GraphQL instrumentation: record every outbound request by installation,
// workflow, endpoint, event, response status, and rate-limit headers so the
// top API consumers can be identified without guessing (issue #3340). The log
// is bounded in memory and exposed only through _test.
const REST_REQUEST_LOG_LIMIT = 1000;
let restRequestLogEntries = [];

function rateLimited(error) {
  return error?.status === 403 || error?.status === 429;
}

function restRateLimitHeaders(headers) {
  const remaining = Number(headers?.['x-ratelimit-remaining']);
  const reset = Number(headers?.['x-ratelimit-reset']);
  const retryAfter = Number(headers?.['retry-after']);
  return {
    remaining: Number.isFinite(remaining) ? remaining : null,
    reset: Number.isFinite(reset) && reset > 0 ? reset : null,
    retryAfter: Number.isFinite(retryAfter) && retryAfter >= 0 ? retryAfter : null,
  };
}

function recordRestRequest(spec) {
  const record = {
    observedAt: spec.observedAt || new Date().toISOString(),
    installation: spec.installation || 'local',
    workflow: spec.workflow || 'unknown',
    endpoint: spec.endpoint || 'unknown',
    event: spec.event || 'unknown',
    kind: spec.kind || 'rest',
    method: spec.method || null,
    status: spec.status ?? null,
    limited: Boolean(spec.limited),
    ...restRateLimitHeaders(spec.headers),
  };
  restRequestLogEntries.push(record);
  if (restRequestLogEntries.length > REST_REQUEST_LOG_LIMIT) restRequestLogEntries.shift();
  return record;
}

// Record a single outbound HTTP request. This is the shared request layer that
// pagination, retries, and individual calls all use so every request is counted.
async function recordRequest(github, context, spec, requestFn) {
  const installation = process.env.GITHUB_ACTIONS === 'true'
    ? 'github-actions-installation'
    : 'local';
  const workflow = process.env.GITHUB_WORKFLOW
    || (context && context.workflow)
    || 'unknown';
  const event = spec.event || (context && context.eventName) || 'unknown';
  const observedAt = new Date().toISOString();
  const record = recordRestRequest({
    installation, workflow, endpoint: spec.endpoint, event,
    kind: spec.kind || 'rest', method: spec.method || null,
    status: null, headers: {}, observedAt,
  });
  try {
    const response = await requestFn();
    const headers = response?.headers || {};
    Object.assign(record, { status: response?.status ?? null, ...restRateLimitHeaders(headers) });
    return response;
  } catch (error) {
    const headers = error?.response?.headers || error?.headers || {};
    Object.assign(record, {
      status: error?.status ?? null,
      ...restRateLimitHeaders(headers),
      limited: rateLimited(error),
    });
    throw error;
  }
}

// Wrap one outbound REST/GraphQL call so its installation, workflow, endpoint,
// event, response status, and rate-limit headers are recorded. The wrapped
// operation owns its own retry/backpressure (withRetry); instrumentation
// observes the final outcome of the call.
// Kept for backward compatibility with call sites that pass a compound operation.
async function restCall(github, context, spec, operation) {
  return recordRequest(github, context, spec, operation);
}

// Manually paginate a REST endpoint and aggregate every page's data. The page
// fetcher owns all recording: call sites pass a withRetryLogged-wrapped
// fetcher, so each page request and each retry attempt is recorded exactly
// once at the shared request layer. This wrapper never records on its own.
async function manualPaginationWithLogging(github, context, baseSpec, fetchPageFn, { perPage = 100 } = {}) {
  const allResults = [];
  let page = 1;
  while (true) {
    const response = await fetchPageFn(page, perPage, {
      ...baseSpec,
      endpoint: `${baseSpec.endpoint}?per_page=${perPage}&page=${page}`,
    });
    const pageData = response.data || [];
    allResults.push(...pageData);
    if (pageData.length < perPage) break;
    page += 1;
  }
  return { data: allResults, headers: {}, status: 200 };
}

// Retry wrapper that records each attempt as a separate request.
async function withRetryLogged(github, context, spec, operation, { attempts = 3, delay = 250, maxRateLimitWait = 30000, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)) } = {}) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await recordRequest(github, context, spec, operation);
    } catch (error) {
      lastError = error;
      if (!isTransient(error) || attempt === attempts) throw error;
      const rateDelay = rateLimitDelay(error);
      if ((error.status === 403 || error.status === 429)
          && (rateDelay === null || rateDelay > maxRateLimitWait)) throw error;
      await sleep(rateDelay === null ? delay * attempt : rateDelay);
    }
  }
  throw lastError;
}

function restRequestLog() {
  return restRequestLogEntries.slice();
}

function restRequestCounts() {
  const counts = {};
  for (const record of restRequestLogEntries) {
    const key = `${record.installation}|${record.workflow}|${record.event}|${record.kind}|${record.endpoint}|${record.status}`;
    counts[key] = (counts[key] || 0) + 1;
  }
  return counts;
}

function clearRestRequestLog() {
  restRequestLogEntries = [];
}

// Open-thread rule, shared with .github/scripts/factory-review-findings.sh
// (FACTORY_GREPTILE_JQ_DEFS; keep them identical, see
// docs/FACTORY_GITHUB_VISIBILITY.md "Open Greptile findings"). A review thread
// is an OPEN Greptile finding when it is unresolved, not outdated, its first
// comment is by Greptile, and neither of these holds:
//   1. Greptile's latest comment is a reply that clearly accepts the answer
//      (an acceptance prefix and no contrast word in its first sentence);
//   2. a trusted responder (OWNER/MEMBER/COLLABORATOR, or the github-actions
//      Bot actor) replied after Greptile's latest comment. A login pattern
//      alone is never trusted: anyone can register a look-alike user name.
const GREPTILE_ACK_RE = /^(you['\u2019]?re (absolutely |totally )?right|you are (absolutely |totally )?right|(that|this) (addresses|resolves|answers|fixes|clears up|handles)|good (catch|point|call)|makes sense|fair (point|enough)|agreed|confirmed)/i;
const GREPTILE_ACK_CONTRAST_RE = /\b(but|however|although|though|still|remains?|not)\b|n['\u2019]t\b/i;
const GITHUB_ACTIONS_LOGIN_RE = /^github-actions(\[bot\])?$/;
const GREPTILE_OUTCOME_MARKER = 'comic-pile-factory-greptile-outcome-v1';
const RECENT_COMMENT_WINDOW = 20;

function isGreptileLogin(login) {
  return login === 'greptile-apps' || login === 'greptile-apps[bot]';
}

function isGreptileAck(body) {
  const text = String(body || '').replace(/<[^>]*>/g, '').replace(/^[\s*_>`"-]+/, '');
  const firstSentence = (text.match(/^[^.!?\n]*/) || [''])[0];
  return GREPTILE_ACK_RE.test(text) && !GREPTILE_ACK_CONTRAST_RE.test(firstSentence);
}

function isTrustedResponder(comment) {
  return TRUSTED_ASSOCIATIONS.has(comment?.authorAssociation || '')
    || (comment?.author?.__typename === 'Bot' && GITHUB_ACTIONS_LOGIN_RE.test(comment?.author?.login || ''));
}

// REST issue comment variant (summary-only outcome comments).
function isTrustedIssueCommenter(comment) {
  return TRUSTED_ASSOCIATIONS.has(comment?.author_association || '')
    || (comment?.user?.type === 'Bot' && comment?.user?.login === 'github-actions[bot]');
}

// Must equal summary_finding_id in factory-review-findings.sh.
function summaryFindingId(title) {
  const slug = String(title || '')
    .replace(/[A-Z]/g, ch => ch.toLowerCase())
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 60);
  return `GREPTILE_SUMMARY_${slug}`;
}

// Fetch every comment of one thread. Used only when the recent window cannot
// establish Greptile's latest comment. Returns null when incomplete.
async function fullThreadComments(github, context, threadId, { pageLimit = 20 } = {}) {
  const comments = [];
  let after = null;
  for (let page = 0; page < pageLimit; page += 1) {
    // Retry records every attempt at the shared request layer; a second wrapper
    // here would double-count the same outbound GraphQL request.
    const response = await withRetryLogged(github, context, {
      endpoint: 'graphql',
      kind: 'graphql',
      event: context.eventName,
    }, async () => {
      const result = await github.graphql(
        `query($id:ID!,$after:String){node(id:$id){... on PullRequestReviewThread{comments(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{url author{__typename login} authorAssociation body createdAt}}}}}`,
        { id: threadId, after },
      );
      return { data: result, headers: {}, status: 200 };
    });
    const connection = response?.data?.node?.comments;
    if (!Array.isArray(connection?.nodes)) return null;
    comments.push(...connection.nodes);
    if (connection.pageInfo?.hasNextPage === false) return comments;
    if (connection.pageInfo?.hasNextPage !== true || !connection.pageInfo.endCursor) return null;
    after = connection.pageInfo.endCursor;
  }
  return null;
}

// thread: { isResolved, isOutdated, firstComment: {author,url}, comments: [...] }
// where comments is the most recent window (oldest first). Only the suffix
// after Greptile's latest comment matters, so a bounded recent window is exact
// whenever it contains that comment or any trusted reply.
function greptileThreadState(thread) {
  const first = thread?.firstComment;
  if (!first || !isGreptileLogin(first.author?.login)) return 'other';
  if (thread.isResolved) return 'resolved';
  if (thread.isOutdated) return 'outdated';
  const comments = Array.isArray(thread.comments) ? thread.comments : [];
  let lastGreptile = -1;
  comments.forEach((comment, index) => {
    if (isGreptileLogin(comment?.author?.login)) lastGreptile = index;
  });
  const latest = lastGreptile >= 0 ? comments[lastGreptile] : null;
  const latestIsReply = latest && (!first.url || latest.url !== first.url);
  if (latestIsReply && isGreptileAck(latest.body)) return 'acknowledged';
  if (comments.slice(lastGreptile + 1).some(isTrustedResponder)) return 'answered';
  return 'open';
}

function lastTrustedAnswerAt(thread) {
  const answers = (thread?.comments || []).filter(isTrustedResponder).map(c => c.createdAt).filter(Boolean);
  return answers.length ? answers.sort().at(-1) : null;
}

async function greptileReviewThreads(github, context, number, { pageLimit = REVIEW_THREAD_PAGE_LIMIT } = {}) {
  // Follow every reviewThreads page: an open finding beyond the first 100
  // threads must still route the PR to repair. Returns the normalized threads
  // and whether every page was read; callers must fail closed when incomplete.
  const threads = [];
  let after = null;
  for (let page = 0; page < pageLimit; page += 1) {
    // Retry records every attempt at the shared request layer; a second wrapper
    // here would double-count the same outbound GraphQL request.
    const response = await withRetryLogged(github, context, {
      endpoint: 'graphql',
      kind: 'graphql',
      event: context.eventName,
    }, async () => {
      const result = await github.graphql(
        `query($owner:String!,$name:String!,$number:Int!,$after:String){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$after){pageInfo{hasNextPage endCursor} nodes{id isResolved isOutdated firstComment:comments(first:1){nodes{databaseId url author{__typename login}}} recent:comments(last:${RECENT_COMMENT_WINDOW}){totalCount nodes{url author{__typename login} authorAssociation body createdAt}}}}}}}`,
        { owner: context.repo.owner, name: context.repo.repo, number, after },
      );
      return { data: result, headers: {}, status: 200 };
    });
    const connection = response?.data?.repository?.pullRequest?.reviewThreads;
    if (!Array.isArray(connection?.nodes)) return { threads, complete: false };
    for (const node of connection.nodes) {
      const thread = {
        id: node.id,
        isResolved: Boolean(node.isResolved),
        isOutdated: Boolean(node.isOutdated),
        firstComment: node.firstComment?.nodes?.[0] || null,
        comments: node.recent?.nodes || [],
      };
      // A long discussion can push Greptile's latest comment out of the recent
      // window; then the window cannot decide the rule, so read every comment.
      // An incomplete read keeps the window, which leaves the finding open.
      const total = node.recent?.totalCount ?? thread.comments.length;
      if (!thread.isResolved && !thread.isOutdated
          && isGreptileLogin(thread.firstComment?.author?.login)
          && total > thread.comments.length
          && !thread.comments.some(comment => isGreptileLogin(comment?.author?.login))) {
        const all = await fullThreadComments(github, context, node.id);
        if (all) thread.comments = all;
      }
      threads.push(thread);
    }
    if (connection.pageInfo?.hasNextPage === false) return { threads, complete: true };
    if (connection.pageInfo?.hasNextPage !== true || !connection.pageInfo.endCursor) {
      return { threads, complete: false };
    }
    after = connection.pageInfo.endCursor;
  }
  return { threads, complete: false };
}

async function unresolvedReviewerBotThreads(github, context, number, options = {}) {
  const { threads, complete } = await greptileReviewThreads(github, context, number, options);
  return { count: threads.filter(thread => greptileThreadState(thread) === 'open').length, complete };
}

// Greptile re-reviews often only rewrite the PR-body summary. Parse its
// findings list for the exact head it reviewed. Returns null when the body has
// no Greptile summary, else { commit, findings: [{ title, discussionId }] }.
function greptileSummaryFindings(body) {
  const lines = String(body || '').split(/\r?\n/);
  const start = lines.findIndex(line => /^\s*<!-- greptile_comment -->\s*$/.test(line));
  if (start < 0) return null;
  let end = lines.findIndex((line, index) => index > start && /^\s*<!-- \/greptile_comment -->\s*$/.test(line));
  if (end < 0) end = lines.length;
  const block = lines.slice(start + 1, end);
  const text = block.join('\n');
  const commit = (text.match(/Last reviewed commit:.*?\/commit\/([0-9a-f]{40})/i) || [])[1] || null;
  const findings = block
    .filter(line => /^\s*\d+\.\s.*<img[^>]*\balt="P\d"/.test(line))
    .map(line => ({
      title: ((line.match(/\*\*([^*]+)\*\*/) || [])[1] || '').trim(),
      discussionId: (line.match(/#discussion_r(\d+)/) || [])[1] || null,
    }));
  return { commit, findings };
}

async function greptileReviewCompletedAt(github, context, sha) {
  const response = await withRetryLogged(github, context, {
    endpoint: `repos/${context.repo.owner}/${context.repo.repo}/commits/${sha}/check-runs`,
    method: 'GET',
    event: context.eventName,
  }, async () => {
    return github.rest.checks.listForRef({
      owner: context.repo.owner,
      repo: context.repo.repo,
      ref: sha,
      per_page: 100,
    });
  });
  const times = (response?.data?.check_runs || [])
    .filter(run => /greptile/i.test(run.name || '') && run.completed_at)
    .map(run => run.completed_at);
  return times.length ? times.sort().at(-1) : null;
}

// A summary finding counts when Greptile lists it for the live head and:
//   - it has no thread: unless a trusted outcome comment for it and this head
//     was posted after Greptile's latest review of the head completed;
//   - its thread is open; or
//   - its thread is answered/resolved, but the latest trusted answer predates
//     Greptile's latest review of this head (Greptile re-reviewed and still
//     lists it). Acknowledged and outdated threads never count.
// Same rule as factory_greptile_required_findings in factory-review-findings.sh.
function openSummaryFindings(summary, threads, head, reviewedAt, issueComments = []) {
  if (!summary || !summary.commit || summary.commit !== head) return [];
  const byComment = new Map(threads
    .filter(thread => thread.firstComment?.databaseId)
    .map(thread => [String(thread.firstComment.databaseId), thread]));
  return summary.findings.filter(finding => {
    const thread = finding.discussionId ? byComment.get(finding.discussionId) : null;
    if (!thread) {
      const id = summaryFindingId(finding.title);
      return !issueComments.some(comment => isTrustedIssueCommenter(comment)
        && String(comment.body || '').includes(`${GREPTILE_OUTCOME_MARKER}:${id}:`)
        && String(comment.body || '').includes(`:${head}`)
        && (!reviewedAt || String(comment.created_at || '') > reviewedAt));
    }
    const state = greptileThreadState(thread);
    if (state === 'open') return true;
    if (state !== 'answered' && state !== 'resolved') return false;
    const answeredAt = lastTrustedAnswerAt(thread);
    return Boolean(answeredAt && reviewedAt && reviewedAt > answeredAt);
  });
}

// State-based Greptile routing. Every routing event (any submitted review or
// a PR-body edit) recomputes from the live PR: its head, its review threads,
// and Greptile's PR-body summary. A superseded or replaced pending run is
// therefore harmless, because the next run sees the same live state. Routing
// only ever moves a PR to factory:changes-requested; it never un-routes.
async function routeGreptileFindings(github, context, number) {
  const live = (await withRetryLogged(github, context, {
    endpoint: `repos/${context.repo.owner}/${context.repo.repo}/pulls/${number}`,
    method: 'GET',
    event: context.eventName,
  }, async () => {
    return github.rest.pulls.get({
      owner: context.repo.owner,
      repo: context.repo.repo,
      pull_number: number,
    });
  }))?.data;
  if (!live || (live.state && live.state !== 'open')) return { routed: false, reason: 'not-open' };
  // Operator holds stay off the factory even when Greptile findings are open.
  // Enrollment already skips this marker; routing must not re-label the PR.
  if (hasManualOnlyDirective(live.body)) return { routed: false, reason: 'manual-only' };
  const head = live.head?.sha;
  const current = await currentLabels(github, context, number);
  const sameRepo = !live.head?.repo || live.head.repo.full_name === context.payload.repository?.full_name;
  if (!(current.has('factory') || (sameRepo && (live.head?.ref || '').startsWith('factory/')))) {
    return { routed: false, reason: 'not-factory' };
  }
  const { threads, complete } = await greptileReviewThreads(github, context, number);
  const openThreads = threads.filter(thread => greptileThreadState(thread) === 'open');
  const summary = greptileSummaryFindings(live.body);
  let openSummary = [];
  if (summary && summary.commit === head && summary.findings.length) {
    const reviewedAt = await greptileReviewCompletedAt(github, context, head);
    const byComment = new Set(threads.map(thread => String(thread.firstComment?.databaseId || '')));
    const needsComments = summary.findings.some(finding => !finding.discussionId || !byComment.has(finding.discussionId));
    const issueComments = needsComments
      ? (await manualPaginationWithLogging(github, context, {
          endpoint: `repos/${context.repo.owner}/${context.repo.repo}/issues/${number}/comments`,
          method: 'GET',
          event: context.eventName,
        }, async (page, perPage, pageSpec) => {
          return withRetryLogged(github, context, pageSpec, async () => {
            const result = await github.rest.issues.listComments({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: number,
              per_page: perPage,
              page,
            });
            return { data: result.data, headers: result.headers, status: result.status };
          });
        })).data
      : [];
    openSummary = openSummaryFindings(summary, threads, head, reviewedAt, issueComments);
  }
  if (openThreads.length === 0 && openSummary.length === 0) {
    // Fail closed: never treat a truncated thread listing as "no findings".
    // Failing the run leaves the event visibly red for retry.
    if (!complete) {
      throw new Error(`PR #${number}: review-thread pagination was incomplete; refusing to treat Greptile findings as absent`);
    }
    return { routed: false, reason: 'no-open-findings' };
  }
  if (current.has('factory:changes-requested')) return { routed: false, reason: 'already-routed' };
  await reconcileLabels(github, context, number, {
    owner: [...current].find(isOwnerLabel) || await ownerFromLinkedIssue(github, context, { ...live, number }),
    stage: 'factory:changes-requested',
  });
  return { routed: true, openThreads: openThreads.length, openSummary: openSummary.length };
}

async function livePullRequest(github, context, number) {
  const response = await withRetryLogged(github, context, {
    endpoint: `repos/${context.repo.owner}/${context.repo.repo}/pulls/${number}`,
    method: 'GET',
    event: context.eventName,
  }, async () => {
    return github.rest.pulls.get({
      owner: context.repo.owner,
      repo: context.repo.repo,
      pull_number: number,
    });
  });
  return response?.data ?? null;
}

async function livePullRequestHead(github, context, number) {
  const response = await withRetry(() => github.rest.pulls.get({
    owner: context.repo.owner,
    repo: context.repo.repo,
    pull_number: number,
  }));
  return response?.data?.head?.sha || null;
}

function workerFrom(body) {
  for (const pattern of [
    /comic-pile-factory-implement-(?:claim|progress)-v\d+:issue-\d+:([^:\s>]+):/,
    /comic-pile-factory-review-claim-v\d+:[^:\s>]+:([^:\s>]+):/,
    /comic-pile-factory-fix-(?:claim|progress)-v\d+:[^:\s>]+:([^:\s>]+):/,
    /comic-pile-factory-claim-released-v\d+:[^:\s>]+:([^:\s>]+):/,
    /comic-pile-factory-head-contributor-v\d+:[^:\s>]+:[^:\s>]+:worker-(\d+):/,
  ]) {
    const match = body.match(pattern);
    if (match) return match[1];
  }
  return null;
}

function ownerFor(worker) {
  if (worker == null || worker === '') return 'factory:unowned';
  const token = String(worker);

  const scheduled = token.match(/^chatgpt-factory-([1-5])$/);
  if (scheduled) return `factory:${scheduled[1]}`;

  let number = null;
  const fixedModel = token.match(/^opencode-(?:free-model|nvidia|omniroute)-factory-(\d+)$/);
  if (fixedModel) {
    number = Number(fixedModel[1]);
  } else {
    const bare = token.match(/^(?:worker-)?(\d+)$/);
    if (bare) number = Number(bare[1]);
  }
  if (number != null && EXPECTED_WORKER_IDS.has(number)) {
    return `factory:${number}`;
  }

  if (token === 'local' || token === 'local-opencode' || token.startsWith('local-opencode-')) {
    return 'factory:local';
  }
  return 'factory:unowned';
}

function durablePrOwner(current) {
  if (current.has('factory:local')) return 'factory:local';
  // Preserve any non-scheduled numeric owner lease already on the PR. Validity
  // for *new* ownership is roster-gated in ownerFor; durability must not depend
  // on a contiguous 1..N label array.
  for (const label of current) {
    const match = /^factory:(\d+)$/.exec(label);
    if (!match) continue;
    const number = Number(match[1]);
    if (SCHEDULED_WORKER_IDS.includes(number)) continue;
    return label;
  }
  return null;
}

const LOCAL_OWNER_LABEL = 'factory:local';
const READY_STAGE_LABEL = 'factory:ready';

// A `factory:local` label present at the write boundary is a live local owner
// lease: authoritative next-action state written by the local coordinator
// (docs/AUTONOMOUS_FACTORY_POLICY.md). This label-level reconciler must never
// replace it with `factory:unowned` or hand it to another owner — only a
// provably stale lease may fall through, and staleness (the local lease TTL) is
// proven by the Python lease policy (factory_work_policy.lease_is_stale, applied
// by factory_issue_pr_state_policy.py to the same live read); this reconciler
// deliberately does not duplicate that TTL. While the lease is live, a
// `factory:ready` stage set alongside it is preserved as well, because the
// local coordinator owns the merge gate: PR #5393 had ready+local reverted to
// review+unowned within seconds by exactly this projection. A new head
// (synchronize) still invalidates ready/ci, and an explicit trusted release of
// the local lease still wins.
function preserveLiveLocalLease(
  current,
  requested,
  { newHead = false, releaseLocalLease = false, preserveReadyStage = false } = {},
) {
  if (releaseLocalLease || !current.has(LOCAL_OWNER_LABEL)) return requested;
  const currentStage = STAGE_LABELS.find(label => current.has(label));
  const stage = preserveReadyStage && !newHead && currentStage === READY_STAGE_LABEL
    ? READY_STAGE_LABEL
    : requested.stage;
  return { ...requested, owner: LOCAL_OWNER_LABEL, stage };
}

function stageFrom(body) {
  if (/comic-pile-factory-needs-human-v\d+:/.test(body)) return 'factory:blocked';
  if (/comic-pile-factory-ready-v\d+:/.test(body)) return 'factory:ready';
  if (/comic-pile-factory-review-v\d+:[^:\s>]+:changes-required/.test(body)) {
    return 'factory:changes-requested';
  }
  if (/comic-pile-factory-review-v\d+:[^:\s>]+:pass/.test(body)) return 'factory:ci';
  if (/comic-pile-factory-review-claim-v\d+:/.test(body)) return 'factory:review';
  if (
    /comic-pile-factory-implement-(?:claim|progress)-v\d+:/.test(body)
    || /comic-pile-factory-fix-(?:claim|progress)-v\d+:/.test(body)
  ) return 'factory:building';
  return null;
}

function rateLimitDelay(error, now = Date.now()) {
  const headers = error?.response?.headers || error?.headers || {};
  const remaining = Number(headers['x-ratelimit-remaining']);
  const limited = (error?.status === 403 && headers['x-ratelimit-remaining'] != null && remaining === 0)
    || error?.status === 429;
  if (!limited) return null;

  const retryAfter = Number(headers['retry-after']);
  if (headers['retry-after'] != null && Number.isFinite(retryAfter) && retryAfter >= 0) {
    return Math.ceil(retryAfter * 1000);
  }
  const reset = Number(headers['x-ratelimit-reset']);
  if (Number.isFinite(reset) && reset > 0) return Math.max(0, reset * 1000 - now + 1000);
  return null;
}

function isTransient(error) {
  return error?.status === 429
    || (error?.status >= 500 && error?.status <= 599)
    || (error?.status === 403 && rateLimitDelay(error) !== null);
}

async function withRetry(operation, { attempts = 3, delay = 250, maxRateLimitWait = 30000, sleep = ms => new Promise(resolve => setTimeout(resolve, ms)) } = {}) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await operation();
    } catch (error) {
      lastError = error;
      if (!isTransient(error) || attempt === attempts) throw error;
      const rateDelay = rateLimitDelay(error);
      // Never spin on exhausted installation quota. Fail the run if reset is too
      // distant or unknown: Actions can resume on the next scheduled/event run.
      if ((error.status === 403 || error.status === 429)
          && (rateDelay === null || rateDelay > maxRateLimitWait)) throw error;
      await sleep(rateDelay === null ? delay * attempt : rateDelay);
    }
  }
  throw lastError;
}

async function currentLabels(github, context, number) {
  const endpoint = `repos/${context.repo.owner}/${context.repo.repo}/issues/${number}/labels`;
  const response = await manualPaginationWithLogging(github, context, {
    endpoint,
    method: 'GET',
    event: context.eventName,
  }, async (page, perPage, pageSpec) => {
    return withRetryLogged(github, context, pageSpec, async () => {
      const result = await github.rest.issues.listLabelsOnIssue({
        owner: context.repo.owner,
        repo: context.repo.repo,
        issue_number: number,
        per_page: perPage,
        page,
      });
      return { data: result.data, headers: result.headers, status: result.status };
    });
  });
  return new Set(response.data.map(label => label.name));
}

async function reconcileLabels(
  github,
  context,
  number,
  { owner, stage, derivedLabels, approved, newHead = false, releaseLocalLease = false, preserveReadyStage = false },
) {
  const current = await currentLabels(github, context, number);
  // Decide ownership/stage against the same live read that builds the
  // replacement set, so a local lease landing between the caller's own read and
  // this write can never be discarded (the PR #5393 revert).
  const lease = preserveLiveLocalLease(
    current,
    { owner, stage },
    { newHead, releaseLocalLease, preserveReadyStage },
  );
  owner = lease.owner;
  stage = lease.stage;
  const next = [...current].filter(
    label => !isOwnerLabel(label) && !STAGE_LABELS.includes(label)
      && !DERIVED_LABELS.has(label) && label !== APPROVAL_LABEL,
  );
  if (!next.includes('factory')) next.push('factory');
  if (owner) next.push(owner);
  if (stage) next.push(stage);
  // A trusted exact-head PASS is code-review visibility, NOT merge authority.
  // Preserve it across CI/ready transitions. New pushes and actionable review
  // findings clear it, even if an older PASS comment remains in history.
  const reviewApproved = approved === true || (approved == null && current.has(APPROVAL_LABEL));
  if (reviewApproved && stage !== 'factory:changes-requested' && stage !== 'factory:building') {
    next.push(APPROVAL_LABEL);
  }
  // `undefined`/`null` preserves the current derived labels. Callers that do not
  // read live mergeability (review routing, comment markers) must not claim or
  // erase an observability fact they did not verify.
  const derived = derivedLabels == null
    ? [...current].filter(label => DERIVED_LABELS.has(label))
    : [...new Set(derivedLabels)].filter(label => DERIVED_LABELS.has(label));
  next.push(...derived);

  // A no-op label replacement still consumes API quota and emits a labels event.
  // Compare sets, since GitHub does not promise stable label ordering.
  if (current.size === next.length && next.every(label => current.has(label))) return;

  // Retry records every attempt at the shared request layer; a second wrapper
  // here would double-count the same outbound PUT request.
  await withRetryLogged(github, context, {
    endpoint: `repos/${context.repo.owner}/${context.repo.repo}/issues/${number}/labels`,
    method: 'PUT',
    event: context.eventName,
  }, async () => {
    const result = await github.rest.issues.setLabels({
      owner: context.repo.owner,
      repo: context.repo.repo,
      issue_number: number,
      labels: next,
    });
    return { data: result.data, headers: result.headers, status: result.status };
  });
}

async function ensureLabels(github, context) {
  const existing = await manualPaginationWithLogging(github, context, {
    endpoint: `repos/${context.repo.owner}/${context.repo.repo}/labels`,
    method: 'GET',
    event: context.eventName,
  }, async (page, perPage, pageSpec) => {
    return withRetryLogged(github, context, pageSpec, async () => {
      const result = await github.rest.issues.listLabelsForRepo({
        owner: context.repo.owner,
        repo: context.repo.repo,
        per_page: perPage,
        page,
      });
      return { data: result.data, headers: result.headers, status: result.status };
    });
  });
  const byName = new Map(existing.data.map(label => [label.name, label]));
  for (const [name, [color, description]] of Object.entries(DEFINITIONS)) {
    const current = byName.get(name);
    if (!current) {
      // Retry records every attempt at the shared request layer; a second
      // wrapper here would double-count the same outbound POST request.
      await withRetryLogged(github, context, {
        endpoint: `repos/${context.repo.owner}/${context.repo.repo}/labels`,
        method: 'POST',
        event: context.eventName,
      }, async () => {
        const result = await github.rest.issues.createLabel({
          owner: context.repo.owner,
          repo: context.repo.repo,
          name,
          color,
          description,
        });
        return { data: result.data, headers: result.headers, status: result.status };
      });
    } else if (
      current.color.toUpperCase() !== color
      || (current.description || '') !== description
    ) {
      // Retry records every attempt at the shared request layer; a second
      // wrapper here would double-count the same outbound PATCH request.
      await withRetryLogged(github, context, {
        endpoint: `repos/${context.repo.owner}/${context.repo.repo}/labels/${name}`,
        method: 'PATCH',
        event: context.eventName,
      }, async () => {
        const result = await github.rest.issues.updateLabel({
          owner: context.repo.owner,
          repo: context.repo.repo,
          name,
          new_name: name,
          color,
          description,
        });
        return { data: result.data, headers: result.headers, status: result.status };
      });
    }
  }
}

async function ownerFromLinkedIssue(github, context, pullRequest) {
  const closing = (pullRequest.body || '').match(
    /(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)/i,
  );
  const fixedModelBranch = pullRequest.head.ref.match(
    /^factory\/\d+-(\d+)-(?:nvidia|omni|opencode-free)(?:-|$)/,
  );
  const legacyBranch = pullRequest.head.ref.match(/^factory\/(\d+)(?:-|$)/);
  const issueNumber = Number(closing?.[1] || fixedModelBranch?.[1] || legacyBranch?.[1] || 0);
  if (!issueNumber) return 'factory:unowned';

  const endpoint = `repos/${context.repo.owner}/${context.repo.repo}/issues/${issueNumber}/comments`;
  const commentsResponse = await manualPaginationWithLogging(github, context, {
    endpoint,
    method: 'GET',
    event: context.eventName,
  }, async (page, perPage, pageSpec) => {
    return withRetryLogged(github, context, pageSpec, async () => {
      const result = await github.rest.issues.listComments({
        owner: context.repo.owner,
        repo: context.repo.repo,
        issue_number: issueNumber,
        per_page: perPage,
        page,
      });
      return { data: result.data, headers: result.headers, status: result.status };
    });
  });
  const comments = commentsResponse.data;
  comments.sort((left, right) => new Date(right.created_at) - new Date(left.created_at));
  for (const comment of comments) {
    if (!trusted(comment)) continue;
    const body = comment.body || '';
    if (/comic-pile-factory-claim-released-v\d+:/.test(body)) return 'factory:unowned';
    const worker = workerFrom(body);
    if (worker) return ownerFor(worker);
  }
  return 'factory:unowned';
}

async function reconcileMissingPrLabels({ github, context, openPullRequests = null }) {
  // PR events must still reconcile their exact live PR. On a recovery sweep,
  // only missing factory enrollment requires work: the open-PR list already
  // contains labels, so there is no reason to fetch every PR and its labels
  // (or ensure the entire repository label catalogue) when all are enrolled.
  let candidates;
  if (context.eventName === 'pull_request_target') {
    candidates = { data: [context.payload.pull_request].filter(Boolean) };
  } else {
    const endpoint = `repos/${context.repo.owner}/${context.repo.repo}/pulls`;
    const fetchOpenPage = page => withRetryLogged(github, context, {
      endpoint: `${endpoint}?state=open&per_page=100&page=${page}`,
      method: 'GET',
      event: context.eventName,
    }, async () => {
      const result = await github.rest.pulls.list({
        ...context.repo, state: 'open', per_page: 100, page,
      });
      return { data: result.data, headers: result.headers, status: result.status };
    });

    // A fresh, webhook-fed Neon snapshot avoids the full open-PR discovery scan.
    // A missing/stale DB snapshot falls back to GitHub. Incomplete records are
    // still revalidated against the live PR and labels before any mutation.
    let open;
    if (Array.isArray(openPullRequests)) {
      open = openPullRequests.map(item => ({
        ...item,
        state: 'open',
        draft: Boolean(item.isDraft),
        head: {
          ref: item.headRefName,
          sha: item.headRefOid,
          repo: item.isCrossRepository ? null : { full_name: `${context.repo.owner}/${context.repo.repo}` },
        },
        user: { login: item.author?.login || '' },
      }));
      console.log(`Factory enrollment: using ${open.length} projected PRs from Neon`);
    } else {
      const first = await fetchOpenPage(1);
      open = [...first.data];
      // A full first page does not prove there are no older unlabeled PRs.
      let lastPage = first.data;
      let page = 1;
      while (lastPage.length === 100) {
        page += 1;
        const next = await fetchOpenPage(page);
        lastPage = next.data;
        open.push(...lastPage);
      }
    }
    // A factory label alone is not complete enrollment: a valid PR must also
    // have exactly one current owner and one stage. The scheduled recovery
    // still repairs those partial or contradictory metadata states.
    const incomplete = open.filter(pr => {
      const labels = new Set(Array.isArray(pr.labels) ? pr.labels.map(label => label.name) : []);
      const owners = [...labels].filter(isOwnerLabel);
      const stages = STAGE_LABELS.filter(label => labels.has(label));
      return !labels.has('factory') || owners.length !== 1 || stages.length !== 1;
    });
    if (incomplete.length === 0) {
      console.log('Factory enrollment: all open PRs have complete ownership/stage metadata; skipping per-PR reads');
      return;
    }
    candidates = { data: incomplete };
  }

  let definitionsEnsured = false;
  for (const pr of candidates.data) {
    if (pr.state !== 'open' || pr.draft
      || !pr.head.repo?.full_name
      || /^(dependabot|renovate)(\[bot\])?$/.test(pr.user?.login || '')) continue;
    // Unlabeled / pull_request_target payloads can carry a null or stale body.
    // Re-fetch the live PR (same contract as routeGreptileFindings) and skip
    // operator holds before any label write. Fail closed when the body cannot
    // be read: never enroll from an unread or truncated payload.
    let live;
    try {
      live = await livePullRequest(github, context, pr.number);
    } catch (error) {
      console.warn(
        `PR #${pr.number}: live body unread (${error?.message || error}); skipping enrollment`,
      );
      continue;
    }
    if (!live || typeof live.body !== 'string') {
      console.warn(`PR #${pr.number}: live body unread; skipping enrollment`);
      continue;
    }
    // Projection rows do not always carry the author, and can briefly lag a
    // draft/cross-repository change. Only the live PR can authorize enrollment.
    if (live.draft || !live.head?.repo?.full_name
      || /^(dependabot|renovate)(\[bot\])?$/.test(live.user?.login || '')) continue;
    if (hasManualOnlyDirective(live.body)) continue;
    const current = await currentLabels(github, context, pr.number);
    if (labelsBlockEnrollment(current)) continue;
    // A local factory lease is a real active owner, not an exemption from
    // factory review. Preserve it until the local session explicitly releases
    // ownership; never take it over during an enrollment sweep.
    const owners = [...current].filter(isOwnerLabel);
    const stages = STAGE_LABELS.filter(label => current.has(label));
    const pushed = context.eventName === 'pull_request_target' && context.payload.action === 'synchronize';
    // Merge-conflict is derived observability from live GitHub mergeability, so
    // it must refresh even when an otherwise complete PR needs no label write.
    // `null` (unresolved mergeability) keeps the current label and counts as
    // already-correct, so a partially computed head never triggers a write loop.
    const derived = derivedLabelsFor(live);
    const currentDerived = [...current].filter(label => DERIVED_LABELS.has(label));
    const derivedSettled = derived === null
      || (derived.length === currentDerived.length && derived.every(label => current.has(label)));
    if (!pushed && current.has('factory') && owners.length === 1 && stages.length === 1
      && derivedSettled) continue;

    // Repair missing metadata without taking an existing lease or downgrading
    // review/CI state. Never manufacture readiness from green checks alone.
    let stage = !pushed && stages.length === 1 ? stages[0] : 'factory:review';
    let approved = pushed ? false : undefined;
    if (!pushed && stages.length === 0) {
      const commentsEndpoint = `repos/${context.repo.owner}/${context.repo.repo}/issues/${pr.number}/comments`;
      const commentsResponse = await manualPaginationWithLogging(github, context, {
        endpoint: commentsEndpoint,
        method: 'GET',
        event: context.eventName,
      }, async (page, perPage, pageSpec) => {
        return withRetryLogged(github, context, pageSpec, async () => {
          const result = await github.rest.issues.listComments({
            ...context.repo, issue_number: pr.number, per_page: perPage, page,
          });
          return { data: result.data, headers: result.headers, status: result.status };
        });
      });
      const comments = commentsResponse.data;
      for (const comment of [...comments].sort(
        (left, right) => new Date(right.created_at) - new Date(left.created_at),
      )) {
        if (!trusted(comment)) continue;
        const match = (comment.body || '').match(
          /comic-pile-factory-review-v\d+:([a-f0-9]{40}):(pass|changes-required)/,
        );
        if (match?.[1] !== pr.head.sha) continue;
        stage = match[2] === 'pass' ? 'factory:ci' : 'factory:changes-requested';
        approved = match[2] === 'pass';
        break;
      }
    }
    // Existing correct metadata is a no-op. Only ensure label definitions
    // immediately before a real enrollment/repair, once per invocation.
    if (!definitionsEnsured) {
      await ensureLabels(github, context);
      definitionsEnsured = true;
    }
    await reconcileLabels(github, context, pr.number, {
      // Never interpret a contradictory local+unowned label pair as a lease
      // release. Keep the active local owner until its session hands off.
      owner: current.has('factory:local')
        ? 'factory:local'
        : owners.length === 1 ? owners[0] : 'factory:unowned',
      stage,
      derivedLabels: derived,
      approved,
      // A push is a new head and invalidates ready/ci; every other event keeps
      // a factory:ready parked under a live local lease (the local coordinator
      // owns the merge gate).
      newHead: pushed,
      preserveReadyStage: true,
    });
  }
}

async function reconcile({ github, context }) {
  // Explicit recovery/bootstrap maintains label definitions. Do not page the
  // repository's label catalog on routine comment/review notifications.
  if (context.eventName === 'workflow_dispatch' || context.eventName === 'schedule') {
    await ensureLabels(github, context);
  }

  const payloadBody = context.payload.pull_request?.body ?? context.payload.issue?.body ?? '';
  if (hasManualOnlyDirective(payloadBody)) return;

  if (context.eventName === 'issue_comment') {
    const comment = context.payload.comment;
    const body = comment?.body || '';
    if (!body.includes('comic-pile-factory-')) return;
    if (!trusted(comment)) return;

    const number = context.payload.issue.number;
    const current = await currentLabels(github, context, number);
    const released = /comic-pile-factory-claim-released-v\d+:/.test(body);
    const worker = workerFrom(body);
    const currentOwner = [...current].find(isOwnerLabel) || null;
    const currentStage = STAGE_LABELS.find(label => current.has(label));
    const requestedStage = stageFrom(body);
    // The trusted controller now writes semantic-review-v1 markers. Legacy
    // review-v2 markers are still supported for historical workflows.
    // Only a matching PR + exact live head can affect approval visibility.
    // Neither format supplies merge authority or a GitHub APPROVED review.
    const firstLine = body.split(/\r?\n/, 1)[0];
    const semanticVerdict = firstLine.match(
      /^<!-- comic-pile-factory-semantic-review-v1:pr-(\d+):head-([a-f0-9]{40}):reviewer-(\d+):producer-(\d+|unknown):verdict-(approve|repair|reject|obsolete|duplicate|superseded|delivered) -->$/,
    );
    const legacyVerdict = body.match(
      /<!-- comic-pile-factory-review-v\d+:([a-f0-9]{40}):(pass|changes-required) -->/,
    );
    if (semanticVerdict || legacyVerdict) {
      if (!context.payload.issue.pull_request) return;
      if (semanticVerdict && Number(semanticVerdict[1]) !== number) return;
      const live = await livePullRequest(github, context, number);
      const markerHead = semanticVerdict ? semanticVerdict[2] : legacyVerdict[1];
      if (!live || markerHead !== live.head?.sha) return;
    }
    const preserveAdvancedPrStage = Boolean(context.payload.issue.pull_request)
      && requestedStage === 'factory:building'
      && ADVANCED_PR_STAGES.has(currentStage);

    // An explicit trusted claim-released marker naming the local worker releases
    // the local lease; any other marker must not replace a live local lease.
    const localLeaseRelease = released && ownerFor(worker) === LOCAL_OWNER_LABEL;

    await reconcileLabels(github, context, number, {
      owner: released ? 'factory:unowned' : worker ? ownerFor(worker) : currentOwner,
      stage: preserveAdvancedPrStage ? currentStage : requestedStage || currentStage,
      approved: semanticVerdict ? semanticVerdict[5] === 'approve'
        : legacyVerdict ? legacyVerdict[2] === 'pass' : undefined,
      releaseLocalLease: localLeaseRelease,
      preserveReadyStage: Boolean(context.payload.issue.pull_request),
    });
    return;
  }

  if (context.eventName === 'pull_request_review_comment') {
    // A thread reply (including Greptile pushback on an answered finding) can
    // reopen a finding; recompute routing from live state.
    await routeGreptileFindings(github, context, context.payload.pull_request.number);
    return;
  }

  if (context.eventName === 'pull_request_target' && context.payload.action === 'edited') {
    // Greptile re-reviews that only rewrite the PR-body summary still route.
    if (!context.payload.changes?.body) return;
    if (!/^\s*<!-- greptile_comment -->\s*$/m.test(context.payload.pull_request?.body || '')) return;
    await routeGreptileFindings(github, context, context.payload.pull_request.number);
    return;
  }

  if (context.eventName === 'pull_request_target') {
    const pullRequest = context.payload.pull_request;
    const sameRepo = pullRequest.head.repo?.full_name === context.payload.repository.full_name;
    const alreadyFactory = (pullRequest.labels || []).some(label => label.name === 'factory');
    const isFactory = alreadyFactory || (
      sameRepo
      && (
        pullRequest.head.ref.startsWith('factory/')
        || (pullRequest.body || '').includes('comic-pile-factory-')
      )
    );
    if (!isFactory) return;

    const current = await currentLabels(github, context, pullRequest.number);
    const prLocalOwner = durablePrOwner(current);
    const action = context.payload.action || '';
    // A push is a new head: it invalidates factory:ci/factory:ready and returns
    // the exact new head to factory:review. Every other PR event must not
    // downgrade an existing stage (PR #5393 lost factory:ready this way).
    const newHead = action === 'synchronize';
    const currentStage = STAGE_LABELS.find(label => current.has(label));
    const stage = newHead || action === 'closed' || !currentStage ? 'factory:review' : currentStage;

    await reconcileLabels(github, context, pullRequest.number, {
      // Local and fixed-model workers write durable PR-local ownership before
      // review workflows refresh visibility. Preserve that stronger signal even
      // when the linked issue has since been released for other work.
      owner: prLocalOwner || await ownerFromLinkedIssue(github, context, pullRequest),
      stage,
      approved: false,
      newHead,
      preserveReadyStage: true,
    });
    return;
  }

  if (context.eventName === 'pull_request_review') {
    const pullRequest = context.payload.pull_request;
    const current = await currentLabels(github, context, pullRequest.number);
    const sameRepo = pullRequest.head.repo?.full_name === context.payload.repository.full_name;
    if (!(current.has('factory') || (sameRepo && pullRequest.head.ref.startsWith('factory/')))) {
      return;
    }

    const currentOwner = [...current].find(isOwnerLabel) || null;
    if (context.payload.action === 'dismissed') {
      await reconcileLabels(github, context, pullRequest.number, {
        owner: currentOwner || await ownerFromLinkedIssue(github, context, pullRequest),
        stage: 'factory:review',
        approved: false,
      });
      return;
    }

    const review = context.payload.review;
    if (trusted(review) && (review.state || '').toUpperCase() === 'CHANGES_REQUESTED') {
      await reconcileLabels(github, context, pullRequest.number, {
        owner: currentOwner || await ownerFromLinkedIssue(github, context, pullRequest),
        stage: 'factory:changes-requested',
        approved: false,
      });
      return;
    }
    // Submitted reviews recompute Greptile findings from live state.
    // Native APPROVED never moves labels by itself.
    await routeGreptileFindings(github, context, pullRequest.number);
    return;
  }
}

module.exports = reconcile;
module.exports.reconcileMissingPrLabels = reconcileMissingPrLabels;
module.exports._test = {
  durablePrOwner,
  preserveLiveLocalLease,
  LOCAL_OWNER_LABEL,
  READY_STAGE_LABEL,
  ownerFor,
  isOwnerLabel,
  hasManualOnlyDirective,
  BLOCKED_LABELS,
  STAGE_LABELS,
  DERIVED_LABELS,
  MERGE_CONFLICT_LABEL,
  APPROVAL_LABEL,
  mergeConflictState,
  derivedLabelsFor,
  labelsBlockEnrollment,
  livePullRequest,
  reconcileLabels,
  withRetry,
  rateLimitDelay,
  workerFrom,
  trusted,
  isRepairRoutingReviewer,
  unresolvedReviewerBotThreads,
  greptileReviewThreads,
  greptileThreadState,
  greptileSummaryFindings,
  openSummaryFindings,
  routeGreptileFindings,
  isGreptileAck,
  isTrustedResponder,
  summaryFindingId,
  livePullRequestHead,
  REVIEW_THREAD_PAGE_LIMIT,
  REPAIR_ROUTING_REVIEWER_BOTS,
  performedViaUntrustedApp,
  TRUSTED_APP_SLUGS,
  TRUSTED_ASSOCIATIONS,
  EXPECTED_WORKER_IDS,
  FIXED_MODEL_OWNER_LABELS,
  WORKER_OWNER_LABELS,
  restCall,
  restRequestLog,
  restRequestCounts,
  clearRestRequestLog,
  currentLabels,
};
