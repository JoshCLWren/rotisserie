"use strict";
// Runs the built hosted adapter against a fake sign-in service and snapshot.
// Usage: node hosted_harness.cjs ADAPTER.js  -> prints one JSON object of results.
const fs = require("fs");
const vm = require("vm");

const AUTH = "https://auth.widgets.example/api/factory/github";
const adapterSource = fs.readFileSync(process.argv[2], "utf8");

function element() {
  return {
    textContent: "", listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; }
  };
}

function environment(service) {
  const elements = {};
  const polls = () => [...service.runs.values()].slice(-1).reduce((sum, run) => sum + run.polls, 0);
  const storage = new Map();
  if (service.session) storage.set("rotisserie-github-admin-session", service.session);
  const body = {classes: new Set()};
  body.classList = {add: c => body.classes.add(c), remove: c => body.classes.delete(c)};
  const document = {
    body,
    getElementById: id => (elements[id] = elements[id] || element()),
    createElement: tag => ({tag, href: "", textContent: ""}),
    addEventListener() {}
  };
  const json = (status, data) => ({ok: status < 400, status, json: async () => data});
  async function fetch(url, options = {}) {
    const href = String(url);
    if (href.startsWith("https://raw.githubusercontent.com/")) {
      if (href.includes("factory-status-snapshot.json")) {
        return json(200, {generated_at: new Date().toISOString(), workers: []});
      }
      return json(200, {
        models: [{source: "opencode-free", model: "old-model", windows: {}}],
        db_usage: {status: "warn", label: "not in use yet", reasons: ["rows_read projected at 80%"]}
      });
    }
    if (!href.startsWith(AUTH)) throw Error("unexpected fetch " + href);
    const path = href.slice(AUTH.length);
    service.calls.push(path);
    if (service.expired) return json(401, {detail: "Session expired"});
    if (path === "/session") return json(200, {login: "admin"});
    if (path === "/dispatch") {
      const request = JSON.parse(options.body);
      service.dispatched.push(request);
      if (service.allowed && !service.allowed.includes(request.operation)) {
        return json(400, {detail: "Unsupported factory operation"});
      }
      return json(202, service.accept(request));
    }
    const match = path.match(/^\/runs\/(\d+)$/);
    if (match) return json(200, service.run(Number(match[1])));
    return json(404, {detail: "not found"});
  }
  const context = {
    REPO: "acme/widgets", document, fetch, URL, URLSearchParams, console,
    crypto: require("crypto").webcrypto,
    setTimeout: fn => { setImmediate(fn); return 1; }, clearTimeout() {},
    location: {
      origin: "https://acme.github.io", hash: "", pathname: "/widgets/", search: "",
      assign(url) { service.assigned = url; }
    },
    history: {replaceState() {}},
    sessionStorage: {
      getItem: k => storage.get(k) || null,
      setItem: (k, v) => storage.set(k, v),
      removeItem: k => storage.delete(k)
    }
  };
  context.window = context;
  const status = document.getElementById("operation-status");
  status.links = [];
  status.append = (...parts) => {
    for (const part of parts) {
      if (typeof part === "object") status.links.push({href: part.href, polls: polls()});
    }
  };
  vm.createContext(context);
  vm.runInContext(adapterSource, context);
  return {context, elements, body};
}

function fakeService(options = {}) {
  const service = {
    session: options.signedIn === false ? null : "signed-session",
    calls: [], dispatched: [], runs: new Map(), next: 100, expired: false,
    allowed: options.oldService ? ["run", "unstick", "cancel", "sync", "doctor_fix",
      "release_expired", "settings", "prioritize"] : null,
    accept(request) {
      if (options.oldService) return {result: "submitted", scheduler_url: "https://github.com/x"};
      const id = service.next++;
      service.runs.set(id, {request, polls: 0});
      return {result: "accepted", request_id: request.request_id, run_id: id,
        run_url: "https://github.com/acme/widgets/actions/runs/" + id};
    },
    run(id) {
      const entry = service.runs.get(id);
      entry.polls += 1;
      const base = {run_id: id, run_url: "https://github.com/acme/widgets/actions/runs/" + id};
      if (entry.polls < 2) return {...base, status: "in_progress"};
      const result = (options.result || defaultResult)(entry.request);
      return {...base, status: "completed", conclusion: result ? result.conclusion || "success" : "failure",
        result: result ? result.envelope : null};
    }
  };
  return service;
}

function defaultResult(request) {
  if (request.operation === "read") {
    return {envelope: {request_id: request.request_id, kind: "read", exit_code: 0,
      finished_at: "2026-11-24T15:00:00+00:00",
      reads: request.reads.map(r => ({name: r.name, query: r.query, result: answer(r)}))}};
  }
  if (request.operation === "estimate_neon") {
    return {envelope: {request_id: request.request_id, kind: "write", exit_code: 0,
      result: {result: "started", bridge: {run_id: 77, url: "https://github.com/acme/widgets/actions/runs/77"}}}};
  }
  if (request.model === "bad-model") {
    return {conclusion: "failure", envelope: {request_id: request.request_id, kind: "write",
      exit_code: 2, result: {result: "rejected", error: "Invalid provider/model identifier"}}};
  }
  return {envelope: {request_id: request.request_id, kind: "write", exit_code: 0,
    result: {result: "dispatched", scheduler_url: "https://github.com/acme/widgets/actions/runs/9"}}};
}

