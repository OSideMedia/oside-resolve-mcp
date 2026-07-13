# ============================================================================
# resolve_api.py — thin wrapper over the DaVinci Resolve scripting API.
#
# Connection bootstrapping follows the standard macOS layout (the same pattern
# samuelgursky/davinci-resolve-mcp uses — MIT, credited in README). Resolve
# Studio must be RUNNING with Preferences → System → General →
# "External scripting using" = Local.
#
# Guardrails (OSIDE-PLAN-2 Phase 4): this module only CREATES projects and
# ADDS media/timelines. It never deletes, never overwrites an existing
# project, and never touches template projects beyond exporting a .drp copy.
# ============================================================================

import os
import sys
import time
import subprocess

RESOLVE_MODULES = (
    "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
)
RESOLVE_APP = "/Applications/DaVinci Resolve/DaVinci Resolve.app"


class ResolveError(RuntimeError):
    pass


def connect():
    """Return the Resolve app object, or raise ResolveError with a fix hint."""
    if RESOLVE_MODULES not in sys.path:
        sys.path.append(RESOLVE_MODULES)
    os.environ.setdefault("RESOLVE_SCRIPT_API", os.path.dirname(RESOLVE_MODULES))
    os.environ.setdefault(
        "RESOLVE_SCRIPT_LIB", f"{RESOLVE_APP}/Contents/Libraries/Fusion/fusionscript.so"
    )
    try:
        import DaVinciResolveScript as dvr  # noqa: N813
    except ImportError as e:
        raise ResolveError(f"DaVinciResolveScript module not found: {e}") from e
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise ResolveError(
            "Cannot reach DaVinci Resolve. Is it running, and is Preferences → "
            "System → General → 'External scripting using' set to Local?"
        )
    return resolve


def launch_and_connect(timeout_s: int = 60):
    """Open Resolve if needed, then poll until scripting answers."""
    try:
        return connect()
    except ResolveError:
        pass
    subprocess.run(["open", "-a", RESOLVE_APP], check=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(3)
        try:
            return connect()
        except ResolveError:
            continue
    raise ResolveError(f"Resolve did not answer scripting within {timeout_s}s of launch.")


def project_manager(resolve):
    pm = resolve.GetProjectManager()
    if pm is None:
        raise ResolveError("GetProjectManager returned nothing — Resolve may still be starting.")
    return pm


def project_names(pm) -> list[str]:
    return list(pm.GetProjectListInCurrentFolder() or [])


def export_project(pm, name: str, drp_path: str) -> None:
    """Export an existing project to a .drp (the template snapshot)."""
    if name not in project_names(pm):
        raise ResolveError(f"No project named {name!r} in the current project library folder.")
    if not pm.ExportProject(name, drp_path):
        raise ResolveError(f"ExportProject failed for {name!r} → {drp_path}")


def create_from_template(pm, drp_path: str, new_name: str):
    """Import a .drp under a NEW name and open it. Refuses to shadow a project."""
    if not os.path.isfile(drp_path):
        raise ResolveError(f"Template file missing: {drp_path}")
    if new_name in project_names(pm):
        raise ResolveError(f"A project named {new_name!r} already exists — pick another name.")
    if not pm.ImportProject(drp_path, new_name):
        raise ResolveError(f"ImportProject failed ({drp_path} as {new_name!r}).")
    project = pm.LoadProject(new_name)
    if project is None:
        raise ResolveError(f"Imported but could not open {new_name!r}.")
    return project


def find_bin(media_pool, name: str):
    """Depth-first search of the media pool for a bin by (case-insensitive) name."""
    root = media_pool.GetRootFolder()
    stack = [root]
    while stack:
        folder = stack.pop()
        if folder.GetName().strip().lower() == name.strip().lower():
            return folder
        stack.extend(folder.GetSubFolderList() or [])
    return None


def ensure_bin(media_pool, name: str):
    """The named bin, created under the root when the template lacks it."""
    found = find_bin(media_pool, name)
    if found is not None:
        return found
    created = media_pool.AddSubFolder(media_pool.GetRootFolder(), name)
    if created is None:
        raise ResolveError(f"Could not create bin {name!r}.")
    return created


def import_into_bin(media_pool, folder, paths: list[str]):
    """Import files into a specific bin; returns the created media pool items."""
    if not media_pool.SetCurrentFolder(folder):
        raise ResolveError(f"Could not enter bin {folder.GetName()!r}.")
    items = media_pool.ImportMedia(paths)
    return items or []


def clips_by_filename(folder) -> dict:
    """filename → MediaPoolItem for every clip in a bin."""
    out = {}
    for clip in folder.GetClipList() or []:
        out[clip.GetName()] = clip
    return out


def build_timeline(project, media_pool, name: str, video_items: list, markers: list[dict]):
    """New timeline named `name`; clips appended in order; one marker per clip.

    markers: [{"name": str, "note": str}] aligned with video_items.
    Returns the timeline. Never touches existing timelines.
    """
    existing = [
        project.GetTimelineByIndex(i + 1).GetName() for i in range(project.GetTimelineCount())
    ]
    if name in existing:
        raise ResolveError(f"A timeline named {name!r} already exists — pick another name.")
    # a timeline is created in the CURRENT bin — after imports that's VO, so
    # file it where it belongs: the template's TIMELINES bin (root fallback)
    home = find_bin(media_pool, "TIMELINES") or media_pool.GetRootFolder()
    media_pool.SetCurrentFolder(home)
    timeline = media_pool.CreateEmptyTimeline(name)
    if timeline is None:
        raise ResolveError(f"CreateEmptyTimeline failed for {name!r}.")
    project.SetCurrentTimeline(name)
    if video_items:
        if not media_pool.AppendToTimeline(video_items):
            raise ResolveError("AppendToTimeline failed for the video clips.")
    # markers ride the timeline start of each appended clip
    timeline = project.GetCurrentTimeline()
    start = timeline.GetStartFrame()
    track_items = timeline.GetItemListInTrack("video", 1) or []
    for item, marker in zip(track_items, markers):
        frame = int(item.GetStart()) - int(start)
        timeline.AddMarker(frame, "Blue", marker["name"], marker.get("note", ""), 1)
    return timeline


def append_audio(media_pool, timeline, audio_items: list) -> bool:
    """Lay VO at the head of the timeline (A1). Falls back to a plain append —
    the clip still arrives, just not at 00:00 — and reports which happened."""
    placed_at_head = True
    start = timeline.GetStartFrame()
    for item in audio_items:
        frames = item.GetClipProperty("Frames")
        try:
            end = int(frames) if frames else None
        except (TypeError, ValueError):
            end = None
        clip_info = {
            "mediaPoolItem": item,
            "trackIndex": 1,
            "mediaType": 2,  # audio-only
            "recordFrame": int(start),
        }
        if end:
            clip_info.update({"startFrame": 0, "endFrame": end - 1})
        if not media_pool.AppendToTimeline([clip_info]):
            placed_at_head = False
            if not media_pool.AppendToTimeline([item]):
                raise ResolveError(f"Could not add VO clip {item.GetName()!r} to the timeline.")
    return placed_at_head
