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

APP_NAME = 'yt-dlp GUI'
SETTINGS_PATH = os.path.join(os.path.expanduser('~'), '.ytdlp_gui.json')

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
}


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

    if s['embed_thumb']:
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
    if not s['skip_existing']:
        args += ['--force-overwrites']
    if s['extra_args'].strip():
        args += shlex.split(s['extra_args'], posix=not sys.platform.startswith('win'))
    return args


class GuiLogger:
    def __init__(self, q):
        self.q = q

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
        self.q.put(('log', msg))


class Worker(threading.Thread):
    def __init__(self, urls, args, q):
        super().__init__(daemon=True)
        self.urls, self.args, self.q = urls, args, q
        self.cancel = threading.Event()

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
            self.q.put(('done_item', {'key': key, 'path': info.get('filepath')}))

    def run(self):
        ok = True
        try:
            parsed = yt_dlp.parse_options(self.args)
            opts = parsed.ydl_opts
            opts['logger'] = GuiLogger(self.q)
            opts['progress_hooks'] = [self._hook]
            opts['postprocessor_hooks'] = [self._pp_hook]
            opts['noprogress'] = True
            opts['quiet'] = False
            with yt_dlp.YoutubeDL(opts) as ydl:
                for url in self.urls:
                    if self.cancel.is_set():
                        break
                    self.q.put(('log', f'=== {url}'))
                    try:
                        ret = ydl.download([url])
                        if ret:
                            ok = False
                    except DownloadCancelled:
                        raise
                    except Exception as e:
                        ok = False
                        self.q.put(('log', f'ERROR: {e}'))
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f'{APP_NAME}  —  yt-dlp {YTDLP_VERSION}')
        self.geometry('980x720')
        self.minsize(820, 600)
        self.q = queue.Queue()
        self.worker = None
        self.rows = {}
        self.settings = dict(DEFAULTS)
        self._load_settings()
        self._style()
        self._build()
        self._apply_settings()
        self._on_mode()
        self.after(100, self._poll)
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
        for theme in ('vista', 'clam'):
            if theme in st.theme_names():
                st.theme_use(theme)
                break
        st.configure('Accent.TButton', font=('Segoe UI', 10, 'bold'), padding=(14, 6))
        st.configure('Treeview', rowheight=24)
        st.configure('Head.TLabel', font=('Segoe UI', 10, 'bold'))

    # ---------- layout ----------
    def _build(self):
        pad = {'padx': 8, 'pady': 4}
        root = ttk.Frame(self, padding=10)
        root.pack(fill='both', expand=True)

        # URLs
        top = ttk.LabelFrame(root, text='URLs (one per line — videos, playlists or channels)', padding=8)
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
        ttk.Button(out, text='Show command', command=self._show_cmd).grid(row=2, column=2, columnspan=2, sticky='ew')

        # -- Buttons
        bar = ttk.Frame(root)
        bar.pack(fill='x', pady=8)
        self.btn_go = ttk.Button(bar, text='⬇  Download', style='Accent.TButton', command=self._start)
        self.btn_go.pack(side='left')
        self.btn_cancel = ttk.Button(bar, text='Cancel', command=self._cancel, state='disabled')
        self.btn_cancel.pack(side='left', padx=6)
        ttk.Button(bar, text='Clear list', command=self._clear_list).pack(side='left')
        self.status = tk.StringVar(value='Ready')
        ttk.Label(bar, textvariable=self.status).pack(side='right')

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
        self.row_paths = {}
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

    def _show_cmd(self):
        try:
            args = build_args(self._collect())
        except ValueError as e:
            messagebox.showerror(APP_NAME, f'Extra arguments could not be parsed:\n{e}')
            return
        cmd = 'yt-dlp ' + ' '.join(shlex.quote(a) for a in args) + ' ' + ' '.join(self._urls() or ['<URL>'])
        win = tk.Toplevel(self)
        win.title('Equivalent command')
        t = tk.Text(win, wrap='word', height=8, width=100, font=('Consolas', 9))
        t.insert('1.0', cmd)
        t.pack(fill='both', expand=True, padx=8, pady=8)
        ttk.Button(win, text='Copy', command=lambda: (self.clipboard_clear(), self.clipboard_append(cmd))).pack(pady=(0, 8))

    def _start(self):
        if self.worker and self.worker.is_alive():
            return
        urls = self._urls()
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
        self.status.set(f'Downloading {len(urls)} link(s)…')
        self._started = time.time()
        self.worker = Worker(urls, args, self.q)
        self.worker.start()

    def _cancel(self):
        if self.worker and self.worker.is_alive():
            self.worker.cancel.set()
            self.status.set('Cancelling…')

    def _clear_list(self):
        if self.worker and self.worker.is_alive():
            return
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.row_paths.clear()

    def _open_row(self, _e):
        sel = self.tree.selection()
        if sel and sel[0] in self.row_paths:
            p = self.row_paths[sel[0]]
            if p and os.path.exists(p):
                if sys.platform.startswith('win'):
                    subprocess.Popen(['explorer', '/select,', os.path.normpath(p)])
                else:
                    open_path(os.path.dirname(p))

    def _on_close(self):
        self._save_settings()
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, 'A download is running. Cancel it and quit?'):
                return
            self.worker.cancel.set()
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
                elif kind == 'progress':
                    iid = self._row(data['key'], data['title'])
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
                        if pct is not None:
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
                elif kind == 'finished':
                    self.btn_go.configure(state='normal')
                    self.btn_cancel.configure(state='disabled')
                    secs = int(time.time() - getattr(self, '_started', time.time()))
                    if data is None:
                        self.status.set('Cancelled')
                    elif data:
                        self.status.set(f'Finished in {human_eta(secs)}')
                        self.progress['value'] = 100
                    else:
                        self.status.set('Finished with errors — see log')
                    for key, iid in self.rows.items():
                        if self.tree.set(iid, 'status') not in ('✔ Done', 'Error'):
                            if data is None:
                                self.tree.set(iid, 'status', 'Cancelled')
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
