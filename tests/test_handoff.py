# ============================================================================
# tests/test_handoff.py — the bridge's self-test, no Resolve required.
#
#   .venv/bin/python tests/test_handoff.py        (plain: no pytest needed)
#   .venv/bin/python -m pytest tests               (when pytest is installed)
#
# Drives the dry-run planner against tests/fixtures/pkg (a fixture package
# with empty media files) and the verify table against synthetic timelines,
# including fake Resolve objects to exercise observe_timeline / the marker
# helpers. Every assertion here is a contract build_timeline / verify_import
# report to OSIDE.
# ============================================================================

import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
# the offline cases must stay offline even when a real Resolve is running on
# this machine (the fake-Resolve cases patch rapi.connect and are unaffected)
os.environ["OSIDE_RESOLVE_OFFLINE"] = "1"

import handoff  # noqa: E402
import resolve_api as rapi  # noqa: E402
import server  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "pkg", "manifest.json")


def _fixture():
    return server._load_manifest(FIXTURE)


# ---------------------------------------------------------------------------
# fakes — just enough Resolve surface for the helpers under test
# ---------------------------------------------------------------------------

class FakeItem:
    def __init__(self, name, start, duration):
        self._n, self._s, self._d = name, start, duration

    def GetName(self):
        return self._n

    def GetStart(self):
        return self._s

    def GetDuration(self):
        return self._d


class FakeTimeline:
    def __init__(self, name="EDIT 01", start=86400, v1=(), a1=(), markers=None, audio_tracks=1):
        self._name, self._start = name, start
        self._tracks = {("video", 1): list(v1), ("audio", 1): list(a1)}
        for i in range(2, audio_tracks + 1):
            self._tracks[("audio", i)] = []
        self._names = {}
        self._markers = dict(markers or {})

    def GetName(self):
        return self._name

    def GetStartFrame(self):
        return self._start

    def GetTrackCount(self, kind):
        return sum(1 for k, _ in self._tracks if k == kind)

    def AddTrack(self, kind, *_):
        idx = self.GetTrackCount(kind) + 1
        self._tracks[(kind, idx)] = []
        return True

    def SetTrackName(self, kind, idx, name):
        if (kind, idx) not in self._tracks:
            return False
        self._names[(kind, idx)] = name
        return True

    def GetTrackName(self, kind, idx):
        return self._names.get((kind, idx), f"{'Audio' if kind == 'audio' else 'Video'} {idx}")

    def GetItemListInTrack(self, kind, index):
        return self._tracks.get((kind, index), [])

    def GetMarkers(self):
        return self._markers

    def AddMarker(self, frame, color, name, note, duration, custom=""):
        if frame in self._markers:
            return False
        self._markers[frame] = {"color": color, "name": name, "note": note, "duration": duration, "customData": custom}
        return True


def _tc(frames, fps):
    base = int(round(fps))
    s_, f = divmod(int(frames), base)
    m, s_ = divmod(s_, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s_:02d}:{f:02d}"


class FakeClip:
    """A media-pool item: name = filename. Video clips answer `Frames` and
    (Seedance 2.x default) carry EMBEDDED AUDIO; audio clips answer '' for
    Frames and only a `Duration` timecode at `FPS` — exactly what Resolve
    21.0.4 hands back [live walk 2026-08-16]."""
    def __init__(self, name, frames, fps=24.0, embedded_audio=None):
        self._n, self._f, self._fps = name, frames, fps
        self._audio_only = name.endswith((".mp3", ".wav"))
        self._embedded = (not self._audio_only) if embedded_audio is None else embedded_audio

    def GetName(self):
        return self._n

    def GetClipProperty(self, key):
        if key == "Frames":
            return "" if self._audio_only else str(self._f)
        if key == "Duration":
            return _tc(self._f, self._fps)
        if key == "FPS":
            return self._fps
        if key == "Type":
            return "Audio" if self._audio_only else ("Video + Audio" if self._embedded else "Video")
        return None


class FakeFolder:
    def __init__(self, name, clips=(), subs=()):
        self._n, self._clips, self._subs = name, list(clips), list(subs)

    def GetName(self):
        return self._n

    def GetClipList(self):
        return self._clips

    def GetSubFolderList(self):
        return self._subs


class _RemoteObject:
    """Stands in for the `<PyRemoteObject>` Resolve returns from AppendToTimeline."""


