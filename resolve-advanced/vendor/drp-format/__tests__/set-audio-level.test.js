/**
 * set-audio-level — author a clip audio level into a real `.drt` (Tier-2 ducking,
 * issue #14). The fixture `audio-unity-baseline.drt` is a REAL Resolve 21.0.2.4
 * export (V1 pic / A2 bed at unity), so the strongest assertion is byte-level:
 * writing +12.0 dB into it must reproduce exactly the audio-clip blobs Resolve
 * itself produced when the same edit was made by hand (captured in the encoder
 * ground truth).
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const JSZip = require('jszip');

const { setAudioLevel } = require('../set-audio-level');

const FIXTURE = path.join(__dirname, 'fixtures', 'audio-unity-baseline.drt');
const EFF_12DB = '000000020000001e800a1b087c4a0f085f1a0b0a091100000000000028404a004a004a004a00';
const FB_FLAGGED = '000000020000000e800a090a05a80180a30520017804';

async function audioClipOf(buffer) {
  const zip = await JSZip.loadAsync(buffer);
  const name = Object.keys(zip.files).find((n) => /SeqContainer.*\.xml$/.test(n));
  const xml = await zip.file(name).async('string');
  return (xml.match(/<Sm2TiAudioClip[\s\S]*?<\/Sm2TiAudioClip>/) || [])[0];
}

test('+12.0 dB into the unity baseline reproduces Resolve\'s exact blobs', async () => {
  const res = await setAudioLevel(FIXTURE, { track: 2, volumeDb: 12.0 });
  assert.equal(res.track, 2);
  assert.equal(res.effectFiltersHex, EFF_12DB);
  const clip = await audioClipOf(res.buffer);
  assert.match(clip, new RegExp(`<EffectFiltersBA>${EFF_12DB}</EffectFiltersBA>`));
  assert.match(clip, new RegExp(`<FieldsBlob>${FB_FLAGGED}</FieldsBlob>`));
});

test('ducking (negative dB) writes a decodable level and flags FieldsBlob', async () => {
  const { decodeAudioVolumeEffectFiltersBA } = require('../audio-effect-encoder');
  const res = await setAudioLevel(FIXTURE, { track: 2, volumeDb: -12.0 });
  assert.equal(decodeAudioVolumeEffectFiltersBA(res.effectFiltersHex), -12.0);
  const clip = await audioClipOf(res.buffer);
  assert.equal(decodeAudioVolumeEffectFiltersBA(
    clip.match(/<EffectFiltersBA>([0-9a-f]*)<\/EffectFiltersBA>/)[1]), -12.0);
  assert.match(clip, /<FieldsBlob>000000020000000e[0-9a-f]*<\/FieldsBlob>/);
});

test('the output stays a valid .drt zip with the media-link entries intact', async () => {
  const fileEntries = (zip) => Object.values(zip.files).filter((f) => !f.dir).map((f) => f.name).sort();
  const before = await JSZip.loadAsync(fs.readFileSync(FIXTURE));
  const res = await setAudioLevel(FIXTURE, { track: 2, volumeDb: -9.0 });
  const after = await JSZip.loadAsync(res.buffer);
  // Same file members — export-then-modify must not add/drop entries (dir markers aside).
  assert.deepEqual(fileEntries(after), fileEntries(before));
});

test('rejects a track that does not exist', async () => {
  await assert.rejects(() => setAudioLevel(FIXTURE, { track: 9, volumeDb: -6 }), /does not exist/);
});

test('rejects a clip index out of range', async () => {
  await assert.rejects(
    () => setAudioLevel(FIXTURE, { track: 2, volumeDb: -6, clipIndex: 5 }),
    /out of range/,
  );
});

test('validates arguments', async () => {
  await assert.rejects(() => setAudioLevel(FIXTURE, { track: 0, volumeDb: -6 }), TypeError);
  await assert.rejects(() => setAudioLevel(FIXTURE, { track: 2, volumeDb: NaN }), TypeError);
  await assert.rejects(() => setAudioLevel(FIXTURE, { track: 2, volumeDb: -6, clipIndex: -1 }), TypeError);
});
