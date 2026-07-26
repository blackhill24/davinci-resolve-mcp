#!/usr/bin/env node
/**
 * Mutation gate for the vendored .drp / .drt / .drx codecs (#121 task 5).
 *
 * The Python side got this treatment in #119 (scripts/mutation_gate.py). The Node
 * side never did — and it is arguably the more dangerous half: these codecs write
 * BINARY project files to disk. A corruption bug here ships silently, and unlike
 * the Python side there is no live suite behind it to catch what the unit tests
 * miss. The question this script answers is the one #121 asks everywhere: if this
 * were broken, would anything fail?
 *
 * Each mutation is one of the three shapes #121 named — flip a byte order, drop a
 * field, truncate a section — applied to real codec source, after which the suite
 * must fail by at least `minFailures` tests.
 *
 * Usage
 * -----
 *     node scripts/mutation-gate.mjs               # all mutations
 *     node scripts/mutation-gate.mjs --list
 *     node scripts/mutation-gate.mjs --only drp_wire_byte_order
 *
 * Exit codes: 0 all killed, 1 at least one survived (or was too weakly killed),
 * 2 the harness itself could not run.
 *
 * Source files are edited IN PLACE and restored in a finally block, plus on
 * SIGINT/SIGTERM/uncaughtException. The run ends by verifying every file is back
 * to its original bytes and refuses to report success otherwise — a mutation
 * gate that left the tree mutated would be worse than no gate.
 *
 * Floors are floors. Never lower one to make this pass; a falling kill count is
 * the signal this script exists to emit.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

const MUTATIONS = {
  // ── byte order ────────────────────────────────────────────────────────────
  drp_wire_byte_order: {
    why: 'EffectFiltersBA envelope header read little-endian instead of big-endian. '
       + 'Resolve writes it BE; an LE read yields a nonsense payload length, so every '
       + 'grade/marker/audio-effect blob round-trip silently produces garbage.',
    file: 'vendor/drp-format/protobuf-wire.js',
    find: "  const hdr = b.readUInt32BE(0);\n  const len = b.readUInt32BE(4);",
    replace: "  const hdr = b.readUInt32LE(0);\n  const len = b.readUInt32LE(4);",
    minFailures: 1,
  },
  drx_float_byte_order: {
    why: 'DRX float32 parameters written big-endian. Every colour value in every '
       + 'generated .drx would be a different number — the file still parses, so only '
       + 'a value-fidelity assertion catches it.',
    file: 'vendor/drx-codec/drx-generator.js',
    find: "  const buf = Buffer.alloc(4);\n  buf.writeFloatLE(value, 0);\n  return buf;",
    replace: "  const buf = Buffer.alloc(4);\n  buf.writeFloatBE(value, 0);\n  return buf;",
    minFailures: 1,
  },
  // ── dropped field ─────────────────────────────────────────────────────────
  drp_wire_drop_field: {
    why: 'The protobuf encoder drops the first field of every message. Round-trip '
       + 'equality is the only thing that notices; a "did not throw" assertion does not.',
    file: 'vendor/drp-format/protobuf-wire.js',
    find: '  for (const f of fields) {',
    replace: '  for (const f of fields.slice(1)) {',
    minFailures: 1,
  },
  drx_drop_length_prefix: {
    why: 'Length-delimited protobuf fields written without their length prefix — the '
       + 'classic "one field short" corruption, which yields a file that is still '
       + 'byte-shaped but structurally wrong.',
    file: 'vendor/drx-codec/drx-generator.js',
    find: '  return Buffer.concat([encodeVarint(tag), encodeVarint(data.length), data]);',
    replace: '  return Buffer.concat([encodeVarint(tag), data]);',
    minFailures: 1,
  },
  // ── truncated section ─────────────────────────────────────────────────────
  drp_truncate_payload: {
    why: 'The EffectFilters envelope declares its true payload length but emits a '
       + 'truncated payload — the shape a partial write or an off-by-one buffer alloc '
       + 'produces, and the one most likely to corrupt a real .drp on disk.',
    file: 'vendor/drp-format/protobuf-wire.js',
    find: '  return Buffer.concat([head, payload]);',
    replace: '  return Buffer.concat([head, payload.slice(0, Math.max(0, payload.length - 1))]);',
    minFailures: 1,
  },
  drt_truncate_varint: {
    why: 'The DRX varint encoder emits only the final byte of a multi-byte value, '
       + 'truncating every id/tag over 127. Small fixtures survive; anything with a '
       + 'realistic parameter id does not.',
    file: 'vendor/drx-codec/drx-generator.js',
    find: "  const bytes = [];\n  while (value > 0x7f) {",
    replace: "  const bytes = [];\n  while (false && value > 0x7f) {",
    minFailures: 1,
  },
};

// node --test's summary line. The spec reporter prefixes it with `ℹ`, the TAP
// reporter with `#`; anchor to line start so a test *named* "fail ..." cannot
// be mistaken for the count.
const SUMMARY_RE = /^\s*(?:ℹ|#)\s*fail\s+(\d+)\s*$/m;

function runSuite() {
  const proc = spawnSync(
    process.execPath,
    ['--test',
     'vendor/drp-format/__tests__/*.test.js',
     'vendor/drt-format/__tests__/*.test.js',
     'vendor/drx-codec/__tests__/*.test.js',
     'test/*.test.mjs'],
    { cwd: ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 },
  );
  const output = `${proc.stdout || ''}${proc.stderr || ''}`;
  const match = output.match(SUMMARY_RE);
  return { code: proc.status, failures: match ? Number(match[1]) : 0, output };
}

function main(argv) {
  const list = argv.includes('--list');
  const only = [];
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--only') only.push(argv[i + 1]);
  }

  if (list) {
    for (const [name, spec] of Object.entries(MUTATIONS)) {
      console.log(`${name}\n    ${spec.why}\n    file: ${spec.file}  floor: ${spec.minFailures}`);
    }
    return 0;
  }

  const selected = only.length ? only : Object.keys(MUTATIONS);
  const unknown = selected.filter((n) => !(n in MUTATIONS));
  if (unknown.length) {
    console.error(`unknown mutation(s): ${unknown.join(', ')}`);
    return 2;
  }

  // Snapshot every file we might touch, and guarantee restoration.
  const originals = new Map();
  for (const name of selected) {
    const abs = join(ROOT, MUTATIONS[name].file);
    if (!originals.has(abs)) originals.set(abs, readFileSync(abs));
  }
  const restoreAll = () => {
    for (const [abs, bytes] of originals) writeFileSync(abs, bytes);
  };
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.on(signal, () => { restoreAll(); process.exit(2); });
  }
  process.on('uncaughtException', (err) => { restoreAll(); throw err; });

  const survivors = [];
  try {
    for (const name of selected) {
      const spec = MUTATIONS[name];
      const abs = join(ROOT, spec.file);
      console.log(`=== ${name} ===`);
      console.log(`    ${spec.why}`);

      const source = originals.get(abs).toString('utf8');
      const occurrences = source.split(spec.find).length - 1;
      if (occurrences !== 1) {
        survivors.push(`${name}: anchor matched ${occurrences} times in ${spec.file} — `
          + 'the code moved; update the mutation rather than deleting it');
        console.log(`    FAIL: anchor matched ${occurrences} times`);
        continue;
      }

      writeFileSync(abs, source.replace(spec.find, spec.replace));
      const { code, failures } = runSuite();
      writeFileSync(abs, originals.get(abs));

      if (code === 0) {
        survivors.push(`${name}: SURVIVED — suite stayed green`);
        console.log('    FAIL: SURVIVED — suite stayed green');
      } else if (failures < spec.minFailures) {
        survivors.push(`${name}: too weakly killed — ${failures} failed, floor ${spec.minFailures}`);
        console.log(`    FAIL: too weakly killed — ${failures} failed`);
      } else {
        console.log(`    OK: killed by ${failures} failing tests (floor ${spec.minFailures})`);
      }
    }
  } finally {
    restoreAll();
  }

  // Never claim success on a tree we might have left mutated.
  for (const [abs, bytes] of originals) {
    if (!readFileSync(abs).equals(bytes)) {
      console.error(`\nHARNESS ERROR — ${abs} was not restored. Check `
        + 'it out from git before trusting anything below.');
      return 2;
    }
  }

  console.log();
  if (survivors.length) {
    console.log(`MUTATION GATE FAILED — ${survivors.length}/${selected.length} survived:`);
    for (const line of survivors) console.log(`  - ${line}`);
    console.log('\nThe codec suite can no longer see a corruption it should. Add coverage; '
      + 'do not lower the floor.');
    return 1;
  }
  console.log(`MUTATION GATE PASSED — ${selected.length}/${selected.length} mutations killed.`);
  return 0;
}

process.exit(main(process.argv.slice(2)));
