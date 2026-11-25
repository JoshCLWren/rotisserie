/* Public reads: sanitized snapshots from the factory-status-live branch.
 * These work without sign-in. They are observations, never live evaluations.
 */
const SNAPSHOT_MS = 60000;
let snapshotCache = null;
let statsCache = null;

function rawUrl(name) {
  return "https://raw.githubusercontent.com/" + REPO + "/factory-status-live/" + name;
}
async function publicJson(name) {
  const response = await fetch(rawUrl(name) + "?v=" + Date.now(), { cache: "no-store" });
  if (!response.ok) throw Error("Published factory data unavailable (" + response.status + ").");
  return response.json();
}
function clearPublic() {
  snapshotCache = null;
  statsCache = null;
}
function cached(slot, name) {
  const current = slot === "snapshot" ? snapshotCache : statsCache;
  if (current && Date.now() - current.time < SNAPSHOT_MS) return current.value;
  const value = publicJson(name).catch(error => {
    if (slot === "snapshot") snapshotCache = null;
    else statsCache = null;
    throw error;
  });
  if (slot === "snapshot") snapshotCache = { time: Date.now(), value };
  else statsCache = { time: Date.now(), value };
  return value;
}
async function snapshot() {
  const result = await cached("snapshot", "factory-status-snapshot.json");
  if (!result || !result.generated_at || !Array.isArray(result.workers)) {
    throw Error("The published snapshot has an invalid schema.");
  }
  return result;
}
async function statistics() {
  const result = await cached("stats", "factory-stats.json");
  if (!Array.isArray(result.models)) throw Error("Published reliability data is unavailable.");
  return result;
}
async function dbUsage() {
  const stats = await cached("stats", "factory-stats.json").catch(() => null);
  const usage = stats && stats.db_usage;
  if (!usage || typeof usage !== "object") {
    return {exit_code: 0, status: "unpublished", source: "published",
      reasons: ["No Turso usage has been published yet."]};
  }
  return {...usage, exit_code: 0, source: "published"};
}
async function crashGroups(hours) {
  const stats = await cached("stats", "factory-stats.json").catch(() => null);
  const block = stats && stats.crashes;
  if (!block || !Array.isArray(block.groups)) {
    return {exit_code: 0, source: "published", published: false, groups: []};
  }
  const since = Date.now() - hours * 3600000;
  const groups = block.groups.map(g => {
    const times = (g.times || []).filter(at => Date.parse(at) >= since).sort();
    return times.length ? {...g, count: times.length, first_at: times[0]} : null;
  }).filter(Boolean);
  return {exit_code: 0, source: "published", published: true,
    generated_at: block.generated_at, groups};
}
async function readyList() {
  const stats = await cached("stats", "factory-stats.json").catch(() => null);
  const block = stats && stats.ready;
  if (!block || !Array.isArray(block.items)) {
    return {exit_code: 0, source: "published", published: false, ready: [], ready_count: 0,
      cycles: [], notes: ["The dispatcher has not published a ready list yet."]};
  }
  return {exit_code: 0, source: "published", published: true, generated_at: block.generated_at,
    ready: block.items, ready_count: block.items.length, cycles: block.cycles || [],
    notes: ["The dispatcher's ready list from its last pass; the next pass may differ."]};
}
function crashFinding(found) {
  const fatal = found.groups.filter(g => g.fatal);
  return {
    check: "Factory crashes (published)",
    status: fatal.length ? "fail" : "ok",
    text: fatal.length ? fatal.slice(0, 3).map(g => g.exception_type + " in " + g.script + ":" +
      g.line + " " + g.function + " (" + g.count + "x)").join("; ") : "No crash in the last hour."
  };
}
function usageFinding(usage) {
  const status = {ok: "ok", critical: "fail", not_configured: "ok", unpublished: "info"}[usage.status];
  return {
    check: "Database usage (published)",
    status: status || "warn",
    text: (usage.label ? usage.label + ": " : "") + usage.status + ". " +
      (usage.reasons || []).join("; ")
  };
}
function asWork(s) {
  const rows = [];
  for (const worker of s.workers || []) {
    if (worker.activity !== "working") continue;
    const tasks = worker.work && worker.work.length ? worker.work : [{
      title: worker.persisted_work || "Factory worker",
      kind: "issue", number: 0
    }];
    for (const task of tasks) {
      rows.push({
        ref: task.number > 0 ? task.kind + ":" + task.number : "",
        title: task.title,
        worker: worker.worker,
        provider: worker.runtime_source,
        model: worker.runtime_model,
        run_url: /^\d+$/.test(String(worker.attempt_run || ""))
          ? "https://github.com/" + REPO + "/actions/runs/" + worker.attempt_run : null,
        running_since: worker.updated || null,
        coding_since: null
      });
    }
  }
  return rows;
}
function asFindings(s) {
  const minutes = Math.max(0, Math.round((Date.now() - Date.parse(s.generated_at)) / 60000));
  return [
    {
      check: "Published snapshot (not a live doctor run)",
      status: minutes > 30 ? "warn" : "ok",
      text: "Generated " + minutes + " minutes ago. GitHub schedules can be delayed."
    },
    {
      check: "Observed capacity",
      status: "info",
      text: (s.busy_workers || 0) + " workers active, " +
        (s.executable_slot_capacity || 0) + " executable slots reported."
    },
    {
      check: "Provider availability observation",
      status: (s.unavailable_candidates || 0) > 0 ? "warn" : "info",
      text: (s.cooling_candidates || 0) + " cooling, " +
        (s.unavailable_candidates || 0) + " unavailable candidates. Run local doctor for diagnosis."
    }
  ];
}
function reliability(stats, window) {
  if (window !== "7d" && window !== "24h") throw Error("Invalid reliability window.");
  return {models: stats.models.map(x => {
    const w = (x.windows || {})[window] || {};
    const fail = Object.entries(w.failures || {}).sort((a, b) => b[1] - a[1])[0];
    return {
      model: x.model, health: x.health?.status || "unverified",
      runs: w.runs || 0, delivered: w.delivered || 0,
      failed: w.failed || 0, top_failure: fail ? fail[0] : ""
    };
  }).filter(x => x.runs > 0).sort((a, b) => b.delivered - a.delivered || b.runs - a.runs)};
}
/* Signed out, the picker shows history only. Historical model IDs are never
 * offered as choices because only the live CLI knows what is valid now (#207). */
