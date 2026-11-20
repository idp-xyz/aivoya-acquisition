#!/usr/bin/env python3
"""Reproducible archive integrity validation. Regenerate manifest after intentional changes."""
from __future__ import annotations
import hashlib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = root / 'MANIFEST_SHA256.txt'
issues = []
for line in manifest.read_text(encoding='utf8').splitlines():
    expected, path = line.split('  ', 1)
    target = root / path
    if not target.is_file():
        issues.append('MISSING ' + path)
    elif hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        issues.append('CHANGED ' + path)
if issues:
    raise SystemExit('Manifest verification FAILED:\n' + '\n'.join(issues))
print('SHA-256 manifest verified:', len(manifest.read_text(encoding='utf8').splitlines()), 'files')
