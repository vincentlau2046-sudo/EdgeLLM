"""
edge_llm/manager.py — Model orchestration layer (v4.0).

v4.0: Profile concept eliminated. Models are self-describing plugins.
Tri-state GPU mode: idle / exclusive / shared.
Switch rules enforced by validate_transition().
"""

import os
import sys
import json
import time
import logging
from pathlib import Path
from typing import Optional

from .config import (
    MODELS_DIR,
    DEFAULT_STATE_DB,
    GPU_FREE_TIMEOUT,
    GPU_FREE_THRESHOLD_MB,
    ModelConfig,
    load_models,
    # Legacy
    DEFAULT_PROFILES,
    Profile,
    load_profiles,
)
from .state import StateDB, ProfileState, GPUMode, validate_transition
from .gpu_lock import GPULock
from .process_manager import ProcessManager
from .health import (
    gpu_used_mb,
    gpu_total_mb,
    check_http_status,
    wait_gpu_free,
)

log = logging.getLogger("edge_llm")


class ModelManager:
    """Orchestrates GPU resource allocation via model plugins.

    GPU Mode State Machine:
      idle → exclusive: deploy exclusive model, GPU fully locked
      idle → shared:    deploy shared model/service
      exclusive → idle:  stop exclusive model
      shared → idle:     stop all shared services
      shared → shared:   add/remove shared service (hot-plug V1: full restart)

      ❌ exclusive → shared:  must idle first
      ❌ shared → exclusive:  must idle first
    """

    def __init__(
        self,
        models_dir: str | Path = str(MODELS_DIR),
        state_db_path: str | Path = str(DEFAULT_STATE_DB),
    ):
        self.models_dir = Path(models_dir)
        self.state = StateDB(Path(state_db_path))
        self._lock = GPULock()
        self._proc = ProcessManager(self.state)
        self._models = load_models(self.models_dir)

    @property
    def gpu_mode(self) -> str:
        return self.state.gpu_mode

    @property
    def active_services(self) -> list[str]:
        return self.state.get_active_services()

    @property
    def current_service(self) -> Optional[str]:
        """For backward compat — returns first active service or 'idle'."""
        services = self.active_services
        return services[0] if services else "idle"

    # ── Model Lookup ─────────────────────────────────────────────

    def get_model(self, name: str) -> Optional[ModelConfig]:
        """Get model config by name."""
        return self._models.get(name)

    def list_models(self) -> list[dict]:
        """List all available models from models.d/. Skips alias_map entries."""
        return [
            {
                "name": m.name,
                "description": m.description,
                "mode": m.mode,
                "type": m.type,
                "active": m.name in self.active_services,
            }
            for m in self._models.values()
            if m.type != "alias_map"
        ]

    def find_model_by_served_name(self, served_name: str) -> Optional[ModelConfig]:
        """Find model by its served_name across all backend types (for proxy routing)."""
        for m in self._models.values():
            if m.served_name == served_name:
                return m
        # Fallback: match by model name
        for m in self._models.values():
            if m.name == served_name:
                return m
        return None

    # ── Health Check (tri-state) ─────────────────────────────────

    def check_vllm_health(self, port: int) -> str:
        return check_http_status(f"http://localhost:{port}/health")

    def check_comfyui_health(self, url: str) -> str:
        return check_http_status(url)

    def check_http_health(self, port: int, path: str = "/health") -> str:
        """Generic HTTP health check for any backend."""
        return check_http_status(f"http://localhost:{port}{path}")

    def check_ollama_health(self, port: int = 11434) -> str:
        """Check if Ollama daemon is running."""
        return check_http_status(f"http://localhost:{port}/api/tags")

    # ── Reconciliation ──────────────────────────────────────────

    def reconcile(self) -> dict:
        """Compare DB state against actual running processes. Fix inconsistencies."""
        db_gpu_mode = self.gpu_mode
        db_services = self.active_services

        # Scan all known model ports
        actual_services = []
        for name, m in self._models.items():
            if m.is_vllm:
                status = self.check_vllm_health(m.vllm.port)
                if status in ("✅", "⏳"):
                    actual_services.append(name)
            elif m.is_comfyui:
                health_url = m.comfyui.health_url or f"http://localhost:{m.comfyui.port}/system_stats"
                status = self.check_comfyui_health(health_url)
                if status in ("✅", "⏳"):
                    actual_services.append(name)
            elif m.is_ollama_daemon:
                status = self.check_ollama_health(m.ollama_daemon.port)
                if status in ("✅", "⏳"):
                    actual_services.append(name)
            elif m.is_ollama_cpp:
                status = self.check_http_health(m.ollama_cpp.port, "/health")
                if status in ("✅", "⏳"):
                    actual_services.append(name)
            # Note: ollama model type doesn't have its own process — it's served by ollama-daemon

        actions: list[str] = []

        # Determine actual gpu_mode from running services
        actual_gpu_mode = GPUMode.IDLE
        if actual_services:
            # Check if any exclusive model is running
            for svc_name in actual_services:
                m = self._models.get(svc_name)
                if m and m.is_exclusive:
                    actual_gpu_mode = GPUMode.EXCLUSIVE
                    break
            if actual_gpu_mode == GPUMode.IDLE:
                actual_gpu_mode = GPUMode.SHARED

        # Fix state inconsistencies
        if actual_gpu_mode != db_gpu_mode:
            actions.append(f"DB gpu_mode='{db_gpu_mode}', actual='{actual_gpu_mode}' — updating")
            self.state.gpu_mode = actual_gpu_mode

        if set(actual_services) != set(db_services):
            actions.append(f"DB services={db_services}, actual={actual_services} — updating")
            self.state.set_active_services(actual_services)

        # Fix profile_state
        db_profile_state = self.state.get("profile_state") or "idle"
        if actual_services and db_profile_state != ProfileState.HEALTHY:
            actions.append(f"profile_state was '{db_profile_state}', services running → healthy")
            self.state.set("profile_state", ProfileState.HEALTHY)
        elif not actual_services and db_gpu_mode == GPUMode.IDLE and db_profile_state != ProfileState.IDLE:
            actions.append(f"profile_state was '{db_profile_state}', no services → idle")
            self.state.set("profile_state", ProfileState.IDLE)

        # P0-4: Fix orphan PID detection — check if PID process actually exists and owns a running port
        if self._proc.vllm_pid:
            try:
                os.killpg(self._proc.vllm_pid, 0)
            except (ProcessLookupError, PermissionError):
                # PID doesn't exist — check if any vLLM service is actually running
                # P0-4 fix: check if port has a live process via fuser
                import subprocess
                has_live_vllm = False
                for svc_name in actual_services:
                    m = self._models.get(svc_name)
                    if m and m.is_vllm:
                        result = subprocess.run(
                            ["fuser", "-v", str(m.vllm.port) + "/tcp"],
                            capture_output=True, text=True, timeout=5
                        )
                        if result.returncode == 0:
                            has_live_vllm = True
                            break
                if not has_live_vllm:
                    actions.append(f"Orphan vllm_pid={self._proc.vllm_pid} dead — clearing")
                    self.state.set("vllm_pid", "")

        # P0-5: Check if PID exists but no services running — stale
        if self._proc.vllm_pid and not actual_services:
            actions.append(f"Stale vllm_pid={self._proc.vllm_pid} with no active services — clearing")
            self.state.set("vllm_pid", "")

        # P0-5: If a vLLM is actually running but PID is not tracked, recover via fuser
        if not self._proc.vllm_pid:
            import subprocess
            for svc_name in actual_services:
                m = self._models.get(svc_name)
                if m and m.is_vllm:
                    try:
                        result = subprocess.run(
                            ["fuser", "-v", str(m.vllm.port) + "/tcp"],
                            capture_output=True, text=True, timeout=5
                        )
                        if result.returncode == 0:
                            import re
                            pid_match = re.search(r'\s+(\d+)\s', result.stdout)
                            if pid_match:
                                recovered_pid = int(pid_match.group(1))
                                self.state.set("vllm_pid", str(recovered_pid))
                                actions.append(f"Recovered vllm_pid={recovered_pid} for {svc_name} via fuser")
                                break
                    except Exception:
                        pass

        return {
            "db_gpu_mode": db_gpu_mode,
            "actual_gpu_mode": actual_gpu_mode,
            "db_services": db_services,
            "actual_services": actual_services,
            "actions": actions,
        }

    # ── Switch ────────────────────────────────────────────────────

    def switch(self, target: str) -> dict:
        """Switch to target model/service.

        Enforces tri-state GPU mode transitions:
          - idle → exclusive/shared: allowed
          - exclusive → idle: allowed
          - shared → idle: allowed
          - shared → shared: allowed (add/remove service, V1: full restart)
          - exclusive → shared: ❌ must idle first
          - shared → exclusive: ❌ must idle first
        """
        # Handle idle
        if target == "idle":
            return self._switch_to_idle()

        # Look up model
        model = self._models.get(target)
        if not model:
            return {"status": "error", "message": f"Unknown model: {target}. Available: {list(self._models.keys())}"}

        # Determine target GPU mode
        target_mode = model.mode  # 'exclusive' or 'shared'
        current_mode = self.gpu_mode

        # Already running? (P0-1: check config drift before skipping)
        if target in self.active_services:
            if model.is_vllm and model.vllm:
                changed = self._check_model_config_changed(model)
                if changed:
                    log.info("Config changed for %s — restarting", target)
                    self._switch_to_idle()
                    current_mode = self.gpu_mode  # Now idle
                else:
                    return {"status": "already_active", "model": target}
            else:
                return {"status": "already_active", "model": target}

        # Validate transition
        if not validate_transition(current_mode, target_mode):
            running = self.active_services
            if current_mode == GPUMode.EXCLUSIVE:
                return {
                    "status": "error",
                    "message": f"GPU is in exclusive mode ({running[0] if running else 'unknown'} running). "
                               f"Run 'edge-llm switch idle' first.",
                }
            elif current_mode == GPUMode.SHARED and target_mode == GPUMode.EXCLUSIVE:
                return {
                    "status": "error",
                    "message": f"GPU is in shared mode ({running} running). "
                               f"Run 'edge-llm switch idle' first to deploy exclusive model.",
                }
            else:
                return {"status": "error", "message": f"Invalid transition: {current_mode} → {target_mode}"}

        # Acquire GPU lock
        if not self._lock.acquire():
            return {"status": "error", "message": "GPU switch in progress (lock held)"}

        t0 = time.time()
        from_services = list(self.active_services)
        log.info("Switch: %s → %s (gpu_mode: %s → %s)", from_services, target, current_mode, target_mode)

        self.state.set("profile_state", ProfileState.SWITCHING)

        try:
            if current_mode == GPUMode.IDLE:
                # Fresh start — just deploy
                result = self._deploy_model(model, target_mode)
            elif current_mode == GPUMode.SHARED and target_mode == GPUMode.SHARED:
                # V1: full restart — stop all, then start all including new one
                result = self._shared_add_service(model)
            else:
                result = {"status": "error", "message": f"Unexpected state: {current_mode} → {target_mode}"}

            # Record history
            elapsed = round(time.time() - t0, 1)
            status = "ok" if result.get("status") in ("switched", "already_active") else "error"
            from_label = ",".join(from_services) if from_services else "idle"
            self.state.add_history(from_label, target, elapsed, status)

            return result

        except Exception as e:
            log.exception("Switch failed")
            self.state.set("profile_state", ProfileState.ERROR)
            self.state.add_history(",".join(from_services), target, time.time() - t0, "error")
            return {"status": "error", "message": str(e)}
        finally:
            self._lock.release()

    # P0-1: Check if model config has changed since it was started
    def _check_model_config_changed(self, model: ModelConfig) -> bool:
        """Compare vLLM process cmdline against YAML config. Returns True if drifted."""
        import subprocess
        import re
        try:
            port = model.vllm.port
            result = subprocess.run(
                ["fuser", "-v", str(port) + "/tcp"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return True  # Port not in use — config definitely changed
            
            pid_match = re.search(r'\s+(\d+)\s', result.stdout)
            if not pid_match:
                return True
            
            pid = int(pid_match.group(1))
            cmdline_path = f"/proc/{pid}/cmdline"
            if not Path(cmdline_path).exists():
                return True
            
            with open(cmdline_path, 'r') as f:
                combined = f.read().replace('\x00', ' ')
            
            # Check critical params: gpu_memory_utilization, max_model_len, max_num_seqs
            critical_keys = ['gpu-memory-utilization', 'max-model-len', 'max-num-seqs']
            for key in critical_keys:
                yaml_val = getattr(model.vllm, key.replace('-', '_'), None)
                if yaml_val is not None:
                    target = str(yaml_val)
                    # Check --key=value or --key value patterns
                    if f'--{key}={target}' not in combined and f'--{key} {target}' not in combined:
                        # Key exists in cmdline but value differs, or key missing entirely
                        if f'--{key}' in combined:
                            return True  # Value mismatch
                        # Key not found — also a mismatch for critical params
                        return True
            
            return False
        except Exception:
            log.debug("Config drift check failed for %s: %s", model.name, sys.exc_info()[1])
            return False
    
    def _switch_to_idle(self) -> dict:
        """Stop all services and transition to idle."""
        current_mode = self.gpu_mode
        if current_mode == GPUMode.IDLE and not self.active_services:
            return {"status": "already_active", "model": "idle"}

        # P0-2: reconcile first to sync state before stopping
        log.info("Reconciling state before idle switch")
        self.reconcile()

        if not self._lock.acquire():
            return {"status": "error", "message": "GPU switch in progress (lock held)"}

        t0 = time.time()
        from_services = list(self.active_services)
        log.info("Switch to idle from %s (gpu_mode=%s)", from_services, current_mode)

        try:
            # Stop all services with port-based cleanup
            ports = []
            comfyui_cfg = None
            for svc_name in from_services:
                m = self._models.get(svc_name)
                if m:
                    if m.is_vllm:
                        ports.append(("vllm", m.vllm.port))
                    elif m.is_comfyui:
                        ports.append(("comfyui", m.comfyui.port))
                        comfyui_cfg = m.comfyui
            self._proc.stop_all(
                comfyui_cfg=comfyui_cfg,
                vllm_ports=[p for t, p in ports if t == "vllm"],
                comfyui_port=ports[-1][1] if ports and ports[-1][0] == "comfyui" else None,
            )
            if not wait_gpu_free():
                self._proc.force_kill_all()
                if not wait_gpu_free(timeout=15):
                    self.state.set("profile_state", ProfileState.ERROR)
                    return {"status": "error", "message": "GPU not freed after force kill"}

            # Update state
            self.state.set_multi({
                "gpu_mode": GPUMode.IDLE,
                "active_services": json.dumps([]),
                "vllm_pid": "",
                "comfyui_pid": "",
                "profile_state": ProfileState.IDLE,
            })

            elapsed = round(time.time() - t0, 1)
            from_label = ",".join(from_services) if from_services else "idle"
            self.state.add_history(from_label, "idle", elapsed, "ok")

            return {
                "status": "switched",
                "model": "idle",
                "elapsed_sec": elapsed,
                "stopped": from_services,
            }
        except Exception as e:
            self.state.set("profile_state", ProfileState.ERROR)
            return {"status": "error", "message": str(e)}
        finally:
            self._lock.release()

    def _deploy_model(self, model: ModelConfig, target_mode: str) -> dict:
        """Deploy a model from idle state."""
        t0 = time.time()

        results = {}
        services_to_start = [model.name]

        # If shared model, optionally also start ComfyUI
        # V1: shared vLLM models don't auto-start ComfyUI
        # User does: edge-llm switch comfyui separately

        # Start the model
        if model.is_vllm:
            results[model.name] = self._proc.start_vllm(model.vllm)
        elif model.is_comfyui:
            results[model.name] = self._proc.start_comfyui(model.comfyui)
        elif model.is_ollama_daemon:
            results[model.name] = {"status": "ok", "message": "Ollama daemon external — verify with 'ollama serve'"}
        elif model.is_ollama:
            # Ollama models are served by daemon — just verify daemon is running
            daemon_healthy = self.check_ollama_health(11434)
            if daemon_healthy == "✅":
                results[model.name] = {"status": "ok", "message": f"Model {model.ollama.model_ref} available via Ollama daemon"}
            else:
                results[model.name] = {"status": "error", "message": "Ollama daemon not running. Start with: ollama serve"}
        elif model.is_ollama_cpp:
            results[model.name] = self._proc.start_ollama_cpp(model.ollama_cpp)
        else:
            return {"status": "error", "message": f"Unknown model type: {model.type}"}

        # Validate
        failed = False
        for svc, res in results.items():
            if res.get("status") not in ("healthy", "started"):
                failed = True
                break

        if failed:
            self.state.set("profile_state", ProfileState.ERROR)
            # Clean up partial start with port-based cleanup
            ports = []
            if model.is_vllm:
                ports.append(model.vllm.port)
            elif model.is_comfyui:
                ports.append(model.comfyui.port)
            self._proc.stop_all(
                comfyui_cfg=model.comfyui if model.is_comfyui else None,
                vllm_ports=ports if model.is_vllm else [],
                comfyui_port=ports[-1] if model.is_comfyui and ports else None,
            )
            self.state.set_multi({
                "gpu_mode": GPUMode.IDLE,
                "active_services": json.dumps([]),
                "vllm_pid": "",
                "comfyui_pid": "",
            })
            elapsed = round(time.time() - t0, 1)
            return {
                "status": "error",
                "message": f"Failed to start {model.name}: {results}",
                "results": results,
            }

        # Success
        elapsed = round(time.time() - t0, 1)
        self.state.set_multi({
            "gpu_mode": target_mode,
            "active_services": json.dumps(services_to_start),
            "profile_state": ProfileState.HEALTHY,
        })

        return {
            "status": "switched",
            "model": model.name,
            "gpu_mode": target_mode,
            "elapsed_sec": elapsed,
            "results": results,
        }

    def _get_current_vram_pct(self) -> float:
        """Get current GPU VRAM usage as a percentage of total."""
        try:
            from .health import gpu_used_mb, gpu_total_mb
            return gpu_used_mb() / max(gpu_total_mb(), 1) * 100
        except Exception:
            return 0.0

    def _shared_add_service(self, model: ModelConfig) -> dict:
        """Add a shared service without touching existing ones.

        Only starts the new model. Existing shared services remain running.
        Checks typical VRAM headroom before starting.
        """
        t0 = time.time()

        if model.name in self.active_services:
            return {
                "status": "already_active",
                "model": model.name,
                "gpu_mode": GPUMode.SHARED,
            }

        # ── VRAM headroom check ──
        if model.typical_vram_pct > 0:
            current_pct = self._get_current_vram_pct()
            if current_pct + model.typical_vram_pct > 95:
                return {
                    "status": "error",
                    "message": (
                        f"Insufficient GPU memory: current ~{current_pct:.0f}%, "
                        f"{model.name} needs ~{model.typical_vram_pct}%, "
                        f"total would be ~{current_pct + model.typical_vram_pct:.0f}% (limit 95%)."
                    ),
                }

        # ── Start only the new service ──
        log.info("Shared add (incremental): %s", model.name)
        results = {}
        if model.is_vllm:
            results[model.name] = self._proc.start_vllm(model.vllm)
        elif model.is_comfyui:
            results[model.name] = self._proc.start_comfyui(model.comfyui)

        # Validate
        failed = [k for k, r in results.items() if r.get("status") not in ("healthy", "started")]
        if failed:
            self.state.set("profile_state", ProfileState.ERROR)
            return {"status": "error", "message": f"Failed to start: {failed}", "results": results}

        # Update state: add to active services
        remaining = list(self.active_services)
        remaining.append(model.name)
        self.state.set_active_services(remaining)
        self.state.set("profile_state", ProfileState.HEALTHY)

        elapsed = round(time.time() - t0, 1)
        return {
            "status": "switched",
            "model": model.name,
            "gpu_mode": GPUMode.SHARED,
            "elapsed_sec": elapsed,
            "active_services": remaining,
            "results": results,
        }

    # ── Stop Single Service ──────────────────────────────────────

    def stop_service(self, name: str) -> dict:
        """Stop a single shared service. Other shared services remain.

        If this is the last shared service, auto-transition to idle.
        Verifies GPU memory is actually freed (catches orphaned processes).
        """
        if name not in self.active_services:
            return {"status": "error", "message": f"Service '{name}' is not running"}

        if self.gpu_mode == GPUMode.EXCLUSIVE:
            return {"status": "error", "message": "Cannot stop individual service in exclusive mode. Use 'switch idle'."}

        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}

        # Stop the specific service (pass port for port-based cleanup)
        if model.is_vllm:
            self._proc.stop_vllm(port=model.vllm.port)
        elif model.is_comfyui:
            self._proc.stop_comfyui_with_config(model.comfyui, port=model.comfyui.port)
        elif model.is_ollama:
            # Ollama models are served by daemon — just unregister
            log.info("Unregistering Ollama model %s", name)
        elif model.is_ollama_daemon:
            log.info("Ollama daemon stop: use 'ollama serve' externally")
        elif model.is_ollama_cpp:
            self._proc.stop_ollama_cpp(port=model.ollama_cpp.port)

        # Verify GPU actually freed — catch orphaned processes (skip CPU-only models)
        if model.needs_gpu:
            if not wait_gpu_free(timeout=20):
                log.warning("GPU not freed after stop %s — force kill remaining processes", name)
                self._proc.force_kill_all()
                wait_gpu_free(timeout=15)

        # Update active services
        remaining = [s for s in self.active_services if s != name]
        self.state.set_active_services(remaining)

        # Auto-transition to idle if no services left
        if not remaining:
            self.state.set_multi({
                "gpu_mode": GPUMode.IDLE,
                "profile_state": ProfileState.IDLE,
            })
            return {
                "status": "stopped",
                "model": name,
                "gpu_mode": GPUMode.IDLE,
                "message": f"Stopped {name}. No services remaining → idle.",
            }

        return {
            "status": "stopped",
            "model": name,
            "gpu_mode": GPUMode.SHARED,
            "remaining": remaining,
        }

    # ── Sleep / Wake (L2 only) ─────────────────────────────────

    def sleep_model(self, name: str) -> dict:
        """Put a running vLLM model to L2 sleep.

        Rules:
        - Only one model may sleep at a time.
        - Exclusive model sleeping → GPU transitions to idle (VRAM freed).
        - Shared model sleeping → GPU stays shared (other services unaffected).
        """
        if name not in self.active_services:
            return {"status": "error", "message": f"Model '{name}' is not running"}

        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}
        if not model.is_vllm:
            return {"status": "error", "message": f"Model '{name}' is not a vLLM model"}

        sleep_cfg = model.vllm.sleep_mode
        if not sleep_cfg or not sleep_cfg.enabled:
            return {"status": "error", "message": f"Sleep mode not enabled for '{name}'"}

        # Check if already sleeping
        if self.state.get_sleep_state(name):
            return {"status": "already_sleeping", "model": name}

        # Mutex: only one model sleeping at a time
        all_sleep = self.state.get_all_sleep_states()
        if all_sleep:
            existing = list(all_sleep.keys())[0]
            return {"status": "error",
                    "message": f"Model '{existing}' is already sleeping. Wake or stop it first."}

        log.info("Sleeping model '%s' (L2)", name)
        result = self._proc.sleep_vllm(model.vllm.port)

        if result["status"] == "ok":
            self.state.set_sleep_state(name, 2)

            # Exclusive sleeping → GPU → idle (VRAM freed)
            if model.is_exclusive:
                self.state.set_multi({
                    "gpu_mode": GPUMode.IDLE,
                    "profile_state": ProfileState.IDLE,
                })

            log.info("Model '%s' is now sleeping (L2), GPU=%s", name,
                     "idle" if model.is_exclusive else "shared")
        else:
            self.state.set_sleep_state(name, None)

        return {**result, "model": name}

    def wake_model(self, name: str) -> dict:
        """Wake a sleeping vLLM model.

        Rules:
        - Exclusive model: GPU must be idle → wake → GPU → exclusive.
        - Shared model: GPU must be idle or shared → wake → GPU → shared.
        """
        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}
        if not model.is_vllm:
            return {"status": "error", "message": f"Model '{name}' is not a vLLM model"}

        sleep_state = self.state.get_sleep_state(name)
        if not sleep_state:
            # Double-check actual server state
            if self._proc.is_sleeping(model.vllm.port):
                self.state.set_sleep_state(name, 2)
            else:
                return {"status": "already_awake", "model": name, "message": "Model is not sleeping"}

        # Validate GPU mode for wake
        current_gpu = self.gpu_mode
        if model.is_exclusive:
            if current_gpu != GPUMode.IDLE:
                return {"status": "error",
                        "message": f"Cannot wake exclusive model '{name}': GPU is {current_gpu}, must be idle"}
        else:
            if current_gpu not in (GPUMode.IDLE, GPUMode.SHARED):
                return {"status": "error",
                        "message": f"Cannot wake shared model '{name}': GPU is {current_gpu}, must be idle or shared"}

        log.info("Waking model '%s' (L2)", name)
        result = self._proc.wake_vllm(model.vllm.port)

        if result["status"] == "ok" or result["status"] == "killed_for_restart":
            self.state.set_sleep_state(name, None)

            if model.is_exclusive:
                self.state.set_multi({
                    "gpu_mode": GPUMode.EXCLUSIVE,
                    "profile_state": ProfileState.HEALTHY,
                })
                current_services = self.active_services
                if name not in current_services:
                    self.state.add_active_service(name)
            else:
                # Shared model: process was killed, restart via switch
                return self.switch(name)

            log.info("Model '%s' is now awake", name)

        return {**result, "model": name}

    # ── Status ────────────────────────────────────────────────────

    def status(self) -> dict:
        active = list(self.active_services)
        sleep_states = self.state.get_all_sleep_states()
        services_status = {}
        services_info = {}
        dead_services = []
        for svc_name in active:
            m = self._models.get(svc_name)
            if not m:
                dead_services.append(svc_name)
                continue
            info = {"mode": m.mode, "type": m.type}
            if m.is_vllm:
                info["port"] = m.vllm.port
                health = self.check_vllm_health(m.vllm.port)
            elif m.is_comfyui:
                info["port"] = m.comfyui.port
                health_url = m.comfyui.health_url or f"http://localhost:{m.comfyui.port}/system_stats"
                health = self.check_comfyui_health(health_url)
            elif m.is_ollama_daemon:
                info["port"] = m.ollama_daemon.port
                health = self.check_ollama_health(m.ollama_daemon.port)
            elif m.is_ollama:
                # Ollama models use daemon port; check if model loaded
                info["port"] = 11434
                info["model_ref"] = m.ollama.model_ref
                health = self.check_ollama_health(11434)
            elif m.is_ollama_cpp:
                info["port"] = m.ollama_cpp.port
                health = self.check_http_health(m.ollama_cpp.port, "/health")
            else:
                health = "?"
            # Append sleep state if applicable
            sleep_label = sleep_states.get(svc_name, "")
            if sleep_label:
                health = f"{health} (sleeping {sleep_label.upper()})"
            services_status[svc_name] = health
            services_info[svc_name] = info
            # Track dead services for auto-cleanup
            if health == "❌":
                dead_services.append(svc_name)

        # Auto-cleanup: remove dead services from state to avoid stale dashboard state
        if dead_services:
            log.warning("Auto-cleanup: removing dead services %s from state", dead_services)
            remaining = [s for s in active if s not in dead_services]
            self.state.set_active_services(remaining)
            active = remaining
            # If no services left, also reset GPU mode to idle
            if not active and self.gpu_mode != GPUMode.IDLE:
                log.info("No active services — resetting gpu_mode to idle")
                self.state.gpu_mode = GPUMode.IDLE

        return {
            "gpu_mode": self.gpu_mode,
            "active_services": active,
            "services_health": services_status,
            "services_info": services_info,
            "sleep_states": sleep_states,
            "gpu_used_mb": gpu_used_mb(),
            "gpu_total_mb": gpu_total_mb(),
            "vllm_pid": self._proc.vllm_pid,
            "comfyui_pid": self._proc.comfyui_pid,
        }

    # ── Force Reset ───────────────────────────────────────────────

    def force_reset(self) -> dict:
        """Nuclear reset: kill everything, verify GPU, clean state."""
        log.info("Force reset")

        # Collect ComfyUI config for proper stop
        comfyui_cfg = None
        for svc_name in self.active_services:
            m = self._models.get(svc_name)
            if m and m.is_comfyui:
                comfyui_cfg = m.comfyui
                break
        self._proc.stop_all(comfyui_cfg=comfyui_cfg)
        self._proc.force_kill_all()

        if not wait_gpu_free(timeout=20):
            try:
                import subprocess
                subprocess.run(["nvidia-smi", "--gpu-reset"], timeout=10, check=False)
                time.sleep(5)
            except Exception:
                pass

        self._lock.force_clear()

        self.state.set_multi({
            "gpu_mode": GPUMode.IDLE,
            "active_services": json.dumps([]),
            "profile_state": ProfileState.IDLE,
            "vllm_pid": "",
            "comfyui_pid": "",
            "sleep_state": "{}",
        })

        return {
            "status": "reset",
            "gpu_mode": GPUMode.IDLE,
            "gpu_free": gpu_used_mb() < GPU_FREE_THRESHOLD_MB,
        }

    # ── Internal Helpers ─────────────────────────────────────────

    def discover_local_models(self) -> dict:
        """Scan ~/models/ and ~/ComfyUI/models/ for unconfigured model directories."""
        discovered = []
        configured = sorted(self._models.keys())
        configured_dirs = set()

        # Build set of known model directories from configured YAMLs
        for m in self._models.values():
            if m.is_vllm and m.vllm.model_dir:
                configured_dirs.add(m.vllm.model_dir)

        models_base = Path.home() / "models"
        if models_base.exists():
            for d in sorted(models_base.iterdir()):
                if not d.is_dir() or d.name.startswith("."):
                    continue
                if d.name in configured_dirs:
                    continue
                config_json = d / "config.json"
                safetensors = list(d.glob("*.safetensors"))
                gguf_files = list(d.glob("*.gguf"))
                if config_json.exists():
                    try:
                        size_mb = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) // (1024*1024)
                    except Exception:
                        size_mb = 0
                    discovered.append({
                        "name": d.name, "path": str(d),
                        "type": "vllm", "size_mb": size_mb,
                        "files": [f.name for f in sorted(d.iterdir()) if f.is_file()][:20],
                    })
                elif gguf_files:
                    size_mb = sum(f.stat().st_size for f in gguf_files) // (1024*1024)
                    discovered.append({
                        "name": d.name, "path": str(d),
                        "type": "ollama_cpp_gguf", "size_mb": size_mb,
                        "files": [f.name for f in gguf_files],
                    })

        comfyui_models = Path.home() / "ComfyUI" / "models"
        if comfyui_models.exists():
            for sub in ["checkpoints", "loras", "diffusion_models", "vae", "ipadapter"]:
                subdir = comfyui_models / sub
                if not subdir.exists():
                    continue
                for f in sorted(subdir.glob("*.safetensors")):
                    name = f.stem
                    size_mb = f.stat().st_size // (1024*1024)
                    discovered.append({
                        "name": name, "path": str(f),
                        "type": f"comfyui_{sub.rstrip('s')}", "size_mb": size_mb,
                        "files": [f.name],
                    })

        return {"discovered": discovered, "configured": configured}

    def auto_deploy(self, name: str, model_type: str) -> dict:
        """Auto-generate YAML and deploy a discovered model."""
        models_dir = self.models_dir
        yaml_path = models_dir / f"{name}.yaml"
        if yaml_path.exists():
            return {"status": "error", "message": f"YAML already exists: {yaml_path}"}

        # Find next available port
        used_ports = set()
        for m in self._models.values():
            if m.port:
                used_ports.add(m.port)

        if model_type == "vllm":
            port = max([p for p in used_ports if p >= 8000] + [7999]) + 1
            yaml_content = f"""name: {name}
description: "{name} (auto-deployed)"
type: vllm
mode: shared
vllm:
  model_dir: {name}
  served_name: {name}
  port: {port}
  conda_env: qw36-27b-vllm
  gpu_memory_utilization: 0.83
  max_model_len: 131072
  max_num_seqs: 4
  kv_cache_dtype: auto
"""
        elif model_type.startswith("ollama_cpp"):
            port = max([p for p in used_ports if p >= 11435] + [11434]) + 1
            yaml_content = f"""name: {name}
description: "{name} (auto-deployed, ollama.cpp)"
type: ollama_cpp
mode: shared
cpu_only: true
ollama_cpp:
  model_path: ~/models/{name}/
  port: {port}
  threads: 16
  context_size: 16384
  gpu_layers: 0
"""
        else:
            return {"status": "error", "message": f"Unsupported type for auto-deploy: {model_type}"}

        yaml_path.write_text(yaml_content)
        log.info("Auto-generated YAML: %s", yaml_path)

        self._models = load_models(self.models_dir)
        result = self.switch(name)
        return result

    def _check_model_health(self, model: ModelConfig) -> bool:
        """Check if a specific model's service is healthy."""
        if model.is_vllm:
            return self.check_vllm_health(model.vllm.port) == "✅"
        elif model.is_comfyui:
            health_url = model.comfyui.health_url or f"http://localhost:{model.comfyui.port}/system_stats"
            return self.check_comfyui_health(health_url) == "✅"
        return False


# ─── Backward Compatibility ──────────────────────────────────────

class ProfileManager(ModelManager):
    """Backward-compatible alias. All v3.x code using ProfileManager will work."""
    pass
