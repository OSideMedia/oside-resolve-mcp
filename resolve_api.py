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
import shutil
import sys
import tempfile
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
# Narration lives on ITS OWN audio track. Clips with embedded audio (Seedance
# 2.x default) fill A1 when the video is appended, and Resolve's
# AppendToTimeline answers a truthy list for an audio clip aimed at an
# occupied A1 frame while placing NOTHING [live walk 2026-08-16].
VO_TRACK_NAME = "VO"
CUE_TAG = "oside:cue"
CUE_MARKER_COLORS = (
    "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose",
    "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream",
)
# FILM STOCK INTENT — one marker at the head, a note for the colourist and
# nothing else: no node, no LUT, no Film Look Creator. Resolve's palette is
# fully spoken for (Blue = shots, the rest = cue speakers), so the COLOUR is
# not a unique signal here and the customData TAG is what tells them apart —
# the same contract the shot/cue markers already work under.
STOCK_TAG = "oside:stock"
STOCK_MARKER_COLOR = "Cocoa"
# IN-FRAME TEXT — the lettering a shot asked for and the generation deliberately
# did NOT render (2026-08-17, OSIDE v0.160.0). Video models re-spell text between
# takes, so a sign that changes across two shots of one scene is a continuity
# break no re-roll reliably fixes; the studio's answer is to describe the surface,
# leave the words out, and lay the real text as a title over the clip HERE. These
# markers are that worklist, on the timeline where the editor works, one per run
# of text. Same contract as the others: the customData TAG is the signal, not the
# colour (the palette is spoken for).
TEXT_TASK_TAG = "oside:text"
TEXT_TASK_MARKER_COLOR = "Cream"


class ResolveError(RuntimeError):
    pass


def marker_frames(timeline) -> set:
    """Every frame currently carrying a marker, as ints."""
    try:
        return {int(float(k)) for k in (timeline.GetMarkers() or {})}
    except (AttributeError, TypeError, ValueError):
        return set()


def first_free_frame(occupied: set, start, window: int, limit=None):
    """The first frame at or after `start` that no marker holds, searched in a
    SET READ ONCE — never by trying writes until one sticks. None when the
    window (or the clip boundary `limit`) runs out first."""
    for bump in range(window):
        at = int(start) + bump
        if limit is not None and at >= int(limit):
            return None
        if at not in occupied:
            return at
    return None


def add_marker_once(timeline, occupied: set, frame, color, name, note, duration, tag) -> bool:
    """ONE AddMarker attempt at a frame already known to be free, then record it
    as taken in `occupied` so the caller never re-reads to find its next slot.

    Collisions are resolved UP FRONT from a single marker read
    (`first_free_frame`) instead of by trying writes until one sticks. Resolve
    will not resolve a collision for you, so the search belongs in our own
    bookkeeping; and a loop over writes is the shape that turns one surprising
    write into a pile of markers, which is exactly what a stray repeated call
    produced during the 2026-08-24 walk.

    Honest scope note: AddMarker's return value was measured on Resolve 21.0.4.5
    and is RELIABLE — it answers False on an occupied frame and True on a free
    one, including for markers placed past the end of the timeline. An earlier
    version of this docstring claimed a measured false-negative; that was my own
    test harness calling apply_look eight times (a dict comprehension evaluating
    its call once per key), not a Resolve defect. Recorded here because a
    fabricated vendor quirk in a comment outlives the session that invented it.
    """
    at = int(frame)
    try:
        timeline.AddMarker(at, color, name, note, duration, tag)
    except (AttributeError, TypeError, ValueError):
        return False
    occupied.add(at)
    return True


