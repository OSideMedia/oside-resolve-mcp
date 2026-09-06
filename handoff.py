# ============================================================================
# handoff.py — the PURE half of the bridge: everything that can be decided
# from the manifest, the filesystem and plain observations WITHOUT a live
# Resolve object.
#
#   plan_timeline(...)   what build_timeline WOULD do — the dry-run plan, and
#                        the same rows the real build reports back
#   evaluate_verify(...) the acceptance gate as a per-check table over an
#                        observed timeline (see resolve_api.observe_timeline)
#   load_cues(...)       manifest.cues → the dialogue-cues.csv rows
#
# Kept free of Resolve imports so tests/ can drive both ends on synthetic data.
# Pattern from OpenChatCut (AGPL) — plan-before-touch and a real acceptance
# gate — restated here from scratch; no code or prose copied.
# ============================================================================

import csv
import json
import os
import re
import shutil
import subprocess

from resolve_api import (
    CUE_MARKER_COLORS, CUE_TAG, SHOT_MARKER_COLOR, SHOT_TAG, STOCK_TAG, TEXT_TASK_TAG,
    VO_TRACK_NAME,
)

MANIFEST_FORMAT = "oside-davinci/v1"
FEATURES = ["placements", "cues", "dry_run", "verify_v2", "vo_track", "look", "text_tasks",
            # 0.5.0 — the gate now BINDS each marker to the shot it names, scores
            # cue/text rows against what the build intended to place rather than
            # the raw worklist, and covers the stock marker and the VO track.
            "verify_v3",
            # 0.5.0 — the pipeline saves before it verifies, so the acceptance
            # read happens on the far side of the save boundary.
            "post_save",
            # 0.5.0 — a CDL outside the starting-balance envelope is refused per
            # clip instead of applied as if it were a balance.
            "cdl_envelope",
            # 0.5.0 — apply_look(verify=True) READS THE GRADE BACK out of an
            # EXPORT_EDL+EXPORT_CDL export instead of trusting SetCDL's return.
            "cdl_readback",
            # 0.6.0 — verify_import writes verify.json beside the manifest, one
            # row per clip keyed by the manifest's generationId, so the verdict
            # reaches OSIDE's ledger (PLAN-SHARED-GENERATION-ID step e).
            "verify_json"]

VERIFY_FORMAT = "oside-verify/1"
VERIFY_SIDECAR = "verify.json"


def verify_sidecar(manifest: dict, observed: dict, result: dict, verified_at: str) -> dict:
    """The per-clip verdict OSIDE reads back (PLAN-SHARED-GENERATION-ID step e).

    Until now `verify_import`'s result was RETURNED and never written anywhere:
    the manifest's per-clip `generationId` was emitted by OSIDE and read by
    nothing, so the first end-of-pipeline truth about a render never reached
    the row that paid for it. One row per manifest video, keyed by its
    generationId: `placed` = that basename sits on V1 of the observed timeline,
    with its start/duration in frames when it does. A video without a
    generationId (a hand-built manifest) is listed under `unkeyed` by file so
    nothing is silently dropped. Pure — the caller writes it."""
    tl = (observed or {}).get("timeline") or {}
    v1 = {it.get("name"): it for it in (tl.get("v1") or [])}
    clips: dict = {}
    unkeyed: list = []
    for v in manifest.get("videos") or []:
        name = os.path.basename(v.get("file", ""))
        item = v1.get(name)
        row = {
            "file": name,
            "placed": item is not None,
            "start": item.get("start") if item else None,
            "duration": item.get("duration") if item else None,
        }
        gid = v.get("generationId")
        if gid:
            clips[str(gid)] = row
        else:
            unkeyed.append(row)
    return {
        "format": VERIFY_FORMAT,
        "verifiedAt": verified_at,
        "manifest": {"project": manifest.get("project"), "kind": manifest.get("kind"),
                     "exportedAt": manifest.get("exportedAt")},
        "timeline": {"name": tl.get("name")},
        "overall": result.get("overall"),
        "clips": clips,
        **({"unkeyed": unkeyed} if unkeyed else {}),
    }


def write_verify_sidecar(base: str, sidecar: dict) -> str:
    """Write verify.json beside the manifest; returns the path. Overwrites — the
    latest verify is the truth, and verifiedAt says when."""
    path = os.path.join(base, VERIFY_SIDECAR)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=2)
        f.write("\n")
    return path

# The template a kind maps to fixes the timeline rate. templates/templates.json
# carries each template's `fps` (server reads it); this is the last-resort
# default when that file cannot be read.
KIND_FPS = {"cinematic": 23.976, "explainer": 60.0}

# What the dry run says about bin presence when it cannot look
BIN_UNKNOWN_OFFLINE = "unknown — Resolve not connected"
BIN_UNKNOWN_NOT_IMPORTED = "unknown — not imported yet"
BIN_UNKNOWN_NOT_OPEN = "unknown — target project exists but is not open"

# Resolve keeps ONE marker per frame. A cue laid exactly on its shot's first
# frame would fight the Blue shot marker there, so cues start one frame in.
CUE_FRAME_OFFSET = 1
# In-frame-text markers sit behind the cues on a shot that has them, and one
# frame further in than a cue on a shot that does not — so a silent shot's text
# task never lands on frame 1 where a cue would go if dialogue were added later.
TEXT_TASK_FRAME_OFFSET = 2


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def shot_word(manifest: dict) -> str:
    return "block" if manifest.get("kind") == "explainer" else "shot"


def scene_word(manifest: dict) -> str:
    return "section" if manifest.get("kind") == "explainer" else "scene"


def display_name(manifest: dict, v: dict) -> str:
    """'scene 1 shot 1A' — the wording the studio's own warnings use."""
    return f"{scene_word(manifest)} {v.get('sceneIndex', '?')} {shot_word(manifest)} {v.get('shotNumber', '?')}"


