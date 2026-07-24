/** Native-dependency preflight (#104 finding 11).
 *
 * ABI drift in better-sqlite3/sharp used to surface as dozens of ERR_DLOPEN_FAILED
 * test failures that read like real regressions. The preflight must tell the three
 * states apart: loads / not installed (optional, fine) / built for the wrong ABI. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { classifyNativeLoadError, checkNativeModules, formatReport, loadAndProbe, NATIVE_MODULES } from '../scripts/preflight-native.mjs';

const abiError = () => {
  const e = new Error(
    '/x/better_sqlite3.node was compiled against a different Node.js version using ' +
      'NODE_MODULE_VERSION 137. This version of Node.js requires NODE_MODULE_VERSION 147.',
  );
  e.code = 'ERR_DLOPEN_FAILED';
  return e;
};

const missingError = () => {
  const e = new Error("Cannot find module 'better-sqlite3'");
  e.code = 'MODULE_NOT_FOUND';
  return e;
};

test('classify: an uninstalled optional dep is "missing", not a failure', () => {
  assert.equal(classifyNativeLoadError(missingError()), 'missing');
  const esm = new Error('not found');
  esm.code = 'ERR_MODULE_NOT_FOUND';
  assert.equal(classifyNativeLoadError(esm), 'missing');
});

test('classify: ABI drift is detected by code and by message', () => {
  assert.equal(classifyNativeLoadError(abiError()), 'abi');
  const noCode = new Error('was compiled against a different Node.js version using NODE_MODULE_VERSION 137');
  assert.equal(classifyNativeLoadError(noCode), 'abi');
});

test('classify: anything else is "broken"', () => {
  assert.equal(classifyNativeLoadError(new Error('segfault in init')), 'broken');
});

test('checkNativeModules ignores absent optional deps', () => {
  const problems = checkNativeModules(NATIVE_MODULES, () => {
    throw missingError();
  });
  assert.deepEqual(problems, [], 'an optionalDependency that was never installed is not a problem');
});

test('checkNativeModules reports every ABI-broken module', () => {
  const problems = checkNativeModules(NATIVE_MODULES, () => {
    throw abiError();
  });
  assert.equal(problems.length, NATIVE_MODULES.length);
  assert.ok(problems.every((p) => p.state === 'abi'));
});

test('checkNativeModules is silent when the modules load', () => {
  assert.deepEqual(
    checkNativeModules(NATIVE_MODULES, () => ({})),
    [],
  );
});

test('the report names the module and the exact rebuild command', () => {
  const problems = checkNativeModules([NATIVE_MODULES[0]], () => {
    throw abiError();
  });
  const report = formatReport(problems);
  assert.match(report, /npm rebuild better-sqlite3/);
  assert.match(report, /NODE_MODULE_VERSION 137/);
  assert.match(report, /stale install, not a code regression/);
});

// The defect that end-to-end verification caught: require() alone is NOT a
// sufficient check. better-sqlite3 binds its .node lazily inside the Database
// constructor, so a wrong-ABI binding imports fine and only dies on first use —
// which let a broken install sail past the preflight and produce exactly the
// ERR_DLOPEN_FAILED pile this exists to prevent.

test('every native module declares a probe that exercises the binding', () => {
  for (const mod of NATIVE_MODULES) {
    assert.equal(typeof mod.probe, 'function', `${mod.name} must declare a probe`);
  }
});

test('a module that imports fine but fails on first use is still caught', () => {
  const lazilyBroken = {
    name: 'better-sqlite3',
    enables: 'x',
    probe: () => {
      const e = new Error('better_sqlite3.node ... NODE_MODULE_VERSION 137 ... requires 147');
      e.code = 'ERR_DLOPEN_FAILED';
      throw e;
    },
  };
  // load() succeeds (the import works); only the probe throws.
  const problems = checkNativeModules([lazilyBroken], (mod) => mod.probe({}));
  assert.equal(problems.length, 1, 'a lazily-binding module must not slip through');
  assert.equal(problems[0].state, 'abi');
});

test("better-sqlite3's probe really constructs a Database", () => {
  const mod = NATIVE_MODULES.find((m) => m.name === 'better-sqlite3');
  let opened = null;
  class FakeDatabase {
    constructor(file) {
      opened = file;
    }
    close() {
      this.closed = true;
    }
  }
  mod.probe(FakeDatabase);
  assert.equal(opened, ':memory:', 'must open a throwaway in-memory DB, not just import');
});

test('loadAndProbe surfaces a real failure from the installed module', () => {
  // Not installed at all -> MODULE_NOT_FOUND, which checkNativeModules treats as fine.
  assert.throws(() => loadAndProbe({ name: 'definitely-not-installed-104', probe: () => {} }), /Cannot find module/);
});
