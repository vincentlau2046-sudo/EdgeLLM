#!/usr/bin/env bash
# ============================================================
# ComfyUI Switcher — start/stop/status
# Usage: switch_comfyui.sh <start|stop|status>
# ============================================================

set -euo pipefail

PORT=8188
LOG_DIR="/home/vince/ComfyUI_logs"
PID_FILE="$LOG_DIR/comfyui.pid"

mkdir -p "$LOG_DIR"

case "${1:-}" in

    start)
        # GPU mutual exclusion: stop vLLM if running
        if pgrep -f "vllm serve" > /dev/null 2>&1; then
            echo "[$(date '+%H:%M:%S')] vLLM is running — stopping first"
            ~/models/switch_vllm.sh stop
        fi

        # Stop existing ComfyUI if running
        pkill -f "python main.py.*comfy" 2>/dev/null || true
        sleep 2

        GPU_MEM=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
        echo "[$(date '+%H:%M:%S')] Starting ComfyUI (GPU used: ${GPU_MEM} MB)"

        # Check prerequisites
        source ~/miniconda3/bin/activate comfyui
        if ! pip show comfyui_manager >/dev/null 2>&1; then
            echo "  ❌ ComfyUI-Manager not installed. Fix: pip install -r ~/ComfyUI/manager_requirements.txt"
            exit 1
        fi

        # Launch
        export HF_ENDPOINT=https://hf-mirror.com
        export LD_LIBRARY_PATH=$HOME/miniconda3/envs/comfyui/lib/python3.12/site-packages/nvidia/cuda_runtime/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}

        nohup python ~/ComfyUI/main.py \
            --listen 0.0.0.0 \
            --port $PORT \
            --cache-none \
            --enable-manager \
            > "$LOG_DIR/comfyui_start.log" 2>&1 &
        echo $! > "$PID_FILE"
        echo "  PID: $(cat "$PID_FILE")"

        # Health check
        WAITED=0
        MAX_WAIT=120
        while [ $WAITED -lt $MAX_WAIT ]; do
            if curl -s --connect-timeout 2 "http://localhost:$PORT" > /dev/null 2>&1; then
                echo "  ✅ ComfyUI ready on :$PORT ($(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1) MB VRAM)"
                exit 0
            fi
            sleep 5
            WAITED=$((WAITED + 5))
            if [ $((WAITED % 30)) -eq 0 ]; then
                echo "  ... still loading (${WAITED}s)"
            fi
        done
        echo "  ❌ Timeout after ${MAX_WAIT}s. Check: $LOG_DIR/comfyui_start.log"
        exit 1
        ;;

    stop)
        echo "[$(date '+%H:%M:%S')] Stopping ComfyUI"
        if [ -f "$PID_FILE" ]; then
            kill "$(cat "$PID_FILE")" 2>/dev/null || true
            rm -f "$PID_FILE"
        fi
        pkill -f "python main.py.*comfy" 2>/dev/null || true
        sleep 2
        echo "  Done. GPU free: $(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -1) MB"
        exit 0
        ;;

    status)
        echo "=== ComfyUI Status ==="
        if ss -tlnp | grep -q ":${PORT} "; then
            if curl -s --connect-timeout 2 "http://localhost:$PORT" > /dev/null 2>&1; then
                echo "  :$PORT → running ✅ ($(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1) MB VRAM)"
            else
                echo "  :$PORT → listening but not responding"
            fi
        else
            echo "  :$PORT → not running"
        fi
        exit 0
        ;;

    *)
        echo "Usage: $0 <start|stop|status>"
        exit 1
        ;;
esac
