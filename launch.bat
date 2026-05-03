@echo off
cd /d "%~dp0"

REM ---- Configuration (edit if needed) ----
set CONDA_ACTIVATE=%USERPROFILE%\miniconda3\Scripts\activate.bat
set ARDUINO_PORT=COM3

REM ---- Locate digiCamControl (try common install locations) ----
set DIGICAM_EXE=
for %%P in (
    "C:\Program Files\digiCamControl\CameraControl.exe"
    "C:\Program Files (x86)\digiCamControl\CameraControl.exe"
    "%LOCALAPPDATA%\digiCamControl\CameraControl.exe"
    "%APPDATA%\digiCamControl\CameraControl.exe"
) do (
    if exist %%P (
        set DIGICAM_EXE=%%P
        goto :found_digicam
    )
)

echo WARNING: digiCamControl not found in any standard location.
echo   Searched:
echo     C:\Program Files\digiCamControl\CameraControl.exe
echo     C:\Program Files (x86)\digiCamControl\CameraControl.exe
echo     %LOCALAPPDATA%\digiCamControl\CameraControl.exe
echo   If it is installed elsewhere, set DIGICAM_EXE manually at the top of this file.
echo   Continuing without launching digiCamControl...
echo.
goto :start_booth

:found_digicam
echo Found digiCamControl at %DIGICAM_EXE%

REM ---- Start digiCamControl if not already running ----
tasklist /FI "IMAGENAME eq CameraControl.exe" 2>NUL | find /I "CameraControl.exe" >NUL
if errorlevel 1 (
    echo Starting digiCamControl...
    start "" %DIGICAM_EXE%
    echo Waiting for digiCamControl to initialise...
    timeout /t 5 /nobreak >NUL
) else (
    echo digiCamControl already running.
)

:start_booth
REM ---- Activate conda and launch booth ----
call "%CONDA_ACTIVATE%" stopmotion
if errorlevel 1 (
    echo ERROR: Could not activate conda environment "stopmotion".
    echo Expected activate script: %CONDA_ACTIVATE%
    echo Run once to create the environment:
    echo   %USERPROFILE%\miniconda3\Scripts\conda env create -f environment.yml
    pause
    exit /b 1
)

python booth.py --backend digicam --port %ARDUINO_PORT%
if errorlevel 1 (
    echo.
    echo Booth exited with an error. See above for details.
    pause
)
