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
  --green:   #30D158; --green-s: rgba(48,209,88,.15);  --green-g: linear-gradient(135deg,#63E6BE,#30D158);
  --red:     #FF453A; --red-s:   rgba(255,69,58,.15);  --red-g:  linear-gradient(135deg,#FF6B6B,#FF453A);
  --orange:  #FF9F0A; --orange-s:rgba(255,159,10,.15); --orange-g:linear-gradient(135deg,#FFD60A,#FF9F0A);
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
  display:flex; flex-direction:column;
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

/* ── Service Chips ── */
.svc-chip {
  display:inline-flex; align-items:center; gap:5px;
  padding:4px 12px; border-radius:16px;
  font-size:13px; font-weight:600;
  transition:all .2s;
}
.svc-chip.active { background:var(--green-s); color:var(--green); }
.svc-chip.inactive { background:var(--bg); color:var(--text4); }
.svc-chip .chip-dot {
  width:6px; height:6px; border-radius:50%;
  background:var(--green); flex-shrink:0;
}
.svc-chip.inactive .chip-dot { background:var(--text4); }
.svc-empty { font-size:13px; color:var(--text4); }

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

/* ── vLLM Perf Panel ── */
.perf-panel {
  background:var(--card); border-radius:var(--radius);
  border:1px solid var(--border); box-shadow:var(--shadow);
  padding:18px 20px; margin-bottom:18px; transition:box-shadow .2s;
}
.perf-panel:hover { box-shadow:0 4px 16px rgba(0,0,0,.06); }
.perf-panel.sleeping { opacity:.5; }
.perf-hdr {
  display:flex; align-items:center; gap:10px; margin-bottom:14px;
}
.perf-refresh { font-size:11px; color:var(--text3); margin-left:auto; }
.perf-sleep-badge {
  padding:3px 10px; border-radius:8px;
  font-size:11px; font-weight:600; color:var(--purple);
  background:var(--purple-s); display:none;
}
.perf-cards {
  display:grid; grid-template-columns:repeat(5,1fr); gap:10px;
}
.perf-card {
  background:var(--green-s); border-radius:10px; padding:12px 14px;
  display:flex; flex-direction:column; gap:6px;
  transition:all .3s; position:relative;
}
.perf-card.warn { background:rgba(255,159,10,.15); }
.perf-card.crit { background:var(--red-s); }
.perf-label { font-size:15px; font-weight:700; color:var(--text2); letter-spacing:.02em; text-transform:uppercase; }
.perf-card.crit .perf-label { color:var(--red); }
.perf-main { font-size:28px; font-weight:700; letter-spacing:-.03em; color:var(--green); line-height:1; }
.perf-card.crit .perf-main { color:var(--red); }
.perf-card.warn .perf-main { color:var(--orange); }
.perf-sub { font-size:13px; color:var(--text2); font-weight:500; }
.perf-tip { display:none; position:absolute; bottom:calc(100% + 6px); left:0; right:0;
  background:var(--text1); color:var(--card); font-size:11px; line-height:1.4;
  padding:6px 10px; border-radius:8px; z-index:10; pointer-events:none;
  box-shadow:0 4px 12px rgba(0,0,0,.15); text-align:center;
  text-transform:none; letter-spacing:0; }
