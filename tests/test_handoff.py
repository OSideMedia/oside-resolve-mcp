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
        # the real template's A1 is STEREO (measured 2026-08-25); only tracks we
        # add ourselves are mono, and only because we now ask for it
        self._subtypes = {("audio", 1): "stereo"}
        self._markers = dict(markers or {})

    def GetName(self):
        return self._name

    def GetStartFrame(self):
        return self._start

    def GetTrackCount(self, kind):
        return sum(1 for k, _ in self._tracks if k == kind)

    def AddTrack(self, kind, subtype=None):
        """Models the vendor contract: `AddTrack(trackType, subTrackType)`, and
        subTrackType DEFAULTS TO 'mono' for audio when omitted (vendor README
        line 376). Measured 2026-08-25 on 21.0.4.5 — a bare AddTrack('audio')
        really does come back 'mono' while the template's A1 is 'stereo'."""
        idx = self.GetTrackCount(kind) + 1
        self._tracks[(kind, idx)] = []
        if kind == "audio":
            self._subtypes[(kind, idx)] = (subtype or "mono")
        return True

    def GetTrackSubType(self, kind, idx):
        if kind != "audio":
            return ""
        return self._subtypes.get((kind, idx), "stereo")

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

    def SetCurrentTimeline(self, timeline):
        """Takes a TIMELINE OBJECT, exactly as Blackmagic's reference specifies
        ("SetCurrentTimeline(timeline) --> Bool", Developer/Scripting/README.txt).

        This fake used to accept a STRING, because the code under test passed
        one. That is how the wrong call survived every green run: the fixture
        implemented the defect rather than the API, so the assertion could never
        go red [audit 2026-08-24]. A string is now refused the way a real Resolve
        would refuse it — it answers False rather than switching."""
        if isinstance(timeline, str) or timeline not in self._timelines:
            return False
        self._current = timeline
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


def _observed(v1, a1, markers, bins=True, vo=None):
    """An observation. `vo`, when given, is a second audio track named VO — the
    layout a correct build produces. Without it the observation carries A1
    alone, which is what a pre-0.2.1 (or hand-made) timeline looks like."""
    manifest, _ = _fixture()
    tracks = [{"index": 1, "name": "A1", "items": a1}]
    if vo is not None:
        tracks.append({"index": 2, "name": rapi.VO_TRACK_NAME, "items": vo})
    return {
        "binVideos": {os.path.basename(v["file"]) for v in manifest["videos"]} if bins else set(),
        "binAudio": {os.path.basename(a["file"]) for a in manifest["audio"]} if bins else set(),
        "timeline": {"name": "EDIT 01", "v1": v1, "a1": a1, "markers": markers,
                     "audioTracks": tracks},
    }


def _good_timeline():
    """A timeline a CORRECT build produces.

    The narration used to sit on `a1` here, and `test_verify_pass` asserted PASS
    over it — so the canonical "good" fixture encoded the pre-0.2.1 layout, the
    very defect the `vo_track` feature was built to prevent (A1 is full of the
    shot clips' embedded audio, Resolve accepts the append and places nothing).
    The gate could not tell the two apart, and this fixture is why nobody
    noticed. VO now rides its own named track [audit 2026-08-24]; A1 carries the
    shot clips' embedded audio, as it does in Resolve.

    Shot markers carry their names, because the gate now binds a marker to the
    shot it names rather than comparing bare frame lists.
    """
    v1 = [
        {"name": "scene01_shot1A.mp4", "start": 0, "duration": 120},
        {"name": "scene01_shot2.mp4", "start": 120, "duration": 96},
        {"name": "scene02_shot1.mp4", "start": 216, "duration": 200},
    ]
    a1 = []  # embedded audio from the shot clips — narration never lands here
    vo = [
        {"name": "vo01_narrator.mp3", "start": 0},
        {"name": "vo03_scene02-shot9_ben.mp3", "start": 0},
        {"name": "vo02_scene01-shot2_ada.mp3", "start": 120},
    ]
    markers = {
        0: {"color": "Blue", "name": "shot 1A", "customData": rapi.SHOT_TAG},
        120: {"color": "Blue", "name": "shot 2", "customData": rapi.SHOT_TAG},
        216: {"color": "Blue", "name": "shot 1", "customData": rapi.SHOT_TAG},
        121: {"color": "Cyan", "customData": rapi.CUE_TAG, "duration": 24},
        145: {"color": "Green", "customData": rapi.CUE_TAG, "duration": 24},
        217: {"color": "Green", "customData": rapi.CUE_TAG, "duration": 48},
    }
    return v1, a1, markers, vo


