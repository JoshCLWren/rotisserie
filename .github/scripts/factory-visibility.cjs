const WORKER_OWNER_LABELS = Array.from({ length: 71 }, (_, index) => `factory:${index + 1}`);

const DEFINITIONS = {
  factory: ['5319E7', 'Work owned or produced by an autonomous ComicPile factory'],
  ...Object.fromEntries(WORKER_OWNER_LABELS.map((name, index) => [
    name,
    ['0366D6', `Current next-action owner is ComicPile Factory ${index + 1}`],
  ])),
  'factory:local': ['0366D6', 'Current next-action owner is the local OpenCode factory'],
  'factory:unowned': ['BFDADC', 'Factory work has no current next-action owner'],
  'factory:building': ['FBCA04', 'A factory is actively implementing or repairing this work'],
  'factory:review': ['D4C5F9', 'The exact current head needs review or re-review'],
  'factory:changes-requested': ['D73A4A', 'Actionable review findings currently block progress'],
  'factory:ci': ['1D76DB', 'Review passed and required exact-head checks are being verified'],
  'factory:ready': ['0E8A16', 'All exact-head factory merge gates are satisfied'],
  'factory:blocked': ['B60205', 'A genuine human, credential, or external blocker remains'],
};

const OWNER_LABELS = [
  ...WORKER_OWNER_LABELS,
  'factory:local',
  'factory:unowned',
];
const STAGE_LABELS = [
  'factory:building',
  'factory:review',
  'factory:changes-requested',
  'factory:ci',
  'factory:ready',
  'factory:blocked',
];
const ADVANCED_PR_STAGES = new Set(['factory:review', 'factory:ci', 'factory:ready']);
const TRUSTED_ASSOCIATIONS = new Set(['OWNER', 'MEMBER', 'COLLABORATOR']);

function trusted(login, association) {
  return TRUSTED_ASSOCIATIONS.has(association)
    || login === 'coderabbitai[bot]'
    || login === 'coderabbitai';
}

function workerFrom(body) {
  for (const pattern of [
    /comic-pile-factory-implement-(?:claim|progress)-v\d+:issue-\d+:([^:\s>]+):/,
    /comic-pile-factory-review-claim-v\d+:[^:\s>]+:([^:\s>]+):/,
    /comic-pile-factory-fix-(?:claim|progress)-v\d+:[^:\s>]+:([^:\s>]+):/,
    /comic-pile-factory-claim-released-v\d+:[^:\s>]+:([^:\s>]+):/,
  ]) {
    const match = body.match(pattern);
    if (match) return match[1];
  }
  return null;
}

function ownerFor(worker) {
  const scheduled = worker?.match(/^chatgpt-factory-([1-5])$/);
  if (scheduled) return `factory:${scheduled[1]}`;

  const fixedModel = worker?.match(/^opencode-(?:free-model|nvidia)-factory-(\d+)$/);
  if (fixedModel) {
    const number = Number(fixedModel[1]);
    if (number >= 6 && number <= 71) return `factory:${number}`;
  }

  if (worker === 'local' || worker === 'local-opencode' || worker?.startsWith('local-opencode-')) {
    return 'factory:local';
  }
  return 'factory:unowned';
}

function durablePrOwner(current) {
  return ['factory:local', ...WORKER_OWNER_LABELS.slice(5)]
    .find(label => current.has(label)) || null;
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

function isTransient(error) {
  return error?.status === 429 || (error?.status >= 500 && error?.status <= 599);
}

async function withRetry(operation, { attempts = 3, delay = 250 } = {}) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await operation();
    } catch (error) {
      lastError = error;
      if (!isTransient(error) || attempt === attempts) throw error;
      await new Promise(resolve => setTimeout(resolve, delay * attempt));
    }
  }
  throw lastError;
}

async function currentLabels(github, context, number) {
  const labels = await withRetry(() => github.paginate(github.rest.issues.listLabelsOnIssue, {
    owner: context.repo.owner,
    repo: context.repo.repo,
    issue_number: number,
    per_page: 100,
  }));
  return new Set(labels.map(label => label.name));
}

async function reconcileLabels(github, context, number, { owner, stage }) {
  const current = await currentLabels(github, context, number);
  const next = [...current].filter(
    label => !OWNER_LABELS.includes(label) && !STAGE_LABELS.includes(label),
  );
  if (!next.includes('factory')) next.push('factory');
  if (owner) next.push(owner);
  if (stage) next.push(stage);

  await withRetry(() => github.rest.issues.setLabels({
    owner: context.repo.owner,
    repo: context.repo.repo,
    issue_number: number,
    labels: next,
  }));
}

