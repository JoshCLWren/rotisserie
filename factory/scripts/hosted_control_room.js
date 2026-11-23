/* Hosted adapter for the canonical Rotisserie web_ui.html.
 * The public dashboard contains no GitHub credentials. GitHub OAuth runs
 * through ComicPile Rust; Rotisserie keeps all factory operation safeguards.
 */
"use strict";
(function () {
  const WORKFLOW = "rotisserie-operator.yml";
  const CACHE_MS = 60000;
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

  // GitHub performs the interactive sign-in. ComicPile's existing Rust
  // service exchanges the code and issues a short-lived dashboard session.
  // The browser never receives a GitHub personal or OAuth access token.
  const AUTH_BASE = "https://comic-pile.vercel.app/api/v2/factory/github";
  const SESSION_KEY = "rotisserie-github-admin-session";
  let session = null;
  let identity = null;

  function disconnect() {
    session = null; identity = null;
    try { sessionStorage.removeItem(SESSION_KEY); } catch (_) { /* optional */ }
    const button = document.getElementById("github-connect");
    const status = document.getElementById("github-identity");
    if (button) button.textContent = "Sign in with GitHub";
    if (status) status.textContent = "Read-only visitor";
  }

  async function authRequest(path, options = {}) {
    const response = await fetch(AUTH_BASE + path, {
      ...options, mode:"cors", credentials:"omit",
      headers: {
        "Authorization":"Bearer " + session, "Accept":"application/json",
        ...(options.headers || {})
      }
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      const message = data.detail || "GitHub request failed (" + response.status + ").";
      if (response.status === 401 || response.status === 403) disconnect();
      throw Error(message);
    }
    return response.json();
  }

  async function validateSession() {
    if (!session) return disconnect();
    try {
      const profile = await authRequest("/session");
      identity = profile.login;
      document.getElementById("github-connect").textContent = "Sign out";
      document.getElementById("github-identity").textContent =
        "Signed in with GitHub as " + identity + ". Admin access verified.";
    } catch (error) {
      disconnect();
      document.getElementById("github-identity").textContent =
        error.message || "GitHub sign-in expired.";
    }
  }

  function connect() {
    if (session) return disconnect();
    location.assign(AUTH_BASE + "/start");
  }

  async function dispatch(body) {
    if (!session) throw Error("Sign in with GitHub to operate the factory.");
    if (JSON.stringify(body).length > 4096) throw Error("Factory command too large.");
    return authRequest("/dispatch", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify(body)
    });
  }

  function bootstrapSession() {
    const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
    const granted = hash.get("rotisserie_session");
    const error = hash.get("rotisserie_error");
    if (granted || error) history.replaceState(null,"",location.pathname+location.search);
    if (error) {
      disconnect();
      document.getElementById("github-identity").textContent =
        error === "not_admin" ? "This GitHub account is not an administrator." :
          "GitHub sign-in was not completed.";
      return;
    }
    if (granted) {
      session = granted;
      try { sessionStorage.setItem(SESSION_KEY,granted); } catch (_) { /* optional */ }
    } else {
      try { session = sessionStorage.getItem(SESSION_KEY); } catch (_) { /* optional */ }
    }
    if (session) void validateSession();
    else disconnect();
  }
  window.hostedRequest = function (path, body) {
    if (path === "/api/command" && body) return dispatch(body);
    if (path.startsWith("/api/") && !body) return publicRead(path);
    return Promise.reject(Error("Unsupported hosted operation."));
  };
  document.getElementById("github-connect").addEventListener("click", connect);
  bootstrapSession();
})();