def test_verify_pass():
    manifest, base = _fixture()
    cues, _ = handoff.load_cues(manifest, base)
    v1, a1, markers, vo = _good_timeline()
    out = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers, vo=vo))
    assert out["overall"] == "PASS", [c for c in out["checks"] if not c["pass"]]
    assert all(c["pass"] for c in out["checks"])
    names = [c["check"] for c in out["checks"]]
    for want in ("videos in bin", "audio in bin", "V1 clip count", "V1 clip order",
                 "no stray V1 clips", "shot markers", "shot marker positions", "cue markers",
                 "shot markers name their own shot"):
        assert want in names, want
    # backward-compatible report shape
    assert out["report"]["videos"] == {"expected": 3, "found": 3, "missing": []}
    assert out["report"]["timeline"]["videoClips"] == 3 and out["report"]["timeline"]["markers"] == 6


def test_verify_fails_on_order_stray_vo_delta_marker_and_cues():
    manifest, base = _fixture()
    cues, _ = handoff.load_cues(manifest, base)
    v1, a1, markers, vo = _good_timeline()
    # swap two clips, add a stray, shift Ada's VO by 3 frames, lose a shot marker, drop a cue
    v1 = [v1[1], v1[0], v1[2], {"name": "stray.mp4", "start": 416, "duration": 10}]
    v1[0]["start"], v1[1]["start"] = 0, 96
    # narration still rides the VO track — this test is about ORDER, STRAYS and a
    # shifted take, not about which track VO landed on (that has its own row now)
    vo = [dict(vo[0]), dict(vo[1]), {"name": "vo02_scene01-shot2_ada.mp3", "start": 3}]
    markers = dict(markers)
    del markers[216]
    del markers[217]
    out = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers, vo=vo))
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
    vo[2]["start"] = 1
    out2 = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers, vo=vo))
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
    v1, a1, _, vo = _good_timeline()
    markers = {0: {"color": "Blue"}, 120: {"color": "Blue"}, 216: {"color": "Blue"}}
    out = handoff.evaluate_verify(manifest, [], _observed(v1, a1, markers, vo=vo), cues_expected=False)
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
    assert caps["features"] == ["placements", "cues", "dry_run", "verify_v2", "vo_track",
                                "look", "text_tasks", "verify_v3", "post_save", "cdl_envelope",
                                "cdl_readback"]
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
        rows = [c for c in ver["checks"] if c["check"].startswith("VO ") and "@" in c["check"]]
        assert len(rows) == 3 and all(c["pass"] and c["track"] == "A2 VO" for c in rows), rows
        # and each take now also carries the row that ASSERTS it is off A1 —
        # the gate used to report the track without ever requiring it
        off_a1 = [c for c in ver["checks"] if c["check"].endswith("not on A1")]
        assert len(off_a1) == 3 and all(c["pass"] and c["found"] == "A2 VO" for c in off_a1), off_a1
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
    v1, _a1, markers, vo = _good_timeline()
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



# ---------------------------------------------------------------------------
# "look travels" — apply_look's pure half (load_look_cdl / plan_look / cdl_payload)
# ---------------------------------------------------------------------------

