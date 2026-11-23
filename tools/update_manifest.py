#!/usr/bin/env python3
"""Regenerate MANIFEST_SHA256.txt from git-tracked file contents.

Format is ``<sha256><two spaces><path>`` with a trailing newline, matching
the existing manifest. Paths already listed keep that order. Git-tracked
files that are not listed yet are appended in lexicographic path order.
The manifest does not list itself.

When ``.git`` is absent (a ``git archive`` export), every file in the tree
is treated as the tracked set. Untracked files in a real checkout are not
included.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = 'MANIFEST_SHA256.txt'
MANIFEST = ROOT / MANIFEST_NAME


def tracked_paths(root: Path) -> set[str]:
    if not (root / '.git').exists():
        found = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name != '.git']
            for name in filenames:
                found.append((Path(dirpath) / name).relative_to(root).as_posix())
        return set(found)
    try:
        out = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root, stderr=subprocess.PIPE)
    except OSError:
        sys.exit('git metadata is present but the git executable was not found')
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.decode('utf-8', 'replace').strip()
        sys.exit('git ls-files failed' + (': ' + err if err else ''))
    return {raw.decode('utf-8') for raw in out.split(b'\0') if raw}


def existing_order(manifest: Path) -> list[str]:
    if not manifest.is_file():
        return []
    order = []
    for line in manifest.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        _digest, path = line.split('  ', 1)
        order.append(path)
    return order


def main() -> None:
    paths = tracked_paths(ROOT)
    paths.discard(MANIFEST_NAME)
    ordered = []
    seen = set()
    for path in existing_order(MANIFEST):
        if path in paths and path not in seen:
            ordered.append(path)
            seen.add(path)
    for path in sorted(paths - seen):
        ordered.append(path)
    lines = []
    for path in ordered:
        target = ROOT / path
        if not target.is_file():
            sys.exit('tracked path is not a file: ' + path)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        lines.append(digest + '  ' + path + '\n')
    MANIFEST.write_bytes(''.join(lines).encode('utf-8'))
    print('Wrote ' + MANIFEST_NAME + ':', len(lines), 'files')


if __name__ == '__main__':
    main()
