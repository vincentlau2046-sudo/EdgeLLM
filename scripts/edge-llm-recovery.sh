#!/usr/bin/env bash
# ============================================================
# edge-llm-recovery.sh — Emergency GPU Reset
# Usage: ~/edge_llm/scripts/edge-llm-recovery.sh [--full]
# --full: attempts nvidia-smi --gpu-reset if normal recovery fails
# ============================================================

set -euo pipefail

FULL="${1:-}"

echo "=== EdgeLLM Emergency Recovery ==="
echo "[$(date '+%H:%M:%S')] Starting recovery..."

# Step 1: Stop via known scripts
echo "[1/6] Stopping via switch scripts..."
~/edge_llm/scripts/switch_vllm.sh stop 2>/dev/null || true
~/edge_llm/scripts/switch_comfyui.sh stop 2>/dev/null || true
sleep 2

# Step 2: SIGKILL all vLLM processes
echo "[2/6] SIGKILL all vLLM processes..."
pkill -9 -f "vllm serve" 2>/dev/null || true
pkill -9 -f "vllm.*8000" 2>/dev/null || true
pkill -9 -f "vllm.*8001" 2>/dev/null || true
pkill -9 -f "vllm.*8002" 2>/dev/null || true
pkill -9 -f "python.*vllm" 2>/dev/null || true
sleep 2

# Step 3: Kill ComfyUI processes
echo "[3/6] Killing ComfyUI processes..."
pkill -9 -f "python main.py" 2>/dev/null || true
pkill -9 -f "ComfyUI" 2>/dev/null || true
sleep 2

# Step 4: Check GPU
echo "[4/6] Checking GPU memory..."
USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 || echo "unknown")
echo "  GPU used: ${USED} MB"

PARTIAL=false
if [ "$USED" != "unknown" ] && [ "$USED" -gt 2048 ] 2>/dev/null; then
    PARTIAL=true
    if [ "$FULL" = "--full" ]; then
        echo "[5/6] Attempting nvidia-smi --gpu-reset..."
        nvidia-smi --gpu-reset 2>/dev/null || echo "  GPU reset failed (may need reboot)"
        sleep 5
        USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 || echo "unknown")
        if [ "$USED" != "unknown" ] && [ "$USED" -lt 2048 ] 2>/dev/null; then
            PARTIAL=false
        fi
    fi
fi

# Step 5: Clean lock and re-init state (NEVER rm -rf state.db)
echo "[5/6] Cleaning lock and state..."
rm -f /tmp/edge_llm_gpu.lock
mkdir -p ~/.edge_llm

# Safely re-init state.db (creates tables if missing, keeps existing data)
python3 << 'PYEOF'
import sqlite3, pathlib
db = pathlib.Path.home() / '.edge_llm' / 'state.db'
db.parent.mkdir(exist_ok=True)
c = sqlite3.connect(str(db))
c.execute('PRAGMA journal_mode=WAL')
c.execute('CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)')
c.execute('''CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    from_profile TEXT, to_profile TEXT, duration REAL
)''')
c.execute("INSERT OR REPLACE INTO state VALUES ('current_profile', 'idle')")
c.commit()
c.close()
PYEOF

# Step 6: Verify
echo "[6/6] Final status..."
USED=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null | head -1 || echo "unknown")
echo "  GPU used: ${USED} MB"

if [ "$PARTIAL" = true ]; then
    echo ""
    echo "⚠️ Partial recovery — GPU still busy (${USED} MB)"
    echo "   State DB reset to idle. Orphan CUDA context may need reboot."
    echo ""
    echo "   You can still try: edge-llm switch <profile>"
    echo "   If GPU remains stuck, reboot the machine."
else
    echo ""
    echo "✅ Full recovery — GPU is free"
fi

echo ""
echo "Available commands:"
echo "  edge-llm switch qw36_full    # Start Qwen3.6"
echo "  edge-llm reconcile           # Fix DB vs reality"
echo "  ~/edge_llm/scripts/switch_vllm.sh qw36  # Bash fallback"
