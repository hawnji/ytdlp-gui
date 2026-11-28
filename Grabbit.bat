@echo off
cd /d "%~dp0"
if exist "App\Grabbit.exe" ( start "" "App\Grabbit.exe" & exit /b )
where pyw >nul 2>&1 && ( start "" pyw -3 ytdlp_gui.py & exit /b )
where pythonw >nul 2>&1 && ( start "" pythonw ytdlp_gui.py & exit /b )
echo.
echo  Python is not installed (or not on PATH), so Grabbit can't start from source.
echo.
echo  Either:
echo   - Use the ready-made Grabbit.exe instead (no Python needed), or
echo   - Install Python 3.11+ from https://www.python.org/downloads/
echo     (tick "Add python.exe to PATH"), then run this again.
echo.
pause