class FakeMediaPool:
    """Enough of MediaPool for build_timeline: bins, CreateEmptyTimeline and
    AppendToTimeline in both of its shapes (a list of items, or clip-info dicts
    with an explicit recordFrame + trackIndex).

    Two Resolve behaviours reproduced on purpose, both measured live on
    21.0.4.5 (2026-08-16):
      * appending a video clip with embedded audio ALSO fills A1;
      * a clip-info append aimed at an OCCUPIED frame answers a truthy
        `[<PyRemoteObject>]` and places NOTHING (never False).
    """
    def __init__(self, project, root):
        self._project, self._root, self._current = project, root, root

    def GetRootFolder(self):
        return self._root

    def SetCurrentFolder(self, folder):
        self._current = folder
        return True

    def CreateEmptyTimeline(self, name):
        tl = FakeTimeline(name=name, start=86400)
        self._project._timelines.append(tl)
        return tl

    def AppendToTimeline(self, items):
        tl = self._project.GetCurrentTimeline()
        out = []
        for it in items:
            if isinstance(it, dict):
                clip = it["mediaPoolItem"]
                kind = "audio" if it.get("mediaType") == 2 else "video"
                track = (kind, int(it.get("trackIndex") or 1))
                start = it["recordFrame"]
                if "endFrame" in it:
                    length = it["endFrame"] - it["startFrame"]  # endFrame exclusive, as Resolve does it
                else:
                    length = rapi.clip_duration_frames(clip)
                lane = tl._tracks.setdefault(track, [])
                if any(x.GetStart() == start for x in lane):
                    out.append(_RemoteObject())   # truthy — and nothing placed
                    continue
                item = FakeItem(clip.GetName(), start, length)
                lane.append(item)
                out.append(item)
            else:
                # a plain append lands at the tail of the track its media type owns
                audio_only = it.GetClipProperty("Type") == "Audio"
                track = ("audio", 1) if audio_only else ("video", 1)
                lane = tl._tracks[track]
                end = max((x.GetStart() + x.GetDuration() for x in lane), default=tl.GetStartFrame())
                length = rapi.clip_duration_frames(it)
                item = FakeItem(it.GetName(), end, length)
                lane.append(item)
                out.append(item)
                if not audio_only and it.GetClipProperty("Type") == "Video + Audio":
                    tl._tracks[("audio", 1)].append(FakeItem(it.GetName(), end, length))
        return out


class FakeProject:
    def __init__(self, media_pool_factory, fps="23.976", name="FIXTURE"):
        self._timelines, self._current, self._fps, self._name = [], None, fps, name
        self._pool = media_pool_factory(self)

    def GetMediaPool(self):
        return self._pool

    def GetSetting(self, key):
        return self._fps if key == "timelineFrameRate" else None

    def GetTimelineCount(self):
        return len(self._timelines)

    def GetTimelineByIndex(self, i):
        return self._timelines[i - 1]

    def SetCurrentTimeline(self, name):
        self._current = next(t for t in self._timelines if t.GetName() == name)
        return True

    def GetCurrentTimeline(self):
        return self._current

    def GetName(self):
        return self._name


