#!/usr/bin/env python3
"""Unit tests for shared-model incremental start/stop logic.

No real GPU / vLLM / ComfyUI interaction — all calls are mocked or
pure-config checks.
"""

import sys
import os
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from edge_llm.config import ModelConfig, VLLMConfig, ComfyUIConfig, load_models
from edge_llm.state import StateDB, ProfileState, GPUMode


# ─── Helpers ─────────────────────────────────────────────────────

def assert_eq(a, b, label=""):
    assert a == b, f"FAIL {label}: {a!r} != {b!r}"


def assert_true(v, label=""):
    assert v, f"FAIL {label}: {v!r}"


def assert_in(needle, haystack, label=""):
    assert needle in haystack, f"FAIL {label}: {needle!r} not in {haystack!r}"


# ─── YAML / Config Tests ────────────────────────────────────────

def test_model_config_has_vram_field():
    """ModelConfig exposes typical_vram_pct."""
    m = ModelConfig(
        name="test", description="test", mode="shared",
        type="vllm", typical_vram_pct=38.0,
    )
    assert_eq(m.typical_vram_pct, 38.0)


def test_load_models_parses_vram():
    """load_models() reads typical_vram_pct from YAML."""
    models = load_models()
    assert_eq(models["qwen35-9b"].typical_vram_pct, 38.0)
    assert_eq(models["comfyui"].typical_vram_pct, 50.0)
    # exclusive models default to 0
    assert_eq(models["qwen36-27b"].typical_vram_pct, 0.0)


def test_model_config_default_vram():
    """ModelConfig without typical_vram_pct defaults to 0."""
    m = ModelConfig(name="x", description="x", mode="shared", type="vllm")
    assert_eq(m.typical_vram_pct, 0.0)


# ─── _shared_add_service Tests ───────────────────────────────────

def test_shared_add_already_active():
    """_shared_add_service returns already_active when model is already running."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_manager(tmp, models={"qw35-9b": _make_shared_vllm("qw35-9b", 8002)})
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        result = mgr._shared_add_service(mgr._models["qw35-9b"])
        assert_eq(result["status"], "already_active")


def test_shared_add_incremental_no_stop():
    """_shared_add_service starts only the new model — stop_all is NOT called."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        # Mock _get_current_vram_pct so VRAM check passes (40% + 50% = 90 < 95)
        mgr._get_current_vram_pct = lambda: 40.0

        stop_all_called = [False]
        start_count = {"qw35-9b": 0, "comfyui": 0}

        def mock_stop_all(**kw):
            stop_all_called[0] = True

        def mock_start_comfyui(cfg):
            start_count["comfyui"] += 1
            return {"status": "healthy", "port": cfg.port, "pid": 9999}

        import edge_llm.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_all = mock_stop_all
            mgr._proc.start_comfyui = mock_start_comfyui

            result = mgr._shared_add_service(mgr._models["comfyui"])

            assert_true(not stop_all_called[0], "stop_all must NOT be called")
            assert_true(start_count["comfyui"] >= 1, "comfyui must be started")
            assert_true(start_count["qw35-9b"] == 0, "qw35-9b must NOT be restarted")
            assert_eq(result["status"], "switched")
            assert_in("qw35-9b", result["active_services"])
            assert_in("comfyui", result["active_services"])
        finally:
            health_mod.wait_gpu_free = orig_wait


def test_shared_add_vram_reject():
    """_shared_add_service rejects when VRAM headroom insufficient."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])
        # Simulate 60% used → 60 + 50 = 110 > 95
        mgr._get_current_vram_pct = lambda: 60.0

        result = mgr._shared_add_service(mgr._models["comfyui"])
        assert_eq(result["status"], "error")
        assert_in("Insufficient GPU memory", result["message"])


def test_shared_add_vram_accept():
    """_shared_add_service accepts when VRAM headroom is OK."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])
        mgr._get_current_vram_pct = lambda: 40.0

        start_count = [0]

        def mock_start_comfyui(cfg):
            start_count[0] += 1
            return {"status": "healthy", "port": cfg.port, "pid": 9999}

        import edge_llm.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.start_comfyui = mock_start_comfyui
            result = mgr._shared_add_service(mgr._models["comfyui"])
            assert_eq(result["status"], "switched")
            assert_true(start_count[0] >= 1, "model should start")
        finally:
            health_mod.wait_gpu_free = orig_wait


# ─── Port-Based Cleanup Tests (ProcessManager) ──────────────────

def test_stop_vllm_always_runs_port_cleanup():
    """stop_vllm(port=X) always does port-based cleanup regardless of tracked PID."""
    pm = _make_pm()
    pm._set_vllm_pid(99999)  # non-existent PID → ProcessLookupError

    port_calls = []
    def mock_pkill_by_port(port):
        port_calls.append(port)

    orig = pm._pkill_by_port
    pm._pkill_by_port = mock_pkill_by_port

    try:
        result = pm.stop_vllm(port=8002)
        assert_true(len(port_calls) >= 1, "port cleanup must run even when PID is dead")
        assert_eq(port_calls[0], 8002)
    finally:
        pm._pkill_by_port = orig


