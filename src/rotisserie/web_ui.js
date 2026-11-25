"use strict";
const el=id=>document.getElementById(id);
const h=x=>String(x==null?"":x).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const link=(url,label)=>{try{const u=new URL(url);if(u.protocol!=="https:"||u.hostname!=="github.com")return h(label);return '<a href="'+h(u.href)+'" target="_blank" rel="noopener noreferrer">'+h(label)+'</a>'}catch{return h(label)}};
const gh=(ref)=>link("https://github.com/"+REPO+(String(ref).startsWith("pr:")?"/pull/":"/issues/")+String(ref).split(":")[1],ref);
const badge=(text,kind)=>'<span class="pill '+h(kind||"")+'"><span class="dot"></span>'+h(text)+'</span>';
const empty=(txt)=>'<div class="empty">'+h(txt)+'</div>';
const raw=(obj)=>'<pre class="raw">'+h(JSON.stringify(obj,null,2))+'</pre>';
const state={page:"overview",providers:[],queue:null,status:null,report:null,recap:null,doctor:null,catalogModels:[],mutating:false};
const modelCache=new Map();
const LOCAL=TOKEN!==null,JOB_KEY="rotisserie-web-job";
let modelRequestSeq=0;
let statusRequest=null;
const titles={overview:["Control room","Start work and monitor the factory."],queue:["Work queue","Which issues are ready and why others are waiting."],workers:["Workers","Real runs and active lease ownership."],outcomes:["Run history","What work was attempted, delivered or failed."],models:["Models","Browse real, provider-discovered model IDs."],settings:["Settings","Factory limits and priorities."],health:["Diagnostics","Find problems before assigning more workers."]};
function notice(message,error=false){const box=el("message");box.className="message visible"+(error?" error":"");box.textContent=message;}
async function request(path,body){const opt={headers:{"X-Rotisserie-Token":TOKEN}};if(body){opt.method="POST";opt.headers["Content-Type"]="application/json";opt.body=JSON.stringify(body)}const res=await fetch(path,opt);let data;try{data=await res.json()}catch{throw Error("The console did not return JSON.")}if(!res.ok)throw Error(data.error||"Request rejected");return data}
/* Panel reads persist in localStorage, keyed by repository, viewer and arguments. Seconds until each kind is stale: */
const TTL={status:90,leases:90,incident:120,why:120,heal:600,doctor:600,report:900,recap:900,outcomes:900,stats:900,settings:1800,providers:1800,models:3600};
const PANELS={status:()=>loadStatus(),leases:()=>loadLeases(),incident:()=>loadIncidents(),why:()=>loadQueue(),heal:()=>loadHeal(),doctor:()=>loadDoctor(),report:()=>loadReport(),recap:()=>loadRecap(),outcomes:()=>loadOutcomes(),stats:()=>loadStats(),settings:()=>loadSettings(),providers:()=>loadProviders()};
const KEEP="rotisserie:v1:",KEEP_ENTRY=400000,KEEP_TOTAL=2000000,pending=new Set();
let forceFresh=false,epoch=0;
const seen=new Set(),lazy={"overview-turning":()=>loadTurning(),"overview-recap":()=>loadRecap(),"overview-heal":()=>loadHeal()};
const viewer=()=>LOCAL?"local":window.hostedViewer?window.hostedViewer():"public";
const keyOf=(name,params)=>KEEP+REPO+":"+viewer()+":"+name+"?"+new URLSearchParams(Object.entries(params).sort());
const keyName=k=>k.slice(k.lastIndexOf(":")+1).split("?")[0];
function ours(){try{return Object.keys(localStorage).filter(k=>k.startsWith(KEEP+REPO+":"))}catch{return[]}}
function kept(key){try{const v=JSON.parse(localStorage.getItem(key)||"null");return v&&typeof v.got==="number"&&typeof v.at==="number"&&v.data&&typeof v.data==="object"?v:null}catch{return null}}
function keep(key,data){try{const blob=JSON.stringify({got:Date.now(),at:dataTime(data),data});if(blob.length>KEEP_ENTRY)return;const rows=ours().filter(k=>k!==key).map(k=>({k,got:(kept(k)||{got:0}).got,n:(localStorage.getItem(k)||"").length})).sort((a,b)=>a.got-b.got);let total=blob.length+rows.reduce((t,r)=>t+r.n,0);while(rows.length&&total>KEEP_TOTAL){const r=rows.shift();localStorage.removeItem(r.k);total-=r.n}localStorage.setItem(key,blob)}catch{}}
function drop(names,who){epoch++;for(const k of ours())if((!names||names.includes(keyName(k)))&&(!who||who(k.split(":")[3])))try{localStorage.removeItem(k)}catch{}}
window.rotisserieForget=()=>drop(null,v=>v!=="public"&&v!=="local");
function dataTime(d){const t=Date.parse(d.read_at||(d._source&&d._source.observed_at)||d.generated_at||d.published_at||"");return Number.isNaN(t)?Date.now():Math.min(t,Date.now())}
const asOf=d=>d&&d._at?'<div class="hint asof" data-at="'+d._at+'">'+h(ago(d._at))+'</div>':"";
function ago(at){const s=Math.max(0,Math.round((Date.now()-at)/1000)),n=s<60?[s,"second"]:s<7200?[Math.round(s/60),"minute"]:[Math.round(s/3600),"hour"];return "As of "+n[0]+" "+n[1]+(n[0]===1?"":"s")+" ago"}
const loading=(id,text,name,params={})=>{if(forceFresh||!viewer()||!kept(keyOf(name,params)))el(id).innerHTML=empty(text)};
const storable=d=>d&&!d.job&&!d.error&&typeof d.exit_code==="number"&&!/^(github_unavailable|invalid|timeout|unavailable|interrupted)$/.test(d.result||"");
async function fetchRead(name,params,fresh){const qs=new URLSearchParams(fresh?{...params,fresh:"1"}:params);return request("/api/"+name+(qs.size?"?"+qs:""))}
async function get(name,params={}){
 const fresh=forceFresh,since=epoch,stored=TTL[name]&&viewer()&&!("target" in params)&&!("background" in params),key=stored&&keyOf(name,params),hit=key&&!fresh&&kept(key);
 if(hit&&Date.now()-hit.got<TTL[name]*1000)return {...hit.data,_at:hit.at};
 if(hit&&PANELS[name]&&!pending.has(key)){pending.add(key);fetchRead(name,params,false).then(d=>{if(storable(d)&&since===epoch){keep(key,d);PANELS[name]()}}).catch(()=>{}).finally(()=>pending.delete(key));return {...hit.data,_at:hit.at}}
 const d=await fetchRead(name,params,fresh);
 if(key&&storable(d)&&since===epoch)keep(key,d);
 return {...d,_at:dataTime(d)};
}
const SWITCHED=["providers","models","why","doctor"],AFFECTS={settings:["settings","why"],prioritize:["why","settings"],provider_switch:SWITCHED,model_switch:SWITCHED,backoffs_reset:SWITCHED,incident_start:["incident","why","status"],incident_stop:["incident","why","status"],estimate_neon:[],store_selftest:[],store_export:[]};
function decisionText(d){const s=d.scheduler||{},a=d.assignment||s.assignment;if(a)return "Scheduler decision: "+(a.kind==="pr"?"PR":"issue")+" #"+a.number+" assigned to worker "+a.worker+" ("+a.provider+", "+(a.model==="auto"?"model chosen at start":a.model)+").";return "Scheduler decision: "+({deferred:"no work assigned",busy:"#"+s.busy_number+" is already being worked on",failed:"the scheduler failed"}[s.status]||"unknown")+(s.reason?". "+s.reason:"")+(d.error?". "+d.error:"")+"."}
async function followDecision(run){const box=document.createElement("div");box.className="hint";box.id="scheduler-decision";box.textContent="Waiting for the scheduler's decision…";el("message").append(box);try{box.textContent=decisionText(await get("watch",{run:String(run)}))}catch(err){box.textContent="Could not read the scheduler's decision: "+err.message}}
async function act(body,source,resumed){
 if(state.mutating)return;
 state.mutating=true;state.lastJob=null;
 const button=source&&source.tagName==="BUTTON"?source:null;
 const operation=({claim:"Requesting claim",finish:"Finishing claim",labels:"Starting label doctor",incident_start:"Starting incident",incident_stop:"Ending incident",provider_switch:"Switching provider",model_switch:"Switching model",run:"Submitting worker request",unstick:"Unblocking work",cancel:"Stopping worker",sync:"Synchronizing PR",doctor_fix:"Applying safe repairs",release_expired:"Releasing expired leases",settings:"Saving setting",close:"Closing pull request",allow_self_review:"Allowing self-review",backoffs_reset:"Resetting quota backoffs",assign:"Assigning to agent",health_reset:"Resetting one model",store_migrate:"Starting store migration",schema_apply:"Applying the store schema",prioritize:"Setting priority"})[body.operation]||"Applying change";
 if(button){button.disabled=true;button.classList.add("is-loading");}
 el("operation-status").textContent=operation+"…";
 el("message").className="message";
 try{
  let data=resumed?await track(resumed,body.operation,operation):await send(body,operation);
  if(data.superseded)return;
  const unstuck=body.operation==="unstick"&&data.plan;
  if(unstuck)el("unstick-plan").innerHTML=renderUnstick(data);
  else if(data.exit_code!==0)throw Error(data.error||(data.status?data.target+": "+data.status+(data.reason?" ("+data.reason+")":""):"")||"Rotisserie returned "+(data.result||"an error")+" (exit "+data.exit_code+")");
  const message=el("message");
  if(unstuck)notice(unstickHeadline(data),data.exit_code!==0&&data.exit_code!==3);
  else if(data.scheduler_url){
   message.className="message visible";
   message.innerHTML="Request submitted. Worker start is not yet confirmed. "+link(data.scheduler_url,"Track GitHub run");
   if(data.scheduler_run_id)void followDecision(data.scheduler_run_id);
  }else if(data.bridge&&data.bridge.url){
   message.className="message visible";
   message.innerHTML="Label doctor "+h(body.mode||"dry-run")+" started. "+link(data.bridge.url,"Open the GitHub run");
  }else if(body.operation==="claim"||body.operation==="finish"){
   notice(data.target+": "+(data.status||data.result)+(data.status==="requested"?". The label changed; the controller answers on the item. Use Claim runs or Follow to see its answer.":"."));
  }else if(/_switch$/.test(body.operation)){
   notice((data.kind==="model"?"Model "+data.model:"Provider "+data.provider)+(data.changed?" turned ":" was already ")+(data.enabled?"on.":"off."));
   modelCache.clear();await loadProviders();
  }else notice("Rotisserie reports: "+(data.result||"completed")+".");
  if(data.operator&&data.operator.run_url)el("message").insertAdjacentHTML("beforeend"," "+link(data.operator.run_url,"GitHub operator run: "+(data.operator.conclusion||"completed")));
  el("operation-status").textContent="Refreshing factory state…";
  drop(AFFECTS[body.operation]||null);
  await refresh();
 }catch(err){drop(AFFECTS[body.operation]||null);notice(err.message||String(err),true)}
 finally{state.mutating=false;el("operation-status").textContent="";if(button){button.disabled=false;button.classList.remove("is-loading")}}
}
function fmtDate(value){if(!value)return "Not recorded";const d=new Date(value);return Number.isNaN(d.getTime())?"Not recorded":d.toLocaleString()}
function setText(id,value){el(id).textContent=String(value)}
function renderWorkers(items,large=false){
 if(!items?.length)return empty("No active workers or claims.");
 const groups=new Map();
 for(const item of items){
  const key=item.run_url||("worker:"+item.worker+":"+(item.running_since||""));
  if(!groups.has(key))groups.set(key,{...item,refs:[]});
  if(item.ref&&!groups.get(key).refs.includes(item.ref))groups.get(key).refs.push(item.ref);
 }
 const rows=[...groups.values()];
 const visible=large?rows:rows.slice(0,3);
 return visible.map(item=>'<div class="item"><div class="item-head"><div><div class="item-title">'+h(item.title||"Worker activity")+'</div><div class="meta">'+item.refs.map(gh).join(" · ")+'</div><div class="meta">'+h(item.provider||"Provider unknown")+' · '+h(item.model&&item.model!=="auto"?item.model:"Automatic model selection")+'</div></div>'+badge(item.coding_since?"Working":item.run_url?"Assigned":"Claim held",item.coding_since?"good":"")+'</div><div class="meta">Started '+h(fmtDate(item.coding_since||item.running_since))+'</div>'+(large?'<div class="item-actions">'+(item.run_url?link(item.run_url,"View run"):"")+' <button class="button small danger" data-cancel="'+h(item.ref)+'">Stop worker</button></div>':"")+'</div>').join("")+(rows.length>visible.length?'<div class="hint">'+h(rows.length-visible.length)+' more active runs. Open Workers to see all.</div>':"");
}
function loadStatus(){
 if(statusRequest)return statusRequest;
 statusRequest=(async()=>{
  try{
   const d=await get("status");
   if(d.exit_code!==0)throw Error(d.error||d.result);
   state.status=d;
   const owners=new Set((d.working||[]).map(x=>x.worker||x.ref));
   setText("k-workers",owners.size);
   el("overview-workers").innerHTML=asOf(d)+renderWorkers(d.working||[]);
   el("workers-content").innerHTML=asOf(d)+renderWorkers(d.working||[],true);
  }catch(err){
   el("overview-workers").innerHTML=empty(err.message);
   el("workers-content").innerHTML=empty(err.message);
   setText("k-workers","?");
  }finally{statusRequest=null}
 })();
 return statusRequest;
}
async function loadReport(){try{const d=await get("report",{since:"12h"});state.report=d;setText("k-merged",(d.merged||[]).length);el("overview-merged").innerHTML=asOf(d)+((d.merged||[]).slice(0,3).map(x=>'<div class="item"><div class="item-title">'+link("https://github.com/"+REPO+"/pull/"+x.number,"#"+x.number)+" "+h(x.title)+"</div></div>").join("")||empty("No recent merges."))}catch(err){setText("k-merged","?");el("overview-merged").innerHTML=empty(err.message)}}
function renderFindings(d){if(!Array.isArray(d.findings))return raw(d);return d.findings.map(f=>'<div class="item"><div class="item-head"><div><div class="item-title">'+h(f.check)+'</div><div class="meta">'+h(f.check==="work turning"?decorate(f.text):(f.text||f.hint||""))+'</div></div>'+badge(f.status,f.status==="ok"?"good":f.status==="fail"?"bad":"")+'</div></div>').join("")||empty("No diagnostic findings.")}
const recapPart=(title,rows)=>rows.length?'<h3 class="recap-h">'+h(title)+'</h3>'+rows.join(""):"";
const recapRow=(text,meta,url)=>'<div class="item"><div class="item-title">'+(url?link(url,text):h(text))+'</div>'+(meta?'<div class="meta">'+h(meta)+'</div>':"")+'</div>';
function renderRecap(d){const m=d.merged||{},w=d.went_wrong||{},p=d.providers||{};const merged=g=>(m[g]||[]).map(x=>recapRow(x.title,"#"+x.number,x.url));const wrongTitle="What went wrong"+(w.read!==w.failed_runs?" (newest "+w.read+" of "+w.failed_runs+" failed runs)":"");
 return sourceNote(d)+'<div class="item-head"><div class="item-title recap-verdict">'+h(d.verdict)+'</div>'+badge(d.tone,d.tone==="good"?"good":d.tone==="rough"?"bad":"")+'</div>'+(d.burning?'<div class="hint">'+h(decorate(d.burning.text))+' ('+h(fmtDate(d.burning.from))+' to '+h(fmtDate(d.burning.to))+')</div>':"")+'<div class="hint">Since '+h(fmtDate(d.since))+'</div>'
 +recapPart("Merged: product",merged("product"))+recapPart("Merged: factory",merged("factory"))
 +recapPart("Needs you",(d.needs_you||[]).map(x=>recapRow(x.text,x.action,x.url)))
 +recapPart("Fixed by itself",(d.healed||[]).map(x=>recapRow(x.text,fmtDate(x.at),x.url)))
 +recapPart("The factory clears these on its next pass",(d.clearing||[]).map(x=>recapRow(x.text,"",x.url)))
 +recapPart(wrongTitle,(w.causes||[]).map(c=>recapRow(c.count+"× "+c.words+((c.providers||[]).length?" ("+c.providers.join(", ")+")":""),[c.example,c.fix?"Fix: "+c.fix:""].filter(Boolean).join(" · "))))
 +recapPart("Providers (last 24 hours)",[...(p.delivered||[]).map(x=>recapRow(x.provider+" delivered "+x.delivered+" of "+x.runs+" runs")),...(p.wasted||[]).map(x=>recapRow(x.provider+" delivered nothing: "+x.failed+" of "+x.runs+" runs failed")),...(p.off||[]).map(x=>recapRow((x.model?x.provider+" "+x.model:x.provider)+" is switched off",x.reason||"no reason given")),...(p.kept_hot||[]).map(x=>recapRow(x.provider+" kept hot: "+(x.models||[]).join(", "))),...(p.paused||[]).map(x=>recapRow(x.provider+": "+x.models+" model(s) paused"+(x.why?" after "+x.why:""),"until "+fmtDate(x.until)))])
 +recapPart("Stuck",(d.stuck||[]).map(x=>recapRow(x.text,"",x.url)))
 +(d.database?recapPart("Database",[recapRow(d.database.text,d.database.status)]):"")+(d.github?recapPart("GitHub requests",(d.github.lines||[d.github.text]).map(t=>recapRow(t,d.github.status))):"")
 +((d.unread||[]).length?'<div class="hint">Could not read: '+h(d.unread.join(", "))+'</div>':"")}
