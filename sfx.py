# ============================================================================
# sfx.py — the PURE half of place_sfx: everything about a spotted SFX pass
# that can be decided without a live Resolve object.
#
#   load_plan(...)        an oside-sfx-plan/v1 file → validated rows, or an
#                         operator sentence saying which row is wrong
#   item_trim(...)        seconds → the source frames AppendToTimeline takes
#   expected_lay(...)     what Resolve WILL lay for one row: timeline length
#                         and left offset, conformed the way Resolve conforms
#   check_spans(...)      the collision verdict from ONE read of the track
#   otio_diff(...)        before/after timeline exports → "nothing else moved"
#
# The Resolve half is resolve_api.place_sfx. Kept free of Resolve imports so
# tests/ can drive every rule on synthetic data, like handoff.py.
# ============================================================================

import json
import math
import os

SFX_PLAN_FORMAT = "oside-sfx-plan/v1"

# Frames are FLOORED (CLAUDE.md anti-pattern 7). A seconds value written from a
# frame count — 124/60 — reads back as 123.99999999999999 frames, and a bare
# floor drops it a whole frame. The epsilon is far below one frame at any rate.
_EPS = 1e-6

# Two rates closer than this are the same rate (24 on 23.976 conforms frame
# for frame — resolve_api.clip_duration_frames uses the same threshold).
_SAME_RATE = 0.05


class PlanError(ValueError):
    """A plan that cannot be placed as written. The message is for the operator."""


def _floor(x: float) -> int:
    return int(math.floor(x + _EPS))


def load_plan(path: str) -> dict:
    """Read and validate an oside-sfx-plan/v1 file.

    {
      "schema": "oside-sfx-plan/v1",
      "timeline": "YT",            the timeline, which must be CURRENT in Resolve
      "track": "SFX 3",            an EXISTING audio track, by name, exactly one
      "bin": "SFX",                an EXISTING bin; missing files import here
      "placements": [{
        "file": "/abs/path.wav",   absolute, must exist
        "sourceIn": 0.0,           seconds into the file
        "sourceOut": 1.633,        seconds, exclusive, > sourceIn
        "record": 609,             timeline frame, RELATIVE to the timeline start
        "label": "...", "kind": "CHECK"     optional, reported back
      }]
    }

    Source trims are SECONDS because the frame unit is the media-pool item's
    own rate, which Resolve assigns at import: a WAV can come in tagged 25 fps
    on a 60 fps project (measured 2026-10-25 on 21.1.1). A plan written in
    frames would have to know that before the file was ever imported."""
    try:
        with open(path, encoding="utf-8") as f:
            plan = json.load(f)
    except OSError as e:
        raise PlanError(f"Cannot read the plan {path!r}: {e}") from e
    except json.JSONDecodeError as e:
        raise PlanError(f"The plan {path!r} is not valid JSON: {e}") from e
    if not isinstance(plan, dict):
        raise PlanError("The plan must be a JSON object.")
    if plan.get("schema") != SFX_PLAN_FORMAT:
        raise PlanError(f"The plan's schema is {plan.get('schema')!r}; this tool reads {SFX_PLAN_FORMAT!r}.")
    for key in ("timeline", "track", "bin"):
        if not isinstance(plan.get(key), str) or not plan[key].strip():
            raise PlanError(f"The plan needs a non-empty {key!r} string.")
    rows = plan.get("placements")
    if not isinstance(rows, list) or not rows:
        raise PlanError("The plan has no placements.")
    out = []
    for i, r in enumerate(rows):
        where = f"placement {i + 1}"
        if not isinstance(r, dict):
            raise PlanError(f"{where} is not an object.")
        f = r.get("file")
        if not isinstance(f, str) or not os.path.isabs(f):
            raise PlanError(f"{where}: 'file' must be an absolute path (got {f!r}).")
        if not os.path.isfile(f):
            raise PlanError(f"{where}: {f!r} does not exist.")
        try:
            s_in, s_out = float(r.get("sourceIn")), float(r.get("sourceOut"))
        except (TypeError, ValueError) as e:
            raise PlanError(f"{where}: 'sourceIn' and 'sourceOut' must be numbers of seconds.") from e
        if not (math.isfinite(s_in) and math.isfinite(s_out)) or s_in < 0 or s_out <= s_in:
            raise PlanError(f"{where}: needs 0 <= sourceIn < sourceOut (got {s_in}, {s_out}).")
        rec = r.get("record")
        if isinstance(rec, bool) or not isinstance(rec, int) or rec < 0:
            raise PlanError(f"{where}: 'record' must be a whole timeline frame >= 0 (got {rec!r}).")
        out.append({
            "row": i + 1, "file": f, "sourceIn": s_in, "sourceOut": s_out, "record": rec,
            "label": str(r.get("label") or ""), "kind": str(r.get("kind") or ""),
        })
    return {"timeline": plan["timeline"].strip(), "track": plan["track"].strip(),
            "bin": plan["bin"].strip(), "placements": out}


