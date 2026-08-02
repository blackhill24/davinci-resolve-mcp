#!/usr/bin/env node
/**
 * Test-harness preflight for the optional NATIVE dependencies (#104 finding 11).
 *
 * better-sqlite3 and sharp ship compiled binaries pinned to an ABI
 * (NODE_MODULE_VERSION). Switch Node major versions without rebuilding and every
 * test that touches a project DB dies with ERR_DLOPEN_FAILED — the audit saw 32
 * such failures and they read as 32 real regressions rather than one stale
 * install. This turns that into a single actionable message before the suite runs.
 *
 * Three states per module, deliberately distinguished:
 *   - loads          → nothing to say
 *   - not installed  → fine, it's an optionalDependency; the suite skips those paths
 *   - ABI mismatch / broken build → hard stop with the rebuild command
 *
 * Escape hatch: SKIP_NATIVE_PREFLIGHT=1 runs the suite anyway.
 */

import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);

// `probe` must actually EXERCISE the native binding. require() alone is not
// enough for better-sqlite3: it binds lazily inside the Database constructor
// (via `bindings`), so a .node compiled for the wrong ABI imports perfectly
// happily and only explodes on first real use — which is precisely the pile of
// ERR_DLOPEN_FAILED test failures this preflight exists to pre-empt.
export const NATIVE_MODULES = [
  {
    name: 'better-sqlite3',
    enables: 'project-DB tests (lineage, reverse-clip, node-meta, rename round-trip)',
    probe: (Database) => new Database(':memory:').close(),
  },
  {
    name: 'sharp',
    enables: 'conform.verify frame-compare tests',
    probe: (sharp) => sharp.versions,
  },
];

/**
 * Classify a require() failure for a native optional dependency.
 * @returns {'missing'|'abi'|'broken'}
 */
export function classifyNativeLoadError(err) {
  const code = err && err.code;
  const message = String((err && err.message) || '');
  if (code === 'MODULE_NOT_FOUND' || code === 'ERR_MODULE_NOT_FOUND') return 'missing';
  if (code === 'ERR_DLOPEN_FAILED' || /NODE_MODULE_VERSION|was compiled against a different Node/.test(message)) {
    return 'abi';
  }
  return 'broken';
}

/** Import a module AND touch its native binding, so lazy binders can't slip through. */
export function loadAndProbe(mod) {
  const loaded = require(mod.name);
  if (typeof mod.probe === 'function') mod.probe(loaded);
  return loaded;
}

/** @returns {Array<{name: string, enables: string, state: string, message: string}>} problems worth failing on. */
export function checkNativeModules(modules = NATIVE_MODULES, load = loadAndProbe) {
  const problems = [];
  for (const mod of modules) {
    try {
      load(mod);
    } catch (err) {
      const state = classifyNativeLoadError(err);
      if (state === 'missing') continue; // optional and absent — not a failure
      problems.push({ ...mod, state, message: String((err && err.message) || err).split('\n')[0] });
    }
  }
  return problems;
}

// ---------------------------------------------------------------------------
// Approval drift (the second half of the Node-26 incident).
//
// npm 12 gates install scripts behind package.json's `allowScripts`, keyed by
// EXACT version. Two ways that silently rots, both of which cost real time:
//
//   1. A version bump (`better-sqlite3@12.11.1` → `12.12.0`) leaves the old key
//      behind, so the new version is unapproved again.
//   2. A package that ships a working prebuilt binary today (sharp) never needs
//      its install script — until the next Node major, when it does.
//
// In both cases npm's failure mode is the trap: `npm rebuild` prints "rebuilt
// dependencies successfully" while building nothing. So the load check alone is
// too late — by the time it fires, the obvious remediation is already a no-op.
// This flags the drift on EVERY run, while things still work.
// ---------------------------------------------------------------------------

/** Does this installed package run an install script npm would gate? */
export function hasInstallScript(pkgJson) {
  const s = (pkgJson && pkgJson.scripts) || {};
  return Boolean(s.preinstall || s.install || s.postinstall);
}

