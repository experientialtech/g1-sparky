#!/bin/bash
# G1 Rocky - party-animal persona with voice + vision (Gemini Live)
# Usage: ./online/start_rocky.sh

ONLINE_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$ONLINE_DIR/.env" ] || ! grep -qE '^GEMINI_API_KEY=.+' "$ONLINE_DIR/.env"; then
    echo "ERROR: No Gemini API key. See online/README.md (\"Add the API key\")."
    exit 1
fi

echo "=========================================="
echo "  G1 ROCKY — voice + vision"
echo "=========================================="

export COMPANY_PROFILE=ROCKY_VISION
exec python3 "$ONLINE_DIR/g1_sparky_vision.py"
