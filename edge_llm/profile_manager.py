"""
EdgeLLM — Local LLM Profile Switcher
Core profile management and GPU lifecycle controller.

v3.0 — Architecture refactoring:
  - Process group management (start_new_session + killpg) replaces pkill
  - GPU lock simplified: pure flock, no PID in file
  - State machine: switching / healthy / idle / error
  - Tri-state health: ✅ / ⏳ / ❌
  - Config centralization: paths from profiles.yaml
  - Reconcile uses _check_vllm_status (not wait_http)
  - Unified log directory: ~/.edge_llm/logs/
  - File descriptor hygiene: no leaked Popen stdout
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
import fcntl
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

import yaml

log = logging.getLogger("edge_llm")


# ─── Configuration ───────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent
DEFAULT_PROFILES = BASE_DIR / "profiles.yaml"
DEFAULT_STATE_DB = Path.home() / ".edge_llm" / "state.db"
DEFAULT_LOG_DIR = Path.home() / ".edge_llm" / "logs"
GPU_LOCK = Path("/tmp/edge_llm_gpu.lock")
MODEL_BASE = Path.home() / "models"
CONDA_ENVS = Path.home() / "miniconda3" / "envs"

# Process management constants
STOP_SIGTERM_TIMEOUT = 10  # seconds to wait after SIGTERM before SIGKILL
VLLM_STARTUP_CHECK_INTERVAL = 0.5  # seconds between startup checks
VLLM_STARTUP_CHECK_ROUNDS = 20  # 10 seconds total for immediate-failure detection
HEALTH_CHECK_TIMEOUT = 300  # 5 minutes for vLLM to become healthy
GPU_FREE_TIMEOUT = 30  # seconds to wait for GPU memory release
GPU_FREE_THRESHOLD_MB = 2048  # MB below which GPU is considered "free"


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


# ─── State Machine ────────────────────────────────────────────────

class ProfileState:
    """Valid profile states for state.db."""
    SWITCHING = "switching"
    HEALTHY = "healthy"
    IDLE = "idle"
    ERROR = "error"

    @classmethod
    def is_active(cls, state: str) -> bool:
        return state in (cls.SWITCHING, cls.HEALTHY, cls.ERROR)


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
                "from_profile TEXT, to_profile TEXT, duration REAL, status TEXT"
                ")"
            )
            # Migration: add status column if missing (from v2 schema)
            try:
                c.execute("SELECT status FROM history LIMIT 1")
            except sqlite3.OperationalError:
                log.info("Migrating history table: adding status column")
                c.execute("ALTER TABLE history ADD COLUMN status TEXT DEFAULT 'ok'")
            c.execute("INSERT OR IGNORE INTO state VALUES ('current_profile', 'idle')")
            c.execute("INSERT OR IGNORE INTO state VALUES ('profile_state', 'idle')")
            c.execute("INSERT OR IGNORE INTO state VALUES ('vllm_pid', '')")
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

    def set_multi(self, kv: dict[str, str]):
        """Atomically set multiple state keys."""
        with self._lock:
            c = self._conn()
            try:
                for k, v in kv.items():
                    c.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (k, v))
                c.commit()
            finally:
                c.close()

    def add_history(self, from_profile: str, to_profile: str, duration: float, status: str = "ok"):
        with self._lock:
            c = self._conn()
            try:
                c.execute(
                    "INSERT INTO history (from_profile, to_profile, duration, status) VALUES (?, ?, ?, ?)",
                    (from_profile, to_profile, duration, status),
                )
                c.execute(
                    "DELETE FROM history WHERE id NOT IN "
                    "(SELECT id FROM history ORDER BY id DESC LIMIT ?)",
                    (100,),
                )
                c.commit()
            finally:
                c.close()

    def get_history(self, limit: int = 20) -> list[dict]:
        c = self._conn()
        try:
            rows = c.execute(
                "SELECT timestamp, from_profile, to_profile, duration, status "
                "FROM history ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {"timestamp": r[0], "from": r[1], "to": r[2], "duration": r[3], "status": r[4]}
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
        # Handle multi-GPU output (sum)
        total = sum(int(x.strip()) for x in r.stdout.strip().splitlines() if x.strip())
        return total
    except Exception:
        return 0


def gpu_total_mb() -> int:
    """Get total GPU memory."""
    try:
        r = _run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"])
        total = sum(int(x.strip()) for x in r.stdout.strip().splitlines() if x.strip())
        return total
    except Exception:
        return 32607  # fallback for RTX 5090D


def wait_gpu_free(timeout: int = GPU_FREE_TIMEOUT, threshold_mb: int = GPU_FREE_THRESHOLD_MB) -> bool:
    """Wait for GPU memory to drop below threshold. Returns False on timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if gpu_used_mb() < threshold_mb:
            return True
        time.sleep(2)
    return False


