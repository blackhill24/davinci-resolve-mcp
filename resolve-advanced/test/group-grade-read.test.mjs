/** Route A — group-grade read path: DRX->UI scaling + group discovery.
 * Validates scaleParam against DRX-VALUE-SCALING.md and listGroupNames against the
 * project.xml <Sm2Group><Name> shape. The decoder itself is covered by
 * group-grade-calibration.test.mjs / drx-value-fidelity.test.mjs. */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { scaleParam, listGroupNames, groupSegment, decodeXmlEntities, readProjectXml } from '../server/group-grade-read.mjs';

test('scaleParam matches DRX-VALUE-SCALING.md', () => {
  assert.equal(scaleParam('lift.r', 0.5), 0.25); // lift /2
  assert.equal(scaleParam('gamma.master', 1.0), 0.25); // gamma /4
  assert.equal(scaleParam('gain.b', 1.5), 1.5); // gain 1:1
  assert.equal(scaleParam('offset.r', 0.004), 10); // offset *2500
  assert.equal(scaleParam('saturation.primary', 1.5), 150); // sat *100 (%)
  assert.equal(scaleParam('contrast', 1.5), 150); // contrast *100
  assert.equal(scaleParam('logHighlight.r', -0.671), -0.671); // log wheel raw
});

test('listGroupNames pulls Sm2Group names, de-duped', () => {
  const xml = [
    '<Sm2GroupList><Element><Sm2Group><FieldsBlob/><Name>Host</Name></Sm2Group></Element>',
    '<Element><Sm2Group><FieldsBlob/><Name>Guest</Name></Sm2Group></Element>',
    '<Element><Sm2Group><FieldsBlob/><Name>Host</Name></Sm2Group></Element></Sm2GroupList>',
  ].join('');
  assert.deepEqual(listGroupNames(xml), ['Host', 'Guest']);
});

// ── #104 findings 2 + 6: entity-aware lookup, precise segment binding, temp-dir hygiene ──

test('decodeXmlEntities handles named, decimal and hex references', () => {
  assert.equal(decodeXmlEntities('A &amp; B'), 'A & B');
  assert.equal(decodeXmlEntities('&lt;tag&gt; &quot;q&quot; &apos;a&apos;'), '<tag> "q" \'a\'');
  assert.equal(decodeXmlEntities('caf&#233;'), 'café');
  assert.equal(decodeXmlEntities('caf&#xE9;'), 'café');
  assert.equal(decodeXmlEntities('&notanentity; stays'), '&notanentity; stays');
});

test('a group whose name contains an escaped entity is discoverable', () => {
  // Resolve stores `A & B` as `A &amp; B`; the raw string match never found it.
  const xml = '<Sm2GroupList><Element><Sm2Group><FieldsBlob/><Name>A &amp; B</Name><Body>ab</Body></Sm2Group></Element></Sm2GroupList>';
  assert.deepEqual(listGroupNames(xml), ['A & B']);
  const seg = groupSegment(xml, 'A & B');
  assert.ok(seg, 'group with an entity in its name must be found by its decoded name');
  assert.match(seg, /<Body>ab<\/Body>/);
});

test('groupSegment ignores an identically-named <Name> in a different element', () => {
  // A non-group element carrying <Name>Host</Name> appears FIRST in the doc. The
  // old lazy indexOf bound to it and then walked backwards to the wrong (or no)
  // Sm2Group.
  const xml = [
    '<Sm2TimelineList><Element><Sm2Timeline><Name>Host</Name><Body>WRONG</Body></Sm2Timeline></Element></Sm2TimelineList>',
    '<Sm2GroupList><Element><Sm2Group><FieldsBlob/><Name>Host</Name><Body>RIGHT</Body></Sm2Group></Element></Sm2GroupList>',
  ].join('');
  const seg = groupSegment(xml, 'Host');
  assert.ok(seg, 'the real Sm2Group must still be found');
  assert.match(seg, /<Body>RIGHT<\/Body>/);
  assert.doesNotMatch(seg, /WRONG/);
  assert.deepEqual(listGroupNames(xml), ['Host'], 'a non-group <Name> is not a group name');
});

test('groupSegment returns null for an unknown group', () => {
  const xml = '<Sm2GroupList><Element><Sm2Group><Name>Host</Name></Sm2Group></Element></Sm2GroupList>';
  assert.equal(groupSegment(xml, 'Nobody'), null);
});

test('readProjectXml cleans up its temp dir even when the read fails', () => {
  const before = fs.readdirSync(os.tmpdir()).filter((d) => d.startsWith('ggr-')).length;
  const missing = path.join(os.tmpdir(), 'definitely-not-a-real-file-104.drp');
  assert.throws(() => readProjectXml(missing), /unzip/);
  const after = fs.readdirSync(os.tmpdir()).filter((d) => d.startsWith('ggr-')).length;
  assert.equal(after, before, 'a failed read must not leak its mkdtemp directory');
});
