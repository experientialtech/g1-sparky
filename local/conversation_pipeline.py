#!/usr/bin/env python3
"""
Local conversational pipeline for Jetson Thor.
Mic → whisper.cpp → Qwen3.5-9B → Piper TTS → speaker
All CPU except LLM (15 GPU layers). Zero Docker needed.
"""
import asyncio
import json
import os
import struct
import subprocess
import sys
import tempfile
import time

import re
import aiohttp

WHISPER_URL = "http://localhost:8080/inference"
LLM_URL = "http://localhost:8860/v1/chat/completions"
PIPER_CMD = "/opt/piper/piper"
PIPER_MODEL = "/home/samwise/models/en_US-amy-low.onnx"
SAMPLE_RATE = 16000
RECORD_SECONDS = 3
SILENCE_THRESHOLD = 300

SYSTEM_PROMPT = """You are Samwise, a sarcastic robot with camera eyes.
RESPOND DIRECTLY. DO NOT THINK OR REASON. Just say your response.
Keep it to 1-2 sentences. Be funny and sarcastic. You can see through your camera.
No markdown, no lists, no analysis — just speak."""

CAMERA_DEVICE = os.environ.get("CAMERA_DEVICE", "/dev/video6")

conversation_history = []


def cprint(role, text, timing=""):
    if role == "you":
        print(f"\033[96m  You:\033[0m {text}  \033[90m{timing}\033[0m")
    elif role == "sam":
        print(f"\033[93m  Sam:\033[0m {text}  \033[90m{timing}\033[0m")
    elif role == "sys":
        print(f"\033[90m  [{text}]\033[0m")
    elif role == "err":
        print(f"\033[91m  ERR: {text}\033[0m")
    sys.stdout.flush()


