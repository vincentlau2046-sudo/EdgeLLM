"""
edge_llm/dashboard.py — Self-contained web dashboard for EdgeLLM.

v4.3: Apple-inspired warm light theme. Card-based with gradient icons,
      comfortable contrast, larger fonts, smooth transitions.
"""

import json
import time
import logging

log = logging.getLogger("edge_llm.dashboard")

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EdgeLLM</title>
<style>
:root {
  --blue:    #0A84FF; --blue-s:  rgba(10,132,255,.10); --blue-g: linear-gradient(135deg,#5AC8FA,#0A84FF);
  --green:   #30D158; --green-s: rgba(48,209,88,.10);  --green-g: linear-gradient(135deg,#63E6BE,#30D158);
  --red:     #FF453A; --red-s:   rgba(255,69,58,.10);  --red-g:  linear-gradient(135deg,#FF6B6B,#FF453A);
  --orange:  #FF9F0A; --orange-s:rgba(255,159,10,.10); --orange-g:linear-gradient(135deg,#FFD60A,#FF9F0A);
  --purple:  #BF5AF2; --purple-s:rgba(191,90,242,.10); --purple-g:linear-gradient(135deg,#DA8FFF,#BF5AF2);
  --teal:    #64D2FF;
  --text1:   #1C1C1E; --text2: #48484A; --text3: #8E8E93; --text4: #C7C7CC;
  --bg:      #F2F2F7; --card:  #FFFFFF;
  --border:  rgba(0,0,0,.06);
  --shadow:  0 2px 8px rgba(0,0,0,.04), 0 1px 2px rgba(0,0,0,.03);
  --radius:  16px;
  --font:    -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", sans-serif;
}

* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family:var(--font); background:var(--bg); color:var(--text1);
  -webkit-font-smoothing:antialiased; font-size:16px; line-height:1.5;
}

/* ── Nav ── */
.nav {
  position:sticky; top:0; z-index:50;
  background:rgba(242,242,247,.78);
  backdrop-filter:saturate(180%) blur(20px);
  -webkit-backdrop-filter:saturate(180%) blur(20px);
  border-bottom:1px solid var(--border);
}
.nav-in { max-width:1000px; margin:0 auto; display:flex; align-items:center; justify-content:space-between; height:52px; padding:0 28px; }
.nav-l { display:flex; align-items:center; gap:12px; }
.nav-logo {
  width:34px; height:34px; border-radius:9px;
  background:var(--blue-g);
  display:flex; align-items:center; justify-content:center;
  font-size:17px; font-weight:800; color:#fff; letter-spacing:-.03em;
  box-shadow:0 2px 8px rgba(10,132,255,.25);
}
.nav-title { font-size:18px; font-weight:700; letter-spacing:-.02em; color:var(--text1); }
.nav-r { display:flex; align-items:center; gap:14px; }
.nav-ver { font-size:13px; color:var(--text4); font-weight:500; }
.nav-ts  { font-size:13px; color:var(--text3); font-variant-numeric:tabular-nums; }

/* ── Tag ── */
.tag { display:inline-flex; align-items:center; gap:5px; padding:4px 14px; border-radius:20px; font-size:13px; font-weight:600; transition:all .3s; }
.tag .dot { width:7px; height:7px; border-radius:50%; transition:background .3s; }
.tag.idle { background:rgba(142,142,147,.1); color:var(--text3); }
.tag.idle .dot { background:var(--text3); }
.tag.exclusive { background:var(--red-s); color:var(--red); }
.tag.exclusive .dot { background:var(--red); }
.tag.shared { background:var(--green-s); color:var(--green); }
.tag.shared .dot { background:var(--green); }

/* ── Main ── */
.main { max-width:1000px; margin:0 auto; padding:24px 28px 48px; }

