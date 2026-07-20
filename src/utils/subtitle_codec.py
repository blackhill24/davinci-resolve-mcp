"""Subtitle cue codec + SRT→.drt authoring (issues #22/#30, 3.2.5 + 3.2.6).

Productization of the oracle-validated codec proven in
``tests/live_srt_import_probe.py``: a Resolve subtitle cue is an
``Sm2TiGenerator`` inside ``<SubtitleTrackVec>`` whose TIMING is plain XML
(``<Start>``/``<Duration>``, frames) and whose TEXT + STYLE live in
``<EffectFiltersBA>`` — ``00000002`` + 4-byte-BE length + ``0x81`` + a zstd
frame wrapping a protobuf envelope around a BMD 4-byte-LE inner struct.

Codec invariants (guarded by the offline oracle in tests/test_subtitle_codec.py):
  - zstd framing must be BMD-exact: level 3, write_content_size=True,
    write_checksum=False. Any other framing is rejected by Resolve and the cue
    silently resets to the default "Subtitle" name.
  - text-length changes cascade through nested protobuf LEN varints AND the BMD
    inner 4-byte-LE length fields; both are recomputed on re-serialize.

Style (3.2.6): the same BMD leaf carries, after the cue text —
  [u32-LE len]["FontName" UTF-16LE][float32-LE size][u32-LE len]["#rrggbb" UTF-16LE]
Font name replacement rides the same length cascade as text; size is a fixed
4-byte float directly after the font name (58.0f verified at exactly font_end in
both ground-truth blobs); color is a "#rrggbb" UTF-16LE string swap.

``zstandard`` is required for any blob rewrite (not a hard repo dependency) —
callers must check ``zstd_available()`` and refuse honestly.
"""

from __future__ import annotations

import binascii
import re
import struct
from typing import Any, Dict, List, Optional, Tuple

try:
    import zstandard as _zstd
except ImportError:  # pragma: no cover - optional dependency
    _zstd = None


def zstd_available() -> bool:
    return _zstd is not None


# ------------------------------------------------------------------ zstd ----

def _zc(data: bytes) -> bytes:
    """Compress with BMD's exact framing (level 3, content-size, no checksum)."""
    params = _zstd.ZstdCompressionParameters.from_level(
        3, write_content_size=True, write_checksum=False)
    return _zstd.ZstdCompressor(compression_params=params).compress(data)


def _zd(data: bytes) -> bytes:
    return _zstd.ZstdDecompressor().decompress(data)


# -------------------------------------------------------- protobuf wire ----

def _rdvar(b: bytes, o: int) -> Tuple[int, int]:
    v = s = n = 0
    while True:
        c = b[o + n]
        v |= (c & 0x7F) << s
        n += 1
        if not c & 0x80:
            break
        s += 7
    return v, o + n


def _wrvar(v: int) -> bytes:
    out = bytearray()
    while True:
        c = v & 0x7F
        v >>= 7
        out.append(c | (0x80 if v else 0))
        if not v:
            break
    return bytes(out)


def _parse(b: bytes, start: int, end: int) -> list:
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


def _try_parse(chunk: bytes):
    if len(chunk) < 2:
        return None
    try:
        nodes = _parse(chunk, 0, len(chunk))
        if _ser(nodes) == chunk:
            return nodes
    except Exception:
        pass
    return None


def _ser(nodes: list) -> bytes:
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


# ------------------------------------------------------- BMD leaf edits ----

def _patch_bmd_string(raw: bytes, old_u16: bytes, new_u16: bytes) -> Optional[bytes]:
    """Replace a UTF-16LE string in a BMD leaf and bump every enclosing
    4-byte-LE length field (any whose declared span covers the string)."""
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


def _edit_string(nodes: list, old_u16: bytes, new_u16: bytes) -> bool:
    changed = False
    for idx, (field, wt, val) in enumerate(nodes):
        if wt != 2:
            continue
        if isinstance(val, list):
            if _edit_string(val, old_u16, new_u16):
                changed = True
        elif isinstance(val, (bytes, bytearray)) and old_u16 in val:
            nodes[idx] = (field, wt, _patch_bmd_string(bytes(val), old_u16, new_u16))
            changed = True
    return changed


def _edit_float_after(nodes: list, anchor_u16: bytes, value: float) -> bool:
    """Overwrite the float32 sitting directly after `anchor_u16` in a BMD leaf
    (the font-size slot follows the font name with no null terminator — verified
    against the ground-truth blobs: 58.0f at exactly font_end)."""
    changed = False
    for idx, (field, wt, val) in enumerate(nodes):
        if wt != 2:
            continue
        if isinstance(val, list):
            if _edit_float_after(val, anchor_u16, value):
                changed = True
        elif isinstance(val, (bytes, bytearray)) and anchor_u16 in val:
            b = bytearray(val)
            p = b.find(anchor_u16) + len(anchor_u16)
            if p + 4 <= len(b):
                b[p:p + 4] = struct.pack("<f", float(value))
                nodes[idx] = (field, wt, bytes(b))
                changed = True
    return changed


