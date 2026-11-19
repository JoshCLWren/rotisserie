const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {
  loadExpectedWorkerIds,
  isExpectedWorkerId,
  defaultLockPath,
  defaultRosterPath,
  SCHEDULED_WORKER_IDS,
} = require('./factory-expected-workers.cjs');

test('active provider slots are derived without an inventory file', () => {
  assert.equal(defaultLockPath(), null);
  assert.equal(defaultRosterPath(), null);
  const loaded = loadExpectedWorkerIds();
  assert(loaded.expectedWorkers.size > 0);
  assert.deepEqual([...loaded.retiredWorkers], []);
  assert.deepEqual([...loaded.expectedWorkers], [...loaded.rosterWorkers]);
  for (const id of loaded.expectedWorkers) {
    assert(Number.isSafeInteger(id));
    assert(isExpectedWorkerId(id, loaded));
  }
  assert(!isExpectedWorkerId(0, loaded));
  assert.deepEqual([...SCHEDULED_WORKER_IDS], [1, 2, 3, 4, 5]);
});

test('historical lock/roster files are supported only when explicitly passed', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'factory-slots-test-'));
  try {
    const lockPath = path.join(dir, 'lock.json');
    const rosterPath = path.join(dir, 'workers.tsv');
    fs.writeFileSync(lockPath, JSON.stringify({
      schema_version: 1, expected_workers: [321], retired_workers: [654],
    }));
    fs.writeFileSync(rosterPath, '321\tprovider\ttest-model\n');
    const loaded = loadExpectedWorkerIds({ lockPath, rosterPath });
    assert.deepEqual([...loaded.expectedWorkers], [321]);
    assert.deepEqual([...loaded.retiredWorkers], [654]);
    assert.deepEqual([...loaded.rosterWorkers], [321]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('GitHub owner labels derive from provider workers and controller identities', () => {
  const visibility = require('./factory-visibility.cjs');
  const { FIXED_MODEL_OWNER_LABELS, WORKER_OWNER_LABELS, EXPECTED_WORKER_IDS } = visibility._test;
  assert.equal(FIXED_MODEL_OWNER_LABELS.length, EXPECTED_WORKER_IDS.size);
  const loaded = loadExpectedWorkerIds();
  for (const worker of loaded.expectedWorkers) {
    assert(FIXED_MODEL_OWNER_LABELS.includes(`factory:${worker}`));
  }
  assert.equal(
    WORKER_OWNER_LABELS.length,
    SCHEDULED_WORKER_IDS.length + EXPECTED_WORKER_IDS.size,
  );
});
