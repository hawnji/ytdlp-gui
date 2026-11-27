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


def git(*args):
    return subprocess.run(['git', *args], capture_output=True, text=True, encoding='utf-8').stdout


def main():
    body_file, ytdlp, sha, build = sys.argv[1:5]
    prev = ''
    if os.path.exists(body_file):
        with open(body_file, encoding='utf-8') as f:
            prev = f.read()
    m_sha = re.search(r'^commit:\s*([0-9a-f]{40})', prev, re.M)
    m_yt = re.search(r'^yt-dlp:\s*(\S+)', prev, re.M)
    prev_sha = m_sha.group(1) if m_sha else None
    prev_yt = m_yt.group(1) if m_yt else None

    changes = []
    if prev_sha and git('cat-file', '-t', prev_sha).strip() == 'commit':
        raw = git('log', '--reverse', '--no-merges', '--format=%x1e%s%x1f%b', f'{prev_sha}..{sha}')
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

    out = [f"## What's new in build {build}", '']
    for subject, details in changes:
        out.append(f'- **{subject}**' if details else f'- {subject}')
        out += [f'  - {d}' for d in details]
    if prev_yt and prev_yt != ytdlp:
        out.append(f'- yt-dlp updated: {prev_yt} → {ytdlp} (fixes for sites that changed recently)')
    if len(out) == 2:
        out.append('- Rebuilt with no changes to the app' if prev_sha else '- First release with patch notes')
    out += ['',
            'Update with **Update now** inside the app, or download the zip below, unblock it '
            '(right-click → Properties → Unblock) and unzip it. Keep ffmpeg.exe, ffprobe.exe and deno.exe '
            'next to yt-dlp GUI.exe.',
            '',
            '---',
            f'yt-dlp: {ytdlp}',
            f'commit: {sha}']
    sys.stdout.reconfigure(encoding='utf-8')
    print('\n'.join(out))


if __name__ == '__main__':
    main()
