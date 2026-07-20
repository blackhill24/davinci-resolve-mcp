// Unit: clip retime (MediaTimemapBA swap) + single-op slip. Structural verification
// against synthetic timelines with real captured timemap blobs; the live Resolve
// round-trip is the acceptance gate (tests/live_retime_probe.py).

const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');
const fs = require('node:fs');
const { retimeClip, slipClip } = require('../retime-clip');
const { decodeTimemap } = require('../media-timemap');

// Live-captured 50% constant retime (Sm2TimeMap keyed-dict) from a real Resolve 21 export.
const RETIMED_50 = fs.readFileSync(
  path.join(__dirname, 'fixtures', 'retimed-timemap-50pct.hex'), 'utf8').trim();

// Identity timemap for the canary media clip: 4576 frames @29.97 → [02][end,0,end,0,end].
const { identityTimemap } = require('../media-timemap');
const IDENTITY = identityTimemap(4576, 30000 / 1001).toString('hex');

async function synthDrp({ timemapHex = IDENTITY, n = 2, inTag = '<In/>' } = {}) {
  const JSZip = require('jszip');
  const clips = Array.from({ length: n }, (_, i) =>
    `<Element><Sm2TiVideoClip DbId="c${i}"><FieldsBlob/><Name>clip${i}</Name>` +
    `<Start>${86400 + i * 100}</Start><Duration>100</Duration>${inTag}<MediaStartTime>0</MediaStartTime>` +
    `<MediaTimemapBA>${timemapHex}</MediaTimemapBA>` +
    `<MediaFilePath>/x/${i}.mov</MediaFilePath></Sm2TiVideoClip></Element>`).join('');
  const track =
    '<Element><Sm2TiTrack DbId="t1"><FieldsBlob/><Type>0</Type><SubType>0</SubType><Flags>0</Flags>' +
    `<Sequence>seq1</Sequence><Items>${clips}</Items><FusionCompHolderItems/><UserDefinedName/><LayersVec/></Sm2TiTrack></Element>`;
  const seq =
    '<?xml version="1.0" encoding="UTF-8"?>\n<Sm2SequenceContainer DbId="seq-1"><FieldsBlob/>' +
    `<VideoTrackVec>${track}</VideoTrackVec><AudioTrackVec/></Sm2SequenceContainer>`;
  const zip = new JSZip();
  zip.file('SeqContainer/seq-1.xml', seq);
  return zip.generateAsync({ type: 'nodebuffer' });
}

async function clipXmls(buf) {
  const JSZip = require('jszip');
  const zip = await JSZip.loadAsync(buf);
  const xml = await zip.file('SeqContainer/seq-1.xml').async('string');
  return xml.match(/<Element>\s*<Sm2TiVideoClip[\s\S]*?<\/Sm2TiVideoClip>\s*<\/Element>/g) || [];
}
const grab = (clip, tag) => (clip.match(new RegExp(`<${tag}>([^<]*)</${tag}>`)) || [])[1];

test('retimeClip 0.5x: swaps in an Sm2TimeMap blob and doubles Duration', async () => {
  const buf = await synthDrp();
  const res = await retimeClip(buf, { track: 1, clipIndex: 0, speed: 0.5 });
  assert.strictEqual(res.oldSpeed, 1);
  assert.strictEqual(res.oldDuration, 100);
  assert.strictEqual(res.newDuration, 200);
  const clips = await clipXmls(res.buffer);
  assert.strictEqual(grab(clips[0], 'Duration'), '200');
  const d = decodeTimemap(grab(clips[0], 'MediaTimemapBA'));
  assert.strictEqual(d.form, 'retimed');
  assert.strictEqual(d.speed, 0.5);
  // Untouched neighbor keeps its identity map + duration.
  assert.strictEqual(grab(clips[1], 'Duration'), '100');
  assert.strictEqual(decodeTimemap(grab(clips[1], 'MediaTimemapBA')).form, 'identity');
});

test('retimeClip ripple shifts later clips by the duration delta', async () => {
  const buf = await synthDrp();
  const res = await retimeClip(buf, { track: 1, clipIndex: 0, speed: 0.5, ripple: true });
  const clips = await clipXmls(res.buffer);
  // clip1 started at 86500 (>= old end 86500) → shifted by +100.
  assert.strictEqual(grab(clips[1], 'Start'), '86600');
});

test('retimeClip speed 1 on a retimed clip resets to the identity form', async () => {
  const buf = await synthDrp({ timemapHex: RETIMED_50 });
  const res = await retimeClip(buf, { track: 1, clipIndex: 0, speed: 1 });
  assert.ok(Math.abs(res.oldSpeed - 0.5) < 1e-9);
  assert.strictEqual(res.newDuration, 50); // 100 * 0.5 / 1
  const clips = await clipXmls(res.buffer);
  const d = decodeTimemap(grab(clips[0], 'MediaTimemapBA'));
  assert.strictEqual(d.form, 'identity');
  // Source duration survives the reset.
  assert.ok(Math.abs(d.seconds[0] - 152.6525) < 1e-3);
});

