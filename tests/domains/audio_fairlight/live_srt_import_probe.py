#!/usr/bin/env python3
"""SRT-import feasibility probe + subtitle-text codec (issue #22, 3.2.5).

Investigation-only artifact (no MCP tool wired yet — see the 3.2.5 scope
decision). Proves that an SRT file can be converted into a Resolve subtitle
track by authoring the `.drt` SubtitleTrackVec directly, since Resolve's FCPXML
importer ignores <caption> elements (see live_subtitle_probe.py / 3.2.4).

The load-bearing piece is `author_subtitle_effblob()`: it rewrites a subtitle
cue's <EffectFiltersBA> (zstd-compressed protobuf wrapping a BMD 4-byte-LE inner
struct that holds the UTF-16LE cue text) with arbitrary-length text. Correctness
is pinned by an OFFLINE ORACLE: re-encoding the real cue1 sample with cue2's text
must reproduce cue2's real blob byte-for-byte. Two gotchas the oracle guards:
  - zstd framing must match BMD exactly: level 3, content-size, NO checksum
    (python `zstandard`); the zstd CLI's windowed+checksum frame is rejected by
    Resolve and the cue silently resets to the default "Subtitle" name.
  - changing text length cascades through nested protobuf LEN varints AND the
    BMD inner 4-byte-LE length fields; both are recomputed on re-serialize.

Phases:
  oracle                       - offline: validate the codec against ground truth. No Resolve.
  author <template.drt> <in.srt> - author a new .drt from an SRT, reusing the subtitle
                                 cue in template.drt as the per-cue blob template. The
                                 template must already contain a subtitle track (produce
                                 one with live_subtitle_probe.py setup + a manual GUI cue).
  import <authored.drt>        - reimport + read the cues back via the live API (needs Resolve).

Run: .venv/bin/python tests/live_srt_import_probe.py oracle
     .venv/bin/python tests/live_srt_import_probe.py author /tmp/.../variant.drt in.srt
     .venv/bin/python tests/live_srt_import_probe.py import /tmp/.../from_srt.drt

Requires the `zstandard` package (exact-framing zstd; not a repo dependency —
install into .venv only for this probe).
"""

from __future__ import annotations

import binascii
import glob
import os
import re
import shutil
import sys
import uuid
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover - probe dependency
    zstd = None

# ------------------------------------------------------------------ codec ----

def _zc(data: bytes) -> bytes:
    """Compress with BMD's exact framing (level 3, content-size, no checksum)."""
    params = zstd.ZstdCompressionParameters.from_level(
        3, write_content_size=True, write_checksum=False)
    return zstd.ZstdCompressor(compression_params=params).compress(data)


def _zd(data: bytes) -> bytes:
    return zstd.ZstdDecompressor().decompress(data)


def _rdvar(b, o):
    v = s = 0
    n = 0
    while True:
        c = b[o + n]
        v |= (c & 0x7F) << s
        n += 1
        if not c & 0x80:
            break
        s += 7
    return v, o + n


def _wrvar(v):
    out = bytearray()
    while True:
        c = v & 0x7F
        v >>= 7
        out.append(c | (0x80 if v else 0))
        if not v:
            break
    return bytes(out)


def _parse(b, start, end):
    """Protobuf wire parse. LEN payloads recurse when they re-serialize cleanly,
    else stay opaque bytes (the BMD inner struct)."""
    nodes = []
    o = start
    while o < end:
        tag, o = _rdvar(b, o)
        field, wt = tag >> 3, tag & 7
        if wt == 0:
            v, o = _rdvar(b, o)
            nodes.append((field, wt, v))
        elif wt == 2:
            ln, o = _rdvar(b, o)
            chunk = b[o:o + ln]
            o += ln
            sub = _try_parse(chunk)
            nodes.append((field, wt, sub if sub is not None else chunk))
        elif wt == 5:
            nodes.append((field, wt, b[o:o + 4]))
            o += 4
        elif wt == 1:
            nodes.append((field, wt, b[o:o + 8]))
            o += 8
        else:
            raise ValueError(f"bad wiretype {wt} at {o}")
    return nodes


def _try_parse(chunk):
    if len(chunk) < 2:
        return None
    try:
        nodes = _parse(chunk, 0, len(chunk))
        if _ser(nodes) == chunk:
            return nodes
    except Exception:
        pass
    return None


def _ser(nodes):
    out = bytearray()
    for field, wt, val in nodes:
        out += _wrvar((field << 3) | wt)
        if wt == 0:
            out += _wrvar(val)
        elif wt == 2:
            body = _ser(val) if isinstance(val, list) else val
            out += _wrvar(len(body)) + body
        else:
            out += val
    return bytes(out)