def kind_fps(manifest: dict) -> float:
    return KIND_FPS.get(manifest.get("kind") or "cinematic", KIND_FPS["cinematic"])


def seconds_to_frames(seconds, fps: float) -> int | None:
    if seconds is None:
        return None
    try:
        return max(1, int(round(float(seconds) * float(fps))))
    except (TypeError, ValueError):
        return None


def seconds_to_clip_frames(seconds, fps: float) -> int | None:
    """Seconds -> the number of TIMELINE frames a clip of that length occupies.

    FLOOR, not round — measured against Resolve 21.0.4.5 on the 2026-08-24 walk:
    a 5.041667 s source lands as 120 frames on a 23.976 timeline (round gives
    121) and as 302 on a 60 fps timeline (round gives 303). Flooring matched
    reality in both, so the dry run's clip starts now agree with the built
    timeline instead of drifting a frame per clip.

    `seconds_to_frames` keeps rounding and is still what CUE durations use: a
    marker's range is a reading aid, and shaving a frame off every spoken line
    is the wrong direction for that.
    """
    if seconds is None:
        return None
    try:
        return max(1, int(float(seconds) * float(fps)))
    except (TypeError, ValueError):
        return None


def probe_duration_seconds(path: str) -> float | None:
    """Clip length via ffprobe when it is on PATH; None otherwise. Only the
    dry run needs this — the real build reads lengths off the media pool."""
    if not shutil.which("ffprobe") or not os.path.isfile(path):
        return None
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=20, check=False,
        ).stdout.strip()
        return float(out) if out else None
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def cue_color_map(speakers) -> dict:
    """Speaker → Resolve marker colour, stable for a project: speakers are
    sorted, so the same cast gets the same colours on every export."""
    # ponytail: 15 colours then wrap — two speakers share a colour past that
    ordered = sorted({s for s in speakers if s})
    return {s: CUE_MARKER_COLORS[i % len(CUE_MARKER_COLORS)] for i, s in enumerate(ordered)}


# ---------------------------------------------------------------------------
# cues
# ---------------------------------------------------------------------------

def load_cues(manifest: dict, base: str) -> tuple[list[dict], str | None]:
    """The dialogue cues named by manifest.cues (a CSV filename written by the
    studio's export: Scene, Shot, Speaker, Line, Est s, File, Audio, Outcome).

    Returns (cues, warning). No key → ([], None). Named but missing on disk →
    ([], reason) — a cue file is advisory, its absence never blocks a build.
    """
    name = manifest.get("cues")
    if not name or not isinstance(name, str):
        return [], None
    path = os.path.join(base, name)
    if not os.path.isfile(path):
        return [], f"cues file named by the manifest is missing: {path}"
    cues = []
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            line = (row.get("Line") or "").strip()
            if not line:
                continue
            est = row.get("Est s")
            try:
                est_s = float(est) if est not in (None, "") else None
            except ValueError:
                est_s = None
            cues.append({
                "speaker": (row.get("Speaker") or "").strip() or "Dialogue",
                "line": line,
                "videoFile": (row.get("File") or "").strip(),
                "shotNumber": (row.get("Shot") or "").strip(),
                "sceneName": (row.get("Scene") or "").strip(),
                "estSeconds": est_s,
            })
    return cues, None


def load_text_tasks(manifest: dict, base: str) -> tuple[list[dict], str | None]:
    """The in-frame-text worklist named by manifest.textTasks (a CSV written by
    the studio's export: Scene, Shot, Text, Where it lands, File, Outcome).

    Same contract as load_cues, and for the same reason: no key → ([], None),
    named but missing on disk → ([], reason). The worklist is advisory — its
    absence never blocks a build, because a package exported before the studio
    learned to write it is still a valid package.
    """
    name = manifest.get("textTasks")
    if not name or not isinstance(name, str):
        return [], None
    path = os.path.join(base, name)
    if not os.path.isfile(path):
        return [], f"text-tasks file named by the manifest is missing: {path}"
    tasks = []
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            text = (row.get("Text") or "").strip()
            if not text:
                continue
            tasks.append({
                "text": text,
                "videoFile": (row.get("File") or "").strip(),
                "shotNumber": (row.get("Shot") or "").strip(),
                "sceneName": (row.get("Scene") or "").strip(),
            })
    return tasks, None


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------

def _basename(rel: str) -> str:
    return os.path.basename(rel or "")


def shot_marker_name(sw: str, v: dict) -> str:
    """The shot marker's NAME — the one place the plan and the verify gate must
    agree. RM-10: an unapproved take carries its outcome in the name."""
    outcome = (v.get("outcome") or "").strip().lower()
    base = f"{sw} {v.get('shotNumber', '?')}"
    return base + (f" · {outcome.upper()} TAKE" if outcome and outcome != "approved" else "")


def base_marker_name(name: str) -> str:
    """A marker name without its outcome suffix — the naming CONTRACT compares
    the base ("shot 2"), so a timeline built before 0.6.0 (no suffix) and one
    built after ("shot 2 · PENDING TAKE") both name their own shot."""
    return re.sub(r"\s·\s[A-Z_-]+ TAKE$", "", name or "")


def no_frame_reason(vf: str | None, starts_by_file: dict, start) -> str:
    """WHY a row has no frame — the clip is not on V1, or it IS planned but its
    position cannot be computed because a clip before it has no measurable
    duration. Before 0.6.0 both cases said "shot has no clip on V1", so an
    offline dry run without ffprobe blamed a clip that was on disk and planned
    (audit 2026-09-06 RM-11); `durationSource: "unknown"` was the only tell."""
    if not vf or vf not in starts_by_file:
        return "no frame (shot has no clip on V1)"
    if start is None:
        return ("frame unknown — the clip is planned on V1 but a clip before it has no "
                "measurable duration (durationSource unknown: is ffprobe on PATH?)")
    return "no frame"