/**
 * @returns {Array<{name:string, version:string, enables:string, state:'unapproved'|'denied'}>}
 *   native modules whose install script npm will refuse to run as installed.
 */
export function checkScriptApprovals(modules = NATIVE_MODULES, readPkg = defaultReadPkg) {
  const root = readPkg(null);
  const allow = (root && root.allowScripts) || {};
  const drift = [];
  for (const mod of modules) {
    const pkg = readPkg(mod.name);
    if (!pkg) continue; // optional and absent — nothing to approve
    if (!hasInstallScript(pkg)) continue; // pure-JS or prebuilt-only: no gate applies
    const key = `${mod.name}@${pkg.version}`;
    if (allow[key] === true) continue;
    drift.push({ name: mod.name, version: pkg.version, enables: mod.enables, state: allow[key] === false ? 'denied' : 'unapproved' });
  }
  return drift;
}

function defaultReadPkg(name) {
  const url = name
    ? new URL(`../node_modules/${name}/package.json`, import.meta.url)
    : new URL('../package.json', import.meta.url);
  try {
    return JSON.parse(readFileSync(url, 'utf8'));
  } catch {
    return null;
  }
}

export function formatApprovalWarning(drift) {
  const lines = ['', '  Native dependency preflight — WARNING (not a failure yet).', ''];
  for (const d of drift) {
    lines.push(`  • ${d.name}@${d.version} runs an install script that npm will ${d.state === 'denied' ? 'REFUSE (explicitly denied)' : 'NOT run (not in allowScripts)'}.`);
    lines.push(`      enables: ${d.enables}`);
    lines.push(`      approve: npm install-scripts approve ${d.name}`);
    lines.push('');
  }
  lines.push('  It works right now on the binary already on disk. It will stop working the');
  lines.push('  moment that binary needs rebuilding — a Node major bump, or a version bump');
  lines.push('  (allowScripts is keyed by exact version, so bumps un-approve themselves).');
  lines.push('  When that happens `npm rebuild` reports success and builds nothing, so fix');
  lines.push('  this now rather than debugging that later.');
  lines.push('');
  return lines.join('\n');
}

export function formatReport(problems) {
  const lines = ['', '  Native dependency preflight FAILED — this is a stale install, not a code regression.', ''];
  for (const p of problems) {
    lines.push(`  • ${p.name} — ${p.state === 'abi' ? 'built for a different Node ABI' : 'failed to load'}`);
    lines.push(`      ${p.message}`);
    lines.push(`      enables: ${p.enables}`);
    lines.push(`      fix:     npm rebuild ${p.name}`);
    lines.push('');
  }
  // npm 12 gates install scripts behind package.json's `allowScripts`. An
  // unapproved package makes `npm rebuild` print "rebuilt dependencies
  // successfully" while building nothing, so the rebuild above looks like it
  // worked and the suite fails identically on the next run. Name that trap
  // here — it cost a session once.
  lines.push('  If `npm rebuild` reports success but this still fails, the install script is');
  lines.push('  blocked: `npm install-scripts ls`, then `npm install-scripts approve <pkg>`.');
  lines.push('  If the build then fails on missing V8 symbols, the pinned version predates');
  lines.push('  this Node major — bump it rather than rebuilding it.');
  lines.push('');
  lines.push(`  Running Node ${process.version} (ABI ${process.versions.modules}).`);
  lines.push('  Set SKIP_NATIVE_PREFLIGHT=1 to run the suite anyway.');
  lines.push('');
  return lines.join('\n');
}

function main() {
  if (process.env.SKIP_NATIVE_PREFLIGHT === '1') return;
  const problems = checkNativeModules();
  if (problems.length > 0) {
    process.stderr.write(formatReport(problems));
    process.exit(1);
  }
  // Everything loads — but say so now if a rebuild would be blocked when it is
  // eventually needed. Warn, never fail: a package working off its prebuilt
  // binary is not broken, and blocking the suite over it would be worse than
  // the problem. The point is that this is impossible to be surprised by.
  const drift = checkScriptApprovals();
  if (drift.length > 0) process.stderr.write(formatApprovalWarning(drift));
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main();
