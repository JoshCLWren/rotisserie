/* ComicPile fleet console: trusted snapshot + public GitHub evidence.
 * Display only. The factory controller remains sole authority for leases,
 * model selection, CI gating, review, merge, and deployment.
 */
(() => {
  "use strict";
  const source = document.getElementById("fleet-snapshot");
  if (!source) return;
  let data;
  try { data = JSON.parse(source.textContent); } catch { return; }
  const repo = "JoshCLWren/comic-pile";
  const gh = "https://github.com/" + repo;
  const api = "https://api.github.com/repos/" + repo;
  const keys = ["provider","model","worker","issue","pr","run","failure"];
  const emptyFilters = () => Object.fromEntries(keys.map(k => [k,""]));
  const s = { view:"attention", text:"", filters:emptyFilters(), prs:[], issues:[], runs:[],
    errors:[], loaded:false, period:14, compare:"", actions:[] };
  try { s.actions = JSON.parse(localStorage.getItem("comicpile-fleet-notes-v1") || "[]"); }
  catch { s.actions = []; }
  const e = value => String(value == null ? "" : value).replace(/[&<>"']/g,
    c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const age = x => { const n = Date.parse(x); if (!Number.isFinite(n)) return "not observed";
    const m = Math.max(0,Math.floor((Date.now()-n)/60000));
    return m<60?m+"m ago":m<1440?Math.floor(m/60)+"h ago":Math.floor(m/1440)+"d ago"; };
  const date = x => { const n = new Date(x); return Number.isFinite(n.valueOf())?n.toLocaleString():"not observed"; };
  const link = (url,label) => '<a target="_blank" rel="noopener noreferrer" href="'+e(url)+'">'+e(label)+' ↗</a>';
  const labels = x => (x.labels||[]).map(l=>typeof l==="string"?l:l.name);
  const stage = x => (labels(x).find(l=>/^factory:(review|ci|ready|changes-requested|blocked|building|unowned)$/.test(l))||"unclassified").replace("factory:","");
  const owner = x => (labels(x).find(l=>/^factory:[0-9]+$/.test(l))||"").replace("factory:","");
  const inFlight = x => ["queued","in_progress","waiting","pending","requested"].includes(x.status);
  const ciFor = pr => s.runs.filter(r=>inFlight(r)&&(r.head_sha===pr.head?.sha||(r.pull_requests||[]).some(p=>p.number===pr.number)));
  const status = value => { const v=String(value||"unknown");
    const kind=/^(success|healthy|proven|ready|completed|merged|persisted|ok)$/i.test(v)?"good":
      /fail|error|blocked|unavailable|throttle|quota|cool|timeout|changes-requested|cancel/i.test(v)?"bad":
      /running|progress|queued|waiting|pending|review|^ci$/i.test(v)?"info":"warn";
    return '<span class="f-status '+kind+'">'+e(v)+'</span>'; };
  const workers = Array.isArray(data.workers)?data.workers:[];
  const candidate = data.candidate_health||{providers:[],models:[]};
  const retry = data.retry || {backoffs:[],reservations:[],pr_retries:[]};
  const backoff = (kind, match) => (retry.backoffs||[]).find(b=>b.kind===kind&&match(b));
  const cooldown = b => !b ? "Not reported" : b.suspended ? "Suspended after "+b.streak+" failures" :
    (b.next_retry_at||b.until) ? "Retry "+date(b.next_retry_at||b.until)+" · streak "+b.streak : "No active delay";

  const models = (()=>{ const map=new Map();
    const add=(provider,model,fields)=>{ if(!provider||!model)return;
      const id=provider+"\0"+model;map.set(id,{...(map.get(id)||{}),provider,model,...fields}); };
    for(const m of candidate.models||[])add(m.source,m.model,{health:m.status,outcome:m.outcome,until:m.until,updated:m.updated,evidence:"trusted health projection"});
    for(const m of data.opencode_free_roster?.models||[]) {
      const provider=m.connection||"", id=provider+"\0"+m.model_id;
      if(!map.has(id))add(provider,m.model_id,{health:m.status,outcome:m.detail,evidence:"observed attempt, not catalog proof"});
    }
    for(const w of workers)if(w.runtime_source&&w.runtime_model){
      const id=w.runtime_source+"\0"+w.runtime_model;
      if(!map.has(id))add(w.runtime_source,w.runtime_model,{health:"unverified",outcome:w.attempt_outcome,run:w.attempt_run,evidence:"latest trusted worker observation"});
    }
    return [...map.values()];
  })();
  const providers=[...new Set([...workers.map(w=>w.runtime_source),...(candidate.providers||[]).map(p=>p.source),...models.map(m=>m.provider)].filter(Boolean))].sort();
  const app=document.createElement("div");app.id="fleet-app";document.body.appendChild(app);
  document.querySelector("main").style.display="none";
  document.querySelector('meta[http-equiv="refresh"]')?.remove();
  const badge=(text,number)=>'<span>'+e(text)+'</span>'+(number==null?"":"<small>"+e(number)+"</small>");
  const inspect=(type,id,label)=>'<button class="f-link" data-inspect="'+e(type)+'" data-id="'+e(id)+'">'+e(label)+'</button>';
  const row=cells=>"<tr>"+cells.map(c=>"<td>"+c+"</td>").join("")+"</tr>";
  const table=(headers,rows,empty)=>'<div class="f-tablewrap"><table class="f-table"><thead><tr>'+
    headers.map(h=>"<th>"+e(h)+"</th>").join("")+"</tr></thead><tbody>"+
    (rows.length?rows.join(""):'<tr><td colspan="'+headers.length+'" class="f-muted">'+e(empty||"No matching evidence")+"</td></tr>")+
    "</tbody></table></div>";
  const extract=(type,x)=>{if(type==="worker")return {provider:x.runtime_source,model:x.runtime_model,worker:x.worker,
      issue:(x.work||[]).filter(a=>a.kind==="issue").map(a=>a.number).join(","),pr:(x.work||[]).filter(a=>a.kind==="pr").map(a=>a.number).join(","),
      run:x.attempt_run,failure:x.attempt_outcome,all:x.display_name+" "+x.health};
    if(type==="model")return {provider:x.provider,model:x.model,run:x.run,failure:x.outcome};
    if(type==="provider")return {provider:x.provider,failure:x.outcome};
    if(type==="pr")return {worker:owner(x),pr:x.number,issue:(x.body||"").match(/(?:closes|refs|fixes)\s+#(\d+)/i)?.[1]||"",
      run:ciFor(x).map(r=>r.id).join(","),failure:stage(x),all:x.title};
    if(type==="issue")return {worker:owner(x),issue:x.number,failure:stage(x),all:x.title};
    return {run:x.id,pr:(x.pull_requests||[]).map(p=>p.number).join(","),failure:x.conclusion||x.status,all:x.name};};
  const filtered=(type,rows)=>rows.filter(x=>{const f=extract(type,x);
    return (!s.text||JSON.stringify(f).toLowerCase().includes(s.text.toLowerCase()))&&
      keys.every(k=>!s.filters[k]||String(f[k]||"").toLowerCase().includes(s.filters[k].toLowerCase()));});
  const filters=()=>'<div class="f-filters"><input data-filter="search" placeholder="Search evidence / IDs" value="'+e(s.text)+'">'+
    keys.map(k=>'<input data-filter="'+k+'" placeholder="'+e(k[0].toUpperCase()+k.slice(1))+'" aria-label="Filter '+e(k)+'" value="'+e(s.filters[k])+'">').join("")+
    '<button class="f-button" data-clear>Clear</button></div>';
  const next=(type,x)=>{if(type==="pr"){
      if(ciFor(x).length)return "Wait for CI, do not assign";
      return ({"changes-requested":"Repair review findings",review:"Independent exact-head review",ci:"Verify exact-head checks",
        ready:"Verify merge gates",blocked:"Resolve documented blocker",building:"Check active lease before takeover"})[stage(x)]||"Inspect policy and next action";
    }
    if(type==="run")return inFlight(x)?"Observe in-progress run, do not duplicate":"Inspect outcome and handoff";
    if(type==="worker"){const b=backoff("worker",v=>v.worker===String(x.worker));return b&&b.next_retry_at&&Date.parse(b.next_retry_at)>Date.now()?"Respect worker Fibonacci backoff":x.activity==="working"?"Observe active lease":x.attempt_outcome?"Check retry eligibility":"Await heartbeat evidence";}
    if(type==="model"){const b=backoff("model",v=>v.source===x.provider&&v.model===x.model);return (b?.suspended||b?.next_retry_at&&Date.parse(b.next_retry_at)>Date.now()||x.until&&x.until*1000>Date.now())?"Respect durable cooldown":"Require real execution proof";}
    return "Check lease, dependencies, and priority";};
  const evidence=(type,x)=>type==="pr"?gh+"/pull/"+x.number:type==="issue"?gh+"/issues/"+x.number:
    type==="run"?gh+"/actions/runs/"+x.id:type==="worker"&&x.attempt_run?gh+"/actions/runs/"+x.attempt_run:
    type==="model"&&x.run?gh+"/actions/runs/"+x.run:gh+"/issues/1093";
  const detail=(type,id)=>{const items=type==="worker"?workers:type==="model"?models:type==="pr"?s.prs:type==="issue"?s.issues:s.runs;
    const x=items.find(a=>String(type==="worker"?a.worker:type==="model"?a.provider+"/"+a.model:type==="pr"||type==="issue"?a.number:a.id)===String(id));
    if(!x)return;const url=evidence(type,x);
    const extra = type==="worker" ? {backoff:backoff("worker",b=>b.worker===String(x.worker)),reservation:(retry.reservations||[]).filter(v=>v.worker===String(x.worker))} : type==="model" ? {backoff:backoff("model",b=>b.source===x.provider&&b.model===x.model)} : type==="pr" ? {retry:(retry.pr_retries||[]).filter(v=>v.pr===x.number)} : {};
    const drawer=document.createElement("div");drawer.className="f-drawer";
    drawer.innerHTML='<aside><div class="f-head"><span class="f-eyebrow">Evidence / '+e(type)+'</span><button class="f-button" data-close>Close ✕</button></div>'+
      '<h2>'+e(x.title||x.display_name||x.model||x.name||id)+'</h2>'+
      '<div class="f-fact"><span class="f-muted">Identity</span><strong>'+e(id)+'</strong></div>'+
      '<div class="f-fact"><span class="f-muted">Observed</span><span>'+e(x.updated_at||x.updated||data.generated_at)+'</span></div>'+
      '<div class="f-fact"><span class="f-muted">Next action</span><span>'+e(next(type,x))+'</span></div>'+
      '<div class="f-note" style="margin-top:14px">Read-only evidence. This Site cannot authorize merges, assign workers, or change leases. Check live GitHub state before acting.</div>'+
      '<div class="f-buttons">'+link(url,"Open source")+' <button class="f-button" data-copy>Copy action</button>'+
      '<button class="f-button" data-record>Record action locally</button></div><h3>Source record</h3><pre>'+e(JSON.stringify({record:x,operational:extra},null,2))+'</pre></aside>';
    drawer.addEventListener("click",ev=>{if(ev.target===drawer||ev.target.closest("[data-close]"))drawer.remove();
      if(ev.target.closest("[data-copy]"))navigator.clipboard?.writeText("ComicPile: "+next(type,x)+"\n"+url);
      if(ev.target.closest("[data-record]")){const note=prompt("Next action, saved only in this browser:",next(type,x));
        if(note?.trim()){s.actions.unshift({at:new Date().toISOString(),type,id,action:note.trim(),url});
          s.actions=s.actions.slice(0,100);persist();drawer.remove();render();}}});
    document.body.appendChild(drawer);
  };
  const persist=()=>{try{localStorage.setItem("comicpile-fleet-notes-v1",JSON.stringify(s.actions));}catch{}};
  const attention=()=>{const items=[];
    for(const p of s.prs){const t=stage(p),running=ciFor(p).length;
      if(running||["changes-requested","blocked","review","ci"].includes(t))
        items.push({type:"pr",id:p.number,title:p.title,detail:"PR #"+p.number+" · "+t+(running?" · CI in progress":""),
          action:next("pr",p),priority:running?3:t==="changes-requested"?1:2,updated:p.updated_at});}
    for(const w of workers)if(w.delivery_outcome==="persisted"&&w.delivery_pr&&w.attempt_outcome&&w.attempt_outcome!=="success")
      items.push({type:"worker",id:w.worker,title:"Code delivered despite execution failure",
        detail:"Worker "+w.worker+" · "+w.attempt_outcome+" · persisted PR #"+w.delivery_pr,
        action:"Inspect persisted PR and retry accounting separately",priority:2,updated:w.updated});
    for(const r of s.runs)if(r.status==="completed"&&["failure","timed_out","cancelled"].includes(r.conclusion))
      items.push({type:"run",id:r.id,title:r.name,detail:"Run "+r.id+" · "+r.conclusion,
        action:"Inspect execution and persisted delivery separately",priority:4,updated:r.updated_at});
    const rows=items.filter(x=>{const f={...extract(x.type,x.type==="pr"?s.prs.find(p=>p.number===x.id):x.type==="worker"?workers.find(w=>w.worker===x.id):s.runs.find(r=>r.id===x.id)),all:x.title};
      return (!s.text||JSON.stringify(f).toLowerCase().includes(s.text.toLowerCase()))&&
        keys.every(k=>!s.filters[k]||String(f[k]||"").toLowerCase().includes(s.filters[k].toLowerCase()));})
      .sort((a,b)=>a.priority-b.priority||Date.parse(b.updated)-Date.parse(a.updated));
    return '<div class="f-panel"><div class="f-head"><div><h2>Action queue</h2><p>Prioritized evidence, not an automatic dispatch plan.</p></div>'+status(s.loaded?"GitHub live":"Snapshot only")+'</div>'+
      filters()+(rows.length?'<div class="f-attention">'+rows.slice(0,35).map((x,i)=>
        '<div class="f-alert"><span class="f-number">'+(i+1)+'</span><div class="f-grow"><strong>'+inspect(x.type,x.id,x.title)+
        '</strong><small>'+e(x.detail)+" · "+e(age(x.updated))+'</small></div><span class="f-next">'+e(x.action)+
        '</span><button class="f-button" data-inspect="'+e(x.type)+'" data-id="'+e(x.id)+'">Evidence</button></div>').join("")+"</div>":
        '<div class="f-empty">No actionable observations in the loaded window. This is not proof the backlog is empty.</div>')+
      '</div><div class="f-grid"><div class="f-panel"><h2>Execution ≠ delivery</h2><p>Timeout, persisted commit, open PR, merge, and production acceptance are distinct outcomes.</p>'+
      '<div class="f-note">If a run failed but created a durable PR, count the execution failure and persisted delivery separately. Unverified fields remain unknown.</div>'+
      link(gh+"/issues/1093","Trusted outcome registry")+'</div><div class="f-panel"><h2>CI-aware allocation</h2><p>Running checks are observation-only. The controller must recheck the exact head before a new lease.</p>'+
      '<strong style="font-size:27px">'+(s.loaded?s.runs.filter(inFlight).length:"—")+'</strong><div class="f-small">In-flight workflow runs in fetched window</div></div></div>';
  };
  const workerView=()=>'<div class="f-panel"><div class="f-head"><div><h2>Provider-derived worker slots</h2>'+
    '<p>No pinned model roster. Missing heartbeat does not mean crashed.</p></div>'+status(filtered("worker",workers).length+" matching")+'</div>'+
    filters()+table(["Worker","Provider / model","Activity","Execution","Persisted delivery","Work / lease","Retry / evidence"],
      filtered("worker",workers).map(w=>row([
        inspect("worker",w.worker,"#"+w.worker)+'<div class="f-small">'+e(w.display_name)+'</div>',
        e(w.runtime_source||"Unobserved")+'<div class="f-small">'+e(w.runtime_model||"No executed model")+'</div>',
        status(w.activity||w.health),status(w.attempt_outcome||"No attempt"),
        w.delivery_outcome==="persisted"&&w.delivery_pr?status("Persisted")+" "+link(gh+"/pull/"+w.delivery_pr,"PR #"+w.delivery_pr):status(w.delivery_outcome||"Unverified"),
        ((w.work||[]).length?(w.work||[]).map(i=>link(i.url,i.kind+" #"+i.number)).join(" · "):'<span class="f-muted">No GitHub owner</span>')+
        ((retry.reservations||[]).some(v=>v.worker===String(w.worker))?'<div class="f-small">Capacity reserved until '+e(date((retry.reservations||[]).find(v=>v.worker===String(w.worker)).expires_at))+'</div>':""),
        (backoff("worker",b=>b.worker===String(w.worker))?e(cooldown(backoff("worker",b=>b.worker===String(w.worker))))+"<div class=\"f-small\">":"")+
        (w.attempt_run?link(gh+"/actions/runs/"+w.attempt_run,"Run "+w.attempt_run):'<span class="f-muted">No run</span>')+
        (backoff("worker",b=>b.worker===String(w.worker))?"</div>":"")
      ])),"No matching workers")+"</div>";
  const providerView=()=>{const grouped=providers.map(provider=>{const ws=workers.filter(w=>w.runtime_source===provider);
      const h=(candidate.providers||[]).find(p=>p.source===provider);
      return {provider,slots:ws.length,models:models.filter(m=>m.provider===provider).length,
        attempts:ws.filter(w=>w.attempt_run).length,outcome:h?.outcome||"not reported",until:h?.until};});
    const ps=filtered("provider",grouped),ms=filtered("model",models);
    return '<div class="f-panel"><div class="f-head"><div><h2>Integrated providers</h2><p>Discovered from trusted slot and execution evidence, not Site configuration.</p></div>'+
      status(ps.length+" providers")+'</div>'+filters()+table(["Provider","Observed models","Slots","Run evidence","Health","Retry after"],
      ps.map(p=>row([e(p.provider),e(p.models),e(p.slots),e(p.attempts),status(p.outcome),
        (backoff("provider",b=>b.source===p.provider)?e(cooldown(backoff("provider",b=>b.source===p.provider))):p.until?e(date(p.until*1000)):"Not exported")])))+'</div>'+
      '<div class="f-panel"><h2>Observed models</h2><p>Published catalog membership alone does not prove an invocation worked.</p>'+
      table(["Provider","Model","Health","Last outcome","Cooldown / provenance"],ms.map(m=>row([
        e(m.provider),inspect("model",m.provider+"/"+m.model,m.model),status(m.health||"unverified"),
        e(m.outcome||"Not observed"),(backoff("model",b=>b.source===m.provider&&b.model===m.model)?e(cooldown(backoff("model",b=>b.source===m.provider&&b.model===m.model)))+" · ":"")+(m.until?"Until "+e(date(m.until*1000))+" · ":"")+e(m.evidence||"trusted observation")
      ])),"No model evidence for this filter")+"</div>";
  };
  const prView=()=>'<div class="f-panel"><div class="f-head"><div><h2>Open pull requests</h2>'+
    '<p>In-flight CI is matched by head SHA or PR association. Never assign against a running check.</p></div>'+status(s.prs.length+" fetched")+'</div>'+
    filters()+table(["PR / head","Stage","Owner","CI","Next action","Updated"],filtered("pr",s.prs).map(p=>{
      const ci=ciFor(p);
      return row([inspect("pr",p.number,"#"+p.number+" "+p.title)+'<div class="f-small">'+e((p.head?.sha||"").slice(0,10))+'</div>',
        status(stage(p)),e(owner(p)||"Unowned"),ci.length?ci.map(r=>link(r.html_url,r.status)).join(" · "):"No in-flight run observed",
        e(next("pr",p)) + ((retry.pr_retries||[]).find(x=>x.pr===p.number&&x.head===p.head?.sha)?.retry_after ? " · stage retry pending" : "") ,e(age(p.updated_at))]);}),"No matching PRs")+"</div>";
  const issueView=()=>'<div class="f-panel"><div class="f-head"><div><h2>Open issues</h2>'+
    '<p>Newest 100 GitHub issue records. Trusted total appears above; no false zero for unseen pages.</p></div>'+status(s.issues.length+" fetched")+'</div>'+
    filters()+table(["Issue","Stage","Owner","Labels","Updated"],filtered("issue",s.issues).map(i=>row([
      inspect("issue",i.number,"#"+i.number+" "+i.title),status(stage(i)),e(owner(i)||"Unowned"),
      e(labels(i).slice(0,5).join(", ")),e(age(i.updated_at))
    ])),"No matching issues in fetched window")+"</div>";
  const runView=()=>{const baseline=s.runs.find(r=>String(r.id)===s.compare);
    return '<div class="f-panel"><div class="f-head"><div><h2>GitHub Actions handoffs</h2>'+
      '<p>Recent workflow execution, exact head, and historical run comparison.</p></div>'+status(s.runs.length+" fetched")+'</div>'+
      filters()+'<div class="f-filters"><select data-compare class="f-field"><option value="">Select baseline run…</option>'+
      s.runs.map(r=>'<option value="'+e(r.id)+'"'+(s.compare===String(r.id)?" selected":"")+'>'+
        e(r.name+" #"+r.run_number+" · "+(r.conclusion||r.status))+'</option>').join("")+'</select></div>'+
      (baseline?'<div class="f-note">Baseline '+link(baseline.html_url,"run "+baseline.id)+" · "+e(baseline.conclusion||baseline.status)+
        " · "+e(date(baseline.created_at))+". Comparing run states does not prove equivalent code delivery.</div>":"")+
      table(["Workflow / run","Status","Head","Started","Compared to baseline","Evidence"],
        filtered("run",s.runs).map(r=>row([
          inspect("run",r.id,r.name+" #"+r.run_number),status(r.conclusion||r.status),
          e(r.head_branch||"Unknown")+'<div class="f-small">'+e((r.head_sha||"").slice(0,10))+'</div>',
          e(age(r.created_at)),baseline?e(Math.round((Date.parse(r.created_at)-Date.parse(baseline.created_at))/3600000)+"h"):"Select baseline",
          link(r.html_url,"Jobs / logs")
        ])),"No workflow runs in fetched window")+"</div>";
  };
  const deliveryView=()=>'<div class="f-panel"><div class="f-head"><div><h2>Delivery funnel</h2>'+
    '<p>Opened, merged and closed are separate event counts, not a cohort conversion rate.</p></div>'+
    '<select data-period class="f-field"><option value="14"'+(s.period===14?" selected":"")+'>14 days</option>'+
    '<option value="30"'+(s.period===30?" selected":"")+'>30 days</option></select></div>'+filters()+
    table(["Group","Worker / model","Opened","Merged","Closed unmerged","Merge credit"],
      (data.delivery_funnel||[]).filter(r=>r.days===s.period&&
        (!s.text||JSON.stringify(r).toLowerCase().includes(s.text.toLowerCase()))&&
        (!s.filters.model||r.group==="model"&&String(r.name).includes(s.filters.model))&&
        (!s.filters.worker||r.group==="worker"&&String(r.name).includes(s.filters.worker))).map(r=>row([
          e(r.group),e(r.name||"Unattributed"),e(r.opened),e(r.merged),e(r.closed),Number(r.commit_share||0).toFixed(2)
        ])),"No attributed delivery in this window")+
    '<div class="f-note" style="margin-top:14px">A failed worker can still deliver a persisted PR. A merged PR is not proof of production deployment. Inspect source evidence.</div></div>';
  const actionsView=()=>'<div class="f-panel"><div class="f-head"><div><h2>Action journal</h2>'+
    '<p>Saved only in this browser. Does not change GitHub, leases, or workflow state.</p></div>'+
    '<div class="f-buttons"><button class="f-button" data-export>Export JSON</button><button class="f-button" data-wipe>Clear local notes</button></div></div>'+
    '<div class="f-panel">'+(s.actions.length?s.actions.map(a=>'<div class="f-alert"><div class="f-grow"><strong>'+
      e(a.action)+'</strong><small>'+e(a.type+" "+a.id)+" · "+e(date(a.at))+'</small></div>'+link(a.url,"Evidence")+'</div>').join(""):
      '<div class="f-empty">Inspect a record and choose “Record action locally”.</div>')+'</div>'+
    '<div class="f-panel"><h2>Trigger an authorized workflow</h2><p>GitHub enforces live CI, lease, Rust-first, review, and merge gates.</p>'+
    '<div class="f-buttons">'+link(gh+"/actions/workflows/fixed-model-factory-dispatch.yml","Dispatcher")+
    link(gh+"/actions/workflows/factory-completion-drain.yml","Completion drain")+
    link(gh+"/actions/workflows/factory-status-pages.yml","Refresh telemetry")+'</div></div>';
  const views={attention,workers:workerView,providers:providerView,prs:prView,issues:issueView,runs:runView,delivery:deliveryView,actions:actionsView};
  const names={attention:"Needs attention",workers:"Worker fleet",providers:"Providers / models",prs:"Pull requests",issues:"Issue backlog",runs:"Workflow runs",delivery:"Delivery outcomes",actions:"Action journal"};
  const render=()=>{const tabs=Object.entries(names),counts={workers:workers.length,providers:providers.length,prs:s.prs.length,issues:data.open_issues,runs:s.runs.length};
    const kpis=[["Open PRs",data.open_prs,"trusted snapshot"],["Open issues",data.open_issues,"trusted snapshot"],
      ["In-flight runs",s.loaded?s.runs.filter(inFlight).length:"—","GitHub API"],["Executable slots",data.executable_slot_capacity,"not active workers"],
      ["Merged / 24h",data.throughput?.merged_day,"trusted event window"]];
    app.innerHTML='<div class="f-layout"><aside class="f-side"><div class="f-brand"><span class="f-logo">🏭</span><div><strong>ComicPile</strong><small>Factory operations</small></div></div>'+
      '<nav class="f-nav">'+tabs.map(([id,name])=>'<button data-view="'+id+'" class="'+(s.view===id?"active":"")+'">'+badge(name,counts[id])+'</button>').join("")+
      '</nav><div class="f-foot">Authority: GitHub + factory controller<br>Snapshot: '+e(date(data.generated_at))+
      '<br>Rust-first · no pinned model roster<br>CI running = no assignment</div></aside>'+
      '<div class="f-main"><div class="f-top"><div><div class="f-eyebrow">ComicPile / Control room</div><h1>'+e(names[s.view])+
      '</h1><div class="f-small">Snapshot '+e(age(data.generated_at))+' · GitHub API '+(s.loaded?"read":"pending / unavailable")+
      ' · Observation ≠ authorization</div></div><div class="f-buttons"><button class="f-button" data-refresh>↻ Refresh</button>'+
      link(gh+"/actions","GitHub Actions")+'</div></div>'+
      '<div class="f-kpis">'+kpis.map(k=>'<div class="f-kpi"><label>'+e(k[0])+'</label><strong>'+e(k[1]??"Unknown")+
      '</strong><small>'+e(k[2])+'</small></div>').join("")+'</div>'+
      (s.errors.length?'<div class="f-note">Live GitHub reads failed: '+e(s.errors.join("; "))+
      '. Snapshot is still visible; unavailable evidence is unknown, not zero.</div>':"")+
      views[s.view]()+ '<div class="f-small" style="padding-top:12px">Trusted factory snapshot + public GitHub API. Local notes are not shared. No tokens, hard-coded model IDs, or direct lease mutations.</div></div></div>';
  };
  app.addEventListener("click",ev=>{const b=ev.target.closest("[data-view],[data-inspect],[data-clear],[data-refresh],[data-export],[data-wipe]");
    if(!b)return;
    if(b.dataset.view){s.view=b.dataset.view;s.text="";s.filters=emptyFilters();render();}
    if(b.dataset.inspect)detail(b.dataset.inspect,b.dataset.id);
    if(b.hasAttribute("data-clear")){s.text="";s.filters=emptyFilters();render();}
    if(b.hasAttribute("data-refresh"))refresh();
    if(b.hasAttribute("data-export")){const blob=new Blob([JSON.stringify(s.actions,null,2)],{type:"application/json"});
      const url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download="comicpile-actions.json";a.click();
      setTimeout(()=>URL.revokeObjectURL(url),1000);}
    if(b.hasAttribute("data-wipe")&&confirm("Clear local action notes?")){s.actions=[];persist();render();}
  });
  app.addEventListener("input",ev=>{const k=ev.target.dataset.filter;if(!k)return;
    if(k==="search")s.text=ev.target.value;else s.filters[k]=ev.target.value;
    const start=ev.target.selectionStart,end=ev.target.selectionEnd;render();
    const target=app.querySelector('[data-filter="'+k+'"]');
    if(target){target.focus();try{target.setSelectionRange(start,end);}catch{}}});
  app.addEventListener("change",ev=>{if(ev.target.hasAttribute("data-period")){s.period=Number(ev.target.value);render();}
    if(ev.target.hasAttribute("data-compare")){s.compare=ev.target.value;render();}});
  const refresh=async()=>{s.errors=[];s.loaded=false;render();
    const requests=[["prs","/pulls?state=open&sort=updated&direction=desc&per_page=100"],
      ["issues","/issues?state=open&sort=updated&direction=desc&per_page=100"],["runs","/actions/runs?per_page=100"]];
    const results=await Promise.allSettled(requests.map(async ([key,path])=>{
      const r=await fetch(api+path,{headers:{Accept:"application/vnd.github+json"},cache:"no-store"});
      if(!r.ok)throw Error(key+" HTTP "+r.status);const body=await r.json();
      const rows=Array.isArray(body)?body:body.workflow_runs;if(!Array.isArray(rows))throw Error(key+" unexpected response");
      return key==="issues"?rows.filter(x=>!x.pull_request):rows;}));
    results.forEach((r,i)=>{if(r.status==="fulfilled")s[requests[i][0]]=r.value;
      else s.errors.push(String(r.reason?.message||r.reason));});
    s.loaded=results.some(r=>r.status==="fulfilled");render();};
  render();refresh();
  // Refresh the trusted Pages snapshot every five minutes, avoiding API-rate-limit churn.
  setInterval(() => { if (document.visibilityState === "visible") location.reload(); }, 300000);
})();