def _same_rate(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) < _SAME_RATE


def item_trim(source_in: float, source_out: float, item_fps: float) -> tuple[int, int]:
    """Seconds → (startFrame, endFrame) in the ITEM's own frames, the unit
    AppendToTimeline's clip-info takes. endFrame is EXCLUSIVE (measured
    2026-08-16: endFrame 47 lands 47 frames)."""
    return _floor(source_in * item_fps), _floor(source_out * item_fps)


def expected_lay(start: int, end: int, item_fps: float, timeline_fps: float) -> dict:
    """What Resolve lays for a trim of [start, end) item frames.

    Same rate: exactly end-start frames, left offset `start`. A mismatched rate
    conforms by DURATION and floors — measured 2026-10-25 on 21.1.1: a WAV the
    pool tagged 25 fps, trimmed 5..119 on a 60 fps timeline, landed 273 frames
    (114/25*60 = 273.6) with GetLeftOffset 12 (5/25*60). One cell, so a
    conformed lay is held to +/-1 frame rather than exact: `tolerance` says
    which."""
    if _same_rate(item_fps, timeline_fps):
        return {"length": end - start, "leftOffset": start, "tolerance": 0}
    k = float(timeline_fps) / float(item_fps)
    # ponytail: +/-1 on conformed rates from a single measured cell; tighten to 0 once a second rate is walked
    return {"length": max(1, _floor((end - start) * k)), "leftOffset": _floor(start * k), "tolerance": 1}


def check_spans(rows: list[dict], existing: list[dict], end_limit: int | None) -> dict:
    """The collision verdict, from ONE read of the target track.

    rows:     [{"row", "name", "record", "length", ...}]   what the plan lays
    existing: [{"name", "start", "duration"}]               the track, as read

    A row whose clip already sits on the track at its frame with its length is
    ALREADY PLACED — re-running a plan is a no-op, not a pile of copies. Any
    other overlap, with the track or with another row, refuses the WHOLE plan
    before anything is written (anti-pattern 3: never retry a write, so judge
    before the first one). A row running past `end_limit` would lengthen the
    timeline, which is a change to more than one track, so it is refused too."""
    to_place, already, problems = [], [], []
    for r in rows:
        span = (r["record"], r["record"] + r["length"])
        same = [it for it in existing
                if it["name"] == r["name"] and it["start"] == r["record"] and it["duration"] == r["length"]]
        if same:
            already.append(r)
            continue
        for it in existing:
            if it["start"] < span[1] and span[0] < it["start"] + it["duration"]:
                problems.append({"row": r["row"], "label": r.get("label", ""), "record": r["record"],
                                 "length": r["length"],
                                 "reason": f"overlaps {it['name']!r} already on the track at "
                                           f"{it['start']}..{it['start'] + it['duration']}"})
        if end_limit is not None and span[1] > end_limit:
            problems.append({"row": r["row"], "label": r.get("label", ""), "record": r["record"],
                             "length": r["length"],
                             "reason": f"ends at {span[1]}, past the timeline end {end_limit}"})
        to_place.append(r)
    ordered = sorted(to_place, key=lambda r: r["record"])
    for a, b in zip(ordered, ordered[1:]):
        if b["record"] < a["record"] + a["length"]:
            problems.append({"row": b["row"], "label": b.get("label", ""), "record": b["record"],
                             "length": b["length"],
                             "reason": f"overlaps plan row {a['row']} ({a['record']}..{a['record'] + a['length']})"})
    return {"toPlace": ordered, "alreadyPlaced": already, "collisions": problems}


