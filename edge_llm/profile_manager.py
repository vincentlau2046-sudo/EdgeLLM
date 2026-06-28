"""
edge_llm/profile_manager.py — Backward-compatible re-export wrapper (v4.0).

v4.0 additions:
  - ModelConfig, load_models, MODELS_DIR from config
  - GPUMode, validate_transition from state
  - ModelManager from manager (ProfileManager is now an alias)

Existing imports like `from edge_llm.profile_manager import ProfileManager`
will continue to work.
"""

# Re-export all public symbols
from .config import (
    BASE_DIR,
    MODELS_DIR,
    DEFAULT_PROFILES,
    DEFAULT_STATE_DB,
    DEFAULT_LOG_DIR,
    GPU_LOCK_PATH,
    MODEL_BASE,
    CONDA_ENVS,
    COMFYUI_DIR,
    STOP_SIGTERM_TIMEOUT,
    VLLM_STARTUP_CHECK_INTERVAL,
    VLLM_STARTUP_CHECK_ROUNDS,
    HEALTH_CHECK_TIMEOUT,
    GPU_FREE_TIMEOUT,
    GPU_FREE_THRESHOLD_MB,
    VLLMConfig,
    ComfyUIConfig,
    ModelConfig,
    Profile,          # Legacy
    load_models,
    load_profiles,    # Legacy
)
from .state import (
    GPUMode,
    ProfileState,
    StateDB,
    validate_transition,
)
from .gpu_lock import (
    GPULock,
)
from .health import (
    gpu_used_mb,
    gpu_total_mb,
    wait_gpu_free,
    check_http_status,
    wait_http,
    kill_port,
)
from .process_manager import (
    ProcessManager,
)
from .manager import (
    ModelManager,
    ProfileManager,   # Backward compat alias
)

# Legacy alias
GPU_LOCK = GPU_LOCK_PATH

__all__ = [
    # Config
    "BASE_DIR", "MODELS_DIR", "DEFAULT_PROFILES", "DEFAULT_STATE_DB", "DEFAULT_LOG_DIR",
    "GPU_LOCK_PATH", "GPU_LOCK", "MODEL_BASE", "CONDA_ENVS", "COMFYUI_DIR",
    "STOP_SIGTERM_TIMEOUT", "VLLM_STARTUP_CHECK_INTERVAL",
    "VLLM_STARTUP_CHECK_ROUNDS", "HEALTH_CHECK_TIMEOUT",
    "GPU_FREE_TIMEOUT", "GPU_FREE_THRESHOLD_MB",
    "VLLMConfig", "ComfyUIConfig", "ModelConfig",
    "Profile", "load_models", "load_profiles",
    # State
    "GPUMode", "ProfileState", "StateDB", "validate_transition",
    # GPU Lock
    "GPULock",
    # Health
    "gpu_used_mb", "gpu_total_mb", "wait_gpu_free",
    "check_http_status", "wait_http", "kill_port",
    # Process Manager
    "ProcessManager",
    # Manager
    "ModelManager", "ProfileManager",
]