.perf-card:hover .perf-tip { display:block; }
.perf-bar { height:4px; border-radius:2px; background:rgba(0,0,0,.08); overflow:hidden; margin-top:2px; }
.perf-bar-f {
  height:100%; border-radius:2px; background:var(--green);
  transition:width .6s cubic-bezier(.4,0,.2,1), background .4s;
}
.perf-bar-f.warn { background:var(--orange); }
.perf-bar-f.crit { background:var(--red); }
@media (max-width:900px) { .perf-cards { grid-template-columns:repeat(3,1fr); } }
@media (max-width:600px) { .perf-cards { grid-template-columns:repeat(2,1fr); } }

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

  <!-- vLLM Perf Panel -->
  <div class="perf-panel" id="perfPanel" style="display:none">
    <div class="perf-hdr">
      <div class="panel-icon" style="background:var(--teal);box-shadow:0 2px 6px rgba(100,210,255,.2)">📊</div>
      <span class="panel-title" id="perfTitle">vLLM 性能</span>
      <span class="perf-sleep-badge" id="sleepBadge">休眠中</span>
      <span class="perf-refresh">刷新 60s</span>
    </div>
    <div class="perf-cards">
      <div class="perf-card" id="pcKv">
        <span class="perf-label">KV Cache</span>
        <span class="perf-tip">GPU KV 缓存占用率。>90% 容易触发抢占，导致延迟飙升</span>
        <span class="perf-main" id="kvVal">—</span>
        <div class="perf-bar"><div class="perf-bar-f" id="kvBar" style="width:0"></div></div>
      </div>
      <div class="perf-card" id="pcWait">
        <span class="perf-label">Waiting</span>
        <span class="perf-tip">排队中的请求数。0 表示无积压</span>
        <span class="perf-main" id="wVal">—</span>
        <span class="perf-sub" id="wSub"></span>
      </div>
      <div class="perf-card" id="pcPreempt">
        <span class="perf-label">Preempt</span>
        <span class="perf-tip">显存不足时强制驱逐已加载请求的次数（累计）。0 表示显存充足，>0 说明显存压力大</span>
        <span class="perf-main" id="pVal">—</span>
        <span class="perf-sub">cumulative</span>
      </div>
      <div class="perf-card" id="pcTtft">
        <span class="perf-label">TTFT</span>
        <span class="perf-tip">Time to First Token。从发送请求到收到第一个字的延迟（秒）。<1s 优秀</span>
        <span class="perf-main" id="tfVal">—</span>
        <span class="perf-sub" id="tfSub"></span>
      </div>
      <div class="perf-card" id="pcTput">
        <span class="perf-label">Throughput</span>
        <span class="perf-tip">每秒处理的总 Token 数（Prompt + Generation）。越高越好，反映 GPU 整体吞吐</span>
        <span class="perf-main" id="tpVal">—</span>
        <span class="perf-sub" id="tpSub"></span>
      </div>
    </div>
  </div>

  <!-- Active Services -->
  <div class="panel" id="svcCard" style="margin-bottom:18px;padding:16px 20px;min-height:40px"></div>

  <!-- Discovered Local Models -->
  <div class="panel" id="localModels" style="margin-bottom:18px;padding:16px 20px;display:none">
    <div style="font-size:14px;font-weight:600;color:var(--text2);margin-bottom:12px">📦 本地未配置模型</div>
    <div id="localModelsList"></div>
  </div>

  <!-- Model Panels -->
  <div class="panels">
    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-icon excl">🔒</div>
        <span class="panel-title">独占模型</span>
      </div>
      <div id="exclList" style="flex:1"></div>
    </div>
    <div class="panel">
      <div class="panel-hdr">
        <div class="panel-icon shrd">🔓</div>
        <span class="panel-title">共享服务</span>
      </div>
      <div id="shrdList" style="flex:1"></div>
    </div>
  </div>

  </div>
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
let sw = false, swT = 0;
function swLock() {
  // Safety: force-unlock after 30s to prevent permanent deadlock
  if(sw && Date.now()-swT>30000) { console.warn('sw lock forced unlock after 30s'); sw=false; }
  if(sw) return false;
  sw=true; swT=Date.now(); return true;
}
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

  // vLLM metrics: detect active vLLM service from services_info
  const svcInfo=s.services_info||{};
  let vPort=null, vName=null;
  for(const n of (s.active_services||[])){
    const info=svcInfo[n]||{};
    if(info.type==='vllm'&&info.port){ vPort=info.port; vName=n; break; }
  }
  if(vPort){
    clearInterval(vllmTimer);
    loadVllmMetrics(vPort,vName);
    vllmTimer=setInterval(()=>loadVllmMetrics(vPort,vName),60000);
  }else{
    document.getElementById('perfPanel').style.display='none';
    clearInterval(vllmTimer);
    vllmTimer=null;
  }

  // Active services row layout
  const svcs=s.active_services||[];
  const health=s.services_health||{};
  const sInfo=s.services_info||{};
  const svcCard=document.getElementById('svcCard');
  let svcHtml='<div style="display:flex;align-items:center;gap:10px;margin-bottom:'+(svcs.length?12:0)+'px"><div class="panel-icon" style="background:var(--blue-g);box-shadow:0 2px 6px rgba(10,132,255,.2)">📡</div><span class="panel-title">活跃服务</span></div>';
  if(svcs.length===0){
    svcHtml+='<span class="svc-empty">无活跃服务</span>';
  }else{
    for(const n of svcs){
      const h=health[n]||"❌";
      const ok=h==='✅';
      const info=sInfo[n]||{};
      const port=info.port||'—';
      const mode=info.mode||'?';
      const modeTag=mode==='exclusive'?'<span class="model-badge excl" style="padding:2px 8px;font-size:10px">独占</span>':'<span class="model-badge shrd" style="padding:2px 8px;font-size:10px">共享</span>';
      const sleepMatch=h.match(/sleeping [A-Z0-9]+/);
      const sleepLabel=sleepMatch?' <span style="color:var(--purple);font-size:11px;font-weight:500">⏸ '+sleepMatch[0]+'</span>':'';
      svcHtml+='<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--bg)">';
      svcHtml+='<span style="color:'+(ok?'var(--green)':'var(--red)')+';font-size:16px">'+(ok?'✓':'✗')+'</span>';
      svcHtml+='<span style="flex:1;font-size:14px;font-weight:600;color:var(--text1)">'+n+'</span>';
      svcHtml+='<span style="font-size:12px;color:var(--text3);font-variant-numeric:tabular-nums">:'+port+'</span>';
      svcHtml+=modeTag+sleepLabel+'</div>';
    }
  }
  svcCard.innerHTML=svcHtml;
}