def _patch_bmd_text(raw, old_u16, new_u16):
    """Replace the UTF-16LE text in a BMD leaf and bump every enclosing
    4-byte-LE length field (any whose declared span covers the text)."""
    p = raw.find(old_u16)
    if p < 0:
        return None
    delta = len(new_u16) - len(old_u16)
    b = bytearray(raw)
    tstart, tend = p, p + len(old_u16)
    for pos in range(0, p - 3):
        v = int.from_bytes(b[pos:pos + 4], "little")
        if 0 < v <= len(raw) and pos + 4 <= tstart and pos + 4 + v >= tend:
            b[pos:pos + 4] = (v + delta).to_bytes(4, "little")
    b[p:p + len(old_u16)] = new_u16
    return bytes(b)


def _edit_text(nodes, old_u16, new_u16):
    changed = False
    for idx, (field, wt, val) in enumerate(nodes):
        if wt != 2:
            continue
        if isinstance(val, list):
            if _edit_text(val, old_u16, new_u16):
                changed = True
        elif isinstance(val, (bytes, bytearray)) and old_u16 in val:
            nodes[idx] = (field, wt, _patch_bmd_text(val, old_u16, new_u16))
            changed = True
    return changed


def _decompress_effblob(hex_blob):
    raw = binascii.unhexlify(hex_blob)
    i = raw.find(b"\x28\xb5\x2f\xfd")
    return _zd(raw[i:])


def author_subtitle_effblob(template_hex, template_text, new_text):
    """Return a new <EffectFiltersBA> hex payload carrying `new_text`, built from
    `template_hex` (a real subtitle cue's EffectFiltersBA) whose current text is
    `template_text`. Handles arbitrary text length."""
    dec = _decompress_effblob(template_hex)
    tree = _parse(dec, 0, len(dec))
    assert _ser(tree) == dec, "protobuf roundtrip mismatch"
    if not _edit_text(tree, template_text.encode("utf-16-le"),
                      new_text.encode("utf-16-le")):
        raise ValueError("template text not found in blob")
    body = b"\x81" + _zc(_ser(tree))
    return (binascii.unhexlify("00000002")
            + len(body).to_bytes(4, "big") + body).hex().upper()


# --------------------------------------------------------- ground truth ------
# Two real subtitle cue EffectFiltersBA blobs exported from Resolve 21.0.2.4
# (live_subtitle_probe.py). cue1 text="HELLO ALPHA CUE", cue2="SECOND BRAVO CUE".
_CUE1 = ("00000002000000a48128b52ffd602d00cd0400f24a212b804b330726311223153058f3df6"
         "20662fd1f51f8bdc336e83928d1c9ef444200ec9c98ad3d5c3ac046640aa753d6e2b6e216"
         "9bd17ac113261348e36d3812019111078e272a9c2044718cdaabb5a9cd5eac6ef0866534e"
         "2c3e6530a91fa6c56a9d4a874e70b0118eece7ca0c42bf67748c0298e02b98ee73e105c85"
         "592ad303e7c12c202f05001b30a979185c9d8bc4a24a88c9ee08")
_CUE2 = ("00000002000000a48128b52ffd602f00cd0400c2ca2029802d660e26311223d54c92b418e"
         "d016efe6f99b1df96449caac9b593461ed0461c02707428cddb2d53a15095e375e395fa61"
         "70050c7b202e64016040652542e48c0c2ff834eed13a9536add58ed10f0eb17c5a7436a31"
         "725567c9b1329469d4fe90d7718f0f076e604314ea9bb53049eb5ec3512d74a129a09b39c"
         "4011da08b3a0b806000d18f9d06a1183ab739160560931d91d01")


def phase_oracle() -> int:
    if zstd is None:
        print("zstandard not installed — exit 2")
        return 2
    # 1. re-encoding cue1 with cue2's text must byte-match cue2's real blob.
    rebuilt = author_subtitle_effblob(_CUE1, "HELLO ALPHA CUE", "SECOND BRAVO CUE")
    ok_exact = binascii.unhexlify(rebuilt) == binascii.unhexlify(_CUE2.upper())
    print(f"oracle exact-match (cue1+'SECOND BRAVO CUE' == cue2): {ok_exact}")
    # 2. arbitrary lengths decompress + carry the intended text.
    for text in ("Hi.", "A considerably longer subtitle line to stress lengths.",
                 "Unicode: café — naïve — 日本語"):
        blob = author_subtitle_effblob(_CUE1, "HELLO ALPHA CUE", text)
        dec = _decompress_effblob(blob)
        got = text.encode("utf-16-le") in dec
        print(f"  len {len(text):>3}  text-embedded={got}  {text!r}")
    return 0 if ok_exact else 1


