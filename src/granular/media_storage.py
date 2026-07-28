"""MediaStorage tools."""

from src.granular.common import *  # noqa: F401,F403

resolve = ResolveProxy()

@mcp.tool()
def get_mounted_volumes() -> Dict[str, Any]:
    """Get list of mounted volumes displayed in Resolve's Media Storage."""
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    ms = resolve.GetMediaStorage()
    if not ms:
        return {"error": "Failed to get MediaStorage"}
    volumes = ms.GetMountedVolumeList()
    return {"volumes": volumes if volumes else []}


@mcp.tool()
def get_media_storage_subfolders(folder_path: str) -> Dict[str, Any]:
    """Get subfolders in a given absolute folder path from Media Storage.

    Args:
        folder_path: Absolute path to the folder to list subfolders for.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    ms = resolve.GetMediaStorage()
    if not ms:
        return {"error": "Failed to get MediaStorage"}
    subfolders = ms.GetSubFolderList(folder_path)
    return {"folder_path": folder_path, "subfolders": subfolders if subfolders else []}


@mcp.tool()
def get_media_storage_files(folder_path: str) -> Dict[str, Any]:
    """Get media and file listings in a given absolute folder path from Media Storage.

    Args:
        folder_path: Absolute path to the folder to list files for.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    ms = resolve.GetMediaStorage()
    if not ms:
        return {"error": "Failed to get MediaStorage"}
    files = ms.GetFileList(folder_path)
    return {"folder_path": folder_path, "files": files if files else []}


@mcp.tool()
def reveal_in_media_storage(file_path: str) -> Dict[str, Any]:
    """Reveal a file path in Resolve's Media Storage browser.

    Args:
        file_path: Absolute path to the file to reveal.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    ms = resolve.GetMediaStorage()
    if not ms:
        return {"error": "Failed to get MediaStorage"}
    result = ms.RevealInStorage(file_path)
    return {"success": bool(result), "file_path": file_path}


@mcp.tool()
def add_items_to_media_pool_from_storage(
    file_paths: Optional[List[str]] = None,
    item_infos: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Add specified file/folder paths from Media Storage into current Media Pool folder.

    Args:
        file_paths: Simple form — list of absolute file/folder paths.
        item_infos: Positioned form — list of dicts with keys media (required),
            startFrame, endFrame. Mirrors
            MediaStorage.AddItemListToMediaPool([{itemInfo}, ...]) per docs line 210.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    ms = resolve.GetMediaStorage()
    if not ms:
        return {"error": "Failed to get MediaStorage"}
    if item_infos is not None:
        if not isinstance(item_infos, list) or not item_infos:
            return {"error": "item_infos must be a non-empty list"}
        for i, info in enumerate(item_infos):
            if not isinstance(info, dict):
                return {"error": f"item_infos[{i}] must be an object"}
            if not info.get("media"):
                return {"error": f"item_infos[{i}] requires media (file path)"}
        clips = ms.AddItemListToMediaPool(item_infos)
    else:
        if not file_paths:
            return {"error": "Provide file_paths (simple) or item_infos (positioned)"}
        clips = ms.AddItemListToMediaPool(file_paths)
    if clips:
        return {"success": True, "clips_added": len(clips)}
    return {"success": False, "error": "Failed to add items to Media Pool"}


@mcp.tool()
def add_clip_mattes_to_media_pool(media_pool_item_id: str, matte_paths: List[str]) -> Dict[str, Any]:
    """Add clip mattes from Media Storage to a MediaPoolItem.

    Args:
        media_pool_item_id: The unique ID of the MediaPoolItem.
        matte_paths: List of absolute file paths for the matte files.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    ms = resolve.GetMediaStorage()
    if not ms:
        return {"error": "Failed to get MediaStorage"}

    # Find the media pool item by ID
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        return {"error": "No project currently open"}
    mp = project.GetMediaPool()
    root = mp.GetRootFolder()

    # Search for clip by ID
    def find_clip_by_id(folder, target_id):
        for clip in (folder.GetClipList() or []):
            if clip.GetUniqueId() == target_id:
                return clip
        for sub in (folder.GetSubFolderList() or []):
            found = find_clip_by_id(sub, target_id)
            if found:
                return found
        return None

    clip = find_clip_by_id(root, media_pool_item_id)
    if not clip:
        return {"error": f"MediaPoolItem with ID {media_pool_item_id} not found"}

    result = ms.AddClipMattesToMediaPool(clip, matte_paths, root)
    return {"success": bool(result)}


@mcp.tool()
def add_timeline_mattes_to_media_pool(matte_paths: List[str]) -> Dict[str, Any]:
    """Add timeline mattes from Media Storage into the current media pool folder.

    Args:
        matte_paths: List of absolute file paths for the matte files.

    Timeline mattes attach to the CURRENT MEDIA POOL FOLDER, not to a timeline
    item — `AddTimelineMattesToMediaPool([paths])` takes only the paths list.
    This tool used to be given the clip-matte signature
    (`AddClipMattesToMediaPool(MediaPoolItem, [paths], stereoEye)`, called
    correctly a few lines up), so it passed a TimelineItem where the paths list
    belongs and the paths landed in a parameter slot the method does not have.
    It cannot ever have worked (#144 finding 3). The timeline_item_index /
    track_type / track_index parameters computed that wrong argument and are
    gone with it.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    ms = resolve.GetMediaStorage()
    if not ms:
        return {"error": "Failed to get MediaStorage"}
    if not matte_paths:
        return {"error": "matte_paths must be a non-empty list of file paths"}

    # Returns [MediaPoolItems], not a bool.
    added = ms.AddTimelineMattesToMediaPool(matte_paths)
    if not added:
        return {"success": False, "error": "Resolve added no timeline mattes"}
    return {
        "success": True,
        "added_count": len(added),
        "added": [_ser(item.GetName()) for item in added],
    }
