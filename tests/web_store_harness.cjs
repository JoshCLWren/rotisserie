"use strict";
// Runs the panel-read store from web_ui.js against a fake localStorage and server.
// Usage: node web_store_harness.cjs WEB_UI.js  -> prints one JSON object of results.
const fs = require("fs");
const vm = require("vm");

const source = fs.readFileSync(process.argv[2], "utf8");
const start = source.indexOf("/* Panel reads persist");
const getAt = source.indexOf("async function get(", start);
const block = source.slice(start, source.indexOf("\n}\n", getAt) + 3);

function storage(broken) {
  const map = new Map();
  const fail = () => { if (broken) throw Error("storage blocked"); };
  const api = {
    getItem: k => { fail(); return map.has(k) ? map.get(k) : null; },
    setItem: (k, v) => { fail(); map.set(k, String(v)); },
    removeItem: k => { fail(); map.delete(k); }
  };
  return new Proxy(api, {ownKeys: () => { fail(); return [...map.keys()]; },
    getOwnPropertyDescriptor: (_, k) => map.has(k) ? {enumerable: true, configurable: true, value: map.get(k)} : undefined});
}

function page({local = true, broken = false, viewer = null} = {}) {
  let clock = Date.parse("2026-11-24T20:00:00Z");
  const requests = [], rendered = [];
  let answer = name => ({exit_code: 0, name, read_at: new Date(clock - 5000).toISOString()});
  class FakeDate extends Date { static now() { return clock; } }
  const context = {
    LOCAL: local, REPO: "acme/widgets", URLSearchParams, JSON, Number, Math, Object, Set, Error,
    Date: FakeDate, localStorage: storage(broken),
    window: viewer ? {hostedViewer: () => viewer} : {},
    h: x => String(x),
    request: async path => { requests.push(path); return answer(path.split("?")[0].slice(5)); }
  };
  for (const name of ["Status", "Leases", "Incidents", "Queue", "Heal", "Doctor", "Report", "Recap",
    "Outcomes", "Stats", "Settings", "Providers"]) context["load" + name] = () => rendered.push(name);
  vm.createContext(context);
  vm.runInContext(block + "\nthis.api={get,drop,keyOf,ours,set fresh(v){forceFresh=v}};", context);
  return {context, api: context.api, requests, rendered,
    tick: ms => { clock += ms; }, answer: fn => { answer = fn; }};
}

const settle = () => new Promise(resolve => setImmediate(resolve));

(async () => {
  const out = {};
  let p = page();
  const first = await p.api.get("status");
  const second = await p.api.get("status");
  out.withinTtl = {requests: p.requests.slice(), asOfIsServerReadTime: second._at === Date.parse("2026-11-24T19:59:55Z"), firstAt: first._at};
  p.tick(91000);
  const stale = await p.api.get("status");
  await settle(); await settle();
  out.stale = {servedStoredAt: stale._at, requests: p.requests.slice(), rerendered: p.rendered.slice()};
  p.api.fresh = true;
  await p.api.get("status");
  p.api.fresh = false;
  out.fresh = p.requests[p.requests.length - 1];
  const before = p.requests.length;
  await p.api.get("why", {target: "issue:4"});
  await p.api.get("why", {target: "issue:4"});
  out.targetedReadsNotStored = p.requests.length - before;
  await p.api.get("doctor");
  p.api.drop(["status"]);
  out.afterDrop = p.api.ours().map(k => k.slice(k.lastIndexOf(":") + 1));
  p.answer(() => ({exit_code: 1, error: "boom"}));
  await p.api.get("report");
  p.answer(() => ({job: "abc", exit_code: 0}));
  await p.api.get("recap");
  p.answer(() => ({exit_code: 4, result: "failed", findings: []}));
  await p.api.get("doctor", {failing: "1"});
  out.failuresAndJobsNotStored = p.api.ours().map(k => k.slice(k.lastIndexOf(":") + 1));

  p = page();
  p.answer(() => ({exit_code: 0, blob: "x".repeat(300000)}));
  for (let i = 0; i < 12; i++) { await p.api.get("providers", {n: String(i)}); p.tick(1000); }
  const sizes = p.api.ours().map(k => p.context.localStorage.getItem(k).length);
  out.bounded = {entries: sizes.length, total: sizes.reduce((a, b) => a + b, 0),
    newestKept: p.api.ours().some(k => k.endsWith("n=11"))};

  p = page({broken: true});
  const blocked = await p.api.get("status");
  out.blockedStorage = {exit_code: blocked.exit_code, requests: p.requests.length};

  p = page({local: false, viewer: "alice"});
  await p.api.get("status");
  out.hostedKey = p.api.ours()[0];
  p.context.window.hostedViewer = () => "public";
  await p.api.get("status");
  out.hostedPublicIsSeparate = p.requests.length;
  p.context.window.rotisserieForget();
  out.afterSignOut = p.api.ours();
  process.stdout.write(JSON.stringify(out));
})();
