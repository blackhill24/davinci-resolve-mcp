/** The duplicated `_resolve-verify` helper must not diverge (#121 task 5).
 *
 * #119's finding on the Python side was that a helper copied into several test
 * files had drifted between copies, so breaking one of them failed nothing. The
 * Node suite has exactly one such duplication by design: each vendored package
 * carries its own `__tests__/_resolve-verify.js` so a test file never reaches
 * across a package boundary.
 *
 * The copies are currently equivalent — only their comments and export
 * formatting differ. That is worth nothing unless something checks it, because
 * a copy that silently started returning `true` from `isResolveVerifyEnabled()`
 * would run Resolve-in-loop assertions in CI, and one that always returned
 * `false` would skip them even with RESOLVE_VERIFY=1 and report as passing.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { existsSync } from 'node:fs';

const require = createRequire(import.meta.url);
const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

const COPIES = [
  'vendor/drp-format/__tests__/_resolve-verify.js',
  'vendor/drt-format/__tests__/_resolve-verify.js',
];

test('every expected copy of the helper exists', () => {
  // A glob/list that stopped matching would make the comparisons below vacuous.
  for (const rel of COPIES) {
    assert.ok(existsSync(join(ROOT, rel)), `missing helper copy: ${rel}`);
  }
  assert.ok(COPIES.length >= 2, 'nothing to compare');
});

test('isResolveVerifyEnabled agrees across copies for every input', () => {
  const modules = COPIES.map((rel) => require(join(ROOT, rel)));
  const original = process.env.RESOLVE_VERIFY;
  try {
    for (const value of ['1', 'true', 'yes', '0', 'false', 'no', '', 'TRUE', undefined]) {
      if (value === undefined) delete process.env.RESOLVE_VERIFY;
      else process.env.RESOLVE_VERIFY = value;
      const results = modules.map((m) => m.isResolveVerifyEnabled());
      assert.equal(
        new Set(results).size, 1,
        `copies disagree for RESOLVE_VERIFY=${JSON.stringify(value)}: ${JSON.stringify(results)}`,
      );
    }
  } finally {
    if (original === undefined) delete process.env.RESOLVE_VERIFY;
    else process.env.RESOLVE_VERIFY = original;
  }
});

test('the gate is not stuck open or shut', () => {
  // Both directions, so a copy hard-wired to a constant fails here rather than
  // silently running (or silently skipping) every Resolve-in-loop assertion.
  const modules = COPIES.map((rel) => require(join(ROOT, rel)));
  const original = process.env.RESOLVE_VERIFY;
  try {
    process.env.RESOLVE_VERIFY = '1';
    for (const m of modules) assert.equal(m.isResolveVerifyEnabled(), true);
    process.env.RESOLVE_VERIFY = '0';
    for (const m of modules) assert.equal(m.isResolveVerifyEnabled(), false);
  } finally {
    if (original === undefined) delete process.env.RESOLVE_VERIFY;
    else process.env.RESOLVE_VERIFY = original;
  }
});

test('each copy exports the same surface', () => {
  const surfaces = COPIES.map((rel) => Object.keys(require(join(ROOT, rel))).sort().join(','));
  assert.equal(new Set(surfaces).size, 1, `export surfaces differ: ${surfaces.join(' | ')}`);
});
