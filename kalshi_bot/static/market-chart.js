"use strict";
(() => {
  const $ = selector => document.querySelector(selector);
  const numeric = value => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;
  const fmt = value => numeric(value) === null ? "—" : (Number(value) * 100).toFixed(1) + "¢";
  const dollars = value => numeric(value) === null ? "—" : Number(value).toLocaleString(undefined, {maximumFractionDigits:4});
  const clock = value => new Date(value * 1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"});
  const make = (tag, cls, text) => {const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=String(text);return node;};
  const svg = (tag, attrs={}) => {const node=document.createElementNS("http://www.w3.org/2000/svg",tag);Object.entries(attrs).forEach(([key,value])=>node.setAttribute(key,String(value)));return node;};
  const s = {asset:"BTC", ticker:"", window:"round", view:"contract", data:null, request:0, busy:null, playing:false, head:null, lastFrame:0, rendered:0, demoKey:null, autoMarket:true};
  const colors = {UP:"#9bf0b7",DOWN:"#b399ef"};

  function select(group, chosen) {document.querySelectorAll(`[data-chart-${group}]`).forEach(button=>{const selected=button.dataset[`chart${group[0].toUpperCase()+group.slice(1)}`]===chosen;button.classList.toggle("selected",selected);button.setAttribute("aria-pressed",String(selected));});}
  function activeFills() {return (s.data?.fills||[]).filter(fill=>!isDemo() || fill.at <= s.head || (fill.kind==="settlement" && s.data.demo_complete && s.head>=s.data.range_end));}
  function isDemo() {return s.data?.dataset === "demo";}
  function visiblePoints() {return (s.data?.points||[]).filter(point=>!isDemo() || point.at<=s.head);}
  function readout() {
    const target=$("#chart-prices");target.replaceChildren();
    if(s.view==="reference"){
      const refs=(s.data?.references||[]).filter(point=>!isDemo()||point.at<=s.head), last=refs.at(-1);
      const item=make("div","chart-price-item up");item.append(make("span",null,s.asset),make("strong",null,dollars(last?.price)));target.append(item);
      const strike=make("div","chart-price-item");strike.append(make("span",null,"STRIKE"),make("strong",null,dollars(s.data?.market?.strike)));target.append(strike);
    }else{
      const point=visiblePoints().at(-1);
      for(const [direction,key] of [["UP","up_bid"],["DOWN","down_bid"]]){const item=make("div",`chart-price-item ${direction.toLowerCase()}`);item.append(make("span",null,direction+" BID"),make("strong",null,fmt(point?.[key])));target.append(item);}
    }
  }
  function activity() {
    const all=activeFills(), target=$("#chart-activity");target.replaceChildren();
    $("#chart-activity-count").textContent=`${all.length} recorded fill${all.length===1?"":"s"} ${isDemo()?"at this playback time":"in this range"}`;
    if(!all.length){target.append(make("span","muted",isDemo()?"Paper buys and sells will appear as the demo plays.":"No paper fills recorded for this market yet."));return;}
    for(const fill of all.slice(-30).reverse()){
      const button=make("button",`chart-fill ${fill.direction.toLowerCase()} ${fill.kind==="settlement"?"settlement":""}`);button.type="button";
      button.append(make("div","fill-label",(fill.action==="BUY"?"▲ ":fill.action==="SELL"?"■ ":"● ")+fill.label),make("div","fill-detail",`${fill.quantity} contracts @ ${fmt(fill.price)}`),make("div","fill-detail",`${clock(fill.at)} · fee $${Number(fill.fee).toFixed(4)}`));
      button.setAttribute("aria-label",`${fill.label}, ${fill.quantity} contracts, ${fmt(fill.price)}, ${clock(fill.at)}`);
      button.addEventListener("click",()=>{if(isDemo()){s.playing=false;s.head=Math.min(s.data.range_end,Math.max(s.data.range_start,fill.at));render();}showFill(fill);});target.append(button);
    }
  }
  function showFill(fill) {const tip=$("#chart-tooltip");tip.replaceChildren(make("strong",fill.direction.toLowerCase(),fill.label),make("div",null,`${fill.quantity} contracts @ ${fmt(fill.price)}`),make("div",null,clock(fill.at)),make("div",null,`Fee $${Number(fill.fee).toFixed(4)}`));tip.hidden=false;}
  function render() {
    const data=s.data, target=$("#market-chart-canvas");
    if(!data?.market){target.replaceChildren(make("div","chart-placeholder","Waiting for this asset's market and price observations..."));$("#chart-prices").textContent="Waiting for observations";$("#chart-activity").replaceChildren(make("span","muted","Waiting for this market's recorded fills."));$("#demo-playback").hidden=true;$("#chart-stream-status").textContent="WAITING FOR DATA";return;}
    readout();activity();
    const demo=isDemo();$("#demo-playback").hidden=!demo;
    $("#chart-play").disabled=demo&&!data.demo_complete;
    $("#chart-play").textContent=!data.demo_complete?"Preparing demo…":s.playing?"Pause demo":s.head>=data.range_end?"Replay demo":"Play demo";
    if(demo){const span=Math.max(1,data.range_end-data.range_start);$("#chart-scrubber").value=String(Math.round((s.head-data.range_start)/span*1000));const seconds=Math.max(0,Math.floor(s.head-data.range_start));$("#chart-playback-time").textContent=`${String(Math.floor(seconds/60)).padStart(2,"0")}:${String(seconds%60).padStart(2,"0")}`;}
    const status=$("#chart-stream-status");status.textContent=demo?(!data.demo_complete?"PREPARING DEMO":s.playing?"DEMO PLAYBACK":"RECORDED DEMO"):{live:"LIVE PAPER DATA",polling:"POLLING / COLLECT ONLY",stale:"STALE / HISTORICAL",mixed:"MIXED DATA — RESEARCH ONLY"}[data.status]||"WAITING FOR DATA";
    status.classList.toggle("active",data.status==="live"||s.playing);
    $("#chart-description").textContent=demo?"Synthetic recorded market data. Playback reveals the bot's recorded paper fills; it does not submit new trades.":`Updates every second. ${data.quote_label||"Waiting for quotes."} ${data.latest_received_at?"Last book: "+clock(data.latest_received_at)+".":"No book received yet."}`;
    const reference=s.view==="reference";
    const legend=$("#chart-legend");legend.replaceChildren();
    for(const [style,label] of reference?[["up","CF reference"],["ask","Dashed: strike"]]:[["up","UP bid"],["down","DOWN bid"],["ask","Dashed: ask"]]){const item=make("span");item.append(make("i",`legend-line ${style}`),document.createTextNode(label));legend.append(item);}for(const label of ["▲ BUY","■ SELL","Settlement is listed separately"])legend.append(make("span",null,label));
    const points=(reference?data.references:data.points).filter(point=>!demo||point.at<=s.head);
    const series=reference?[{key:"price",color:colors.UP,label:"CF reference"}]:[
      {key:"up_bid",color:colors.UP,label:"UP bid"},{key:"down_bid",color:colors.DOWN,label:"DOWN bid"},
      {key:"up_ask",color:colors.UP,label:"UP ask",dash:true},{key:"down_ask",color:colors.DOWN,label:"DOWN ask",dash:true}];
    const values=points.flatMap(point=>series.map(line=>numeric(point[line.key]))).filter(value=>value!==null);
    if(!values.length){target.replaceChildren(make("div","chart-placeholder",reference?"The CF reference feed is not available yet. No substitute price is shown.":"Waiting for the first order-book quote..."));return;}
    const W=960,H=340,L=65,R=935,T=23,B=295;
    const from=data.range_start,to=Math.max(from+1,data.range_end);
    let low=0, high=1;
    if(reference){if(numeric(data.market.strike)!==null)values.push(Number(data.market.strike));low=Math.min(...values);high=Math.max(...values);const padding=(high-low)*.12||Math.max(high*.00005,.0001);low-=padding;high+=padding;}
    const x=time=>L+(time-from)/(to-from)*(R-L), y=value=>B-(value-low)/(high-low)*(B-T);
    const canvas=svg("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":`${s.asset} ${reference?"reference price and strike":"UP and DOWN contract prices"} with recorded paper buy and sell markers`});
    for(let i=0;i<=4;i++){const value=low+(high-low)*i/4;canvas.append(svg("line",{x1:L,x2:R,y1:y(value),y2:y(value),class:"axis-grid"}));const text=svg("text",{x:L-12,y:y(value)+4,"text-anchor":"end",class:"axis"});text.textContent=reference?dollars(value):Math.round(value*100)+"¢";canvas.append(text);}
    for(let i=0;i<=4;i++){const at=from+(to-from)*i/4,text=svg("text",{x:x(at),y:B+25,"text-anchor":i===0?"start":i===4?"end":"middle",class:"axis"});text.textContent=clock(at);canvas.append(text);}
    if(reference&&numeric(data.market.strike)!==null){canvas.append(svg("line",{x1:L,x2:R,y1:y(Number(data.market.strike)),y2:y(Number(data.market.strike)),stroke:"#d4b67d","stroke-dasharray":"6 5"}));}
    for(const line of series){let segment=[],last=null;const flush=()=>{if(segment.length>1)canvas.append(svg("polyline",{points:segment.join(" "),fill:"none",stroke:line.color,"stroke-width":line.dash?1.2:2.4,"stroke-dasharray":line.dash?"4 5":"none",opacity:line.dash?.45:1,"stroke-linecap":"round","stroke-linejoin":"round"}));segment=[];};for(const point of points){const value=numeric(point[line.key]);if(value===null||(last!==null&&point.at-last>5))flush();if(value!==null)segment.push(`${x(point.at)},${y(value)}`);last=point.at;}flush();}
    for(const fill of activeFills()){
      if(fill.kind==="settlement"||fill.at<from||fill.at>to||numeric(fill.price)===null)continue;
      let value=Number(fill.price);
      if(reference){const known=points.filter(point=>point.at<=fill.at&&fill.at-point.at<=2).at(-1);value=numeric(known?.price);if(value===null)continue;}
      const px=x(fill.at),py=y(value),color=colors[fill.direction],group=svg("g",{"data-trade-marker":fill.label,tabindex:"0",role:"button","aria-label":`${fill.label} ${fill.quantity} at ${fmt(fill.price)} ${clock(fill.at)}`});
      group.append(svg(fill.action==="BUY"?"path":"rect",fill.action==="BUY"?{d:`M ${px} ${py-8} L ${px-7} ${py+6} L ${px+7} ${py+6} Z`,fill:color,stroke:"#0a151c","stroke-width":2}:{x:px-6,y:py-6,width:12,height:12,rx:2,fill:color,stroke:"#0a151c","stroke-width":2}));
      const label=svg("text",{x:Math.max(L+35,Math.min(R-38,px)),y:Math.max(T+12,py-15),"text-anchor":"middle",fill:color,"font-size":10,"font-weight":700,"paint-order":"stroke",stroke:"#0b171e","stroke-width":3});label.textContent=fill.label;group.append(label);
      const title=svg("title");title.textContent=`${fill.label}: ${fill.quantity} @ ${fmt(fill.price)}, ${clock(fill.at)}`;group.append(title);
      group.addEventListener("click",()=>showFill(fill));group.addEventListener("focus",()=>showFill(fill));group.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();showFill(fill);}});canvas.append(group);
    }
    if(demo&&s.head>=from&&s.head<=to)canvas.append(svg("line",{x1:x(s.head),x2:x(s.head),y1:T,y2:B,stroke:"#b8c9d0","stroke-width":1,"stroke-dasharray":"3 4",opacity:.6}));
    const cross=svg("line",{x1:L,x2:L,y1:T,y2:B,stroke:"#9aafbb","stroke-width":1,visibility:"hidden"});canvas.append(cross);
    canvas.addEventListener("pointermove",event=>{const rect=canvas.getBoundingClientRect(),px=(event.clientX-rect.left)/rect.width*W;if(px<L||px>R)return;const at=from+(px-L)/(R-L)*(to-from);const past=points.filter(point=>point.at<=at),point=past.at(-1);const tip=$("#chart-tooltip");if(!point||at-point.at>5){tip.hidden=true;return;}cross.setAttribute("x1",x(point.at));cross.setAttribute("x2",x(point.at));cross.setAttribute("visibility","visible");tip.replaceChildren(make("strong",null,clock(point.at)));for(const line of series){const row=make("div",line.key.startsWith("down")?"down":"up",`${line.label}: ${reference?dollars(point[line.key]):fmt(point[line.key])}`);tip.append(row);}tip.hidden=false;});
    canvas.addEventListener("pointerleave",()=>{cross.setAttribute("visibility","hidden");$("#chart-tooltip").hidden=true;});
    target.replaceChildren(canvas);
  }
  async function refresh() {
    const params=new URLSearchParams({asset:s.asset,window:s.window});if(s.ticker&&!s.autoMarket)params.set("ticker",s.ticker);
    const query=params.toString();if(s.busy===query)return;s.busy=query;const request=++s.request;
    try{
      const response=await fetch("/api/chart?"+params,{cache:"no-store"}), data=await response.json();if(request!==s.request)return;if(!response.ok)throw new Error(data.detail||"Chart unavailable");
      const oldTicker=s.data?.market?.ticker;s.data=data;
      if(!data.market){render();return;}
      s.ticker=data.market.ticker;
      const menu=$("#chart-market");const options=JSON.stringify(data.markets.map(item=>item.ticker));if(menu.dataset.options!==options){menu.replaceChildren();for(const item of data.markets){const option=make("option",null,item.ticker);option.value=item.ticker;menu.append(option);}menu.dataset.options=options;}menu.value=s.ticker;
      if(isDemo()){
        const key=s.ticker+":"+s.window;
        if(s.demoKey!==key||s.head===null){s.head=data.range_start;s.playing=false;s.demoKey=null;}
        if(data.demo_complete&&s.demoKey!==key){s.demoKey=key;s.head=data.range_start;s.playing=true;s.lastFrame=performance.now();}
        if(!data.demo_complete)s.head=data.range_end;
        s.head=Math.min(data.range_end,Math.max(data.range_start,s.head));
      }else{s.playing=false;s.head=data.range_end;s.demoKey=null;}
      if(oldTicker!==s.ticker)$("#chart-tooltip").hidden=true;
      render();
    }catch(error){if(request!==s.request)return;$("#chart-stream-status").textContent="WAITING / DISCONNECTED";$("#chart-description").textContent=error.message+" The chart retries automatically; any displayed prices are older observations.";}finally{if(request===s.request)s.busy=null;}
  }
  document.querySelectorAll("[data-chart-asset]").forEach(button=>button.addEventListener("click",()=>{s.asset=button.dataset.chartAsset;s.ticker="";s.autoMarket=true;s.data=null;s.head=null;s.demoKey=null;s.playing=false;select("asset",s.asset);$("#chart-tooltip").hidden=true;render();refresh();}));
  document.querySelectorAll("[data-chart-view]").forEach(button=>button.addEventListener("click",()=>{s.view=button.dataset.chartView;select("view",s.view);$("#chart-tooltip").hidden=true;render();}));
  document.querySelectorAll("[data-chart-window]").forEach(button=>button.addEventListener("click",()=>{s.window=button.dataset.chartWindow;s.head=null;s.demoKey=null;select("window",s.window);refresh();}));
  $("#chart-market").addEventListener("change",event=>{s.ticker=event.target.value;s.autoMarket=false;s.head=null;s.demoKey=null;s.playing=false;refresh();});
  $("#chart-play").addEventListener("click",()=>{if(!s.data?.demo_complete)return;if(s.head>=s.data.range_end)s.head=s.data.range_start;s.playing=!s.playing;s.lastFrame=performance.now();render();});
  $("#chart-scrubber").addEventListener("input",event=>{if(!s.data)return;s.playing=false;s.head=s.data.range_start+(s.data.range_end-s.data.range_start)*Number(event.target.value)/1000;$("#chart-tooltip").hidden=true;render();});
  function animate(now){const elapsed=Math.max(0,(now-s.lastFrame)/1000);s.lastFrame=now;if(s.playing&&s.data&&document.visibilityState!=="hidden"){s.head=Math.min(s.data.range_end,s.head+elapsed*Number($("#chart-speed").value));if(s.head>=s.data.range_end)s.playing=false;if(now-s.rendered>=100){s.rendered=now;render();}}requestAnimationFrame(animate);}
  refresh();setInterval(()=>{if(document.visibilityState!=="hidden")refresh();},1000);requestAnimationFrame(animate);
})();
