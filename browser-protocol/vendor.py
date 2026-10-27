#!/usr/bin/env python3
"""Vendor an upstream commit from this Git checkout, preserving its Git identity proof."""
import argparse
import io
import pathlib
import shutil
import subprocess
import tarfile

root = pathlib.Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('commit', help='full upstream commit SHA')
parser.add_argument('destination', type=pathlib.Path, help='app/browser-protocol')
parser.add_argument('extension', type=pathlib.Path, help='app/BrowserExtension or app/browser-extension')
args = parser.parse_args()
assert len(args.commit) == 40 and all(c in '0123456789abcdef' for c in args.commit)
assert args.destination.resolve() != root and args.extension.resolve() != root / 'extension'
def git(*args): return subprocess.check_output(['git', '-C', str(root), *args])
archive = git('archive', args.commit)
args.destination.mkdir(parents=True, exist_ok=True)
# Remove only the old contract's tracked file list, preserving unrelated local artifacts.
index = args.destination / 'SHA256SUMS'
if index.exists():
    for line in index.read_text().splitlines():
        relative = line.split('  ', 1)[1]
        assert not pathlib.Path(relative).is_absolute() and '..' not in pathlib.Path(relative).parts
        (args.destination / relative).unlink(missing_ok=True)
with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
    tar.extractall(args.destination)
(args.destination / 'PINNED_COMMIT').write_text(args.commit + '\n')
(args.destination / 'UPSTREAM_COMMIT').write_bytes(git('cat-file', 'commit', args.commit))
shutil.copytree(args.destination / 'extension', args.extension, dirs_exist_ok=True)
subprocess.run(['python3', str(args.destination / 'verify.py'), '--pinned', str(args.extension)], check=True)
