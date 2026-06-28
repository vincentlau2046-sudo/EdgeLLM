"""
edge_llm/dashboard.py — Self-contained web dashboard for EdgeLLM.

v4.1: Apple light-theme redesign. Clean typography, spacious layout,
      SF-style colors, frosted glass, linear bars.
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
  --blue:    #007AFF; --blue-l:  #E8F2FF; --blue-bg: #F0F5FF;
  --green:   #34C759; --green-l: #E6F9ED; --green-bg:#EDFFF3;
  --red:     #FF3B30; --red-l:   #FFEAE9; --red-bg:  #FFF5F5;
  --orange:  #FF9500; --orange-l:#FFF3E0;
  --purple:  #AF52DE; --purple-l:#F3E8FA;
  --gray:    #8E8E93; --gray-l:  #F2F2F7; --gray-bg: #F5F5F7;
  --text:    #1D1D1F; --text2:   #6E6E73; --text3:   #AEAEB2;
  --white:   #FFFFFF; --border:  #E5E5EA;
  --shadow-sm: 0 1px 3px rgba(0,0,0,.04), 0 1px 2px rgba(0,0,0,.06);
  --shadow-md: 0 4px 12px rgba(0,0,0,.04), 0 2px 6px rgba(0,0,0,.06);
  --shadow-lg: 0 8px 30px rgba(0,0,0,.06), 0 4px 14px rgba(0,0,0,.04);
  --radius-sm: 10px; --radius: 14px; --radius-lg: 20px;
}

* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display",
    "SF Pro Text", "Helvetica Neue", sans-serif;
  background: var(--gray-bg); color: var(--text);
  padding: 0; min-height:100vh;
  -webkit-font-smoothing: antialiased;
  font-size: 14px; line-height: 1.5;
}

/* ── Frosted Header ── */
.nav {
  position: sticky; top:0; z-index:50;
  background: rgba(255,255,255,.72);
  backdrop-filter: saturate(180%) blur(20px);
  -webkit-backdrop-filter: saturate(180%) blur(20px);
  border-bottom: 1px solid rgba(0,0,0,.06);
  padding: 0 32px;
}
.nav-inner {
  max-width: 1100px; margin:0 auto;
  display:flex; align-items:center; justify-content:space-between;
  height: 52px;
}
.nav-l { display:flex; align-items:center; gap:12px; }
.nav-logo {
  width:32px; height:32px; border-radius:8px;
  background: var(--blue);
  display:flex; align-items:center; justify-content:center;
  font-size:16px; font-weight:700; color:#fff; letter-spacing:-.02em;
}
.nav-title { font-size:17px; font-weight:600; letter-spacing:-.01em; }
.nav-r { display:flex; align-items:center; gap:10px; }

/* ── Badge / Tag ── */
.tag {
  display:inline-flex; align-items:center; gap:5px;
  padding:3px 12px; border-radius:20px; font-size:12px; font-weight:600;
  letter-spacing:-.01em; transition:all .25s;
}
.tag .dot { width:7px; height:7px; border-radius:50%; }
.tag.idle      { background:var(--gray-l);   color:var(--gray); }
.tag.idle .dot      { background:var(--gray); }
.tag.exclusive      { background:var(--red-l);    color:var(--red); }
.tag.exclusive .dot      { background:var(--red); }
.tag.shared   { background:var(--green-l);  color:var(--green); }
.tag.shared .dot   { background:var(--green); }
.tag.error     { background:var(--red-l);    color:var(--red); }
.nav-ver { font-size:12px; color:var(--text3); }
.nav-time { font-size:12px; color:var(--text2); font-variant-numeric:tabular-nums; }

/* ── Main Container ── */
.main { max-width:1100px; margin:0 auto; padding:28px 32px 40px; }
.grid-2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; margin-bottom:16px; }
.grid-3 { display:grid; grid-template-columns:1fr 1fr 1fr; gap:16px; margin-bottom:16px; }
.grid-4 { display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:16px; }

/* ── Card ── */
.card {
  background:var(--white); border-radius:var(--radius);
  border:1px solid var(--border);
  box-shadow:var(--shadow-sm);
  padding:20px 22px;
  transition:box-shadow .2s;
}
.card:hover { box-shadow:var(--shadow-md); }
.card-hdr {
  display:flex; align-items:center; justify-content:space-between;
  margin-bottom:14px;
}
.card-title {
  font-size:11px; font-weight:600; text-transform:uppercase;
  letter-spacing:.06em; color:var(--text3);
}

/* ── Stat Bar Card ── */
.stat-card { padding:16px 20px; }
.stat-row { display:flex; align-items:center; gap:14px; }
.stat-icon {
  width:42px; height:42px; border-radius:11px;
  display:flex; align-items:center; justify-content:center;
  font-size:18px; flex-shrink:0;
}
.stat-icon.gpu  { background:var(--blue-l);  }
.stat-icon.ram  { background:var(--purple-l); }
.stat-icon.cpu  { background:var(--orange-l); }
.stat-body { flex:1; min-width:0; }
.stat-label { font-size:12px; font-weight:500; color:var(--text2); margin-bottom:6px; }
.stat-val { font-size:22px; font-weight:700; letter-spacing:-.02em; margin-bottom:8px; line-height:1; }
.stat-bar {
  height:4px; border-radius:2px; background:var(--gray-l); overflow:hidden;
}
.stat-bar-fill {
  height:100%; border-radius:2px;
  transition:width .6s cubic-bezier(.4,0,.2,1), background .3s;
}

/* ── Status Row Card ── */
.service-row { display:flex; flex-wrap:wrap; gap:8px; }
.svc-chip {
  display:inline-flex; align-items:center; gap:6px;
  padding:4px 12px; border-radius:16px;
  font-size:13px; font-weight:500;
  background:var(--gray-l); color:var(--text2);
}
.svc-chip.active { background:var(--green-l); color:var(--green); }
.svc-chip .chip-dot {
  width:6px; height:6px; border-radius:50%;
  background:var(--text3);
}
.svc-chip.active .chip-dot { background:var(--green); }
.svc-placeholder { font-size:13px; color:var(--text3); padding:4px 0; }

/* ── Model Switcher ── */
.model-grid {
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(170px,1fr));
  gap:10px;
}
.model-card {
  background:var(--white);
  border:1.5px solid var(--border);
  border-radius:var(--radius-sm);
  padding:14px 16px; cursor:pointer;
  transition:all .2s; position:relative;
  user-select:none;
}
.model-card:hover {
  border-color:var(--blue);
  box-shadow:0 0 0 3px rgba(0,122,255,.08);
  transform:translateY(-1px);
}
.model-card.active {
  border-color:var(--green);
  background:var(--green-bg);
}
.model-card.active::before {
  content:''; position:absolute; top:8px; right:8px;
  width:7px; height:7px; border-radius:50%; background:var(--green);
  box-shadow:0 0 0 3px rgba(52,199,89,.2);
}
.model-card.loading { opacity:.5; pointer-events:none; }
.model-name {
  font-size:15px; font-weight:600; margin-bottom:3px;
  letter-spacing:-.01em;
}
.model-desc { font-size:12px; color:var(--text2); line-height:1.4; }
.model-mode {
  display:inline-block; margin-top:6px; padding:2px 8px; border-radius:6px;
  font-size:10px; font-weight:600; letter-spacing:.03em; text-transform:uppercase;
}
.model-mode.excl { background:var(--red-l); color:var(--red); }
.model-mode.shrd { background:var(--green-l); color:var(--green); }
.model-card.idle-card {
  border-style:dashed; border-color:var(--text3);
}
.model-card.idle-card:hover { border-color:var(--gray); }

/* ─── Section Label ── */
.sec-label {
  font-size:12px; font-weight:600; text-transform:uppercase;
  letter-spacing:.06em; color:var(--text3);
  margin:6px 0 10px;
}

/* ── Actions ── */
.act-row { display:flex; gap:10px; }
.act-btn {
  flex:1; padding:11px 16px; border:none; border-radius:var(--radius-sm);
  font-size:13px; font-weight:600; cursor:pointer;
  transition:all .15s; letter-spacing:-.01em;
}
.act-btn.pri { background:var(--blue); color:#fff; }
.act-btn.pri:hover { background:#0066D6; }
.act-btn.sec { background:var(--gray-l); color:var(--text); }
.act-btn.sec:hover { background:#E5E5EA; }
.act-btn.warn { background:var(--red-l); color:var(--red); }
.act-btn.warn:hover { background:#FFD6D4; }

/* ── History Table ── */
.hist-scroll { max-height:260px; overflow-y:auto; }
.hist-scroll::-webkit-scrollbar { width:6px; }
.hist-scroll::-webkit-scrollbar-track { background:transparent; }
.hist-scroll::-webkit-scrollbar-thumb { background:var(--border); border-radius:3px; }
.hist-table { width:100%; border-collapse:collapse; font-size:13px; }
.hist-table th {
  text-align:left; padding:8px 12px;
  font-size:11px; font-weight:600; text-transform:uppercase;
  letter-spacing:.05em; color:var(--text3); border-bottom:1px solid var(--border);
}
.hist-table td { padding:9px 12px; border-bottom:1px solid var(--gray-l); }
.hist-table tr:last-child td { border-bottom:none; }
.hist-from { color:var(--red); font-weight:500; }
.hist-to   { color:var(--green); font-weight:500; }
.hist-dur  { font-variant-numeric:tabular-nums; color:var(--text); font-weight:600; }
.hist-time { color:var(--text3); font-variant-numeric:tabular-nums; }
.hist-ok   { color:var(--green); }
.hist-err  { color:var(--red); }

/* ── Toast ── */
.toast {
  position:fixed; bottom:24px; right:24px; z-index:200;
  padding:12px 20px; border-radius:var(--radius-sm);
  font-size:14px; font-weight:500;
  transform:translateY(100px); opacity:0;
  transition:all .35s cubic-bezier(.4,0,.2,1);
  box-shadow:var(--shadow-lg);
  max-width:360px;
}
.toast.show { transform:translateY(0); opacity:1; }
.toast.ok    { background:var(--green-l); color:var(--green); border:1px solid var(--green); }
.toast.err   { background:var(--red-l);  color:var(--red);  border:1px solid var(--red); }
.toast.info  { background:var(--blue-l); color:var(--blue); border:1px solid var(--blue); }

@media (max-width:768px) {
  .grid-2,.grid-3 { grid-template-columns:1fr; }
  .grid-4 { grid-template-columns:repeat(2,1fr); }
  .main { padding:20px 16px 32px; }
  .nav { padding:0 16px; }
  .model-grid { grid-template-columns:repeat(2,1fr); }
}
</style>
</head>
<body>

<!-- Frosted Nav -->
<div class="nav">
  <div class="nav-inner">
    <div class="nav-l">
      <div class="nav-logo">E</div>
      <span class="nav-title">EdgeLLM</span>
      <span class="tag idle" id="sTag"><span class="dot"></span><span id="sTxt">idle</span></span>
    </div>
    <div class="nav-r">
      <span class="nav-ver">v4.0</span>
      <span class="nav-time" id="ts">—</span>
    </div>
  </div>
</div>

<div class="main">

  <!-- Row 1: 3 stat bars -->
  <div class="grid-3">
    <div class="card stat-card">
      <div class="stat-row">
        <div class="stat-icon gpu">💾</div>
        <div class="stat-body">
          <div class="stat-label">GPU 显存</div>
          <div class="stat-val" style="color:var(--blue)"><span id="gP">0</span><span style="font-size:13px;color:var(--text3);font-weight:500">%</span></div>
          <div class="stat-bar"><div class="stat-bar-fill" id="gB" style="width:0%;background:var(--blue)"></div></div>
          <div style="font-size:12px;color:var(--text3);margin-top:6px"><strong style="color:var(--text)" id="gU">0</strong> / <span id="gT">32,607</span> MB</div>
        </div>
      </div>
    </div>

    <div class="card stat-card">
      <div class="stat-row">
        <div class="stat-icon ram">🧠</div>
        <div class="stat-body">
          <div class="stat-label">系统内存</div>
          <div class="stat-val" style="color:var(--purple)"><span id="rP">0</span><span style="font-size:13px;color:var(--text3);font-weight:500">%</span></div>
          <div class="stat-bar"><div class="stat-bar-fill" id="rB" style="width:0%;background:var(--purple)"></div></div>
          <div style="font-size:12px;color:var(--text3);margin-top:6px"><strong style="color:var(--text)" id="rU">0</strong> / <span id="rT">—</span> GB</div>
        </div>
      </div>
    </div>

    <div class="card stat-card">
      <div class="stat-row">
        <div class="stat-icon cpu">⚡</div>
        <div class="stat-body">
          <div class="stat-label">CPU</div>
          <div class="stat-val" style="color:var(--orange)"><span id="cP">0</span><span style="font-size:13px;color:var(--text3);font-weight:500">%</span></div>
          <div class="stat-bar"><div class="stat-bar-fill" id="cB" style="width:0%;background:var(--orange)"></div></div>
          <div style="font-size:12px;color:var(--text3);margin-top:6px"><span id="cC">—</span> 核心 · 运行 <span id="cU">—</span></div>
        </div>
      </div>
    </div>
  </div>

  <!-- Row 2: GPU Mode + Active Services -->
  <div class="grid-2">
    <div class="card">
      <div class="card-hdr">
        <span class="card-title">GPU 模式</span>
      </div>
      <div style="font-size:28px;font-weight:700;letter-spacing:-.02em;margin-bottom:4px" id="pN">idle</div>
      <div style="font-size:14px;color:var(--text2)" id="pD">GPU 空闲</div>
    </div>

    <div class="card">
      <div class="card-hdr">
        <span class="card-title">活跃服务</span>
        <span style="font-size:12px;color:var(--text3)" id="svcCount"></span>
      </div>
      <div class="service-row" id="svcRow">
        <span class="svc-placeholder">—</span>
      </div>
    </div>
  </div>

  <!-- Row 3: Model Switcher -->
  <div class="card" style="margin-bottom:16px">
    <div class="card-hdr">
      <span class="card-title">模型管理</span>
    </div>
    <div id="swArea"></div>
  </div>

  <!-- Row 4: Actions -->
  <div class="act-row" style="margin-bottom:16px">
    <button class="act-btn sec" onclick="doReconcile()">Reconcile</button>
    <button class="act-btn warn" onclick="doReset()">强制重置</button>
  </div>

  <!-- Row 5: History -->
  <div class="card">
    <div class="card-hdr">
      <span class="card-title">切换历史</span>
    </div>
    <div class="hist-scroll">
      <table class="hist-table">
        <thead><tr><th>时间</th><th>来源</th><th>目标</th><th>耗时</th><th></th></tr></thead>
        <tbody id="hBody"><tr><td colspan="5" style="text-align:center;color:var(--text3);padding:20px">加载中…</td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let sw = false;

function toast(m, type) {
  const e = document.getElementById('toast');
  e.textContent = m; e.className = 'toast ' + type + ' show';
  clearTimeout(e._t); e._t = setTimeout(() => e.classList.remove('show'), 2800);
}

async function j(p, o) {
  const r = await fetch(p, o);
  if (!r.ok && r.status !== 503) throw new Error(r.statusText);
  return r.json();
}

function barColor(id, pct) {
  const el = document.getElementById(id);
  if (id === 'gB') {
    el.style.background = pct < 50 ? 'var(--blue)' : pct < 80 ? 'var(--orange)' : 'var(--red)';
  }
}

async function load() {
  const [s, sys, hist] = await Promise.all([
    j('/status'), j('/system').catch(() => ({})), j('/history').catch(() => [])
  ]);

  // Nav tag
  const gm = s.gpu_mode || 'idle';
  const labels = { idle:'idle', exclusive:'exclusive', shared:'shared' };
  const tag = document.getElementById('sTag');
  tag.className = 'tag ' + gm;
  document.getElementById('sTxt').textContent = labels[gm] || gm;

  // GPU mode text
  const desc = { idle:'GPU 空闲', exclusive:'独占模式', shared:'共享模式' };
  document.getElementById('pN').textContent = labels[gm] || gm;
  document.getElementById('pD').textContent = desc[gm] || '';

  // Services
  const svcs = s.active_services || [];
  const health = s.services_health || {};
  const svcRow = document.getElementById('svcRow');
  document.getElementById('svcCount').textContent = svcs.length > 0 ? svcs.length + ' 个活跃' : '';
  if (svcs.length === 0) {
    svcRow.innerHTML = '<span class="svc-placeholder">无活跃服务</span>';
  } else {
    svcRow.innerHTML = svcs.map(n => {
      const h = health[n] || '❌';
      const cls = h === '✅' ? 'svc-chip active' : 'svc-chip';
      const dot = h === '✅' ? '<span class="chip-dot"></span>' : '';
      return '<span class="' + cls + '">' + dot + n + '</span>';
    }).join('');
  }

  // GPU bar
  const gt = s.gpu_total_mb || 32607, gu = s.gpu_used_mb || 0, gp = (gu/gt*100);
  document.getElementById('gP').textContent = gp.toFixed(1);
  document.getElementById('gU').textContent = gu.toLocaleString();
  document.getElementById('gT').textContent = gt.toLocaleString();
  document.getElementById('gB').style.width = gp.toFixed(1) + '%';
  barColor('gB', gp);

  // RAM bar
  const rt = sys.ram_total_gb || 1, ru = sys.ram_used_gb || 0, rp = (ru/rt*100);
  document.getElementById('rP').textContent = rp.toFixed(1);
  document.getElementById('rU').textContent = ru.toFixed(1);
  document.getElementById('rT').textContent = rt.toFixed(1);
  document.getElementById('rB').style.width = rp.toFixed(1) + '%';

  // CPU bar
  const cp = sys.cpu_percent || 0;
  document.getElementById('cP').textContent = cp.toFixed(1);
  document.getElementById('cC').textContent = sys.cpu_cores || '—';
  document.getElementById('cB').style.width = cp.toFixed(1) + '%';
  const us = sys.uptime_seconds || 0;
  document.getElementById('cU').textContent = Math.floor(us/3600)+'h '+Math.floor((us%3600)/60)+'m';

  // History table
  const hBody = document.getElementById('hBody');
  if (!hist || hist.length === 0) {
    hBody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text3);padding:20px">暂无记录</td></tr>';
  } else {
    hBody.innerHTML = hist.slice(0,15).map(h => {
      const t = h.timestamp ? new Date(h.timestamp) : new Date();
      const ts = t.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
      const d = h.duration != null ? h.duration.toFixed(1)+'s' : '—';
      const st = h.status==='ok' ? '<span class="hist-ok">✓</span>' : '<span class="hist-err">✗</span>';
      return '<tr><td class="hist-time">'+ts+'</td><td class="hist-from">'+(h.from||'—')+'</td><td class="hist-to">'+h.to+'</td><td class="hist-dur">'+d+'</td><td>'+st+'</td></tr>';
    }).join('');
  }

  document.getElementById('ts').textContent = new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
}

async function loadModels() {
  const models = await j('/models');
  const area = document.getElementById('swArea');

  const exclusive = models.filter(m => m.mode==='exclusive');
  const shared = models.filter(m => m.mode==='shared');

  let html = '';

  // Idle
  html += '<div class="sec-label">释放</div>';
  html += '<div class="model-grid">';
  html += '<div class="model-card idle-card" id="sw-idle" onclick="doSwitch(\'idle\')">';
  html += '<div class="model-name" style="color:var(--text2)">⚪ idle</div>';
  html += '<div class="model-desc">释放 GPU，停止所有服务</div>';
  html += '</div>';
  html += '</div>';

  // Exclusive
  if (exclusive.length) {
    html += '<div class="sec-label" style="margin-top:14px">独占模型</div>';
    html += '<div class="model-grid">';
    for (const m of exclusive) {
      const a = m.active;
      html += '<div class="model-card'+(a?' active':'')+'" id="sw-'+m.name+'" onclick="doSwitch(\''+m.name+'\')">';
      html += '<div class="model-name">'+m.name+'</div>';
      html += '<div class="model-desc">'+(m.description||'')+'</div>';
      html += '<span class="model-mode excl">独占</span>';
      html += '</div>';
    }
    html += '</div>';
  }

  // Shared
  if (shared.length) {
    html += '<div class="sec-label" style="margin-top:14px">共享服务</div>';
    html += '<div class="model-grid">';
    for (const m of shared) {
      const a = m.active;
      html += '<div class="model-card'+(a?' active':'')+'" id="sw-'+m.name+'" onclick="doSwitch(\''+m.name+'\')">';
      html += '<div class="model-name">'+m.name+'</div>';
      html += '<div class="model-desc">'+(m.description||'')+'</div>';
      html += '<span class="model-mode shrd">共享</span>';
      html += '</div>';
    }
    html += '</div>';
  }

  area.innerHTML = html;
}

async function doSwitch(n) {
  if (sw) return;
  const b = document.getElementById('sw-'+n);
  if (b) b.classList.add('loading');
  sw = true;
  try {
    const r = await j('/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if (r.status==='switched')      toast(n+' ✓  '+r.elapsed_sec+'s','ok');
    else if (r.status==='already_active') toast('已在 '+n,'info');
    else                             toast(r.message||'失败','err');
  } catch(e) { toast(e.message,'err'); }
  sw = false;
  document.querySelectorAll('.model-card').forEach(c => c.classList.remove('loading'));
  await Promise.all([load(),loadModels()]);
}

async function doReset() {
  if (!confirm('强制重置到 idle？')) return;
  const r = await j('/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  toast(r.status==='reset'?'已重置 ✓':'失败','ok');
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