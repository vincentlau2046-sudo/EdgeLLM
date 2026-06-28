"""
edge_llm/config.py — Configuration, constants, and model definitions.

v4.0: Profile concept eliminated. Models are self-describing plugins in models.d/.
Each YAML file declares its own mode (exclusive/shared) and resource requirements.
"""

from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import yaml


# ─── Path Constants ──────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent
MODELS_DIR = BASE_DIR / "models.d"
DEFAULT_STATE_DB = Path.home() / ".edge_llm" / "state.db"
DEFAULT_LOG_DIR = Path.home() / ".edge_llm" / "logs"
GPU_LOCK_PATH = Path("/tmp/edge_llm_gpu.lock")
MODEL_BASE = Path.home() / "models"
CONDA_ENVS = Path.home() / "miniconda3" / "envs"
COMFYUI_DIR = Path.home() / "ComfyUI"

# Legacy (backward compat)
DEFAULT_PROFILES = BASE_DIR / "profiles.yaml"


# ─── Process Management Constants ────────────────────────────────

STOP_SIGTERM_TIMEOUT = 10       # seconds to wait after SIGTERM before SIGKILL
VLLM_STARTUP_CHECK_INTERVAL = 0.5  # seconds between startup checks
VLLM_STARTUP_CHECK_ROUNDS = 20  # 10 seconds total for immediate-failure detection
HEALTH_CHECK_TIMEOUT = 300      # 5 minutes for vLLM to become healthy
GPU_FREE_TIMEOUT = 30           # seconds to wait for GPU memory release
GPU_FREE_THRESHOLD_MB = 2048    # MB below which GPU is considered "free"


# ─── Data Classes ────────────────────────────────────────────────

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
            import shlex
            flags.extend(shlex.split(self.extra_flags))
        return flags


@dataclass
class ComfyUIConfig:
    """ComfyUI configuration. Supports both native Python and legacy script modes."""
    conda_env: str = "comfyui"
    port: int = 8188
    working_dir: str = ""
    health_url: str = ""
    extra_flags: str = "--cache-none --enable-manager"
    # Legacy fallback (deprecated — native mode preferred)
    startup_script: str = ""
    stop_script: str = ""

    @property
    def use_native(self) -> bool:
        """True if we should use native Python process management."""
        return bool(self.conda_env and not self.startup_script)

    @property
    def resolved_working_dir(self) -> Path:
        wd = self.working_dir or str(COMFYUI_DIR)
        return Path(wd).expanduser().resolve()


@dataclass
class ModelConfig:
    """A deployable model/service — one per YAML in models.d/.

    Core attributes:
      name:        Unique identifier (must match YAML filename stem)
      description: Human-readable description
      mode:        'exclusive' (GPU fully locked) or 'shared' (coexists with other shared services)
      type:        'vllm' or 'comfyui'
      vllm:        VLLMConfig if type='vllm', else None
      comfyui:     ComfyUIConfig if type='comfyui', else None
    """
    name: str
    description: str
    mode: str  # 'exclusive' | 'shared'
    type: str = "vllm"  # 'vllm' | 'comfyui'
    vllm: Optional[VLLMConfig] = None
    comfyui: Optional[ComfyUIConfig] = None

    @property
    def is_exclusive(self) -> bool:
        return self.mode == "exclusive"

    @property
    def is_shared(self) -> bool:
        return self.mode == "shared"

    @property
    def is_vllm(self) -> bool:
        return self.type == "vllm" and self.vllm is not None

    @property
    def is_comfyui(self) -> bool:
        return self.type == "comfyui" and self.comfyui is not None


# ─── Legacy Profile class (backward compat, will be removed in Phase 7) ──

@dataclass
class Profile:
    name: str
    description: str
    gpu_owner: str
    vllm: Optional[VLLMConfig] = None
    comfyui: Optional[ComfyUIConfig] = None
    switch_cost_sec: int = 0


# ─── Model Loading ───────────────────────────────────────────────

def load_models(models_dir: Path = MODELS_DIR) -> dict[str, ModelConfig]:
    """Load model configs from models.d/ directory.

    Each YAML file defines one model. The 'name' field must match the filename stem.
    Returns dict keyed by model name.
    """
    result: dict[str, ModelConfig] = {}
    if not models_dir.exists():
        return result

    for yaml_file in sorted(models_dir.glob("*.yaml")):
        raw = yaml.safe_load(yaml_file.read_text())
        model_name = yaml_file.stem

        # Validate name matches filename
        if raw.get("name") != model_name:
            raise ValueError(
                f"Name mismatch in {yaml_file}: YAML name='{raw.get('name')}' "
                f"vs filename stem='{model_name}'"
            )

        # Parse type
        model_type = raw.get("type", "vllm")

        # Parse vllm config if present
        vllm_cfg = None
        if raw.get("vllm"):
            vllm_cfg = VLLMConfig(**raw["vllm"])

        # Parse comfyui config if present
        comfy_cfg = None
        if raw.get("comfyui"):
            comfy_cfg = ComfyUIConfig(**raw["comfyui"])

        # For type=comfyui, parse top-level comfyui fields
        if model_type == "comfyui" and not comfy_cfg:
            comfy_fields = {}
            for f in ("conda_env", "port", "working_dir", "health_url", "extra_flags"):
                if f in raw:
                    comfy_fields[f] = raw[f]
            if comfy_fields:
                comfy_cfg = ComfyUIConfig(**comfy_fields)

        result[model_name] = ModelConfig(
            name=model_name,
            description=raw.get("description", model_name),
            mode=raw.get("mode", "exclusive"),
            type=model_type,
            vllm=vllm_cfg,
            comfyui=comfy_cfg,
        )

    return result


# ─── Legacy Profile Loading (backward compat, will be removed in Phase 7) ──

def load_profiles(profiles_path: Path) -> dict[str, Profile]:
    """Load profiles from YAML configuration file. (Legacy, will be removed.)"""
    raw = yaml.safe_load(profiles_path.read_text())["profiles"]
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
