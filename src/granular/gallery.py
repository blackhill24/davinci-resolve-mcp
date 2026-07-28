"""Gallery, still album, and powergrade tools."""

from src.granular.common import *  # noqa: F401,F403

resolve = ResolveProxy()


def _gallery_or_error():
    """(gallery, None) or (None, error dict). Guards the whole chain."""
    resolve = get_resolve()
    if resolve is None:
        return None, {"error": "Not connected to DaVinci Resolve"}
    pm = resolve.GetProjectManager()
    if pm is None:
        return None, {"error": "Failed to get Project Manager"}
    project = pm.GetCurrentProject()
    if not project:
        return None, {"error": "No project open"}
    gallery = project.GetGallery()
    if not gallery:
        return None, {"error": "Failed to get Gallery"}
    return gallery, None


def _still_album(gallery, album_index=None):
    """Resolve a GalleryStillAlbum object. (album, None) or (None, error).

    Every Gallery album method takes the album OBJECT as its first argument —
    `GetAlbumName(galleryStillAlbum)`, `SetAlbumName(galleryStillAlbum,
    albumName)`. Calling them with only the name never touched an album at all
    (#144 finding 1).
    """
    if album_index is None:
        album = gallery.GetCurrentStillAlbum()
        if not album:
            return None, {"error": "No current gallery still album"}
        return album, None
    albums = gallery.GetGalleryStillAlbums() or []
    if not isinstance(album_index, int) or album_index < 0 or album_index >= len(albums):
        return None, {"error": f"Album index {album_index} out of range ({len(albums)} albums)"}
    return albums[album_index], None

@mcp.tool(annotations=READ_ONLY_TOOL)
def get_gallery_album_name(album_index: Optional[int] = None) -> Dict[str, Any]:
    """Get a gallery still album's name.

    Args:
        album_index: 0-based index into GetGalleryStillAlbums(). Omit for the
            current album.
    """
    gallery, err = _gallery_or_error()
    if err:
        return err
    album, err = _still_album(gallery, album_index)
    if err:
        return err
    # GetAlbumName takes the album object; calling it bare read nothing.
    name = gallery.GetAlbumName(album)
    return {"album_name": name if name else ""}


@mcp.tool()
def set_gallery_album_name(name: str, album_index: Optional[int] = None) -> Dict[str, Any]:
    """Set a gallery still album's name.

    Args:
        name: New album name.
        album_index: 0-based index into GetGalleryStillAlbums(). Omit to rename
            the current album.
    """
    gallery, err = _gallery_or_error()
    if err:
        return err
    album, err = _still_album(gallery, album_index)
    if err:
        return err
    # SetAlbumName(galleryStillAlbum, albumName) - the album argument was never
    # passed, so this renamed nothing (#144 finding 1). The compound surface has
    # always had it right (color_grade/actions.py set_album_name).
    return {"success": bool(gallery.SetAlbumName(album, name))}


@mcp.tool()
def get_gallery_still_albums() -> Dict[str, Any]:
    """Get list of all gallery still albums."""
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    _pm = resolve.GetProjectManager()
    project = _pm.GetCurrentProject() if _pm else None
    if not project:
        return {"error": "No project open"}
    gallery = project.GetGallery()
    if not gallery:
        return {"error": "Failed to get Gallery"}
    albums = gallery.GetGalleryStillAlbums()
    return {"albums": [str(a) for a in albums] if albums else []}


@mcp.tool()
def get_gallery_power_grade_albums() -> Dict[str, Any]:
    """Get list of all gallery power grade albums."""
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    _pm = resolve.GetProjectManager()
    project = _pm.GetCurrentProject() if _pm else None
    if not project:
        return {"error": "No project open"}
    gallery = project.GetGallery()
    if not gallery:
        return {"error": "Failed to get Gallery"}
    albums = gallery.GetGalleryPowerGradeAlbums()
    return {"albums": [str(a) for a in albums] if albums else []}