// ── vLLM Performance ──
let _tput={pt:0,gt:0,ts:0};
let vllmTimer=null;

async function loadVllmMetrics(port,modelName) {
  const panel=document.getElementById('perfPanel');
  try {
    const m=await j('/vllm_metrics?port='+port);
    if(m.error){ panel.style.display='none'; return; }
    panel.style.display='';
    document.getElementById('perfTitle').textContent=modelName+' 性能';

    const sleeping=m.sleep_state===1;
    panel.className='perf-panel'+(sleeping?' sleeping':'');
    document.getElementById('sleepBadge').style.display=sleeping?'inline-block':'none';

    // KV Cache
    const kv=m.kv_cache_usage_perc??0;
    const kvCls=kv>90?'crit':kv>70?'warn':'';
    document.getElementById('kvVal').textContent=kv.toFixed(1)+'%';
    document.getElementById('kvBar').style.width=kv.toFixed(1)+'%';
    document.getElementById('kvBar').className='perf-bar-f'+(kvCls?' '+kvCls:'');
    document.getElementById('pcKv').className='perf-card'+(kvCls?' '+kvCls:'');

    // Waiting
    const w=m.num_requests_waiting??0;
    const r=m.num_requests_running??0;
    document.getElementById('wVal').textContent=w;
    document.getElementById('wSub').textContent=r+' 执行中';
    document.getElementById('pcWait').className='perf-card'+(w>0?' warn':'');

    // Preemptions
    const pre=m.num_preemptions??0;
    document.getElementById('pVal').textContent=pre;
    document.getElementById('pcPreempt').className='perf-card'+(pre>0?' crit':'');

    // TTFT — 运行期间平均值 (排除零值)
    const tf=m.ttft_seconds||{};
    if(m.ttft_cum_mean!=null) {
      document.getElementById('tfVal').textContent=m.ttft_cum_mean.toFixed(2)+'s';
      document.getElementById('tfSub').textContent='累计 '+m.ttft_cum_n+' 次 | P50 '+tf.p50?.toFixed(2)+'s | P95 '+tf.p95?.toFixed(2)+'s';
    } else if(tf.mean!=null) {
      document.getElementById('tfVal').textContent=tf.mean.toFixed(2)+'s';
      document.getElementById('tfSub').textContent='P50 '+tf.p50?.toFixed(2)+'s | P95 '+tf.p95?.toFixed(2)+'s | '+tf.count+' 次';
    } else {
      document.getElementById('tfVal').textContent='—';
      document.getElementById('tfSub').textContent='';
    }

    // Throughput — 运行期间平均值 (排除零值)
    if(m.throughput_cum!=null) {
      document.getElementById('tpVal').textContent=m.throughput_cum.toLocaleString();
      document.getElementById('tpSub').textContent='P '+m.prompt_tokens_cum?.toLocaleString()+' G '+m.generation_tokens_cum?.toLocaleString()+' t/s (累计 '+m.cum_n+' 次)';
    } else {
      document.getElementById('tpVal').textContent='—';
      document.getElementById('tpSub').textContent='';
    }
  }catch(e){ panel.style.display='none'; }
}