async function ensureLabels(github, context) {
  const existing = await withRetry(() => github.paginate(github.rest.issues.listLabelsForRepo, {
    owner: context.repo.owner,
    repo: context.repo.repo,
    per_page: 100,
  }));
  const byName = new Map(existing.map(label => [label.name, label]));
  for (const [name, [color, description]] of Object.entries(DEFINITIONS)) {
    const current = byName.get(name);
    if (!current) {
      await withRetry(() => github.rest.issues.createLabel({
        owner: context.repo.owner,
        repo: context.repo.repo,
        name,
        color,
        description,
      }));
    } else if (
      current.color.toUpperCase() !== color
      || (current.description || '') !== description
    ) {
      await withRetry(() => github.rest.issues.updateLabel({
        owner: context.repo.owner,
        repo: context.repo.repo,
        name,
        new_name: name,
        color,
        description,
      }));
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

  const comments = await withRetry(() => github.paginate(github.rest.issues.listComments, {
    owner: context.repo.owner,
    repo: context.repo.repo,
    issue_number: issueNumber,
    per_page: 100,
  }));
  comments.sort((left, right) => new Date(right.created_at) - new Date(left.created_at));
  for (const comment of comments) {
    if (!trusted(comment.user?.login, comment.author_association)) continue;
    const body = comment.body || '';
    if (/comic-pile-factory-claim-released-v\d+:/.test(body)) return 'factory:unowned';
    const worker = workerFrom(body);
    if (worker) return ownerFor(worker);
  }
  return 'factory:unowned';
}

async function reconcile({ github, context }) {
  await ensureLabels(github, context);

  if (context.eventName === 'issue_comment') {
    const comment = context.payload.comment;
    const body = comment?.body || '';
    if (!body.includes('comic-pile-factory-')) return;
    if (!trusted(comment?.user?.login, comment?.author_association)) return;

    const number = context.payload.issue.number;
    const current = await currentLabels(github, context, number);
    const released = /comic-pile-factory-claim-released-v\d+:/.test(body);
    const worker = workerFrom(body);
    const currentOwner = OWNER_LABELS.find(label => current.has(label));
    const currentStage = STAGE_LABELS.find(label => current.has(label));
    const requestedStage = stageFrom(body);
    const preserveAdvancedPrStage = Boolean(context.payload.issue.pull_request)
      && requestedStage === 'factory:building'
      && ADVANCED_PR_STAGES.has(currentStage);

    await reconcileLabels(github, context, number, {
      owner: released ? 'factory:unowned' : worker ? ownerFor(worker) : currentOwner,
      stage: preserveAdvancedPrStage ? currentStage : requestedStage || currentStage,
    });
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

    await reconcileLabels(github, context, pullRequest.number, {
      // Local and fixed-model workers write durable PR-local ownership before
      // review workflows refresh visibility. Preserve that stronger signal even
      // when the linked issue has since been released for other work.
      owner: prLocalOwner || await ownerFromLinkedIssue(github, context, pullRequest),
      stage: 'factory:review',
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

    const currentOwner = OWNER_LABELS.find(label => current.has(label));
    if (context.payload.action === 'dismissed') {
      await reconcileLabels(github, context, pullRequest.number, {
        owner: currentOwner || await ownerFromLinkedIssue(github, context, pullRequest),
        stage: 'factory:review',
      });
      return;
    }

    const review = context.payload.review;
    if (!trusted(review.user?.login, review.author_association)) return;
    const state = (review.state || '').toUpperCase();
    const body = review.body || '';
    let stage = 'factory:review';
    if (state === 'CHANGES_REQUESTED') stage = 'factory:changes-requested';
    else if (state === 'APPROVED') stage = 'factory:ci';
    else if (/Actionable comments posted:\s*[1-9]\d*/i.test(body)) {
      stage = 'factory:changes-requested';
    }
    await reconcileLabels(github, context, pullRequest.number, {
      owner: currentOwner || await ownerFromLinkedIssue(github, context, pullRequest),
      stage,
    });
  }
}

module.exports = reconcile;
module.exports._test = {
  durablePrOwner,
  ownerFor,
  reconcileLabels,
  withRetry,
};
