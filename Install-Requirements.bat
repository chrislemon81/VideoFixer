@echo off
rem Installs the two free programs VideoFixer needs: Python and FFmpeg.
setlocal
echo This installs Python and FFmpeg (both free) using winget.
echo.

where winget >nul 2>nul
if errorlevel 1 (
    echo winget is not available on this PC.
    echo Install "App Installer" from the Microsoft Store, or install manually:
    echo   Python: https://www.python.org/downloads/   ^(tick "Add python.exe to PATH"^)
    echo   FFmpeg: https://www.gyan.dev/ffmpeg/builds/  ^(put ffmpeg.exe and ffprobe.exe next to videofixer.py^)
    pause
    exit /b 1
)

where py >nul 2>nul
if errorlevel 1 (
    echo Installing Python...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
) else (
    echo Python is already installed.
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo Installing FFmpeg...
    winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
) else (
    echo FFmpeg is already installed.
)

echo.
echo All done. Now double-click FixVideo.bat to start VideoFixer.
pause
