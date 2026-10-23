from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
import math
import os
from pathlib import Path
import sys
import time
from urllib.request import urlopen

MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
    "hand_landmarker/float16/1/hand_landmarker.task"
)
CONNECTIONS = (
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14),
    (14, 15), (15, 16), (13, 17), (0, 17), (17, 18), (18, 19), (19, 20),
)


def title_matches(title, target):
    name = title.casefold()
    if "visual studio code" in name or name == "geometry hand controller":
        return False
    if target.casefold() == "geometry dash":
        return "geometry dash" in name or "geometrydash" in name
    return bool(target.strip()) and target.casefold() in name


def pinch_ratio(hand, width, height):
    def distance(a, b):
        return math.hypot((hand[a].x - hand[b].x) * width,
                          (hand[a].y - hand[b].y) * height)

    palm = max(distance(0, 9), distance(5, 17))
    return distance(4, 8) / palm if palm >= 30 else None


@dataclass(frozen=True)
class JumpIntent:
    tap: bool = False
    hold: bool = False
    label: str = "Separate fingertips first"


class PinchController:
    def __init__(self, mode="hold", close=0.32, open_ratio=0.52):
        if not 0 < close < open_ratio:
            raise ValueError("Require 0 < close < open.")
        self.mode, self.close, self.open_ratio = mode, close, open_ratio
        self.reset()

    def reset(self):
        self.armed = False
        self.closed = False
        self.open_since = None
        self.last_tap = float("-inf")

    def update(self, ratio, now, enabled=True):
        if not enabled or ratio is None or not math.isfinite(ratio):
            self.reset()
            return JumpIntent(label="Paused" if not enabled else "Show hand / move closer")

        tap = False
        if ratio >= self.open_ratio:
            self.closed = False
            if self.open_since is None:
                self.open_since = now
            if now - self.open_since >= 0.025:
                self.armed = True
        elif ratio <= self.close:
            self.open_since = None
            if self.armed:
                self.armed = False
                self.closed = True
                if self.mode == "tap" and now - self.last_tap >= 0.08:
                    tap = True
                    self.last_tap = now
        else:
            self.open_since = None

        hold = self.mode == "hold" and self.closed
        label = ("HOLDING SPACE" if hold else "JUMP!" if tap else
                 "Separate fingertips" if self.closed else
                 "Ready - tap fingertips" if self.armed else "Separate fingertips first")
        return JumpIntent(tap=tap, hold=hold, label=label)


class WindowsInput:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.held = False
        self.user32 = self.keyboard = None
        self.previous_keys = {}
        if os.name == "nt":
            from ctypes import wintypes
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            signatures = {
                "GetForegroundWindow": ([], wintypes.HWND),
                "GetWindowTextLengthW": ([wintypes.HWND], ctypes.c_int),
                "GetWindowTextW": ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
                "GetAsyncKeyState": ([ctypes.c_int], ctypes.c_short),
            }
            for name, (args, result) in signatures.items():
                function = getattr(self.user32, name)
                function.argtypes, function.restype = args, result
        elif not dry_run:
            raise RuntimeError("Game control requires Windows; --dry-run previews on other systems.")

        if not dry_run:
            import pydirectinput
            self.keyboard = pydirectinput
            self.keyboard.PAUSE = 0.0

    def pressed(self, code):
        if self.user32 is None:
            return False
        down = bool(self.user32.GetAsyncKeyState(code) & 0x8000)
        previous = self.previous_keys.get(code, False)
        self.previous_keys[code] = down
        return down and not previous

    def game_focused(self, target):
        if self.dry_run:
            return True
        handle = self.user32.GetForegroundWindow()
        size = self.user32.GetWindowTextLengthW(handle) + 1
        title = ctypes.create_unicode_buffer(size)
        self.user32.GetWindowTextW(handle, title, size)
        return title_matches(title.value, target)

    def release(self):
        if not self.held:
            return
        if not self.dry_run:
            failsafe = self.keyboard.FAILSAFE
            try:
                self.keyboard.FAILSAFE = False
                self.keyboard.keyUp("space", _pause=False)
            finally:
                self.keyboard.FAILSAFE = failsafe
        self.held = False

    def apply(self, intent, target):
        if not self.game_focused(target):
            self.release()
            return False
        if intent.hold:
            if not self.held:
                self.held = True
                try:
                    if self.dry_run:
                        print("Preview: HOLD", flush=True)
                    else:
                        self.keyboard.keyDown("space", _pause=False)
                except BaseException:
                    self.release()
                    raise
            return True

        self.release()
        if intent.tap:
            self.held = True
            try:
                if self.dry_run:
                    print("Preview: JUMP", flush=True)
                else:
                    self.keyboard.keyDown("space", _pause=False)
                    time.sleep(0.035)
            finally:
                self.release()
        return True


