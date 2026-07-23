"""Runtime-tool capability detection, install guidance, and analysis-plan building."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform as _platform
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple
from src.domains.media_analysis.utils.sync_detection import detect_sync_event_capabilities

from src.domains.media_analysis.utils.caps_gating import ANALYSIS_VERSION, DEFAULT_DEPTH, DEFAULT_MAX_RELATED_PROJECT_ROOTS, DEFAULT_TRANSCRIPTION_ENABLED, DEFAULT_VISION_ANALYSIS_PROMPT, DEPTHS, FRAME_CAPS, HARD_FRAME_CAP, HOST_CHAT_PATHS_PROVIDER, HOST_CHAT_VISION_PROVIDERS, _coerce_bool
from src.domains.media_analysis.utils.clip_identity_registry import _resolve_sampling_config, analysis_registry_path, normalize_path, related_analysis_project_roots, resolve_clip_directory, resolve_output_root
from src.domains.media_analysis.utils.subtitles_and_reuse import _mark_reuse_blocked, _record_analysis_report_paths, _record_has_analysis_provenance, _report_reuse_score, _why_not_reused, find_reusable_report_across_roots, find_reusable_report_from_path, find_reusable_report_from_registry


# ── Runtime-tool install metadata ────────────────────────────────────────────
# Per-tool install commands keyed by platform. The dashboard reads this through
# detect_capabilities() so each missing-tool chip can render a one-click "Copy"
# or "Ask Claude/Codex" affordance. We never execute installs server-side; the
# user runs the command themselves, or lets their agent (Claude Code / Codex)
# run it with its own confirmation gating.
TOOL_INSTALL: Dict[str, Dict[str, Any]] = {
    "ffprobe": {
        "label": "ffprobe",
        "bundle": "ffmpeg_suite",
        "required_for": ["technical metadata", "scene detection", "sync detection"],
        "commands": {
            "macos": "brew install ffmpeg",
            "linux_debian": "sudo apt install ffmpeg",
            "linux_rhel": "sudo dnf install ffmpeg",
            "linux_arch": "sudo pacman -S ffmpeg",
            "windows": "winget install --id=Gyan.FFmpeg -e",
        },
        "verify": "ffprobe -version",
        "notes": "Bundled with ffmpeg. One install covers both ffprobe and ffmpeg.",
    },
    "ffmpeg": {
        "label": "ffmpeg",
        "bundle": "ffmpeg_suite",
        "required_for": ["frame extraction", "motion analysis", "audio decode for sync"],
        "commands": {
            "macos": "brew install ffmpeg",
            "linux_debian": "sudo apt install ffmpeg",
            "linux_rhel": "sudo dnf install ffmpeg",
            "linux_arch": "sudo pacman -S ffmpeg",
            "windows": "winget install --id=Gyan.FFmpeg -e",
        },
        "verify": "ffmpeg -version",
        "notes": "Bundled with ffprobe. One install covers both.",
    },
    "whisper_cli": {
        "label": "openai-whisper",
        "bundle": "transcription",
        "required_for": ["transcription (CPU/GPU, Python)"],
        "commands": {
            "all": "pip install -U openai-whisper",
        },
        "verify": "whisper --help",
        "notes": "Pure-Python reference implementation. Choose this OR whisper_cpp OR mlx_whisper.",
    },
    "ollama_embeddings": {
        "label": "ollama + nomic-embed-text",
        "bundle": "embeddings",
        "required_for": ["semantic search (text embeddings)", "find_similar"],
        "commands": {
            "macos": "brew install ollama && ollama pull nomic-embed-text",
            "linux": "curl -fsSL https://ollama.com/install.sh | sh && ollama pull nomic-embed-text",
            "windows": "winget install Ollama.Ollama, then: ollama pull nomic-embed-text",
        },
        "verify": "ollama list",
        "notes": "Local embedding model (~270 MB). sentence-transformers is an alternative text backend.",
    },
    "open_clip": {
        "label": "open_clip (CLIP visual embeddings)",
        "bundle": "embeddings",
        "required_for": ["visual similarity (find_similar kind=visual)", "cross-clip entity clustering"],
        "commands": {
            "all": "pip install open_clip_torch",
        },
        "verify": "python -c \"import open_clip\"",
        "notes": "Needs torch. Model weights (~350 MB) download on first use.",
    },
    "clap_audio": {
        "label": "CLAP (audio embeddings)",
        "bundle": "embeddings",
        "required_for": ["audio similarity (find_similar kind=audio)"],
        "commands": {
            "all": "pip install transformers",
        },
        "verify": "python -c \"import transformers\"",
        "notes": (
            "Needs torch + ffmpeg. Uses laion/clap-htsat-unfused (~600 MB, "
            "downloads on first use); the laion_clap package works as an "
            "alternative backend."
        ),
    },
    "whisper_cpp": {
        "label": "whisper.cpp",
        "bundle": "transcription",
        "required_for": ["transcription (fast C++ backend)"],
        "commands": {
            "macos": "brew install whisper-cpp",
            "linux_debian": "Build from source: https://github.com/ggerganov/whisper.cpp",
            "linux_rhel": "Build from source: https://github.com/ggerganov/whisper.cpp",
            "linux_arch": "yay -S whisper.cpp",
            "windows": "Build from source or use WSL: https://github.com/ggerganov/whisper.cpp",
        },
        "verify": "whisper-cli --help",
        "notes": "Fastest CPU option. Choose this OR whisper_cli OR mlx_whisper.",
    },
    "mlx_whisper": {
        "label": "mlx-whisper",
        "bundle": "transcription",
        "required_for": ["transcription on Apple Silicon (MLX backend)"],
        "commands": {
            "macos_apple_silicon": "pip install mlx-whisper",
        },
        "verify": "python -c 'import mlx_whisper'",
        "requires": "apple_silicon",
        "notes": "Apple Silicon only. Choose this OR whisper_cli OR whisper_cpp.",
    },
    "opencv": {
        "label": "opencv-python",
        "required_for": ["optical-flow motion scoring (optional)"],
        "commands": {
            "all": "pip install opencv-python",
        },
        "verify": "python -c 'import cv2'",
        "notes": "Optional. Standard frame-difference motion scoring works without it.",
    },
}


def _runtime_platform_id() -> Tuple[str, str]:
    """Return (platform_id, machine) for install-command resolution.

    platform_id is one of: "macos", "macos_apple_silicon", "linux_debian",
    "linux_rhel", "linux_arch", "linux", "windows", "unknown".
    """
    machine = (_platform.machine() or "").lower()
    if sys.platform == "darwin":
        if machine in ("arm64", "aarch64"):
            return "macos_apple_silicon", machine
        return "macos", machine
    if sys.platform.startswith("win"):
        return "windows", machine
    if sys.platform.startswith("linux"):
        # Detect distro family for the most-likely package manager. Best-effort;
        # the dashboard always shows the resolved command so a wrong guess is a
        # one-click copy-and-tweak, not a silent failure.
        os_release = "/etc/os-release"
        try:
            with open(os_release, "r", encoding="utf-8") as fh:
                data = fh.read().lower()
            if "id=debian" in data or "id_like=debian" in data or "ubuntu" in data:
                return "linux_debian", machine
            if "id=fedora" in data or "rhel" in data or "centos" in data or "id_like=\"rhel" in data:
                return "linux_rhel", machine
            if "id=arch" in data or "manjaro" in data:
                return "linux_arch", machine
        except OSError:
            pass
        return "linux", machine
    return "unknown", machine


def install_plan_for(tool_name: str, platform_id: Optional[str] = None) -> Dict[str, Any]:
    """Return a structured install plan for a single tool.

    The plan resolves the best command for the current platform, includes
    alternates so the UI/agent can offer choices, and surfaces the verify
    command and any platform requirement (e.g. Apple Silicon for mlx_whisper).
    """
    meta = TOOL_INSTALL.get(tool_name)
    if not meta:
        return {"tool": tool_name, "available": False, "command": None, "notes": "No install plan registered."}
    if platform_id is None:
        platform_id, _ = _runtime_platform_id()

    commands = meta.get("commands") or {}
    # Resolution order: exact platform → family fallback → "all" → None.
    # macos_apple_silicon falls through to macos; linux_<distro> falls through
    # to a generic "linux" key. We don't pick a random first command — better to
    # tell the UI we don't know and let it surface "no suggested command".
    resolved_key = None
    if platform_id in commands:
        resolved_key = platform_id
    elif platform_id == "macos_apple_silicon" and "macos" in commands:
        resolved_key = "macos"
    elif platform_id.startswith("linux_") and "linux" in commands:
        resolved_key = "linux"
    elif "all" in commands:
        resolved_key = "all"
    resolved = commands.get(resolved_key) if resolved_key else None

    # Alternates: every other distinct command we know about, keyed for display.
    alternates: Dict[str, str] = {}
    for key, value in commands.items():
        if key == resolved_key:
            continue
        if value == resolved:
            continue  # don't show the same command twice under a different label
        alternates[key] = value

    requires = meta.get("requires")
    requirement_met = True
    requirement_note = None
    if requires == "apple_silicon":
        # Use the resolved platform_id (caller's view of the world) rather than the
        # current process, so a Linux user querying mlx_whisper sees "not for you".
        if platform_id != "macos_apple_silicon":
            requirement_met = False
            requirement_note = "Requires Apple Silicon (arm64 macOS). Use whisper_cli or whisper_cpp on other platforms."

    return {
        "tool": tool_name,
        "label": meta.get("label", tool_name),
        "bundle": meta.get("bundle"),
        "platform_id": platform_id,
        "command": resolved,
        "alternates": alternates,
        "verify": meta.get("verify"),
        "required_for": meta.get("required_for", []),
        "notes": meta.get("notes"),
        "requires": requires,
        "requirement_met": requirement_met,
        "requirement_note": requirement_note,
    }


def _which_tool(name: str) -> Optional[str]:
    """shutil.which, then the running interpreter's own bin dir.

    pip console scripts land next to the interpreter, so a venv-installed
    whisper is invisible to PATH-based lookup whenever the server was started
    as `.venv/bin/python …` without activating the venv.
    """
    found = shutil.which(name)
    if found:
        return found
    candidate = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return None


def detect_capabilities(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Detect available analysis helpers without installing or downloading."""
    env = env if env is not None else os.environ
    whisper_cli = _which_tool("whisper")
    # Modern brew whisper-cpp ships the binary as `whisper-cli`; older builds
    # used `whisper-cpp`. Accept either so a fresh `brew install whisper-cpp`
    # is detected. (Distinct from the `whisper_cli` slot above, which is the
    # openai-whisper Python CLI invoked as `whisper`.)
    whisper_cpp = _which_tool("whisper-cli") or _which_tool("whisper-cpp")
    mlx_whisper = importlib.util.find_spec("mlx_whisper") is not None
    cv2 = importlib.util.find_spec("cv2") is not None
    provider = env.get("DAVINCI_RESOLVE_MCP_VISION_PROVIDER")

    sync_events = detect_sync_event_capabilities()

    # Phase C — embedding backends (detected like the whisper backends; the
    # ollama probe is a short local HTTP call and fails fast when not serving).
    try:
        from src.domains.media_analysis.utils import embeddings as _embeddings

        embedding_caps = _embeddings.detect_embedding_capabilities()
    except Exception:  # noqa: BLE001 — detection must never break capabilities
        embedding_caps = {"text": {"available": False, "backends": []},
                          "visual": {"available": False, "backends": []},
                          "install_guidance": {}}

    platform_id, machine = _runtime_platform_id()

    def _tool_entry(name: str, available: bool, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"available": bool(available)}
        if extra:
            entry.update(extra)
        if not available:
            entry["install"] = install_plan_for(name, platform_id=platform_id)
        return entry

    return {
        "success": True,
        "analysis_version": ANALYSIS_VERSION,
        "no_auto_install": True,
        "platform": {"id": platform_id, "machine": machine, "sys_platform": sys.platform},
        "tools": {
            "ffprobe": _tool_entry("ffprobe", bool(shutil.which("ffprobe")), {"path": shutil.which("ffprobe")}),
            "ffmpeg": _tool_entry("ffmpeg", bool(shutil.which("ffmpeg")), {"path": shutil.which("ffmpeg")}),
            "whisper_cli": _tool_entry("whisper_cli", bool(whisper_cli), {"path": whisper_cli}),
            "whisper_cpp": _tool_entry("whisper_cpp", bool(whisper_cpp), {"path": whisper_cpp}),
            "mlx_whisper": _tool_entry("mlx_whisper", bool(mlx_whisper), {"python_module": "mlx_whisper"}),
            "opencv": _tool_entry("opencv", bool(cv2), {"python_module": "cv2"}),
            "ollama_embeddings": _tool_entry(
                "ollama_embeddings",
                bool(embedding_caps.get("text", {}).get("available")),
                {"backends": embedding_caps.get("text", {}).get("backends", [])},
            ),
            "open_clip": _tool_entry(
                "open_clip",
                bool(embedding_caps.get("visual", {}).get("available")),
                {"python_module": "open_clip"},
            ),
            "clap_audio": _tool_entry(
                "clap_audio",
                bool(embedding_caps.get("audio", {}).get("available")),
                {"backends": embedding_caps.get("audio", {}).get("backends", [])},
            ),
        },
        "embeddings": embedding_caps,
        "transcription": {
            "available": bool(whisper_cli or whisper_cpp or mlx_whisper),
            "backends": [
                name for name, available in (
                    ("whisper_cli", bool(whisper_cli)),
                    ("whisper_cpp", bool(whisper_cpp)),
                    ("mlx_whisper", bool(mlx_whisper)),
                )
                if available
            ],
        },
        "vision": {
            "available": True,
            "provider": provider or HOST_CHAT_PATHS_PROVIDER,
            "default_provider": HOST_CHAT_PATHS_PROVIDER,
            "enabled_by_default": True,
            "note": (
                "Media-analysis tools default to host_chat_paths vision: the analyze "
                "actions return absolute paths to extracted analysis frames in a "
                "deferred-vision payload. The host chat model reads those frames as "
                "local images, produces JSON per the included schema, and calls "
                "media_analysis(action='commit_vision', ...) to merge the result, "
                "rebuild markers, and publish vision-dependent metadata to Resolve. "
                "Works with any MCP client whose chat model is vision-capable; no "
                "sampling/createMessage support required. The 'mock' provider is "
                "local-only for tests and never sends frames off-machine."
            ),
        },
        "sync_events": {
            "available": bool(sync_events.get("available")),
            "event_types": sync_events.get("event_types", []),
            "source_safe": True,
            "requires": ["ffmpeg", "ffprobe"],
            "note": "Detects likely audio 2-pops and slate claps for advisory sync offset planning.",
        },
    }


