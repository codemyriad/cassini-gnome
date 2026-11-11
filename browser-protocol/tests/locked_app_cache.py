#!/usr/bin/env python3
"""Content-addressed CI cache for the pinned disposable Talk harness archives."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit


def archives(stack):
    result = []
    for app, lock in sorted(stack['apps'].items()):
        if lock['source'] == 'bundled':
            continue
        if lock['source'] != 'archive' or not re.fullmatch(r'[a-f0-9]{64}', lock['sha256']):
            raise ValueError(f'invalid archive lock: {app}')
        url = urlsplit(lock['url'])
        if url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None:
            raise ValueError(f'archive must use credential-free HTTPS: {app}')
        result.append({'app': app, 'sha256': lock['sha256'], 'url': lock['url']})
    if not result:
        raise ValueError('no locked archive apps')
    return result


def cache_key(stack):
    body = json.dumps(archives(stack), sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(body).hexdigest()


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def populate(stack, directory, opener=None, offline=False):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    results = []
    for lock in archives(stack):
        started = time.monotonic()
        destination = directory / (lock['sha256'] + '.tar.gz')
        hit = destination.exists()
        if not hit:
            if offline:
                raise ValueError('offline cache entry missing: ' + lock['app'])
            # One request only. An outage or bad payload fails before Docker starts;
            # no retry/fallback may disguise a download or verification failure.
            with tempfile.NamedTemporaryFile(dir=directory, prefix='download-', suffix='.partial', delete=False) as stream:
                temporary = Path(stream.name)
                try:
                    print("Fetching locked " + lock['app'] + " SHA256 " + lock['sha256'], flush=True)
                    with (opener or urllib.request.urlopen)(lock['url'], timeout=90) as response:
                        total = 0
                        while chunk := response.read(1024 * 1024):
                            total += len(chunk)
                            if total > 512 * 1024 * 1024:
                                raise ValueError('archive exceeds 512 MiB bound: ' + lock['app'])
                            stream.write(chunk)
                    stream.close()
                    if sha256(temporary) != lock['sha256']:
                        raise ValueError('download checksum mismatch: ' + lock['app'])
                    os.replace(temporary, destination)
                finally:
                    temporary.unlink(missing_ok=True)  # Only this invocation's partial file.
        if sha256(destination) != lock['sha256']:
            raise ValueError('cached archive checksum mismatch: ' + lock['app'])
        results.append({'app': lock['app'], 'sha256': lock['sha256'], 'cacheHit': hit,
                        'bytes': destination.stat().st_size,
                        'elapsedMs': round((time.monotonic() - started) * 1000, 3)})
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['key', 'populate'])
    parser.add_argument('--stack', required=True, type=Path)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--offline', action='store_true', help='refuse a cache miss without a request')
    args = parser.parse_args()
    stack = json.loads(args.stack.read_text())
    if args.action == 'key':
        print(cache_key(stack))
        return
    if not args.cache:
        parser.error('populate requires --cache')
    report = {'key': cache_key(stack), 'archives': populate(stack, args.cache, offline=args.offline)}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