def connect():
    """Return the Resolve app object, or raise ResolveError with a fix hint.
    OSIDE_RESOLVE_OFFLINE=1 in the environment makes this raise without
    looking — the self-test's way of keeping its offline cases offline while a
    real Resolve is running on the same machine."""
    if os.environ.get("OSIDE_RESOLVE_OFFLINE"):
        raise ResolveError("Resolve connection disabled (OSIDE_RESOLVE_OFFLINE is set).")
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
    # SetCurrentTimeline takes a TIMELINE OBJECT, not a name — Blackmagic's own
    # reference is explicit (Developer/Scripting/README.txt: "SetCurrentTimeline
    # (timeline) --> Bool"). This used to pass `name`, and the call is a no-op on
    # a string: the build then appended into WHATEVER TIMELINE WAS CURRENT, which
    # on a fresh template copy is one of the four timelines the .drp ships with —
    # i.e. it could silently edit a template timeline, the one thing the
    # create-only guardrail exists to forbid. It went unseen because the test
    # fake was written to accept a string [audit 2026-08-24].
    # We already hold the object CreateEmptyTimeline returned; use it, and only
    # fall back to reading current if Resolve refuses the switch.
    if not project.SetCurrentTimeline(timeline):
        current = project.GetCurrentTimeline()
        if current is None or current.GetName() != name:
            raise ResolveError(
                f"Could not make {name!r} the current timeline — refusing to build "
                "into whichever timeline is open instead."
            )
        timeline = current
    if video_items:
        if not media_pool.AppendToTimeline(video_items):
            raise ResolveError("AppendToTimeline failed for the video clips.")
    # markers ride the timeline start of each appended clip. Tagged through
    # customData so verify_import can tell a shot marker from a cue marker by
    # something firmer than its colour.
    start = timeline.GetStartFrame()
    track_items = timeline.GetItemListInTrack("video", 1) or []
    # Reconcile before marking: zip() truncates silently, so a Resolve that laid
    # fewer clips than it was handed (an unreadable/offline source) would get
    # markers stamped onto the WRONG pictures and a report claiming success.
    if len(track_items) != len(markers):
        raise ResolveError(
            f"Resolve laid {len(track_items)} clip(s) on V1 but {len(markers)} were sent — "
            f"timeline {name!r} is incomplete; nothing further was written to it."
        )
    occupied = marker_frames(timeline)
    for item, marker in zip(track_items, markers):
        frame = int(item.GetStart()) - int(start)
        if frame in occupied:
            raise ResolveError(
                f"Frame {frame} already carries a marker — refusing to build "
                f"{name!r} over an existing marker set."
            )
        add_marker_once(timeline, occupied, frame, SHOT_MARKER_COLOR, marker["name"],
                        marker.get("note", ""), 1, SHOT_TAG)
    return timeline


def add_range_markers(timeline, rows: list[dict]) -> dict:
    """Range markers for dialogue cues — one per scripted line, under its shot.

    rows: [{"frame": int, "duration": int, "color": str, "name": str, "note": str,
            "limit": int | None}]
      frame is TIMELINE-RELATIVE (0 = the timeline's first frame), the same
      reference the shot markers use. `limit` is the first frame PAST this cue's
      own shot — the nudge never crosses it.

    Resolve keys markers by frame — one marker per frame, and AddMarker simply
    answers False when the slot is taken. A cue whose computed frame is already
    occupied (a colliding cue) is nudged forward a few frames before it is
    given up on; a skipped cue is REPORTED, never silently dropped.

    THE NUDGE STOPS AT THE CLIP BOUNDARY. Without `limit` this loop would walk a
    cue onto the NEXT shot and report it `placed` — defeating the boundary guard
    the planner applies, because the guard lives in handoff.py and the transform
    lives here [audit 2026-08-24]. A marker on the wrong picture tells the editor
    the wrong line belongs to this shot, which is worse than no marker.
    """
    # ponytail: 4-frame search window; widen only if cue-dense boards need it
    occupied = marker_frames(timeline)
    placed, skipped = 0, []
    for r in rows:
        frame = r.get("frame")
        if frame is None:
            skipped.append({"name": r["name"],
                            "reason": r.get("reason") or "no frame (shot has no clip on V1)"})
            continue
        at = first_free_frame(occupied, frame, 4, r.get("limit"))
        if at is None:
            skipped.append({"name": r["name"], "frame": frame,
                            "reason": "no free frame inside this shot"})
            continue
        add_marker_once(timeline, occupied, at, r["color"], r["name"], r.get("note", ""),
                        max(1, int(r.get("duration") or 1)), CUE_TAG)
        placed += 1
    return {"placed": placed, "skipped": skipped}