def check_http_status(url: str, timeout: int = 3) -> str:
    """Check HTTP endpoint: '✅' (200), '⏳' (503 loading), '❌' (unreachable/error)."""
    import urllib.request, urllib.error
    try:
        resp = urllib.request.urlopen(url, timeout=timeout)
        try:
            if resp.status == 200:
                return "✅"
        finally:
            resp.close()
    except urllib.error.HTTPError as e:
        if e.code == 503:
            return "⏳"
        # Other HTTP errors (401, 404, 500) — log but return ⏳ to not overreact
        log.debug("HTTP %d from %s", e.code, url)
        return "⏳"
    except Exception:
        return "❌"


def wait_http(url: str, timeout: int = HEALTH_CHECK_TIMEOUT) -> bool:
    """Wait for HTTP endpoint to return 200. Respects 503 as transient (loading).
    Returns True if healthy within timeout, False otherwise."""
    import urllib.request, urllib.error
    deadline = time.time() + timeout
    consecutive_non_503_errors = 0
    while time.time() < deadline:
        try:
            resp = urllib.request.urlopen(url, timeout=5)
            try:
                if resp.status == 200:
                    return True
            finally:
                resp.close()
        except urllib.error.HTTPError as e:
            if e.code == 503:
                consecutive_non_503_errors = 0  # loading is expected
            else:
                consecutive_non_503_errors += 1
                if consecutive_non_503_errors >= 10:
                    log.error("HTTP %d from %s 10 times consecutively — giving up", e.code, url)
                    return False
        except Exception:
            consecutive_non_503_errors = 0  # connection refused is expected during startup
        time.sleep(3)
    return False


def kill_port(pidfile: Path, timeout: int = STOP_SIGTERM_TIMEOUT) -> None:
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
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
        time.sleep(1)
        pidfile.unlink(missing_ok=True)
    except ProcessLookupError:
        pidfile.unlink(missing_ok=True)


# ─── GPU Lock (simplified — pure flock, no PID content) ──────────