def _look_pkg(tmpdir: str, with_look=True, with_cdl=True, schema="oside-look-cdl/1"):
    manifest = {
        "format": "oside-davinci/v1", "kind": "cinematic",
        "videos": [
            {"file": "videos/scene01_shot1A.mp4", "sceneIndex": 1, "shotNumber": "1A"},
            {"file": "videos/scene01_shot2A.mp4", "sceneIndex": 1, "shotNumber": "2A"},
            {"file": "videos/scene01_shot3A.mp4", "sceneIndex": 1, "shotNumber": "3A"},
        ],
    }
    if with_look:
        manifest["look"] = {"schema": "oside-look/1", "name": "Test look", "hex": ["#3d4245", "#847262", "#c6ab91"]}
    if with_cdl:
        doc = {"schema": schema, "look": "Test look", "clips": [
            {"file": "videos/scene01_shot1A.mp4", "identity": False, "measured": {"luma": 12.0},
             "cdl": {"slope": [1, 1, 1], "offset": [-0.0878, -0.0572, -0.0267], "power": [1, 1, 1], "saturation": 0.806}},
            {"file": "videos/scene01_shot2A.mp4", "identity": True, "measured": {"luma": 0.1},
             "cdl": {"slope": [1, 1, 1], "offset": [0, 0, 0], "power": [1, 1, 1], "saturation": 1.0}},
            {"file": "videos/scene01_shot3A.mp4", "error": "unreadable"},
        ]}
        with open(os.path.join(tmpdir, "look-cdl.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f)
    return manifest


def test_look_refuses_without_block_or_cdl_file():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        m = _look_pkg(d, with_look=False)
        doc, why = handoff.load_look_cdl(m, d)
        assert doc is None and "no `look` block" in why
    with tempfile.TemporaryDirectory() as d:
        m = _look_pkg(d, with_look=True, with_cdl=False)
        doc, why = handoff.load_look_cdl(m, d)
        assert doc is None and "look-cdl.json is missing" in why and "--emit-cdl" in why
    with tempfile.TemporaryDirectory() as d:
        m = _look_pkg(d, schema="oside-look-cdl/9")
        doc, why = handoff.load_look_cdl(m, d)
        assert doc is None and "schema" in why


def test_look_plan_matches_by_clip_name_and_reports_missing_and_extra():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        m = _look_pkg(d)
        doc, why = handoff.load_look_cdl(m, d)
        assert doc and why is None
        v1 = [{"name": "scene01_shot1A.mp4"}, {"name": "scene01_shot2A.mp4"}, {"name": "stray.mov"}]
        plan = handoff.plan_look(m, doc, v1)
        assert [r["clip"] for r in plan["rows"]] == ["scene01_shot1A.mp4", "scene01_shot2A.mp4"]
        assert plan["rows"][0]["identity"] is False and plan["rows"][1]["identity"] is True
        assert plan["rows"][0]["shot"] == "scene 1 shot 1A"
        assert plan["missing"] == [{"clip": "scene01_shot3A.mp4", "why": "no CDL for this clip in look-cdl.json (measured file missing or unreadable)"}]
        assert plan["extra"] == ["stray.mov"]
        # 3A present on V1 but with no CDL is still 'missing', never graded blind
        plan2 = handoff.plan_look(m, doc, v1 + [{"name": "scene01_shot3A.mp4"}])
        assert any(x["clip"] == "scene01_shot3A.mp4" for x in plan2["missing"])


def test_cdl_payload_is_the_setcdl_shape():
    p = handoff.cdl_payload({"slope": [1, 1, 1], "offset": [-0.0878, -0.0572, -0.0267], "power": [1, 1, 1], "saturation": 0.806})
    assert p == {"NodeIndex": "1", "Slope": "1.0000 1.0000 1.0000", "Offset": "-0.0878 -0.0572 -0.0267",
                 "Power": "1.0000 1.0000 1.0000", "Saturation": "0.806"}


def test_capabilities_advertise_look():
    assert "look" in server.capabilities()["features"]


# ---- film stock: INTENT ONLY (0.3.1) --------------------------------------
# A stock is a creative grade, and apply_look sells a starting balance. These
# hold the line: the note reads only from a well-formed additive key, and it
# never turns into anything a node would consume.

def _look_with_stock(**intent):
    return {"look": {"schema": "oside-look/1", "name": "L", "stockIntent": intent}}


def test_stock_note_reads_a_well_formed_intent():
    note = handoff.stock_intent_note(
        _look_with_stock(id="velvia-50", label="Fuji Velvia 50", balance="daylight",
                         note="OSIDE measured saturation + contrast. Intent only: nothing has been applied for it.")
    )
    assert note.startswith("Stock intent: Fuji Velvia 50 (daylight)")
    assert "measured saturation + contrast" in note
    # the scope is stated ONCE — the head must not repeat the studio's clause
    assert note.count("nothing has been applied") == 1
    assert "colourist's call, nothing applied" not in note


def test_stock_note_absent_is_none_not_a_broken_marker():
    # an older studio build simply has no stockIntent — additive key
    assert handoff.stock_intent_note({"look": {"schema": "oside-look/1"}}) is None
    assert handoff.stock_intent_note({}) is None
    assert handoff.stock_intent_note({"look": "not-a-dict"}) is None
    # half-filled / hand-edited: unusable reads as "no stock"
    assert handoff.stock_intent_note(_look_with_stock(label="")) is None
    assert handoff.stock_intent_note(_look_with_stock(id="x")) is None


def test_stock_note_survives_a_partial_intent():
    # a note-less manifest (older or hand-edited) still says what it means
    note = handoff.stock_intent_note(_look_with_stock(label="Kodak Portra 400"))
    assert note == "Stock intent: Kodak Portra 400 — colourist's call, nothing applied."


def test_stock_marker_nudges_past_the_shot_marker_and_reports_its_frame():
    # frame 0 holds shot 1's Blue marker in every built timeline, so the stock
    # note must move over rather than vanish
    shot0 = {"color": rapi.SHOT_MARKER_COLOR, "name": "Shot 1", "note": "", "duration": 1, "customData": rapi.SHOT_TAG}
    tl = FakeTimeline(markers={0: shot0})
    out = rapi.add_stock_marker(tl, "Stock intent: X — colourist's call, nothing applied.")
    assert out["placed"] is True and out["frame"] == 1
    placed = tl.GetMarkers()[1]
    assert placed["customData"] == rapi.STOCK_TAG          # the TAG is the discriminator
    assert placed["color"] != rapi.SHOT_MARKER_COLOR       # …colour is only a hint
    assert "nothing applied" in placed["note"]
    assert tl.GetMarkers()[0]["customData"] == rapi.SHOT_TAG   # shot marker untouched

    # a head-dense timeline is REPORTED, never silently dropped
    blocked = FakeTimeline(markers={f: dict(shot0) for f in range(0, 8)})
    out = rapi.add_stock_marker(blocked, "note")
    assert out["placed"] is False and "occupied" in out["reason"]



# ---------------------------------------------------------------------------
# IN-FRAME TEXT (2026-08-17) — the worklist the studio's export writes when a
# shot asked for lettering the generation deliberately did not render.
# ---------------------------------------------------------------------------

def test_text_tasks_load_and_skip_blank_rows():
    manifest, base = _fixture()
    tasks, warn = handoff.load_text_tasks(manifest, base)
    assert warn is None
    # the fixture has three rows, one with an empty Text cell
    assert [t["text"] for t in tasks] == ["OPEN ALL NIGHT", "CLOSED"]


def test_text_tasks_absent_key_is_not_an_error():
    tasks, warn = handoff.load_text_tasks({}, ".")
    assert tasks == [] and warn is None


def test_text_tasks_named_but_missing_warns_and_never_blocks():
    manifest, base = _fixture()
    tasks, warn = handoff.load_text_tasks({**manifest, "textTasks": "nope.csv"}, base)
    assert tasks == [] and "missing" in warn


def test_text_task_markers_never_collide_with_the_cues_on_the_same_shot():
    """The point of placing them AFTER the cues. Given a shot whose cues run to
    frame 40, its text task must start at or past 40 — never on top of a line."""
    manifest, _ = _fixture()
    cue_rows = [
        {"file": "a.mp4", "frame": 1, "duration": 24},
        {"file": "a.mp4", "frame": 25, "duration": 15},
    ]
    rows = handoff.text_task_rows(
        manifest,
        [{"text": "CLOSED", "videoFile": "a.mp4", "shotNumber": "2", "sceneName": "X"}],
        cue_rows, {"a.mp4": 0}, {"a.mp4": 200},
    )
    assert rows[0]["frame"] >= 40, rows[0]["frame"]


def test_two_text_tasks_on_one_shot_take_different_frames():
    manifest, _ = _fixture()
    rows = handoff.text_task_rows(
        manifest,
        [{"text": "ONE", "videoFile": "a.mp4", "shotNumber": "1", "sceneName": "X"},
         {"text": "TWO", "videoFile": "a.mp4", "shotNumber": "1", "sceneName": "X"}],
        [], {"a.mp4": 0}, {"a.mp4": 200},
    )
    assert rows[0]["frame"] != rows[1]["frame"]


def test_text_task_on_a_silent_shot_still_clears_frame_zero():
    """scene01_shot1A has no dialogue, so nothing reserves frames for it — the
    task must still miss the Blue shot marker on the shot's first frame."""
    res = server.build_timeline(FIXTURE, dry_run=True)
    plan = res["plan"]
    starts = {m["file"]: m["frame"] for m in plan["markers"]}
    t = next(t for t in plan["textTaskMarkers"] if t["file"] == "scene01_shot1A.mp4")
    assert t["frame"] > starts["scene01_shot1A.mp4"]


def test_text_task_marker_carries_the_exact_wording_for_the_editor():
    res = server.build_timeline(FIXTURE, dry_run=True)
    t = next(t for t in res["plan"]["textTaskMarkers"] if t["file"] == "scene01_shot1A.mp4")
    assert "OPEN ALL NIGHT" in t["name"] and "OPEN ALL NIGHT" in t["note"]
    # and says WHY it is a task rather than a rendered sign
    assert "did not render it" in t["note"]


def test_text_tasks_ride_the_cues_switch():
    res = server.build_timeline(FIXTURE, dry_run=True, cues=False)
    assert res["plan"]["textTaskMarkers"] == []
    assert res["textTaskMarkers"] == 0


def test_text_task_without_a_clip_is_reported_not_placed():
    manifest, base = _fixture()
    rows = handoff.text_task_rows(
        manifest,
        [{"text": "GHOST", "videoFile": "videos/not_on_timeline.mp4", "shotNumber": "9", "sceneName": "X"}],
        [], {}, {},
    )
    assert rows[0]["frame"] is None and "no clip" in rows[0]["reason"]


def test_text_task_that_cannot_fit_inside_its_shot_is_reported_not_misplaced():
    """A marker nudged past its own clip would tell the editor to title the
    NEXT shot — worse than no marker, so it is reported instead."""
    manifest, _ = _fixture()
    cue = [{"file": "a.mp4", "frame": 0, "duration": 500}]
    rows = handoff.text_task_rows(
        manifest,
        [{"text": "LATE", "videoFile": "a.mp4", "shotNumber": "1", "sceneName": "X"}],
        cue, {"a.mp4": 0}, {"a.mp4": 100},
    )
    assert rows[0]["frame"] is None and "no free frame" in rows[0]["reason"]


def test_verify_counts_text_task_markers_by_tag():
    manifest, base = _fixture()
    tasks, _ = handoff.load_text_tasks(manifest, base)
    v1, a1, markers, vo = _good_timeline()
    # every task laid, correctly tagged
    for i, t in enumerate(tasks):
        markers[300 + i] = {"name": f"Text: {t['text']}", "customData": rapi.TEXT_TASK_TAG, "color": "Cream"}
    observed = _observed(v1, a1, markers)
    cues, _ = handoff.load_cues(manifest, base)
    res = handoff.evaluate_verify(manifest, cues, observed, text_tasks=tasks)
    row = next(c for c in res["checks"] if c["check"] == "text-task markers")
    assert row["pass"] and row["expected"] == len(tasks) == row["found"]


def test_verify_fails_when_a_text_task_marker_is_missing():
    manifest, base = _fixture()
    tasks, _ = handoff.load_text_tasks(manifest, base)
    v1, a1, markers, vo = _good_timeline()
    markers[300] = {"name": "Text: OPEN ALL NIGHT", "customData": rapi.TEXT_TASK_TAG, "color": "Cream"}
    observed = _observed(v1, a1, markers)
    cues, _ = handoff.load_cues(manifest, base)
    res = handoff.evaluate_verify(manifest, cues, observed, text_tasks=tasks)
    row = next(c for c in res["checks"] if c["check"] == "text-task markers")
    assert not row["pass"] and res["overall"] == "FAIL"


def test_a_package_with_no_worklist_adds_no_check_and_still_passes():
    """A package exported before the studio wrote text-tasks.csv must not gain
    a failing row asserting zero."""
    manifest, base = _fixture()
    v1, a1, markers, vo = _good_timeline()
    observed = _observed(v1, a1, markers)
    cues, _ = handoff.load_cues(manifest, base)
    res = handoff.evaluate_verify(manifest, cues, observed, text_tasks=[])
    assert not any(c["check"] == "text-task markers" for c in res["checks"])



# ---------------------------------------------------------------------------
# Audit 2026-08-24 — each of these goes RED against the pre-fix code.
# ---------------------------------------------------------------------------

def test_cue_that_outruns_its_shot_is_reported_not_laid_on_the_next_one():
    """The sibling of test_text_task_that_cannot_fit_inside_its_shot_... which
    existed for lettering and never for dialogue. Two ordinary lines under a
    short shot used to put cue 2 on the NEXT shot and cue 3 past the end of the
    timeline, with reason None on every row."""
    manifest = {"kind": "cinematic", "audio": [], "videos": [
        {"file": "videos/s1.mp4", "sceneIndex": 1, "shotNumber": "1"},
        {"file": "videos/s2.mp4", "sceneIndex": 1, "shotNumber": "2"}]}
    cues = [{"speaker": "ADA", "line": "one", "videoFile": "videos/s1.mp4", "estSeconds": 4.0},
            {"speaker": "BEN", "line": "two", "videoFile": "videos/s1.mp4", "estSeconds": 4.0},
            {"speaker": "ADA", "line": "three", "videoFile": "videos/s1.mp4", "estSeconds": 2.0}]
    rows = handoff.cue_rows(manifest, cues, {"s1.mp4": 0, "s2.mp4": 48},
                            {"s1.mp4": 48, "s2.mp4": 48}, 24.0)
    assert rows[0]["frame"] == 1 and rows[0]["limit"] == 48
    # the two that cannot fit are REPORTED, never placed on the next shot
    assert [r["frame"] for r in rows[1:]] == [None, None], rows
    assert all("no room left in this shot" in r["reason"] for r in rows[1:]), rows
    # and nothing ever lands at or past the next shot's first frame
    assert all(r["frame"] is None or r["frame"] < 48 for r in rows), rows


def test_the_nudge_never_crosses_the_clip_boundary():
    """The planner's guard is not enough on its own: the placement nudge knew
    nothing about the clip and walked a correctly-planned marker onto the next
    shot anyway, reporting it `placed`."""
    # shot A ends at frame 96; the task is planned at 94 and 94/95 are taken
    tl = FakeTimeline(name="T", start=0, markers={94: {"color": "Blue"}, 95: {"color": "Blue"}})
    out = rapi.add_text_task_markers(tl, [{"frame": 94, "name": "Text: SALE", "note": "", "limit": 96}])
    assert out["placed"] == 0, out
    assert out["skipped"] and "no free frame inside this shot" in out["skipped"][0]["reason"]
    assert 96 not in tl.GetMarkers(), "a marker crossed into the next shot"
    # the same guard on the cue nudge
    tl2 = FakeTimeline(name="T", start=0, markers={10: {"color": "Blue"}, 11: {"color": "Blue"}})
    out2 = rapi.add_range_markers(tl2, [{"frame": 10, "duration": 4, "color": "Cyan",
                                         "name": "ADA", "note": "x", "limit": 12}])
    assert out2["placed"] == 0 and 12 not in tl2.GetMarkers(), out2


def test_gate_fails_when_a_shot_marker_names_the_wrong_picture():
    """Rotating every shot marker onto a different clip used to return PASS:
    the gate compared sorted frame multisets and never read the marker's name."""
    manifest, base = _fixture()
    v1, a1, markers, vo = _good_timeline()
    rotated = dict(markers)
    rotated[0] = {**markers[0], "name": "shot 1"}       # belongs on 216
    rotated[216] = {**markers[216], "name": "shot 1A"}  # belongs on 0
    out = handoff.evaluate_verify(manifest, [], _observed(v1, a1, rotated, vo=vo),
                                  cues_expected=False)
    assert out["overall"] == "FAIL"
    row = {c["check"]: c for c in out["checks"]}["shot markers name their own shot"]
    assert not row["pass"] and len(row["mismatched"]) == 2, row
    # the two OLD rows still pass — which is exactly why this one had to exist
    by = {c["check"]: c for c in out["checks"]}
    assert by["shot markers"]["pass"] and by["shot marker positions"]["pass"]


def test_gate_does_not_fail_a_build_that_skipped_a_task_by_design():
    """`expected` must mean what the build INTENDED to place. Scoring against
    the raw worklist made a deliberate, correct skip read as a failure."""
    manifest, base = _fixture()
    v1, a1, markers, vo = _good_timeline()
    tasks = [{"text": "SALE", "videoFile": "videos/scene01_shot2.mp4", "shotNumber": "2", "sceneName": "X"},
             {"text": "OPEN", "videoFile": "videos/scene01_shot2.mp4", "shotNumber": "2", "sceneName": "X"}]
    # a plan in which only the first task could be placed
    planned = [{"frame": 130, "name": "Text: SALE", "note": "", "file": "scene01_shot2.mp4"},
               {"frame": None, "name": "Text: OPEN", "note": "", "file": "scene01_shot2.mp4",
                "reason": "no free frame inside this shot (its cues fill it)"}]
    m = dict(markers)
    m[130] = {"color": "Cream", "name": "Text: SALE", "note": "", "duration": 1,
              "customData": rapi.TEXT_TASK_TAG}
    cues, _ = handoff.load_cues(manifest, base)
    out = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, m, vo=vo),
                                  text_tasks=tasks, planned_text_tasks=planned)
    row = {c["check"]: c for c in out["checks"]}["text-task markers"]
    assert row["expected"] == 1 and row["found"] == 1 and row["pass"], row
    assert row["skippedByDesign"] == 1
    assert out["overall"] == "PASS", [c for c in out["checks"] if not c["pass"]]