def add_stock_marker(timeline, text: str) -> dict:
    """The film-stock intent, as ONE marker at the head of the timeline.

    A film stock in Resolve is a CREATIVE GRADE, and `apply_look`'s whole
    promise is a starting balance — so the stock never becomes a node, a LUT or
    a Film Look Creator preset. It travels as a note the colourist can read and
    act on, or ignore.

    Frame 0 normally already holds shot 1's Blue marker (Resolve keeps one
    marker per frame and AddMarker just answers False when the slot is taken),
    so this nudges forward a few frames the way the dialogue cues do, and
    REPORTS the frame it landed on. A marker it could not place is reported as
    skipped — never silently dropped.
    """
    # IDEMPOTENT. apply_look is documented as re-runnable ("absolute values, a
    # re-run resets node 1 to the same CDL") and the CDL half is — but the stock
    # marker was not: every run added ANOTHER one, so re-applying a look to a
    # timeline three times left three "Stock intent" markers a few frames apart.
    # Found on the 2026-08-24 walk, when a harness bug called apply_look eight
    # times and six markers piled up at the head of the timeline. The harness bug
    # was mine; the accumulation it exposed is real, and a colourist re-running
    # the look would have hit it [audit 2026-08-24].
    existing = None
    for k, m in (timeline.GetMarkers() or {}).items():
        if (m.get("customData") or "") == STOCK_TAG:
            existing = int(float(k))
            break
    if existing is not None:
        return {"placed": True, "frame": existing, "note": text, "alreadyPresent": True}
    # ponytail: 8-frame search window, same shape as the cue search
    occupied = marker_frames(timeline)
    at = first_free_frame(occupied, 0, 8)
    if at is None:
        return {"placed": False, "reason": "no free frame in 0-7 (all occupied)", "note": text}
    add_marker_once(timeline, occupied, at, STOCK_MARKER_COLOR, "Stock intent", text, 1, STOCK_TAG)
    return {"placed": True, "frame": at, "note": text}


def add_text_task_markers(timeline, rows: list[dict]) -> dict:
    """The in-frame-text worklist — one POINT marker per run of lettering, on
    the shot that asked for it.

    rows: [{"frame": int|None, "name": str, "note": str, "limit": int | None}]
      frame is TIMELINE-RELATIVE, the same reference the shot and cue markers
      use. `limit` is the first frame PAST this task's own clip. A point marker,
      not a range: a title has no duration until the editor gives it one, and
      inventing a length here would be a claim about the cut.

    Nudge-and-report, the same shape as the cue and stock markers, because the
    frames near a shot's head are the contested ones — shot 1's Blue marker sits
    on frame 0 and the dialogue cues cascade from frame 1. A marker that cannot
    find a free slot is REPORTED, never silently dropped.

    THE NUDGE STOPS AT THE CLIP BOUNDARY. text_task_rows refuses to plan a task
    past its own clip; without `limit` this loop walked straight past that
    decision and landed the marker on the next shot anyway, reporting `placed`
    [audit 2026-08-24].
    """
    # ponytail: 12-frame search window — wider than the cues' 4 because text
    # tasks are placed AFTER them and therefore start further into a busy shot.
    occupied = marker_frames(timeline)
    placed, skipped = 0, []
    for r in rows:
        frame = r.get("frame")
        if frame is None:
            skipped.append({"name": r["name"],
                            "reason": r.get("reason") or "no frame (shot has no clip on V1)"})
            continue
        at = first_free_frame(occupied, frame, 12, r.get("limit"))
        if at is None:
            skipped.append({"name": r["name"], "frame": frame,
                            "reason": "no free frame inside this shot"})
            continue
        add_marker_once(timeline, occupied, at, TEXT_TASK_MARKER_COLOR, r["name"],
                        r.get("note", ""), 1, TEXT_TASK_TAG)
        placed += 1
    return {"placed": placed, "skipped": skipped}


