"""
edge_llm/dashboard.py — Self-contained web dashboard for EdgeLLM.

Serves a real-time status page with model switching controls.
Designed to be embedded in OpenClaw canvas or viewed standalone.
"""

import json
import time
import logging

log = logging.getLogger("edge_llm.dashboard")

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>EdgeLLM Dashboard</title>
<style>
  :root {
    --bg: #0f1117; --card: #1a1d27; --border: #2a2d3a;
    --green: #22c55e; --red: #ef4444; --yellow: #eab308;
    --blue: #3b82f6; --text: #e2e8f0; --muted: #94a3b8;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Inter', -apple-system, sans-serif;
    background: var(--bg); color: var(--text);
    padding: 24px; min-height: 100vh;
  }
  .header {
    display: flex; align-items: center; gap: 16px;
    margin-bottom: 32px; padding-bottom: 24px;
    border-bottom: 1px solid var(--border);
  }
  .header h1 {
    font-size: 24px; font-weight: 700;
    background: linear-gradient(135deg, #3b82f6, #8b5cf6);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }
  .header .badge {
    padding: 4px 12px; border-radius: 20px;
    font-size: 12px; font-weight: 600;
    background: rgba(34,197,94,0.15); color: var(--green);
  }
  .header .badge.idle { background: rgba(148,163,184,0.15); color: var(--muted); }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }
  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px;
    transition: border-color 0.2s;
  }
  .card:hover { border-color: var(--blue); }
  .card h3 {
    font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--muted);
    margin-bottom: 12px;
  }
  .status-dot {
    display: inline-block; width: 8px; height: 8px;
    border-radius: 50%; margin-right: 8px;
  }
  .status-dot.green { background: var(--green); }
  .status-dot.red { background: var(--red); }
  .status-dot.yellow { background: var(--yellow); }
  .profile-name { font-size: 20px; font-weight: 600; margin-bottom: 4px; }
  .profile-desc { font-size: 13px; color: var(--muted); }
  .gpu-bar {
    height: 8px; background: var(--border); border-radius: 4px;
    margin-top: 12px; overflow: hidden;
  }
  .gpu-fill {
    height: 100%; border-radius: 4px;
    transition: width 0.5s ease, background 0.5s ease;
  }
  .gpu-fill.low { background: var(--green); }
  .gpu-fill.med { background: var(--yellow); }
  .gpu-fill.high { background: var(--red); }
  .services { display: flex; gap: 12px; margin-top: 8px; }
  .service-tag {
    padding: 4px 10px; border-radius: 6px;
    font-size: 12px; font-weight: 500;
    background: rgba(59,130,246,0.1); color: var(--blue);
  }
  .service-tag.active { background: rgba(34,197,94,0.1); color: var(--green); }
  .switcher { margin-bottom: 24px; }
  .switcher h3 {
    font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--muted);
    margin-bottom: 12px;
  }
  .switch-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
  .switch-btn {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px 12px; cursor: pointer;
    color: var(--text); transition: all 0.2s; text-align: center;
  }
  .switch-btn:hover { border-color: var(--blue); transform: translateY(-2px); }
  .switch-btn.active { border-color: var(--green); background: rgba(34,197,94,0.1); }
  .switch-btn .name { font-size: 13px; font-weight: 600; margin-bottom: 4px; }
  .switch-btn .desc { font-size: 10px; color: var(--muted); }
  .switch-btn .cost { font-size: 10px; color: var(--yellow); margin-top: 4px; }
  .switch-btn.loading {
    opacity: 0.6; pointer-events: none;
  }
  .history { margin-top: 24px; }
  .history h3 {
    font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--muted);
    margin-bottom: 12px;
  }
  .history-list { max-height: 200px; overflow-y: auto; }
  .history-item {
    display: flex; align-items: center; gap: 12px;
    padding: 10px 14px; background: var(--card);
    border: 1px solid var(--border); border-radius: 8px;
    margin-bottom: 6px; font-size: 12px;
  }
  .history-item .from { color: var(--red); }
  .history-item .arrow { color: var(--muted); }
  .history-item .to { color: var(--green); }
  .history-item .time { margin-left: auto; color: var(--muted); font-size: 11px; }
  .toast {
    position: fixed; bottom: 24px; right: 24px;
    padding: 12px 20px; border-radius: 8px;
    font-size: 13px; font-weight: 500;
    transform: translateY(100px); transition: transform 0.3s;
    z-index: 100;
  }
  .toast.show { transform: translateY(0); }
  .toast.success { background: rgba(34,197,94,0.2); color: var(--green); border: 1px solid var(--green); }
  .toast.error { background: rgba(239,68,68,0.2); color: var(--red); border: 1px solid var(--red); }
  .refresh-info { text-align: right; font-size: 11px; color: var(--muted); margin-bottom: 16px; }