async function loadModels() {
  const [models, st] = await Promise.all([j('/models'), j('/status')]);
  const excl=models.filter(m=>m.mode==='exclusive');
  const shrd=models.filter(m=>m.mode==='shared');
  const sleepSt=st.sleep_states||{};
  const svcInfo=st.services_info||{};

  function renderCard(m, modeBadge) {
    const isVllm=m.type==='vllm';
    const info=svcInfo[m.name]||{};
    const port=info.port||'—';
    const sleeping=!!sleepSt[m.name];
    const active=m.active&&!sleeping;
    const cls='model-card'+(active?' active':'');

    let statusLine='<span style="color:var(--text4);font-size:12px">○ stopped</span>';
    if(active) statusLine='<span style="color:var(--green);font-size:12px;font-weight:600">✅ running</span>';
    else if(sleeping) statusLine='<span style="color:var(--purple);font-size:12px;font-weight:600">⏸ sleeping</span>';

    let btns='';
    if(active){
      btns+='<button class="btn-card stop" onclick="event.stopPropagation();doRelease(\''+m.name+'\','+(m.mode==='exclusive')+')">释放</button>';
      if(isVllm) btns+='<button class="btn-card start" onclick="event.stopPropagation();doSleep(\''+m.name+'\')">休眠</button>';
    }else if(sleeping){
      btns+='<button class="btn-card stop" onclick="event.stopPropagation();doRelease(\''+m.name+'\','+(m.mode==='exclusive')+')">释放</button>';
      if(isVllm) btns+='<button class="btn-card start" onclick="event.stopPropagation();doWake(\''+m.name+'\')">唤醒</button>';
    }else{
      btns+='<button class="btn-card start" onclick="event.stopPropagation();doSwitch(\''+m.name+'\')">启动</button>';
    }

    return '<div class="'+cls+'" id="sw-'+m.name+'">'+
      '<div class="model-top">'+
        '<div class="model-dot"></div>'+
        '<div class="model-info"><div class="model-name">'+m.name+'</div>'+
          '<div style="font-size:11px;color:var(--text3);margin-top:3px">'+statusLine+' <span style="margin-left:6px;font-variant-numeric:tabular-nums;color:var(--text4)">:'+port+'</span></div>'+
        '</div>'+
        '<span class="model-badge '+modeBadge+'">'+modeBadge+'</span>'+
      '</div>'+
      '<div class="model-actions">'+btns+'</div>'+
    '</div>';
  }

  document.getElementById('exclList').innerHTML=excl.map(m=>renderCard(m,'excl')).join('');
  document.getElementById('shrdList').innerHTML=shrd.map(m=>renderCard(m,'shrd')).join('');
}

