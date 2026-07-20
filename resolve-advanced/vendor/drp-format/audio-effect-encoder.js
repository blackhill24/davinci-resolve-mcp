/**
 * audio-effect-encoder — encode/decode a clip AUDIO VOLUME level into a Resolve
 * timeline `.drt`, offline. This is the Tier-2 ducking path (issue #14): the
 * scripting API cannot set clip volume (SetProperty('Volume') is a no-op and
 * GetProperty('Volume') returns null), but a clip level DOES round-trip through
 * an exported `.drt`, so the level can be authored here with no Resolve running.
 *
 * Ground truth — reverse-engineered by export-diff on Resolve Studio 21.0.2.4
 * (level set by hand in the Inspector, two samples +12.0 dB and +3.7 dB diffed
 * against a unity baseline; recorded in src/utils/api_truth.py, issue=14):
 *
 *   At unity (0 dB) the audio clip's <EffectFiltersBA/> is EMPTY.
 *   A non-unity level writes a fixed audio-volume effect-filter blob whose only
 *   variable is the level, stored as the dB value in an IEEE-754 little-endian
 *   float64. Protobuf tree (all field numbers/constants verified byte-exact):
 *     f1 {
 *       f1 = 124                 # filter kind
 *       f9 { f1 = 95, f3 { f1 { f2 = <dB as fixed64 double> } } }
 *       f9{} f9{} f9{} f9{}      # four empty channel entries
 *     }
 *   wrapped in the standard EffectFiltersBA header: version 0x00000002 (BE) +
 *   payload size (BE) + 0x80 uncompressed marker.
 *
 *   Resolve also flips a flag in the clip's own <FieldsBlob>: it inserts
 *   f4 = 1 ("has effect filters") into the blob's outer f1 group. The rest of
 *   the FieldsBlob (record position etc.) is clip-specific and preserved.
 *
 * NOTE: effect-encoder.js reports Resolve 21 stopped writing <EffectFiltersBA>
 * for VIDEO transform/ResolveFX on DRP export. That does not apply to audio clip
 * volume — verified live: it is still written on .drt export, as encoded here.
 *
 * PAN — reverse-engineered the same way (issue #22, 3.2.1: live sample, clip Pan
 * set to 50 by hand in the Edit-page Inspector Audio panel on Resolve Studio 21,
 * diffed against a unity/center baseline). Byte-exact same template as volume
 * (outer f1 group -> f9 { f1 = paramId, f3 { f1 { f2 = fixed64 double } } }), but:
 *   - filter kind (outer f1) = 144, not 124
 *   - param id (f9's f1) = 96, not 95
 *   - only ONE f9 present — no four trailing empty f9 channel placeholders
 *   - the double is the RAW pan value (e.g. 50.0), not a dB-scaled level
 * The FieldsBlob "has effect filters" flag (f4 = 1) is set identically to volume
 * — enableEffectFiltersFlag is reused unchanged.
 *
 * @module drp-format/audio-effect-encoder
 */

const {
  encodeVarint,
  decodeVarint,
  buildDoubleField,
  buildVarintField,
  buildNestedField,
} = require('./effect-encoder');

// The full EffectFiltersBA header: 4-byte version (BE) + 4-byte payload size (BE).
const EFFECT_VERSION = 2;
const UNCOMPRESSED_MARKER = 0x80;
// Constants captured from the two live samples (identical across them).
const AUDIO_FILTER_KIND = 124; // f1 in the outer group
const AUDIO_PARAM_ID = 95; // f1 inside f9
// Pan constants, captured from one live sample (pan=50, see module docstring).
const PAN_FILTER_KIND = 144;
const PAN_PARAM_ID = 96;

/**
 * Encode a clip audio level (in dB) into an EffectFiltersBA hex blob.
 * @param {number} volumeDb - level in dB (0 = unity; note unity is normally left
 *   as an empty <EffectFiltersBA/>, so callers duck by passing a negative dB).
 * @returns {string} lowercase hex string for the <EffectFiltersBA> element body.
 */
function encodeAudioVolumeEffectFiltersBA(volumeDb) {
  if (typeof volumeDb !== 'number' || !Number.isFinite(volumeDb)) {
    throw new TypeError('encodeAudioVolumeEffectFiltersBA: volumeDb must be a finite number');
  }
  const level = buildDoubleField(2, volumeDb); // f2 fixed64 = the dB value
  const f3 = buildNestedField(3, buildNestedField(1, level)); // f3 { f1 { f2 } }
  const f9 = buildNestedField(9, Buffer.concat([buildVarintField(1, AUDIO_PARAM_ID), f3]));
  const empty9 = buildNestedField(9, Buffer.alloc(0));
  const outer = buildNestedField(1, Buffer.concat([
    buildVarintField(1, AUDIO_FILTER_KIND),
    f9,
    empty9, empty9, empty9, empty9,
  ]));
  const payload = Buffer.concat([Buffer.from([UNCOMPRESSED_MARKER]), outer]);
  const header = Buffer.alloc(8);
  header.writeUInt32BE(EFFECT_VERSION, 0);
  header.writeUInt32BE(payload.length, 4);
  return Buffer.concat([header, payload]).toString('hex');
}

/**
 * Decode the dB level out of an audio-volume EffectFiltersBA blob.
 * @param {string|Buffer} blob - hex string or Buffer of the element body.
 * @returns {number|null} the level in dB, or null if the blob is empty/unrecognized.
 */
