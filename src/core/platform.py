#!/usr/bin/env python3
"""
Platform-specific functionality for DaVinci Resolve MCP Server
"""

import os
import sys
import platform

def get_platform():
    """Identify the current operating system platform.
    
    Returns:
        str: 'windows', 'darwin' (macOS), or 'linux'
    """
    system = platform.system().lower()
    if system == 'darwin':
        return 'darwin'
    elif system == 'windows':
        return 'windows'
    elif system == 'linux':
        return 'linux'
    return system

def get_resolve_paths():
    """Get platform-specific paths for DaVinci Resolve scripting API.
    
    Returns:
        dict: Dictionary containing api_path, lib_path, and modules_path
    """
    platform_name = get_platform()
    
    if platform_name == 'darwin':  # macOS
        api_path = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
        lib_path = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
        modules_path = os.path.join(api_path, "Modules")
    
    elif platform_name == 'windows':  # Windows
        program_files = os.environ.get('PROGRAMDATA', 'C:\\ProgramData')
        program_files_64 = os.environ.get('PROGRAMFILES', 'C:\\Program Files')
        
        api_path = os.path.join(program_files, 'Blackmagic Design', 'DaVinci Resolve', 'Support', 'Developer', 'Scripting')
        lib_path = os.path.join(program_files_64, 'Blackmagic Design', 'DaVinci Resolve', 'fusionscript.dll')
        modules_path = os.path.join(api_path, "Modules")
    
    elif platform_name == 'linux':  # Linux
        # /opt/resolve is the stock install root; some distro packages install to
        # /opt/resolve/... via a symlink from /home/resolve. The scripting lib
        # ships under libs/Fusion/ on current builds (21.x) but older layouts put
        # it directly under libs/ — probe both so a correct env var is exported
        # regardless (server.py overrides RESOLVE_SCRIPT_LIB from this value).
        api_path = "/opt/resolve/Developer/Scripting"
        _lib_candidates = (
            "/opt/resolve/libs/Fusion/fusionscript.so",
            "/opt/resolve/libs/fusionscript.so",
        )
        lib_path = next((c for c in _lib_candidates if os.path.exists(c)),
                        _lib_candidates[0])
        modules_path = os.path.join(api_path, "Modules")
    
    else:
        # Fallback to macOS paths if unknown platform
        api_path = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
        lib_path = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
        modules_path = os.path.join(api_path, "Modules")
    
    return {
        "api_path": api_path,
        "lib_path": lib_path,
        "modules_path": modules_path
    }

def get_resolve_plugin_paths():
    """Get platform-specific paths for Resolve plugin install dirs.

    Returns:
        dict: {
            'fuses_dir':  Fusion Fuses directory,
            'dctl_dir':   LUT directory (where regular .dctl files live),
            'aces_idt_dir': ACES IDT transforms (separate scan path; restart
                            required after install),
            'aces_odt_dir': ACES ODT transforms (same caveat),
        }
    """
    platform_name = get_platform()
    home = os.path.expanduser("~")

    # NOTE: The Fuse SDK doc (June 2023) lists Fuses under "Support/Fusion/Fuses"
    # on macOS, but the directory Resolve actually scans is the sibling
    # "Fusion/Fuses" (without "Support") — verified live against Resolve Studio
    # 20.3.2.9 by writing test fuses to both paths and observing which loaded.
    # Per-platform conventions also differ from the SDK doc; we follow the
    # canonical Fusion user-data layout where every Fusion user directory
    # (Macros, Templates, Scripts, Modules, Fuses, ...) lives directly under
    # the platform's Fusion user root.
    if platform_name == 'darwin':
        support = os.path.join(home, "Library", "Application Support",
                               "Blackmagic Design", "DaVinci Resolve")
        fuses_dir = os.path.join(support, "Fusion", "Fuses")
        dctl_dir = os.path.join(support, "LUT")
        aces_root = os.path.join(support, "ACES Transforms")
    elif platform_name == 'windows':
        appdata = os.environ.get('APPDATA', os.path.join(home, 'AppData', 'Roaming'))
        fuses_dir = os.path.join(appdata, 'Blackmagic Design', 'DaVinci Resolve',
                                 'Support', 'Fusion', 'Fuses')
        dctl_dir = os.path.join(appdata, 'Blackmagic Design', 'DaVinci Resolve',
                                'Support', 'LUT')
        aces_root = os.path.join(appdata, 'Blackmagic Design', 'DaVinci Resolve',
                                 'Support', 'ACES Transforms')
    elif platform_name == 'linux':
        base = os.path.join(home, '.local', 'share', 'DaVinciResolve')
        fuses_dir = os.path.join(base, 'Fusion', 'Fuses')
        dctl_dir = os.path.join(base, 'LUT')
        aces_root = os.path.join(base, 'ACES Transforms')
    else:
        support = os.path.join(home, "Library", "Application Support",
                               "Blackmagic Design", "DaVinci Resolve")
        fuses_dir = os.path.join(support, "Fusion", "Fuses")
        dctl_dir = os.path.join(support, "LUT")
        aces_root = os.path.join(support, "ACES Transforms")

    # Resolve scans these subdirs of Fusion/Scripts/ at startup and exposes
    # them in Workspace → Scripts → <category>. Categories per Resolve docs.
    if platform_name == 'darwin':
        scripts_root = os.path.join(support, "Fusion", "Scripts")
    elif platform_name == 'windows':
        scripts_root = os.path.join(appdata, 'Blackmagic Design', 'DaVinci Resolve',
                                    'Support', 'Fusion', 'Scripts')
    elif platform_name == 'linux':
        scripts_root = os.path.join(base, 'Fusion', 'Scripts')
    else:
        scripts_root = os.path.join(support, "Fusion", "Scripts")

    return {
        "fuses_dir": fuses_dir,
        "dctl_dir": dctl_dir,
        "aces_idt_dir": os.path.join(aces_root, "IDT"),
        "aces_odt_dir": os.path.join(aces_root, "ODT"),
        "scripts_root": scripts_root,
        # Category subdirs Resolve actually scans (verified live):
        "scripts_categories": ("Edit", "Color", "Deliver", "Comp",
                               "Tool", "Utility", "Views"),
    }