async function doRelease(n,isExcl) {
  if(!swLock())return;
  try{
    // P0-3: For shared models, stop then check if idle needed
    if(isExcl) {
      const r = await j('/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:'idle'})});
      if(r.status==='switched') toast(n+' 已释放','ok');
      else toast(r.message||'失败','err');
    } else {
      const r = await j('/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
      if(r.status==='stopped') {
        // P0-3: Check if we should transition to idle
        const status = await j('/status');
        if(status.active_services && status.active_services.length === 0) {
          await j('/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:'idle'})});
          toast(n+' 已释放 → idle','ok');
        } else {
          toast(n+' 已释放','ok');
        }
      } else {
        toast(r.message||'失败','err');
      }
    }
  }catch(e){toast(e.message,'err');}
  finally{sw=false;}
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

async function doSleep(n) {
  if(!swLock())return;
  try{
    const r=await j('/sleep',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if(r.status==='ok') toast(n+' 休眠 ✓','ok');
    else if(r.status==='already_sleeping') toast(n+' 已在休眠','info');
    else toast(r.message||'失败','err');
  }catch(e){toast(e.message,'err');}
  finally{sw=false;}
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

async function doWake(n) {
  if(!swLock())return;
  try{
    const r=await j('/wake',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if(r.status==='ok') toast(n+' 已唤醒 ✓','ok');
    else if(r.status==='already_awake') toast(n+' 未休眠','info');
    else toast(r.message||'失败','err');
  }catch(e){toast(e.message,'err');}
  finally{sw=false;}
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

async function doSwitch(n) {
  if(!swLock())return;
  try{
    const r=await j('/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if(r.status==='switched') toast(n+' ✓ '+r.elapsed_sec+'s','ok');
    else if(r.status==='config_changed_restart') toast('配置已变更，'+n+' 正在重启','ok');
    else if(r.status==='already_active') toast('已在 '+n,'info');
    else toast(r.message||'失败','err');
  }catch(e){toast(e.message,'err');}
  finally{sw=false;}
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

async function doStop(n) {
  if(!swLock())return;
  try{
    const r=await j('/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:n})});
    if(r.status==='stopped') toast(n+' 已停止','ok');
    else if(r.status==='already_stopped') toast(n+' 未运行','info');
    else toast(r.message||'停止失败','err');
  }catch(e){toast(e.message,'err');}
  finally{sw=false;}
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

async function loadLocalModels() {
  try {
    const d = await j('/local-models');
    const list = d.discovered || [];
    const el = document.getElementById('localModels');
    const listEl = document.getElementById('localModelsList');
    if (list.length === 0) {
      el.style.display = 'none';
      return;
    }
    el.style.display = 'block';
    let html = '';
    for (const m of list) {
      const gb = m.size_mb >= 1024 ? (m.size_mb/1024).toFixed(1)+' GB' : m.size_mb+' MB';
      html += '<div class="model-card" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">';
      html += '<div><div class="model-name" style="font-size:14px">'+m.name+'</div>';
      html += '<div style="font-size:11px;color:var(--text3);margin-top:2px">'+m.path+' · '+gb+' · '+m.type+'</div></div>';
      html += '<button class="btn-card start" onclick="event.stopPropagation();doDeploy(\''+m.name+'\',\''+m.type+'\')">Deploy</button>';
      html += '</div>';
    }
    listEl.innerHTML = html;
  } catch(e) { /* ignore */ }
}

async function doDeploy(name, type) {
  if (!swLock()) return;
  try {
    const r = await j('/deploy', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name,type:type})});
    if (r.status === 'switched' || r.status === 'already_active') {
      toast(name+' 已部署 ✓', 'ok');
    } else {
      toast(r.message || '部署失败', 'err');
    }
  } catch(e) { toast(e.message, 'err'); }
  finally { sw = false; }
  await Promise.all([load(), loadModels(), loadLocalModels()]);
}

async function doReset() {
  if(!confirm('强制重置到 idle？'))return;
  const r=await j('/reset',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  toast(r.status==='reset'?'已重置 ✓':'失败',r.status==='reset'?'ok':'err');
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

async function doReconcile() {
  const r=await j('/reconcile',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
  const a=r.actions||[];
  toast(a.length===0?'状态一致 ✓':'修复: '+a.join('; '),'ok');
  await Promise.all([load(),loadModels(),loadLocalModels()]);
}

Promise.all([load(),loadModels(),loadLocalModels()]);
setInterval(()=>{load();loadModels();loadLocalModels();},5000);
</script>
</body>
</html>"""