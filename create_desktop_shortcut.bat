@echo off
REM Creates a desktop shortcut that launches the Stop Motion Booth.
REM Run this once; afterwards double-click the desktop icon to start.

set "TARGET=%~dp0launch.bat"
set "SHORTCUT=%USERPROFILE%\Desktop\Stop Motion Booth.lnk"
set "ICON=%SystemRoot%\system32\imageres.dll,20"

powershell -NoProfile -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%SHORTCUT%');" ^
  "$s.TargetPath = 'cmd.exe';" ^
  "$s.Arguments = '/c \"%TARGET%\"';" ^
  "$s.WorkingDirectory = '%~dp0';" ^
  "$s.IconLocation = '%ICON%';" ^
  "$s.WindowStyle = 1;" ^
  "$s.Save()"

if errorlevel 1 (
    echo ERROR: Could not create shortcut.
) else (
    echo Desktop shortcut created: %SHORTCUT%
)
pause