# --- Crash guards -----------------------------------------------------------
# Native Scripting API calls proven to take the whole Resolve process down on
# specific platforms. Guards return None when the call is safe to attempt
# here, or a machine-readable block dict the callsite wraps into its own
# error shape (compound _err envelope, granular legacy {"error": ...}).

ENV_ALLOW_SUBTITLE_GENERATION = "RESOLVE_ALLOW_SUBTITLE_GENERATION"

SUBTITLE_GENERATION_CRASH_ISSUE = (
    "https://github.com/blackhill24/davinci-resolve-mcp/issues/90"
)


def subtitle_generation_override_active():
    """True when RESOLVE_ALLOW_SUBTITLE_GENERATION opts into the crash-prone
    native subtitle call on a platform where the crash is proven (Linux).

    Callers that proceed under this override should surface a warning in their
    result — the env var may be inherited from a launcher shell the caller
    never sees, and a silent bypass of a process-killing guard is worse than
    the refusal (issue #90).
    """
    if get_platform() != "linux":
        return False
    return os.environ.get(ENV_ALLOW_SUBTITLE_GENERATION, "").strip().lower() in {"1", "true", "yes"}


def subtitle_generation_guard():
    """Guard for Timeline.CreateSubtitlesFromAudio (issue #90).

    On Linux (reproduced 2/2 on Resolve Studio 21.0.2.4) the native call kills
    the Resolve process outright — no exception, no error return — leaking any
    disposable project that was open. Unreproduced on macOS/Windows (no test
    hardware), so the guard only fires on Linux; other platforms proceed.

    RESOLVE_ALLOW_SUBTITLE_GENERATION=1 overrides the refusal for anyone who
    accepts the crash risk (e.g. probing a newer Resolve build for a fix).
    This is the tool-facing sibling of the live probe's
    RESOLVE_PROBE_ALLOW_SUBTITLE_GENERATION opt-in.
    """
    if get_platform() != "linux":
        return None
    if subtitle_generation_override_active():
        return None
    return {
        "blocked_call": "Timeline.CreateSubtitlesFromAudio",
        "platform": "linux",
        "reason": (
            "Native CreateSubtitlesFromAudio crashes the entire Resolve "
            "process on Linux (reproduced 2/2 on Studio 21.0.2.4) — the "
            "process dies mid-call, leaking the open project and requiring "
            "a full relaunch."
        ),
        "override_env": ENV_ALLOW_SUBTITLE_GENERATION,
        "issue": SUBTITLE_GENERATION_CRASH_ISSUE,
        "alternative": (
            "Generate subtitles offline (e.g. Whisper via media-analysis) "
            "and bring them in with timeline(action='import_srt') instead — "
            "that path is live-proven on this platform."
        ),
    }


def setup_environment():
    """Set up environment variables for DaVinci Resolve scripting.
    
    Returns:
        bool: True if setup was successful, False otherwise
    """
    try:
        paths = get_resolve_paths()
        
        os.environ["RESOLVE_SCRIPT_API"] = paths["api_path"]
        os.environ["RESOLVE_SCRIPT_LIB"] = paths["lib_path"]
        
        # Add modules path to Python's path if it's not already there
        if paths["modules_path"] not in sys.path:
            sys.path.append(paths["modules_path"])
        
        return True
    
    except Exception as e:
        print(f"Error setting up environment: {str(e)}")
        return False 