async function loadHeal(){loading("overview-heal","Reading what the factory fixed by itself…","heal",{since:"24h"});try{const d=await get("heal",{since:"24h"});const rows=(d.healed||[]).map(x=>recapRow(x.text,fmtDate(x.at),x.url));el("overview-heal").innerHTML=asOf(d)+(rows.length?rows.join(""):empty("The factory cleared no stall by itself in the last 24 hours."))}catch(err){el("overview-heal").innerHTML=empty(err.message)}}
async function loadRecap(){loading("overview-recap","Reading what the factory did…","recap",{since:"12h"});try{const d=await get("recap",{since:"12h"});state.recap=d;el("overview-recap").innerHTML=renderRecap(d)}catch(err){el("overview-recap").innerHTML=empty(err.message)}}
const FACES={turning:"🍗🔄 Turning",slowing:"🐢 Slowing",burning:"🔥🔥 Burning",unknown:"❔ Unknown"};
const decorate=t=>{const m=/^(\w+):([\s\S]*)$/.exec(String(t||""));const f=m&&FACES[m[1].toLowerCase()];return f?f+":"+m[2]:String(t||"")};
const reasonWords=r=>Object.entries(r||{}).map(([k,v])=>(k==="not logged"?"reason not logged":k)+" "+v).join(", ");
function renderTurning(d){const e=d.evidence||{},p=e.dispatcher_passes||[],f=e.control_failures||[],lp=e.item_loops||[],b=e.backlog||{},s=e.store||{},pr=e.providers||{};
 return sourceNote(d)+'<div class="item-head"><div class="item-title recap-verdict">'+h(FACES[d.verdict]||d.verdict)+'</div>'+badge(d.verdict,d.verdict==="turning"?"good":d.verdict==="burning"?"bad":"")+'</div><div class="meta">'+h(d.text||"")+'</div>'+(d.next?'<div class="hint">👉 '+h(d.next)+'</div>':"")
 +recapRow("🧑‍🍳 Last worker started: "+fmtDate(e.last_worker_started),e.workers_running?e.workers_running+" running now":"")
 +recapRow("🍽️ Last outcome served: "+fmtDate(e.last_outcome_recorded))
 +p.map(x=>recapRow("🎛️ Dispatcher pass: "+x.eligible+" eligible, "+x.assigned+" assigned, "+x.none+" none",[fmtDate(x.at),reasonWords(x.none_reasons)].filter(Boolean).join(" · "),x.url)).join("")
 +recapRow("💥 Control failures in the last "+e.failure_window_minutes+"m: "+f.length,"Slowing at "+e.failure_threshold)+f.slice(0,5).map(x=>recapRow(x.workflow+(x.step?" at "+x.step:""),fmtDate(x.at),x.url)).join("")
 +recapRow("🔁 Looping items: "+lp.length)+lp.slice(0,5).map(x=>recapRow((x.title||x.target)+" (#"+x.number+")"+(x.incident?" [incident]":"")+": "+x.text,x.next,x.url)).join("")
 +(pr.read?recapRow("🧊 Providers cooling: "+(pr.cooling||[]).length+" of "+pr.sources,(pr.cooling||[]).join(", ")):"")
 +(b.open_prs!=null?recapRow("📦 Backlog: "+b.eligible_issues+" eligible issues, "+b.open_prs+" open factory PRs",b.limit_text||""):"")
 +(e.holds?recapRow("⏰ Expired holds: "+(e.holds.expired==null?"not readable here":e.holds.expired),e.holds.note||""):"")
 +recapRow("🗄️ Store: last dispatcher pass "+(s.dispatcher_store_step||"unknown"),"db usage "+(s.db_usage_status||"unknown")+", published "+fmtDate(s.db_usage_checked_at)+(s.blocked?" · Turso reports BLOCKED":""))}