def _fake_resolve(monkeypatch_target=server, videos_frames=(120, 96, 200), audio_frames=(288, 74, 48),
                  project_name="FIXTURE", with_bins=True, projects=None):
    """Wire server/rapi to a fake Resolve holding the fixture's clips in
    VIDEOS / VO bins (or an empty pool with with_bins=False). Returns
    (project, restore_fn)."""
    manifest, _ = _fixture()
    vclips = [FakeClip(os.path.basename(v["file"]), f) for v, f in zip(manifest["videos"], videos_frames)]
    aclips = [FakeClip(os.path.basename(a["file"]), f) for a, f in zip(manifest["audio"], audio_frames)]
    if with_bins:
        root = FakeFolder("Master", subs=[FakeFolder("VIDEOS", vclips), FakeFolder("VO", aclips), FakeFolder("TIMELINES")])
    else:
        root = FakeFolder("Master", subs=[FakeFolder("VIDEOS"), FakeFolder("VO"), FakeFolder("TIMELINES")])
    project = FakeProject(lambda p: FakeMediaPool(p, root), name=project_name)

    class PM:
        def GetCurrentProject(self):
            return project

        def GetProjectListInCurrentFolder(self):
            return list(projects if projects is not None else [project_name])

    class Resolve:
        def GetProjectManager(self):
            return PM()

        def GetVersionString(self):
            return "fake"

    orig_connect = rapi.connect
    rapi.connect = lambda: Resolve()

    def restore():
        rapi.connect = orig_connect
    return project, restore


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_real_build_and_verify_against_fake_resolve():
    """The non-dry path end to end on a fake Resolve: clips appended in order,
    shot markers on each clip's first frame, pinned VO under its shot, cue
    range markers under theirs — and verify_import PASSES on what was built."""
    project, restore = _fake_resolve()
    try:
        # a connected dry run first: bins known, frames computed from clip lengths
        dry = server.build_timeline(FIXTURE, "EDIT 01", dry_run=True)
        assert dry["ok"] and dry["plan"]["resolveConnected"] is True
        assert [c["inBin"] for c in dry["plan"]["clips"]] == [True, True, True]
        assert [c["startFrame"] for c in dry["plan"]["clips"]] == [0, 120, 216]
        assert dry["plan"]["fpsSource"] == "resolve"
        assert project.GetTimelineCount() == 0  # touched nothing

        real = server.build_timeline(FIXTURE, "EDIT 01")
        assert real["ok"], real
        assert real["dryRun"] is False
        assert real["clips"] == 3 and real["markers"] == 3
        assert real["voClips"] == 3 and real["voUnderShot"] == 1 and real["voAtHead"] == 1
        # Ben's pin has no clip AND the narrator already sits at the head → the
        # second head clip finds the VO track occupied → laid loose (still on
        # the VO track, at its tail), reported by label
        assert real["voLoose"] == 1 and real["voLooseLabels"] == ["Ben"]
        # narration rides its OWN track named VO (A2 on a fresh timeline);
        # A1 holds the shot clips' embedded audio and nothing else
        assert real["voTrack"] == "VO" and real["voTrackIndex"] == 2
        assert all(r["track"] == "VO" and r["trackIndex"] == 2 for r in real["plan"]["vo"])
        assert real["cueMarkers"] == 3 and real["cueMarkersSkipped"] == []
        # dry and real plans agree on the rows that matter
        for key in ("clips", "markers", "vo", "cueMarkers"):
            assert len(dry["plan"][key]) == len(real["plan"][key]), key
        assert [c["startFrame"] for c in real["plan"]["clips"]] == [0, 120, 216]
        assert [c["frame"] for c in real["plan"]["cueMarkers"]] == [121, 145, 217]

        tl = project.GetCurrentTimeline()
        assert tl.GetName() == "EDIT 01"
        assert tl.GetTrackName("audio", 2) == "VO"
        assert [x.GetName() for x in tl.GetItemListInTrack("audio", 1)] == [
            "scene01_shot1A.mp4", "scene01_shot2.mp4", "scene02_shot1.mp4"]
        assert [(x.GetName(), x.GetStart() - 86400, x.GetDuration()) for x in tl.GetItemListInTrack("audio", 2)] == [
            ("vo01_narrator.mp3", 0, 288), ("vo02_scene01-shot2_ada.mp3", 120, 74), ("vo03_scene02-shot9_ben.mp3", 288, 48)]
        marks = tl.GetMarkers()
        assert {f for f, m in marks.items() if m["customData"] == rapi.SHOT_TAG} == {0, 120, 216}
        assert {f for f, m in marks.items() if m["customData"] == rapi.CUE_TAG} == {121, 145, 217}
        assert marks[121]["name"] == "Ada" and marks[121]["note"] == "Two coffees, please."
        assert marks[121]["duration"] == 24

        ver = server.verify_import(FIXTURE, "EDIT 01")
        assert ver["ok"], ver
        # the loose Ben clip landed at A1's tail, not the head. An UNPINNED
        # take passes on presence (two head takes cannot share frame 0 on A1)
        # but the row still names where it sits, so the editor can see it.
        assert ver["overall"] == "PASS", [c for c in ver["checks"] if not c["pass"]]
        ben = next(c for c in ver["checks"] if c["check"] == "VO vo03_scene02-shot9_ben.mp3 @ head")
        assert ben["expected"] == 0 and ben["found"] == 288 and ben["delta"] == 288 and ben["pass"]
        assert ben["track"] == "A2 VO"
        ada0 = next(c for c in ver["checks"] if c["check"].startswith("VO vo02"))
        assert ada0["found"] == 120 and ada0["delta"] == 0 and ada0["track"] == "A2 VO"
        assert ver["clean"] is True
        # a PINNED take one frame off is a FAIL — zero tolerance
        tl.GetItemListInTrack("audio", 2)[1]._s += 1  # Ada's clip, on the VO track
        ver2 = server.verify_import(FIXTURE, "EDIT 01")
        ada = next(c for c in ver2["checks"] if c["check"].startswith("VO vo02"))
        assert ver2["overall"] == "FAIL" and ada["delta"] == 1 and not ada["pass"]
        tl.GetItemListInTrack("audio", 2)[1]._s -= 1

        # a second build under the same name is refused (create-only guardrail)
        again = server.build_timeline(FIXTURE, "EDIT 01")
        assert again["ok"] is False and "already exists" in again["error"]
        assert project.GetTimelineCount() == 1
    finally:
        restore()