/* ── Status Bar ── */
.status-bar {
  display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px;
  margin-bottom:20px;
}
.stat-card {
  background:var(--card); border-radius:var(--radius);
  border:1px solid var(--border); box-shadow:var(--shadow);
  padding:16px 20px; display:flex; align-items:center; gap:14px;
  transition:box-shadow .2s;
}
.stat-card:hover { box-shadow:0 4px 16px rgba(0,0,0,.06); }
.stat-icon {
  width:44px; height:44px; border-radius:12px;
  display:flex; align-items:center; justify-content:center;
  font-size:20px; flex-shrink:0; color:#fff;
}
.stat-icon.gpu  { background:var(--blue-g);   box-shadow:0 2px 8px rgba(10,132,255,.2); }
.stat-icon.ram  { background:var(--purple-g); box-shadow:0 2px 8px rgba(191,90,242,.2); }
.stat-icon.cpu  { background:var(--orange-g); box-shadow:0 2px 8px rgba(255,159,10,.2); }
.stat-body { flex:1; min-width:0; }
.stat-label { font-size:13px; font-weight:500; color:var(--text3); margin-bottom:4px; }
.stat-row { display:flex; align-items:baseline; gap:6px; margin-bottom:8px; }
.stat-val { font-size:26px; font-weight:700; letter-spacing:-.03em; color:var(--text1); line-height:1; }
.stat-unit { font-size:14px; font-weight:500; color:var(--text3); }
.stat-bar { height:5px; border-radius:3px; background:var(--bg); overflow:hidden; }
.stat-bar-f {
  height:100%; border-radius:3px;
  transition:width .8s cubic-bezier(.4,0,.2,1), background .4s;
}
.stat-sub { font-size:12px; color:var(--text4); margin-top:5px; font-variant-numeric:tabular-nums; }
.stat-sub b { color:var(--text2); font-weight:600; }

/* ── Two-column panels ── */
.panels { display:grid; grid-template-columns:1fr 1fr; gap:14px; margin-bottom:18px; }
.panel {
  background:var(--card); border-radius:var(--radius);
  border:1px solid var(--border); box-shadow:var(--shadow);
  padding:20px; transition:box-shadow .2s;
}
.panel:hover { box-shadow:0 4px 16px rgba(0,0,0,.06); }
.panel-hdr {
  display:flex; align-items:center; gap:8px; margin-bottom:16px;
}
.panel-icon {
  width:28px; height:28px; border-radius:7px;
  display:flex; align-items:center; justify-content:center;
  font-size:14px; color:#fff;
}
.panel-icon.excl { background:var(--red-g);  box-shadow:0 2px 6px rgba(255,69,58,.2); }
.panel-icon.shrd { background:var(--green-g); box-shadow:0 2px 6px rgba(48,209,88,.2); }
.panel-title { font-size:15px; font-weight:600; color:var(--text1); letter-spacing:-.01em; }

/* ── Model Card ── */
.model-card {
  background:var(--bg); border:1.5px solid transparent;
  border-radius:12px; padding:14px 16px; margin-bottom:8px;
  cursor:pointer; transition:all .2s;
}
.model-card:hover {
  border-color:var(--blue);
  background:rgba(10,132,255,.04);
  transform:translateY(-1px);
  box-shadow:0 2px 8px rgba(10,132,255,.08);
}
.model-card.active {
  border-color:var(--green);
  background:var(--green-s);
}
.model-card.active:hover {
  border-color:var(--green);
  background:rgba(48,209,88,.15);
}
.model-top { display:flex; align-items:center; gap:10px; }
.model-dot {
  width:8px; height:8px; border-radius:50%; flex-shrink:0;
  background:var(--text4); transition:all .3s;
}
.model-card.active .model-dot { background:var(--green); box-shadow:0 0 0 3px rgba(48,209,88,.2); }
.model-info { flex:1; min-width:0; }
.model-name { font-size:15px; font-weight:600; color:var(--text1); letter-spacing:-.01em; }
.model-desc { font-size:12px; color:var(--text3); margin-top:2px; }
.model-badge {
  padding:3px 10px; border-radius:8px;
  font-size:11px; font-weight:600; letter-spacing:.02em;
}
.model-badge.excl { background:var(--red-s); color:var(--red); }
.model-badge.shrd { background:var(--green-s); color:var(--green); }

/* ── Card Actions ── */
.model-actions {
  display:flex; gap:6px; margin-top:10px;
}
.btn-card {
  flex:1; padding:7px 0; border:none; border-radius:8px;
  font-size:13px; font-weight:600; cursor:pointer;
  transition:all .15s; letter-spacing:-.01em;
}
.btn-card.start { background:var(--green-s); color:var(--green); }
.btn-card.start:hover { background:rgba(48,209,88,.18); }
.btn-card.start:disabled { opacity:.3; cursor:default; }
.btn-card.stop  { background:var(--red-s); color:var(--red); }
.btn-card.stop:hover  { background:rgba(255,69,58,.18); }
.btn-card.stop:disabled  { opacity:.3; cursor:default; }

