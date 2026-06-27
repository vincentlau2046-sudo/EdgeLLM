"""
edge_llm/proxy.py — Lightweight auto-routing proxy + web dashboard.

Sits between OpenClaw and vLLM:
1. Receives OpenAI-compatible /v1/chat/completions request
2. Checks which model is being requested
3. Auto-switches profile if needed
4. Forwards to the correct vLLM instance
5. Serves web dashboard at /

v3.0 changes:
  - ThreadingHTTPServer (no more single-thread blocking)
  - Fixed non-streaming proxy bug (double read)
  - Uses dashboard.py HTML (not inline fallback)
  - Adapted to new ProfileManager API (profile_state, vllm_pid)
  - Health check logs only, never auto-restarts
"""

import sys
import os
import signal
import logging
import urllib.request
import json
import http.server
import socketserver
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from edge_llm.profile_manager import ProfileManager, ProfileState

log = logging.getLogger("edge_llm.proxy")

# ─── Config ──────────────────────────────────────────────────────

PROXY_HOST = os.environ.get("EDGE_PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("EDGE_PROXY_PORT", "8999"))
AUTO_SWITCH = os.environ.get("EDGE_AUTO_SWITCH", "1") == "1"
HEALTH_CHECK_INTERVAL = int(os.environ.get("EDGE_HEALTH_CHECK", "60"))


# ─── Proxy Manager ──────────────────────────────────────────────

class ProxyManager:
    """Manages profile switching + request routing."""

    def __init__(self):
        self.mgr = ProfileManager()
        self._last_switch = 0.0
        self._cooldown = 10  # min seconds between switches
        self._switch_lock = threading.Lock()

    @property
    def current(self) -> str:
        return self.mgr.current_profile

    def model_to_profile(self, model_name: str):
        model_map = {
            "vllm_qwen27b": "qw36_full",
            "vllm_qw35_gptq": "qw35_comfyui",
            "vllm_gemma26b_nvfp4": "gemma_full",
        }
        return model_map.get(model_name)

    def ensure_profile(self, target: str) -> bool:
        """Thread-safe profile switching with cooldown."""
        if self.current == target:
            return True
        if not self._switch_lock.acquire(blocking=False):
            log.warning("Switch already in progress, skipping")
            return False
        try:
            if time.time() - self._last_switch < self._cooldown:
                log.warning("Switch cooldown active, skipping")
                return False
            log.info("Auto-switch: %s → %s", self.current, target)
            result = self.mgr.switch(target)
            self._last_switch = time.time()
            return result["status"] == "switched"
        finally:
            self._switch_lock.release()

    def get_target_port(self, model_name: str):
        for name, p in self.mgr._profiles.items():
            if p.vllm and p.vllm.served_name == model_name:
                return p.vllm.port
        return None

    def health_check(self):
        """Background health monitor: LOG ONLY, never auto-restart.
        Auto-restart was too aggressive — it kills processes during model loading
        (when /health returns 503), causing infinite kill/restart loops.
        Use `edge-llm switch <profile>` or `edge-llm reconcile` manually instead."""
        try:
            s = self.mgr.status()
            log.info("Health check: profile=%s state=%s vllm=%s comfyui=%s",
                     s["profile"], s.get("state", "?"), s["vllm"], s["comfyui"])
            p = self.mgr._profiles.get(s["profile"])
            if p and p.vllm and s["vllm"] == "❌" and s.get("state") == ProfileState.HEALTHY:
                log.warning("vLLM unhealthy but state=healthy — use `edge-llm reconcile` to fix")
            if p and p.comfyui and s["comfyui"] == "❌" and s.get("state") == ProfileState.HEALTHY:
                log.warning("ComfyUI unhealthy but state=healthy — use `edge-llm reconcile` to fix")
        except Exception as e:
            log.error("Health check exception: %s", e)


# ─── HTTP Handler ───────────────────────────────────────────────

class ProxyHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.debug("[proxy] " + fmt, *args)

    @property
    def proxy(self):
        """Resolve proxy manager from the server instance."""
        return self.server.proxy_mgr

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        pm = self.proxy
        if self.path == "/":
            self._serve_dashboard()
        elif self.path == "/health":
            self._send_json({"status": "ok", "profile": pm.current})
        elif self.path == "/status":
            self._send_json(pm.mgr.status())
        elif self.path == "/profiles":
            self._send_json(pm.mgr.list_profiles())
        elif self.path == "/system":
            self._send_json(self._system_info())
        elif self.path == "/history":
            self._send_json(pm.mgr.state.get_history(limit=30))
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        pm = self.proxy
        if self.path in ("/v1/chat/completions", "/v1/completions"):
            self._handle_chat(pm)
        elif self.path == "/switch":
            self._handle_switch(pm)
        elif self.path == "/reset":
            self._handle_reset(pm)
        elif self.path == "/reconcile":
            self._handle_reconcile(pm)
        else:
            self._send_json({"error": "not found"}, 404)

    def _serve_dashboard(self):
        """Serve dashboard HTML. Priority: static/index.html > dashboard.py > minimal fallback."""
        # Try static/index.html first
        static_dir = Path(__file__).parent / "static"
        dashboard = static_dir / "index.html"
        body = None

        if dashboard.exists():
            body = dashboard.read_bytes()

        # Try dashboard.py
        if body is None:
            try:
                from edge_llm.dashboard import DASHBOARD_HTML
                body = DASHBOARD_HTML.encode("utf-8")
            except ImportError:
                pass

        # Minimal fallback
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
        self.wfile.write(body)

    def _system_info(self):
        """Get CPU, RAM, and system info."""
        info = {"cpu_percent": 0, "cpu_cores": os.cpu_count() or 1, "ram_total_gb": 0, "ram_used_gb": 0, "uptime_seconds": 0}
        try:
            with open("/proc/meminfo") as f:
                mem = f.read()
            total_kb = int([l for l in mem.splitlines() if l.startswith("MemTotal")][0].split()[1])
            avail_kb = int([l for l in mem.splitlines() if l.startswith("MemAvailable")][0].split()[1])
            info["ram_total_gb"] = round(total_kb / 1024**2, 1)
            info["ram_used_gb"] = round((total_kb - avail_kb) / 1024**2, 1)
        except Exception: pass
        try:
            with open("/proc/loadavg") as f:
                loadavg = f.read().split()[0]
            load = float(loadavg)
            cores = info["cpu_cores"]
            info["cpu_percent"] = round(load / cores * 100, 1)
        except Exception: pass
        try:
            with open("/proc/uptime") as f:
                info["uptime_seconds"] = int(float(f.read().split()[0]))
        except Exception: pass
        return info

    def _handle_chat(self, pm):
        """Forward chat/completions request to upstream vLLM with streaming support."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                self._send_json({"error": "Empty request body"}, 400)
                return
            body = self.rfile.read(content_length)
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as e:
            self._send_json({"error": f"Invalid JSON: {e}"}, 400)
            return

        model = data.get("model", "vllm_qwen27b")
        stream = data.get("stream", False)
        profile = pm.model_to_profile(model)

        if profile and AUTO_SWITCH:
            pm.ensure_profile(profile)

        target_port = pm.get_target_port(model)
        if not target_port:
            self._send_json({"error": f"Unknown model: {model}"}, 404)
            return

        # Forward upstream request
        upstream_url = f"http://127.0.0.1:{target_port}{self.path}"
        upstream_req = urllib.request.Request(
            upstream_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            upstream_resp = urllib.request.urlopen(upstream_req, timeout=300)
        except Exception as e:
            log.error("Forward to :%d failed: %s", target_port, e)
            self._send_json({"error": str(e)}, 502)
            return

        try:
            resp_status = upstream_resp.status
            resp_content_type = upstream_resp.headers.get("Content-Type", "text/plain")

            if stream:
                # Streaming: forward chunk-by-chunk
                self.send_response(resp_status)
                self.send_header("Content-Type", resp_content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                chunk_size = 8192
                while True:
                    chunk = upstream_resp.read(chunk_size)
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):x}\r\n".encode())
                    self.wfile.write(chunk)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                # Non-streaming: read once, send once (BUG FIX: was reading twice)
                resp_body = upstream_resp.read()
                self.send_response(resp_status)
                self.send_header("Content-Type", resp_content_type)
                self.send_header("Content-Length", str(len(resp_body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp_body)

            upstream_resp.close()
        except Exception as e:
            log.error("Error forwarding response: %s", e)
            try:
                self._send_json({"error": str(e)}, 502)
            except Exception:
                pass

    def _handle_switch(self, pm):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b'{}'
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        target = data.get("profile")
        if not target:
            self._send_json({"error": "Missing profile"}, 400)
            return
        result = pm.mgr.switch(target)
        self._send_json(result)

    def _handle_reset(self, pm):
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length) if content_length > 0 else b'{}'
            data = json.loads(body) if body else {}
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON"}, 400)
            return
        target = data.get("profile", "idle")
        result = pm.mgr.force_reset(target)
        self._send_json(result)

    def _handle_reconcile(self, pm):
        result = pm.mgr.reconcile()
        self._send_json(result)

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)


# ─── Threaded HTTP Server ───────────────────────────────────────

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """HTTPServer with thread-per-connection. Solves single-thread blocking."""
    allow_reuse_address = True
    daemon_threads = True  # Don't wait for worker threads on shutdown


# ─── Main ─────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    mgr = ProxyManager()

    server = ThreadedHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
    server.proxy_mgr = mgr  # type: ignore

    def health_loop():
        while True:
            time.sleep(HEALTH_CHECK_INTERVAL)
            try:
                mgr.health_check()
            except Exception as e:
                log.error("Health check error: %s", e)

    threading.Thread(target=health_loop, daemon=True).start()

    shutdown_requested = False

    def shutdown(signum, frame):
        nonlocal shutdown_requested
        if shutdown_requested:
            log.warning("Forced shutdown — second signal")
            server.shutdown()
            sys.exit(1)
        shutdown_requested = True
        log.info("Shutting down (signal %s)", signum)
        server.shutdown()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("EdgeLLM Proxy: %s:%d (auto_switch=%s, threaded)", PROXY_HOST, PROXY_PORT, AUTO_SWITCH)
    log.info("Dashboard: http://%s:%d/", PROXY_HOST, PROXY_PORT)
    log.info("Current profile: %s", mgr.current)
    server.serve_forever()


if __name__ == "__main__":
    main()