def test_real_build_verify_passes_without_head_collision():
    """Same package minus Ben's clipless pin: every check green."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    dst = os.path.join(tmp, "pkg")
    shutil.copytree(os.path.dirname(FIXTURE), dst)
    mpath = os.path.join(dst, "manifest.json")
    m = json.load(open(mpath))
    m["audio"] = m["audio"][:2]
    json.dump(m, open(mpath, "w"))
    project, restore = _fake_resolve(audio_frames=(288, 74))
    try:
        real = server.build_timeline(mpath, "EDIT 01")
        assert real["ok"] and real["voLoose"] == 0
        ver = server.verify_import(mpath, "EDIT 01")
        assert ver["overall"] == "PASS", [c for c in ver["checks"] if not c["pass"]]
        assert ver["clean"] is True
        assert ver["report"]["timeline"]["videoClips"] == 3
        by = {c["check"]: c for c in ver["checks"]}
        assert by["cue markers"]["found"] == 3
        # cues off at verify time → 3 cue markers present is a FAIL, not a shrug
        off = server.verify_import(mpath, "EDIT 01", cues=False)
        assert off["overall"] == "FAIL"
    finally:
        restore()
        shutil.rmtree(tmp)

def test_dry_run_offline_plan():
    """Resolve is not connected in the test process: the plan must still come
    back from manifest + disk alone, with bin presence marked unknown."""
    res = server.build_timeline(FIXTURE, "EDIT 01", dry_run=True)
    assert res["ok"], res
    assert res["dryRun"] is True
    plan = res["plan"]
    assert plan["resolveConnected"] is False
    assert plan["fpsSource"] == "template" and abs(plan["fps"] - 23.976) < 1e-6
    # ordered clip list, shot order, all on disk, bin unknown
    assert [c["file"] for c in plan["clips"]] == [
        "videos/scene01_shot1A.mp4", "videos/scene01_shot2.mp4", "videos/scene02_shot1.mp4"]
    assert all(c["onDisk"] for c in plan["clips"])
    assert all(c["inBin"] == "unknown — Resolve not connected" for c in plan["clips"])
    assert plan["clips"][0]["startFrame"] == 0
    # empty fixture files → ffprobe cannot size them → later starts unknown
    assert plan["clips"][1]["startFrame"] is None
    assert plan["missing"] == [] and plan["wouldBuild"] is True
    # VO: narrator at head, Ada under scene 1 shot 2, Ben's pin has no clip → head
    modes = {(r["file"], r["mode"]) for r in plan["vo"]}
    assert ("vo/vo01_narrator.mp3", "atHead") in modes
    assert ("vo/vo02_scene01-shot2_ada.mp3", "underShot") in modes
    assert ("vo/vo03_scene02-shot9_ben.mp3", "atHead") in modes
    ada = next(r for r in plan["vo"] if r["file"].endswith("ada.mp3"))
    assert ada["targetShot"] == "scene 1 shot 2"
    # summary fields match the real call's names
    for k in ("clips", "markers", "voClips", "voUnderShot", "voAtHead", "voLoose", "cueMarkers"):
        assert k in res, k
    assert res["clips"] == 3 and res["markers"] == 3 and res["voClips"] == 3
    assert res["voUnderShot"] == 1 and res["voAtHead"] == 2 and res["cueMarkers"] == 3
    # cues: 3 lines, colours per speaker, sequential under shot 2
    cues = plan["cueMarkers"]
    assert [c["name"] for c in cues] == ["Ada", "Ben", "Ben"]
    assert cues[0]["color"] != cues[1]["color"] and cues[1]["color"] == cues[2]["color"]
    assert all(c["color"] != rapi.SHOT_MARKER_COLOR for c in cues)
    assert cues[0]["duration"] == 24 and cues[2]["duration"] == 48  # 1.0 s / 2.0 s @ 23.976
    json.dumps(res)  # the whole thing must be JSON-serialisable for MCP


def test_dry_run_missing_file_blocks_build(tmp_path=None):
    """A package file absent on disk lands in `missing` and flips wouldBuild."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    dst = os.path.join(tmp, "pkg")
    shutil.copytree(os.path.dirname(FIXTURE), dst)
    os.remove(os.path.join(dst, "videos", "scene01_shot2.mp4"))
    res = server.build_timeline(os.path.join(dst, "manifest.json"), dry_run=True)
    assert res["ok"] and res["wouldBuild"] is False
    assert res["missing"] == [{"file": "videos/scene01_shot2.mp4", "where": "disk"}]
    # Ada's pin targeted that clip → now falls to the head, cue under it has no frame
    ada = next(r for r in res["plan"]["vo"] if r["file"].endswith("ada.mp3"))
    assert ada["mode"] == "atHead"
    assert res["plan"]["cueMarkers"][0]["frame"] is None
    assert res["clips"] == 2
    shutil.rmtree(tmp)


def test_plan_with_known_durations_and_bins():
    """With durations and bin presence supplied (what a connected dry run
    sees) the plan carries computed start frames for clips, VO and cues."""
    manifest, base = _fixture()
    cues, warn = handoff.load_cues(manifest, base)
    assert warn is None and len(cues) == 3
    durations = {"scene01_shot1A.mp4": 120, "scene01_shot2.mp4": 96, "scene02_shot1.mp4": 200}
    plan = handoff.plan_timeline(
        manifest, base, "EDIT 01", cues, 24.0, "resolve", True,
        bin_lookup=lambda kind, name: True,
        duration_lookup=lambda v, p: (durations[os.path.basename(p)], "resolve"),
    )
    assert [c["startFrame"] for c in plan["clips"]] == [0, 120, 216]
    assert [m["frame"] for m in plan["markers"]] == [0, 120, 216]
    ada = next(r for r in plan["vo"] if r["file"].endswith("ada.mp3"))
    assert ada["startFrame"] == 120
    # cues start one frame into the shot (Resolve keeps one marker per frame),
    # back to back within a shot: 121, 121+24 = 145; scene 2 shot 1 → 217
    assert [c["frame"] for c in plan["cueMarkers"]] == [121, 145, 217]
    assert plan["wouldBuild"] and all(c["inBin"] is True for c in plan["clips"])


def test_cues_opt_out():
    res = server.build_timeline(FIXTURE, dry_run=True, cues=False)
    assert res["ok"] and res["cueMarkers"] == 0 and res["plan"]["cueMarkers"] == []
    assert res["plan"]["cuesEnabled"] is False