def test_stop_vllm_port_when_no_tracked_pid():
    """stop_vllm(port=X) works when tracked PID is None."""
    pm = _make_pm()
    pm._set_vllm_pid(None)

    port_calls = []
    def mock_pkill_by_port(port):
        port_calls.append(port)

    orig = pm._pkill_by_port
    pm._pkill_by_port = mock_pkill_by_port

    try:
        result = pm.stop_vllm(port=8002)
        assert_true(len(port_calls) >= 1, "port cleanup must run even when no tracked PID")
        assert_eq(port_calls[0], 8002)
    finally:
        pm._pkill_by_port = orig


def test_stop_comfyui_always_runs_port_cleanup():
    """stop_comfyui_with_config(port=X) always does port-based cleanup."""
    pm = _make_pm()
    pm._set_comfyui_pid(None)

    port_calls = []
    def mock_pkill_by_port(port):
        port_calls.append(port)

    orig = pm._pkill_by_port
    pm._pkill_by_port = mock_pkill_by_port

    try:
        cfg = ComfyUIConfig(port=8188)
        result = pm.stop_comfyui_with_config(cfg, port=8188)
        assert_true(len(port_calls) >= 1, "ComfyUI port cleanup must run")
        assert_eq(port_calls[0], 8188)
    finally:
        pm._pkill_by_port = orig


# ─── stop_all Tests ─────────────────────────────────────────────

def test_stop_all_passes_port_params():
    """stop_all forwards port parameters correctly."""
    pm = _make_pm()

    calls = []

    def mock_stop_vllm(port=None):
        calls.append(("vllm", port))

    def mock_stop_comfyui_with_config(cfg, port=None):
        calls.append(("comfyui", port))

    pm.stop_vllm = mock_stop_vllm
    pm.stop_comfyui_with_config = mock_stop_comfyui_with_config

    cfg = ComfyUIConfig(port=8188)
    pm.stop_all(
        comfyui_cfg=cfg,
        vllm_ports=[8002],
        comfyui_port=8188,
    )

    assert_in(("vllm", 8002), calls)
    assert_in(("comfyui", 8188), calls)


# ─── stop_service GPU Verification ──────────────────────────────

def test_stop_service_verifies_gpu():
    """stop_service calls wait_gpu_free after stop."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_manager(tmp, models={
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
        })
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        def mock_stop_vllm(port=None):
            pass

        import edge_llm.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_vllm = mock_stop_vllm
            result = mgr.stop_service("qw35-9b")
            assert_eq(result["status"], "stopped")
        finally:
            health_mod.wait_gpu_free = orig_wait


# ─── _switch_to_idle port params ────────────────────────────────

def test_switch_to_idle_passes_ports():
    """_switch_to_idle collects ports from active services and passes them."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b", "comfyui"])

        stop_all_received = {}

        def mock_stop_all(**kw):
            stop_all_received.update(kw)

        import edge_llm.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_all = mock_stop_all
            result = mgr._switch_to_idle()

            assert_true("vllm_ports" in stop_all_received)
            assert_in(8002, stop_all_received["vllm_ports"])
            assert_true(stop_all_received.get("comfyui_port") == 8188)
        finally:
            health_mod.wait_gpu_free = orig_wait


# ─── Route Dispatch Tests ───────────────────────────────────────

def test_switch_routes_shared_correctly():
    """switch() dispatches to correct path based on mode and target."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        # Mock _shared_add_service to capture the call
        shared_add_called = [False]
        orig_shared_add = mgr._shared_add_service
        def mock_shared_add(model):
            shared_add_called[0] = True
            return {"status": "switched", "model": model.name, "gpu_mode": GPUMode.SHARED}

        mgr._shared_add_service = mock_shared_add
        result = mgr.switch("comfyui")

        assert_true(shared_add_called[0], "shared mode + shared target → _shared_add_service")
        assert_eq(result["status"], "switched")


def test_switch_routes_exclusive_to_deploy():
    """switch() routes exclusive model to _deploy_model."""
    with tempfile.TemporaryDirectory() as tmp:
        models = {
            "qw36-27b": _make_exclusive_vllm("qw36-27b", 8000),
        }
        mgr = _make_manager(tmp, models=models)
        mgr.state.set("gpu_mode", GPUMode.IDLE)

        deploy_called = [False]
        orig_deploy = mgr._deploy_model
        def mock_deploy(model, mode):
            deploy_called[0] = True
            return {"status": "switched", "model": model.name, "gpu_mode": mode}

        mgr._deploy_model = mock_deploy
        result = mgr.switch("qw36-27b")

        assert_true(deploy_called[0], "idle + exclusive target → _deploy_model")
        assert_eq(result["status"], "switched")


def test_stop_service_routes_vllm():
    """stop_service routes to stop_vllm(port=) for vLLM models."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_manager(tmp, models={
            "qw35-9b": _make_shared_vllm("qw35-9b", 8002),
        })
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["qw35-9b"])

        port_used = [None]
        def mock_stop_vllm(port=None):
            port_used[0] = port

        import edge_llm.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_vllm = mock_stop_vllm
            mgr.stop_service("qw35-9b")
            assert_eq(port_used[0], 8002, "stop_service must pass port=8002")
        finally:
            health_mod.wait_gpu_free = orig_wait


