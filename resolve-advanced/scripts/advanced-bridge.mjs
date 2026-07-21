#!/usr/bin/env node
/**
 * advanced-bridge — one-shot JSON bridge over ANY advanced-server tool.
 *
 *   node scripts/advanced-bridge.mjs <tool> <action> [argsJson]
 *
 * Prints exactly one JSON object to stdout: {success, result} | {success:false, error}.
 *
 * Generalizes drp-bridge.mjs (which is deliberately scoped to drp/drt/drx —
 * the mutating drp-format op path) to the full 18-tool set, for offline
 * compute calls that are pure file/DB reads with no drpPath-in/outputPath-out
 * shape (deliverable QC, conform analysis, color_trace, provenance, ...).
 *
 * This bridge does NOT know which actions need Resolve closed — several
 * tools mix pure-file actions with a live-DB path that requires it (e.g.
 * conform.fix_reverse_clip, offline_ref's LIVE DB link/unlink,
 * project_db.relayout_node_graphs). Check the target tool's own action
 * description before calling; the caller owns that precondition.
 */

const [tool, action, argsJson] = process.argv.slice(2);

// stdout is reserved for the single JSON result line — route any vendored
// console chatter to stderr so it can't corrupt the JSON contract.
console.log = (...a) => process.stderr.write(a.join(' ') + '\n');
console.info = console.log;

function out(obj) {
  process.stdout.write(JSON.stringify(obj));
}

const TOOL_MODULES = {
  drp: ['../server/tools/drp.mjs', 'drpTool'],
  drt: ['../server/tools/drt.mjs', 'drtTool'],
  drx: ['../server/tools/drx.mjs', 'drxTool'],
  offline_ref: ['../server/tools/offline_ref.mjs', 'offlineRefTool'],
  fusion: ['../server/tools/fusion.mjs', 'fusionTool'],
  audio_plan: ['../server/tools/audio_plan.mjs', 'audioPlanTool'],
  fairlight: ['../server/tools/fairlight.mjs', 'fairlightTool'],
  audio: ['../server/tools/audio.mjs', 'audioTool'],
  conform: ['../server/tools/conform.mjs', 'conformTool'],
  project_db: ['../server/tools/project_db.mjs', 'projectDbTool'],
  project_read: ['../server/tools/project_read.mjs', 'projectReadTool'],
  color_trace: ['../server/tools/color_trace.mjs', 'colorTraceTool'],
  capabilities: ['../server/tools/capabilities.mjs', 'capabilitiesTool'],
  pipeline: ['../server/tools/pipeline.mjs', 'pipelineTool'],
  deliverable: ['../server/tools/deliverable.mjs', 'deliverableTool'],
  media: ['../server/tools/media.mjs', 'mediaTool'],
  editorial: ['../server/tools/editorial.mjs', 'editorialTool'],
  provenance: ['../server/tools/provenance.mjs', 'provenanceTool'],
};

try {
  const args = argsJson ? JSON.parse(argsJson) : {};
  if (!action) throw new Error('action is required (e.g. deliverable_qc)');
  const entry = TOOL_MODULES[tool];
  if (!entry) {
    throw new Error(`unknown tool '${tool}' (${Object.keys(TOOL_MODULES).join('|')})`);
  }
  const [modPath, exportName] = entry;
  const mod = await import(modPath);
  const toolObj = mod[exportName];
  const result = await toolObj.handler({ action, args });
  out({ success: true, result });
} catch (e) {
  out({ success: false, error: String((e && e.message) || e) });
  process.exitCode = 1;
}
