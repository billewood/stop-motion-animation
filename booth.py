"""
Stop-motion animation booth for kids fair.

Hardware:
  - Windows 11 desktop
  - USB webcam (OpenCV index, default 0)
  - Arduino sending single-char button events over serial:
      G = green  -> take picture
      R = red    -> delete last picture
      B = blue   -> build movie and play it back
      W = white  -> yes (save / upload current movie)
      K = black  -> no  (keep working)
      Y = yellow -> toggle onion skin

Setup (Windows):
    py -m venv .venv
    .venv\\Scripts\\activate
    pip install -r requirements.txt
    python booth.py --port COM3

Run with --port none to test with keyboard only:
    g/r/b/w/k/y mirror the buttons; q quits.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

try:
    import serial  # pyserial
except ImportError:
    serial = None


# ---- Configuration ----------------------------------------------------------

FPS = 6                       # playback frame rate for the finished movie
ONION_OPACITY = 0.40          # blend strength of previous frame on live preview
WINDOW_NAME = 'Stop Motion Booth'
SESSIONS_DIR = Path('sessions')
SAVED_DIR = Path('saved_movies')
CLOUD_DIR = Path('cloud_outbox')   # files dropped here get picked up by uploader

SERIAL_BAUD = 9600


# ---- Button event source ----------------------------------------------------

class ButtonSource:
    """Reads single-char button events from Arduino, or the keyboard if no port."""

    KEY_MAP = {
        ord('g'): 'G', ord('r'): 'R', ord('b'): 'B',
        ord('w'): 'W', ord('k'): 'K', ord('y'): 'Y',
    }

    def __init__(self, port: str | None):
        self.ser = None
        if port and port.lower() != 'none':
            if serial is None:
                raise RuntimeError('pyserial not installed; run: pip install pyserial')
            self.ser = serial.Serial(port, SERIAL_BAUD, timeout=0)
            time.sleep(2.0)  # allow Arduino reset
            self.ser.reset_input_buffer()

    def poll(self, key: int) -> str | None:
        """Return one of G/R/B/W/K/Y, or None. Call once per frame."""
        if self.ser is not None:
            data = self.ser.read(64)
            if data:
                for ch in data:
                    c = chr(ch).upper()
                    if c in 'GRBWKY':
                        return c
        if key != -1 and key in self.KEY_MAP:
            return self.KEY_MAP[key]
        return None

    def close(self):
        if self.ser is not None:
            self.ser.close()


# ---- Session state ----------------------------------------------------------

class Session:
    """One animation session: a folder of numbered JPEGs."""

    def __init__(self, root: Path):
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.dir = root / stamp
        self.dir.mkdir(parents=True, exist_ok=True)
        self.frames: list[Path] = []

    def add(self, frame_bgr: np.ndarray) -> Path:
        path = self.dir / f'frame_{len(self.frames):04d}.jpg'
        cv2.imwrite(str(path), frame_bgr)
        self.frames.append(path)
        return path

    def pop(self) -> Path | None:
        if not self.frames:
            return None
        path = self.frames.pop()
        try:
            path.unlink()
        except OSError:
            pass
        return path

    def last_frame(self) -> np.ndarray | None:
        if not self.frames:
            return None
        return cv2.imread(str(self.frames[-1]))

    def cleanup_if_empty(self):
        if not self.frames and self.dir.exists():
            try:
                self.dir.rmdir()
            except OSError:
                pass


# ---- Rendering helpers ------------------------------------------------------

def draw_hud(frame: np.ndarray, frames_count: int, onion: bool) -> np.ndarray:
    """Top banner with frame count + onion-skin status, large and friendly."""
    out = frame.copy()
    h, w = out.shape[:2]
    cv2.rectangle(out, (0, 0), (w, 70), (0, 0, 0), -1)
    cv2.putText(out, f'Frames: {frames_count}', (20, 50),
                cv2.FONT_HERSHEY_SIMPLEX, 1.4, (255, 255, 255), 3)
    if onion:
        cv2.putText(out, 'ONION', (w - 200, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 220, 255), 3)
    return out


def apply_onion(live: np.ndarray, prev: np.ndarray | None) -> np.ndarray:
    if prev is None:
        return live
    if prev.shape != live.shape:
        prev = cv2.resize(prev, (live.shape[1], live.shape[0]))
    return cv2.addWeighted(live, 1.0 - ONION_OPACITY, prev, ONION_OPACITY, 0)


def big_message(frame: np.ndarray, lines: list[str]) -> np.ndarray:
    out = frame.copy()
    h, w = out.shape[:2]
    overlay = out.copy()
    cv2.rectangle(overlay, (0, h // 2 - 120), (w, h // 2 + 120), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.7, out, 0.3, 0, out)
    for i, line in enumerate(lines):
        size = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 1.4, 3)[0]
        x = (w - size[0]) // 2
        y = h // 2 + (i - len(lines) / 2 + 0.7) * 60
        cv2.putText(out, line, (x, int(y)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (255, 255, 255), 3)
    return out


# ---- Movie build & playback -------------------------------------------------

def build_movie(session: Session) -> Path | None:
    if not session.frames:
        return None
    first = cv2.imread(str(session.frames[0]))
    h, w = first.shape[:2]
    out_path = session.dir / 'movie.mp4'
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    writer = cv2.VideoWriter(str(out_path), fourcc, FPS, (w, h))
    for p in session.frames:
        img = cv2.imread(str(p))
        if img is None:
            continue
        if img.shape[:2] != (h, w):
            img = cv2.resize(img, (w, h))
        writer.write(img)
    writer.release()
    return out_path


def play_movie(path: Path, buttons: ButtonSource) -> None:
    """Play movie once, looping until any button press."""
    while True:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return
        delay = max(1, int(1000 / FPS))
        interrupted = False
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            cv2.imshow(WINDOW_NAME, frame)
            key = cv2.waitKey(delay) & 0xFF
            key = -1 if key == 255 else key
            if key == ord('q'):
                cap.release()
                return
            if buttons.poll(key) is not None:
                interrupted = True
                break
        cap.release()
        if interrupted:
            return


# ---- Main loop --------------------------------------------------------------

def run(camera_index: int, port: str | None, fullscreen: bool) -> int:
    SESSIONS_DIR.mkdir(exist_ok=True)
    SAVED_DIR.mkdir(exist_ok=True)
    CLOUD_DIR.mkdir(exist_ok=True)

    cap = cv2.VideoCapture(camera_index, cv2.CAP_DSHOW if os.name == 'nt' else 0)
    if not cap.isOpened():
        print(f'Could not open camera index {camera_index}', file=sys.stderr)
        return 2
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    buttons = ButtonSource(port)

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    if fullscreen:
        cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN,
                              cv2.WINDOW_FULLSCREEN)

    session = Session(SESSIONS_DIR)
    onion = False
    flash_until = 0.0   # brief white flash after capture
    pending_save: Path | None = None

    try:
        while True:
            ok, live = cap.read()
            if not ok:
                continue

            display = apply_onion(live, session.last_frame() if onion else None)
            display = draw_hud(display, len(session.frames), onion)

            if pending_save is not None:
                display = big_message(display, [
                    'Save this movie?',
                    'WHITE = yes    BLACK = keep working',
                ])

            if time.time() < flash_until:
                display = cv2.addWeighted(display, 0.4,
                                          np.full_like(display, 255), 0.6, 0)

            cv2.imshow(WINDOW_NAME, display)
            key = cv2.waitKey(1) & 0xFF
            key = -1 if key == 255 else key
            if key == ord('q'):
                break

            event = buttons.poll(key)
            if event is None:
                continue

            if pending_save is not None:
                # Yes/No prompt active — only white/black do anything.
                if event == 'W':
                    saved = SAVED_DIR / f'{session.dir.name}.mp4'
                    shutil.copy2(pending_save, saved)
                    shutil.copy2(pending_save, CLOUD_DIR / saved.name)
                    print(f'Saved {saved} (queued for cloud upload)')
                    pending_save = None
                    # start a fresh session
                    session.cleanup_if_empty()
                    session = Session(SESSIONS_DIR)
                    onion = False
                elif event == 'K':
                    pending_save = None
                continue

            if event == 'G':
                session.add(live)
                flash_until = time.time() + 0.12
            elif event == 'R':
                session.pop()
            elif event == 'Y':
                onion = not onion
            elif event == 'B':
                movie = build_movie(session)
                if movie is not None:
                    play_movie(movie, buttons)
                    pending_save = movie
            # white/black outside of prompt: ignore

    finally:
        cap.release()
        buttons.close()
        cv2.destroyAllWindows()
        session.cleanup_if_empty()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description='Stop-motion animation booth')
    p.add_argument('--camera', type=int, default=0, help='OpenCV camera index')
    p.add_argument('--port', default='none',
                   help='Arduino serial port (e.g. COM3) or "none" for keyboard')
    p.add_argument('--windowed', action='store_true', help='Disable fullscreen')
    args = p.parse_args()
    return run(args.camera, args.port, fullscreen=not args.windowed)


if __name__ == '__main__':
    sys.exit(main())
