#!/usr/bin/env python3
"""
G1 Sparky Vision - voice + OpenCV webcam (Gemini Live)
- Camera: OpenCV VideoCapture (USB webcam), streamed as JPEG video input
- Falls back to voice-only if the camera can't be opened
Persona comes from COMPANY_PROFILE in online/.env (default: SPARKY_VISION).
"""

import base64, time
import cv2

import live_chat

CAMERA_DEVICE = "/dev/video0"
JPEG_QUALITY = 75             # JPEG compression quality
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480


class Camera:
    """Keeps the latest webcam frame so the voice loop can send it as JPEG."""

    def __init__(self):
        print(f"Opening camera {CAMERA_DEVICE}...")
        self.cap = cv2.VideoCapture(CAMERA_DEVICE, cv2.CAP_V4L2)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        self.latest_frame = None
        self.ok = self.cap.isOpened()
        if not self.ok:
            print("WARNING: Camera not available, running voice-only")
            return
        # Warmup frames
        for _ in range(10):
            self.cap.read()
        print(f"Camera ready: {int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")

    def start(self, is_running):
        """Reader thread: keep grabbing frames while is_running() is true."""
        while is_running():
            try:
                ret, frame = self.cap.read()
                if ret:
                    self.latest_frame = frame
                time.sleep(0.033)  # ~30fps
            except Exception:
                time.sleep(0.1)

    def jpeg_b64(self):
        """Latest frame as base64 JPEG, or None if there's no frame yet."""
        frame = self.latest_frame
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            return None
        return base64.b64encode(buf.tobytes()).decode("ascii")

    def release(self):
        try:
            self.cap.release()
        except Exception:
            pass


if __name__ == "__main__":
    live_chat.run(default_profile="SPARKY_VISION", camera=Camera())
