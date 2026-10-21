#!/usr/bin/env python3
"""
G1 Sparky Vision - PulseAudio voice + OpenCV webcam
- Mic: parec from webcam PA source at 16kHz
- Speakers: pacat to each USB speaker sink at 24kHz
- Camera: OpenCV VideoCapture (USB webcam)
- Streams camera frames into the live session as JPEG video input
- Gemini Live API
"""

import os, sys, asyncio, json, base64, time, subprocess
import websockets
import queue
import cv2
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prompts

load_dotenv()

# --- Config ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live")
VOICE = os.getenv("GEMINI_LIVE_VOICE", "Fenrir")
TEMPERATURE = float(os.getenv("GEMINI_LIVE_TEMPERATURE", "1.2"))      # higher = wilder replies
SILENCE_MS = int(os.getenv("GEMINI_LIVE_SILENCE_MS", "500"))          # pause before the robot replies
SYSTEM_PROMPT_NAME = os.getenv("COMPANY_PROFILE", "SPARKY_VISION")

GEMINI_WS_URL = ("wss://generativelanguage.googleapis.com/ws/"
                 "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")

PA_MIC_SOURCE = "alsa_input.usb-HCVsight_FHD_webcamera-02.mono-fallback"

MIC_RATE = 16000      # Gemini Live input: 16kHz PCM16
SPEAKER_RATE = 24000  # Gemini Live output: 24kHz PCM16
S16LE_BYTES = 2
MIC_CHUNK_BYTES = MIC_RATE * S16LE_BYTES // 10          # 100ms = 3200 bytes
SPEAKER_CHUNK_BYTES = SPEAKER_RATE * S16LE_BYTES // 20  # 50ms = 2400 bytes
PREBUFFER_BYTES = SPEAKER_RATE * S16LE_BYTES // 4       # 250ms = 12000 bytes

# --- Vision Config ---
CAMERA_DEVICE = "/dev/video0"
IMAGE_SEND_INTERVAL = float(os.getenv("IMAGE_SEND_INTERVAL", "1.0"))  # Gemini accepts <= 1 frame/sec
JPEG_QUALITY = 75             # JPEG compression quality
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480


def find_usb_sinks():
    """Find all USB audio PulseAudio sinks."""
    try:
        out = subprocess.check_output(["pactl", "list", "sinks", "short"], text=True)
    except Exception:
        return []
    sinks = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and "usb" in parts[1].lower():
            sinks.append(parts[1])
    return sinks


def encode_frame_to_jpeg_b64(frame: np.ndarray) -> str:
    """Encode an OpenCV BGR frame to base64 JPEG."""
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        return None
    return base64.b64encode(buf.tobytes()).decode("ascii")


def build_setup(prompt_text, resume_handle):
    """Gemini Live setup message. Must be the first message on the socket."""
    setup = {
        "model": f"models/{MODEL}",
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "temperature": TEMPERATURE,
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICE}}},
        },
        "systemInstruction": {"parts": [{"text": "You must ONLY respond in English. Never speak Spanish or any other language.\n\n" + prompt_text}]},
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        "realtimeInputConfig": {
            "automaticActivityDetection": {
                "startOfSpeechSensitivity": "START_SENSITIVITY_LOW",
                "prefixPaddingMs": 300,
                "silenceDurationMs": SILENCE_MS,
            }
        },
        # Without compression, sessions hit Gemini's context/duration cap
        "contextWindowCompression": {"slidingWindow": {}},
        # Connections are recycled by the server (goAway); resume keeps the conversation
        "sessionResumption": {"handle": resume_handle} if resume_handle else {},
    }
    return {"setup": setup}


