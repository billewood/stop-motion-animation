"""
Stop-motion animation booth for kids fair.

Capture backends:
  - digicam: Canon (e.g. T3) or other DSLR via digiCamControl's HTTP webserver.
             Enable it in digiCamControl: File -> Settings -> Webserver.
             Default URL http://localhost:5513. Start live view in the app
             (or let this program start it) before running.
  - webcam:  USB webcam via OpenCV, for bench testing without the DSLR.

Arduino sends a single ASCII char per button press over USB serial @ 9600:
  G green  -> take picture
  R red    -> delete last picture
  B blue   -> build movie and play it back
  W white  -> yes (save & queue for cloud upload)
  Y yellow -> toggle onion skin / no (keep working)

Keyboard fallback with --port none: g/r/b/w/y mirror the buttons; q quits.

Setup (Windows):
    py -m venv .venv
    .venv\\Scripts\\activate
    pip install -r requirements.txt
    python booth.py --backend digicam --port COM3
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

try:
    import serial  # pyserial
except ImportError:
    serial = None

try:
    import requests
except ImportError:
    requests = None


# ---- Configuration ----------------------------------------------------------

FPS = 6                       # playback frame rate for the finished movie
ONION_OPACITY = 0.40          # blend strength of previous frame on live preview
MOVIE_WIDTH = 1280            # downscale target for the rendered movie
WINDOW_NAME = 'Stop Motion Booth'
SESSIONS_DIR = Path('sessions')
SAVED_DIR = Path('saved_movies')
CLOUD_DIR = Path('cloud_outbox')   # external uploader picks files up from here

SERIAL_BAUD = 9600
DIGICAM_DEFAULT_URL = 'http://localhost:5513'
DIGICAM_CAPTURE_TIMEOUT_S = 20.0
LOG_FILE = Path('booth.log')


def _setup_logging():
    fmt = '%(asctime)s %(levelname)-7s %(message)s'
    logging.basicConfig(
        level=logging.DEBUG,
        format=fmt,
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )

log = logging.getLogger('booth')


# ---- Button event source ----------------------------------------------------

class ButtonSource:
    """Reads single-char button events from Arduino, or the keyboard if no port."""

    KEY_MAP = {
        ord('g'): 'G', ord('r'): 'R', ord('b'): 'B',
        ord('w'): 'W', ord('y'): 'Y',
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
                    if c in 'GRBWY':
                        return c
        if key != -1 and key in self.KEY_MAP:
            return self.KEY_MAP[key]
        return None

    def close(self):
        if self.ser is not None:
            self.ser.close()


# ---- Capture backends -------------------------------------------------------

class WebcamBackend:
    """OpenCV USB webcam (used for testing without the DSLR)."""

    def __init__(self, index: int = 0):
        flag = cv2.CAP_DSHOW if os.name == 'nt' else 0
        self.cap = cv2.VideoCapture(index, flag)
        if not self.cap.isOpened():
            raise RuntimeError(f'Could not open camera index {index}')
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    def read_preview(self) -> np.ndarray | None:
        ok, frame = self.cap.read()
        return frame if ok else None

    def capture(self, stem: Path) -> Path | None:
        ok, frame = self.cap.read()
        if not ok:
            return None
        path = stem.with_suffix('.jpg')
        cv2.imwrite(str(path), frame)
        return path

    def release(self):
        self.cap.release()


_HTML_TAG_RE = re.compile(r'<[^>]+>')


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub('', text).strip()


class DigiCamBackend:
    """Canon / Nikon / Sony DSLR via digiCamControl HTTP single-line commands.

    Docs: http://digicamcontrol.com/doc/userguide/web (Webserver + SLC).
    The webserver must be enabled in digiCamControl settings.
    """

    def __init__(self, base_url: str = DIGICAM_DEFAULT_URL):
        if requests is None:
            raise RuntimeError('requests not installed; run: pip install requests')
        self.base = base_url.rstrip('/')
        log.info('DigiCam connecting to %s', self.base)
        result = self._slc({'slc': 'list', 'param1': 'cameras'})  # raises if unreachable
        log.info('DigiCam cameras: %s', result)
        self._try_start_liveview()

    def _slc(self, params: dict, timeout: float = 5) -> str:
        log.debug('DigiCam SLC request: %s', params)
        r = requests.get(self.base, params=params, timeout=timeout)
        log.debug('DigiCam SLC response: HTTP %s — %s', r.status_code, r.text[:200])
        r.raise_for_status()
        return _strip_html(r.text)

    def _try_start_liveview(self):
        # Best-effort; different digiCamControl versions expose slightly different
        # verbs. Either of these will start live view on modern builds.
        for params in (
            {'slc': 'do', 'param1': 'LiveViewWnd_Show'},
            {'slc': 'LiveViewWnd_Show'},
        ):
            try:
                self._slc(params)
                log.info('Live view started with params: %s', params)
                break
            except Exception as e:
                log.debug('Live view attempt failed (%s): %s', params, e)
                continue

    def _get_last_captured(self) -> str:
        try:
            result = self._slc({'slc': 'get', 'param1': 'lastcaptured'})
            return result
        except Exception as e:
            log.warning('_get_last_captured failed: %s', e)
            return ''

    def read_preview(self) -> np.ndarray | None:
        try:
            r = requests.get(f'{self.base}/liveview.jpg', timeout=1.5)
            if r.status_code != 200 or not r.content:
                log.debug('Live view frame unavailable: HTTP %s, %d bytes',
                          r.status_code, len(r.content))
                return None
            arr = np.frombuffer(r.content, dtype=np.uint8)
            return cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except Exception as e:
            log.debug('read_preview exception: %s', e)
            return None

    def capture(self, stem: Path) -> Path | None:
        before = self._get_last_captured()
        log.info('Capture triggered — lastcaptured before: %s', before)
        try:
            # Use a short timeout — DigiCamControl blocks the response while it
            # processes the image, but the shutter fires immediately. We don't
            # need the response; we detect the capture by polling lastcaptured.
            self._slc({'slc': 'capture'}, timeout=0.5)
        except requests.exceptions.Timeout:
            log.debug('Capture SLC timed out as expected — polling for result')
        except Exception as e:
            log.error('Capture SLC call failed: %s', e)
            return None
        deadline = time.time() + DIGICAM_CAPTURE_TIMEOUT_S
        while time.time() < deadline:
            current = self._get_last_captured()
            if current and current != before:
                log.info('New capture detected: %s', current)
                src = Path(current)
                if not src.is_absolute():
                    # DigiCamControl sometimes returns just a filename — search
                    # the session download folder for it.
                    session_folder = self._get_session_folder()
                    if session_folder:
                        candidate = Path(session_folder) / src.name
                        log.debug('Relative path received, trying session folder: %s', candidate)
                        src = candidate
                if src.exists():
                    dest = stem.with_suffix(src.suffix.lower() or '.jpg')
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.move(str(src), str(dest))
                    except OSError:
                        shutil.copy2(src, dest)
                    log.info('Frame saved to %s', dest)
                    return dest
                else:
                    log.warning('Capture path reported but file not found: %s', src)
            time.sleep(0.15)
        log.error('Capture timed out after %.0fs — no new file appeared', DIGICAM_CAPTURE_TIMEOUT_S)
        return None

    def _get_session_folder(self) -> str:
        try:
            result = self._slc({'slc': 'get', 'param1': 'session.folder'})
            log.debug('Session folder: %s', result)
            return result
        except Exception as e:
            log.warning('Could not get session.folder: %s', e)
            return ''

    def lock_exposure(self):
        """Read current ISO/shutter/aperture and pin them so auto-exposure stops."""
        props = [
            ('camera.isocurrent',          'camera.iso'),
            ('camera.shutterspeedcurrent', 'camera.shutter'),
            ('camera.aperturecurrent',     'camera.aperture'),
        ]
        for get_param, set_param in props:
            try:
                value = self._slc({'slc': 'get', 'param1': get_param})
                if value:
                    self._slc({'slc': 'set', 'param1': set_param, 'param2': value})
                    log.info('Exposure locked — %s = %s', set_param, value)
            except Exception as e:
                log.warning('Could not lock %s: %s', set_param, e)

    def release(self):
        try:
            self._slc({'slc': 'do', 'param1': 'LiveViewWnd_Hide'})
            log.info('Live view hidden')
        except Exception as e:
            log.debug('release exception: %s', e)


# ---- Session state ----------------------------------------------------------

class Session:
    """One animation session: a folder of numbered image files."""

    def __init__(self, root: Path):
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.dir = root / stamp
        self.dir.mkdir(parents=True, exist_ok=True)
        self.frames: list[Path] = []
        self._preview_cache: dict[tuple[str, int], np.ndarray] = {}

    def next_stem(self) -> Path:
        return self.dir / f'frame_{len(self.frames):04d}'

    def register(self, path: Path):
        self.frames.append(path)

    def pop(self) -> Path | None:
        if not self.frames:
            return None
        path = self.frames.pop()
        self._preview_cache = {k: v for k, v in self._preview_cache.items()
                               if k[0] != str(path)}
        try:
            path.unlink()
        except OSError:
            pass
        return path

    def last_frame_scaled(self, target_w: int) -> np.ndarray | None:
        """Return last captured frame downscaled to roughly target_w wide."""
        if not self.frames:
            return None
        p = self.frames[-1]
        key = (str(p), target_w)
        cached = self._preview_cache.get(key)
        if cached is not None:
            return cached
        img = cv2.imread(str(p))
        if img is None:
            return None
        h, w = img.shape[:2]
        if w > target_w:
            img = cv2.resize(img, (target_w, int(h * target_w / w)))
        # Keep the cache tiny — we only ever want the most recent frame cached.
        self._preview_cache.clear()
        self._preview_cache[key] = img
        return img

    def cleanup_if_empty(self):
        if not self.frames and self.dir.exists():
            try:
                self.dir.rmdir()
            except OSError:
                pass


# ---- Rendering helpers ------------------------------------------------------

BUTTON_LEGEND = [
    ('GREEN',  ( 50, 200,  50), 'TAKE PICTURE'),
    ('RED',    ( 50,  50, 220), 'DELETE LAST'),
    ('BLUE',   (220, 100,  50), 'PLAY MOVIE'),
    ('WHITE',  (220, 220, 220), 'SAVE'),
    ('YELLOW', ( 50, 220, 220), 'ONION SKIN'),
]


def draw_hud(frame: np.ndarray, frames_count: int, onion: bool,
             capturing: bool) -> np.ndarray:
    out = frame.copy()
    h, w = out.shape[:2]

    # Top bar — two rows: button legend + status
    cv2.rectangle(out, (0, 0), (w, 110), (0, 0, 0), -1)

    # Row 1: button legend spread across the width
    n = len(BUTTON_LEGEND)
    col_w = w // n
    for i, (label, color, action) in enumerate(BUTTON_LEGEND):
        x = i * col_w + 10
        cv2.putText(out, label, (x, 32),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        cv2.putText(out, action, (x, 58),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (200, 200, 200), 1)

    # Row 2: frame count and onion state
    cv2.putText(out, f'Frames: {frames_count}', (20, 95),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (255, 255, 255), 2)
    if onion:
        cv2.putText(out, 'ONION ON', (w - 180, 95),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 220, 255), 2)

    if capturing:
        cv2.putText(out, 'CAPTURING...', (w // 2 - 200, h // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.8, (0, 0, 255), 5)
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
    if first is None:
        return None
    h0, w0 = first.shape[:2]
    if w0 > MOVIE_WIDTH:
        scale = MOVIE_WIDTH / w0
        out_w, out_h = MOVIE_WIDTH, int(h0 * scale)
    else:
        out_w, out_h = w0, h0

    out_path = session.dir / 'movie.mp4'
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    writer = cv2.VideoWriter(str(out_path), fourcc, FPS, (out_w, out_h))
    for p in session.frames:
        img = cv2.imread(str(p))
        if img is None:
            continue
        if img.shape[1] != out_w or img.shape[0] != out_h:
            img = cv2.resize(img, (out_w, out_h))
        writer.write(img)
    writer.release()
    return out_path


def play_movie(path: Path, buttons: ButtonSource) -> None:
    """Play movie looping until any button press (or q)."""
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
            h, w = frame.shape[:2]
            cv2.rectangle(frame, (0, h - 50), (w, h), (0, 0, 0), -1)
            cv2.putText(frame, 'PUSH ANY BUTTON TO STOP',
                        (w // 2 - 260, h - 14),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
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

def make_backend(kind: str, camera_index: int, digicam_url: str):
    if kind == 'webcam':
        return WebcamBackend(camera_index)
    if kind == 'digicam':
        return DigiCamBackend(digicam_url)
    raise ValueError(f'Unknown backend: {kind}')


def run(backend_kind: str, camera_index: int, digicam_url: str,
        port: str | None, fullscreen: bool) -> int:
    _setup_logging()
    log.info('Starting booth — backend=%s port=%s url=%s', backend_kind, port, digicam_url)
    SESSIONS_DIR.mkdir(exist_ok=True)
    SAVED_DIR.mkdir(exist_ok=True)
    CLOUD_DIR.mkdir(exist_ok=True)

    backend = make_backend(backend_kind, camera_index, digicam_url)
    buttons = ButtonSource(port)

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    if fullscreen:
        cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN,
                              cv2.WINDOW_FULLSCREEN)

    session = Session(SESSIONS_DIR)
    onion = False
    flash_until = 0.0
    pending_save: Path | None = None
    capture_state = {'in_flight': False, 'last_preview': None}

    def do_capture():
        try:
            path = backend.capture(session.next_stem())
            if path is not None:
                session.register(path)
                if len(session.frames) == 1 and hasattr(backend, 'lock_exposure'):
                    backend.lock_exposure()
        finally:
            capture_state['in_flight'] = False

    try:
        while True:
            live = backend.read_preview()
            if live is None:
                # Fall back to the last good preview so the UI doesn't flicker to black
                # (happens briefly during DSLR capture when the mirror is up).
                live = capture_state['last_preview']
                if live is None:
                    time.sleep(0.05)
                    # give the UI a chance to pump events so window stays responsive
                    cv2.waitKey(1)
                    continue
            else:
                capture_state['last_preview'] = live.copy()

            prev = session.last_frame_scaled(live.shape[1]) if onion else None
            display = apply_onion(live, prev)
            display = draw_hud(display, len(session.frames), onion,
                               capture_state['in_flight'])

            if pending_save is not None:
                display = big_message(display, [
                    'Save this movie?',
                    'WHITE = yes    YELLOW = keep working',
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
                if event == 'W':
                    saved = SAVED_DIR / f'{session.dir.name}.mp4'
                    shutil.copy2(pending_save, saved)
                    shutil.copy2(pending_save, CLOUD_DIR / saved.name)
                    print(f'Saved {saved} (queued for cloud upload)')
                    pending_save = None
                    session.cleanup_if_empty()
                    session = Session(SESSIONS_DIR)
                    onion = False
                elif event == 'Y':
                    pending_save = None
                continue

            # Block further input while a capture is in flight — DSLR takes ~1-2s
            # per shot and we don't want a second press to queue up.
            if capture_state['in_flight']:
                continue

            if event == 'G':
                capture_state['in_flight'] = True
                flash_until = time.time() + 0.12
                threading.Thread(target=do_capture, daemon=True).start()
            elif event == 'R':
                session.pop()
            elif event == 'Y':
                onion = not onion
            elif event == 'B':
                movie = build_movie(session)
                if movie is not None:
                    play_movie(movie, buttons)
                    pending_save = movie

    finally:
        buttons.close()
        try:
            backend.release()
        except Exception:
            pass
        cv2.destroyAllWindows()
        session.cleanup_if_empty()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description='Stop-motion animation booth')
    p.add_argument('--backend', choices=('digicam', 'webcam'), default='digicam',
                   help='Capture backend (default: digicam)')
    p.add_argument('--camera', type=int, default=0,
                   help='OpenCV camera index (webcam backend only)')
    p.add_argument('--digicam-url', default=DIGICAM_DEFAULT_URL,
                   help=f'digiCamControl webserver URL (default {DIGICAM_DEFAULT_URL})')
    p.add_argument('--port', default='none',
                   help='Arduino serial port (e.g. COM3) or "none" for keyboard')
    p.add_argument('--windowed', action='store_true', help='Disable fullscreen')
    args = p.parse_args()
    return run(args.backend, args.camera, args.digicam_url, args.port,
               fullscreen=not args.windowed)


if __name__ == '__main__':
    sys.exit(main())
