/* GitHub performs the interactive sign-in. The adopter's sign-in service
 * ([dashboard] auth_url) exchanges the code and issues a short-lived
 * dashboard session. The browser never receives a GitHub personal or OAuth
 * access token. Without a service the dashboard stays read-only.
 */
const AUTH_BASE = "__AUTH_BASE__";
const SESSION_KEY = "rotisserie-github-admin-session";
const SIGNED_OUT_HINT = "Read-only visitor. Sign in to use live views and operator actions.";
let session = null;
let identity = null;
const signedOutListeners = [];
const signedInListeners = [];

function signedIn() {
  return Boolean(session);
}
function onSignedOut(listener) {
  signedOutListeners.push(listener);
}
function onSignedIn(listener) {
  signedInListeners.push(listener);
}
function setIdentity(text) {
  const status = document.getElementById("github-identity");
  if (status) status.textContent = text;
}
function disconnect() {
  session = null; identity = null;
  try { sessionStorage.removeItem(SESSION_KEY); } catch (_) { /* optional */ }
  document.body.classList.remove("hosted-signed-in");
  const button = document.getElementById("github-connect");
  if (button) button.textContent = "Sign in with GitHub";
  setIdentity(SIGNED_OUT_HINT);
  for (const listener of signedOutListeners) listener();
}

async function authRequest(path, options = {}) {
  if (!session) throw Error("Sign in with GitHub to use this.");
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
    if (response.status === 401 || response.status === 403) {
      disconnect();
      setIdentity("GitHub sign-in expired or was refused. Sign in again.");
      throw Error("Your GitHub sign-in expired or was refused. Sign in again. " + message);
    }
    throw Error(message);
  }
  return response.json();
}

async function validateSession() {
  if (!session) return disconnect();
  try {
    const profile = await authRequest("/session");
    identity = profile.login;
    document.body.classList.add("hosted-signed-in");
    document.getElementById("github-connect").textContent = "Sign out";
    setIdentity("Signed in with GitHub as " + identity + ". Admin access verified.");
    for (const listener of signedInListeners) listener();
  } catch (error) {
    disconnect();
    setIdentity(error.message || "GitHub sign-in expired.");
  }
}

function connect() {
  if (session) return disconnect();
  if (!AUTH_BASE) {
    setIdentity("Sign-in is not configured.");
    return;
  }
  location.assign(AUTH_BASE + "/start");
}

async function dispatchRaw(body) {
  if (JSON.stringify(body).length > 4096) throw Error("Factory command too large.");
  return authRequest("/dispatch", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body:JSON.stringify(body)
  });
}

async function runState(runId) {
  if (!/^[0-9]{1,20}$/.test(String(runId))) throw Error("The sign-in service returned an invalid run.");
  return authRequest("/runs/" + runId);
}

function bootstrapSession() {
  const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
  const granted = hash.get("rotisserie_session");
  const error = hash.get("rotisserie_error");
  if (granted || error) history.replaceState(null,"",location.pathname+location.search);
  if (error) {
    disconnect();
    setIdentity(error === "not_admin" ? "This GitHub account is not an administrator." :
      "GitHub sign-in was not completed.");
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
