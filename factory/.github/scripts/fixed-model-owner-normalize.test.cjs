const assert = require('node:assert/strict');
const test = require('node:test');

const normalize = require('./fixed-model-owner-normalize.cjs');

function harness({ labels, comments = [], branch = 'factory/39-1070-opencode-free' }) {
  const calls = { comments: 0, labels: [] };
  const listLabelsOnIssue = async () => {};
  const listComments = async () => {};
  const github = {
    paginate: async (fn) => {
      if (fn === listLabelsOnIssue) return labels.map((name) => ({ name }));
      if (fn === listComments) {
        calls.comments += 1;
        return comments.map((body) => ({ body }));
      }
      throw new Error('unexpected paginate function');
    },
    rest: {
      pulls: {
        get: async () => ({ data: { head: { ref: branch } } }),
      },
      issues: {
        listLabelsOnIssue,
        listComments,
        setLabels: async ({ labels: next }) => {
          calls.labels.push(next);
        },
      },
    },
  };
  const context = {
    payload: { issue: { number: 1230, pull_request: {} } },
    repo: { owner: 'JoshCLWren', repo: 'comic-pile' },
  };
  const core = { info: () => {} };
  return { github, context, core, calls };
}

test('current numeric owner outranks branch provenance during cross-worker handoff', async () => {
  const state = harness({
    labels: ['factory', 'factory:46', 'factory:changes-requested'],
    comments: ['<!-- free-model-factory-owner:46 -->'],
  });

  await normalize(state);

  assert.equal(state.calls.comments, 0);
  assert.equal(state.calls.labels.length, 1);
  assert.ok(state.calls.labels[0].includes('factory:46'));
  assert.ok(!state.calls.labels[0].includes('factory:39'));
});

test('explicit unowned handoff is not resurrected from branch provenance', async () => {
  const state = harness({
    labels: ['factory', 'factory:unowned', 'factory:review'],
    comments: ['<!-- free-model-factory-owner:39 -->'],
  });

  await normalize(state);

  assert.equal(state.calls.comments, 0);
  assert.equal(state.calls.labels.length, 0);
});

test('ownership marker bootstraps an unlabeled PR before branch provenance', async () => {
  const state = harness({
    labels: ['factory:review'],
    comments: [
      '<!-- free-model-factory-owner:39 -->',
      '<!-- free-model-factory-owner:46 -->',
    ],
  });

  await normalize(state);

  assert.equal(state.calls.comments, 1);
  assert.ok(state.calls.labels[0].includes('factory:46'));
  assert.ok(!state.calls.labels[0].includes('factory:39'));
});

test('branch provenance remains a last-resort bootstrap', async () => {
  const state = harness({ labels: ['factory:review'] });

  await normalize(state);

  assert.equal(state.calls.comments, 1);
  assert.ok(state.calls.labels[0].includes('factory:39'));
});

test('three-digit roster owners survive normalization and branch recovery', async () => {
  const owned = harness({
    labels: ['factory', 'factory:104', 'factory:review'],
    comments: ['<!-- free-model-factory-owner:104 -->'],
    branch: 'factory/107-4960-opencode-free',
  });
  await normalize(owned);
  assert.equal(owned.calls.comments, 0);
  assert.equal(owned.calls.labels.length, 1);
  assert.ok(owned.calls.labels[0].includes('factory:104'));
  assert.ok(!owned.calls.labels[0].includes('factory:107'));

  const fromBranch = harness({
    labels: ['factory:review'],
    branch: 'factory/107-4960-opencode-free',
  });
  await normalize(fromBranch);
  assert.ok(fromBranch.calls.labels[0].includes('factory:107'));

  const fromMarker = harness({
    labels: ['factory:review'],
    comments: ['<!-- free-model-factory-owner:105 -->'],
    branch: 'factory/107-4960-opencode-free',
  });
  await normalize(fromMarker);
  assert.ok(fromMarker.calls.labels[0].includes('factory:105'));
  assert.ok(!fromMarker.calls.labels[0].includes('factory:107'));
});
