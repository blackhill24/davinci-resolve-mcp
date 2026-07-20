/**
 * retime-clip — clip speed / retime surgery on a real `.drp`/`.drt` timeline.
 *
 * A clip's playback speed lives in its per-clip `<MediaTimemapBA>` blob (see
 * media-timemap.js — identity compact form for 1×, `Sm2TimeMap` keyed-dict for
 * anything else, keyframed record→source curve for variable-speed ramps). The
 * timemap covers the WHOLE media source (verified live: a 50% retime exported
 * XMax = full-source-duration / 0.5), so retiming is a blob swap plus an
 * optional `<Duration>` rescale — the clip's `<In>`/`<Duration>` then select a
 * window of the retimed record time exactly as they do at 1×.
 *
 * Two ops:
 *  - retimeClip: constant speed (`speed`), explicit ramp (`keyframes`), or
 *    fit-to-fill (`newDuration` alone derives the speed); rescales `<Duration>`
 *    (record frames) unless `newDuration` overrides it, with optional
 *    track-scoped ripple of later clips.
 *  - slipClip: shift the source in-point by `frames` in EITHER direction while
 *    Start/Duration stay fixed (the head-EXTEND primitive trim_clip_head lacks;
 *    unblocks slip retreat, issue #30 / 3.1.1).
 *
 * @module drp-format/retime-clip
 */

const {
  loadDrpZip,
  selectTargetSeq,
  splitClipElements,
  getItemsInner,
  setItemsInner,
  getTrackVec,
  replaceTrackVec,
} = require('./seq-surgery');
const { generateUUID } = require('./xml-builder');
const {
  decodeTimemap, encodeTimemap, buildTimemap, buildConstantSpeedTimemap, TYPE_LINEAR,
} = require('./media-timemap');

function clipDbId(clipXml) {
  return (clipXml.match(/<Sm2Ti(?:Video|Audio)Clip DbId="([^"]+)"/) || [])[1] || null;
}
function clipStart(clipXml) {
  const m = clipXml.match(/<Start>(\d+)<\/Start>/);
  return m ? parseInt(m[1], 10) : null;
}
function clipDuration(clipXml) {
  const m = clipXml.match(/<Duration>(\d+)<\/Duration>/);
  return m ? parseInt(m[1], 10) : null;
}
function setClipStart(clipXml, v) {
  return clipXml.replace(/<Start>\d+<\/Start>/, `<Start>${v}</Start>`);
}
function setClipDuration(clipXml, v) {
  return clipXml.replace(/<Duration>\d+<\/Duration>/, `<Duration>${v}</Duration>`);
}
// Same dual-encoding rules as splice-clips.js clipIn/setClipIn (plain int from a
// real export, framePos|hex from our own writers) — kept in sync by the
// slip-round-trip test.
function encodeSourceIn(frame) {
  const buf = Buffer.alloc(8);
  buf.writeDoubleLE(frame * 0.001, 0);
  return `${frame}|${buf.toString('hex')}`;
}
function clipIn(clipXml) {
  const m = clipXml.match(/<In>(\d+)/);
  return m ? parseInt(m[1], 10) : 0;
}
function setClipIn(clipXml, framePos) {
  const enc = `<In>${encodeSourceIn(framePos)}</In>`;
  if (/<In\s*\/>/.test(clipXml)) return clipXml.replace(/<In\s*\/>/, enc);
  if (/<In>[^<]*<\/In>/.test(clipXml)) return clipXml.replace(/<In>[^<]*<\/In>/, enc);
  return clipXml;
}

function pickClip(clips, { clipDbId: wantId, nameContains, clipIndex = 0 }) {
  if (wantId) {
    const i = clips.findIndex((c) => clipDbId(c) === wantId);
    if (i < 0) throw new Error(`retime: no clip with DbId ${wantId} on the target track`);
    return i;
  }
  if (nameContains) {
    const i = clips.findIndex((c) => {
      const m = c.match(/<Name>([\s\S]*?)<\/Name>/);
      return m && m[1].includes(nameContains);
    });
    if (i < 0) throw new Error(`retime: no clip whose Name contains "${nameContains}" on the target track`);
    return i;
  }
  if (clipIndex < 0 || clipIndex >= clips.length) {
    throw new Error(`retime: clipIndex ${clipIndex} out of range (track has ${clips.length} clip(s))`);
  }
  return clipIndex;
}

