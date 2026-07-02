"""
edge_llm/proxy.py — Robust auto-routing proxy + web dashboard.

v4.0: Model-plugin architecture. No more Profile concept.
      Dynamic model lookup from models.d/ via find_model_by_served_name().

v3.2 fixes preserved:
  - shutdown deadlock: handle_request() loop + Event flag
  - BrokenPipeError: silently caught in all response paths
  - streaming proxy: robust chunk forwarding
  - systemd watchdog: independent thread
  - connection reuse: HTTPConnection per upstream
"""

import sys
import os
import signal
import socket
import logging
import json
import http.server
import socketserver
import threading
import time
from pathlib import Path
from http.client import HTTPConnection

sys.path.insert(0, str(Path(__file__).parent.parent))
from edge_llm.manager import ModelManager
from edge_llm.state import GPUMode, ProfileState
from edge_llm.config import load_aliases

log = logging.getLogger("edge_llm.proxy")

# ─── Config ──────────────────────────────────────────────────────

PROXY_HOST = os.environ.get("EDGE_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("EDGE_PROXY_PORT", "8999"))
AUTO_SWITCH = os.environ.get("EDGE_AUTO_SWITCH", "1") == "1"
HEALTH_CHECK_INTERVAL = int(os.environ.get("EDGE_HEALTH_CHECK", "60"))
WATCHDOG_INTERVAL = 20


# ─── Proxy Manager ──────────────────────────────────────────────

class ProxyManager:
    """Manages model switching + request routing (v4.0: model-plugin)."""

    def __init__(self):
        self.mgr = ModelManager()
        self._aliases = load_aliases()
        self._last_switch = 0.0
        self._cooldown = 10
        self._switch_lock = threading.Lock()
        self._upstream_pool: dict[int, HTTPConnection] = {}
        self._pool_lock = threading.Lock()
        self._cum = self._make_cum()
        if self._aliases:
            log.info("Loaded %d model aliases: %s", len(self._aliases), list(self._aliases.keys()))

    @staticmethod
    def _make_cum():
        return {"ttft_sum": 0.0, "ttft_n": 0,
                "tput_sum": 0.0, "tput_n": 0,
                "pt_sum": 0.0, "pt_n": 0,
                "gt_sum": 0.0, "gt_n": 0}

    def reset_cum(self):
        """Reset cumulative accumulators (called after switch/reset)."""
        self._cum = self._make_cum()

    @property
    def current(self) -> str:
        """Current active service or 'idle'."""
        return self.mgr.current_service

    def model_to_service(self, model_name: str):
        """Map served_model_name to model config name. Resolves aliases first."""
        # Step 1: resolve aliases (fast → llama3-8b)
        resolved = self._aliases.get(model_name, model_name)
        # Step 2: find by served_name
        m = self.mgr.find_model_by_served_name(resolved)
        if m:
            log.debug("model_to_service: %s → %s (served=%s)", model_name, resolved, m.name)
            return m.name
        # Step 3: fallback — also try original name
        if resolved != model_name:
            m2 = self.mgr.find_model_by_served_name(model_name)
            if m2:
                return m2.name
        return None

    def _wait_healthy(self, target: str, timeout: float = 180) -> bool:
        """Wait for a model to become healthy after switch. Works across all backend types."""
        model = self.mgr.get_model(target)
        if not model:
            return False
        port = model.port
        if not port:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                if resp.status == 200:
                    log.info("Model %s healthy on :%d", target, port)
                    return True
            except Exception:
                pass
            time.sleep(2)
        log.warning("Model %s not healthy after %.0fs", target, timeout)
        return False

    def ensure_service(self, target: str) -> bool:
        """Ensure a model is running, auto-switch if needed."""
        if target in self.mgr.active_services:
            return True
        if self.mgr.state.is_manually_stopped(target):
            log.info("Auto-switch to %s blocked: manually stopped by user", target)
            return False
        if not self._switch_lock.acquire(timeout=30):
            log.warning("Switch lock timeout, skipping")
            return False
        try:
            if time.time() - self._last_switch < self._cooldown:
                log.warning("Switch cooldown active, skipping")
                return False
            log.info("Auto-switch → %s", target)
            result = self.mgr.switch(target)
            self._last_switch = time.time()
            if result["status"] == "switched":
                # Wait for model to become healthy before returning
                return self._wait_healthy(target)
            return result["status"] in ("switched", "already_active")
        finally:
            self._switch_lock.release()

    def get_target_port(self, model_name: str):
        """Get port for a served_model_name. Works across all backend types."""
        m = self.mgr.find_model_by_served_name(model_name)
        return m.port if m else None

    def get_upstream(self, port: int) -> HTTPConnection:
        """Get or create a keep-alive HTTPConnection to upstream.

        Uses lazy invalidation: return cached connection without probing.
        If the connection fails during use, caller should invalidate_upstream().
        """
        with self._pool_lock:
            conn = self._upstream_pool.get(port)
            if conn:
                return conn
            conn = HTTPConnection("127.0.0.1", port, timeout=300)
            self._upstream_pool[port] = conn
            return conn

    def invalidate_upstream(self, port: int):
        """Remove and close a stale upstream connection."""
        with self._pool_lock:
            conn = self._upstream_pool.pop(port, None)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def health_check(self):
        try:
            s = self.mgr.status()
            log.info("Health check: gpu_mode=%s services=%s",
                     s.get("gpu_mode"), s.get("active_services"))
            # Check for unhealthy services in healthy state
            for svc, health in s.get("services_health", {}).items():
                if health == "❌" and s.get("gpu_mode") != GPUMode.IDLE:
                    log.warning("%s unhealthy but GPU not idle — use `edge-llm reconcile`", svc)
            # Clean up expired manual_stop records
            self._clean_manual_stops()
        except Exception as e:
            log.error("Health check exception: %s", e)

    def _clean_manual_stops(self):
        """Remove expired manual_stop records from StateDB."""
        try:
            stops = json.loads(self.mgr.state.get("manual_stops") or "{}")
            expired = [k for k, v in stops.items() if time.time() - v > self.mgr.state.MANUAL_STOP_TTL]
            if expired:
                for k in expired:
                    del stops[k]
                self.mgr.state.set("manual_stops", json.dumps(stops))
                log.debug("Cleaned %d expired manual_stop records", len(expired))
        except Exception as e:
            log.debug("Manual stop cleanup error: %s", e)


# ─── HTTP Handler ───────────────────────────────────────────────

class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.debug("[proxy] " + fmt, *args)

    @property
    def proxy(self):
        return self.server.proxy_mgr

    def _safe_write(self, data: bytes):
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_OPTIONS(self):
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
        except Exception:
            pass

    def do_GET(self):
        pm = self.proxy
        try:
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            if path == "/":
                self._serve_dashboard()
            elif self.path == "/health":
                self._send_json({"status": "ok", "gpu_mode": pm.mgr.gpu_mode})
            elif self.path == "/status":
                self._send_json(pm.mgr.status())
            elif self.path in ("/models", "/profiles"):  # /profiles backward compat
                self._send_json(pm.mgr.list_models())
            elif self.path == "/v1/models":
                self._handle_v1_models(pm)
            elif self.path == "/system":
                self._send_json(self._system_info())
            elif self.path == "/history":
                self._send_json(pm.mgr.state.get_history(limit=30))
            elif path == "/vllm_metrics":
                self._handle_vllm_metrics(pm)
            else:
                self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.error("GET %s error: %s", self.path, e)

    def do_POST(self):
        pm = self.proxy
        try:
            from urllib.parse import urlparse
            path = urlparse(self.path).path
            if path in ("/v1/chat/completions", "/v1/completions"):
                self._handle_chat(pm)
            elif path == "/switch":
                self._handle_switch(pm)
            elif path == "/stop":
                self._handle_stop(pm)
            elif path == "/sleep":
                self._handle_sleep(pm)
            elif path == "/wake":
                self._handle_wake(pm)
            elif path == "/reset":
                self._handle_reset(pm)
            elif path == "/reconcile":
                self._handle_reconcile(pm)
            else:
                self._send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.error("POST %s error: %s", self.path, e)

    def _serve_dashboard(self):
        body = None
        try:
            from edge_llm.dashboard import DASHBOARD_HTML
            body = DASHBOARD_HTML.encode("utf-8")
        except ImportError:
            pass
        if body is None:
            body = (
                "<!DOCTYPE html><html><head><title>EdgeLLM</title>"
                "<style>body{font-family:sans-serif;background:#0f1117;color:#e2e8f0;padding:24px}"
                "h1{color:#3b82f6}</style></head><body>"
                "<h1>EdgeLLM</h1><p>Dashboard unavailable. Use <code>edge-llm status</code></p>"
                "</body></html>"
            ).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self._safe_write(body)

    def _handle_v1_models(self, pm):
        """Forward /v1/models to active upstream services.

        Aggregates model lists from all active backends (vLLM, Ollama, etc.).
        """
        active = list(pm.mgr.active_services)
        if not active:
            models_d = pm.mgr.list_models()
            self._send_json(models_d)
            return

        # Collect models from all active services across all backend types
        all_models = []
        for svc in active:
            model = pm.mgr.get_model(svc)
            if not model or not model.port:
                continue
            port = model.port
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", "/v1/models")
                resp = conn.getresponse()
                body = resp.read()
                conn.close()
                if resp.status == 200:
                    data = json.loads(body)
                    all_models.extend(data.get("data", []))
            except Exception as e:
                log.error("/v1/models forward failed for %s (port %d): %s", svc, port, e)

        if all_models:
            self._send_json({"object": "list", "data": all_models})
        else:
            self._send_json({"error": "no upstream available"}, 503)

    def _handle_vllm_metrics(self, pm):
        """Fetch and parse vLLM Prometheus /metrics endpoint."""
        import urllib.request
        import math
        from urllib.parse import urlparse, parse_qs

        qs = parse_qs(urlparse(self.path).query)
        try:
            port = int(qs.get("port", ["8000"])[0])
        except (ValueError, IndexError):
            self._send_json({"error": "invalid port"}, 400)
            return

        url = f"http://127.0.0.1:{port}/metrics"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                text = resp.read().decode("utf-8")
        except Exception as e:
            self._send_json({"error": str(e)}, 502)
            return

        # Parse Prometheus text format
        gauges, counters, histos = {}, {}, {}

        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Extract name and value
            bracket = line.find("{")
            if bracket >= 0:
                name = line[:bracket]
                close = line.rfind("}")
                val_str = line[close+1:].strip() if close > bracket else ""
            else:
                parts = line.split()
                if len(parts) < 2:
                    continue
                name, val_str = parts[0], parts[1]

            try:
                val = float(val_str)
            except ValueError:
                continue

            # Classify
            if name.endswith("_bucket"):
                base = name[:-7]
                le_start = line.find('le="', bracket) if bracket >= 0 else -1
                le_end = line.find('"', le_start + 4) if le_start >= 0 else -1
                le_val = float(line[le_start+4:le_end]) if le_start >= 0 else math.inf
                if base not in histos:
                    histos[base] = {"buckets": [], "sum": 0.0, "count": 0}
                histos[base]["buckets"].append((le_val, int(val)))
            elif name.endswith("_sum"):
                base = name[:-4]
                if base not in histos:
                    histos[base] = {"buckets": [], "sum": 0.0, "count": 0}
                histos[base]["sum"] = val
            elif name.endswith("_count"):
                base = name[:-6]
                if base not in histos:
                    histos[base] = {"buckets": [], "sum": 0.0, "count": 0}
                histos[base]["count"] = int(val)
            elif "_total" in name:
                counters[name.rsplit("_total", 1)[0]] = val
            else:
                gauges[name] = val

        def _quantile(buckets, count, q):
            if count == 0 or not buckets:
                return None
            target = count * q
            sorted_bk = sorted(buckets, key=lambda x: x[0])
            cum = 0
            for i, (le, c) in enumerate(sorted_bk):
                cum = c
                if cum >= target:
                    if i == 0:
                        return le / 2
                    prev_le, prev_c = sorted_bk[i - 1]
                    if math.isfinite(le) and c > prev_c:
                        return prev_le + (le - prev_le) * (target - prev_c) / (c - prev_c)
                    return prev_le
            return sorted_bk[-1][0]

        result = {}

        # Gauges
        gauge_map = {
            "vllm:kv_cache_usage_perc": ("kv_cache_usage_perc", lambda v: round(v * 100, 1)),
            "vllm:num_requests_waiting": ("num_requests_waiting", int),
            "vllm:num_requests_running": ("num_requests_running", int),
            "vllm:engine_sleep_state": ("sleep_state", int),
        }
        for prom, (key, fn) in gauge_map.items():
            v = gauges.get(prom)
            if v is not None:
                result[key] = fn(v)

        # Counters
        counter_map = {
            "vllm:num_preemptions": "num_preemptions",
            "vllm:prompt_tokens": "prompt_tokens",
            "vllm:generation_tokens": "generation_tokens",
        }
        for prom, key in counter_map.items():
            v = counters.get(prom)
            if v is not None:
                result[key] = int(v)

        # TTFT histogram (cumulative since start)
        # TTFT — running average of non-zero samples
        h = histos.get("vllm:time_to_first_token_seconds")
        if h and h["count"] > 0:
            result["ttft_seconds"] = {
                "p50": round(_quantile(h["buckets"], h["count"], 0.50), 3),
                "p95": round(_quantile(h["buckets"], h["count"], 0.95), 3),
                "mean": round(h["sum"] / h["count"], 3),
                "count": h["count"],
            }

        # Running accumulators for cumulative averages (stored on pm)
        if not hasattr(pm, "_cum"):
            pm._cum = pm._make_cum()

        cum = pm._cum
        # TTFT cumulative average (use p50 as sample)
        if h and h["count"] > 0:
            sample = h["sum"] / h["count"]  # mean TTFT this snapshot
            if sample > 0:
                cum["ttft_sum"] += sample
                cum["ttft_n"] += 1

        # Throughput cumulative average
        pt = result.get("prompt_tokens", 0)
        gt = result.get("generation_tokens", 0)
        if gt > 0:  # generation tokens > 0 means work is being done
            # Calculate instant throughput from gauge values
            total = pt + gt
            cum["tput_sum"] += total
            cum["tput_n"] += 1
            cum["pt_sum"] += pt
            cum["pt_n"] += 1
            cum["gt_sum"] += gt
            cum["gt_n"] += 1

        if cum["tput_n"] > 0:
            result["throughput_cum"] = round(cum["tput_sum"] / cum["tput_n"], 1)
            result["prompt_tokens_cum"] = round(cum["pt_sum"] / cum["pt_n"], 1)
            result["generation_tokens_cum"] = round(cum["gt_sum"] / cum["gt_n"], 1)
            result["cum_n"] = cum["tput_n"]
        if cum["ttft_n"] > 0:
            result["ttft_cum_mean"] = round(cum["ttft_sum"] / cum["ttft_n"], 3)
            result["ttft_cum_n"] = cum["ttft_n"]

        self._send_json(result)

    def _system_info(self):
        info = {"cpu_percent": 0, "cpu_cores": os.cpu_count() or 1,
                "ram_total_gb": 0, "ram_used_gb": 0, "uptime_seconds": 0}
        try:
            with open("/proc/meminfo") as f:
                mem = f.read()
            total_kb = int([l for l in mem.splitlines() if l.startswith("MemTotal")][0].split()[1])
            avail_kb = int([l for l in mem.splitlines() if l.startswith("MemAvailable")][0].split()[1])
            info["ram_total_gb"] = round(total_kb / 1024**2, 1)
            info["ram_used_gb"] = round((total_kb - avail_kb) / 1024**2, 1)
        except Exception:
            pass
        try:
            with open("/proc/loadavg") as f:
                loadavg = f.read().split()[0]
            info["cpu_percent"] = round(float(loadavg) / info["cpu_cores"] * 100, 1)
        except Exception:
            pass
        try:
            with open("/proc/uptime") as f:
                info["uptime_seconds"] = int(float(f.read().split()[0]))
        except Exception:
            pass
        return info

    def _read_body(self):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                return {}
            if content_length > 10 * 1024 * 1024:  # 10MB limit
                self._send_json({"error": "payload too large (max 10MB)"}, 413)
                return None
            body = self.rfile.read(content_length)
            return json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json({"error": f"Invalid JSON: {e}"}, 400)
            return None

    def _handle_chat(self, pm):
        data = self._read_body()
        if data is None:
            return

        # Enable Qwen3 thinking/reasoning mode by default (vLLM only).
        # The --reasoning-parser qwen3 correctly splits output into
        # reasoning + content fields when thinking is enabled.
        # Only override when the caller explicitly sets enable_thinking.
        svc_name = pm.model_to_service(model)
        model_obj = pm.mgr.get_model(svc_name) if svc_name else None
        if model_obj and model_obj.is_vllm:
            if "chat_template_kwargs" not in data:
                data["chat_template_kwargs"] = {}
            if "enable_thinking" not in data.get("chat_template_kwargs", {}):
                data["chat_template_kwargs"]["enable_thinking"] = True

        model = data.get("model", "vllm_qwen27b")
        stream = data.get("stream", False)

        # Dynamic model lookup
        service_name = pm.model_to_service(model)
        if service_name and AUTO_SWITCH:
            switched = pm.ensure_service(service_name)
            if not switched and service_name not in pm.mgr.active_services:
                if pm.mgr.state.is_manually_stopped(service_name):
                    reason = f"{service_name} was manually stopped — auto-switch blocked for {pm.mgr.state.MANUAL_STOP_TTL}s"
                else:
                    reason = f"tri-state rule violation or switch in progress"
                self._send_json({"error": f"Cannot switch to {reason}"}, 503)
                return

        target_port = pm.get_target_port(model)
        if not target_port:
            self._send_json({"error": f"Unknown model: {model}"}, 404)
            return

        body = json.dumps(data).encode("utf-8")
        # P1-1: Connection health check — invalidate if stale
        try:
            conn = pm.get_upstream(target_port)
            conn.request("POST", self.path, body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
        except Exception as e:
            log.error("Forward to :%d failed: %s", target_port, e)
            pm.invalidate_upstream(target_port)
            # P1-1: Retry once with fresh connection
            try:
                conn = pm.get_upstream(target_port)
                conn.request("POST", self.path, body=body,
                             headers={"Content-Type": "application/json"})
                resp = conn.getresponse()
            except Exception as e2:
                log.error("Retry also failed: %s", e2)
                self._send_json({"error": str(e2)}, 502)
                return

        try:
            resp_status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            resp_ct = resp_headers.get("content-type", "application/json")
            headers_sent = False

            if stream:
                headers_sent = True
                self.send_response(resp_status)
                self.send_header("Content-Type", resp_ct)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    while True:
                        chunk = resp.read(8192)
                        if not chunk:
                            break
                        size = f"{len(chunk):x}\r\n".encode()
                        self._safe_write(size)
                        self._safe_write(chunk)
                        self._safe_write(b"\r\n")
                    self._safe_write(b"0\r\n\r\n")
                except Exception as e:
                    log.debug("Stream forwarding interrupted: %s", e)
                    # Headers already sent — just terminate chunked encoding and close
                    try:
                        self._safe_write(b"0\r\n\r\n")
                    except Exception:
                        pass
            else:
                resp_body = resp.read()
                self.send_response(resp_status)
                self.send_header("Content-Type", resp_ct)
                self.send_header("Content-Length", str(len(resp_body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self._safe_write(resp_body)

            try:
                resp.read()
            except Exception:
                pass

        except Exception as e:
            log.error("Error forwarding response: %s", e)
            if not headers_sent:
                try:
                    self._send_json({"error": str(e)}, 502)
                except Exception:
                    pass
            else:
                # Headers already sent (streaming mode) — just close
                try:
                    self._safe_write(b"0\r\n\r\n")
                except Exception:
                    pass

    def _handle_switch(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("model") or data.get("profile")  # accept both
        if not target:
            self._send_json({"error": "Missing model"}, 400)
            return
        # Record manual stop for services being replaced
        if target == "idle":
            for svc in list(pm.mgr.active_services):
                pm.mgr.state.record_manual_stop(svc)
        elif target != "idle":
            # Clear manual stop for target (user explicitly wants it)
            pm.mgr.state.clear_manual_stop(target)
        result = pm.mgr.switch(target)
        # Reset cumulative accumulators after switch
        pm.reset_cum()
        self._send_json(result)

    def _handle_stop(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("model")
        if not target:
            self._send_json({"error": "Missing model"}, 400)
            return
        result = pm.mgr.stop_service(target)
        if result.get("status") in ("stopped", "already_stopped"):
            pm.mgr.state.record_manual_stop(target)
        self._send_json(result)

    def _handle_reset(self, pm):
        for svc in list(pm.mgr.active_services):
            pm.mgr.state.record_manual_stop(svc)
        pm.mgr.force_reset()
        pm.reset_cum()
        self._send_json({"status": "reset", "gpu_mode": GPUMode.IDLE})

    def _handle_sleep(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("model")
        if not target:
            self._send_json({"error": "Missing model"}, 400)
            return
        result = pm.mgr.sleep_model(target)
        self._send_json(result)

    def _handle_wake(self, pm):
        data = self._read_body()
        if data is None:
            return
        target = data.get("model")
        if not target:
            self._send_json({"error": "Missing model"}, 400)
            return
        result = pm.mgr.wake_model(target)
        self._send_json(result)

    def _handle_reconcile(self, pm):
        result = pm.mgr.reconcile()
        self._send_json(result)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self._safe_write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# ─── Threaded HTTP Server ───────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    timeout = 1.0


# ─── Main ─────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    # Rotating file handler for persistent logs
    log_dir = Path.home() / ".edge_llm" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler(log_dir / "proxy.log", maxBytes=10_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
    logging.getLogger("edge_llm").addHandler(fh)

    mgr = ProxyManager()
    shutdown_event = threading.Event()

    server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
    server.proxy_mgr = mgr

    _notify_socket = os.environ.get('NOTIFY_SOCKET')
    _notify_enabled = bool(_notify_socket)

    def sd_notify(message: str):
        if not _notify_socket:
            return
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            sock.connect(_notify_socket)
            sock.sendall(message.encode())
            sock.close()
        except Exception:
            pass

    def handle_signal(signum, frame):
        log.info("Received signal %s, initiating shutdown", signum)
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    def watchdog_loop():
        while not shutdown_event.is_set():
            shutdown_event.wait(WATCHDOG_INTERVAL)
            if not shutdown_event.is_set():
                sd_notify("WATCHDOG=1")

    if _notify_enabled:
        threading.Thread(target=watchdog_loop, daemon=True, name="watchdog").start()

    def health_loop():
        while not shutdown_event.is_set():
            shutdown_event.wait(HEALTH_CHECK_INTERVAL)
            if not shutdown_event.is_set():
                mgr.health_check()

    # Auto-reconcile on startup: sync state with actual running services
    try:
        rec = mgr.mgr.reconcile()
        if rec.get("actions"):
            log.info("Startup reconcile: %s", rec["actions"])
    except Exception as e:
        log.warning("Startup reconcile failed: %s", e)

    threading.Thread(target=health_loop, daemon=True, name="health").start()

    sd_notify("READY=1")
    log.info("EdgeLLM Proxy: %s:%d (auto_switch=%s, threaded, v4.0)",
             PROXY_HOST, PROXY_PORT, AUTO_SWITCH)
    log.info("Dashboard: http://%s:%d/", PROXY_HOST, PROXY_PORT)
    log.info("GPU mode: %s | Services: %s", mgr.mgr.gpu_mode, mgr.mgr.active_services)

    try:
        while not shutdown_event.is_set():
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("Closing server...")
        try:
            server.server_close()
        except Exception:
            pass
        for port, conn in mgr._upstream_pool.items():
            try:
                conn.close()
            except Exception:
                pass
        sd_notify("STOPPING=1")
        log.info("Shutdown complete")


if __name__ == "__main__":
    main()