function historicalProviders(stats) {
  const unique = [...new Set(stats.models.map(x => x.source).filter(Boolean))];
  return {exit_code: 0, historical: true, providers: unique.map(p => ({
    provider: p === "opencode-free" ? "opencode-go" : p,
    name: p, setup: "published history, sign in for the live list"
  }))};
}
function historicalModels(stats, requested) {
  const source = requested === "opencode-go" ? "opencode-free" : requested;
  return {
    exit_code: 0,
    historical: true,
    models: stats.models.filter(x => x.source === source && x.model !== "unclassified")
      .map(x => ({
        model: x.model,
        offered: false,
        historical: true,
        track_record: "Published history only. " +
          ((x.windows || {})["7d"]?.delivered || 0) + " deliveries / " +
          ((x.windows || {})["7d"]?.runs || 0) + " runs in 7 days"
      }))
  };
}
async function publicRead(name, params) {
  if (name === "stats") return reliability(await statistics(), params.get("window") || "7d");
  if (name === "providers") return historicalProviders(await statistics());
  if (name === "models") return historicalModels(await statistics(), params.get("provider"));
  if (name === "db-usage") return dbUsage();
  if (name === "crashes") {
    const hours = Number((params.get("since") || "6h").replace(/h$/, "")) || 6;
    return crashGroups(Math.min(hours, 24));
  }
  if (name === "ready") return readyList();
  const s = await snapshot();
  if (name === "status") return {exit_code: 0, working: asWork(s), generated_at: s.generated_at};
  if (name === "report") {
    const cutoff = Date.now() - 12 * 3600000;
    const merged = (s.merged_prs || []).filter(p => Date.parse(p.merged_at) >= cutoff);
    return {exit_code: 0, merged, published_at: s.generated_at};
  }
  if (name === "doctor") {
    return {exit_code: 0, findings: [...asFindings(s), usageFinding(await dbUsage()),
      crashFinding(await crashGroups(1))]};
  }
  if (name === "outcomes") {
    return {exit_code: 0, requests: (s.workers || [])
      .filter(w => w.attempt_outcome)
      .map(w => ({
        target: w.persisted_work || "Worker " + w.worker,
        action: "Last recorded attempt",
        model: w.runtime_model || "Unknown",
        decision: w.attempt_outcome,
        worker_result: w.delivery_outcome || w.attempt_outcome
      }))};
  }
  throw Error("No published snapshot serves " + name + ".");
}
