'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

// These are independently scheduled controller identities, not model pins.
const SCHEDULED_WORKER_IDS = Object.freeze([1, 2, 3, 4, 5]);

function defaultLockPath() {
  return null; // Retired: no checked-in expected-worker lock.
}

function defaultRosterPath() {
  return null; // Retired: no checked-in TSV roster.
}

let cachedSlots = null;

function runtimeSlots() {
  if (cachedSlots !== null) return cachedSlots;
  const output = execFileSync(
    'python3', [path.join(__dirname, 'factory_worker_slots.py'), 'list'],
    { encoding: 'utf8', timeout: 15000, maxBuffer: 1024 * 1024 },
  );
  const slots = JSON.parse(output);
  if (!Array.isArray(slots) || slots.some(
    (entry) => !entry || !/^\d+$/.test(String(entry.worker)) || !entry.source,
  )) {
    throw new Error('Untrusted provider-slot output');
  }
  cachedSlots = slots;
  return cachedSlots;
}

function readRosterWorkerIds(rosterPath = defaultRosterPath()) {
  if (rosterPath === null) {
    return new Set(runtimeSlots().map((entry) => Number(entry.worker)));
  }
  // Historical test fixture only, never a production model inventory.
  const workers = new Set();
  for (const line of fs.readFileSync(rosterPath, 'utf8').split(/\r?\n/)) {
    const raw = line.split('\t')[0]?.trim();
    if (raw && /^\d+$/.test(raw)) workers.add(Number(raw));
  }
  return workers;
}

function readLock(lockPath = defaultLockPath()) {
  if (lockPath === null) {
    return { expected: readRosterWorkerIds(), retired: new Set(), schemaVersion: 1 };
  }
  // Explicit fixture path supports old parser regression tests.
  const payload = JSON.parse(fs.readFileSync(lockPath, 'utf8'));
  const expected = new Set(
    Array.isArray(payload.expected_workers)
      ? payload.expected_workers.map(Number).filter(Number.isSafeInteger)
      : [],
  );
  const retired = new Set(
    Array.isArray(payload.retired_workers)
      ? payload.retired_workers.map(Number).filter(Number.isSafeInteger)
      : [],
  );
  return { expected, retired, schemaVersion: payload.schema_version };
}

function loadExpectedWorkerIds({
  lockPath = defaultLockPath(),
  rosterPath = defaultRosterPath(),
} = {}) {
  if (lockPath === null && rosterPath === null) {
    const ids = new Set(runtimeSlots().map((entry) => Number(entry.worker)));
    return {
      expectedWorkers: ids,
      retiredWorkers: new Set(),
      rosterWorkers: new Set(ids),
      scheduledWorkerIds: new Set(SCHEDULED_WORKER_IDS),
    };
  }
  const { expected, retired } = readLock(lockPath);
  const rosterWorkers = readRosterWorkerIds(rosterPath);
  return {
    expectedWorkers: expected.size > 0 ? expected : rosterWorkers,
    retiredWorkers: retired,
    rosterWorkers,
    scheduledWorkerIds: new Set(SCHEDULED_WORKER_IDS),
  };
}

function isExpectedWorkerId(workerId, loaded = loadExpectedWorkerIds()) {
  return loaded.expectedWorkers.has(Number(workerId));
}

module.exports = {
  SCHEDULED_WORKER_IDS,
  defaultLockPath,
  defaultRosterPath,
  loadExpectedWorkerIds,
  isExpectedWorkerId,
  readRosterWorkerIds,
  readLock,
};