def test_cue_colours_stable_and_not_blue():
    a = handoff.cue_color_map(["Zed", "Ada", "Ben"])
    b = handoff.cue_color_map(["Ben", "Ada", "Zed", "Ada"])
    assert a == b
    assert len({a["Ada"], a["Ben"], a["Zed"]}) == 3
    assert rapi.SHOT_MARKER_COLOR not in a.values()


def _observed(v1, a1, markers, bins=True):
    manifest, _ = _fixture()
    return {
        "binVideos": {os.path.basename(v["file"]) for v in manifest["videos"]} if bins else set(),
        "binAudio": {os.path.basename(a["file"]) for a in manifest["audio"]} if bins else set(),
        "timeline": {"name": "EDIT 01", "v1": v1, "a1": a1, "markers": markers},
    }


def _good_timeline():
    v1 = [
        {"name": "scene01_shot1A.mp4", "start": 0, "duration": 120},
        {"name": "scene01_shot2.mp4", "start": 120, "duration": 96},
        {"name": "scene02_shot1.mp4", "start": 216, "duration": 200},
    ]
    a1 = [
        {"name": "vo01_narrator.mp3", "start": 0},
        {"name": "vo03_scene02-shot9_ben.mp3", "start": 0},
        {"name": "vo02_scene01-shot2_ada.mp3", "start": 120},
    ]
    markers = {
        0: {"color": "Blue", "customData": rapi.SHOT_TAG},
        120: {"color": "Blue", "customData": rapi.SHOT_TAG},
        216: {"color": "Blue", "customData": rapi.SHOT_TAG},
        121: {"color": "Cyan", "customData": rapi.CUE_TAG, "duration": 24},
        145: {"color": "Green", "customData": rapi.CUE_TAG, "duration": 24},
        217: {"color": "Green", "customData": rapi.CUE_TAG, "duration": 48},
    }
    return v1, a1, markers


def test_verify_pass():
    manifest, base = _fixture()
    cues, _ = handoff.load_cues(manifest, base)
    v1, a1, markers = _good_timeline()
    out = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers))
    assert out["overall"] == "PASS", [c for c in out["checks"] if not c["pass"]]
    assert all(c["pass"] for c in out["checks"])
    names = [c["check"] for c in out["checks"]]
    for want in ("videos in bin", "audio in bin", "V1 clip count", "V1 clip order",
                 "no stray V1 clips", "shot markers", "shot marker positions", "cue markers"):
        assert want in names, want
    # backward-compatible report shape
    assert out["report"]["videos"] == {"expected": 3, "found": 3, "missing": []}
    assert out["report"]["timeline"]["videoClips"] == 3 and out["report"]["timeline"]["markers"] == 6


def test_verify_fails_on_order_stray_vo_delta_marker_and_cues():
    manifest, base = _fixture()
    cues, _ = handoff.load_cues(manifest, base)
    v1, a1, markers = _good_timeline()
    # swap two clips, add a stray, shift Ada's VO by 3 frames, lose a shot marker, drop a cue
    v1 = [v1[1], v1[0], v1[2], {"name": "stray.mp4", "start": 416, "duration": 10}]
    v1[0]["start"], v1[1]["start"] = 0, 96
    a1 = [dict(a1[0]), dict(a1[1]), {"name": "vo02_scene01-shot2_ada.mp3", "start": 3}]
    markers = dict(markers)
    del markers[216]
    del markers[217]
    out = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers))
    assert out["overall"] == "FAIL"
    by = {c["check"]: c for c in out["checks"]}
    assert by["V1 clip order"]["pass"] is False and by["V1 clip order"]["firstDivergence"] == 0
    assert by["no stray V1 clips"]["found"] == ["stray.mp4"] and not by["no stray V1 clips"]["pass"]
    assert by["V1 clip count"]["expected"] == 3 and by["V1 clip count"]["found"] == 4
    ada = by["VO vo02_scene01-shot2_ada.mp3 @ scene01_shot2.mp4"]
    assert ada["expected"] == 0 and ada["found"] == 3 and ada["delta"] == 3 and not ada["pass"]
    assert by["shot markers"]["expected"] == 3 and by["shot markers"]["found"] == 2
    assert by["shot marker positions"]["pass"] is False
    assert by["cue markers"]["expected"] == 3 and by["cue markers"]["found"] == 2 and not by["cue markers"]["pass"]
    # a 1-frame VO miss is still a miss — tolerance is zero
    a1[2]["start"] = 1
    out2 = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers))
    assert {c["check"]: c for c in out2["checks"]}[ada["check"]]["delta"] == 1
    assert out2["overall"] == "FAIL"


def test_verify_no_timeline_and_missing_bins_fail():
    manifest, base = _fixture()
    out = handoff.evaluate_verify(manifest, [], {"binVideos": set(), "binAudio": None, "timeline": None})
    assert out["overall"] == "FAIL"
    by = {c["check"]: c for c in out["checks"]}
    assert by["videos in bin"]["missing"] == ["scene01_shot1A.mp4", "scene01_shot2.mp4", "scene02_shot1.mp4"]
    assert by["V1 clip count"]["detail"] == "no timeline"
    assert out["report"]["timeline"] is None


