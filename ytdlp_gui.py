#!/usr/bin/env python3
"""
yt-dlp GUI — a desktop front end for yt-dlp.

Drop this file into the root of the yt-dlp source folder (next to the
`yt_dlp` package) and run it with Python 3.10+:

    python ytdlp_gui.py          (or double-click ytdlp_gui.pyw on Windows)

It also works if yt-dlp is installed with pip.
FFmpeg is needed for merging best video+audio, audio conversion and embedding.
"""

import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

FROZEN = getattr(sys, 'frozen', False)
HERE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
if not FROZEN and HERE not in sys.path:
    sys.path.insert(0, HERE)

# Let yt-dlp find ffmpeg/ffprobe/deno placed next to the app (or in a "bin" folder)
for _d in (os.path.join(HERE, 'bin'), HERE):
    if os.path.isdir(_d):
        os.environ['PATH'] = _d + os.pathsep + os.environ.get('PATH', '')

# A windowed .exe has no console; give stray prints somewhere harmless to go
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')

try:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled
    try:
        from yt_dlp.version import __version__ as YTDLP_VERSION
    except Exception:
        YTDLP_VERSION = '?'
except ImportError:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror(
        'yt-dlp not found',
        'Could not import yt_dlp.\n\nPut ytdlp_gui.py in the yt-dlp source folder '
        '(next to the "yt_dlp" folder), or install it with:\n\npip install yt-dlp')
    sys.exit(1)

import re
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError, float_or_none, parse_iso8601, traverse_obj, url_or_none


class SunoIE(InfoExtractor):
    """Suno songs. yt-dlp deliberately ships without Suno support, so the GUI adds it."""
    IE_NAME = 'suno'
    _VALID_URL = r'https?://(?:www\.|app\.)?suno\.com/(?:song|s|embed)/(?P<id>[\w-]+)'
    _UUID_RE = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    _API = 'https://studio-api.prod.suno.com/api/clip/'

    def _real_extract(self, url):
        song_id = self._match_id(url)
        webpage = None
        if not re.fullmatch(self._UUID_RE, song_id):  # short share link: suno.com/s/XXXX
            webpage, urlh = self._download_webpage_handle(url, song_id, 'Resolving share link')
            m = (re.search(self._UUID_RE, urlh.url)
                 or re.search(rf'/song/({self._UUID_RE})', webpage)
                 or re.search(rf'image_large_({self._UUID_RE})', webpage))
            if not m:
                raise ExtractorError('Could not find a song in this Suno link', expected=True)
            song_id = m.group(m.lastindex or 0)

        clip = self._download_json(self._API + song_id, song_id, 'Downloading song info', fatal=False) or {}
        meta = clip.get('metadata') or {}

        formats = []
        audio = url_or_none(clip.get('audio_url'))
        if audio and 'forbidden' in audio:
            audio = None
        for a in dict.fromkeys(filter(None, (audio, f'https://cdn1.suno.ai/{song_id}.mp3'))):
            if self._is_valid_url(a, song_id, 'MP3 audio'):
                formats.append({'format_id': 'mp3', 'url': a, 'ext': 'mp3',
                                'vcodec': 'none', 'acodec': 'mp3', 'quality': 1})
                break
        video = url_or_none(clip.get('video_url')) or f'https://cdn1.suno.ai/{song_id}.mp4'
        if self._is_valid_url(video, song_id, 'MP4 video'):
            formats.append({'format_id': 'mp4', 'url': video, 'ext': 'mp4',
                            'vcodec': 'h264', 'acodec': 'aac', 'quality': 0})
        if not formats:
            raise ExtractorError('Suno did not offer a downloadable file for this song '
                                 '(it may be private, deleted or still generating)', expected=True)

        title = clip.get('title')
        if not title:
            webpage = webpage or self._download_webpage(f'https://suno.com/song/{song_id}', song_id, fatal=False)
            title = (webpage and self._og_search_title(webpage, default=None)) or song_id
        handle = clip.get('handle')
        artist = clip.get('display_name') or handle
        return {
            'id': song_id,
            'title': title,
            'track': title,
            'formats': formats,
            'thumbnail': (url_or_none(clip.get('image_large_url'))
                          or f'https://cdn2.suno.ai/image_large_{song_id}.jpeg'),
            'uploader': artist,
            'uploader_id': handle,
            'uploader_url': f'https://suno.com/@{handle}' if handle else None,
            'artists': [artist] if artist else None,
            'duration': float_or_none(meta.get('duration')),
            'timestamp': parse_iso8601(clip.get('created_at')),
            'description': meta.get('tags'),
            'webpage_url': f'https://suno.com/song/{song_id}',
            # Suno songs often share titles; file names can include this short ID to tell versions apart
            'suno_tag': f' [{song_id[:8]}]',
        }


class SunoPlaylistIE(InfoExtractor):
    """Suno playlists: suno.com/playlist/<id> — each song is handed to SunoIE."""
    IE_NAME = 'suno:playlist'
    _VALID_URL = r'https?://(?:www\.|app\.)?suno\.com/playlist/(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
    _API = 'https://studio-api.prod.suno.com/api/playlist/'

    def _real_extract(self, url):
        playlist_id = self._match_id(url)
        info, entries, seen = {}, [], set()
        for page in range(1, 101):
            data = self._download_json(f'{self._API}{playlist_id}/', playlist_id,
                                       f'Downloading playlist page {page}', query={'page': page})
            if page == 1:
                info = data
            clips = [(item or {}).get('clip') or {} for item in data.get('playlist_clips') or []]
            new = [c for c in clips if c.get('id') and c['id'] not in seen]
            for c in new:
                seen.add(c['id'])
                entries.append(self.url_result(
                    f'https://suno.com/song/{c["id"]}', SunoIE, c['id'], c.get('title'),
                    duration=float_or_none((c.get('metadata') or {}).get('duration')),
                    uploader=c.get('display_name')))
            total = data.get('num_total_results')
            if not new or (total and len(entries) >= total):
                break
        if not entries:
            raise ExtractorError('This Suno playlist is empty, private or deleted', expected=True)
        handle = info.get('user_handle')
        return self.playlist_result(
            entries, playlist_id, info.get('name'), info.get('description') or None,
            thumbnail=url_or_none(info.get('image_url')),
            uploader=info.get('user_display_name') or handle, uploader_id=handle,
            uploader_url=f'https://suno.com/@{handle}' if handle else None,
            webpage_url=f'https://suno.com/playlist/{playlist_id}')


class SunoProfileIE(InfoExtractor):
    """Suno profiles: suno.com/@handle — every published song, newest first."""
    IE_NAME = 'suno:profile'
    _VALID_URL = r'https?://(?:www\.|app\.)?suno\.com/@(?P<id>[\w.-]+)'
    _API = 'https://studio-api.prod.suno.com/api/profiles/'

    def _real_extract(self, url):
        handle = self._match_id(url)
        info, entries, seen = {}, [], set()
        for page in range(1, 201):
            data = self._download_json(
                f'{self._API}{handle}', handle, f'Downloading song list page {page}',
                query={'page': page, 'playlists_sort_by': 'created_at', 'clips_sort_by': 'created_at'})
            if page == 1:
                info = data
            new = [c for c in data.get('clips') or [] if c.get('id') and c['id'] not in seen]
            for c in new:
                seen.add(c['id'])
                entries.append(self.url_result(
                    f'https://suno.com/song/{c["id"]}', SunoIE, c['id'], c.get('title'),
                    duration=float_or_none((c.get('metadata') or {}).get('duration')),
                    uploader=c.get('display_name')))
            total = data.get('num_total_clips')
            if not new or (total and len(entries) >= total):
                break
        if not entries:
            raise ExtractorError('No public songs found on this Suno profile', expected=True)
        name = info.get('display_name') or handle
        return self.playlist_result(
            entries, info.get('handle') or handle, name, info.get('profile_description') or None,
            thumbnail=url_or_none(info.get('avatar_image_url')),
            uploader=name, uploader_id=info.get('handle') or handle,
            uploader_url=f'https://suno.com/@{handle}', webpage_url=f'https://suno.com/@{handle}')