# ------------------------------------------------------------- authoring -----
FPS = 24  # probe default; a real tool must read timeline fps + start frame


def _parse_srt(path):
    cues = []
    blocks = re.split(r"\n\s*\n", open(path, encoding="utf-8").read().strip())
    for blk in blocks:
        lines = [ln for ln in blk.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*"
                      r"(\d+):(\d+):(\d+)[,.](\d+)", lines[1])
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, m.groups())
        st = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
        en = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
        cues.append((st, en, " ".join(lines[2:])))
    return cues


def phase_author(template_drt, srt_path) -> int:
    if zstd is None:
        print("zstandard not installed — exit 2")
        return 2
    workdir = os.path.join(os.path.dirname(os.path.abspath(template_drt)),
                           "srt_probe_work")
    shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir)
    with zipfile.ZipFile(template_drt) as z:
        names = z.namelist()
        z.extractall(workdir)
    seqp = glob.glob(os.path.join(workdir, "SeqContainer", "*.xml"))[0]
    xml = open(seqp, encoding="utf-8").read()

    gen = re.search(r"<Sm2TiGenerator DbId=\"[0-9a-f-]+\">.*?</Sm2TiGenerator>",
                    xml, re.S)
    if not gen:
        print("template.drt has no subtitle cue to model on — exit 1")
        return 1
    tmpl_gen = gen.group(0)
    tmpl_eff = re.search(r"<EffectFiltersBA>([0-9A-Fa-f]+)</EffectFiltersBA>",
                         tmpl_gen).group(1)
    tmpl_text = "HELLO ALPHA CUE"  # the phrase live_subtitle_probe instructs

    start_frame = 86400  # probe assumption; a real tool reads GetStartFrame()

    def make_gen(sf, df, text):
        g = re.sub(r"<Sm2TiGenerator DbId=\"[0-9a-f-]+\">",
                   f'<Sm2TiGenerator DbId="{uuid.uuid4()}">', tmpl_gen)
        g = re.sub(r"<Start>\d+</Start>", f"<Start>{sf}</Start>", g)
        g = re.sub(r"<Duration>\d+</Duration>", f"<Duration>{df}</Duration>", g)
        eff = author_subtitle_effblob(tmpl_eff, tmpl_text, text)
        return re.sub(r"<EffectFiltersBA>[0-9A-Fa-f]+</EffectFiltersBA>",
                      f"<EffectFiltersBA>{eff}</EffectFiltersBA>", g)

    cues = _parse_srt(srt_path)
    elems = []
    for st, en, text in cues:
        sf = start_frame + round(st * FPS)
        df = max(1, round((en - st) * FPS))
        elems.append(f"<Element>{make_gen(sf, df, text)}</Element>")

    si, sj = xml.find("<SubtitleTrackVec>"), xml.find("</SubtitleTrackVec>")
    block = re.sub(r"<Items>.*?</Items>", "<Items>" + "".join(elems) + "</Items>",
                   xml[si:sj], count=1, flags=re.S)
    xml2 = xml[:si] + block + xml[sj:]
    open(seqp, "w", encoding="utf-8").write(xml2)

    out = os.path.join(os.path.dirname(os.path.abspath(template_drt)), "from_srt.drt")
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.write(os.path.join(workdir, n), n)
    print(f"authored {len(cues)} cues -> {out}")
    for st, en, t in cues:
        print(f"  {st:6.2f}-{en:6.2f}s  ({len(t):>3}ch) {t!r}")
    return 0


def phase_import(drt_path) -> int:
    import src.server as s
    r = s.get_resolve()
    if r is None:
        print("Resolve not available — exit 2")
        return 2
    pm = r.GetProjectManager()
    proj = pm.GetCurrentProject()
    mp = proj.GetMediaPool()
    tl = mp.ImportTimelineFromFile(
        drt_path, {"timelineName": "srt_import_probe", "importSourceClips": False})
    if tl is None:
        print("import failed — exit 1")
        return 1
    proj.SetCurrentTimeline(tl)
    import json
    tr = s._timeline_transcript(tl, with_timecodes=True)
    print(f"subtitle tracks: {tl.GetTrackCount('subtitle')}")
    print(json.dumps(tr, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    phase = sys.argv[1] if len(sys.argv) > 1 else "oracle"
    if phase == "oracle":
        return phase_oracle()
    if phase == "author":
        return phase_author(sys.argv[2], sys.argv[3])
    if phase == "import":
        from preflight import gate  # oracle/author phases are offline — only gate here
        gate("project")
        return phase_import(sys.argv[2])
    print(f"unknown phase {phase!r} (oracle|author|import)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