def test_verify_untagged_blue_markers_count_as_shots():
    """Timelines built by the pre-tag server carry bare Blue markers."""
    manifest, base = _fixture()
    v1, a1, _ = _good_timeline()
    markers = {0: {"color": "Blue"}, 120: {"color": "Blue"}, 216: {"color": "Blue"}}
    out = handoff.evaluate_verify(manifest, [], _observed(v1, a1, markers), cues_expected=False)
    assert out["overall"] == "PASS", [c for c in out["checks"] if not c["pass"]]


def test_observe_timeline_and_range_markers_with_fakes():
    tl = FakeTimeline(
        v1=[FakeItem("a.mp4", 86400, 50), FakeItem("b.mp4", 86450, 30)],
        a1=[FakeItem("vo.mp3", 86450, 20)],
    )
    tl.AddMarker(0, "Blue", "shot 1", "", 1, rapi.SHOT_TAG)
    tl.AddMarker(50, "Blue", "shot 2", "", 1, rapi.SHOT_TAG)
    obs = rapi.observe_timeline(tl)
    assert obs["v1"] == [{"name": "a.mp4", "start": 0, "duration": 50}, {"name": "b.mp4", "start": 50, "duration": 30}]
    assert obs["a1"] == [{"name": "vo.mp3", "start": 50, "duration": 20}]
    assert obs["markers"][0]["customData"] == rapi.SHOT_TAG
    # cue markers: one collides with the shot-2 marker at 50 → nudged forward;
    # one has no frame → reported skipped, never dropped silently
    rep = rapi.add_range_markers(tl, [
        {"frame": 1, "duration": 10, "color": "Cyan", "name": "Ada", "note": "hi"},
        {"frame": 50, "duration": 5, "color": "Green", "name": "Ben", "note": "yo"},
        {"frame": None, "duration": 5, "color": "Green", "name": "Ben", "note": "lost"},
    ])
    assert rep["placed"] == 2 and len(rep["skipped"]) == 1
    assert tl.GetMarkers()[1]["duration"] == 10 and tl.GetMarkers()[1]["customData"] == rapi.CUE_TAG
    assert 51 in tl.GetMarkers() and tl.GetMarkers()[50]["customData"] == rapi.SHOT_TAG
    assert rapi.timeline_by_name  # exists


def test_tool_annotations_prompt_and_capabilities():
    import asyncio
    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"resolve_status", "launch_resolve", "list_templates", "export_template",
            "create_project", "import_package", "build_timeline", "verify_import"} <= names
    for t in tools:
        a = t.annotations
        assert a is not None, f"{t.name} has no annotations"
        for hint in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
            assert getattr(a, hint) is not None, f"{t.name}.{hint} unset"
        assert a.openWorldHint is False
    by = {t.name: t.annotations for t in tools}
    assert by["verify_import"].readOnlyHint is True and by["resolve_status"].readOnlyHint is True
    assert by["build_timeline"].readOnlyHint is False and by["build_timeline"].destructiveHint is False
    assert by["create_project"].idempotentHint is False
    prompts = asyncio.run(server.mcp.list_prompts())
    assert [p.name for p in prompts] == ["handoff"]
    text = asyncio.run(server.mcp.get_prompt("handoff", {"manifest_path": "/x/manifest.json"}))
    body = text.messages[0].content.text
    for step in ("create_project", "import_package", "build_timeline", "verify_import", "dry_run", "project=name",
                 "not imported yet", "OWN audio track named VO"):
        assert step in body
    # the dry run comes BEFORE create_project in the recipe
    assert body.index("dry_run=True") < body.index("create_project(kind, name)")
    caps = server.capabilities()
    assert caps["manifest"] == "oside-davinci/v1"
    assert caps["features"] == ["placements", "cues", "dry_run", "verify_v2", "vo_track"]
    assert caps["version"] == server._version() and caps["version"] != "0.0.0"
    # resolve_status hands the block back even when Resolve is unreachable
    st = server.resolve_status()
    assert st["capabilities"] == caps


def test_pipeline_dry_run_cli():
    import subprocess
    out = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(HERE), "pipeline.py"), FIXTURE, "--dry-run"],
        capture_output=True, text=True, check=False,
    )
    rep = json.loads(out.stdout.strip().splitlines()[-1])
    assert out.returncode == 0 and rep["ok"] and rep["dryRun"] is True
    assert rep["steps"]["plan"]["wouldBuild"] is True