def cumulative_starts(durations: list) -> list:
    """Timeline-relative start of each clip in append order; None once any
    earlier duration is unknown (the position cannot be computed past a gap)."""
    starts, cursor = [], 0
    for d in durations:
        starts.append(cursor)
        if cursor is None or d is None:
            cursor = None
        else:
            cursor += int(d)
    return starts


def vo_rows(manifest: dict, starts_by_file: dict, planned_files: set,
            on_disk: dict, in_bin: dict) -> list[dict]:
    """One row per VO LAY (a take pinned to several shots is laid once per
    shot). mode: underShot | atHead — the same split build_timeline reports.
    Every row says `track: "VO"` — narration never rides A1 (the shot clips'
    embedded audio owns it)."""
    rows = []
    videos_by_file = {_basename(v["file"]): v for v in (manifest.get("videos") or [])}
    for a in manifest.get("audio") or []:
        fname = _basename(a["file"])
        common = {
            "file": a["file"], "label": a.get("label"), "track": VO_TRACK_NAME,
            "onDisk": on_disk.get(fname), "inBin": in_bin.get(fname),
        }
        placements = a.get("placements") or []
        laid = False
        for p in placements:
            vf = _basename(p.get("videoFile") or "")
            if vf and vf in planned_files:
                laid = True
                v = videos_by_file.get(vf)
                rows.append({
                    **common, "mode": "underShot",
                    "targetShot": display_name(manifest, v) if v else vf,
                    "targetFile": p.get("videoFile"),
                    "startFrame": starts_by_file.get(vf),
                })
        if not laid:
            reason = "no placements (board-wide VO)" if not placements else "pinned shot has no clip in this package"
            rows.append({
                **common, "mode": "atHead", "targetShot": None, "targetFile": None,
                "startFrame": 0, "reason": reason,
            })
    return rows


def cue_rows(manifest: dict, cues: list, starts_by_file: dict, durations_by_file: dict,
             fps: float) -> list[dict]:
    """Range-marker rows for the dialogue cues, timeline-relative frames.

    Lines under one shot are laid back to back from the shot's first frame
    (+CUE_FRAME_OFFSET), each as long as its estimate — or the shot itself
    when the cue carries no estimate. A cue whose shot has no clip on the
    timeline gets frame None and is reported, not placed.
    """
    videos_by_file = {_basename(v["file"]): v for v in (manifest.get("videos") or [])}
    colors = cue_color_map(c["speaker"] for c in cues)
    cursor_by_file: dict = {}
    rows = []
    for c in cues:
        vf = _basename(c.get("videoFile") or "")
        v = videos_by_file.get(vf)
        start = starts_by_file.get(vf) if vf else None
        shot_len = durations_by_file.get(vf) if vf else None
        duration = seconds_to_frames(c.get("estSeconds"), fps)
        duration_source = "estimate"
        if duration is None:
            # A cue with no `Est s` used to claim the WHOLE SHOT, which then
            # cascaded every following cue off the end of it and swallowed every
            # text task on that shot. A nominal second is the honest reservation
            # for a line whose length nobody measured [audit 2026-08-24].
            duration, duration_source = seconds_to_frames(1.0, fps), "nominal"
        shot = display_name(manifest, v) if v else (vf or None)
        row = {
            "shot": shot, "file": vf or None, "duration": duration,
            "durationSource": duration_source, "color": colors[c["speaker"]],
            "name": c["speaker"], "note": c["line"],
        }
        if start is None or vf not in starts_by_file:
            rows.append({**row, "frame": None, "limit": None,
                         "reason": no_frame_reason(vf, starts_by_file, start)})
            continue
        offset = cursor_by_file.get(vf, CUE_FRAME_OFFSET)
        # NEVER LET A CUE LAND PAST ITS OWN CLIP. text_task_rows has enforced
        # this since 0.4.0 and the changelog gives the reason — "a marker sitting
        # on the next shot would tell the editor to title the wrong picture,
        # which is worse than no marker". The identical sentence is true of a
        # line of dialogue, and the guard was simply never retrofitted here: two
        # ordinary lines under a five-second generated shot put cue 2 on the next
        # shot and cue 3 past the end of the timeline, silently [audit
        # 2026-08-24]. `limit` carries the boundary through to the placement
        # nudge, which would otherwise walk across it anyway.
        if shot_len is not None and offset >= shot_len:
            rows.append({**row, "frame": None, "limit": None,
                         "reason": "no room left in this shot for its dialogue"})
            continue
        cursor_by_file[vf] = offset + (duration or 1)
        rows.append({**row, "frame": start + offset,
                     "limit": (start + shot_len) if shot_len is not None else None})
    return rows