def test_gate_fails_narration_left_on_a1():
    """The 2026-08-16 defect's own shape. The gate reported which track a take
    sat on and never required it to be off A1."""
    manifest, base = _fixture()
    v1, _a1, markers, vo = _good_timeline()
    out = handoff.evaluate_verify(manifest, [], _observed(v1, vo, markers), cues_expected=False)
    assert out["overall"] == "FAIL"
    rows = [c for c in out["checks"] if c["check"].endswith("not on A1")]
    assert rows and not any(c["pass"] for c in rows), rows


def test_gate_requires_the_stock_marker_when_the_manifest_names_a_stock():
    manifest, base = _fixture()
    v1, a1, markers, vo = _good_timeline()
    cues, _ = handoff.load_cues(manifest, base)
    obs = _observed(v1, a1, markers, vo=vo)
    out = handoff.evaluate_verify(manifest, cues, obs,
                                  stock_intent="Stock intent: Kodak 2383 — x")
    by = {c["check"]: c for c in out["checks"]}
    assert by["film-stock marker"]["found"] == 0 and not by["film-stock marker"]["pass"]
    # present -> passes
    m = dict(markers)
    m[3] = {"color": "Cocoa", "name": "Stock intent", "note": "x", "duration": 1,
            "customData": rapi.STOCK_TAG}
    out2 = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, m, vo=vo),
                                   stock_intent="Stock intent: Kodak 2383 — x")
    assert out2["overall"] == "PASS", [c for c in out2["checks"] if not c["pass"]]


