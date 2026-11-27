"""Write release notes: what changed since the previous release.

usage: release_notes.py PREVIOUS_RELEASE_BODY_FILE YTDLP_VERSION COMMIT_SHA BUILD_NUMBER > notes.md

The previous release's notes end with "yt-dlp: X" and "commit: SHA" lines (the app's updater and the
weekly build read them too); the commits between that SHA and this one become the patch notes.
"""
import os
import re
import subprocess
import sys

TRAILERS = ('co-authored-by:', 'claude-session:', 'signed-off-by:')
# Only changes to these files are news for people using the app (README edits, release tooling etc. are not)
APP_FILES = ('ytdlp_gui.py', 'ytdlp_gui.pyw', 'build_exe.bat', '.github/workflows/build-gui-exe.yml')
FOOTER = ('Update with **Update now** inside the app, or download the zip below, unblock it '
          '(right-click → Properties → Unblock) and unzip it. Keep ffmpeg.exe, ffprobe.exe and deno.exe '
          'next to yt-dlp GUI.exe.')


def git(*args):
    return subprocess.run(['git', *args], capture_output=True, text=True, encoding='utf-8').stdout


def is_commit(sha):
    return bool(sha) and git('cat-file', '-t', sha).strip() == 'commit'


def commits_between(prev_sha, sha):
    """[(subject, [detail, ...]), ...] for commits after prev_sha up to sha (or just sha if no prev)."""
    rng = [f'{prev_sha}..{sha}'] if prev_sha else ['-1', sha]
    raw = git('log', '--reverse', '--no-merges', '--format=%x1e%s%x1f%b', *rng, '--', *APP_FILES)
    changes = []
    for entry in raw.split('\x1e'):
        if not entry.strip():
            continue
        subject, _, text = entry.partition('\x1f')
        details = []
        for line in text.splitlines():
            s = line.strip()
            if s.lower().startswith(TRAILERS):
                continue
            if s.startswith(('- ', '* ')):
                details.append(s[2:].strip())
            elif s and details and line.startswith('  '):
                details[-1] += ' ' + s  # wrapped bullet
        changes.append((subject.strip(), details))
    return changes


def make_notes(prev_sha, prev_yt, ytdlp, sha, build, first=False, lines=None):
    if lines:  # hand-written notes
        changes = [(line, []) for line in lines]
    else:
        changes = commits_between(prev_sha, sha) if (first or is_commit(prev_sha)) else []
    out = [f"## What's new in build {build}", '']
    if first:
        out[0] = f'## Build {build} — first release'
    for subject, details in changes:
        out.append(f'- **{subject}**' if details else f'- {subject}')
        out += [f'  - {d}' for d in details]
    if prev_yt and ytdlp and prev_yt != ytdlp:
        out.append(f'- yt-dlp updated: {prev_yt} → {ytdlp} (fixes for sites that changed recently)')
    if len(out) == 2:
        out.append('- Rebuilt with no changes to the app' if prev_sha else '- First release with patch notes')
    out += ['', FOOTER, '', '---']
    if ytdlp:
        out.append(f'yt-dlp: {ytdlp}')
    out.append(f'commit: {sha}')
    return '\n'.join(out)


def main():
    body_file, ytdlp, sha, build = sys.argv[1:5]
    prev = ''
    if os.path.exists(body_file):
        with open(body_file, encoding='utf-8') as f:
            prev = f.read()
    m_sha = re.search(r'^commit:\s*([0-9a-f]{40})', prev, re.M)
    m_yt = re.search(r'^yt-dlp:\s*(\S+)', prev, re.M)
    sys.stdout.reconfigure(encoding='utf-8')
    print(make_notes(m_sha.group(1) if m_sha else None, m_yt.group(1) if m_yt else None, ytdlp, sha, build))


if __name__ == '__main__':
    main()
