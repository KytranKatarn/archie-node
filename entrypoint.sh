#!/bin/bash
set -e

echo "╔══════════════════════════════════════════╗"
echo "║   A.R.C.H.I.E. Fleet Node               ║"
echo "╚══════════════════════════════════════════╝"

# Validate required env vars
if [ -z "$HUB_URL" ]; then
    echo "[FAIL] HUB_URL is required"
    echo "  Usage: docker run -e HUB_URL=http://192.168.1.200:3000 -e TOKEN=abc123 archie-node"
    exit 1
fi

echo "[OK] Hub: $HUB_URL"
echo "[OK] Ollama: ${OLLAMA_HOST:-not configured}"
echo "[..] Starting node agent..."

exec python3 -u node_agent.py