def _leaf_bytes(nodes: list) -> List[bytes]:
    out = []
    for _field, wt, val in nodes:
        if wt != 2:
            continue
        if isinstance(val, list):
            out.extend(_leaf_bytes(val))
        elif isinstance(val, (bytes, bytearray)) and len(val) > 0:
            out.append(bytes(val))
    return out


# --------------------------------------------------------- blob wrapper ----

def _decompress_effblob(hex_blob: str) -> bytes:
    raw = binascii.unhexlify(hex_blob)
    i = raw.find(b"\x28\xb5\x2f\xfd")
    if i < 0:
        raise ValueError("no zstd frame in EffectFiltersBA payload")
    return _zd(raw[i:])


def _wrap_effblob(serialized: bytes) -> str:
    body = b"\x81" + _zc(serialized)
    return (binascii.unhexlify("00000002")
            + len(body).to_bytes(4, "big") + body).hex().upper()


# ----------------------------------------------------------- public API ----

_U16_STR = re.compile(rb"(?:[\x20-\x7e][\x00]){3,}")


def read_cue_style(template_hex: str) -> Dict[str, Any]:
    """Best-effort read of a cue blob's style fields: font, size, color.

    The style block follows the strings in the BMD leaves:
    font name (UTF-16LE) + null + float32 size + len + "#rrggbb" (UTF-16LE).
    """
    dec = _decompress_effblob(template_hex)
    tree = _parse(dec, 0, len(dec))
    style: Dict[str, Any] = {}
    for leaf in _leaf_bytes(tree):
        # The real style color sits at the leaf TAIL (…[font][size][len][#rrggbb]).
        # Take the LAST match, not the first: cue text may contain a #rrggbb-shaped
        # substring ("Error #FF0000", "Room #123456") that would otherwise shadow
        # the actual color and cascade into bogus font/size reads.
        color_matches = list(re.finditer(rb"(?:\x23\x00(?:[0-9a-fA-F]\x00){6})", leaf))
        if not color_matches:
            continue
        color_m = color_matches[-1]
        style["color"] = leaf[color_m.start():color_m.end()].decode("utf-16-le")
        strings = [m for m in _U16_STR.finditer(leaf) if m.end() <= color_m.start()]
        if strings:
            font_m = strings[-1]
            style["font"] = leaf[font_m.start():font_m.end()].decode("utf-16-le")
            size_at = font_m.end()  # float32 follows the font name directly
            if size_at + 4 <= len(leaf):
                style["size"] = round(struct.unpack_from("<f", leaf, size_at)[0], 3)
        break
    return style


def author_cue_effblob(
    template_hex: str,
    template_text: str,
    new_text: str,
    *,
    font: Optional[str] = None,
    size: Optional[float] = None,
    color: Optional[str] = None,
) -> str:
    """Build a new <EffectFiltersBA> hex payload carrying `new_text` (and optional
    style overrides), from `template_hex` — a real subtitle cue's EffectFiltersBA
    whose current text is `template_text`. Handles arbitrary text length.

    Style overrides (3.2.6): `font` replaces the template's font name (any
    length, same cascade as text), `size` overwrites the float32 slot after the
    font name, `color` swaps the "#rrggbb" string (must be #rrggbb form).
    """
    if _zstd is None:
        raise RuntimeError("zstandard is required for subtitle blob authoring")
    dec = _decompress_effblob(template_hex)
    tree = _parse(dec, 0, len(dec))
    if _ser(tree) != dec:
        raise ValueError("protobuf roundtrip mismatch — blob layout not understood")
    if not _edit_string(tree, template_text.encode("utf-16-le"),
                        new_text.encode("utf-16-le")):
        raise ValueError("template text not found in blob")

    current = read_cue_style(template_hex)
    if color is not None:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("color must be '#rrggbb'")
        old_color = current.get("color")
        if not old_color:
            raise ValueError("template blob has no color field to replace")
        _edit_string(tree, old_color.encode("utf-16-le"), color.encode("utf-16-le"))
    active_font = current.get("font")
    if font is not None:
        if not active_font:
            raise ValueError("template blob has no font field to replace")
        _edit_string(tree, active_font.encode("utf-16-le"), font.encode("utf-16-le"))
        active_font = font
    if size is not None:
        if not active_font:
            raise ValueError("template blob has no font field (size slot anchors after it)")
        if not _edit_float_after(tree, active_font.encode("utf-16-le"), float(size)):
            raise ValueError("could not locate the size slot after the font name")
    return _wrap_effblob(_ser(tree))