def text_task_rows(manifest: dict, tasks: list, cue_marker_rows: list,
                   starts_by_file: dict, durations_by_file: dict) -> list[dict]:
    """Point-marker rows for the in-frame-text worklist, timeline-relative.

    PLACED AFTER THE CUES, and that is the whole subtlety. Resolve keeps one
    marker per frame; shot 1's Blue marker owns frame 0 and the dialogue cues
    cascade forward from frame 1 for as long as their estimates run. So the
    frames a text task may use are computed from the cue rows ALREADY BUILT for
    the same shot — start after the last one ends — rather than from a fixed
    offset that would fight them on any shot carrying dialogue.

    A task whose shot has no clip on the timeline gets frame None and is
    reported. A task whose free frame would fall past the end of its own clip
    also gets None WITH ITS REASON: a marker sitting on the next shot would tell
    the editor to title the wrong picture, which is worse than no marker.
    """
    videos_by_file = {_basename(v["file"]): v for v in (manifest.get("videos") or [])}
    # the first frame each file is free from, after its cues have been laid
    free_from: dict = {}
    for c in cue_marker_rows:
        vf = c.get("file")
        if not vf or c.get("frame") is None:
            continue
        end = int(c["frame"]) + max(1, int(c.get("duration") or 1))
        free_from[vf] = max(free_from.get(vf, 0), end)

    rows = []
    for t in tasks:
        vf = _basename(t.get("videoFile") or "")
        v = videos_by_file.get(vf)
        start = starts_by_file.get(vf) if vf else None
        shot_len = durations_by_file.get(vf) if vf else None
        name = f"Text: {t['text']}"
        note = "; ".join(x for x in (
            t["text"] and f'"{t["text"]}"',
            t.get("shotNumber") and f"shot {t['shotNumber']}",
            "lay this as a title over the clip — the generation deliberately did not render it",
        ) if x)

        if start is None or vf not in starts_by_file:
            rows.append({"shot": (display_name(manifest, v) if v else (vf or None)),
                         "file": vf or None, "frame": None, "limit": None,
                         "name": name, "note": note,
                         "reason": no_frame_reason(vf, starts_by_file, start)})
            continue

        offset = max(free_from.get(vf, 0) - start, TEXT_TASK_FRAME_OFFSET)
        frame = start + offset
        # never let a task land past its own clip
        if shot_len is not None and offset >= shot_len:
            rows.append({"shot": display_name(manifest, v) if v else vf, "file": vf,
                         "frame": None, "limit": None, "name": name, "note": note,
                         "reason": "no free frame inside this shot (its cues fill it)"})
            continue
        free_from[vf] = start + offset + 1
        # `limit` carries this decision through to add_text_task_markers. The
        # planner's guard alone was not enough: the 12-frame placement nudge knew
        # nothing about the clip and walked a correctly-planned task onto the next
        # shot anyway, reporting it `placed` [audit 2026-08-24].
        rows.append({"shot": display_name(manifest, v) if v else vf, "file": vf,
                     "frame": frame, "name": name, "note": note,
                     "limit": (start + shot_len) if shot_len is not None else None})
    return rows


def plan_timeline(manifest: dict, base: str, timeline_name: str, cues: list,
                  fps: float, fps_source: str, resolve_connected: bool,
                  bin_lookup, duration_lookup, bin_unknown: str = BIN_UNKNOWN_OFFLINE,
                  target_project: dict | None = None, text_tasks: list | None = None) -> dict:
    """What build_timeline would lay down, decided from the manifest + disk +
    (optionally) the media pool. Never touches Resolve.

    bin_lookup(kind, basename)  -> True | False | None   (None = cannot look:
        Resolve not connected, or the target project is not the open one —
        `bin_unknown` is the wording the rows then carry, and wouldBuild is
        judged on disk presence alone)
    duration_lookup(video_entry, abs_path) -> (frames | None, source_str)
    target_project: {"name", "exists", "current"} when the caller named one
    """
    videos = manifest.get("videos") or []
    audio = manifest.get("audio") or []
    unknown = bin_unknown

    clips, missing, durations = [], [], []
    for i, v in enumerate(videos):
        fname = _basename(v["file"])
        abs_path = os.path.join(base, v["file"])
        on_disk = os.path.isfile(abs_path)
        in_bin = bin_lookup("videos", fname)
        dur, src = duration_lookup(v, abs_path)
        durations.append(dur)
        if not on_disk:
            missing.append({"file": v["file"], "where": "disk"})
        if in_bin is False:
            missing.append({"file": v["file"], "where": "bin"})
        clips.append({
            "index": i + 1,
            "shot": display_name(manifest, v),
            "shotNumber": v.get("shotNumber"),
            "file": v["file"],
            "onDisk": on_disk,
            "inBin": unknown if in_bin is None else in_bin,
            "durationFrames": dur,
            "durationSource": src,
        })
    starts = cumulative_starts(durations)
    for c, s in zip(clips, starts):
        c["startFrame"] = s

    # only clips that will actually be appended count as targets: the build
    # reads the BIN, so the bin decides when Resolve is there; the disk stands
    # in for it when it is not
    planned = {_basename(c["file"]) for c in clips
               if c["inBin"] is True or (c["inBin"] is not False and c["onDisk"])}
    starts_by_file = {_basename(c["file"]): c["startFrame"] for c in clips if _basename(c["file"]) in planned}
    durations_by_file = {_basename(c["file"]): c["durationFrames"] for c in clips}

    a_on_disk, a_in_bin = {}, {}
    for a in audio:
        fname = _basename(a["file"])
        a_on_disk[fname] = os.path.isfile(os.path.join(base, a["file"]))
        b = bin_lookup("audio", fname)
        a_in_bin[fname] = unknown if b is None else b
        if not a_on_disk[fname]:
            missing.append({"file": a["file"], "where": "disk"})
        if b is False:
            missing.append({"file": a["file"], "where": "bin"})

    sw = shot_word(manifest)
    markers = []
    for c, v in zip(clips, videos):
        if _basename(c["file"]) not in planned:
            continue
        note = v.get("description") or ""
        if v.get("vo"):
            note = f"{note}\nVO: {v['vo']}".strip()
        # RM-10 (audit 2026-09-06): OSIDE deliberately exports UNAPPROVED takes
        # and warns; the MCP laid a `pending` clip on V1 with nothing on the
        # timeline saying so. The marker keeps the shot colour and tag (identity
        # is the tag, Resolve's palette is spoken for) and carries the outcome
        # in its NAME and the head of its note, where the editor reads.
        outcome = (v.get("outcome") or "").strip().lower()
        unapproved = bool(outcome) and outcome != "approved"
        name = shot_marker_name(sw, v)
        if unapproved:
            note = f"UNAPPROVED TAKE (outcome: {outcome}) — exported on purpose, not a dailies verdict.\n{note}".strip()
        row = {
            "frame": c["startFrame"], "color": SHOT_MARKER_COLOR, "duration": 1,
            "name": name, "note": note, "shot": c["shot"],
            "file": _basename(c["file"]), "outcome": outcome or None,
        }
        if c["startFrame"] is None:
            # RM-11: a row without a frame says why, never a bare null
            row["reason"] = no_frame_reason(_basename(c["file"]), starts_by_file, None)
        markers.append(row)

    vo = vo_rows(manifest, starts_by_file, planned, a_on_disk, a_in_bin)
    cue_list = cue_rows(manifest, cues, starts_by_file, durations_by_file, fps)
    # AFTER the cues, and from their laid rows — see text_task_rows on why a
    # fixed offset would fight them on any shot that carries dialogue.
    # `text_tasks` defaults to None so every existing caller keeps working.
    text_task_list = text_task_rows(
        manifest, text_tasks or [], cue_list, starts_by_file, durations_by_file,
    )

    would_build = not missing and len(planned) == len(videos)
    return {
        "timeline": timeline_name,
        "resolveConnected": resolve_connected,
        "targetProject": target_project,
        "voTrack": VO_TRACK_NAME,
        "fps": fps,
        "fpsSource": fps_source,
        "clips": clips,
        "markers": markers,
        "vo": vo,
        "cueMarkers": cue_list,
        "textTaskMarkers": text_task_list,
        "missing": missing,
        "wouldBuild": would_build,
    }


