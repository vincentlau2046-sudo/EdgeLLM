#!/usr/bin/env python3
"""
edge-llm — CLI for local LLM profile switching.

Usage:
  edge-llm status              Show current profile and GPU state
  edge-llm list                 List all profiles
  edge-llm switch <profile>    Switch to a profile
  edge-llm history            Show switch history
  edge-llm reset [profile]    Force reset (default: idle)
  edge-llm reconcile        Check DB vs actual state and fix
"""

import sys
import json
import logging
import datetime
from pathlib import Path

# Bootstrap logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
log = logging.getLogger("edge_llm.cli")

# Add parent to path for import
sys.path.insert(0, str(Path(__file__).parent.parent))
from edge_llm.profile_manager import ProfileManager, ProfileState


def cmd_status():
    mgr = ProfileManager()
    s = mgr.status()
    state_label = {
        ProfileState.HEALTHY: "🟢 healthy",
        ProfileState.SWITCHING: "🔄 switching",
        ProfileState.IDLE: "⚪ idle",
        ProfileState.ERROR: "🔴 error",
    }.get(s.get("state", ""), s.get("state", "?"))
    print(f"Profile : {s['profile']} ({s['description']})")
    print(f"State   : {state_label}")
    print(f"vLLM    : {s['vllm']}  (PID: {s.get('vllm_pid', '-') or '-'})")
    print(f"ComfyUI : {s['comfyui']}")
    print(f"GPU     : {s['gpu_used_mb']}/{s['gpu_total_mb']} MiB used")


def cmd_list():
    mgr = ProfileManager()
    print("\nAvailable Profiles:")
    print(f"{'name':<20} {'description':<35} {'gpu':<10} {'cost':<6} {'curr'}")
    print("-" * 80)
    for p in mgr.list_profiles():
        marker = " ← active" if p["current"] else ""
        vllm = "vllm" if p["has_vllm"] else ""
        comfy = "+comfyui" if p["has_comfyui"] else ""
        owner = (vllm + comfy).strip("+") or p["gpu_owner"]
        print(f"{p['name']:<20} {p['description']:<35} {owner:<10} {p['switch_cost_sec']}s{marker}")


def cmd_switch(args):
    if not args:
        print("Usage: edge-llm switch <profile_name>")
        sys.exit(1)
    target = args[0]
    mgr = ProfileManager()

    # Validate target exists
    if target not in mgr._profiles:
        print(f"❌ Unknown profile: {target}")
        print("Available profiles:")
        for p in mgr.list_profiles():
            print(f"  - {p['name']}")
        sys.exit(1)

    print(f"Switching to '{target}'... (est. {mgr._profiles[target].switch_cost_sec}s)")
    result = mgr.switch(target)

    if result["status"] == "already_active":
        print(f"Already on '{target}'")
    elif result["status"] == "switched":
        print(f"✅ Switched to '{target}' in {result['elapsed_sec']}s")
        for svc, res in result.get("results", {}).items():
            status = "✅" if res.get("status") in ("healthy", "started") else "❌"
            detail = res.get("message", res.get("log", ""))
            print(f"  {status} {svc}: {res.get('status', '?')}" + (f" — {detail}" if detail else ""))
    else:
        print(f"❌ {result['message']}")
        if result.get("results"):
            print("  Details:", json.dumps(result["results"], indent=2))
        sys.exit(1)


def cmd_history(args):
    mgr = ProfileManager()
    history = mgr.state.get_history(limit=30)
    if not history:
        print("No switch history.")
        return
    print(f"\nSwitch History (last {len(history)} entries):")
    print(f"{'#':<4} {'from':<15} {'to':<15} {'cost':<8} {'status':<8} {'time'}")
    print("-" * 80)
    for i, h in enumerate(history):
        ts = datetime.datetime.fromisoformat(h["timestamp"])
        dur = f"{h['duration']:.1f}s" if h["duration"] else "-"
        status = h.get("status", "?")
        print(f"{i+1:<4} {h['from']:<15} {h['to']:<15} {dur:<8} {status:<8} {ts.strftime('%Y-%m-%d %H:%M:%S')}")


def cmd_reset(args):
    """Force reset: kill everything, verify GPU, clean state."""
    target = args[0] if args else "idle"
    mgr = ProfileManager()
    print(f"Force resetting to '{target}'...")

    result = mgr.force_reset(target)

    if result["status"] == "reset":
        print(f"✅ Reset to '{target}'")
        if not result["gpu_free"]:
            print(f"⚠️ WARNING: GPU still has {gpu_used_mb()} MB used — orphan CUDA context likely")
            print(f"   May need 'nvidia-smi --gpu-reset' or reboot")
    else:
        print(f"❌ Reset failed")
        sys.exit(1)


def cmd_reconcile(args):
    """Check DB state vs actual running processes and fix."""
    mgr = ProfileManager()
    result = mgr.reconcile()

    print("State Reconciliation:")
    print(f"  DB profile : {result['db_profile']} (state: {result.get('db_state', '?')})")
    print(f"  Actual     : {result['actual_profile']}")
    print(f"  ComfyUI    : {'✅' if result['comfyui_alive'] else '❌'}")
    if result.get("actual_states"):
        print(f"  Port scan  :")
        for name, state in result["actual_states"].items():
            print(f"    {name}: {state}")
    if result["actions"]:
        print("\n  Actions taken:")
        for a in result["actions"]:
            print(f"    • {a}")
    else:
        print("  ✓ State is consistent")


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    if cmd == "status":
        cmd_status()
    elif cmd == "list":
        cmd_list()
    elif cmd == "switch":
        cmd_switch(rest)
    elif cmd == "history":
        cmd_history(rest)
    elif cmd == "reset":
        cmd_reset(rest)
    elif cmd == "reconcile":
        cmd_reconcile(rest)
    else:
        print(f"Unknown command: {cmd}")
        print("Available: status, list, switch, history, reset, reconcile")
        sys.exit(1)


if __name__ == "__main__":
    main()
