<p align="center"><img src="assets/grabbit.png" width="128" alt="Grabbit"></p>

# Grabbit

A desktop downloader for Windows built on [yt-dlp](https://github.com/yt-dlp/yt-dlp): videos, music and whole playlists from YouTube and well over a thousand other sites, playlists, albums and profiles.

- Video or audio (MP3, M4A, Opus, FLAC, WAV), with separate folders for each
- Several downloads at once, retry failed items, only-new-items syncing
- Preview & clip: scrub a video or song, listen, and download just the part you want
- Search YouTube, YouTube Music and SoundCloud from inside the app
- Pick an exact quality per link, see the file name before you download
- Clipboard watching, drag and drop, dark mode, tray notifications, automatic updates

## Get it
Download **Grabbit-windows.zip** from the [Releases](../../releases/latest) page, unzip it and run **Grabbit.exe**. Keep `ffmpeg.exe`, `ffprobe.exe` and `deno.exe` next to it.

Every release lists what changed. The app updates itself from inside (**Update now**), and a new build with the latest yt-dlp is made every week.

## Run from source
`pip install "yt-dlp[default]" pillow pystray` then `python ytdlp_gui.py` (Python 3.11+), or build the .exe yourself with `build_exe.bat`.
