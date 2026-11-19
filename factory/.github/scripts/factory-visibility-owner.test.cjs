const assert = require('node:assert/strict');
const test = require('node:test');

const reconcile = require('./factory-visibility.cjs');
const {
  durablePrOwner,
  preserveLiveLocalLease,
  LOCAL_OWNER_LABEL,
  READY_STAGE_LABEL,
} = reconcile._test;

test('durable PR owner preserves local and fixed-model leases', () => {
  assert.equal(durablePrOwner(new Set(['factory:local'])), 'factory:local');
  assert.equal(durablePrOwner(new Set(['factory:13'])), 'factory:13');
  assert.equal(durablePrOwner(new Set(['factory:46'])), 'factory:46');
  assert.equal(durablePrOwner(new Set(['factory:76'])), 'factory:76');
});

test('durable PR owner does not pin scheduled or unowned leases', () => {
  assert.equal(durablePrOwner(new Set(['factory:5'])), null);
  assert.equal(durablePrOwner(new Set(['factory:unowned'])), null);
});

test('a live local lease is never replaced by unowned or another owner', () => {
  const current = new Set(['factory', 'factory:local', 'factory:ready']);
  // The reconciler computed unowned from a stale read or an off-roster marker.
  const held = preserveLiveLocalLease(
    current,
    { owner: 'factory:unowned', stage: 'factory:review' },
    { preserveReadyStage: true },
  );
  assert.equal(held.owner, 'factory:local');
  assert.equal(held.stage, 'factory:ready');
});

test('a live local lease keeps the ready merge gate on non-push events', () => {
  const current = new Set(['factory', 'factory:local', 'factory:ready']);
  const held = preserveLiveLocalLease(
    current,
    { owner: 'factory:local', stage: 'factory:review' },
    { preserveReadyStage: true },
  );
  assert.equal(held.owner, LOCAL_OWNER_LABEL);
  assert.equal(held.stage, READY_STAGE_LABEL);
});

test('a new head still invalidates ready but keeps the local owner', () => {
  const current = new Set(['factory', 'factory:local', 'factory:ready']);
  const pushed = preserveLiveLocalLease(
    current,
    { owner: 'factory:unowned', stage: 'factory:review' },
    { newHead: true, preserveReadyStage: true },
  );
  assert.equal(pushed.owner, 'factory:local');
  assert.equal(pushed.stage, 'factory:review');
});

test('an explicit local lease release is honored', () => {
  const current = new Set(['factory', 'factory:local', 'factory:review']);
  const released = preserveLiveLocalLease(
    current,
    { owner: 'factory:unowned', stage: 'factory:review' },
    { releaseLocalLease: true, preserveReadyStage: true },
  );
  assert.equal(released.owner, 'factory:unowned');
  assert.equal(released.stage, 'factory:review');
});

test('targets without a local lease keep the requested owner and stage', () => {
  const current = new Set(['factory', 'factory:unowned', 'factory:review']);
  const kept = preserveLiveLocalLease(
    current,
    { owner: 'factory:unowned', stage: 'factory:review' },
    { preserveReadyStage: true },
  );
  assert.deepEqual(kept, { owner: 'factory:unowned', stage: 'factory:review' });
  const numbered = preserveLiveLocalLease(
    new Set(['factory', 'factory:13', 'factory:review']),
    { owner: 'factory:unowned', stage: 'factory:review' },
    { preserveReadyStage: true },
  );
  assert.equal(numbered.owner, 'factory:unowned');
});

test('ready is preserved only while a local lease is live and held', () => {
  // Without preserveReadyStage (genuine review routing) the stage still moves.
  const routed = preserveLiveLocalLease(
    new Set(['factory', 'factory:local', 'factory:ready']),
    { owner: 'factory:local', stage: 'factory:changes-requested' },
    {},
  );
  assert.equal(routed.stage, 'factory:changes-requested');
  // A local lease without ready does not invent a stage.
  const noStage = preserveLiveLocalLease(
    new Set(['factory', 'factory:local']),
    { owner: 'factory:unowned', stage: 'factory:review' },
    { preserveReadyStage: true },
  );
  assert.equal(noStage.owner, 'factory:local');
  assert.equal(noStage.stage, 'factory:review');
});