def test_gate_surfaces_a_named_but_missing_worklist():
    manifest, base = _fixture()
    v1, a1, markers, vo = _good_timeline()
    out = handoff.evaluate_verify(manifest, [], _observed(v1, a1, markers, vo=vo),
                                  cues_expected=False,
                                  sidecar_warnings=["cues file named by the manifest is missing: /x/y.csv"])
    assert out["overall"] == "FAIL"
    assert any(c["check"] == "worklist file present" for c in out["checks"])


def test_a_cdl_outside_the_starting_balance_envelope_is_refused_by_name():
    """'never a creative grade' was carried in prose only. One clip's bad
    numbers must not be applied, and must not take the whole run down."""
    manifest, _ = _fixture()
    good = {"slope": [1, 1, 1], "offset": [0.01, 0.0, -0.01], "power": [1, 1, 1], "saturation": 0.95}
    doc = {"schema": handoff.LOOK_CDL_SCHEMA, "clips": [
        {"file": "videos/scene01_shot1A.mp4", "cdl": good},
        {"file": "videos/scene01_shot2.mp4", "cdl": {**good, "saturation": 0.1}},
        {"file": "videos/scene02_shot1.mp4", "cdl": {"slope": [1, 1], "offset": [0, 0, 0],
                                                     "power": [1, 1, 1], "saturation": 1.0}},
    ]}
    v1 = [{"name": "scene01_shot1A.mp4"}, {"name": "scene01_shot2.mp4"}, {"name": "scene02_shot1.mp4"}]
    plan = handoff.plan_look(manifest, doc, v1)
    assert [r["clip"] for r in plan["rows"]] == ["scene01_shot1A.mp4"]
    why = {m["clip"]: m["why"] for m in plan["missing"]}
    assert "saturation" in why["scene01_shot2.mp4"], why
    assert "slope" in why["scene02_shot1.mp4"], why


