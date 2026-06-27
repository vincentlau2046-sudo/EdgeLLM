"""EdgeLLM — Local LLM Profile Switcher"""

from .profile_manager import (
    ProfileManager,
    ProfileState,
    StateDB,
    GPULock,
    ProcessManager,
    VLLMConfig,
    ComfyUIConfig,
    Profile,
    gpu_used_mb,
    gpu_total_mb,
    wait_http,
    check_http_status,
)

__version__ = "3.0.0"
