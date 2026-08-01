"""montage_motion — beat-locked Fusion motion, flash, and grain/vignette
directives for the montage genre (issue #180, phase 5/6 of the
montage-quality epic).

Pure planning + expression generation: no Resolve, no I/O. montage_edit.py
calls compute_motion_directive() to fill a grid-locked segment's `motion`
field (phase 2's placeholder); actions.py's finish() reads it back and turns
it into a Fusion Transform expression via build_zoom_expression(), applied
through fusion_comp(action="bulk_set_expressions") in ONE call for every
clip — never one call per clip.

Desktop's ffmpeg zoompan formula (a ramp + a decaying pulse locked to the
master beat, plus a lighter offbeat-8th pulse) is the model — ported onto a
Fusion Transform's Size input, which is GPU-accelerated, non-destructive,
and re-gradable, unlike ffmpeg's baked pixels.

The phase trap (read this before touching the formula): a clip's Fusion comp
`time` is COMP-LOCAL (0 at the clip's own first frame), not the timeline's
global frame. That is fine here and needs NO correction term, because every
grid-locked montage segment starts exactly ON a beat: comp-local frame 0 IS a
beat, and the beats after it sit at exact multiples of the beat period, so
`fmod(time, beat_frames)` is already the timeline's own beat phase.

These expressions used to add `record_start_frame` into the modulo phase to
"lock to the master grid". That was backwards and it cost the headline
feature its accuracy (#193 phase 3): a segment's start frame is congruent to
`beat_zero * fps` modulo the beat period, so adding it shifted every peak by a
constant `beat_zero` — up to half a beat off the beat the pulse is named
after. Dropping the term is what makes the lock real. Verified live on Resolve
Studio 21.0.2.4: `time` in a Fusion expression is exactly the frame argument
passed to get_input/SetExpression, and `fmod`/`exp` behave as the C stdlib
versions.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# Zoom range per arrangement section (montage_arrangement.SECTION_CUT_BEATS'
# vocabulary) — held tension in intro/breathe, most aggressive on drop/high.
MOTION_ZOOM_RANGE: Dict[str, Tuple[float, float]] = {
    "intro": (1.0, 1.03),
    "build": (1.0, 1.05),
    "drop": (1.0, 1.08),
    "high": (1.02, 1.06),
    "mid": (1.0, 1.03),
    "low": (1.0, 1.02),
    "breathe": (1.0, 1.01),
    "accelerate": (1.02, 1.06),
    "outro": (1.0, 1.02),
}
DEFAULT_ZOOM_RANGE = (1.0, 1.02)

# Per-shot zoom variation (#193 phase 6.2.2). Every shot in a section used to
# get the IDENTICAL range and every move was a push in — no pull-outs, no
# variation, which reads as mechanical however well the cuts land.
#
# The variation is a deterministic function of the shot's index, never random:
# a plan is a saved, re-loadable artifact that a revision re-derives, so the
# same cut must produce the same move every time it is planned. The cycle
# alternates direction and scales the span, and it starts on a push so the
# hook still opens by pushing in.
#
# Pull-outs are safe: every range in MOTION_ZOOM_RANGE sits at or above 1.0,
# so reversing one eases from zoomed-in back to frame rather than under-scanning
# past the edges.
ZOOM_VARIATION_CYCLE: Tuple[Tuple[bool, float], ...] = (
    (False, 1.00),   # push in, full span
    (True, 0.85),    # pull out, slightly gentler
    (False, 1.20),   # push in, further
    (True, 1.00),    # pull out, full span
    (False, 0.75),   # push in, subtle
    (True, 1.20),    # pull out, further
)

# Decaying beat-pulse amplitude per section — opt-IN, not opt-out (#209).
# Ported wholesale from the Claude Desktop ffmpeg baseline, which ran the
# pulse across its whole build; doing the same here meant every section not
# listed fell through to a "default" amplitude that was really a permanent
# throb — 76 of 107 segments on a real 134s/108bpm track pulsed purely
# because nothing had opted them OUT, which both reads as a tic over 2+
# minutes and erases the contrast the drop is supposed to have against
# quieter material. This list is now exhaustive: only `drop` and `high` are
# genuine peaks. `build`/`accelerate` are deliberately NOT here — they
# already carry their own energy (MONTAGE_RETIME_SPEED slows them, and the
# zoom ramp itself already accelerates), so a pulse under a section named
# for tension read as arriving too early rather than earning the drop.
MOTION_PULSE_AMP: Dict[str, float] = {
    "drop": 0.06,
    "high": 0.05,
}
DEFAULT_PULSE_AMP = 0.0

FLASH_GAIN = 1.6          # brightness multiplier at the flash's peak
FLASH_DECAY_FRAMES = 3.0  # the lift is gone within ~3 frames

# Shake (phase 2's `shake` flag — the drop and `high` sections). Applied to the
# beat-pulse Transform's ANGLE, not its Center: Center is a Point input, and
# this codebase's own bridge notes (fusion_composition._fusion_set_point_input)
# record that Point inputs accept different encodings per platform/version, so
# an expression on one is not a safe bet. Angle is a plain scalar like Size and
# Gain — the two inputs whose expressions are already live-verified on 21.0.2.4.
# A rotational jitter reads as camera shake and costs nothing extra: the
# Transform tool is already in the chain whenever motion is on.
SHAKE_MAX_DEGREES = 0.8   # peak rotation at the beat
SHAKE_DECAY = 6.0         # e-folds per beat — gone well before the next one
SHAKE_OSCILLATIONS = 2.2  # jitter cycles per frame of phase

FADEOUT_SECONDS = 1.0     # fade-to-black length at the tail of the outro

# Speed for the `retime` flag, by section. Read by auto_edit.plan_polish_ops,
# which emits the drp-format `retime_clip` op — the scripting API cannot change
# clip speed at all (SetProperty("Speed") returns False on 21.x), so the ramp is
# authored in the .drt round trip while finish() only sets the interpolation
# quality.
#
# Both values are BELOW 1 on purpose, and not merely for taste. The op holds the
# record duration at the segment's beat-locked length, so speed is what decides
# how much SOURCE the slot consumes: at 0.5 a slot eats half the frames it
# already owns (always available), while anything above 1 would need handles the
# CutList never reserved. Slow motion is also the montage move these sections
# want — shots lingering while the cuts get shorter.
MONTAGE_RETIME_SPEED: Dict[str, float] = {
    "build": 0.5,        # tension: holds stretch as the cut length ramps 4 -> 2
    "accelerate": 0.4,   # the pre-drop 1-beat run reads as frozen moments
}
DEFAULT_RETIME_SPEED = 0.5


def compute_motion_directive(
    section: Optional[str], *, beat_seconds: float, variation_index: int = 0,
) -> Dict[str, Any]:
    """The ``{zoom_start, zoom_end, amp, beat_seconds}`` directive for a
    grid-locked segment's ``motion`` field. Only meaningful when the beat
    grid is confident (``beat_seconds > 0``) — callers leave ``motion: None``
    for fallback-mode segments, where there is no reliable beat to lock to.

    ``variation_index`` (the segment's own index) selects this shot's entry in
    ``ZOOM_VARIATION_CYCLE`` so the move alternates push/pull and changes
    magnitude across a section instead of repeating one identical push.
    Deterministic by design — a plan is re-derived on every revision and must
    produce the same cut each time.
    """
    zoom_start, zoom_end = MOTION_ZOOM_RANGE.get(section or "", DEFAULT_ZOOM_RANGE)
    # Vary the move per shot so a section isn't N identical pushes (#193).
    reverse, scale = ZOOM_VARIATION_CYCLE[int(variation_index) % len(ZOOM_VARIATION_CYCLE)]
    span = (zoom_end - zoom_start) * scale
    zoom_start, zoom_end = ((zoom_start + span, zoom_start) if reverse
                            else (zoom_start, zoom_start + span))
    zoom_start, zoom_end = round(zoom_start, 6), round(zoom_end, 6)
    amp = MOTION_PULSE_AMP.get(section or "", DEFAULT_PULSE_AMP)
    return {
        "zoom_start": zoom_start,
        "zoom_end": zoom_end,
        "amp": amp,
        "beat_seconds": round(beat_seconds, 6),
    }


def build_zoom_expression(
    *,
    zoom_start: float,
    zoom_end: float,
    amp: float,
    beat_seconds: float,
    fps: float,
    clip_length_frames: int,
) -> str:
    """Fusion expression for a Transform's ``Size`` input: a zoom ramp
    (``zoom_start`` -> ``zoom_end`` across the clip's own length) plus, when
    ``amp`` is positive, a decaying pulse on the beat with a lighter
    offbeat-8th pulse — Desktop's ffmpeg zoompan formula, ported.

    The phase is plain comp-local ``time``: a grid-locked segment starts on a
    beat, so ``time`` is already the beat phase (see the module docstring —
    the old ``record_start_frame`` term put every peak a constant ``beat_zero``
    off the beat).

    ``amp <= 0`` (a section not in ``MOTION_PULSE_AMP``, or the ``pulse``
    host override) omits the pulse term from the expression ENTIRELY (#209)
    — not a zero-amplitude pulse term left in place, which would still show
    up in the emitted expression even though it evaluates to nothing. The
    ramp is unaffected either way: restraint on the pulse costs no life in
    the image.
    """
    beat_frames = beat_seconds * fps
    if beat_frames <= 0:
        beat_frames = 1.0
    ramp_rate = (zoom_end - zoom_start) / max(1, clip_length_frames)
    if amp <= 0:
        return f"{zoom_start:.6f}+{ramp_rate:.8f}*time"
    phase = "time"
    pulse = (
        f"{amp:.6f}*exp(-7*fmod({phase},{beat_frames:.6f})/{beat_frames:.6f})"
        f"+{amp * 0.42:.6f}*exp(-9*fmod({phase}+{beat_frames / 2.0:.6f},"
        f"{beat_frames:.6f})/{beat_frames:.6f})"
    )
    return f"{zoom_start:.6f}+{ramp_rate:.8f}*time+{pulse}"


def build_flash_expression() -> str:
    """BrightnessContrast ``Gain`` expression: a brief lift at the clip's own
    first frame — a flash on a section-opening downbeat (phase 2's ``flash``
    flag). Comp-local time already starts at 0 exactly there, so unlike the
    beat pulse this needs no ``record_start_frame`` offset: a flash is a
    one-shot at the cut itself, not locked to a repeating grid position.
    """
    return f"1.0+{FLASH_GAIN - 1.0:.4f}*exp(-{20.0 / FLASH_DECAY_FRAMES:.4f}*time)"


def build_shake_expression(
    *, beat_seconds: float, fps: float,
    amp_degrees: float = SHAKE_MAX_DEGREES,
) -> str:
    """Fusion expression for a Transform's ``Angle`` input: a rotational
    jitter that spikes on each beat and decays before the next one (phase 2's
    ``shake`` flag).

    On the beat the same way ``build_zoom_expression`` is: the phase is plain
    comp-local ``time``, which is already the beat phase because every
    grid-locked segment starts on a beat (see the module docstring).
    """
    beat_frames = beat_seconds * fps
    if beat_frames <= 0:
        beat_frames = 1.0
    phase = "time"
    return (
        f"{amp_degrees:.6f}"
        f"*exp(-{SHAKE_DECAY:.4f}*fmod({phase},{beat_frames:.6f})/{beat_frames:.6f})"
        f"*sin({phase}*{SHAKE_OSCILLATIONS:.4f})"
    )


def build_fadeout_expression(*, fps: float, clip_length_frames: int) -> str:
    """BrightnessContrast ``Gain`` expression: a ramp to black over the last
    ``FADEOUT_SECONDS`` of the clip (phase 2's ``fadeout`` flag, set on the
    final entry of the outro).

    Comp-local time starts at 0 on the clip's own first frame, so this needs no
    ``record_start_frame`` offset — the fade is anchored to the clip's tail, not
    to a grid position. The fade is clamped to the clip's own length so a very
    short outro shot fades across all of itself rather than starting mid-black.
    """
    fade_frames = max(1.0, min(float(FADEOUT_SECONDS * fps), float(max(1, clip_length_frames))))
    fade_start = max(0.0, float(clip_length_frames) - fade_frames)
    return f"min(1,max(0,({fade_start + fade_frames:.4f}-time)/{fade_frames:.4f}))"
