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

export function formatReport(problems) {
  const lines = ['', '  Native dependency preflight FAILED — this is a stale install, not a code regression.', ''];
  for (const p of problems) {
    lines.push(`  • ${p.name} — ${p.state === 'abi' ? 'built for a different Node ABI' : 'failed to load'}`);
    lines.push(`      ${p.message}`);
    lines.push(`      enables: ${p.enables}`);
    lines.push(`      fix:     npm rebuild ${p.name}`);
    lines.push('');
  }
  lines.push(`  Running Node ${process.version} (ABI ${process.versions.modules}).`);
  lines.push('  Set SKIP_NATIVE_PREFLIGHT=1 to run the suite anyway.');
  lines.push('');
  return lines.join('\n');
}

function main() {
  if (process.env.SKIP_NATIVE_PREFLIGHT === '1') return;
  const problems = checkNativeModules();
  if (problems.length === 0) return;
  process.stderr.write(formatReport(problems));
  process.exit(1);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) main();
