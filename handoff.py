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
import os
import shutil
import subprocess

from resolve_api import CUE_MARKER_COLORS, CUE_TAG, SHOT_MARKER_COLOR, SHOT_TAG

MANIFEST_FORMAT = "oside-davinci/v1"
FEATURES = ["placements", "cues", "dry_run", "verify_v2"]

# The template a kind maps to fixes the timeline rate. Only used when Resolve
# is not there to ask; the templates themselves are the authority.
KIND_FPS = {"cinematic": 23.976, "explainer": 60.0}

# Resolve keeps ONE marker per frame. A cue laid exactly on its shot's first
# frame would fight the Blue shot marker there, so cues start one frame in.
CUE_FRAME_OFFSET = 1


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


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------

def _basename(rel: str) -> str:
    return os.path.basename(rel or "")


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
    shot). mode: underShot | atHead — the same split build_timeline reports."""
    rows = []
    videos_by_file = {_basename(v["file"]): v for v in (manifest.get("videos") or [])}
    for a in manifest.get("audio") or []:
        fname = _basename(a["file"])
        common = {
            "file": a["file"], "label": a.get("label"),
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
            duration, duration_source = shot_len, "shot"
        offset = cursor_by_file.get(vf, CUE_FRAME_OFFSET)
        frame = (start + offset) if (start is not None and vf in starts_by_file) else None
        cursor_by_file[vf] = offset + (duration or 1)
        rows.append({
            "shot": display_name(manifest, v) if v else (vf or None),
            "file": vf or None,
            "frame": frame,
            "duration": duration,
            "durationSource": duration_source,
            "color": colors[c["speaker"]],
            "name": c["speaker"],
            "note": c["line"],
        })
    return rows


def plan_timeline(manifest: dict, base: str, timeline_name: str, cues: list,
                  fps: float, fps_source: str, resolve_connected: bool,
                  bin_lookup, duration_lookup) -> dict:
    """What build_timeline would lay down, decided from the manifest + disk +
    (optionally) the media pool. Never touches Resolve.

    bin_lookup(kind, basename)  -> True | False | None   (None = not connected)
    duration_lookup(video_entry, abs_path) -> (frames | None, source_str)
    """
    videos = manifest.get("videos") or []
    audio = manifest.get("audio") or []
    unknown = "unknown — Resolve not connected"

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
        markers.append({
            "frame": c["startFrame"], "color": SHOT_MARKER_COLOR, "duration": 1,
            "name": f"{sw} {v.get('shotNumber', '?')}", "note": note, "shot": c["shot"],
            "file": _basename(c["file"]),
        })

    vo = vo_rows(manifest, starts_by_file, planned, a_on_disk, a_in_bin)
    cue_list = cue_rows(manifest, cues, starts_by_file, durations_by_file, fps)

    would_build = not missing and len(planned) == len(videos)
    return {
        "timeline": timeline_name,
        "resolveConnected": resolve_connected,
        "fps": fps,
        "fpsSource": fps_source,
        "clips": clips,
        "markers": markers,
        "vo": vo,
        "cueMarkers": cue_list,
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


def evaluate_verify(manifest: dict, cues: list, observed: dict, cues_expected: bool = True) -> dict:
    """The acceptance gate. observed:
        {"binVideos": set|None, "binAudio": set|None,
         "timeline": {"name", "v1": [{"name","start","duration"}],
                      "a1": [{"name","start"}], "markers": {frame: {...}}} | None}
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
        for name in ("V1 clip count", "V1 clip order", "no stray V1 clips", "shot markers", "shot marker positions", "cue markers"):
            check(name, None, None, False, detail="no timeline")
        for a in audio:
            check(f"VO {_basename(a['file'])}", None, None, False, detail="no timeline")
        return {"checks": checks, "overall": "FAIL", "report": report}

    v1 = tl.get("v1") or []
    a1 = tl.get("a1") or []
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
    # Unpinned takes (spine, board-wide) are MEANT for the head, but A1 holds
    # one clip per frame, so a second head take is appended loose by design —
    # those rows pass on presence and report where the take actually sits.
    v1_start = {}
    for it in v1:
        v1_start.setdefault(it["name"], it["start"])
    a1_by_name: dict = {}
    for it in a1:
        a1_by_name.setdefault(it["name"], []).append(it["start"])
    for a in audio:
        fname = _basename(a["file"])
        found_starts = a1_by_name.get(fname, [])
        targets = []
        for p in a.get("placements") or []:
            vf = _basename(p.get("videoFile") or "")
            if vf and vf in v1_start:
                targets.append((vf, v1_start[vf]))
        pinned = bool(targets)
        if not targets:
            targets = [("head", 0)]
        for label, expected in targets:
            if found_starts:
                nearest = min(found_starts, key=lambda s: abs(s - expected))
                delta = nearest - expected
            else:
                nearest, delta = None, None
            ok = (delta == 0) if pinned else (nearest is not None)
            check(f"VO {fname} @ {label}", expected, nearest, ok, delta=delta,
                  rule="pinned: exact frame" if pinned else "unpinned: present on A1 (head when free)")

    shot_frames = sorted(f for f, m in markers.items() if _is_shot_marker(m))
    expected_frames = sorted(v1_start[n] for n in video_names if n in v1_start)
    check("shot markers", len(video_names), len(shot_frames), len(shot_frames) == len(video_names))
    check("shot marker positions", expected_frames, shot_frames, shot_frames == expected_frames)

    cue_found = sum(1 for m in markers.values() if _is_cue_marker(m))
    cue_expected = len(cues) if cues_expected else 0
    check("cue markers", cue_expected, cue_found, cue_found == cue_expected)

    overall = "PASS" if all(c["pass"] for c in checks) else "FAIL"
    return {"checks": checks, "overall": overall, "report": report}
