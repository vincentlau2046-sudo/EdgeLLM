"""
edge_llm/process_manager.py — Unified process lifecycle for vLLM + ComfyUI.

Extracted from profile_manager.py (v3.0 → v3.1 refactoring).

Key improvements over v3.0:
  - ComfyUI now uses native Python process management (no bash script dependency)
  - Process group tracking for both vLLM and ComfyUI
  - ComfyUI PID tracked in state.db
  - Unified stop pattern: SIGTERM → wait → SIGKILL process group
"""

import os
import time
import signal
import shlex
import json
import logging
import urllib.request
import urllib.error
import subprocess
from pathlib import Path
from typing import Optional

from .config import (
    CONDA_ENVS,
    DEFAULT_LOG_DIR,
    COMFYUI_DIR,
    VLLM_STARTUP_CHECK_INTERVAL,
    VLLM_STARTUP_CHECK_ROUNDS,
    HEALTH_CHECK_TIMEOUT,
    STOP_SIGTERM_TIMEOUT,
    VLLMConfig,
    ComfyUIConfig,
    SleepModeConfig,
)
from .state import StateDB
from .health import wait_http, check_http_status, wait_gpu_free, gpu_used_mb

log = logging.getLogger("edge_llm")


class ProcessManager:
    """Manages vLLM and ComfyUI processes using process groups (not pkill)."""

    def __init__(self, state: StateDB, log_dir: Path = DEFAULT_LOG_DIR):
        self._state = state
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)

    # ─── PID Tracking ────────────────────────────────────────────

    @property
    def vllm_pid(self) -> Optional[int]:
        pid_str = self._state.get("vllm_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    @property
    def comfyui_pid(self) -> Optional[int]:
        pid_str = self._state.get("comfyui_pid")
        if pid_str:
            try:
                return int(pid_str)
            except ValueError:
                pass
        return None

    def _set_vllm_pid(self, pid: Optional[int]):
        self._state.set("vllm_pid", str(pid) if pid else "")

    def _set_comfyui_pid(self, pid: Optional[int]):
        self._state.set("comfyui_pid", str(pid) if pid else "")

    # ─── vLLM ────────────────────────────────────────────────────

    def start_vllm(self, cfg: VLLMConfig) -> dict:
        """Start vLLM via conda env's vllm binary. Uses start_new_session for process group isolation."""
        log_file = self._log_dir / f"vllm_{cfg.conda_env}.log"
        pid_file = self._log_dir / f"vllm_{cfg.conda_env}.pid"

        vllm_bin = CONDA_ENVS / cfg.conda_env / "bin" / "vllm"
        if not vllm_bin.exists():
            log.error("vllm binary not found: %s", vllm_bin)
            return {"status": "error", "message": f"vllm not found in conda env {cfg.conda_env}"}

        cmd = cfg.build_cmd()
        cmd[0] = str(vllm_bin)

        log.info("Starting vLLM cmd: %s", " ".join(cmd[:8]) + "...")
        env = dict(os.environ)
        # KV offloading conflicts with expandable_segments (NIXL/Mooncake IB memory)
        has_kv_offload = "--kv-offloading-size" in " ".join(cmd)
        if not has_kv_offload:
            env["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
        else:
            log.info("KV offloading detected — skipping expandable_segments for %s", cfg.served_name)

        # Enable sleep mode if configured
        if cfg.sleep_mode and cfg.sleep_mode.enabled:
            env["VLLM_SERVER_DEV_MODE"] = "1"
            cmd.append("--enable-sleep-mode")
            log.info("Sleep mode enabled (L2) for %s", cfg.served_name)

        conda_bin = str(CONDA_ENVS / cfg.conda_env / "bin")
        env["PATH"] = conda_bin + ":" + env.get("PATH", "")

        log_file.write_text("")

        log_fh = open(str(log_file), "a")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        except Exception as e:
            log.error("Failed to start vLLM: %s", e)
            return {"status": "error", "message": f"Popen failed: {e}"}
        finally:
            log_fh.close()

        pgid = proc.pid  # With start_new_session, PID == PGID
        self._set_vllm_pid(pgid)
        pid_file.write_text(str(pgid))
        log.info("vLLM started: PID=%d (PGID=%d)", proc.pid, pgid)

        # Check if process died immediately
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

        # Wait for vLLM to become healthy
        healthy = wait_http(f"http://localhost:{cfg.port}/health", timeout=HEALTH_CHECK_TIMEOUT)
        if healthy:
            return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        else:
            if proc.poll() is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = ""
                return {"status": "error", "message": "vLLM crashed during loading", "log": str(log_file)}
            else:
                self.stop_vllm()
                return {"status": "timeout", "message": "vLLM didn't become healthy within 5 minutes"}

    def stop_vllm(self) -> dict:
        """Stop vLLM using process group kill. SIGTERM → wait → SIGKILL entire group."""
        pgid = self.vllm_pid
        if pgid is None:
            log.warning("No vLLM PID tracked, falling back to pkill")
            return self._pkill_vllm_fallback()

        log.info("Stopping vLLM PGID=%d", pgid)

        # SIGTERM the process group
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            log.info("Process group %d already dead", pgid)
            self._set_vllm_pid(None)
            self._cleanup_pid_files("vllm")
            return {"status": "ok", "message": "already dead"}

        # Wait for graceful shutdown
        for i in range(STOP_SIGTERM_TIMEOUT):
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                log.info("vLLM process group %d terminated gracefully in %ds", pgid, i + 1)
                self._set_vllm_pid(None)
                self._cleanup_pid_files("vllm")
                self._reap_zombies()
                self._wait_gpu_idle()
                return {"status": "ok", "message": f"terminated in {i + 1}s"}
            time.sleep(1)

        # SIGKILL
        log.warning("SIGTERM timeout for PGID %d, sending SIGKILL to group", pgid)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

        time.sleep(2)
        self._set_vllm_pid(None)
        self._cleanup_pid_files("vllm")
        self._reap_zombies()
        self._wait_gpu_idle()
        return {"status": "ok", "message": "killed (SIGKILL)"}

    def _pkill_vllm_fallback(self) -> dict:
        """Fallback: stop vLLM using pkill when no PID is tracked."""
        # Try to discover vLLM ports from known models.d configs
        vllm_ports = []
        try:
            from .config import load_models
            for m in load_models().values():
                if m.vllm:
                    vllm_ports.append(m.vllm.port)
        except Exception:
            pass
        if not vllm_ports:
            vllm_ports = [8000, 8001, 8002]  # fallback defaults

        for port in vllm_ports:
            subprocess.run(["pkill", "-f", f"vllm.*{port}"], timeout=5, check=False, capture_output=True)

        time.sleep(3)

        for port in vllm_ports:
            subprocess.run(["pkill", "-9", "-f", f"vllm.*{port}"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "vllm serve"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "VLLM::EngineCore"], timeout=5, check=False)

        time.sleep(2)
        self._cleanup_pid_files("vllm")
        self._reap_zombies()
        self._wait_gpu_idle()
        return {"status": "ok", "message": "pkill fallback"}

    # ─── ComfyUI ─────────────────────────────────────────────────

    def start_comfyui(self, cfg: ComfyUIConfig) -> dict:
        """Start ComfyUI. Uses native Python process management when config supports it,
        falls back to bash script for legacy configs."""
        if cfg.use_native:
            return self._start_comfyui_native(cfg)
        elif cfg.startup_script:
            return self._start_comfyui_script(cfg)
        else:
            return {"status": "error", "message": "ComfyUI config has neither conda_env nor startup_script"}

    def _start_comfyui_native(self, cfg: ComfyUIConfig) -> dict:
        """Start ComfyUI natively via conda env's Python with process group isolation."""
        python_bin = CONDA_ENVS / cfg.conda_env / "bin" / "python"
        if not python_bin.exists():
            log.error("Python binary not found: %s", python_bin)
            return {"status": "error", "message": f"python not found in conda env {cfg.conda_env}"}

        main_py = cfg.resolved_working_dir / "main.py"
        if not main_py.exists():
            log.error("ComfyUI main.py not found: %s", main_py)
            return {"status": "error", "message": f"main.py not found at {main_py}"}

        cmd = [str(python_bin), str(main_py), "--listen", "0.0.0.0",
               "--port", str(cfg.port)]
        if cfg.extra_flags:
            cmd.extend(shlex.split(cfg.extra_flags))

        log.info("Starting ComfyUI cmd: %s", " ".join(cmd))
        env = dict(os.environ)
        env["HF_ENDPOINT"] = "https://hf-mirror.com"
        # Add CUDA runtime to LD_LIBRARY_PATH
        cuda_rt = str(CONDA_ENVS / cfg.conda_env / "lib" / "python3.12" / "site-packages" / "nvidia" / "cuda_runtime" / "lib")
        env["LD_LIBRARY_PATH"] = cuda_rt + (":" + env.get("LD_LIBRARY_PATH", "") if env.get("LD_LIBRARY_PATH") else "")
        # Add conda env's bin/ to PATH
        conda_bin = str(CONDA_ENVS / cfg.conda_env / "bin")
        env["PATH"] = conda_bin + ":" + env.get("PATH", "")

        log_file = self._log_dir / "comfyui.log"
        log_file.write_text("")

        log_fh = open(str(log_file), "a")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
                cwd=str(cfg.resolved_working_dir),
            )
        except Exception as e:
            log.error("Failed to start ComfyUI: %s", e)
            return {"status": "error", "message": f"Popen failed: {e}"}
        finally:
            log_fh.close()

        pgid = proc.pid  # start_new_session → PID == PGID
        self._set_comfyui_pid(pgid)
        pid_file = self._log_dir / "comfyui.pid"
        pid_file.write_text(str(pgid))
        log.info("ComfyUI started: PID=%d (PGID=%d)", proc.pid, pgid)

        # Quick check for immediate failure
        for _ in range(6):  # 3 seconds
            ret = proc.poll()
            if ret is not None:
                try:
                    err = log_file.read_text()[-2000:]
                except Exception:
                    err = "read log failed"
                log.error("ComfyUI exited immediately (ret=%d): %s", ret, err[-500:])
                self._set_comfyui_pid(None)
                pid_file.unlink(missing_ok=True)
                return {"status": "error", "message": f"ComfyUI exited with code {ret}", "log": str(log_file)}
            time.sleep(0.5)

        # Wait for health check
        health_url = cfg.health_url or f"http://localhost:{cfg.port}/system_stats"
        healthy = wait_http(health_url, timeout=120)
        if healthy:
            return {"status": "healthy", "port": cfg.port, "pid": proc.pid}
        else:
            if proc.poll() is not None:
                return {"status": "error", "message": "ComfyUI crashed during loading"}
            else:
                self.stop_comfyui()
                return {"status": "timeout", "message": "ComfyUI didn't become healthy within 2 minutes"}

    def _start_comfyui_script(self, cfg: ComfyUIConfig) -> dict:
        """Legacy: start ComfyUI via bash startup script."""
        script = Path(cfg.startup_script).expanduser().resolve()
        home = Path.home().resolve()
        if not (script.is_absolute() and (str(script).startswith(str(home)) or str(script).startswith("/home"))):
            log.error("Unsafe ComfyUI script path: %s", script)
            return {"status": "error", "message": "Script path must be absolute under home"}
        try:
            result = subprocess.run([str(script), "start"], timeout=120, check=False)
            return {"status": "started" if result.returncode == 0 else "error"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def stop_comfyui(self) -> dict:
        """Stop ComfyUI using process group kill (native) or stop script (legacy)."""
        pgid = self.comfyui_pid

        if pgid is not None:
            return self._stop_comfyui_native(pgid)

        # Legacy fallback: try stop script from current profile
        # (manager.py should call with config if available)
        log.warning("No ComfyUI PID tracked, falling back to pkill")
        return self._pkill_comfyui_fallback()

    def stop_comfyui_with_config(self, cfg: ComfyUIConfig) -> dict:
        """Stop ComfyUI with config knowledge for legacy script fallback."""
        pgid = self.comfyui_pid
        if pgid is not None:
            return self._stop_comfyui_native(pgid)
        elif cfg.stop_script:
            return self._stop_comfyui_script(cfg)
        else:
            return self._pkill_comfyui_fallback()

    def _stop_comfyui_native(self, pgid: int) -> dict:
        """Stop ComfyUI by process group. SIGTERM → wait → SIGKILL."""
        log.info("Stopping ComfyUI PGID=%d", pgid)

        try:
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            log.info("ComfyUI process group %d already dead", pgid)
            self._set_comfyui_pid(None)
            self._cleanup_pid_files("comfyui")
            return {"status": "ok", "message": "already dead"}

        for i in range(STOP_SIGTERM_TIMEOUT):
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                log.info("ComfyUI process group %d terminated gracefully in %ds", pgid, i + 1)
                self._set_comfyui_pid(None)
                self._cleanup_pid_files("comfyui")
                self._wait_gpu_idle()
                return {"status": "ok", "message": f"terminated in {i + 1}s"}
            time.sleep(1)

        log.warning("SIGTERM timeout for ComfyUI PGID %d, sending SIGKILL", pgid)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

        time.sleep(2)
        self._set_comfyui_pid(None)
        self._cleanup_pid_files("comfyui")
        self._wait_gpu_idle()
        return {"status": "ok", "message": "killed (SIGKILL)"}

    def _stop_comfyui_script(self, cfg: ComfyUIConfig) -> dict:
        """Legacy: stop ComfyUI via bash stop script."""
        script = Path(cfg.stop_script).expanduser().resolve()
        try:
            result = subprocess.run(
                ["bash", "-c", f"{script} stop"],
                timeout=15, check=False, capture_output=True
            )
            self._set_comfyui_pid(None)
            return {"status": "ok", "returncode": result.returncode}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _pkill_comfyui_fallback(self) -> dict:
        """Fallback: stop ComfyUI via pkill."""
        subprocess.run(["pkill", "-f", "python main.py"], timeout=5, check=False, capture_output=True)
        time.sleep(2)
        # SIGKILL remaining
        subprocess.run(["pkill", "-9", "-f", "python main.py"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "ComfyUI"], timeout=5, check=False)
        time.sleep(1)
        self._set_comfyui_pid(None)
        self._cleanup_pid_files("comfyui")
        self._wait_gpu_idle()
        return {"status": "ok", "message": "pkill fallback"}

    # ─── Combined Operations ─────────────────────────────────────

    def stop_all(self, comfyui_cfg: Optional[ComfyUIConfig] = None) -> dict:
        """Stop all services: ComfyUI first, then vLLM."""
        results = {}
        if comfyui_cfg:
            results["comfyui"] = self.stop_comfyui_with_config(comfyui_cfg)
        else:
            results["comfyui"] = self.stop_comfyui()
        results["vllm"] = self.stop_vllm()
        return results

    def force_kill_all(self) -> dict:
        """Nuclear option: SIGKILL everything related to vLLM + ComfyUI."""
        # vLLM
        pgid = self.vllm_pid
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        subprocess.run(["pkill", "-9", "-f", "vllm serve"], timeout=5, check=False)
        subprocess.run(["pkill", "-9", "-f", "VLLM::EngineCore"], timeout=5, check=False)
        for port in [8000, 8001, 8002]:
            subprocess.run(["pkill", "-9", "-f", f"vllm.*{port}"], timeout=5, check=False)

        # ComfyUI
        cpgid = self.comfyui_pid
        if cpgid:
            try:
                os.killpg(cpgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        subprocess.run(["pkill", "-9", "-f", "python main.py"], timeout=5, check=False)
        # Try to kill ComfyUI specifically by working dir
        comfyui_dir = str(COMFYUI_DIR)
        subprocess.run(["pkill", "-9", "-f", f"python.*{comfyui_dir}"], timeout=5, check=False)

        time.sleep(2)
        self._set_vllm_pid(None)
        self._set_comfyui_pid(None)
        self._cleanup_pid_files("vllm")
        self._cleanup_pid_files("comfyui")
        self._reap_zombies()
        self._wait_gpu_idle()
        return {"status": "ok"}

    # ─── Health Checks/Sleep ─────────────────────────────────────

    # ─── Sleep/Wake ────────────────────────────────────────────

    def sleep_vllm(self, port: int) -> dict:
        """Put vLLM server to L2 sleep (discard weights, free VRAM)."""
        url = f"http://localhost:{port}/sleep?level=2"
        log.info("Sleeping vLLM at port %d (L2)", port)
        t0 = time.time()
        try:
            req = urllib.request.Request(url, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                elapsed = round(time.time() - t0, 1)
                log.info("vLLM sleep OK (port=%d, %.1fs)", port, elapsed)
                return {"status": "ok", "port": port, "elapsed_sec": elapsed}
        except urllib.error.HTTPError as e:
            elapsed = round(time.time() - t0, 1)
            log.error("vLLM sleep HTTP %d (port=%d): %s", e.code, port, e.reason)
            return {"status": "error", "message": f"HTTP {e.code}: {e.reason}", "elapsed_sec": elapsed}
        except Exception as e:
            elapsed = round(time.time() - t0, 1)
            log.error("vLLM sleep failed (port=%d): %s", port, e)
            return {"status": "error", "message": str(e), "elapsed_sec": elapsed}

    def wake_vllm(self, port: int) -> dict:
        """L2 wake: kill sleeping process, then cold restart via switch.

        vLLM 0.23.0 L2 sleep leaves the engine in an unrecoverable state
        (wake_up CUDA invalid argument). We kill the sleeping process
        and let the caller handle restart.
        """
        log.info("Killing sleeping vLLM at port %d for restart", port)
        self.stop_vllm()
        return {"status": "killed_for_restart", "port": port, "elapsed_sec": 0}

    def is_sleeping(self, port: int) -> bool:
        """Check if vLLM server is currently in sleep mode."""
        try:
            req = urllib.request.Request(f"http://localhost:{port}/is_sleeping")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("is_sleeping", False)
        except Exception:
            return False

    def is_vllm_alive(self, port: int) -> bool:
        """Check if vLLM process is still alive (by PID or HTTP)."""
        pgid = self.vllm_pid
        if pgid:
            try:
                os.killpg(pgid, 0)
                return True
            except (ProcessLookupError, PermissionError):
                return False
        return check_http_status(f"http://localhost:{port}/health") != "❌"

    def is_comfyui_alive(self, port: int = 8188) -> bool:
        """Check if ComfyUI process is still alive (by PID or HTTP)."""
        pgid = self.comfyui_pid
        if pgid:
            try:
                os.killpg(pgid, 0)
                return True
            except (ProcessLookupError, PermissionError):
                return False
        health_url = f"http://localhost:{port}/system_stats"
        return check_http_status(health_url) != "❌"

    # ─── GPU Cleanup ─────────────────────────────────────────────

    def _wait_gpu_idle(self, timeout: int = 60) -> dict:
        """Wait for GPU to return to idle state after process exit.

        When a CUDA process exits, its GPU memory is freed by the kernel.
        This method verifies nvidia-smi shows idle before returning.
        """
        for _ in range(timeout):
            used = gpu_used_mb()
            if used is not None and used < 2048:  # <2GB = idle (系统基线~1GB + 余量)
                log.info("GPU returned to idle (%d MB)", used)
                return {"status": "ok", "used_mb": used}
            time.sleep(1)
        return {"status": "timeout", "message": "GPU did not return to idle"}

    # ─── Internal Helpers ────────────────────────────────────────

    def _cleanup_pid_files(self, prefix: str):
        """Remove PID files for a given prefix (vllm or comfyui)."""
        for pf in self._log_dir.glob(f"{prefix}*.pid"):
            pf.unlink(missing_ok=True)
        if prefix == "vllm":
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
