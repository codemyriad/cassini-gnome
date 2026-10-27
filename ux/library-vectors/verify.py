#!/usr/bin/env python3
# SPDX-License-Identifier: CC0-1.0
"""Check S-3a fixture structure/pins/determinism, never derive expected rows."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

HERE=Path(__file__).resolve().parent
FIELDS={'id','origin','lifecycle','interruptedAtMs','transcript','etaMinutes','error','badge','title','startMs','durationMs','names','quote','canWriteDown','speakers'}


def tree(root):
    entries={}
    for parent,dirs,files in os.walk(root,followlinks=False):
        for name in dirs+files:
            path=Path(parent)/name;info=path.lstat()
            value=[stat.S_IFMT(info.st_mode),stat.S_IMODE(info.st_mode)]
            if stat.S_ISLNK(info.st_mode):
                value.append(os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                digest=hashlib.sha256()
                with path.open('rb') as stream:
                    while block:=stream.read(1<<20):digest.update(block)
                value.extend([info.st_size,digest.hexdigest()])
            entries[str(path.relative_to(root))]=value
    return entries


def main():
    cases=json.loads((HERE/'cases.json').read_text())['cases']
    names={case['id'] for case in cases}
    assert len(names)==len(cases)
    assert {p.stem for p in (HERE/'expected').glob('*.json')}==names
    assert {f'R{i}' for i in range(1,10)} <= {rule for case in cases for rule in case['rules']}
    for case in cases:
        expected=json.loads((HERE/'expected'/f"{case['id']}.json").read_text())
        assert set(expected)=={'scan','groups'}
        assert expected['scan'] in ('ok','empty','failed')
        for group in expected['groups']:
            assert set(group)=={'title','meetings'}
            for row in group['meetings']:
                assert set(row)==FIELDS,(case['id'],set(row)^FIELDS)
                assert all(set(s)=={'id','name','colourIndex'} for s in row['speakers'])
    with tempfile.TemporaryDirectory(prefix='cassini-library-vectors-') as temporary:
        a,b=Path(temporary)/'a',Path(temporary)/'b'
        for out in (a,b):subprocess.run([sys.executable,str(HERE/'make.py'),str(out)],check=True)
        assert tree(a)==tree(b),'Fixture generation is nondeterministic'
    print(f'Verified {len(cases)} hand-authored cases, R1–R9 coverage and byte-identical builds')


if __name__=='__main__':main()
