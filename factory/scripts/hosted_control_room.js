/* Hosted adapter for the canonical Rotisserie web_ui.html.
 * The public dashboard contains no GitHub credentials. GitHub OAuth runs
 * through the adopter sign-in service; Rotisserie keeps all factory operation
 * safeguards. The builder inlines factory/scripts/hosted/*.js before this file
 * and injects CONTRACT from src/rotisserie/control_contract.py.
 */
const SIGN_IN_NEEDED = "Sign in with GitHub to use this. It needs a live Rotisserie " +
  "evaluation, which the published snapshot cannot provide.";

function hostedRead(path) {
  const parsed = new URL(path, location.origin);
  const name = parsed.pathname.replace(/^\/api\//, "");
  if (parsed.searchParams.get("fresh") === "1") {
    clearLive();
    clearPublic();
  }
  parsed.searchParams.delete("fresh");
  if (CONTRACT.public_reads.includes(name)) return publicRead(name, parsed.searchParams);
  if (!CONTRACT.live_reads.includes(name)) return Promise.reject(Error("Unknown read operation."));
  if (!signedIn()) {
    if (name === "providers" || name === "models") return publicRead(name, parsed.searchParams);
    return Promise.reject(Error(SIGN_IN_NEEDED));
  }
  const live = liveRead(name, Object.fromEntries(parsed.searchParams));
  if (name !== "providers" && name !== "models") return live;
  // An older sign-in service or a missing read caller must not take away the
  // launcher: fall back to labeled history, which is never selectable.
  return live.catch(async error => {
    const history = await publicRead(name, parsed.searchParams);
    liveLine("Live " + name + " unavailable, showing published history: " + error.message);
    return {...history, live_error: error.message};
  });
}

window.hostedRequest = function (path, body) {
  if (path === "/api/command" && body) return liveWrite(body);
  if (path.startsWith("/api/") && !body) return hostedRead(path);
  return Promise.reject(Error("Unsupported hosted operation."));
};
document.addEventListener("click", event => {
  const button = event.target.closest && event.target.closest("button");
  if (button && /(^|-)refresh$/.test(button.id || "")) clearLive();
}, true);
onSignedOut(() => {
  clearLive();
  if (window.rotisserieForget) window.rotisserieForget();
});
// The page keys stored reads by this viewer. While a session is still being checked the
// viewer is unknown (""), and the page stores nothing, so live results never land as public.
window.hostedViewer = () => (signedIn() ? identity || "" : "public");
onSignedIn(() => void resumeWrite());
// Signed-out visitors see the published snapshot. Refresh it in place, never by
// reloading the page: a reload drops a signed-in tab's live reads and operator runs.
setInterval(() => {
  const button = document.getElementById("refresh");
  if (!signedIn() && !document.hidden && button && !button.disabled) button.click();
}, SNAPSHOT_MS);
document.getElementById("github-connect").addEventListener("click", connect);
bootstrapSession();