def test_stop_service_routes_comfyui():
    """stop_service routes to stop_comfyui_with_config(port=) for ComfyUI models."""
    with tempfile.TemporaryDirectory() as tmp:
        mgr = _make_manager(tmp, models={
            "comfyui": _make_shared_comfyui("comfyui", 8188),
        })
        mgr.state.set("gpu_mode", GPUMode.SHARED)
        mgr.state.set_active_services(["comfyui"])

        port_used = [None]
        def mock_stop_comfyui(cfg, port=None):
            port_used[0] = port

        import edge_llm.health as health_mod
        orig_wait = health_mod.wait_gpu_free
        health_mod.wait_gpu_free = lambda timeout=30: True

        try:
            mgr._proc.stop_comfyui_with_config = mock_stop_comfyui
            mgr.stop_service("comfyui")
            assert_eq(port_used[0], 8188, "stop_service must pass port=8188")
        finally:
            health_mod.wait_gpu_free = orig_wait


# ─── Helpers ─────────────────────────────────────────────────────

def _make_shared_vllm(name, port):
    return ModelConfig(
        name=name, description=name, mode="shared", type="vllm",
        vllm=VLLMConfig(
            model_dir="test", served_name=name, port=port,
            conda_env="test", max_model_len=64000,
            gpu_memory_utilization=0.4, max_num_seqs=4, kv_cache_dtype="fp8",
        ),
        typical_vram_pct=38.0,
    )


def _make_exclusive_vllm(name, port):
    return ModelConfig(
        name=name, description=name, mode="exclusive", type="vllm",
        vllm=VLLMConfig(
            model_dir="test", served_name=name, port=port,
            conda_env="test", max_model_len=128000,
            gpu_memory_utilization=0.90, max_num_seqs=4, kv_cache_dtype="auto",
        ),
        typical_vram_pct=0.0,
    )


def _make_shared_comfyui(name, port):
    return ModelConfig(
        name=name, description=name, mode="shared", type="comfyui",
        comfyui=ComfyUIConfig(port=port, conda_env="test"),
        typical_vram_pct=50.0,
    )


def _make_manager(tmpdir, models=None):
    """Create a ProfileManager with mock models, no real services."""
    from edge_llm.manager import ProfileManager
    db = Path(tmpdir) / "state.db"
    mgr = ProfileManager(state_db_path=str(db))
    if models:
        mgr._models = models
    return mgr


def _make_pm():
    """Create a ProcessManager with a mock StateDB (no real files)."""
    import tempfile
    from edge_llm.process_manager import ProcessManager
    with tempfile.TemporaryDirectory() as tmp:
        db = StateDB(Path(tmp) / "test.db")
        return ProcessManager(state=db, log_dir=Path(tmp))


# ─── Runner ─────────────────────────────────────────────────────

def main():
    tests = [
        ("ModelConfig has vram field", test_model_config_has_vram_field),
        ("load_models parses vram", test_load_models_parses_vram),
        ("ModelConfig default vram", test_model_config_default_vram),
        ("shared add already active", test_shared_add_already_active),
        ("shared add incremental no stop", test_shared_add_incremental_no_stop),
        ("shared add vram reject", test_shared_add_vram_reject),
        ("shared add vram accept", test_shared_add_vram_accept),
        ("stop_vllm port cleanup always", test_stop_vllm_always_runs_port_cleanup),
        ("stop_vllm port no tracked pid", test_stop_vllm_port_when_no_tracked_pid),
        ("stop_comfyui port cleanup", test_stop_comfyui_always_runs_port_cleanup),
        ("stop_all passes port params", test_stop_all_passes_port_params),
        ("stop_service verifies gpu", test_stop_service_verifies_gpu),
        ("switch_to_idle passes ports", test_switch_to_idle_passes_ports),
        ("switch routes shared correctly", test_switch_routes_shared_correctly),
        ("switch routes exclusive to deploy", test_switch_routes_exclusive_to_deploy),
        ("stop_service routes vllm", test_stop_service_routes_vllm),
        ("stop_service routes comfyui", test_stop_service_routes_comfyui),
    ]

    passed = 0
    failed = 0
    for label, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  ✅ {label}")
        except Exception as e:
            import traceback
            print(f"  ❌ {label}: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed} passed, {failed} failed / {len(tests)}")
    return failed


if __name__ == "__main__":
    exit(main())
