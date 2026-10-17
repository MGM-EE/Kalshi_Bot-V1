"use strict";
const $ = (selector) => document.querySelector(selector);
const state = {offset: 0, limit: 25, filters: new URLSearchParams(), fetching: false};
const number = (value) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
const money = (value) => number(value) === null ? "—" : new Intl.NumberFormat("en-US", {style:"currency",currency:"USD",minimumFractionDigits:2,maximumFractionDigits:2}).format(Number(value));
const price = (value) => number(value) === null ? "—" : new Intl.NumberFormat("en-US", {maximumFractionDigits:4}).format(Number(value));
const cents = (value) => number(value) === null ? "—" : `${(Number(value)*100).toFixed(1)}¢`;
const age = (value) => number(value) === null ? "missing" : Number(value) < 1 ? "<1s" : `${Math.round(value)}s`;
const title = (value) => String(value ?? "").replaceAll("_", " ");
function el(tag, className, text) {const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=String(text);return node;}
function append(parent, ...nodes) {nodes.forEach(node => parent.append(node));return parent;}
function emptyRow(columns, text) {const row=el("tr");const cell=el("td","empty",text);cell.colSpan=columns;return append(row,cell);}
async function get(path) {const response=await fetch(path,{cache:"no-store"});if(!response.ok){let detail=`Request failed (${response.status})`;try{const body=await response.json();detail=typeof body.detail ==="string"?body.detail:detail;}catch{}throw new Error(detail);}return response.json();}
function svg(tag, attributes) {const node=document.createElementNS("http://www.w3.org/2000/svg",tag);Object.entries(attributes).forEach(([key,value])=>node.setAttribute(key,String(value)));return node;}
function chart(points, series, horizontal, markers=[]) {
  const wrap=el("div","chart");
  const valid=points.filter(point=>number(point.at)!==null && series.some(s=>number(point[s.key])!==null));
  if(valid.length<2){wrap.append(el("div","missing","Insufficient observations · no interpolation"));return wrap;}
  const width=360,height=100,left=2,right=358,top=9,bottom=88;
  const all=valid.flatMap(point=>series.map(s=>number(point[s.key]))).filter(v=>v!==null);
  if(number(horizontal)!==null)all.push(Number(horizontal));
  let low=Math.min(...all),high=Math.max(...all);const pad=(high-low)*.12||Math.max(high*.0001,.001);low-=pad;high+=pad;
  const first=Number(valid[0].at),last=Number(valid[valid.length-1].at);
  const x=(value)=>left+(Number(value)-first)/(last-first||1)*(right-left);
  const y=(value)=>bottom-(Number(value)-low)/(high-low)*(bottom-top);
  const canvas=svg("svg",{viewBox:`0 0 ${width} ${height}`,role:"img","aria-label":"Received observations over time"});
  for(const v of [.15,.5,.85])canvas.append(svg("line",{x1:left,x2:right,y1:height*v,y2:height*v,class:"grid"}));
  if(number(horizontal)!==null)canvas.append(svg("line",{x1:left,x2:right,y1:y(horizontal),y2:y(horizontal),stroke:"#c3ad7e","stroke-dasharray":"4 4","stroke-width":1}));
  for(const s of series){let segment=[];const flush=()=>{if(segment.length>1)canvas.append(svg("polyline",{points:segment.join(" "),fill:"none",stroke:s.color,"stroke-width":1.7,"stroke-linejoin":"round"}));segment=[];};let previous=null;for(const point of valid){if(number(point[s.key])===null || (previous!==null && Number(point.at)-previous>5)){flush();}if(number(point[s.key])!==null)segment.push(`${x(point.at)},${y(point[s.key])}`);previous=Number(point.at);}flush();}
  for(const marker of markers){if(number(marker.created_at)!==null && marker.created_at>=first && marker.created_at<=last && number(marker.price)!==null){const plottedPrice=marker.side==="no"?1-Number(marker.price):Number(marker.price);canvas.append(svg("circle",{cx:x(marker.created_at),cy:y(plottedPrice),r:3,fill:marker.kind==="entry"?"#f2c778":"#eee",stroke:"#111920","stroke-width":1}));}}
  const label=svg("text",{x:right,y:99,"text-anchor":"end",fill:"#627782","font-size":8});label.textContent=`${Math.round(last-first)}s window`;canvas.append(label);wrap.append(canvas);return wrap;
}
function metric(label,value,signed=false) {const card=el("div","metric");append(card,el("div","metric-label",label),el("div",`metric-value ${signed&&number(value)!==null?(Number(value)<0?"negative":"positive"):""}`,signed||label.includes("cash")||label.includes("equity")?money(value):value??"—"));return card;}
function drawSummary(summary) {
  const target=$("#metrics");target.replaceChildren();
  [["Paper cash",summary?.cash],["Net bid equity",summary?.equity],["Realized P&L",summary?.realized_pnl,true],["Net P&L / marks",summary?.net_pnl,true],["Reserved cash",summary?.cash_reserved],["Open positions",summary?.open_positions],["Awaiting settlement",summary?.pending_settlement]].forEach(args=>target.append(metric(...args)));
  $("#valuation").textContent=summary?`${summary.valuation_policy} ${summary.unpriced_quantity} unpriced contracts. Available cash ${money(summary.available_cash)} · fees paid ${money(summary.fees_paid)} · worst-case open loss ${money(summary.worst_case_open_loss)}.`:"Start the paper engine to initialize the portfolio. No balances are inferred.";
}
function drawAsset(card) {
  const node=el("article","asset-card");
  const head=el("div","asset-top");const label=append(el("div","asset-title"),el("span","coin",card.asset==="BTC"?"₿":card.asset==="ETH"?"Ξ":"◎"),el("span",null,card.asset));
  append(head,label,el("span",`badge${card.eligible?" active":""}`,card.eligible?"ELIGIBLE":"COLLECT / WAIT"));
  append(node,head,el("div","ticker",card.ticker||"No verified market discovered"));
  const prices=el("div","price-row");append(prices,el("div","price",price(card.reference?.price)),el("div","strike",`Strike ${price(card.market?.strike)}`));node.append(prices);
  const ttl=number(card.seconds_to_cutoff);const settled=card.settlement;node.append(el("div","sub-label",settled?`${settled.dataset==="demo"?"Demo":"Official"} settlement complete · result ${String(settled.result??"unavailable").toUpperCase()}`:ttl===null?"Cutoff unavailable":ttl>0?`${Math.floor(ttl/60)}m ${Math.floor(ttl%60)}s to trading cutoff`:`Trading cutoff passed · awaiting official outcome`));
  append(node,append(el("div","chart-title"),el("span",null,"CF REFERENCE"),el("span",null,"— STRIKE")),chart(card.reference_history,[{key:"price",color:"#98f5b1"}],card.market?.strike));
  const q=card.quotes||{};const quotes=el("div","quote-row");[["YES BID",q.yes_bid],["YES ASK",q.yes_ask],["NO BID",q.no_bid],["NO ASK",q.no_ask]].forEach(([label,value])=>quotes.append(append(el("div"),el("span",null,label),el("strong",null,cents(value)))));node.append(quotes);
  append(node,append(el("div","chart-title"),el("span",null,"YES BID / ASK"),el("span",null,"● OWN FILLS")),chart(card.book_history,[{key:"yes_bid",color:"#82bfff"},{key:"yes_ask",color:"#bba2ed"}],null,card.fills));
  const reasons=el("div","reason-list");(card.reasons?.length?card.reasons:[card.eligible?"all_entry_gates_passed":"no_decision_recorded"]).forEach(reason=>reasons.append(el("span","reason",title(reason))));append(node,el("div","eligibility","Every recorded eligibility gate"),reasons);
  const fresh=el("div","freshness");append(fresh,el("span",card.reference_stale?"stale":"",`Reference ${age(card.reference_age_seconds)}${card.reference_stale?" · stale / missing":""}`),el("span",card.book_stale?"stale":"",`Book ${age(card.book_age_seconds)}${card.book_stale?" · verify":""}`));node.append(fresh);
  const window=el("div","settlement-progress");window.append(el("span",null,"Final-minute observation window · distinct from strike crossing"));const bar=el("div","progress");const fill=el("span");fill.style.width=`${Math.max(0,Math.min(100,Number(card.settlement_window_progress||0)*100))}%`;append(window,append(bar,fill));node.append(window);return node;
}
function drawPositions(positions) {
  const target=$("#positions");target.replaceChildren();const open=positions.filter(p=>p.remaining_quantity>0);
  if(!open.length){target.append(emptyRow(6,"No open paper positions"));return;}
  for(const p of open){const row=el("tr");[p.market_ticker,p.side==="yes"?"UP / YES":"DOWN / NO",p.remaining_quantity,money(p.remaining_cost),money(p.liquidation_value),title(p.valuation)].forEach(value=>row.append(el("td",null,value)));target.append(row);}
}
function drawHealth(cards, events) {
  const target=$("#feed-health");target.replaceChildren();
  for(const card of cards){const row=el("div","health-row");append(row,el("span",null,`${card.asset} reference / order book`),el("span",card.reference_stale||card.book_stale?"stale":"positive",card.reference_stale||card.book_stale?"WAIT / CHECK FEED":"OBSERVATIONS FRESH"));target.append(row);}
  for(const event of events.slice(-6).reverse()){const d=event.details?.details||event.details||{};const row=el("div","health-row");append(row,append(el("div"),el("span",null,d.component||event.type),el("small",null,d.reason||d.status||"Health event recorded")),el("span","muted",`${age(event.age_seconds)} ago`));target.append(row);}
  if(!target.children.length)target.append(el("p","muted","No feed health observations recorded."));
}
async function refreshState() {
  if(state.fetching)return;state.fetching=true;
  try {const data=await get("/api/state");const ready=data.database.status==="ready";$("#connection").textContent=ready?"Read-only / connected":"Waiting for engine";$("#connection").className=`status${ready?"":" warn"}`;
    $("#data-label").textContent=data.data_mode==="demo"?"SYNTHETIC DEMO DATA":data.data_mode==="mixed_demo_and_real"?"MIXED SYNTHETIC & COLLECTED DATA":data.data_mode==="replay"?"RECORDED REPLAY DATA":data.data_mode==="live_collected"?"LIVE COLLECTED DATA / PAPER": "NO DATA SOURCE VERIFIED";
    $("#last-update").textContent=`Updated ${new Date(data.generated_at*1000).toLocaleTimeString()}`;
    $("#notice").hidden=ready&&!["demo","mixed_demo_and_real"].includes(data.data_mode);$("#notice").textContent=ready?(data.data_mode==="mixed_demo_and_real"?"This database includes synthetic and collected data. Combined results cannot be used as market performance.":"Synthetic demonstration data. These trades and outcomes are generated examples, not market performance."):data.database.detail;
    drawSummary(data.summary);const assets=$("#assets");assets.replaceChildren();data.assets.forEach(card=>assets.append(drawAsset(card)));if(!data.assets.length)for(const asset of ["BTC","ETH","SOL"])assets.append(drawAsset({asset,market:{},reference_history:[],book_history:[],reasons:["database_uninitialized"],reference_stale:true,book_stale:true}));drawPositions(data.positions);drawHealth(data.assets,data.feed_health);
  }catch(error){$("#connection").textContent="Connection lost";$("#connection").className="status warn";$("#notice").hidden=false;$("#notice").textContent=`Dashboard data unavailable: ${error.message}. Displayed observations may be stale.`;}finally{state.fetching=false;}
}
async function refreshTrades() {
  const params=new URLSearchParams(state.filters);params.set("limit",state.limit);params.set("offset",state.offset);
  $("#csv-download").href=`/api/export.csv?${state.filters}`;$("#json-download").href=`/api/export.json?${state.filters}`;
  try{const data=await get(`/api/trades?${params}`);$("#trade-error").textContent="";const target=$("#trades");target.replaceChildren();for(const p of data.items){const row=el("tr");[new Date(p.opened_at*1000).toLocaleString(),p.market_ticker,p.side==="yes"?"UP / YES":"DOWN / NO",p.quantity,money(p.entry_cost),money(p.realized_pnl),p.remaining_quantity,p.settlement_status].forEach((value,i)=>row.append(el("td",i===5?(Number(p.realized_pnl)<0?"negative":"positive"):"",value)));target.append(row);}if(!data.items.length)target.append(emptyRow(8,"No trades match these filters"));$("#trade-count").textContent=data.total?`${state.offset+1}–${Math.min(state.offset+data.limit,data.total)} of ${data.total} trades`:`0 trades`;$("#prev").disabled=state.offset===0;$("#next").disabled=state.offset+state.limit>=data.total;
  }catch(error){$("#trade-error").textContent=error.message;$("#trades").replaceChildren(emptyRow(8,"Trade data unavailable"));$("#prev").disabled=true;$("#next").disabled=true;}
}
function renderResearch(target,data) {
  target.replaceChildren();
  if(!data||Object.keys(data).length===0){target.textContent="No observations yet.";return;}
  for(const [key,value] of Object.entries(data)){
    if(value!==null&&typeof value==="object"){const details=el("details");append(details,el("summary",null,title(key)),el("pre",null,JSON.stringify(value,null,2)));target.append(details);}
    else target.append(append(el("div","research-kv"),el("span",null,title(key)),el("strong",null,value??"missing")));
  }
}
function researchTable(headers, rows) {const wrap=el("div","table-scroll"),table=el("table"),head=el("thead"),tr=el("tr");headers.forEach(label=>tr.append(el("th",null,label)));head.append(tr);const body=el("tbody");rows.forEach(values=>{const row=el("tr");values.forEach(value=>row.append(el("td",null,value??"—")));body.append(row);});if(!rows.length)body.append(emptyRow(headers.length,"No eligible observations"));append(table,head,body);wrap.append(table);return wrap;}
function renderAnalytics(target,data) {
  const t=data.trajectories;if(!t){renderResearch(target,data);return;}target.replaceChildren();
  for(const [key,value] of [["Baseline positions",t.positions],["With observations",t.with_observations],["Unavailable",t.unavailable]])target.append(append(el("div","research-kv"),el("span",null,key),el("strong",null,value??"—")));
  target.append(researchTable(["Horizon","Eligible","Observed","Missing / censored","Mean net","P10 / P90"],Object.entries(t.fixed_elapsed_horizons||{}).map(([seconds,row])=>[`${seconds}s`,row.eligible_trades,row.fully_observed_trades,row.missing_or_censored,money(row.net_pnl?.mean),`${money(row.net_pnl?.p10)} / ${money(row.net_pnl?.p90)}`])));
  target.append(researchTable(["Distribution","Observed","Mean","Median","P10","P90"],[["MFE",t.mfe?.count,money(t.mfe?.mean),money(t.mfe?.median),money(t.mfe?.p10),money(t.mfe?.p90)],["MAE",t.mae?.count,money(t.mae?.mean),money(t.mae?.median),money(t.mae?.p10),money(t.mae?.p90)]]));
  const details=el("details");append(details,el("summary",null,"Full research report / coverage / assumptions"),el("pre",null,JSON.stringify(data,null,2)));target.append(details);
}
function renderExperiments(target,data) {
  if(!data.policies){renderResearch(target,data);return;}target.replaceChildren();
  target.append(researchTable(["Policy","Observed","Pending / censored","Mean net","Paired Δ vs 95¢"],Object.entries(data.policies).map(([name,row])=>[title(name),row.observed_outcomes,row.pending_or_censored,money(row.net_pnl?.mean),money(row.paired_difference_vs_95?.mean)])));
  const details=el("details");append(details,el("summary",null,"Full paired report / fallbacks / assumptions"),el("pre",null,JSON.stringify(data,null,2)));target.append(details);
}
async function refreshResearch() {for(const [route,id,render] of [["analytics","#analytics",renderAnalytics],["experiments","#experiments",renderExperiments]]){try{render($(id),await get(`/api/${route}`));}catch(error){$(id).textContent=error.message;}}}
$("#filters").addEventListener("submit",event=>{event.preventDefault();state.filters=new URLSearchParams();const data=new FormData(event.target);for(const [key,value] of data.entries()){if(!value)continue;state.filters.set(key,key.startsWith("started_")?String(new Date(value).getTime()/1000):value);}state.offset=0;refreshTrades();});
$("#prev").addEventListener("click",()=>{state.offset=Math.max(0,state.offset-state.limit);refreshTrades();});
$("#next").addEventListener("click",()=>{state.offset+=state.limit;refreshTrades();});
refreshState();refreshTrades();refreshResearch();setInterval(refreshState,2000);setInterval(refreshTrades,10000);setInterval(refreshResearch,15000);