# ---------------------------------------------------------------------------
# the export diff
# ---------------------------------------------------------------------------

def otio_tracks(doc: dict) -> list[dict]:
    """The tracks of a Resolve OTIO export, each tagged with its 1-based index
    WITHIN ITS KIND — the index GetItemListInTrack takes. Resolve writes video
    tracks then audio tracks, each in index order (measured 2026-10-25 on
    21.1.1: Video 1..4, then Audio 1, VO, MUSIC, SFX, SFX 2, SFX 3)."""
    tracks = (doc.get("tracks") or {}).get("children") or []
    seen: dict[str, int] = {}
    out = []
    for t in tracks:
        kind = str(t.get("kind") or "")
        seen[kind] = seen.get(kind, 0) + 1
        out.append({"kind": kind, "index": seen[kind], "name": t.get("name"), "json": t})
    return out


def otio_clips(track: dict) -> list[tuple[str, int, int]]:
    """(name, record start, duration) for every clip on one exported track, in
    timeline-relative frames. Gaps advance the playhead; transitions do not
    (OTIO transitions overlap their neighbours rather than taking room)."""
    pos = 0.0
    out = []
    for c in track.get("children") or []:
        schema = str(c.get("OTIO_SCHEMA") or "")
        if schema.startswith("Transition"):
            continue
        dur = float((((c.get("source_range") or {}).get("duration")) or {}).get("value") or 0)
        if schema.startswith("Clip"):
            out.append((str(c.get("name") or ""), int(round(pos)), int(round(dur))))
        pos += dur
    return out


def _strip_children(track_json: dict) -> dict:
    return {k: v for k, v in track_json.items() if k != "children"}


def otio_diff(before: dict, after: dict, kind: str, index: int, placed: list[tuple[str, int, int]]) -> dict:
    """The proof that the pass touched ONE track and only by adding `placed`.

    Every other track must come back byte-identical (as JSON), the top level
    and the stack around the tracks must not change, the target track's own
    settings must not change, and its clips must be exactly the clips it had
    plus the clips laid. Returns per-check rows and `pass`."""
    checks = []

    def row(check, ok, **kw):
        checks.append({"check": check, "pass": bool(ok), **kw})

    tb, ta = otio_tracks(before), otio_tracks(after)
    row("trackCount", len(tb) == len(ta), before=len(tb), after=len(ta))
    target_b = target_a = None
    changed_others = []
    for b, a in zip(tb, ta):
        if (b["kind"], b["index"]) == (kind, index):
            target_b, target_a = b, a
            continue
        if b["json"] != a["json"]:
            changed_others.append(f"{b['kind']} {b['index']} {b['name']!r}")
    row("otherTracksIdentical", not changed_others and len(tb) == len(ta), changed=changed_others)

    top_b = {k: v for k, v in before.items() if k != "tracks"}
    top_a = {k: v for k, v in after.items() if k != "tracks"}
    stack_b = _strip_children(before.get("tracks") or {})
    stack_a = _strip_children(after.get("tracks") or {})
    row("timelineMetadataUnchanged", top_b == top_a and stack_b == stack_a)

    if target_b is None or target_a is None:
        row("targetTrackFound", False, kind=kind, index=index)
        return {"pass": False, "checks": checks}
    row("targetTrackSettingsUnchanged", _strip_children(target_b["json"]) == _strip_children(target_a["json"]))
    had, has = otio_clips(target_b["json"]), otio_clips(target_a["json"])
    want = sorted(had + list(placed))
    got = sorted(has)
    missing = [c for c in want if c not in got]
    extra = list(got)
    for c in want:
        if c in extra:
            extra.remove(c)
    row("targetTrackIsBeforePlusPlaced", got == want,
        before=len(had), placed=len(placed), after=len(has), missing=missing, unexpected=extra)
    return {"pass": all(c["pass"] for c in checks), "checks": checks}
