#!/usr/bin/env node
/**
 * drp-bridge — one-shot JSON bridge for MUTATING drp-format ops on an exported
 * .drt/.drp in a scratch dir. Invoked as:
 *
 *   node scripts/drp-bridge.mjs <tool> <action> [argsJson]
 *
 * Prints exactly one JSON object to stdout: {success, result} | {success:false, error}.
 *
 * This is the WRITE counterpart to panel-bridge.mjs (which is a deliberate
 * read-only inspection allowlist). The Python live server drives drt/drp surgery
 * through here on a scratch copy — never on source media (src/utils/advanced_bridge.py
 * owns the scratch discipline). Timelines are stateless artifacts: an op reads a
 * drpPath and writes a mutated buffer to outputPath.
 *
 *   tool  drp → server/tools/drp.mjs  (place_transition, place_fusion_title,
 *                                      split_clip, move_clip, delete_clip, ...)
 *   tool  drt → server/tools/drt.mjs  (parse, list_sequences, author, validate,
 *                                      inject_into_drp, extract_from_drp, ...)
 *   tool  drx → server/tools/drx.mjs  (generate, build_graph, merge, parse, ... —
 *                                      grade authoring; args carry outputPath, no drpPath)
 */

const [tool, action, argsJson] = process.argv.slice(2);

// stdout is reserved for the single JSON result line. Some codecs (e.g. the DRX merger)
// console.log progress — route that to stderr so it can't corrupt the JSON contract.
console.log = (...a) => process.stderr.write(a.join(' ') + '\n');
console.info = console.log;

function out(obj) {
  process.stdout.write(JSON.stringify(obj));
}

try {
  const args = argsJson ? JSON.parse(argsJson) : {};
  if (!action) throw new Error('action is required (e.g. place_transition)');

  let toolObj;
  if (tool === 'drp') {
    ({ drpTool: toolObj } = await import('../server/tools/drp.mjs'));
  } else if (tool === 'drt') {
    ({ drtTool: toolObj } = await import('../server/tools/drt.mjs'));
  } else if (tool === 'drx') {
    // Offline per-clip grade authoring (generate/build_graph/merge/...). Unlike drp/drt these
    // ops don't mutate a source drpPath — they synthesize a .drx from params, so the caller
    // passes the whole args object (outputPath included) rather than going through run_drp_op.
    ({ drxTool: toolObj } = await import('../server/tools/drx.mjs'));
  } else {
    throw new Error(`unknown tool '${tool}' (drp|drt|drx)`);
  }

  const result = await toolObj.handler({ action, args });
  out({ success: true, result });
} catch (e) {
  out({ success: false, error: String((e && e.message) || e) });
  process.exitCode = 1;
}