class GPULock:
    """GPU mutual exclusion via flock. No PID in file — flock auto-releases on process death."""

    def __init__(self, lock_path: Path = GPU_LOCK):
        self._lock_path = str(lock_path)
        self._fd: Optional[int] = None

    def acquire(self, timeout: float = 0) -> bool:
        """Acquire GPU lock. timeout=0 means non-blocking. Returns True if acquired."""
        if self._fd is not None:
            return True  # already held
        try:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            if timeout > 0:
                deadline = time.time() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        self._fd = fd
                        return True
                    except BlockingIOError:
                        if time.time() >= deadline:
                            os.close(fd)
                            return False
                        time.sleep(1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return True
        except BlockingIOError:
            try:
                os.close(fd)
            except OSError:
                pass
            return False
        except OSError:
            return False

    def release(self):
        """Release GPU lock."""
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
        except OSError:
            pass
        finally:
            self._fd = None

    def force_clear(self):
        """Emergency: close any stale lock. Only use when no other process could hold it."""
        self.release()
        try:
            os.unlink(self._lock_path)
        except FileNotFoundError:
            pass

    @property
    def is_held(self) -> bool:
        return self._fd is not None


# ─── Process Manager ─────────────────────────────────────────────

class ProcessManager:
    """Manages vLLM and ComfyUI processes using process groups (not pkill)."""

    def __init__(self, state: StateDB, log_dir: Path = DEFAULT_LOG_DIR):
        self._state = state
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)

    @property
    def vllm_pid(self) -> Optional[int]:
        """Get tracked vLLM main PID from state.db."""
        pid_str = self._state.get("vllm_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    def _set_vllm_pid(self, pid: Optional[int]):
        self._state.set("vllm_pid", str(pid) if pid else "")

    def start_vllm(self, cfg: VLLMConfig) -> dict:
        """Start vLLM via conda env's vllm binary. Uses start_new_session for process group isolation."""
        log_file = self._log_dir / f"vllm_{cfg.conda_env}.log"
        pid_file = self._log_dir / f"vllm_{cfg.conda_env}.pid"

        # Use vllm binary from the conda env directly
        vllm_bin = CONDA_ENVS / cfg.conda_env / "bin" / "vllm"
        if not vllm_bin.exists():
            log.error("vllm binary not found: %s", vllm_bin)
            return {"status": "error", "message": f"vllm not found in conda env {cfg.conda_env}"}

        cmd = cfg.build_cmd()
        cmd[0] = str(vllm_bin)

        log.info("Starting vLLM cmd: %s", " ".join(cmd[:8]) + "...")
        env = dict(os.environ)
        env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
        # Add conda env's bin/ to PATH so ninja, triton, etc. are available
        conda_bin = str(CONDA_ENVS / cfg.conda_env / "bin")
        env["PATH"] = conda_bin + ":" + env.get("PATH", "")

        # Truncate log file for this session
        log_file.write_text("")

        # Use start_new_session=True for process group isolation (setsid equivalent)
        # This allows us to kill the entire process group (including EngineCore children)
        # and prevents SIGHUP from the parent terminal killing vLLM
        log_fh = open(str(log_file), "a")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,  # setsid — creates new process group
            )
        except Exception as e:
            log_fh.close()
            log.error("Failed to start vLLM: %s", e)
            return {"status": "error", "message": f"Popen failed: {e}"}

        pgid = proc.pid  # With start_new_session, PID == PGID
        self._set_vllm_pid(pgid)
        pid_file.write_text(str(pgid))
        log.info("vLLM started: PID=%d (PGID=%d)", proc.pid, pgid)

        # Close our file handle — vLLM has its own fd now
        log_fh.close()

        # Check if process died immediately (argument parse error, missing binary, etc.)
        for _ in range(VLLM_STARTUP_CHECK_ROUNDS):
            ret = proc.poll()
            if ret is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = "read log failed"
                log.error("vLLM exited immediately (ret=%d): %s", ret, err[-500:])
                self._set_vllm_pid(None)
                pid_file.unlink(missing_ok=True)
                return {"status": "error", "message": f"vLLM exited with code {ret}", "log": str(log_file)}

            time.sleep(VLLM_STARTUP_CHECK_INTERVAL)

        # Wait for vLLM to become healthy (or timeout)
        healthy = wait_http(f"http://localhost:{cfg.port}/health", timeout=HEALTH_CHECK_TIMEOUT)
        if healthy:
            return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        else:
            # vLLM didn't become healthy — check if it's still running or dead
            if proc.poll() is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = ""
                return {"status": "error", "message": f"vLLM crashed during loading", "log": str(log_file)}
            else:
                # Still running but not healthy — kill it
                self.stop_vllm()
                return {"status": "timeout", "message": "vLLM didn't become healthy within 5 minutes"}

    def stop_vllm(self) -> dict:
        """Stop vLLM using process group kill. SIGTERM → wait → SIGKILL entire group."""
        pgid = self.vllm_pid
        if pgid is None:
            # No tracked PID — try legacy pkill as fallback
            log.warning("No vLLM PID tracked, falling back to pkill")
            return self._pkill_vllm_fallback()

        log.info("Stopping vLLM PGID=%d", pgid)

        # Step 1: SIGTERM the process group
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            log.info("Process group %d already dead", pgid)
            self._set_vllm_pid(None)
            self._cleanup_pid_files()
            return {"status": "ok", "message": "already dead"}

        # Step 2: Wait for graceful shutdown
        for i in range(STOP_SIGTERM_TIMEOUT):
            try:
                os.killpg(pgid, 0)  # Check if group still exists
            except (ProcessLookupError, PermissionError):
                log.info("vLLM process group %d terminated gracefully in %ds", pgid, i + 1)
                self._set_vllm_pid(None)
                self._cleanup_pid_files()
                # Reap zombie
                self._reap_zombies()
                return {"status": "ok", "message": f"terminated in {i + 1}s"}
            time.sleep(1)

        # Step 3: SIGKILL the entire process group (kills EngineCore too)
        log.warning("SIGTERM timeout for PGID %d, sending SIGKILL to group", pgid)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

        time.sleep(2)
        self._set_vllm_pid(None)
        self._cleanup_pid_files()
        self._reap_zombies()

        return {"status": "ok", "message": "killed (SIGKILL)"}

    def _pkill_vllm_fallback(self) -> dict:
        """Fallback: stop vLLM using pkill when no PID is tracked."""
        killed_any = False
        for port in [8000, 8001, 8002]:
            result = subprocess.run(
                ["pkill", "-f", f"vllm.*{port}"],
                timeout=5, check=False, capture_output=True
            )
            if result.returncode == 0:
                killed_any = True

        time.sleep(3)

        # SIGKILL remaining
        for port in [8000, 8001, 8002]:
            subprocess.run(["pkill", "-9", "-f", f"vllm.*{port}"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "vllm serve"], timeout=5, check=False)
        # Also try to kill any remaining EngineCore processes
        subprocess.run(["pkill", "-9", "-f", "VLLM::EngineCore"], timeout=5, check=False)

        time.sleep(2)
        self._cleanup_pid_files()
        self._reap_zombies()

        return {"status": "ok", "message": "pkill fallback"}

    def stop_comfyui(self) -> dict:
        """Stop ComfyUI via its stop script."""
        comfyui_script = Path.home() / "edge_llm" / "scripts" / "switch_comfyui.sh"
        try:
            result = subprocess.run(
                ["bash", "-c", f"{comfyui_script} stop"],
                timeout=15, check=False, capture_output=True
            )
            return {"status": "ok", "returncode": result.returncode}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def stop_all(self) -> dict:
        """Stop all services: ComfyUI first, then vLLM."""
        results = {}
        results["comfyui"] = self.stop_comfyui()
        results["vllm"] = self.stop_vllm()
        return results

    def force_kill_all(self) -> dict:
        """Nuclear option: SIGKILL everything related to vLLM + ComfyUI."""
        # Kill vLLM process group first
        pgid = self.vllm_pid
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        # Fallback: pkill all vLLM patterns
        subprocess.run(["pkill", "-9", "-f", "vllm serve"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "VLLM::EngineCore"], timeout=5, check=False)
        for port in [8000, 8001, 8002]:
            subprocess.run(["pkill", "-9", "-f", f"vllm.*{port}"], timeout=5, check=False)

        # Kill ComfyUI
        self.stop_comfyui()
        subprocess.run(["pkill", "-9", "-f", "python main.py"], timeout=5, check=False,
                       cwd=str(Path.home() / "ComfyUI"))

        time.sleep(2)
        self._set_vllm_pid(None)
        self._cleanup_pid_files()
        self._reap_zombies()

        return {"status": "ok"}

    def is_vllm_alive(self, port: int) -> bool:
        """Check if vLLM process is still alive (by PID or HTTP)."""
        pgid = self.vllm_pid
        if pgid:
            try:
                os.killpg(pgid, 0)
                return True
            except (ProcessLookupError, PermissionError):
                return False
        # No tracked PID — try HTTP
        return check_http_status(f"http://localhost:{port}/health") != "❌"

    def _cleanup_pid_files(self):
        """Remove PID files."""
        for pf in self._log_dir.glob("vllm_*.pid"):
            pf.unlink(missing_ok=True)
        # Also clean legacy PID files
        legacy_dir = Path.home() / "models" / "vllm_logs"
        if legacy_dir.exists():
            for pf in legacy_dir.glob("*.pid"):
                pf.unlink(missing_ok=True)

    def _reap_zombies(self):
        """Reap zombie child processes."""
        try:
            while True:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
        except ChildProcessError:
            pass


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
        self._lock = GPULock()
        self._proc = ProcessManager(self.state)
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

    @property
    def profile_state(self) -> str:
        return self.state.get("profile_state") or ProfileState.IDLE

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

    # ── Health Check (tri-state) ─────────────────────────────────

    def check_vllm_health(self, port: int) -> str:
        """Check vLLM health: ✅ healthy, ⏳ loading, ❌ dead."""
        return check_http_status(f"http://localhost:{port}/health")

    def check_comfyui_health(self, url: str) -> str:
        """Check ComfyUI health."""
        return check_http_status(url)

    # ── Reconciliation ──────────────────────────────────────────

    def reconcile(self) -> dict:
        """Compare DB state against actual running processes. Fix inconsistencies.
        Uses tri-state health check to avoid killing processes during loading."""
        db_profile = self.current_profile
        db_state = self.profile_state

        # Scan all known vLLM ports for actual state
        actual_states = {}
        for name, p in self._profiles.items():
            if p.vllm:
                actual_states[name] = self.check_vllm_health(p.vllm.port)

        # Find the actually running profile (✅ or ⏳)
        actual_profile = None
        loading_profile = None
        for name, state in actual_states.items():
            if state == "✅":
                actual_profile = name
                break
            if state == "⏳":
                loading_profile = name

        # If nothing is ✅ but something is ⏳, don't kill it
        if actual_profile is None and loading_profile is not None:
            actual_profile = loading_profile

        # Check ComfyUI
        comfyui_ok = False
        for p in self._profiles.values():
            if p.comfyui:
                h = self.check_comfyui_health(p.comfyui.health_url)
                if h == "✅":
                    comfyui_ok = True
                    break

        actions: list[str] = []

        # Fix state inconsistencies
        if actual_profile and actual_profile != db_profile:
            if actual_states.get(actual_profile) == "⏳":
                actions.append(f"DB says '{db_profile}', but {actual_profile} is loading (⏳) — updating DB")
                self.state.set_multi({
                    "current_profile": actual_profile,
                    "profile_state": ProfileState.SWITCHING,
                })
            else:
                actions.append(f"DB says '{db_profile}', but {actual_profile} is running (✅) — updating DB")
                self.state.set_multi({
                    "current_profile": actual_profile,
                    "profile_state": ProfileState.HEALTHY,
                })
        elif actual_profile is None and db_profile != "idle":
            # Nothing running — check if there's a tracked PID
            if self._proc.vllm_pid:
                try:
                    os.killpg(self._proc.vllm_pid, 0)
                    # Process group exists but HTTP doesn't respond — probably crashed
                    actions.append(f"Tracked PGID {self._proc.vllm_pid} alive but HTTP dead — killing orphan")
                    self._proc.stop_vllm()
                except (ProcessLookupError, PermissionError):
                    actions.append(f"DB says '{db_profile}' but nothing running and tracked PID is dead → forcing idle")
            else:
                actions.append(f"DB says '{db_profile}' but nothing running → forcing idle")
            self.state.set_multi({
                "current_profile": "idle",
                "profile_state": ProfileState.IDLE,
                "vllm_pid": "",
            })
        elif actual_profile is None and db_profile == "idle" and db_state != ProfileState.IDLE:
            # Profile is idle but state is wrong (e.g. error after failed switch)
            actions.append(f"Profile is idle but state is '{db_state}' → fixing to idle")
            self.state.set("profile_state", ProfileState.IDLE)

        # Fix profile_state inconsistencies
        if actual_profile and actual_states.get(actual_profile) == "✅" and db_state != ProfileState.HEALTHY:
            actions.append(f"State was '{db_state}' but {actual_profile} is healthy (✅) — updating to healthy")
            self.state.set("profile_state", ProfileState.HEALTHY)
        elif db_state == ProfileState.SWITCHING and actual_profile:
            actual_health = actual_states.get(actual_profile, "❌")
            if actual_health == "✅":
                actions.append(f"State was 'switching' but {actual_profile} is healthy — updating to healthy")
                self.state.set("profile_state", ProfileState.HEALTHY)

        return {
            "db_profile": db_profile,
            "db_state": db_state,
            "actual_profile": actual_profile or "none",
            "actual_states": actual_states,
            "actions": actions,
            "comfyui_alive": comfyui_ok,
        }

    # ── Switch ────────────────────────────────────────────────────

    def switch(self, target: str) -> dict:
        """Switch to target profile. Validates all services healthy before success."""
        if target == self.current_profile and self.profile_state == ProfileState.HEALTHY:
            return {"status": "already_active", "profile": target}

        profile = self._profiles.get(target)
        if not profile:
            return {"status": "error", "message": f"Unknown profile: {target}"}

        if not self._lock.acquire():
            return {"status": "error", "message": "GPU switch in progress (lock held)"}

        t0 = time.time()
        from_profile = self.current_profile
        log.info("Switching %s → %s", from_profile, target)

        # Set switching state
        self.state.set_multi({
            "current_profile": target,
            "profile_state": ProfileState.SWITCHING,
        })

        try:
            # Step 1: Stop current services
            stop_result = self._proc.stop_all()
            log.info("Stop result: %s", stop_result)

            # Step 2: Wait for GPU to be free
            if not wait_gpu_free():
                log.warning("GPU not free after %ds, force killing...", GPU_FREE_TIMEOUT)
                self._proc.force_kill_all()
                if not wait_gpu_free(timeout=15):
                    self.state.set("profile_state", ProfileState.ERROR)
                    return {
                        "status": "error",
                        "message": "GPU not freed even after force kill — check nvidia-smi",
                    }

            # Step 3: Start target services
            results = {}
            if profile.comfyui:
                log.info("Starting ComfyUI")
                results["comfyui"] = self._start_comfyui(profile.comfyui)
            if profile.vllm:
                log.info("Starting vLLM: %s on :%d", profile.vllm.served_name, profile.vllm.port)
                results["vllm"] = self._proc.start_vllm(profile.vllm)

            # Step 4: Validate
            if profile.vllm and results["vllm"]["status"] != "healthy":
                log.error("vLLM failed to become healthy for %s", target)
                self.state.set("profile_state", ProfileState.ERROR)
                # Clean up partial start
                if profile.comfyui:
                    self._proc.stop_comfyui()
                self.state.add_history(from_profile, target, time.time() - t0, "error")
                return {
                    "status": "error",
                    "message": f"vLLM failed: {results['vllm'].get('message', 'timeout')}",
                    "profile": target,
                    "results": results,
                }

            # Step 5: Success — update state
            elapsed = round(time.time() - t0, 1)
            self.state.set_multi({
                "current_profile": target,
                "profile_state": ProfileState.HEALTHY,
            })
            self.state.add_history(from_profile, target, elapsed, "ok")
            log.info("Switch complete in %.1fs", elapsed)
            return {
                "status": "switched",
                "profile": target,
                "elapsed_sec": elapsed,
                "results": results,
            }
        except Exception as e:
            log.exception("Switch failed")
            self.state.set("profile_state", ProfileState.ERROR)
            self.state.add_history(from_profile, target, time.time() - t0, "error")
            return {"status": "error", "message": str(e)}
        finally:
            self._lock.release()

    def _start_comfyui(self, cfg: ComfyUIConfig) -> dict:
        """Start ComfyUI via its startup script."""
        script = Path(cfg.startup_script).expanduser().resolve()
        home = Path.home().resolve()
        if not (script.is_absolute() and (str(script).startswith(str(home)) or str(script).startswith("/home"))):
            log.error("Unsafe ComfyUI script path: %s", script)
            return {"status": "error", "message": "Script path must be absolute under home"}
        result = subprocess.run([str(script), "start"], timeout=120, check=False)
        return {"status": "started" if result.returncode == 0 else "error"}

    def status(self) -> dict:
        profile = self._profiles.get(self.current_profile)
        vllm_status = "❌"
        comfyui_status = "❌"
        if profile:
            if profile.vllm:
                vllm_status = self.check_vllm_health(profile.vllm.port)
            if profile.comfyui:
                comfyui_status = self.check_comfyui_health(profile.comfyui.health_url)
        return {
            "profile": self.current_profile,
            "state": self.profile_state,
            "description": profile.description if profile else "unknown",
            "vllm": vllm_status,
            "comfyui": comfyui_status,
            "gpu_used_mb": gpu_used_mb(),
            "gpu_total_mb": gpu_total_mb(),
            "vllm_pid": self._proc.vllm_pid,
        }

    # ── Force Reset ───────────────────────────────────────────────

    def force_reset(self, target: str = "idle") -> dict:
        """Nuclear reset: kill everything, verify GPU, clean state."""
        log.info("Force reset → %s", target)

        # 1. Stop everything through proper channels first
        self._proc.stop_all()

        # 2. SIGKILL all remaining
        self._proc.force_kill_all()

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

        # 5. Clean lock
        self._lock.force_clear()

        # 6. Write state
        self.state.set_multi({
            "current_profile": target,
            "profile_state": ProfileState.IDLE if target == "idle" else ProfileState.ERROR,
            "vllm_pid": "",
        })
        return {
            "status": "reset",
            "profile": target,
            "gpu_free": gpu_used_mb() < GPU_FREE_THRESHOLD_MB,
        }
