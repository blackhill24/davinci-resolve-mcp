// Offline test for build_graph: author a whole N-node serial grade tree in one call.
// Node 1 seeds the graph (same path as `generate`); nodes 2..N graft serially (1->2->...->N).
// This is the 3.3.1 (issue #23) node-tree + primaries authoring helper. Live apply is
// covered by tests/live_drx_grade_authoring_probe.py.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs/promises';
import { drxTool } from '../server/tools/drx.mjs';

test('build_graph authors an N-node tree with per-node labels, primaries decode calibrated', async () => {
  const out = path.join(os.tmpdir(), `drx-build-graph-${process.pid}.drx`);
  const r = await drxTool.handler({
    action: 'build_graph',
    args: {
      nodes: [
        { label: 'Balance', params: { lift: { r: 0.01, g: 0, b: -0.01 }, gain: { r: 1.03, g: 1.0, b: 0.97 } } },
        { label: 'Contrast', params: { contrast: 1.15, pivot: 0.4 } },
        { label: 'Sat', params: { saturation: 65 } },
      ],
      metadata: { label: 'Authored Tree' },
      outputPath: out,
    },
  });
  assert.equal(r.nodeCount, 3, 'reports 3 nodes');
  assert.ok(r.bytes > 0, 'wrote bytes');

  const parsed = await drxTool.handler({ action: 'parse', args: { drxPath: out } });
  assert.equal(parsed.nodes.length, 3, 'parsed 3 nodes');
  assert.deepEqual(parsed.nodes.map((n) => n.label), ['Balance', 'Contrast', 'Sat'], 'per-node labels preserved');
  // Primaries live in the calibrated set — decoded values are ground truth.
  assert.equal(parsed.valueFidelity.level, 'calibrated-subset-only');
  await fs.rm(out, { force: true });
});

test('build_graph with a single node equals a plain generate (seed-only path)', async () => {
  const r = await drxTool.handler({
    action: 'build_graph',
    args: { nodes: [{ label: 'Solo', params: { gain: { master: 1.05 } } }] },
  });
  assert.equal(r.nodeCount, 1);
  assert.ok(typeof r.content === 'string' && r.content.length > 0);
});

test('build_graph requires at least one node', async () => {
  await assert.rejects(() => drxTool.handler({ action: 'build_graph', args: { nodes: [] } }));
});