def ensure_model(path):
    if path.is_file() and path.stat().st_size > 100_000:
        return
    print("Downloading the hand model (first run only)...", flush=True)
    temporary = path.with_suffix(".task.part")
    try:
        with urlopen(MODEL_URL, timeout=30) as response:
            temporary.write_bytes(response.read())
        if temporary.stat().st_size < 100_000:
            raise RuntimeError("Incomplete model download")
        temporary.replace(path)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            "Model download failed. Check your internet connection, or download "
            + MODEL_URL + " and save it beside this script as hand_landmarker.task."
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
            camera.set(cv2.CAP_PROP_FPS, 30)
            ok, frame = camera.read()
            if ok and frame is not None:
                return camera
        camera.release()
    raise RuntimeError("Cannot open webcam. Close other camera apps or try --camera 1.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--title", default="Geometry Dash")
    parser.add_argument("--mode", choices=("tap", "hold"), default="hold")
    parser.add_argument("--close", type=float, default=0.32)
    parser.add_argument("--open", dest="open_ratio", type=float, default=0.52)
    args = parser.parse_args()
    if not args.title.strip():
        parser.error("--title must not be empty")
    if args.camera < 0:
        parser.error("--camera must be 0 or higher")
    if not 0 < args.close < args.open_ratio:
        parser.error("Require 0 < --close < --open.")

    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision

    controls = WindowsInput(args.dry_run)
    detector = PinchController(args.mode, args.close, args.open_ratio)
    model = Path(__file__).resolve().parent / "hand_landmarker.task"
    ensure_model(model)
    options = vision.HandLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(model)),
        running_mode=vision.RunningMode.VIDEO, num_hands=1,
        min_hand_detection_confidence=0.6, min_hand_presence_confidence=0.6,
        min_tracking_confidence=0.6,
    )
    paused, camera = False, None
    last_stamp, jumps = -1, 0
    window = "Geometry Hand Controller"
    print("Pinch thumb and index to hold jump; separate them to release.", flush=True)
    print("Click inside Geometry Dash. F8: pause | F9: TAP/HOLD | F10: stop.", flush=True)
    try:
        with vision.HandLandmarker.create_from_options(options) as landmarker:
            camera = open_camera(cv2, args.camera)
            cv2.namedWindow(window, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(window, 640, 480)
            while True:
                if controls.pressed(0x79):
                    break
                if controls.pressed(0x77):
                    paused = not paused
                    controls.release()
                    detector.reset()
                if controls.pressed(0x78):
                    controls.release()
                    detector.mode = "hold" if detector.mode == "tap" else "tap"
                    detector.reset()

                if paused or not controls.game_focused(args.title):
                    controls.release()
                ok, frame = camera.read()
                if not ok or frame is None:
                    raise RuntimeError("The webcam stopped providing frames.")
                frame = cv2.flip(frame, 1)
                height, width = frame.shape[:2]
                stamp = max(last_stamp + 1, time.monotonic_ns() // 1_000_000)
                last_stamp = stamp
                image = mp.Image(image_format=mp.ImageFormat.SRGB,
                                 data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                result = landmarker.detect_for_video(image, stamp)
                ratio = None
                if result.hand_landmarks:
                    hand = result.hand_landmarks[0]
                    ratio = pinch_ratio(hand, width, height)
                    points = [(int(p.x * width), int(p.y * height)) for p in hand]
                    for a, b in CONNECTIONS:
                        cv2.line(frame, points[a], points[b], (0, 180, 220), 2)
                    for point in points:
                        cv2.circle(frame, point, 3, (50, 240, 100), -1)
                    cv2.line(frame, points[4], points[8], (255, 180, 40), 3)

                focused = controls.game_focused(args.title)
                active = focused and not paused
                intent = detector.update(ratio, time.monotonic(), enabled=active)
                if controls.apply(intent, args.title) and intent.tap:
                    jumps += 1
                cv2.imshow(window, frame)
                key = cv2.waitKey(1) & 0xFF
                if key in (27, ord("q")):
                    break
                if key == ord("p"):
                    paused = not paused
                    controls.release()
                    detector.reset()
                if key == ord("m"):
                    controls.release()
                    detector.mode = "hold" if detector.mode == "tap" else "tap"
                    detector.reset()
                if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                    break
    finally:
        try:
            controls.release()
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
              "Run in the same VS Code terminal:\n"
              "python -m pip install opencv-python mediapipe pydirectinput", file=sys.stderr)
        raise SystemExit(1)
    except Exception as exc:
        print(f"Controller stopped: {exc}", file=sys.stderr)
        raise SystemExit(1)