def _walk(obj, key):
    """Every value stored under `key` anywhere inside nested dicts/lists, in order."""
    stack = [obj]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            for k, v in cur.items():
                if k == key:
                    yield v
                if isinstance(v, (dict, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            stack.extend(cur)


try:
    from yt_dlp.extractor.youtube import YoutubeIE, YoutubeTabIE
    from yt_dlp.extractor.youtube._base import YoutubeBaseInfoExtractor

    class YouTubeMusicRadioIE(YoutubeBaseInfoExtractor):
        """YouTube Music radios/mixes (watch?v=…&list=RD…). yt-dlp opens these on regular YouTube, where music
        radios often don't exist, and then downloads only the first song. This asks YouTube Music for the queue."""
        IE_NAME = 'youtube:music:radio'
        _VALID_URL = r'https?://music\.youtube\.com/watch\?(?=[^#]*\blist=(?P<id>RD[\w-]+))(?=[^#]*\bv=(?P<vid>[\w-]{11}))'
        DEFAULT_LIMIT = 50

        def _real_extract(self, url):
            m = self._match_valid_url(url)
            list_id, video_id = m.group('id'), m.group('vid')
            if list_id.startswith('RDCLAK'):  # albums are normal playlists: yt-dlp handles those itself
                return self.url_result(url, YoutubeTabIE)
            if self.get_param('noplaylist'):
                self.to_screen(f'Downloading just the song {video_id} (playlist links are set to single video)')
                return self.url_result(f'https://www.youtube.com/watch?v={video_id}', YoutubeIE, video_id)

            limit = max(self.DEFAULT_LIMIT, self.get_param('playlistend') or 0)
            base = {'videoId': video_id, 'playlistId': list_id, 'isAudioOnly': True,
                    'enablePersistentPlaylistPanel': True, 'tunerSettingValue': 'AUTOMIX_SETTING_NORMAL'}
            entries, seen, title, token = [], set(), None, None
            for page in range(1, 21):
                query = {**base, 'continuation': token} if token else base
                data = self._call_api('next', query, list_id, fatal=page == 1, default_client='web_music',
                                      note=f'Downloading YouTube Music radio page {page}')
                if not data:
                    break
                if title is None:
                    panel = next((t for t in _walk(data, 'playlistPanelRenderer') if isinstance(t, dict)), {})
                    raw = panel.get('title')
                    title = (raw if isinstance(raw, str)
                             else traverse_obj(raw, ('simpleText', {str}))
                             or ''.join(traverse_obj(raw, ('runs', ..., 'text', {str})) or []) or None)
                new = 0
                for r in _walk(data, 'playlistPanelVideoRenderer'):
                    vid = isinstance(r, dict) and r.get('videoId')
                    if not vid or vid in seen:
                        continue
                    seen.add(vid)
                    new += 1
                    entries.append(self.url_result(
                        f'https://www.youtube.com/watch?v={vid}', YoutubeIE, vid,
                        traverse_obj(r, ('title', 'runs', ..., 'text'), get_all=False),
                        uploader=traverse_obj(r, ('shortBylineText', 'runs', 0, 'text'))))
                if len(entries) >= limit or not new:
                    break
                token = next((c for key in ('nextRadioContinuationData', 'nextContinuationData')
                              for d in _walk(data, key) if isinstance(d, dict) and (c := d.get('continuation'))),
                             None)
                if not token:
                    break
            if not entries:
                self.report_warning('YouTube Music did not return the radio\'s songs; trying the regular way')
                return self.url_result(url, YoutubeTabIE)
            entries = entries[:limit]
            self.to_screen(f'YouTube Music radio: {len(entries)} songs'
                           + (' (radios are endless; the first 50 are used — set a playlist range in Extra '
                              'arguments, e.g. --playlist-end 100, for more)' if len(entries) >= self.DEFAULT_LIMIT
                              and not self.get_param('playlistend') else ''))
            return self.playlist_result(entries, list_id, title or 'YouTube Music radio')

    _YTM_EXTRACTORS = (YouTubeMusicRadioIE,)
except Exception:  # a future yt-dlp may move these internals; then simply do without this extra
    _YTM_EXTRACTORS = ()


EXTRA_EXTRACTORS = (SunoIE, SunoPlaylistIE, SunoProfileIE, *_YTM_EXTRACTORS)


def make_ydl(opts):
    """A YoutubeDL with the GUI's own extractors placed ahead of yt-dlp's (and its block list)."""
    ydl = yt_dlp.YoutubeDL(opts)
    for cls in reversed(EXTRA_EXTRACTORS):
        ie = cls()
        ydl.add_info_extractor(ie)
        key = ie.ie_key()
        ydl._ies = {key: ydl._ies.pop(key), **ydl._ies}
    ydl.add_post_processor(RelayClipsPP(ydl), when='before_dl')
    return ydl


_URL_RE = re.compile(r'https?://[^\s<>"\'{}|\\^`\[\]]+', re.I)
_SKIP_IES = {'Generic', 'KnownDRM', 'KnownPiracy', 'KnownLiability'}
_IE_CLASSES = None


def find_urls(text):
    """All http(s) links in a piece of text, in order, without duplicates."""
    out = []
    for m in _URL_RE.finditer(text or ''):
        u = m.group(0).rstrip('.,;:!?)\'"')
        if u not in out:
            out.append(u)
    return out


def is_supported_url(url):
    """True if a specific site handler (not the catch-all) recognises the link."""
    global _IE_CLASSES
    if _IE_CLASSES is None:
        from yt_dlp.extractor import gen_extractor_classes
        _IE_CLASSES = list(EXTRA_EXTRACTORS) + [c for c in gen_extractor_classes() if c.ie_key() not in _SKIP_IES]
    for c in _IE_CLASSES:
        try:
            if c.suitable(url):
                return True
        except Exception:
            pass
    return False


def urls_from_drop(paths_or_text, splitlist=None):
    """Links from something dropped on the window: text, browser links, .url shortcuts or .txt lists."""
    items = []
    if splitlist:
        try:
            items = list(splitlist(paths_or_text))
        except Exception:
            items = []
    if items and all(os.path.isfile(i) for i in items):
        urls = []
        for path in items:
            ext = os.path.splitext(path)[1].lower()
            try:
                with open(path, encoding='utf-8', errors='replace') as f:
                    text = f.read(2_000_000)
            except OSError:
                continue
            if ext in ('.url', '.website'):  # Windows internet shortcut
                m = re.search(r'^URL=(\S+)', text, re.M)
                text = m.group(1) if m else ''
            elif ext not in ('.txt', '.text', '.list', '.csv', '.md', '.html', '.htm', ''):
                continue
            urls += [u for u in find_urls(text) if u not in urls]
        return urls
    return find_urls(paths_or_text)


APP_NAME = 'yt-dlp GUI'
UPDATE_REPO = 'hawnji/ytdlp-gui'
UPDATE_ASSET = 'yt-dlp-GUI-windows.zip'
try:
    from _build_info import BUILD, BUILT_YTDLP  # written by the GitHub build
except ImportError:
    BUILD, BUILT_YTDLP = 0, None


def _https_context():
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def fetch_latest_release(timeout=15):
    """Latest GitHub release of the app: {'build', 'ytdlp', 'url', 'page', 'size'} or None."""
    import urllib.request
    req = urllib.request.Request(
        f'https://api.github.com/repos/{UPDATE_REPO}/releases/latest',
        headers={'Accept': 'application/vnd.github+json', 'User-Agent': APP_NAME})
    with urllib.request.urlopen(req, timeout=timeout, context=_https_context()) as r:
        rel = json.load(r)
    m = re.fullmatch(r'build-(\d+)', rel.get('tag_name') or '')
    asset = next((a for a in rel.get('assets') or [] if a.get('name') == UPDATE_ASSET), None)
    if not m or not asset:
        return None
    ver = re.search(r'yt-dlp:\s*(\S+)', rel.get('body') or '')
    return {'build': int(m.group(1)), 'ytdlp': ver.group(1) if ver else None,
            'url': asset['browser_download_url'], 'size': asset.get('size') or 0,
            'page': rel.get('html_url')}


def download_update(info, workdir, progress=None, cancel=None):
    """Download and unpack an update into workdir/new; returns that folder."""
    import urllib.request
    import zipfile
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir)
    zpath = os.path.join(workdir, UPDATE_ASSET)
    req = urllib.request.Request(info['url'], headers={'User-Agent': APP_NAME})
    with urllib.request.urlopen(req, timeout=60, context=_https_context()) as r, open(zpath, 'wb') as f:
        total = int(r.headers.get('Content-Length') or info.get('size') or 0)
        done = 0
        while True:
            if cancel and cancel.is_set():
                raise DownloadCancelled('Update cancelled')
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    new = os.path.join(workdir, 'new')
    with zipfile.ZipFile(zpath) as z:
        z.extractall(new)
    # the zip holds the app's files at its root (or inside one folder)
    if not os.path.exists(os.path.join(new, 'yt-dlp GUI.exe')):
        subs = [os.path.join(new, d) for d in os.listdir(new) if os.path.isdir(os.path.join(new, d))]
        if len(subs) == 1 and os.path.exists(os.path.join(subs[0], 'yt-dlp GUI.exe')):
            new = subs[0]
        else:
            raise RuntimeError('The update package does not contain yt-dlp GUI.exe')
    return new


def write_updater_script(src, dst, exe_name, pids, workdir):
    """Batch file that waits for the app to close, copies the new files over it and restarts it."""
    waits = '\r\n'.join(
        f':wait{i}\r\ntasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul\r\n'
        f'if not errorlevel 1 ( ping -n 2 127.0.0.1 >nul & goto wait{i} )'
        for i, pid in enumerate(pids))
    script = (
        '@echo off\r\n'
        'title Updating yt-dlp GUI...\r\n'
        f'{waits}\r\n'
        'ping -n 2 127.0.0.1 >nul\r\n'
        f'robocopy "{src}" "{dst}" /E /R:15 /W:1 /NFL /NDL /NJH /NJS /NP >nul\r\n'
        # without this the new one-file .exe tries to reuse the old copy's deleted temp folder
        'set "PYINSTALLER_RESET_ENVIRONMENT=1"\r\n'
        f'start "" "{os.path.join(dst, exe_name)}"\r\n'
        f'rmdir /s /q "{workdir}" >nul 2>&1\r\n'
        '(goto) 2>nul & del "%~f0"\r\n')
    path = os.path.join(os.path.dirname(workdir), 'ytdlp_gui_update.bat')
    with open(path, 'w', encoding='mbcs' if sys.platform.startswith('win') else 'utf-8') as f:
        f.write(script)
    return path


SETTINGS_PATH = os.path.join(os.path.expanduser('~'), '.ytdlp_gui.json')
ARCHIVE_PATH = os.path.join(os.path.expanduser('~'), '.ytdlp_gui_history.txt')

VIDEO_QUALITIES = ['Best', '2160p (4K)', '1440p', '1080p', '720p', '480p', '360p', '240p']
CONTAINERS = ['mp4', 'mkv', 'webm', 'Any']
AUDIO_FORMATS = ['mp3', 'm4a', 'opus', 'flac', 'wav', 'Best (no convert)']
AUDIO_QUALITIES = ['Best (0)', '320k', '256k', '192k', '128k', '96k']
BROWSERS = ['None', 'chrome', 'firefox', 'edge', 'brave', 'opera', 'vivaldi', 'safari', 'chromium']
PLAYLIST_MODES = ['Whole playlist', 'Single video only']

DEFAULTS = {
    'outdir': os.path.join(os.path.expanduser('~'), 'Downloads'),   # video folder
    'outdir_audio': '',  # audio folder ('' = same as the video folder; filled in on first run)
    'template': '%(title)s [%(id)s].%(ext)s',
    'mode': 'video',
    'quality': 'Best',
    'container': 'mp4',
    'audio_format': 'mp3',
    'audio_quality': 'Best (0)',
    'subs': False,
    'sub_langs': 'en.*',
    'embed_thumb': True,
    'metadata': True,
    'chapters': True,
    'sponsorblock': False,
    'playlist': 'Whole playlist',
    'cookies_browser': 'None',
    'rate_limit': '',
    'extra_args': '',
    'skip_existing': True,
    'parallel': 3,
    'only_new': False,
    'watch_clipboard': False,
    'clear_done': True,
    'suno_id': True,
    'preview_volume': 50.0,
    'clip_precise': True,
    'dark': True,
}

# Colours matched to Claude's dark and light themes
PALETTES = {
    'dark': {
        'bg': '#262624', 'surface': '#30302e', 'hover': '#3a3a37', 'field': '#1f1e1d',
        'border': '#3e3e38', 'fg': '#ece9e0', 'muted': '#a3a198', 'disabled': '#6b6a64',
        'accent': '#d97757', 'accent_hover': '#c6613f', 'on_accent': '#1f1e1d',
        'select': '#57392c', 'ok': '#7fbf7f', 'err': '#ff8a7a', 'warn': '#e3b341',
    },
    'light': {
        'bg': '#f5f4ee', 'surface': '#ffffff', 'hover': '#ebe9e1', 'field': '#ffffff',
        'border': '#d6d3c7', 'fg': '#1f1e1d', 'muted': '#6b6a64', 'disabled': '#a3a198',
        'accent': '#d97757', 'accent_hover': '#c6613f', 'on_accent': '#ffffff',
        'select': '#f3d9cd', 'ok': '#1a7f37', 'err': '#cf222e', 'warn': '#9a6700',
    },
}

# Options that make no sense inside the GUI
PICKER_SKIP = {'--help', '--version', '--update', '--no-update', '--update-to',
               '--newline', '--no-newline', '--color', '--no-colors', '--quiet', '--no-quiet',
               '--progress', '--no-progress', '--console-title', '--progress-template',
               '--progress-delta', '--simulate', '--no-simulate', '--dump-json',
               '--dump-single-json', '--list-extractors', '--extractor-descriptions'}


def split_args(s):
    """Split an argument string like a shell, but keep backslashes (Windows paths)."""
    lex = shlex.shlex(s, posix=True)
    lex.whitespace_split = True
    lex.escape = ''
    lex.commenters = ''
    return list(lex)


def quote_arg(a):
    if a and not any(c.isspace() or c in '"\'' for c in a):
        return a
    return "'" + a + "'" if '"' in a else '"' + a + '"'


_CLI_OPTIONS = None


def get_cli_options():
    """Every yt-dlp command-line option, read from yt-dlp's own parser."""
    global _CLI_OPTIONS
    if _CLI_OPTIONS is None:
        import optparse
        from yt_dlp.options import create_parser
        p = create_parser()
        p.formatter.parser = p
        groups = []
        for g in p.option_groups:
            opts = []
            for o in g.option_list:
                if o.help == optparse.SUPPRESS_HELP or not o._long_opts:
                    continue
                name = o._long_opts[0]
                if name in PICKER_SKIP:
                    continue
                try:
                    text = p.formatter.expand_default(o)
                except Exception:
                    text = o.help or ''
                opts.append({
                    'name': name,
                    'aliases': o._short_opts + o._long_opts,
                    'takes_value': o.takes_value(),
                    'nargs': o.nargs or 1,
                    'metavar': o.metavar or 'VALUE',
                    'help': ' '.join((text or '').split()),
                })
            if opts:
                groups.append((g.title, opts))
        _CLI_OPTIONS = groups
    return _CLI_OPTIONS


def human_bytes(n):
    if not n:
        return ''
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024:
            return f'{n:.1f} {unit}' if unit != 'B' else f'{int(n)} B'
        n /= 1024
    return f'{n:.1f} PiB'


def human_eta(s):
    if s is None:
        return ''
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f'{h}:{m:02d}:{sec:02d}' if h else f'{m}:{sec:02d}'


def open_path(path):
    try:
        if sys.platform.startswith('win'):
            os.startfile(path)  # noqa
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        else:
            subprocess.Popen(['xdg-open', path])
    except Exception as e:
        messagebox.showerror(APP_NAME, f'Could not open:\n{path}\n\n{e}')


def out_folder(s):
    """The download folder for the current mode."""
    if s.get('mode') == 'audio' and s.get('outdir_audio'):
        return s['outdir_audio']
    return s['outdir']


class SettingsError(ValueError):
    """A setting the user needs to fix (shown as-is)."""


def clip_range(s):
    """(start, end) in seconds from the clip boxes, or None for the whole thing. end may be None."""
    from yt_dlp.utils import parse_duration
    a, b = (s.get('clip_start') or '').strip(), (s.get('clip_end') or '').strip()
    if not a and not b:
        return None
    start = parse_duration(a) if a else 0
    end = parse_duration(b) if b else None
    if start is None:
        raise SettingsError(f'Clip start "{a}" is not a time. Use something like 1:30, 90 or 1:02:03.')
    if b and end is None:
        raise SettingsError(f'Clip end "{b}" is not a time. Use something like 2:45, 165 or 1:05:00.')
    if end is not None and end <= start:
        raise SettingsError('The clip end time must be after the start time.')
    return start, end


def build_args(s):
    """Turn GUI settings into yt-dlp command-line arguments."""
    args = ['--newline', '--no-colors']
    template = s['template'] or DEFAULTS['template']
    if s.get('suno_id', True) and '%(id)s' not in template:
        # Suno only: " [4f85f050]" before the extension (empty for every other site)
        template = (template[:-len('.%(ext)s')] + '%(suno_tag|)s.%(ext)s'
                    if template.endswith('.%(ext)s') else template + '%(suno_tag|)s')
    clip = clip_range(s)
    if clip:
        start, end = clip
        # keep clips from overwriting (or being skipped because of) the full download
        suffix = (' [clip %(section_start).1f-%(section_end).1fs]' if end is not None
                  else ' [from %(section_start).1fs]')
        template = (template[:-len('.%(ext)s')] + suffix + '.%(ext)s'
                    if template.endswith('.%(ext)s') else template + suffix)
        args += ['--download-sections', f'*{start:g}-{end:g}' if end is not None else f'*{start:g}-inf']
        if s.get('clip_precise'):
            args += ['--force-keyframes-at-cuts']
    args += ['-P', out_folder(s), '-o', template]

    if s['mode'] == 'audio':
        args += ['-f', 'bestaudio/best', '-x']
        fmt = s['audio_format']
        args += ['--audio-format', 'best' if fmt.startswith('Best') else fmt]
        q = s['audio_quality']
        args += ['--audio-quality', '0' if q.startswith('Best') else q.upper()[:-1] + 'K']
    else:
        q = s['quality']
        height = None if q == 'Best' else q.split('p')[0]
        cont = s['container']
        if cont == 'mp4':
            # Prefer H.264/AAC streams so the result plays everywhere
            h = f'[height<={height}]' if height else ''
            fmt = (f'bv*{h}[ext=mp4]+ba[ext=m4a]/bv*{h}+ba/b{h}[ext=mp4]/b{h}/b')
        else:
            h = f'[height<={height}]' if height else ''
            fmt = f'bv*{h}+ba/b{h}/b'
        args += ['-f', fmt]
        if cont != 'Any':
            args += ['--merge-output-format', cont]
        if s['subs']:
            args += ['--write-subs', '--sub-langs', s['sub_langs'] or 'en.*', '--embed-subs']

    # WAV has no standard place for cover art; yt-dlp would report an error
    wav = s['mode'] == 'audio' and s['audio_format'] == 'wav'
    if s['embed_thumb'] and not wav:
        args += ['--embed-thumbnail']
    if s['metadata']:
        args += ['--embed-metadata']
    if s['chapters'] and s['mode'] != 'audio':
        args += ['--embed-chapters']
    if s['sponsorblock']:
        args += ['--sponsorblock-remove', 'sponsor,selfpromo,interaction']
    args += ['--yes-playlist' if s['playlist'] == 'Whole playlist' else '--no-playlist']
    if s['cookies_browser'] != 'None':
        args += ['--cookies-from-browser', s['cookies_browser']]
    if s['rate_limit'].strip():
        args += ['-r', s['rate_limit'].strip()]
    if s.get('only_new'):
        args += ['--download-archive', ARCHIVE_PATH]
    if not s['skip_existing']:
        args += ['--force-overwrites']
    if s['extra_args'].strip():
        args += split_args(s['extra_args'])
    return args


_SKIP_HISTORY_RE = re.compile(r'^\[download\] (?:(?P<id>[^\s:]+): )?(?P<title>.*?)\s*has already been recorded in the archive')
_SKIP_EXISTS_RE = re.compile(r'^\[download\] (?P<path>.+?) has already been downloaded$')


class GuiLogger:
    def __init__(self, q, on_error=None, on_exists=None):
        self.q = q
        self.on_error = on_error
        self.on_exists = on_exists

    def _skip(self, msg):
        m = _SKIP_HISTORY_RE.match(msg)
        if m:
            title = m.group('title') or m.group('id') or 'item'
            self.q.put(('skipped', {'key': m.group('id'), 'title': title, 'reason': 'history'}))
            self.q.put(('log', f'Skipped (already in your download history): {title}'))
            return True
        m = _SKIP_EXISTS_RE.match(msg)
        if m:
            if self.on_exists:
                self.on_exists(m.group('path'))
            self.q.put(('log', f'Skipped (file already in the folder): {os.path.basename(m.group("path"))}'))
            return True
        return False

    def debug(self, msg):
        # yt-dlp routes normal [info] messages through debug(); hide true debug noise
        if msg.startswith('[debug] '):
            return
        if msg.startswith('[download]') and '%' in msg:
            return  # progress lines are shown in the table instead
        if self._skip(msg):
            return
        self.q.put(('log', msg))

    def info(self, msg):
        if self._skip(msg):
            return
        self.q.put(('log', msg))

    def warning(self, msg):
        self.q.put(('log', 'WARNING: ' + msg))

    def error(self, msg):
        if self.on_error:
            self.on_error()
        self.q.put(('log', msg))


class Worker(threading.Thread):
    """Runs downloads. `jobs` is a list of URLs, or of ('url', url, None) / ('entry', entry, extra) tuples
    (the latter is how failed playlist items are retried with their playlist numbering intact)."""

    def __init__(self, jobs, args, q, parallel=1, args_whole=None, clip_confirmed=False):
        super().__init__(daemon=True)
        # args_whole: the same settings without the clip, used if the user chooses to download a playlist whole
        self.args_whole = args_whole
        self._clip_choice = 'clip' if clip_confirmed else None
        self._ask_lock = threading.Lock()
        self.jobs = [('url', j, None) if isinstance(j, str) else tuple(j) for j in jobs]
        self.args, self.q = args, q
        self.parallel = max(1, int(parallel or 1))
        self.cancel = threading.Event()
        self.errors = 0
        self._lock = threading.Lock()
        self._tl = threading.local()
        self._root = {}          # id(playlist item) -> the link it came from
        self._failed_roots = set()
        self._key_roots = {}     # (site, video id) -> every pasted link that contains that video
        self._existing = set()   # files yt-dlp found already in the folder (skipped, not downloaded)

    @property
    def urls(self):
        return [j[1] for j in self.jobs if j[0] == 'url']

    def _on_exists(self, path):
        with self._lock:
            self._existing.add(os.path.normcase(os.path.abspath(path)))

    def _on_error(self):
        with self._lock:
            self.errors += 1
        self._tl.errors = getattr(self._tl, 'errors', 0) + 1

    def _hook(self, d):
        if self.cancel.is_set():
            raise DownloadCancelled('Cancelled by user')
        info = d.get('info_dict') or {}
        key = info.get('id') or d.get('filename') or '?'
        total = d.get('total_bytes') or d.get('total_bytes_estimate')
        done = d.get('downloaded_bytes') or 0
        self.q.put(('progress', {
            'key': key,
            'title': info.get('title') or os.path.basename(d.get('filename') or ''),
            'url': info.get('webpage_url') or info.get('original_url'),
            'status': d.get('status'),
            'pct': (done / total * 100) if total else None,
            'size': human_bytes(total),
            'speed': (human_bytes(d['speed']) + '/s') if d.get('speed') else '',
            'eta': human_eta(d.get('eta')),
            'fragment': (d.get('fragment_index'), d.get('fragment_count')),
            'format_id': info.get('format_id'),
        }))

    def _pp_hook(self, d):
        if self.cancel.is_set():
            raise DownloadCancelled('Cancelled by user')
        info = d.get('info_dict') or {}
        key = info.get('id') or '?'
        if d.get('status') == 'started':
            self.q.put(('pp', {'key': key, 'title': info.get('title'), 'pp': d.get('postprocessor')}))
        elif d.get('status') == 'finished' and d.get('postprocessor') == 'MoveFiles':
            self.q.put(('done_item', {'key': key, 'path': info.get('filepath'),
                                      'url': info.get('webpage_url') or info.get('original_url'),
                                      'title': info.get('title'),
                                      'skipped': bool(info.get('filepath')) and os.path.normcase(
                                          os.path.abspath(info['filepath'])) in self._existing}))

    def _clip_decision(self, n, title):
        """A clip is set and a playlist turned out to have n items: ask the user once per run."""
        if not self.args_whole:
            return 'clip'
        with self._ask_lock:
            if self._clip_choice is None:
                answer = threading.Event()
                box = {}
                self.q.put(('ask_clip', {'n': n, 'title': title, 'event': answer, 'box': box}))
                while not answer.wait(0.2):
                    if self.cancel.is_set():
                        return 'cancel'
                self._clip_choice = box.get('answer') or 'cancel'
            return self._clip_choice

    def _opts(self, args=None):
        opts = dict(yt_dlp.parse_options(args or self.args).ydl_opts)
        opts['logger'] = GuiLogger(self.q, self._on_error, self._on_exists)
        opts['progress_hooks'] = [self._hook]
        opts['postprocessor_hooks'] = [self._pp_hook]
        opts['noprogress'] = True
        opts['quiet'] = False
        return opts

    def _expand(self, ydl, url):
        """Return (entries, info): playlist items as jobs, or info to process directly."""
        info = ydl.extract_info(url, download=False, process=False)
        for _ in range(10):  # follow redirect-style results (short links, channel -> videos tab)
            if not info or info.get('_type') not in ('url', 'url_transparent'):
                break
            info = ydl.extract_info(info['url'], download=False, process=False, ie_key=info.get('ie_key'))
        if not info:
            return [], None
        if info.get('_type') not in ('playlist', 'multi_video') or ydl.params.get('lazy_playlist'):
            return None, info
        ydl.params['extract_flat'] = 'in_playlist'
        try:
            res = ydl.process_ie_result(info, download=False)
        finally:
            ydl.params.pop('extract_flat', None)
        if not res:
            return [], None
        entries = list(res.get('entries') or [])
        if any((e or {}).get('_type') not in ('url', 'url_transparent') for e in entries):
            return None, None  # unusual playlist: let yt-dlp download it in one go
        indices = res.get('requested_entries') or list(range(1, len(entries) + 1))
        common = yt_dlp.YoutubeDL._playlist_infodict(res, n_entries=len(entries))
        if res.get('playlist_count') and len(entries) < res['playlist_count'] and ydl.params.get('download_archive'):
            skipped = res['playlist_count'] - len(entries)
            self.q.put(('log', f'{skipped} item(s) already downloaded earlier — skipping them'))
        jobs = []
        for n, (idx, entry) in enumerate(zip(indices, entries), 1):
            jobs.append((dict(entry), {**common, 'playlist_index': idx, 'playlist_autonumber': n}))
        return jobs, None

    def _run_jobs(self, base_opts):
        jobs = queue.Queue()
        stop = threading.Event()
        counts = {'total': 0, 'done': 0}

        def add_total(n):
            with self._lock:
                counts['total'] += n
            self.q.put(('overall', dict(counts)))

        def finish_one():
            with self._lock:
                counts['done'] += 1
            self.q.put(('overall', dict(counts)))

        for job in self.jobs:
            jobs.put(job)
        add_total(len(self.jobs))

        def video_key(kind, item, info=None):
            if kind == 'url':
                return (info.get('extractor_key') or info.get('ie_key'), info.get('id')) if info and info.get('id') else None
            return (item.get('ie_key'), item.get('id') or item.get('url'))

        def claim(key, root):
            """True if this video should be downloaded now; False if it's already part of this batch."""
            if not key:
                return True
            with self._lock:
                roots = self._key_roots.setdefault(key, set())
                first = not roots
                if root:
                    roots.add(root)
                return first

        def failed(kind, item, extra, info=None):
            root = item if kind == 'url' else self._root.get(id(item))
            with self._lock:
                if root:
                    self._failed_roots.add(root)
                # every pasted link that shares this video failed too
                self._failed_roots.update(self._key_roots.get(video_key(kind, item, info), ()))
            if kind in ('entry', 'entry_whole'):
                key = item.get('id') or item.get('url')
                title, url = item.get('title') or item.get('url'), item.get('url')
            else:
                key = (info or {}).get('id') or item
                title, url = (info or {}).get('title') or item, item
            self.q.put(('item_failed', {'key': key, 'title': title, 'url': url, 'job': (kind, item, extra)}))

        def worker():
            try:
                ydl = make_ydl(dict(base_opts))
            except Exception as e:
                self._on_error()
                self.q.put(('log', f'ERROR: could not start yt-dlp: {e}'))
                return
            whole = {}  # a second YoutubeDL without the clip, made only if needed

            def ydl_for(kind):
                if kind != 'entry_whole':
                    return ydl
                if 'ydl' not in whole:
                    whole['ydl'] = make_ydl(self._opts(self.args_whole))
                return whole['ydl']

            with ydl:
                while not stop.is_set():
                    try:
                        kind, item, extra = jobs.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    info = None
                    before = getattr(self._tl, 'errors', 0)
                    try:
                        if self.cancel.is_set():
                            continue
                        if kind == 'url':
                            self.q.put(('log', f'=== {item}'))
                            entries, info = self._expand(ydl, item)
                            if entries is None:
                                if info is not None and not claim(video_key('url', item, info), item):
                                    self.q.put(('log', f'Skipping {info.get("title") or item}: '
                                                       'already being downloaded in this batch'))
                                elif info is not None:
                                    ydl.process_ie_result(info, download=True)
                                else:
                                    ydl.download([item])
                            elif entries:
                                pl = entries[0][1].get('playlist') or item
                                entry_kind = 'entry'
                                if self.args_whole and len(entries) > 1:
                                    choice = self._clip_decision(len(entries), pl)
                                    if choice == 'cancel':
                                        self.cancel.set()
                                        continue
                                    if choice == 'whole':
                                        entry_kind = 'entry_whole'
                                self.q.put(('log', f'Playlist "{pl}": {len(entries)} item(s), '
                                                   f'{self.parallel} at a time'
                                                   + (' — downloading whole, no clip' if entry_kind == 'entry_whole'
                                                      else ' — clipping each' if self.args_whole else '')))
                                for entry, ex in entries:
                                    if entry.get('id'):
                                        self.q.put(('queued', {'key': entry['id'], 'url': entry.get('url'),
                                                               'title': entry.get('title') or entry.get('url')}))
                                    self._root[id(entry)] = item
                                    jobs.put((entry_kind, entry, ex))
                                add_total(len(entries) - 1)  # the playlist itself was a placeholder
                                continue
                        elif not claim(video_key(kind, item), self._root.get(id(item))):
                            self.q.put(('log', f'Skipping {item.get("title") or item.get("url")}: '
                                               'already being downloaded in this batch'))
                            if item.get('id'):
                                self.q.put(('item_dup', {'key': item['id']}))
                        else:
                            ydl_for(kind).process_ie_result(dict(item), download=True, extra_info=extra)
                        if getattr(self._tl, 'errors', 0) > before:
                            failed(kind, item, extra, info)
                        finish_one()
                    except DownloadCancelled:
                        self.cancel.set()
                    except Exception as e:
                        if not str(e).startswith('ERROR'):  # yt-dlp errors were already logged
                            self._on_error()
                            self.q.put(('log', f'ERROR: {e}'))
                        if not self.cancel.is_set():
                            failed(kind, item, extra, info)
                        finish_one()
                    finally:
                        jobs.task_done()

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(self.parallel)]
        for t in threads:
            t.start()
        # wait until every job is done, or every thread has died (never hang)
        while jobs.unfinished_tasks and any(t.is_alive() for t in threads):
            time.sleep(0.2)
        stop.set()
        for t in threads:
            t.join()

    def run(self):
        try:
            self._run_jobs(self._opts())
            if self.cancel.is_set():
                raise DownloadCancelled('Cancelled by user')
            roots = self.urls
            self.q.put(('urls_done', {'done': [u for u in roots if u not in self._failed_roots],
                                      'failed': [u for u in roots if u in self._failed_roots]}))
            ok = self.errors == 0
        except DownloadCancelled:
            self.q.put(('log', 'Download cancelled.'))
            ok = None
        except SystemExit as e:
            ok = False
            self.q.put(('log', f'Invalid options (exit {e.code}). Check "Extra arguments".'))
        except Exception as e:
            ok = False
            self.q.put(('log', f'ERROR: {e}'))
        self.q.put(('finished', ok))


