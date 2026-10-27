#!/usr/bin/env python3
import hashlib, pathlib, sys
root = pathlib.Path(__file__).resolve().parent
installed = pathlib.Path(sys.argv[1]).resolve()
for line in (root / 'SHA256SUMS').read_text().splitlines():
    digest, relative = line.split('  ', 1)
    snapshot = root / relative
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == digest, f'Snapshot changed: {relative}'
    if relative.startswith('extension/'):
        copy = installed / relative[len('extension/'):]
        assert copy.read_bytes() == snapshot.read_bytes(), f'Extension drift: {copy}'
print('Shared snapshot and installed extension match SHA256SUMS')
