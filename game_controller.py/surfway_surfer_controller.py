from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import sys
import time
from urllib.request import urlopen


MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/gesture_recognizer/"
    "gesture_recognizer/float16/1/gesture_recognizer.task"
)
GESTURE_KEYS = {"Pointing_Up": "up", "Closed_Fist": "down", "Thumb_Up": "space"}
GESTURE_CONFIDENCE = 0.65
HAND_CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14),
    (14, 15), (15, 16), (13, 17), (0, 17), (17, 18), (18, 19), (19, 20),
)


class OneTap:

    def __init__(self, stable_seconds=0.08, cooldown=0.18):
        self.stable_seconds = stable_seconds
        self.cooldown = cooldown
        self.reset()

    def reset(self):
        self.candidate = None
        self.since = None
        self.fired = None
        self.last_time = float("-inf")

    def update(self, value, now):
        if self.since is None or value != self.candidate:
            self.candidate = value
            self.since = now
        if now - self.since < self.stable_seconds:
            return None
        if value is None:
            self.fired = None
            return None
        if value != self.fired and now - self.last_time >= self.cooldown:
            self.fired = value
            self.last_time = now
            return value
        return None


class HorizontalPosition:

    def __init__(self):
        self.reset()

    def reset(self):
        self.x = None
        self.zone = None

    def update(self, palm_x):
        if palm_x is None:
            self.reset()
            return None
        self.x = palm_x if self.x is None else 0.45 * palm_x + 0.55 * self.x
        if self.x < 0.36:
            self.zone = "left"
        elif self.x > 0.64:
            self.zone = "right"
        elif 0.42 <= self.x <= 0.58:
            self.zone = None
        return self.zone


class WindowsInput:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.user32 = None
        self.keyboard = None
        self.previous_keys = {}
        if os.name == "nt":
            from ctypes import wintypes

            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            self.user32.GetForegroundWindow.argtypes = []
            self.user32.GetForegroundWindow.restype = wintypes.HWND
            self.user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            self.user32.GetWindowTextLengthW.restype = ctypes.c_int
            self.user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            self.user32.GetWindowTextW.restype = ctypes.c_int
            self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
            self.user32.GetAsyncKeyState.restype = ctypes.c_short
        elif not dry_run:
            raise RuntimeError("Game control requires Windows. Use --dry-run for a camera preview.")

        if not dry_run:
            import pydirectinput

            self.keyboard = pydirectinput
            self.keyboard.PAUSE = 0.0

    def pressed(self, virtual_key):
        if self.user32 is None:
            return False
        down = bool(self.user32.GetAsyncKeyState(virtual_key) & 0x8000)
        was_down = self.previous_keys.get(virtual_key, False)
        self.previous_keys[virtual_key] = down
        return down and not was_down

    def game_focused(self, title_text):
        if self.dry_run:
            return True
        handle = self.user32.GetForegroundWindow()
        size = self.user32.GetWindowTextLengthW(handle) + 1
        title = ctypes.create_unicode_buffer(size)
        self.user32.GetWindowTextW(handle, title, size)
        return title_text.casefold() in title.value.casefold()

    def tap(self, key, title_text):
        if not self.game_focused(title_text):
            return False
        if self.dry_run:
            print("Preview command:", key)
        else:
            try:
                self.keyboard.keyDown(key, _pause=False)
                time.sleep(0.04)
            finally:
                self.keyboard.keyUp(key, _pause=False)
        return True


def ensure_model(path):
    if path.is_file() and path.stat().st_size > 100_000:
        return
    print("Downloading the official gesture model (first run only)...", flush=True)
    temporary = path.with_suffix(".task.part")
    try:
        with urlopen(MODEL_URL, timeout=30) as response:
            temporary.write_bytes(response.read())
        if temporary.stat().st_size < 100_000:
            raise RuntimeError("The model download is incomplete.")
        temporary.replace(path)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            "Could not download the model. Connect to the internet, or put your "
            "existing gesture_recognizer.task beside this Python file."
        ) from exc


