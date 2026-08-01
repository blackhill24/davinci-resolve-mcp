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
global frame. build_zoom_expression bakes `record_start_frame` (phase 2
guarantees this is an exact beat-grid frame) into the modulo phase so the
pulse locks to the TIMELINE's beat grid, not to each clip's own arbitrary
start. Verified live on Resolve Studio 21.0.2.4: `time` in a Fusion
expression is exactly the frame argument passed to get_input/SetExpression,
and `fmod`/`exp` behave as the C stdlib versions — see the phase-5 PR
description for the read-back values that proved a pulse peaks exactly on
the beat and resets cleanly at the next one.
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

# Decaying beat-pulse amplitude per section — bigger on the sections phase 2
# already reads as high-energy (the same sections that get the `flash`/
# `shake` flags).
MOTION_PULSE_AMP: Dict[str, float] = {
    "drop": 0.06,
    "high": 0.05,
    "accelerate": 0.04,
    "build": 0.03,
}
DEFAULT_PULSE_AMP = 0.02

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


def compute_motion_directive(section: Optional[str], *, beat_seconds: float) -> Dict[str, Any]:
    """The ``{zoom_start, zoom_end, amp, beat_seconds}`` directive for a
    grid-locked segment's ``motion`` field. Only meaningful when the beat
    grid is confident (``beat_seconds > 0``) — callers leave ``motion: None``
    for fallback-mode segments, where there is no reliable beat to lock to.
    """
    zoom_start, zoom_end = MOTION_ZOOM_RANGE.get(section or "", DEFAULT_ZOOM_RANGE)
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
    record_start_frame: int,
    clip_length_frames: int,
) -> str:
    """Fusion expression for a Transform's ``Size`` input: a zoom ramp
    (``zoom_start`` -> ``zoom_end`` across the clip's own length) plus a
    decaying pulse locked to the MASTER beat grid, with a lighter offbeat-8th
    pulse — Desktop's ffmpeg zoompan formula, ported (see module docstring
    for the comp-local-time phase offset this bakes in via
    ``record_start_frame``).
    """
    beat_frames = beat_seconds * fps
    if beat_frames <= 0:
        beat_frames = 1.0
    ramp_rate = (zoom_end - zoom_start) / max(1, clip_length_frames)
    phase = f"(time+{int(record_start_frame)})"
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
    *, beat_seconds: float, fps: float, record_start_frame: int,
    amp_degrees: float = SHAKE_MAX_DEGREES,
) -> str:
    """Fusion expression for a Transform's ``Angle`` input: a rotational
    jitter that spikes on each beat and decays before the next one (phase 2's
    ``shake`` flag).

    Locked to the MASTER beat grid the same way ``build_zoom_expression`` is —
    ``record_start_frame`` is baked into the modulo phase because a clip's comp
    ``time`` is COMP-LOCAL (see the module docstring's phase trap).
    """
    beat_frames = beat_seconds * fps
    if beat_frames <= 0:
        beat_frames = 1.0
    phase = f"(time+{int(record_start_frame)})"
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
