#!/usr/bin/env python3
"""Refresh checksums after staging upstream edits; run before committing."""
import hashlib
import pathlib
import subprocess
root = pathlib.Path(__file__).resolve().parent
files = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode().split('\0')
(root / 'SHA256SUMS').write_text(''.join(
    hashlib.sha256((root / relative).read_bytes()).hexdigest() + '  ' + relative + '\n'
    for relative in sorted(files) if relative and relative != 'SHA256SUMS'))