# --------------------------------------------------------------- SRT -------

def parse_srt(text: str) -> List[Tuple[float, float, str]]:
    """Parse SRT content → [(start_sec, end_sec, text)] (multi-line cues joined
    with a space, matching the probe's behavior)."""
    cues: List[Tuple[float, float, str]] = []
    for blk in re.split(r"\n\s*\n", text.strip()):
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
        body = " ".join(lines[2:]) if len(lines) > 2 else ""
        if body:
            cues.append((st, en, body))
    return cues


# ----------------------------------------------------- .drt XML surgery ----

_GEN_RE = re.compile(r"<Sm2TiGenerator DbId=\"[0-9a-f-]+\">.*?</Sm2TiGenerator>", re.S)
_EFF_RE = re.compile(r"<EffectFiltersBA>([0-9A-Fa-f]+)</EffectFiltersBA>")


def find_template_cue(seq_xml: str) -> Optional[Dict[str, str]]:
    """Locate a template subtitle cue in a SeqContainer XML: the first
    Sm2TiGenerator inside <SubtitleTrackVec> that carries a non-empty
    EffectFiltersBA. Returns {element, eff_hex, text} or None."""
    si = seq_xml.find("<SubtitleTrackVec>")
    sj = seq_xml.find("</SubtitleTrackVec>")
    if si < 0 or sj < 0:
        return None
    block = seq_xml[si:sj]
    for gen in _GEN_RE.finditer(block):
        eff = _EFF_RE.search(gen.group(0))
        if not eff:
            continue
        name = re.search(r"<Name>([\s\S]*?)</Name>", gen.group(0))
        text = name.group(1) if name else ""
        if not text:
            continue
        try:
            dec = _decompress_effblob(eff.group(1))
        except Exception:
            continue
        if text.encode("utf-16-le") in dec:
            return {"element": gen.group(0), "eff_hex": eff.group(1), "text": text}
    return None


def author_subtitle_track(
    seq_xml: str,
    cues: List[Tuple[float, float, str]],
    *,
    fps: float,
    start_frame: int,
    template: Optional[Dict[str, str]] = None,
    style: Optional[Dict[str, Any]] = None,
    mode: str = "replace",
) -> Tuple[str, int]:
    """Author `cues` into the SubtitleTrackVec's first track in `seq_xml`,
    cloned off `template` (default: harvested via find_template_cue).
    mode='replace' (default) swaps the track's Items for the new cues;
    mode='append' keeps the existing cues and adds the new ones after them.
    Returns (new_xml, cue_count)."""
    import uuid as _uuid

    if template is None:
        template = find_template_cue(seq_xml)
    if template is None:
        raise ValueError(
            "timeline has no subtitle cue to use as a template — add one cue first "
            "(GUI, or create_subtitles_from_audio), or pass template_drt")
    style = style or {}

    def make_gen(sf: int, df: int, text: str) -> str:
        g = re.sub(r"<Sm2TiGenerator DbId=\"[0-9a-f-]+\">",
                   f'<Sm2TiGenerator DbId="{_uuid.uuid4()}">', template["element"])
        g = re.sub(r"<Start>\d+</Start>", f"<Start>{sf}</Start>", g)
        g = re.sub(r"<Duration>\d+</Duration>", f"<Duration>{df}</Duration>", g)
        g = re.sub(r"<Name>[\s\S]*?</Name>", lambda _m: f"<Name>{_xml_escape(text)}</Name>", g, count=1)
        eff = author_cue_effblob(
            template["eff_hex"], template["text"], text,
            font=style.get("font"), size=style.get("size"), color=style.get("color"))
        return _EFF_RE.sub(f"<EffectFiltersBA>{eff}</EffectFiltersBA>", g, count=1)

    elems = []
    for st, en, text in cues:
        sf = start_frame + round(st * fps)
        df = max(1, round((en - st) * fps))
        elems.append(f"<Element>{make_gen(sf, df, text)}</Element>")

    if mode not in ("replace", "append"):
        raise ValueError("mode must be 'replace' or 'append'")
    si = seq_xml.find("<SubtitleTrackVec>")
    sj = seq_xml.find("</SubtitleTrackVec>")
    if si < 0 or sj < 0:
        raise ValueError("SeqContainer has no <SubtitleTrackVec>")
    authored = "".join(elems)

    def _items_repl(m: "re.Match[str]") -> str:
        existing = m.group(1) if mode == "append" else ""
        return f"<Items>{existing}{authored}</Items>"

    block, n_subs = re.subn(r"<Items>(.*?)</Items>", _items_repl,
                            seq_xml[si:sj], count=1, flags=re.S)
    if not n_subs:  # first track's Items may be self-closing (<Items/>)
        block = re.sub(r"<Items\s*/>", f"<Items>{authored}</Items>",
                       seq_xml[si:sj], count=1)
    return seq_xml[:si] + block + seq_xml[sj:], len(elems)