def open_camera(cv2, index):
    backends = [cv2.CAP_ANY]
    if os.name == "nt":
        backends.insert(0, cv2.CAP_DSHOW)
    for backend in backends:
        camera = cv2.VideoCapture(index, backend)
        if camera.isOpened():
            camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            ok, frame = camera.read()
            if ok and frame is not None:
                return camera
        camera.release()
    raise RuntimeError("Cannot open the webcam. Close your other camera program and try again.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", type=int, default=0, help="Webcam number, usually 0 or 1")
    parser.add_argument("--dry-run", action="store_true", help="Show detections without sending keys")
    parser.add_argument("--title", default="Subway Surfers", help="Text in the game window title")
    args = parser.parse_args()
    if not args.title.strip():
        parser.error("--title must not be empty")

    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    controls = WindowsInput(args.dry_run)
    model = Path(__file__).resolve().parent / "gesture_recognizer.task"
    ensure_model(model)
    options = vision.GestureRecognizerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(model)),
        running_mode=vision.RunningMode.VIDEO,
        num_hands=1,
        min_hand_detection_confidence=0.6,
        min_hand_presence_confidence=0.6,
        min_tracking_confidence=0.6,
    )
    direction = HorizontalPosition()
    lane_taps, gesture_taps = OneTap(), OneTap()
    paused = False
    last_stamp = -1
    last_action, last_action_at = "", 0.0
    window = "Hand Gesture Controller"
    camera = None
    print("Click inside Subway Surfers to play. F8 pauses controls; Esc exits.", flush=True)
    print("Center your hand between lane changes. Relax between repeated gestures.", flush=True)
    try:
        with vision.GestureRecognizer.create_from_options(options) as recognizer:
            camera = open_camera(cv2, args.camera)
            cv2.namedWindow(window, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(window, 640, 480)
            while True:
                if controls.pressed(0x1B):
                    break
                if controls.pressed(0x77):
                    paused = not paused
                ok, frame = camera.read()
                if not ok or frame is None:
                    raise RuntimeError("The webcam stopped providing frames.")
                frame = cv2.flip(frame, 1)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                stamp = max(last_stamp + 1, time.monotonic_ns() // 1_000_000)
                last_stamp = stamp
                result = recognizer.recognize_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), stamp
                )
                now = time.monotonic()
                height, width = frame.shape[:2]
                palm_x, gesture, confidence = None, None, 0.0
                if result.hand_landmarks:
                    hand = result.hand_landmarks[0]
                    palm_x = sum(hand[i].x for i in (0, 5, 9, 13, 17)) / 5
                    points = [(int(p.x * width), int(p.y * height)) for p in hand]
                    for a, b in HAND_CONNECTIONS:
                        cv2.line(frame, points[a], points[b], (0, 200, 180), 2)
                    for point in points:
                        cv2.circle(frame, point, 3, (0, 255, 100), -1)
                    if result.gestures and result.gestures[0]:
                        category = result.gestures[0][0]
                        confidence = category.score
                        if confidence >= GESTURE_CONFIDENCE:
                            gesture = category.category_name

                focused = controls.game_focused(args.title)
                active = focused and not paused
                if active:
                    for action in (
                        lane_taps.update(direction.update(palm_x), now),
                        gesture_taps.update(GESTURE_KEYS.get(gesture), now),
                    ):
                        if action and controls.tap(action, args.title):
                            last_action, last_action_at = action.upper(), now
                else:
                    lane_taps.reset()
                    gesture_taps.reset()
                    direction.reset()

                color = (70, 235, 100) if active else (0, 180, 255)
                status = "PAUSED (F8 to resume)" if paused else (
                    "PREVIEW - no keys sent" if args.dry_run else
                    "ACTIVE" if focused else "Click inside Subway Surfers"
                )
                for fraction in (0.36, 0.64):
                    cv2.line(frame, (int(width * fraction), 65),
                             (int(width * fraction), height - 55), (170, 170, 170), 1)
                lines = [status, f"Gesture: {gesture or 'None'} ({confidence:.0%})"]
                if now - last_action_at < 0.8:
                    lines.append("Command: " + last_action)
                for i, line in enumerate(lines):
                    cv2.putText(frame, line, (12, 25 + 26 * i),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.57, color, 2, cv2.LINE_AA)
                cv2.putText(frame, "F8: pause controls | ESC: exit", (12, height - 16),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
                cv2.imshow(window, frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q")):
                    break
                if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                    break
    finally:
        if camera is not None:
            camera.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except ImportError as exc:
        print(f"Missing or incompatible package: {exc}\nPython: {sys.executable}\n"
              "In your VS Code PowerShell terminal, run:\n"
              "python -m pip install opencv-python mediapipe pydirectinput", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"Controller stopped: {exc}", file=sys.stderr)
        raise SystemExit(1)
