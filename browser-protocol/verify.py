#!/usr/bin/env python3
"""Verify a snapshot and (when vendored) its exact upstream Git commit, offline."""
import argparse
import hashlib
import pathlib
import stat


def git_object(kind, data):
    return hashlib.sha1(kind.encode() + b' ' + str(len(data)).encode() + b'\0' + data).digest()


def tree_hash(root, paths):
    children = {}
    for relative in paths:
        first, *tail = relative.split('/', 1)
        children.setdefault(first, []).extend(tail)
    entries = []
    for name, descendants in children.items():
        file = root / name
        if descendants:
            mode, digest, sort_key = b'40000', tree_hash(file, descendants), (name + '/').encode()
        else:
            assert not file.is_symlink(), f'Unexpected symlink: {file}'
            mode = b'100755' if file.stat().st_mode & stat.S_IXUSR else b'100644'
            digest, sort_key = git_object('blob', file.read_bytes()), name.encode()
        entries.append((sort_key, mode + b' ' + name.encode() + b'\0' + digest))
    return git_object('tree', b''.join(entry for _, entry in sorted(entries)))


def verify(root, installed, require_pin=False):
    paths = []
    for line in (root / 'SHA256SUMS').read_text().splitlines():
        digest, relative = line.split('  ', 1)
        assert not pathlib.PurePosixPath(relative).is_absolute() and '..' not in pathlib.PurePosixPath(relative).parts
        snapshot = root / relative
        assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == digest, f'Snapshot changed: {relative}'
        paths.append(relative)
        if relative.startswith('extension/'):
            copy = installed / relative[len('extension/'):]
            assert copy.read_bytes() == snapshot.read_bytes(), f'Extension drift: {copy}'
    pin = root / 'PINNED_COMMIT'
    if require_pin or pin.exists():
        commit = (root / 'UPSTREAM_COMMIT').read_bytes()
        expected = pin.read_text().strip()
        assert git_object('commit', commit).hex() == expected, 'PINNED_COMMIT does not match upstream commit object'
        tree = next(line[5:] for line in commit.splitlines() if line.startswith(b'tree ')).decode()
        assert tree_hash(root, paths + ['SHA256SUMS']).hex() == tree, 'Vendored files differ from pinned upstream Git tree'
        print('Verified upstream commit', expected)
    print('Shared snapshot and installed extension match SHA256SUMS')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('installed', nargs='?', default=str(pathlib.Path(__file__).resolve().parent / 'extension'))
    parser.add_argument('--pinned', action='store_true', help='require and verify the vendored upstream commit proof')
    args = parser.parse_args()
    verify(pathlib.Path(__file__).resolve().parent, pathlib.Path(args.installed).resolve(), args.pinned)