# ------------------------------------------------- embedded template -------
# A SYNTHETIC subtitle cue element wrapping the ground-truth blob. Proven live
# (tests/live_import_srt_tool_probe.py Q1 on 21.0.2.4): a minimal Sm2TiGenerator
# inside a synthesized Sm2TiTrack SubType=3 survives .drt reimport with the cue
# text intact — so import_srt needs no pre-existing cue at all.

_TEMPLATE_TEXT = "HELLO ALPHA CUE"
_TEMPLATE_EFF = (
    "00000002000000a48128b52ffd602d00cd0400f24a212b804b330726311223153058f3df6"
    "20662fd1f51f8bdc336e83928d1c9ef444200ec9c98ad3d5c3ac046640aa753d6e2b6e216"
    "9bd17ac113261348e36d3812019111078e272a9c2044718cdaabb5a9cd5eac6ef0866534e"
    "2c3e6530a91fa6c56a9d4a874e70b0118eece7ca0c42bf67748c0298e02b98ee73e105c85"
    "592ad303e7c12c202f05001b30a979185c9d8bc4a24a88c9ee08").upper()


def _builtin_cue_element() -> str:
    return ('<Sm2TiGenerator DbId="9e6b1a58-0000-4000-8000-000000000001">'
            "<FieldsBlob/><PrettyType>Subtitle</PrettyType>"
            f"<Name>{_TEMPLATE_TEXT}</Name>"
            "<Start>86400</Start><Duration>24</Duration>"
            "<MarkersBA/><UiMemento>0</UiMemento><Flags>0</Flags><PriorityIndex>0</PriorityIndex>"
            f"<EffectFiltersBA>{_TEMPLATE_EFF}</EffectFiltersBA>"
            "<RenderTextEnabled>true</RenderTextEnabled></Sm2TiGenerator>")


def builtin_template() -> Dict[str, str]:
    """The embedded per-cue template: {element, eff_hex, text}."""
    return {"element": _builtin_cue_element(), "eff_hex": _TEMPLATE_EFF,
            "text": _TEMPLATE_TEXT}


def builtin_track_block() -> str:
    """A full synthetic <SubtitleTrackVec> block (one empty track) for timelines
    that have none (their exports carry `<SubtitleTrackVec/>`). The <Items> slot
    is left empty on purpose — author_subtitle_track receives the per-cue
    template explicitly via `template=`, so seeding a cue here would leak it into
    the output on mode='append' (it survives _items_repl's existing-content
    branch)."""
    return ("<SubtitleTrackVec><Element>"
            '<Sm2TiTrack DbId="9e6b1a58-0000-4000-8000-000000000002">'
            "<FieldsBlob/><Type>2</Type><SubType>3</SubType><Flags>0</Flags>"
            "<Sequence>9e6b1a58-0000-4000-8000-000000000003</Sequence>"
            "<Items/>"
            "<FusionCompHolderItems/><UserDefinedName/><LayersVec/></Sm2TiTrack>"
            "</Element></SubtitleTrackVec>")


def ensure_subtitle_track(seq_xml: str) -> str:
    """Give `seq_xml` a populated SubtitleTrackVec if it has none, using the
    embedded synthetic track."""
    if "<SubtitleTrackVec/>" in seq_xml:
        return seq_xml.replace("<SubtitleTrackVec/>", builtin_track_block(), 1)
    return seq_xml


def transplant_subtitle_track(seq_xml: str, template_seq_xml: str) -> str:
    """Copy the template SeqContainer's whole <SubtitleTrackVec> block into a
    timeline that has none (real exports carry an empty `<SubtitleTrackVec/>`
    when no subtitle track exists)."""
    si = template_seq_xml.find("<SubtitleTrackVec>")
    sj = template_seq_xml.find("</SubtitleTrackVec>")
    if si < 0 or sj < 0:
        raise ValueError("template timeline has no populated <SubtitleTrackVec>")
    block = template_seq_xml[si:sj + len("</SubtitleTrackVec>")]
    if "<SubtitleTrackVec/>" in seq_xml:
        return seq_xml.replace("<SubtitleTrackVec/>", block, 1)
    if "<SubtitleTrackVec>" in seq_xml:
        return seq_xml  # already has one — nothing to transplant
    raise ValueError("target SeqContainer has no <SubtitleTrackVec/> slot")


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
