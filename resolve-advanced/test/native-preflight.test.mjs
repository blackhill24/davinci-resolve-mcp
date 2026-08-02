/** Native-dependency preflight (#104 finding 11).
 *
 * ABI drift in better-sqlite3/sharp used to surface as dozens of ERR_DLOPEN_FAILED
 * test failures that read like real regressions. The preflight must tell the three
 * states apart: loads / not installed (optional, fine) / built for the wrong ABI. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  classifyNativeLoadError,
  checkNativeModules,
  checkScriptApprovals,
  formatApprovalWarning,
  formatReport,
  hasInstallScript,
  loadAndProbe,
  NATIVE_MODULES,
} from '../scripts/preflight-native.mjs';

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

// --- Approval drift -------------------------------------------------------
// The Node-26 incident had two halves. The load check above catches the first
// (a stale binary). These cover the second: npm 12 refuses to run install
// scripts that aren't in package.json's `allowScripts`, keyed by EXACT version
// — and when it refuses, `npm rebuild` prints "rebuilt dependencies
// successfully" and builds nothing. By the time the load check fires, the
// documented fix is already a silent no-op. So drift must be reported while
// everything still works, or it is guaranteed to be discovered the hard way.

const fakeReader = (root, installed) => (name) => (name === null ? root : installed[name] || null);
const withInstallScript = (version) => ({ version, scripts: { install: 'node-gyp rebuild' } });

test('approval drift: an installed native dep missing from allowScripts is flagged', () => {
  const drift = checkScriptApprovals(
    [{ name: 'better-sqlite3', enables: 'project-DB tests' }],
    fakeReader({ allowScripts: {} }, { 'better-sqlite3': withInstallScript('12.11.1') }),
  );
  assert.equal(drift.length, 1);
  assert.equal(drift[0].state, 'unapproved');
  assert.equal(drift[0].version, '12.11.1');
});

// The specific rot this exists for: approving 12.11.1 does NOT approve 12.12.0.
// A routine version bump silently un-approves the package, and nothing else in
// the toolchain says so until a rebuild is needed and quietly does nothing.
test('approval drift: a version bump past the approved key is flagged', () => {
  const allowScripts = { 'better-sqlite3@12.11.1': true };
  const modules = [{ name: 'better-sqlite3', enables: 'project-DB tests' }];

  const approved = checkScriptApprovals(modules, fakeReader({ allowScripts }, { 'better-sqlite3': withInstallScript('12.11.1') }));
  assert.deepEqual(approved, [], 'the exact approved version is clean');

  const bumped = checkScriptApprovals(modules, fakeReader({ allowScripts }, { 'better-sqlite3': withInstallScript('12.12.0') }));
  assert.equal(bumped.length, 1, 'a bump past the approved key must be flagged');
  assert.equal(bumped[0].version, '12.12.0');
});

test('approval drift: an explicit deny is reported distinctly from a missing entry', () => {
  const drift = checkScriptApprovals(
    [{ name: 'sharp', enables: 'frame compare' }],
    fakeReader({ allowScripts: { 'sharp@0.33.5': false } }, { sharp: withInstallScript('0.33.5') }),
  );
  assert.equal(drift[0].state, 'denied', 'a deliberate deny is not the same as an oversight');
});

test('approval drift: absent optional deps and script-less packages are not flagged', () => {
  const modules = [{ name: 'better-sqlite3', enables: 'x' }, { name: 'sharp', enables: 'y' }];
  assert.deepEqual(
    checkScriptApprovals(modules, fakeReader({ allowScripts: {} }, {})),
    [],
    'never installed — nothing to approve',
  );
  assert.deepEqual(
    checkScriptApprovals(modules, fakeReader({ allowScripts: {} }, { sharp: { version: '1.0.0', scripts: { test: 'x' } } })),
    [],
    'no install script — npm has nothing to gate',
  );
});

test('hasInstallScript covers every hook npm gates, not just "install"', () => {
  assert.equal(hasInstallScript({ scripts: { install: 'x' } }), true);
  assert.equal(hasInstallScript({ scripts: { preinstall: 'x' } }), true);
  assert.equal(hasInstallScript({ scripts: { postinstall: 'x' } }), true);
  assert.equal(hasInstallScript({ scripts: { build: 'x' } }), false);
  assert.equal(hasInstallScript({}), false);
});

test('the warning names the approve command and the silent-no-op trap', () => {
  const warning = formatApprovalWarning([{ name: 'sharp', version: '0.33.5', enables: 'frame compare', state: 'unapproved' }]);
  assert.match(warning, /npm install-scripts approve sharp/);
  assert.match(warning, /sharp@0\.33\.5/);
  assert.match(warning, /reports success and builds nothing/, 'the trap must be spelled out, not implied');
  assert.match(warning, /WARNING \(not a failure yet\)/, 'must not read as a hard failure — the package still works');
});

// A guard nobody can act on is noise. This is the real repo state, so it fails
// the moment an actual native dep here goes unapproved.
test('the repo as it stands has no approval drift', () => {
  assert.deepEqual(checkScriptApprovals(), [], 'approve the named package, or this is a live warning on every test run');
});
