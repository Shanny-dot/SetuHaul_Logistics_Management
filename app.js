/* SetuHaul shared helpers: nav, formatting, lookups, motion */
(function(){
  const PAGES=[
    ['index.html','Home'],['brief.html','Brief Coverage'],['flow.html','Flow & Architecture'],['driver.html','Driver Messages'],['coordinator.html','Day Board'],
    ['handoffs.html','Hand-offs'],['planner.html','Planner'],['insights.html','Data Insights'],
    ['integrity.html','Integrity'],['mvp2.html','MVP2 · GPS Tower']
  ];
  const LOGO=`<svg viewBox="0 0 40 40" fill="none"><rect width="40" height="40" rx="11" fill="#DB7E00"/>
    <path d="M6 26c4-9 10-13 14-13s10 4 14 13" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/>
    <path d="M11 20.5v6M16 16.5v10M20 15v11.5M24 16.5v10M29 20.5v6" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>
    <path d="M4 29.5h32" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/></svg>`;
  window.SH_LOGO=LOGO;
  const here=(location.pathname.split('/').pop()||'index.html');
  const top=document.createElement('header');top.className='topbar';
  top.innerHTML=`<div class="in"><a class="brand" href="index.html">${LOGO}<span><b>SetuHaul</b><small>Logistic Management</small></span></a>
    <nav class="nav">${PAGES.map(([h,l])=>`<a href="${h}" class="${h===here?'on':''}"><span class="dot"></span>${l}</a>`).join('')}<span class="more"><button class="${['trucks.html','how.html'].includes(here)?'on':''}">More ▾</button><span class="mm"><a href="trucks.html">Trucks</a><a href="how.html">Inside a turn</a></span></span></nav></div>`;
  document.body.prepend(top);
  const f=document.createElement('footer');f.className='foot';
  f.innerHTML=`SetuHaul — Logistic Management · Driver-Exception Agent prototype · built from the FDE Academy Mini Project 2 deliverables (snapshot 4 Aug 2026, Jaipur DC). All numbers come from the scenario replay.`;
  document.body.append(f);
})();

const SH=window.SH;
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const byId=(arr,k)=>Object.fromEntries(arr.map(x=>[x[k],x]));
const SHIP=byId(SH.ships,'shipment_id'), DRV=byId(SH.drivers,'driver_id'), VEH=byId(SH.vehicles,'vehicle_id');
const toMin=h=>{const[a,b]=h.split(':').map(Number);return a*60+b};
const hm=m=>String(Math.floor(m/60)).padStart(2,'0')+':'+String(m%60).padStart(2,'0');
const ampm=m=>{let h=Math.floor(m/60),mm=m%60,s=h>=12?'PM':'AM';h=h%12||12;return `${h}:${String(mm).padStart(2,'0')} ${s}`};
const tsMin=ts=>ts?toMin(ts.slice(11,16)):null;
const prioCls=p=>({CRITICAL:'crit',HIGH:'high',NORMAL:'norm',LOW:'low'})[p]||'low';
const prioChip=p=>`<span class="chip ${prioCls(p)}">${p}</span>`;
const dockChip=t=>t==='REEFER'?'<span class="chip reefer">❄ Reefer</span>':t==='HEAVY'?'<span class="chip heavy">⛓ Heavy</span>':'<span class="chip">Standard</span>';
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const sleep=ms=>new Promise(r=>setTimeout(r,ms));

function toast(msg,icon='✓'){let t=$('.toast');if(!t){t=document.createElement('div');t.className='toast';document.body.append(t)}
  t.innerHTML=`<span>${icon}</span><span>${msg}</span>`;t.classList.add('on');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('on'),2600)}

function countUp(el){const end=parseFloat(el.dataset.to),dec=(el.dataset.to.split('.')[1]||'').length,suf=el.dataset.suf||'';let t0=null;
  const step=t=>{t0??=t;const p=Math.min(1,(t-t0)/1300),e=1-Math.pow(1-p,3);el.textContent=(end*e).toFixed(dec)+suf;if(p<1)requestAnimationFrame(step)};requestAnimationFrame(step)}

