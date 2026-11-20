#!/usr/bin/env node
'use strict';

// Interactive owner-run provisioning only; scheduled workers must not invoke this tool.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');

const REPO = 'JoshCLWren/comic-pile';
const ROOT = path.resolve(__dirname, '../..');
const MAPPING = path.join(ROOT, '.github/factory-worker-github-apps.json');
const DEFAULT_DIR = path.join(os.homedir(), '.local/state/comic-pile/worker-app-48');

function savePrivate(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const temporary = `${file}.${crypto.randomBytes(8).toString('hex')}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(value, null, 2) + '\n', { mode: 0o600, flag: 'wx' });
  fs.renameSync(temporary, file);
}

function savePublic(file, value) {
  const temporary = `${file}.${crypto.randomBytes(8).toString('hex')}.tmp`;
  fs.writeFileSync(temporary, JSON.stringify(value, null, 2) + '\n', { mode: 0o644, flag: 'wx' });
  fs.renameSync(temporary, file);
}

function load(file) {
  return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : null;
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
}

function appJwt(credentials) {
  const encode = value => Buffer.from(JSON.stringify(value)).toString('base64url');
  const now = Math.floor(Date.now() / 1000);
  const body = `${encode({ alg: 'RS256', typ: 'JWT' })}.${encode({ iat: now - 60, exp: now + 540, iss: credentials.id })}`;
  return `${body}.${crypto.sign('RSA-SHA256', Buffer.from(body), credentials.pem).toString('base64url')}`;
}

async function api(endpoint, { token, body, fetcher = fetch } = {}) {
  const response = await fetcher(`https://api.github.com${endpoint}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
      'User-Agent': 'comic-pile-owner-provisioning',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(30000),
  });
  // Never include the response body: manifest responses contain private keys.
  if (!response.ok) throw new Error(`GitHub API returned HTTP ${response.status}`);
  return response.json();
}

function validateCredentials(value, expectedName) {
  if (!value || !Number.isSafeInteger(value.id) || value.id < 1 ||
      value.name !== expectedName || !/^[a-z0-9-]+$/.test(value.slug || '') ||
      value.owner?.login !== 'JoshCLWren' || typeof value.pem !== 'string' ||
      !value.pem.includes('PRIVATE KEY')) {
    throw new Error('App response does not match the expected owner and worker identity');
  }
  crypto.createPrivateKey(value.pem);
}

function manifest(entry, base) {
  return {
    name: entry.display_name,
    url: `https://github.com/${REPO}`,
    description: entry.profile,
    public: false,
    redirect_url: `${base}/callback`,
    setup_url: `${base}/installed`,
    default_permissions: { contents: 'write', pull_requests: 'write', issues: 'write', metadata: 'read' },
    default_events: [],
  };
}

async function finish(credentials, { runner = spawnSync, request = api, mappingFile = MAPPING } = {}) {
  const jwt = appJwt(credentials);
  const installation = await request(`/repos/${REPO}/installation`, { token: jwt });
  if (installation.app_id !== credentials.id || installation.account?.login !== 'JoshCLWren' ||
      installation.repository_selection !== 'selected' || installation.suspended_at ||
      !Number.isSafeInteger(installation.id) || installation.id < 1 ||
      installation.permissions?.contents !== 'write' ||
      installation.permissions?.pull_requests !== 'write' ||
      installation.permissions?.issues !== 'write') {
    throw new Error('Install this worker App on only the comic-pile repository, then retry');
  }
  const minted = await request(`/app/installations/${installation.id}/access_tokens`, {
    token: jwt, body: {},
  });
  if (typeof minted.token !== 'string' || !minted.token) throw new Error('Installation token missing');
  const repositories = await request('/installation/repositories', { token: minted.token });
  if (repositories.total_count !== 1 || repositories.repositories?.[0]?.full_name !== REPO) {
    throw new Error('Installation token does not have the expected repository scope');
  }
  const mapping = load(mappingFile);
  const entry = mapping?.workers?.['48'];
  if (!entry || entry.display_name !== credentials.name ||
      (entry.app_id && Number(entry.app_id) !== credentials.id)) {
    throw new Error('Persisted worker mapping conflicts with the App identity');
  }
  const values = [
    [entry.private_key_secret, credentials.pem],
    [entry.installation_id_secret, String(installation.id)],
    [entry.app_id_secret, String(credentials.id)],
  ];
  for (const [name, input] of values) {
    if (!/^FACTORY_WORKER_48_(APP_PRIVATE_KEY|INSTALLATION_ID|APP_ID)$/.test(name)) {
      throw new Error('Unexpected secret name in worker mapping');
    }
    const result = runner('gh', ['secret', 'set', name, '--repo', REPO], {
      input, encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'], timeout: 30000,
    });
    if (result.error || result.status !== 0) throw new Error(`Secret upload failed for ${name}; run finish again`);
  }
  Object.assign(entry, { app_id: credentials.id, app_login: `${credentials.slug}[bot]`, installation_id: installation.id });
  savePublic(mappingFile, mapping);
  return { app_id: credentials.id, app_login: entry.app_login, installation_id: installation.id };
}

function page(content) {
  return `<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Worker 48 App provisioning</title><main><h1>Worker 48 App provisioning</h1>${content}</main></html>`;
}