def test_a_malformed_cdl_entry_does_not_take_the_whole_run_down():
    """cdl_payload does cdl['slope'] unguarded — a missing key used to raise
    KeyError inside apply_look's blanket except and kill every clip."""
    assert handoff.cdl_problem({"offset": [0, 0, 0], "power": [1, 1, 1], "saturation": 1.0})
    assert handoff.cdl_problem(None)
    assert handoff.cdl_problem({"slope": [1, 1, 1], "offset": [0, 0, 0],
                                "power": [1, 1, 1], "saturation": "nope"})
    assert handoff.cdl_problem({"slope": [1, 1, 1], "offset": [0.9, 0, 0],
                                "power": [1, 1, 1], "saturation": 1.0})
    assert handoff.cdl_problem({"slope": [1, 1, 1], "offset": [0, 0, 0],
                                "power": [1, 1, 1], "saturation": 1.0}) is None


def test_a_source_at_another_rate_is_conformed_to_the_timeline():
    """Every OSIDE render is 24fps; the explainer template is 60. Reading the
    source frame count as timeline frames made the CONNECTED dry run short by
    182 frames per clip on every explainer board."""
    clip = FakeClip("shot.mp4", 121, fps=24.0)           # 5.04s at 24fps
    assert rapi.clip_duration_frames(clip) == 121         # no timeline rate -> unchanged
    assert rapi.clip_duration_frames(clip, 60.0) == 302   # 5.04s at 60fps (was read as 121)
    assert rapi.clip_duration_frames(clip, 23.976) == 121  # conforms frame-for-frame