/* ── Idle Card ── */
.idle-card {
  background:transparent; border:1.5px dashed var(--text4);
  border-radius:12px; padding:14px 16px; margin-bottom:8px;
  cursor:pointer; transition:all .2s;
  display:flex; align-items:center; gap:10px;
}
.idle-card:hover { background:var(--bg); border-color:var(--text3); }
.idle-card-icon { font-size:18px; }
.idle-card-text { font-size:14px; font-weight:500; color:var(--text3); }

/* ── Action Row ── */
.act-row { display:flex; gap:8px; margin-bottom:18px; }
.act-btn {
  padding:10px 20px; border:none; border-radius:10px;
  font-size:14px; font-weight:600; cursor:pointer;
  transition:all .15s; letter-spacing:-.01em;
}
.act-btn.pri { background:var(--blue); color:#fff; box-shadow:0 2px 8px rgba(10,132,255,.2); }
.act-btn.pri:hover { background:#0070E0; }
.act-btn.sec { background:var(--card); color:var(--text2); border:1px solid var(--border); }
.act-btn.sec:hover { background:var(--bg); }
.act-btn.warn { background:var(--red-s); color:var(--red); }
.act-btn.warn:hover { background:rgba(255,69,58,.18); }

/* ── History ── */
.hist-card {
  background:var(--card); border-radius:var(--radius);
  border:1px solid var(--border); box-shadow:var(--shadow);
  padding:20px; transition:box-shadow .2s;
}
.hist-card:hover { box-shadow:0 4px 16px rgba(0,0,0,.06); }
.hist-scroll { max-height:220px; overflow-y:auto; }
.hist-scroll::-webkit-scrollbar { width:5px; }
.hist-scroll::-webkit-scrollbar-track { background:transparent; }
.hist-scroll::-webkit-scrollbar-thumb { background:var(--text4); border-radius:2.5px; }
.hrow {
  display:grid; grid-template-columns:68px 1fr 24px 1fr 56px 24px;
  align-items:center; gap:4px; padding:8px 4px; font-size:13px;
  border-bottom:1px solid var(--bg);
  transition:background .15s;
}
.hrow:hover { background:var(--bg); border-radius:6px; }
.hrow:last-child { border-bottom:none; }
.hrow-hdr { font-weight:600; color:var(--text4); font-size:11px; text-transform:uppercase; letter-spacing:.04em; border-bottom:none; }
.hrow-hdr:hover { background:transparent; }
.h-time { color:var(--text3); font-variant-numeric:tabular-nums; }
.h-from { color:var(--red); font-weight:600; }
.h-arrow { color:var(--text4); text-align:center; }
.h-to   { color:var(--green); font-weight:600; }
.h-dur  { font-variant-numeric:tabular-nums; font-weight:600; color:var(--text1); }
.h-ok   { color:var(--green); }
.h-err  { color:var(--red); }

/* ── Toast ── */
.toast {
  position:fixed; bottom:24px; right:24px; z-index:200;
  padding:12px 20px; border-radius:12px;
  font-size:14px; font-weight:600;
  transform:translateY(80px); opacity:0;
  transition:all .35s cubic-bezier(.4,0,.2,1);
  box-shadow:0 4px 20px rgba(0,0,0,.1);
  max-width:340px;
}
.toast.show { transform:translateY(0); opacity:1; }
.toast.ok   { background:var(--green-s); color:var(--green); border:1px solid rgba(48,209,88,.2); }
.toast.err  { background:var(--red-s);   color:var(--red);   border:1px solid rgba(255,69,58,.2); }
.toast.info { background:var(--blue-s);  color:var(--blue);  border:1px solid rgba(10,132,255,.2); }

@media (max-width:700px) {
  .panels { grid-template-columns:1fr; }
  .status-bar { grid-template-columns:1fr; }
  .main { padding:16px; }
}
</style>
</head>
<body>

<div class="nav">
  <div class="nav-in">
    <div class="nav-l">
      <div class="nav-logo">E</div>
      <span class="nav-title">EdgeLLM</span>
      <span class="tag idle" id="sTag"><span class="dot"></span><span id="sTxt">idle</span></span>
    </div>
    <div class="nav-r">
      <span class="nav-ver">v4.3</span>
      <span class="nav-ts" id="ts">—</span>
    </div>
  </div>
</div>

<div class="main">

  <!-- Status Cards -->
  <div class="status-bar">
    <div class="stat-card">
      <div class="stat-icon gpu">💾</div>
      <div class="stat-body">
        <div class="stat-label">GPU 显存</div>
        <div class="stat-row">
          <span class="stat-val" id="gP">0</span><span class="stat-unit">%</span>
        </div>
        <div class="stat-bar"><div class="stat-bar-f" id="gB" style="width:0%;background:var(--blue)"></div></div>
        <div class="stat-sub"><b id="gU">0</b> / <span id="gT">32,607</span> MB</div>
      </div>
    </div>
    <div class="stat-card">
      <div class="stat-icon ram">🧠</div>
      <div class="stat-body">
        <div class="stat-label">系统内存</div>
        <div class="stat-row">
          <span class="stat-val" id="rP">0</span><span class="stat-unit">%</span>
        </div>
        <div class="stat-bar"><div class="stat-bar-f" id="rB" style="width:0%;background:var(--purple)"></div></div>
        <div class="stat-sub"><b id="rU">0</b> / <span id="rT">—</span> GB</div>
      </div>
    </div>
    <div class="stat-card">
      <div class="stat-icon cpu">⚡</div>
      <div class="stat-body">
        <div class="stat-label">CPU 负载</div>
        <div class="stat-row">
          <span class="stat-val" id="cP">0</span><span class="stat-unit">%</span>
        </div>
        <div class="stat-bar"><div class="stat-bar-f" id="cB" style="width:0%;background:var(--orange)"></div></div>
        <div class="stat-sub"><span id="cC">—</span> 核心 · 运行 <span id="cU">—</span></div>
      </div>
    </div>
  </div>

  <!-- Model Panels -->
  <div class="panels">
    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-icon excl">🔒</div>
        <span class="panel-title">独占模型</span>
      </div>
      <div id="exclList"></div>
    </div>
    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-icon shrd">🔓</div>
        <span class="panel-title">共享服务</span>
      </div>
      <div id="shrdList"></div>
    </div>
  </div>

  <!-- Actions -->
  <div class="act-row">
    <button class="act-btn pri" onclick="doSwitch('idle')">释放 GPU</button>
    <button class="act-btn sec" onclick="doReconcile()">Reconcile</button>
    <button class="act-btn warn" onclick="doReset()">强制重置</button>
  </div>

  <!-- History -->
  <div class="hist-card">
    <div class="panel-hdr">
      <div class="panel-icon" style="background:var(--purple-g);box-shadow:0 2px 6px rgba(191,90,242,.2)">🕐</div>
      <span class="panel-title">切换历史</span>
    </div>
    <div class="hist-scroll">
      <div class="hrow hrow-hdr"><span>时间</span><span>来源</span><span></span><span>目标</span><span>耗时</span><span></span></div>
      <div id="hBody"><div style="text-align:center;padding:20px;color:var(--text4);font-size:14px">加载中…</div></div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let sw = false;
function toast(m,t) {
  const e=document.getElementById('toast');
  e.textContent=m; e.className='toast '+t+' show';
  clearTimeout(e._t); e._t=setTimeout(()=>e.classList.remove('show'),2800);
}
async function j(p,o) { return (await fetch(p,o)).json(); }

async function load() {
  const [s,sys,hist]=await Promise.all([j('/status'),j('/system').catch(()=>({})),j('/history').catch(()=>[])]);
  const gm=s.gpu_mode||'idle';
  const labels={idle:'idle',exclusive:'exclusive',shared:'shared'};
  const tag=document.getElementById('sTag');
  tag.className='tag '+gm;
  document.getElementById('sTxt').textContent=labels[gm]||gm;

  // GPU
  const gt=s.gpu_total_mb||32607,gu=s.gpu_used_mb||0,gp=(gu/gt*100);
  document.getElementById('gP').textContent=gp.toFixed(1);
  document.getElementById('gU').textContent=gu.toLocaleString();
  document.getElementById('gT').textContent=gt.toLocaleString();
  document.getElementById('gB').style.width=gp.toFixed(1)+'%';
  document.getElementById('gB').style.background=gp<50?'var(--blue)':gp<80?'var(--orange)':'var(--red)';

  // RAM
  const rt=sys.ram_total_gb||1,ru=sys.ram_used_gb||0,rp=(ru/rt*100);
  document.getElementById('rP').textContent=rp.toFixed(1);
  document.getElementById('rU').textContent=ru.toFixed(1);
  document.getElementById('rT').textContent=rt.toFixed(1);
  document.getElementById('rB').style.width=rp.toFixed(1)+'%';

  // CPU
  const cp=sys.cpu_percent||0;
  document.getElementById('cP').textContent=cp.toFixed(1);
  document.getElementById('cC').textContent=sys.cpu_cores||'—';
  document.getElementById('cB').style.width=cp.toFixed(1)+'%';
  const us=sys.uptime_seconds||0;
  document.getElementById('cU').textContent=Math.floor(us/3600)+'h '+Math.floor((us%3600)/60)+'m';

  // History
  const hBody=document.getElementById('hBody');
  if(!hist||!hist.length){hBody.innerHTML='<div style="text-align:center;padding:20px;color:var(--text4);font-size:14px">暂无记录</div>';}
  else{hBody.innerHTML=hist.slice(0,12).map(h=>{
    const t=h.timestamp?new Date(h.timestamp):new Date();
    const ts=t.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
    const d=h.duration!=null?h.duration.toFixed(1)+'s':'—';
    const st=h.status==='ok'?'<span class="h-ok">✓</span>':'<span class="h-err">✗</span>';
    return '<div class="hrow"><span class="h-time">'+ts+'</span><span class="h-from">'+(h.from||'—')+'</span><span class="h-arrow">→</span><span class="h-to">'+h.to+'</span><span class="h-dur">'+d+'</span><span>'+st+'</span></div>';
  }).join('');}

  document.getElementById('ts').textContent=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

async function loadModels() {
  const models=await j('/models');
  const excl=models.filter(m=>m.mode==='exclusive');
  const shrd=models.filter(m=>m.mode==='shared');

  // Exclusive
  const eList=document.getElementById('exclList');
  let eh='';
  for(const m of excl){
    const a=m.active;
    eh+='<div class="model-card'+(a?' active':'')+'" id="sw-'+m.name+'" onclick="doSwitch(\''+m.name+'\')">';
    eh+='<div class="model-top">';
    if(a) eh+='<div class="model-dot"></div>';
    else eh+='<div class="model-dot"></div>';
    eh+='<div class="model-info"><div class="model-name">'+m.name+'</div><div class="model-desc">'+(m.description||'')+'</div></div>';
    eh+='<span class="model-badge excl">独占</span>';
    eh+='</div></div>';
  }
  eList.innerHTML=eh;

  // Shared
  const sList=document.getElementById('shrdList');
  let sh='';
  for(const m of shrd){
    const a=m.active;
    sh+='<div class="model-card'+(a?' active':'')+'" id="sw-'+m.name+'">';
    sh+='<div class="model-top">';
    sh+='<div class="model-dot"></div>';
    sh+='<div class="model-info"><div class="model-name">'+m.name+'</div><div class="model-desc">'+(m.description||'')+'</div></div>';
    sh+='<span class="model-badge shrd">共享</span>';
    sh+='</div>';
    sh+='<div class="model-actions">';
    sh+='<button class="btn-card stop"'+(a?'':' disabled')+' onclick="event.stopPropagation();doStop(\''+m.name+'\')">停止</button>';
    sh+='<button class="btn-card start"'+(a?' disabled':'')+' onclick="event.stopPropagation();doSwitch(\''+m.name+'\')">启动</button>';
    sh+='</div></div>';
  }
  sList.innerHTML=sh;
}

async function doSwitch(n) {
  if(sw)return; sw=true;
  try{
    const r=await j('/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if(r.status==='switched') toast(n+' ✓ '+r.elapsed_sec+'s','ok');
    else if(r.status==='already_active') toast('已在 '+n,'info');
    else toast(r.message||'失败','err');
  }catch(e){toast(e.message,'err');}
  sw=false;
  await Promise.all([load(),loadModels()]);
}

async function doStop(n) {
  if(sw)return; sw=true;
  try{
    const r=await j('/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if(r.status==='stopped') toast(n+' 已停止','ok');
    else if(r.status==='already_stopped') toast(n+' 未运行','info');
    else toast(r.message||'停止失败','err');
  }catch(e){toast(e.message,'err');}
  sw=false;
  await Promise.all([load(),loadModels()]);
}

async function doReset() {
  if(!confirm('强制重置到 idle？'))return;
  const r=await j('/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  toast(r.status==='reset'?'已重置 ✓':'失败',r.status==='reset'?'ok':'err');
  await Promise.all([load(),loadModels()]);
}

async function doReconcile() {
  const r=await j('/reconcile',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const a=r.actions||[];
  toast(a.length===0?'状态一致 ✓':'修复: '+a.join('; '),'ok');
  await Promise.all([load(),loadModels()]);
}

Promise.all([load(),loadModels()]);
setInterval(()=>{load();loadModels();},5000);
</script>
</body>
</html>"""