def export_timeline_cdl(resolve, timeline) -> str | None:
    """Export the timeline as an EDL carrying ASC CDL and return its TEXT.

    This is the only read-back Resolve offers for a grade: there is no `GetCDL`,
    but `Timeline.Export(path, EXPORT_EDL, EXPORT_CDL)` writes the applied
    values (proven on the 2026-08-24 walk — the file came back with exactly the
    numbers just set). The enum constants must come off the LIVE resolve handle;
    passing the strings is silently rejected.

    Writes into a temp dir and removes it: the read-back is evidence for one
    call, not an artefact to leave beside the package.
    """
    tmp = tempfile.mkdtemp(prefix="oside-cdl-")
    path = os.path.join(tmp, "readback.edl")
    try:
        ok = timeline.Export(path, resolve.EXPORT_EDL, resolve.EXPORT_CDL)
        # SIZE, not just existence. MEASURED 2026-08-25 on 21.0.4.5: exporting a
        # populated timeline as EXPORT_ALE_CDL returns True and writes a
        # ZERO-BYTE file. `Timeline.Export` therefore lies in a second way — the
        # documented trap is that a string enum is silently rejected with no file
        # written, but a live enum can also answer True over an empty one. An
        # existence check passes a 0-byte file, and the read-back then reports
        # "no CDL for this clip" for EVERY clip, which blames the grade for a
        # failure of the export.
        if not ok or not os.path.isfile(path) or os.path.getsize(path) == 0:
            return None
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except (AttributeError, TypeError, OSError):
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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


def track_items(timeline, track_type: str, index: int, start: int | None = None) -> list[dict]:
    """The items on one track as plain data, timeline-relative frames."""
    if start is None:
        start = int(timeline.GetStartFrame())
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


def track_name(timeline, track_type: str, index: int) -> str:
    try:
        return str(timeline.GetTrackName(track_type, index) or "")
    except (AttributeError, TypeError):
        return ""


def audio_track_count(timeline) -> int:
    try:
        return int(timeline.GetTrackCount("audio") or 0)
    except (AttributeError, TypeError, ValueError):
        return 1


def observe_timeline(timeline) -> dict:
    """Read a timeline into plain data (timeline-relative frames) so the verify
    checks are pure functions of the manifest + this dict, testable without
    Resolve. `audioTracks` lists EVERY audio track (index, name, items) — the
    VO check reads them all and reports which track each take sits on; `a1`
    stays for older callers."""
    start = int(timeline.GetStartFrame())
    audio_tracks = []
    for idx in range(1, audio_track_count(timeline) + 1):
        audio_tracks.append({
            "index": idx,
            "name": track_name(timeline, "audio", idx) or f"A{idx}",
            "items": track_items(timeline, "audio", idx, start),
        })
    if not audio_tracks:
        audio_tracks = [{"index": 1, "name": "A1", "items": track_items(timeline, "audio", 1, start)}]

    markers = {}
    for frame, m in (timeline.GetMarkers() or {}).items():
        markers[int(frame)] = {
            "color": m.get("color"),
            "name": m.get("name"),
            "note": m.get("note"),
            "duration": m.get("duration"),
            "customData": m.get("customData") or "",
        }
    return {
        "name": timeline.GetName(),
        "v1": track_items(timeline, "video", 1, start),
        "a1": audio_tracks[0]["items"],
        "audioTracks": audio_tracks,
        "markers": markers,
    }


def timecode_to_frames(tc: str, fps) -> int | None:
    """'00:00:05:01' at 23.976 → 121. Timecode counts whole frames at the
    rounded rate (23.976 → 24, 29.97 → 30); drop-frame separators (';') are
    read the same way — VO clips are seconds long, the drift is nil."""
    if not tc or not isinstance(tc, str):
        return None
    parts = tc.replace(";", ":").split(":")
    if len(parts) != 4:
        return None
    try:
        h, m, s_, f = (int(x) for x in parts)
        base = int(round(float(fps))) if fps else 24
    except (TypeError, ValueError):
        return None
    return ((h * 60 + m) * 60 + s_) * base + f


