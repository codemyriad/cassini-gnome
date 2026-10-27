#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
const { runCase } = require('../../extension/tests/support/app-control-harness.cjs');
(async () => {
  let failed = 0;
  const vectors = JSON.parse(fs.readFileSync(path.join(__dirname, 'vectors.json')));
  for (const vector of vectors) {
    try { await runCase(vector); console.log('PASS', vector.name); }
    catch (error) { failed++; console.error('FAIL', vector.name, error.message); }
  }
  console.log(`${vectors.length - failed} passed, ${failed} failed`); process.exitCode = failed ? 1 : 0;
})();
