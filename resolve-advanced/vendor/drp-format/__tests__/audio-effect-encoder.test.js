/**
 * audio-effect-encoder — clip audio volume (Tier-2 ducking, issue #14).
 *
 * Ground truth: two samples captured by export-diff on Resolve Studio 21.0.2.4
 * (level set by hand in the Inspector, diffed against a unity baseline). The blob
 * is a fixed protobuf tree whose only variable is the dB value as an LE float64.
 * A ROUND dB (12.0) stores byte-exact; a non-round typed value carries Resolve's
 * own ~6-ULP rounding (3.7 -> 3.700000000000003), so we assert exactness only for
 * the round sample and round-trip equality otherwise.
 */

const test = require('node:test');
const assert = require('node:assert/strict');

const {
  encodeAudioVolumeEffectFiltersBA,
  decodeAudioVolumeEffectFiltersBA,
  enableEffectFiltersFlag,
} = require('../audio-effect-encoder');

// Captured live on Resolve 21.0.2.4.
const EFF_12DB = '000000020000001e800a1b087c4a0f085f1a0b0a091100000000000028404a004a004a004a00';
const FB_UNITY = '000000020000000c800a070a05a80180a3057804';
const FB_FLAGGED = '000000020000000e800a090a05a80180a30520017804';

test('encodes +12.0 dB byte-exact to the captured Resolve blob', () => {
  assert.equal(encodeAudioVolumeEffectFiltersBA(12.0), EFF_12DB);
});

test('a round dB value stores exactly; decode recovers it', () => {
  for (const db of [12.0, -12.0, -6.0, 0.0, -20.0, -3.0]) {
    const hex = encodeAudioVolumeEffectFiltersBA(db);
    assert.equal(decodeAudioVolumeEffectFiltersBA(hex), db, `round-trip ${db} dB`);
  }
});

test('decode recovers a non-round level (canonical nearest double)', () => {
  const hex = encodeAudioVolumeEffectFiltersBA(-14.5);
  assert.equal(decodeAudioVolumeEffectFiltersBA(hex), -14.5);
});

test('the blob differs from the captured sample ONLY in the 8 payload bytes', () => {
  // Same fixed prefix + suffix regardless of level — proves the structure is stable.
  const a = encodeAudioVolumeEffectFiltersBA(-12.0);
  const b = encodeAudioVolumeEffectFiltersBA(-3.0);
  const prefix = EFF_12DB.slice(0, 44); // header + proto down to the f2 tag
  const suffix = EFF_12DB.slice(44 + 16); // the four empty f9 entries
  assert.ok(a.startsWith(prefix) && a.endsWith(suffix));
  assert.ok(b.startsWith(prefix) && b.endsWith(suffix));
  assert.notEqual(a, b);
});

test('rejects non-finite levels', () => {
  assert.throws(() => encodeAudioVolumeEffectFiltersBA(NaN), TypeError);
  assert.throws(() => encodeAudioVolumeEffectFiltersBA('x'), TypeError);
});

test('decode returns null for an empty EffectFiltersBA', () => {
  assert.equal(decodeAudioVolumeEffectFiltersBA(''), null);
  assert.equal(decodeAudioVolumeEffectFiltersBA(null), null);
});

test('enableEffectFiltersFlag inserts f4=1 byte-exact and is idempotent', () => {
  assert.equal(enableEffectFiltersFlag(FB_UNITY), FB_FLAGGED);
  assert.equal(enableEffectFiltersFlag(FB_FLAGGED), FB_FLAGGED); // already flagged
});

test('enableEffectFiltersFlag handles the zstd-compressed (0x81) FieldsBlob form', () => {
  // Resolve 21 also exports FieldsBlob as 0x81 + zstd frame (seen live on
  // 21.0.2.4, #30 sweep). Build one by compressing FB_UNITY's raw protobuf;
  // the flagged result must decompress to exactly the raw-form output.
  const zlib = require('node:zlib');
  const raw = Buffer.from(FB_UNITY, 'hex').subarray(9); // proto after hdr+0x80
  const frame = zlib.zstdCompressSync(raw);
  const payload = Buffer.concat([Buffer.from([0x81]), frame]);
  const hdr = Buffer.alloc(8);
  hdr.writeUInt32BE(2, 0);
  hdr.writeUInt32BE(payload.length, 4);
  const compressedBlob = Buffer.concat([hdr, payload]).toString('hex');
  assert.equal(enableEffectFiltersFlag(compressedBlob), FB_FLAGGED);
});
