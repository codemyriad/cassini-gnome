#!/usr/bin/env python3
"""Run only in a disposable worktree: temporary source changes restore in finally."""
from pathlib import Path
import os,subprocess
root=Path(__file__).resolve().parents[2]
ev=Path(os.environ['CASSINI_RELAY_EVIDENCE']).expanduser().resolve();ev.mkdir(parents=True,exist_ok=True)
proof=root/'proofs/native-relay-events'
test=root/'extension/tests/multistream-relay.test.cjs';fixed=test.read_bytes()
command=['node','--require',str(proof/'manual-lifetime.cjs'),'--test',str(test)]
def run(name,command,expected_failure=False):
 with (ev/(name+'.log')).open('w') as log:r=subprocess.run(command,cwd=root,stdout=log,stderr=subprocess.STDOUT,timeout=600)
 text=(ev/(name+'.log')).read_text()
 assert (r.returncode!=0)==expected_failure,(name,r.returncode)
 return text
try:
 test.write_bytes(subprocess.check_output(['git','show','475b8b792ff0515b35935ef4c5496dd1180a329f:extension/tests/multistream-relay.test.cjs'],cwd=root))
 assert 'Manual lifetime deadline expired during a live native response' in run('before',command,True)
finally:test.write_bytes(fixed)
run('after',command)
source=root/'extension/background.js';original=source.read_bytes()
try:
 subprocess.run(['git','apply','--check',str(proof/'strip-provenance.patch')],cwd=root,check=True)
 subprocess.run(['git','apply',str(proof/'strip-provenance.patch')],cwd=root,check=True)
 output=run('mutation',['node','--test',str(test)],True)
 assert 'ERR_ASSERTION' in output and 'dom' in output and 'timed out' not in output
finally:source.write_bytes(original)
files=['tests/event-value.test.mjs']+[str(f.relative_to(root)) for f in sorted((root/'extension/tests').glob('*.test.cjs'))]
run('restored',['node','--test',*files])
print('Baseline failure, corrected exchange, real production mutation and restored full suite verified')
