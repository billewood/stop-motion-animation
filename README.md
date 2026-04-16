# Stop-Motion Animation Booth

A kids' fair stop-motion animation booth. Kids pose a subject, press a button to capture a frame, and when they're done the booth builds and plays back their movie. They can then save it for cloud upload or keep working.

## Hardware

- **Camera** — Canon (e.g. T3i) or other DSLR supported by [digiCamControl](http://digicamcontrol.com/), connected via USB
- **Arduino Uno** — connected via USB, running `arduino/booth_buttons.ino`
- **5 momentary push buttons** wired between the Arduino digital pins and GND

### Button wiring

| Pin | Button | Action |
|-----|--------|--------|
| 2   | Yellow | Toggle onion skin |
| 4   | Red    | Delete last frame |
| 5   | White  | Save movie (yes) |
| 6   | Green  | Take picture |
| 7   | Blue   | Build & play movie |

Each button connects one leg to the listed pin and the other leg to GND. `INPUT_PULLUP` is used — a press reads LOW.

## Software setup

### 1. Install digiCamControl (Windows only)

Download from [digicamcontrol.com](http://digicamcontrol.com/) and enable the webserver:
**File → Settings → Webserver** (default port 5513). Start live view before running the booth.

### 2. Create the conda environment

```
conda env create -f environment.yml
conda activate stopmotion
```

Or with pip:

```
pip install -r requirements.txt
```

### 3. Upload the Arduino sketch

```
arduino-cli compile --fqbn arduino:avr:uno arduino/booth_buttons
arduino-cli upload  --fqbn arduino:avr:uno --port COM3 arduino/booth_buttons
```

Replace `COM3` with your actual port.

## Running

```
conda activate stopmotion
python booth.py --backend digicam --port COM3
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--backend` | `digicam` | `digicam` (DSLR) or `webcam` (USB webcam for testing) |
| `--port` | `none` | Arduino serial port, e.g. `COM3`. Use `none` for keyboard-only mode |
| `--camera` | `0` | OpenCV camera index (webcam backend only) |
| `--digicam-url` | `http://localhost:5513` | digiCamControl webserver URL |
| `--windowed` | off | Disable fullscreen |

### Keyboard fallback (--port none)

`g` take picture · `r` delete last · `b` build movie · `w` save · `y` onion skin · `q` quit

## Troubleshooting

Run the Arduino diagnostic before starting the booth:

```
python diagnose_arduino.py
python diagnose_arduino.py --port COM3   # specify port explicitly
```

This checks pyserial, lists ports, opens the connection, and lets you verify each button sends the right code.

A log file `booth.log` is written on every run with full detail on digiCamControl HTTP calls, capture events, and errors.

### Common issues

| Symptom | Likely cause |
|---------|-------------|
| `PermissionError` on COM3 | Another app (Arduino IDE Serial Monitor) has the port open |
| Buttons do nothing | Sketch not uploaded, or charge-only USB cable |
| "CAPTURING" hangs, no frame added | digiCamControl not set to download to PC; check session folder in digiCamControl settings |
| Live view blank | digiCamControl Live View not started; check the app |

## File layout

```
booth.py               Main application
diagnose_arduino.py    Arduino communication diagnostic
arduino/
  booth_buttons.ino    Arduino sketch
environment.yml        Conda environment
requirements.txt       pip requirements
sessions/              Captured frames (created at runtime)
saved_movies/          Finished movies kept after saving
cloud_outbox/          Movies queued for upload (created at runtime)
booth.log              Run log (created at runtime)
```