@mcp.tool()
def get_current_still_album() -> Dict[str, Any]:
    """Get the current still album."""
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    _pm = resolve.GetProjectManager()
    project = _pm.GetCurrentProject() if _pm else None
    if not project:
        return {"error": "No project open"}
    gallery = project.GetGallery()
    if not gallery:
        return {"error": "Failed to get Gallery"}
    album = gallery.GetCurrentStillAlbum()
    return {"has_album": album is not None}


@mcp.tool()
def set_current_still_album(album_index: int) -> Dict[str, Any]:
    """Set the current still album by index.

    Args:
        album_index: 0-based index of the album in GetGalleryStillAlbums() list.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    _pm = resolve.GetProjectManager()
    project = _pm.GetCurrentProject() if _pm else None
    if not project:
        return {"error": "No project open"}
    gallery = project.GetGallery()
    if not gallery:
        return {"error": "Failed to get Gallery"}
    albums = gallery.GetGalleryStillAlbums()
    if not albums or album_index >= len(albums):
        return {"error": f"No album at index {album_index}"}
    result = gallery.SetCurrentStillAlbum(albums[album_index])
    return {"success": bool(result)}


@mcp.tool()
def create_gallery_still_album(album_name: str = "") -> Dict[str, Any]:
    """Create a new gallery still album.

    Args:
        album_name: Optional name for the new album.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    _pm = resolve.GetProjectManager()
    project = _pm.GetCurrentProject() if _pm else None
    if not project:
        return {"error": "No project open"}
    gallery = project.GetGallery()
    if not gallery:
        return {"error": "Failed to get Gallery"}
    # CreateGalleryStillAlbum() takes NO arguments - the naming path was an
    # undocumented 1-arg call, so the tool's only parameter took the unverified
    # branch (#144 finding 2). Naming is a second call, SetAlbumName(album, name).
    album = gallery.CreateGalleryStillAlbum()
    if album is None:
        return {"success": False, "error": "Failed to create album"}
    named = None
    if album_name:
        named = bool(gallery.SetAlbumName(album, album_name))
    out = {"success": True}
    if album_name:
        out["named"] = named
        if not named:
            out["warning"] = f"Album created but could not be renamed to {album_name!r}"
    return out


@mcp.tool()
def create_gallery_power_grade_album(album_name: str = "") -> Dict[str, Any]:
    """Create a new gallery power grade album.

    Args:
        album_name: Optional name for the new album.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    _pm = resolve.GetProjectManager()
    project = _pm.GetCurrentProject() if _pm else None
    if not project:
        return {"error": "No project open"}
    gallery = project.GetGallery()
    if not gallery:
        return {"error": "Failed to get Gallery"}
    # CreateGalleryPowerGradeAlbum() takes NO arguments - the naming path was an
    # undocumented 1-arg call, so the tool's only parameter took the unverified
    # branch (#144 finding 2). Naming is a second call, SetAlbumName(album, name).
    album = gallery.CreateGalleryPowerGradeAlbum()
    if album is None:
        return {"success": False, "error": "Failed to create album"}
    named = None
    if album_name:
        named = bool(gallery.SetAlbumName(album, album_name))
    out = {"success": True}
    if album_name:
        out["named"] = named
        if not named:
            out["warning"] = f"Album created but could not be renamed to {album_name!r}"
    return out


@mcp.tool()
def get_album_stills(album_index: int = 0) -> Dict[str, Any]:
    """Get list of stills in a gallery album.

    Args:
        album_index: 0-based index of the album. Default: 0.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    _pm = resolve.GetProjectManager()
    project = _pm.GetCurrentProject() if _pm else None
    if not project:
        return {"error": "No project open"}
    gallery = project.GetGallery()
    if not gallery:
        return {"error": "Failed to get Gallery"}
    albums = gallery.GetGalleryStillAlbums()
    if not albums or album_index >= len(albums):
        return {"error": f"No album at index {album_index}"}
    stills = albums[album_index].GetStills()
    return {"still_count": len(stills) if stills else 0}