def record_audio():
    """Record until silence detected (VAD) or max 8 seconds."""
    raw_path = tempfile.mktemp(suffix=".raw")
    wav_path = tempfile.mktemp(suffix=".wav")
    try:
        proc = subprocess.Popen(
            ["parecord", "--format=s16le", "--rate=16000", "--channels=1",
             "--device=default_mic_aec", raw_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Wait for speech to start (max 5s), then record until 0.8s of silence
        started = False
        silent_chunks = 0
        for tick in range(80):  # max 8 seconds (80 x 0.1s)
            time.sleep(0.1)
            try:
                sz = os.path.getsize(raw_path)
                if sz < 3200:
                    continue
                with open(raw_path, "rb") as rf:
                    rf.seek(max(0, sz - 3200))
                    chunk = rf.read(3200)
                n = len(chunk) // 2
                if n == 0:
                    continue
                samples = struct.unpack(f"<{n}h", chunk[:n*2])
                rms = (sum(s*s for s in samples) / n) ** 0.5
                if rms > SILENCE_THRESHOLD:
                    started = True
                    silent_chunks = 0
                elif started:
                    silent_chunks += 1
                    if silent_chunks >= 8:  # 0.8s of silence after speech
                        break
            except:
                continue
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try: proc.kill()
        except: pass

    try:
        with open(raw_path, "rb") as f:
            pcm = f.read()
        os.unlink(raw_path)
    except FileNotFoundError:
        return None

    if len(pcm) < 2000:
        return None

    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm[:n*2])
    rms = (sum(s*s for s in samples) / n) ** 0.5
    if rms < SILENCE_THRESHOLD:
        return None

    with open(wav_path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + len(pcm)))
        f.write(b"WAVEfmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE*2, 2, 16))
        f.write(b"data")
        f.write(struct.pack("<I", len(pcm)))
        f.write(pcm)
    return wav_path


async def transcribe(wav_path):
    t0 = time.time()
    try:
        async with aiohttp.ClientSession() as session:
            with open(wav_path, "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename="audio.wav", content_type="audio/wav")
                data.add_field("response_format", "json")
                data.add_field("temperature", "0.0")
                async with session.post(WHISPER_URL, data=data,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        text = result.get("text", "").strip()
                        for junk in ["[BLANK_AUDIO]", "(silence)", "[silence]", "you", ".", ""]:
                            if text.lower().strip() == junk.lower():
                                return "", 0
                        return text, time.time() - t0
                    else:
                        return "", 0
    except Exception as e:
        cprint("err", f"ASR: {e}")
        return "", 0


def capture_frame():
    """Grab a JPEG frame from the camera."""
    tmp = tempfile.mktemp(suffix=".jpg")
    try:
        subprocess.run(
            ["ffmpeg", "-f", "v4l2", "-i", CAMERA_DEVICE, "-frames:v", "1", "-update", "1", "-y", "-q:v", "10", tmp],
            capture_output=True, timeout=5, env={**os.environ, "FFREPORT": ""})
        if os.path.exists(tmp) and os.path.getsize(tmp) > 1000:
            import base64
            with open(tmp, "rb") as f:
                return base64.b64encode(f.read()).decode()
    except:
        pass
    finally:
        try: os.unlink(tmp)
        except: pass
    return None


async def think(user_text, include_vision=True):
    global conversation_history

    # Build user message with optional image
    if include_vision:
        frame_b64 = capture_frame()
        if frame_b64:
            user_msg = {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"}},
                {"type": "text", "text": user_text}
            ]}
        else:
            user_msg = {"role": "user", "content": user_text}
    else:
        user_msg = {"role": "user", "content": user_text}

    conversation_history.append({"role": "user", "content": user_text})
    if len(conversation_history) > 20:
        conversation_history = conversation_history[-16:]

    # Use text-only history but vision for current turn
    history_msgs = conversation_history[:-1]  # all except the one we just added
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history_msgs + [user_msg]
    t0 = time.time()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(LLM_URL, json={
                "model": "qwen", "messages": messages,
                "max_tokens": 200, "temperature": 0.7,
            }, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    raw = result["choices"][0]["message"]["content"].strip()
                    # Extract actual response from Gemma output
                    # 1. Split on channel token if present
                    if "<channel|>" in raw:
                        raw = raw.split("<channel|>")[-1].strip()
                    # 2. Clean special tokens
                    for tok in ["<|im_end|>", "<end_of_turn>", "<eos>", "<channel|>"]:
                        raw = raw.replace(tok, "").strip()
                    # 3. If still contains thinking, extract the actual response
                    thinking_markers = ["thinking process", "the user said", "the user asked",
                                       "i need to", "i should", "let me", "**analyze",
                                       "**identify", "**determine", "1.", "2.", "3."]
                    if any(m in raw.lower() for m in thinking_markers):
                        # Take last paragraph or sentence that doesn't look like thinking
                        paragraphs = [p.strip() for p in raw.split("\n\n") if p.strip()]
                        for p in reversed(paragraphs):
                            clean = re.sub(r'\*\*[^*]+\*\*:?\s*', '', p).strip()
                            clean = re.sub(r'^\d+\.\s*', '', clean).strip()
                            if (len(clean) > 3 and
                                not any(m in clean.lower() for m in thinking_markers)):
                                raw = clean
                                break
                    reply = raw
                    conversation_history.append({"role": "assistant", "content": reply})
                    return reply, time.time() - t0
                else:
                    return "", 0
    except asyncio.TimeoutError:
        return "Hmm, give me a second.", 0
    except Exception as e:
        cprint("err", f"LLM: {e}")
        return "", 0


def speak(text):
    """TTS via piper (CPU-only, instant)."""
    if not text:
        return 0
    t0 = time.time()
    tmp = tempfile.mktemp(suffix=".wav")
    try:
        proc = subprocess.run(
            f'echo "{text}" | {PIPER_CMD} --model {PIPER_MODEL} --output_file {tmp}',
            shell=True, capture_output=True, timeout=10,
            env={**os.environ, "LD_LIBRARY_PATH": "/opt/piper"})
        if os.path.exists(tmp):
            subprocess.run(
                ["paplay", "--device=default_output_aec", tmp],
                capture_output=True, timeout=30)
            return time.time() - t0
        return 0
    except Exception as e:
        cprint("err", f"TTS: {e}")
        return 0
    finally:
        try: os.unlink(tmp)
        except: pass


async def check_services():
    checks = [
        ("whisper", "http://localhost:8080/"),
        ("llama", "http://localhost:8860/health"),
    ]
    ok = True
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
        for name, url in checks:
            try:
                async with s.get(url) as r:
                    up = r.status == 200
            except: up = False
            status = "\033[92mOK\033[0m" if up else "\033[91mOFFLINE\033[0m"
            print(f"  {name:10s} {status}")
            if not up: ok = False

    # Check piper
    try:
        subprocess.run([PIPER_CMD, "--help"], capture_output=True, timeout=3)
        print(f"  {'piper':10s} \033[92mOK\033[0m")
    except:
        print(f"  {'piper':10s} \033[91mOFFLINE\033[0m")
        ok = False
    return ok


async def main():
    print()
    print("\033[1m╔══════════════════════════════════════╗\033[0m")
    print("\033[1m║   SAMWISE — Local Voice Chat         ║\033[0m")
    print("\033[1m╚══════════════════════════════════════╝\033[0m")
    print()
    print("  Services:")
    if not await check_services():
        print("\n  Start services first.")
        return
    print()
    print("  \033[92mAll systems go.\033[0m")
    print("  \033[90mCtrl+C to quit.\033[0m")
    print()
    print("─" * 42)

    greeting = "Hello! I'm Samwise. What's on your mind?"
    cprint("sam", greeting)
    speak(greeting)
    print()

    while True:
        try:
            print("  \033[90m● listening...\033[0m", end="\r", flush=True)
            wav = record_audio()
            if wav is None:
                continue

            print("  \033[90m● transcribing...\033[0m", end="\r", flush=True)
            text, asr_t = await transcribe(wav)
            try: os.unlink(wav)
            except: pass

            if not text or len(text.split()) < 2:
                continue

            print(" " * 42, end="\r")
            cprint("you", text, f"[{asr_t:.1f}s]")

            print("  \033[90m● thinking...\033[0m", end="\r", flush=True)
            reply, llm_t = await think(text)
            if not reply:
                cprint("err", "Empty LLM response")
                continue

            print(" " * 42, end="\r")
            cprint("sam", reply, f"[{llm_t:.1f}s]")

            tts_t = speak(reply)
            print()

        except KeyboardInterrupt:
            print("\n\n  \033[90mGoodbye!\033[0m\n")
            break
        except Exception as e:
            cprint("err", str(e))
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
