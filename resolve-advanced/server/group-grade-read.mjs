/**
 * Color-group grade READ PATH (Route A) — decode a project's Color Group pre/post-clip
 * grades from an exported .drp, OFFLINE, no Resolve required.
 *
 * Closes the gap noted in test/group-grade-calibration.test.mjs: a group's Pre/Post-Clip
 * grade is stored as a standard DRX `<Body>` inside `project.xml`'s `<Sm2Group>` element —
 * same format as a clip grade, so the existing drx-parser decodes it unchanged. The only
 * missing piece was a READ PATH (locate the group bodies); this is it.
 *
 * Pipeline:.drp (zip) -> project.xml -> per-group <Body> (pre, post) -> drx-parser.parse
 * -> flatten (LUT refs, HSL curves) -> DRX->UI scale -> compact per-node summary.
 *
 * Values for the calibrated native control set (primaries, log wheels, curves, HSL/RGB/3D
 * qualifier ranges, windows, Color Warper, ColorSlice, HDR zones) are EXACT; OFX/long-tail
 * IDs are raw (see parse().valueFidelity).
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { drxTool } from './tools/drx.mjs';

const require = createRequire(import.meta.url);
const { extractDrxLutRefs } = require('../vendor/drx-codec/extract-lut-refs.js');
const { extractHSLCurves } = require('../vendor/drx-codec/extract-hsl-curves.js');

// DRX -> UI scaling (vendor/drx-parameters/DRX-VALUE-SCALING.md)
export function scaleParam(name, v) {
  if (typeof v !== 'number') return v;
  const n = name.toLowerCase();
  if (/saturation/.test(n)) return +(v * 100).toFixed(2); // %
  if (/contrast/.test(n)) return +(v * 100).toFixed(2); // %
  if (/(^|\.)lift/.test(n)) return +(v / 2).toFixed(4);
  if (/(^|\.)gamma/.test(n)) return +(v / 4).toFixed(4);
  if (/offset/.test(n)) return +(v * 2500).toFixed(2);
  return +(+v).toFixed(4);
}

const IDENTITY_3X3 = '1,0,0,0,1,0,0,0,1';
const isIdentity = (a) => Array.isArray(a) && a.length === 9 && a.join(',') === IDENTITY_3X3;
const STRUCTURAL = /hslcurves|trackingblob|polygonshape|gradientwindow|nodelut|softmatrix|(^|\.)matrix$/i;

// A wedged `unzip` used to hang the tool call forever — spawnSync blocks the
// whole Node process, so there is no other way out.
const UNZIP_TIMEOUT_MS = 30000;

export function readProjectXml(drpPath) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'ggr-'));
  // finally, not a trailing rmSync: every throw below (and readFileSync's own)
  // used to leak the temp dir on each failed read.
  try {
    const r = spawnSync('unzip', ['-o', drpPath, 'project.xml', '-d', tmp], {
      encoding: 'utf8',
      timeout: UNZIP_TIMEOUT_MS,
    });
    if (r.error) {
      if (r.error.code === 'ENOENT') {
        throw new Error(
          `\`unzip\` was not found on PATH — it is required to read ${drpPath}. ` + 'Install it (apt install unzip / brew install unzip / dnf install unzip).',
        );
      }
      if (r.error.code === 'ETIMEDOUT') {
        throw new Error(`unzip timed out after ${UNZIP_TIMEOUT_MS}ms reading ${drpPath}`);
      }
      throw new Error(`unzip could not be run for ${drpPath}: ${r.error.message}`);
    }
    if (r.status !== 0) throw new Error(`unzip failed for ${drpPath}: ${(r.stderr || '').slice(-200)}`);
    return fs.readFileSync(path.join(tmp, 'project.xml'), 'utf8');
  } finally {
    try {
      fs.rmSync(tmp, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  }
}

const NAMED_ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };

/** Decode the XML entities Resolve writes into project.xml text nodes. */
export function decodeXmlEntities(s) {
  return String(s).replace(/&(#x[0-9a-fA-F]+|#[0-9]+|[a-zA-Z]+);/g, (whole, body) => {
    if (body[0] === '#') {
      const code = body[1] === 'x' || body[1] === 'X' ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : whole;
    }
    return Object.prototype.hasOwnProperty.call(NAMED_ENTITIES, body) ? NAMED_ENTITIES[body] : whole;
  });
}