def install_guidance(capabilities: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    caps = capabilities or detect_capabilities()
    tools = caps.get("tools", {})
    missing = {}

    if not tools.get("ffprobe", {}).get("available") or not tools.get("ffmpeg", {}).get("available"):
        missing["ffmpeg_suite"] = {
            "required_for": [
                "technical metadata",
                "scene detection",
                "motion and variance analysis",
                "2-pop and slate-clap sync detection",
            ],
            "macos": "Ask the user before running: brew install ffmpeg",
            "linux": "Ask the user to install ffmpeg with their distribution package manager.",
            "windows": "Ask the user to install ffmpeg and add ffmpeg/ffprobe to PATH.",
        }
    if not caps.get("transcription", {}).get("available"):
        missing["transcription"] = {
            "required_for": ["transcription analysis", "default Resolve media analysis"],
            "options": [
                "Install/configure whisper CLI",
                "Install/configure whisper-cpp",
                "Install mlx-whisper on supported Apple Silicon systems",
            ],
            "macos": "Ask the user before running: brew install whisper-cpp, or configure another supported local Whisper backend.",
            "note": "The MCP server must not install these automatically.",
        }
    if not tools.get("opencv", {}).get("available"):
        missing["opencv"] = {
            "required_for": ["optional optical-flow motion scoring"],
            "note": "OpenCV is optional; standard frame-difference motion scoring can work without it.",
        }
    # Vision uses host_chat_paths by default and is always advertised available;
    # the host chat reads frame files locally and posts results back via
    # media_analysis(action="commit_vision"). No external provider install is required.

    return {
        "success": True,
        "no_auto_install": True,
        "missing": missing,
    }


def normalize_depth(value: Any) -> Tuple[Optional[str], Optional[str]]:
    depth = str(value or DEFAULT_DEPTH).strip().lower()
    if depth not in DEPTHS:
        return None, f"Unknown analysis depth '{value}'. Valid: {sorted(DEPTHS)}"
    return depth, None


def _coerce_optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stable_json_hash(value: Any, length: int = 12) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:length]