def summarize(plan: dict) -> dict:
    """The count fields build_timeline has always returned, from a plan."""
    vo = plan["vo"]
    return {
        "timeline": plan["timeline"],
        "clips": len(plan["markers"]),  # one shot marker per appended clip
        "markers": len(plan["markers"]),
        "voClips": len(vo),
        "voUnderShot": sum(1 for r in vo if r["mode"] == "underShot"),
        "voAtHead": sum(1 for r in vo if r["mode"] == "atHead"),
        "voLoose": 0,
        "voLooseLabels": [],
        "cueMarkers": len(plan["cueMarkers"]),
        # .get: a plan built by an older caller has no such key, and a KeyError
        # here would take down a build over an advisory worklist.
        "textTaskMarkers": len(plan.get("textTaskMarkers") or []),
    }


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------

def _is_shot_marker(m: dict) -> bool:
    tag = m.get("customData") or ""
    # timelines built before markers were tagged: a bare Blue marker is a shot
    return tag == SHOT_TAG or (not tag and m.get("color") == SHOT_MARKER_COLOR)


def _is_cue_marker(m: dict) -> bool:
    return (m.get("customData") or "") == CUE_TAG


def _is_text_task_marker(m: dict) -> bool:
    return (m.get("customData") or "") == TEXT_TASK_TAG


def _is_stock_marker(m: dict) -> bool:
    return (m.get("customData") or "") == STOCK_TAG


def _placeable(rows: list | None) -> int | None:
    """How many planned marker rows actually CARRY a frame — i.e. what the build
    intended to place, as opposed to how many lines the worklist CSV holds.

    The gate used to score `found` against the raw CSV count, so a cue or text
    task the planner deliberately refused (no room inside its own shot) was
    counted as a failure and the whole build reported FAIL for behaving exactly
    as designed [audit 2026-08-24]. Returns None when no plan was handed over,
    and the caller falls back to the old count."""
    if rows is None:
        return None
    return sum(1 for r in rows if r.get("frame") is not None)


