"""
Arduino communication diagnostics for the stop-motion booth.

Run this before booth.py to confirm the Arduino is wired and talking correctly:

    python diagnose_arduino.py            # auto-scan all ports
    python diagnose_arduino.py --port COM3

Checks performed:
  1. pyserial installed
  2. Available serial ports listed
  3. Port opens at 9600 baud
  4. Arduino reset + buffer flush
  5. Live character monitor — press each button and confirm the right code appears

Expected codes:
  G = green  (take picture)
  R = red    (delete last picture)
  B = blue   (build & play movie)
  W = white  (yes / save & upload)
  Y = yellow (toggle onion skin)
"""

from __future__ import annotations

import argparse
import sys
import time

BAUD = 9600
VALID_CODES = set('GRBWY')
TIMEOUT_S = 30


def step(n: int, msg: str):
    print(f'\n[{n}] {msg}')


def ok(msg: str = 'OK'):
    print(f'    PASS  {msg}')


def fail(msg: str):
    print(f'    FAIL  {msg}')


def warn(msg: str):
    print(f'    WARN  {msg}')


# ── 1. pyserial ───────────────────────────────────────────────────────────────

step(1, 'Checking pyserial import')
try:
    import serial
    import serial.tools.list_ports
    ok(f'pyserial {serial.__version__}')
except ImportError:
    fail('pyserial not found — run: pip install pyserial')
    sys.exit(1)


# ── 2. List ports ─────────────────────────────────────────────────────────────

step(2, 'Scanning serial ports')
ports = list(serial.tools.list_ports.comports())
if not ports:
    fail('No serial ports found. Is the Arduino plugged in via USB?')
    sys.exit(1)

arduino_candidates = []
for p in ports:
    desc = f'{p.device}  —  {p.description}'
    if any(kw in p.description.lower() for kw in ('arduino', 'ch340', 'ch341',
                                                    'ftdi', 'cp210', 'usb serial')):
        print(f'    >>>  {desc}')
        arduino_candidates.append(p.device)
    else:
        print(f'         {desc}')

if not arduino_candidates:
    warn('No port auto-identified as Arduino. '
         'Specify one with --port COMx if the list above looks right.')


# ── 3. Resolve port ───────────────────────────────────────────────────────────

def resolve_port(requested: str | None) -> str:
    if requested:
        return requested
    if len(arduino_candidates) == 1:
        print(f'\n    Auto-selected {arduino_candidates[0]}')
        return arduino_candidates[0]
    if arduino_candidates:
        print(f'\n    Multiple candidates: {arduino_candidates}')
        print('    Re-run with --port COMx to pick one.')
        sys.exit(1)
    if len(ports) == 1:
        print(f'\n    Only one port available — trying {ports[0].device}')
        return ports[0].device
    print('\n    Cannot auto-select port. Re-run with --port COMx.')
    sys.exit(1)


parser = argparse.ArgumentParser(description='Arduino diagnostic tool')
parser.add_argument('--port', default=None, help='Serial port (e.g. COM3)')
args = parser.parse_args()
port = resolve_port(args.port)


# ── 4. Open port ─────────────────────────────────────────────────────────────

step(3, f'Opening {port} at {BAUD} baud')
try:
    ser = serial.Serial(port, BAUD, timeout=0)
    ok(f'Port opened')
except serial.SerialException as e:
    fail(str(e))
    print('\n    Possible causes:')
    print('      - Arduino IDE Serial Monitor is open (close it)')
    print('      - booth.py is already running')
    print('      - Wrong COM port')
    sys.exit(1)


# ── 5. Reset + flush ─────────────────────────────────────────────────────────

step(4, 'Waiting for Arduino reset (2 s)')
time.sleep(2.0)
ser.reset_input_buffer()
ok('Buffer flushed')


# ── 6. Live monitor ───────────────────────────────────────────────────────────

step(5, f'Live button monitor — press each button. Ctrl-C or wait {TIMEOUT_S} s to exit.\n')
print('    Received codes will appear below.')
print('    Expected: G  R  B  W  Y')
print('    ' + '-' * 40)

seen: set[str] = set()
deadline = time.time() + TIMEOUT_S
last_activity = time.time()

try:
    while time.time() < deadline:
        data = ser.read(64)
        if data:
            last_activity = time.time()
            deadline = time.time() + TIMEOUT_S  # reset timeout on any activity
            for byte in data:
                ch = chr(byte).upper()
                status = 'VALID' if ch in VALID_CODES else 'UNEXPECTED'
                print(f'    Received: {repr(chr(byte))}  [{status}]')
                if ch in VALID_CODES:
                    seen.add(ch)
        else:
            elapsed = time.time() - last_activity
            remaining = deadline - time.time()
            print(f'\r    Waiting for button press... ({remaining:.0f} s remaining)  ',
                  end='', flush=True)
            time.sleep(0.1)
except KeyboardInterrupt:
    print()

ser.close()

# ── Summary ───────────────────────────────────────────────────────────────────

print('\n' + '=' * 50)
print('SUMMARY')
print('=' * 50)

missing = VALID_CODES - seen
if seen:
    ok(f'Codes received: {" ".join(sorted(seen))}')
else:
    fail('No valid codes received')
    print('\n    Possible causes:')
    print('      - Wrong sketch uploaded (check booth_buttons.ino is flashed)')
    print('      - USB cable is charge-only (no data lines)')
    print('      - Baud mismatch (sketch must use Serial.begin(9600))')
    print('      - Button not wired to GND -- check each button goes pin -> GND')

if missing and seen:
    warn(f'Codes NOT seen (buttons not pressed or not working): {" ".join(sorted(missing))}')

if not missing and seen == VALID_CODES:
    print('\n    All 5 buttons verified. Arduino communication looks good!')
    print('    You can now run booth.py.')
