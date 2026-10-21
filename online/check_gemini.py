#!/usr/bin/env python3
"""
Check that the Gemini API key in online/.env works with Gemini Live.
Opens a session, asks for a short spoken reply, and confirms audio comes back.
Exit code 0 = ready to run the robot, 1 = something is wrong (reason printed).
"""

import os, sys, asyncio, json
import websockets
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")
load_dotenv(ENV_PATH)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live")
VOICE = os.getenv("GEMINI_LIVE_VOICE", "Fenrir")
GEMINI_WS_URL = ("wss://generativelanguage.googleapis.com/ws/"
                 "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


async def check():
    async with websockets.connect(GEMINI_WS_URL, additional_headers={"x-goog-api-key": GEMINI_API_KEY},
                                  close_timeout=5, max_size=None) as ws:
        await ws.send(json.dumps({"setup": {
            "model": f"models/{MODEL}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}},
            },
        }}))
        reply = json.loads(await asyncio.wait_for(ws.recv(), 15))
        if "setupComplete" not in reply:
            fail(f"session setup rejected: {reply}")
        await ws.send(json.dumps({"realtimeInput": {"text": "Say: ready."}}))
        audio_parts = 0
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 30))
            sc = msg.get("serverContent", {})
            audio_parts += sum(1 for p in sc.get("modelTurn", {}).get("parts", []) if "inlineData" in p)
            if sc.get("turnComplete"):
                break
        if not audio_parts:
            fail("connected, but no audio came back")


def main():
    if not os.path.exists(ENV_PATH):
        fail(f"{ENV_PATH} not found. Run: cp online/.env.example online/.env  then set GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        fail(f"GEMINI_API_KEY is empty in {ENV_PATH}")
    try:
        asyncio.run(check())
    except websockets.exceptions.InvalidStatus as e:
        fail(f"Google rejected the connection (HTTP {e.response.status_code}). The API key is probably wrong or disabled.")
    except websockets.exceptions.ConnectionClosedError as e:
        fail(f"Google closed the session: {(e.reason or str(e)).rstrip('.')}. Check the API key and GEMINI_LIVE_MODEL.")
    except (OSError, asyncio.TimeoutError) as e:
        fail(f"could not reach Gemini ({type(e).__name__}: {e}). Check the internet connection.")
    print(f"OK: Gemini Live is working (model {MODEL}, voice {VOICE}).")


if __name__ == "__main__":
    main()
