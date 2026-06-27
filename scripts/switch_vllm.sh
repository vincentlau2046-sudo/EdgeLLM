#!/usr/bin/env bash
# ============================================================
# vLLM Model Switcher — Qwen3.6 ↔ Qwen3.5-9B ↔ Gemma4-26B
# Usage: switch_vllm.sh <model> [context_len]
#   model: qw36 | qw35 | gemma
#   context_len: optional, defaults to model's sweet spot
#
# NOTE: This script works alongside edge-llm. For robust management
# with GPU lock, state tracking, and recovery, prefer:
#   edge-llm switch <profile> | edge-llm reset | edge-llm reconcile
# ============================================================

set -euo pipefail

MODEL="${1:-}"
CONTEXT_LEN="${2:-}"
PORT_QWEN=8000
PORT_QW35=8002
PORT_GEMMA=8001
LOG_DIR="/home/vince/models/vllm_logs"

mkdir -p "$LOG_DIR"

case "$MODEL" in
    qw36)
        CTX="${CONTEXT_LEN:-128000}"
        echo "[$(date '+%H:%M:%S')] Switching → Qwen3.6-27B (port $PORT_QWEN, ctx=$CTX)"
        # Kill other vLLM instances
        pkill -f "vllm.*$PORT_GEMMA" 2>/dev/null || true
        pkill -f "vllm.*$PORT_QW35" 2>/dev/null || true
        # Also kill by process name (not just port) for broader cleanup
        pkill -f "vllm serve.*$PORT_GEMMA" 2>/dev/null || true
        pkill -f "vllm serve.*$PORT_QW35" 2>/dev/null || true
        sleep 2

        # Verify GPU is free
        GPU_MEM=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
        echo "  GPU memory before: ${GPU_MEM} MB"

        source ~/miniconda3/bin/activate qw36-27b-vllm
        nohup env PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
            vllm serve /home/vince/models/Qwen3.6-27B-Text-NVFP4-MTP \
            --served-model-name vllm_qwen27b \
            --max-model-len "$CTX" \
            --kv-cache-dtype fp8 \
            --gpu-memory-utilization 0.90 \
            --speculative-config '{"method": "mtp", "num_speculative_tokens": 3}' \
            --max-num-batched-tokens 8192 \
            --max-num-seqs 4 \
            --language-model-only \
            --enable-prefix-caching \
            --compilation-config '{"cudagraph_mode": "PIECEWISE", "cudagraph_capture_sizes": [1, 2, 4, 8]}' \
            --enable-chunked-prefill \
            --async-scheduling \
            --enable-auto-tool-choice \
            --tool-call-parser qwen3_coder \
            --reasoning-parser qwen3 \
            --trust-remote-code \
            --port "$PORT_QWEN" \
            --host 0.0.0.0 \
            > "$LOG_DIR/qw36-27b.log" 2>&1 &
        echo $! > "$LOG_DIR/qw36-27b.pid"
        ;;

    qw35)
        CTX="${CONTEXT_LEN:-128000}"
        echo "[$(date '+%H:%M:%S')] Switching → Qwen3.5-9B-GPTQ-4bit (port $PORT_QW35, ctx=$CTX)"
        # Kill qw36/gemma instances if running
        pkill -f "vllm.*$PORT_QWEN" 2>/dev/null || true
        pkill -f "vllm.*$PORT_GEMMA" 2>/dev/null || true
        pkill -f "vllm serve.*$PORT_QWEN" 2>/dev/null || true
        pkill -f "vllm serve.*$PORT_GEMMA" 2>/dev/null || true
        sleep 2

        GPU_MEM=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
        echo "  GPU memory before: ${GPU_MEM} MB"

        source ~/miniconda3/bin/activate qw35-9b-vllm
        nohup env PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
            vllm serve /home/vince/models/Qwen3.5-9B-GPTQ-4bit/Qwen3.5-9B-GPTQ-4bit \
            --served-model-name vllm_qw35_gptq \
            --max-model-len 128000 \
            --kv-cache-dtype fp8 \
            --gpu-memory-utilization 0.4 \
            --max-num-batched-tokens 8192 \
            --max-num-seqs 4 \
            --enable-prefix-caching \
            --compilation-config '{"cudagraph_mode": "PIECEWISE", "cudagraph_capture_sizes": [1, 2, 4, 8]}' \
            --enable-chunked-prefill \
            --async-scheduling \
            --quantization gptq_marlin \
            --enable-auto-tool-choice \
            --tool-call-parser qwen3_coder \
            --reasoning-parser qwen3 \
            --trust-remote-code \
            --port "$PORT_QW35" \
            --host 0.0.0.0 \
            > "$LOG_DIR/qw35-9b.log" 2>&1 &
        echo $! > "$LOG_DIR/qw35-9b.pid"
        ;;

    gemma)
        CTX="${CONTEXT_LEN:-128000}"
        echo "[$(date '+%H:%M:%S')] Switching → Gemma4-26B-A4B-NVFP4 (port $PORT_GEMMA, ctx=$CTX)"
        # Kill other vLLM instances
        pkill -f "vllm.*$PORT_QWEN" 2>/dev/null || true
        pkill -f "vllm.*$PORT_QW35" 2>/dev/null || true
        pkill -f "vllm serve.*$PORT_QWEN" 2>/dev/null || true
        pkill -f "vllm serve.*$PORT_QW35" 2>/dev/null || true
        sleep 2

        # Verify GPU is free
        GPU_MEM=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1)
        echo "  GPU memory before: ${GPU_MEM} MB"

        source ~/miniconda3/bin/activate gm4-26b-vllm
        nohup vllm serve /home/vince/models/gemma-4-26b-a4b-nvfp4 \
            --served-model-name vllm_gemma26b_nvfp4 \
            --max-model-len "$CTX" \
            --kv-cache-dtype fp8 \
            --gpu-memory-utilization 0.88 \
            --moe-backend marlin \
            --enable-chunked-prefill \
            --async-scheduling \
            --max-num-batched-tokens 8192 \
            --max-num-seqs 4 \
            --generation-config vllm \
            --enable-auto-tool-choice \
            --tool-call-parser gemma4 \
            --compilation-config '{"cudagraph_mode": "PIECEWISE", "cudagraph_capture_sizes": [1, 2, 4, 8]}' \
            --trust-remote-code \
            --port "$PORT_GEMMA" \
            --host 0.0.0.0 \
            > "$LOG_DIR/gm4-26b.log" 2>&1 &
        echo $! > "$LOG_DIR/gm4-26b.pid"
        ;;

    stop)
        echo "[$(date '+%H:%M:%S')] Stopping all vllm instances"
        # SIGTERM first
        pkill -f "vllm serve" 2>/dev/null || true
        sleep 2
        # SIGKILL any survivors
        pkill -9 -f "vllm serve" 2>/dev/null || true
        pkill -9 -f "vllm.*8000" 2>/dev/null || true
        pkill -9 -f "vllm.*8001" 2>/dev/null || true
        pkill -9 -f "vllm.*8002" 2>/dev/null || true
        sleep 1
        rm -f "$LOG_DIR"/*.pid
        # Clean edge-llm lock file
        rm -f /tmp/edge_llm_gpu.lock
        echo "  Done. GPU free: $(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits | head -1) MB"
        exit 0
        ;;

    status)
        echo "=== vLLM Status ==="
        for port in $PORT_QWEN $PORT_QW35 $PORT_GEMMA; do
            if ss -tlnp | grep -q ":$port "; then
                RESP=$(curl -s --connect-timeout 2 "http://localhost:$port/v1/models" 2>/dev/null || echo "{}")
                MODEL_NAME=$(echo "$RESP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data',[{}])[0].get('id','unknown'))" 2>/dev/null || echo "unknown")
                echo "  :$port → $MODEL_NAME ✅"
            else
                echo "  :$port → not running"
            fi
        done
        echo "  GPU: $(nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader,nounits | head -1) MB used"
        exit 0
        ;;

    *)
        echo "Usage: $0 <qw36|qw35|gemma|stop|status> [context_len]"
        echo ""
        echo "  qw36   Start Qwen3.6-27B on :8000"
        echo "  qw35   Start Qwen3.5-9B GPTQ-4bit on :8002"
        echo "  gemma  Start Gemma4-26B on :8001"
        echo "  stop   Stop all vllm instances"
        echo "  status Show running instances"
        echo ""
        echo "For robust management (GPU lock, state tracking, recovery):"
        echo "  edge-llm status | edge-llm switch <profile> | edge-llm reset"
        exit 1
        ;;
esac

echo "  Log: $LOG_DIR/${MODEL}.log"
echo "  Waiting for server to be ready..."
WAITED=0
MAX_WAIT=300  # 5 min max

while [ $WAITED -lt $MAX_WAIT ]; do
    if [ "$MODEL" = "qw36" ]; then PORT=$PORT_QWEN
    elif [ "$MODEL" = "qw35" ]; then PORT=$PORT_QW35
    else PORT=$PORT_GEMMA
    fi
    if curl -s --connect-timeout 2 "http://localhost:$PORT/v1/models" > /dev/null 2>&1; then
        echo "  ✅ Server ready on :$PORT ($(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1) MB VRAM)"
        exit 0
    fi
    sleep 5
    WAITED=$((WAITED + 5))
    if [ $((WAITED % 30)) -eq 0 ]; then
        echo "  ... still loading (${WAITED}s, $(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | head -1) MB VRAM)"
    fi
done

echo "  ❌ Timeout after ${MAX_WAIT}s. Check log: $LOG_DIR/${MODEL}.log"
exit 1
