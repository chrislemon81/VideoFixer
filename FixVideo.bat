@echo off
rem VideoFixer launcher
rem   Double-click this file to open the VideoFixer window.
rem   Or drag broken video files (or a whole folder) onto it: the window opens with them added.
setlocal
set "HERE=%~dp0"

where pyw >nul 2>nul
if not errorlevel 1 (
    start "" pyw -3 "%HERE%videofixer.py" --gui %*
    exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    start "" pythonw "%HERE%videofixer.py" --gui %*
    exit /b 0
)

echo Python is not installed yet.
echo Double-click "Install-Requirements.bat" first, then try again.
echo.
pause
