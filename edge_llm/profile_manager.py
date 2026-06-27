"""
EdgeLLM — Local LLM Profile Switcher
Core profile management and GPU lifecycle controller.
v2.0 — Robust reset, state reconciliation, proper GPU lock.
"""

import os
import time
import json
import shlex
import sqlite3
import signal
import subprocess
import logging
import threading
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

import yaml

log = logging.getLogger("edge_llm")


# ─── Configuration ───────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent
DEFAULT_PROFILES = BASE_DIR / "profiles.yaml"
DEFAULT_STATE_DB = Path.home() / ".edge_llm" / "state.db"
GPU_LOCK = Path("/tmp/edge_llm_gpu.lock")
MODEL_BASE = Path.home() / "models"


@dataclass
class VLLMConfig:
    model_dir: str
    served_name: str
    port: int
    conda_env: str
    max_model_len: int
    gpu_memory_utilization: float
    max_num_seqs: int
    kv_cache_dtype: str
    speculative_config: Optional[str] = None
    extra_flags: str = ""

    def build_cmd(self) -> list[str]:
        """Build vLLM command. JSON args stay as single elements."""
        model_path = MODEL_BASE / self.model_dir
        flags = [
            "vllm", "serve", str(model_path),
            "--served-model-name", self.served_name,
            "--max-model-len", str(self.max_model_len),
            "--gpu-memory-utilization", str(self.gpu_memory_utilization),
            "--max-num-seqs", str(self.max_num_seqs),
            "--kv-cache-dtype", self.kv_cache_dtype,
            "--port", str(self.port),
            "--host", "0.0.0.0",
        ]
        if self.speculative_config:
            flags.extend(["--speculative-config", self.speculative_config])
        if self.extra_flags:
            flags.extend(shlex.split(self.extra_flags))
        return flags


@dataclass
class ComfyUIConfig:
    startup_script: str
    health_url: str
    stop_script: str


@dataclass
class Profile:
    name: str
    description: str
    gpu_owner: str
    vllm: Optional[VLLMConfig] = None
    comfyui: Optional[ComfyUIConfig] = None
    switch_cost_sec: int = 0


# ─── State Manager ─────────────────────────────────────────────────

class StateDB:
    """Thread-safe SQLite — fresh connection per call (WAL mode)."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init()

    def _conn(self):
        c = sqlite3.connect(str(self._db_path), timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init(self):
        with self._lock:
            c = self._conn()
            c.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
            c.execute(
                "CREATE TABLE IF NOT EXISTS history ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "timestamp TEXT DEFAULT CURRENT_TIMESTAMP, "
                "from_profile TEXT, to_profile TEXT, duration REAL"
                ")"
            )
            c.execute("INSERT OR IGNORE INTO state VALUES ('current_profile', 'idle')")
            c.commit()
            c.close()

    def get(self, key: str) -> Optional[str]:
        c = self._conn()
        try:
            row = c.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
        finally:
            c.close()

    def set(self, key: str, value: str):
        with self._lock:
            c = self._conn()
            try:
                c.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))
                c.commit()
            finally:
                c.close()

    def add_history(self, from_profile: str, to_profile: str, duration: float):
        with self._lock:
            c = self._conn()
            try:
                c.execute(
                    "INSERT INTO history (from_profile, to_profile, duration) VALUES (?, ?, ?)",
                    (from_profile, to_profile, duration),
                )
                c.execute(
                    "DELETE FROM history WHERE id NOT IN "
                    "(SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                    (50,),
                )
                c.commit()
            finally:
                c.close()

    def get_history(self, limit: int = 20) -> list[dict]:
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT timestamp, from_profile, to_profile, duration "
                "FROM history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {"timestamp": r[0], "from": r[1], "to": r[2], "duration": r[3]}
                for r in rows
            ]
        finally:
            c.close()


# ─── GPU / Process Helpers ────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 30, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


def gpu_used_mb() -> int:
    """Get total GPU memory used across all GPUs."""
    try:
        r = _run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
        return int(r.stdout.strip())
    except Exception:
        return 0


def gpu_total_mb() -> int:
    """Get total GPU memory."""
    try:
        r = _run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
        return int(r.stdout.strip())
    except Exception:
        return 32768  # fallback


def wait_gpu_free(timeout: int = 30, threshold_mb: int = 2048) -> bool:
    """Wait for GPU memory to drop below threshold. Returns False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if gpu_used_mb() < threshold_mb:
            return True
        time.sleep(2)
    return False


