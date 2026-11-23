"""Render the same Rotisserie control-room UI for public GitHub Pages.

The hosted edition has no credential or mutation authority. It reads a
sanitized, already-published snapshot and sends human operators to GitHub's
authenticated workflow_dispatch interface. The workflow revalidates identity.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SNAPSHOT_PATTERN = re.compile(
    r'<script id="fleet-snapshot" type="application/json">([\s\S]*?)</script>'
)
PUBLIC_ADAPTER = r"""
<script>
"use strict";
window.rotisserieHosted = (() => {
  const REPO = "JoshCLWren/comic-pile";
  const WORKFLOW = "https://github.com/" + REPO +
    "/actions/workflows/rotisserie-admin-command.yml";
  let statePromise;
  const load = () => {
    if (!statePromise) {
      statePromise = fetch("https://raw.githubusercontent.com/" + REPO + "/factory-status-live/factory-control-room.json", {cache: "no-store"})
        .then(response => {
          if (!response.ok) throw Error("Published factory snapshot unavailable (" + response.status + ")");
          return response.json();
        }).catch(error => {statePromise = null; throw error;});
    }
    return statePromise;
  };
  function requireDetailedView(name) {
    throw Error(name + " is not present in the sanitized public snapshot. Open the local control room for live diagnostics or use GitHub Actions.");
  }
  function modelsFrom(snapshot, provider) {
    const found = new Map();
    for (const item of snapshot.workers || []) {
      if (item.runtime_source !== provider || !item.runtime_model) continue;
      found.set(item.runtime_model, {model:item.runtime_model, offered:undefined,
        track_record:"Published worker inventory, not an availability guarantee"});
    }
    for (const item of snapshot.candidate_models || []) {
      if (item.source !== provider || !item.model) continue;
      const old = found.get(item.model) || {model:item.model};
      old.offered = item.status === "retired" || item.status === "unavailable" || item.status === "cooling" ? false : undefined;
      old.track_record = "Published status: " + (item.status || "unverified");
      found.set(item.model, old);
    }
    return [...found.values()].sort((a,b) => a.model.localeCompare(b.model));
  }
  async function request(path, body) {
    if (body) throw Error("Hosted control-room writes must be authorized on GitHub.");
    const snapshot = await load();
    if (!snapshot.generated_at) throw Error("Published snapshot has no generation timestamp");
    if (Date.now() - Date.parse(snapshot.generated_at) > 30 * 60 * 1000) {
      throw Error("Published factory data is stale. No live GitHub API calls were made.");
    }
    const url = new URL(path, "https://rotisserie.invalid");
    const key = url.pathname.replace(/^\/api\//, "");
    if (key === "status") {
      return {exit_code:0, working:(snapshot.workers || []).filter(item =>
        item.activity === "working" || item.activity === "running" ||
        item.activity === "active" || item.activity === "busy").map(item => ({
          worker:item.worker,
          title:item.display_name || "Factory worker",
          provider:item.runtime_source || "",
          model:item.runtime_model || "",
          running_since:item.updated,
          run_url:/^[0-9]+$/.test(String(item.run || "")) ?
            "https://github.com/" + REPO + "/actions/runs/" + item.run : "",
          ref:item.work_ref || ""
        }))};
    }
    if (key === "report") return {exit_code:0, merged_day:snapshot.throughput?.merged_day ?? null,
      opened_day:snapshot.throughput?.opened_day ?? null};
    if (key === "providers") {
      const names = new Set((snapshot.workers || []).map(x => x.runtime_source).filter(Boolean));
      for (const item of snapshot.candidate_models || []) if (item.source) names.add(item.source);
      return {exit_code:0, providers:[...names].sort().map(provider =>
        ({provider, name:provider, setup:"unverified"}))};
    }
    if (key === "models") return {exit_code:0,
      models:modelsFrom(snapshot, url.searchParams.get("provider"))};
    if (key === "doctor") return {exit_code:0, findings:[{
      check:"Published snapshot (not a live doctor run)", status:"warning",
      text:"Generated " + snapshot.generated_at + ". " +
        snapshot.cooling_slots + " cooling slots, " +
        snapshot.unavailable_slots + " unavailable slots. Run rotisserie factory doctor locally for full checks."
    }]};
    requireDetailedView(key);
  }
  async function dispatch(command) {
    // GitHub Pages cannot perform the OAuth code exchange or hold a write token.
    // Copy only validated UI values; GitHub performs authentication, and the
    // trusted workflow checks immutable actor ID before executing anything.
    const json = JSON.stringify(command);
    if (json.length > 4096) throw Error("Command is too long");
    try {
      await navigator.clipboard.writeText(json);
    } catch {
      window.prompt("Copy this command JSON, then paste it into GitHub Actions:", json);
    }
    
    const box = document.getElementById("message");
    if (box) {
      box.className = "message visible";
      box.textContent = "Command copied. In the GitHub Actions page, choose Run workflow on main and paste it into command_json. GitHub verifies administrator access before running.";
    }
    window.location.assign(WORKFLOW);
  }
  return {request, dispatch};
})();
</script>
"""

PUBLIC_REQUEST = (
    "async function request(path,body){return window.rotisserieHosted.request(path,body)}"
)
PUBLIC_ACT = """async function act(body,source){
 if(state.mutating)return;
 state.mutating=true;
 try{await window.rotisserieHosted.dispatch(body)}
 catch(error){notice(error.message||String(error),true)}
 finally{state.mutating=false}
}
"""
PUBLIC_REPORT = """async function loadReport(){
 try{
  const d=await get("report",{since:"12h"});
  setText("k-merged",d.merged_day===null?"?":d.merged_day);
  el("overview-merged").innerHTML='<div class="item"><div class="item-title">'+
   h(d.merged_day===null?"Daily merge count unavailable":d.merged_day+" merged in last 24h")+
   '</div><div class="meta">Published aggregate only. '+
   link("https://github.com/"+REPO+"/pulls?q=is%3Apr+is%3Amerged","See actual merged PRs")+
   '</div></div>';
 }catch(err){setText("k-merged","?");el("overview-merged").innerHTML=empty(err.message)}
}
"""


def sanitized_snapshot(source_html: str) -> dict[str, object]:
    """Allowlist fields; public publishing must never gain new data implicitly."""
    match = SNAPSHOT_PATTERN.search(source_html)
    if not match:
        raise ValueError("Existing factory dashboard has no fleet-snapshot JSON")
    data = json.loads(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("Fleet snapshot must be a JSON object")
    generated = data.get("generated_at")
    if not isinstance(generated, str) or not generated:
        raise ValueError("Factory snapshot has no generation timestamp")
    workers: list[dict[str, object]] = []
    for raw in data.get("workers", []):
        if not isinstance(raw, dict):
            continue
        hb = raw.get("heartbeat")
        hb = hb if isinstance(hb, dict) else {}
        work = raw.get("work")
        work_ref = ""
        if isinstance(work, list):
            for item in work:
                if isinstance(item, str) and re.fullmatch(r"(?:pr|issue):[1-9][0-9]*", item):
                    work_ref = item
                    break
        workers.append(
            {
                key: raw.get(key)
                for key in (
                    "worker",
                    "display_name",
                    "runtime_source",
                    "runtime_model",
                    "health",
                    "activity",
                    "updated",
                    "merged_day",
                    "merged_week",
                )
            }
            | {"run": hb.get("run", ""), "work_ref": work_ref}
        )
    candidates = data.get("candidate_health")
    candidates = candidates.get("models", []) if isinstance(candidates, dict) else []
    models = [
        {key: item.get(key) for key in ("source", "model", "status")}
        for item in candidates
        if isinstance(item, dict)
    ]
    throughput = data.get("throughput")
    throughput = throughput if isinstance(throughput, dict) else {}
    return {
        "generated_at": generated,
        "repo": "JoshCLWren/comic-pile",
        "open_prs": data.get("open_prs"),
        "open_issues": data.get("open_issues"),
        "throughput": {
            key: throughput.get(key)
            for key in (
                "opened_day",
                "merged_day",
                "net_day",
            )
        },
        "cooling_slots": data.get("cooling_slots"),
        "unavailable_slots": data.get("unavailable_slots"),
        "workers": workers,
        "candidate_models": models,
    }


def render_page(source_html: str, template: str) -> tuple[str, str]:
    """Return hosted UI and its sanitized JSON; never alter the local UI."""
    public = sanitized_snapshot(source_html)
    main_script = "const TOKEN=__SESSION_TOKEN__, REPO=__REPOSITORY__;"
    request_start = "async function request(path,body){"
    act_start = "async function act(body,source){"
    report_start = "async function loadReport(){"
    if template.count(main_script) != 1:
        raise ValueError("Local UI bootstrap contract changed")
    if template.count(request_start) != 1 or template.count(act_start) != 1:
        raise ValueError("Local UI command interface changed")
    if template.count(report_start) != 1:
        raise ValueError("Local report interface changed")
    output = template.replace(
        main_script,
        'const TOKEN=null, REPO="JoshCLWren/comic-pile";',
        1,
    )
    request_at = output.index(request_start)
    get_at = output.index("async function get(", request_at)
    output = output[:request_at] + PUBLIC_REQUEST + "\n" + output[get_at:]
    act_at = output.index(act_start)
    after_act = output.index("function fmtDate(", act_at)
    output = output[:act_at] + PUBLIC_ACT + output[after_act:]
    report_at = output.index(report_start)
    after_report = output.index("function renderFindings(", report_at)
    output = output[:report_at] + PUBLIC_REPORT + output[after_report:]
    output = output.replace(
        "Served locally · All mutations pass through existing factory commands",
        "Hosted public snapshot · Admin operations run through authenticated GitHub Actions",
    )
    output = output.replace(
        "<script>\nconst TOKEN=",
        PUBLIC_ADAPTER + "<script>\nconst TOKEN=",
        1,
    )
    # Preserve a route to historical fleet telemetry, which otherwise would
    # disappear when the shared control room becomes the public homepage.
    output = output.replace(
        '<div class="footer">',
        '<div class="footer"><a href="?legacy=1">Full fleet telemetry</a> · ',
        1,
    )
    return output, json.dumps(public, separators=(",", ":"), ensure_ascii=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    template = Path(__file__).with_name("web_ui.html").read_text(encoding="utf-8")
    source_html = args.source.read_text(encoding="utf-8")
    public_html, data = render_page(source_html, template)
    args.output.write_text(public_html, encoding="utf-8")
    args.snapshot.write_text(data, encoding="utf-8")


if __name__ == "__main__":
    main()