def evaluate_verify(manifest: dict, cues: list, observed: dict, cues_expected: bool = True,
                    text_tasks: list | None = None, planned_cues: list | None = None,
                    planned_text_tasks: list | None = None, stock_intent: str | None = None,
                    sidecar_warnings: list | None = None) -> dict:
    """The acceptance gate. observed:
        {"binVideos": set|None, "binAudio": set|None,
         "timeline": {"name", "v1": [{"name","start","duration"}],
                      "a1": [{"name","start"}],
                      "audioTracks": [{"index","name","items":[{"name","start"}]}],
                      "markers": {frame: {...}}} | None}
    VO takes are looked for on EVERY audio track (`audioTracks`; `a1` alone
    for older observations) and each VO row names the track the take sits on.
    Returns {"checks": [{check, expected, found, pass, ...}], "overall", "report"}
    — `report` is the pre-v2 shape, kept for callers that read it.
    """
    checks = []

    def check(name, expected, found, ok, **extra):
        checks.append({"check": name, "expected": expected, "found": found, "pass": bool(ok), **extra})
        return bool(ok)

    videos = manifest.get("videos") or []
    audio = manifest.get("audio") or []
    video_names = [_basename(v["file"]) for v in videos]
    audio_names = [_basename(a["file"]) for a in audio]

    report = {}
    for key, names, present in (("videos", video_names, observed.get("binVideos")),
                                ("audio", audio_names, observed.get("binAudio"))):
        present = set(present or [])
        missing = [n for n in names if n not in present]
        report[key] = {"expected": len(names), "found": len(names) - len(missing), "missing": missing}
        check(f"{key} in bin", len(names), len(names) - len(missing), not missing, missing=missing)

    tl = observed.get("timeline")
    if not tl:
        report["timeline"] = None
        for name in ("V1 clip count", "V1 clip order", "no stray V1 clips", "shot markers", "shot marker positions", "cue markers", "text-task markers"):
            check(name, None, None, False, detail="no timeline")
        for a in audio:
            check(f"VO {_basename(a['file'])}", None, None, False, detail="no timeline")
        return {"checks": checks, "overall": "FAIL", "report": report}

    v1 = tl.get("v1") or []
    audio_tracks = tl.get("audioTracks")
    if not audio_tracks:
        audio_tracks = [{"index": 1, "name": "A1", "items": tl.get("a1") or []}]
    markers = tl.get("markers") or {}
    v1_names = [it["name"] for it in v1]
    report["timeline"] = {"name": tl.get("name"), "videoClips": len(v1), "markers": len(markers)}

    check("V1 clip count", len(video_names), len(v1), len(v1) == len(video_names))
    in_package = [n for n in v1_names if n in set(video_names)]
    first_diff = next((i for i, (a, b) in enumerate(zip(in_package, video_names)) if a != b), None)
    check("V1 clip order", video_names, in_package, in_package == video_names,
          firstDivergence=first_diff)
    strays = [n for n in v1_names if n not in set(video_names)]
    check("no stray V1 clips", [], strays, not strays)

    # every pinned VO lay sits on its shot's first frame (zero tolerance).
    # Unpinned takes (spine, board-wide) are MEANT for the head, but a track
    # holds one clip per frame, so a second head take is appended loose by
    # design — those rows pass on presence and report where the take sits.
    # Takes are looked for on every audio track; the row names the track.
    v1_start = {}
    for it in v1:
        v1_start.setdefault(it["name"], it["start"])
    audio_by_name: dict = {}   # name -> [(start, track label)]
    for tr in audio_tracks:
        label = f"A{tr.get('index')} {tr.get('name') or ''}".strip()
        for it in tr.get("items") or []:
            audio_by_name.setdefault(it["name"], []).append((it["start"], label))
    for a in audio:
        fname = _basename(a["file"])
        found = audio_by_name.get(fname, [])
        targets = []
        for p in a.get("placements") or []:
            vf = _basename(p.get("videoFile") or "")
            if vf and vf in v1_start:
                targets.append((vf, v1_start[vf]))
        pinned = bool(targets)
        if not targets:
            targets = [("head", 0)]
        for label, expected in targets:
            if found:
                nearest, track = min(found, key=lambda ft: abs(ft[0] - expected))
                delta = nearest - expected
            else:
                nearest, track, delta = None, None, None
            ok = (delta == 0) if pinned else (nearest is not None)
            check(f"VO {fname} @ {label}", expected, nearest, ok, delta=delta, track=track,
                  rule="pinned: exact frame" if pinned else "unpinned: present on an audio track (head when free)")
            # NARRATION MUST NOT BE ON A1. The `vo_track` feature exists because
            # the 2026-08-16 live walk found A1 already full of the shot clips'
            # embedded audio, so Resolve accepted the append and placed nothing.
            # The gate looked on every track (right, so a take is always found)
            # and reported which one — but never asserted it, so narration back
            # on A1 PASSed: the exact defect the feature was built to prevent
            # [audit 2026-08-24].
            if track is not None:
                on_a1 = track.split()[0] == "A1"
                check(f"VO {fname} not on A1", f"a track named {VO_TRACK_NAME}", track, not on_a1,
                      rule="narration rides its own track; A1 is the shot clips' embedded audio")

    shot_frames = sorted(f for f, m in markers.items() if _is_shot_marker(m))
    expected_frames = sorted(v1_start[n] for n in video_names if n in v1_start)
    check("shot markers", len(video_names), len(shot_frames), len(shot_frames) == len(video_names))
    # RM-10: unapproved takes on V1 are NAMED in the gate — an advisory row
    # (pass stays true: OSIDE exported them on purpose and warned), so the
    # verdict carries what the timeline holds instead of a green over it
    unapproved_takes = [display_name(manifest, v) for v in videos
                        if (v.get("outcome") or "").strip().lower() not in ("", "approved")]
    check("unapproved takes on V1", 0, len(unapproved_takes), True,
          detail=("advisory — " + ", ".join(unapproved_takes)) if unapproved_takes else "none",
          advisory=True)
    check("shot marker positions", expected_frames, shot_frames, shot_frames == expected_frames)

    # BIND EACH MARKER TO THE SHOT IT NAMES. The two rows above compare sorted
    # frame MULTISETS, so rotating every shot marker onto a different clip left
    # both of them green and the whole gate PASSed a timeline where every marker
    # named the wrong picture [audit 2026-08-24]. The marker's name is observed
    # and carried; it was simply never read. This is the row that makes a
    # mis-zipped build (markers stamped onto the wrong clips) visible.
    sw = shot_word(manifest)
    expected_named = {}
    for v in manifest.get("videos") or []:
        n = _basename(v["file"])
        if n in v1_start:
            expected_named[v1_start[n]] = f"{sw} {v.get('shotNumber', '?')}"
    # Only TAGGED markers are held to the naming contract. A bare Blue marker
    # from a pre-0.2 timeline still counts as a shot marker (README: "a bare Blue
    # marker from a pre-0.2 timeline still counts"), and those were written
    # before this server named them — holding them to a name this version
    # invented would fail a timeline that is legitimately fine.
    tagged = {f: (m.get("name") or "") for f, m in markers.items()
              if (m.get("customData") or "") == SHOT_TAG}
    mismatched = sorted(
        f"frame {f}: expected {want!r}, found {tagged.get(f, '(no marker)')!r}"
        for f, want in expected_named.items() if f in tagged and base_marker_name(tagged[f]) != want
    )
    check("shot markers name their own shot", expected_named, tagged, not mismatched,
          mismatched=mismatched,
          rule="tagged markers only; untagged pre-0.2 Blue markers are exempt")

    # Cue and text-task rows are scored against WHAT THE BUILD INTENDED TO PLACE
    # (planned rows carrying a frame), not against the raw worklist — a
    # deliberately skipped row is a correct outcome, not a failure. The raw
    # counts remain the fallback for a caller that hands over no plan.
    cue_found = sum(1 for m in markers.values() if _is_cue_marker(m))
    planned_cue_count = _placeable(planned_cues)
    cue_expected = 0 if not cues_expected else (
        planned_cue_count if planned_cue_count is not None else len(cues))
    cue_skipped = (len(planned_cues) - planned_cue_count) if planned_cue_count is not None else 0
    check("cue markers", cue_expected, cue_found, cue_found == cue_expected,
          skippedByDesign=cue_skipped)

    # In-frame text (2026-08-17). Checked only when the package HAS a worklist:
    # a package exported before the studio wrote one must still PASS, so an
    # empty list adds no row rather than a row asserting zero.
    if text_tasks:
        text_found = sum(1 for m in markers.values() if _is_text_task_marker(m))
        planned_text_count = _placeable(planned_text_tasks)
        text_expected = (planned_text_count if planned_text_count is not None
                         else len(text_tasks))
        text_skipped = (len(planned_text_tasks) - planned_text_count
                        if planned_text_count is not None else 0)
        check("text-task markers", text_expected, text_found, text_found == text_expected,
              skippedByDesign=text_skipped)

    # The film-stock intent is a SHIPPED feature (0.3.1) that had no gate row at
    # all — add_stock_marker can report `placed: False` and nothing noticed the
    # colourist's note had gone missing [audit 2026-08-24].
    if stock_intent:
        stock_found = sum(1 for m in markers.values() if _is_stock_marker(m))
        check("film-stock marker", 1, stock_found, stock_found == 1)

    # A manifest that NAMES a cue or text file which is not on disk is not an
    # error — the build must not block on an advisory worklist. But reporting
    # PASS without mentioning it turns a half-copied package into a clean
    # handoff. "Does not block" and "is not worth saying" are different claims.
    for w in (sidecar_warnings or []):
        check("worklist file present", "named by the manifest", w, False, detail=w)

    overall = "PASS" if all(c["pass"] for c in checks) else "FAIL"
    return {"checks": checks, "overall": overall, "report": report}


