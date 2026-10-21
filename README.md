# G1 Sparky

Conversation software for a Unitree G1 humanoid robot. The robot listens through its webcam mic, talks through
USB speakers, and can see through its webcam.

There are two independent modes:

| Mode | Folder | How it works |
|---|---|---|
| **Online** (main) | [`online/`](online/README.md) | Google Gemini Live handles speech, conversation, voice, and vision in one streaming session. Needs internet and a Gemini API key. |
| **Local** | `local/` | Fully offline pipeline on the robot's GPU: whisper.cpp (speech to text) → llama.cpp (LLM) → Piper (text to speech). Start it with `local/scripts/start_conversation.sh`. |

## Quick start (online)

```bash
cp online/.env.example online/.env        # then set GEMINI_API_KEY in online/.env
pip install -r online/requirements.txt
python3 online/check_gemini.py            # should print "OK: Gemini Live is working"
./online/start_rocky.sh                   # Rocky, voice + vision
```

Personas, settings, and troubleshooting: [online/README.md](online/README.md).
For agents setting up the key: [AGENTS.md](AGENTS.md).