def test_a_manifest_cannot_reach_outside_its_package_or_reuse_a_clip_name():
    base = os.path.join(HERE, "fixtures", "pkg")
    for bad, expect in (
        ({"videos": [{"file": "/etc/passwd"}], "audio": []}, "absolute path"),
        ({"videos": [{"file": "../../../etc/passwd"}], "audio": []}, "outside the package"),
        ({"videos": [{"file": "videos/a.mp4"}, {"file": "other/a.mp4"}], "audio": []}, "share the clip name"),
    ):
        try:
            server._check_entries(bad, base)
            raise AssertionError(f"expected a refusal for {bad}")
        except rapi.ResolveError as e:
            assert expect in str(e), (expect, str(e))
    # the real fixture is fine
    manifest, b = _fixture()
    server._check_entries(manifest, b)


def test_stock_marker_is_idempotent():
    """apply_look is documented re-runnable and the CDL half was — but the stock
    marker used to be added AGAIN on every run, so re-applying a look three times
    left three 'Stock intent' markers. Surfaced by the 2026-08-24 walk."""
    tl = FakeTimeline(name="T", start=0, markers={0: {"color": "Blue", "customData": rapi.SHOT_TAG}})
    first = rapi.add_stock_marker(tl, "Stock intent: Kodak 2383 — x")
    assert first["placed"] and first["frame"] == 1, first
    n_after_first = len(tl.GetMarkers())
    for _ in range(5):
        again = rapi.add_stock_marker(tl, "Stock intent: Kodak 2383 — x")
        assert again["placed"] and again.get("alreadyPresent") is True, again
        assert again["frame"] == 1
    assert len(tl.GetMarkers()) == n_after_first, "a re-run added another stock marker"
    assert sum(1 for m in tl.GetMarkers().values()
               if (m.get("customData") or "") == rapi.STOCK_TAG) == 1


def test_markers_resolve_collisions_from_one_read_not_by_retrying_writes():
    """The search for a free frame happens in a set read ONCE. A loop over
    WRITES is the shape that turns a repeated call into a pile of markers."""
    occupied = {0, 2, 3}
    assert rapi.first_free_frame(occupied, 0, 8) == 1
    assert rapi.first_free_frame(occupied, 2, 8) == 4
    # the clip boundary still wins over the window
    assert rapi.first_free_frame({0, 1}, 0, 8, limit=2) is None
    # and a placement records itself so the next search skips it, with no re-read
    tl = FakeTimeline(name="T", start=0)
    occ = rapi.marker_frames(tl)
    at = rapi.first_free_frame(occ, 5, 4)
    rapi.add_marker_once(tl, occ, at, "Cocoa", "n", "note", 1, rapi.STOCK_TAG)
    assert at in occ and rapi.first_free_frame(occ, 5, 4) == 6


def test_clip_frames_floor_to_match_resolve():
    """MEASURED on the 2026-08-24 walk: a 5.041667s 24fps source lands as 120
    frames on a 23.976 timeline and 302 on a 60fps one. Rounding gave 121/303,
    so the dry run drifted a frame per clip against the timeline it predicts."""
    assert handoff.seconds_to_clip_frames(5.041667, 23.976) == 120
    assert handoff.seconds_to_clip_frames(5.041667, 60.0) == 302
    # cue durations still ROUND — shaving a frame off every spoken line is the
    # wrong direction for a reading aid
    assert handoff.seconds_to_frames(5.041667, 60.0) == 303
    assert handoff.seconds_to_clip_frames(None, 24) is None
    assert handoff.seconds_to_clip_frames(0.001, 24) == 1   # never zero-length