function mapClipsOnTrack(trackElement, fn) {
  const inner = getItemsInner(trackElement);
  let out = inner;
  for (const c of splitClipElements(inner)) {
    const nc = fn(c);
    if (nc !== c) out = out.replace(c, nc);
  }
  return setItemsInner(trackElement, out);
}

function timemapHex(clipXml) {
  const m = clipXml.match(/<MediaTimemapBA>([0-9a-f]*)<\/MediaTimemapBA>/);
  return m ? m[1] : null; // <MediaTimemapBA/> or absent ⇒ null
}
function setTimemapHex(clipXml, hex) {
  const enc = `<MediaTimemapBA>${hex}</MediaTimemapBA>`;
  if (/<MediaTimemapBA\s*\/>/.test(clipXml)) return clipXml.replace(/<MediaTimemapBA\s*\/>/, enc);
  if (/<MediaTimemapBA>[0-9a-f]*<\/MediaTimemapBA>/.test(clipXml)) {
    return clipXml.replace(/<MediaTimemapBA>[0-9a-f]*<\/MediaTimemapBA>/, enc);
  }
  throw new Error('retime: clip has no MediaTimemapBA element to write');
}

/** Decode a clip's current timemap → { sourceDurationSec, oldSpeed, constant, uniqueId }. */
function readClipTimemap(clipXml) {
  const hex = timemapHex(clipXml);
  if (!hex) return null;
  const d = decodeTimemap(hex);
  if (d.form === 'identity') {
    // Media identity map is [end,0,end,0,end]; titles/generators carry [duration].
    return { sourceDurationSec: d.seconds[0], oldSpeed: 1, constant: true, uniqueId: null };
  }
  const uniqueId = (d.entries.find((e) => e.key === 'UniqueId') || {}).value || null;
  return {
    sourceDurationSec: d.sourceDurationSec,
    oldSpeed: d.speed,
    constant: !d.variable,
    uniqueId,
  };
}

/**
 * Retime a clip: constant speed or explicit variable-speed ramp.
 *
 * @param {Buffer|string} drpInput
 * @param {object} opts
 * @param {number} opts.track              - 1-based track holding the clip.
 * @param {number} [opts.speed]            - constant speed (>0; 0.5 = half speed, 2 = double).
 *                                           speed 1 resets to the identity map.
 * @param {Array}  [opts.keyframes]        - explicit ramp [{recordSec, sourceSec}] (ordered,
 *                                           implicit (0,0) start excluded); requires newDuration.
 * @param {number} [opts.newDuration]      - explicit new record duration in frames; default =
 *                                           round(oldDuration * oldSpeed / speed) (constant only).
 * @param {number} [opts.sourceDurationSec]- override the source duration (only needed when the
 *                                           clip has no readable timemap).
 * @param {number} [opts.clipIndex=0]
 * @param {string} [opts.clipDbId]
 * @param {string} [opts.nameContains]
 * @param {boolean} [opts.ripple=false]    - shift later clips on THIS track by the duration delta.
 * @param {string} [opts.timelineUuid]
 * @param {string} [opts.trackType='video']
 * @returns {Promise<{buffer:Buffer, entry:string, timelineUuid:string, track:number,
 *   retimedClipDbId:string|null, oldSpeed:number|null, speed:number|null, variable:boolean,
 *   oldDuration:number, newDuration:number, sourceDurationSec:number, rippled:boolean}>}
 */