# ---------------------------------------------------------------------------
# "look travels" (2026-08-16) — the look block + the per-clip CDL file
# ---------------------------------------------------------------------------

LOOK_CDL_FILE = "look-cdl.json"
LOOK_SCHEMA = "oside-look/1"  # the manifest's look block; checked at load (RM-4)
LOOK_CDL_SCHEMA = "oside-look-cdl/1"


def _fmt3(v) -> str:
    return " ".join(f"{float(x):.4f}" for x in v)


def cdl_payload(cdl: dict, node: int = 1) -> dict:
    """An ASC CDL dict {slope,offset,power,saturation} → the SetCDL argument
    Resolve's scripting API takes (strings, node index as a string)."""
    return {
        "NodeIndex": str(node),
        "Slope": _fmt3(cdl["slope"]),
        "Offset": _fmt3(cdl["offset"]),
        "Power": _fmt3(cdl["power"]),
        "Saturation": f"{float(cdl['saturation']):.3f}",
    }


def load_look_cdl(manifest: dict, base: str) -> tuple[dict | None, str | None]:
    """The per-clip CDL file Depth Converter writes beside the manifest
    (`depthc look-compare --manifest --emit-cdl`). Returns (doc, reason):
    no `look` block → (None, why); file missing → (None, why); wrong schema →
    (None, why). The reason is the sentence the tool refuses with — a look that
    cannot be applied honestly is not applied at all."""
    if not manifest.get("look"):
        return None, "manifest carries no `look` block — the studio project had no Style Constant with hexes; nothing to apply"
    path = os.path.join(base, LOOK_CDL_FILE)
    if not os.path.isfile(path):
        return None, (f"{LOOK_CDL_FILE} is missing beside the manifest — run "
                      "`depthc look-compare --manifest <manifest.json> --emit-cdl` first (Depth Converter measures each clip; this tool only applies)")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"{LOOK_CDL_FILE} unreadable: {e}"
    if doc.get("schema") != LOOK_CDL_SCHEMA:
        return None, f"{LOOK_CDL_FILE} schema {doc.get('schema')!r} is not {LOOK_CDL_SCHEMA}"
    return doc, None


def stock_intent_note(manifest: dict) -> str | None:
    """The one line the film-stock marker carries, or None when the manifest
    names no stock.

    Read defensively: `stockIntent` is an ADDITIVE key on `oside-look/1`, so a
    manifest written by an older studio build simply will not have it, and a
    hand-edited one may have it half-filled. Anything unusable reads as "no
    stock" rather than putting a broken marker on a colourist's timeline.
    """
    look = manifest.get("look")
    if not isinstance(look, dict):
        return None
    intent = look.get("stockIntent")
    if not isinstance(intent, dict):
        return None
    label = str(intent.get("label") or "").strip()
    if not label:
        return None
    balance = str(intent.get("balance") or "").strip()
    note = str(intent.get("note") or "").strip()
    head = f"Stock intent: {label}" + (f" ({balance})" if balance else "")
    # The studio's note already carries the evidence AND the "nothing applied"
    # scope, so the head stays a label — repeating the clause here made the
    # marker say the same sentence twice. Only a note-less manifest (older or
    # hand-edited) needs the fallback clause.
    return f"{head} — {note}" if note else f"{head} — colourist's call, nothing applied."


# The enforceable form of "a starting balance, never a creative grade". Depth
# Converter's own emitter clamps to these (CDL_OFFSET_MAX 0.25, saturation
# 0.5–1.5, slope/power fixed at 1) — but nothing on THIS side checked, so the
# promise in apply_look's docstring was carried entirely in prose. A depthc bug
# emitting saturation 0.1 would have graded every clip and reported success
# [audit 2026-08-24]. Values outside the envelope are refused per clip, by name,
# rather than taking the whole tool down.
CDL_SLOPE_RANGE = (0.5, 2.0)
CDL_POWER_RANGE = (0.5, 2.0)
CDL_OFFSET_LIMIT = 0.25
CDL_SAT_RANGE = (0.5, 1.5)


