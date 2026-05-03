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
import colorsys
import logging
import math
import os
import re
import shutil
import subprocess
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
TRASH_DIR = SAVED_DIR / 'trash'    # deleted movies land here instead of being permanently removed
CLOUD_DIR = Path('cloud_outbox')   # external uploader picks files up from here

SERIAL_BAUD = 9600
DIGICAM_DEFAULT_URL = 'http://localhost:5513'
DIGICAM_CAPTURE_TIMEOUT_S = 20.0
DIGICAM_EXE = Path('C:/Program Files (x86)/digiCamControl/CameraControl.exe')
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
        self._last_event: str | None = None
        self._last_event_time: float = 0.0
        if port and port.lower() != 'none':
            if serial is None:
                raise RuntimeError('pyserial not installed; run: pip install pyserial')
            self.ser = serial.Serial(port, SERIAL_BAUD, timeout=0)
            time.sleep(2.0)  # allow Arduino reset
            self.ser.reset_input_buffer()

    def poll(self, key: int) -> str | None:
        """Return one of G/R/B/W/Y/EXIT, or None. Call once per frame.

        EXIT is returned when R and Y are pressed together (simultaneously in
        the same serial read, or sequentially within 300 ms of each other).
        """
        events: list[str] = []

        if self.ser is not None:
            data = self.ser.read(64)
            for ch in data:
                c = chr(ch).upper()
                if c in 'GRBWY':
                    events.append(c)

        if key != -1 and key in self.KEY_MAP:
            events.append(self.KEY_MAP[key])

        if not events:
            return None

        now = time.time()
        has_r = 'R' in events
        has_y = 'Y' in events
        recent_r = self._last_event == 'R' and now - self._last_event_time < 0.3
        recent_y = self._last_event == 'Y' and now - self._last_event_time < 0.3

        if (has_r and has_y) or (has_r and recent_y) or (has_y and recent_r):
            self._last_event = None
            return 'EXIT'

        event = events[0]
        self._last_event = event
        self._last_event_time = now
        return event

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
        self._lock = threading.Lock()   # cv2.VideoCapture is not thread-safe

    def read_preview(self) -> np.ndarray | None:
        with self._lock:
            ok, frame = self.cap.read()
        return frame if ok else None

    def capture(self, stem: Path) -> Path | None:
        with self._lock:
            ok, frame = self.cap.read()
        if not ok:
            return None
        path = stem.with_suffix('.jpg')
        cv2.imwrite(str(path), frame)
        return path

    def lock_exposure(self):
        """Disable auto-exposure so brightness stays constant across frames.
        DirectShow (Windows): 0.25 = manual mode. V4L2 (Linux): 1 = manual.
        This is driver-dependent and may have no effect on some cameras."""
        manual_val = 0.25 if os.name == 'nt' else 1
        result = self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, manual_val)
        wb_result = self.cap.set(cv2.CAP_PROP_AUTO_WB, 0)
        log.info('Webcam: auto-exposure lock attempted (set=%s, wb=%s)', result, wb_result)

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
        # Separate persistent sessions so SLC commands and liveview fetches
        # never block each other, and TCP connections are reused.
        self._slc_session = requests.Session()
        self._slc_lock = threading.Lock()   # SLC session shared across threads
        self._lv_session = requests.Session()  # used only by preview thread
        self.lv_blank = False               # set by read_preview(); True = server up but no frame
        log.info('DigiCam connecting to %s', self.base)
        result = self._slc({'slc': 'list', 'param1': 'cameras'})  # raises if unreachable
        log.info('DigiCam cameras: %s', result)
        self._try_start_liveview()

    def _slc(self, params: dict, timeout: float = 2.0) -> str:
        log.debug('DigiCam SLC request: %s', params)
        with self._slc_lock:
            r = self._slc_session.get(self.base, params=params, timeout=timeout)
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

    def _get_last_captured(self, timeout: float = 1.0) -> tuple[str, bool]:
        """Return (path, server_ok). path is '' when unknown; server_ok=False on connection failure."""
        try:
            result = self._slc({'slc': 'get', 'param1': 'lastcaptured'}, timeout=timeout)
            return result, True
        except Exception as e:
            log.warning('_get_last_captured failed: %s', e)
            return '', False

    def read_preview(self) -> np.ndarray | None:
        """Return a decoded liveview frame, or None on failure.

        Sets self.lv_blank=True when the server responded but sent no image
        (liveview window inactive), False when the server was unreachable.
        """
        try:
            r = self._lv_session.get(f'{self.base}/liveview.jpg', timeout=0.5)
            if r.status_code != 200 or not r.content:
                log.debug('Live view frame unavailable: HTTP %s, %d bytes',
                          r.status_code, len(r.content))
                self.lv_blank = True
                return None
            arr = np.frombuffer(r.content, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            self.lv_blank = False
            return frame
        except Exception as e:
            log.debug('read_preview exception: %s', e)
            self.lv_blank = False  # server unreachable, not a blank frame
            return None

    def capture(self, stem: Path) -> Path | None:
        before, _ = self._get_last_captured()
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
        consecutive_conn_fails = 0
        while time.time() < deadline:
            current, server_ok = self._get_last_captured(timeout=1.0)
            if not server_ok:
                consecutive_conn_fails += 1
                if consecutive_conn_fails >= 4:
                    log.error('Camera server unreachable for %d consecutive polls — aborting',
                              consecutive_conn_fails)
                    return None
            else:
                consecutive_conn_fails = 0
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
            time.sleep(0.08)
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
        """Switch the camera to Manual exposure mode and pin ISO/shutter/aperture.

        Setting individual exposure values has no effect while the camera body
        is in an Auto or Program mode — the metering system overrides them every
        shot.  We therefore request Manual mode first, then write the values.

        If the body doesn't allow software exposure-mode changes (some Canon
        entry-level bodies require the physical dial), the set command will fail
        silently and we log a warning.  In that case, turn the dial to M before
        starting the booth.
        """
        # Step 1 — switch to Manual exposure program
        try:
            current_mode = self._slc({'slc': 'get', 'param1': 'camera.exposuremode'})
            log.info('Exposure mode before lock: %s', current_mode)
        except Exception as e:
            log.debug('Could not read exposuremode: %s', e)
            current_mode = ''

        if current_mode != 'M':
            try:
                self._slc({'slc': 'set', 'param1': 'camera.exposuremode', 'param2': 'M'})
                log.info('Exposure mode set to Manual (M)')
            except Exception as e:
                log.warning(
                    'Could not set exposure mode to Manual (%s). '
                    'If brightness keeps changing, turn the camera dial to M.',
                    e,
                )

        # Step 2 — read the current metered values and pin them
        props = [
            ('camera.isocurrent',          'camera.iso'),
            ('camera.shutterspeedcurrent', 'camera.shutter'),
            ('camera.aperturecurrent',     'camera.aperture'),
        ]
        locked: list[str] = []
        for get_param, set_param in props:
            try:
                value = self._slc({'slc': 'get', 'param1': get_param})
                if value:
                    self._slc({'slc': 'set', 'param1': set_param, 'param2': value})
                    locked.append(f'{set_param.split(".")[-1]}={value}')
                    log.info('Exposure locked — %s = %s', set_param, value)
            except Exception as e:
                log.warning('Could not lock %s: %s', set_param, e)

        if locked:
            log.info('Exposure lock complete: %s', '  '.join(locked))
        else:
            log.warning('Exposure lock: no values were set — '
                        'check booth.log and ensure the camera is in Manual (M) mode')

    def try_reconnect(self) -> bool:
        """Kill and restart digiCamControl, then restore live view and exposure lock.

        Called from the preview background thread when the server stops responding.
        Safe to call concurrently — all HTTP calls go through the existing locks.
        Returns True on success, False if the exe is missing or the server
        doesn't come back within 30 s.
        """
        log.warning('DigiCam reconnect: killing CameraControl.exe')
        subprocess.run(
            ['taskkill', '/F', '/IM', 'CameraControl.exe'],
            capture_output=True,
        )
        time.sleep(1.0)

        if not DIGICAM_EXE.exists():
            log.error('DigiCam reconnect: exe not found at %s', DIGICAM_EXE)
            return False

        subprocess.Popen([str(DIGICAM_EXE)])
        log.info('DigiCam reconnect: started %s — waiting for HTTP server', DIGICAM_EXE.name)

        # Poll until the SLC endpoint responds (up to 30 s)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                r = self._lv_session.get(
                    self.base,
                    params={'slc': 'list', 'param1': 'cameras'},
                    timeout=2.0,
                )
                if r.ok:
                    break
            except Exception:
                pass
            time.sleep(2.0)
        else:
            log.error('DigiCam reconnect: server did not respond within 30 s')
            return False

        # Give digiCamControl a moment to enumerate the camera before sending commands
        time.sleep(2.0)
        log.info('DigiCam reconnect: server up — restoring live view and exposure lock')
        self._try_start_liveview()
        self.lock_exposure()
        return True

    def release(self):
        try:
            self._slc({'slc': 'do', 'param1': 'LiveViewWnd_Hide'})
            log.info('Live view hidden')
        except Exception as e:
            log.debug('release exception: %s', e)
        self._slc_session.close()
        self._lv_session.close()


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
    cv2.rectangle(overlay, (0, h // 2 - 70), (w, h // 2 + 70), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.7, out, 0.3, 0, out)
    for i, line in enumerate(lines):
        size = cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)[0]
        x = (w - size[0]) // 2
        y = h // 2 + (i - len(lines) / 2 + 0.7) * 38
        cv2.putText(out, line, (x, int(y)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
    return out


# ---- Launch / attract screen ------------------------------------------------

from PIL import Image, ImageDraw, ImageFont  # noqa: E402  (Pillow required dep)

_FONTS_DIR = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'

# RGB colors (PIL uses RGB; OpenCV uses BGR — convert at the boundary)
_LAUNCH_BUTTONS = [
    ('GREEN',  ( 55, 200,  55), 'Take a picture'),
    ('BLUE',   ( 55, 130, 220), 'Build & play movie'),
    ('RED',    (220,  55,  55), 'Delete last frame'),
    ('YELLOW', (220, 195,  50), 'Toggle onion skin'),
    ('WHITE',  (210, 210, 210), 'Save movie'),
]

# Video preview panel dimensions and positions (2x2 grid on splash screen)
_PREV_W, _PREV_H = 258, 145          # 16:9 thumbnail (slightly smaller for 4-up layout)
_PREV_POSITIONS = [
    (80,  248),   # top-left
    (942, 248),   # top-right
    (80,  453),   # bottom-left
    (942, 453),   # bottom-right
]


def _ttf(name: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a TrueType font, falling back through candidates to PIL default."""
    for candidate in (name, 'segoeuib.ttf', 'arialbd.ttf', 'arial.ttf'):
        p = _FONTS_DIR / candidate
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                continue
    return ImageFont.load_default(size=size)


# Base saturation / value for border rainbow elements.
# Kept below 1.0 so the runtime pulse has room to boost up to full vividness.
_STRIP_S = 0.85
_STRIP_V = 0.72


def _draw_filmstrip(draw: ImageDraw.ImageDraw, y: int, w: int, h: int = 50) -> None:
    """Horizontal filmstrip band with rainbow-coloured rounded sprocket holes."""
    draw.rectangle([(0, y), (w, y + h)], fill=(8, 8, 8))
    hole_w, hole_h, gap = 22, 32, 38
    cx_start = gap // 2
    cy = y + h // 2
    x = cx_start
    while x + hole_w <= w:
        hue = ((x + hole_w / 2) / w) * (270 / 360)
        r, g, b = colorsys.hsv_to_rgb(hue, _STRIP_S, _STRIP_V)
        fill = (int(r * 255), int(g * 255), int(b * 255))
        draw.rounded_rectangle(
            [(x, cy - hole_h // 2), (x + hole_w, cy + hole_h // 2)],
            radius=5, fill=fill,
        )
        x += gap


def _draw_vertical_strip(draw: ImageDraw.ImageDraw, x: int, h: int, w: int = 50) -> None:
    """Vertical filmstrip band with rainbow 5-point stars top to bottom."""
    draw.rectangle([(x, 0), (x + w, h)], fill=(8, 8, 8))
    sz  = 11
    gap = 38
    cx  = x + w // 2
    y   = sz
    while y + sz <= h:
        hue = (y / h) * (270 / 360)
        r, g, b = colorsys.hsv_to_rgb(hue, _STRIP_S, _STRIP_V)
        fill = (int(r * 255), int(g * 255), int(b * 255))
        pts = []
        for i in range(10):
            a  = math.pi * i / 5 - math.pi / 2
            r2 = sz if i % 2 == 0 else sz * 0.42
            pts.append((cx + r2 * math.cos(a), y + r2 * math.sin(a)))
        draw.polygon(pts, fill=fill)
        y += gap


def _build_launch_base(show_prompt: bool) -> np.ndarray:
    """Render the static layer of the launch screen as a BGR numpy array.

    Video frames are composited on top of this in the animation loop.
    """
    W, H = 1280, 720
    STRIP = 50

    img = Image.new('RGB', (W, H), (18, 18, 30))
    draw = ImageDraw.Draw(img)

    # Horizontal filmstrip bands
    _draw_filmstrip(draw, 0,        W, STRIP)
    _draw_filmstrip(draw, H - STRIP, W, STRIP)

    # Vertical strips drawn after so stars reach the top and bottom edges
    _draw_vertical_strip(draw, 0,        H, STRIP)
    _draw_vertical_strip(draw, W - STRIP, H, STRIP)

    # Header band behind title — stays between the vertical strips and matches background
    draw.rectangle([(STRIP, STRIP), (W - STRIP, STRIP + 148)], fill=(18, 18, 30))

    # Title
    font_title = _ttf('impact.ttf', 88)
    draw.text((W // 2, STRIP + 16), 'Movie Making Booth',
              font=font_title, fill=(255, 200, 40), anchor='mt')

    # Thumbnails flanking the title — cropped square frames from assets/
    _THUMB_SIZE = 140
    _THUMB_GAP  = 14
    title_bb = draw.textbbox((W // 2, STRIP + 16), 'Movie Making Booth',
                             font=font_title, anchor='mt')
    thumb_y = (STRIP + STRIP + 148) // 2 - _THUMB_SIZE // 2
    for side, tpath in enumerate([Path('assets/title_thumb_0.jpg'),
                                   Path('assets/title_thumb_1.jpg')]):
        if not tpath.exists():
            continue
        try:
            t = Image.open(str(tpath)).convert('RGB').resize((_THUMB_SIZE, _THUMB_SIZE), Image.LANCZOS)
        except Exception:
            continue
        tx = (title_bb[0] - _THUMB_GAP - _THUMB_SIZE) if side == 0 else (title_bb[2] + _THUMB_GAP)
        tx = max(STRIP + 6, min(tx, W - STRIP - 6 - _THUMB_SIZE))
        img.paste(t, (tx, thumb_y))
        draw.rounded_rectangle(
            [(tx - 3, thumb_y - 3), (tx + _THUMB_SIZE + 3, thumb_y + _THUMB_SIZE + 3)],
            radius=6, outline=(90, 95, 125), width=3,
        )

    # Subtitle
    font_sub = _ttf('segoeui.ttf', 34)
    draw.text((W // 2, STRIP + 118), "Harding Kid's Stop Motion Animation Studio",
              font=font_sub, fill=(165, 175, 210), anchor='mt')

    # Divider
    div_y = STRIP + 172
    draw.line([(130, div_y), (W - 130, div_y)], fill=(72, 72, 95), width=2)

    # Video panel placeholders — dark wells with a subtle border
    font_ph = _ttf('segoeui.ttf', 18)
    for (vx, vy) in _PREV_POSITIONS:
        draw.rectangle([(vx, vy), (vx + _PREV_W, vy + _PREV_H)], fill=(8, 10, 18))
        draw.rounded_rectangle(
            [(vx - 3, vy - 3), (vx + _PREV_W + 3, vy + _PREV_H + 3)],
            radius=6, outline=(72, 78, 105), width=3,
        )
        draw.text(
            (vx + _PREV_W // 2, vy + _PREV_H // 2),
            'No movies yet', font=font_ph, fill=(52, 58, 78), anchor='mm',
        )

    # Button legend — colored circle + bold label + grey action text
    font_lbl   = _ttf('segoeuib.ttf', 28)
    font_act   = _ttf('segoeui.ttf',  28)
    CIRCLE_R   = 11
    CIRCLE_GAP = 18
    COL_GAP    = 14

    label_max_w = max(
        draw.textbbox((0, 0), lbl, font=font_lbl)[2]
        - draw.textbbox((0, 0), lbl, font=font_lbl)[0]
        for lbl, _, _ in _LAUNCH_BUTTONS
    )
    action_max_w = max(
        draw.textbbox((0, 0), act, font=font_act)[2]
        - draw.textbbox((0, 0), act, font=font_act)[0]
        for _, _, act in _LAUNCH_BUTTONS
    )
    row_w = 2 * CIRCLE_R + CIRCLE_GAP + label_max_w + COL_GAP + action_max_w
    x0 = (W - row_w) // 2

    for i, (label, color_rgb, action) in enumerate(_LAUNCH_BUTTONS):
        y_top     = div_y + 20 + i * 56
        circle_cy = y_top + 18

        draw.ellipse(
            [(x0, circle_cy - CIRCLE_R), (x0 + 2 * CIRCLE_R, circle_cy + CIRCLE_R)],
            fill=color_rgb,
        )
        draw.text((x0 + 2 * CIRCLE_R + CIRCLE_GAP, y_top),
                  label, font=font_lbl, fill=color_rgb, anchor='lt')
        draw.text((x0 + 2 * CIRCLE_R + CIRCLE_GAP + label_max_w + COL_GAP, y_top),
                  action, font=font_act, fill=(155, 160, 178), anchor='lt')

    # Blinking prompt
    if show_prompt:
        font_prompt = _ttf('segoeuib.ttf', 40)
        draw.text((W // 2, H - STRIP - 28), 'GREEN = new movie',
                  font=font_prompt, fill=(50, 215, 65), anchor='mb')
        font_hint = _ttf('segoeui.ttf', 30)
        draw.text((W // 2, H - STRIP - 4), 'BLUE = browse saved movies',
                  font=font_hint, fill=(80, 140, 230), anchor='mb')

    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _open_preview_videos() -> list[tuple[cv2.VideoCapture, tuple[int, int]]]:
    """Return up to 4 VideoCapture objects paired with their (x, y) positions.

    Picks the four most recently modified files from saved_movies/.
    """
    if not SAVED_DIR.exists():
        return []
    candidates = sorted(
        SAVED_DIR.glob('*.mp4'),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[:4]
    result = []
    for path, pos in zip(candidates, _PREV_POSITIONS):
        cap = cv2.VideoCapture(str(path))
        if cap.isOpened():
            result.append((cap, pos))
        else:
            cap.release()
    return result


def browse_saved_movies(buttons: ButtonSource) -> bool:
    """Browse and play saved movies from the menu screen.

    BLUE = older movie, WHITE = newer movie, RED/GREEN = back to menu.
    Returns False if the EXIT combo was pressed (caller should quit).
    """
    movies = sorted(
        [p for p in SAVED_DIR.glob('*.mp4') if p.parent == SAVED_DIR],
        key=lambda p: p.stat().st_mtime,
        reverse=True,  # index 0 = newest
    )

    if not movies:
        bg = np.zeros((720, 1280, 3), dtype=np.uint8)
        msg = 'No saved movies yet'
        sz = cv2.getTextSize(msg, cv2.FONT_HERSHEY_SIMPLEX, 1.2, 2)[0]
        cv2.putText(bg, msg, ((1280 - sz[0]) // 2, 360),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, (150, 150, 150), 2)
        cv2.imshow(WINDOW_NAME, bg)
        cv2.waitKey(1800)
        return True

    idx = 0
    delay = max(1, int(1000 / FPS))

    while True:
        path = movies[idx]
        n = len(movies)
        age = 'newest' if idx == 0 else ('oldest' if idx == n - 1 else '')
        label = f'Movie {idx + 1} of {n}' + (f'  ({age})' if age else '')
        hint  = 'BLUE=older   WHITE=newer   GREEN / RED=menu'

        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            # skip bad file
            if n == 1:
                return True
            idx = min(idx + 1, n - 1)
            continue

        nav = None
        while nav is None:
            ok, frame = cap.read()
            if not ok:
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                ok, frame = cap.read()
            if not ok:
                break

            h, w = frame.shape[:2]
            cv2.rectangle(frame, (0, h - 54), (w, h), (0, 0, 0), -1)
            cv2.putText(frame, label, (20, h - 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.75, (200, 200, 200), 2)
            sz = cv2.getTextSize(hint, cv2.FONT_HERSHEY_SIMPLEX, 0.65, 1)[0]
            cv2.putText(frame, hint, (w - sz[0] - 20, h - 18),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (140, 140, 140), 1)

            cv2.imshow(WINDOW_NAME, frame)
            key = cv2.waitKey(delay) & 0xFF
            key = -1 if key == 255 else key
            if key == ord('q'):
                nav = 'menu'
                break
            event = buttons.poll(key)
            if event == 'EXIT':
                nav = 'exit'
            elif event == 'B':
                nav = 'older'
            elif event == 'W':
                nav = 'newer'
            elif event in ('R', 'G'):
                nav = 'menu'

        cap.release()

        if nav == 'exit':
            return False
        if nav == 'menu':
            return True
        if nav == 'older':
            idx = min(idx + 1, n - 1)
        elif nav == 'newer':
            idx = max(idx - 1, 0)


def _apply_border_pulse(frame: np.ndarray, t: float) -> np.ndarray:
    """Boost saturation and value of border-strip pixels with a moving Gaussian pulse.

    The static base renders elements at (_STRIP_S, _STRIP_V).  At the peak of the
    pulse those values are boosted to (1.0, 1.0), making the colors fully vivid.
    Horizontal pulse sweeps left→right every 2 s; vertical sweeps top→bottom
    every 2 s with a 1 s phase offset so the two axes don't fire together.
    """
    out = frame.copy()
    H, W = out.shape[:2]
    STRIP = 50
    SIGMA = 90.0   # glow half-width in pixels
    # Multipliers that lift base S/V to 1.0 at peak (glow=1)
    BOOST_S = 1.0 / _STRIP_S   # ≈ 1.43
    BOOST_V = 1.0 / _STRIP_V   # ≈ 2.00

    def _boost(region_bgr: np.ndarray, glow_1d: np.ndarray,
                axis: int) -> np.ndarray:
        """Apply per-column (axis=1) or per-row (axis=0) HSV boost."""
        hsv = cv2.cvtColor(region_bgr, cv2.COLOR_BGR2HSV).astype(np.float32)
        g = glow_1d if axis == 1 else glow_1d[:, np.newaxis]
        # S channel: lerp from 1.0 to BOOST_S by glow weight
        s_mult = 1.0 + g * (BOOST_S - 1.0)
        # V channel: lerp from 1.0 to BOOST_V by glow weight
        v_mult = 1.0 + g * (BOOST_V - 1.0)
        if axis == 1:   # horizontal strip: glow shape (W,) → broadcast over rows
            s_mult = s_mult[np.newaxis, :]
            v_mult = v_mult[np.newaxis, :]
        hsv[:, :, 1] = np.clip(hsv[:, :, 1] * s_mult, 0, 255)
        hsv[:, :, 2] = np.clip(hsv[:, :, 2] * v_mult, 0, 255)
        return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)

    # ---- horizontal pulse (top + bottom strips) ----
    px = (t % 2.0) / 2.0 * W
    xs = np.arange(W, dtype=np.float32)
    h_glow = np.exp(-((xs - px) ** 2) / (2.0 * SIGMA ** 2))   # (W,)

    out[:STRIP]      = _boost(out[:STRIP],      h_glow, axis=1)
    out[H - STRIP:]  = _boost(out[H - STRIP:],  h_glow, axis=1)

    # ---- vertical pulse (left + right strips), 1 s offset ----
    py = ((t + 1.0) % 2.0) / 2.0 * H
    ys = np.arange(H, dtype=np.float32)
    v_glow = np.exp(-((ys - py) ** 2) / (2.0 * SIGMA ** 2))   # (H,)

    out[:, :STRIP]      = _boost(out[:, :STRIP],      v_glow, axis=0)
    out[:, W - STRIP:]  = _boost(out[:, W - STRIP:],  v_glow, axis=0)

    return out


def show_launch_screen(buttons: ButtonSource) -> bool:
    """Display the attract screen until any button is pressed (or q to quit).

    Returns True to continue, False if the user pressed q.
    Saved movies play on the left and right sides of the screen.
    """
    base_on  = _build_launch_base(show_prompt=True)
    base_off = _build_launch_base(show_prompt=False)

    caps = _open_preview_videos()
    # Most-recent decoded frame per video slot (reused until next video tick)
    cur_vframes: list[np.ndarray | None] = [None] * len(caps)

    blink_on    = True
    last_blink  = time.time()
    last_vtick  = time.time() - 1.0   # trigger an immediate first read
    VIDEO_INTERVAL = 1.0 / FPS        # advance previews at the movie's own FPS

    try:
        while True:
            now = time.time()

            # Advance each video preview one frame at movie FPS
            if now - last_vtick >= VIDEO_INTERVAL:
                for i, (cap, _) in enumerate(caps):
                    ok, vf = cap.read()
                    if not ok:                       # loop back to start
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ok, vf = cap.read()
                    if ok:
                        cur_vframes[i] = cv2.resize(vf, (_PREV_W, _PREV_H))
                last_vtick = now

            # Compose: static base + current video frames
            frame = (base_on if blink_on else base_off).copy()
            for i, (_, (vx, vy)) in enumerate(caps):
                if cur_vframes[i] is not None:
                    frame[vy:vy + _PREV_H, vx:vx + _PREV_W] = cur_vframes[i]
                # Re-draw border on top so it isn't obscured by the video
                cv2.rectangle(frame,
                              (vx - 3, vy - 3),
                              (vx + _PREV_W + 3, vy + _PREV_H + 3),
                              (90, 95, 125), 2)

            frame = _apply_border_pulse(frame, now)
            cv2.imshow(WINDOW_NAME, frame)
            key = cv2.waitKey(50) & 0xFF
            key = -1 if key == 255 else key
            if key == ord('q'):
                return False

            blink_threshold = 0.6 if not blink_on else 1.4
            if now - last_blink > blink_threshold:
                blink_on   = not blink_on
                last_blink = now

            event = buttons.poll(key)
            if event == 'EXIT':
                return False
            if event == 'B':
                if not browse_saved_movies(buttons):
                    return False   # EXIT pressed during browsing
                # Rebuild bases in case new movies were saved this session
                base_on  = _build_launch_base(show_prompt=True)
                base_off = _build_launch_base(show_prompt=False)
            elif event is not None:
                return True   # any other button starts a session

        return True  # unreachable — satisfies type checkers
    finally:
        for cap, _ in caps:
            cap.release()


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


def play_movie(path: Path, buttons: ButtonSource) -> bool:
    """Play movie looping until any button press (or q).

    Returns True if the R+Y exit combo was pressed (caller should quit).
    """
    while True:
        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return False
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
                return False
            event = buttons.poll(key)
            if event == 'EXIT':
                cap.release()
                return True
            if event is not None:
                interrupted = True
                break
        cap.release()
        if interrupted:
            return False


# ---- Background preview thread ----------------------------------------------

class _PreviewThread(threading.Thread):
    """Fetches camera preview frames continuously so the UI loop never blocks
    waiting for a network response or a camera that's mid-capture."""

    def __init__(self, backend):
        super().__init__(daemon=True, name='preview')
        self._backend = backend
        self._frame: np.ndarray | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._capturing = False  # set True during shutter/capture to suppress reconnect

    def latest(self) -> np.ndarray | None:
        with self._lock:
            return self._frame

    def set_capturing(self, capturing: bool) -> None:
        """Tell the thread whether a shutter capture is in progress.
        While True, liveview being dark is expected and reconnect is suppressed."""
        self._capturing = capturing

    def stop(self):
        self._stop.set()

    def run(self):
        consecutive_fails = 0
        blank_fails = 0
        STALE_THRESHOLD = 10
        # After this many consecutive blank frames (HTTP 200, 0 bytes — server
        # is up but liveview window is inactive), send LiveViewWnd_Show again.
        BLANK_REACTIVATE = 20
        blank_reactivate_cooldown = 0.0

        while not self._stop.is_set():
            frame = self._backend.read_preview()
            if frame is not None:
                consecutive_fails = 0
                blank_fails = 0
                with self._lock:
                    self._frame = frame
            else:
                consecutive_fails += 1
                if consecutive_fails == STALE_THRESHOLD:
                    with self._lock:
                        self._frame = None

                if (getattr(self._backend, 'lv_blank', False)
                        and not self._capturing):
                    blank_fails += 1
                    if (blank_fails >= BLANK_REACTIVATE
                            and time.time() >= blank_reactivate_cooldown
                            and hasattr(self._backend, '_try_start_liveview')):
                        log.warning('Preview: liveview blank — sending LiveViewWnd_Show')
                        try:
                            self._backend._try_start_liveview()
                        except Exception as e:
                            log.debug('Preview: liveview reactivation failed: %s', e)
                        blank_fails = 0
                        blank_reactivate_cooldown = time.time() + 5.0
                else:
                    blank_fails = 0

            # read_preview() for a DSLR liveview JPEG already takes ~1-2 s;
            # this sleep only matters for fast backends (webcam).
            time.sleep(0.02)


# ---- Main loop --------------------------------------------------------------

def make_backend(kind: str, camera_index: int, digicam_url: str):
    if kind == 'webcam':
        return WebcamBackend(camera_index)
    if kind == 'digicam':
        return DigiCamBackend(digicam_url)
    raise ValueError(f'Unknown backend: {kind}')


def _minimize_digicam_and_focus_booth() -> None:
    """Minimize every CameraControl.exe window and raise the booth window on top."""
    if not sys.platform.startswith('win'):
        return

    # Minimize via PowerShell — runs in a subprocess so any crash there
    # cannot take down our process.
    ps = (
        'Add-Type -TypeDefinition "'
        'using System;using System.Runtime.InteropServices;'
        'public class W{'
        '[DllImport(\\"user32.dll\\")]public static extern bool ShowWindow(IntPtr h,int n);'
        '[DllImport(\\"user32.dll\\")]public static extern IntPtr GetTopWindow(IntPtr h);'
        '[DllImport(\\"user32.dll\\")]public static extern IntPtr GetWindow(IntPtr h,uint u);'
        '[DllImport(\\"user32.dll\\")]public static extern uint GetWindowThreadProcessId(IntPtr h,out uint p);'
        '}" -ErrorAction SilentlyContinue;'
        '$pids=(Get-Process CameraControl -EA SilentlyContinue).Id;'
        'if($pids){'
        '$h=[W]::GetTopWindow([IntPtr]::Zero);$i=0;'
        'while($h -ne [IntPtr]::Zero -and $i -lt 500){'
        '$p=0;[W]::GetWindowThreadProcessId($h,[ref]$p)|Out-Null;'
        'if($pids -contains $p){[W]::ShowWindow($h,6)|Out-Null};'
        '$h=[W]::GetWindow($h,2);$i++}}'
    )
    try:
        subprocess.run(
            ['powershell', '-NoProfile', '-WindowStyle', 'Hidden', '-Command', ps],
            capture_output=True, timeout=8,
        )
        time.sleep(0.2)
    except Exception as e:
        log.debug('minimize digicam via PowerShell failed: %s', e)

    # Pin the booth window on top — HWND_TOPMOST beats modal dialogs that
    # resist SW_MINIMIZE.  We leave it topmost permanently since this is a
    # kiosk application and nothing should cover it.
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype  = ctypes.c_void_p
        user32.SetWindowPos.restype = ctypes.c_bool
        hwnd = user32.FindWindowW(None, WINDOW_NAME)
        if hwnd:
            HWND_TOPMOST  = ctypes.c_void_p(-1)
            SWP_NOMOVE    = 0x0002
            SWP_NOSIZE    = 0x0001
            SWP_SHOWWINDOW = 0x0040
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
    except Exception as e:
        log.debug('focus booth window failed: %s', e)


_TRASH_WARN_BYTES = 100 * 1024 * 1024  # 100 MB


def _warn_if_trash_large(buttons: 'ButtonSource') -> None:
    """Show a fullscreen warning if the trash folder exceeds the size threshold.

    Blocks until GREEN is pressed.
    """
    if not TRASH_DIR.exists():
        return
    total = sum(f.stat().st_size for f in TRASH_DIR.iterdir() if f.is_file())
    if total < _TRASH_WARN_BYTES:
        return
    mb = total / (1024 * 1024)
    path_str = str(TRASH_DIR.resolve())

    bg = np.zeros((720, 1280, 3), dtype=np.uint8)
    lines = [
        (f'Deleted-movies trash is {mb:.0f} MB', 1.1, (220, 160, 40)),
        ('To free space, delete files from:', 0.7, (200, 200, 200)),
        (path_str, 0.6, (140, 180, 255)),
        ('Files are date-stamped so you can check before deleting.', 0.6, (170, 170, 170)),
        ('', 0.5, (0, 0, 0)),
        ('Press GREEN to continue', 0.9, (50, 215, 65)),
    ]
    y = 240
    for text, scale, color in lines:
        if text:
            sz = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)[0]
            cv2.putText(bg, text, ((1280 - sz[0]) // 2, y),
                        cv2.FONT_HERSHEY_SIMPLEX, scale, color, 2)
        y += int(scale * 60) + 18

    while True:
        cv2.imshow(WINDOW_NAME, bg)
        key = cv2.waitKey(50) & 0xFF
        key = -1 if key == 255 else key
        event = buttons.poll(key)
        if event == 'G':
            break


def run(backend_kind: str, camera_index: int, digicam_url: str,
        port: str | None, fullscreen: bool) -> int:
    _setup_logging()
    log.info('Starting booth — backend=%s port=%s url=%s', backend_kind, port, digicam_url)
    SESSIONS_DIR.mkdir(exist_ok=True)
    SAVED_DIR.mkdir(exist_ok=True)
    TRASH_DIR.mkdir(exist_ok=True)
    CLOUD_DIR.mkdir(exist_ok=True)

    backend = make_backend(backend_kind, camera_index, digicam_url)
    buttons = ButtonSource(port)

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    if fullscreen:
        cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN,
                              cv2.WINDOW_FULLSCREEN)

    _minimize_digicam_and_focus_booth()
    _warn_if_trash_large(buttons)

    if not show_launch_screen(buttons):
        buttons.close()
        try:
            backend.release()
        except Exception:
            pass
        cv2.destroyAllWindows()
        return 0

    # Preview runs in a background thread — the main loop reads the latest
    # decoded frame without ever blocking on a network call.
    preview_thread = _PreviewThread(backend)
    preview_thread.start()

    # Lock exposure in the background immediately after the launch screen so
    # it's done long before the first button press (6 HTTP round-trips, ~0.5 s).
    if hasattr(backend, 'lock_exposure'):
        threading.Thread(target=backend.lock_exposure, daemon=True,
                         name='lock-exposure').start()

    session = Session(SESSIONS_DIR)
    onion = False
    flash_until = 0.0
    pending_save: Path | None = None
    pending_delete = False
    pending_exit = False
    pending_white_save = False
    capture_state = {'in_flight': False, 'error_until': 0.0}

    # Pre-render the "camera disconnected" frame shown when the preview has been
    # gone for more than 1 s outside of a normal in-flight capture.
    _no_signal = np.zeros((720, 1280, 3), dtype=np.uint8)
    for _msg, _y in [('CAMERA DISCONNECTED', 310), ('Restart digiCamControl to continue', 370), ('RED = exit', 430)]:
        _s = cv2.getTextSize(_msg, cv2.FONT_HERSHEY_SIMPLEX, 1.4, 2)[0]
        cv2.putText(_no_signal, _msg, ((1280 - _s[0]) // 2, _y),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (70, 70, 70), 2)
    _preview_gone_since: float | None = None

    def do_capture():
        preview_thread.set_capturing(True)
        try:
            path = backend.capture(session.next_stem())
            if path is not None:
                session.register(path)
            else:
                capture_state['error_until'] = time.time() + 4.0
        finally:
            preview_thread.set_capturing(False)
            capture_state['in_flight'] = False

    try:
        while True:
            live = preview_thread.latest()
            if live is None:
                now = time.time()
                if _preview_gone_since is None:
                    _preview_gone_since = now
                # Only show the disconnected overlay if the preview has been gone
                # for >1 s AND no capture is in flight (mirror-up during a normal
                # shot is also ~1-2 s but in_flight is True throughout).
                if now - _preview_gone_since > 1.0 and not capture_state['in_flight']:
                    cv2.imshow(WINDOW_NAME, _no_signal)
                key = cv2.waitKey(50) & 0xFF
                key = -1 if key == 255 else key
                event = buttons.poll(key)
                if event == 'R' or event == 'EXIT':
                    break
                continue

            _preview_gone_since = None
            prev = session.last_frame_scaled(live.shape[1]) if onion else None
            display = apply_onion(live, prev)
            display = draw_hud(display, len(session.frames), onion,
                               capture_state['in_flight'])

            if pending_exit:
                display = big_message(display, [
                    'Exit the booth?',
                    'RED = yes    ANY OTHER = cancel',
                ])
            elif pending_save is not None:
                display = big_message(display, [
                    'Save this movie?',
                    'WHITE = save    YELLOW = keep working    RED = delete',
                ])
            elif pending_white_save:
                display = big_message(display, [
                    'Save movie and exit?',
                    'BLUE = save & exit    GREEN = keep filming',
                ])
            elif pending_delete:
                display = big_message(display, [
                    'Delete last frame?',
                    'RED = confirm    ANY OTHER = cancel',
                ])
            elif time.time() < capture_state['error_until']:
                display = big_message(display, [
                    'Camera not responding',
                    'Check digiCamControl and try again',
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

            # Exit combo — show confirmation dialog
            if event == 'EXIT':
                pending_exit = True
                continue

            if pending_exit:
                if event == 'R':
                    break          # confirmed — quit
                pending_exit = False   # any other button cancels
                continue

            if pending_white_save:
                if event == 'B':
                    cv2.imshow(WINDOW_NAME, big_message(display, ['Building movie...']))
                    cv2.waitKey(1)
                    movie = build_movie(session)
                    pending_white_save = False
                    if movie is not None:
                        saved = SAVED_DIR / f'{session.dir.name}.mp4'
                        shutil.copy2(movie, saved)
                        shutil.copy2(movie, CLOUD_DIR / saved.name)
                        print(f'Saved {saved} (queued for cloud upload)')
                    session.cleanup_if_empty()
                    if not show_launch_screen(buttons):
                        break
                    session = Session(SESSIONS_DIR)
                    onion = False
                elif event == 'G':
                    pending_white_save = False
                continue

            if pending_save is not None:
                if event == 'W':
                    saved = SAVED_DIR / f'{session.dir.name}.mp4'
                    shutil.copy2(pending_save, saved)
                    shutil.copy2(pending_save, CLOUD_DIR / saved.name)
                    print(f'Saved {saved} (queued for cloud upload)')
                    pending_save = None
                    session.cleanup_if_empty()
                    if not show_launch_screen(buttons):
                        break
                    session = Session(SESSIONS_DIR)
                    onion = False
                elif event == 'R':
                    try:
                        ts = time.strftime('%Y%m%d_%H%M%S')
                        shutil.move(str(pending_save),
                                    str(TRASH_DIR / f'{ts}_{pending_save.name}'))
                    except OSError:
                        pass
                    pending_save = None
                    session.cleanup_if_empty()
                    if not show_launch_screen(buttons):
                        break
                    session = Session(SESSIONS_DIR)
                    onion = False
                elif event == 'Y':
                    pending_save = None
                continue

            if pending_delete:
                if event == 'R':
                    session.pop()
                pending_delete = False   # any button clears the dialog
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
                if session.frames:
                    pending_delete = True
            elif event == 'Y':
                onion = not onion
            elif event == 'W':
                if session.frames:
                    pending_white_save = True
            elif event == 'B':
                cv2.imshow(WINDOW_NAME, big_message(display, ['Building movie...']))
                cv2.waitKey(1)
                movie = build_movie(session)
                if movie is not None:
                    if play_movie(movie, buttons):
                        pending_exit = True  # EXIT during playback → ask to confirm
                    else:
                        pending_save = movie

    finally:
        preview_thread.stop()
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