def test_vo_lands_on_own_track_when_embedded_audio_fills_a1():
    """THE 2026-08-16 LIVE DEFECT. Every shot clip carries embedded audio, so
    appending the video fills A1. The pre-0.2.1 code then asked for
    trackIndex 1 at an occupied frame; Resolve answered a truthy list and
    placed NOTHING, and build_timeline reported voUnderShot 1 / voAtHead 1
    over an empty timeline (verify_v2 caught it: VO rows found:null).

    The fake reproduces both behaviours. On the old code this test goes RED
    (nothing on any audio track but the embedded audio; the VO rows in
    verify find null); on the fix it is green: a track named VO is added and
    every take is on it at its frame, confirmed by re-reading the track."""
    project, restore = _fake_resolve(audio_frames=(47, 121, 30))
    try:
        media_pool = project.GetMediaPool()
        real = server.build_timeline(FIXTURE, "EDIT 01")
        assert real["ok"], real
        tl = project.GetCurrentTimeline()
        # A1 is full of the shot clips' embedded audio at exactly the shot frames…
        a1 = [(x.GetName(), x.GetStart() - 86400) for x in tl.GetItemListInTrack("audio", 1)]
        assert a1 == [("scene01_shot1A.mp4", 0), ("scene01_shot2.mp4", 120), ("scene02_shot1.mp4", 216)]
        # …which is precisely why an A1 append at 0 or 120 is a silent drop:
        # truthy answer, nothing placed (the trap the old code fell into)
        narrator = rapi.clips_by_filename(rapi.find_bin(media_pool, "VO"))["vo01_narrator.mp3"]
        before = len(tl.GetItemListInTrack("audio", 1))
        answer = media_pool.AppendToTimeline([{"mediaPoolItem": narrator, "trackIndex": 1, "mediaType": 2, "recordFrame": 86400}])
        assert bool(answer) is True and len(tl.GetItemListInTrack("audio", 1)) == before
        # the fix: a VO track exists, named, and holds every take at its frame
        assert tl.GetTrackCount("audio") == 2 and tl.GetTrackName("audio", 2) == "VO"
        vo = [(x.GetName(), x.GetStart() - 86400, x.GetDuration()) for x in tl.GetItemListInTrack("audio", 2)]
        # Ben's pin has no clip → head → frame 0 is the narrator's → laid at
        # the VO track's tail (241 = Ada's end), still on the VO track
        assert vo == [("vo01_narrator.mp3", 0, 47), ("vo02_scene01-shot2_ada.mp3", 120, 121),
                      ("vo03_scene02-shot9_ben.mp3", 241, 30)]
        assert real["voUnderShot"] == 1 and real["voAtHead"] == 1 and real["voLoose"] == 1
        assert real["voTrack"] == "VO" and real["voTrackIndex"] == 2
        # the counts are backed by placements, not by return values: observe
        # agrees, and the gate finds every VO row on the VO track
        obs = rapi.observe_timeline(tl)
        assert [t["name"] for t in obs["audioTracks"]] == ["Audio 1", "VO"]
        assert obs["a1"] == obs["audioTracks"][0]["items"]
        ver = server.verify_import(FIXTURE, "EDIT 01")
        rows = [c for c in ver["checks"] if c["check"].startswith("VO ")]
        assert len(rows) == 3 and all(c["pass"] and c["track"] == "A2 VO" for c in rows), rows
        assert ver["overall"] == "PASS", [c for c in ver["checks"] if not c["pass"]]
        # the dry-run plan says where narration goes: track VO, every row
        dry = server.build_timeline(FIXTURE, "EDIT 02", dry_run=True)
        assert dry["plan"]["voTrack"] == "VO" and all(r["track"] == "VO" for r in dry["plan"]["vo"])
    finally:
        restore()


def test_append_audio_never_trusts_the_return_value():
    """append_audio judges a placement by re-reading the track. A pool that
    answers truthy but places nothing at BOTH the pinned frame and the tail
    is an error, never a counted clip."""
    class DeafPool:
        def AppendToTimeline(self, items):
            return [_RemoteObject()]  # truthy, nothing placed
    tl = FakeTimeline(audio_tracks=2)
    tl.SetTrackName("audio", 2, "VO")
    clip = FakeClip("vo.mp3", 40)
    try:
        rapi.append_audio(DeafPool(), tl, [{"item": clip, "recordFrame": 86400, "label": "x"}], 2)
    except rapi.ResolveError as e:
        assert "placed nothing" in str(e)
    else:
        raise AssertionError("a silent drop was counted as a placement")


def test_ensure_vo_track_reuses_named_track_else_adds_one():
    tl = FakeTimeline(audio_tracks=3)
    tl.SetTrackName("audio", 3, "VO")
    assert rapi.ensure_vo_track(tl) == 3 and tl.GetTrackCount("audio") == 3
    tl2 = FakeTimeline()
    assert rapi.ensure_vo_track(tl2) == 2 and tl2.GetTrackName("audio", 2) == "VO"


def test_clip_duration_frames_reads_audio_timecode():
    """Audio media-pool items answer '' for Frames — the length comes from the
    Duration timecode at the clip's FPS (00:00:05:01 @ 23.976 = 121)."""
    assert rapi.timecode_to_frames("00:00:05:01", 23.976) == 121
    assert rapi.timecode_to_frames("00:00:01:23", 23.976) == 47
    assert rapi.timecode_to_frames("00:01:00:00", 60) == 3600
    assert rapi.timecode_to_frames("garbage", 24) is None
    assert rapi.clip_duration_frames(FakeClip("vo.mp3", 121, fps=23.976)) == 121
    assert rapi.clip_duration_frames(FakeClip("v.mp4", 240)) == 240


