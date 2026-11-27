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
from yt_dlp.utils import ExtractorError, float_or_none, parse_iso8601, url_or_none


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


EXTRA_EXTRACTORS = (SunoIE, SunoPlaylistIE, SunoProfileIE)


def make_ydl(opts):
    """A YoutubeDL with the GUI's own extractors placed ahead of yt-dlp's (and its block list)."""
    ydl = yt_dlp.YoutubeDL(opts)
    for cls in reversed(EXTRA_EXTRACTORS):
        ie = cls()
        ydl.add_info_extractor(ie)
        key = ie.ie_key()
        ydl._ies = {key: ydl._ies.pop(key), **ydl._ies}
    return ydl


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
    'outdir': os.path.join(os.path.expanduser('~'), 'Downloads'),
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


def build_args(s):
    """Turn GUI settings into yt-dlp command-line arguments."""
    args = ['--newline', '--no-colors']
    args += ['-P', s['outdir'], '-o', s['template'] or DEFAULTS['template']]

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


class GuiLogger:
    def __init__(self, q, on_error=None):
        self.q = q
        self.on_error = on_error

    def debug(self, msg):
        # yt-dlp routes normal [info] messages through debug(); hide true debug noise
        if msg.startswith('[debug] '):
            return
        if msg.startswith('[download]') and '%' in msg:
            return  # progress lines are shown in the table instead
        self.q.put(('log', msg))

    def info(self, msg):
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

    def __init__(self, jobs, args, q, parallel=1):
        super().__init__(daemon=True)
        self.jobs = [('url', j, None) if isinstance(j, str) else tuple(j) for j in jobs]
        self.args, self.q = args, q
        self.parallel = max(1, int(parallel or 1))
        self.cancel = threading.Event()
        self.errors = 0
        self._lock = threading.Lock()
        self._tl = threading.local()

    @property
    def urls(self):
        return [j[1] for j in self.jobs if j[0] == 'url']

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
                                      'url': info.get('webpage_url') or info.get('original_url')}))

    def _opts(self):
        opts = dict(yt_dlp.parse_options(self.args).ydl_opts)
        opts['logger'] = GuiLogger(self.q, self._on_error)
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

        def failed(kind, item, extra, info=None):
            if kind == 'entry':
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
                                if info is not None:
                                    ydl.process_ie_result(info, download=True)
                                else:
                                    ydl.download([item])
                            elif entries:
                                pl = entries[0][1].get('playlist') or item
                                self.q.put(('log', f'Playlist "{pl}": {len(entries)} item(s), '
                                                   f'{self.parallel} at a time'))
                                for entry, ex in entries:
                                    if entry.get('id'):
                                        self.q.put(('queued', {'key': entry['id'], 'url': entry.get('url'),
                                                               'title': entry.get('title') or entry.get('url')}))
                                    jobs.put(('entry', entry, ex))
                                add_total(len(entries) - 1)  # the playlist itself was a placeholder
                                continue
                        else:
                            ydl.process_ie_result(dict(item), download=True, extra_info=extra)
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
        self._load_settings()
        self.palette = PALETTES['dark' if self.settings.get('dark', True) else 'light']
        self._style()
        self._build()
        self._apply_settings()
        self._apply_theme()
        self._on_mode()
        self.after(100, self._poll)
        self.after(2000, lambda: self._check_updates(quiet=True))
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
        self.template = tk.StringVar()
        self.extra_args = tk.StringVar()
        ttk.Label(out, text='Folder').grid(row=0, column=0, sticky='w')
        ttk.Entry(out, textvariable=self.outdir).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Button(out, text='Browse…', command=self._browse).grid(row=0, column=2)
        ttk.Button(out, text='Open', command=lambda: self._open_outdir()).grid(row=0, column=3, padx=(4, 0))
        ttk.Label(out, text='File name').grid(row=1, column=0, sticky='w', pady=4)
        ttk.Combobox(out, textvariable=self.template, values=[
            '%(title)s [%(id)s].%(ext)s',
            '%(title)s.%(ext)s',
            '%(uploader)s - %(title)s.%(ext)s',
            '%(playlist_title|Singles)s/%(playlist_index|)s%(playlist_index& - |)s%(title)s.%(ext)s',
            '%(uploader)s/%(upload_date>%Y-%m-%d)s - %(title)s.%(ext)s',
        ]).grid(row=1, column=1, sticky='ew', padx=6, pady=4)
        ttk.Label(out, text='Extra arguments').grid(row=2, column=0, sticky='w')
        ttk.Entry(out, textvariable=self.extra_args).grid(row=2, column=1, sticky='ew', padx=6)
        ttk.Button(out, text='Pick…', command=self._pick_args).grid(row=2, column=2, sticky='ew')
        ttk.Button(out, text='Show command', command=self._show_cmd).grid(row=2, column=3, sticky='ew', padx=(4, 0))

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
        for c, w, anchor in (('title', 380, 'w'), ('progress', 80, 'e'), ('size', 90, 'e'),
                             ('speed', 100, 'e'), ('eta', 60, 'e'), ('status', 130, 'w')):
            self.tree.heading(c, text=c.capitalize() if c != 'eta' else 'ETA')
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == 'title'))
        vs = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
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
                self.settings.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
        except Exception:
            pass

    def _apply_settings(self):
        for k, var in self._vars().items():
            var.set(self.settings[k])

    def _collect(self):
        return {k: var.get() for k, var in self._vars().items()}

    def _save_settings(self):
        try:
            with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
                json.dump(self._collect(), f, indent=2)
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

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get() or os.path.expanduser('~'))
        if d:
            self.outdir.set(d)

    def _open_outdir(self):
        d = self.outdir.get()
        os.makedirs(d, exist_ok=True)
        open_path(d)

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
            messagebox.showerror(APP_NAME, f'Extra arguments could not be parsed:\n{e}')
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
        urls = jobs or self._urls()
        if not urls:
            messagebox.showinfo(APP_NAME, 'Paste at least one URL first.')
            return
        s = self._collect()
        if not s['outdir']:
            messagebox.showinfo(APP_NAME, 'Choose an output folder.')
            return
        try:
            args = build_args(s)
        except ValueError as e:
            messagebox.showerror(APP_NAME, f'Extra arguments could not be parsed:\n{e}')
            return
        os.makedirs(s['outdir'], exist_ok=True)
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
        self.worker = Worker(urls, args, self.q, parallel=n)
        self.worker.start()

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
                elif kind == 'finished':
                    self.btn_go.configure(state='normal')
                    self.btn_cancel.configure(state='disabled')
                    self.btn_retry.configure(state='normal' if self.row_jobs else 'disabled')
                    secs = int(time.time() - getattr(self, '_started', time.time()))
                    if data is None:
                        self.status.set('Cancelled')
                    elif data:
                        self.status.set(f'Finished in {human_eta(secs)}')
                        self.progress['value'] = 100
                    elif self.row_jobs:
                        self.status.set(f'Finished — {len(self.row_jobs)} failed (right-click or Retry failed)')
                    else:
                        self.status.set('Finished with errors — see log')
                    for key, iid in self.rows.items():
                        if self.tree.set(iid, 'status') not in ('✔ Done', 'Error'):
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
