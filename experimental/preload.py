"""
edge_llm/preload.py — Model weight preloader.

Keeps model files in OS page cache (CPU RAM) so vLLM switch avoids disk I/O.
Strategy:
  1. mmap the model files into a background process
  2. Keep the process running to hold the mapping
  3. When vLLM needs the model, files are already cached → fast GPU transfer

This is NOT about loading models into CPU for inference.
It's about keeping the WEIGHT FILES in RAM so disk reads are zero.
"""

import os
import sys
import mmap
import time
import json
import signal
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("edge_llm.preload")

MODEL_BASE = Path.home() / "models"

# Known models and their key weight files
MODEL_FILES = {
    "Qwen3.6-27B-Text-NVFP4-MTP": [
        "consolidated.safetensors",
    ],
    "Qwen3.5-9B-GPTQ-4bit/Qwen3.5-9B-GPTQ-4bit": [
        "model-00001-of-00002.safetensors",
        "model-00002-of-00002.safetensors",
    ],
    "RedHatAI/gemma-4-26B-A4B-it-NVFP4": [
        "consolidated.safetensors",
    ],
}

# Max RAM for cache (don't starve system)
MAX_CACHE_GB = int(os.environ.get("EDGE_PRELOAD_MAX_GB", "16"))


class ModelPreloader:
    """Preload model weights into page cache."""

    def __init__(self, max_cache_gb: int = MAX_CACHE_GB):
        self.max_cache_bytes = max_cache_gb * 1024**3
        self._mmaps: dict[str, mmap.mmap] = {}
        self._paths: dict[str, Path] = {}

    def preload(self, model_dir: str) -> dict:
        """Preload model into page cache."""
        base = MODEL_BASE / model_dir
        if not base.exists():
            return {"status": "error", "message": f"Model not found: {base}"}

        total = 0
        files = []
        for f in MODEL_FILES.get(model_dir, []):
            path = base / f
            if path.exists():
                size = path.stat().st_size
                total += size
                files.append({"file": f, "size_mb": round(size / 1024**2, 1)})

        # Check RAM availability
        free = self._free_ram_bytes()
        if total > self.max_cache_bytes:
            log.warning("Model %s (%.1f GB) exceeds cache limit (%d GB)",
                        model_dir, total / 1024**3, self.max_cache_gb)
            return {"status": "skipped", "reason": "exceeds cache limit"}

        if total > free:
            log.warning("Model %s needs %.1f GB but only %.1f GB free",
                        model_dir, total / 1024**3, free / 1024**3)
            return {"status": "skipped", "reason": "insufficient RAM"}

        # mmap all files to bring into page cache
        t0 = time.time()
        for f in MODEL_FILES.get(model_dir, []):
            path = base / f
            if path.exists():
                with open(path, "rb") as fp:
                    mm = mmap.mmap(fp.fileno(), 0, access=mmap.ACCESS_READ)
                    # Touch every page to populate page cache
                    for offset in range(0, len(mm), mmap.PAGESIZE):
                        mm[offset:offset+1]
                    self._mmaps[str(path)] = mm
                    self._paths[str(path)] = path
        elapsed = time.time() - t0

        return {
            "status": "cached",
            "model": model_dir,
            "files": files,
            "total_mb": round(total / 1024**2, 1),
            "elapsed_sec": round(elapsed, 1),
        }

    def uncached(self) -> dict:
        """Clear all cached models (free RAM)."""
        count = 0
        for path_str, mm in self._mmaps.items():
            try:
                mm.close()
                count += 1
            except Exception:
                pass
        self._mmaps.clear()
        self._paths.clear()
        return {"status": "cleared", "files": count}

    def status(self) -> dict:
        """Current preload status."""
        return {
            "cached_models": [str(p) for p in self._mmaps.values()],
            "total_files": len(self._mmaps),
            "free_ram_gb": round(self._free_ram_bytes() / 1024**3, 1),
            "max_cache_gb": self.max_cache_gb,
        }

    def _free_ram_bytes(self) -> int:
        """Estimate free RAM."""
        try:
            with open("/proc/meminfo") as f:
                mem = f.read()
            free = int([l for l in mem.splitlines() if l.startswith("MemFree")][0].split()[1])
            avail = int([l for l in mem.splitlines() if l.startswith("MemAvailable")][0].split()[1])
            # Use MemAvailable (more realistic)
            return avail * 1024
        except Exception:
            return 0


def cli_preload(args):
    preloader = ModelPreloader()
    if not args:
        print("Usage: edge-llm preload <model_dir>")
        print(f"Available models: {list(MODEL_FILES.keys())}")
        return

    result = preloader.preload(args[0])
    if result["status"] == "cached":
        print(f"✅ Preloaded {result['model']} ({result['total_mb']} MB) in {result['elapsed_sec']}s")
    else:
        print(f"⚠️ {result['status']}: {result.get('reason', '')}")


def cli_preload_status():
    preloader = ModelPreloader()
    s = preloader.status()
    print(f"Cached: {s['total_files']} files")
    print(f"Free RAM: {s['free_ram_gb']} GB / {s['max_cache_gb']} GB limit")


def cli_preload_clear():
    preloader = ModelPreloader()
    result = preloader.uncached()
    print(f"Cleared {result['files']} cached files")


# Register with main CLI
def register_with_cli(dispatch):
    dispatch["preload"] = cli_preload
    dispatch["preload_status"] = cli_preload_status
    dispatch["preload_clear"] = cli_preload_clear

# NOTE: preload is experimental and not integrated into the switch flow.
# It can be activated manually via: edge-llm preload <model>
# Future: integrate into switch() to pre-load target model weights
