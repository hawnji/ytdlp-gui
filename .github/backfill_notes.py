"""Give older releases patch notes, once.

Releases whose notes don't start with "What's new" get notes listing the changes since the build before
them. Which commit each build came from is read from release_commits.json (or a "commit:" line).
usage: backfill_notes.py            (needs GH_TOKEN and full git history)
       backfill_notes.py --dry-run  (print what would be written)
"""
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from release_notes import make_notes  # noqa: E402

DRY = '--dry-run' in sys.argv


def gh(*args):
    r = subprocess.run(['gh', *args], capture_output=True, text=True, encoding='utf-8')
    if r.returncode != 0:
        raise RuntimeError(f'gh {" ".join(args)} failed: {r.stderr.strip()}')
    return r.stdout


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, 'release_commits.json'), encoding='utf-8') as f:
        commits = {int(k): v for k, v in json.load(f).items() if k.isdigit()}
    overrides = {}
    if os.path.exists(os.path.join(here, 'release_overrides.json')):
        with open(os.path.join(here, 'release_overrides.json'), encoding='utf-8') as f:
            overrides = {int(k): v for k, v in json.load(f).items() if k.isdigit()}

    releases = json.loads(gh('release', 'list', '--limit', '500', '--json', 'tagName'))
    tags = {}
    for r in releases:
        m = re.fullmatch(r'build-(\d+)', r['tagName'])
        if m:
            tags[int(m.group(1))] = r['tagName']

    bodies = {n: json.loads(gh('release', 'view', tag, '--json', 'body'))['body'] or '' for n, tag in tags.items()}

    def recorded(n, key):
        m = re.search(rf'^{key}:\s*(\S+)', bodies.get(n, ''), re.M)
        return m.group(1) if m else None

    first = min(tags) if tags else None
    done = 0
    for n in sorted(tags):
        body = bodies[n]
        if re.search(r"^## (What's new|Build \d+ — first release)", body, re.M):
            continue
        sha = commits.get(n) or recorded(n, 'commit')
        if not sha:
            print(f'{tags[n]}: no commit known, skipping')
            continue
        earlier = [k for k in tags if k < n]
        prev = max(earlier) if earlier else None
        prev_sha = (commits.get(prev) or recorded(prev, 'commit')) if prev else None
        notes = make_notes(prev_sha, recorded(prev, 'yt-dlp') if prev else None, recorded(n, 'yt-dlp'),
                           sha, n, first=(n == first), lines=overrides.get(n))
        if DRY:
            print(f'===== {tags[n]} =====\n{notes}\n')
        else:
            with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False, encoding='utf-8') as tmp:
                tmp.write(notes)
            gh('release', 'edit', tags[n], '--notes-file', tmp.name)
            os.remove(tmp.name)
            print(f'{tags[n]}: patch notes added')
        done += 1
    print(f'{done} release(s) updated' if done else 'All releases already have patch notes')


if __name__ == '__main__':
    main()