@mcp.tool()
def get_still_label(album_index: int, still_index: int) -> Dict[str, Any]:
    """Get the label of a still in a gallery album.

    Args:
        album_index: 0-based album index.
        still_index: 0-based still index.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    _pm = resolve.GetProjectManager()
    project = _pm.GetCurrentProject() if _pm else None
    if not project:
        return {"error": "No project open"}
    gallery = project.GetGallery()
    albums = gallery.GetGalleryStillAlbums()
    if not albums or album_index >= len(albums):
        return {"error": f"No album at index {album_index}"}
    stills = albums[album_index].GetStills()
    if not stills or still_index >= len(stills):
        return {"error": f"No still at index {still_index}"}
    label = albums[album_index].GetLabel(stills[still_index])
    return {"label": label if label else ""}


@mcp.tool()
def set_still_label(album_index: int, still_index: int, label: str) -> Dict[str, Any]:
    """Set the label of a still in a gallery album.

    Args:
        album_index: 0-based album index.
        still_index: 0-based still index.
        label: New label for the still.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    _pm = resolve.GetProjectManager()
    project = _pm.GetCurrentProject() if _pm else None
    if not project:
        return {"error": "No project open"}
    gallery = project.GetGallery()
    albums = gallery.GetGalleryStillAlbums()
    if not albums or album_index >= len(albums):
        return {"error": f"No album at index {album_index}"}
    stills = albums[album_index].GetStills()
    if not stills or still_index >= len(stills):
        return {"error": f"No still at index {still_index}"}
    result = albums[album_index].SetLabel(stills[still_index], label)
    return {"success": bool(result)}


@mcp.tool()
def import_stills_to_album(album_index: int, file_paths: List[str]) -> Dict[str, Any]:
    """Import stills from file paths into a gallery album.

    Args:
        album_index: 0-based album index.
        file_paths: List of absolute file paths to import.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    _pm = resolve.GetProjectManager()
    project = _pm.GetCurrentProject() if _pm else None
    if not project:
        return {"error": "No project open"}
    gallery = project.GetGallery()
    albums = gallery.GetGalleryStillAlbums()
    if not albums or album_index >= len(albums):
        return {"error": f"No album at index {album_index}"}
    result = albums[album_index].ImportStills(file_paths)
    return {"success": bool(result)}


@mcp.tool()
def export_stills_from_album(album_index: int, folder_path: str, file_prefix: str = "still", format: str = "dpx") -> Dict[str, Any]:
    """Export stills from a gallery album.

    Args:
        album_index: 0-based album index.
        folder_path: Directory to export to.
        file_prefix: Filename prefix. Default: 'still'.
        format: File format (dpx, cin, tif, jpg, png, ppm, bmp, xpm, drx). Default: 'dpx'.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    _pm = resolve.GetProjectManager()
    project = _pm.GetCurrentProject() if _pm else None
    if not project:
        return {"error": "No project open"}
    gallery = project.GetGallery()
    albums = gallery.GetGalleryStillAlbums()
    if not albums or album_index >= len(albums):
        return {"error": f"No album at index {album_index}"}
    stills = albums[album_index].GetStills()
    if not stills:
        return {"error": "No stills in album"}
    result = albums[album_index].ExportStills(stills, folder_path, file_prefix, format)
    return {"success": bool(result)}


@mcp.tool()
def delete_stills_from_album(album_index: int, still_indices: List[int]) -> Dict[str, Any]:
    """Delete stills from a gallery album.

    Args:
        album_index: 0-based album index.
        still_indices: List of 0-based still indices to delete.
    """
    resolve = get_resolve()
    if resolve is None:
        return {"error": "Not connected to DaVinci Resolve"}
    _pm = resolve.GetProjectManager()
    project = _pm.GetCurrentProject() if _pm else None
    if not project:
        return {"error": "No project open"}
    gallery = project.GetGallery()
    albums = gallery.GetGalleryStillAlbums()
    if not albums or album_index >= len(albums):
        return {"error": f"No album at index {album_index}"}
    stills = albums[album_index].GetStills()
    if not stills:
        return {"error": "No stills in album"}
    to_delete = [stills[i] for i in still_indices if i < len(stills)]
    result = albums[album_index].DeleteStills(to_delete)
    return {"success": bool(result)}