def wait_http(url: str, timeout: int = 300) -> bool:
    """Check if an HTTP endpoint returns 200. Retries with backoff.
    Accepts: 200 (healthy), 503 (loading — not a failure, just not ready).
    Rejects: connection refused, other errors."""
    import urllib.request, urllib.error
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=3)
            try:
                if resp.status == 200:
                    return True
            finally:
                resp.close()
        except urllib.error.HTTPError as e:
            # 503 = server exists but not ready (model loading)
            if e.code == 503:
                pass  # keep waiting
            else:
                pass  # other HTTP errors — also keep waiting
        except Exception:
            pass  # connection refused etc — keep waiting
        time.sleep(3)
    return False


def kill_port(pidfile: Path, timeout: int = 10) -> None:
    """Kill a process by PID file, with SIGKILL fallback."""
    if not pidfile.exists():
        return
    try:
        pid = int(pidfile.read_text().strip())
    except (ValueError, PermissionError):
        pidfile.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(timeout):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pidfile.unlink(missing_ok=True)
                return
            time.sleep(1)
        # SIGTERM didn't work — SIGKILL
        log.warning("SIGTERM failed for PID %d, sending SIGKILL", pid)
        os.kill(pid, signal.SIGKILL)
        time.sleep(1)
        pidfile.unlink(missing_ok=True)
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)


# ─── Profile Manager ─────────────────────────────────────────────

