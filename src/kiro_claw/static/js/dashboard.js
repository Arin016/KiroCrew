// ── Nav ──
const shell=document.getElementById('shell'),navEl=document.getElementById('nav'),mainContent=document.getElementById('main-content');
document.querySelectorAll('.nav-item').forEach(n=>{
  n.onclick=()=>{
    document.querySelectorAll('.nav-item').forEach(x=>x.classList.remove('active'));
    n.classList.add('active');
    const pg=n.dataset.page;
    document.querySelectorAll('.page').forEach(x=>x.classList.remove('active'));
    document.getElementById('page-'+pg).classList.add('active');
    mainContent.className=pg==='chat'?'content is-chat':'content';
    // Track active page — controls conditional polling (e.g. /api/system only on system page)
    if(typeof _activePage!=='undefined')_activePage=pg;
    if(pg==='system')rSystem();
    if(pg==='logs'&&typeof startLogSSE==='function')startLogSSE();
  };
});

// ── Nav collapse ──
const navToggle=document.getElementById('nav-toggle');
function setNavState(collapsed){shell.classList.toggle('nav-collapsed',collapsed);navEl.classList.toggle('collapsed',collapsed);localStorage.setItem('mc-nav',collapsed?'1':'0')}
setNavState(localStorage.getItem('mc-nav')==='1');
navToggle.onclick=()=>setNavState(!shell.classList.contains('nav-collapsed'));

// ── Theme ──
const tBtn=document.getElementById('theme-toggle');
function setTheme(t){document.documentElement.dataset.theme=t;localStorage.setItem('mc-theme',t);tBtn.textContent=t==='dark'?'☀ Light':'🌙 Dark'}
setTheme(localStorage.getItem('mc-theme')||'dark');
tBtn.onclick=()=>setTheme(document.documentElement.dataset.theme==='dark'?'light':'dark');
// ── SSE ──
const sse=new EventSource('/api/stream')
sse.addEventListener('dashboard',e=>{try{const d=JSON.parse(e.data);
  document.getElementById('s-up').textContent=d.uptime;
  document.getElementById('s-sess').textContent=d.sessions;
  document.getElementById('s-msg').textContent=d.messages;
  document.getElementById('s-cron').textContent=d.cron_jobs;
  document.getElementById('s-sub').textContent=d.subagents;
  document.getElementById('s-les').textContent=d.lessons;
  document.getElementById('a-sess').textContent=d.sessions;
  document.getElementById('a-sub').textContent=d.subagents;
  document.getElementById('sse-dot').classList.add('ok');
  document.getElementById('sse-lbl').textContent='OK';
}catch(x){}});
sse.onerror=()=>{document.getElementById('sse-dot').classList.remove('ok');document.getElementById('sse-lbl').textContent='Offline'};

// ── Notifications (cron/subagent results) ──
const notifList=document.getElementById('notif-list');
const notifBadge=document.getElementById('notif-badge');
let _notifCount=0;
function _updateBadge(){notifBadge.textContent=_notifCount;notifBadge.style.display=_notifCount>0?'':'none'}
function addNotification(n,silent){
  if(!silent){_notifCount++;_updateBadge()}
  const el=document.createElement('div');
  el.className='notif-item '+(n.kind||'cron');
  el.dataset.read='';
  el.dataset.ts=n.ts||'';
  el.innerHTML=`<div class="notif-title">${n.kind==='cron'?'⏰':'🤖'} ${esc(n.title)}<span class="s-close notif-close" style="float:right;opacity:.5;cursor:pointer;font-size:10px;margin-left:6px">✕</span></div><div class="notif-body">${esc(n.body||'').slice(0,200)}</div>`;
  el.onclick=(e)=>{
    // Close button — remove from server + DOM + decrement
    if(e.target.classList.contains('notif-close')){
      e.stopPropagation();
      if(!el.dataset.read){_notifCount=Math.max(0,_notifCount-1);_updateBadge()}
      // Delete from server so it doesn't come back on refresh
      const ts=el.dataset.ts;
      if(ts)fetch('/api/notifications',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({ts})}).catch(()=>{});
      el.remove();
      return;
    }
    // Mark as read + decrement badge on first view
    if(!el.dataset.read){el.dataset.read='1';_notifCount=Math.max(0,_notifCount-1);_updateBadge()}
    // Show full body in chat pane — clear activeSlot so user can click back
    activeSlot=null;
    showChatPane(true);
    document.getElementById('chat-hdr-title').textContent=n.title;
    cMsgs.style.display='flex';
    cMsgs.innerHTML='';
    addMsg('assistant',md(n.body||'No response'));
    chatHdr.style.display='flex';
    chatBar.style.display='none';
    _lastSessFingerprint='';refreshSessions();
  };
  notifList.insertBefore(el,notifList.firstChild);
  if(notifList.children.length>50)notifList.removeChild(notifList.lastChild);
}
sse.addEventListener('notification',e=>{try{addNotification(JSON.parse(e.data))}catch(x){}});
// Load existing notifications on page load — badge shows total count
// (server unread resets on gateway restart, so fall back to total persisted count)
(async()=>{try{const d=await(await fetch('/api/notifications')).json();const ns=d.notifications||[];for(const n of ns)addNotification(n,true);_notifCount=d.unread>0?d.unread:ns.length;_updateBadge()}catch(e){}})();
// Toggle notifications panel (never fully hide — just collapse/expand)
let _notifOpen=true;
document.getElementById('notif-toggle').onclick=()=>{
  _notifOpen=!_notifOpen;
  notifList.style.display=_notifOpen?'':'none';
};
// Clear all notifications (keep panel open for new ones) — persists to server
document.getElementById('notif-clear').onclick=(e)=>{
  e.stopPropagation();
  notifList.innerHTML='';
  _notifCount=0;notifBadge.style.display='none';
  _notifOpen=true;
  notifList.style.display='';
  // Clear on server so they don't come back on refresh
  fetch('/api/notifications/clear',{method:'POST'}).catch(()=>{});
};