def clip_duration_frames(item, timeline_fps: float | None = None) -> int | None:
    """A media-pool item's length in frames, or None when Resolve won't say.

    Video items answer `Frames`; AUDIO items answer '' there and only carry
    `Duration` as timecode at the item's `FPS` [live walk 2026-08-16] — read
    that, and fall back to ffprobe on the file when even that is blank.

    `Frames` is the count in the CLIP'S OWN rate. Pass `timeline_fps` to get the
    number of TIMELINE frames the clip will occupy — they are not the same
    number whenever the source rate differs from the timeline's. Every OSIDE
    render lands at exactly 24 fps (measured across the live-fire library
    2026-08-24) while the explainer template runs at 60, so a 5.04 s clip
    reporting 121 source frames actually occupies 303 frames on an explainer
    timeline. Without the conversion the connected dry run was wrong by 182
    frames per clip on every explainer board, and every cue frame computed from
    those starts was wrong with it. Cinematic escapes only by luck: 24 on 23.976
    conforms frame-for-frame."""
    def _conform(frames: int, src_fps) -> int:
        try:
            src = float(src_fps or 0)
        except (TypeError, ValueError):
            return frames
        if not timeline_fps or not src or abs(src - float(timeline_fps)) < 0.05:
            return frames
        return max(1, int(round(frames / src * float(timeline_fps))))

    try:
        frames = item.GetClipProperty("Frames")
        if frames not in (None, ""):
            return _conform(int(frames), item.GetClipProperty("FPS"))
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        fps = item.GetClipProperty("FPS")
        n = timecode_to_frames(item.GetClipProperty("Duration"), fps)
        if n:
            return _conform(n, fps)
    except (AttributeError, TypeError, ValueError):
        pass
    try:
        path = item.GetClipProperty("File Path")
        fps = float(item.GetClipProperty("FPS") or 0) or None
    except (AttributeError, TypeError, ValueError):
        return None
    if path and fps:
        # local import: handoff owns the ffprobe helper and imports this module
        from handoff import probe_duration_seconds, seconds_to_frames
        return seconds_to_frames(probe_duration_seconds(path), fps)
    return None


# `video_start_frames` lived here until 2026-08-24. It was dead (grep found only
# its own definition) AND it returned ABSOLUTE frames where every sibling in this
# module returns timeline-relative ones — so the next caller to reach for it by
# name would have laid every marker and VO take an hour into the timeline (a
# default project starts at frame 86400). server.build_timeline does the job now,
# from observe_timeline. Deleted rather than left as a trap.


def ensure_vo_track(timeline) -> int:
    """The index of the timeline's VO audio track — an existing track named
    VO_TRACK_NAME, else a NEW audio track (named VO when the API can name it).
    Never A1: the video clips' embedded audio owns A1."""
    count = audio_track_count(timeline)
    for idx in range(1, count + 1):
        if track_name(timeline, "audio", idx).strip().lower() == VO_TRACK_NAME.lower():
            return idx
    added = False
    try:
        added = bool(timeline.AddTrack("audio"))
    except (AttributeError, TypeError):
        added = False
    if not added:
        raise ResolveError("AddTrack('audio') failed — could not create the VO track.")
    idx = audio_track_count(timeline)
    if idx <= count:
        raise ResolveError("AddTrack('audio') answered True but the track count did not grow.")
    try:
        timeline.SetTrackName("audio", idx, VO_TRACK_NAME)
    except (AttributeError, TypeError):
        pass  # unnamed A<n> is still its own track — the placement is what matters
    return idx


def _find_new_item(before: list[dict], after: list[dict], name: str, frame: int | None):
    """The item that APPEARED between two reads of a track (by name, and at
    `frame` when one is given). Resolve's AppendToTimeline answers a truthy
    list for an audio clip aimed at an occupied frame while placing nothing,
    so its return value is never trusted — the track is re-read instead."""
    if len(after) <= len(before):
        return None
    seen = {(it["name"], it["start"]) for it in before}
    for it in after:
        if it["name"] == name and (it["name"], it["start"]) not in seen:
            if frame is None or it["start"] == frame:
                return it
    return None


