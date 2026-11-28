@echo off
setlocal
cd /d "%~dp0"
title Building Grabbit.exe
echo.
echo  ===  Grabbit  -  .exe builder  ===
echo.

rem ---- Find Python 3.11+ ----
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY goto :nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 || goto :nopython
%PY% -c "import tkinter" >nul 2>&1 || (
  echo Your Python is missing tkinter. Re-run the Python installer, choose Modify,
  echo and tick "tcl/tk and IDLE", then run this again.
  goto :fail
)

echo [1/4] Creating a private build environment...
if not exist ".build-venv\Scripts\python.exe" ( %PY% -m venv .build-venv || goto :fail )
set "VPY=%~dp0.build-venv\Scripts\python.exe"
"%VPY%" -m pip install --upgrade pip --quiet

echo [2/4] Installing yt-dlp, its dependencies, Deno, curl_cffi and PyInstaller...
"%VPY%" -m pip install --upgrade "yt-dlp[default,deno,curl-cffi]" tkinterdnd2 pyinstaller --quiet || goto :fail

echo [3/4] Building the .exe (this takes a minute or two)...
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "Grabbit" --icon "%~dp0assets\grabbit.ico" --add-data "%~dp0assets\grabbit.png;assets" --add-data "%~dp0assets\grabbit.ico;assets" --distpath "App" --workpath ".build" --specpath ".build" --collect-all tkinterdnd2 ^
  "%~dp0ytdlp_gui.py" || goto :fail

echo [4/4] Putting FFmpeg and Deno next to the .exe...
for /r ".build-venv" %%F in (deno.exe) do if exist "%%F" copy /y "%%F" "App\" >nul
if exist "App\ffmpeg.exe" goto :ffdone
if exist "ffmpeg.exe" (
  copy /y "ffmpeg.exe" "App\" >nul
  if exist "ffprobe.exe" copy /y "ffprobe.exe" "App\" >nul
  goto :ffdone
)
echo       Downloading FFmpeg (about 140 MB)...
if not exist ".build" mkdir ".build"
curl -L --fail --progress-bar -o ".build\ffmpeg.zip" "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" || (
  echo       Could not download FFmpeg. Put ffmpeg.exe and ffprobe.exe in the App folder yourself.
  goto :ffdone
)
if exist ".build\ff" rmdir /s /q ".build\ff"
mkdir ".build\ff"
tar -xf ".build\ffmpeg.zip" -C ".build\ff" || goto :ffdone
for /r ".build\ff" %%F in (ffmpeg.exe ffprobe.exe) do if exist "%%F" copy /y "%%F" "App\" >nul
:ffdone

echo.
echo  Done!  Your app is in the "App" folder:
echo     App\Grabbit.exe
echo  Keep ffmpeg.exe, ffprobe.exe and deno.exe in the same folder as the .exe.
echo  You can move the whole App folder anywhere and make a shortcut to the .exe.
echo.
explorer "App"
pause
exit /b 0

:nopython
echo Python 3.11 or newer was not found.
echo Install it from https://www.python.org/downloads/  (tick "Add python.exe to PATH"),
echo then double-click this file again.
:fail
echo.
echo  Build failed - see the messages above.
pause
exit /b 1