</style>
</head>
<body>
  <div class="header">
    <h1>EdgeLLM</h1>
    <span class="badge" id="profileBadge">loading...</span>
  </div>
  <div class="refresh-info">Auto-refresh: 5s · Last: <span id="lastRefresh">-</span></div>

  <div class="grid">
    <div class="card">
      <h3>当前 Profile</h3>
      <div class="profile-name" id="currentProfile">-</div>
      <div class="profile-desc" id="currentDesc">-</div>
      <div class="services">
        <span class="service-tag" id="vllmTag">vLLM: -</span>
        <span class="service-tag" id="comfyuiTag">ComfyUI: -</span>
      </div>
    </div>
    <div class="card">
      <h3>GPU 显存</h3>
      <div style="display:flex;align-items:baseline;gap:8px;margin-top:8px">
        <span style="font-size:28px;font-weight:700" id="gpuUsed">-</span>
        <span style="font-size:13px;color:var(--muted)">/ 32607 MB</span>
      </div>
      <div class="gpu-bar">
        <div class="gpu-fill" id="gpuFill" style="width:0%"></div>
      </div>
      <div style="font-size:12px;color:var(--muted);margin-top:8px">
        <span id="gpuPercent">-</span> 已用
      </div>
    </div>
  </div>

  <div class="switcher">
    <h3>切换 Profile</h3>
    <div class="switch-grid" id="switchGrid"></div>
  </div>

  <div class="history">
    <h3>切换历史</h3>
    <div class="history-list" id="historyList"></div>
  </div>

  <div class="toast" id="toast"></div>

<script>
const API = '';
let profiles = [];
let history = [];
let switching = false;

function showToast(msg, type) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast ' + type + ' show';
  setTimeout(() => t.classList.remove('show'), 3000);
}

async function fetchJSON(path, options) {
  const resp = await fetch(API + path, options);
  return resp.json();
}

async function loadStatus() {
  const s = await fetchJSON('/status');
  document.getElementById('currentProfile').textContent = s.profile;
  document.getElementById('currentDesc').textContent = s.description || '';
  document.getElementById('vllmTag').textContent = 'vLLM: ' + s.vllm;
  document.getElementById('vllmTag').className = 'service-tag' + (s.vllm === '✅' ? ' active' : '');
  document.getElementById('comfyuiTag').textContent = 'ComfyUI: ' + s.comfyui;
  document.getElementById('comfyuiTag').className = 'service-tag' + (s.comfyui === '✅' ? ' active' : '');
  const badge = document.getElementById('profileBadge');
  badge.textContent = s.profile;
  badge.className = 'badge' + (s.profile === 'idle' ? ' idle' : '');
  const used = s.gpu_used_mb;
  const pct = (used / 32607 * 100).toFixed(1);
  document.getElementById('gpuUsed').textContent = used.toLocaleString();
  document.getElementById('gpuPercent').textContent = pct + '%';
  const fill = document.getElementById('gpuFill');
  fill.style.width = pct + '%';
  fill.className = 'gpu-fill ' + (pct < 50 ? 'low' : pct < 80 ? 'med' : 'high');
  document.getElementById('lastRefresh').textContent = new Date().toLocaleTimeString();
}

async function loadProfiles() {
  profiles = await fetchJSON('/profiles');
  const grid = document.getElementById('switchGrid');
  grid.innerHTML = '';
  for (const p of profiles) {
    const btn = document.createElement('div');
    btn.className = 'switch-btn' + (p.current ? ' active' : '');
    btn.id = 'btn-' + p.name;
    btn.innerHTML = '<div class="name">' + p.name + '</div>' +
                    '<div class="desc">' + p.description + '</div>' +
                    '<div class="cost">~' + p.switch_cost_sec + 's</div>';
    btn.onclick = () => doSwitch(p.name);
    grid.appendChild(btn);
  }
}

async function loadHistory() {
  try {
    const s = await fetchJSON('/status');
    // History is stored in DB; we get it via a dedicated endpoint or infer from switch results
    const list = document.getElementById('historyList');
    list.innerHTML = '<div style="padding:14px;color:var(--muted);font-size:12px">历史数据通过 CLI 查看: edge-llm history</div>';
  } catch(e) {}
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
      showToast('✅ 切换到 ' + name + ' (' + result.elapsed_sec + 's)', 'success');
    } else if (result.status === 'already_active') {
      showToast('ℹ️ 已在 ' + name + ' 上', 'success');
    } else {
      showToast('❌ ' + (result.message || 'switch failed'), 'error');
    }
    await loadStatus();
    await loadProfiles();
  } catch(e) {
    showToast('❌ ' + e.message, 'error');
  }
  switching = false;
  document.querySelectorAll('.switch-btn').forEach(b => b.classList.remove('loading'));
}

async function refresh() {
  await loadStatus();
  await loadProfiles();
  await loadHistory();
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""
