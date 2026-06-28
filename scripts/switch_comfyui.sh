#!/usr/bin/env bash
# ============================================================
# switch_comfyui.sh — Thin wrapper around edge-llm (v4.0)
#
# Usage: switch_comfyui.sh <start|stop|status>
# ============================================================

set -euo pipefail

case "${1:-}" in

    start)
        # If a shared vLLM is running, just add ComfyUI
        exec edge-llm switch comfyui
        ;;

    stop)
        exec edge-llm stop comfyui
        ;;

    status)
        exec edge-llm status
        ;;

    *)
        echo "Usage: $0 <start|stop|status>"
        echo ""
        echo "  Preferred: edge-llm switch comfyui"
        echo "             edge-llm stop comfyui"
        exit 1
        ;;
esac
