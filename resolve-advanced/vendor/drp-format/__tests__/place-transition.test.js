// Unit: insert a cross-dissolve between two abutting clips. Live Resolve round-trip is the
// acceptance gate. Template captured from a GUI-authored Cross Dissolve in Resolve 21.

const test = require('node:test');
const assert = require('node:assert');
const { placeTransition } = require('../place-transition');

// Two abutting Sm2TiVideoClips: c0 [0,100), c1 [100,200) — cut at 100.
async function synth2() {
  const JSZip = require('jszip');
  const clip = (i) => `<Element><Sm2TiVideoClip DbId="c${i}"><FieldsBlob/><Name>c${i}</Name><Start>${i * 100}</Start><Duration>100</Duration><In/></Sm2TiVideoClip></Element>`;
  const track = `<Element><Sm2TiTrack DbId="t"><FieldsBlob/><Type>0</Type><SubType>0</SubType><Flags>0</Flags><Sequence>s</Sequence><Items>${clip(0)}${clip(1)}</Items><FusionCompHolderItems/><UserDefinedName/><LayersVec/></Sm2TiTrack></Element>`;
  const seq = `<?xml version="1.0"?>\n<Sm2SequenceContainer DbId="s1"><FieldsBlob/><VideoTrackVec>${track}</VideoTrackVec><AudioTrackVec/></Sm2SequenceContainer>`;
  const z = new JSZip(); z.file('SeqContainer/s1.xml', seq); return z.generateAsync({ type: 'nodebuffer' });
}

async function vtv(buf) {
  const JSZip = require('jszip');
  const zip = await JSZip.loadAsync(buf);
  const x = await zip.file('SeqContainer/s1.xml').async('string');
  return x.match(/<VideoTrackVec>([\s\S]*?)<\/VideoTrackVec>/)[1];
}

test('placeTransition inserts an Sm2TiTransition between the two clips, centered on the cut', async () => {
  const res = await placeTransition(await synth2(), { track: 1, atFrame: 100, durationFrames: 24 });
  assert.strictEqual(res.start, 88, 'centered: 100 - 24/2');
  assert.strictEqual(res.durationFrames, 24);
  assert.ok(res.transitionDbId, 'fresh DbId assigned');

  const body = await vtv(res.buffer);
  assert.ok(/<Sm2TiTransition\b/.test(body), 'transition present');
  assert.ok(/<PrettyType>Cross Dissolve<\/PrettyType>/.test(body), 'is a Cross Dissolve');
  assert.ok(/<Start>88<\/Start>/.test(body) && /<Duration>24<\/Duration>/.test(body), 'start/duration set');
  // ordering: clip c0 ... transition ... clip c1
  const order = [...body.matchAll(/<Sm2Ti(VideoClip|Transition)\b[^>]*DbId="([^"]+)"/g)].map((m) => m[1]);
  assert.deepStrictEqual(order, ['VideoClip', 'Transition', 'VideoClip'], 'transition sits between the clips');
});

test('placeTransition errors when no abutting boundary at atFrame', async () => {
  const buf = await synth2();
  await assert.rejects(() => placeTransition(buf, { track: 1, atFrame: 9999 }), /no abutting clip boundary/);
});

test('placeTransition validates args', async () => {
  const buf = await synth2();
  await assert.rejects(() => placeTransition(buf, { track: 1, atFrame: 100, durationFrames: 1 }), /durationFrames/);
  await assert.rejects(() => placeTransition(buf, { track: 1, atFrame: 100, trackType: 'audio' }), /only video/);
});

test('placeTransition accepts an explicit type= "cross_dissolve" (default, still works named)', async () => {
  const res = await placeTransition(await synth2(), { track: 1, atFrame: 100, durationFrames: 24, type: 'cross_dissolve' });
  assert.strictEqual(res.type, 'cross_dissolve');
  const body = await vtv(res.buffer);
  assert.ok(/<PrettyType>Cross Dissolve<\/PrettyType>/.test(body));
});

test('placeTransition type="dip_to_colour" inserts a Dip To Color Dissolve (template captured Resolve 21, #208)', async () => {
  const res = await placeTransition(await synth2(), { track: 1, atFrame: 100, durationFrames: 24, type: 'dip_to_colour' });
  assert.strictEqual(res.type, 'dip_to_colour');
  assert.strictEqual(res.start, 88, 'centered: 100 - 24/2');
  assert.ok(res.transitionDbId, 'fresh DbId assigned');

  const body = await vtv(res.buffer);
  assert.ok(/<PrettyType>Dip To Color Dissolve<\/PrettyType>/.test(body), 'is a Dip To Color Dissolve');
  assert.ok(/<Start>88<\/Start>/.test(body) && /<Duration>24<\/Duration>/.test(body), 'start/duration set');
  const order = [...body.matchAll(/<Sm2Ti(VideoClip|Transition)\b[^>]*DbId="([^"]+)"/g)].map((m) => m[1]);
  assert.deepStrictEqual(order, ['VideoClip', 'Transition', 'VideoClip'], 'transition sits between the clips');
});

test('placeTransition rejects an unregistered type rather than silently substituting', async () => {
  const buf = await synth2();
  await assert.rejects(
    () => placeTransition(buf, { track: 1, atFrame: 100, type: 'smooth_cut' }),
    /unknown type "smooth_cut"/,
  );
});