const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){e.target.classList.add('in');$$('[data-to]',e.target).forEach(countUp);if(e.target.dataset.to)countUp(e.target);io.unobserve(e.target)}}),{threshold:.15});
function watchReveal(){ $$('.rv:not(.in)').forEach(el=>io.observe(el)) }
document.addEventListener('DOMContentLoaded',watchReveal);

/* drawer */
function openDrawer(html){let bg=$('.drawer-bg'),d=$('.drawer');
  if(!d){bg=document.createElement('div');bg.className='drawer-bg';d=document.createElement('aside');d.className='drawer';document.body.append(bg,d);bg.onclick=closeDrawer}
  d.innerHTML=`<button class="x" onclick="closeDrawer()">✕</button>`+html;requestAnimationFrame(()=>{bg.classList.add('open');d.classList.add('open')})}
function closeDrawer(){$('.drawer-bg')?.classList.remove('open');$('.drawer')?.classList.remove('open')}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeDrawer()});

/* case lookup: shipment -> scenario case */
const CASES={};SH.log.forEach(e=>{if(e.case&&e.shipment_id&&!CASES[e.shipment_id])CASES[e.shipment_id]=e.case});

function shipmentDrawer(id){const s=SHIP[id];if(!s)return;const d=DRV[s.driver_id],v=VEH[s.vehicle_id];
  const appts=SH.appts.filter(a=>a.sh===id);
  const c=CASES[id];
  openDrawer(`<div class="eyebrow">Shipment</div><h2 style="font-size:28px">${s.shipment_id}</h2>
   <div class="muted mono" style="margin:4px 0 14px">${s.order_reference}</div>
   <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:18px">${prioChip(s.priority_code)}${dockChip(s.required_dock_type)}<span class="chip">${s.current_status}</span></div>
   <dl class="kv"><dt>Customer</dt><dd>${esc(s.customer_name)}</dd><dt>Load</dt><dd>${esc(s.product_category)} · ${(s.load_weight_kg/1000).toFixed(1)} t · ${s.pallet_count} pallets</dd>
   <dt>From</dt><dd>${esc(s.origin_city)}</dd><dt>Driver</dt><dd>${d?esc(d.driver_name)+' <span class=dim>('+d.driver_id+')</span>':'—'}</dd>
   <dt>Vehicle</dt><dd class="mono">${v?v.registration_number+' · '+v.vehicle_type_code:'—'}</dd>
   <dt>Original ETA</dt><dd>${ampm(tsMin(s.original_eta_ts))}</dd><dt>Latest ETA</dt><dd>${ampm(tsMin(s.latest_eta_ts))}</dd><dt>Unload</dt><dd>${s.expected_unload_min} min</dd></dl>
   <h3 style="font-size:15px;margin:22px 0 10px">Booking history</h3>
   ${appts.length?appts.map(a=>`<div style="display:flex;justify-content:space-between;gap:10px;padding:10px 12px;border:1px solid var(--line);border-radius:12px;margin-bottom:8px;${a.cancelled!=null?'opacity:.55':''}">
     <div><b class="mono">${a.id}</b><div class="dim" style="font-size:12px">${a.src.replace('_',' ').toLowerCase()}${a.cancelled!=null?' · cancelled '+hm(a.cancelled)+' — '+esc(a.why):''}</div></div>
     <div style="text-align:right"><b>Dock ${a.dock}</b><div class="muted mono" style="font-size:12px">${hm(a.start)}–${hm(a.end)}</div></div></div>`).join(''):'<div class="muted">No booking at Jaipur DC.</div>'}
   ${c?`<a class="btn pri" style="margin-top:14px" href="driver.html#${c.toLowerCase()}">Open driver messages ${c} →</a>`:''}`)}
