"""
edge_llm/manager.py — Model orchestration layer (v4.0).

v4.0: Profile concept eliminated. Models are self-describing plugins.
Tri-state GPU mode: idle / exclusive / shared.
Switch rules enforced by validate_transition().
"""

import os
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
        """List all available models from models.d/."""
        return [
            {
                "name": m.name,
                "description": m.description,
                "mode": m.mode,
                "type": m.type,
                "active": m.name in self.active_services,
            }
            for m in self._models.values()
        ]

    def find_model_by_served_name(self, served_name: str) -> Optional[ModelConfig]:
        """Find vLLM model by its served_model_name (for proxy routing)."""
        for m in self._models.values():
            if m.vllm and m.vllm.served_name == served_name:
                return m
        return None

    # ── Health Check (tri-state) ─────────────────────────────────

    def check_vllm_health(self, port: int) -> str:
        return check_http_status(f"http://localhost:{port}/health")

    def check_comfyui_health(self, url: str) -> str:
        return check_http_status(url)

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

        # Check orphan PIDs
        if self._proc.vllm_pid:
            try:
                os.killpg(self._proc.vllm_pid, 0)
            except (ProcessLookupError, PermissionError):
                if not any(s in actual_services for s in actual_services if self._models.get(s, ModelConfig(name="", description="", mode="", type="vllm")).is_vllm):
                    actions.append(f"Orphan vllm_pid={self._proc.vllm_pid} dead — clearing")
                    self.state.set("vllm_pid", "")

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

        # Already running?
        if target in self.active_services:
            # Check if this model is healthy
            is_healthy = self._check_model_health(model)
            if is_healthy:
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

    def _switch_to_idle(self) -> dict:
        """Stop all services and transition to idle."""
        current_mode = self.gpu_mode
        if current_mode == GPUMode.IDLE and not self.active_services:
            return {"status": "already_active", "model": "idle"}

        if not self._lock.acquire():
            return {"status": "error", "message": "GPU switch in progress (lock held)"}

        t0 = time.time()
        from_services = list(self.active_services)
        log.info("Switch to idle from %s (gpu_mode=%s)", from_services, current_mode)

        try:
            # Stop all services
            self._proc.stop_all()
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
            results["vllm"] = self._proc.start_vllm(model.vllm)
        elif model.is_comfyui:
            results["comfyui"] = self._proc.start_comfyui(model.comfyui)

        # Validate
        failed = False
        for svc, res in results.items():
            if res.get("status") not in ("healthy", "started"):
                failed = True
                break

        if failed:
            self.state.set("profile_state", ProfileState.ERROR)
            # Clean up partial start
            self._proc.stop_all()
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

    def _shared_add_service(self, model: ModelConfig) -> dict:
        """Add a shared service to existing shared mode. V1: full restart."""
        t0 = time.time()

        # Get current running shared services
        current_services = list(self.active_services)
        target_services = current_services + [model.name]

        log.info("Shared add: %s → %s", current_services, target_services)

        # V1: stop all, then start all
        self._proc.stop_all()
        if not wait_gpu_free(timeout=15):
            self._proc.force_kill_all()
            wait_gpu_free(timeout=10)

        # Start all target services
        results = {}
        for svc_name in target_services:
            svc = self._models.get(svc_name)
            if not svc:
                continue
            if svc.is_vllm:
                results[f"vllm_{svc_name}"] = self._proc.start_vllm(svc.vllm)
            elif svc.is_comfyui:
                results[f"comfyui_{svc_name}"] = self._proc.start_comfyui(svc.comfyui)

        # Validate all
        failed = []
        for key, res in results.items():
            if res.get("status") not in ("healthy", "started"):
                failed.append(key)

        if failed:
            self.state.set("profile_state", ProfileState.ERROR)
            return {
                "status": "error",
                "message": f"Failed services: {failed}",
                "results": results,
            }

        elapsed = round(time.time() - t0, 1)
        self.state.set_multi({
            "gpu_mode": GPUMode.SHARED,
            "active_services": json.dumps(target_services),
            "profile_state": ProfileState.HEALTHY,
        })

        return {
            "status": "switched",
            "model": model.name,
            "gpu_mode": GPUMode.SHARED,
            "elapsed_sec": elapsed,
            "active_services": target_services,
            "results": results,
        }

    # ── Stop Single Service ──────────────────────────────────────

    def stop_service(self, name: str) -> dict:
        """Stop a single shared service. Other shared services remain.

        If this is the last shared service, auto-transition to idle.
        """
        if name not in self.active_services:
            return {"status": "error", "message": f"Service '{name}' is not running"}

        if self.gpu_mode == GPUMode.EXCLUSIVE:
            return {"status": "error", "message": "Cannot stop individual service in exclusive mode. Use 'switch idle'."}

        model = self._models.get(name)
        if not model:
            return {"status": "error", "message": f"Unknown model: {name}"}

        # Stop the specific service
        if model.is_vllm:
            self._proc.stop_vllm()
        elif model.is_comfyui:
            self._proc.stop_comfyui()

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

    # ── Status ────────────────────────────────────────────────────

    def status(self) -> dict:
        active = self.active_services
        services_status = {}
        for svc_name in active:
            m = self._models.get(svc_name)
            if not m:
                continue
            if m.is_vllm:
                services_status[svc_name] = self.check_vllm_health(m.vllm.port)
            elif m.is_comfyui:
                health_url = m.comfyui.health_url or f"http://localhost:{m.comfyui.port}/system_stats"
                services_status[svc_name] = self.check_comfyui_health(health_url)

        return {
            "gpu_mode": self.gpu_mode,
            "active_services": active,
            "services_health": services_status,
            "gpu_used_mb": gpu_used_mb(),
            "gpu_total_mb": gpu_total_mb(),
            "vllm_pid": self._proc.vllm_pid,
            "comfyui_pid": self._proc.comfyui_pid,
        }

    # ── Force Reset ───────────────────────────────────────────────

    def force_reset(self) -> dict:
        """Nuclear reset: kill everything, verify GPU, clean state."""
        log.info("Force reset")

        self._proc.stop_all()
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
        })

        return {
            "status": "reset",
            "gpu_mode": GPUMode.IDLE,
            "gpu_free": gpu_used_mb() < GPU_FREE_THRESHOLD_MB,
        }

    # ── Internal Helpers ─────────────────────────────────────────

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