function serverFor({ stateFile, credentialsFile, entry, base, request = api }) {
  let busy = false;
  return http.createServer(async (req, res) => {
    res.setHeader('Cache-Control', 'no-store');
    res.setHeader('Referrer-Policy', 'no-referrer');
    res.setHeader('X-Content-Type-Options', 'nosniff');
    res.setHeader('Content-Security-Policy', "default-src 'none'; form-action https://github.com; base-uri 'none'; frame-ancestors 'none'");
    if (req.headers.host !== new URL(base).host) { res.writeHead(403).end(); return; }
    const url = new URL(req.url, base);
    const state = load(stateFile);
    const credentials = load(credentialsFile);
    const respond = (status, content) => { res.writeHead(status, { 'Content-Type': 'text/html; charset=utf-8' }); res.end(page(content)); };
    if (req.method !== 'GET') { respond(405, '<p>Method not allowed.</p>'); return; }
    if (url.pathname === '/') {
      if (credentials) {
        respond(200, `<p>App registered. Continue installation.</p><a href="https://github.com/apps/${escapeHtml(credentials.slug)}/installations/new">Install worker 48 App</a>`);
      } else {
        const action = `https://github.com/settings/apps/new?state=${state.nonce}`;
        respond(200, `<p>${escapeHtml(entry.display_name)} — autonomous ComicPile Factory worker.</p><form method="post" action="${action}"><input type="hidden" name="manifest" value="${escapeHtml(JSON.stringify(manifest(entry, base)))}"><button type="submit">Register worker 48 App</button></form>`);
      }
      return;
    }
    if (url.pathname === '/callback') {
      if (url.searchParams.get('state') !== state.nonce) { respond(403, '<p>Invalid registration state.</p>'); return; }
      if (credentials) { res.writeHead(303, { Location: '/' }).end(); return; }
      const code = url.searchParams.get('code');
      if (!code || !/^[a-zA-Z0-9]+$/.test(code)) { respond(400, '<p>Missing registration code.</p>'); return; }
      if (busy) { respond(409, '<p>Registration is already being processed.</p>'); return; }
      busy = true;
      try {
        const converted = await request(`/app-manifests/${code}/conversions`, { body: {} });
        validateCredentials(converted, entry.display_name);
        savePrivate(credentialsFile, { id: converted.id, name: converted.name, slug: converted.slug, owner: converted.owner, pem: converted.pem });
        res.writeHead(303, { Location: '/' }).end();
      } catch {
        respond(502, '<p>Registration could not be saved. Check the terminal; do not create another App.</p>');
        console.error('Manifest conversion failed; no credentials were printed. Inspect existing Apps before retrying.');
      } finally { busy = false; }
      return;
    }
    if (url.pathname === '/installed') {
      if (!credentials) { respond(409, '<p>Register the App first.</p>'); return; }
      // Installation callbacks trigger no mutation. Finish explicitly from the owner CLI.
      respond(200, '<p>Installation returned. Run the finish command in the terminal to verify access and upload secrets.</p>');
      return;
    }
    respond(404, '<p>Page not found.</p>');
  });
}

async function main(argv) {
  const command = argv[0];
  const dirIndex = argv.indexOf('--state-dir');
  const directory = dirIndex < 0 ? DEFAULT_DIR : argv[dirIndex + 1];
  if (!directory || !['start', 'finish', 'status'].includes(command)) {
    console.log('Usage: node .github/scripts/provision-worker-app.cjs start|finish|status [--state-dir DIR] [--port PORT]');
    return 2;
  }
  const mapping = load(MAPPING);
  const entry = mapping.workers['48'];
  const stateFile = path.join(directory, 'registration.json');
  const credentialsFile = path.join(directory, 'credentials.json');
  const credentials = load(credentialsFile);
  if (command === 'status') {
    console.log(JSON.stringify({ worker: 48, registered: Boolean(credentials), app_id: credentials?.id || null, app_login: credentials ? `${credentials.slug}[bot]` : null, configured: Boolean(entry.app_id && entry.installation_id) }, null, 2));
    return 0;
  }
  if (command === 'finish') {
    if (!credentials) throw new Error('No saved App credentials; run start first');
    validateCredentials(credentials, entry.display_name);
    console.log(JSON.stringify(await finish(credentials), null, 2));
    return 0;
  }
  const auth = spawnSync('gh', ['api', 'user', '--jq', '.login'], { encoding: 'utf8', timeout: 30000 });
  if (auth.status !== 0 || auth.stdout.trim() !== 'JoshCLWren') throw new Error('gh must be authenticated as JoshCLWren');
  if (entry.app_id && !credentials) throw new Error('Worker 48 is already mapped; refusing duplicate registration');
  const portIndex = argv.indexOf('--port');
  const port = portIndex < 0 ? 8765 : Number(argv[portIndex + 1]);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Port must be 1024–65535');
  const base = `http://127.0.0.1:${port}`;
  if (!load(stateFile)) savePrivate(stateFile, { nonce: crypto.randomBytes(32).toString('hex') });
  const server = serverFor({ stateFile, credentialsFile, entry, base });
  server.on('error', () => { console.error('Could not start local provisioning server; check the port.'); process.exitCode = 1; });
  server.listen(port, '127.0.0.1', () => console.log(`Worker 48 provisioning: ${base}\nCredentials stay in ${directory}; no keys are printed.\nAfter installation: node .github/scripts/provision-worker-app.cjs finish`));
}

module.exports = { savePrivate, manifest, validateCredentials, finish, serverFor, main };
if (require.main === module) main(process.argv.slice(2)).then(code => { if (code) process.exitCode = code; }).catch(error => { console.error(error.message); process.exitCode = 1; });
