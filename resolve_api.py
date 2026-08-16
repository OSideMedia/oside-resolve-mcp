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

# Marker vocabulary. Shot markers stay Blue points (the 2026-07 contract);
# dialogue cues are RANGE markers in any other Resolve colour, one colour per
# speaker. Both carry a customData tag so verify_import can tell them apart
# without leaning on colour.
SHOT_MARKER_COLOR = "Blue"
SHOT_TAG = "oside:shot"
CUE_TAG = "oside:cue"
CUE_MARKER_COLORS = (
    "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose",
    "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream",
)


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
    # markers ride the timeline start of each appended clip. Tagged through
    # customData so verify_import can tell a shot marker from a cue marker by
    # something firmer than its colour.
    timeline = project.GetCurrentTimeline()
    start = timeline.GetStartFrame()
    track_items = timeline.GetItemListInTrack("video", 1) or []
    for item, marker in zip(track_items, markers):
        frame = int(item.GetStart()) - int(start)
        timeline.AddMarker(frame, SHOT_MARKER_COLOR, marker["name"], marker.get("note", ""), 1, SHOT_TAG)
    return timeline


def add_range_markers(timeline, rows: list[dict]) -> dict:
    """Range markers for dialogue cues — one per scripted line, under its shot.

    rows: [{"frame": int, "duration": int, "color": str, "name": str, "note": str}]
      frame is TIMELINE-RELATIVE (0 = the timeline's first frame), the same
      reference the shot markers use.

    Resolve keys markers by frame — one marker per frame, and AddMarker simply
    answers False when the slot is taken. A cue whose computed frame is already
    occupied (a long line running past its clip into the next shot's Blue
    marker, or a colliding cue) is nudged forward a few frames before it is
    given up on; a skipped cue is REPORTED, never silently dropped.
    """
    # ponytail: 4-frame nudge window; a proper free-slot search if cue-dense boards need it
    placed, skipped = 0, []
    for r in rows:
        frame = r.get("frame")
        if frame is None:
            skipped.append({"name": r["name"], "reason": "no frame (shot has no clip on V1)"})
            continue
        ok = False
        for bump in range(0, 4):
            if timeline.AddMarker(int(frame) + bump, r["color"], r["name"], r.get("note", ""), max(1, int(r.get("duration") or 1)), CUE_TAG):
                ok = True
                break
        if ok:
            placed += 1
        else:
            skipped.append({"name": r["name"], "frame": frame, "reason": "AddMarker refused (frame occupied)"})
    return {"placed": placed, "skipped": skipped}


def timeline_by_name(project, name: str | None):
    """The named timeline (or the current one when name is None) WITHOUT
    switching the current timeline — verify_import must not move the editor."""
    if name is None:
        return project.GetCurrentTimeline()
    for i in range(project.GetTimelineCount()):
        tl = project.GetTimelineByIndex(i + 1)
        if tl is not None and tl.GetName() == name:
            return tl
    return None


def observe_timeline(timeline) -> dict:
    """Read a timeline into plain data (timeline-relative frames) so the verify
    checks are pure functions of the manifest + this dict, testable without
    Resolve."""
    start = int(timeline.GetStartFrame())

    def items(track_type: str, index: int) -> list[dict]:
        out = []
        for it in timeline.GetItemListInTrack(track_type, index) or []:
            try:
                out.append({
                    "name": it.GetName(),
                    "start": int(it.GetStart()) - start,
                    "duration": int(it.GetDuration()),
                })
            except (AttributeError, TypeError, ValueError):
                continue
        return out

    markers = {}
    for frame, m in (timeline.GetMarkers() or {}).items():
        markers[int(frame)] = {
            "color": m.get("color"),
            "name": m.get("name"),
            "note": m.get("note"),
            "duration": m.get("duration"),
            "customData": m.get("customData") or "",
        }
    return {"name": timeline.GetName(), "v1": items("video", 1), "a1": items("audio", 1), "markers": markers}


def clip_duration_frames(item) -> int | None:
    """A media-pool item's length in frames, or None when Resolve won't say."""
    try:
        frames = item.GetClipProperty("Frames")
        return int(frames) if frames else None
    except (AttributeError, TypeError, ValueError):
        return None


def video_start_frames(timeline, video_items: list) -> dict:
    """Where each appended clip actually LANDED on V1: {clip name -> recordFrame}.

    The caller already has the media-pool items in append order and Resolve
    lays them in that same order, so zipping the track items back against them
    is the same correspondence the marker loop above relies on.

    Exists so shot-attached audio can sit UNDER its clip instead of stacking at
    00:00. Keyed by the media-pool item's own name (which is the filename), so
    the caller can look up by manifest basename.
    """
    track_items = timeline.GetItemListInTrack("video", 1) or []
    frames = {}
    for pool_item, track_item in zip(video_items, track_items):
        try:
            frames[pool_item.GetName()] = int(track_item.GetStart())
        except (AttributeError, TypeError, ValueError):
            continue
    return frames


def append_audio(media_pool, timeline, placements: list) -> dict:
    """Lay each VO clip on A1 — UNDER ITS OWN SHOT when the package says which
    shot it belongs to, at the head otherwise.

    placements: [{"item": mediaPoolItem, "recordFrame": int | None, "label": str}]
      recordFrame None  -> the head of the timeline (a board-wide VO, or a pin
                           whose shot has no clip in this package).

    WHY THIS EXISTS. OSIDE's DaVinci export pins narration takes to individual
    shots and writes `manifest.audio[].placements` naming the owning scene, shot
    and clip file. This end used to ignore all of that and stamp every clip at
    `timeline.GetStartFrame()`, so a board with four shot-attached takes handed
    the editor four clips piled on top of each other at 00:00 — the files were
    right, the timeline was not, and the only fix was dragging each one by hand
    [council 2026-08-07].

    Returns a report rather than a bool: with per-shot placement there are now
    three outcomes worth telling the editor apart — placed under its shot,
    parked at the head, or appended loose because Resolve refused the frame.
    """
    start = int(timeline.GetStartFrame())
    report = {"underShot": 0, "atHead": 0, "loose": 0, "looseLabels": []}

    for p in placements:
        item = p["item"]
        target = p.get("recordFrame")
        at_head = target is None
        end = clip_duration_frames(item)
        clip_info = {
            "mediaPoolItem": item,
            "trackIndex": 1,
            "mediaType": 2,  # audio-only
            "recordFrame": start if at_head else int(target),
        }
        if end:
            clip_info.update({"startFrame": 0, "endFrame": end - 1})
        if media_pool.AppendToTimeline([clip_info]):
            report["atHead" if at_head else "underShot"] += 1
            continue
        # Resolve refused the exact frame (an occupied slot on A1 is the usual
        # cause). The clip still has to ARRIVE — a silent drop is the one
        # outcome worse than a misplaced clip.
        if not media_pool.AppendToTimeline([item]):
            raise ResolveError(f"Could not add VO clip {item.GetName()!r} to the timeline.")
        report["loose"] += 1
        report["looseLabels"].append(p.get("label") or item.GetName())

    return report