async def main():
    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY not set")
        return

    # Find USB speaker sinks
    usb_sinks = find_usb_sinks()
    if not usb_sinks:
        print("ERROR: No USB speaker sinks found")
        return
    print(f"Speakers: {usb_sinks}")

    # Start parec mic (PA resamples webcam 48kHz -> 16kHz)
    mic_proc = subprocess.Popen(
        ["parec", "--device", PA_MIC_SOURCE,
         "--format=s16le", "--channels=1", f"--rate={MIC_RATE}",
         "--latency-msec=100"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    if mic_proc.poll() is not None:
        print("ERROR: parec failed to start")
        return
    print(f"Mic: {PA_MIC_SOURCE} -> 16kHz")

    # Start one pacat per USB speaker sink
    speaker_procs = []
    for sink in usb_sinks:
        p = subprocess.Popen(
            ["pacat", "--playback", "--device", sink,
             "--format=s16le", "--channels=1", f"--rate={SPEAKER_RATE}",
             "--latency-msec=50"],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        speaker_procs.append(p)
        print(f"Speaker pipe: {sink}")

    # Init webcam
    print(f"Opening camera {CAMERA_DEVICE}...")
    cap = cv2.VideoCapture(CAMERA_DEVICE, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
    if not cap.isOpened():
        print("WARNING: Camera not available, running voice-only")
        camera_ok = False
    else:
        # Warmup frames
        for _ in range(10):
            cap.read()
        print(f"Camera ready: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
        camera_ok = True

    # Load prompt
    prompt_text = prompts.get_prompt(SYSTEM_PROMPT_NAME)
    print(f"Prompt: {SYSTEM_PROMPT_NAME}")

    # --- State ---
    audio_buf = bytearray()
    mic_on = True
    prebuffered = False
    is_running = True
    response_active = False  # track if we're in a response cycle
    resume_handle = None
    latest_frame = None

    # --- Mic reader thread ---
    mic_q = queue.Queue(maxsize=200)

    def read_mic():
        while is_running:
            try:
                data = mic_proc.stdout.read(MIC_CHUNK_BYTES)
                if not data:
                    break
                try:
                    mic_q.put_nowait(data)
                except queue.Full:
                    pass
            except Exception:
                break

    # --- Camera reader thread ---
    def read_camera():
        nonlocal latest_frame
        if not camera_ok:
            return
        while is_running:
            try:
                ret, frame = cap.read()
                if ret:
                    latest_frame = frame
                time.sleep(0.033)  # ~30fps
            except Exception:
                time.sleep(0.1)

    pool = ThreadPoolExecutor(max_workers=2)
    pool.submit(read_mic)
    pool.submit(read_camera)

    # --- Write to all speakers ---
    def write_speakers(data):
        for i, p in enumerate(speaker_procs):
            try:
                if p.poll() is None:
                    p.stdin.write(data)
                    p.stdin.flush()
                else:
                    speaker_procs[i] = subprocess.Popen(
                        ["pacat", "--playback", "--device", usb_sinks[i],
                         "--format=s16le", "--channels=1", f"--rate={SPEAKER_RATE}",
                         "--latency-msec=50"],
                        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    # --- Speaker feeder ---
    async def feeder():
        nonlocal prebuffered
        while is_running:
            if not prebuffered:
                if len(audio_buf) >= PREBUFFER_BYTES:
                    prebuffered = True
                    print("[Prebuffer OK - playing]")
                else:
                    await asyncio.sleep(0.005)
                    continue

            if len(audio_buf) >= SPEAKER_CHUNK_BYTES:
                chunk = bytes(audio_buf[:SPEAKER_CHUNK_BYTES])
                del audio_buf[:SPEAKER_CHUNK_BYTES]
                write_speakers(chunk)
                await asyncio.sleep(0.045)
            elif prebuffered and audio_buf:
                # Flush the tail of a response that's shorter than one chunk
                chunk = bytes(audio_buf)
                audio_buf.clear()
                write_speakers(chunk)
            else:
                await asyncio.sleep(0.005)

    # --- Mic sender ---
    async def send_mic(ws):
        while is_running:
            if mic_on:
                try:
                    data = mic_q.get_nowait()
                    await ws.send(json.dumps({
                        "realtimeInput": {
                            "audio": {
                                "data": base64.b64encode(data).decode(),
                                "mimeType": f"audio/pcm;rate={MIC_RATE}",
                            }
                        }
                    }))
                except queue.Empty:
                    pass
                except websockets.exceptions.ConnectionClosed:
                    return
            else:
                try:
                    mic_q.get_nowait()
                except queue.Empty:
                    pass
            await asyncio.sleep(0.008)

    # --- Camera frame sender ---
    async def send_frames(ws):
        if not camera_ok:
            return
        sent = 0
        while is_running:
            if latest_frame is not None:
                b64 = encode_frame_to_jpeg_b64(latest_frame)
                if b64:
                    try:
                        await ws.send(json.dumps({
                            "realtimeInput": {
                                "video": {"data": b64, "mimeType": "image/jpeg"}
                            }
                        }))
                    except websockets.exceptions.ConnectionClosed:
                        return
                    sent += 1
                    if sent == 1:
                        print("[Camera streaming]")
            await asyncio.sleep(IMAGE_SEND_INTERVAL)

    # --- Receiver ---
    async def recv(ws):
        nonlocal mic_on, prebuffered, response_active, resume_handle
        user_text = ""
        while is_running:
            try:
                msg = json.loads(await ws.recv())

                if "sessionResumptionUpdate" in msg:
                    upd = msg["sessionResumptionUpdate"]
                    if upd.get("resumable") and upd.get("newHandle"):
                        resume_handle = upd["newHandle"]
                    continue

                if "goAway" in msg:
                    print(f"[Server recycling connection in {msg['goAway'].get('timeLeft', '?')}]")
                    return

                if "error" in msg:
                    print(f"ERROR: {msg['error'].get('message', msg['error'])}")
                    continue

                sc = msg.get("serverContent")
                if not sc:
                    continue

                if "inputTranscription" in sc:
                    user_text += sc["inputTranscription"].get("text", "")

                if sc.get("interrupted"):
                    # User barged in: drop whatever is still queued for the speakers
                    print("\n[Interrupted]")
                    audio_buf.clear()
                    prebuffered = False
                    response_active = False
                    mic_on = True

                for part in sc.get("modelTurn", {}).get("parts", []):
                    b64 = part.get("inlineData", {}).get("data")
                    if not b64:
                        continue
                    if not response_active:
                        response_active = True
                        if user_text.strip():
                            print(f"You: {user_text.strip()}")
                            user_text = ""
                        audio_buf.clear()
                        prebuffered = False
                    if mic_on:
                        mic_on = False
                        print("[Mic muted]")
                    audio_buf.extend(base64.b64decode(b64))

                if "outputTranscription" in sc:
                    print(sc["outputTranscription"].get("text", ""), end="", flush=True)

                if sc.get("turnComplete"):
                    if not response_active:
                        continue
                    print()
                    response_active = False
                    t0 = time.time()
                    while len(audio_buf) > 0 and time.time() - t0 < 10:
                        await asyncio.sleep(0.02)
                    # Delay to let speaker finish and avoid echo feedback
                    await asyncio.sleep(1.5)
                    # Drain any mic data captured during playback
                    while not mic_q.empty():
                        try:
                            mic_q.get_nowait()
                        except queue.Empty:
                            break
                    prebuffered = False
                    mic_on = True
                    print("[Mic on]\n")

            except websockets.exceptions.ConnectionClosed as e:
                print(f"Connection closed: {e}")
                return

    # --- Connection loop (reconnects + resumes when the server recycles us) ---
    feeder_task = asyncio.create_task(feeder())
    headers = {"x-goog-api-key": GEMINI_API_KEY}
    failures = 0

    try:
        while is_running and failures < 5:
            print("Connecting..." if resume_handle is None else "Reconnecting (resuming session)...")
            try:
                async with websockets.connect(GEMINI_WS_URL, additional_headers=headers,
                                              ping_timeout=30, close_timeout=5,
                                              max_size=None) as ws:
                    await ws.send(json.dumps(build_setup(prompt_text, resume_handle)))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), 15))
                    if "setupComplete" not in reply:
                        raise RuntimeError(f"Setup failed: {reply}")
                    print(f"Connected to Gemini Live - Model: {MODEL}, Voice: {VOICE}")
                    print("Speak to start. Ctrl+C to quit.\n")
                    failures = 0
                    mic_on = True
                    response_active = False

                    tasks = [asyncio.create_task(send_mic(ws)),
                             asyncio.create_task(recv(ws))]
                    if camera_ok:
                        tasks.append(asyncio.create_task(send_frames(ws)))
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    for t in done:
                        if t.exception():
                            raise t.exception()
            except Exception as e:
                failures += 1
                print(f"Connection error ({failures}/5): {e}")
                await asyncio.sleep(2)

    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nBye")
    finally:
        is_running = False
        feeder_task.cancel()
        try:
            mic_proc.kill()
        except Exception:
            pass
        for p in speaker_procs:
            try:
                p.kill()
            except Exception:
                pass
        if camera_ok:
            try:
                cap.release()
            except Exception:
                pass
        print("Done")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