async function retimeClip(drpInput, opts = {}) {
  const {
    track: trackIdx, speed, keyframes, newDuration: wantDuration, sourceDurationSec: srcOverride,
    clipIndex = 0, clipDbId: selId, nameContains, ripple = false, timelineUuid, trackType = 'video',
  } = opts;
  if (!Number.isInteger(trackIdx) || trackIdx < 1) throw new TypeError('retimeClip: track must be a positive integer');
  const hasSpeed = speed !== undefined && speed !== null;
  const hasRamp = Array.isArray(keyframes) && keyframes.length > 0;
  // Third mode — fit-to-fill: newDuration alone derives the constant speed that
  // makes the clip's current source segment fill exactly newDuration frames.
  const fitMode = !hasSpeed && !hasRamp && wantDuration != null;
  if (hasSpeed && hasRamp) throw new TypeError('retimeClip: pass exactly one of speed | keyframes');
  if (!hasSpeed && !hasRamp && !fitMode) {
    throw new TypeError('retimeClip: pass speed, keyframes, or newDuration (fit-to-fill)');
  }
  if (hasSpeed && !(speed > 0)) throw new TypeError('retimeClip: speed must be > 0');
  if (hasRamp && wantDuration == null) {
    throw new TypeError('retimeClip: keyframes (variable ramp) requires an explicit newDuration — '
      + 'the record-frames total depends on the timeline fps, which the caller knows');
  }
  if (wantDuration != null && (!Number.isInteger(wantDuration) || wantDuration < 1)) {
    throw new TypeError('retimeClip: newDuration must be a positive integer');
  }

  const zip = await loadDrpZip(drpInput);
  const { entry, xml: seqXml, seqId } = await selectTargetSeq(zip, timelineUuid);
  const { match: vtv, tracks } = getTrackVec(seqXml, trackType);
  if (trackIdx > tracks.length) throw new Error(`retimeClip: track ${trackIdx} does not exist (timeline has ${tracks.length} ${trackType} track(s))`);

  const items = getItemsInner(tracks[trackIdx - 1]);
  const clips = splitClipElements(items);
  if (clips.length === 0) throw new Error(`retimeClip: track ${trackIdx} has no clips`);
  const idx = pickClip(clips, { clipDbId: selId, nameContains, clipIndex });
  const target = clips[idx];
  const oldDuration = clipDuration(target);
  const tStart = clipStart(target);

  const current = readClipTimemap(target);
  const sourceDurationSec = srcOverride != null ? srcOverride : current && current.sourceDurationSec;
  if (sourceDurationSec == null) {
    throw new Error('retimeClip: clip has no readable MediaTimemapBA — pass sourceDurationSec');
  }
  const oldSpeed = current ? current.oldSpeed : 1;
  const oldConstant = current ? current.constant : true;

  // New record duration (timeline frames). Constant→constant scales by the speed
  // ratio (fps-free); everything else needs the explicit value. Fit mode inverts
  // the same ratio to derive the speed from the requested duration.
  let newDuration = wantDuration;
  let effSpeed = speed;
  if (fitMode) {
    if (!oldConstant) {
      throw new Error('retimeClip: fit-to-fill on a variable-speed clip is ambiguous — pass speed or keyframes');
    }
    effSpeed = oldSpeed * (oldDuration / newDuration);
    if (!(effSpeed > 0)) throw new Error('retimeClip: derived fit-to-fill speed is not positive');
  } else if (newDuration == null) {
    if (!oldConstant) {
      throw new Error('retimeClip: clip already has a variable-speed ramp — pass newDuration explicitly');
    }
    newDuration = Math.max(1, Math.round(oldDuration * (oldSpeed / speed)));
  }

  // Preserve the Sm2TimeMap identity across re-retimes; mint one on first retime.
  const uniqueId = (current && current.uniqueId) || generateUUID();
  let blob;
  let variable = false;
  if (hasRamp) {
    blob = buildTimemap({ keyframes, sourceDurationSec, uniqueId });
    variable = keyframes.length > 1;
  } else if (effSpeed === 1) {
    const end = sourceDurationSec;
    blob = encodeTimemap({ type: TYPE_LINEAR, seconds: [end, 0, end, 0, end] });
  } else {
    blob = buildConstantSpeedTimemap({ speed: effSpeed, sourceDurationSec, uniqueId });
  }

  let edited = setTimemapHex(target, blob.toString('hex'));
  edited = setClipDuration(edited, newDuration);

  let track = setItemsInner(tracks[trackIdx - 1], items.replace(target, edited));
  if (ripple && oldDuration !== null && tStart !== null) {
    const delta = newDuration - oldDuration;
    const tEnd = tStart + oldDuration;
    track = mapClipsOnTrack(track, (c) => {
      const s = clipStart(c);
      return s !== null && s >= tEnd ? setClipStart(c, s + delta) : c;
    });
  }
  tracks[trackIdx - 1] = track;

  const xml = replaceTrackVec(seqXml, trackType, vtv, tracks);
  zip.file(entry, xml);
  const buffer = await zip.generateAsync({ type: 'nodebuffer', compression: 'DEFLATE' });
  return {
    buffer, entry, timelineUuid: seqId, track: trackIdx,
    retimedClipDbId: clipDbId(target),
    oldSpeed, speed: hasRamp ? null : effSpeed, variable,
    oldDuration, newDuration, sourceDurationSec,
    rippled: Boolean(ripple),
  };
}