// ── Data fetchers ──
function _fmtMB(mb){return mb>=1024?((mb/1024).toFixed(1)+' GB'):(mb+' MB')}
function _fmtSpeed(kbs){return kbs>=1024?((kbs/1024).toFixed(1)+' MB/s'):(Math.round(kbs)+' KB/s')}
async function rSystem(){try{const d=await(await fetch('/api/system')).json();
  document.getElementById('si-host').textContent=d.hostname;document.getElementById('si-os').textContent=d.os;
  document.getElementById('si-arch').textContent=d.arch;document.getElementById('si-cpu').textContent=d.cpu_count;
  document.getElementById('si-load').textContent=d.load_1m!=null?d.load_1m+' / '+d.load_5m+' / '+d.load_15m:'—';
  document.getElementById('si-cpupct').textContent=d.cpu_pct!=null?d.cpu_pct+'%':'—';
  document.getElementById('si-memused').textContent=d.mem_used_gb!=null?d.mem_used_gb+' / '+d.mem_total_gb+' GB':'—';
  // Network speed from server (server-side delta, survives page refresh)
  const _rxSpd=d.net_rx_kbs!=null?_fmtSpeed(d.net_rx_kbs):'—';
  const _txSpd=d.net_tx_kbs!=null?_fmtSpeed(d.net_tx_kbs):'—';
  document.getElementById('si-netrx').textContent=_rxSpd;
  document.getElementById('si-nettx').textContent=_txSpd;
  document.getElementById('si-memtotal').textContent=d.mem_total_gb?d.mem_total_gb+' GB':'—';
  document.getElementById('si-memusedv').textContent=d.mem_used_gb?d.mem_used_gb+' GB':'—';
  document.getElementById('si-memfree').textContent=d.mem_free_gb?d.mem_free_gb+' GB':'—';
  document.getElementById('si-py').textContent=d.python;document.getElementById('si-pid').textContent=d.pid;
  document.getElementById('si-cwd').textContent=d.cwd;
  document.getElementById('si-procmem').textContent=d.proc_mem_mb?d.proc_mem_mb+' MB':'—';
  document.getElementById('si-childprocs').textContent=d.child_processes!=null?d.child_processes:'—';
  document.getElementById('si-threads').textContent=d.thread_count!=null?d.thread_count:'—';
  document.getElementById('si-proccpu').textContent=d.proc_cpu_pct!=null?d.proc_cpu_pct+'%':'—';
  // Fetch status for session count + uptime
  try{const st=await(await fetch('/api/status')).json();
    document.getElementById('si-procsess').textContent=st.sessions||0;
    document.getElementById('si-procup').textContent=st.uptime||'—';
  }catch(x){}
  document.getElementById('si-ip').textContent=d.ip||'—';
  document.getElementById('si-netrxv').textContent=_rxSpd;
  document.getElementById('si-nettxv').textContent=_txSpd;
  document.getElementById('si-dt').textContent=d.disk_total_gb?d.disk_total_gb+' GB':'—';
  document.getElementById('si-df').textContent=d.disk_free_gb?d.disk_free_gb+' GB':'—'}catch(e){}}
async function rAgents(){try{const d=await(await fetch('/api/spawn')).json();const t=document.getElementById('ag-tb');
  if(!d.agents.length){t.replaceChildren();const r=t.insertRow();const c=r.insertCell();c.colSpan=3;c.className='empty';c.textContent='No subagents';return}
  t.replaceChildren();d.agents.forEach(a=>{const r=t.insertRow();const c0=r.insertCell();const cd=document.createElement('code');cd.textContent=a.id;c0.appendChild(cd);
  const c1=r.insertCell();c1.textContent=a.task;const c2=r.insertCell();const b=document.createElement('span');
  b.className=a.done?(a.error?'badge b-err':'badge b-ok'):'badge b-warn';b.textContent=a.done?(a.error?'Failed':'Done'):'Running';c2.appendChild(b);
  if(!a.done){const s=document.createElement('small');let pt=` ${a.elapsed||0}s`;if(a.turns)pt+=`, ${a.turns} turns`;if(a.last_tool)pt+=`, ${a.last_tool}`;s.textContent=pt;c2.appendChild(s)}})}catch(e){}}
// Track active page to avoid polling invisible pages (saves HTTP connections)
let _activePage='chat';
rCrons();rLessons();rAgents();
// Only poll /api/system when System page is visible (heavy endpoint — subprocess calls).
// Other pages poll at relaxed intervals to keep HTTP connection pool free.
setInterval(()=>{if(_activePage==='system')rSystem()},2000);
// Crons/lessons/agents now refresh via SSE push — no polling needed.
// Initial load on page open + SSE refresh events handle all updates.

// ── Overview tabs ──
document.querySelectorAll('.ov-tab').forEach(t=>{
  t.onclick=()=>{
    document.querySelectorAll('.ov-tab').forEach(x=>x.classList.remove('active'));
    t.classList.add('active');
    document.querySelectorAll('.ov-panel').forEach(x=>x.classList.remove('active'));
    document.getElementById('ov-'+t.dataset.tab).classList.add('active');
    ovLoad(t.dataset.tab);
  };
});

function ovLoad(tab){
  if(tab==='memory')rMemory();
  if(tab==='cron')rCrons();
  if(tab==='lessons')rLessons();
  if(tab==='skills')rSkills();
  if(tab==='mcp')rMcp();
  if(tab==='agentcfg')rAgentCfg();
}

// Memory
async function rMemory(){
  try{const d=await(await fetch('/api/memory/preferences')).json();document.getElementById('mem-pref').value=d.content||''}catch(e){}
  try{const d=await(await fetch('/api/memory/projects')).json();document.getElementById('mem-proj').value=d.content||''}catch(e){}
  try{const d=await(await fetch('/api/memory/history')).json();document.getElementById('mem-hist').textContent=d.content||'No history'}catch(e){}
}
document.getElementById('mem-pref-save').onclick=async()=>{
  const r=await fetch('/api/memory/preferences',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:document.getElementById('mem-pref').value})});
  if(r.ok)document.getElementById('mem-pref-save').textContent='✓ Saved';setTimeout(()=>document.getElementById('mem-pref-save').textContent='Save',2000);
};
document.getElementById('mem-proj-save').onclick=async()=>{
  const r=await fetch('/api/memory/projects',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:document.getElementById('mem-proj').value})});
  if(r.ok)document.getElementById('mem-proj-save').textContent='✓ Saved';setTimeout(()=>document.getElementById('mem-proj-save').textContent='Save',2000);
};

// Cron (enhanced with actions)
async function rCrons(){try{const d=await(await fetch('/api/crons')).json();const t=document.getElementById('cron-tb');
  t.replaceChildren();
  if(!d.jobs.length){const r=t.insertRow();const c=r.insertCell();c.colSpan=8;c.className='empty';c.textContent='No cron jobs';return}
  d.jobs.forEach(j=>{
    const r=t.insertRow();
    const c0=r.insertCell();const code0=document.createElement('code');code0.textContent=j.id;c0.appendChild(code0);
    r.insertCell().textContent=j.name;
    const c2=r.insertCell();const code2=document.createElement('code');code2.textContent=j.schedule;c2.appendChild(code2);
    r.insertCell().textContent=j.message;
    const c4=r.insertCell();
    if(j.approval_mode){const s=document.createElement('span');s.className='badge b-ok';s.textContent=j.approval_mode;c4.appendChild(s)}else{c4.textContent='—'}
    if(j.silent){c4.append(' 🔇')}
    const c5=r.insertCell();
    if(j.channel){const cd=document.createElement('code');cd.textContent=j.channel;c5.appendChild(cd)}else{c5.textContent='—'}
    const c6=r.insertCell();
    let bClass,bText;
    if(!j.enabled){bClass='b-warn';bText='Paused'}else if(j.last_status==='ok'){bClass='b-ok';bText='OK'}else if(j.last_status==='error'){bClass='b-err';bText='Error'}else{bClass='b-ok';bText='Ready'}
    const badge=document.createElement('span');badge.className='badge '+bClass;badge.textContent=bText;c6.appendChild(badge);
    const c7=r.insertCell();
    const tb=document.createElement('button');tb.className='act-btn';tb.textContent=j.enabled?'Pause':'Resume';tb.onclick=()=>cronToggle(j.id,!j.enabled);c7.appendChild(tb);
    c7.append(' ');
    const db=document.createElement('button');db.className='act-btn danger';db.textContent='Delete';db.onclick=()=>cronDel(j.id);c7.appendChild(db);
  })}catch(e){}}
