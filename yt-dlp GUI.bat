@echo off
cd /d "%~dp0"
if exist "App\yt-dlp GUI.exe" ( start "" "App\yt-dlp GUI.exe" & exit /b )
where pyw >nul 2>&1 && ( start "" pyw -3 ytdlp_gui.pyw & exit /b )
where pythonw >nul 2>&1 && ( start "" pythonw ytdlp_gui.pyw & exit /b )
echo.
echo  Python is not installed (or not on PATH), so the GUI can't start from source.
echo.
echo  Either:
echo   - Use the ready-made "yt-dlp GUI.exe" instead (no Python needed), or
echo   - Install Python 3.11+ from https://www.python.org/downloads/
echo     (tick "Add python.exe to PATH"), then run this again.
echo.
pause