def _source_file_signature(path: Any) -> Dict[str, Any]:
    payload = {
        "path": normalize_path(path) if path else None,
        "exists": False,
        "size_bytes": None,
        "mtime_ns": None,
    }
    if not payload["path"]:
        return payload
    try:
        stat = os.stat(payload["path"])
    except OSError:
        return payload
    payload.update({
        "exists": True,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    })
    return payload


def analysis_request_signature(
    record: Dict[str, Any],
    depth: str,
    options: Dict[str, Any],
    frame_count: int,
    sampling: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return the cache signature for a requested analysis profile."""
    transcription = options.get("transcription") or {}
    vision = options.get("vision") or {}
    marker_plan = options.get("marker_plan") or {}
    vision_prompt = vision.get("prompt") or DEFAULT_VISION_ANALYSIS_PROMPT
    signature = {
        "analysis_version": ANALYSIS_VERSION,
        "depth": depth,
        "analysis_keyframe_budget": int(frame_count or 0),
        "source_file": _source_file_signature(record.get("file_path")),
        "layers": {
            "technical": True,
            "readthrough": depth in {"standard", "deep", "custom"},
            "motion": depth in {"standard", "deep", "custom"},
            "transcription": {
                "enabled": _coerce_bool(transcription.get("enabled"), default=DEFAULT_TRANSCRIPTION_ENABLED),
                "backend": transcription.get("backend"),
                "model": transcription.get("model"),
                "language": transcription.get("language"),
            },
            "vision": {
                "enabled": _coerce_bool(vision.get("enabled"), default=False),
                "provider": vision.get("provider"),
                "prompt_hash": _stable_json_hash(vision_prompt),
            },
            "marker_plan": {
                "enabled": _coerce_bool(marker_plan.get("enabled"), default=True),
                "min_shot_duration_seconds": marker_plan.get("min_shot_duration_seconds"),
                "colors_hash": _stable_json_hash(marker_plan.get("colors") or {}),
            },
            "cut_boundary_analysis": {
                "enabled": depth in {"standard", "deep", "custom"},
                "version": 1,
                "hard_frame_cap": HARD_FRAME_CAP,
            },
        },
        "signature_hash": _stable_json_hash({
            "analysis_version": ANALYSIS_VERSION,
            "depth": depth,
            "frame_count": int(frame_count or 0),
            "source_file": _source_file_signature(record.get("file_path")),
            "transcription": {
                "enabled": _coerce_bool(transcription.get("enabled"), default=DEFAULT_TRANSCRIPTION_ENABLED),
                "backend": transcription.get("backend"),
                "model": transcription.get("model"),
                "language": transcription.get("language"),
            },
            "vision": {
                "enabled": _coerce_bool(vision.get("enabled"), default=False),
                "provider": vision.get("provider"),
                "prompt_hash": _stable_json_hash(vision_prompt),
            },
            "marker_plan": {
                "enabled": _coerce_bool(marker_plan.get("enabled"), default=True),
                "min_shot_duration_seconds": marker_plan.get("min_shot_duration_seconds"),
                "colors_hash": _stable_json_hash(marker_plan.get("colors") or {}),
            },
            "cut_boundary_analysis": {
                "enabled": depth in {"standard", "deep", "custom"},
                "version": 1,
                "hard_frame_cap": HARD_FRAME_CAP,
            },
        }),
    }
    # Recorded outside signature_hash so it doesn't bust pre-existing caches;
    # mode changes are reconciled by thoroughness rank in _report_cache_state.
    if sampling:
        signature["analysis_sampling"] = {
            "mode": sampling.get("mode"),
            "frames_per_minute": sampling.get("frames_per_minute"),
            "frame_floor": sampling.get("frame_floor"),
            "frame_ceiling": sampling.get("frame_ceiling"),
        }
    return signature


def vision_uses_host_chat(options: Dict[str, Any], capabilities: Optional[Dict[str, Any]] = None) -> bool:
    vision = options.get("vision") or {}
    if not _coerce_bool(vision.get("enabled"), default=False):
        return False
    provider = vision.get("provider") or (capabilities or {}).get("vision", {}).get("provider")
    return provider in HOST_CHAT_VISION_PROVIDERS


vision_uses_chat_context = vision_uses_host_chat


def vision_requested(options: Dict[str, Any]) -> bool:
    return _coerce_bool((options.get("vision") or {}).get("enabled"), default=False)


def vision_is_pending_host_analysis(vision: Dict[str, Any]) -> bool:
    if not isinstance(vision, dict):
        return False
    return str(vision.get("status") or "").strip().lower() == "pending_host_analysis"


def visual_analysis_completed(vision: Dict[str, Any]) -> bool:
    if not isinstance(vision, dict):
        return False
    if not vision.get("success"):
        return False
    status = str(vision.get("status") or "").strip().lower()
    if status in {"skipped", "disabled", "pending_host_analysis"}:
        return False
    return bool(
        vision.get("clip_summary")
        or vision.get("content")
        or vision.get("editing_notes")
        or vision.get("analysis_keyframes")
        or vision.get("slate")
        or vision.get("shot_and_style")
    )


def _bounded_frame_count(depth: str, requested: Any = None) -> int:
    default = FRAME_CAPS.get(depth, FRAME_CAPS[DEFAULT_DEPTH])
    if requested is None:
        return default
    try:
        count = int(requested)
    except (TypeError, ValueError):
        return default
    return max(0, min(count, HARD_FRAME_CAP))


def _artifact_paths(project_root: str, record: Dict[str, Any], depth: str, options: Dict[str, Any]) -> Dict[str, Any]:
    clip_dir = resolve_clip_directory(project_root, record)
    artifacts: Dict[str, Any] = {
        "clip_dir": clip_dir,
        "analysis_json": os.path.join(clip_dir, "analysis.json"),
        "technical_json": os.path.join(clip_dir, "technical.json"),
        "marker_plan_json": os.path.join(clip_dir, "clip_analysis_markers.json"),
    }

    if depth in {"standard", "deep", "custom"}:
        artifacts["motion_json"] = os.path.join(clip_dir, "motion.json")
        artifacts["frames_dir"] = os.path.join(clip_dir, "frames")

    transcription = options.get("transcription") or {}
    if _coerce_bool(transcription.get("enabled"), default=DEFAULT_TRANSCRIPTION_ENABLED):
        artifacts["transcript_json"] = os.path.join(clip_dir, "transcript.json")
        artifacts["transcript_srt"] = os.path.join(clip_dir, "transcript.srt")
        artifacts["transcript_vtt"] = os.path.join(clip_dir, "transcript.vtt")

    vision = options.get("vision") or {}
    if _coerce_bool(vision.get("enabled"), default=False):
        artifacts["visual_json"] = os.path.join(clip_dir, "visual.json")

    return artifacts


def _required_capability_gaps(depth: str, options: Dict[str, Any], capabilities: Dict[str, Any]) -> List[Dict[str, Any]]:
    tools = capabilities.get("tools", {})
    gaps: List[Dict[str, Any]] = []
    if not tools.get("ffprobe", {}).get("available"):
        gaps.append({"capability": "ffprobe", "required_for": ["quick", "standard", "deep"]})
    if depth in {"standard", "deep", "custom"} and not tools.get("ffmpeg", {}).get("available"):
        gaps.append({"capability": "ffmpeg", "required_for": ["standard", "deep"]})

    transcription = options.get("transcription") or {}
    if _coerce_bool(transcription.get("enabled"), default=DEFAULT_TRANSCRIPTION_ENABLED):
        backend = transcription.get("backend")
        if backend in {"mock", "local_mock"}:
            pass
        elif not capabilities.get("transcription", {}).get("available"):
            gaps.append({"capability": "transcription_backend", "required_for": ["transcription"]})

    vision = options.get("vision") or {}
    if _coerce_bool(vision.get("enabled"), default=False):
        provider = vision.get("provider") or capabilities.get("vision", {}).get("provider")
        if provider in {"mock", "local_mock"} or provider in HOST_CHAT_VISION_PROVIDERS:
            pass
        elif not capabilities.get("vision", {}).get("available"):
            gaps.append({"capability": "vision_provider", "required_for": ["vision"]})

    return gaps


def build_plan(
    *,
    project_name: Any,
    project_id: Any = None,
    records: List[Dict[str, Any]],
    target: Dict[str, Any],
    params: Optional[Dict[str, Any]] = None,
    capabilities: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    params = params or {}
    depth, depth_error = normalize_depth(params.get("depth"))
    if depth_error:
        return {"success": False, "error": depth_error}
    assert depth is not None

    source_paths = [record.get("file_path") for record in records if record.get("file_path")]
    root = resolve_output_root(
        project_name=project_name,
        project_id=project_id,
        analysis_root=params.get("analysis_root"),
        source_paths=source_paths,
        create=False,
    )
    if not root.get("success"):
        return {"success": False, "error": "Invalid analysis output root", "output_root": root}

    caps = capabilities or detect_capabilities()
    options = {
        "transcription": params.get("transcription") or {},
        "vision": params.get("vision") or {},
        "marker_plan": params.get("marker_plan") or params.get("markerPlan") or {},
    }
    gaps = _required_capability_gaps(depth, options, caps)
    frame_count = _bounded_frame_count(depth, params.get("max_analysis_frames"))
    sampling_config = _resolve_sampling_config(params)
    transcription_enabled = _coerce_bool((options.get("transcription") or {}).get("enabled"), default=DEFAULT_TRANSCRIPTION_ENABLED)
    notes = [
        "Plans describe analysis before execution.",
        "All planned artifacts are under the project analysis root, never beside source media.",
        "Missing optional tools are reported as guidance only; nothing is installed automatically.",
        "Session-only execution returns reports to the MCP response and removes scratch artifacts unless keep_artifacts=true.",
    ]
    if caps.get("transcription", {}).get("available") and not transcription_enabled:
        notes.append(
            "Transcription is available but disabled; for story, sound, or audio-spine decisions, "
            "rerun with transcription.enabled=true and allow_model_download=true only if local model use is approved."
        )
    reuse_existing = _coerce_bool(params.get("reuse_existing", params.get("reuseExisting")), default=True)
    force_refresh = _coerce_bool(params.get("force_refresh", params.get("forceRefresh")), default=False)
    max_report_age_days = _coerce_optional_float(params.get("max_report_age_days", params.get("maxReportAgeDays")))
    reuse_policy = str(params.get("reuse_policy", params.get("reusePolicy") or "compatible")).strip().lower()
    if reuse_policy not in {"compatible", "fresh", "strict"}:
        reuse_policy = "compatible"
    if params.get("_reuse_default_analysis_root"):
        reuse_root_payload = resolve_output_root(
            project_name=project_name,
            project_id=project_id,
            analysis_root=None,
            source_paths=source_paths,
            create=False,
        )
        reuse_project_root = reuse_root_payload.get("project_root")
    else:
        reuse_project_root = params.get("reuse_project_root") or params.get("reuseProjectRoot") or root["project_root"]
        reuse_project_root = normalize_path(reuse_project_root) if reuse_project_root else root["project_root"]
    raw_reuse_project_roots = params.get("reuse_project_roots") or params.get("reuseProjectRoots") or []
    if isinstance(raw_reuse_project_roots, str):
        raw_reuse_project_roots = [raw_reuse_project_roots]
    elif not isinstance(raw_reuse_project_roots, list):
        raw_reuse_project_roots = []
    reuse_project_roots = []
    search_related_project_roots = _coerce_bool(
        params.get("search_related_project_roots", params.get("searchRelatedProjectRoots")),
        default=True,
    )
    max_related_project_roots = int(
        _coerce_optional_float(params.get("max_related_project_roots", params.get("maxRelatedProjectRoots")))
        or DEFAULT_MAX_RELATED_PROJECT_ROOTS
    )
    related_project_roots = (
        related_analysis_project_roots(reuse_project_root, limit=max_related_project_roots)
        if search_related_project_roots
        else []
    )
    for candidate_root in [reuse_project_root, *raw_reuse_project_roots]:
        if not candidate_root:
            continue
        normalized_root = normalize_path(candidate_root)
        if normalized_root not in reuse_project_roots:
            reuse_project_roots.append(normalized_root)
    for candidate_root in related_project_roots:
        if candidate_root not in reuse_project_roots:
            reuse_project_roots.append(candidate_root)

    clip_plans = []
    for record in records:
        artifacts = _artifact_paths(root["project_root"], record, depth, options)
        request_signature = analysis_request_signature(record, depth, options, frame_count, sampling=sampling_config)
        existing: Optional[Dict[str, Any]] = None
        clip_plan = {
            "record": record,
            "analysis_keyframe_budget": frame_count,
            "sampling": sampling_config,
            "analysis_signature": request_signature,
            "cache_status": "not_checked",
            "artifacts": artifacts,
        }
        if not reuse_existing:
            clip_plan["cache_status"] = "reuse_disabled"
        elif force_refresh:
            clip_plan["cache_status"] = "refresh_forced"
        else:
            candidates: List[Dict[str, Any]] = []
            for report_path in _record_analysis_report_paths(record):
                candidate = find_reusable_report_from_path(
                    report_path,
                    record,
                    depth,
                    options,
                    request_signature=request_signature,
                    max_report_age_days=max_report_age_days,
                    reuse_policy=reuse_policy,
                )
                if candidate:
                    candidates.append(candidate)
            registry_candidate = find_reusable_report_from_registry(
                reuse_project_root,
                record,
                depth,
                options,
                request_signature=request_signature,
                max_report_age_days=max_report_age_days,
                reuse_policy=reuse_policy,
            )
            if registry_candidate:
                candidates.append(registry_candidate)
            existing = find_reusable_report_across_roots(
                reuse_project_roots,
                record,
                depth,
                options,
                request_signature=request_signature,
                max_report_age_days=max_report_age_days,
                reuse_policy=reuse_policy,
            )
            if existing:
                candidates.append(existing)
            if candidates:
                reusable_candidates = [row for row in candidates if row.get("reusable")]
                pool = reusable_candidates or candidates
                pool.sort(key=_report_reuse_score)
                existing = pool[0]
            if existing:
                clip_plan["existing_report"] = {
                    "path": existing.get("path"),
                    "reusable": existing.get("reusable", False),
                    "missing_layers": existing.get("missing_layers", []),
                    "cache_issues": existing.get("cache_issues", []),
                    "cache_warnings": existing.get("cache_warnings", []),
                    "analyzed_at": existing.get("analyzed_at"),
                    "project_root": existing.get("project_root"),
                    "source": existing.get("source") or "analysis_root_search",
                    "registry_path": existing.get("registry_path"),
                    "superseded_by_relink": bool(existing.get("superseded_by_relink")),
                    "superseded_at": existing.get("superseded_at"),
                    "superseded_reason": existing.get("superseded_reason"),
                }
                if existing.get("reusable"):
                    clip_plan["skip_execution"] = True
                    clip_plan["cache_status"] = "reusable"
                    clip_plan["reused_from"] = existing.get("path")
                    clip_plan["reuse_source"] = existing.get("source") or "analysis_root_search"
                    if existing.get("source") == "record_analysis_report_path":
                        clip_plan["reuse_reason"] = "Resolve clip metadata points to an existing analysis report that satisfies the requested depth and modalities."
                    elif existing.get("source") == "analysis_registry":
                        clip_plan["reuse_reason"] = "Global analysis registry points to an existing report that satisfies the requested depth and modalities."
                    elif existing.get("project_root") and existing.get("project_root") != root["project_root"]:
                        clip_plan["reuse_reason"] = "Existing analysis report from a related project version satisfies the requested depth and modalities."
                    else:
                        clip_plan["reuse_reason"] = "Existing analysis report satisfies the requested depth and modalities."
                else:
                    clip_plan["cache_status"] = "stale_or_incomplete"
                    clip_plan["why_not_reused"] = _why_not_reused(existing)
            else:
                clip_plan["cache_status"] = "miss"
                clip_plan["why_not_reused"] = _why_not_reused(None, provenance_present=_record_has_analysis_provenance(record))
        if (
            reuse_existing
            and not force_refresh
            and not clip_plan.get("skip_execution")
            and clip_plan.get("cache_status") not in {"reuse_disabled", "refresh_forced"}
            and _record_has_analysis_provenance(record)
        ):
            _mark_reuse_blocked(clip_plan, record, existing)
        clip_plans.append(clip_plan)

    per_clip_seconds = {"quick": 2, "standard": 45, "deep": 180, "custom": 45}.get(depth, 45)
    reusable_count = sum(1 for clip in clip_plans if clip.get("skip_execution"))
    stale_count = sum(1 for clip in clip_plans if clip.get("cache_status") == "stale_or_incomplete")
    blocked_count = sum(1 for clip in clip_plans if clip.get("reuse_blocked"))
    miss_count = sum(1 for clip in clip_plans if clip.get("cache_status") == "miss")
    reused_sources: Dict[str, int] = {}
    for clip in clip_plans:
        source = clip.get("reuse_source")
        if source:
            reused_sources[str(source)] = reused_sources.get(str(source), 0) + 1
    reuse_summary = {
        "checked": reuse_existing and not force_refresh,
        "reusable_clip_count": reusable_count,
        "blocked_clip_count": blocked_count,
        "stale_or_incomplete_clip_count": stale_count,
        "miss_clip_count": miss_count,
        "estimated_seconds_saved": per_clip_seconds * reusable_count,
        "sources": reused_sources,
        "registry_path": analysis_registry_path(reuse_project_root),
    }
    return {
        "success": True,
        "analysis_version": ANALYSIS_VERSION,
        "dry_run": _coerce_bool(params.get("dry_run"), default=True),
        "session_only": _coerce_bool(params.get("session_only"), default=False),
        "target": target,
        "depth": depth,
        "clip_count": len(records),
        "output_root": root,
        "capability_gaps": gaps,
        "install_guidance": install_guidance(caps) if gaps else {"success": True, "missing": {}},
        "estimated_seconds": per_clip_seconds * len(records),
        "estimated_seconds_after_reuse": per_clip_seconds * max(0, len(records) - reusable_count),
        "analysis_keyframe_budget_per_clip": frame_count,
        "sampling": sampling_config,
        "sampling_mode": sampling_config.get("mode"),
        "reuse_existing": reuse_existing,
        "force_refresh": force_refresh,
        "reuse_policy": reuse_policy,
        "max_report_age_days": max_report_age_days,
        "reuse_project_root": reuse_project_root,
        "reuse_project_roots": reuse_project_roots,
        "search_related_project_roots": search_related_project_roots,
        "related_project_roots": related_project_roots,
        "reusable_clip_count": reusable_count,
        "stale_or_incomplete_clip_count": stale_count,
        "reuse_blocked_clip_count": blocked_count,
        "reuse_summary": reuse_summary,
        "clips": clip_plans,
        "notes": notes,
    }