class ProfileManager:
    """Orchestrates GPU resource allocation via predefined profiles."""

    def __init__(
        self,
        profiles_path: str | Path = str(DEFAULT_PROFILES),
        state_db_path: str | Path = str(DEFAULT_STATE_DB),
    ):
        self.profiles_path = Path(profiles_path)
        self.state = StateDB(Path(state_db_path))
        self._profiles = self._load()

    def _load(self) -> dict[str, Profile]:
        raw = yaml.safe_load(self.profiles_path.read_text())["profiles"]
        result = {}
        for name, cfg in raw.items():
            vllm_cfg = None
            if cfg.get("vllm"):
                vllm_cfg = VLLMConfig(**cfg["vllm"])
            comfy_cfg = None
            if cfg.get("comfyui"):
                comfy_cfg = ComfyUIConfig(**cfg["comfyui"])
            result[name] = Profile(
                name=name,
                description=cfg.get("description", name),
                gpu_owner=cfg.get("gpu_owner", "none"),
                vllm=vllm_cfg,
                comfyui=comfy_cfg,
                switch_cost_sec=cfg.get("switch_cost_sec", 0),
            )
        return result

    @property
    def current_profile(self) -> str:
        return self.state.get("current_profile") or "idle"

    def list_profiles(self) -> list[dict]:
        return [
            {
                "name": p.name,
                "description": p.description,
                "current": p.name == self.current_profile,
                "gpu_owner": p.gpu_owner,
                "has_vllm": p.vllm is not None,
                "has_comfyui": p.comfyui is not None,
                "switch_cost_sec": p.switch_cost_sec,
            }
            for p in self._profiles.values()
        ]

    # ── Reconciliation ──────────────────────────────────────────────

    def reconcile(self) -> dict:
        """Compare DB state against actual running processes. Fix inconsistencies."""
        db_profile = self.current_profile
        actual_vllm_ports = set()

        for port in self._all_vllm_ports():
            if wait_http(f"http://localhost:{port}/health", timeout=2):
                actual_vllm_ports.add(port)

        actual_profile = None
        for name, p in self._profiles.items():
            if p.vllm and p.vllm.port in actual_vllm_ports:
                actual_profile = name
                break

        comfyui_ok = False
        for p in self._profiles.values():
            if p.comfyui and wait_http(p.comfyui.health_url, timeout=2):
                comfyui_ok = True
                break

        actions: list[str] = []
        if actual_profile != db_profile:
            if actual_profile:
                actions.append(f"DB says '{db_profile}', but {actual_profile} is running")
                self.state.set("current_profile", actual_profile)
            elif actual_vllm_ports:
                actions.append(f"DB says '{db_profile}', ports {actual_vllm_ports} active — killing orphans")
                self._stop_orphan_ports(actual_vllm_ports)
                if db_profile == "idle":
                    self.state.set("current_profile", "idle")
                else:
                    self.state.set("current_profile", "idle")
            else:
                # Nothing running — force to idle
                if db_profile != "idle":
                    actions.append(f"DB says '{db_profile}' but nothing running → forcing idle")
                    self.state.set("current_profile", "idle")

        return {
            "db_profile": db_profile,
            "actual_profile": actual_profile or "none",
            "actions": actions,
            "comfyui_alive": comfyui_ok,
        }

    def _all_vllm_ports(self) -> set[int]:
        return {p.vllm.port for p in self._profiles.values() if p.vllm}

    def _stop_orphan_ports(self, ports: set[int]) -> None:
        for port in ports:
            log.info("Killing orphan vLLM on :%d", port)
            subprocess.run(["pkill", "-f", "--", f"vllm.*{port}"], timeout=10, check=False)
            subprocess.run(["pkill", "-9", "-f", "--", f"vllm.*{port}"], timeout=10, check=False)
            pid_file = Path.home() / "models" / "vllm_logs" / f"*-{port}.pid"
            for pf in Path.home().glob(f"models/vllm_logs/*.pid"):
                subprocess.run(["pkill", "-9", "-f", "--", f"vllm.*{port}"], timeout=5, check=False)

    # ── GPU Lock ──────────────────────────────────────────────────

    def _acquire_gpu_lock(self):
        """Acquire GPU lock with PID-based stale detection. Returns fd or raises."""
        import fcntl

        # Write PID first so stale detection can find us
        lock_path = str(GPU_LOCK)
        pid = os.getpid()

        # Try to read existing PID for stale detection
        existing_pid = None
        try:
            existing_pid = int(GPU_LOCK.read_text().strip())
        except (ValueError, FileNotFoundError, PermissionError):
            existing_pid = None

        # Open for reading first, then acquire, then write PID
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            if existing_pid is not None:
                # Check if lock holder is alive
                try:
                    os.kill(existing_pid, 0)
                    # Lock held by live process — refuse
                    return None
                except (ProcessLookupError, PermissionError):
                    # Stale lock — steal it
                    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        os.close(lock_fd)
                        return None
            else:
                return None  # Lock held, unknown owner — refuse

        # Lock acquired — write our PID atomically
        os.write(lock_fd, str(pid).encode())
        os.fsync(lock_fd)
        return lock_fd

    def _release_gpu_lock(self, lock_fd):
        """Release GPU lock."""
        if lock_fd is None:
            return
        try:
            os.ftruncate(lock_fd, 0)
            os.close(lock_fd)
        except OSError:
            pass

    # ── Switch ────────────────────────────────────────────────────

    def switch(self, target: str) -> dict:
        """Switch to target profile. Validates all services healthy before success."""
        if target == self.current_profile:
            return {"status": "already_active", "profile": target}

        profile = self._profiles.get(target)
        if not profile:
            return {"status": "error", "message": f"Unknown profile: {target}"}

        lock_fd = self._acquire_gpu_lock()
        if lock_fd is None:
            return {"status": "error", "message": "GPU switch in progress (lock held)"}

        t0 = time.time()
        from_profile = self.current_profile
        log.info("Switching %s → %s", from_profile, target)

        try:
            self._stop_current()
            if not wait_gpu_free(timeout=30):
                # Force kill remaining
                log.warning("GPU not free after 30s, force killing...")
                self._force_kill_all()
                if not wait_gpu_free(timeout=30):
                    return {"status": "error", "message": "GPU not freed even after force kill — check nvidia-smi"}

            results = {}
            if profile.comfyui:
                log.info("Starting ComfyUI")
                results["comfyui"] = self._start_comfyui(profile.comfyui)
            if profile.vllm:
                log.info("Starting vLLM: %s on :%d", profile.vllm.served_name, profile.vllm.port)
                results["vllm"] = self._start_vllm(profile.vllm)

            # Validate: if vLLM was supposed to start, check it's healthy
            if profile.vllm and results["vllm"]["status"] != "healthy":
                log.error("vLLM failed to become healthy for %s", target)
                # Clean up partial start
                if profile.comfyui:
                    self._stop_comfyui()
                return {
                    "status": "error",
                    "message": "vLLM failed to start (timeout after 5min)",
                    "profile": target,
                    "results": results,
                }

            elapsed = round(time.time() - t0, 1)
            self.state.set("current_profile", target)
            self.state.add_history(from_profile, target, elapsed)
            log.info("Switch complete in %.1fs", elapsed)
            return {
                "status": "switched",
                "profile": target,
                "elapsed_sec": elapsed,
                "results": results,
            }
        except Exception as e:
            log.exception("Switch failed")
            return {"status": "error", "message": str(e)}
        finally:
            self._release_gpu_lock(lock_fd)

    def _check_vllm_status(self, port: int) -> str:
        """Check vLLM: '✅' healthy, '⏳' loading (503), '❌' dead."""
        import urllib.request, urllib.error
        try:
            resp = urllib.request.urlopen(f"http://localhost:{port}/health", timeout=3)
            if resp.status == 200:
                return "✅"
        except urllib.error.HTTPError as e:
            if e.code == 503:
                return "⏳"
        except Exception:
            pass
        return "❌"

    def status(self) -> dict:
        profile = self._profiles.get(self.current_profile)
        vllm_status = "❌"
        comfyui_ok = False
        if profile:
            if profile.vllm:
                vllm_status = self._check_vllm_status(profile.vllm.port)
            if profile.comfyui:
                comfyui_ok = wait_http(profile.comfyui.health_url, timeout=3)
        return {
            "profile": self.current_profile,
            "description": profile.description if profile else "unknown",
            "vllm": vllm_status,
            "comfyui": "✅" if comfyui_ok else "❌",
            "gpu_used_mb": gpu_used_mb(),
            "gpu_total_mb": gpu_total_mb(),
        }

    # ── Force Reset ───────────────────────────────────────────────

    def force_reset(self, target: str = "idle") -> dict:
        """Nuclear reset: kill everything, verify GPU, clean state."""
        log.info("Force reset → %s", target)

        # 1. Stop everything through proper channels first
        self._stop_current()

        # 2. SIGKILL all vLLM processes
        self._force_kill_all()

        # 3. Wait for GPU
        if not wait_gpu_free(timeout=20):
            # 4. Try nvidia-smi GPU reset if possible
            try:
                subprocess.run(["nvidia-smi", "--gpu-reset"], timeout=10, check=False)
                time.sleep(5)
            except Exception:
                pass
            if not wait_gpu_free(timeout=15):
                # Last resort accepted — clear state but warn
                log.warning("GPU still busy after force reset — orphan CUDA context likely")

        # 5. Clean lock file
        try:
            GPU_LOCK.unlink(missing_ok=True)
        except Exception:
            pass

        # 6. Write state
        self.state.set("current_profile", target)
        return {
            "status": "reset",
            "profile": target,
            "gpu_free": gpu_used_mb() < 2048,
        }

    def _force_kill_all(self) -> None:
        """Kill every known vLLM + ComfyUI process with SIGKILL."""
        # Kill by port patterns
        for port in self._all_vllm_ports():
            subprocess.run(["pkill", "-9", "-f", f"vllm.*{port}"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "vllm serve"], timeout=5, check=False)
        # Kill ComfyUI
        comfyui_script = Path.home() / "edge_llm" / "scripts" / "switch_comfyui.sh"
        subprocess.run(["bash", "-c", f"{comfyui_script} stop"], timeout=10, check=False)
        subprocess.run(["pkill", "-9", "-f", "python main.py"], timeout=5, check=False)
        time.sleep(2)

    # ── Internal ─────────────────────────────────────────────────

    def _stop_current(self):
        """Kill all known services. SIGTERM first, SIGKILL fallback."""
        log.info("Stopping all services...")

        # Stop ComfyUI
        comfyui_script = Path.home() / "edge_llm" / "scripts" / "switch_comfyui.sh"
        subprocess.run(["bash", "-c", f"{comfyui_script} stop"], timeout=15, check=False)

        # Stop vLLM by port — SIGTERM with fallback
        all_ports = self._all_vllm_ports()
        for port in all_ports:
            # SIGTERM first
            subprocess.run(["pkill", "-f", "--", f"vllm.*{port}"], timeout=10, check=False)
            subprocess.run(["pkill", "-f", "--", f"--port.*{port}"], timeout=5, check=False)

        # Wait a moment for graceful shutdown
        time.sleep(3)

        # Check if any vLLM survived — SIGKILL
        for port in all_ports:
            if wait_http(f"http://localhost:{port}/health", timeout=2):
                log.warning("vLLM on :%d survived SIGTERM, sending SIGKILL", port)
                subprocess.run(["pkill", "-9", "-f", f"vllm.*{port}"], timeout=5, check=False)
                subprocess.run(["pkill", "-9", "-f", f"--port.*{port}"], timeout=5, check=False)

        # Kill all remaining vLLM regardless of port
        subprocess.run(["pkill", "-9", "-f", "vllm serve"], timeout=5, check=False)
        time.sleep(2)

        # Reap zombies
        try:
            while True:
                os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            pass

    def _stop_services_by_ports(self, ports: set[int]):
        """Kill only specific vLLM port processes."""
        for port in ports:
            log.info("Killing vLLM on :%d", port)
            subprocess.run(["pkill", "-f", "--", f"vllm.*{port}"], timeout=10, check=False)
            subprocess.run(["pkill", "-9", "-f", "--", f"vllm.*{port}"], timeout=5, check=False)

    def _stop_comfyui(self) -> None:
        """Stop ComfyUI."""
        comfyui_script = Path.home() / "edge_llm" / "scripts" / "switch_comfyui.sh"
        subprocess.run(["bash", "-c", f"{comfyui_script} stop"], timeout=15, check=False)

    def _start_comfyui(self, cfg: ComfyUIConfig) -> dict:
        """Start ComfyUI via its startup script."""
        script = Path(cfg.startup_script).expanduser().resolve()
        home = Path.home().resolve()
        if not (script.is_absolute() and (str(script).startswith(str(home)) or str(script).startswith("/home"))):
            log.error("Unsafe ComfyUI script path: %s", script)
            return {"status": "error", "message": "Script path must be absolute under home"}
        result = subprocess.run([str(script), "start"], timeout=120, check=False)
        return {"status": "started" if result.returncode == 0 else "error"}

    def _start_vllm(self, cfg: VLLMConfig) -> dict:
        """Start vLLM via conda env's vllm binary."""
        log_dir = Path.home() / "models" / "vllm_logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / f"{cfg.conda_env}.log"

        # Use vllm binary from the conda env directly
        conda_envs = Path.home() / "miniconda3" / "envs"
        vllm_bin = conda_envs / cfg.conda_env / "bin" / "vllm"
        if not vllm_bin.exists():
            log.error("vllm binary not found: %s", vllm_bin)
            return {"status": "error", "message": f"vllm not found in conda env {cfg.conda_env}"}

        cmd = cfg.build_cmd()
        cmd[0] = str(vllm_bin)

        log.info("Starting vLLM cmd: %s", " ".join(cmd[:8]) + "...")
        env = dict(os.environ)
        env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
        # Add conda env's bin/ to PATH so ninja, triton, etc. are available
        conda_bin = str(conda_envs / cfg.conda_env / "bin")
        env["PATH"] = conda_bin + ":" + env.get("PATH", "")

        # Truncate log file for this session
        log_file.write_text("")

        proc = subprocess.Popen(
            cmd,
            stdout=open(str(log_file), "a"),
            stderr=subprocess.STDOUT,
            env=env,
        )

        pid_file = log_dir / f"{cfg.conda_env}.pid"
        pid_file.write_text(str(proc.pid))

        # Check if process died immediately (argument parse error)
        for _ in range(10):
            ret = proc.poll()
            if ret is not None:
                try:
                    err = log_file.read_text()[-500:]
                except Exception:
                    err = "read log failed"
                log.error("vLLM exited immediately (ret=%d): %s", ret, err)
                return {"status": "error", "message": f"vLLM exited with code {ret}", "log": str(log_file)}
            time.sleep(0.5)

        healthy = wait_http(f"http://localhost:{cfg.port}/health", timeout=300)
        return {
            "status": "healthy" if healthy else "timeout",
            "port": cfg.port,
            "pid": proc.pid,
        }
