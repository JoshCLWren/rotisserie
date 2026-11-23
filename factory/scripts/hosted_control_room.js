/* Hosted adapter for the canonical Rotisserie web_ui.html.
 * The public dashboard contains no GitHub credentials. Operator tokens live
 * in memory only; every write is independently authorized by GitHub Actions.
 */
"use strict";
(function () {
  const WORKFLOW = "rotisserie-operator.yml";
  const CACHE_MS = 60000;
  let token = null;
  let identity = null;
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
  function cached(slot, name) {
    const current = slot === "snapshot" ? snapshotCache : statsCache;
    if (current && Date.now() - current.time < CACHE_MS) return current.value;
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
  async function publicRead(path) {
    const parsed = new URL(path, location.origin);
    const name = parsed.pathname.replace(/^\/api\//, "");
    if (name === "stats" || name === "models" || name === "providers") {
      const stats = await statistics();
      if (name === "stats") {
        const window = parsed.searchParams.get("window") || "7d";
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
      if (name === "providers") {
        const unique = [...new Set(stats.models.map(x => x.source).filter(Boolean))];
        return {exit_code: 0, providers: unique.map(p => ({
          provider: p === "opencode-free" ? "opencode-go" : p,
          name: p, setup: "published observation"
        }))};
      }
      const requested = parsed.searchParams.get("provider");
      const source = requested === "opencode-go" ? "opencode-free" : requested;
      return {
        exit_code: 0,
        models: stats.models.filter(x => x.source === source && x.model !== "unclassified")
          .map(x => ({
            model: x.model,
            offered: ["retired", "unavailable"].includes(x.health?.status) ? false : null,
            track_record: "Published history only. " +
              ((x.windows || {})["7d"]?.delivered || 0) + " deliveries / " +
              ((x.windows || {})["7d"]?.runs || 0) + " runs in 7 days"
          }))
      };
    }
    const s = await snapshot();
    if (name === "status") return {exit_code: 0, working: asWork(s), generated_at: s.generated_at};
    if (name === "report") {
      const cutoff = Date.now() - 12 * 3600000;
      const merged = (s.merged_prs || []).filter(p => Date.parse(p.merged_at) >= cutoff);
      return {exit_code: 0, merged, published_at: s.generated_at};
    }
    if (name === "doctor") return {exit_code: 0, findings: asFindings(s)};
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
    throw Error(
      "This requires a live Rotisserie evaluation, not a published snapshot. " +
      "Use the local control room or the authenticated GitHub operator workflow."
    );
  }

  async function githubRequest(path, headers, options) {
    const response = await fetch("https://api.github.com" + path, Object.assign({
      headers: Object.assign({ "Accept": "application/vnd.github+json" }, headers)
    }, options || {}));
    if (!response.ok) {
      let message = "GitHub rejected the request (" + response.status + ").";
      try {
        const result = await response.json();
        if (typeof result.message === "string") message += " " + result.message;
      } catch (_) { /* Do not include raw server output or credentials. */ }
      if (response.status === 401) disconnect();
      throw Error(message);
    }
    return response.status === 204 ? null : response.json();
  }

  function disconnect() {
    token = null; identity = null;
    const button = document.getElementById("github-connect");
    const status = document.getElementById("github-identity");
    if (button) button.textContent = "Connect GitHub";
    if (status) status.textContent = "Read-only visitor. Operator token is not stored.";
  }
  async function connect() {
    if (token) { disconnect(); return; }
    const value = window.prompt(
      "Paste a short-lived, fine-grained GitHub token scoped to " + REPO +
      " with Actions: write and Metadata: read. It stays in memory only and " +
      "disappears when you close or reload the page."
    );
    if (!value) return;
    try {
      const headers = { Authorization: "Bearer " + value.trim() };
      const user = await githubRequest("/user", headers);
      const published = await snapshot();
      if (!(published.admin_ids || []).includes(user.id)) {
        throw Error("This GitHub account is not in the Rotisserie administrator allowlist.");
      }
      const repo = await githubRequest("/repos/" + REPO, headers);
      if (!repo.permissions || !repo.permissions.push) {
        throw Error("The GitHub token does not have repository write access.");
      }
      token = value.trim(); identity = user.login;
      document.getElementById("github-connect").textContent = "Disconnect GitHub";
      document.getElementById("github-identity").textContent =
        "Connected as " + identity + ". Workflow also verifies your GitHub actor ID.";
    } catch (error) {
      disconnect();
      window.alert(error.message || String(error));
    }
  }
  async function dispatch(body) {
    if (!token) {
      throw Error("Connect your GitHub admin account first. No factory credentials are stored on this page.");
    }
    const payload = JSON.stringify(body);
    if (payload.length > 4096) throw Error("The factory command is too large.");
    await githubRequest("/repos/" + REPO + "/actions/workflows/" + WORKFLOW + "/dispatches",
      { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      { method: "POST", body: JSON.stringify({
        ref: "main", inputs: { request: payload }
      }) });
    return {
      result: "submitted", exit_code: 0,
      scheduler_url: "https://github.com/" + REPO +
        "/actions/workflows/" + WORKFLOW
    };
  }
  window.hostedRequest = function (path, body) {
    if (path === "/api/command" && body) return dispatch(body);
    if (path.startsWith("/api/") && !body) return publicRead(path);
    return Promise.reject(Error("Unsupported hosted operation."));
  };
  document.getElementById("github-connect").addEventListener("click", connect);
})();
