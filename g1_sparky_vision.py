#!/usr/bin/env python3
"""
G1 Sparky Vision - PulseAudio voice + OpenCV webcam
- Mic: parec from webcam PA source at 24kHz
- Speakers: pacat to each USB speaker sink at 24kHz
- Camera: OpenCV VideoCapture (USB webcam)
- Periodically injects camera frames into conversation
- OpenAI Realtime API
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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview")
VOICE = os.getenv("OPENAI_REALTIME_VOICE", "cedar")
SYSTEM_PROMPT_NAME = os.getenv("COMPANY_PROFILE", "SPARKY_VISION")

PA_MIC_SOURCE = "alsa_input.usb-HCVsight_FHD_webcamera-02.mono-fallback"

AUDIO_RATE = 24000
S16LE_BYTES = 2
MIC_CHUNK_BYTES = AUDIO_RATE * S16LE_BYTES // 10     # 100ms = 4800 bytes
SPEAKER_CHUNK_BYTES = AUDIO_RATE * S16LE_BYTES // 20  # 50ms = 2400 bytes
PREBUFFER_BYTES = AUDIO_RATE * S16LE_BYTES // 4       # 250ms = 12000 bytes

# --- Vision Config ---
CAMERA_DEVICE = "/dev/video0"
IMAGE_SEND_INTERVAL = 5.0     # Send a frame every N seconds
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


def encode_frame_to_data_url(frame: np.ndarray) -> str:
    """Encode an OpenCV BGR frame to a JPEG data URL."""
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    if not ok:
        return None
    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


async def main():
    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY not set")
        return

    # Find USB speaker sinks
    usb_sinks = find_usb_sinks()
    if not usb_sinks:
        print("ERROR: No USB speaker sinks found")
        return
    print(f"Speakers: {usb_sinks}")

    # Start parec mic
    mic_proc = subprocess.Popen(
        ["parec", "--device", PA_MIC_SOURCE,
         "--format=s16le", "--channels=1", "--rate=24000",
         "--latency-msec=100"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    time.sleep(0.3)
    if mic_proc.poll() is not None:
        print("ERROR: parec failed to start")
        return
    print(f"Mic: {PA_MIC_SOURCE} -> 24kHz")

    # Start one pacat per USB speaker sink
    speaker_procs = []
    for sink in usb_sinks:
        p = subprocess.Popen(
            ["pacat", "--playback", "--device", sink,
             "--format=s16le", "--channels=1", "--rate=24000",
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

    # Connect to OpenAI
    url = f"wss://api.openai.com/v1/realtime?model={MODEL}"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "OpenAI-Beta": "realtime=v1",
    }
    print("Connecting...")

    try:
        async with websockets.connect(url, additional_headers=headers,
                                       ping_timeout=30, close_timeout=5) as ws:
            print("Connected to OpenAI Realtime")

            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "modalities": ["audio", "text"],
                    "instructions": "You must ONLY respond in English. Never speak Spanish or any other language.\n\n" + prompt_text,
                    "voice": VOICE,
                    "input_audio_format": "pcm16",
                    "output_audio_format": "pcm16",
                    "input_audio_transcription": {"model": "whisper-1", "language": "en"},
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.65,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 700
                    }
                }
            }))
            print(f"Session configured - Voice: {VOICE}")
            print("Speak to start. Ctrl+C to quit.\n")

            # --- State ---
            audio_buf = bytearray()
            mic_on = True
            prebuffered = False
            is_running = True
            response_active = False
            latest_frame = None
            last_image_ts = 0.0

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
                                 "--format=s16le", "--channels=1", "--rate=24000",
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
                    else:
                        await asyncio.sleep(0.005)

            # --- Mic sender ---
            async def send_mic():
                while is_running:
                    if mic_on:
                        try:
                            data = mic_q.get_nowait()
                            await ws.send(json.dumps({
                                "type": "input_audio_buffer.append",
                                "audio": base64.b64encode(data).decode(),
                            }))
                        except queue.Empty:
                            pass
                    else:
                        try:
                            mic_q.get_nowait()
                        except queue.Empty:
                            pass
                    await asyncio.sleep(0.008)

            # --- Image injector ---
            async def image_injector():
                nonlocal last_image_ts
                if not camera_ok:
                    return
                while is_running:
                    now = time.time()
                    if (now - last_image_ts) >= IMAGE_SEND_INTERVAL and latest_frame is not None:
                        data_url = encode_frame_to_data_url(latest_frame)
                        if data_url:
                            await ws.send(json.dumps({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_image",
                                            "image_url": data_url
                                        },
                                        {
                                            "type": "input_text",
                                            "text": "(Latest camera frame - use this to see who you're talking to. Don't mention receiving an image unless asked what you see.)"
                                        }
                                    ]
                                }
                            }))
                            print("[Camera frame sent]")
                            last_image_ts = now
                    await asyncio.sleep(0.5)

            # --- Receiver ---
            async def recv():
                nonlocal mic_on, prebuffered, response_active
                while is_running:
                    try:
                        msg = json.loads(await ws.recv())
                        t = msg.get("type")

                        if t == "response.created":
                            audio_buf.clear()
                            prebuffered = False
                            response_active = True
                            if mic_on:
                                mic_on = False
                                print("[Mic muted]")

                        elif t in ("response.audio.delta", "response.output_audio.delta"):
                            if mic_on:
                                mic_on = False
                                print("[Mic muted]")
                            b64 = msg.get("delta") or msg.get("audio") or ""
                            if b64:
                                audio_buf.extend(base64.b64decode(b64))

                        elif t in ("response.audio.done", "response.output_audio.done", "response.done"):
                            if not response_active:
                                continue
                            response_active = False
                            t0 = time.time()
                            while len(audio_buf) > 0 and time.time() - t0 < 10:
                                await asyncio.sleep(0.02)
                            await asyncio.sleep(1.5)
                            while not mic_q.empty():
                                try:
                                    mic_q.get_nowait()
                                except queue.Empty:
                                    break
                            await ws.send(json.dumps({
                                "type": "input_audio_buffer.clear"
                            }))
                            prebuffered = False
                            mic_on = True
                            print("[Mic on]\n")

                        elif t == "input_audio_buffer.speech_started":
                            print("[Listening]")
                            audio_buf.clear()
                            prebuffered = False
                            response_active = False
                            if not mic_on:
                                mic_on = True

                        elif t == "response.audio_transcript.delta":
                            print(msg.get("delta", ""), end="", flush=True)

                        elif t == "response.audio_transcript.done":
                            print()

                        elif t == "input_audio_buffer.speech_stopped":
                            print("[Processing...]")

                        elif t == "conversation.item.input_audio_transcription.completed":
                            print(f"You: {msg.get('transcript', '')}")

                        elif t == "error":
                            print(f"ERROR: {msg.get('error', {}).get('message', '?')}")

                    except websockets.exceptions.ConnectionClosed:
                        print("Connection closed")
                        break
                    except Exception as e:
                        print(f"Recv error: {e}")
                        break

            await asyncio.gather(
                asyncio.create_task(feeder()),
                asyncio.create_task(send_mic()),
                asyncio.create_task(image_injector()),
                asyncio.create_task(recv()),
            )

    except KeyboardInterrupt:
        print("\nBye")
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
        is_running = False
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
    MAX_RETRIES = 5
    retry = 0
    while retry < MAX_RETRIES:
        try:
            asyncio.run(main())
            break
        except KeyboardInterrupt:
            break
        except Exception as e:
            retry += 1
            print(f"Retry {retry}/{MAX_RETRIES}: {e}")
            time.sleep(3)
