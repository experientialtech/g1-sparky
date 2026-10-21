"""
Shared Gemini Live voice loop for the G1 online scripts.
- Mic: parec from webcam PA source at 16kHz
- Speakers: pacat to each USB speaker sink at 24kHz
- Optional camera: JPEG frames streamed as video input
- Reconnects (and resumes the conversation) when the server recycles the connection

g1_sparky.py runs it voice-only; g1_sparky_vision.py passes in a camera.
"""

import os, sys, asyncio, json, base64, time, subprocess
import websockets
import queue
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prompts

load_dotenv()  # online/.env (or a .env further up the tree)

# --- Config ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live")
VOICE = os.getenv("GEMINI_LIVE_VOICE", "Fenrir")
TEMPERATURE = float(os.getenv("GEMINI_LIVE_TEMPERATURE", "1.2"))  # higher = wilder replies
SILENCE_MS = int(os.getenv("GEMINI_LIVE_SILENCE_MS", "500"))      # pause before the robot replies
IMAGE_SEND_INTERVAL = float(os.getenv("IMAGE_SEND_INTERVAL", "1.0"))  # Gemini accepts <= 1 frame/sec

GEMINI_WS_URL = ("wss://generativelanguage.googleapis.com/ws/"
                 "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")

PA_MIC_SOURCE = "alsa_input.usb-HCVsight_FHD_webcamera-02.mono-fallback"

MIC_RATE = 16000      # Gemini Live input: 16kHz PCM16
SPEAKER_RATE = 24000  # Gemini Live output: 24kHz PCM16
S16LE_BYTES = 2
MIC_CHUNK_BYTES = MIC_RATE * S16LE_BYTES // 10          # 100ms = 3200 bytes
SPEAKER_CHUNK_BYTES = SPEAKER_RATE * S16LE_BYTES // 20  # 50ms = 2400 bytes
PREBUFFER_BYTES = SPEAKER_RATE * S16LE_BYTES // 4       # 250ms = 12000 bytes
ECHO_GUARD_SECONDS = 1.5  # keep the mic off this long after the robot stops talking
MAX_RETRY_DELAY = 30


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


def start_speaker(sink):
    return subprocess.Popen(
        ["pacat", "--playback", "--device", sink,
         "--format=s16le", "--channels=1", f"--rate={SPEAKER_RATE}",
         "--latency-msec=50"],
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)


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


async def main(default_profile, camera=None):
    """Run the voice loop. `camera` is an optional object with ok, start(), jpeg_b64(), release()."""
    if not GEMINI_API_KEY:
        print("ERROR: GEMINI_API_KEY not set. See online/README.md (\"Add the API key\").")
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
        speaker_procs.append(start_speaker(sink))
        print(f"Speaker pipe: {sink}")

    use_camera = camera is not None and camera.ok

    # Load prompt
    profile = os.getenv("COMPANY_PROFILE", default_profile)
    prompt_text = prompts.get_prompt(profile)
    print(f"Prompt: {profile}")

    # --- State ---
    audio_buf = bytearray()
    mic_on = True
    prebuffered = False
    is_running = True
    response_active = False  # track if we're in a response cycle
    resume_handle = None

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

    pool = ThreadPoolExecutor(max_workers=2)
    pool.submit(read_mic)
    if use_camera:
        pool.submit(camera.start, lambda: is_running)

    # --- Write to all speakers ---
    def write_speakers(data):
        for i, p in enumerate(speaker_procs):
            try:
                if p.poll() is None:
                    p.stdin.write(data)
                    p.stdin.flush()
                else:
                    speaker_procs[i] = start_speaker(usb_sinks[i])
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
        sent = 0
        while is_running:
            b64 = camera.jpeg_b64()
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
                    await asyncio.sleep(ECHO_GUARD_SECONDS)
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

    # --- Connection loop: reconnect forever, resuming the conversation when possible ---
    feeder_task = asyncio.create_task(feeder())
    headers = {"x-goog-api-key": GEMINI_API_KEY}
    retry_delay = 1

    try:
        while is_running:
            resuming = resume_handle is not None
            print("Reconnecting (resuming session)..." if resuming else "Connecting...")
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
                    retry_delay = 1
                    mic_on = True
                    response_active = False

                    tasks = [asyncio.create_task(send_mic(ws)),
                             asyncio.create_task(recv(ws))]
                    if use_camera:
                        tasks.append(asyncio.create_task(send_frames(ws)))
                    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for t in pending:
                        t.cancel()
                    for t in done:
                        if t.exception():
                            raise t.exception()
            except Exception as e:
                print(f"Connection error: {e}")
                if resuming:
                    # Saved sessions expire; don't keep retrying a dead one
                    print("[Starting a fresh conversation]")
                    resume_handle = None
                    continue
                print(f"Retrying in {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, MAX_RETRY_DELAY)

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
        if use_camera:
            camera.release()
        print("Done")


def run(default_profile, camera=None):
    try:
        asyncio.run(main(default_profile, camera))
    except KeyboardInterrupt:
        pass
