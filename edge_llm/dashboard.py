"""
edge_llm/dashboard.py — Self-contained web dashboard for EdgeLLM.

v4.2: Apple HIG light-theme. Status bar, side-by-side model panels,
      proper contrast ratios, clean typography hierarchy.
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
/* ── Apple HIG Tokens ── */
:root {
  --blue:    #007AFF; --blue-bg:  #E8F0FE;
  --green:   #34C759; --green-bg: #E8F8ED;
  --red:     #FF3B30; --red-bg:   #FEDEDE;
  --orange:  #FF9500; --orange-bg:#FFF0DB;
  --purple:  #AF52DE; --purple-bg:#F1E5FA;
  --teal:    #5AC8FA; --teal-bg:  #E3F5FD;
  --gray1:   #8E8E93; --gray2:   #AEAEB2; --gray3: #C7C7CC;
  --gray4:   #D1D1D6; --gray5:   #E5E5EA; --gray6: #F2F2F7;
  --text1:   #1D1D1F; --text2:   #3C3C43; --text3:  #86868B; --text4: #AEAEB2;
  --bg:      #F5F5F7; --card:    #FFFFFF;
  --radius:  12px;
  --font:    -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "Helvetica Neue", sans-serif;
}

* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:var(--font); background:var(--bg); color:var(--text1); -webkit-font-smoothing:antialiased; font-size:14px; line-height:1.47; }

/* ── Nav ── */
.nav {
  position:sticky; top:0; z-index:50;
  background:rgba(245,245,247,.82);
  backdrop-filter:saturate(180%) blur(20px);
  -webkit-backdrop-filter:saturate(180%) blur(20px);
  border-bottom:.5px solid var(--gray5);
}
.nav-in { max-width:960px; margin:0 auto; display:flex; align-items:center; justify-content:space-between; height:44px; padding:0 24px; }
.nav-l { display:flex; align-items:center; gap:10px; }
.nav-logo { width:28px; height:28px; border-radius:7px; background:var(--blue); display:flex; align-items:center; justify-content:center; font-size:14px; font-weight:700; color:#fff; }
.nav-t { font-size:15px; font-weight:600; letter-spacing:-.02em; }
.nav-r { display:flex; align-items:center; gap:12px; }
.nav-ver { font-size:11px; color:var(--text4); font-weight:500; }
.nav-ts  { font-size:11px; color:var(--text3); font-variant-numeric:tabular-nums; }

/* ── Tag ── */
.tag { display:inline-flex; align-items:center; gap:4px; padding:2px 10px; border-radius:9px; font-size:12px; font-weight:600; }
.tag .dot { width:6px; height:6px; border-radius:50%; }
.tag.idle { background:var(--gray6); color:var(--gray1); }
.tag.idle .dot { background:var(--gray1); }
.tag.exclusive { background:var(--red-bg); color:var(--red); }
.tag.exclusive .dot { background:var(--red); }
.tag.shared { background:var(--green-bg); color:var(--green); }
.tag.shared .dot { background:var(--green); }

/* ── Main ── */
.main { max-width:960px; margin:0 auto; padding:20px 24px 40px; }

/* ── Status Strip ── */
.strip {
  display:flex; align-items:center; gap:20px;
  padding:10px 18px; border-radius:var(--radius);
  background:var(--card); border:.5px solid var(--gray5);
  margin-bottom:16px;
}
.strip-item { display:flex; align-items:center; gap:8px; }
.strip-label { font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.04em; color:var(--text4); }
.strip-val { font-size:13px; font-weight:600; color:var(--text1); font-variant-numeric:tabular-nums; }
.strip-bar { width:80px; height:4px; border-radius:2px; background:var(--gray6); overflow:hidden; }
.strip-bar-f { height:100%; border-radius:2px; transition:width .6s cubic-bezier(.4,0,.2,1); }
.strip-sep { width:.5px; height:20px; background:var(--gray5); flex-shrink:0; }

/* ── Section ── */
.sec { margin-bottom:16px; }
.sec-title { font-size:13px; font-weight:600; color:var(--text3); margin-bottom:8px; letter-spacing:-.01em; }

/* ── Two-column panels ── */
.panels { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:16px; }
.panel {
  background:var(--card); border:.5px solid var(--gray5); border-radius:var(--radius);
  padding:16px;
}
.panel-title {
  font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.05em;
  color:var(--text4); margin-bottom:12px;
  display:flex; align-items:center; gap:6px;
}
.panel-title .icon { font-size:13px; }

/* ── Model Item ── */
.model-item {
  display:flex; align-items:center; gap:10px;
  padding:10px 12px; border-radius:10px;
  margin-bottom:4px; cursor:pointer;
  transition:background .15s;
  border:.5px solid transparent;
}
.model-item:hover { background:var(--gray6); }
.model-item.active {
  background:var(--blue-bg); border-color:rgba(0,122,255,.15);
}
.model-info { flex:1; min-width:0; }
.model-name { font-size:14px; font-weight:600; color:var(--text1); letter-spacing:-.01em; }
.model-desc { font-size:11px; color:var(--text3); margin-top:1px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.model-badge {
  padding:2px 8px; border-radius:6px;
  font-size:10px; font-weight:600; letter-spacing:.02em;
}
.model-badge.excl { background:var(--red-bg); color:var(--red); }
.model-badge.shrd { background:var(--green-bg); color:var(--green); }
.model-active-dot {
  width:8px; height:8px; border-radius:50%; background:var(--green);
  box-shadow:0 0 0 3px rgba(52,199,89,.2); flex-shrink:0;
}

/* ── Item Actions (shared services) ── */
.item-actions { display:flex; gap:4px; margin-left:8px; }
.btn-sm {
  padding:4px 12px; border:none; border-radius:7px;
  font-size:11px; font-weight:600; cursor:pointer;
  transition:all .15s; letter-spacing:-.01em;
}
.btn-sm.start { background:var(--green-bg); color:var(--green); }
.btn-sm.start:hover { background:#D4F0DD; }
.btn-sm.start:disabled { opacity:.35; cursor:default; }
.btn-sm.stop  { background:var(--red-bg); color:var(--red); }
.btn-sm.stop:hover  { background:#F8CFCF; }
.btn-sm.stop:disabled  { opacity:.35; cursor:default; }

/* ── Idle Item ── */
.idle-item {
  display:flex; align-items:center; gap:10px;
  padding:10px 12px; border-radius:10px;
  margin-bottom:4px; cursor:pointer;
  border:1px dashed var(--gray4);
  transition:all .15s;
}
.idle-item:hover { background:var(--gray6); border-color:var(--gray3); }
.idle-icon { font-size:16px; }
.idle-text { font-size:13px; font-weight:500; color:var(--text3); }

/* ── Action Row ── */
.act-row { display:flex; gap:8px; margin-bottom:16px; }
.act-btn {
  padding:8px 16px; border:none; border-radius:8px;
  font-size:12px; font-weight:600; cursor:pointer;
  transition:all .12s; letter-spacing:-.01em;
}
.act-btn.sec { background:var(--gray6); color:var(--text2); }
.act-btn.sec:hover { background:var(--gray5); }
.act-btn.warn { background:var(--red-bg); color:var(--red); }
.act-btn.warn:hover { background:#F8CFCF; }

/* ── History ── */
.hist-scroll { max-height:200px; overflow-y:auto; }
.hist-scroll::-webkit-scrollbar { width:5px; }
.hist-scroll::-webkit-scrollbar-track { background:transparent; }
.hist-scroll::-webkit-scrollbar-thumb { background:var(--gray4); border-radius:2.5px; }
.hrow {
  display:grid; grid-template-columns:60px 1fr 20px 1fr 52px 20px;
  align-items:center; gap:4px; padding:7px 12px; font-size:12px;
  border-bottom:.5px solid var(--gray6);
}
.hrow:last-child { border-bottom:none; }
.hrow-hdr { font-weight:600; color:var(--text4); font-size:10px; text-transform:uppercase; letter-spacing:.04em; border-bottom:none; }
.h-time { color:var(--text3); font-variant-numeric:tabular-nums; }
.h-from { color:var(--red); font-weight:600; }
.h-arrow { color:var(--gray3); text-align:center; }
.h-to { color:var(--green); font-weight:600; }
.h-dur { font-variant-numeric:tabular-nums; font-weight:600; color:var(--text1); }
.h-ok { color:var(--green); }
.h-err { color:var(--red); }

/* ── Toast ── */
.toast {
  position:fixed; bottom:20px; right:20px; z-index:200;
  padding:10px 18px; border-radius:10px;
  font-size:13px; font-weight:600;
  transform:translateY(80px); opacity:0;
  transition:all .3s cubic-bezier(.4,0,.2,1);
  box-shadow:0 4px 16px rgba(0,0,0,.1);
  max-width:320px;
}
.toast.show { transform:translateY(0); opacity:1; }
.toast.ok   { background:var(--green-bg); color:var(--green); border:1px solid var(--green); }
.toast.err  { background:var(--red-bg);   color:var(--red);   border:1px solid var(--red); }
.toast.info { background:var(--blue-bg);  color:var(--blue);  border:1px solid var(--blue); }

@media (max-width:700px) {
  .panels { grid-template-columns:1fr; }
  .strip { flex-wrap:wrap; gap:12px; }
  .strip-sep { display:none; }
}
</style>
</head>
<body>

<div class="nav">
  <div class="nav-in">
    <div class="nav-l">
      <div class="nav-logo">E</div>
      <span class="nav-t">EdgeLLM</span>
      <span class="tag idle" id="sTag"><span class="dot"></span><span id="sTxt">idle</span></span>
    </div>
    <div class="nav-r">
      <span class="nav-ver">v4.2</span>
      <span class="nav-ts" id="ts">—</span>
    </div>
  </div>
</div>

<div class="main">

  <!-- Status Strip -->
  <div class="strip">
    <div class="strip-item">
      <span class="strip-label">GPU</span>
      <span class="strip-val" id="gP">0%</span>
      <div class="strip-bar"><div class="strip-bar-f" id="gB" style="width:0%;background:var(--blue)"></div></div>
    </div>
    <div class="strip-sep"></div>
    <div class="strip-item">
      <span class="strip-label">RAM</span>
      <span class="strip-val" id="rP">0%</span>
      <div class="strip-bar"><div class="strip-bar-f" id="rB" style="width:0%;background:var(--purple)"></div></div>
    </div>
    <div class="strip-sep"></div>
    <div class="strip-item">
      <span class="strip-label">CPU</span>
      <span class="strip-val" id="cP">0%</span>
      <div class="strip-bar"><div class="strip-bar-f" id="cB" style="width:0%;background:var(--orange)"></div></div>
    </div>
    <div class="strip-sep"></div>
    <div class="strip-item">
      <span class="strip-label">服务</span>
      <span class="strip-val" id="svcTxt">—</span>
    </div>
  </div>

  <!-- Model Panels: Exclusive | Shared -->
  <div class="panels">
    <div class="panel" id="panelExcl">
      <div class="panel-title"><span class="icon">🔒</span> 独占模型</div>
      <div id="exclList"></div>
    </div>
    <div class="panel" id="panelShrd">
      <div class="panel-title"><span class="icon">🔓</span> 共享服务</div>
      <div id="shrdList"></div>
    </div>
  </div>

  <!-- Actions -->
  <div class="act-row">
    <button class="act-btn sec" onclick="doSwitch('idle')">释放 GPU</button>
    <button class="act-btn sec" onclick="doReconcile()">Reconcile</button>
    <button class="act-btn warn" onclick="doReset()">强制重置</button>
  </div>

  <!-- History -->
  <div class="panel" style="padding:12px 0 0">
    <div class="panel-title" style="padding:0 14px"><span class="icon">🕐</span> 切换历史</div>
    <div class="hist-scroll">
      <div class="hrow hrow-hdr">
        <span>时间</span><span>来源</span><span></span><span>目标</span><span>耗时</span><span></span>
      </div>
      <div id="hBody"><div style="text-align:center;padding:16px;color:var(--text4);font-size:12px">加载中…</div></div>
    </div>
  </div>

</div>

<div class="toast" id="toast"></div>

<script>
let sw = false;

function toast(m, t) {
  const e = document.getElementById('toast');
  e.textContent = m; e.className = 'toast ' + t + ' show';
  clearTimeout(e._t); e._t = setTimeout(() => e.classList.remove('show'), 2500);
}

async function j(p, o) {
  const r = await fetch(p, o);
  return r.json();
}

async function load() {
  const [s, sys, hist] = await Promise.all([
    j('/status'), j('/system').catch(()=>({})), j('/history').catch(()=>[])
  ]);

  const gm = s.gpu_mode || 'idle';
  const labels = {idle:'idle', exclusive:'exclusive', shared:'shared'};

  // Nav tag
  const tag = document.getElementById('sTag');
  tag.className = 'tag ' + gm;
  document.getElementById('sTxt').textContent = labels[gm] || gm;

  // Strip — GPU
  const gt=s.gpu_total_mb||32607, gu=s.gpu_used_mb||0, gp=(gu/gt*100);
  document.getElementById('gP').textContent = gp.toFixed(1)+'%';
  document.getElementById('gB').style.width = gp.toFixed(1)+'%';
  document.getElementById('gB').style.background = gp<50?'var(--blue)':gp<80?'var(--orange)':'var(--red)';

  // Strip — RAM
  const rt=sys.ram_total_gb||1, ru=sys.ram_used_gb||0, rp=(ru/rt*100);
  document.getElementById('rP').textContent = rp.toFixed(1)+'%';
  document.getElementById('rB').style.width = rp.toFixed(1)+'%';

  // Strip — CPU
  const cp=sys.cpu_percent||0;
  document.getElementById('cP').textContent = cp.toFixed(1)+'%';
  document.getElementById('cB').style.width = cp.toFixed(1)+'%';

  // Strip — Services
  const svcs = s.active_services||[];
  document.getElementById('svcTxt').textContent = svcs.length ? svcs.join(', ') : '—';

  // History
  const hBody = document.getElementById('hBody');
  if (!hist||!hist.length) {
    hBody.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text4);font-size:12px">暂无记录</div>';
  } else {
    hBody.innerHTML = hist.slice(0,12).map(h => {
      const t = h.timestamp?new Date(h.timestamp):new Date();
      const ts = t.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
      const d = h.duration!=null?h.duration.toFixed(1)+'s':'—';
      const st = h.status==='ok'?'<span class="h-ok">✓</span>':'<span class="h-err">✗</span>';
      return '<div class="hrow"><span class="h-time">'+ts+'</span><span class="h-from">'+(h.from||'—')+'</span><span class="h-arrow">→</span><span class="h-to">'+h.to+'</span><span class="h-dur">'+d+'</span><span>'+st+'</span></div>';
    }).join('');
  }

  document.getElementById('ts').textContent = new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

async function loadModels() {
  const models = await j('/models');
  const excl = models.filter(m=>m.mode==='exclusive');
  const shrd = models.filter(m=>m.mode==='shared');

  // Exclusive panel
  const eList = document.getElementById('exclList');
  let eh = '';
  for (const m of excl) {
    const a = m.active;
    eh += '<div class="model-item'+(a?' active':'')+'" id="sw-'+m.name+'" onclick="doSwitch(\''+m.name+'\')">';
    if (a) eh += '<div class="model-active-dot"></div>';
    eh += '<div class="model-info"><div class="model-name">'+m.name+'</div><div class="model-desc">'+(m.description||'')+'</div></div>';
    eh += '<span class="model-badge excl">独占</span>';
    eh += '</div>';
  }
  eList.innerHTML = eh;

  // Shared panel
  const sList = document.getElementById('shrdList');
  let sh = '';
  for (const m of shrd) {
    const a = m.active;
    sh += '<div class="model-item'+(a?' active':'')+'" id="sw-'+m.name+'">';
    if (a) sh += '<div class="model-active-dot"></div>';
    sh += '<div class="model-info"><div class="model-name">'+m.name+'</div><div class="model-desc">'+(m.description||'')+'</div></div>';
    sh += '<span class="model-badge shrd">共享</span>';
    sh += '<div class="item-actions">';
    sh += '<button class="btn-sm stop"'+(a?'':' disabled')+' onclick="event.stopPropagation();doStop(\''+m.name+'\')">停止</button>';
    sh += '<button class="btn-sm start"'+(a?' disabled':'')+' onclick="event.stopPropagation();doSwitch(\''+m.name+'\')">启动</button>';
    sh += '</div>';
    sh += '</div>';
  }
  sList.innerHTML = sh;
}

async function doSwitch(n) {
  if (sw) return; sw = true;
  try {
    const r = await j('/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if (r.status==='switched') toast(n+' ✓ '+r.elapsed_sec+'s','ok');
    else if (r.status==='already_active') toast('已在 '+n,'info');
    else toast(r.message||'失败','err');
  } catch(e) { toast(e.message,'err'); }
  sw = false;
  await Promise.all([load(),loadModels()]);
}

async function doStop(n) {
  if (sw) return; sw = true;
  try {
    const r = await j('/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if (r.status==='stopped') toast(n+' 已停止','ok');
    else if (r.status==='already_stopped') toast(n+' 未运行','info');
    else toast(r.message||'停止失败','err');
  } catch(e) { toast(e.message,'err'); }
  sw = false;
  await Promise.all([load(),loadModels()]);
}

async function doReset() {
  if (!confirm('强制重置到 idle？')) return;
  const r = await j('/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  toast(r.status==='reset'?'已重置 ✓':'失败', r.status==='reset'?'ok':'err');
  await Promise.all([load(),loadModels()]);
}

async function doReconcile() {
  const r = await j('/reconcile',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const a = r.actions||[];
  toast(a.length===0?'状态一致 ✓':'修复: '+a.join('; '),'ok');
  await Promise.all([load(),loadModels()]);
}

Promise.all([load(),loadModels()]);
setInterval(()=>{load();loadModels();},5000);
</script>
</body>
</html>"""