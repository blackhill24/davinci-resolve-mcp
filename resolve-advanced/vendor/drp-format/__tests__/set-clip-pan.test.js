/**
 * set-clip-pan — author a clip audio pan into a real `.drt` (issue #22, 3.2.1).
 * EFF_50 is the byte-exact ground truth captured live (Resolve Studio 21.0.2.4,
 * pan set to 50 by hand in the Edit-page Inspector Audio panel, export-diffed
 * against a unity/center baseline via tests/live_pan_probe.py).
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const JSZip = require('jszip');

const { setClipPan } = require('../set-audio-level');

const FIXTURE = path.join(__dirname, 'fixtures', 'audio-unity-baseline.drt');
const EFF_50 = '0000000200000017800a140890014a0f08601a0b0a09110000000000004940';
const FB_FLAGGED = '000000020000000e800a090a05a80180a30520017804';

async function audioClipOf(buffer) {
  const zip = await JSZip.loadAsync(buffer);
  const name = Object.keys(zip.files).find((n) => /SeqContainer.*\.xml$/.test(n));
  const xml = await zip.file(name).async('string');
  return (xml.match(/<Sm2TiAudioClip[\s\S]*?<\/Sm2TiAudioClip>/) || [])[0];
}

test('pan=50 into the unity baseline reproduces Resolve\'s exact blobs', async () => {
  const res = await setClipPan(FIXTURE, { track: 2, panValue: 50 });
  assert.equal(res.track, 2);
  assert.equal(res.effectFiltersHex, EFF_50);
  const clip = await audioClipOf(res.buffer);
  assert.match(clip, new RegExp(`<EffectFiltersBA>${EFF_50}</EffectFiltersBA>`));
  assert.match(clip, new RegExp(`<FieldsBlob>${FB_FLAGGED}</FieldsBlob>`));
});

test('negative pan (left) writes a decodable value and flags FieldsBlob', async () => {
  const { decodeAudioPanEffectFiltersBA } = require('../audio-effect-encoder');
  const res = await setClipPan(FIXTURE, { track: 2, panValue: -30 });
  assert.equal(decodeAudioPanEffectFiltersBA(res.effectFiltersHex), -30);
  const clip = await audioClipOf(res.buffer);
  assert.equal(decodeAudioPanEffectFiltersBA(
    clip.match(/<EffectFiltersBA>([0-9a-f]*)<\/EffectFiltersBA>/)[1]), -30);
  assert.match(clip, /<FieldsBlob>000000020000000e[0-9a-f]*<\/FieldsBlob>/);
});

test('the output stays a valid .drt zip with the media-link entries intact', async () => {
  const fs = require('node:fs');
  const fileEntries = (zip) => Object.values(zip.files).filter((f) => !f.dir).map((f) => f.name).sort();
  const before = await JSZip.loadAsync(fs.readFileSync(FIXTURE));
  const res = await setClipPan(FIXTURE, { track: 2, panValue: 0 });
  const after = await JSZip.loadAsync(res.buffer);
  assert.deepEqual(fileEntries(after), fileEntries(before));
});

test('rejects a track that does not exist', async () => {
  await assert.rejects(() => setClipPan(FIXTURE, { track: 9, panValue: 10 }), /does not exist/);
});

test('rejects a clip index out of range', async () => {
  await assert.rejects(
    () => setClipPan(FIXTURE, { track: 2, panValue: 10, clipIndex: 5 }),
    /out of range/,
  );
});

test('validates arguments', async () => {
  await assert.rejects(() => setClipPan(FIXTURE, { track: 0, panValue: 10 }), TypeError);
  await assert.rejects(() => setClipPan(FIXTURE, { track: 2, panValue: NaN }), TypeError);
  await assert.rejects(() => setClipPan(FIXTURE, { track: 2, panValue: 10, clipIndex: -1 }), TypeError);
});
