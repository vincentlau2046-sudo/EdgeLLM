"""EdgeLLM — Local LLM Model Switcher (v4.0)"""

from .manager import ModelManager, ProfileManager
from .state import GPUMode, ProfileState, StateDB, validate_transition
from .gpu_lock import GPULock
from .process_manager import ProcessManager
from .config import (
    VLLMConfig,
    ComfyUIConfig,
    ModelConfig,
    load_models,
    MODELS_DIR,
)
from .health import (
    gpu_used_mb,
    gpu_total_mb,
    wait_http,
    check_http_status,
)

__version__ = "4.0.0"
