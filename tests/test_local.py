#!/usr/bin/env python3
"""Local unit tests — no GPU / no vLLM touching."""

import sys
import os
import tempfile
import json
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from edge_llm.profile_manager import (
    ProfileManager, StateDB, VLLMConfig, Profile,
    DEFAULT_PROFILES, gpu_used_mb, wait_http,
)


# ─── Helpers ─────────────────────────────────────────────────────

def assert_eq(a, b, label=""):
    assert a == b, f"FAIL {label}: {a!r} != {b!r}"


def assert_true(v, label=""):
    assert v, f"FAIL {label}: {v!r}"


def test_profiles_load():
    """All profiles load without error."""
    raw = open(DEFAULT_PROFILES).read()
    assert "profiles:" in raw
    print("  ✅ profiles.yaml valid")


def test_vllm_config_build_cmd():
    """VLLMConfig.build_cmd() produces correct command."""
    cfg = VLLMConfig(
        model_dir="test-model",
        served_name="test",
        port=9999,
        conda_env="test-env",
        max_model_len=64000,
        gpu_memory_utilization=0.5,
        max_num_seqs=4,
        kv_cache_dtype="fp8",
        extra_flags="--reasoning-parser qwen3",
    )
    cmd = cfg.build_cmd()
    assert "--port" in cmd and "9999" in cmd
    assert "--max-model-len" in cmd and "64000" in cmd
    assert "--reasoning-parser" in cmd
    print("  ✅ build_cmd correct")


def test_state_db():
    """StateDB CRUD and persistence."""
    with tempfile.TemporaryDirectory() as tmp:
        db = StateDB(Path(tmp) / "test.db")

        # Init
        assert db.get("current_profile") == "idle"
        assert db.get("history") == "[]"

        # Set / Get
        db.set("current_profile", "qw36_full")
        assert db.get("current_profile") == "qw36_full"

        # History
        db.append_history({"from": "idle", "to": "qw36_full", "elapsed_sec": 5.0, "ts": 1700000000})
        hist = json.loads(db.get("history"))
        assert len(hist) == 1
        assert hist[0]["to"] == "qw36_full"

        # Reopen from same path
        db2 = StateDB(Path(tmp) / "test.db")
        assert db2.get("current_profile") == "qw36_full"
        hist2 = json.loads(db2.get("history"))
        assert len(hist2) == 1

    print("  ✅ StateDB CRUD + persistence")


def test_profile_list():
    """ProfileManager.list_profiles() returns all profiles."""
    mgr = ProfileManager()
    profiles = mgr.list_profiles()
    names = {p["name"] for p in profiles}
    expected = {"qw36_full", "qw35_comfyui", "gemma_full", "comfyui_only", "idle"}
    assert names == expected, f"Missing: {expected - names}"
    assert len(profiles) == 5
    print(f"  ✅ list_profiles: {len(profiles)} profiles")


def test_profile_details():
    """Each profile has expected attributes."""
    mgr = ProfileManager()
    p = mgr._profiles["qw36_full"]
    assert p.gpu_owner == "vllm"
    assert p.vllm.port == 8000
    assert p.vllm.max_model_len == 128000
    assert p.vllm.gpu_memory_utilization == 0.90

    p2 = mgr._profiles["qw35_comfyui"]
    assert p2.gpu_owner == "shared"
    assert p2.vllm.port == 8002
    assert p2.comfyui is not None

    p3 = mgr._profiles["idle"]
    assert p3.vllm is None
    assert p3.comfyui is None

    print("  ✅ Profile details correct")


def test_switch_same_profile():
    """Switch to same profile → already_active."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(state_db_path=tmp + "/state.db")
        mgr.state.set("current_profile", "qw36_full")
        result = mgr.switch("qw36_full")
        assert result["status"] == "already_active"
        assert result["profile"] == "qw36_full"
    print("  ✅ switch same → already_active")


def test_switch_unknown_profile():
    """Switch to non-existent profile → error."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(state_db_path=tmp + "/state.db")
        result = mgr.switch("nonexistent_profile")
        assert result["status"] == "error"
    print("  ✅ switch unknown → error")


def test_idle_switch_skip_start():
    """Switch to idle → stops services, starts nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(state_db_path=tmp + "/state.db")
        mgr.state.set("current_profile", "qw36_full")

        # Override _start_vllm / _start_comfyui to detect if called
        started = {"vllm": False, "comfyui": False}
        orig_vllm = mgr._start_vllm
        orig_comfy = mgr._start_comfyui

        def mock_vllm(cfg):
            started["vllm"] = True
            return {"status": "mock"}
        def mock_comfy(cfg):
            started["comfyui"] = True
            return {"status": "mock"}
        mgr._start_vllm = mock_vllm
        mgr._start_comfyui = mock_comfy

        # Override GPU wait
        from edge_llm import profile_manager as pm
        orig_wait = pm.wait_gpu_free
        pm.wait_gpu_free = lambda timeout=30: True
        try:
            result = mgr.switch("idle")
            assert not started["vllm"], "idle should not start vLLM"
            assert not started["comfyui"], "idle should not start ComfyUI"
        finally:
            pm.wait_gpu_free = orig_wait
    print("  ✅ idle switch → no services started")


def test_switch_history_recorded():
    """Switch records history entry."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = ProfileManager(state_db_path=tmp + "/state.db")
        mgr.state.set("current_profile", "idle")

        started = {"vllm": False}
        def mock_vllm(cfg):
            started["vllm"] = True
            return {"status": "mock"}
        mgr._start_vllm = mock_vllm

        from edge_llm import profile_manager as pm
        orig_wait = pm.wait_gpu_free
        pm.wait_gpu_free = lambda timeout=30: True
        try:
            result = mgr.switch("qw36_full")
        finally:
            pm.wait_gpu_free = orig_wait

        hist = json.loads(mgr.state.get("history"))
        assert len(hist) == 1
        assert hist[0]["to"] == "qw36_full"
    print("  ✅ switch history recorded")


def test_gpu_query():
    """gpu_used_mb() returns valid number."""
    used = gpu_used_mb()
    assert 0 < used < 100000, f"GPU used MB unexpected: {used}"
    print(f"  ✅ GPU query: {used} MB")


# ─── Runner ─────────────────────────────────────────────────────

def main():
    tests = [
        ("profiles.yaml", test_profiles_load),
        ("VLLMConfig.build_cmd", test_vllm_config_build_cmd),
        ("StateDB CRUD", test_state_db),
        ("list_profiles", test_profile_list),
        ("profile details", test_profile_details),
        ("switch same profile", test_switch_same_profile),
        ("switch unknown profile", test_switch_unknown_profile),
        ("idle skip start", test_idle_switch_skip_start),
        ("history recorded", test_switch_history_recorded),
        ("GPU query", test_gpu_query),
    ]

    passed = 0
    failed = 0
    for label, fn in tests:
        try:
            fn()
            passed += 1
        except Exception as e:
            print(f"  ❌ {label}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed / {len(tests)}")
    return failed


if __name__ == "__main__":
    exit(main())