// One <Sm2Group>…</Sm2Group> element. Scoping every lookup to a whole element
// is what stops a <Name> belonging to some other element from being mistaken
// for a group's name.
const SM2GROUP_PATTERN = /<Sm2Group\b[\s\S]*?<\/Sm2Group>/g;

const sm2GroupSegments = (xml) => String(xml).match(SM2GROUP_PATTERN) || [];

/** The decoded name of one <Sm2Group> segment, or null if it carries none. */
export function groupNameOf(segment) {
  const m = /<Name>([\s\S]*?)<\/Name>/.exec(segment);
  return m ? decodeXmlEntities(m[1]) : null;
}

/** All color-group names present in the project (from <Sm2Group><Name>…). */
export function listGroupNames(xml) {
  const names = [];
  for (const seg of sm2GroupSegments(xml)) {
    const name = groupNameOf(seg);
    if (name !== null) names.push(name);
  }
  return [...new Set(names)];
}

/** The <Sm2Group> element whose decoded name is `name`, or null.
 *
 * Compares DECODED names: a group called `A & B` is stored as `A &amp; B`, so a
 * raw string match never found it. And it walks whole <Sm2Group> elements
 * rather than seeking a bare `<Name>` anywhere in the document, so an
 * identically-named node inside a different element can't be picked up.
 */
export function groupSegment(xml, name) {
  for (const seg of sm2GroupSegments(xml)) {
    if (groupNameOf(seg) === name) return seg;
  }
  return null;
}

export const groupBodies = (seg) => [...seg.matchAll(/<Body>([0-9a-fA-F]+)<\/Body>/g)].map((m) => m[1]);

async function decodeBody(bodyHex, label) {
  const content = `<?xml version="1.0" encoding="UTF-8"?>\n<Resolve_Color_Exchange><Label>${label}</Label><Width>1920</Width><Height>1080</Height><Body>${bodyHex}</Body></Resolve_Color_Exchange>`;
  let r;
  try {
    r = await drxTool.handler({ action: 'parse', args: { content } });
  } catch (e) {
    return { error: String(e.message || e), node_count: 0, nodes: [] };
  }
  const lutRefs = {};
  for (const l of extractDrxLutRefs(r) || []) lutRefs[l.nodeIndex] = l.lutPath;
  const nodes = (r.nodes || [])
    .map((n) => {
      const idx = n.nodeIndex ?? n.index;
      const all = (n.correctors || []).flatMap((c) => c.parameters || []);
      const params = {};
      for (const p of all) {
        if (!p.name || /^unknown/i.test(p.name) || STRUCTURAL.test(p.name)) continue;
        if (typeof p.value === 'object') continue;
        if (typeof p.value === 'number' && Math.abs(p.value) < 1e-6) continue;
        params[p.name] = scaleParam(p.name, p.value);
      }
      let curves;
      try {
        const h = extractHSLCurves(all);
        if (h) curves = Object.keys(h);
      } catch {
        /* ignore */
      }
      const win = all.find((p) => /softmatrix|polygonshape\.matrix|gradientwindow/i.test(p.name) && !isIdentity(p.value));
      const node = { node: idx, tools: (n.correctors || []).map((c) => c.type ?? c.correctorType) };
      if (lutRefs[idx]) node.lut = lutRefs[idx];
      if (Object.keys(params).length) node.params = params;
      if (curves && curves.length) node.curves = curves;
      if (win) node.window = true;
      return node;
    })
    .filter((n) => n.params || n.lut || n.curves || n.window);
  return { node_count: (r.nodes || []).length, valueFidelity: r.valueFidelity?.level ?? null, nodes };
}

/**
 * Decode color-group grades from a .drp.
 * @param {string} drpPath
 * @param {{groups?: string[], includePreClip?: boolean}} [opts]
 * @returns {Promise<Object>} { <group>: { pre_clip?, post_clip } }
 */
export async function decodeGroupGrades(drpPath, opts = {}) {
  const xml = readProjectXml(drpPath);
  const groups = opts.groups && opts.groups.length ? opts.groups : listGroupNames(xml);
  const out = {};
  for (const g of groups) {
    const seg = groupSegment(xml, g);
    if (!seg) {
      out[g] = { error: 'group not found' };
      continue;
    }
    const bs = groupBodies(seg); // typically [pre, post]
    out[g] = {};
    if (bs.length >= 2 && opts.includePreClip !== false) out[g].pre_clip = await decodeBody(bs[0], `${g} pre`);
    if (bs.length >= 1) out[g].post_clip = await decodeBody(bs[bs.length - 1], `${g} post`);
  }
  return out;
}