def test_verify_finds_vo_on_any_audio_track_and_names_it():
    manifest, base = _fixture()
    v1, _a1, markers = _good_timeline()
    markers = {f: m for f, m in markers.items() if m["customData"] == rapi.SHOT_TAG}
    tracks = [
        {"index": 1, "name": "Audio 1", "items": [{"name": n, "start": s} for n, s in
                                                  (("scene01_shot1A.mp4", 0), ("scene01_shot2.mp4", 120), ("scene02_shot1.mp4", 216))]},
        {"index": 2, "name": "VO", "items": [{"name": "vo01_narrator.mp3", "start": 0},
                                              {"name": "vo02_scene01-shot2_ada.mp3", "start": 120},
                                              {"name": "vo03_scene02-shot9_ben.mp3", "start": 300}]},
    ]
    obs = _observed(v1, [], markers)
    obs["timeline"]["audioTracks"] = tracks
    out = handoff.evaluate_verify(manifest, [], obs, cues_expected=False)
    by = {c["check"]: c for c in out["checks"]}
    assert out["overall"] == "PASS", [c for c in out["checks"] if not c["pass"]]
    assert by["VO vo02_scene01-shot2_ada.mp3 @ scene01_shot2.mp4"]["track"] == "A2 VO"
    assert by["VO vo01_narrator.mp3 @ head"]["track"] == "A2 VO"
    # a VO track that is EMPTY (the live defect) → found null, FAIL, even
    # though A1 is full of embedded audio
    tracks[1]["items"] = []
    out2 = handoff.evaluate_verify(manifest, [], obs, cues_expected=False)
    by2 = {c["check"]: c for c in out2["checks"]}
    assert out2["overall"] == "FAIL"
    assert by2["VO vo01_narrator.mp3 @ head"]["found"] is None and by2["VO vo01_narrator.mp3 @ head"]["track"] is None


def test_dry_run_before_create_is_unknown_not_missing():
    """pipeline.py plans BEFORE create/import. With Resolve open on some OTHER
    project, planning for a project that does not exist yet must say
    'unknown — not imported yet' (never missing), judge wouldBuild on disk,
    and take fps from the kind's template — not the open project's."""
    project, restore = _fake_resolve(project_name="SOMETHING ELSE", with_bins=False, projects=["SOMETHING ELSE"])
    project._fps = "60"  # the open project is a 60fps explainer; the fixture is cinematic
    try:
        # the pre-fix reading (no project named): the open project IS the target,
        # its empty bins count → every file 'missing where bin', wouldBuild False
        naive = server.build_timeline(FIXTURE, "EDIT 01", dry_run=True)
        assert naive["ok"] and naive["wouldBuild"] is False
        assert all(m["where"] == "bin" for m in naive["missing"]) and len(naive["missing"]) == 6
        assert naive["plan"]["fps"] == 60.0 and naive["plan"]["fpsSource"] == "resolve"
        # project-aware: not created yet
        aware = server.build_timeline(FIXTURE, "EDIT 01", dry_run=True, project="NEW PROJECT")
        assert aware["ok"], aware
        assert aware["wouldBuild"] is True and aware["missing"] == []
        plan = aware["plan"]
        assert plan["resolveConnected"] is True
        assert plan["targetProject"] == {"name": "NEW PROJECT", "exists": False, "current": False}
        assert all(c["inBin"] == "unknown — not imported yet" for c in plan["clips"])
        assert all(r["inBin"] == "unknown — not imported yet" for r in plan["vo"])
        assert plan["fpsSource"] == "template" and abs(plan["fps"] - 23.976) < 1e-6
        assert all(c["onDisk"] for c in plan["clips"])
        # exists but not open: still unknown, worded so
        restore()
        project, restore = _fake_resolve(project_name="SOMETHING ELSE", with_bins=False,
                                         projects=["SOMETHING ELSE", "NEW PROJECT"])
        aware2 = server.build_timeline(FIXTURE, "EDIT 01", dry_run=True, project="NEW PROJECT")
        assert aware2["plan"]["targetProject"] == {"name": "NEW PROJECT", "exists": True, "current": False}
        assert all(c["inBin"] == "unknown — target project exists but is not open" for c in aware2["plan"]["clips"])
        # target IS the open project → the bins count again (here: empty → missing)
        restore()
        project, restore = _fake_resolve(project_name="NEW PROJECT", with_bins=False)
        cur = server.build_timeline(FIXTURE, "EDIT 01", dry_run=True, project="NEW PROJECT")
        assert cur["plan"]["targetProject"] == {"name": "NEW PROJECT", "exists": True, "current": True}
        assert cur["wouldBuild"] is False and len(cur["missing"]) == 6
        # a real build for the wrong project is refused, not run against the open one
        wrong = server.build_timeline(FIXTURE, "EDIT 01", project="ANOTHER")
        assert wrong["ok"] is False and "not the open project" in wrong["error"]
        assert project.GetTimelineCount() == 0
    finally:
        restore()


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
