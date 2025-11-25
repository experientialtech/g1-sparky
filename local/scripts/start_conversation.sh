#!/bin/bash
# G1 Sparky - Local Conversation Pipeline
# Self-contained: whisper (CPU) + llama.cpp (GPU) + piper (CPU)
# Zero Docker. Zero cloud.

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LOCAL_DIR="$REPO_ROOT/local"
MODELS="$LOCAL_DIR/models"

echo "=========================================="
echo "  G1 SPARKY — Local Conversation"
echo "  Root: $REPO_ROOT"
echo "=========================================="

killall -9 whisper-server llama-server parecord 2>/dev/null
sleep 1

echo ""
echo "[1/2] Starting llama-server (Gemma 4 E2B, full GPU)..."
llama-server \
    --model "$MODELS/gemma-4-E2B-it-Q4_K_M.gguf" \
    --mmproj "$MODELS/gemma4-mmproj-F16.gguf" \
    --host 0.0.0.0 --port 8860 \
    --n-gpu-layers 99 \
    --ctx-size 4096 \
    --threads 12 \
    --parallel 1 \
    --no-mmap \
    --jinja \
    --reasoning-budget 0 \
    > /tmp/llama_server.log 2>&1 &
echo "  PID: $!"
echo "  Waiting..."
for i in $(seq 1 60); do
    curl -s http://localhost:8860/health > /dev/null 2>&1 && echo "  llama: READY" && break
    if [ $((i % 10)) -eq 0 ]; then echo "  llama: loading... (${i}s)"; fi
    sleep 1
done

echo ""
echo "[2/2] Starting whisper-server (ASR, CPU)..."
whisper-server \
    --model "$MODELS/ggml-tiny.en.bin" \
    --host 0.0.0.0 --port 8080 \
    > /tmp/whisper_server.log 2>&1 &
echo "  PID: $!"
echo "  Waiting..."
for i in $(seq 1 30); do
    curl -s http://localhost:8080/ > /dev/null 2>&1 && echo "  whisper: READY" && break
    sleep 1
done

echo ""
echo "  piper: /opt/piper/piper (CPU, instant)"
echo ""
echo "All services ready. Starting conversation..."
echo ""

python3 "$LOCAL_DIR/conversation_pipeline.py"
