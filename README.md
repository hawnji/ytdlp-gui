# yt-dlp GUI

A desktop front end for [yt-dlp](https://github.com/yt-dlp/yt-dlp) — the newest yt-dlp release is pulled in at build time.

## Get the app
Download `yt-dlp-GUI-windows.zip` from the **Releases** page, unzip it, and run `yt-dlp GUI.exe`.
Keep `ffmpeg.exe`, `ffprobe.exe` and `deno.exe` in the same folder.

Every push to `main` builds a new release automatically (see `.github/workflows/build-gui-exe.yml`).

## Run from source
`pip install "yt-dlp[default]"` then `python ytdlp_gui.py` (Python 3.11+), or build the .exe locally with `build_exe.bat`.

## Extra sites
The app adds support for **Suno** songs (`suno.com/song/...`, `suno.com/s/...` share links) and playlists (`suno.com/playlist/...`), which yt-dlp itself does not handle.
