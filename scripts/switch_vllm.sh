#!/usr/bin/env bash
# ============================================================
# switch_vllm.sh — Thin wrapper around edge-llm (v4.0)
#
# Maps old profile names to new model names.
#
# Usage: switch_vllm.sh <qw36|qw35|gemma> [context_len]
#   switch_vllm.sh stop
#   switch_vllm.sh status
# ============================================================

set -euo pipefail

# Map short names to model names (models.d/)
declare -A MODEL_MAP=(
    [qw36]="qwen36-27b"
    [qw35]="qwen35-9b"
    [gemma]="gemma4-26b"
)

case "${1:-}" in

    stop)
        exec edge-llm switch idle
        ;;

    status)
        exec edge-llm status
        ;;

    qw36|qw35|gemma)
        MODEL="${MODEL_MAP[$1]}"
        shift || true

        # Optional: context_len override (ignored by edge-llm, but logged)
        if [ -n "${1:-}" ]; then
            echo "[switch_vllm.sh] Note: context_len override ($1) ignored — edit models.d/ YAML"
        fi

        exec edge-llm switch "$MODEL"
        ;;

    *)
        echo "Usage: $0 <qw36|qw35|gemma|stop|status> [context_len]"
        echo ""
        echo "  Preferred: edge-llm switch <model_name>"
        echo "  Models:    edge-llm models"
        exit 1
        ;;
esac