window.cronToggle=async(id,en)=>{await fetch('/api/crons/'+id+'/enable',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:en})});rCrons()};
window.cronDel=async(id)=>{await fetch('/api/crons/'+id,{method:'DELETE'});rCrons()};
document.getElementById('cron-add-btn').onclick=async()=>{
  const name=document.getElementById('cron-name').value.trim();
  const msg=document.getElementById('cron-msg').value.trim();
  const sched=document.getElementById('cron-sched').value.trim();
  if(!name||!msg){alert('Name and message required');return}
  const body={name,message:msg};
  if(sched.match(/^[\d\s\*\/\-\,A-Z]+$/i)&&sched.split(/\s+/).length===5)body.cron=sched;
  else if(sched.match(/^\d+$/))body.every=parseInt(sched);
  else body.schedule=sched;
  const ap=document.getElementById('cron-approval').value;
  if(ap)body.approval_mode=ap;
  const ch=document.getElementById('cron-channel').value.trim();
  if(ch)body.channel=ch;
  if(document.getElementById('cron-silent').checked)body.silent=true;
  const r=await fetch('/api/crons',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.ok){document.getElementById('cron-name').value='';document.getElementById('cron-msg').value='';document.getElementById('cron-sched').value='';document.getElementById('cron-approval').value='';document.getElementById('cron-channel').value='';document.getElementById('cron-silent').checked=false;rCrons()}
};

// Lessons (enhanced with actions)
async function rLessons(){try{const d=await(await fetch('/api/lessons')).json();const t=document.getElementById('les-tb');
  if(!d.lessons.length){t.innerHTML='<tr><td colspan="4" class="empty">No lessons</td></tr>';return}
  t.innerHTML=d.lessons.slice(-20).reverse().map(l=>{
    let acts=`<button class="act-btn danger" onclick="lesDel('${esc(l.rule.replace(/'/g,"\\'"))}')">Delete</button>`;
    return`<tr><td>${esc(l.rule)}</td><td><span class="badge b-ok">${l.category}</span></td><td>${new Date(l.ts).toLocaleString()}</td><td>${acts}</td></tr>`;
  }).join('')}catch(e){}}
window.lesDel=async(rule)=>{await fetch('/api/lessons',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({rule})});rLessons()};
document.getElementById('les-add-btn').onclick=async()=>{
  const rule=document.getElementById('les-rule').value.trim();
  const cat=document.getElementById('les-cat').value;
  if(!rule){alert('Rule is required');return}
  const r=await fetch('/api/lessons',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rule,category:cat})});
  if(r.ok){document.getElementById('les-rule').value='';rLessons()}
};

// Skills (CRUD)
let _activeSkillName='';
async function rSkills(){
  try{
    const skills=await(await fetch('/api/skills')).json();
    const el=document.getElementById('skills-list');
    if(!skills.length){el.innerHTML='<div class="empty">No skills installed</div>';return}
    el.innerHTML=skills.map(s=>`<div class="skill-item" onclick="showSkill('${esc(s.name)}')"><span class="skill-name">${esc(s.name)}</span><span class="skill-desc">${esc(s.description||'')}</span></div>`).join('');
  }catch(e){}
}
window.showSkill=async(name)=>{
  try{
    const d=await(await fetch('/api/skills/'+encodeURIComponent(name))).json();
    _activeSkillName=name;
    document.getElementById('skill-detail-title').textContent=name;
    document.getElementById('skill-detail-content').textContent=d.content||'';
    document.getElementById('skill-detail-content').style.display='';
    document.getElementById('skill-edit-area').style.display='none';
    document.getElementById('skill-detail-card').style.display='';
  }catch(e){}
};
// Create skill
document.getElementById('skill-create-toggle').onclick=()=>{
  const f=document.getElementById('skill-create-form');
  f.style.display=f.style.display==='none'?'':'none';
};
document.getElementById('skill-create-cancel').onclick=()=>{
  document.getElementById('skill-create-form').style.display='none';
};
document.getElementById('skill-create-btn').onclick=async()=>{
  const name=document.getElementById('skill-new-name').value.trim();
  const content=document.getElementById('skill-new-content').value.trim();
  if(!name){alert('Skill name is required');return}
  if(!content){alert('Skill content is required');return}
  const r=await fetch('/api/skills',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,content})});
  if(r.ok){
    document.getElementById('skill-new-name').value='';
    document.getElementById('skill-new-content').value='';
    document.getElementById('skill-create-form').style.display='none';
    rSkills();
  }else{const b=await r.json();alert(b.error||'Failed to create')}
};
// Edit skill
document.getElementById('skill-edit-btn').onclick=()=>{
  document.getElementById('skill-detail-content').style.display='none';
  document.getElementById('skill-edit-area').style.display='';
  document.getElementById('skill-edit-content').value=document.getElementById('skill-detail-content').textContent;
};
document.getElementById('skill-edit-cancel').onclick=()=>{
  document.getElementById('skill-detail-content').style.display='';
  document.getElementById('skill-edit-area').style.display='none';
};
document.getElementById('skill-save-btn').onclick=async()=>{
  const content=document.getElementById('skill-edit-content').value;
  const r=await fetch('/api/skills/'+encodeURIComponent(_activeSkillName),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({content})});
  if(r.ok){
    document.getElementById('skill-detail-content').textContent=content;
    document.getElementById('skill-detail-content').style.display='';
    document.getElementById('skill-edit-area').style.display='none';
    document.getElementById('skill-save-btn').textContent='✓ Saved';
    setTimeout(()=>document.getElementById('skill-save-btn').textContent='Save',2000);
    rSkills();
  }else{const b=await r.json();alert(b.error||'Failed to save')}
};
// Delete skill
document.getElementById('skill-del-btn').onclick=async()=>{
  if(!confirm('Delete skill "'+_activeSkillName+'"?'))return;
  const r=await fetch('/api/skills/'+encodeURIComponent(_activeSkillName),{method:'DELETE'});
  if(r.ok){
    document.getElementById('skill-detail-card').style.display='none';
    _activeSkillName='';
    rSkills();
  }
};

