"""
edge_llm/dashboard.py — Self-contained web dashboard for EdgeLLM.

v3.1: Redesigned with 2.5D neumorphic cards, rich status display,
      GPU/CPU/RAM metrics, and switch history from DB.
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
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

:root {
  --bg: #13151a;
  --surface: #1b1e27;
  --card: #1e2130;
  --card-hi: #252839;
  --border: rgba(255,255,255,0.06);
  --shadow-d: rgba(0,0,0,0.5);
  --shadow-l: rgba(255,255,255,0.03);
  --green: #34d399;
  --red: #f87171;
  --yellow: #fbbf24;
  --blue: #60a5fa;
  --purple: #a78bfa;
  --cyan: #22d3ee;
  --text: #e8ecf4;
  --text2: #a0aec0;
  --muted: #6b7280;
  --radius: 16px;
}

* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: 'Inter', -apple-system, sans-serif;
  background: var(--bg);
  color: var(--text);
  padding: 28px 32px;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}

/* ── Header ──────────────────────────────── */
.header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 28px;
}
.header-left { display: flex; align-items: center; gap: 14px; }
.logo {
  width: 36px; height: 36px; border-radius: 10px;
  background: linear-gradient(135deg, #60a5fa, #a78bfa);
  display: flex; align-items: center; justify-content: center;
  font-size: 18px; font-weight: 700; color: #fff;
  box-shadow: 0 4px 16px rgba(96,165,250,0.25);
}
.header h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.02em; }
.badge {
  padding: 5px 14px; border-radius: 20px;
  font-size: 12px; font-weight: 600;
  transition: all 0.3s;
}
.badge.healthy { background: rgba(52,211,153,0.12); color: var(--green); border: 1px solid rgba(52,211,153,0.2); }
.badge.switching { background: rgba(251,191,36,0.12); color: var(--yellow); border: 1px solid rgba(251,191,36,0.2); }
.badge.idle { background: rgba(107,114,128,0.12); color: var(--muted); border: 1px solid rgba(107,114,128,0.2); }
.badge.error { background: rgba(248,113,113,0.12); color: var(--red); border: 1px solid rgba(248,113,113,0.2); }
.header-right { display: flex; align-items: center; gap: 16px; }
.refresh-tag { font-size: 11px; color: var(--muted); }

/* ── 2.5D Card ──────────────────────────── */
.card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 22px 24px;
  box-shadow:
    8px 8px 24px var(--shadow-d),
    -4px -4px 12px var(--shadow-l),
    inset 0 1px 0 rgba(255,255,255,0.04);
  transition: transform 0.25s, box-shadow 0.25s;
  position: relative;
  overflow: hidden;
}
.card::before {
  content: '';
  position: absolute; top: 0; left: 0; right: 0; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.06), transparent);
}
.card:hover {
  transform: translateY(-2px);
  box-shadow:
    12px 12px 32px var(--shadow-d),
    -6px -6px 16px var(--shadow-l),
    inset 0 1px 0 rgba(255,255,255,0.06);
}
.card-label {
  font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.08em; color: var(--muted); margin-bottom: 14px;
}

/* ── Metric Row ─────────────────────────── */
.metrics { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; margin-bottom: 24px; }
.metric-value {
  font-size: 28px; font-weight: 700; letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}
.metric-unit { font-size: 13px; font-weight: 400; color: var(--text2); margin-left: 2px; }
.metric-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }

/* ── Progress Ring ──────────────────────── */
.ring-wrap { position: relative; width: 100px; height: 100px; margin: 0 auto 8px; }
.ring-wrap svg { transform: rotate(-90deg); }
.ring-bg { fill: none; stroke: rgba(255,255,255,0.06); stroke-width: 8; }
.ring-fg { fill: none; stroke-width: 8; stroke-linecap: round; transition: stroke-dashoffset 0.8s ease, stroke 0.5s; }
.ring-text {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
}
.ring-pct { font-size: 20px; font-weight: 700; }
.ring-label { font-size: 10px; color: var(--muted); margin-top: 2px; }

/* ── Status Grid ────────────────────────── */
.status-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 24px; }
.status-grid .card { min-height: 0; }

.service-row {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 0; border-bottom: 1px solid var(--border);
}
.service-row:last-child { border-bottom: none; }
.service-dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  box-shadow: 0 0 8px currentColor;
}
.service-dot.on { color: var(--green); background: var(--green); }
.service-dot.off { color: var(--muted); background: var(--muted); box-shadow: none; }
.service-dot.load { color: var(--yellow); background: var(--yellow); animation: pulse 1.5s infinite; }
.service-name { font-size: 13px; font-weight: 500; flex: 1; }
.service-pid { font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }

@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

/* ── Profile Detail ─────────────────────── */
.profile-hero { text-align: center; padding: 8px 0; }
.profile-hero .name { font-size: 24px; font-weight: 700; margin-bottom: 4px; }
.profile-hero .desc { font-size: 13px; color: var(--text2); }

/* ── Switcher ───────────────────────────── */
.switcher { margin-bottom: 24px; }
.switcher .card-label { margin-bottom: 16px; }
.switch-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; }
.switch-btn {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px 12px;
  cursor: pointer; color: var(--text);
  text-align: center;
  box-shadow: 4px 4px 12px var(--shadow-d), -2px -2px 6px var(--shadow-l);
  transition: all 0.25s;
  position: relative; overflow: hidden;
}
.switch-btn::before {
  content: ''; position: absolute; inset: 0;
  background: linear-gradient(135deg, rgba(255,255,255,0.03), transparent);
  pointer-events: none;
}
.switch-btn:hover {
  transform: translateY(-3px);
  box-shadow: 6px 6px 20px var(--shadow-d), -3px -3px 10px var(--shadow-l);
  border-color: rgba(96,165,250,0.3);
}
.switch-btn.active {
  border-color: rgba(52,211,153,0.4);
  background: linear-gradient(135deg, rgba(52,211,153,0.08), rgba(96,165,250,0.05));
  box-shadow: 4px 4px 12px var(--shadow-d), -2px -2px 6px var(--shadow-l), 0 0 20px rgba(52,211,153,0.08);
}
.switch-btn.loading { opacity: 0.5; pointer-events: none; }
.switch-btn .sname { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.switch-btn .sdesc { font-size: 10px; color: var(--muted); line-height: 1.4; }
.switch-btn .scost { font-size: 10px; color: var(--yellow); margin-top: 8px; font-weight: 500; }
.switch-btn .sactive {
  position: absolute; top: 8px; right: 8px;
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--green); box-shadow: 0 0 6px var(--green);
}

/* ── Actions ────────────────────────────── */
.actions { display: flex; gap: 12px; margin-bottom: 24px; }
.act-btn {
  flex: 1; padding: 12px 16px;
  background: var(--card); border: 1px solid var(--border);
  border-radius: 12px; cursor: pointer; color: var(--text2);
  font-size: 13px; font-weight: 500; text-align: center;
  box-shadow: 4px 4px 10px var(--shadow-d), -2px -2px 5px var(--shadow-l);
  transition: all 0.2s;
}
.act-btn:hover { transform: translateY(-1px); color: var(--text); }
.act-btn.warn { border-color: rgba(251,191,36,0.3); }
.act-btn.warn:hover { color: var(--yellow); border-color: rgba(251,191,36,0.5); }
.act-btn.danger { border-color: rgba(248,113,113,0.3); }
.act-btn.danger:hover { color: var(--red); border-color: rgba(248,113,113,0.5); }

/* ── History ────────────────────────────── */
.history .card-label { margin-bottom: 12px; }
.history-list { max-height: 220px; overflow-y: auto; }
.history-list::-webkit-scrollbar { width: 4px; }
.history-list::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }
.h-item {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 14px; border-radius: 10px;
  margin-bottom: 6px; font-size: 12px;
  background: var(--surface);
  border: 1px solid var(--border);
}
.h-item .h-from { color: var(--red); font-weight: 500; min-width: 80px; }
.h-item .h-arrow { color: var(--muted); font-size: 14px; }
.h-item .h-to { color: var(--green); font-weight: 500; min-width: 80px; }
.h-item .h-dur { color: var(--cyan); font-variant-numeric: tabular-nums; min-width: 50px; }
.h-item .h-time { margin-left: auto; color: var(--muted); font-size: 11px; }
.h-item .h-ok { color: var(--green); font-size: 10px; }
.h-item .h-err { color: var(--red); font-size: 10px; }

/* ── Toast ──────────────────────────────── */
.toast {
  position: fixed; bottom: 28px; right: 28px;
  padding: 14px 22px; border-radius: 12px;
  font-size: 13px; font-weight: 500;
  transform: translateY(100px); transition: transform 0.35s cubic-bezier(0.4,0,0.2,1);
  z-index: 100;
  box-shadow: 0 8px 32px rgba(0,0,0,0.4);
}
.toast.show { transform: translateY(0); }
.toast.success { background: rgba(52,211,153,0.15); color: var(--green); border: 1px solid rgba(52,211,153,0.25); backdrop-filter: blur(12px); }
.toast.error { background: rgba(248,113,113,0.15); color: var(--red); border: 1px solid rgba(248,113,113,0.25); backdrop-filter: blur(12px); }

/* ── Responsive ─────────────────────────── */
@media (max-width: 900px) {
  .status-grid { grid-template-columns: 1fr; }
  .switch-grid { grid-template-columns: repeat(3, 1fr); }
  .metrics { grid-template-columns: 1fr; }
}
@media (max-width: 600px) {
  .switch-grid { grid-template-columns: repeat(2, 1fr); }
  body { padding: 16px; }
}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="logo">E</div>
    <h1>EdgeLLM</h1>
    <span class="badge idle" id="stateBadge">idle</span>
  </div>
  <div class="header-right">
    <span class="refresh-tag">5s · <span id="lastRefresh">-</span></span>
  </div>
</div>

<!-- Metrics -->
<div class="metrics">
  <div class="card" style="text-align:center">
    <div class="card-label">GPU 显存</div>
    <div class="ring-wrap">
      <svg width="100" height="100" viewBox="0 0 100 100">
        <circle class="ring-bg" cx="50" cy="50" r="42"/>
        <circle class="ring-fg" id="gpuRing" cx="50" cy="50" r="42"
          stroke="var(--green)" stroke-dasharray="264" stroke-dashoffset="264"/>
      </svg>
      <div class="ring-text">
        <span class="ring-pct" id="gpuPct">0%</span>
        <span class="ring-label">已用</span>
      </div>
    </div>
    <div style="font-size:13px;color:var(--text2)"><span id="gpuUsed">0</span> / <span id="gpuTotal">32,607</span> MB</div>
  </div>

  <div class="card" style="text-align:center">
    <div class="card-label">系统内存</div>
    <div class="ring-wrap">
      <svg width="100" height="100" viewBox="0 0 100 100">
        <circle class="ring-bg" cx="50" cy="50" r="42"/>
        <circle class="ring-fg" id="ramRing" cx="50" cy="50" r="42"
          stroke="var(--blue)" stroke-dasharray="264" stroke-dashoffset="264"/>
      </svg>
      <div class="ring-text">
        <span class="ring-pct" id="ramPct">0%</span>
        <span class="ring-label">已用</span>
      </div>
    </div>
    <div style="font-size:13px;color:var(--text2)"><span id="ramUsed">0</span> / <span id="ramTotal">-</span> GB</div>
  </div>

  <div class="card" style="text-align:center">
    <div class="card-label">CPU 负载</div>
    <div class="ring-wrap">
      <svg width="100" height="100" viewBox="0 0 100 100">
        <circle class="ring-bg" cx="50" cy="50" r="42"/>
        <circle class="ring-fg" id="cpuRing" cx="50" cy="50" r="42"
          stroke="var(--purple)" stroke-dasharray="264" stroke-dashoffset="264"/>
      </svg>
      <div class="ring-text">
        <span class="ring-pct" id="cpuPct">0%</span>
        <span class="ring-label">负载</span>
      </div>
    </div>
    <div style="font-size:13px;color:var(--text2)"><span id="cpuCores">-</span> 核心 · <span id="uptime">-</span></div>
  </div>
</div>

<!-- Status -->
<div class="status-grid">
  <div class="card">
    <div class="card-label">当前 Profile</div>
    <div class="profile-hero">
      <div class="name" id="profileName">idle</div>
      <div class="desc" id="profileDesc">GPU 空闲</div>
    </div>
  </div>
  <div class="card">
    <div class="card-label">服务状态</div>
    <div class="service-row">
      <span class="service-dot off" id="vllmDot"></span>
      <span class="service-name">vLLM</span>
      <span class="service-pid" id="vllmPid">—</span>
    </div>
    <div class="service-row">
      <span class="service-dot off" id="comfyuiDot"></span>
      <span class="service-name">ComfyUI</span>
      <span class="service-pid" id="comfyuiPid">—</span>
    </div>
  </div>
</div>

<!-- Switcher -->
<div class="switcher">
  <div class="card">
    <div class="card-label">切换 Profile</div>
    <div class="switch-grid" id="switchGrid"></div>
  </div>
</div>

<!-- Actions -->
<div class="actions">
  <button class="act-btn warn" onclick="doReconcile()">🔍 Reconcile</button>
  <button class="act-btn danger" onclick="doReset()">⏹ Reset to Idle</button>
</div>

<!-- History -->
<div class="history">
  <div class="card">
    <div class="card-label">切换历史</div>
    <div class="history-list" id="historyList">
      <div style="padding:16px;text-align:center;color:var(--muted);font-size:12px">加载中…</div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const API = '';
let switching = false;

function showToast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + type + ' show';
  setTimeout(() => t.classList.remove('show'), 3500);
}

async function fetchJSON(path, opts) {
  const r = await fetch(API + path, opts);
  return r.json();
}

function setRing(id, pct) {
  const circ = 2 * Math.PI * 42; // ~264
  const el = document.getElementById(id);
  const offset = circ * (1 - Math.min(pct, 100) / 100);
  el.style.strokeDashoffset = offset;
  // Color by pct
  if (id === 'gpuRing') {
    el.style.stroke = pct < 50 ? 'var(--green)' : pct < 80 ? 'var(--yellow)' : 'var(--red)';
  }
}

function dotClass(status) {
  if (status === '✅') return 'service-dot on';
  if (status === '⏳') return 'service-dot load';
  return 'service-dot off';
}

async function loadStatus() {
  const s = await fetchJSON('/status');
  const sys = await fetchJSON('/system').catch(() => ({}));

  // Profile
  document.getElementById('profileName').textContent = s.profile;
  document.getElementById('profileDesc').textContent = s.description || '';

  // State badge
  const badge = document.getElementById('stateBadge');
  const stateMap = { healthy: 'healthy', switching: 'switching', idle: 'idle', error: 'error' };
  const stateLabel = { healthy: '运行中', switching: '切换中', idle: '空闲', error: '异常' };
  badge.textContent = stateLabel[s.state] || s.state;
  badge.className = 'badge ' + (stateMap[s.state] || 'idle');

  // Services
  document.getElementById('vllmDot').className = dotClass(s.vllm);
  document.getElementById('comfyuiDot').className = dotClass(s.comfyui);
  document.getElementById('vllmPid').textContent = s.vllm === '✅' ? 'PID ' + (s.vllm_pid || '?') : '—';
  document.getElementById('comfyuiPid').textContent = s.comfyui === '✅' ? 'PID ' + (s.comfyui_pid || '?') : '—';

  // GPU
  const gpuTotal = s.gpu_total_mb || 32607;
  const gpuUsed = s.gpu_used_mb || 0;
  const gpuPct = (gpuUsed / gpuTotal * 100).toFixed(1);
  document.getElementById('gpuUsed').textContent = gpuUsed.toLocaleString();
  document.getElementById('gpuTotal').textContent = gpuTotal.toLocaleString();
  document.getElementById('gpuPct').textContent = gpuPct + '%';
  setRing('gpuRing', gpuPct);

  // RAM
  const ramTotal = sys.ram_total_gb || 0;
  const ramUsed = sys.ram_used_gb || 0;
  const ramPct = ramTotal > 0 ? (ramUsed / ramTotal * 100).toFixed(1) : 0;
  document.getElementById('ramUsed').textContent = ramUsed.toFixed(1);
  document.getElementById('ramTotal').textContent = ramTotal.toFixed(1);
  document.getElementById('ramPct').textContent = ramPct + '%';
  setRing('ramRing', ramPct);

  // CPU
  const cpuPct = sys.cpu_percent || 0;
  document.getElementById('cpuPct').textContent = cpuPct.toFixed(1) + '%';
  document.getElementById('cpuCores').textContent = sys.cpu_cores || '-';
  setRing('cpuRing', cpuPct);

  // Uptime
  const upSec = sys.uptime_seconds || 0;
  const upH = Math.floor(upSec / 3600);
  const upM = Math.floor((upSec % 3600) / 60);
  document.getElementById('uptime').textContent = upH + 'h ' + upM + 'm';

  document.getElementById('lastRefresh').textContent = new Date().toLocaleTimeString();
}

async function loadProfiles() {
  const profiles = await fetchJSON('/profiles');
  const grid = document.getElementById('switchGrid');
  grid.innerHTML = '';
  for (const p of profiles) {
    const btn = document.createElement('div');
    btn.className = 'switch-btn' + (p.current ? ' active' : '');
    btn.id = 'btn-' + p.name;
    btn.innerHTML =
      (p.current ? '<div class="sactive"></div>' : '') +
      '<div class="sname">' + p.name.replace(/_/g, ' ') + '</div>' +
      '<div class="sdesc">' + p.description + '</div>' +
      '<div class="scost">⏱ ~' + p.switch_cost_sec + 's</div>';
    btn.onclick = () => doSwitch(p.name);
    grid.appendChild(btn);
  }
}

async function loadHistory() {
  try {
    const hist = await fetchJSON('/history');
    const list = document.getElementById('historyList');
    if (!hist || hist.length === 0) {
      list.innerHTML = '<div style="padding:16px;text-align:center;color:var(--muted);font-size:12px">暂无历史</div>';
      return;
    }
    list.innerHTML = hist.map(h => {
      const ts = h.timestamp ? new Date(h.timestamp) : new Date();
      const time = ts.toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
      const dur = h.duration ? h.duration.toFixed(1) + 's' : '-';
      const st = h.status === 'ok' ? '<span class="h-ok">✓</span>' : '<span class="h-err">✗</span>';
      return '<div class="h-item">' +
        '<span class="h-from">' + (h.from || '-') + '</span>' +
        '<span class="h-arrow">→</span>' +
        '<span class="h-to">' + h.to + '</span>' +
        '<span class="h-dur">' + dur + '</span>' +
        st +
        '<span class="h-time">' + time + '</span>' +
      '</div>';
    }).join('');
  } catch(e) {
    document.getElementById('historyList').innerHTML =
      '<div style="padding:16px;text-align:center;color:var(--muted);font-size:12px">加载失败</div>';
  }
}

async function doSwitch(name) {
  if (switching) return;
  const btn = document.getElementById('btn-' + name);
  if (btn) btn.classList.add('loading');
  switching = true;
  try {
    const result = await fetchJSON('/switch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({profile: name})
    });
    if (result.status === 'switched') {
      showToast('✅ ' + name + ' (' + result.elapsed_sec + 's)', 'success');
    } else if (result.status === 'already_active') {
      showToast('ℹ️ 已在 ' + name, 'success');
    } else {
      showToast('❌ ' + (result.message || 'failed'), 'error');
    }
    await refresh();
  } catch(e) {
    showToast('❌ ' + e.message, 'error');
  }
  switching = false;
  document.querySelectorAll('.switch-btn').forEach(b => b.classList.remove('loading'));
}

async function doReset() {
  if (!confirm('强制重置到 idle？所有服务将被终止。')) return;
  const result = await fetchJSON('/reset', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({profile: 'idle'})
  });
  showToast(result.status === 'reset' ? '✅ 已重置' : '❌ ' + (result.message || 'fail'),
    result.status === 'reset' ? 'success' : 'error');
  await refresh();
}

async function doReconcile() {
  const result = await fetchJSON('/reconcile', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({})
  });
  const actions = result.actions || [];
  showToast(actions.length === 0 ? '✅ 状态一致' : '🔧 ' + actions.join('; '),
    'success');
  await refresh();
}

async function refresh() {
  await Promise.all([loadStatus(), loadProfiles(), loadHistory()]);
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""