_EDL = """TITLE: readback
FCM: NON-DROP FRAME

001  AX       V     C        00:00:00:00 00:00:05:00 00:00:00:00 00:00:05:00
*ASC_SOP (1.000000 1.000000 1.000000)(-0.025000 -0.020000 -0.015000)(1.000000 1.000000 1.000000)
*ASC_SAT 0.940000

002  AX       V     C        00:00:00:00 00:00:05:00 00:00:05:00 00:00:10:00
*ASC_SOP (1.000000 1.000000 1.000000)(0.025000 0.030000 0.035000)(1.000000 1.000000 1.000000)
*ASC_SAT 1.050000
"""


def test_cdl_edl_parses_in_event_order():
    """The EDL is the ONLY read-back Resolve offers for a grade — there is no
    GetCDL. Its reel column is `AX` on every event, so events match clips by
    POSITION, and the parser must preserve order."""
    rows = handoff.parse_cdl_edl(_EDL)
    assert len(rows) == 2, rows
    assert rows[0]["offset"] == [-0.025, -0.02, -0.015]
    assert rows[0]["saturation"] == 0.94
    assert rows[1]["offset"] == [0.025, 0.03, 0.035]
    assert rows[1]["saturation"] == 1.05
    assert rows[0]["slope"] == [1.0, 1.0, 1.0] and rows[0]["power"] == [1.0, 1.0, 1.0]
    assert handoff.parse_cdl_edl("") == []


def test_cdl_readback_catches_a_grade_that_did_not_take():
    """A verdict that cannot go red is not a verdict. Same CDL -> match; a
    changed channel, a changed saturation, or an absent event -> caught."""
    got = handoff.parse_cdl_edl(_EDL)
    same = {"slope": [1, 1, 1], "offset": [-0.025, -0.02, -0.015],
            "power": [1, 1, 1], "saturation": 0.94}
    assert handoff.compare_cdl(same, got[0])["match"] is True

    # one offset channel silently not applied
    drifted = handoff.compare_cdl({**same, "offset": [-0.025, -0.02, 0.20]}, got[0])
    assert drifted["match"] is False and "offset[2]" in drifted["diffs"][0], drifted

    # saturation applied at the wrong value
    sat = handoff.compare_cdl({**same, "saturation": 0.60}, got[0])
    assert sat["match"] is False and "saturation" in sat["diffs"][0], sat

    # the clip has no event in the EDL at all — SetCDL claimed success over nothing
    absent = handoff.compare_cdl(same, None)
    assert absent["match"] is False and "no CDL for this clip" in absent["diffs"][0]

    # tolerance is real but tight: 6-decimal EDL vs our 4-decimal write
    near = handoff.compare_cdl({**same, "offset": [-0.0250004, -0.02, -0.015]}, got[0])
    assert near["match"] is True


def test_vo_track_is_mono_by_decision_not_by_default():
    """Narration is MONO by decision [Peter, 2026-08-25]; stereo is reserved for
    SFX and music. AddTrack('audio') defaults to mono SILENTLY, so until 0.5.x
    every VO track OSIDE built was mono by an undocumented default nobody chose.
    We now ask for it and read it back — and a Resolve that hands us something
    else must not receive narration."""
    tl = FakeTimeline(name="T", start=0)
    assert tl.GetTrackSubType("audio", 1) == "stereo", "the template's A1 is stereo"
    idx = rapi.ensure_vo_track(tl)
    assert idx == 2
    assert tl.GetTrackSubType("audio", idx) == rapi.VO_TRACK_SUBTYPE == "mono"
    assert tl.GetTrackName("audio", idx) == rapi.VO_TRACK_NAME

    # a Resolve that gives us the WRONG format is refused, not used
    class WrongFormat(FakeTimeline):
        def GetTrackSubType(self, kind, idx):
            return "5.1" if idx > 1 else "stereo"
    try:
        rapi.ensure_vo_track(WrongFormat(name="T", start=0))
        raise AssertionError("expected a refusal when the track came back 5.1")
    except rapi.ResolveError as e:
        assert "mono" in str(e) and "5.1" in str(e), str(e)


def test_an_existing_vo_track_is_reused_whatever_its_format():
    """The subtype check guards what we CREATE. A timeline that already carries
    a VO track — a rebuild, or one the editor made — is reused as-is; refusing
    it would block a build over a track we did not lay."""
    tl = FakeTimeline(name="T", start=0, audio_tracks=2)
    tl.SetTrackName("audio", 2, rapi.VO_TRACK_NAME)
    tl._subtypes[("audio", 2)] = "stereo"
    assert rapi.ensure_vo_track(tl) == 2      # reused, no refusal


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