// MCP Servers
async function rMcp(){
  try{
    const servers=await(await fetch('/api/mcp')).json();
    _renderMcp(servers);
  }catch(e){}
}
function _renderMcp(servers){
  const badges=document.getElementById('mcp-badges');
  const tb=document.getElementById('mcp-tb');
  if(!servers.length){
    badges.innerHTML='<span style="color:var(--muted);font-size:12px">No MCP servers configured</span>';
    tb.innerHTML='<tr><td colspan="6" class="empty">No MCP servers</td></tr>';
    return;
  }
  badges.innerHTML=servers.map(s=>{
    const en=s.enabled!==false;
    const color=s.status==='ok'?'b-ok':s.status==='error'?'b-err':en?'b-warn':'b-err';
    const dim=en?'':'opacity:.4';
    return`<span class="badge ${color}" style="${dim}">🔌 ${esc(s.name)}${en?'':' (off)'}</span>`;
  }).join('');
  tb.innerHTML=servers.map(s=>{
    const en=s.enabled!==false;
    const status=s.status==='ok'?'<span class="badge b-ok">Online</span>':s.status==='error'?'<span class="badge b-err" title="'+esc(s.error||'')+'">Error</span>':s.status==='probing'?'<span class="badge b-warn">Probing…</span>':'<span class="badge b-warn">Unknown</span>';
    const tools=s.tools&&s.tools.length?s.tools.map(t=>'<code style="font-size:11px">'+esc(t)+'</code>').join(', '):'<span class="empty">—</span>';
    const src=s.source==='agent'?'<span class="badge b-ok">agent</span>':s.source==='mcp.json'?'<span class="badge b-warn">mcp.json</span>':'<span class="badge b-warn">discovered</span>';
    const toggle=en?`<button class="act-btn" onclick="mcpToggle('${esc(s.name)}',false)">Disable</button>`:`<button class="act-btn" onclick="mcpToggle('${esc(s.name)}',true)">Enable</button>`;
    const rowStyle=en?'':'opacity:.5';
    return`<tr style="${rowStyle}"><td><code>${esc(s.name)}</code></td><td><code style="font-size:11px">${esc(s.command)} ${(s.args||[]).map(a=>esc(a)).join(' ')}</code></td><td>${status}</td><td>${tools}</td><td>${src}</td><td>${toggle}</td></tr>`;
  }).join('');
}
window.mcpToggle=async(name,enabled)=>{
  await fetch('/api/mcp/toggle',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,enabled})});
  rMcp();
};
document.getElementById('mcp-probe-btn').onclick=async()=>{
  const btn=document.getElementById('mcp-probe-btn');
  btn.textContent='🔍 Probing…';btn.disabled=true;
  try{
    const servers=await(await fetch('/api/mcp/probe',{method:'POST'})).json();
    _renderMcp(servers);
  }catch(e){alert('Probe failed')}
  btn.textContent='🔍 Probe All';btn.disabled=false;
};
document.getElementById('mcp-sync-btn').onclick=async()=>{
  const btn=document.getElementById('mcp-sync-btn');
  btn.textContent='⚡ Syncing…';btn.disabled=true;
  try{
    const d=await(await fetch('/api/mcp/sync',{method:'POST'})).json();
    const msg=document.getElementById('mcp-sync-msg');
    if(d.added>0){
      msg.textContent='✅ Added '+d.added+' server(s): '+d.servers.join(', ')+'. Run kiroclaw setup --agent-only to apply.';
      msg.style.display='';
      rMcp();
    }else{
      msg.textContent='No new MCP servers found.';
      msg.style.background='var(--warn-subtle)';msg.style.color='var(--warn)';
      msg.style.display='';
    }
    setTimeout(()=>{msg.style.display='none';msg.style.background='';msg.style.color=''},5000);
  }catch(e){alert('Sync failed')}
  btn.textContent='⚡ Auto-Sync';btn.disabled=false;
};

// Agent Config
async function rAgentCfg(){
  try{
    const d=await(await fetch('/api/agent/config')).json();
    document.getElementById('agentcfg-editor').value=JSON.stringify(d,null,2);
  }catch(e){}
}
document.getElementById('agentcfg-save').onclick=async()=>{
  const txt=document.getElementById('agentcfg-editor').value.trim();
  let config;
  try{config=JSON.parse(txt)}catch(e){alert('Invalid JSON');return}
  const r=await fetch('/api/agent/config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({config})});
  if(r.ok){document.getElementById('agentcfg-warn').style.display='';document.getElementById('agentcfg-save').textContent='✓ Saved';setTimeout(()=>document.getElementById('agentcfg-save').textContent='Save',2000)}
};

// Load memory tab on init
ovLoad('memory');

// ── Logs ──
const logBox=document.getElementById('log-box');
let logSSE=null;
let _currentLogLevel='INFO';

// Fetch current log level on init
(async()=>{try{const d=await(await fetch('/api/logs/level')).json();_currentLogLevel=d.level;_syncLogButtons()}catch(e){}})();

function _syncLogButtons(){
  document.querySelectorAll('.log-level-btn').forEach(b=>{
    b.classList.toggle('active',b.dataset.level===_currentLogLevel);
  });
}

// Log level buttons — single-select, changes server-side level
document.querySelectorAll('.log-level-btn').forEach(btn=>{
  btn.onclick=async()=>{
    const lvl=btn.dataset.level;
    if(lvl===_currentLogLevel)return;
    try{
      const r=await fetch('/api/logs/level',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({level:lvl})});
      if(r.ok){_currentLogLevel=lvl;_syncLogButtons();
        // Reconnect log SSE so new level takes effect immediately
        if(logSSE){logSSE.close();logSSE=null;startLogSSE();}
      }
    }catch(e){}
  };
});

startLogSSE(); // Start immediately, not just on tab click
function stopLogSSE(){if(logSSE){logSSE.close();logSSE=null}}
function startLogSSE(){if(logSSE)return;logSSE=new EventSource('/api/logs');
  logSSE.onmessage=e=>{try{const d=JSON.parse(e.data);const ln=document.createElement('div');
    const lvl=d.level||'INFO';
    ln.dataset.level=lvl;
    ln.style.color=lvl==='ERROR'?'var(--danger)':lvl==='WARNING'?'var(--warn)':lvl==='DEBUG'?'var(--muted)':'var(--text)';
    ln.textContent=d.msg;logBox.appendChild(ln);
    if(logBox.children.length>500)logBox.removeChild(logBox.firstChild);
    logBox.scrollTop=logBox.scrollHeight}catch(x){}};
  logSSE.onerror=()=>{logSSE.close();logSSE=null;setTimeout(startLogSSE,3000)}}