def cdl_problem(cdl) -> str | None:
    """Why this CDL cannot be applied honestly, or None when it is in bounds.

    Shape first: cdl_payload does `cdl["slope"]` unguarded and joins whatever
    arity it is handed, so a 2-element vector produced a silently malformed
    SetCDL argument and a missing key raised KeyError inside apply_look's
    blanket except — one bad clip entry killed the entire run."""
    if not isinstance(cdl, dict):
        return "not a CDL object"
    for key, rng in (("slope", CDL_SLOPE_RANGE), ("power", CDL_POWER_RANGE), ("offset", None)):
        v = cdl.get(key)
        if not isinstance(v, (list, tuple)) or len(v) != 3:
            return f"{key} must be a list of 3 numbers"
        try:
            nums = [float(x) for x in v]
        except (TypeError, ValueError):
            return f"{key} holds a non-numeric value"
        if any(x != x or x in (float("inf"), float("-inf")) for x in nums):
            return f"{key} holds a non-finite value"
        if rng and any(not (rng[0] <= x <= rng[1]) for x in nums):
            return f"{key} {nums} outside the starting-balance envelope {rng}"
        if key == "offset" and any(abs(x) > CDL_OFFSET_LIMIT for x in nums):
            return f"offset {nums} exceeds ±{CDL_OFFSET_LIMIT} — that is a grade, not a balance"
    try:
        sat = float(cdl.get("saturation"))
    except (TypeError, ValueError):
        return "saturation is missing or non-numeric"
    if not (CDL_SAT_RANGE[0] <= sat <= CDL_SAT_RANGE[1]):
        return f"saturation {sat} outside the starting-balance envelope {CDL_SAT_RANGE}"
    return None


CDL_READBACK_TOL = 1e-3


def parse_cdl_edl(text: str) -> list[dict]:
    """The ASC CDL values out of an EDL exported with EXPORT_EDL+EXPORT_CDL, in
    EVENT ORDER (which is timeline order, i.e. V1 order).

    Resolve exposes no `GetCDL`, so this export is the only way to READ BACK
    what a `SetCDL` actually did. Proven on the 2026-08-24 walk: the file came
    back carrying the exact values just applied. The reel column is `AX` for
    every event — there are no clip names in it — so events match clips BY
    POSITION and nothing else.

        001  AX       V     C        00:00:00:00 ...
        *ASC_SOP (1.000000 1.000000 1.000000)(-0.025000 ...)(1.000000 ...)
        *ASC_SAT 0.940000
    """
    triple = r"\(\s*([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s*\)"
    sop_re = re.compile(r"\*\s*ASC_SOP\s*" + triple + r"\s*" + triple + r"\s*" + triple)
    sat_re = re.compile(r"\*\s*ASC_SAT\s+([-\d.eE+]+)")
    out: list[dict] = []
    for line in text.splitlines():
        m = sop_re.search(line)
        if m:
            v = [float(x) for x in m.groups()]
            out.append({"slope": v[0:3], "offset": v[3:6], "power": v[6:9], "saturation": None})
            continue
        m = sat_re.search(line)
        if m and out:
            out[-1]["saturation"] = float(m.group(1))
    return out


def compare_cdl(expected: dict, found: dict | None, tol: float = CDL_READBACK_TOL) -> dict:
    """Did the CDL we set come back? {match: bool, diffs: [...]} — a real
    read-back verdict, as opposed to SetCDL's own answer about itself."""
    if not found:
        return {"match": False, "diffs": ["no CDL for this clip in the exported EDL"]}
    diffs = []
    for key in ("slope", "offset", "power"):
        want, got = expected.get(key), found.get(key)
        if not isinstance(got, (list, tuple)) or len(got) != 3:
            diffs.append(f"{key}: unreadable in the EDL")
            continue
        for i, (a, b) in enumerate(zip(want, got)):
            if abs(float(a) - float(b)) > tol:
                diffs.append(f"{key}[{i}]: set {float(a):.4f}, read back {float(b):.4f}")
    want_sat, got_sat = expected.get("saturation"), found.get("saturation")
    if got_sat is None:
        diffs.append("saturation: absent from the EDL")
    elif abs(float(want_sat) - float(got_sat)) > tol:
        diffs.append(f"saturation: set {float(want_sat):.3f}, read back {float(got_sat):.3f}")
    return {"match": not diffs, "diffs": diffs}


def plan_look(manifest: dict, cdl_doc: dict, v1_items: list[dict]) -> dict:
    """Decide, from the manifest + the CDL file + the observed V1 items, what
    apply_look will do — offline-testable. Rows match by clip NAME (the file's
    basename, which is what Resolve names an imported clip). Returns
    {rows, missing, extra}: rows carry the CDL to set per timeline item;
    `missing` = manifest clips with no CDL or not on V1; `extra` = V1 items
    the manifest does not know (left untouched, never graded)."""
    by_file = {}
    for c in cdl_doc.get("clips") or []:
        if c.get("cdl"):
            by_file[_basename(c["file"])] = c
    on_v1 = {it["name"]: it for it in v1_items}
    rows, missing = [], []
    for v in manifest.get("videos") or []:
        name = _basename(v["file"])
        c = by_file.get(name)
        if c is None:
            missing.append({"clip": name, "why": "no CDL for this clip in look-cdl.json (measured file missing or unreadable)"})
            continue
        if name not in on_v1:
            missing.append({"clip": name, "why": "not on V1 of the timeline"})
            continue
        problem = cdl_problem(c.get("cdl"))
        if problem:
            missing.append({"clip": name, "why": f"CDL refused: {problem}"})
            continue
        rows.append({
            "clip": name,
            "shot": display_name(manifest, v),
            "identity": bool(c.get("identity")),
            "measured": c.get("measured") or {},
            "cdl": c["cdl"],
            "set": cdl_payload(c["cdl"]),
        })
    known = {_basename(v["file"]) for v in manifest.get("videos") or []}
    extra = [n for n in on_v1 if n not in known]
    return {"rows": rows, "missing": missing, "extra": extra}