def fmt_time(sec):
    """90.5 -> '1:30.5', 3723 -> '1:02:03'."""
    if sec is None:
        return ''
    sec = max(0.0, float(sec))
    whole = int(sec)
    tenths = int(round((sec - whole) * 10))
    if tenths == 10:
        whole, tenths = whole + 1, 0
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    out = f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'
    return out + (f'.{tenths}' if tenths else '')


_NO_WINDOW = 0x08000000 if sys.platform.startswith('win') else 0


def _run_ffmpeg(args, timeout=60):
    ff = shutil.which('ffmpeg')
    if not ff:
        raise RuntimeError('FFmpeg was not found, so previews are not available.')
    r = subprocess.run([ff, '-hide_banner', '-loglevel', 'error', '-nostdin', *args],
                       capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        msg = (r.stderr or b'').decode('utf-8', 'replace').strip().splitlines()
        raise RuntimeError(msg[-1] if msg else f'FFmpeg failed ({r.returncode})')
    return r.stdout


def _ff_input(src, seek=None):
    url, headers = src
    args = []
    if seek:
        args += ['-ss', f'{seek:.3f}']
    if headers:
        args += ['-headers', ''.join(f'{k}: {v}\r\n' for k, v in headers.items())]
    return args + ['-i', url]


def _pick_stream(formats, want_video):
    ok_proto = ('https', 'http', 'm3u8', 'm3u8_native')
    cands = []
    for f in formats or []:
        if not f.get('url') or (f.get('protocol') or 'https') not in ok_proto:
            continue
        has_v = f.get('vcodec') != 'none'
        has_a = f.get('acodec') != 'none'
        if want_video and has_v:
            # small and over plain HTTP(S) seeks fastest
            cands.append(((f.get('protocol') or 'https').startswith('m3u8'), abs((f.get('height') or 360) - 360), f))
        elif not want_video and has_a:
            cands.append((has_v, (f.get('protocol') or 'https').startswith('m3u8'), -(f.get('abr') or f.get('tbr') or 0), f))
    if not cands:
        return None
    cands.sort(key=lambda c: c[:-1])
    return cands[0][-1]


class _RangeRelay:
    """Local HTTP relay for previews. FFmpeg asks for "bytes=N-" (everything from N on); hosts such as
    YouTube refuse that with 403 and only serve bounded pieces. The relay fetches bounded pieces, as
    yt-dlp's own downloader does, and streams them to FFmpeg, so seeking works normally."""
    CHUNK = 2 << 20  # small pieces: a preview frame only needs a little data

    def __init__(self):
        self.streams = {}
        self.server = None
        self.lock = threading.Lock()

    def register(self, url, headers, size=None, chunk=None):
        import secrets
        with self.lock:
            if not self.server:
                from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
                relay = self

                class Handler(BaseHTTPRequestHandler):
                    protocol_version = 'HTTP/1.1'

                    def log_message(self, *a):
                        pass

                    def do_HEAD(self):
                        relay._serve(self, head=True)

                    def do_GET(self):
                        relay._serve(self)

                self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                self.server.daemon_threads = True
                threading.Thread(target=self.server.serve_forever, daemon=True).start()
            token = secrets.token_hex(8)
            self.streams[token] = {'url': url, 'headers': dict(headers or {}), 'size': size,
                                   'chunk': chunk or self.CHUNK,
                                   'slots': threading.BoundedSemaphore(3)}  # hosts dislike many at once
            return f'http://127.0.0.1:{self.server.server_address[1]}/s/{token}'

    def owns(self, url):
        return bool(self.server) and url.startswith(f'http://127.0.0.1:{self.server.server_address[1]}/s/')

    def _open(self, st, a, b):
        import urllib.request
        req = urllib.request.Request(st['url'], headers={**st['headers'], 'Range': f'bytes={a}-{b}'})
        return urllib.request.urlopen(req, timeout=30, context=_https_context())

    def _size(self, st):
        for attempt in range(3):
            if st['size']:
                break
            try:
                with st['slots'], self._open(st, 0, 0) as r:
                    m = re.search(r'/(\d+)', r.headers.get('Content-Range') or '')
                    st['size'] = int(m.group(1)) if m else None
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(0.6 * (attempt + 1))
        return st['size']

    def _serve(self, h, head=False):
        m = re.fullmatch(r'/s/(\w+)', h.path)
        st = self.streams.get(m.group(1)) if m else None
        if not st:
            return h.send_error(404)
        try:
            size = self._size(st)
        except Exception as e:
            return h.send_error(502, str(e)[:200])
        if not size:
            return h.send_error(502, 'The video host did not report a size')
        a, b, partial = 0, size - 1, False
        rm = re.match(r'bytes=(\d*)-(\d*)', h.headers.get('Range') or '')
        if rm and (rm.group(1) or rm.group(2)):
            partial = True
            if rm.group(1):
                a = int(rm.group(1))
                if rm.group(2):
                    b = min(int(rm.group(2)), size - 1)
            else:  # "bytes=-N": the last N bytes
                a = max(0, size - int(rm.group(2)))
        if a >= size:
            h.send_response(416)
            h.send_header('Content-Range', f'bytes */{size}')
            h.send_header('Content-Length', '0')
            h.end_headers()
            return
        h.send_response(206 if partial else 200)
        h.send_header('Content-Type', 'application/octet-stream')
        h.send_header('Accept-Ranges', 'bytes')
        h.send_header('Content-Length', str(b - a + 1))
        if partial:
            h.send_header('Content-Range', f'bytes {a}-{b}/{size}')
        h.end_headers()
        if head:
            return
        pos, failures = a, 0
        while pos <= b:
            end = min(b, pos + st['chunk'] - 1)
            got = 0
            try:
                with st['slots'], self._open(st, pos, end) as r:
                    while pos <= end:
                        data = r.read(min(65536, end - pos + 1))
                        if not data:
                            break
                        try:
                            h.wfile.write(data)
                        except OSError:
                            return  # FFmpeg hung up: it has what it needed
                        pos += len(data)
                        got += len(data)
                if pos <= end:
                    raise OSError('the host sent less than asked')
                failures = 0
            except Exception:
                # the host refused or dropped this piece: try again from where we got to
                failures = 0 if got else failures + 1
                if failures > 3:
                    return
                time.sleep(0.5 * failures)


_RANGE_RELAY = _RangeRelay()


def _needs_relay(f):
    """Streams whose host requires bounded range requests (yt-dlp marks these with a chunk size)."""
    return bool((f.get('downloader_options') or {}).get('http_chunk_size')
                and re.match(r'https?://', f.get('url') or ''))


from yt_dlp.postprocessor.common import PostProcessor  # noqa: E402


class RelayClipsPP(PostProcessor):
    """Clipped downloads are fetched by FFmpeg, whose open-ended requests YouTube refuses with 403.
    Right before such a download, point FFmpeg at the relay instead (same bounded pieces as yt-dlp)."""

    def run(self, info):
        if info.get('section_start') is None and not info.get('section_end'):
            return [], info
        for f in [info, *(info.get('requested_formats') or [])]:
            if _needs_relay(f) and not _RANGE_RELAY.owns(f['url']):
                headers = dict(f.get('http_headers') or info.get('http_headers') or {})
                try:
                    cookie = self._downloader.cookiejar.get_cookie_header(f['url'])
                    if cookie:
                        headers['Cookie'] = cookie
                except Exception:
                    pass
                chunk = (f.get('downloader_options') or {}).get('http_chunk_size')
                f['url'] = _RANGE_RELAY.register(f['url'], headers, f.get('filesize'), chunk=chunk)
        return [], info


def preview_source(url, settings):
    """Look up one video/song and pick streams for previewing (no download)."""
    opts = dict(yt_dlp.parse_options(build_args({**settings, 'clip_start': '', 'clip_end': ''})).ydl_opts)
    opts.update(noplaylist=True, playlistend=1, quiet=True, no_warnings=True, noprogress=True,
                format='bv*+ba/b/bv*/ba', postprocessors=[], logger=GuiLogger(queue.Queue()))
    for k in ('download_ranges', 'force_keyframes_at_cuts', 'download_archive'):
        opts.pop(k, None)
    with make_ydl(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        if info and info.get('_type') in ('playlist', 'multi_video'):
            entries = [e for e in (info.get('entries') or []) if e]
            if not entries:
                raise RuntimeError('That playlist is empty.')
            info = entries[0]
        if not info:
            raise RuntimeError('Could not read that link.')

        def src(f):
            if not f:
                return None
            headers = dict(f.get('http_headers') or {})
            try:
                cookie = ydl.cookiejar.get_cookie_header(f['url'])
                if cookie:
                    headers['Cookie'] = cookie
            except Exception:
                pass
            if _needs_relay(f):  # e.g. YouTube: FFmpeg's own requests would get 403 Forbidden
                return _RANGE_RELAY.register(f['url'], headers, f.get('filesize')), {}
            return f['url'], headers

        formats = info.get('formats') or ([info] if info.get('url') else [])
        return {
            'title': info.get('title') or url,
            'duration': float_or_none(info.get('duration')),
            'video': src(_pick_stream(formats, True)),
            'audio': src(_pick_stream(formats, False)),
            'url': info.get('webpage_url') or url,
            'live': bool(info.get('is_live')),
        }


def probe_duration(src):
    """Length in seconds via ffprobe, for sources that don't report one."""
    ff = shutil.which('ffprobe')
    if not ff:
        return None
    url, headers = src
    hdr = ['-headers', ''.join(f'{k}: {v}\r\n' for k, v in headers.items())] if headers else []
    try:
        r = subprocess.run([ff, '-v', 'error', *hdr, '-show_entries', 'format=duration', '-of', 'csv=p=0', url],
                           capture_output=True, timeout=30, creationflags=_NO_WINDOW)
        return float_or_none(r.stdout.decode().strip())
    except Exception:
        return None


def grab_frame(src, t, width=640, height=None):
    """PNG bytes of the frame at t seconds."""
    vf = (f'scale={width}:{height}:force_original_aspect_ratio=decrease,'
          f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black' if height else f'scale={width}:-2')
    return _run_ffmpeg([*_ff_input(src, t), '-frames:v', '1', '-vf', vf,
                        '-f', 'image2pipe', '-vcodec', 'png', '-'], timeout=45)


def render_waveform(src, width, height, color='#d97757'):
    """PNG bytes of the whole track's waveform."""
    return _run_ffmpeg([*_ff_input(src), '-filter_complex',
                        f'aformat=channel_layouts=mono,showwavespic=s={width}x{height}:colors={color}:scale=sqrt',
                        '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'], timeout=180)


def audio_snippet(src, t, seconds, path, volume=1.0):
    """Write `seconds` of audio starting at t to a WAV file, at `volume` (0..1, perceptual)."""
    gain = max(0.0, min(1.0, volume)) ** 2  # squared so the slider feels even to the ear
    _run_ffmpeg(['-y', *_ff_input(src, t), '-t', f'{seconds:.2f}', '-vn', '-af', f'volume={gain:.4f}',
                 '-ac', '2', '-ar', '44100', '-c:a', 'pcm_s16le', path], timeout=60)
    return path


class PreviewWindow(tk.Toplevel):
    """Scrub through a video (frames) or song (waveform), listen, and pick clip start/end."""
    PV_W, PV_H = 640, 360          # frame preview
    TL_W, TL_H, THUMBS = 800, 56, 8  # timeline strip

    def __init__(self, app, url):
        super().__init__(app)
        import base64
        import tempfile
        self._b64 = base64.b64encode
        self.app, self.url = app, url
        p = self.p = app.palette
        self.title('Preview & clip')
        self.configure(bg=p['bg'])
        self.transient(app)
        self.minsize(900, 560)
        self._resize_job = None
        self._frame_job = None
        self._strip_gen = 0
        self.q = queue.Queue()
        self.src = None
        self.duration = None
        self.t = 0.0
        self.start = self.end = None
        self._req = 0
        self._inflight = self._want = False
        self._debounce = None
        self._frame_img = None
        self._strip_imgs = []
        self._closed = False
        self._snip_n = 0
        self._tmp = tempfile.mkdtemp(prefix='ytdlp_gui_preview_')
        try:
            import winsound
            self._winsound = winsound
        except ImportError:
            self._winsound = None
        try:
            cr = clip_range(app._collect())
        except SettingsError:
            cr = None
        if cr:
            self.start, self.end = cr

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)  # the frame preview takes any extra room
        self.lbl_title = ttk.Label(outer, text='Loading…', style='Head.TLabel', wraplength=self.TL_W)
        self.lbl_title.grid(row=0, column=0, sticky='w')

        self.pv = tk.Canvas(outer, width=self.PV_W, height=self.PV_H, bg='#000000', highlightthickness=0)
        self.pv.grid(row=1, column=0, sticky='nsew', pady=(8, 8))
        self.pv.bind('<Configure>', self._on_pv_resize)
        self.pv_img = self.pv.create_image(self.PV_W // 2, self.PV_H // 2)
        self.pv_text = self.pv.create_text(self.PV_W // 2, self.PV_H // 2, text='Looking up the video…',
                                           fill='#cccccc', font=('Segoe UI', 11), width=self.PV_W - 60,
                                           justify='center')

        self.tl = tk.Canvas(outer, width=self.TL_W, height=self.TL_H + 18, bg=p['field'],
                            highlightthickness=1, highlightbackground=p['border'], cursor='hand2')
        self.tl.grid(row=2, column=0, sticky='ew')
        self.tl.bind('<Configure>', self._on_tl_resize)
        self._drag = None
        self.tl.bind('<Button-1>', self._tl_press)
        self.tl.bind('<B1-Motion>', self._tl_motion)
        self.tl.bind('<ButtonRelease-1>', self._tl_release)
        self.tl.bind('<Motion>', self._tl_hover)
        self.pos = tk.DoubleVar(value=0.0)
        self._syncing = False
        self.scale = ttk.Scale(outer, from_=0, to=1, orient='horizontal', length=self.TL_W, takefocus=False,
                               variable=self.pos, command=self._on_scale, style='Seek.Horizontal.TScale')
        self.scale.bind('<ButtonRelease-1>', lambda e: self.focus_set(), add='+')
        self.scale.grid(row=3, column=0, sticky='ew', pady=(6, 0))

        ctl = ttk.Frame(outer)
        ctl.grid(row=4, column=0, sticky='ew', pady=(8, 0))
        for text, d in (('−5s', -5), ('−1s', -1), ('−0.1s', -0.1)):
            ttk.Button(ctl, text=text, width=5, command=lambda d=d: self._nudge(d)).pack(side='left', padx=(0, 3))
        self.lbl_time = ttk.Label(ctl, text='0:00', style='Head.TLabel', width=10, anchor='center')
        self.lbl_time.pack(side='left', padx=4)
        for text, d in (('+0.1s', 0.1), ('+1s', 1), ('+5s', 5)):
            ttk.Button(ctl, text=text, width=5, command=lambda d=d: self._nudge(d)).pack(side='left', padx=(3, 0))
        ttk.Button(ctl, text='Set start  [', command=self._set_start).pack(side='left', padx=(16, 3))
        ttk.Button(ctl, text='Set end  ]', command=self._set_end).pack(side='left', padx=3)
        self.btn_stop = ttk.Button(ctl, text='■', width=3, command=self._stop_sound)
        self.btn_hear = ttk.Button(ctl, text='▶ Hear 5 s', command=self._hear)
        if self._winsound:
            self.btn_stop.pack(side='right')
            self.btn_hear.pack(side='right', padx=(0, 3))
            vol = app.preview_volume
            self.lbl_vol = ttk.Label(ctl, width=5, anchor='e')
            vs = ttk.Scale(ctl, from_=0, to=100, orient='horizontal', length=110, variable=vol, takefocus=False,
                           command=self._on_volume, style='Seek.Horizontal.TScale')
            vs.pack(side='right', padx=(0, 10))
            # give the keyboard back to the timeline, so arrows/Home/End never move the volume
            vs.bind('<ButtonRelease-1>', lambda e: self.focus_set(), add='+')
            self.lbl_vol.pack(side='right')
            ttk.Label(ctl, text='Volume').pack(side='right', padx=(10, 4))
            self._vol_job = None
            self._playing_from = None
            self._show_volume()

        bot = ttk.Frame(outer)
        bot.grid(row=5, column=0, sticky='ew', pady=(10, 0))
        self.lbl_sel = ttk.Label(bot, text='')
        self.lbl_sel.pack(side='left')
        ttk.Button(bot, text='Apply', style='Accent.TButton', command=self._apply).pack(side='right')
        ttk.Button(bot, text='Cancel', command=self._close).pack(side='right', padx=6)
        ttk.Button(bot, text='Clear', command=self._clear).pack(side='right')
        self.lbl_hint = ttk.Label(outer, style='Hint.TLabel',
                                  text='Click or drag on the timeline · ←/→ 1 s · Shift 5 s · Ctrl 0.1 s · '
                                       'Home/End go to clip start/end · [ ] set start/end'
                                       + (' · Space to hear' if self._winsound else '')
                                       + ' · Make the window bigger for a finer timeline')
        self.lbl_hint.grid(row=6, column=0, sticky='w', pady=(6, 0))

        self.bind('<Left>', lambda e: self._nudge(-1))
        self.bind('<Right>', lambda e: self._nudge(1))
        self.bind('<Shift-Left>', lambda e: self._nudge(-5))
        self.bind('<Shift-Right>', lambda e: self._nudge(5))
        self.bind('<Control-Left>', lambda e: self._nudge(-0.1))
        self.bind('<Control-Right>', lambda e: self._nudge(0.1))
        self.bind('<Home>', lambda e: self._seek(self.start or 0, now=True))
        self.bind('<End>', lambda e: self._seek(self.end if self.end is not None else (self.duration or 0), now=True))
        self.bind('[', lambda e: self._set_start())
        self.bind(']', lambda e: self._set_end())
        self.bind('<space>', lambda e: self._hear())
        self.bind('<Escape>', lambda e: self._close())
        self.protocol('WM_DELETE_WINDOW', self._close)

        # buttons and sliders don't hold the keyboard: keys always drive the timeline
        for frame in (ctl, bot):
            for w in frame.winfo_children():
                try:
                    w.configure(takefocus=False)
                except tk.TclError:
                    pass
        self._update_labels()
        app._set_titlebar(self)
        self.focus_set()
        self._bg(lambda: ('src', self._load()))
        self.after(40, self._pump)

    # ---- background work ----
    def _bg(self, fn, on_error='error'):
        def run():
            try:
                self.q.put(fn())
            except Exception as e:
                self.q.put((on_error, str(e).replace('ERROR: ', '')))
        threading.Thread(target=run, daemon=True).start()

    def _load(self):
        src = preview_source(self.url, self.app._collect())
        if not src['duration'] and not src['live']:
            s = src['video'] or src['audio']
            src['duration'] = probe_duration(s) if s else None
        return src

    def _pump(self):
        if self._closed:
            return
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == 'src':
                    self._on_src(item[1])
                elif kind == 'frame':
                    _, rid, png = item
                    self._inflight = False
                    self._frame_retry = 0
                    if rid == self._req and png:
                        self._frame_img = tk.PhotoImage(data=self._b64(png).decode())
                        self.pv.itemconfigure(self.pv_img, image=self._frame_img)
                        self.pv.itemconfigure(self.pv_text, text='')
                    if self._want:
                        self._want = False
                        self._fetch_frame()
                    self._start_strip_once()
                elif kind == 'frame_error':
                    self._inflight = False
                    if self._want:
                        self._want = False
                        self._fetch_frame()
                    elif getattr(self, '_frame_retry', 0) < 2:  # hosts sometimes refuse once; try again
                        self._frame_retry = getattr(self, '_frame_retry', 0) + 1
                        self.pv.itemconfigure(self.pv_text, text='Loading frame… (retrying)')
                        self._debounce = self.after(800 * self._frame_retry, self._fetch_frame)
                    else:
                        self._frame_retry = 0
                        self._frame_img = None
                        self.pv.itemconfigure(self.pv_img, image='')
                        self.pv.itemconfigure(self.pv_text, text=f"Couldn't load this frame:\n{item[1]}\n\n"
                                                                 'Move the slider to try another spot.')
                    self._start_strip_once()
                elif kind == 'strip_error':
                    self._strip_failed = getattr(self, '_strip_failed', 0) + 1
                    if self._strip_failed == 1:
                        self.lbl_hint.configure(text="Some timeline thumbnails couldn't load — the timeline "
                                                     'and frame preview still work.')
                elif kind == 'thumb':
                    _, gen, x, png = item
                    if gen == self._strip_gen:  # ignore thumbnails made for an older window size
                        img = tk.PhotoImage(data=self._b64(png).decode())
                        self._strip_imgs.append(img)
                        self.tl.create_image(x, 0, image=img, anchor='nw', tags='bgimg')
                        self.tl.tag_raise('ov')
                elif kind == 'wave':
                    _, gen, png = item
                    if gen == self._strip_gen:
                        img = tk.PhotoImage(data=self._b64(png).decode())
                        self._strip_imgs.append(img)
                        self.tl.create_image(0, 0, image=img, anchor='nw', tags='bgimg')
                        self.tl.tag_raise('ov')
                elif kind == 'snip':
                    self.btn_hear.configure(text='▶ Hear 5 s')
                    try:
                        w = self._winsound
                        w.PlaySound(item[1], w.SND_FILENAME | w.SND_ASYNC | w.SND_NODEFAULT)
                        self._playing_from, self._playing_until = item[2], time.time() + 5
                    except Exception as e:
                        self.lbl_sel.configure(text=f'Could not play audio: {e}')
                elif kind == 'snip_error':
                    self.btn_hear.configure(text='▶ Hear 5 s')
                    self.lbl_sel.configure(text=f"Couldn't get audio here: {item[1]}")
                elif kind == 'error':
                    self.lbl_title.configure(text='Preview not available')
                    self.pv.itemconfigure(self.pv_text, text=item[1])
        except queue.Empty:
            pass
        self.after(40, self._pump)

    def _on_src(self, src):
        self.src = src
        self.duration = src['duration']
        self.lbl_title.configure(text=src['title'])
        if src['live'] or not self.duration:
            self.pv.itemconfigure(self.pv_text, text="This can't be previewed: live streams and links without a "
                                                     'known length have no timeline.')
            return
        if self.end is not None and self.end > self.duration:
            self.end = None
        self.scale.configure(to=self.duration)
        self._draw_ticks()
        if src['video']:
            self.pv.itemconfigure(self.pv_text, text='Loading frame…')
            self._strip_started = False  # thumbnails start once the first frame is in (or failed)
            self.after(4000, self._start_strip_once)  # ...or after a few seconds regardless
        else:
            self.pv.itemconfigure(self.pv_text, text='♪  Audio only\n\nUse the waveform below to find your spot'
                                  + (', and ▶ Hear to listen.' if self._winsound else '.'))
            self._strip_started = True
            self._rebuild_strip()
        if not (src['audio'] or src['video']):
            self.btn_hear.state(['disabled'])
        self._seek(self.start or 0, now=True)

    # ---- resizing ----
    _thumb_slots = threading.Semaphore(4)  # don't start dozens of FFmpegs at once

    def _start_strip_once(self):
        if not getattr(self, '_strip_started', True) and not self._closed:
            self._strip_started = True
            self._rebuild_strip()

    def _rebuild_strip(self):
        """(Re)make the thumbnail strip or waveform for the current timeline width."""
        self._resize_job = None
        src = self.src
        if not src or not self.duration:
            return
        self._strip_gen += 1
        gen, width = self._strip_gen, self.TL_W
        self.tl.delete('bgimg')
        self._strip_imgs = []
        if src['video']:
            n = max(4, round(width / 100))  # about one thumbnail per 100 px
            for i in range(n):
                x0, x1 = round(i * width / n), round((i + 1) * width / n)
                t = self.duration * (i + 0.5) / n

                def job(x0=x0, w=x1 - x0, t=t):
                    with self._thumb_slots:
                        if gen != self._strip_gen or self._closed:
                            return ('thumb', -1, 0, b'')
                        return ('thumb', gen, x0, grab_frame(src['video'], t, w, self.TL_H))
                self._bg(job, on_error='strip_error')
        elif src['audio']:
            self._bg(lambda: ('wave', gen, render_waveform(src['audio'], width, self.TL_H, self.p['accent'])),
                     on_error='strip_error')

    def _on_tl_resize(self, e):
        if abs(e.width - self.TL_W) < 2:
            return
        self.TL_W = e.width
        self.lbl_title.configure(wraplength=max(200, e.width))
        self.lbl_hint.configure(wraplength=max(200, e.width))
        if self.duration:
            self._draw_ticks()
            self._draw_overlay()
        if self._resize_job:
            self.after_cancel(self._resize_job)
        if getattr(self, '_strip_started', True):
            self._resize_job = self.after(350, self._rebuild_strip)  # once the user stops dragging the edge

    def _on_pv_resize(self, e):
        if (e.width, e.height) == (self.PV_W, self.PV_H):
            return
        self.PV_W, self.PV_H = e.width, e.height
        self.pv.coords(self.pv_img, e.width // 2, e.height // 2)
        self.pv.coords(self.pv_text, e.width // 2, e.height // 2)
        self.pv.itemconfigure(self.pv_text, width=max(100, e.width - 60))
        if self.src and self.src['video']:
            if self._frame_job:
                self.after_cancel(self._frame_job)
            self._frame_job = self.after(350, self._refetch_frame)

    def _refetch_frame(self):
        self._frame_job = None
        self._fetch_frame()

    # ---- timeline ----
    def _x(self, t):
        return 0 if not self.duration else max(0, min(self.TL_W, t / self.duration * self.TL_W))

    def _draw_ticks(self):
        self.tl.delete('tick')
        for f in (0, 0.25, 0.5, 0.75, 1):
            x = f * self.TL_W
            anchor = 'nw' if f == 0 else 'ne' if f == 1 else 'n'
            self.tl.create_text(x + (2 if f == 0 else -2 if f == 1 else 0), self.TL_H + 2,
                                text=fmt_time(f * self.duration), anchor=anchor, fill=self.p['muted'],
                                font=('Segoe UI', 8), tags='tick')

    def _draw_overlay(self):
        self.tl.delete('ov')
        a = self.p['accent']
        if self.duration and (self.start is not None or self.end is not None):
            x1, x2 = self._x(self.start or 0), self._x(self.end if self.end is not None else self.duration)
            self.tl.create_rectangle(x1, 1, x2, self.TL_H - 1, outline=a, width=3, tags='ov')
            self.tl.create_rectangle(x1, self.TL_H - 5, x2, self.TL_H, fill=a, outline='', tags='ov')
            for xe in (x1, x2):  # grab tabs for dragging the edges
                self.tl.create_rectangle(xe - 4, self.TL_H // 2 - 12, xe + 4, self.TL_H // 2 + 12,
                                         fill=a, outline='#ffffff', width=1, tags='ov')
        x = self._x(self.t)
        self.tl.create_line(x, 0, x, self.TL_H, fill='#ffffff', width=3, tags='ov')
        self.tl.create_line(x, 0, x, self.TL_H, fill='#000000', width=1, tags='ov')
        self.tl.create_polygon(x - 7, 0, x + 7, 0, x, 10, fill='#ffffff', outline='#000000', tags='ov')

    def _update_labels(self):
        self.lbl_time.configure(text=fmt_time(self.t))
        self._syncing = True
        try:
            self.pos.set(self.t)
        finally:
            self._syncing = False
        if self.start is None and self.end is None:
            self.lbl_sel.configure(text='No section chosen — the whole thing will download')
        else:
            s = self.start or 0
            e = self.end if self.end is not None else self.duration
            length = f' · Length {fmt_time(e - s)}' if e is not None else ''
            self.lbl_sel.configure(text=f'Start {fmt_time(s)} · End {fmt_time(self.end) if self.end is not None else "end"}'
                                        + length)
        self._draw_overlay()

    # ---- moving around ----
    def _seek(self, t, now=False, live=False):
        if not self.duration:
            return
        self.t = max(0.0, min(float(t), max(0.0, self.duration - 0.05)))
        self._update_labels()
        if self.src and self.src['video']:
            if self._debounce:
                self.after_cancel(self._debounce)
                self._debounce = None
            if live:
                self._fetch_frame()  # while dragging: keep frames coming, newest position wins
            else:
                self._debounce = self.after(1 if now else 120, self._fetch_frame)

    def _edge_xs(self):
        if not self.duration or (self.start is None and self.end is None):
            return None
        return self._x(self.start or 0), self._x(self.end if self.end is not None else self.duration)

    def _edge_at(self, x):
        xs = self._edge_xs()
        if not xs:
            return None
        ds, de = abs(x - xs[0]), abs(x - xs[1])
        if min(ds, de) > 7:
            return None
        return 'start' if ds <= de else 'end'

    def _tl_hover(self, e):
        self.tl.configure(cursor='sb_h_double_arrow' if self._edge_at(e.x) else 'hand2')

    def _tl_press(self, e):
        self._drag = self._edge_at(e.x) or 'play'
        self._tl_motion(e)

    def _tl_motion(self, e):
        if not self.duration or not self._drag:
            return
        t = max(0.0, min(self.duration, e.x / self.TL_W * self.duration))
        if self._drag == 'start':
            end = self.end if self.end is not None else self.duration
            self.start = min(t, end - 0.1)
            self._seek(self.start, live=True)
        elif self._drag == 'end':
            self.end = max(t, (self.start or 0) + 0.1)
            self._seek(self.end, live=True)
        else:
            self._seek(t, live=True)

    def _tl_release(self, _e):
        self._drag = None

    def _on_scale(self, value):
        if not self._syncing:
            self._seek(float(value), live=True)

    def _nudge(self, d):
        self._seek(round(self.t + d, 2), now=True)

    def _fetch_frame(self):
        self._debounce = None
        if self._inflight:
            self._want = True
            return
        self._inflight = True
        self._req += 1
        rid, t, src = self._req, self.t, self.src['video']

        def work():
            try:
                return ('frame', rid, grab_frame(src, t, self.PV_W, self.PV_H))
            except Exception as e:
                return ('frame_error', str(e))
        self._bg(work)

    def _set_start(self):
        self.start = self.t
        if self.end is not None and self.end <= self.start:
            self.end = None
        self._update_labels()

    def _set_end(self):
        if self.t <= (self.start or 0):
            self.lbl_sel.configure(text='The end has to be after the start — move right first')
            return
        self.end = self.t
        self._update_labels()

    def _clear(self):
        self.start = self.end = None
        self._update_labels()

    # ---- listening ----
    def _volume(self):
        try:
            return max(0.0, min(100.0, float(self.app.preview_volume.get()))) / 100
        except (tk.TclError, ValueError):
            return 0.5

    def _show_volume(self):
        self.lbl_vol.configure(text=f'{round(self._volume() * 100)}%')

    def _on_volume(self, _value=None):
        self._show_volume()
        # if a snippet is playing, replay it from the same spot at the new volume (once the slider settles)
        if self._playing_from is not None and time.time() < self._playing_until:
            if self._vol_job:
                self.after_cancel(self._vol_job)
            self._vol_job = self.after(300, lambda: self._hear(start=self._playing_from))

    def _hear(self, start=None):
        if not self._winsound or not self.src or not self.duration:
            return
        s = self.src['audio'] or self.src['video']
        if not s:
            return
        self._vol_job = None
        self._stop_sound()
        vol = self._volume()
        if vol <= 0:
            self.lbl_sel.configure(text='Volume is at 0% — turn it up to hear the audio')
            return
        self.btn_hear.configure(text='…')
        self._snip_n += 1
        path = os.path.join(self._tmp, f'snip{self._snip_n}.wav')
        t = self.t if start is None else start

        def work():
            try:
                return ('snip', audio_snippet(s, t, 5, path, vol), t)
            except Exception as e:
                return ('snip_error', str(e))
        self._bg(work)

    def _stop_sound(self):
        self._playing_from = None
        if self._winsound:
            try:
                self._winsound.PlaySound(None, 0)
            except Exception:
                pass

    # ---- finish ----
    def _apply(self):
        s, e = self.start, self.end
        if self.duration and e is not None and e >= self.duration - 0.05:
            e = None  # "to the end"
        self.app.clip_start.set(fmt_time(s) if s else '')
        self.app.clip_end.set(fmt_time(e) if e is not None else '')
        if s or e is not None:
            self.app.status.set('Clip set from preview — it applies to every link in this batch')
        self._close()

    def _close(self):
        self._closed = True
        for job in (self._resize_job, self._frame_job, self._debounce, getattr(self, '_vol_job', None)):
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
        self._stop_sound()
        shutil.rmtree(self._tmp, ignore_errors=True)
        try:
            self.app._save_settings()
        except Exception:
            pass
        self.destroy()


class ArgPicker(tk.Toplevel):
    """Checkbox list of every yt-dlp option, written back into 'Extra arguments'."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title('Pick yt-dlp options')
        self.geometry('900x640')
        self.minsize(640, 460)
        self.transient(app)
        pal = app.palette
        self.configure(bg=pal['bg'])

        self.groups = get_cli_options()
        self.by_alias = {}
        self.state = {}
        for _title, opts in self.groups:
            for o in opts:
                self.state[o['name']] = {'on': False, 'value': ''}
                for a in o['aliases']:
                    self.by_alias[a] = o
        self.leftover = self._load(app.extra_args.get())
        self.current = None

        outer = ttk.Frame(self, padding=10)
        outer.pack(fill='both', expand=True)

        top = ttk.Frame(outer)
        top.pack(fill='x')
        ttk.Label(top, text='Search').pack(side='left')
        self.query = tk.StringVar()
        self.query.trace_add('write', lambda *_: self._fill())
        ent = ttk.Entry(top, textvariable=self.query)
        ent.pack(side='left', fill='x', expand=True, padx=8)
        self.only_on = tk.BooleanVar()
        ttk.Checkbutton(top, text='Show selected only', variable=self.only_on,
                        command=self._fill).pack(side='left')

        mid = ttk.Frame(outer)
        mid.pack(fill='both', expand=True, pady=8)
        self.tree = ttk.Treeview(mid, columns=('value', 'desc'), selectmode='browse')
        self.tree.heading('#0', text='Option')
        self.tree.heading('value', text='Value')
        self.tree.heading('desc', text='Description')
        self.tree.column('#0', width=260, stretch=False)
        self.tree.column('value', width=150, stretch=False)
        self.tree.column('desc', width=420)
        sb = ttk.Scrollbar(mid, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.tree.tag_configure('group', foreground=pal['accent'])
        self.tree.tag_configure('on', foreground=pal['fg'])
        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        self.tree.bind('<Button-1>', self._on_click)
        self.tree.bind('<space>', lambda e: (self._toggle(self.current), 'break')[1])

        det = ttk.LabelFrame(outer, text='Details', padding=8)
        det.pack(fill='x')
        det.columnconfigure(1, weight=1)
        self.d_name = ttk.Label(det, text='Click an option to see what it does.', style='Head.TLabel')
        self.d_name.grid(row=0, column=0, columnspan=3, sticky='w')
        self.d_help = ttk.Label(det, text='', wraplength=840, justify='left')
        self.d_help.grid(row=1, column=0, columnspan=3, sticky='w', pady=(4, 6))
        self.d_vlabel = ttk.Label(det, text='Value')
        self.d_vlabel.grid(row=2, column=0, sticky='w')
        self.value = tk.StringVar()
        self.value.trace_add('write', self._on_value)
        self.d_value = ttk.Entry(det, textvariable=self.value, state='disabled')
        self.d_value.grid(row=2, column=1, sticky='ew', padx=8)
        self.d_hint = ttk.Label(det, text='', foreground=pal['muted'])
        self.d_hint.grid(row=2, column=2, sticky='w')

        bot = ttk.Frame(outer)
        bot.pack(fill='x', pady=(8, 0))
        self.count = tk.StringVar()
        ttk.Label(bot, textvariable=self.count).pack(side='left')
        ttk.Button(bot, text='Apply', style='Accent.TButton', command=self._apply).pack(side='right')
        ttk.Button(bot, text='Cancel', command=self.destroy).pack(side='right', padx=6)
        ttk.Button(bot, text='Clear all', command=self._clear).pack(side='right')

        self._fill()
        app._set_titlebar(self)
        ent.focus_set()
        self.grab_set()

    # parse the current "Extra arguments" so already-used options start ticked
    def _load(self, text):
        try:
            toks = split_args(text)
        except ValueError:
            return text.strip()
        left, i = [], 0
        while i < len(toks):
            t = toks[i]
            key, val = (t.split('=', 1) + [None])[:2] if t.startswith('--') else (t, None)
            o = self.by_alias.get(key)
            if o is None:
                left.append(t)
                i += 1
                continue
            st = self.state[o['name']]
            st['on'] = True
            if o['takes_value']:
                if val is not None:
                    st['value'] = val
                else:
                    vals = toks[i + 1:i + 1 + o['nargs']]
                    if o['nargs'] == 1:
                        st['value'] = vals[0] if vals else ''
                    else:
                        st['value'] = ' '.join(quote_arg(v) for v in vals)
                    i += len(vals)
            i += 1
        return ' '.join(quote_arg(t) for t in left)

    def _label(self, o):
        return ('☑  ' if self.state[o['name']]['on'] else '☐  ') + o['name']

    def _fill(self):
        q = self.query.get().strip().lower()
        only = self.only_on.get()
        self.tree.delete(*self.tree.get_children())
        self.iid_to_opt = {}
        for title, opts in self.groups:
            shown = [o for o in opts
                     if (not only or self.state[o['name']]['on'])
                     and (not q or q in o['name'].lower() or q in o['help'].lower()
                          or any(q in a.lower() for a in o['aliases']))]
            if not shown:
                continue
            gid = self.tree.insert('', 'end', text=title, open=True, tags=('group',))
            for o in shown:
                st = self.state[o['name']]
                desc = o['help'].split('. ')[0]
                iid = self.tree.insert(gid, 'end', text=self._label(o),
                                       values=(st['value'], desc), tags=('on',) if st['on'] else ())
                self.iid_to_opt[iid] = o
        self._update_count()

    def _update_count(self):
        n = sum(1 for s in self.state.values() if s['on'])
        self.count.set(f'{n} option(s) selected')

    def _refresh_row(self, iid):
        o = self.iid_to_opt[iid]
        st = self.state[o['name']]
        self.tree.item(iid, text=self._label(o), tags=('on',) if st['on'] else ())
        self.tree.set(iid, 'value', st['value'])
        self._update_count()

    def _on_click(self, e):
        iid = self.tree.identify_row(e.y)
        if iid in self.iid_to_opt and self.tree.identify_column(e.x) == '#0':
            el = self.tree.identify_element(e.x, e.y)
            if 'indicator' not in el:
                self.tree.selection_set(iid)
                self._toggle(iid)
                return 'break'

    def _toggle(self, iid):
        if iid not in self.iid_to_opt:
            return
        o = self.iid_to_opt[iid]
        st = self.state[o['name']]
        st['on'] = not st['on']
        self._refresh_row(iid)
        if st['on'] and o['takes_value'] and not st['value']:
            self.d_value.focus_set()

    def _on_select(self, _e=None):
        sel = self.tree.selection()
        iid = sel[0] if sel else None
        self.current = iid
        o = self.iid_to_opt.get(iid)
        if not o:
            return
        self.d_name.configure(text='  '.join(o['aliases']) +
                              (f'  {o["metavar"]}' if o['takes_value'] else ''))
        self.d_help.configure(text=o['help'] or '(no description)')
        self._loading = True
        self.value.set(self.state[o['name']]['value'])
        self._loading = False
        if o['takes_value']:
            self.d_value.configure(state='normal')
            hint = f'{o["nargs"]} values, separated by spaces' if o['nargs'] > 1 else o['metavar']
            self.d_hint.configure(text=hint)
        else:
            self.d_value.configure(state='disabled')
            self.d_hint.configure(text='on/off option — no value needed')

    def _on_value(self, *_):
        if getattr(self, '_loading', False):
            return
        o = self.iid_to_opt.get(self.current)
        if not o or not o['takes_value']:
            return
        st = self.state[o['name']]
        st['value'] = self.value.get()
        if st['value'].strip():
            st['on'] = True
        self._refresh_row(self.current)

    def _clear(self):
        for st in self.state.values():
            st['on'] = False
            st['value'] = ''
        self.leftover = ''
        self._fill()

    def _apply(self):
        parts, missing = [], []
        for _title, opts in self.groups:
            for o in opts:
                st = self.state[o['name']]
                if not st['on']:
                    continue
                if not o['takes_value']:
                    parts.append(o['name'])
                    continue
                try:
                    vals = split_args(st['value'])
                except ValueError:
                    vals = [st['value']]
                if len(vals) < o['nargs'] or not st['value'].strip():
                    missing.append(o['name'])
                    continue
                if o['nargs'] == 1:
                    vals = [st['value'].strip()]
                parts.append(o['name'] + ' ' + ' '.join(quote_arg(v) for v in vals[:o['nargs']]))
        if missing:
            messagebox.showwarning('Missing values', 'These options need a value:\n\n' + '\n'.join(missing),
                                   parent=self)
            return
        if self.leftover:
            parts.append(self.leftover)
        self.app.extra_args.set(' '.join(parts))
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'{APP_NAME}' + (f'  (build {BUILD})' if BUILD else '') + f'  —  yt-dlp {YTDLP_VERSION}')
        self._updating = False
        self.geometry('980x720')
        self.minsize(820, 600)
        self.q = queue.Queue()
        self.worker = None
        self.rows = {}
        self.settings = dict(DEFAULTS)
        self._col_widths = {}
        self._skipped = 0
        self._load_settings()
        self.palette = PALETTES['dark' if self.settings.get('dark', True) else 'light']
        self._style()
        self._build()
        self._apply_settings()
        self._apply_theme()
        self._on_mode()
        self.after(100, self._poll)
        self.after(2000, lambda: self._check_updates(quiet=True))
        self._clip_last = self._read_clipboard()
        self.after(700, self._poll_clipboard)
        self._enable_drag_and_drop()
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        if not shutil.which('ffmpeg'):
            self._log('WARNING: FFmpeg was not found on PATH. Merging best video+audio, '
                      'audio conversion and embedding need it. Get it from https://ffmpeg.org '
                      'or https://github.com/yt-dlp/FFmpeg-Builds')
        if not any(shutil.which(x) for x in ('deno', 'node', 'bun', 'qjs')):
            self._log('WARNING: No JavaScript runtime (Deno) found. YouTube downloads may be '
                      'limited or fail. Install Deno from https://deno.land')

    # ---------- look ----------
    def _style(self):
        st = ttk.Style(self)
        st.theme_use('clam')  # the only built-in theme that can be fully recoloured
        st.configure('Accent.TButton', font=('Segoe UI', 10, 'bold'), padding=(14, 6))
        st.configure('Treeview', rowheight=24)
        st.configure('Head.TLabel', font=('Segoe UI', 10, 'bold'))

    def _apply_theme(self):
        p = self.palette = PALETTES['dark' if self.dark.get() else 'light']
        st = ttk.Style(self)
        st.configure('.', background=p['bg'], foreground=p['fg'], fieldbackground=p['field'],
                     bordercolor=p['border'], darkcolor=p['bg'], lightcolor=p['bg'],
                     troughcolor=p['field'], selectbackground=p['accent'],
                     selectforeground=p['on_accent'], insertcolor=p['fg'], focuscolor=p['accent'],
                     arrowcolor=p['muted'], font=('Segoe UI', 9))
        st.map('.', foreground=[('disabled', p['disabled'])])
        st.configure('TFrame', background=p['bg'])
        st.configure('TLabel', background=p['bg'], foreground=p['fg'])
        st.configure('Head.TLabel', background=p['bg'], foreground=p['fg'])
        st.configure('Active.TLabel', background=p['bg'], foreground=p['accent'])
        st.configure('Bad.TLabel', background=p['bg'], foreground=p['err'])
        st.configure('Hint.TLabel', background=p['bg'], foreground=p['muted'])
        st.configure('TLabelframe', background=p['bg'], bordercolor=p['border'])
        st.configure('TLabelframe.Label', background=p['bg'], foreground=p['muted'])
        st.configure('TButton', background=p['surface'], foreground=p['fg'], bordercolor=p['border'],
                     lightcolor=p['surface'], darkcolor=p['surface'], padding=(10, 4))
        st.map('TButton', background=[('disabled', p['bg']), ('pressed', p['hover']), ('active', p['hover'])],
               lightcolor=[('active', p['hover'])], darkcolor=[('active', p['hover'])])
        st.configure('Accent.TButton', background=p['accent'], foreground=p['on_accent'],
                     bordercolor=p['accent'], lightcolor=p['accent'], darkcolor=p['accent'])
        st.map('Accent.TButton',
               background=[('disabled', p['surface']), ('pressed', p['accent_hover']), ('active', p['accent_hover'])],
               foreground=[('disabled', p['disabled'])],
               lightcolor=[('active', p['accent_hover'])], darkcolor=[('active', p['accent_hover'])])
        for w in ('TEntry', 'TCombobox', 'TSpinbox'):
            st.configure(w, fieldbackground=p['field'], foreground=p['fg'], bordercolor=p['border'],
                         lightcolor=p['field'], darkcolor=p['field'], background=p['surface'])
            st.map(w, fieldbackground=[('disabled', p['bg']), ('readonly', p['field'])],
                   foreground=[('disabled', p['disabled'])],
                   bordercolor=[('focus', p['accent'])], lightcolor=[('focus', p['accent'])],
                   selectbackground=[('readonly', p['field'])], selectforeground=[('readonly', p['fg'])],
                   background=[('active', p['hover'])])
        for w in ('TCheckbutton', 'TRadiobutton'):
            st.configure(w, background=p['bg'], foreground=p['fg'], indicatorbackground=p['field'],
                         indicatorforeground=p['accent'], upperbordercolor=p['border'],
                         lowerbordercolor=p['border'])
            st.map(w, background=[('active', p['bg'])],
                   indicatorbackground=[('disabled', p['bg']), ('pressed', p['hover']), ('selected', p['field'])])
        st.configure('Treeview', background=p['field'], fieldbackground=p['field'], foreground=p['fg'],
                     bordercolor=p['border'], lightcolor=p['field'], darkcolor=p['field'])
        st.map('Treeview', background=[('selected', p['select'])], foreground=[('selected', p['fg'])])
        st.configure('Treeview.Heading', background=p['surface'], foreground=p['muted'],
                     bordercolor=p['border'], lightcolor=p['surface'], darkcolor=p['surface'],
                     font=('Segoe UI', 9, 'bold'))
        st.map('Treeview.Heading', background=[('active', p['hover'])])
        st.configure('Seek.Horizontal.TScale', background=p['accent'], troughcolor=p['field'],
                     bordercolor=p['border'], lightcolor=p['accent'], darkcolor=p['accent'])
        st.map('Seek.Horizontal.TScale', background=[('active', p['accent_hover'])])
        st.configure('TProgressbar', background=p['accent'], troughcolor=p['field'],
                     bordercolor=p['border'], lightcolor=p['accent'], darkcolor=p['accent'])
        st.configure('TScrollbar', background=p['surface'], troughcolor=p['bg'], bordercolor=p['bg'],
                     arrowcolor=p['muted'], lightcolor=p['surface'], darkcolor=p['surface'])
        st.map('TScrollbar', background=[('active', p['hover'])])
        st.configure('TPanedwindow', background=p['bg'])
        st.configure('Sash', sashthickness=6, gripcount=0, background=p['bg'])

        self.configure(bg=p['bg'])
        # Combobox drop-down lists are plain Tk listboxes
        for opt, val in (('background', p['field']), ('foreground', p['fg']),
                         ('selectBackground', p['accent']), ('selectForeground', p['on_accent'])):
            self.option_add(f'*TCombobox*Listbox.{opt}', val)
        for cb in self._all_widgets(self, ttk.Combobox):
            try:
                lb = self.tk.call('ttk::combobox::PopdownWindow', cb) + '.f.l'
                self.tk.call(lb, 'configure', '-background', p['field'], '-foreground', p['fg'],
                             '-selectbackground', p['accent'], '-selectforeground', p['on_accent'])
            except tk.TclError:
                pass
        for t in (self.url_text, self.log):
            t.configure(background=p['field'], foreground=p['fg'], insertbackground=p['fg'],
                        selectbackground=p['select'], selectforeground=p['fg'],
                        highlightthickness=1, highlightbackground=p['border'],
                        highlightcolor=p['accent'], relief='flat', borderwidth=4)
        self.log.tag_configure('warn', foreground=p['warn'])
        self.log.tag_configure('err', foreground=p['err'])
        self.tree.tag_configure('done', foreground=p['ok'])
        self.tree.tag_configure('skip', foreground=p['muted'])
        self.tree.tag_configure('err', foreground=p['err'])
        self._set_titlebar(self)

    def _all_widgets(self, root, cls):
        out = []
        for w in root.winfo_children():
            if isinstance(w, cls):
                out.append(w)
            out += self._all_widgets(w, cls)
        return out

    def _set_titlebar(self, win):
        """Dark window title bar on Windows 10/11."""
        if not sys.platform.startswith('win'):
            return
        try:
            import ctypes
            win.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
            val = ctypes.c_int(1 if self.dark.get() else 0)
            for attr in (20, 19):
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), 4) == 0:
                    break
            # nudge Windows to repaint the title bar
            win.withdraw()
            win.deiconify()
        except Exception:
            pass

    def _toggle_theme(self):
        self._apply_theme()
        self._save_settings()

    # ---------- layout ----------
    def _build(self):
        pad = {'padx': 8, 'pady': 4}
        root = ttk.Frame(self, padding=10)
        root.pack(fill='both', expand=True)

        # URLs
        self.banner = tk.Frame(root)  # 'update available' strip, shown when needed
        top = ttk.LabelFrame(root, text='URLs (one per line — videos, playlists or channels)', padding=8)
        self._first_section = top
        top.pack(fill='x')
        self.url_text = tk.Text(top, height=4, wrap='none', font=('Consolas', 10), undo=True)
        self.url_text.grid(row=0, column=0, rowspan=3, sticky='nsew')
        top.columnconfigure(0, weight=1)
        ttk.Button(top, text='Paste', command=self._paste).grid(row=0, column=1, sticky='ew', padx=(8, 0))
        ttk.Button(top, text='Clear', command=lambda: self.url_text.delete('1.0', 'end')).grid(
            row=1, column=1, sticky='ew', padx=(8, 0), pady=4)
        ttk.Button(top, text='From file…', command=self._load_urls).grid(row=2, column=1, sticky='ew', padx=(8, 0))
        self.watch_clipboard = tk.BooleanVar()
        self.preview_volume = tk.DoubleVar()  # used by the Preview & clip window
        self.clear_done = tk.BooleanVar()
        opts_row = ttk.Frame(top)
        opts_row.grid(row=3, column=0, columnspan=2, sticky='w', pady=(6, 0))
        ttk.Checkbutton(opts_row, text='Watch clipboard — links you copy are added automatically',
                        variable=self.watch_clipboard, command=self._on_watch_toggle).pack(side='left')
        ttk.Checkbutton(opts_row, text='Clear links when finished (failed ones stay)',
                        variable=self.clear_done).pack(side='left', padx=(20, 0))

        # Options
        opt = ttk.Frame(root)
        opt.pack(fill='x', pady=(8, 0))
        opt.columnconfigure(0, weight=1)
        opt.columnconfigure(1, weight=1)

        # -- Format
        fmt = ttk.LabelFrame(opt, text='Format', padding=8)
        fmt.grid(row=0, column=0, sticky='nsew', padx=(0, 4))
        self.mode = tk.StringVar()
        ttk.Radiobutton(fmt, text='Video', value='video', variable=self.mode, command=self._on_mode).grid(
            row=0, column=0, sticky='w')
        ttk.Radiobutton(fmt, text='Audio only', value='audio', variable=self.mode, command=self._on_mode).grid(
            row=0, column=1, sticky='w')

        self.quality = tk.StringVar()
        self.container = tk.StringVar()
        self.audio_format = tk.StringVar()
        self.audio_quality = tk.StringVar()
        ttk.Label(fmt, text='Max quality').grid(row=1, column=0, sticky='w', pady=(6, 0))
        self.cb_quality = ttk.Combobox(fmt, textvariable=self.quality, values=VIDEO_QUALITIES, state='readonly', width=14)
        self.cb_quality.grid(row=1, column=1, sticky='w', pady=(6, 0))
        ttk.Label(fmt, text='Container').grid(row=2, column=0, sticky='w')
        self.cb_container = ttk.Combobox(fmt, textvariable=self.container, values=CONTAINERS, state='readonly', width=14)
        self.cb_container.grid(row=2, column=1, sticky='w', pady=2)
        ttk.Label(fmt, text='Audio format').grid(row=3, column=0, sticky='w')
        self.cb_afmt = ttk.Combobox(fmt, textvariable=self.audio_format, values=AUDIO_FORMATS, state='readonly', width=14)
        self.cb_afmt.grid(row=3, column=1, sticky='w', pady=2)
        ttk.Label(fmt, text='Audio quality').grid(row=4, column=0, sticky='w')
        self.cb_aq = ttk.Combobox(fmt, textvariable=self.audio_quality, values=AUDIO_QUALITIES, state='readonly', width=14)
        self.cb_aq.grid(row=4, column=1, sticky='w', pady=2)

        self.subs = tk.BooleanVar()
        self.sub_langs = tk.StringVar()
        sf = ttk.Frame(fmt)
        sf.grid(row=5, column=0, columnspan=2, sticky='w', pady=(4, 0))
        self.chk_subs = ttk.Checkbutton(sf, text='Embed subtitles, languages:', variable=self.subs)
        self.chk_subs.pack(side='left')
        self.ent_langs = ttk.Entry(sf, textvariable=self.sub_langs, width=10)
        self.ent_langs.pack(side='left', padx=4)

        # -- Extras
        ext = ttk.LabelFrame(opt, text='Options', padding=8)
        ext.grid(row=0, column=1, sticky='nsew', padx=(4, 0))
        self.embed_thumb = tk.BooleanVar()
        self.metadata = tk.BooleanVar()
        self.chapters = tk.BooleanVar()
        self.sponsorblock = tk.BooleanVar()
        self.skip_existing = tk.BooleanVar()
        ttk.Checkbutton(ext, text='Embed thumbnail', variable=self.embed_thumb).grid(row=0, column=0, sticky='w')
        ttk.Checkbutton(ext, text='Embed metadata', variable=self.metadata).grid(row=0, column=1, sticky='w')
        self.chk_chapters = ttk.Checkbutton(ext, text='Embed chapters', variable=self.chapters)
        self.chk_chapters.grid(row=1, column=0, sticky='w')
        ttk.Checkbutton(ext, text='Remove sponsors (SponsorBlock)', variable=self.sponsorblock).grid(
            row=1, column=1, sticky='w')
        ttk.Checkbutton(ext, text='Skip files that already exist', variable=self.skip_existing).grid(
            row=2, column=0, columnspan=2, sticky='w')

        self.playlist = tk.StringVar()
        self.cookies_browser = tk.StringVar()
        self.rate_limit = tk.StringVar()
        ttk.Label(ext, text='Playlist links').grid(row=3, column=0, sticky='w', pady=(6, 0))
        ttk.Combobox(ext, textvariable=self.playlist, values=PLAYLIST_MODES, state='readonly', width=18).grid(
            row=3, column=1, sticky='w', pady=(6, 0))
        ttk.Label(ext, text='Cookies from browser').grid(row=4, column=0, sticky='w')
        ttk.Combobox(ext, textvariable=self.cookies_browser, values=BROWSERS, state='readonly', width=18).grid(
            row=4, column=1, sticky='w', pady=2)
        ttk.Label(ext, text='Speed limit (e.g. 2M)').grid(row=5, column=0, sticky='w')
        ttk.Entry(ext, textvariable=self.rate_limit, width=20).grid(row=5, column=1, sticky='w')
        self.parallel = tk.IntVar()
        ttk.Label(ext, text='Simultaneous downloads').grid(row=6, column=0, sticky='w', pady=(2, 0))
        ttk.Spinbox(ext, from_=1, to=10, textvariable=self.parallel, width=6, state='readonly',
                    wrap=False).grid(row=6, column=1, sticky='w', pady=(2, 0))
        self.only_new = tk.BooleanVar()
        nf = ttk.Frame(ext)
        nf.grid(row=7, column=0, columnspan=2, sticky='w', pady=(4, 0))
        ttk.Checkbutton(nf, text='Only download new items (remember what I already have)',
                        variable=self.only_new).pack(side='left')
        ttk.Button(nf, text='Forget…', width=8, command=self._forget_history).pack(side='left', padx=(6, 0))

        # -- Output
        out = ttk.LabelFrame(root, text='Output', padding=8)
        out.pack(fill='x', pady=(8, 0))
        out.columnconfigure(1, weight=1)
        self.outdir = tk.StringVar()
        self.outdir_audio = tk.StringVar()
        self.template = tk.StringVar()
        self.extra_args = tk.StringVar()
        self.lbl_dirs = {}
        for r, (mode, label, var) in enumerate((('video', 'Video folder', self.outdir),
                                                 ('audio', 'Audio folder', self.outdir_audio))):
            self.lbl_dirs[mode] = ttk.Label(out, text=label)
            self.lbl_dirs[mode].grid(row=r, column=0, sticky='w', pady=(0, 3))
            ttk.Entry(out, textvariable=var).grid(row=r, column=1, sticky='ew', padx=6, pady=(0, 3))
            ttk.Button(out, text='Browse…', command=lambda v=var: self._browse(v)).grid(row=r, column=2, pady=(0, 3))
            ttk.Button(out, text='Open', command=lambda v=var: self._open_outdir(v)).grid(
                row=r, column=3, padx=(4, 0), pady=(0, 3))
        ttk.Label(out, text='File name').grid(row=2, column=0, sticky='w', pady=4)
        ttk.Combobox(out, textvariable=self.template, values=[
            '%(title)s [%(id)s].%(ext)s',
            '%(title)s.%(ext)s',
            '%(uploader)s - %(title)s.%(ext)s',
            '%(playlist_title|Singles)s/%(playlist_index|)s%(playlist_index& - |)s%(title)s.%(ext)s',
            '%(uploader)s/%(upload_date>%Y-%m-%d)s - %(title)s.%(ext)s',
        ]).grid(row=2, column=1, sticky='ew', padx=6, pady=4)
        self.suno_id = tk.BooleanVar()
        ttk.Checkbutton(out, text='Add song ID to Suno names', variable=self.suno_id).grid(
            row=2, column=2, columnspan=2, sticky='w', padx=(4, 0))
        ttk.Label(out, text='Extra arguments').grid(row=3, column=0, sticky='w')
        ttk.Entry(out, textvariable=self.extra_args).grid(row=3, column=1, sticky='ew', padx=6)
        ttk.Button(out, text='Pick…', command=self._pick_args).grid(row=3, column=2, sticky='ew')
        ttk.Button(out, text='Show command', command=self._show_cmd).grid(row=3, column=3, sticky='ew', padx=(4, 0))
        self.clip_start = tk.StringVar()
        self.clip_end = tk.StringVar()
        self.clip_precise = tk.BooleanVar()
        self.lbl_clip = ttk.Label(out, text='Clip section')
        self.lbl_clip.grid(row=4, column=0, sticky='w', pady=(4, 0))
        cf = ttk.Frame(out)
        cf.grid(row=4, column=1, columnspan=3, sticky='w', padx=6, pady=(4, 0))
        ttk.Button(cf, text='Preview…', command=self._open_preview).pack(side='left', padx=(0, 10))
        ttk.Label(cf, text='Start').pack(side='left')
        ttk.Entry(cf, textvariable=self.clip_start, width=9).pack(side='left', padx=(4, 10))
        ttk.Label(cf, text='End').pack(side='left')
        ttk.Entry(cf, textvariable=self.clip_end, width=9).pack(side='left', padx=(4, 6))
        ttk.Button(cf, text='✕', width=3, command=lambda: (self.clip_start.set(''), self.clip_end.set(''))).pack(
            side='left')
        ttk.Checkbutton(cf, text='Precise cut', variable=self.clip_precise).pack(side='left', padx=(10, 0))
        self.lbl_clip_hint = ttk.Label(cf, text='e.g. 1:30 to 2:45 — leave blank for the whole thing',
                                       style='Hint.TLabel')
        self.clip_precise.trace_add('write', lambda *_: self._on_clip_change())
        self.lbl_clip_hint.pack(side='left', padx=(10, 0))
        for v in (self.clip_start, self.clip_end):
            v.trace_add('write', lambda *_: self._on_clip_change())

        # -- Buttons
        bar = ttk.Frame(root)
        bar.pack(fill='x', pady=8)
        self.btn_go = ttk.Button(bar, text='⬇  Download', style='Accent.TButton', command=self._start)
        self.btn_go.pack(side='left')
        self.btn_cancel = ttk.Button(bar, text='Cancel', command=self._cancel, state='disabled')
        self.btn_cancel.pack(side='left', padx=6)
        ttk.Button(bar, text='Clear list', command=self._clear_list).pack(side='left')
        self.btn_retry = ttk.Button(bar, text='↻  Retry failed', command=self._retry_failed, state='disabled')
        self.btn_retry.pack(side='left', padx=6)
        self.dark = tk.BooleanVar()
        ttk.Checkbutton(bar, text='Dark mode', variable=self.dark, command=self._toggle_theme).pack(side='right')
        ttk.Button(bar, text='Updates', command=lambda: self._check_updates(quiet=False)).pack(side='right', padx=(0, 10))
        self.status = tk.StringVar(value='Ready')
        ttk.Label(bar, textvariable=self.status).pack(side='right', padx=(0, 16))

        # -- Queue + log
        pane = ttk.PanedWindow(root, orient='vertical')
        pane.pack(fill='both', expand=True)

        tf = ttk.Frame(pane)
        cols = ('title', 'progress', 'size', 'speed', 'eta', 'status')
        self.tree = ttk.Treeview(tf, columns=cols, show='headings', height=7)
        for c, w, anchor in (('title', 420, 'w'), ('progress', 80, 'e'), ('size', 90, 'e'),
                             ('speed', 100, 'e'), ('eta', 60, 'e'), ('status', 150, 'w')):
            self.tree.heading(c, text=c.capitalize() if c != 'eta' else 'ETA')
            try:
                w = max(40, int(self._col_widths.get(c, w)))
            except (TypeError, ValueError):
                pass
            # only the last column soaks up spare room, so every other column keeps the width you drag it to
            self.tree.column(c, width=w, minwidth=40, anchor=anchor, stretch=(c == 'status'))
        vs = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        hs = ttk.Scrollbar(tf, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        hs.pack(side='bottom', fill='x')
        self.tree.pack(side='left', fill='both', expand=True)
        vs.pack(side='right', fill='y')
        self.tree.tag_configure('done', foreground='#1a7f37')
        self.tree.tag_configure('err', foreground='#cf222e')
        self.tree.bind('<Double-1>', self._open_row)
        self.tree.bind('<Button-3>', self._row_menu)
        self.tree.bind('<Button-2>' if sys.platform == 'darwin' else '<Control-Button-1>', self._row_menu)
        self.row_paths = {}   # iid -> downloaded file
        self.row_urls = {}    # iid -> page link
        self.row_jobs = {}    # iid -> job to re-run if it failed
        pane.add(tf, weight=3)

        lf = ttk.Frame(pane)
        self.progress = ttk.Progressbar(lf, mode='determinate', maximum=100)
        self.progress.pack(fill='x', pady=(4, 4))
        self.log = tk.Text(lf, height=8, wrap='word', font=('Consolas', 9), state='disabled',
                           background='#1e1e1e', foreground='#d4d4d4', insertbackground='white')
        ls = ttk.Scrollbar(lf, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=ls.set)
        self.log.pack(side='left', fill='both', expand=True)
        ls.pack(side='right', fill='y')
        self.log.tag_configure('warn', foreground='#e3b341')
        self.log.tag_configure('err', foreground='#ff7b72')
        pane.add(lf, weight=2)

        self.bind('<Control-Return>', lambda e: self._start())

    # ---------- settings ----------
    def _vars(self):
        return {k: getattr(self, k) for k in DEFAULTS}

    def _load_settings(self):
        try:
            with open(SETTINGS_PATH, encoding='utf-8') as f:
                data = json.load(f)
            self.settings.update({k: v for k, v in data.items() if k in DEFAULTS})
            self._col_widths = data.get('col_widths') or {}
        except Exception:
            pass
        if not self.settings.get('outdir_audio'):
            self.settings['outdir_audio'] = self.settings['outdir']

    def _apply_settings(self):
        for k, var in self._vars().items():
            var.set(self.settings[k])

    def _collect(self):
        s = {k: var.get() for k, var in self._vars().items()}
        # clip times apply to this run only, so they are not saved with the settings
        s.update(clip_start=self.clip_start.get(), clip_end=self.clip_end.get())
        return s

    def _on_clip_change(self):
        try:
            clip = clip_range(self._collect())
        except SettingsError:
            self.lbl_clip.configure(style='Bad.TLabel')
            self.lbl_clip_hint.configure(text='not a valid time yet', style='Bad.TLabel')
            return
        self.lbl_clip.configure(style='Active.TLabel' if clip else 'TLabel')
        if clip:
            start, end = clip
            text = f'clipping {human_eta(start)} → {human_eta(end) if end is not None else "end"}'
            self.lbl_clip_hint.configure(text=text, style='Active.TLabel')
        else:
            self.lbl_clip_hint.configure(text='e.g. 1:30 to 2:45 — leave blank for the whole thing', style='Hint.TLabel')
        if clip and not self.clip_precise.get():
            self.lbl_clip_hint.configure(text=self.lbl_clip_hint.cget('text')
                                         + '  (fast cut: may start a few seconds early in some players)')

    def _save_settings(self):
        try:
            data = self._collect()
            try:
                data['col_widths'] = {c: int(self.tree.column(c, 'width')) for c in self.tree['columns']}
            except Exception:
                pass
            with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    # ---------- actions ----------
    def _on_mode(self):
        video = self.mode.get() == 'video'
        for w in (self.cb_quality, self.cb_container):
            w.configure(state='readonly' if video else 'disabled')
        for w in (self.cb_afmt, self.cb_aq):
            w.configure(state='disabled' if video else 'readonly')
        for w in (self.chk_subs, self.ent_langs, self.chk_chapters):
            w.configure(state='normal' if video else 'disabled')
        # show which folder this download will go to
        for mode, lbl in self.lbl_dirs.items():
            lbl.configure(style='Active.TLabel' if mode == self.mode.get() else 'TLabel')

    def _paste(self):
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            return
        if text:
            cur = self.url_text.get('1.0', 'end').strip()
            self.url_text.insert('end', ('\n' if cur else '') + text)

    def _load_urls(self):
        p = filedialog.askopenfilename(filetypes=[('Text files', '*.txt'), ('All files', '*.*')])
        if p:
            with open(p, encoding='utf-8', errors='replace') as f:
                self.url_text.insert('end', '\n' + f.read())

    def _browse(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or os.path.expanduser('~'), parent=self)
        if d:
            var.set(d)

    def _open_outdir(self, var=None):
        d = (var.get() if var is not None else out_folder(self._collect())) or os.path.expanduser('~')
        os.makedirs(d, exist_ok=True)
        open_path(d)

    def _remove_urls(self, urls):
        """Take these links out of the URL box, leaving everything else (and anything added meanwhile)."""
        gone = set(urls)
        lines = self.url_text.get('1.0', 'end-1c').splitlines()
        keep = [l for l in lines if l.strip() not in gone]
        removed = len(lines) - len(keep)
        if removed:
            while keep and not keep[-1].strip():
                keep.pop()
            self.url_text.delete('1.0', 'end')
            self.url_text.insert('1.0', '\n'.join(keep))
        return removed

    def _urls(self):
        lines = self.url_text.get('1.0', 'end').splitlines()
        return [u.strip() for u in lines if u.strip() and not u.strip().startswith('#')]

    def _pick_args(self):
        try:
            ArgPicker(self)
        except Exception as e:
            messagebox.showerror(APP_NAME, f'Could not load the yt-dlp option list:\n{e}')

    def _show_cmd(self):
        try:
            args = build_args(self._collect())
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e) if isinstance(e, SettingsError)
                                 else f'Extra arguments could not be parsed:\n{e}')
            return
        cmd = 'yt-dlp ' + ' '.join(quote_arg(a) for a in args) + ' ' + ' '.join(self._urls() or ['<URL>'])
        p = self.palette
        win = tk.Toplevel(self, bg=p['bg'])
        win.title('Equivalent command')
        t = tk.Text(win, wrap='word', height=8, width=100, font=('Consolas', 9), background=p['field'],
                    foreground=p['fg'], insertbackground=p['fg'], relief='flat', borderwidth=6,
                    selectbackground=p['select'], selectforeground=p['fg'])
        t.insert('1.0', cmd)
        t.pack(fill='both', expand=True, padx=8, pady=8)
        ttk.Button(win, text='Copy', command=lambda: (self.clipboard_clear(), self.clipboard_append(cmd))).pack(pady=(0, 8))

    def _busy(self):
        return bool(self.worker and self.worker.is_alive())

    def _start(self, jobs=None):
        if self._busy() or self._updating:
            return
        urls = jobs or list(dict.fromkeys(self._urls()))  # the same link twice only downloads once
        if not urls:
            messagebox.showinfo(APP_NAME, 'Paste at least one URL first.')
            return
        s = self._collect()
        if not out_folder(s):
            messagebox.showinfo(APP_NAME, f"Choose a {'n audio' if s['mode'] == 'audio' else ' video'} folder first.")
            return
        try:
            args = build_args(s)
            clip = clip_range(s)
            args_whole = build_args({**s, 'clip_start': '', 'clip_end': ''}) if clip else None
        except ValueError as e:
            messagebox.showerror(APP_NAME, str(e) if isinstance(e, SettingsError)
                                 else f'Extra arguments could not be parsed:\n{e}')
            return
        clip_confirmed = bool(jobs)  # retries keep whatever was decided before
        if clip and not jobs and len(urls) > 1:
            choice = self._ask_clip_many(f'You have {len(urls)} links', clip)
            if choice is None:
                return
            if choice:
                clip_confirmed = True
            else:
                args, args_whole = args_whole, None  # download everything whole this time
        os.makedirs(out_folder(s), exist_ok=True)
        self._save_settings()
        self.progress['value'] = 0
        self.btn_go.configure(state='disabled')
        self.btn_cancel.configure(state='normal')
        self.btn_retry.configure(state='disabled')
        self.status.set(f'Retrying {len(urls)} item(s)…' if jobs else f'Downloading {len(urls)} link(s)…')
        self._started = time.time()
        try:
            n = max(1, min(10, int(self.parallel.get())))
        except (tk.TclError, ValueError):
            n = 1
        self._overall_total = 0
        self._skipped = 0
        self.worker = Worker(urls, args, self.q, parallel=n, args_whole=args_whole, clip_confirmed=clip_confirmed)
        self.worker.start()

    def _ask_clip_many(self, what, clip):
        """True = clip every item, False = download them whole, None = cancel."""
        start, end = clip
        section = f'{fmt_time(start)} → {fmt_time(end) if end is not None else "the end"}'
        return messagebox.askyesnocancel(
            'Clip every item?',
            f'{what}, and a clip section is set ({section}).\n\n'
            f'Yes  —  clip every item to {section}\n'
            'No  —  download every item whole (ignore the clip this time)\n'
            'Cancel  —  stop and change the settings',
            icon='warning', default='no', parent=self)

    def _cancel(self):
        if self.worker and self.worker.is_alive():
            self.worker.cancel.set()
            self.status.set('Cancelling…')

    def _clear_list(self):
        if self._busy():
            return
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.row_paths.clear()
        self.row_urls.clear()
        self.row_jobs.clear()
        self.btn_retry.configure(state='disabled')

    def _retry(self, iids):
        iids = [i for i in iids if i in self.row_jobs]
        if not iids or self._busy():
            return
        jobs = []
        for iid in iids:
            jobs.append(self.row_jobs.pop(iid))
            self.tree.set(iid, 'status', 'Queued')
            for col in ('progress', 'speed', 'eta'):
                self.tree.set(iid, col, '')
            self.tree.item(iid, tags=())
        self._start(jobs)

    def _forget_history(self):
        try:
            with open(ARCHIVE_PATH, encoding='utf-8') as f:
                n = sum(1 for line in f if line.strip())
        except OSError:
            n = 0
        if not n:
            messagebox.showinfo(APP_NAME, 'The download history is already empty.', parent=self)
            return
        if messagebox.askyesno(APP_NAME, f'The app remembers {n} downloaded item(s).\n\n'
                               'Forget them all? Next time, everything will be downloaded again '
                               '(files already in the folder are still skipped).', parent=self):
            try:
                os.remove(ARCHIVE_PATH)
                self._log(f'Download history cleared ({n} items).')
            except OSError as e:
                messagebox.showerror(APP_NAME, f'Could not clear the history:\n{e}', parent=self)

    def _retry_failed(self):
        self._retry(list(self.row_jobs))

    def _remove_rows(self, iids):
        busy = self._busy()
        for iid in iids:
            st = self.tree.set(iid, 'status')
            if busy and st not in ('✔ Done', 'Error', 'Skipped', 'Cancelled'):
                continue  # don't remove items that are still being worked on
            self.tree.delete(iid)
            for d in (self.row_paths, self.row_urls, self.row_jobs):
                d.pop(iid, None)
            for k, v in list(self.rows.items()):
                if v == iid:
                    del self.rows[k]
        self.btn_retry.configure(state='normal' if self.row_jobs and not busy else 'disabled')

    def _show_in_folder(self, path):
        if sys.platform.startswith('win'):
            subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
        else:
            open_path(os.path.dirname(path))

    def _copy(self, text):
        self._clip_last = text  # don't let the clipboard watcher re-add links copied from the list
        self.clipboard_clear()
        self.clipboard_append(text)

    def _row_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        sel = self.tree.selection()
        if iid not in sel:
            self.tree.selection_set(iid)
            sel = (iid,)
        p = self.palette
        m = tk.Menu(self, tearoff=0, background=p['surface'], foreground=p['fg'],
                    activebackground=p['accent'], activeforeground=p['on_accent'],
                    disabledforeground=p['disabled'], borderwidth=1, relief='flat')
        path = self.row_paths.get(iid)
        has_file = bool(path and os.path.exists(path))
        urls = [self.row_urls[i] for i in sel if self.row_urls.get(i)]
        failed = [i for i in sel if i in self.row_jobs]
        many = len(sel) > 1
        m.add_command(label='Open file', command=lambda: open_path(path),
                      state='normal' if has_file and not many else 'disabled')
        m.add_command(label='Show in folder', command=lambda: self._show_in_folder(path),
                      state='normal' if has_file and not many else 'disabled')
        m.add_separator()
        m.add_command(label='Open link in browser', command=lambda: __import__('webbrowser').open(urls[0]),
                      state='normal' if urls and not many else 'disabled')
        m.add_command(label='Copy links' if len(urls) > 1 else 'Copy link',
                      command=lambda: self._copy('\n'.join(urls)), state='normal' if urls else 'disabled')
        m.add_separator()
        m.add_command(label=f'Retry ({len(failed)})' if many else 'Retry', command=lambda: self._retry(failed),
                      state='normal' if failed and not self._busy() else 'disabled')
        m.add_command(label='Remove from list', command=lambda: self._remove_rows(sel))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _open_row(self, _e):
        sel = self.tree.selection()
        if sel and sel[0] in self.row_paths:
            p = self.row_paths[sel[0]]
            if p and os.path.exists(p):
                self._show_in_folder(p)

    def _on_close(self):
        self._save_settings()
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, 'A download is running. Cancel it and quit?'):
                return
            self.worker.cancel.set()
        self.destroy()

    def _open_preview(self):
        urls = self._urls()
        if not urls:
            messagebox.showinfo(APP_NAME, 'Paste a link first, then click Preview.', parent=self)
            return
        url = urls[0]
        try:  # prefer the line the text cursor is on
            line = self.url_text.get('insert linestart', 'insert lineend').strip()
            if line in urls:
                url = line
        except tk.TclError:
            pass
        if not shutil.which('ffmpeg'):
            messagebox.showerror(APP_NAME, 'Previews need FFmpeg, which was not found next to the app.', parent=self)
            return
        PreviewWindow(self, url)

    # ---------- adding links: clipboard watching and drag-and-drop ----------
    def _add_urls(self, urls, source):
        have = set(self._urls())
        new = [u for u in urls if u not in have]
        if not new:
            return 0
        cur = self.url_text.get('1.0', 'end').strip()
        self.url_text.insert('end', ('\n' if cur else '') + '\n'.join(new))
        self.url_text.see('end')
        what = f'{len(new)} link' + ('s' if len(new) != 1 else '')
        self.status.set(f'Added {what} from {source}')
        self._log(f'Added {what} from {source}: ' + ', '.join(new[:3]) + (' …' if len(new) > 3 else ''))
        return len(new)

    def _read_clipboard(self):
        try:
            return self.clipboard_get()
        except tk.TclError:
            return None

    def _on_watch_toggle(self):
        # don't grab whatever was already on the clipboard when the box is ticked
        self._clip_last = self._read_clipboard()
        if self.watch_clipboard.get():
            self.status.set('Watching the clipboard — copy a link and it will appear here')

    def _poll_clipboard(self):
        try:
            if self.watch_clipboard.get():
                text = self._read_clipboard()
                if text is not None and text != self._clip_last:
                    self._clip_last = text
                    if len(text) < 200_000:
                        urls = [u for u in find_urls(text) if is_supported_url(u)]
                        if urls:
                            self._add_urls(urls, 'the clipboard')
        finally:
            self.after(700, self._poll_clipboard)

    def _enable_drag_and_drop(self):
        """Accept links, .url shortcuts and .txt lists dropped on the window (needs tkdnd)."""
        try:
            from tkinterdnd2 import TkinterDnD
            TkinterDnD._require(self)
        except Exception:
            return False
        cmd = self.register(self._on_drop)
        ok = False
        for w in (self.url_text, self.tree, self.log):
            try:
                self.tk.call('tkdnd::drop_target', 'register', w._w, ('DND_Text', 'DND_Files'))
                self.tk.call('bind', w._w, '<<Drop>>', cmd + ' %D')
                ok = True
            except tk.TclError:
                pass
        if ok:
            self._first_section.configure(
                text='URLs (one per line — videos, playlists or channels; you can also drag links here)')
        return ok

    def _on_drop(self, data):
        urls = urls_from_drop(data, self.tk.splitlist)
        if urls:
            if not self._add_urls(urls, 'drag and drop'):
                self.status.set('Those links are already in the list')
        else:
            self.status.set('No links found in what was dropped')
        return 'copy'

    # ---------- updates ----------
    def _check_updates(self, quiet=True):
        if not FROZEN or not BUILD:
            if not quiet:
                messagebox.showinfo(APP_NAME, 'Automatic updates work in the downloaded .exe version.\n'
                                              'You are running the app from source.', parent=self)
            return
        if not quiet:
            self.status.set('Checking for updates…')

        def work():
            try:
                info = fetch_latest_release()
            except Exception as e:
                info = e
            self.q.put(('update_check', {'info': info, 'quiet': quiet}))
        threading.Thread(target=work, daemon=True).start()

    def _on_update_check(self, info, quiet):
        if isinstance(info, Exception):
            if not quiet:
                self.status.set('Ready')
                messagebox.showerror(APP_NAME, f'Could not check for updates:\n{info}', parent=self)
            return
        if info and info['build'] > BUILD:
            self._show_update_banner(info)
        elif not quiet:
            self.status.set('Ready')
            messagebox.showinfo(APP_NAME, f'You have the latest version (build {BUILD}, yt-dlp {YTDLP_VERSION}).',
                                parent=self)

    def _show_update_banner(self, info):
        p = self.palette
        b = self.banner
        for w in b.winfo_children():
            w.destroy()
        b.configure(bg=p['accent'])
        text = f"A new version is available — build {info['build']}"
        if info.get('ytdlp') and info['ytdlp'] != YTDLP_VERSION:
            text += f"  (yt-dlp {YTDLP_VERSION} → {info['ytdlp']})"
        tk.Label(b, text=text, bg=p['accent'], fg=p['on_accent'],
                 font=('Segoe UI', 10, 'bold')).pack(side='left', padx=10, pady=6)
        mk = dict(bg=p['on_accent'], fg=p['accent'], activebackground=p['bg'], activeforeground=p['fg'],
                  relief='flat', borderwidth=0, padx=10, pady=2, font=('Segoe UI', 9, 'bold'), cursor='hand2')
        tk.Button(b, text='Later', command=b.pack_forget, **mk).pack(side='right', padx=(4, 8))
        if info.get('page'):
            tk.Button(b, text="What's new", command=lambda: __import__('webbrowser').open(info['page']),
                      **mk).pack(side='right', padx=4)
        tk.Button(b, text='Update now', command=lambda: self._install_update(info), **mk).pack(side='right', padx=4)
        b.pack(fill='x', before=self._first_section, pady=(0, 8))

    def _install_update(self, info):
        if self._busy():
            messagebox.showinfo(APP_NAME, 'Let the current downloads finish (or cancel them) before updating.',
                                parent=self)
            return
        try:
            probe = os.path.join(HERE, '.write_test')
            with open(probe, 'w'):
                pass
            os.remove(probe)
        except OSError:
            if messagebox.askyesno(APP_NAME, "The app's folder can't be written to, so it can't update itself.\n\n"
                                   'Open the download page instead?', parent=self) and info.get('page'):
                __import__('webbrowser').open(info['page'])
            return
        size = human_bytes(info.get('size')) or 'about 200 MB'
        if not messagebox.askyesno(APP_NAME, f"Download build {info['build']} ({size}) and restart the app?",
                                   parent=self):
            return
        import tempfile
        workdir = os.path.join(tempfile.gettempdir(), 'ytdlp_gui_update')
        self._updating = True
        self.btn_go.configure(state='disabled')
        self.banner.pack_forget()
        self.status.set('Downloading update…')

        def work():
            try:
                new = download_update(info, workdir,
                                      progress=lambda d, t: self.q.put(('update_progress', (d, t))))
                self.q.put(('update_ready', {'new': new, 'workdir': workdir}))
            except Exception as e:
                self.q.put(('update_failed', str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _finish_update(self, new, workdir):
        pids = [os.getpid()]
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass and os.path.normcase(meipass) != os.path.normcase(HERE):
            pids.append(os.getppid())  # one-file .exe: the launcher process holds the .exe open too
        bat = write_updater_script(new, HERE, os.path.basename(sys.executable), pids, workdir)
        try:
            env = {k: v for k, v in os.environ.items() if not k.startswith(('_PYI_', '_MEIPASS'))}
            env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
            subprocess.Popen(['cmd', '/c', bat], close_fds=True, env=env,
                             creationflags=0x08000000 | 0x00000200)  # no window, own process group
        except Exception as e:
            self._updating = False
            self.btn_go.configure(state='normal')
            messagebox.showerror(APP_NAME, f'Could not start the updater:\n{e}', parent=self)
            return
        self._save_settings()
        self.destroy()

    # ---------- queue pump ----------
    def _row(self, key, title=None):
        iid = self.rows.get(key)
        if iid is None:
            iid = self.tree.insert('', 'end', values=(title or key, '', '', '', '', 'Starting'))
            self.rows[key] = iid
            self.tree.see(iid)
        elif title:
            self.tree.set(iid, 'title', title)
        return iid

    def _log(self, msg):
        tag = 'err' if msg.startswith('ERROR') else 'warn' if msg.startswith('WARNING') else None
        self.log.configure(state='normal')
        self.log.insert('end', msg + '\n', tag)
        self.log.see('end')
        self.log.configure(state='disabled')

    def _poll(self):
        try:
            for _ in range(300):
                kind, data = self.q.get_nowait()
                if kind == 'log':
                    self._log(data)
                elif kind == 'queued':
                    iid = self._row(data['key'], data['title'])
                    self.tree.set(iid, 'status', 'Queued')
                    if data.get('url'):
                        self.row_urls.setdefault(iid, data['url'])
                elif kind == 'item_failed':
                    iid = self._row(data['key'], data['title'])
                    self.tree.set(iid, 'status', 'Error')
                    self.tree.set(iid, 'speed', '')
                    self.tree.set(iid, 'eta', '')
                    self.tree.item(iid, tags=('err',))
                    self.row_jobs[iid] = data['job']
                    if data.get('url'):
                        self.row_urls.setdefault(iid, data['url'])
                elif kind == 'overall':
                    self._overall_total = data['total']
                    if data['total'] > 1 or data['done']:
                        self.progress['value'] = data['done'] / max(1, data['total']) * 100
                        self.status.set(f"Downloading… {data['done']} of {data['total']} done")
                elif kind == 'progress':
                    iid = self._row(data['key'], data['title'])
                    if data.get('url'):
                        self.row_urls[iid] = data['url']
                    st = data['status']
                    if st == 'downloading':
                        pct = data['pct']
                        if pct is None and data['fragment'][1]:
                            fi, fc = data['fragment']
                            pct = (fi or 0) / fc * 100
                        self.tree.set(iid, 'progress', f'{pct:.1f}%' if pct is not None else '…')
                        self.tree.set(iid, 'size', data['size'])
                        self.tree.set(iid, 'speed', data['speed'])
                        self.tree.set(iid, 'eta', data['eta'])
                        self.tree.set(iid, 'status', 'Downloading')
                        if pct is not None and self._overall_total <= 1:
                            self.progress['value'] = pct
                    elif st == 'finished':
                        self.tree.set(iid, 'progress', '100%')
                        self.tree.set(iid, 'speed', '')
                        self.tree.set(iid, 'eta', '')
                        self.tree.set(iid, 'status', 'Downloaded')
                    elif st == 'error':
                        self.tree.set(iid, 'status', 'Error')
                        self.tree.item(iid, tags=('err',))
                elif kind == 'pp':
                    iid = self._row(data['key'], data['title'])
                    name = (data['pp'] or '').replace('FFmpeg', '').replace('PP', '') or 'Processing'
                    if data['pp'] != 'MoveFiles':
                        self.tree.set(iid, 'status', f'Processing ({name})')
                elif kind == 'skipped':
                    iid = self.rows.get(data['key']) if data.get('key') else None
                    if iid is None:
                        iid = self._row(data.get('key') or f"skip-{len(self.rows)}", data['title'])
                    self.tree.set(iid, 'status', 'Skipped – in history')
                    self.tree.set(iid, 'progress', '')
                    self.tree.item(iid, tags=('skip',))
                    self._skipped += 1
                elif kind == 'done_item' and data.get('skipped'):
                    iid = self._row(data['key'], data.get('title'))
                    self.tree.set(iid, 'status', 'Skipped – file exists')
                    self.tree.set(iid, 'progress', '')
                    self.tree.item(iid, tags=('skip',))
                    self.row_paths[iid] = data['path']
                    self.row_jobs.pop(iid, None)
                    self._skipped += 1
                elif kind == 'done_item':
                    iid = self._row(data['key'])
                    self.tree.set(iid, 'status', '✔ Done')
                    self.tree.set(iid, 'progress', '100%')
                    self.tree.item(iid, tags=('done',))
                    self.row_paths[iid] = data['path']
                    self.row_jobs.pop(iid, None)
                    if data.get('url'):
                        self.row_urls[iid] = data['url']
                elif kind == 'update_check':
                    self._on_update_check(data['info'], data['quiet'])
                elif kind == 'update_progress':
                    d, t = data
                    if t:
                        self.progress['value'] = d / t * 100
                    self.status.set(f'Downloading update… {human_bytes(d)}' + (f' of {human_bytes(t)}' if t else ''))
                elif kind == 'update_ready':
                    self.status.set('Installing update — the app will restart…')
                    self._finish_update(data['new'], data['workdir'])
                    return
                elif kind == 'update_failed':
                    self._updating = False
                    self.btn_go.configure(state='normal')
                    self.progress['value'] = 0
                    self.status.set('Update failed')
                    messagebox.showerror(APP_NAME, f'The update could not be downloaded:\n{data}', parent=self)
                elif kind == 'ask_clip':
                    try:
                        clip = clip_range(self._collect()) or (0, None)
                    except SettingsError:
                        clip = (0, None)
                    choice = self._ask_clip_many(f'"{data["title"]}" has {data["n"]} items', clip)
                    data['box']['answer'] = 'clip' if choice else 'whole' if choice is False else 'cancel'
                    data['event'].set()
                elif kind == 'item_dup':
                    iid = self.rows.get(data['key'])
                    if iid and self.tree.set(iid, 'status') == 'Queued':
                        self.tree.set(iid, 'status', 'In batch already')
                elif kind == 'urls_done':
                    if self.clear_done.get() and data['done']:
                        n = self._remove_urls(data['done'])
                        if n:
                            self._log(f'Cleared {n} finished link(s) from the list'
                                      + (f"; {len(data['failed'])} with failures kept" if data['failed'] else ''))
                elif kind == 'finished':
                    self.btn_go.configure(state='normal')
                    self.btn_cancel.configure(state='disabled')
                    self.btn_retry.configure(state='normal' if self.row_jobs else 'disabled')
                    secs = int(time.time() - getattr(self, '_started', time.time()))
                    if data is None:
                        self.status.set('Cancelled')
                    elif data:
                        self.status.set(f'Finished in {human_eta(secs)}'
                                        + (f' — {self._skipped} skipped (see log)' if self._skipped else ''))
                        self.progress['value'] = 100
                    elif self.row_jobs:
                        self.status.set(f'Finished — {len(self.row_jobs)} failed (right-click or Retry failed)')
                    else:
                        self.status.set('Finished with errors — see log')
                    if self._skipped:
                        self._log(f'{self._skipped} item(s) were skipped because you already have them '
                                  '(file in the folder, or in your download history).')
                    for key, iid in self.rows.items():
                        if self.tree.set(iid, 'status') not in ('✔ Done', 'Error') and \
                                not self.tree.set(iid, 'status').startswith('Skipped'):
                            if data is None:
                                self.tree.set(iid, 'status', 'Cancelled')
                            elif self.tree.set(iid, 'status') == 'Queued':
                                self.tree.set(iid, 'status', 'Skipped')
                            elif self.tree.set(iid, 'status') == 'Downloaded':
                                self.tree.set(iid, 'status', '✔ Done')
                                self.tree.item(iid, tags=('done',))
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == '__main__':
    if sys.platform.startswith('win'):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()
