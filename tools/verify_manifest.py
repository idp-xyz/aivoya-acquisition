#!/usr/bin/env python3
"""Reproducible archive integrity validation. Regenerate manifest after intentional changes."""
from __future__ import annotations
import hashlib
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = root / 'MANIFEST_SHA256.txt'
issues = []
listed = []
for line in manifest.read_text(encoding='utf8').splitlines():
    expected, path = line.split('  ', 1)
    listed.append(path)
    target = root / path
    if not target.is_file():
        issues.append('MISSING ' + path)
    elif hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        issues.append('CHANGED ' + path)

# Inside a git checkout, a tracked file that is not listed (for example a new
# go.sum) must fail the check. An export without git metadata keeps the
# previous behavior and only checks paths already in the manifest.
if (root / '.git').exists():
    try:
        out = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root, stderr=subprocess.PIPE)
    except OSError:
        issues.append('GIT_LIST_FAILED git executable not found')
    except subprocess.CalledProcessError as exc:
        err = exc.stderr.decode('utf-8', 'replace').strip().replace('\n', ' ')
        issues.append('GIT_LIST_FAILED ' + err)
    else:
        listed_set = set(listed)
        tracked = [raw.decode('utf-8') for raw in out.split(b'\0') if raw]
        for path in tracked:
            if path != 'MANIFEST_SHA256.txt' and path not in listed_set:
                issues.append('UNLISTED ' + path)

if issues:
    raise SystemExit('Manifest verification FAILED:\n' + '\n'.join(issues))
print('SHA-256 manifest verified:', len(manifest.read_text(encoding='utf8').splitlines()), 'files')