test('retimeClip re-retime preserves the Sm2TimeMap UniqueId', async () => {
  const buf = await synthDrp({ timemapHex: RETIMED_50 });
  const res = await retimeClip(buf, { track: 1, clipIndex: 0, speed: 2 });
  const clips = await clipXmls(res.buffer);
  const d = decodeTimemap(grab(clips[0], 'MediaTimemapBA'));
  assert.strictEqual(
    d.entries.find((e) => e.key === 'UniqueId').value,
    '346625ac-b0e3-4768-8418-276483860709');
});

test('retimeClip variable ramp requires explicit newDuration, then encodes the keyframes', async () => {
  const buf = await synthDrp();
  const kfs = [{ recordSec: 10, sourceSec: 5 }, { recordSec: 15, sourceSec: 15 }];
  await assert.rejects(
    () => retimeClip(buf, { track: 1, clipIndex: 0, keyframes: kfs }),
    /requires an explicit newDuration/);
  const res = await retimeClip(buf, { track: 1, clipIndex: 0, keyframes: kfs, newDuration: 360 });
  assert.strictEqual(res.variable, true);
  const clips = await clipXmls(res.buffer);
  const d = decodeTimemap(grab(clips[0], 'MediaTimemapBA'));
  assert.strictEqual(d.variable, true);
  assert.ok(Math.abs(d.segments[0].speed - 0.5) < 1e-9);
  assert.ok(Math.abs(d.segments[1].speed - 2) < 1e-9);
  assert.strictEqual(grab(clips[0], 'Duration'), '360');
});

test('retimeClip rejects speed+keyframes together, and no mode at all', async () => {
  const buf = await synthDrp();
  await assert.rejects(() => retimeClip(buf, { track: 1, speed: 2, keyframes: [{ recordSec: 1, sourceSec: 2 }] }), /exactly one/);
  await assert.rejects(() => retimeClip(buf, { track: 1 }), /speed, keyframes, or newDuration/);
});

test('retimeClip fit-to-fill: newDuration alone derives the constant speed', async () => {
  const buf = await synthDrp();
  const res = await retimeClip(buf, { track: 1, clipIndex: 0, newDuration: 250 });
  assert.ok(Math.abs(res.speed - 0.4) < 1e-9); // 100 frames source segment fills 250
  const clips = await clipXmls(res.buffer);
  assert.strictEqual(grab(clips[0], 'Duration'), '250');
  const d = decodeTimemap(grab(clips[0], 'MediaTimemapBA'));
  assert.ok(Math.abs(d.speed - 0.4) < 1e-9);
});

test('retimeClip errors on a missing timemap unless sourceDurationSec is passed', async () => {
  const buf = await synthDrp({ timemapHex: '' });
  // '' hex → <MediaTimemapBA></MediaTimemapBA> reads as no map.
  await assert.rejects(() => retimeClip(buf, { track: 1, speed: 0.5 }), /no readable MediaTimemapBA/);
  const res = await retimeClip(buf, { track: 1, speed: 0.5, sourceDurationSec: 4 });
  const clips = await clipXmls(res.buffer);
  const d = decodeTimemap(grab(clips[0], 'MediaTimemapBA'));
  assert.strictEqual(d.speed, 0.5);
  assert.ok(Math.abs(d.sourceDurationSec - 4) < 1e-9);
});

test('slipClip advances the in-point without touching Start/Duration', async () => {
  const buf = await synthDrp();
  const res = await slipClip(buf, { track: 1, clipIndex: 0, frames: 12 });
  assert.strictEqual(res.oldIn, 0);
  assert.strictEqual(res.newIn, 12);
  const clips = await clipXmls(res.buffer);
  assert.ok(/<In>12\|/.test(clips[0]));
  assert.strictEqual(grab(clips[0], 'Start'), '86400');
  assert.strictEqual(grab(clips[0], 'Duration'), '100');
});

test('slipClip retreats a positive in-point and round-trips its own encoding', async () => {
  const buf = await synthDrp();
  const once = await slipClip(buf, { track: 1, clipIndex: 0, frames: 20 });
  const twice = await slipClip(once.buffer, { track: 1, clipIndex: 0, frames: -15 });
  assert.strictEqual(twice.oldIn, 20);
  assert.strictEqual(twice.newIn, 5);
});

test('slipClip refuses to retreat past the source head', async () => {
  const buf = await synthDrp();
  await assert.rejects(() => slipClip(buf, { track: 1, clipIndex: 0, frames: -1 }), /cannot retreat/);
});

test('slipClip reads a real-export plain-integer <In>', async () => {
  const buf = await synthDrp({ inTag: '<In>30</In>' });
  const res = await slipClip(buf, { track: 1, clipIndex: 0, frames: -10 });
  assert.strictEqual(res.oldIn, 30);
  assert.strictEqual(res.newIn, 20);
});
