/** Malformed-varint hardening for the hand-rolled byte walkers in drx-parser.js.
 *
 * A 5-byte length varint with bit 31 set used to decode NEGATIVE (`<<` is a signed
 * 32-bit shift), so `p += len` walked backwards and the enclosing `while (p < buf.length)`
 * spun forever — one corrupt blob wedged the whole Node process. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { readVarintAt } = require('../vendor/drx-codec/drx-parser.js');

test('readVarintAt never returns a negative length', () => {
  // 0xfa 0xff 0xff 0xff 0x0f == 0xFFFFFFFA — decoded as signed this is -6.
  const [len, next] = readVarintAt(Buffer.from([0xfa, 0xff, 0xff, 0xff, 0x0f]), 0);
  assert.ok(len >= 0, `length must be non-negative, got ${len}`);
  assert.equal(len, 0xfffffffa);
  assert.equal(next, 5);
});

test('readVarintAt always advances past the tag byte', () => {
  const [, next] = readVarintAt(Buffer.from([0x00]), 0);
  assert.equal(next, 1, 'a zero varint still consumes its byte — walkers rely on progress');
});

test('readVarintAt gives up on an over-long varint instead of shifting off the end', () => {
  const runaway = Buffer.from(Array(12).fill(0xff));
  const [len, next] = readVarintAt(runaway, 0);
  assert.equal(len, 0);
  assert.equal(next, runaway.length, 'must jump to the end so the caller stops walking');
});

test('readVarintAt stops at the buffer end on a truncated varint', () => {
  const [, next] = readVarintAt(Buffer.from([0xff, 0xff]), 0);
  assert.equal(next, 2);
});

// Same signed-shift bug, same consequence (`offset += length` walking backwards),
// in the drp-format wire helpers.
test('drp-format decodeVarint helpers decode lengths unsigned', () => {
  const overflow = Buffer.from([0xfa, 0xff, 0xff, 0xff, 0x0f]); // 0xFFFFFFFA
  for (const mod of ['effect-encoder', 'marker-encoder']) {
    const { decodeVarint } = require(`../vendor/drp-format/${mod}.js`);
    const { value } = decodeVarint(overflow, 0);
    assert.ok(value >= 0, `${mod}: length must be non-negative, got ${value}`);
  }
});