function decodeAudioVolumeEffectFiltersBA(blob) {
  if (blob == null) return null;
  const buf = Buffer.isBuffer(blob) ? blob : Buffer.from(String(blob).trim(), 'hex');
  if (buf.length < 9) return null; // empty / header-only
  // Descend to f3 { f1 { f2 fixed64 } }: the double is the 8 bytes right after the
  // `0a 09 11` (f1 len 9 -> f2 fixed64 tag) marker unique to this template.
  const needle = Buffer.from([0x0a, 0x09, 0x11]);
  const at = buf.indexOf(needle);
  if (at < 0 || at + 3 + 8 > buf.length) return null;
  return buf.readDoubleLE(at + 3);
}

/**
 * Encode a clip audio pan into an EffectFiltersBA hex blob.
 * @param {number} panValue - pan position (0 = center; negative left, positive
 *   right — matches the Inspector Audio panel's Pan field, e.g. -100..100).
 * @returns {string} lowercase hex string for the <EffectFiltersBA> element body.
 */
function encodeAudioPanEffectFiltersBA(panValue) {
  if (typeof panValue !== 'number' || !Number.isFinite(panValue)) {
    throw new TypeError('encodeAudioPanEffectFiltersBA: panValue must be a finite number');
  }
  const value = buildDoubleField(2, panValue); // f2 fixed64 = the pan value
  const f3 = buildNestedField(3, buildNestedField(1, value)); // f3 { f1 { f2 } }
  const f9 = buildNestedField(9, Buffer.concat([buildVarintField(1, PAN_PARAM_ID), f3]));
  // Unlike volume, only one f9 is present — no empty channel placeholders.
  const outer = buildNestedField(1, Buffer.concat([
    buildVarintField(1, PAN_FILTER_KIND),
    f9,
  ]));
  const payload = Buffer.concat([Buffer.from([UNCOMPRESSED_MARKER]), outer]);
  const header = Buffer.alloc(8);
  header.writeUInt32BE(EFFECT_VERSION, 0);
  header.writeUInt32BE(payload.length, 4);
  return Buffer.concat([header, payload]).toString('hex');
}

/**
 * Decode the pan value out of a pan EffectFiltersBA blob.
 * @param {string|Buffer} blob - hex string or Buffer of the element body.
 * @returns {number|null} the pan value, or null if the blob is empty/unrecognized.
 */
function decodeAudioPanEffectFiltersBA(blob) {
  // Same double-field template as volume — the needle-search decode is identical.
  return decodeAudioVolumeEffectFiltersBA(blob);
}

// Walk the top-level protobuf fields of `buf`, returning true if any has the
// given field number. Only handles the wire types these blobs use (0, 1, 2).
function topLevelHasField(buf, fieldNumber) {
  let i = 0;
  while (i < buf.length) {
    const tag = decodeVarint(buf, i);
    const fn = tag.value >> 3;
    const wt = tag.value & 0x7;
    i += tag.bytesRead;
    if (fn === fieldNumber) return true;
    if (wt === 0) { const v = decodeVarint(buf, i); i += v.bytesRead; }
    else if (wt === 1) { i += 8; }
    else if (wt === 5) { i += 4; }
    else if (wt === 2) { const len = decodeVarint(buf, i); i += len.bytesRead + len.value; }
    else return false; // unknown wire type — bail rather than mis-read
  }
  return false;
}

/**
 * Set the "has effect filters" flag (f4 = 1) in a clip's FieldsBlob, so Resolve
 * parses the EffectFiltersBA we just wrote. Idempotent: if the flag is already
 * present the blob is returned unchanged. Preserves all clip-specific content.
 * @param {string|Buffer} blob - hex string or Buffer of the <FieldsBlob> body.
 * @returns {string} lowercase hex string.
 */
function enableEffectFiltersFlag(blob) {
  const buf = Buffer.isBuffer(blob) ? blob : Buffer.from(String(blob).trim(), 'hex');
  if (buf.length < 9) throw new Error('enableEffectFiltersFlag: FieldsBlob too short');
  const version = buf.readUInt32BE(0);
  const marker = buf[8];
  const proto = buf.subarray(9);
  if (proto[0] !== 0x0a) {
    throw new Error('enableEffectFiltersFlag: unexpected FieldsBlob shape (no outer f1 group)');
  }
  const { value: outerLen, bytesRead: lenBytes } = decodeVarint(proto, 1);
  const contentStart = 1 + lenBytes;
  const content = proto.subarray(contentStart, contentStart + outerLen);
  if (topLevelHasField(content, 4)) {
    return buf.toString('hex'); // already flagged — idempotent
  }
  const newContent = Buffer.concat([content, buildVarintField(4, 1)]); // append f4 = 1
  const newOuter = Buffer.concat([
    Buffer.from([0x0a]), encodeVarint(newContent.length), newContent,
  ]);
  const rest = proto.subarray(contentStart + outerLen); // trailing top-level fields (e.g. f15)
  const newPayload = Buffer.concat([Buffer.from([marker]), newOuter, rest]);
  const header = Buffer.alloc(8);
  header.writeUInt32BE(version, 0);
  header.writeUInt32BE(newPayload.length, 4);
  return Buffer.concat([header, newPayload]).toString('hex');
}

module.exports = {
  encodeAudioVolumeEffectFiltersBA,
  decodeAudioVolumeEffectFiltersBA,
  encodeAudioPanEffectFiltersBA,
  decodeAudioPanEffectFiltersBA,
  enableEffectFiltersFlag,
  AUDIO_FILTER_KIND,
  AUDIO_PARAM_ID,
  PAN_FILTER_KIND,
  PAN_PARAM_ID,
};