// ── Helpers ──
function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function md(t){let h=esc(t);h=h.replace(/```(\w*)\n([\s\S]*?)```/g,'<pre><code>$2</code></pre>');h=h.replace(/`([^`]+)`/g,'<code>$1</code>');h=h.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>');h=h.replace(/\*(.+?)\*/g,'<em>$1</em>');return h}
function safeSetHTML(el,html){if(typeof DOMPurify==='undefined'){el.textContent=html;return;}el.textContent='';const clean=DOMPurify.sanitize(html,{ALLOWED_TAGS:['pre','code','strong','em'],ALLOWED_ATTR:[]});const t=document.createElement('template');t.innerHTML=clean;el.replaceChildren(t.content);}

// ── Multi-session Chat ──
const cMsgs=document.getElementById('chat-msgs'),cIn=document.getElementById('chat-in'),cSend=document.getElementById('chat-send');
const sessList=document.getElementById('sess-list'),histList=document.getElementById('hist-list');
const chatHdr=document.getElementById('chat-hdr'),chatBar=document.getElementById('chat-bar'),noChat=document.getElementById('no-chat');
const btnStop=document.getElementById('btn-stop'),btnDel=document.getElementById('btn-del');
let activeSlot=null;
const _streams={};

function showChatPane(show){
  chatHdr.style.display=show?'flex':'none';
  cMsgs.style.display=show?'flex':'none';
  chatBar.style.display=show?'flex':'none';
  noChat.style.display=show?'none':'flex';
}

// Smart scroll: only auto-scroll if user is near the bottom
let _userScrolledUp=false;
cMsgs.addEventListener('scroll',()=>{
  const atBottom=cMsgs.scrollHeight-cMsgs.scrollTop-cMsgs.clientHeight<80;
  _userScrolledUp=!atBottom;
  if(cMsgs.scrollTop<80&&_slotHasMore&&!_loadingMore&&activeSlot){
    _loadOlderMessages();
  }
});
function _autoScroll(){if(!_userScrolledUp)cMsgs.scrollTop=cMsgs.scrollHeight;}

// Grouped message rendering
function addMsg(role,html){
  const isUser=role==='user';
  const grp=document.createElement('div');
  grp.className='chat-group'+(isUser?' user':'');
  const av=document.createElement('div');
  av.className='chat-avatar '+(isUser?'user':'assistant');
  av.textContent=isUser?'U':'M';
  const msgs=document.createElement('div');
  msgs.className='cg-msgs';
  const m=document.createElement('div');
  m.className='msg '+(isUser?'msg-u':'msg-a');
  if(isUser) m.textContent=html; else safeSetHTML(m,html);
  msgs.appendChild(m);
  grp.appendChild(av);grp.appendChild(msgs);
  cMsgs.appendChild(grp);_autoScroll();
  return m;
}
function addTool(text){const d=document.createElement('div');d.className='msg msg-tool';d.textContent=text;cMsgs.appendChild(d);_autoScroll();return d}
function addQueued(text){const d=document.createElement('div');d.className='msg msg-queued';const em=document.createElement('em');em.textContent='Queued: ';d.appendChild(document.createTextNode('⏳ '));d.appendChild(em);d.appendChild(document.createTextNode(text));cMsgs.appendChild(d);_autoScroll();return d}
function _makeToolInputPre(ti){const pre=document.createElement('pre');pre.style.cssText='background:var(--bg-hover,#1a1a2e);border-radius:6px;padding:8px 12px;font-size:13px;font-family:var(--mono,monospace);white-space:pre-wrap;word-break:break-all;max-height:4.5em;overflow-y:auto;margin:6px 0 0;cursor:pointer';pre.textContent=ti.length>10240?ti.slice(0,10240)+'\n… (truncated)':ti;let expanded=false;pre.onclick=function(){if(window.getSelection()&&!window.getSelection().isCollapsed)return;expanded=!expanded;pre.style.maxHeight=expanded?'60vh':'4.5em'};return pre}
function addApproval(tool,slot,meta){
  const d=document.createElement('div');d.className='msg msg-approval';
  const label=document.createElement('strong');label.textContent=tool;
  d.appendChild(document.createTextNode('🔐 '));d.appendChild(label);d.appendChild(document.createTextNode(' wants to run'));
  const ti=meta&&meta.tool_input;
  if(ti){
    d.appendChild(_makeToolInputPre(ti));
  }
  const row=document.createElement('div');row.style.cssText='margin-top:6px;display:flex;gap:6px;flex-wrap:wrap';
  [['approved','act-btn','✅ Approve'],['trust','act-btn','🤝 Trust'],['rejected','act-btn danger','🚫 Reject']].forEach(([act,cls,txt])=>{
    const b=document.createElement('button');b.className=cls;b.textContent=txt;
    if(act==='trust')b.title='Auto-approve all tools in this session';
    b.onclick=function(){approveSlot(slot,act,this)};row.appendChild(b);
  });
  d.appendChild(row);cMsgs.appendChild(d);_autoScroll();return d;
}
window.approveSlot=async(slot,action,btn)=>{
  const row=btn.closest('.msg-approval');
  row.querySelectorAll('button').forEach(b=>{b.disabled=true});
  try{
    const r=await fetch('/api/chat/slots/'+encodeURIComponent(slot)+'/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
    if(!r.ok)throw new Error(r.status);
  }catch(e){row.querySelectorAll('button').forEach(b=>{b.disabled=false});row.querySelector('.approve-err')?.remove();row.insertAdjacentHTML('beforeend','<div class="approve-err" style="color:var(--danger);font-size:11px">⚠ Failed — try again</div>');return}
  const labels={approved:'✅ Approved',trust:'🤝 Trusted (session)',rejected:'🚫 Rejected'};
  row.textContent=labels[action]||'✅ Approved';
  row.className='msg msg-tool';
  if(action==='trust')_setApprovalMode('trust');
};

// ── Top bar approval mode ──
let _approvalMode='normal';
function _setApprovalMode(mode){
  _approvalMode=mode;
  document.querySelectorAll('.approval-btn').forEach(b=>{
    b.classList.toggle('active',b.dataset.mode===mode);
  });
  // Call dedicated mode endpoint — works without pending approval
  fetch('/api/chat/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})})
    .then(r=>{if(r.ok&&(mode==='trust'||mode==='yolo')){
      document.querySelectorAll('.msg-approval').forEach(el=>{
        el.textContent=mode==='trust'?'🤝 Trusted (session)':'🚀 Auto-approved (YOLO)';
        el.className='msg msg-tool';
      });
    }}).catch(()=>{});
}
document.querySelectorAll('.approval-btn').forEach(btn=>{
  btn.onclick=()=>_setApprovalMode(btn.dataset.mode);
});
function addErr(text){const d=document.createElement('div');d.className='msg msg-err';d.textContent=text;cMsgs.appendChild(d);_autoScroll();return d}
function addThink(){
  const grp=document.createElement('div');grp.className='chat-group';
  const av=document.createElement('div');av.className='chat-avatar assistant';av.textContent='M';
  const msgs=document.createElement('div');msgs.className='cg-msgs';
  const t=document.createElement('div');t.className='thinking';const bar=document.createElement('div');bar.className='think-bar';t.appendChild(bar);t.appendChild(document.createTextNode('Processing…'));
  msgs.appendChild(t);grp.appendChild(av);grp.appendChild(msgs);
  cMsgs.appendChild(grp);_autoScroll();return grp;
}

// Track whether current slot has older messages available
let _slotHasMore=false;
let _slotTotal=0;
let _loadingMore=false;

function renderMessages(msgs){
  cMsgs.innerHTML='';
  // Show "load more" banner if there are older messages
  if(_slotHasMore){
    _addLoadMoreBanner();
  }
  for(const m of msgs){
    if(m.cls==='chunk'||m.cls==='done')continue;
    if(m.role==='tool')addTool('🔧 '+m.content);
    else if(m.role==='queued')addQueued(m.content);
    else if(m.role==='permission'){const ti=m.meta&&m.meta.tool_input;const el=addTool('🔐 '+m.content+' (pending)');if(ti){el.appendChild(_makeToolInputPre(ti))}}
    else if(m.role==='error')addErr(m.content);
    else{const html=m.role==='user'?m.content:md(m.content);addMsg(m.role,html)}
  }
}

function _addLoadMoreBanner(){
  const banner=document.createElement('div');
  banner.className='msg-load-more';
  banner.id='load-more-banner';
  const span=document.createElement('span');span.style.cssText='cursor:pointer;color:var(--accent);font-size:12px;font-weight:500';span.textContent='⬆ Load older messages';banner.appendChild(span);
  banner.style.cssText='text-align:center;padding:10px;border-bottom:1px solid var(--border);cursor:pointer';
  banner.onclick=()=>_loadOlderMessages();
  cMsgs.insertBefore(banner,cMsgs.firstChild);
}

async function _loadOlderMessages(){
  if(_loadingMore||!activeSlot||!_slotHasMore)return;
  _loadingMore=true;
  const banner=document.getElementById('load-more-banner');
  if(banner){banner.textContent='';const s=document.createElement('span');s.style.cssText='color:var(--muted);font-size:12px';s.textContent='Loading…';banner.appendChild(s);}
  try{
    // Count current visible messages to calculate "before" index
    const currentCount=cMsgs.querySelectorAll('.chat-group,.msg-tool,.msg-queued,.msg-err,.msg-approval').length;
    const before=_slotTotal-currentCount;
    if(before<=0){_slotHasMore=false;if(banner)banner.remove();return}
    const d=await fetch('/api/chat/slots/'+encodeURIComponent(activeSlot)+'?limit=100&before='+before).then(r=>r.json());
    if(!d.messages||!d.messages.length){_slotHasMore=false;if(banner)banner.remove();return}
    _slotHasMore=d.has_more;
    // Remember scroll position to maintain view after prepending
    const prevHeight=cMsgs.scrollHeight;
    const prevTop=cMsgs.scrollTop;
    // Remove old banner
    if(banner)banner.remove();
    // Add new banner if still has more
    if(_slotHasMore)_addLoadMoreBanner();
    // Prepend older messages (after banner if present)
    const insertRef=document.getElementById('load-more-banner');
    const afterEl=insertRef?insertRef.nextSibling:cMsgs.firstChild;
    for(const m of d.messages){
      if(m.cls==='chunk'||m.cls==='done')continue;
      let el;
      if(m.role==='tool'){el=document.createElement('div');el.className='msg msg-tool';el.innerHTML='🔧 '+esc(m.content)}
      else if(m.role==='queued'){el=document.createElement('div');el.className='msg msg-queued';el.innerHTML='⏳ <em>Queued:</em> '+esc(m.content)}
      else if(m.role==='error'){el=document.createElement('div');el.className='msg msg-err';el.innerHTML=esc(m.content)}
      else{
        const isUser=m.role==='user';
        const grp=document.createElement('div');
        grp.className='chat-group'+(isUser?' user':'');
        const av=document.createElement('div');
        av.className='chat-avatar '+(isUser?'user':'assistant');
        av.textContent=isUser?'U':'M';
        const msgs=document.createElement('div');
        msgs.className='cg-msgs';
        const msg=document.createElement('div');
        msg.className='msg '+(isUser?'msg-u':'msg-a');
        msg.innerHTML=isUser?esc(m.content):md(m.content);
        msgs.appendChild(msg);grp.appendChild(av);grp.appendChild(msgs);
        el=grp;
      }
      if(el)cMsgs.insertBefore(el,afterEl);
    }
    // Restore scroll position so user doesn't jump
    const newHeight=cMsgs.scrollHeight;
    cMsgs.scrollTop=prevTop+(newHeight-prevHeight);
  }catch(e){console.warn('Load more failed:',e)}
  _loadingMore=false;
}



// ── Session list ──
let _lastSessFingerprint='';
async function refreshSessions(){
  try{
    const slots=await(await fetch('/api/chat/slots')).json();
    const fp=slots.map(s=>s.key+':'+s.title+':'+s.running+':'+(activeSlot===s.key)).join('|');
    if(fp!==_lastSessFingerprint){
      _lastSessFingerprint=fp;
      sessList.innerHTML='';
      for(const s of slots){
        const el=document.createElement('div');
        el.className='sess-item'+(activeSlot===s.key?' active':'');
        const label=s.title&&s.title!==s.key?s.title:s.key;
        el.innerHTML=`${s.running?'<span class="s-dot"></span>':'<span class="s-idle"></span>'}<span class="s-name" title="${esc(s.key)}">${esc(label)}</span><span class="s-close" data-key="${esc(s.key)}">✕</span>`;
        el.onmousedown=e=>{
          e.preventDefault();
          if(e.target.classList.contains('s-close')){deleteSlot(e.target.dataset.key);return}
          switchSlot(s.key);
        };
        sessList.appendChild(el);
      }
    }
    btnStop.style.display=(activeSlot&&slots.find(s=>s.key===activeSlot&&s.running))?'inline-block':'none';
  }catch(e){}
}

let _lastHistFingerprint='';
let _histOffset=0;
let _histHasMore=false;
let _histTotal=0;
async function refreshHistory(append){
  try{
    const limit=30;
    const offset=append?_histOffset:0;
    const d=await(await fetch('/api/sessions?limit='+limit+'&offset='+offset)).json();
    const ss=d.sessions||d;  // backward compat: old API returns array
    _histTotal=d.total||ss.length;
    _histHasMore=d.has_more||false;
    const fp=ss.map(s=>s.key+':'+s.messages).join('|');
    if(!append&&fp===_lastHistFingerprint)return;
    _lastHistFingerprint=fp;
    if(!append)histList.innerHTML='';
    if(!ss.length&&!append){histList.innerHTML='<div style="padding:10px 12px;font-size:12px;color:var(--muted);font-style:italic">No history</div>';return}
    for(const s of ss){
      const el=document.createElement('div');
      el.className='sess-item';
      const src=s.key.startsWith('dashboard')?'🖥':'💬';
      el.innerHTML=`<span class="s-idle"></span><span style="font-size:10px;flex-shrink:0">${src}</span><span class="s-name" title="${esc(s.key)}">${esc(s.title||s.key)}</span><span style="font-size:11px;color:var(--muted);font-family:var(--mono)">${s.messages}</span>`;
      el.onmousedown=e=>{e.preventDefault();resumeHistory(s.key,s.title||s.key)};
      histList.appendChild(el);
    }
    _histOffset=offset+ss.length;
    // Remove old "load more" button if present
    const oldBtn=histList.querySelector('.hist-load-more');
    if(oldBtn)oldBtn.remove();
    // Add "load more" button if there are more sessions
    if(_histHasMore){
      const btn=document.createElement('div');
      btn.className='sess-item hist-load-more';
      btn.style.cssText='justify-content:center;color:var(--accent);font-size:12px;font-weight:500;cursor:pointer';
      btn.textContent='Load more… ('+_histTotal+' total)';
      btn.onmousedown=e=>{e.preventDefault();refreshHistory(true)};
      histList.appendChild(btn);
    }
  }catch(e){}
}

// History toggle (default collapsed)
document.getElementById('hist-toggle').onclick=()=>{
  const list=document.getElementById('hist-list');
  const arrow=document.getElementById('hist-arrow');
  const open=list.classList.toggle('hist-collapsed');
  arrow.classList.toggle('open',!open);
};

let _switchAbort=null;
async function switchSlot(name){
  if(activeSlot===name&&cMsgs.children.length>0)return;
  // Abort any in-flight slot fetch to free HTTP connection immediately
  if(_switchAbort){try{_switchAbort.abort()}catch(x){}_switchAbort=null}
  // Instant UI update — highlight immediately, load data in background
  activeSlot=name;
  showChatPane(true);
  document.getElementById('chat-hdr-title').textContent=name;
  cMsgs.innerHTML='<div style="padding:20px;color:var(--muted)">Loading…</div>';
  // Update session list highlight immediately (no fetch)
  _lastSessFingerprint='';
  document.querySelectorAll('.sess-item').forEach(el=>{
    el.classList.toggle('active',el.querySelector('.s-name')?.title===name||el.querySelector('.s-close')?.dataset.key===name);
  });
  try{
    const ac=new AbortController();_switchAbort=ac;
    const d=await fetch('/api/chat/slots/'+encodeURIComponent(name),{signal:ac.signal}).then(r=>r.json());
    _switchAbort=null;
    if(activeSlot!==name)return; // user switched away while loading
    const title=(d.title&&d.title!==name)?d.title:name;
    document.getElementById('chat-hdr-title').textContent=title;
    // Track pagination state for "load more" on scroll-up
    _slotHasMore=d.has_more||false;
    _slotTotal=d.total||d.messages.length;
    renderMessages(d.messages);
    _userScrolledUp=false;cMsgs.scrollTop=cMsgs.scrollHeight;
    btnStop.style.display=d.running?'inline-block':'none';
    if(_streams[name]){
      _streams[name].aDiv=null;
      if(_streams[name].acc){
        _streams[name].aDiv=addMsg('assistant',md(_streams[name].acc));
        _streams[name].aDiv.classList.add('streaming');
      }
    }
    if(d.running&&!_streams[name])startStream(name);
  }catch(e){}
}

async function createSlot(name){
  try{
    const body=name?{name}:{};
    const d=await(await fetch('/api/chat/slots',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    // SSE push_slots_update() from server will update the sidebar instantly.
    // Switch to the new slot — don't set activeSlot first (switchSlot guard checks it).
    await switchSlot(d.key);
  }catch(e){console.warn('createSlot failed:',e)}
}

async function deleteSlot(name){
  try{await fetch('/api/chat/slots/'+encodeURIComponent(name),{method:'DELETE'})}catch(e){}
  if(_streams[name]){try{_streams[name].reader.cancel()}catch(x){} delete _streams[name]}
  if(activeSlot===name){activeSlot=null;showChatPane(false)}
  refreshSessions();refreshHistory();
}

async function resumeHistory(key,title){
  try{
    const body={name:key,key:key,title:title||key};
    const d=await(await fetch('/api/chat/slots/'+encodeURIComponent(key)+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
    if(!d.ok)return;
    await switchSlot(d.key);
  }catch(e){}
}

// ── Non-blocking streaming ──
function startStream(slotName){
  if(_streams[slotName])return;
  const state={reader:null,aDiv:null,acc:'',think:null};
  _streams[slotName]=state;

  fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:'',slot:slotName})})
  .then(r=>{
    state.reader=r.body.getReader();
    const dec=new TextDecoder();let buf='';
    function pump(){
      state.reader.read().then(({done,value})=>{
        if(done){delete _streams[slotName];refreshSessions();return}
        buf+=dec.decode(value,{stream:true});
        const lines=buf.split('\n');buf=lines.pop();
        const isActive=activeSlot===slotName;
        for(const ln of lines){
          if(!ln.startsWith('data: '))continue;
          const p=ln.slice(6);
          if(p==='[DONE]'){
            if(state.aDiv)state.aDiv.classList.remove('streaming');
            if(state.think)state.think.remove();
            delete _streams[slotName];refreshSessions();return;
          }
          try{const ev=JSON.parse(p);
            if(ev.type==='chunk'){
              state.acc+=ev.content;
              if(isActive){
                if(!state.aDiv){if(state.think)state.think.remove();state.aDiv=addMsg('assistant','');state.aDiv.classList.add('streaming');}
                safeSetHTML(state.aDiv,md(state.acc));_autoScroll();
              }
            }else if(isActive){
              if(ev.type==='tool')addTool('🔧 '+ev.content);
              else if(ev.type==='permission')addApproval(ev.content,slotName,ev.meta);
              else if(ev.type==='queued')addQueued(ev.content);
              else if(ev.type==='user'){
                // Queued message now being processed — start a new message group
                if(state.aDiv){state.aDiv.classList.remove('streaming');state.aDiv=null}
                state.acc='';
                addMsg('user',ev.content);
                state.think=addThink();
              }
              else if(ev.type==='error')addErr(ev.content);
            }
            if(ev.type==='assistant'){if(state.aDiv){state.aDiv.classList.remove('streaming');safeSetHTML(state.aDiv,md(ev.content))}state.aDiv=null;state.acc='';if(state.think){state.think.remove();state.think=null}}
          }catch(x){}
        }
        pump();
      }).catch(()=>{delete _streams[slotName];refreshSessions()});
    }
    pump();
  }).catch(()=>{delete _streams[slotName]});
}

async function send(){
  const txt=cIn.value.trim();if(!txt)return;
  if(!activeSlot)await createSlot();
  const slot=activeSlot;
  cIn.value='';cIn.style.height='auto';_userScrolledUp=false;

  // If already streaming, queue via API (server returns JSON, not SSE).
  // Don't render user msg or queued banner locally — the server emits
  // both a "queued" event (shown immediately by existing pump) and later
  // a "user" event (shown in correct position when queue is processed).
  if(_streams[slot]){
    try{await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:txt,slot:slot})})}catch(e){}
    cIn.focus();return;
  }

  addMsg('user',txt);
  const think=addThink();

  const state={reader:null,aDiv:null,acc:'',think:think,_skipFirstUser:true};
  _streams[slot]=state;
  btnStop.style.display='inline-block';
  refreshSessions();

  try{
    const r=await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:txt,slot:slot})});
    state.reader=r.body.getReader();
    const dec=new TextDecoder();let buf='';
    function pump(){
      state.reader.read().then(({done,value})=>{
        if(done){if(state.think)state.think.remove();delete _streams[slot];refreshSessions();return}
        buf+=dec.decode(value,{stream:true});
        const lines=buf.split('\n');buf=lines.pop();
        const isActive=activeSlot===slot;
        for(const ln of lines){
          if(!ln.startsWith('data: '))continue;
          const p=ln.slice(6);
          if(p==='[DONE]'){
            if(state.aDiv)state.aDiv.classList.remove('streaming');
            if(state.think)state.think.remove();
            delete _streams[slot];refreshSessions();return;
          }
          try{const ev=JSON.parse(p);
            if(ev.type==='chunk'){
              state.acc+=ev.content;
              if(isActive){
                if(!state.aDiv){if(state.think)state.think.remove();state.think=null;state.aDiv=addMsg('assistant','');state.aDiv.classList.add('streaming');}
                safeSetHTML(state.aDiv,md(state.acc));_autoScroll();
              }
            }else if(isActive){
              if(ev.type==='tool')addTool('🔧 '+ev.content);
              else if(ev.type==='permission')addApproval(ev.content,slot,ev.meta);
              else if(ev.type==='queued')addQueued(ev.content);
              else if(ev.type==='user'){
                // Skip the first user event — already rendered locally by send().
                // Subsequent user events come from queued messages being dequeued.
                if(state._skipFirstUser){state._skipFirstUser=false;continue}
                // Queued message now being processed — start a new message group
                if(state.aDiv){state.aDiv.classList.remove('streaming');state.aDiv=null}
                state.acc='';
                addMsg('user',ev.content);
                state.think=addThink();
              }
              else if(ev.type==='error')addErr(ev.content);
            }
            if(ev.type==='assistant'){if(state.aDiv){state.aDiv.classList.remove('streaming');safeSetHTML(state.aDiv,md(ev.content))}state.aDiv=null;state.acc='';if(state.think){state.think.remove();state.think=null}}
          }catch(x){}
        }
        pump();
      }).catch(()=>{if(state.think)state.think.remove();delete _streams[slot];refreshSessions()});
    }
    pump();
  }catch(e){think.remove();addErr('Connection error');delete _streams[slot]}
  cIn.focus();
}

// ── Wiring ──
document.getElementById('sess-new').onclick=()=>createSlot();
cSend.onclick=send;
let _composing=false;cIn.oncompositionstart=()=>{_composing=true};cIn.oncompositionend=()=>{_composing=true;setTimeout(()=>{_composing=false},50)};cIn.onkeydown=e=>{if(e.key==='Enter'&&!e.isComposing&&!_composing){if((e.metaKey||e.ctrlKey)&&e.shiftKey){e.preventDefault();optimizeAndSend()}else if(!e.shiftKey){e.preventDefault();send()}}};
let _optimizing=false;async function optimizeAndSend(){if(_optimizing)return;_optimizing=true;const txt=cIn.value.trim();if(!txt){_optimizing=false;return;}const wrap=cIn.parentElement;const ov=document.createElement('div');ov.className='optimizer-overlay';ov.innerHTML='<span class="optimizer-spinner"></span> Optimizing…';wrap.style.position='relative';wrap.appendChild(ov);let cancelled=false;const esc=e=>{if(e.key==='Escape'){cancelled=true;ov.remove();document.removeEventListener('keydown',esc);_optimizing=false;send()}};document.addEventListener('keydown',esc);try{const msgs=[...document.querySelectorAll('#chat-msgs .msg')].slice(-10).map(m=>m.textContent.slice(0,200)).join('\n');const r=await fetch('/api/optimizer/optimize',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prompt:txt,context:msgs})});if(cancelled)return;const d=await r.json();if(cancelled)return;ov.remove();document.removeEventListener('keydown',esc);if(d.changed&&d.optimized){cIn.value=d.optimized;cIn.style.height='auto';cIn.style.height=Math.min(cIn.scrollHeight,140)+'px';cIn.classList.add('optimized');setTimeout(()=>cIn.classList.remove('optimized'),1500)}send()}catch(e){if(!cancelled){ov.remove();document.removeEventListener('keydown',esc);send()}}finally{_optimizing=false}}
cIn.oninput=()=>{cIn.style.height='auto';cIn.style.height=Math.min(cIn.scrollHeight,140)+'px'};
btnStop.onclick=async()=>{if(activeSlot){try{await fetch('/api/chat/slots/'+encodeURIComponent(activeSlot)+'/stop',{method:'POST'})}catch(e){}btnStop.style.display='none'}};
btnDel.onclick=()=>{if(activeSlot)deleteSlot(activeSlot)};

// ── Init ──
(async()=>{
  try{
    const slots=await(await fetch('/api/chat/slots')).json();
    if(slots.length){activeSlot=slots[0].key;await switchSlot(activeSlot)}
  }catch(e){console.warn('Init slots failed:',e)}
  refreshSessions();refreshHistory();
})();
// Session list updates via SSE push (no polling needed)
// The server pushes a "slots" event whenever a slot is created, deleted,
// starts running, or finishes — so the UI updates instantly.
sse.addEventListener('slots',e=>{try{
  const slots=JSON.parse(e.data);
  _renderSlotList(slots);
}catch(x){}});

function _renderSlotList(slots){
  sessList.innerHTML='';
  for(const s of slots){
    const el=document.createElement('div');
    el.className='sess-item'+(activeSlot===s.key?' active':'');
    const label=s.title&&s.title!==s.key?s.title:s.key;
    el.innerHTML=`${s.running?'<span class="s-dot"></span>':'<span class="s-idle"></span>'}<span class="s-name" title="${esc(s.key)}">${esc(label)}</span><span class="s-close" data-key="${esc(s.key)}">✕</span>`;
    el.onmousedown=e=>{
      e.preventDefault();
      if(e.target.classList.contains('s-close')){deleteSlot(e.target.dataset.key);return}
      switchSlot(s.key);
    };
    sessList.appendChild(el);
  }
  btnStop.style.display=(activeSlot&&slots.find(s=>s.key===activeSlot&&s.running))?'inline-block':'none';
}

// ── SSE refresh hints — server tells us exactly when to fetch fresh data ──
// Replaces all polling intervals (crons, lessons, agents, history, taskrunner).
// The server pushes "event: refresh\ndata: crons,history" when data changes.
sse.addEventListener('refresh',e=>{try{
  const kinds=e.data.split(',');
  for(const k of kinds){
    if(k==='crons')rCrons();
    else if(k==='lessons')rLessons();
    else if(k==='agents')rAgents();
    else if(k==='history'){_lastHistFingerprint='';refreshHistory()}
    else if(k==='taskrunner')rTaskRunner();
  }
}catch(x){}});

// ── Task Runner ──
async function rTaskRunner(){
  try{
    const d=await(await fetch('/api/taskrunner')).json();
    const running=d.running||false;
    const status=d.status||'idle';
    document.getElementById('tr-status').textContent=status;
    document.getElementById('tr-steps').textContent=d.steps||'—';
    document.getElementById('tr-done').textContent=d.completed!=null?d.completed:'—';
    document.getElementById('tr-fail').textContent=d.failed!=null?d.failed:'—';
    document.getElementById('tr-start-btn').style.display=running?'none':'';
    document.getElementById('tr-cancel-btn').style.display=running?'':'none';
    if(d.spec){
      document.getElementById('tr-detail').style.display='';
      const name=d.spec.split('/').pop();
      document.getElementById('tr-spec-name').textContent=name;
      // Build step list from status
      const steps=d.steps||0;
      const completed=d.completed||0;
      const current=d.current_step||0;
      let html='';
      for(let i=1;i<=steps;i++){
        let icon='⬜';
        if(i<current||(i<=completed&&status==='completed'))icon='✅';
        else if(i===current&&running)icon='🔄';
        else if(d.failed>0&&i===current)icon='❌';
        html+=icon+' Step '+i+'/'+steps+(i===current?' ← current':'')+'\n';
      }
      document.getElementById('tr-step-list').innerHTML=html;
      if(d.error){document.getElementById('tr-error').style.display='';document.getElementById('tr-error').textContent='Error: '+d.error}
      else{document.getElementById('tr-error').style.display='none'}
    }else{
      document.getElementById('tr-detail').style.display='none';
    }
  }catch(e){}
}
document.getElementById('tr-start-btn').onclick=async()=>{
  const spec=document.getElementById('tr-spec').value.trim();
  if(!spec){alert('Enter a spec file path');return}
  try{
    const r=await fetch('/api/taskrunner',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({spec})});
    if(!r.ok){const b=await r.json();alert(b.error||'Failed to start')}
    else{document.getElementById('tr-spec').value='';rTaskRunner()}
  }catch(e){alert('Connection error')}
};
document.getElementById('tr-cancel-btn').onclick=async()=>{
  try{await fetch('/api/taskrunner/cancel',{method:'POST'});rTaskRunner()}catch(e){}
};
rTaskRunner();
setInterval(rTaskRunner,3000);