async function loadTurning(){loading("overview-turning","Checking whether the factory is turning…","turning");try{const d=await get("turning");el("overview-turning").innerHTML=renderTurning(d)}catch(err){el("overview-turning").innerHTML=empty(err.message)}}
async function loadDoctor(){try{const d=await get("doctor");state.doctor=d;setText("k-health",(d.findings||[]).filter(x=>x.status!=="ok").length);el("overview-health").innerHTML=renderFindings({...d,findings:(d.findings||[]).filter(x=>x.status!=="ok").slice(0,5)});if(!(d.findings||[]).some(x=>x.status!=="ok"))el("overview-health").innerHTML=empty("All checked systems healthy.");el("overview-health").insertAdjacentHTML("afterbegin",asOf(d));el("health-content").innerHTML=asOf(d)+renderFindings(d)}catch(err){setText("k-health","?");el("overview-health").innerHTML=empty(err.message);el("health-content").innerHTML=empty(err.message)}}
async function loadIncidents(){try{const d=await get("incident");const rows=d.incidents||[];el("incidents-content").innerHTML=asOf(d)+(rows.length?'<div class="table-wrap"><table class="table"><thead><tr><th>Issue</th><th>Attempts</th><th>State</th><th>Reason</th><th>Action</th></tr></thead><tbody>'+rows.map(i=>'<tr><td>#'+h(i.issue)+'</td><td>'+h(i.attempts)+'</td><td>'+badge(i.active?"active":"ended",i.active?"bad":"good")+'</td><td>'+h(i.reason||"")+'</td><td><button class="button small" data-incident-stop="'+h(i.issue)+'">Stop</button></td></tr>').join("")+'</tbody></table></div>':empty("No incident. Normal intake."))}catch(err){el("incidents-content").innerHTML=empty(err.message)}}
async function loadQueue(){loading("queue-content","Evaluating the current work graph…","why");try{const d=await get("why");state.queue=d;setText("k-ready",(d.ready||[]).length);renderQueue()}catch(err){setText("k-ready","?");el("queue-content").innerHTML=empty(err.message)}}
function renderQueue(){
 const d=state.queue;if(!d)return;
 let issues=d.issues||[],filter=el("queue-filter").value.toLowerCase(),status=el("queue-state").value;
 issues=issues.filter(x=>{const ready=x.verdict?.status==="ready";return(status==="all"||status==="ready"&&ready||status==="held"&&!ready)&&(String(x.issue).includes(filter)||String(x.title).toLowerCase().includes(filter))});
 const counts=Object.entries(d.not_starting||{}).map(([k,v])=>k.replace(/[-_]/g," ")+": "+v).join(" · ");
 const shown=issues.slice(0,35);
 el("queue-content").innerHTML=sourceNote(d)+'<div class="hint" style="margin:4px 0 12px">'+h((d.ready||[]).length)+' ready / '+h(d.open_issues||0)+' open · '+h(counts)+'</div>'+(shown.map(x=>'<div class="item"><div class="item-head"><div><div class="item-title">'+gh("issue:"+x.issue)+' · '+h(x.title)+'</div><div class="meta">'+h(x.verdict?.reasons?.map?.(r=>r.text||r).join("; ")||"")+'</div></div>'+badge(x.verdict?.status||"unknown",x.verdict?.status==="ready"?"good":"")+'</div><div class="item-actions"><button class="button small" data-explain="'+h(x.issue)+'">Why?</button><button class="button small" data-choose="'+h(x.issue)+'">Assign</button><button class="button small ghost" data-prioritize="'+h(x.issue)+'">Priority</button></div></div>').join("")||empty("No issues match."))+(issues.length>shown.length?'<div class="hint">Showing first 35 of '+h(issues.length)+' matches. Refine the search to narrow the list.</div>':"");
}
async function explain(number){try{const d=await get("why",{target:"issue:"+number});notice("#"+number+": "+(d.reasons||[]).join("; ")+(d.error?" · "+d.error:""))}catch(err){notice(err.message,true)}}
async function loadProviders(){
 const selected=el("provider").value;
 const catalogSelected=el("catalog-provider").value;
 el("provider").disabled=true;
 try{
  const d=await get("providers");
  if(d.exit_code!==0)throw Error(d.error||d.result);
  state.providers=d.providers||[];
  renderSwitches();el("provider-switches").insertAdjacentHTML("afterbegin",asOf(d));
  const options=state.providers.map(p=>'<option value="'+h(p.provider)+'">'+h(p.name)+' · '+h(p.setup)+'</option>').join("");
  el("provider").innerHTML=options||'<option value="">No providers found</option>';
  const preferred=state.providers.find(p=>p.provider===selected)||state.providers.find(p=>p.setup==="ready")||state.providers[0];
  if(preferred)el("provider").value=preferred.provider;
  el("provider").disabled=!preferred;
  el("catalog-provider").innerHTML=options||'<option value="">No providers found</option>';
  if(catalogSelected&&state.providers.some(p=>p.provider===catalogSelected))el("catalog-provider").value=catalogSelected;
  else if(preferred)el("catalog-provider").value=preferred.provider;
  await loadModelOptions();
  if(state.page==="models")await loadCatalog();
  preview();
 }catch(err){
  el("provider").innerHTML='<option value="">Provider discovery failed</option>';
  el("provider-switches").innerHTML=empty(err.message);
  el("catalog-summary").textContent=err.message;
  el("model").innerHTML='<option value="auto">Automatic selection</option>';
  el("model").disabled=false;
 }
}
async function fetchModels(provider){
 if(!modelCache.has(provider))modelCache.set(provider,get("models",{provider}).then(d=>{
  if(d.exit_code!==undefined&&d.exit_code!==0)throw Error(d.error||"Could not discover models.");
  return d.models||[];
 }).catch(err=>{modelCache.delete(provider);throw err}));
 return modelCache.get(provider);
}
const refusal=m=>!m?"":m.excluded?"is excluded by the worker right now ("+m.excluded+")":m.disabled?"is turned off"+(m.disabled_reason?" ("+m.disabled_reason+")":""):m.offered===false?"is not offered by the provider now":"";
function refusedModel(data){const p=state.picker,m=data.model!=="auto"&&p&&p.provider===data.provider&&p.models.find(x=>x.model===data.model),why=m&&refusal(m);return why?"Model "+data.model+" "+why+". Choose another model or Automatic selection.":""}
async function loadModelOptions(){
 const provider=el("provider").value;
 const seq=++modelRequestSeq;
 const old=el("model").value;
 el("model").disabled=true;
 el("model").innerHTML='<option value="auto">Loading models…</option>';
 if(!provider){el("launch").disabled=true;return;}
 try{
  const models=await fetchModels(provider);
  if(seq!==modelRequestSeq||el("provider").value!==provider)return;
  state.picker={provider,models};
  const selectable=models.filter(m=>!refusal(m));
  el("model").innerHTML='<option value="auto">Automatic selection (recommended)</option>'+models.map(m=>'<option value="'+h(m.model)+'"'+(refusal(m)?" disabled":"")+'>'+h(m.model)+(m.historical?" (history only)":m.excluded?" (excluded now)":m.disabled?" (turned off)":m.offered===false?" (unavailable)":m.offered!==true?" (unverified)":"")+'</option>').join("");
  if(selectable.some(m=>m.model===old))el("model").value=old;
 }catch(err){
  if(seq!==modelRequestSeq)return;
  el("model").innerHTML='<option value="auto">Automatic selection (catalog unavailable)</option>';
  el("catalog-summary").textContent="Model discovery failed: "+err.message;
 }finally{
  if(seq===modelRequestSeq){el("model").disabled=false;el("launch").disabled=false;preview();}
 }
}
function renderCatalog(){
 const list=state.catalogModels||[],provider=el("catalog-provider").value;
 const q=el("model-search").value.trim().toLowerCase();
 const filtered=list.filter(m=>m.model.toLowerCase().includes(q));
 const displayed=filtered.slice(0,40);
 el("catalog-summary").textContent=filtered.length+" model"+(filtered.length===1?"":"s")+" found · "+(displayed.length<filtered.length?"showing first 40 · ":"")+"Model listing does not guarantee quota.";
 el("model-catalog").innerHTML=displayed.map(m=>'<div class="mini"><div class="item-head"><div class="item-title">'+h(m.model)+'</div>'+badge(m.excluded?"Excluded now":m.offered===false?"Unavailable":m.offered===true?"Listed":"Unverified",m.offered===true&&!m.excluded?"good":m.offered===false||m.excluded?"bad":"")+'</div><div class="meta">'+h(m.track_record||"No recorded deliveries")+(m.excluded?" · The worker refuses it: "+h(m.excluded):"")+'</div><div class="meta">Switch: '+h(switchText(m.disabled,m.disabled_reason))+'</div><div class="item-actions"><button class="button small" data-use-model="'+h(m.model)+'"'+(refusal(m)?" disabled":"")+'>Select model</button>'+switchButton(m.disabled,provider,m.model)+'</div></div>').join("")||empty("No models match this provider or search.");
}
async function loadCatalog(){
 const provider=el("catalog-provider").value;
 if(!provider)return;
 el("model-catalog").innerHTML=empty("Discovering models…");
 el("catalog-summary").textContent="Loading model catalog…";
 try{
  const models=await fetchModels(provider);
  if(el("catalog-provider").value!==provider)return;
  state.catalogModels=models;renderCatalog();
 }catch(err){if(el("catalog-provider").value===provider){el("catalog-summary").textContent="";el("model-catalog").innerHTML=empty("Could not discover models: "+err.message)}}
}
async function loadSettings(){loading("settings-content","Loading repository settings…","settings");try{const d=await get("settings");el("settings-content").innerHTML=sourceNote(d)+(d.settings||[]).map(s=>'<div class="item"><div class="item-head"><div><div class="item-title">'+h(s.title)+'</div><div class="meta">'+h(s.description)+'<br>'+h(s.name)+' · '+h(s.origin)+'</div></div><div class="item-actions"><input aria-label="'+h(s.title)+'" style="width:85px;padding:9px;background:#111a25;color:#fff;border:1px solid #3b4a5a;border-radius:8px" data-setting-name="'+h(s.name)+'" value="'+h(s.value)+'"><button class="button small" data-save-setting="'+h(s.name)+'">Save</button></div></div></div>').join("")||empty("No settings found.")}catch(err){el("settings-content").innerHTML=empty(err.message)}}
async function loadOutcomes(){try{const d=await get("outcomes");el("outcomes-content").innerHTML=asOf(d)+((d.requests||[]).slice(0,40).map(x=>'<div class="item"><div class="item-head"><div><div class="item-title">'+h(x.target||"Next eligible")+' · '+h(x.action||"advance")+'</div><div class="meta">'+h(x.model||"Auto model")+' · '+h(x.decision||"")+'</div></div>'+badge(x.goal||x.worker_result||"Recorded",x.goal==="met"?"good":"")+'</div></div>').join("")||empty("No targeted requests in the selected period."))}catch(err){el("outcomes-content").innerHTML=empty(err.message)}}
async function loadStats(){try{const d=await get("stats",{window:el("stats-window").value});el("stats-content").innerHTML=asOf(d)+((d.models||[]).slice(0,30).map(x=>'<div class="item"><div class="item-head"><div class="item-title">'+h(x.model)+'</div>'+badge(x.health||"recorded",x.health==="paused"?"bad":"")+(x.kept_hot?badge("kept hot",""):"")+'</div><div class="meta">'+h(x.delivered)+' PRs delivered / '+h(x.runs)+' runs · '+h(x.failed)+' failed'+(x.top_failure?" · "+h(x.top_failure):"")+'</div></div>').join("")||empty("No model reliability data published."))}catch(err){el("stats-content").innerHTML=empty(err.message)}}
async function inspectPr(which){const number=el("pr-number").value;if(!/^[1-9][0-9]*$/.test(number)){notice("Enter a valid PR number.",true);return}el("pr-inspector").innerHTML=empty("Inspecting PR #"+number+"…");try{const d=await get(which,{target:"pr:"+number});el("pr-inspector").innerHTML=sourceNote(d)+(which==="checks"?renderChecks(ok(d)):raw(d))}catch(err){el("pr-inspector").innerHTML=empty(err.message)}}
const ok=d=>{if(d.exit_code!==undefined&&d.exit_code!==0)throw Error(d.error||d.reason||"Rotisserie returned "+(d.result||"an error"));return d};
const sourceNote=d=>asOf(d)+(d&&d._source?'<div class="hint">Live result from '+link(d._source.run_url,"GitHub Actions run")+' · observed '+h(fmtDate(d._source.observed_at))+'</div>':"");
const short=sha=>String(sha||"").slice(0,12);
const runBadge=v=>badge(v,/^(success|reserved|released|already-held|completed)$/.test(v)?"good":/^(failure|conflict|rejected|unavailable)$/.test(v)?"bad":"");
function renderChecks(d){
 if(!Array.isArray(d.failing))return raw(d);
 const where=d.branch?"Branch "+d.branch+(d.head?" at "+short(d.head):""):"PR #"+d.pr+" current head";
 if(!d.failing.length)return '<div class="item"><div class="item-head"><div class="item-title">'+h(where)+'</div>'+badge("No failing checks","good")+'</div></div>';
 return '<div class="hint">'+h(where)+': '+h(d.failing.length)+' failing check'+(d.failing.length===1?"":"s")+'</div>'+d.failing.map(c=>'<div class="item"><div class="item-head"><div><div class="item-title">'+h(c.name)+'</div><div class="meta">'+h(c.kind)+' · '+h(c.summary)+'</div>'+(Array.isArray(c.files)&&c.files.length?'<div class="meta">'+h(c.files.join(", "))+'</div>':"")+'</div>'+badge("Failing","bad")+'</div><div class="item-actions">'+(c.url?link(c.url,"Log"):"")+'</div></div>').join("");
}
function renderFollow(d){
 if(!d.target)return raw(d);
 const rows=[["State",d.state],["Stage",d.stage],["Owner",d.owner],["Checks",d.checks],["Claim labels",(d.claim_labels||[]).join(", ")||"none"],["Merged",d.merged===undefined?"":d.merged?"yes":"no"],["Head",short(d.head)]];
 let html='<div class="item"><div class="item-head"><div class="item-title">'+gh(d.target)+'</div>'+badge(d.finished?"Finished: "+d.reason:"Not finished",d.finished?"good":"")+'</div>'+rows.filter(r=>r[1]!=null&&r[1]!=="").map(r=>'<div class="meta">'+h(r[0])+': '+h(r[1])+'</div>').join("")+'</div>';
 if(d.issue&&d.issue.target)html+='<div class="item"><div class="item-title">Linked issue '+gh(d.issue.target)+'</div><div class="meta">'+h(d.issue.state)+(d.issue.stage?" · "+h(d.issue.stage):"")+(d.issue.done?" · done":"")+'</div></div>';
 return html;
}
function renderRuns(d){
 if(!Array.isArray(d.runs))return raw(d);
 const rows=d.runs.map(r=>{const o=r.result&&typeof r.result==="object"?r.result:{};const verdict=String(o.status||o.result||r.conclusion||r.status||"unknown");return '<div class="item"><div class="item-head"><div><div class="item-title">'+h(fmtDate(r.created_at))+' · '+h(r.event)+' by '+h(r.actor||"?")+'</div><div class="meta">'+h(o.reason||o.error||"")+'</div></div>'+runBadge(verdict)+'</div><div class="item-actions">'+(r.url?link(r.url,"View run"):"")+'</div></div>'}).join("")||empty("No controller run for this item in the last 50 runs.");
 return rows+(d.status_comment?'<details class="plan-details"><summary>Status comment on the item</summary><pre class="raw">'+h(d.status_comment)+'</pre></details>':"");
}
function renderHistory(d){
 if(!Array.isArray(d.events))return raw(d);
 let html='<div class="hint">'+h(d.target)+' is '+h(d.state)+'.</div>'+(d.events.map(e=>'<div class="plan-row">'+h(fmtDate(e.at))+' · '+h(e.actor||"?")+': '+h(e.detail)+'</div>').join("")||empty("No recorded events."));
 const runs=Array.isArray(d.controller_runs)?d.controller_runs:[];
 if(runs.length)html+='<h3 class="spaced">Label claim controller runs</h3>'+runs.map(r=>'<div class="plan-row">'+h(fmtDate(r.created_at))+' · '+h(r.event)+' by '+h(r.actor||"?")+': '+h(r.conclusion||r.status)+' '+(r.url?link(r.url,"View run"):"")+'</div>').join("");
 for(const note of Array.isArray(d.notes)?d.notes:[])html+='<div class="info spaced">'+h(note)+'</div>';
 return html;
}
function renderEstimate(d){
 if(!d.source||!d.similar)return raw(d);
 const s=d.so_far||{},src=d.source,sim=d.similar,num=x=>Math.round(x*10)/10,hrs=x=>num(x)+" h";
 const range=(r,f)=>r?"median "+f(r.median)+", middle 80% "+f(r.low)+" to "+f(r.high)+" (from "+r.n+" merged items)":"not enough similar merged items";
 const rows=[["Attempts from",src.label],["So far",s.attempts+" attempt(s)"+(s.refusals!=null?", "+s.capacity_runs+" run(s) where no model worked, "+s.refusals+" catalog refusal(s)":"")],["Similar items",sim.count?sim.count+" ("+sim.tier+")":"too few for an estimate"]];
 if(sim.count){rows.push(["Attempts to merge",range(d.attempts,num)],["Time to merge",range(d.hours,hrs)]);if(d.unattended)rows.push(["Without a human",Math.round(d.unattended.probability*100)+"% ("+d.unattended.unattended+" of "+d.unattended.n+")"])}
 if(src.attempts==="neon")rows.push(["Store history",src.rows_read+" events over "+src.window_days+" days; "+src.items_from_neon+" item(s) from the store, "+src.items_from_github+" from GitHub"]);
 let html='<div class="item"><div class="item-head"><div class="item-title">Estimate for '+gh(d.target)+'</div>'+badge(src.attempts==="neon"?"The factory store":"GitHub",src.attempts==="neon"?"good":"")+'</div>'+rows.map(r=>'<div class="meta">'+h(r[0])+': '+h(r[1])+'</div>').join("")+(src.bridge&&src.bridge.url?'<div class="item-actions">'+link(src.bridge.url,"store history run")+'</div>':"")+'</div>';
 for(const note of Array.isArray(d.notes)?d.notes:[])html+='<div class="info spaced">'+h(note)+'</div>';
 return html;
}
async function estimateNeon(){
 const t=itemTarget(),box=el("item-content");if(!t){notice("Enter an issue or PR number.",true);return}
 box.innerHTML=empty("Starting the store history run for "+t+"…");
 try{
  const started=ok(await send({operation:"estimate_neon",target:t},"Starting the store history run").finally(()=>{el("operation-status").textContent=""}));
  const run=started.bridge&&started.bridge.run_id;
  if(!run){box.innerHTML=empty("The run was requested. This page cannot read its result here; use the local control room or the CLI with --run.");return}
  for(let i=0;i<60;i++){
   box.innerHTML=empty("Waiting for the store history run")+(started.bridge.url?'<div class="hint">'+link(started.bridge.url,"Open the GitHub run")+'</div>':"");
   await new Promise(r=>setTimeout(r,15000));
   const d=await get("estimate",{target:t,run:String(run)});
   if(d.result==="pending")continue;
   box.innerHTML=renderEstimate(ok(d));return;
  }
  box.innerHTML=empty("The store history run did not finish in 15 minutes.");
 }catch(err){box.innerHTML=empty(err.message)}
}
function itemTarget(){const number=el("item-number").value;return /^[1-9][0-9]*$/.test(number)?el("item-kind").value+":"+number:""}
async function itemRead(which){const t=itemTarget();if(!t){notice("Enter an issue or PR number.",true);return}el("item-content").innerHTML=empty("Reading "+which+" for "+t+"…");try{const d=ok(await get(which,{target:t}));el("item-content").innerHTML=sourceNote(d)+({follow:renderFollow,runs:renderRuns,history:renderHistory,estimate:renderEstimate})[which](d)}catch(err){el("item-content").innerHTML=empty(err.message)}}
function itemWrite(operation,question){const t=itemTarget();if(!t){notice("Enter an issue or PR number.",true);return}if(window.confirm(question+" "+t+"?"))act({operation,target:t},el(operation==="claim"?"item-claim":"item-finish"))}
const switchText=(off,reason)=>off?"off"+(reason?": "+reason:""):"on";
const switchAttrs=(provider,model)=>' data-provider="'+h(provider)+'"'+(model?' data-model="'+h(model)+'"':"");
const switchButton=(off,provider,model)=>off?'<button class="button small"'+switchAttrs(provider,model)+' data-switch-on="1">Turn on</button>':'<button class="button small danger"'+switchAttrs(provider,model)+' data-switch-off="1">Turn off</button>';
function switchBody(b,enabled,reason){const body={operation:b.dataset.model?"model_switch":"provider_switch",provider:b.dataset.provider,enabled,reason};if(b.dataset.model)body.model=b.dataset.model;return body}
function renderSwitches(){el("provider-switches").innerHTML=state.providers.length?'<div class="table-wrap"><table class="table"><thead><tr><th>Provider</th><th>Setup</th><th>Switch</th><th>Working</th><th>Action</th></tr></thead><tbody>'+state.providers.map(p=>'<tr><td>'+h(p.provider)+'<small>'+h(p.name)+'</small></td><td>'+h(p.setup)+'</td><td>'+badge(switchText(p.disabled,p.disabled_reason),p.disabled?"bad":"good")+'</td><td>'+h(p.working_now??"")+'</td><td>'+switchButton(p.disabled,p.provider)+'</td></tr>').join("")+'</tbody></table></div>':empty("No providers found.")}
async function loadLeases(){try{const d=ok(await get("leases"));el("leases-content").innerHTML=sourceNote(d)+'<div class="hint" style="margin-bottom:12px">Source: '+h(d.source==="neon"?"factory store":d.source==="labels"?"owner labels (the factory store was not read)":"unavailable")+(d.note?' · '+h(d.note):"")+(d.bridge&&d.bridge.url?' · '+link(d.bridge.url,"store read run"):"")+'</div>'+(d.rows||[]).map(x=>'<div class="item"><div class="item-head"><div><div class="item-title">'+h(x.target)+' · '+h(x.holder)+'</div><div class="meta">Expires '+h(x.expires_at?fmtDate(x.expires_at):"not recorded")+(x.owner_run?' · '+h(x.owner_run):"")+'</div></div>'+badge(x.active?"Active":"Expired",x.active?"good":"")+'</div></div>').join("")||empty("No lease rows found.")}catch(err){el("leases-content").innerHTML=empty(err.message)}}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const secs=v=>{const s=Math.round(Number(v)||0);return s<60?s+" s":Math.floor(s/60)+" min "+String(s%60).padStart(2,"0")+" s"};
function remember(job,kind){try{sessionStorage.setItem(JOB_KEY,JSON.stringify({job,kind}))}catch{}}
function forget(){try{sessionStorage.removeItem(JOB_KEY)}catch{}}
function recalled(){try{const v=JSON.parse(sessionStorage.getItem(JOB_KEY)||"null");return v&&typeof v.job==="string"&&typeof v.kind==="string"?v:null}catch{return null}}
function stepRows(view){const rows=[];for(const s of (view&&view.steps)||[]){const last=rows[rows.length-1];if(last&&s.phase===last.phase&&s.total!=null&&last.total!=null)rows[rows.length-1]=s;else rows.push(s)}return rows}
function renderSteps(view){
 const rows=stepRows(view),running=view.state==="running";
 return '<div class="info">'+(running?"Working for ":"Finished in ")+h(secs(view.elapsed))+(running?". The command keeps running if you leave or reload this page.":"")+'</div>'+(rows.map((s,i)=>'<div class="plan-row'+(s.error?" step-error":"")+'">'+(running&&i===rows.length-1?'<span class="step-live"></span>':"")+h(s.step)+(s.url?" · "+link(s.url,"GitHub run"):"")+'<span class="meta"> · '+h(secs(s.at))+'</span></div>').join("")||'<div class="plan-row"><span class="step-live"></span>Starting the factory command…</div>');
}
function stepLog(){const v=state.lastJob;return v&&(v.steps||[]).length?'<details class="plan-details"><summary>'+h(stepRows(v).length)+' steps in '+h(secs(v.elapsed))+'</summary>'+renderSteps(v)+'</details>':""}
async function send(body,label){const data=await request("/api/command",body);return data.job?track(data,body.operation,label):data}
async function track(view,kind,label){
 const box=/^unstick/.test(kind)?el("unstick-plan"):null,job=view.job;
 let misses=0;
 if(box)state.panel=job;
 if(kind!=="estimate_neon")remember(job,kind);
 try{
  for(;;){
   if(box&&state.panel!==job)return {superseded:true};
   state.lastJob=view;
   const rows=stepRows(view),last=rows[rows.length-1];
   el("operation-status").textContent=label+"… "+secs(view.elapsed)+(last?" · "+last.step:"");
   if(box)box.innerHTML=renderSteps(view);
   if(view.state!=="running")break;
   await sleep(1000);
   try{view=await request("/api/jobs/"+encodeURIComponent(view.job));misses=0}
   catch(err){if(++misses>=3||/no longer known/.test(err.message))throw err}
  }
 }finally{const saved=recalled();if(saved&&saved.job===job)forget()}
 if(view.state==="failed")throw Error((view.result&&view.result.error)||"The factory command failed.");
 return view.result||{};
}
async function resumeJob(){
 const saved=LOCAL&&recalled();if(!saved)return;
 let view;try{view=await request("/api/jobs/"+encodeURIComponent(saved.job))}catch{forget();return}
 if(saved.kind==="unstick-plan")showPlan(el("view-plan"),view);
 else act({operation:saved.kind},saved.kind==="unstick"?el("unstick"):null,view);
}
function unstickHeadline(d){
 const s=(d.started||[]).length,y=(d.synced||[]).filter(x=>x.ok).length,f=(d.failed||[]).length+(d.not_attempted||[]).length;
 if(!s&&!y&&!f)return d.error||((d.plan&&d.plan.problems)||[])[0]||"Nothing was started.";
 return "Unblock work: "+[s?s+" worker"+(s===1?"":"s")+" started":"",y?y+" pull request"+(y===1?"":"s")+" synced with main":"",f?f+" not started because of errors":""].filter(Boolean).join(", ")+".";
}
function renderUnstick(d){
 const p=d.plan||{},by=new Map((p.assignments||[]).map(a=>[a.task&&a.task.target,a]));
 const row=(title,meta,cls)=>'<div class="plan-row'+(cls?" "+cls:"")+'">'+title+(meta?'<div class="meta">'+meta+'</div>':"")+'</div>';
 const part=(t,rows)=>rows.length?'<h3 class="recap-h">'+h(t)+'</h3>'+rows.join(""):"";
 const started=(d.started||[]).map(x=>{const a=by.get(x.target)||{};return row('<strong>'+h(String((a.task&&a.task.action)||"work").replace(/-/g," "))+' '+gh(x.target)+'</strong> · '+(x.url?link(x.url,"GitHub run"):"run not found yet"),h(a.model||""))});
 const synced=(d.synced||[]).map(x=>row(h(x.text),"",x.ok?"":"step-error"));
 const errors=[...(d.failed||[]).map(x=>row('<strong>'+gh(x.target)+' not started</strong>',h(x.error),"step-error")),...(d.not_attempted||[]).map(t=>row(gh(t)+" not attempted",h(d.stopped||""),"step-error"))];
 const skipped=[...(p.no_model||[]),...(p.held||[]),...(p.needs_person||[]),...(p.waiting_on_main||[]),...(p.obsolete||[])].map(x=>row(h(x.text)));
 if(p.left_out)skipped.push(row(h(p.left_out+" more could start; the worker limit left them out.")));
 if(!(d.started||[]).length&&!(d.synced||[]).length)for(const x of p.problems||[])skipped.push(row(h(x)));
 return '<div class="info">'+h(unstickHeadline(d))+'</div>'+part("Started",started)+part("Synced with main",synced)+part("Errors",errors)+part("Not started, and why",skipped)+stepLog();
}
function renderPlan(plan){
 const p=plan||{}, assignments=p.assignments||[], syncs=p.syncs||[],held=p.held||[],problems=p.problems||[],diagnosis=p.diagnosis||[];
 const total=assignments.length+syncs.length;
 let html='<div class="info">'+total+' proposed action'+(total===1?"":"s")+(p.needed_to_reopen_intake?" · "+p.needed_to_reopen_intake+" review slots need clearing":"")+'. The plan will be recalculated on execution.</div>';
 if(assignments.length)html+='<div class="spaced">'+assignments.map(a=>'<div class="plan-row"><strong>'+h((a.task?.action||"advance").replace(/-/g," "))+' '+gh(a.task?.target||"issue:0")+'</strong> · '+h(a.task?.title||"")+'<div class="meta">'+h(a.provider||"")+" · "+h(a.model||"Auto")+(a.task?.why?" · "+h(a.task.why):"")+'</div></div>').join("")+'</div>';
 if(syncs.length)html+='<div class="spaced">'+syncs.map(x=>'<div class="plan-row"><strong>Sync '+gh("pr:"+x.pr)+'</strong><div class="meta">'+h(x.why||"Update branch against main")+'</div></div>').join("")+'</div>';
 if(!total)html+='<div class="empty">No safe immediate actions. Existing claims remain protected.</div>';
 const details=[...(p.no_model||[]).map(x=>x.text),...(p.skipped_models||[]).map(x=>"Skipped "+x),...diagnosis.map(x=>x.text),...held.map(x=>x.text),...problems.map(x=>typeof x==="string"?x:x.text||x.message||"Unclassified diagnostic")].filter(Boolean);
 if(details.length)html+='<details class="plan-details"><summary>'+details.length+' waiting conditions and other diagnostics</summary>'+details.map(x=>'<div class="plan-row">'+h(x)+'</div>').join("")+'</details>';
 return html;
}
async function showPlan(source,resumed){
 if(state.mutating){notice("Wait for the running change to finish.",true);return}
 const button=source||el("view-plan");
 button.disabled=true;button.classList.add("is-loading");
 state.lastJob=null;
 el("unstick-plan").innerHTML=empty("Calculating safe actions and checking current claims…");
 try{let d=resumed||await get("unstick",LOCAL?{background:"1"}:{});if(d.job)d=await track(d,"unstick-plan","Reading the factory");if(d.superseded)return;if(!d.plan)throw Error(d.error||"Rotisserie returned "+(d.result||"no plan"));el("unstick-plan").innerHTML=renderPlan(d.plan)+stepLog();}
 catch(err){el("unstick-plan").innerHTML=empty("Could not prepare recovery plan: "+err.message)+stepLog();}
 finally{button.disabled=false;button.classList.remove("is-loading");el("operation-status").textContent="";}
}
function target(){const kind=el("work-kind").value;const number=el("target-number").value;return kind==="next"?"":kind+":"+number}
function preview(){const kind=el("work-kind").value;el("target-field").hidden=kind==="next";el("action-field").hidden=kind!=="pr";el("take-over").disabled=kind==="next";if(kind==="next")el("take-over").checked=false;const args=["rotisserie","factory","run",el("provider").value||"PROVIDER",el("model").value||"auto"];if(kind!=="next")args.push("--target",target()||kind+":NUMBER");if(kind==="pr"&&el("action").value!=="advance")args.push("--action",el("action").value);if(el("force").checked)args.push("--force");if(el("exact").checked&&el("model").value&&el("model").value!=="auto")args.push("--exact");if(el("take-over").checked)args.push("--take-over");if(el("slot").value)args.push("--slot",el("slot").value);args.push("--no-wait");el("command-preview").textContent=args.join(" ");el("force-hint").textContent=el("force").checked?"Override bypasses scheduler pacing and capacity limits, but not another active claim. Takeover is a separate explicit action.":"Normal scheduling respects factory limits and existing leases. Overrides never silently steal another worker's claim."}
async function refresh(fresh=false){
 const page=state.page,lazyLoaded=Object.keys(lazy).filter(id=>seen.has(id)).map(id=>lazy[id]);
 const loads={overview:[loadStatus,loadReport,loadDoctor,loadQueue,...lazyLoaded],queue:[loadQueue,loadIncidents],workers:[loadStatus,loadLeases],outcomes:[loadOutcomes,loadStats],models:[loadCatalog],settings:[loadSettings],health:[loadDoctor]}[page]||[];
 el("refresh").disabled=true;el("refresh").classList.add("is-loading");
 forceFresh=fresh===true;
 try{
  const running=loads.map(load=>load());
  forceFresh=false;
  await Promise.all(running);
 }finally{el("refresh").disabled=false;el("refresh").classList.remove("is-loading")}
}
function go(page){if(!titles[page])return;state.page=page;document.querySelectorAll(".view").forEach(x=>x.classList.toggle("active",x.id===page));document.querySelectorAll(".nav").forEach(x=>x.classList.toggle("active",x.dataset.view===page));setText("page-title",titles[page][0]);setText("page-sub",titles[page][1]);window.scrollTo({top:0,behavior:"smooth"});refresh()}
el("repository").textContent=REPO;el("all-merges").href="https://github.com/"+REPO+"/pulls?q=is%3Apr+is%3Amerged";el("nav").addEventListener("click",e=>{const b=e.target.closest("[data-view]");if(b)go(b.dataset.view)});
document.body.addEventListener("click",e=>{const b=e.target.closest("button");if(!b)return;
 if(b.dataset.goto)go(b.dataset.goto);
 if(b.dataset.cancel)act({operation:"cancel",target:b.dataset.cancel},b);
 if(b.dataset.explain)explain(b.dataset.explain);
 if(b.dataset.prioritize){
  const item=b.closest(".item");if(!item)return;
  const existing=item.querySelector(".priority-inline");if(existing){existing.remove();return;}
  item.insertAdjacentHTML("beforeend",'<div class="priority-inline"><label for="priority-'+h(b.dataset.prioritize)+'">Priority</label><select id="priority-'+h(b.dataset.prioritize)+'"><option value="critical">Critical</option><option value="high" selected>High</option><option value="medium">Medium</option><option value="low">Low</option><option value="none">None</option></select><button class="button small" data-priority-apply="'+h(b.dataset.prioritize)+'">Save priority</button></div>');
 }
 if(b.dataset.priorityApply){
  const select=el("priority-"+b.dataset.priorityApply);
  if(select)act({operation:"prioritize",target:"issue:"+b.dataset.priorityApply,level:select.value},b);
 }
 if(b.dataset.incidentStop&&window.confirm("End the incident on #"+b.dataset.incidentStop+"? Normal intake resumes."))act({operation:"incident_stop",issue:"issue:"+b.dataset.incidentStop},b);
 if(b.dataset.prInspect)inspectPr(b.dataset.prInspect);
 if(b.dataset.itemRead)itemRead(b.dataset.itemRead);
 if(b.dataset.switchOn)act(switchBody(b,true,""),b);
 if(b.dataset.switchOff){
  const box=b.parentElement,open=box.querySelector(".switch-form");if(open){open.remove();return;}
  box.insertAdjacentHTML("beforeend",'<div class="priority-inline switch-form"><input aria-label="Reason for turning off" maxlength="180" placeholder="Reason (required)"><button class="button small danger"'+switchAttrs(b.dataset.provider,b.dataset.model)+' data-switch-apply="1">Confirm turn off</button></div>');
 }
 if(b.dataset.switchApply){
  const reason=b.parentElement.querySelector("input").value.trim();
  if(!reason){notice("Give a reason before turning it off.",true);return}
  if(window.confirm("Turn off "+(b.dataset.model?"model "+b.dataset.model:"provider "+b.dataset.provider)+"? The next dispatcher tick skips it. Running work continues.\nReason: "+reason))act(switchBody(b,false,reason),b);
 }
 if(b.dataset.choose){go("overview");el("work-kind").value="issue";el("target-number").value=b.dataset.choose;preview();}
 if(b.dataset.useModel){const selected=b.dataset.useModel;el("provider").value=el("catalog-provider").value;go("overview");loadModelOptions().then(()=>{if([...el("model").options].some(o=>o.value===selected&&!o.disabled))el("model").value=selected;preview()});}
 if(b.dataset.saveSetting){const x=document.querySelector('[data-setting-name="'+b.dataset.saveSetting+'"]');if(x)act({operation:"settings",name:b.dataset.saveSetting,value:x.value},b)}
});
el("launcher").addEventListener("submit",e=>{e.preventDefault();if(state.mutating||el("model").disabled)return;const kind=el("work-kind").value;const number=el("target-number").value;if(kind!=="next"&&(!/^[1-9][0-9]*$/.test(number))){notice("Enter a valid issue or PR number.",true);return}const data={operation:"run",provider:el("provider").value,model:el("model").value||"auto",target:kind==="next"?"":target(),action:kind==="pr"?el("action").value:"advance",force:el("force").checked,exact:el("exact").checked&&!!el("model").value&&el("model").value!=="auto",take_over:el("take-over").checked};if(el("slot").value)data.slot=Number(el("slot").value);const refused=refusedModel(data);if(refused){notice(refused,true);return}act(data,el("launch"))});
["provider","model","work-kind","target-number","action","slot","force","exact","take-over"].forEach(id=>el(id).addEventListener("change",()=>{preview();if(id==="provider")loadModelOptions()}));
el("model").addEventListener("input",preview);el("target-number").addEventListener("input",preview);
const fresh=fn=>()=>{forceFresh=true;try{return fn()}finally{forceFresh=false}};
el("refresh").onclick=()=>refresh(true);el("recap-refresh").onclick=fresh(loadRecap);el("turning-refresh").onclick=fresh(loadTurning);el("heal-refresh").onclick=fresh(loadHeal);el("queue-refresh").onclick=fresh(loadQueue);el("workers-refresh").onclick=fresh(loadStatus);el("leases-refresh").onclick=fresh(loadLeases);el("outcomes-refresh").onclick=fresh(loadOutcomes);el("models-refresh").onclick=fresh(()=>{modelCache.clear();drop(["providers","models"]);return loadProviders()});el("doctor-refresh").onclick=fresh(loadDoctor);el("queue-filter").oninput=renderQueue;el("queue-state").onchange=renderQueue;el("stats-window").onchange=loadStats;el("catalog-provider").onchange=loadCatalog;el("model-search").oninput=renderCatalog;el("view-plan").onclick=()=>showPlan(el("view-plan"));
el("pr-select").onclick=()=>{const number=el("pr-number").value;if(!/^[1-9][0-9]*$/.test(number)){notice("Enter a valid PR number.",true);return}go("overview");el("work-kind").value="pr";el("target-number").value=number;preview()};
el("pr-sync").onclick=()=>{const number=el("pr-number").value;if(!/^[1-9][0-9]*$/.test(number)){notice("Enter a valid PR number.",true);return}act({operation:"sync",target:"pr:"+number},el("pr-sync"))};
el("main-checks").onclick=async()=>{el("pr-inspector").innerHTML=empty("Reading checks on main…");try{el("pr-inspector").innerHTML=renderChecks(ok(await get("checks",{target:"main"})))}catch(err){el("pr-inspector").innerHTML=empty(err.message)}};
el("incidents-refresh").onclick=fresh(loadIncidents);
el("pr-allow-self-review").onclick=()=>{const number=el("pr-number").value;if(!/^[1-9][0-9]*$/.test(number)){notice("Enter a valid PR number.",true);return}if(window.confirm("Let the model that wrote PR #"+number+"'s current head review it?\nThis adds factory:allow-self-review as you, for this head only. Every other merge gate still applies."))act({operation:"allow_self_review",target:"pr:"+number},el("pr-allow-self-review"))};
el("pr-close").onclick=()=>{const number=el("pr-number").value;if(!/^[1-9][0-9]*$/.test(number)){notice("Enter a valid PR number.",true);return}const reason=(window.prompt("Why close PR #"+number+"? The reason is posted as the closing comment, and the issue it delivers closes as not planned.")||"").trim();if(!reason){notice("Give a reason before closing.",true);return}if(window.confirm("Close PR #"+number+" and the issue it delivers?\nReason: "+reason))act({operation:"close",target:"pr:"+number,reason},el("pr-close"))};
el("failures-read").onclick=async()=>{el("outcomes-content").innerHTML=empty("Reading failed worker runs from the last 10 hours…");try{const d=ok(await get("failures",{since:"10h"}));el("outcomes-content").innerHTML=sourceNote(d)+raw(d)}catch(err){el("outcomes-content").innerHTML=empty(err.message)}};
el("backoffs-read").onclick=async()=>{el("limits-content").innerHTML=empty("Reading backoffs through the trusted workflow…");try{const d=ok(await get("backoffs"));el("limits-content").innerHTML=sourceNote(d)+raw(d)}catch(err){el("limits-content").innerHTML=empty(err.message)}};
const usageRows=d=>{const m=d.metrics||{};return Object.entries({rows_read:"Rows read",rows_written:"Rows written",storage_bytes:"Storage (bytes)",bytes_synced:"Sync (bytes)"}).filter(([k])=>m[k]).map(([k,n])=>'<tr><td>'+h(n)+'</td><td>'+h(Number(m[k].organization).toLocaleString())+' of '+h(Number(m[k].allowance).toLocaleString())+'</td><td>'+h(m[k].share_used==null?"?":(m[k].share_used*100).toFixed(1)+"%")+'</td><td>'+h(Number(m[k].projected_organization).toLocaleString())+'</td></tr>').join("")};
const renderUsage=d=>'<div class="item"><div class="item-head"><div class="item-title">Turso this month'+(d.label?' · '+h(d.label):'')+'</div>'+badge(d.status||"unknown",d.status==="critical"||d.status==="error"?"bad":d.status==="ok"?"good":"")+'</div><div class="meta">'+(d.resets_at?h(d.days_left)+' days left · resets '+h(fmtDate(d.resets_at))+' (exact hour unverified) · allowances: '+h(d.allowance_source)+'<br>':'')+h((d.reasons||[]).join("; "))+'<br>Source: '+h(d.source||"published")+' · checked '+h(d.checked_at?fmtDate(d.checked_at):"never")+'</div>'+(usageRows(d)?'<div class="table-wrap"><table class="table"><thead><tr><th>Metric</th><th>Used of allowance</th><th>Share</th><th>Projected month end</th></tr></thead><tbody>'+usageRows(d)+'</tbody></table></div>':'')+'</div>';
el("db-usage-read").onclick=async()=>{el("limits-content").innerHTML=empty("Reading the published Turso usage…");try{const d=ok(await get("db-usage"));el("limits-content").innerHTML=sourceNote(d)+renderUsage(d)}catch(err){el("limits-content").innerHTML=empty(err.message)}};
const crashLink=u=>/^https:\/\/github\.com\//.test(u||"")?' · <a href="'+h(u)+'" target="_blank" rel="noopener">last run</a>':"";
const renderCrashes=d=>(d.groups||[]).map(g=>'<div class="item"><div class="item-head"><div><div class="item-title">'+(g.fatal?"💥 Crash: ":"🩹 Item failure: ")+h(g.exception_type)+' in '+h(g.script)+':'+h(g.line)+' '+h(g.function)+'</div><div class="meta">'+h(g.count)+' times since '+h(fmtDate(g.first_at))+' · last '+h(fmtDate(g.last_at))+' · '+h((g.workflows||[]).join(", "))+' · '+h(g.message||"")+crashLink(g.last_run_url)+'</div></div><button class="button small" data-crash-trace="'+h(g.fingerprint)+'">Traceback</button></div></div>').join("")||empty("No factory crashes in the last 6 hours.");
el("crashes-read").onclick=async()=>{el("limits-content").innerHTML=empty("Reading the published factory crashes…");try{const d=ok(await get("crashes"));el("limits-content").innerHTML=sourceNote(d)+renderCrashes(d)}catch(err){el("limits-content").innerHTML=empty(err.message)}};
document.addEventListener("click",async e=>{const b=e.target.closest&&e.target.closest("[data-crash-trace]");if(!b)return;el("limits-content").innerHTML=empty("Reading the newest traceback through the trusted workflow…");try{const d=ok(await get("crash-trace",{fingerprint:b.dataset.crashTrace}));el("limits-content").innerHTML=sourceNote(d)+(d.found?'<pre class="raw">'+h(d.traceback||"")+'</pre>':empty("That crash is no longer stored."))}catch(err){el("limits-content").innerHTML=empty(err.message)}});
el("backoffs-reset").onclick=()=>{if(window.confirm("Clear quota and throttle backoffs and cap other streaks at one day? Real-failure streaks stay."))act({operation:"backoffs_reset"},el("backoffs-reset"))};
el("store-selftest").onclick=()=>act({operation:"store_selftest"},el("store-selftest"));
el("store-export").onclick=()=>act({operation:"store_export"},el("store-export"));
el("store-migrate-dry").onclick=()=>act({operation:"store_migrate",dry_run:true},el("store-migrate-dry"));
el("store-migrate").onclick=()=>{if(window.confirm("Copy the Neon factory state into the factory store now? Read a dry run first. Rows already in the store are never overwritten, and a completed migration refuses to run again."))act({operation:"store_migrate",dry_run:false},el("store-migrate"))};
el("store-read").onclick=async()=>{const run=el("store-run").value;if(!/^[1-9][0-9]*$/.test(run)){notice("Enter the store run ID.",true);return}const mode=el("store-mode").value;el("limits-content").innerHTML=empty("Reading the store "+mode+" run…");try{const d=ok(await get("store",{mode,run}));el("limits-content").innerHTML=sourceNote(d)+raw(d)}catch(err){el("limits-content").innerHTML=empty(err.message)}};
const renderSchema=d=>{const objs=d.objects||[],missing=objs.filter(o=>!o.present),last=d.last_upgrade;return '<div class="item"><div class="item-head"><div class="item-title">Store schema · stored version '+h(d.stored_version??"none")+' of '+h(d.code_version)+'</div>'+badge(d.status||"unknown",d.status==="ok"?"good":"bad")+'</div>'+(d.reasons||[]).map(r=>'<div class="meta">'+h(r)+'</div>').join("")+'<div class="meta">Last upgrade: '+(last?h(last.result)+' '+h(fmtDate(last.at))+' ('+h(last.source)+(last.workflow?', '+h(last.workflow)+' run '+h(last.run_id):'')+')'+(last.error?' · '+h(last.error):''):'none recorded')+'</div><div class="meta">'+h(objs.length-missing.length)+' of '+h(objs.length)+' tables and indexes present'+(missing.length?'; missing '+h(missing.map(o=>o.name).join(", ")):'')+'</div></div>'};
el("schema-read").onclick=async()=>{el("limits-content").innerHTML=empty("Reading the store schema through the trusted workflow…");try{const d=ok(await get("schema"));el("limits-content").innerHTML=sourceNote(d)+renderSchema(d)+raw(d)}catch(err){el("limits-content").innerHTML=empty(err.message)}};
el("schema-apply").onclick=()=>{if(window.confirm("Run every store schema statement now? Each one is IF NOT EXISTS, so only missing tables and indexes are created."))act({operation:"schema_apply"},el("schema-apply"))};
el("limits-read").onclick=async()=>{el("limits-content").innerHTML=empty("Reading GitHub API limits…");try{const d=ok(await get("limits"));el("limits-content").innerHTML=sourceNote(d)+raw(d)}catch(err){el("limits-content").innerHTML=empty(err.message)}};
el("incident-start").onclick=()=>{const number=el("incident-number").value;if(!/^[1-9][0-9]*$/.test(number)){notice("Enter a valid issue number.",true);return}const body={operation:"incident_start",issue:"issue:"+number,reason:el("incident-reason").value.trim()};if(el("incident-attempts").value)body.attempts=el("incident-attempts").value;if(window.confirm("Start an incident on #"+number+"? Free workers go to it first and other new intake pauses."))act(body,el("incident-start"))};
el("item-claim").onclick=()=>itemWrite("claim","Claim");
el("item-estimate-neon").onclick=estimateNeon;
el("item-finish").onclick=()=>itemWrite("finish","End your claim on");
el("item-assign").onclick=()=>{const t=itemTarget(),agent=el("item-agent").value.trim();if(!t||!agent){notice("Enter an item number and an agent name.",true);return}if(window.confirm("Claim "+t+" as you and hand it to agent:"+agent+" with the task for its current stage?"))act({operation:"assign",target:t,agent},el("item-assign"))};
el("item-assigned").onclick=async()=>{el("item-content").innerHTML=empty("Reading agent holds…");try{const d=ok(await get("assigned"));el("item-content").innerHTML=sourceNote(d)+((d.assigned||[]).map(x=>'<div class="item"><div class="item-title">'+h(x.target)+' · agent:'+h(x.agent)+' · '+h(x.held_for)+'</div><div class="meta">'+h(x.title)+'</div></div>').join("")||empty("No item is held by an outside agent."))}catch(err){el("item-content").innerHTML=empty(err.message)}};
el("health-reset").onclick=()=>{const model=el("health-model").value.trim(),source=el("health-source").value.trim();if(!model){notice("Enter the exact model ID.",true);return}if(window.confirm("Clear only "+model+"'s health and backoff rows"+(source?" from "+source:"")+"?"))act({operation:"health_reset",model,...(source?{source}:{})},el("health-reset"))};
el("outcome-read").onclick=async()=>{const run=el("outcome-run").value.trim();if(!/^[1-9][0-9]*$/.test(run)){notice("Enter the worker run ID.",true);return}el("limits-content").innerHTML=empty("Reading what run "+run+" did…");try{const d=ok(await get("outcome",{run}));el("limits-content").innerHTML=sourceNote(d)+raw(d)}catch(err){el("limits-content").innerHTML=empty(err.message)}};
el("labels-dry-run").onclick=()=>act({operation:"labels",mode:"dry-run"},el("labels-dry-run"));
el("labels-repair").onclick=()=>{if(window.confirm("Start a label repair run? It changes labels on the repository."))act({operation:"labels",mode:"repair"},el("labels-repair"))};
el("unstick").onclick=()=>act({operation:"unstick"},el("unstick"));
el("doctor-fix").onclick=()=>act({operation:"doctor_fix"},el("doctor-fix"));
el("release-expired").onclick=()=>act({operation:"release_expired"},el("release-expired"));
const renderReady=d=>sourceNote(d)+(d.notes||[]).map(n=>'<div class="hint">'+h(n)+'</div>').join("")+(d.cycles||[]).map(c=>'<div class="item"><div class="item-title">Dependency cycle: '+h(c.concat(c.slice(0,1)).map(n=>"#"+n).join(" → "))+'</div><div class="meta">Remove one link to release it.</div></div>').join("")+((d.ready||[]).map(x=>'<div class="item"><div class="item-title">'+h(x.rank)+". "+h(x.target)+" · "+h(x.stage||"")+" → "+h(x.action)+'</div><div class="meta">'+h(x.title||"")+'</div></div>').join("")||empty("Nothing is claimable right now."));
el("queue-ready").onclick=async()=>{el("queue-content").innerHTML=empty("Reading the ready list…");try{el("queue-content").innerHTML=renderReady(ok(await get("ready")))}catch(err){el("queue-content").innerHTML=empty(err.message)}};
el("queue-explain").onclick=()=>{const number=el("queue-explain-number").value;if(/^[1-9][0-9]*$/.test(number))explain(number);else notice("Enter an issue number to explain.",true)};
setInterval(()=>{if(!document.hidden&&!state.mutating&&(state.page==="overview"||state.page==="workers"))loadStatus()},30000);
setInterval(()=>{for(const x of document.querySelectorAll(".asof"))x.textContent=ago(Number(x.dataset.at))},15000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden&&!state.mutating)refresh()});
const watcher=window.IntersectionObserver?new IntersectionObserver(rows=>{for(const r of rows)if(r.isIntersecting&&!seen.has(r.target.id)){seen.add(r.target.id);lazy[r.target.id]()}}):null;
for(const id in lazy)if(watcher)watcher.observe(el(id));else{seen.add(id);lazy[id]()}
resumeJob();loadProviders();refresh();preview();
