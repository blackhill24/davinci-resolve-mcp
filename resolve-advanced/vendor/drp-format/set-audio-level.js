/**
 * set-audio-level — author a clip AUDIO VOLUME level into a real `.drt`, offline.
 * This is the Tier-2 ducking path (issue #14): the scripting API can't set clip
 * volume, but the level round-trips through an exported `.drt`, so we write it
 * here with no Resolve running. Export the approved timeline as `.drt`, run this,
 * reimport — the media-link blobs are preserved byte-for-byte (export-then-modify).
 *
 * It targets ONE audio clip (by track + clip index) and writes two coordinated
 * changes, exactly as Resolve itself does (see audio-effect-encoder.js for the
 * reverse-engineered ground truth): the audio-volume <EffectFiltersBA> blob and
 * the "has effect filters" flag in the clip's <FieldsBlob>.
 *
 * @module drp-format/set-audio-level
 */

const {
  loadDrpZip,
  selectTargetSeq,
  getTrackVec,
  getItemsInner,
  setItemsInner,
  replaceTrackVec,
} = require('./seq-surgery');
const {
  encodeAudioVolumeEffectFiltersBA,
  enableEffectFiltersFlag,
} = require('./audio-effect-encoder');

// Audio clip <Element> blocks within a track's <Items> (excludes video clips).
const AUDIO_CLIP_PATTERN =
  '<Element>\\s*<Sm2TiAudioClip\\b[\\s\\S]*?<\\/Sm2TiAudioClip>\\s*<\\/Element>';

function splitAudioClips(itemsInner) {
  return itemsInner.match(new RegExp(AUDIO_CLIP_PATTERN, 'g')) || [];
}

// Audio clip blocks with their byte offsets, so a patch can splice by position
// instead of String.replace (which expands `$` patterns in the replacement and
// always hits the FIRST occurrence — wrong clip when two elements are identical).
function matchAudioClips(itemsInner) {
  const re = new RegExp(AUDIO_CLIP_PATTERN, 'g');
  const out = [];
  let m;
  while ((m = re.exec(itemsInner)) !== null) out.push({ xml: m[0], at: m.index });
  return out;
}

// Replace the clip's own (first) <FieldsBlob> and its <EffectFiltersBA>. Both are
// unique within a clip element; other *BA blobs (MarkersBA, RenderCacheBA, …) have
// distinct tag names so they are untouched.
function applyToClip(clipXml, volumeDb) {
  const fb = clipXml.match(/<FieldsBlob>([0-9a-fA-F]*)<\/FieldsBlob>/);
  if (!fb) throw new Error('set-audio-level: target audio clip has no <FieldsBlob>');
  const newFields = enableEffectFiltersFlag(fb[1]);
  let out = clipXml.replace(fb[0], `<FieldsBlob>${newFields}</FieldsBlob>`);

  const effHex = encodeAudioVolumeEffectFiltersBA(volumeDb);
  if (/<EffectFiltersBA\s*\/>/.test(out)) {
    out = out.replace(/<EffectFiltersBA\s*\/>/, `<EffectFiltersBA>${effHex}</EffectFiltersBA>`);
  } else if (/<EffectFiltersBA>[\s\S]*?<\/EffectFiltersBA>/.test(out)) {
    out = out.replace(/<EffectFiltersBA>[\s\S]*?<\/EffectFiltersBA>/, `<EffectFiltersBA>${effHex}</EffectFiltersBA>`);
  } else {
    throw new Error('set-audio-level: target audio clip has no <EffectFiltersBA> element');
  }
  // Hand the encoded blob back so the caller reports the exact bytes written.
  return { xml: out, effHex };
}

/**
 * Set an audio clip's volume level in a `.drt`.
 *
 * @param {Buffer|string} drtInput          - `.drt` bytes or path.
 * @param {object} opts
 * @param {number} opts.track               - 1-based audio track (A2 = 2).
 * @param {number} opts.volumeDb            - level in dB (negative ducks; e.g. -12).
 * @param {number} [opts.clipIndex=0]       - which clip on the track (0-based, in track order).
 * @param {string} [opts.timelineUuid]      - target SeqContainer DbId (default: first with tracks).
 * @returns {Promise<{buffer:Buffer, entry:string, timelineUuid:string, track:number,
 *   clipIndex:number, volumeDb:number, effectFiltersHex:string}>}
 */
async function setAudioLevel(drtInput, opts = {}) {
  const { track, volumeDb, clipIndex = 0, timelineUuid } = opts;
  if (!Number.isInteger(track) || track < 1) {
    throw new TypeError('setAudioLevel: track must be a positive integer (A1 = 1)');
  }
  if (typeof volumeDb !== 'number' || !Number.isFinite(volumeDb)) {
    throw new TypeError('setAudioLevel: volumeDb must be a finite number');
  }
  if (!Number.isInteger(clipIndex) || clipIndex < 0) {
    throw new TypeError('setAudioLevel: clipIndex must be a non-negative integer');
  }

  const zip = await loadDrpZip(drtInput);
  const { entry, xml: seqXml, seqId } = await selectTargetSeq(zip, timelineUuid);
  const { match: vec, tracks } = getTrackVec(seqXml, 'audio');
  if (track > tracks.length) {
    throw new Error(`setAudioLevel: audio track ${track} does not exist (timeline has ${tracks.length})`);
  }

  const items = getItemsInner(tracks[track - 1]);
  const clips = matchAudioClips(items);
  if (clipIndex >= clips.length) {
    throw new Error(
      `setAudioLevel: clip index ${clipIndex} out of range on audio track ${track} ` +
      `(${clips.length} clip(s))`,
    );
  }
  const { xml: target, at } = clips[clipIndex];
  const { xml: patched, effHex } = applyToClip(target, volumeDb);
  const newItems = items.slice(0, at) + patched + items.slice(at + target.length);
  tracks[track - 1] = setItemsInner(tracks[track - 1], newItems);

  const xml = replaceTrackVec(seqXml, 'audio', vec, tracks);
  zip.file(entry, xml);
  const buffer = await zip.generateAsync({ type: 'nodebuffer', compression: 'DEFLATE' });
  return {
    buffer,
    entry,
    timelineUuid: seqId,
    track,
    clipIndex,
    volumeDb,
    effectFiltersHex: effHex,
  };
}

module.exports = { setAudioLevel, splitAudioClips };
