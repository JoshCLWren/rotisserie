const assert = require('node:assert/strict');
const test = require('node:test');

const reconcile = require('./factory-visibility.cjs');
const { durablePrOwner } = reconcile._test;

test('durable PR owner preserves local and fixed-model leases', () => {
  assert.equal(durablePrOwner(new Set(['factory:local'])), 'factory:local');
  assert.equal(durablePrOwner(new Set(['factory:13'])), 'factory:13');
  assert.equal(durablePrOwner(new Set(['factory:46'])), 'factory:46');
});

test('durable PR owner does not pin scheduled or unowned leases', () => {
  assert.equal(durablePrOwner(new Set(['factory:5'])), null);
  assert.equal(durablePrOwner(new Set(['factory:unowned'])), null);
});
