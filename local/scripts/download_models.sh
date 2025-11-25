#!/bin/bash
# Download all required models from HuggingFace
MODELS_DIR="$(cd "$(dirname "$0")/../../local/models" && pwd)"
mkdir -p "$MODELS_DIR"

echo "Downloading models to: $MODELS_DIR"
echo ""

# Gemma 4 E2B - LLM (3.1GB)
echo "[1/5] Gemma 4 E2B Q4_K_M (LLM, 3.1GB)..."
curl -L -C - "https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF/resolve/main/gemma-4-E2B-it-Q4_K_M.gguf" -o "$MODELS_DIR/gemma-4-E2B-it-Q4_K_M.gguf"

# Gemma 4 E2B - Vision projector (940MB)
echo "[2/5] Gemma 4 E2B mmproj (vision, 940MB)..."
curl -L -C - "https://huggingface.co/unsloth/gemma-4-E2B-it-GGUF/resolve/main/mmproj-F16.gguf" -o "$MODELS_DIR/gemma4-mmproj-F16.gguf"

# Whisper tiny.en - ASR (75MB)
echo "[3/5] Whisper tiny.en (ASR, 75MB)..."
curl -L -C - "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.en.bin" -o "$MODELS_DIR/ggml-tiny.en.bin"

# Piper TTS voice (61MB)
echo "[4/5] Piper amy-low voice (TTS, 61MB)..."
curl -L -C - "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx" -o "$MODELS_DIR/en_US-amy-low.onnx"

echo "[5/5] Piper voice config..."
curl -L -C - "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/low/en_US-amy-low.onnx.json" -o "$MODELS_DIR/en_US-amy-low.onnx.json"

echo ""
echo "Done. Total:"
du -sh "$MODELS_DIR"
