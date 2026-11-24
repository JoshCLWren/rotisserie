/* Live reads and writes through the adopter's trusted GitHub operator workflows.
 * The sign-in service dispatches the request, returns the GitHub run ID, and
 * reports that run's state and its JSON result artifact. Every request carries
 * a fresh request ID, and the result must echo it before the page trusts it.
 */
const LIVE_TTL_MS = 60000;
const POLL_MS = 5000;
const READ_WAIT_MS = 8 * 60000;
const WRITE_WAIT_MS = 15 * 60000;
const BATCH_DELAY_MS = 40;
const liveCache = new Map();
let pendingReads = [];
let batchTimer = null;

function newRequestId() {
  if (crypto.randomUUID) return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return [...bytes].map(b => b.toString(16).padStart(2, "0")).join("");
}
function liveKey(name, query) {
  return name + "?" + Object.keys(query).sort().map(k => k + "=" + query[k]).join("&");
}
function clearLive() {
  liveCache.clear();
}
function liveLine(text) {
  const box = document.getElementById("hosted-live");
  if (box) box.textContent = text;
}
function progress(label, run) {
  const text = label + ": GitHub run " + (run.run_id || "") + " " +
    String(run.status || "queued").replace(/_/g, " ") + "…";
  const status = document.getElementById("operation-status");
  if (label.startsWith("Operator") && status) status.textContent = text;
  else liveLine(text);
}

async function waitForRun(accepted, label, limitMs) {
  const deadline = Date.now() + limitMs;
  let run = {run_id: accepted.run_id, run_url: accepted.run_url, status: "queued"};
  for (;;) {
    progress(label, run);
    if (run.status === "completed") return run;
    if (Date.now() > deadline) {
      throw Error(label + " did not finish in " + Math.round(limitMs / 60000) +
        " minutes. It may still act. Follow " + (run.run_url || "the GitHub run") + ".");
    }
    await new Promise(resolve => setTimeout(resolve, POLL_MS));
    run = {...run, ...(await runState(accepted.run_id))};
  }
}

function needsRunReporting(accepted) {
  return !accepted || !/^[0-9]{1,20}$/.test(String(accepted.run_id || ""));
}

function trustedEnvelope(run, requestId, label) {
  const envelope = run.result;
  if (!envelope || typeof envelope !== "object") {
    throw Error(label + " ended (" + (run.conclusion || "unknown") +
      ") before Rotisserie returned a result. Open " + (run.run_url || "the GitHub run") + ".");
  }
  if (envelope.request_id !== requestId) {
    throw Error(label + " returned a result for another request. Refusing to show it.");
  }
  if (envelope.error) throw Error(envelope.error + " (" + (run.run_url || "GitHub run") + ")");
  return envelope;
}

async function flushReads() {
  batchTimer = null;
  const batch = pendingReads.splice(0, CONTRACT.max_batch);
  if (pendingReads.length) batchTimer = setTimeout(flushReads, 0);
  const requestId = newRequestId();
  const label = "Live read (" + batch.map(item => item.name).join(", ") + ")";
  try {
    const accepted = await dispatchRaw({
      operation: "read", request_id: requestId,
      reads: batch.map(item => ({name: item.name, query: item.query}))
    });
    if (needsRunReporting(accepted)) {
      throw Error("The sign-in service accepted the read but cannot report GitHub run " +
        "results yet, so live data is unavailable. Its /runs endpoint is required.");
    }
    const run = await waitForRun(accepted, label, READ_WAIT_MS);
    const envelope = trustedEnvelope(run, requestId, label);
    const observed = envelope.finished_at || new Date().toISOString();
    batch.forEach((item, index) => {
      const answer = (envelope.reads || [])[index];
      if (!answer || answer.name !== item.name) {
        item.reject(Error(label + " did not return " + item.name + "."));
        return;
      }
      item.resolve({
        ...(answer.result || {}),
        _source: {kind: "live", run_url: run.run_url, observed_at: observed}
      });
    });
    liveLine("Live data from GitHub run " + run.run_id + ", observed " +
      new Date(observed).toLocaleTimeString() + ".");
  } catch (error) {
    for (const item of batch) item.reject(error);
    liveLine(error.message);
  }
}

function liveRead(name, query) {
  const key = liveKey(name, query);
  const hit = liveCache.get(key);
  if (hit && Date.now() - hit.time < LIVE_TTL_MS) return hit.value;
  const value = new Promise((resolve, reject) => {
    pendingReads.push({name, query, resolve, reject});
    if (pendingReads.length >= CONTRACT.max_batch) {
      clearTimeout(batchTimer);
      void flushReads();
    } else if (!batchTimer) batchTimer = setTimeout(flushReads, BATCH_DELAY_MS);
  }).then(answer => {
    // A pending answer (an estimate waiting for its Neon run) must be asked again.
    if (answer && answer.result === "pending" && liveCache.get(key)?.value === value) {
      liveCache.delete(key);
    }
    return answer;
  }, error => {
    if (liveCache.get(key)?.value === value) liveCache.delete(key);
    throw error;
  });
  liveCache.set(key, {time: Date.now(), value});
  return value;
}

async function checkModel(body) {
  if (!body.model || body.model === "auto") return;
  const listing = await liveRead("models", {provider: body.provider});
  const model = (listing.models || []).find(m => m.model === body.model);
  if (!model) {
    throw Error("Model " + body.model + " is not in the live model list for " + body.provider +
      ". Choose a listed model or Automatic selection.");
  }
  if (model.offered === false || model.disabled) {
    throw Error("Model " + body.model + " is " + (model.disabled ? "turned off" : "unavailable") +
      " for " + body.provider + ". Choose another model or Automatic selection.");
  }
}

async function liveWrite(body) {
  if (!signedIn()) throw Error("Sign in with GitHub to use this.");
  if (!CONTRACT.write_operations.includes(body.operation)) {
    throw Error("The hosted control room does not support " + body.operation + ".");
  }
  if (body.operation === "run") await checkModel(body);
  const requestId = newRequestId();
  const accepted = await dispatchRaw({...body, request_id: requestId});
  if (needsRunReporting(accepted)) {
    return {exit_code: 0, result: "accepted by GitHub. This sign-in service does not report " +
      "the operator run's outcome yet, so check the repository's Actions page"};
  }
  const label = "Operator run";
  const run = await waitForRun(accepted, label, WRITE_WAIT_MS);
  const envelope = trustedEnvelope(run, requestId, label);
  clearLive();
  const result = envelope.result || {};
  if (envelope.exit_code !== 0) {
    throw Error((result.error || result.reason || (result.status ? result.target + ": " +
      result.status : "Rotisserie returned exit " + envelope.exit_code)) +
      ". GitHub operator run " + run.run_url + " " + (run.conclusion || "failed") + ".");
  }
  return {...result, exit_code: 0, operator: {run_url: run.run_url, conclusion: run.conclusion}};
}
