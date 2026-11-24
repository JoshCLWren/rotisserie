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
// The page keys stored live reads by this viewer, so one person never sees another's.
window.hostedViewer = () => (signedIn() && identity) || "public";
document.getElementById("github-connect").addEventListener("click", connect);
bootstrapSession();
