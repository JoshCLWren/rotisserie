const OWNER_RE = /^factory:(?:unowned|local|[1-9][0-9]?)$/;
const STAGE_RE = /^factory:(?:building|review|changes-requested|ci|ready|blocked)$/;
const WORKER_BRANCH_RE = /^factory\/([1-9][0-9]?)-/;
const MARKER_RE = /<!-- (?:free-model-factory-owner|nvidia-factory-owner|omniroute-factory-owner):([1-9][0-9]?) -->/g;

async function normalize({ github, context, core }) {
  const pullRequest = context.payload.pull_request;
  const number = pullRequest?.number || (context.payload.issue?.pull_request ? context.payload.issue.number : null);
  if (!number) return;

  const { data: pr } = pullRequest
    ? { data: pullRequest }
    : await github.rest.pulls.get({
      owner: context.repo.owner,
      repo: context.repo.repo,
      pull_number: number,
    });

  const labels = await github.paginate(github.rest.issues.listLabelsOnIssue, {
    owner: context.repo.owner,
    repo: context.repo.repo,
    issue_number: number,
    per_page: 100,
  });
  const names = labels.map(label => label.name);
  const numericOwners = names.filter(name => /^factory:[1-9][0-9]?$/.test(name));

  // A current lease is authoritative. Branch names record provenance only and
  // must not steal a PR back after another factory adopts it.
  let worker = numericOwners.length === 1 ? numericOwners[0].split(':')[1] : '';

  // An explicit handoff to unowned is also authoritative. Do not resurrect a
  // historical owner from the branch name or an older ownership marker.
  if (!worker && names.includes('factory:unowned')) {
    core.info(`Preserving factory:unowned on PR #${number}`);
    return;
  }

  if (!worker) {
    const comments = await github.paginate(github.rest.issues.listComments, {
      owner: context.repo.owner,
      repo: context.repo.repo,
      issue_number: number,
      per_page: 100,
    });
    for (const comment of comments) {
      for (const match of (comment.body || '').matchAll(MARKER_RE)) worker = match[1];
    }
  }
  if (!worker) worker = pr.head?.ref?.match(WORKER_BRANCH_RE)?.[1] || '';
  if (!worker) return;

  const owner = `factory:${worker}`;
  const stage = names.find(name => STAGE_RE.test(name)) || 'factory:review';
  const next = names.filter(name => !OWNER_RE.test(name) && !STAGE_RE.test(name) && name !== 'factory');
  next.push('factory', owner, stage);

  await github.rest.issues.setLabels({
    owner: context.repo.owner,
    repo: context.repo.repo,
    issue_number: number,
    labels: [...new Set(next)],
  });
  core.info(`Normalized PR #${number} to ${owner} with ${stage}`);
}

module.exports = normalize;