let estimatePolls = 0;
function answer(read) {
  if (read.name === "estimate" && read.query.run) {
    estimatePolls += 1;
    return estimatePolls < 2 ? {exit_code: 0, result: "pending"} : {exit_code: 0, result: "completed", target: "issue:5"};
  }
  if (read.name === "models") {
    return {exit_code: 0, models: [{model: "good-model", offered: true},
      {model: "off-model", offered: true, disabled: true}, {model: "gone", offered: false}]};
  }
  if (read.name === "leases") return {exit_code: 0, source: "neon", rows: [{target: "pr:4"}]};
  return {exit_code: 0, ready: [], issues: [{issue: 7}]};
}

const settle = p => p.then(value => ({ok: true, value}), error => ({ok: false, error: error.message}));
const tick = () => new Promise(resolve => setImmediate(resolve));

async function main() {
  const out = {};

  let service = fakeService({signedIn: false});
  let env = environment(service);
  const request = env.context.window.hostedRequest;
  out.signedOutWhy = await settle(request("/api/why"));
  out.signedOutStatus = await settle(request("/api/status"));
  out.signedOutModels = await settle(request("/api/models?provider=opencode-go"));
  out.signedOutWrite = await settle(request("/api/command", {operation: "unstick"}));
  out.signedOutDbUsage = await settle(request("/api/db-usage"));
  out.signedOutDoctor = await settle(request("/api/doctor"));
  out.signedOutIdentity = env.elements["github-identity"].textContent;
  out.signedOutViewer = env.context.window.hostedViewer();
  out.signedOutCalls = service.calls;

  service = fakeService();
  env = environment(service);
  await tick();
  out.signedInIdentity = env.elements["github-identity"].textContent;
  const hosted = env.context.window.hostedRequest;
  const [why, leases] = await Promise.all([settle(hosted("/api/why")), settle(hosted("/api/leases"))]);
  out.why = why; out.leases = leases;
  out.readDispatches = service.dispatched.map(d => ({operation: d.operation, reads: d.reads}));
  await settle(hosted("/api/why"));
  out.dispatchesAfterCachedRead = service.dispatched.length;
  out.liveLine = env.elements["hosted-live"].textContent;

  out.badModel = await settle(hosted("/api/command", {operation: "run", provider: "p", model: "nope"}));
  out.offModel = await settle(hosted("/api/command", {operation: "run", provider: "p", model: "off-model"}));
  out.dispatchesAfterRejectedModels = service.dispatched.map(d => d.operation);
  env.elements["operation-status"].links.length = 0;
  out.goodRun = await settle(hosted("/api/command", {operation: "run", provider: "p", model: "good-model"}));
  out.runLinks = env.elements["operation-status"].links.slice();
  out.estimateNeon = await settle(hosted("/api/command", {operation: "estimate_neon", target: "issue:5"}));
  const before = service.dispatched.length;
  out.estimatePending = await settle(hosted("/api/estimate?target=issue%3A5&run=77"));
  out.estimateDone = await settle(hosted("/api/estimate?target=issue%3A5&run=77"));
  out.estimateReadRuns = service.dispatched.length - before;
  out.unknownOperation = await settle(hosted("/api/command", {operation: "shell"}));

  service = fakeService();
  env = environment(service);
  await tick();
  service.accept = request => {
    const id = service.next++;
    service.runs.set(id, {request: {...request, model: "bad-model"}, polls: 0});
    return {run_id: id, run_url: "https://github.com/acme/widgets/actions/runs/" + id};
  };
  out.invalidModelRun = await settle(env.context.window.hostedRequest("/api/command",
    {operation: "run", provider: "p", model: "auto"}));

  service = fakeService({result: request => ({envelope: {request_id: "someone-else", exit_code: 0, reads: []}})});
  env = environment(service);
  await tick();
  out.mismatch = await settle(env.context.window.hostedRequest("/api/settings"));

  service = fakeService({result: () => null});
  env = environment(service);
  await tick();
  out.noArtifact = await settle(env.context.window.hostedRequest("/api/command", {operation: "unstick"}));

  service = fakeService({oldService: true});
  env = environment(service);
  await tick();
  out.oldServiceWrite = await settle(env.context.window.hostedRequest("/api/command", {operation: "unstick"}));
  out.oldServiceRead = await settle(env.context.window.hostedRequest("/api/leases"));
  out.oldServiceProviders = await settle(env.context.window.hostedRequest("/api/providers"));
  out.oldServiceModels = await settle(env.context.window.hostedRequest("/api/models?provider=opencode-go"));
  out.oldServiceAutoRun = await settle(env.context.window.hostedRequest("/api/command",
    {operation: "run", provider: "opencode-go", model: "auto"}));
  out.oldServiceLiveLine = env.elements["hosted-live"].textContent;

  service = fakeService();
  env = environment(service);
  out.viewerWhileChecking = env.context.window.hostedViewer();
  await tick();
  out.viewerSignedIn = env.context.window.hostedViewer();
  out.freshRead = await settle(env.context.window.hostedRequest("/api/settings?fresh=1"));
  out.freshQuery = service.dispatched[service.dispatched.length - 1].reads[0].query;
  service.expired = true;
  out.expired = await settle(env.context.window.hostedRequest("/api/why"));
  out.expiredIdentity = env.elements["github-identity"].textContent;
  out.expiredSignedInClass = env.body.classes.has("hosted-signed-in");
  out.afterExpiry = await settle(env.context.window.hostedRequest("/api/why"));

  process.stdout.write(JSON.stringify(out));
}

process.on("unhandledRejection", error => {
  process.stdout.write(JSON.stringify({unhandledRejection: String(error)}));
  process.exit(3);
});
main();
