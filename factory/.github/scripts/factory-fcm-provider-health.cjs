#!/usr/bin/env node
// Manual credential diagnostics using free-coding-models' maintained provider
// catalog and provider-aware inference probes. No local or GitHub secrets are
// written, and neither worker leases nor GitHub issues/PRs are touched.
// The upstream catalog is a curated candidate set, not authoritative runtime
// discovery. No provider/model roster is copied into this repository.
const { appendFileSync, mkdirSync, writeFileSync } = require('node:fs');
const { join, dirname } = require('node:path');
const { pathToFileURL } = require('node:url');

async function main() {
const root = process.env.FCM_PACKAGE_DIR;
if (!root) throw new Error('FCM_PACKAGE_DIR must reference the installed free-coding-models package');

const moduleFromPackage = async (file) => import(pathToFileURL(join(root, file)).href);
const { sources, MODELS } = await moduleFromPackage('sources.js');
const { ping } = await moduleFromPackage('src/core/ping.js');
const { getApiKey } = await moduleFromPackage('src/core/config.js');

const scope = process.env.FCM_PROBE_SCOPE === 'all' ? 'all' : 'sample';
const maxPerProvider = scope === 'all' ? Infinity : 5;
const concurrency = 4;
const output = process.env.FCM_REPORT_PATH || join(process.cwd(), 'fcm-provider-report.json');
const queue = Object.entries(sources);
const results = [];

function classify(code) {
  if (code === '200') return 'HTTP_OK';
  if (code === '401') return 'AUTH_FAILED';
  if (code === '403') return 'ACCESS_DENIED';
  if (code === '402') return 'PAYMENT_REQUIRED';
  if (code === '429') return 'RATE_LIMITED';
  if (code === '404' || code === '410') return 'MODEL_UNAVAILABLE';
  if (code === '000') return 'TIMEOUT';
  if (code === 'ERR') return 'NETWORK_ERROR';
  return 'HTTP_' + code;
}

function summarizedStatus(probes) {
  if (probes.some((p) => p.status === 'HTTP_OK')) return 'PASS_HTTP';
  if (!probes.length) return 'NO_MODELS';
  const terminal = probes.find((p) => ['AUTH_FAILED', 'ACCESS_DENIED', 'PAYMENT_REQUIRED', 'RATE_LIMITED'].includes(p.status));
  return terminal?.status || 'NO_WORKING_MODEL_IN_SAMPLE';
}

async function examine(provider, source) {
  const key = getApiKey({}, provider);
  // Kilo and LLM7 intentionally permit anonymous probes.
  if (!key && !source.noKeyNeeded) {
    return { provider, name: source.name || provider, status: 'NO_KEY', checked: 0, probes: [] };
  }
  // MODELS is FCM's active catalog projection and excludes upstream-retired
  // model tuples, unlike the raw per-provider source arrays.
  const active = MODELS.filter((entry) => entry[5] === provider);
  const candidates = active.map((entry) => entry[0])
    .filter((id) => typeof id === 'string' && id.length > 0)
    .slice(0, maxPerProvider);
  const probes = [];
  for (const id of candidates) {
    const response = await ping(key, id, provider, source.url);
    const code = String(response?.code || 'ERR');
    const status = classify(code);
    probes.push({ model: id, status, http_code: code, latency_ms: Number.isFinite(response?.ms) ? response.ms : null });
    // "sample" means prove at least one catalog entry is reachable. Do not
    // exhaust a provider's quota checking additional models after success.
    if (scope === 'sample' && status === 'HTTP_OK') break;
    // Stop when auth, billing, or rate limits are provider-wide.
    if (['AUTH_FAILED', 'ACCESS_DENIED', 'PAYMENT_REQUIRED', 'RATE_LIMITED'].includes(status)) break;
  }
  return {
    provider, name: source.name || provider,
    status: summarizedStatus(probes),
    checked: probes.length,
    listed: active.length,
    probes,
  };
}

async function worker() {
  for (;;) {
    const next = queue.shift();
    if (!next) return;
    const [provider, source] = next;
    try {
      results.push(await examine(provider, source));
    } catch {
      // No request/response bodies, tokens or exception messages in reports.
      results.push({ provider, name: source.name || provider, status: 'PROBE_EXCEPTION', checked: 0, probes: [] });
    }
  }
}

await Promise.all(Array.from({ length: concurrency }, () => worker()));
results.sort((a, b) => a.provider.localeCompare(b.provider));
mkdirSync(dirname(output), { recursive: true });
writeFileSync(output, JSON.stringify({
  tool: 'free-coding-models',
  scope,
  caveat: 'HTTP_OK means an inference POST returned 2xx. The FCM ping API does not validate generated content.',
  providers: results,
}, null, 2) + '\n', { mode: 0o600 });

function escaped(value) {
  return String(value || '').replaceAll('|', '\\|').replaceAll('\n', ' ');
}
const summary = [
  '## FCM provider credential diagnostic',
  '',
  'Mode: **' + scope + '**. Uses FCM catalog candidates, not a model roster in ComicPile.',
  'A 2xx inference response is **not** proof that the model can edit code or use tools.',
  '',
  '| Provider | Result | Models checked | First successful model |',
  '|---|---|---:|---|',
  ...results.map((r) => '| ' + escaped(r.name) + ' | ' + escaped(r.status) + ' | ' + r.checked + ' | ' +
    escaped(r.probes.find((p) => p.status === 'HTTP_OK')?.model || '') + ' |'),
  '',
  'The JSON artifact includes per-model HTTP statuses and latency without credential values.',
  'Providers not included in the upstream FCM catalog are not tested by this workflow.',
].join('\n') + '\n';
console.log(summary);
if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY, summary);

const configured = results.filter((r) => r.status !== 'NO_KEY');
const failures = configured.filter((r) => r.status !== 'PASS_HTTP');
console.log('Configured providers: ' + configured.length + '; no 2xx inference response: ' + failures.length);
if (!configured.length || failures.length) process.exitCode = 1;

}
main().catch((error) => { console.error('FCM diagnostic error:', String(error?.message || 'unknown')); process.exitCode = 1; });