def append_audio(media_pool, timeline, placements: list, track_index: int) -> dict:
    """Lay each VO clip on the VO track (`track_index`) — UNDER ITS OWN SHOT
    when the package says which shot it belongs to, at the head otherwise.

    placements: [{"item": mediaPoolItem, "recordFrame": int | None, "label": str}]
      recordFrame None  -> the head of the timeline (a board-wide VO, or a pin
                           whose shot has no clip in this package).

    WHY THIS EXISTS. OSIDE's DaVinci export pins narration takes to individual
    shots and writes `manifest.audio[].placements` naming the owning scene, shot
    and clip file. This end used to ignore all of that and stamp every clip at
    `timeline.GetStartFrame()`, so a board with four shot-attached takes handed
    the editor four clips piled on top of each other at 00:00 [council 2026-08-07].

    WHY ITS OWN TRACK, AND WHY THE TRACK IS RE-READ. On the first live walk
    (2026-08-16) every shot clip carried embedded audio, so appending the
    video filled A1; asking for A1 at an occupied frame got `[<PyRemoteObject>]`
    back — truthy — while Resolve placed NOTHING, and the build reported two
    VO clips that did not exist. Placement is therefore judged by re-reading
    the track after each append (a new item, by name, at the frame asked for),
    never by the return value.

    Returns a report: placed under its shot, parked at the head, or appended
    loose (still on the VO track, at its tail) because the exact frame was
    refused — a silent drop is the one outcome worse than a misplaced clip.
    """
    start = int(timeline.GetStartFrame())
    report = {"underShot": 0, "atHead": 0, "loose": 0, "looseLabels": [],
              "track": track_index, "trackName": track_name(timeline, "audio", track_index) or f"A{track_index}"}

    def read():
        return track_items(timeline, "audio", track_index, start)

    for p in placements:
        item = p["item"]
        name = item.GetName()
        target = p.get("recordFrame")
        at_head = target is None
        end = clip_duration_frames(item)
        record = start if at_head else int(target)
        clip_info = {
            "mediaPoolItem": item,
            "trackIndex": track_index,
            "mediaType": 2,  # audio-only
            "recordFrame": record,
        }
        if end:
            # endFrame is EXCLUSIVE (measured 2026-08-16: a 47-frame clip with
            # endFrame 47 lands as 47 frames, endFrame 46 as 46)
            clip_info.update({"startFrame": 0, "endFrame": end})
        before = read()
        media_pool.AppendToTimeline([clip_info])  # return value NOT trusted
        after = read()
        if _find_new_item(before, after, name, record - start) is not None:
            report["atHead" if at_head else "underShot"] += 1
            continue
        # Did it arrive somewhere ELSE? Matching on the exact frame alone meant a
        # placement Resolve honoured at a shifted frame read as "nothing was
        # placed", and the tail retry below then appended a SECOND copy of the
        # take before raising an error saying nothing had been placed at all
        # [audit 2026-08-24]. Look by name first; a take that arrived is `loose`,
        # not a reason to append it twice.
        shifted = _find_new_item(before, after, name, None)
        if shifted is not None:
            report["loose"] += 1
            report["looseLabels"].append(p.get("label") or name)
            continue
        # The exact frame was refused (an occupied slot on the VO track — two
        # head takes, or overlapping pins). The clip still has to ARRIVE: lay
        # it at the VO track's tail and re-read again.
        tail = max((it["start"] + it["duration"] for it in before), default=0)
        clip_info["recordFrame"] = start + tail
        before = read()
        media_pool.AppendToTimeline([clip_info])
        if _find_new_item(before, read(), name, tail) is None:
            raise ResolveError(
                f"Could not add VO clip {name!r} to the timeline (track {track_index}): "
                "Resolve placed nothing at the pinned frame or at the track tail."
            )
        report["loose"] += 1
        report["looseLabels"].append(p.get("label") or name)

    return report
