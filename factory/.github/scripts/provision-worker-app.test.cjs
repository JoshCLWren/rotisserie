'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { once } = require('node:events');
const { savePrivate, manifest, validateCredentials, finish, serverFor } = require('./provision-worker-app.cjs');
const original = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../factory-worker-github-apps.json'), 'utf8'));
const scaffold = structuredClone(original);
Object.assign(scaffold.workers['48'], { app_id: null, app_login: null, installation_id: null });
const entry = scaffold.workers['48'];
const { privateKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
const credentials = { id: 123, name: 'Mark Cordova', slug: 'mark-cordova', owner: { login: 'JoshCLWren' }, pem: privateKey.export({ type: 'pkcs1', format: 'pem' }) };

function temporary(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'worker-app-test-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function apiStub(overrides = {}) {
  return async endpoint => {
    if (endpoint.endsWith('/installation')) return { id: 456, app_id: 123, account: { login: 'JoshCLWren' }, repository_selection: 'selected', suspended_at: null, permissions: { contents: 'write', pull_requests: 'write', issues: 'write' }, ...overrides };
    if (endpoint.endsWith('/access_tokens')) return { token: 'test-installation-token' };
    if (endpoint === '/installation/repositories') return { total_count: 1, repositories: [{ full_name: 'JoshCLWren/comic-pile' }] };
    throw new Error(`Unexpected endpoint ${endpoint}`);
  };
}

test('manifest scopes the pilot to its persisted name and readable handoff permissions', () => {
  const value = manifest(entry, 'http://127.0.0.1:8765');
  assert.equal(value.name, 'Mark Cordova');
  assert.equal(value.public, false);
  assert.equal(value.hook_attributes, undefined);
  assert.deepEqual(value.default_permissions, { contents: 'write', pull_requests: 'write', issues: 'write', metadata: 'read' });
  assert.match(value.description, /Autonomous ComicPile Factory worker/);
  assert.equal(value.redirect_url, 'http://127.0.0.1:8765/callback');
});

test('credential validation rejects another account, renamed App, and invalid key', () => {
  validateCredentials(credentials, 'Mark Cordova');
  assert.throws(() => validateCredentials({ ...credentials, owner: { login: 'someone-else' } }, 'Mark Cordova'));
  assert.throws(() => validateCredentials(credentials, 'Different Name'));
  assert.throws(() => validateCredentials({ ...credentials, pem: 'INVALID PRIVATE KEY' }, 'Mark Cordova'));
});

test('private state is atomically saved with owner-only permissions', t => {
  const file = path.join(temporary(t), 'nested/state.json');
  savePrivate(file, credentials);
  assert.equal(fs.statSync(file).mode & 0o777, 0o600);
  assert.equal(fs.statSync(path.dirname(file)).mode & 0o777, 0o700);
  assert.equal(JSON.parse(fs.readFileSync(file)).id, 123);
});

test('finish uploads only worker 48 secrets through stdin and persists no key', async t => {
  const file = path.join(temporary(t), 'mapping.json');
  savePrivate(file, scaffold);
  const calls = [];
  const result = await finish(credentials, { mappingFile: file, request: apiStub(), runner: (command, args, options) => { calls.push({ command, args, options }); return { status: 0 }; } });
  assert.equal(result.app_login, 'mark-cordova[bot]');
  assert.equal(calls.length, 3);
  assert.deepEqual(calls.map(call => call.args[2]), ['FACTORY_WORKER_48_APP_PRIVATE_KEY', 'FACTORY_WORKER_48_INSTALLATION_ID', 'FACTORY_WORKER_48_APP_ID']);
  assert.equal(calls[0].options.input, credentials.pem);
  assert.ok(calls.every(call => !call.args.includes(credentials.pem)));
  const persisted = fs.readFileSync(file, 'utf8');
  assert.ok(!persisted.includes('PRIVATE KEY'));
  assert.equal(fs.statSync(file).mode & 0o777, 0o644);
  assert.deepEqual(Object.keys(JSON.parse(persisted).workers), ['48']);
});

test('finish rejects wrong App and broad installations before any upload', async t => {
  const file = path.join(temporary(t), 'mapping.json');
  savePrivate(file, scaffold);
  let uploads = 0;
  const runner = () => { uploads++; return { status: 0 }; };
  for (const overrides of [{ app_id: 999 }, { repository_selection: 'all' }, { account: { login: 'another-owner' } }, { suspended_at: 'today' }, { permissions: { contents: 'read' } }]) {
    await assert.rejects(finish(credentials, { mappingFile: file, request: apiStub(overrides), runner }));
  }
  assert.equal(uploads, 0);
});

test('partial secret failure keeps credentials resumable and mapping disabled', async t => {
  const file = path.join(temporary(t), 'mapping.json');
  savePrivate(file, scaffold);
  let calls = 0;
  await assert.rejects(finish(credentials, { mappingFile: file, request: apiStub(), runner: () => ({ status: ++calls === 2 ? 1 : 0 }) }), /run finish again/);
  assert.equal(JSON.parse(fs.readFileSync(file)).workers['48'].app_id, null);
  await finish(credentials, { mappingFile: file, request: apiStub(), runner: () => ({ status: 0 }) });
  assert.equal(JSON.parse(fs.readFileSync(file)).workers['48'].app_id, 123);
});

async function serve(t, request) {
  const dir = temporary(t);
  const stateFile = path.join(dir, 'registration.json');
  const credentialsFile = path.join(dir, 'credentials.json');
  savePrivate(stateFile, { nonce: 'test-nonce' });
  // Bind first to obtain an unused local port, then update the base used by the handler.
  const net = require('node:net');
  const reservation = net.createServer();
  reservation.listen(0, '127.0.0.1');
  await once(reservation, 'listening');
  const port = reservation.address().port;
  await new Promise(resolve => reservation.close(resolve));
  const base = `http://127.0.0.1:${port}`;
  const server = serverFor({ stateFile, credentialsFile, entry, base, request });
  server.listen(port, '127.0.0.1');
  await once(server, 'listening');
  t.after(() => new Promise(resolve => { server.closeAllConnections(); server.close(resolve); }));
  return { base, credentialsFile };
}

test('callback validates nonce before exchanging a code and never exposes private credentials', async t => {
  let conversions = 0;
  const { base, credentialsFile } = await serve(t, async endpoint => { conversions++; assert.match(endpoint, /app-manifests\/validcode\/conversions/); return credentials; });
  const bad = await fetch(`${base}/callback?state=wrong&code=validcode`);
  assert.equal(bad.status, 403);
  assert.equal(conversions, 0);
  const good = await fetch(`${base}/callback?state=test-nonce&code=validcode`, { redirect: 'manual' });
  assert.equal(good.status, 303);
  assert.equal(conversions, 1);
  assert.equal(JSON.parse(fs.readFileSync(credentialsFile)).id, 123);
  const html = await (await fetch(base)).text();
  assert.match(html, /mark-cordova\/installations\/new/);
  assert.ok(!html.includes('PRIVATE KEY'));
  const repeated = await fetch(`${base}/callback?state=test-nonce&code=validcode`, { redirect: 'manual' });
  assert.equal(repeated.status, 303);
  assert.equal(conversions, 1);
});

test('local server rejects foreign Host headers and exposes no credentials endpoint', async t => {
  const { base } = await serve(t, async () => credentials);
  const foreign = await new Promise((resolve, reject) => {
    const req = require('node:http').get(base, { headers: { Host: 'evil.example' } }, res => { res.resume(); resolve(res.statusCode); });
    req.on('error', reject);
  });
  assert.equal(foreign, 403);
  assert.equal((await fetch(`${base}/credentials.json`)).status, 404);
  assert.equal((await fetch(base, { method: 'POST' })).status, 405);
  assert.equal((await fetch(`${base}/installed`)).status, 409);
});