/**
 * Slip a clip: shift its source in-point by `frames` (positive = later source
 * content, negative = earlier). Start and Duration are untouched, so the
 * timeline is otherwise identical — this is the single-op slip that the
 * trim_clip_head + move + trim composition approximated (advance only).
 *
 * Bounds: the new in-point must stay >= 0. The tail bound (enough source after
 * the new in-point) is not verifiable offline — Resolve holds the last frame if
 * the source runs out.
 *
 * @param {Buffer|string} drpInput
 * @param {object} opts
 * @param {number} opts.track          - 1-based track.
 * @param {number} opts.frames         - non-zero slip amount (timeline frames; sign = direction).
 * @param {number} [opts.clipIndex=0]
 * @param {string} [opts.clipDbId]
 * @param {string} [opts.nameContains]
 * @param {string} [opts.timelineUuid]
 * @param {string} [opts.trackType='video']
 * @returns {Promise<{buffer:Buffer, entry:string, timelineUuid:string, track:number,
 *   clipDbId:string|null, frames:number, oldIn:number, newIn:number}>}
 */
async function slipClip(drpInput, opts = {}) {
  const { track: trackIdx, frames, clipIndex = 0, clipDbId: selId, nameContains, timelineUuid, trackType = 'video' } = opts;
  if (!Number.isInteger(trackIdx) || trackIdx < 1) throw new TypeError('slipClip: track must be a positive integer');
  if (!Number.isInteger(frames) || frames === 0) throw new TypeError('slipClip: frames must be a non-zero integer');

  const zip = await loadDrpZip(drpInput);
  const { entry, xml: seqXml, seqId } = await selectTargetSeq(zip, timelineUuid);
  const { match: vtv, tracks } = getTrackVec(seqXml, trackType);
  if (trackIdx > tracks.length) throw new Error(`slipClip: track ${trackIdx} does not exist (timeline has ${tracks.length} ${trackType} track(s))`);

  const items = getItemsInner(tracks[trackIdx - 1]);
  const clips = splitClipElements(items);
  if (clips.length === 0) throw new Error(`slipClip: track ${trackIdx} has no clips`);
  const idx = pickClip(clips, { clipDbId: selId, nameContains, clipIndex });
  const target = clips[idx];
  const oldIn = clipIn(target);
  const newIn = oldIn + frames;
  if (newIn < 0) {
    throw new Error(`slipClip: cannot retreat the in-point by ${-frames} — the clip's in-point is `
      + `${oldIn} frame(s); at most ${oldIn} available before the source head`);
  }

  const edited = setClipIn(target, newIn);
  if (edited === target) throw new Error('slipClip: clip has no <In> element to write');
  tracks[trackIdx - 1] = setItemsInner(tracks[trackIdx - 1], items.replace(target, edited));

  const xml = replaceTrackVec(seqXml, trackType, vtv, tracks);
  zip.file(entry, xml);
  const buffer = await zip.generateAsync({ type: 'nodebuffer', compression: 'DEFLATE' });
  return { buffer, entry, timelineUuid: seqId, track: trackIdx, clipDbId: clipDbId(target), frames, oldIn, newIn };
}

module.exports = { retimeClip, slipClip };
