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
    """A timeline item, with COLOUR VERSIONS modelled per the vendor contract and
    the 2026-08-25 live measurements on 21.0.4.5:

      * a clip starts with one version, 'Version 1';
      * `AddVersion(name, 0)` CREATES AND SWITCHES — GetCurrentVersion reports
        the new one immediately;
      * `SetCDL` writes into whichever version is ACTIVE.

    Before this the fake had no SetCDL and no versions at all, so apply_look's
    impure half had never been exercised by anything.
    """
    def __init__(self, name, start, duration, left_offset=0, media=None, embedded=False):
        self._n, self._s, self._d = name, start, duration
        self._left, self._media, self._embedded = left_offset, media, embedded
        self._versions = {"Version 1": None}
        self._current = "Version 1"

    def GetName(self):
        return self._n

    def GetStart(self):
        return self._s

    def GetDuration(self):
        return self._d

    def GetLeftOffset(self, subframe_precision=False):
        """Timeline frames of source before the in-point (measured 2026-10-25 on
        21.1.1: a 25 fps item trimmed from item frame 5 on a 60 fps timeline
        answers 12)."""
        return float(self._left)

    def GetMediaPoolItem(self):
        return self._media

    # ---- colour versions ----
    def GetVersionNameList(self, version_type=0):
        return list(self._versions)

    def GetCurrentVersion(self):
        return {"versionName": self._current, "versionType": 0}

    def AddVersion(self, name, version_type=0):
        if name in self._versions:
            return False
        self._versions[name] = None
        self._current = name          # measured: AddVersion switches
        return True

    def LoadVersionByName(self, name, version_type=0):
        if name not in self._versions:
            return False
        self._current = name
        return True

    def SetCDL(self, payload):
        self._versions[self._current] = dict(payload)   # lands in the ACTIVE version
        return True

    def cdl_in(self, version):
        return self._versions.get(version)


_EXPORT_OTIO, _EXPORT_NONE, _EXPORT_EDL = object(), object(), object()


class FakeTimeline:
    """`fps`: the timeline's own rate, answered by GetSetting — None leaves the
    rate to the project, and the clip-info append then lays trims frame for
    frame as the older cells expect.

    `tail_tracks`: audio tracks that model the 21.1.0.14 PLAIN-TRACK collision
    (measured 2026-09-08): a clip aimed at an occupied frame lands at the track
    TAIL, truncated to about min(recordFrame, clipLength), behind a live,
    honest proxy. Every other track keeps the embedded-audio mode the older
    cells were written against: nothing placed, a truthy answer.
    # ponytail: tail mode is opt-in per track; make it the default for plain tracks once append_audio's loose path is re-judged against it
    """
    def __init__(self, name="EDIT 01", start=86400, v1=(), a1=(), markers=None, audio_tracks=1,
                 fps=None, end=None, tail_tracks=(), export_bytes=None):
        self._name, self._start = name, start
        self._fps, self._end, self._tail = fps, end, set(tail_tracks)
        self._export_bytes = export_bytes
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

    def GetEndFrame(self):
        """The end of the last item on any track — never less than the `end`
        the fixture set, but a clip laid past it LENGTHENS the timeline, as
        Resolve's does. A fixed answer here could never fail
        timelineEndUnchanged."""
        ends = [x._s + x._d for lane in self._tracks.values() for x in lane]     # the fake's own state, not a read
        return max(ends + ([self._end] if self._end is not None else []), default=self._start)

    def GetSetting(self, key):
        if key == "timelineFrameRate" and self._fps is not None:
            return str(self._fps)
        return None

    def Export(self, path, export_type, export_subtype):
        """OTIO only, and only with the LIVE enum — a string is refused the way
        Resolve refuses it (no file, False). Writes the shape Resolve writes
        (measured 2026-10-25 on 21.1.1): video tracks then audio tracks, each a
        run of Gap.1 / Clip.2 children at the timeline rate. `export_bytes=0`
        reproduces the True-over-an-empty-file lie of anti-pattern 9. Like
        Resolve's, every track runs to the timeline end: an empty track is ONE
        Gap, and every other ends in a trailing Gap (measured 2026-10-25)."""
        if export_type is not _EXPORT_OTIO:
            return False
        if self._export_bytes == 0:
            open(path, "w").close()
            return True
        rate = float(self._fps or 24)

        def rt(v):
            return {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": float(v)}

        end = self.GetEndFrame()

        def gap(n):
            return {"OTIO_SCHEMA": "Gap.1", "metadata": {}, "name": "",
                    "source_range": {"OTIO_SCHEMA": "TimeRange.1", "duration": rt(n), "start_time": rt(0)},
                    "effects": [], "markers": [], "enabled": True}

        def track(kind, idx):
            pos, kids = self._start, []
            for x in sorted(self._tracks[(kind, idx)], key=lambda x: x.GetStart()):
                if x.GetStart() > pos:
                    kids.append(gap(x.GetStart() - pos))
                media = x.GetMediaPoolItem()
                url = media.GetClipProperty("File Path") if media is not None else None
                kids.append({"OTIO_SCHEMA": "Clip.2", "metadata": {}, "name": x.GetName(),
                             "source_range": {"OTIO_SCHEMA": "TimeRange.1", "duration": rt(x.GetDuration()),
                                              "start_time": rt(x._left)}, "effects": [], "markers": [], "enabled": True,
                             "media_references": {"DEFAULT_MEDIA": {"OTIO_SCHEMA": "ExternalReference.1",
                                                                    "target_url": url}},
                             "active_media_reference_key": "DEFAULT_MEDIA"})
                pos = x.GetStart() + x.GetDuration()
            if end > pos:
                kids.append(gap(end - pos))
            meta = {"Resolve_OTIO": {"Audio Type": self.GetTrackSubType(kind, idx).title()}} if kind == "audio" else {}
            return {"OTIO_SCHEMA": "Track.1", "metadata": meta, "name": self.GetTrackName(kind, idx),
                    "source_range": None, "effects": [], "markers": [], "enabled": True,
                    "children": kids, "kind": kind.title()}

        order = sorted(self._tracks, key=lambda k: (k[0] != "video", k[1]))
        doc = {"OTIO_SCHEMA": "Timeline.1", "metadata": {"Resolve_OTIO": {"Resolve OTIO Meta Version": "1.0"}},
               "name": self._name, "global_start_time": rt(self._start),
               "tracks": {"OTIO_SCHEMA": "Stack.1", "metadata": {}, "name": "", "source_range": None,
                          "effects": [], "markers": [], "enabled": True,
                          "children": [track(k, i) for k, i in order]}}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return True

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
    def __init__(self, name, frames, fps=24.0, embedded_audio=None, path=None):
        self._n, self._f, self._fps, self._path = name, frames, fps, path
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
        if key == "File Path":
            return self._path
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
    def __init__(self, project, root, disk=None):
        self._project, self._root, self._current = project, root, root
        # path -> (frames, fps) for files ImportMedia can read; anything else
        # is a file Resolve cannot open, and imports nothing
        self._disk = dict(disk or {})
        self.imports = []

    def GetRootFolder(self):
        return self._root

    def GetCurrentFolder(self):
        return self._current

    def SetCurrentFolder(self, folder):
        self._current = folder
        return True

    def ImportMedia(self, paths):
        """A plain list of path strings imports into the CURRENT folder. The
        documented dict form returns None and imports nothing on 21.1 (measured
        2026-09-08). A path already in the folder comes back as the same item
        (idempotent across calls, measured 2026-08-25 on 21.0.4.5)."""
        if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
            return None
        out = []
        for p in paths:
            self.imports.append(p)
            have = [c for c in self._current._clips if c.GetClipProperty("File Path") == p]
            if have:
                out.append(have[0])
                continue
            if p not in self._disk:
                continue
            frames, fps = self._disk[p]
            clip = FakeClip(os.path.basename(p), frames, fps=fps, path=p)
            self._current._clips.append(clip)
            out.append(clip)
        return out

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
                left = 0
                if "endFrame" in it:
                    left = it.get("startFrame", 0)
                    length = it["endFrame"] - left  # endFrame exclusive, as Resolve does it
                    # a mismatched source rate conforms by DURATION and floors
                    # (anti-pattern 7) — only when the timeline states its rate
                    src = float(clip.GetClipProperty("FPS") or 0)
                    if tl._fps and src and abs(src - float(tl._fps)) >= 0.05:
                        k = float(tl._fps) / src
                        length, left = max(1, int(length * k)), int(left * k)
                else:
                    length = rapi.clip_duration_frames(clip)
                lane = tl._tracks.setdefault(track, [])
                if track in {("audio", i) for i in tl._tail}:
                    held = [x for x in lane if x.GetStart() <= start < x.GetStart() + x.GetDuration()]
                    if held and not any(x._embedded for x in held):
                        tail = max(x.GetStart() + x.GetDuration() for x in lane)
                        item = FakeItem(clip.GetName(), tail, max(1, min(start - tl._start, length)), left, clip)
                        lane.append(item)
                        out.append(item)   # live and honest, and in the wrong place
                        continue
                if any(x.GetStart() == start for x in lane):
                    out.append(_RemoteObject())   # truthy — and nothing placed
                    continue
                item = FakeItem(clip.GetName(), start, length, left, clip)
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
                    tl._tracks[("audio", 1)].append(FakeItem(it.GetName(), end, length, embedded=True))
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
                  project_name="FIXTURE", with_bins=True, projects=None, manifest_path=None):
    """Wire server/rapi to a fake Resolve holding the fixture's clips in
    VIDEOS / VO bins (or an empty pool with with_bins=False). Returns
    (project, restore_fn).

    `manifest_path` stocks the bins from ANOTHER package instead of the fixture
    — needed whenever a test builds a manifest the fixture does not contain
    (an extra take, a different pin), or the clip would never reach the bin and
    the build would report it unplaced for the wrong reason."""
    manifest, _ = _fixture() if manifest_path is None else server._load_manifest(manifest_path)
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

        EXPORT_OTIO, EXPORT_NONE, EXPORT_EDL = _EXPORT_OTIO, _EXPORT_NONE, _EXPORT_EDL

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
        # …and each ROW names the lane it actually landed on, by door: the
        # board-wide takes on the spine, the pinned take on VO PINS. Stamping
        # the spine's name onto every row made pinned rows claim "VO" while the
        # take sat elsewhere — an honest aggregate over a lying detail.
        lanes = {os.path.basename(r["file"]): (r["track"], r["trackIndex"]) for r in real["plan"]["vo"]}
        assert lanes == {"vo01_narrator.mp3": ("VO", 2),
                         "vo03_scene02-shot9_ben.mp3": ("VO", 2),
                         "vo02_scene01-shot2_ada.mp3": ("VO PINS", 3)}, lanes
        # placedAt used to be None on every row; the lay now reports it
        assert {os.path.basename(r["file"]): r["placedAt"] for r in real["plan"]["vo"]} == {
            "vo01_narrator.mp3": 0, "vo03_scene02-shot9_ben.mp3": 288, "vo02_scene01-shot2_ada.mp3": 120}
        assert real["cueMarkers"] == 3 and real["cueMarkersSkipped"] == []
        # dry and real plans agree on the rows that matter
        for key in ("clips", "markers", "vo", "cueMarkers"):
            assert len(dry["plan"][key]) == len(real["plan"][key]), key
        assert [c["startFrame"] for c in real["plan"]["clips"]] == [0, 120, 216]
        assert [c["frame"] for c in real["plan"]["cueMarkers"]] == [121, 145, 217]

        tl = project.GetCurrentTimeline()
        assert tl.GetName() == "EDIT 01"
        assert tl.GetTrackName("audio", 2) == "VO" and tl.GetTrackName("audio", 3) == "VO PINS"
        assert [x.GetName() for x in tl.GetItemListInTrack("audio", 1)] == [
            "scene01_shot1A.mp4", "scene01_shot2.mp4", "scene02_shot1.mp4"]
        # the spine keeps VO; Ben is a pin whose shot has no clip, so he is a
        # head take too and slides behind the narrator (unpinned, no contract)
        assert [(x.GetName(), x.GetStart() - 86400, x.GetDuration()) for x in tl.GetItemListInTrack("audio", 2)] == [
            ("vo01_narrator.mp3", 0, 288), ("vo03_scene02-shot9_ben.mp3", 288, 48)]
        # Ada is PINNED, so she rides VO PINS and lands on her shot's own frame
        # — under the single-track layout the narrator held 0..288 and she was
        # refused to 288, which is the defect this split removes
        assert [(x.GetName(), x.GetStart() - 86400, x.GetDuration()) for x in tl.GetItemListInTrack("audio", 3)] == [
            ("vo02_scene01-shot2_ada.mp3", 120, 74)]
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
        assert ada0["found"] == 120 and ada0["delta"] == 0 and ada0["track"] == "A3 VO PINS"
        assert ver["clean"] is True
        # a PINNED take one frame off is a FAIL — zero tolerance
        tl.GetItemListInTrack("audio", 3)[0]._s += 1  # Ada's clip, on the PINS lane
        ver2 = server.verify_import(FIXTURE, "EDIT 01")
        ada = next(c for c in ver2["checks"] if c["check"].startswith("VO vo02"))
        assert ver2["overall"] == "FAIL" and ada["delta"] == 1 and not ada["pass"]
        tl.GetItemListInTrack("audio", 3)[0]._s -= 1

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


def _observed(v1, a1, markers, bins=True, vo=None, pins=None):
    """An observation. `vo`, when given, is a second audio track named VO — the
    SPINE lane, carrying board-wide takes. `pins`, when given, is a third track
    named VO PINS carrying the takes pinned to a shot. That is the layout a
    correct build produces since 2026-09-08: the two doors OSIDE exports
    through overlap in time, so they cannot share one track. Without either the
    observation carries A1 alone, which is what a pre-0.2.1 (or hand-made)
    timeline looks like."""
    manifest, _ = _fixture()
    tracks = [{"index": 1, "name": "A1", "items": a1}]
    if vo is not None:
        tracks.append({"index": 2, "name": rapi.VO_TRACK_NAME, "items": vo})
    if pins is not None:
        tracks.append({"index": len(tracks) + 1, "name": rapi.VO_PINS_TRACK_NAME, "items": pins})
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
    # lengths are the manifest's durationSeconds conformed to 23.976 (12.0s,
    # 2.0s, 3.1s): the length gate is only real if the good fixture carries one.
    # THE SPINE LANE — board-wide takes only. Ben's pin names a shot with no
    # clip in this package, so he is a head take too and sits behind the
    # narrator (unpinned takes have no frame contract).
    vo = [
        {"name": "vo01_narrator.mp3", "start": 0, "duration": 287},
        {"name": "vo03_scene02-shot9_ben.mp3", "start": 287, "duration": 47},
    ]
    # THE PINS LANE — Ada is pinned to scene01_shot2 and lands on its frame.
    # Under the old single-track layout the narrator held 0..287 and she was
    # refused to the tail; keeping her here is what makes the gate honest.
    pins = [
        {"name": "vo02_scene01-shot2_ada.mp3", "start": 120, "duration": 74},
    ]
    markers = {
        0: {"color": "Blue", "name": "shot 1A", "customData": rapi.SHOT_TAG},
        120: {"color": "Blue", "name": "shot 2", "customData": rapi.SHOT_TAG},
        216: {"color": "Blue", "name": "shot 1", "customData": rapi.SHOT_TAG},
        121: {"color": "Cyan", "customData": rapi.CUE_TAG, "duration": 24},
        145: {"color": "Green", "customData": rapi.CUE_TAG, "duration": 24},
        217: {"color": "Green", "customData": rapi.CUE_TAG, "duration": 48},
    }
    return v1, a1, markers, vo, pins


def test_verify_pass():
    manifest, base = _fixture()
    cues, _ = handoff.load_cues(manifest, base)
    v1, a1, markers, vo, pins = _good_timeline()
    out = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers, vo=vo, pins=pins))
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
    v1, a1, markers, vo, pins = _good_timeline()
    # swap two clips, add a stray, shift Ada's VO by 3 frames, lose a shot marker, drop a cue
    v1 = [v1[1], v1[0], v1[2], {"name": "stray.mp4", "start": 416, "duration": 10}]
    v1[0]["start"], v1[1]["start"] = 0, 96
    # narration still rides the VO track — this test is about ORDER, STRAYS and a
    # shifted take, not about which track VO landed on (that has its own row now)
    vo = [dict(vo[0]), dict(vo[1]), {"name": "vo02_scene01-shot2_ada.mp3", "start": 3}]
    markers = dict(markers)
    del markers[216]
    del markers[217]
    out = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers, vo=vo, pins=pins))
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
    out2 = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers, vo=vo, pins=pins))
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
    v1, a1, _, vo, pins = _good_timeline()
    markers = {0: {"color": "Blue"}, 120: {"color": "Blue"}, 216: {"color": "Blue"}}
    out = handoff.evaluate_verify(manifest, [], _observed(v1, a1, markers, vo=vo, pins=pins), cues_expected=False)
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
            "create_project", "import_package", "build_timeline", "verify_import",
            "place_sfx", "snapshot_timeline"} <= names
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
    # place_sfx writes into an existing timeline but only ADDS, and a re-run is a no-op
    assert by["place_sfx"].readOnlyHint is False and by["place_sfx"].destructiveHint is False
    assert by["place_sfx"].idempotentHint is True
    # snapshot_timeline writes a new file and refuses an existing one
    assert by["snapshot_timeline"].destructiveHint is False and by["snapshot_timeline"].idempotentHint is False
    prompts = asyncio.run(server.mcp.list_prompts())
    assert [p.name for p in prompts] == ["handoff"]
    text = asyncio.run(server.mcp.get_prompt("handoff", {"manifest_path": "/x/manifest.json"}))
    body = text.messages[0].content.text
    for step in ("create_project", "import_package", "build_timeline", "verify_import", "dry_run", "project=name",
                 "not imported yet",
                 # the LANE contract, not just "a track called VO": an agent
                 # reading this prompt must know pins do not ride the spine
                 "NEVER A1", "head of VO", "VO PINS"):
        assert step in body
    # the dry run comes BEFORE create_project in the recipe
    assert body.index("dry_run=True") < body.index("create_project(kind, name)")
    caps = server.capabilities()
    assert caps["manifest"] == "oside-davinci/v1"
    assert caps["features"] == ["placements", "cues", "dry_run", "verify_v2", "vo_track",
                                "look", "text_tasks", "verify_v3", "post_save", "cdl_envelope",
                                "cdl_readback", "verify_json", "vo_lanes", "sfx_plan"]
    assert caps["sfxPlan"] == "oside-sfx-plan/v1"
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
        assert tl.GetTrackCount("audio") == 3
        assert tl.GetTrackName("audio", 2) == "VO" and tl.GetTrackName("audio", 3) == "VO PINS"
        vo = [(x.GetName(), x.GetStart() - 86400, x.GetDuration()) for x in tl.GetItemListInTrack("audio", 2)]
        # Ben's pin has no clip → head → frame 0 is the narrator's → laid at
        # the VO track's tail, still on the spine track
        assert vo == [("vo01_narrator.mp3", 0, 47), ("vo03_scene02-shot9_ben.mp3", 47, 30)]
        # the PINNED take has its own lane and its own frame
        pins = [(x.GetName(), x.GetStart() - 86400, x.GetDuration()) for x in tl.GetItemListInTrack("audio", 3)]
        assert pins == [("vo02_scene01-shot2_ada.mp3", 120, 121)]
        assert real["voUnderShot"] == 1 and real["voAtHead"] == 1 and real["voLoose"] == 1
        assert real["voTrack"] == "VO" and real["voTrackIndex"] == 2
        # the counts are backed by placements, not by return values: observe
        # agrees, and the gate finds every VO row on the VO track
        obs = rapi.observe_timeline(tl)
        assert [t["name"] for t in obs["audioTracks"]] == ["Audio 1", "VO", "VO PINS"]
        assert obs["a1"] == obs["audioTracks"][0]["items"]
        ver = server.verify_import(FIXTURE, "EDIT 01")
        rows = [c for c in ver["checks"] if c["check"].startswith("VO ") and "@" in c["check"]]
        assert len(rows) == 3 and all(c["pass"] for c in rows), rows
        by_track = {c["check"].split()[1]: c["track"] for c in rows}
        assert by_track["vo01_narrator.mp3"] == "A2 VO"
        assert by_track["vo03_scene02-shot9_ben.mp3"] == "A2 VO"
        assert by_track["vo02_scene01-shot2_ada.mp3"] == "A3 VO PINS"
        # and each take now also carries the row that ASSERTS it is off A1 —
        # the gate used to report the track without ever requiring it
        off_a1 = [c for c in ver["checks"] if c["check"].endswith("not on A1")]
        assert len(off_a1) == 3 and all(c["pass"] for c in off_a1), off_a1
        assert {c["found"] for c in off_a1} == {"A2 VO", "A3 VO PINS"}
        assert ver["overall"] == "PASS", [c for c in ver["checks"] if not c["pass"]]
        # the dry-run plan says where narration goes: track VO, every row
        dry = server.build_timeline(FIXTURE, "EDIT 02", dry_run=True)
        assert dry["plan"]["voTrack"] == "VO"
        assert {r["mode"]: r["track"] for r in dry["plan"]["vo"]} == {"atHead": "VO", "underShot": "VO PINS"}
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
    v1, _a1, markers, vo, pins = _good_timeline()
    markers = {f: m for f, m in markers.items() if m["customData"] == rapi.SHOT_TAG}
    tracks = [
        {"index": 1, "name": "Audio 1", "items": [{"name": n, "start": s} for n, s in
                                                  (("scene01_shot1A.mp4", 0), ("scene01_shot2.mp4", 120), ("scene02_shot1.mp4", 216))]},
        {"index": 2, "name": "VO", "items": [{"name": "vo01_narrator.mp3", "start": 0},
                                              {"name": "vo03_scene02-shot9_ben.mp3", "start": 300}]},
        # the PINNED take rides its own lane — the gate must still find it on
        # whatever index it sits at, and name that index back
        {"index": 3, "name": "VO PINS", "items": [{"name": "vo02_scene01-shot2_ada.mp3", "start": 120}]},
    ]
    obs = _observed(v1, [], markers)
    obs["timeline"]["audioTracks"] = tracks
    out = handoff.evaluate_verify(manifest, [], obs, cues_expected=False)
    by = {c["check"]: c for c in out["checks"]}
    assert out["overall"] == "PASS", [c for c in out["checks"] if not c["pass"]]
    assert by["VO vo02_scene01-shot2_ada.mp3 @ scene01_shot2.mp4"]["track"] == "A3 VO PINS"
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
    v1, a1, markers, vo, pins = _good_timeline()
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
    v1, a1, markers, vo, pins = _good_timeline()
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
    v1, a1, markers, vo, pins = _good_timeline()
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
    v1, a1, markers, vo, pins = _good_timeline()
    rotated = dict(markers)
    rotated[0] = {**markers[0], "name": "shot 1"}       # belongs on 216
    rotated[216] = {**markers[216], "name": "shot 1A"}  # belongs on 0
    out = handoff.evaluate_verify(manifest, [], _observed(v1, a1, rotated, vo=vo, pins=pins),
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
    v1, a1, markers, vo, pins = _good_timeline()
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
    out = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, m, vo=vo, pins=pins),
                                  text_tasks=tasks, planned_text_tasks=planned)
    row = {c["check"]: c for c in out["checks"]}["text-task markers"]
    assert row["expected"] == 1 and row["found"] == 1 and row["pass"], row
    assert row["skippedByDesign"] == 1
    assert out["overall"] == "PASS", [c for c in out["checks"] if not c["pass"]]


def test_gate_fails_narration_left_on_a1():
    """The 2026-08-16 defect's own shape. The gate reported which track a take
    sat on and never required it to be off A1."""
    manifest, base = _fixture()
    v1, _a1, markers, vo, pins = _good_timeline()
    out = handoff.evaluate_verify(manifest, [], _observed(v1, vo, markers), cues_expected=False)
    assert out["overall"] == "FAIL"
    rows = [c for c in out["checks"] if c["check"].endswith("not on A1")]
    assert rows and not any(c["pass"] for c in rows), rows


def test_gate_requires_the_stock_marker_when_the_manifest_names_a_stock():
    manifest, base = _fixture()
    v1, a1, markers, vo, pins = _good_timeline()
    cues, _ = handoff.load_cues(manifest, base)
    obs = _observed(v1, a1, markers, vo=vo, pins=pins)
    out = handoff.evaluate_verify(manifest, cues, obs,
                                  stock_intent="Stock intent: Kodak 2383 — x")
    by = {c["check"]: c for c in out["checks"]}
    assert by["film-stock marker"]["found"] == 0 and not by["film-stock marker"]["pass"]
    # present -> passes
    m = dict(markers)
    m[3] = {"color": "Cocoa", "name": "Stock intent", "note": "x", "duration": 1,
            "customData": rapi.STOCK_TAG}
    out2 = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, m, vo=vo, pins=pins),
                                   stock_intent="Stock intent: Kodak 2383 — x")
    assert out2["overall"] == "PASS", [c for c in out2["checks"] if not c["pass"]]


def test_gate_surfaces_a_named_but_missing_worklist():
    manifest, base = _fixture()
    v1, a1, markers, vo, pins = _good_timeline()
    out = handoff.evaluate_verify(manifest, [], _observed(v1, a1, markers, vo=vo, pins=pins),
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



def test_the_vo_length_gate_can_actually_fail():
    """RED-PROOF for the length row added 2026-09-08.

    Resolve 21.1.0.14 places a take onto an occupied frame at the track TAIL
    **truncated** to about min(recordFrame, clipLength), answering truthy the
    whole way. That mode always MOVES the clip, so the start rows catch it —
    but nothing in the gate had an opinion about LENGTH, so a take sitting on
    its own frame carrying a fraction of its audio passed. This proves the new
    row is wired to a real subject and can go red on it.
    """
    manifest, base = _fixture()
    cues, _ = handoff.load_cues(manifest, base)

    # POSITIVE CONTROL — the subject is in the set and it PASSES on a good build
    v1, a1, markers, vo, pins = _good_timeline()
    out = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers, vo=vo, pins=pins))
    row = next((c for c in out["checks"] if c["check"] == "VO vo02_scene01-shot2_ada.mp3 length"), None)
    assert row is not None, "the length row never ran — the gate has no subject"
    assert row["pass"] and row["expected"] == 74 and row["found"] == 74
    assert out["overall"] == "PASS"

    # RED — same frame, truncated audio. Ada is pinned to scene01_shot2 (frame
    # 120) and still lands exactly there, so every START row stays green: this
    # is the one shape only the length row can see.
    v1, a1, markers, vo, pins = _good_timeline()
    pins[0]["duration"] = 20
    out = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers, vo=vo, pins=pins))
    by = {c["check"]: c for c in out["checks"]}
    assert by["VO vo02_scene01-shot2_ada.mp3 @ scene01_shot2.mp4"]["pass"] is True, \
        "the start row must stay GREEN, or this test is not isolating length"
    bad = by["VO vo02_scene01-shot2_ada.mp3 length"]
    assert bad["pass"] is False and bad["found"] == 20 and bad["shortBy"] == 54
    assert out["overall"] == "FAIL"

    # the slack is real: 2 frames short passes, 3 does not
    for short, want_pass in ((2, True), (3, False)):
        v1, a1, markers, vo, pins = _good_timeline()
        pins[0]["duration"] = 74 - short
        out = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers, vo=vo, pins=pins))
        got = {c["check"]: c for c in out["checks"]}["VO vo02_scene01-shot2_ada.mp3 length"]
        assert got["pass"] is want_pass, (short, got)



def test_a_spine_no_longer_swallows_the_pinned_takes():
    """THE 2026-09-08 DEFECT, end to end.

    OSIDE's exporter documents VO arriving through TWO doors and ships both:
    board-wide takes (`placements: []`) and takes pinned to one shot. Laid on a
    single track the spine went down first — a board-wide narration is often as
    long as the finished piece — and every pinned take was then refused to the
    tail. A real explainer package with a recorded spine plus any pin could not
    pass verify: pins expected at 119/214 landed at 288/363.

    The fix is a lane per door. This test asserts the PRECONDITION first (the
    spine really does cover the pin's frame, or the test proves nothing) and
    then that the pin lands on its own frame anyway.
    """
    project, restore = _fake_resolve()
    try:
        real = server.build_timeline(FIXTURE, "EDIT 01")
        assert real["ok"], real
        tl = project.GetCurrentTimeline()
        spine = [(x.GetName(), x.GetStart() - 86400, x.GetDuration())
                 for x in tl.GetItemListInTrack("audio", 2)]
        pins = [(x.GetName(), x.GetStart() - 86400, x.GetDuration())
                for x in tl.GetItemListInTrack("audio", 3)]

        # PRECONDITION — the subject is in the set: the spine occupies Ada's
        # pinned frame (120). Without this the lanes are untested luxury.
        narrator = next(c for c in spine if c[0] == "vo01_narrator.mp3")
        assert narrator[1] <= 120 < narrator[1] + narrator[2], \
            f"the spine does not cover the pinned frame — this test proves nothing: {narrator}"

        # …and the pin lands on its shot's frame regardless, on its own lane
        assert tl.GetTrackName("audio", 2) == "VO"
        assert tl.GetTrackName("audio", 3) == "VO PINS"
        assert pins == [("vo02_scene01-shot2_ada.mp3", 120, 74)]
        assert real["voUnderShot"] == 1

        ver = server.verify_import(FIXTURE, "EDIT 01")
        by = {c["check"]: c for c in ver["checks"]}
        ada = by["VO vo02_scene01-shot2_ada.mp3 @ scene01_shot2.mp4"]
        assert ada["pass"] and ada["delta"] == 0 and ada["track"] == "A3 VO PINS"
        assert by["VO vo02_scene01-shot2_ada.mp3 rides a pins lane"]["pass"]
    finally:
        restore()


def test_the_pins_lane_gate_can_actually_fail():
    """RED-PROOF for the lane row. A pinned take back on the spine track is the
    regression this split exists to prevent, and it must read as exactly that
    rather than as an unexplained frame delta."""
    manifest, base = _fixture()
    cues, _ = handoff.load_cues(manifest, base)

    # POSITIVE CONTROL — the row runs on a good build and passes.
    # The pins lane is built with a LITERAL name rather than rapi's constant so
    # that this test still expresses itself against source that has no such
    # constant: the red must come from the predicate (no lane row) and not from
    # an AttributeError raised before the gate is ever reached.
    v1, a1, markers, vo, pins = _good_timeline()
    obs = _observed(v1, a1, markers, vo=vo)
    obs["timeline"]["audioTracks"].append({"index": 3, "name": "VO PINS", "items": pins})
    out = handoff.evaluate_verify(manifest, cues, obs)
    row = next((c for c in out["checks"]
                if c["check"] == "VO vo02_scene01-shot2_ada.mp3 rides a pins lane"), None)
    assert row is not None, "the lane row never ran — the gate has no subject"
    assert row["pass"] and row["found"] == "A3 VO PINS"
    assert out["overall"] == "PASS"

    # RED — the pin is back on the spine lane, on its own frame. The FRAME row
    # stays green, so this shape is only visible to the lane row.
    v1, a1, markers, vo, pins = _good_timeline()
    out = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers, vo=vo + pins))
    by = {c["check"]: c for c in out["checks"]}
    assert by["VO vo02_scene01-shot2_ada.mp3 @ scene01_shot2.mp4"]["pass"] is True, \
        "the frame row must stay GREEN, or this test is not isolating the lane"
    bad = by["VO vo02_scene01-shot2_ada.mp3 rides a pins lane"]
    assert bad["pass"] is False and bad["found"] == "A2 VO"
    assert out["overall"] == "FAIL"

    # an UNPINNED take on the spine is correct and must NOT get a lane row
    assert "VO vo01_narrator.mp3 rides a pins lane" not in by


def test_colliding_pins_checkerboard_onto_a_second_lane():
    """Pins can collide with EACH OTHER — two takes pinned to the same shot
    want the same frame. The answer is another lane, never a slide to the tail:
    that slide is the behaviour the split removes, and re-introducing it as the
    overflow strategy would put the take back where nobody expects it."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp()
    dst = os.path.join(tmp, "pkg")
    shutil.copytree(os.path.dirname(FIXTURE), dst)
    shutil.copyfile(os.path.join(dst, "vo", "vo02_scene01-shot2_ada.mp3"),
                    os.path.join(dst, "vo", "vo04_scene01-shot2_ada2.mp3"))
    mpath = os.path.join(dst, "manifest.json")
    m = json.load(open(mpath))
    ada = m["audio"][1]
    second = json.loads(json.dumps(ada))          # same pin, a different take
    second["file"] = "vo/vo04_scene01-shot2_ada2.mp3"
    second["label"] = "Ada (alt)"
    second["narrationId"] = "n4"
    m["audio"] = [m["audio"][0], ada, second]     # spine, pin, colliding pin
    json.dump(m, open(mpath, "w"))

    project, restore = _fake_resolve(manifest_path=mpath, audio_frames=(288, 74, 74))
    try:
        real = server.build_timeline(mpath, "EDIT 01")
        assert real["ok"], real
        tl = project.GetCurrentTimeline()
        names = [tl.GetTrackName("audio", i) for i in range(1, tl.GetTrackCount("audio") + 1)]
        assert names == ["Audio 1", "VO", "VO PINS", "VO PINS 2"], names
        lane1 = [(x.GetName(), x.GetStart() - 86400) for x in tl.GetItemListInTrack("audio", 3)]
        lane2 = [(x.GetName(), x.GetStart() - 86400) for x in tl.GetItemListInTrack("audio", 4)]
        # BOTH takes on their shot's frame — the second checkerboarded, not slid
        assert lane1 == [("vo02_scene01-shot2_ada.mp3", 120)], lane1
        assert lane2 == [("vo04_scene01-shot2_ada2.mp3", 120)], lane2
        assert real["voUnderShot"] == 2 and real["voLoose"] == 0
        ver = server.verify_import(mpath, "EDIT 01")
        lanes = {c["check"]: c for c in ver["checks"] if c["check"].endswith("rides a pins lane")}
        assert len(lanes) == 2 and all(c["pass"] for c in lanes.values()), lanes
    finally:
        restore()
        shutil.rmtree(tmp)



# ---------------------------------------------------------------------------
# THE TEMPLATES ARE PUBLISHED BYTES. `export_template` snapshots the LIVE
# Resolve template project straight into templates/*.drp, so whatever those
# projects hold on the day it runs is what lands in the repo. Audited by hand
# 2026-09-08 (clean: bin and track names only) — but an audit is a moment and a
# gate is a guarantee, and the next snapshot is one command away.
#
# THE ZIP IS THE TRANSFORM THAT BLINDS THE GUARD. A .drp is a zip archive: every
# path, address and hostname inside it is COMPRESSED, so a scanner pointed at the
# file reads nothing and passes. The archive is therefore extracted and the
# MEMBERS are scanned — the guard is only as good as what it is allowed to see.
# ---------------------------------------------------------------------------

TEMPLATE_LEAK_PATTERNS = (
    (r"/Users/[A-Za-z0-9._-]+", "a macOS home path"),
    (r"/home/[A-Za-z0-9._-]+", "a Linux home path"),
    (r"/Volumes/[A-Za-z0-9 ._-]+", "a mounted volume"),
    (r"\b[A-Za-z]:\\\\[A-Za-z0-9 ._-]+", "a Windows path"),
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "an email address"),
    (r"\b[A-Za-z0-9-]+\.local\b", "a bonjour hostname"),
    (r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token)\b\s*[:=]", "a credential"),
)


# ---------------------------------------------------------------------------
# THE SECOND TRANSFORM (audit 2026-09-12, R-1, ow-8543c5). Unzipped, a member is
# XML — but its <FieldsBlob> elements are HEX-ENCODED byte strings, and inside
# them Resolve stores a zstd frame (project.xml, at a small offset behind a
# header) or a zlib stream (MpFolder.xml). The 2026-09-08 audit "by hand" read
# the XML, saw bin and track names, and called it clean; the operator's home
# directory, login and every mounted-volume label sat one decode further in,
# public, for five days. A guard is only as good as what it is allowed to see:
# every hex run is decoded, every compressed layer inside it is opened at any
# offset, and the text is read as UTF-8 AND UTF-16-LE (Resolve stores strings
# both ways). A decoder that is missing is a RED, never a skip.
# ---------------------------------------------------------------------------

def _inner_texts(member_bytes):
    """Every text this member carries, one decode layer at a time: the member
    itself, then each hex-encoded blob, then each zstd / zlib layer found inside
    those blobs, each read as UTF-8 and as UTF-16-LE. Yields (layer, text)."""
    import zlib
    try:
        import zstandard
    except ImportError as e:  # pragma: no cover - the environment is the subject here
        raise AssertionError(
            "the template leak gate cannot open Resolve's zstd layer: install the dev extra "
            "(pip install -e '.[dev]' / uv sync --extra dev) — a missing decoder is a red, not a pass"
        ) from e
    yield ("member", member_bytes.decode("utf-8", "ignore"))
    for i, hexrun in enumerate(re.findall(rb"[0-9a-fA-F]{64,}", member_bytes)):
        try:
            dec = bytes.fromhex(hexrun.decode("ascii"))
        except ValueError:
            continue
        layers = [("hex#%d" % i, dec)]
        for mo in re.finditer(rb"\x28\xb5\x2f\xfd", dec):
            try:
                layers.append(("hex#%d/zstd@%d" % (i, mo.start()), zstandard.ZstdDecompressor().decompressobj().decompress(dec[mo.start():])))
            except Exception as e:  # noqa: BLE001
                layers.append(("hex#%d/zstd@%d" % (i, mo.start()), b"UNREADABLE zstd frame: " + str(e).encode()))
        for mo in re.finditer(rb"\x78[\x01\x5e\x9c\xda]", dec):
            try:
                text = zlib.decompressobj().decompress(dec[mo.start():])
            except Exception:  # noqa: BLE001 - not every 0x78 is a stream
                continue
            if len(text) > 40:
                layers.append(("hex#%d/zlib@%d" % (i, mo.start()), text))
        for layer, raw in layers:
            yield (layer, raw.decode("utf-8", "ignore"))
            # UTF-16 at BOTH byte alignments: a string inside a binary record can start on
            # an odd offset, and a decode from 0 then reads every character as garbage
            yield (layer + "/utf16", raw.decode("utf-16-le", "ignore"))
            yield (layer + "/utf16+1", raw[1:].decode("utf-16-le", "ignore"))


def _scan_drp(path):
    """Every leak in one .drp, as (member, kind, sample). Extracts first: the
    archive is the transform, and a scan of the compressed bytes sees nothing —
    then decodes the hex blobs and the zstd/zlib layers inside them, which is
    where the 2026-09-08 audit's "clean" templates held the operator's home
    directory (R-1)."""
    import zipfile
    hits = []
    if not zipfile.is_zipfile(path):
        # NOT a skip. An unreadable template is UNKNOWN, and unknown is never a
        # pass — a future .drp format the scanner cannot open must fail loudly
        # rather than sail through as "no leaks found".
        return [(os.path.basename(path), "unreadable", "not a zip archive — the scanner cannot see inside it")]
    with zipfile.ZipFile(path) as z:
        for member in z.namelist():
            if member.endswith("/"):
                continue
            try:
                raw = z.read(member)
            except Exception as e:  # noqa: BLE001
                hits.append((member, "unreadable", str(e)))
                continue
            seen = set()
            for layer, text in _inner_texts(raw):
                if text.startswith("UNREADABLE"):
                    hits.append((member + " :: " + layer, "unreadable", text[:80]))
                    continue
                for pattern, kind in TEMPLATE_LEAK_PATTERNS:
                    m = re.search(pattern, text)
                    if m and (layer, kind) not in seen:
                        seen.add((layer, kind))
                        hits.append((member + " :: " + layer, kind, m.group(0)[:80]))
    return hits


def test_no_template_publishes_a_path_an_email_or_a_hostname():
    """The gate that lets these templates live in a public repo.

    Carries its own counterexample: a .drp-shaped archive with a real home path
    and an address in it is run through the SAME predicate first, and must be
    caught. Without that, "no leaks found" is indistinguishable from a scanner
    that cannot read a zip — which is exactly what a scanner pointed at the
    compressed file would report.
    """
    import zipfile

    # POSITIVE CONTROL — the scanner can SEE, and sees through the compression
    with tempfile.TemporaryDirectory() as td:
        planted = os.path.join(td, "LEAKY.drp")
        with zipfile.ZipFile(planted, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("project.xml", "<Project><Render>/Users/someone/Movies/cut.mov</Render></Project>")
            z.writestr("MediaPool/Master/MpFolder.xml", "<Name>client@agency.com</Name>")
        found = {kind for _m, kind, _s in _scan_drp(planted)}
        assert "a macOS home path" in found, "the scanner cannot see a path through the zip"
        assert "an email address" in found, "the scanner cannot see an address through the zip"
        # and the naive scan — the compressed BYTES — must NOT find them, which
        # is the whole reason the extraction step exists
        with open(planted, "rb") as fh:
            raw = fh.read().decode("utf-8", "ignore")
        assert "/Users/someone" not in raw, "the fixture did not actually compress — control is void"

    # POSITIVE CONTROL, ONE LAYER IN (R-1): the member's XML is clean; a home path
    # sits in a zstd frame behind a header inside a hex-encoded <FieldsBlob>, and a
    # volume label in a zlib stream inside another — Resolve's own shapes, measured
    # on the published templates 2026-09-13 (zstd at offset 44 in project.xml,
    # zlib at offset 4 in MpFolder.xml). Both are UTF-16-LE, as Resolve writes them.
    import zlib
    import zstandard
    with tempfile.TemporaryDirectory() as td:
        planted = os.path.join(td, "DEEP.drp")
        home = "/Users/someone/Movies/render.mov".encode("utf-16-le")
        volume = "/Volumes/T7 PROJECTS/clip.mov".encode("utf-16-le")
        zstd_blob = (b"\x00" * 44) + zstandard.ZstdCompressor().compress(b"\x00\x00" + home + b"\x00\x00")
        zlib_blob = b"\x00\x00\x00\x00" + zlib.compress(b"pad" + volume)
        with zipfile.ZipFile(planted, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("project.xml", "<SM_Project><FieldsBlob>%s</FieldsBlob></SM_Project>" % zstd_blob.hex())
            z.writestr("MediaPool/Master/MpFolder.xml", "<SM_Folder><FieldsBlob>%s</FieldsBlob></SM_Folder>" % zlib_blob.hex())
        deep = _scan_drp(planted)
        kinds = {kind for _m, kind, _s in deep}
        layers = {m for m, _k, _s in deep}
        assert "a macOS home path" in kinds and any("zstd@" in m for m in layers), "the scanner cannot see a home path through hex + zstd: %r" % (deep,)
        assert "a mounted volume" in kinds and any("zlib@" in m for m in layers), "the scanner cannot see a volume through hex + zlib: %r" % (deep,)
        # COUNTEREXAMPLE — the pre-fix scan (the members' own text) sees neither, which is
        # exactly how two templates carrying the operator's home directory were called clean
        with zipfile.ZipFile(planted) as z:
            flat = "".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist())
        assert not any(re.search(p, flat) for p, _k in TEMPLATE_LEAK_PATTERNS), "the fixture leaks in plain XML — the deep control is void"

    # THE SUBJECT SET, NAMED AND NON-EMPTY. An empty templates/ dir would make
    # every assertion below vacuously true, so the set is asserted first and
    # tied to what templates.json actually declares.
    with open(os.path.join(os.path.dirname(HERE), "templates", "templates.json"), encoding="utf-8") as fh:
        declared = json.load(fh)
    drps = sorted(glob.glob(os.path.join(os.path.dirname(HERE), "templates", "*.drp")))
    assert drps, "no templates/*.drp found — the gate has no subject"
    names = {os.path.basename(p) for p in drps}
    for kind, spec in declared.items():
        assert spec["drp"] in names, f"templates.json declares {spec['drp']!r} for {kind!r}, but it is not on disk"

    # THE SUBJECTS THEMSELVES
    leaks = {os.path.basename(p): _scan_drp(p) for p in drps}
    dirty = {k: v for k, v in leaks.items() if v}
    assert not dirty, (
        "a template would publish identifying data:\n"
        + "\n".join(f"  {f} :: {m} :: {kind} :: {sample!r}"
                     for f, hits in dirty.items() for m, kind, sample in hits)
    )



def test_the_readme_version_badge_matches_the_shipped_version():
    """A STATIC BADGE IS A CLAIM THAT ROTS. The README shows a version pill, and
    pyproject is the single place the version actually lives (`_version()` reads
    it). Nothing tied the two together, so the first release after the badge was
    added would have shipped a README advertising the previous version — the
    same class as the skill/prompt drift, and just as invisible.

    Red-proof: bump either side alone and this fails.
    """
    with open(README_PATH, encoding="utf-8") as fh:
        readme = fh.read()
    m = re.search(r"img\.shields\.io/badge/version-([0-9][^-\s)]*)-", readme)
    assert m, "the README has no version badge — the gate has no subject"
    badge = m.group(1)
    assert badge == server._version(), (
        f"README badge says {badge!r} but pyproject says {server._version()!r} — "
        "bump the badge with the version, or drop the badge"
    )


def test_the_licence_is_present_and_named_consistently():
    """A public repo with no LICENSE is 'all rights reserved' — readable but not
    usable, which defeats publishing it. Assert the file exists, that it is the
    MIT text, and that pyproject and the README agree with it."""
    root = os.path.dirname(HERE)
    lic = os.path.join(root, "LICENSE")
    assert os.path.exists(lic), "no LICENSE file — a public repo without one grants nobody any rights"
    with open(lic, encoding="utf-8") as fh:
        text = fh.read()
    assert "MIT License" in text and "WITHOUT WARRANTY OF ANY KIND" in text
    with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
        assert 'license = "MIT"' in fh.read(), "pyproject does not declare the licence"
    with open(README_PATH, encoding="utf-8") as fh:
        assert "badge/license-MIT" in fh.read(), "the README badge does not say MIT"


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


def test_the_look_lands_in_its_own_colour_version_not_the_colourists():
    """SetCDL writes node 1 of whatever version is ACTIVE, so the tool used to
    write into the same slot a colourist grades in — the one place this server
    overwrote a human's work. Measured 2026-08-25: AddVersion creates AND
    switches, so the CDL lands in ours with no extra call."""
    it = FakeItem("shot01.mp4", 0, 120)
    # the colourist's own work, in the version the clip ships with
    it.SetCDL({"NodeIndex": "1", "Saturation": "0.777"})
    assert it.cdl_in("Version 1")["Saturation"] == "0.777"

    ver = rapi.look_version(it)
    assert ver["version"] == rapi.LOOK_VERSION_NAME == "OSIDE base"
    assert ver["created"] is True and ver["active"] is True
    assert ver["was"] == "Version 1"

    it.SetCDL({"NodeIndex": "1", "Saturation": "0.940"})
    # ours landed in ours...
    assert it.cdl_in("OSIDE base")["Saturation"] == "0.940"
    # ...and THEIRS SURVIVED, which is the whole point
    assert it.cdl_in("Version 1")["Saturation"] == "0.777"
    # and ours is left ACTIVE, so the film still opens showing the balance
    assert it.GetCurrentVersion()["versionName"] == "OSIDE base"


def test_a_second_run_reuses_the_version_and_overwrites_only_ours():
    """A re-run overwriting OSIDE base is INTENDED — the version carries our
    name. What must not happen is a second version, or a write to theirs."""
    it = FakeItem("shot01.mp4", 0, 120)
    it.SetCDL({"Saturation": "0.777"})                     # colourist, Version 1
    rapi.look_version(it); it.SetCDL({"Saturation": "0.940"})
    again = rapi.look_version(it)
    assert again["created"] is False and again["active"] is True
    it.SetCDL({"Saturation": "1.010"})
    assert it.GetVersionNameList(0) == ["Version 1", "OSIDE base"], "no version proliferation"
    assert it.cdl_in("OSIDE base")["Saturation"] == "1.010"
    assert it.cdl_in("Version 1")["Saturation"] == "0.777"


def test_a_resolve_without_versions_falls_back_and_says_so():
    """Colour bookkeeping must never fail a build. A Resolve that will not give
    us a version writes the active one — today's behaviour — and reports why."""
    class NoVersions(FakeItem):
        def GetVersionNameList(self, version_type=0):
            raise AttributeError("not on this build")
    v = rapi.look_version(NoVersions("s.mp4", 0, 10))
    assert v["version"] is None and v["active"] is False
    assert "does not expose colour versions" in v["why"]

    class WontCreate(FakeItem):
        def AddVersion(self, name, version_type=0):
            return False
    v2 = rapi.look_version(WontCreate("s.mp4", 0, 10))
    assert v2["version"] is None and "could not create" in v2["why"]



# ---------------------------------------------------------------------------
# the handoff SKILL vs the handoff PROMPT — sibling-surface drift
#
# `~/.claude/skills/oside-resolve-handoff/SKILL.md` carries a SHORT version of
# the recipe `handoff_prompt()` ships. The skill says so itself and names the
# prompt as the source of truth, because the prompt lives inside the server and
# cannot drift from the tools; the skill is hand-maintained and can. It exists
# for DISCOVERY: a prompt only arrives when someone already knows to ask for it,
# and by then the first tool call is usually made.
#
# Omission is fine — the skill is deliberately an abridgement. What is NOT fine
# is the skill going WRONG: a step added to the prompt and missed here, a step
# removed and still listed, a tool renamed, or the order changed. That is the
# only class this gate holds, and it is the class that would make an agent
# follow a stale sequence.
#
# The skill is tracked in another repo (claude-commands), so this repo's CI
# cannot see it. Unreachable is UNKNOWN, never a pass: under pytest that is a
# visible skip. Registered as `python3 -m pytest -q tests`, which is the path
# that matters.
# ---------------------------------------------------------------------------

import ast  # noqa: E402
import glob  # noqa: E402
import re  # noqa: E402

SKILL_LOCATIONS = (
    os.path.expanduser("~/.claude/skills/oside-resolve-handoff/SKILL.md"),
    os.path.expanduser(
        "~/Projects/Toolkit/claude-commands/skills/oside-resolve-handoff/SKILL.md"),
)


def _skill_text():
    for p in SKILL_LOCATIONS:
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return fh.read()
    return None


def _shipped_tool_names():
    """The tools the server actually ships, read from its own @mcp.tool
    decorators rather than a list here that would need maintaining."""
    with open(os.path.join(os.path.dirname(HERE), "server.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                fn = d.func if isinstance(d, ast.Call) else d
                if isinstance(fn, ast.Attribute) and fn.attr == "tool":
                    names.add(node.name)
    return names


def _skill_steps(skill_text):
    """The ordered call sequence from the skill's fenced order block."""
    m = re.search(r"## The order, and it matters.*?```\n(.*?)```", skill_text, re.S)
    if not m:
        raise AssertionError(
            "the skill lost its fenced order block — that block IS the contract this "
            "gate reads; if the skill was restructured, update this test with it")
    return [re.match(r"\s*(\w+)\(", ln).group(1)
            for ln in m.group(1).splitlines() if re.match(r"\s*\w+\(", ln)]


def _drift(skill_text, prompt_text, shipped):
    """Pure predicate, so the red case below can run the SAME code on a mutated
    skill. Returns a list of complaints; empty means the two agree."""
    out = []
    steps = _skill_steps(skill_text)

    # 1. every step the skill names is a tool this server still ships
    for name in dict.fromkeys(steps):
        if name not in shipped:
            out.append(f"skill lists {name}(), which the server does not ship")

    # 2. every tool the PROMPT actually invokes must appear in the skill.
    #    This is the added-a-step case, the one that makes the skill wrong.
    invoked = dict.fromkeys(re.findall(r"\b(\w+)\(", prompt_text))
    for name in invoked:
        if name in shipped and name not in steps:
            out.append(f"prompt invokes {name}() but the skill's order block omits it")

    # 3. every step the skill names must at least be MENTIONED in the prompt.
    #    (mentioned, not invoked: the prompt says "call resolve_status." in prose)
    for name in dict.fromkeys(steps):
        if not re.search(r"\b" + re.escape(name) + r"\b", prompt_text):
            out.append(f"skill lists {name}() but the prompt never mentions it")

    # 4. order: first mention of each step in the prompt must run in skill order
    firsts = []
    for name in dict.fromkeys(steps):
        m = re.search(r"\b" + re.escape(name) + r"\b", prompt_text)
        if m:
            firsts.append((name, m.start()))
    for (an, ai), (bn, bi) in zip(firsts, firsts[1:]):
        if ai >= bi:
            out.append(f"order: skill puts {an}() before {bn}(); the prompt does not")
    return out


class Unknown(Exception):
    """A test that could not run here. NOT a pass (audit 2026-09-06 RM-2): the
    old _skip fell through to a print when pytest was absent — which it always
    is in this venv — and the tally still read 64/64 with the skill hidden.
    The runner counts these on their own line and never folds them into the
    passes; a skip needs positive evidence (CI=1 means the skill is absent by
    construction), otherwise the test FAILS naming what is missing."""


def _skip(why):
    if not os.environ.get("CI"):
        raise AssertionError(
            f"{why} — install ~/.claude/skills/oside-resolve-handoff (or set CI=1 where the "
            "skill is absent by construction). An absent subject is not a green gate."
        )
    raise Unknown(why)


def test_the_handoff_skill_has_not_drifted_from_the_prompt():
    """The skill is an abridgement of handoff_prompt(). Omission is allowed;
    contradiction is not."""
    skill = _skill_text()
    if skill is None:
        _skip("oside-resolve-handoff is not installed on this machine, so skill/prompt "
              "agreement is UNKNOWN here — it is not passing")
        return
    complaints = _drift(skill, server.handoff_prompt(), _shipped_tool_names())
    assert not complaints, "handoff skill has drifted from the prompt:\n  " + \
        "\n  ".join(complaints)


def test_the_drift_check_can_actually_fail():
    """The gate above is only worth having if it goes red. Run the SAME predicate
    over deliberately broken skills and require each to be caught."""
    skill = _skill_text()
    if skill is None:
        _skip("skill absent; the red case has nothing to mutate")
        return
    shipped = _shipped_tool_names()
    prompt = server.handoff_prompt()
    assert not _drift(skill, prompt, shipped), "precondition: the real pair agrees"

    # a) a reordered skill must be caught
    swapped = skill.replace("save_project()", "__TMP__(") \
                   .replace("verify_import(manifest)", "save_project()") \
                   .replace("__TMP__(", "verify_import(manifest)", 1)
    assert any("order:" in c for c in _drift(swapped, prompt, shipped)), \
        "a reordered skill went undetected"

    # b) a step dropped from the skill while the prompt still invokes it
    dropped = re.sub(r"^\s*apply_look\(.*\n", "", skill, count=1, flags=re.M)
    assert any("omits it" in c for c in _drift(dropped, prompt, shipped)), \
        "a step dropped from the skill went undetected"

    # c) a step the server no longer ships
    renamed = skill.replace("verify_import(manifest)", "verify_importt(manifest)")
    assert any("does not ship" in c for c in _drift(renamed, prompt, shipped)), \
        "a renamed/removed tool went undetected"


# ---------------------------------------------------------------------------
# Manifest SHAPE (audit 2026-09-06 RM-1/3/4/5/6). Fourteen corrupt variants
# were run by hand on 2026-09-06; these pin the ones that killed the pipeline
# or lost data silently, and require every refusal to have a README row.
# ---------------------------------------------------------------------------
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402

README_PATH = os.path.join(HERE, "..", "README.md")


def _mutated_package(mutate):
    """A throwaway copy of the fixture package with `mutate(manifest)` applied.
    Returns (manifest_path, tmpdir) — the caller removes tmpdir."""
    tmp = tempfile.mkdtemp(prefix="oside-mcp-shape-")
    pkg = os.path.join(tmp, "pkg")
    shutil.copytree(os.path.dirname(FIXTURE), pkg)
    path = os.path.join(pkg, "manifest.json")
    with open(path, encoding="utf-8") as fh:
        m = json.load(fh)
    mutate(m)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(m, fh)
    return path, tmp


def _refusal(mutate) -> str:
    path, tmp = _mutated_package(mutate)
    try:
        server._load_manifest(path)
    except rapi.ResolveError as e:
        return str(e)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    raise AssertionError("the corrupt manifest was accepted")


def _readme_has(phrase: str) -> bool:
    with open(README_PATH, encoding="utf-8") as fh:
        return phrase in fh.read()


def test_pipeline_prints_a_json_report_when_project_is_not_an_object():
    """RM-1 (P0). The one-click door reads stdout as JSON; zero bytes is
    'Pipeline returned no report'. Red on the pre-fix code: AttributeError
    outside the try, 0 bytes, exit 1."""
    path, tmp = _mutated_package(lambda m: m.__setitem__("project", "THE DROP"))
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "..", "pipeline.py"), path, "--dry-run"],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert proc.stdout.strip(), f"pipeline wrote NO report (stderr: {proc.stderr[-300:]})"
    report = json.loads(proc.stdout)
    assert report["ok"] is False and proc.returncode == 1
    assert "manifest.project" in report["error"], report["error"]
    assert "features" in report, "the capability block still rides the failure report"


def test_pipeline_report_survives_a_crash_past_the_load(monkeypatch=None):
    """The catch-all: a crash after the manifest loads still prints the
    report, naming the exception."""
    src = os.path.join(HERE, "..", "pipeline.py")
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    assert "pipeline crashed before a report could be built" in body
    assert body.index("def _run(") > body.index("except Exception as e:  # noqa: BLE001\n        report[\"error\"] = f\"pipeline crashed"), \
        "the catch-all wraps _run"


def test_corrupt_manifest_shapes_are_refused_with_a_readme_sentence():
    """RM-3/4/5/6: every refusal is an operator sentence with a README row,
    never a Python traceback or a silent loss."""
    cases = [
        ("project as string", lambda m: m.__setitem__("project", "x"), "manifest.project must be an object"),
        ("videos entry as string", lambda m: m["videos"].__setitem__(0, "videos/x.mp4"), "manifest.videos[0] must be an entry object"),
        ("videos entry without file", lambda m: m["videos"][0].pop("file"), "manifest.videos[0] has no `file`"),
        ("audio not a list", lambda m: m.__setitem__("audio", {"file": "a.mp3"}), "manifest.audio must be a list"),
        ("cues as list (RM-6)", lambda m: m.__setitem__("cues", ["cues.csv"]), "manifest.cues must be the worklist's filename"),
        ("textTasks as object (RM-6)", lambda m: m.__setitem__("textTasks", {"file": "t.csv"}), "manifest.textTasks must be the worklist's filename"),
        ("unknown kind (RM-5)", lambda m: m.__setitem__("kind", "documentary"), "Unknown kind 'documentary'"),
        ("look v2 (RM-4)", lambda m: m.__setitem__("look", {"schema": "oside-look/2", "hex": []}), "manifest.look.schema 'oside-look/2' is not oside-look/1"),
        ("look as string", lambda m: m.__setitem__("look", "warm"), "manifest.look must be an object"),
    ]
    # the README row each sentence lands in (a row may cover a key family)
    readme_row = {
        "manifest.audio must be a list": "manifest.videos must be a list of entries",
        "manifest.textTasks must be the worklist's filename": "manifest.cues must be the worklist's filename",
    }
    for label, mutate, expect in cases:
        msg = _refusal(mutate)
        assert expect in msg, f"{label}: {msg!r}"
        # the README row is keyed on the sentence's stable head (before any quoted value)
        head = expect.split(" '")[0].split("[0]")[0]
        head = readme_row.get(head, head)
        assert _readme_has(head), f"{label}: README's error table has no row for {head!r}"


def test_shape_check_accepts_the_fixture_and_the_optional_keys_absent():
    """The counterexample's twin: the real package still loads, and a manifest
    with none of the optional keys (no look, no cues, no textTasks) is fine."""
    server._load_manifest(FIXTURE)
    path, tmp = _mutated_package(lambda m: [m.pop(k, None) for k in ("look", "cues", "textTasks")])
    try:
        server._load_manifest(path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unknown_is_never_counted_as_a_pass():
    """RM-2: the runner tallies UNKNOWN on its own line; without CI the skip
    is a FAIL that names the missing skill."""
    src = os.path.join(HERE, "test_handoff.py")
    with open(src, encoding="utf-8") as fh:
        body = fh.read()
    assert "unknown (not passes)" in body and "except Unknown as e:" in body
    assert 'if not os.environ.get("CI")' in body

def test_verify_sidecar_keys_every_clip_by_generation_id():
    """step (e): the verdict reaches a file OSIDE can read — one row per clip by
    the manifest's generationId, placed = on V1, start/duration when it is."""
    import tempfile
    manifest, base = _fixture()
    cues, _ = handoff.load_cues(manifest, base)
    v1, a1, markers, vo, pins = _good_timeline()
    observed = _observed(v1, a1, markers, vo=vo, pins=pins)
    result = handoff.evaluate_verify(manifest, cues, observed)
    side = handoff.verify_sidecar(manifest, observed, result, "2026-09-06T00:00:00Z")
    assert side["format"] == "oside-verify/1" and side["overall"] == "PASS"
    assert set(side["clips"]) == {v["generationId"] for v in manifest["videos"]}
    for v in manifest["videos"]:
        row = side["clips"][v["generationId"]]
        assert row["placed"] is True and row["file"] == os.path.basename(v["file"])
        assert isinstance(row["start"], int) and isinstance(row["duration"], int)
    assert "unkeyed" not in side
    with tempfile.TemporaryDirectory() as td:
        path = handoff.write_verify_sidecar(td, side)
        assert os.path.basename(path) == "verify.json"
        with open(path, encoding="utf-8") as f:
            assert json.load(f) == side


def test_verify_sidecar_names_the_unplaced_and_the_unkeyed():
    manifest, base = _fixture()
    cues, _ = handoff.load_cues(manifest, base)
    v1, a1, markers, vo, pins = _good_timeline()
    v1 = v1[:-1]  # the last clip never landed
    observed = _observed(v1, a1, markers, vo=vo, pins=pins)
    result = handoff.evaluate_verify(manifest, cues, observed)
    last = manifest["videos"][-1]
    side = handoff.verify_sidecar(manifest, observed, result, "2026-09-06T00:00:00Z")
    assert side["overall"] == "FAIL"
    assert side["clips"][last["generationId"]] == {"file": os.path.basename(last["file"]), "placed": False, "start": None, "duration": None}
    stripped = dict(manifest, videos=[dict(v) for v in manifest["videos"]])
    stripped["videos"][0].pop("generationId")
    side2 = handoff.verify_sidecar(stripped, observed, result, "2026-09-06T00:00:00Z")
    assert len(side2["clips"]) == len(manifest["videos"]) - 1
    assert side2["unkeyed"][0]["file"] == os.path.basename(manifest["videos"][0]["file"])


def test_verify_sidecar_with_no_timeline_places_nothing():
    manifest, base = _fixture()
    side = handoff.verify_sidecar(manifest, {"timeline": None}, {"overall": "FAIL"}, "2026-09-06T00:00:00Z")
    assert all(row["placed"] is False for row in side["clips"].values())
    assert side["timeline"] == {"name": None}


def test_pending_take_is_named_on_its_shot_marker_and_in_the_gate():
    """RM-10: the fixture's shot 2 is `outcome: pending` — its marker name and
    note say so, its tag and colour stay (identity is the tag), and verify
    carries an advisory row naming it without failing the build."""
    manifest, base = _fixture()
    cues, _ = handoff.load_cues(manifest, base)
    plan = handoff.plan_timeline(
        manifest, base, "EDIT 01", cues, 24.0, "resolve", True,
        bin_lookup=lambda kind, name: True,
        duration_lookup=lambda v, p: (120, "resolve"),
    )
    pending = [m for m in plan["markers"] if m.get("outcome") == "pending"]
    assert len(pending) == 1, [m.get("outcome") for m in plan["markers"]]
    m = pending[0]
    assert m["name"].endswith("· PENDING TAKE") and m["note"].startswith("UNAPPROVED TAKE (outcome: pending)")
    assert m["color"] == "Blue"
    approved = [x for x in plan["markers"] if x.get("outcome") == "approved"]
    assert approved and not any("TAKE" in x["name"] for x in approved)
    v1, a1, markers, vo, pins = _good_timeline()
    out = handoff.evaluate_verify(manifest, cues, _observed(v1, a1, markers, vo=vo, pins=pins))
    row = next(c for c in out["checks"] if c["check"] == "unapproved takes on V1")
    assert row["pass"] is True and row["found"] == 1 and row.get("advisory") is True
    assert "shot 2" in row["detail"] or "1A" in row["detail"] or "2" in row["detail"]
    assert out["overall"] == "PASS"


def test_no_frame_reason_blames_the_gap_not_a_missing_clip():
    """RM-11: a planned clip whose start cannot be computed (an earlier clip's
    duration unmeasurable) must not be reported as 'no clip on V1'."""
    starts = {"a.mp4": 0, "b.mp4": None}
    assert handoff.no_frame_reason("zzz.mp4", starts, None).startswith("no frame (shot has no clip on V1)")
    r = handoff.no_frame_reason("b.mp4", starts, None)
    assert r.startswith("frame unknown") and "ffprobe" in r and "no clip" not in r
    # the shot-marker row carries the same reason when its frame is None
    manifest, base = _fixture()
    cues, _ = handoff.load_cues(manifest, base)
    # an unmeasurable FIRST clip: every later clip is planned yet has no frame,
    # and each marker row must say why (RM-11) — never a bare null
    plan = handoff.plan_timeline(
        manifest, base, "EDIT 01", cues, 24.0, "resolve", True,
        bin_lookup=lambda kind, name: True,
        duration_lookup=lambda v, p: (None, "unknown"),
    )
    frameless = [m for m in plan["markers"] if m["frame"] is None]
    assert frameless, [m["frame"] for m in plan["markers"]]
    for m in frameless:
        assert m.get("reason", "").startswith("frame unknown"), m
    cue_rows = [r for r in plan["cueMarkers"] if r["frame"] is None] if "cueMarkers" in plan else []
    for r in cue_rows:
        assert "no clip" not in r["reason"] or r["file"] not in {m["file"] for m in plan["markers"]}, r


# ---------------------------------------------------------------------------
# place_sfx — a spotted SFX pass onto one existing track (0.7.0)
# ---------------------------------------------------------------------------

import shutil  # noqa: E402
import tempfile  # noqa: E402
import types  # noqa: E402

import sfx  # noqa: E402


def _sfx_world(files, pooled=(), start=0, end=6000, fps=60, lane=(), export_bytes=None, pool_cls=None,
               current="YT", track_names=("SFX", "SFX 3")):
    """A Resolve holding timeline YT (current) with a picture on V1, a busy SFX
    lane on A2 and the target `SFX 3` on A3 (a plain track: the TAIL collision
    mode is on). `files` = {basename: (frames, item fps)} written as empty files
    in a temp dir; `pooled` are already in the SFX bin, the rest import."""
    tmp = tempfile.mkdtemp(prefix="sfx-test-")
    paths = {}
    for n in files:
        paths[n] = os.path.join(tmp, n)
        open(paths[n], "w").close()
    sfx_bin = FakeFolder("SFX", [FakeClip(n, files[n][0], fps=files[n][1], path=paths[n]) for n in pooled])
    vo_bin = FakeFolder("VO", [FakeClip("vo.wav", 500, fps=60, path="/elsewhere/vo.wav")])
    root = FakeFolder("Master", subs=[vo_bin, sfx_bin])
    disk = {paths[n]: files[n] for n in files}
    cls = pool_cls or FakeMediaPool
    project = FakeProject(lambda p: cls(p, root, disk), fps=str(fps), name="DV COLOR MANG")
    tl = FakeTimeline(name="YT", start=start, v1=[FakeItem("pic.mp4", start, end)], audio_tracks=3, fps=fps,
                      end=start + end, tail_tracks=(3,), export_bytes=export_bytes)
    for i, n in enumerate(track_names, start=2):
        tl.SetTrackName("audio", i, n)
    tl._tracks[("audio", 2)] = [FakeItem("swoosh.wav", start + 100, 30)]
    tl._tracks[("audio", 3)] = [FakeItem(n, start + s, d, lo) for n, s, d, lo in lane]
    # the other timeline has the same tracks by name: only the current-timeline
    # check stands between a plan and the wrong edit
    other = FakeTimeline(name="YT Shorts", start=start, v1=[FakeItem("short.mp4", start, end)], audio_tracks=3,
                         fps=fps, end=start + end, tail_tracks=(3,))
    for i, n in enumerate(track_names, start=2):
        other.SetTrackName("audio", i, n)
    project._timelines.extend([tl, other])
    project._current = tl if current == "YT" else other

    class PM:
        def GetCurrentProject(self):
            return project

    class Resolve:
        EXPORT_OTIO, EXPORT_NONE, EXPORT_EDL = _EXPORT_OTIO, _EXPORT_NONE, _EXPORT_EDL

        def GetProjectManager(self):
            return PM()

    orig = rapi.connect
    rapi.connect = lambda: Resolve()

    def restore():
        rapi.connect = orig
        os.chmod(tmp, 0o700)
        shutil.rmtree(tmp, ignore_errors=True)
    return types.SimpleNamespace(project=project, tl=tl, other=other, pool=project.GetMediaPool(), bin=sfx_bin,
                                 paths=paths, tmp=tmp, restore=restore, resolve=Resolve())


def _plan(w, rows, **over):
    doc = {"schema": "oside-sfx-plan/v1", "timeline": "YT", "track": "SFX 3", "bin": "SFX",
           "placements": [{"file": w.paths[n], "sourceIn": a, "sourceOut": b, "record": r, "label": f"cue {i + 1}"}
                          for i, (n, a, b, r) in enumerate(rows)]}
    doc.update(over)
    path = os.path.join(w.tmp, f"plan-{len(os.listdir(w.tmp))}.json")
    with open(path, "w") as f:
        json.dump(doc, f)
    return path


def _lane(tl, idx=3):
    return [(x.GetName(), x.GetStart() - tl.GetStartFrame(), x.GetDuration(), int(x.GetLeftOffset()))
            for x in sorted(tl.GetItemListInTrack("audio", idx), key=lambda x: x.GetStart())]


SFX_FILES = {"pop.wav": (13, 60), "check.wav": (98, 60), "hit.wav": (227, 25)}
SFX_ROWS = [("pop.wav", 0, 13 / 60, 212), ("check.wav", 0, 82 / 60, 609), ("hit.wav", 0.2, 4.76, 1200)]


def test_sfx_plan_refuses_each_bad_row_with_a_sentence():
    w = _sfx_world(SFX_FILES)
    try:
        good = _plan(w, SFX_ROWS)
        assert len(sfx.load_plan(good)["placements"]) == 3
        bad = [
            ({"schema": "oside-sfx-plan/v0"}, "schema"),
            ({"track": ""}, "'track'"),
            ({"placements": []}, "no placements"),
            ({"placements": [{"file": "pop.wav", "sourceIn": 0, "sourceOut": 1, "record": 0}]}, "absolute"),
            ({"placements": [{"file": "/nope/x.wav", "sourceIn": 0, "sourceOut": 1, "record": 0}]}, "does not exist"),
            ({"placements": [{"file": w.paths["pop.wav"], "sourceIn": 1, "sourceOut": 1, "record": 0}]}, "sourceIn < sourceOut"),
            ({"placements": [{"file": w.paths["pop.wav"], "sourceIn": 0, "sourceOut": 1, "record": 1.5}]}, "whole timeline frame"),
            ({"placements": [{"file": w.paths["pop.wav"], "sourceIn": 0, "sourceOut": 1, "record": True}]}, "whole timeline frame"),
            ({"placements": [{"file": w.paths["pop.wav"], "sourceIn": 0, "sourceOut": 1, "record": -1}]}, "whole timeline frame"),
            ({"placements": [{"file": w.paths["pop.wav"], "sourceIn": "a", "sourceOut": 1, "record": 0}]}, "numbers of seconds"),
        ]
        for over, needle in bad:
            try:
                sfx.load_plan(_plan(w, SFX_ROWS, **over))
            except sfx.PlanError as e:
                assert needle in str(e), (needle, str(e))
            else:
                raise AssertionError(f"accepted a plan with {over}")
    finally:
        w.restore()


def test_sfx_trim_floors_through_float_noise():
    """A plan written from frames carries 123/60 s. A bare floor reads that
    back as 122.99999999999999 and lays the clip a frame early — the naive
    form must fail on this very input, or the test proves nothing."""
    assert int(123 / 60 * 60) == 122                      # the trap is real on this input
    assert sfx.item_trim(123 / 60, 245 / 60, 60) == (123, 245)
    assert sfx.item_trim(29 / 25, 114 / 25, 25) == (29, 114)
    assert sfx.item_trim(0.2, 4.76, 25) == (5, 119)


def test_sfx_expected_lay_conforms_by_duration():
    # same rate: exact, no tolerance
    assert sfx.expected_lay(124, 148, 60, 60) == {"length": 24, "leftOffset": 124, "tolerance": 0}
    assert sfx.expected_lay(0, 24, 24, 23.976) == {"length": 24, "leftOffset": 0, "tolerance": 0}
    assert sfx.expected_lay(0, 60, 59.94, 60)["tolerance"] == 1      # 0.06 apart is two rates
    # the measured 2026-10-25 cell: a 25 fps item, 5..119, on 60 → 273 frames, offset 12
    assert sfx.expected_lay(5, 119, 25, 60) == {"length": 273, "leftOffset": 12, "tolerance": 1}


def test_sfx_check_spans_judges_from_one_read():
    def row(n, name, record, length):
        return {"row": n, "name": name, "file": f"/lib/{name}", "record": record, "length": length,
                "leftOffset": 0, "tolerance": 0}
    rows = [row(1, "a.wav", 100, 10),
            row(2, "b.wav", 110, 10),     # back to back: end is exclusive
            row(3, "c.wav", 200, 5)]
    clear = sfx.check_spans(rows, [], 1000)
    assert not clear["collisions"] and [r["row"] for r in clear["toPlace"]] == [1, 2, 3]
    # identical clip already there: a no-op, not a collision
    again = sfx.check_spans(rows, [{"name": "c.wav", "path": "/lib/c.wav", "start": 200, "duration": 5,
                                    "leftOffset": 0}], 1000)
    assert [r["row"] for r in again["alreadyPlaced"]] == [3] and not again["collisions"]
    # same frame, different clip: a collision, and it names what is in the way
    hit = sfx.check_spans(rows, [{"name": "z.wav", "start": 115, "duration": 50}], 1000)
    assert [c["row"] for c in hit["collisions"]] == [2] and "'z.wav'" in hit["collisions"][0]["reason"]
    # rows that overlap EACH OTHER
    inner = sfx.check_spans(rows + [row(4, "d.wav", 105, 2)], [], 1000)
    assert [c["row"] for c in inner["collisions"]] == [4] or {c["row"] for c in inner["collisions"]} == {2, 4}
    # running past the timeline end would lengthen the timeline
    late = sfx.check_spans(rows, [], 204)
    assert [c["row"] for c in late["collisions"]] == [3] and "past the timeline end" in late["collisions"][0]["reason"]
    assert not sfx.check_spans(rows, [], 205)["collisions"]


def test_sfx_otio_diff_goes_red_on_each_kind_of_change():
    w = _sfx_world(SFX_FILES)
    try:
        def snap():
            path = os.path.join(w.tmp, f"s{len(os.listdir(w.tmp))}.otio")
            assert w.tl.Export(path, _EXPORT_OTIO, _EXPORT_NONE)
            with open(path) as f:
                return json.load(f)
        before = snap()
        w.tl._tracks[("audio", 3)].append(FakeItem("pop.wav", 212, 13))
        ok = sfx.otio_diff(before, snap(), "Audio", 3, [("pop.wav", 212, 13)])
        assert ok["pass"], ok
        # the laid clip not declared → the target is not before + placed
        assert not sfx.otio_diff(before, snap(), "Audio", 3, [])["pass"]
        # something moved on ANOTHER track
        w.tl._tracks[("audio", 2)].append(FakeItem("stray.wav", 900, 10))
        bad = sfx.otio_diff(before, snap(), "Audio", 3, [("pop.wav", 212, 13)])
        row = {c["check"]: c for c in bad["checks"]}["otherTracksIdentical"]
        assert not bad["pass"] and not row["pass"] and row["changed"] == ["Audio 2 'SFX'"], row
        w.tl._tracks[("audio", 2)].pop()
        # a track renamed
        w.tl.SetTrackName("audio", 3, "SFX 3b")
        assert not {c["check"]: c for c in sfx.otio_diff(before, snap(), "Audio", 3, [("pop.wav", 212, 13)])["checks"]}[
            "targetTrackSettingsUnchanged"]["pass"]
        # and the clip view skips transitions and counts gaps as room
        track = {"children": [{"OTIO_SCHEMA": "Gap.1", "source_range": {"duration": {"value": 7}}},
                              {"OTIO_SCHEMA": "Clip.2", "name": "a", "source_range": {"duration": {"value": 10}}},
                              {"OTIO_SCHEMA": "Transition.1", "in_offset": {"value": 5}},
                              {"OTIO_SCHEMA": "Clip.2", "name": "b", "source_range": {"duration": {"value": 4}}}]}
        assert sfx.otio_clips(track) == [("a", 7, 10), ("b", 17, 4)]
    finally:
        w.restore()


def test_place_sfx_lays_reads_back_and_proves_nothing_else_moved():
    """The whole chain on a 60 fps timeline: one file already in the bin, two
    imported; one of those the pool tags 25 fps, so its trim conforms. Each
    clip lands at its frame with its length AND its trim, the bin the editor
    had open is still open, and the export diff passes."""
    w = _sfx_world(SFX_FILES, pooled=("pop.wav",), start=0)
    try:
        before_v1 = _lane(w.tl, 2)
        w.pool.SetCurrentFolder(w.pool.GetRootFolder())
        r = server.place_sfx(_plan(w, SFX_ROWS))
        assert r["ok"] and r["outcome"] == "placed", r
        assert _lane(w.tl) == [("pop.wav", 212, 13, 0), ("check.wav", 609, 82, 0), ("hit.wav", 1200, 273, 12)]
        assert sorted(os.path.basename(p) for p in r["imported"]) == ["check.wav", "hit.wav"]
        assert [p["laidLength"] for p in r["placed"]] == [13, 82, 273]
        assert r["diff"]["pass"] and {c["check"] for c in r["diff"]["checks"]} >= {
            "otherTracksIdentical", "targetTrackIsBeforePlusPlaced", "timelineEndUnchanged"}
        assert _lane(w.tl, 2) == before_v1
        assert w.pool.GetCurrentFolder() is w.pool.GetRootFolder()       # the editor's bin, restored
        assert r["saved"] is False
    finally:
        w.restore()


def test_place_sfx_frames_are_relative_to_the_timeline_start():
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), start=86400)
    try:
        r = server.place_sfx(_plan(w, SFX_ROWS[:1]))
        assert r["ok"], r
        assert [x.GetStart() for x in w.tl.GetItemListInTrack("audio", 3)] == [86400 + 212]
    finally:
        w.restore()


def test_place_sfx_rerun_is_a_no_op():
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES))
    try:
        plan = _plan(w, SFX_ROWS)
        assert server.place_sfx(plan)["outcome"] == "placed"
        lane = _lane(w.tl)
        again = server.place_sfx(plan)
        assert again["ok"] and again["outcome"] == "nothing-to-do" and len(again["alreadyPlaced"]) == 3, again
        assert _lane(w.tl) == lane
        # and the post-save confirmation the docstring asks for reads the same
        dry = server.place_sfx(plan, dry_run=True)
        assert dry["ok"] and len(dry["alreadyPlaced"]) == 3 and dry["toPlace"] == []
    finally:
        w.restore()


def test_place_sfx_will_not_relay_what_an_editor_deleted():
    """The 2026-10-25 case: a pass was laid, the editor deleted the cues that
    did not work, and the same plan run again would have put them all back.
    A partly placed plan is refused (and flagged on the dry run); resume=True
    is the explicit way to finish a run that stopped. The ledger is moved
    aside here, so the track alone has to tell."""
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES))
    try:
        plan = _plan(w, SFX_ROWS)
        assert server.place_sfx(plan)["outcome"] == "placed"
        os.remove(plan + ".placed.json")
        lane = w.tl._tracks[("audio", 3)]
        lane.remove(next(x for x in lane if x.GetStart() == 609))          # the editor's cut
        kept = _lane(w.tl)
        dry = server.place_sfx(plan, dry_run=True)
        assert dry["ok"] and dry["partial"] is True and [t["record"] for t in dry["toPlace"]] == [609], dry
        r = server.place_sfx(plan)
        assert not r["ok"] and r["outcome"] == "refused" and "removed on purpose" in r["failure"]["reason"], r
        assert "2 of 3 rows are already on the track" in r["failure"]["reason"], r
        assert _lane(w.tl) == kept
        done = server.place_sfx(plan, resume=True)
        assert done["ok"] and [p["record"] for p in done["placed"]] == [609], done
    finally:
        w.restore()


def test_place_sfx_refuses_a_collision_before_writing_anything():
    """Row 2 lands on a clip already on the track. The whole plan is refused:
    nothing imported, nothing appended, nothing exported — not rows 1 and 3
    with a complaint about 2."""
    w = _sfx_world(SFX_FILES, lane=[("theirs.wav", 600, 40, 0)])
    try:
        exports = []
        real_export = w.tl.Export
        w.tl.Export = lambda *a: exports.append(a) or real_export(*a)
        r = server.place_sfx(_plan(w, SFX_ROWS))
        assert not r["ok"] and r["outcome"] == "refused", r
        assert [c["row"] for c in r["collisions"]] == [2] and "'theirs.wav'" in r["collisions"][0]["reason"]
        assert _lane(w.tl) == [("theirs.wav", 600, 40, 0)]
        assert w.pool.imports == [] and exports == []
    finally:
        w.restore()


def test_place_sfx_refuses_a_trim_past_the_end_of_the_file():
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES))
    try:
        r = server.place_sfx(_plan(w, [("pop.wav", 0, 1.0, 212)]))     # pop is 13 frames long
        assert r["outcome"] == "refused" and "past the clip's 13 frames" in r["collisions"][0]["reason"], r
        assert _lane(w.tl) == []
    finally:
        w.restore()


def test_place_sfx_refuses_the_wrong_timeline_track_or_bin():
    for kw, needle in ((dict(current="YT Shorts"), "current timeline is 'YT Shorts'"),
                       (dict(track_names=("SFX 3", "SFX 3")), "found 2"),
                       (dict(track_names=("SFX", "FX")), "found 0")):
        w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), **kw)
        try:
            r = server.place_sfx(_plan(w, SFX_ROWS))
            assert not r["ok"] and needle in r.get("error", ""), (kw, r)
            assert _lane(w.other) == [] and _lane(w.tl) == [], (kw, r)
        finally:
            w.restore()
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES))
    try:
        r = server.place_sfx(_plan(w, SFX_ROWS, bin="SOUNDS"))
        assert not r["ok"] and "No bin named 'SOUNDS'" in r["error"], r
        assert _lane(w.tl) == []
    finally:
        w.restore()


def test_place_sfx_dry_run_touches_nothing():
    w = _sfx_world(SFX_FILES, pooled=("pop.wav",))
    try:
        r = server.place_sfx(_plan(w, SFX_ROWS), dry_run=True)
        assert r["ok"] and r["outcome"] == "dry-run" and len(r["toPlace"]) == 3, r
        assert sorted(os.path.basename(p) for p in r["wouldImport"]) == ["check.wav", "hit.wav"]
        assert [t["estimated"] for t in r["toPlace"]] == [False, True, True]
        assert w.pool.imports == [] and _lane(w.tl) == []
    finally:
        w.restore()


def test_place_sfx_stops_at_the_first_clip_that_lands_wrong():
    """Resolve lays the second clip a frame late (any of anti-pattern 2's
    modes looks like this from outside). The read-back must stop the run
    THERE — the third clip is never appended — name what landed, and the
    export diff must agree that the track is not before + placed."""
    class Shifty(FakeMediaPool):
        def AppendToTimeline(self, items):
            if items[0]["recordFrame"] == 609:
                items = [dict(items[0], recordFrame=610)]
            return super().AppendToTimeline(items)
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), pool_cls=Shifty)
    try:
        r = server.place_sfx(_plan(w, SFX_ROWS))
        assert not r["ok"] and r["outcome"] == "failed", r
        assert [p["record"] for p in r["placed"]] == [212]
        assert r["failure"]["row"] == 2 and r["failure"]["landed"][0]["start"] == 610
        assert [x[1] for x in _lane(w.tl)] == [212, 610]                # row 3 never went in
        assert not {c["check"]: c for c in r["diff"]["checks"]}["targetTrackIsBeforePlusPlaced"]["pass"]
    finally:
        w.restore()


def test_place_sfx_catches_the_plain_track_tail_mode():
    """The measured 21.1 lie, end to end, on the measured cell (2026-09-08:
    asking frame 30 of a track held 0..75 landed at 75, 30 frames long). The
    occupant arrives after every pre-write read (an editor working while the
    tool runs), the append lands at the TAIL truncated, and the read-back calls
    it — never a placement."""
    class Late(FakeMediaPool):
        def AppendToTimeline(self, items):
            self._project.GetCurrentTimeline()._tracks[("audio", 3)].append(FakeItem("hidden.wav", 0, 75))
            return super().AppendToTimeline(items)
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), pool_cls=Late)
    try:
        r = server.place_sfx(_plan(w, [("check.wav", 0, 82 / 60, 30)]))
        assert not r["ok"] and r["outcome"] == "failed", r
        assert r["placed"] == [] and ("check.wav", 75, 30) in [
            (x["name"], x["start"], x["duration"]) for x in r["failure"]["landed"]], r["failure"]
    finally:
        w.restore()


def test_place_sfx_catches_a_clip_laid_with_the_wrong_trim():
    """Right frame, right length, wrong SOUND: the slice taken from the head of
    the file instead of from its in-point. Only the left offset tells them
    apart, so the read-back must compare it."""
    class Untrimmed(FakeMediaPool):
        def AppendToTimeline(self, items):
            it = items[0]
            return super().AppendToTimeline([dict(it, startFrame=0, endFrame=it["endFrame"] - it["startFrame"])])
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), pool_cls=Untrimmed)
    try:
        r = server.place_sfx(_plan(w, [("check.wav", 0.5, 1.0, 609)]))
        assert not r["ok"] and r["outcome"] == "failed", r
        assert r["failure"]["landed"] == [{"name": "check.wav", "start": 609, "duration": 30, "leftOffset": 0,
                                           "path": w.paths["check.wav"]}]
    finally:
        w.restore()


def test_place_sfx_catches_a_silent_drop():
    class Deaf(FakeMediaPool):
        def AppendToTimeline(self, items):
            return [_RemoteObject()]
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), pool_cls=Deaf)
    try:
        r = server.place_sfx(_plan(w, SFX_ROWS))
        assert not r["ok"] and r["failure"]["reason"] == "nothing new on the track" and r["placed"] == [], r
    finally:
        w.restore()


def test_place_sfx_diff_catches_a_change_on_another_track():
    """A write that also touched a track it was never aimed at. The per-clip
    read-back of the target cannot see it; the export diff must."""
    class Leaky(FakeMediaPool):
        def AppendToTimeline(self, items):
            out = super().AppendToTimeline(items)
            self._project.GetCurrentTimeline()._tracks[("audio", 2)].append(FakeItem("leak.wav", 3000, 5))
            return out
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), pool_cls=Leaky)
    try:
        r = server.place_sfx(_plan(w, SFX_ROWS[:1]))
        assert not r["ok"] and r["outcome"] == "failed", r
        row = {c["check"]: c for c in r["diff"]["checks"]}["otherTracksIdentical"]
        assert not row["pass"] and row["changed"] == ["Audio 2 'SFX'"]
    finally:
        w.restore()


def test_place_sfx_refuses_on_an_empty_snapshot():
    """Export answered True over a zero-byte file: no before-state to judge
    against, so nothing is laid."""
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), export_bytes=0)
    try:
        r = server.place_sfx(_plan(w, SFX_ROWS))
        assert not r["ok"] and r["outcome"] == "refused" and "0 bytes" in r["failure"]["reason"], r
        assert _lane(w.tl) == []
    finally:
        w.restore()


def test_sfx_check_spans_will_not_vouch_for_a_different_sound():
    """[Codex r1, 2026-10-25] `alreadyPlaced` once matched name, frame and
    length only: a clip with the wrong trim or from another library read as
    this row, and one item vouched for any number of rows."""
    r = {"row": 1, "name": "c.wav", "file": "/lib/c.wav", "record": 200, "length": 5, "leftOffset": 7,
         "tolerance": 0}
    item = {"name": "c.wav", "path": "/lib/c.wav", "start": 200, "duration": 5, "leftOffset": 7}
    assert [x["row"] for x in sfx.check_spans([r], [item], 1000)["alreadyPlaced"]] == [1]
    for change, needle in (({"leftOffset": 0}, "different trim"), ({"path": "/other/c.wav"}, "overlaps"),
                           ({"path": None}, "overlaps"), ({"leftOffset": None}, "different trim")):
        v = sfx.check_spans([r], [dict(item, **change)], 1000)
        assert not v["alreadyPlaced"] and [c["row"] for c in v["collisions"]] == [1], change
        assert needle in v["collisions"][0]["reason"], (change, v["collisions"])
    # within a conformed row's tolerance it IS the row — what the read-back accepted, a re-run accepts
    soft = dict(r, tolerance=1)
    assert sfx.check_spans([soft], [dict(item, duration=6, leftOffset=8)], 1000)["alreadyPlaced"]
    assert not sfx.check_spans([soft], [dict(item, duration=7)], 1000)["alreadyPlaced"]
    # one item vouches for ONE row
    twice = sfx.check_spans([r, dict(r, row=2)], [item], 1000)
    assert [x["row"] for x in twice["alreadyPlaced"]] == [1] and [c["row"] for c in twice["collisions"]] == [2]


def test_sfx_a_trim_shorter_than_a_frame_is_refused_not_rounded_up():
    assert sfx.expected_lay(0, 0, 60, 60)["length"] == 0
    assert sfx.expected_lay(0, 1, 60, 24)["length"] == 0          # 1/60 s is 0.4 of a 24 fps frame
    v = sfx.check_spans([{"row": 1, "name": "a", "file": "/a", "record": 10, "length": 0, "leftOffset": 0,
                          "tolerance": 1}], [], 1000)
    assert [c["row"] for c in v["collisions"]] == [1] and "shorter than one frame" in v["collisions"][0]["reason"]
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES))
    try:
        r = server.place_sfx(_plan(w, [("pop.wav", 0.001, 0.002, 212)]))
        assert r["outcome"] == "refused" and any("empty trim" in c["reason"] for c in r["collisions"]), r
        assert _lane(w.tl) == []
    finally:
        w.restore()


def test_sfx_otio_diff_sees_a_changed_clip_on_the_target_track():
    """[Codex r1] The target was compared as (name, start, length): a clip
    already there whose trim, media or neighbour transition changed passed."""
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), lane=[("theirs.wav", 600, 40, 3)])
    try:
        def snap():
            path = os.path.join(w.tmp, f"s{len(os.listdir(w.tmp))}.otio")
            assert w.tl.Export(path, _EXPORT_OTIO, _EXPORT_NONE)
            with open(path) as f:
                return json.load(f)
        before = snap()
        assert sfx.otio_diff(before, snap(), "Audio", 3, [])["pass"]
        w.tl._tracks[("audio", 3)][0]._left = 9                        # same name, frame, length — new trim
        d = sfx.otio_diff(before, snap(), "Audio", 3, [])
        row = {c["check"]: c for c in d["checks"]}["targetTrackIsBeforePlusPlaced"]
        assert not d["pass"] and row["changedOrMissing"] == [(600, "theirs.wav")], row
        w.tl._tracks[("audio", 3)][0]._left = 3
        # a transition on the target that is gone afterwards
        with_t = json.loads(json.dumps(before))
        kids = sfx.find_track(with_t, "Audio", 3)["children"]
        kids.insert(2, {"OTIO_SCHEMA": "Transition.1", "name": "Cross Fade", "in_offset": {"value": 5},
                        "out_offset": {"value": 5}})
        d = sfx.otio_diff(with_t, before, "Audio", 3, [])
        assert not {c["check"]: c for c in d["checks"]}["targetTrackIsBeforePlusPlaced"]["pass"]
        # and a transition that APPEARS is not a laid clip either
        d = sfx.otio_diff(before, with_t, "Audio", 3, [])
        assert {c["check"]: c for c in d["checks"]}["targetTrackIsBeforePlusPlaced"]["unexpected"]
    finally:
        w.restore()


def test_place_sfx_rerun_holds_on_a_conformed_lay():
    """[Codex r1] A conformed row is accepted on placement within +/-1 frame;
    the re-run used to demand an exact match, so a lay one frame long read as
    a collision with itself."""
    class Long(FakeMediaPool):
        def AppendToTimeline(self, items):
            out = super().AppendToTimeline(items)
            lane = self._project.GetCurrentTimeline()._tracks[("audio", 3)]
            new = next(x for x in lane if x.GetStart() == items[0]["recordFrame"])
            new._d += 1
            return out
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), pool_cls=Long)
    try:
        plan = _plan(w, [SFX_ROWS[2]])                                  # hit.wav: 25 fps on 60 → 273
        r = server.place_sfx(plan)
        assert r["ok"] and r["placed"][0]["laidLength"] == 274, r
        again = server.place_sfx(plan)
        assert again["ok"] and again["outcome"] == "nothing-to-do" and not again["collisions"], again
    finally:
        w.restore()


def test_place_sfx_will_not_relay_a_pass_deleted_whole():
    """[Codex r1] Delete EVERY clip a plan laid and nothing on the track says
    it was ever run — the partial check passes and the whole pass comes back.
    The ledger beside the plan remembers; resume=True or moving the ledger
    aside are the two deliberate ways past it."""
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES))
    try:
        plan = _plan(w, SFX_ROWS)
        first = server.place_sfx(plan)
        assert first["outcome"] == "placed" and first["ledgerWritten"] is True, first
        with open(plan + ".placed.json") as f:
            ledger = json.load(f)
        entry = ledger["entries"][0]
        assert ledger["schema"] == "oside-sfx-ledger/v1" and (entry["project"], entry["timeline"], entry["track"]) == (
            "DV COLOR MANG", "YT", "SFX 3")
        assert [p["record"] for p in entry["runs"][0]["placed"]] == [212, 609, 1200]
        w.tl._tracks[("audio", 3)].clear()                            # the editor's verdict on the pass
        dry = server.place_sfx(plan, dry_run=True)
        assert dry["ok"] and dry["laidBefore"] == entry["placedAt"] and dry["partial"] is False, dry
        r = server.place_sfx(plan)
        assert not r["ok"] and r["outcome"] == "refused" and "was laid on" in r["failure"]["reason"], r
        assert _lane(w.tl) == []
        done = server.place_sfx(plan, resume=True)
        assert done["ok"] and len(done["placed"]) == 3, done
        with open(plan + ".placed.json") as f:
            assert len(json.load(f)["entries"][0]["runs"]) == 2
        # the ledger is per project/timeline/track: a plan run elsewhere starts clean
        assert sfx.ledger_entry(json.load(open(plan + ".placed.json")), "OTHER", "YT", "SFX 3") is None
        w.tl._tracks[("audio", 3)].clear()
        os.remove(plan + ".placed.json")                              # moved aside on purpose: a new plan
        assert server.place_sfx(plan)["outcome"] == "placed"
    finally:
        w.restore()


def test_place_sfx_refuses_on_a_ledger_it_cannot_read_and_reports_one_it_cannot_write():
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES))
    try:
        plan = _plan(w, SFX_ROWS[:1])
        with open(plan + ".placed.json", "w") as f:
            f.write("{not json")
        r = server.place_sfx(plan)
        assert not r["ok"] and "does not read" in r["error"] and _lane(w.tl) == [], r
        os.remove(plan + ".placed.json")
        os.chmod(w.tmp, 0o500)                                        # the plan's folder will not take a file
        r = server.place_sfx(plan)
        os.chmod(w.tmp, 0o700)
        assert r["ok"] and r["outcome"] == "placed" and r["ledgerWritten"] is False and r["ledgerError"], r
    finally:
        w.restore()


def test_place_sfx_says_so_when_it_refuses_after_importing():
    """[Codex r1] The docs said a refused plan writes nothing. A file not in
    the pool cannot be judged exactly until it is imported — here its trim
    runs past its end, which only the pooled item can tell — so a refusal
    after the import HAS written to the bin, and must say so."""
    w = _sfx_world(SFX_FILES)
    try:
        r = server.place_sfx(_plan(w, [("pop.wav", 0, 1.0, 212)]))      # pop is 13 frames
        assert not r["ok"] and r["outcome"] == "refused", r
        assert r["imported"] == [w.paths["pop.wav"]] and "AFTER importing 1 file" in r["failure"]["reason"], r
        assert "past the clip's 13 frames" in r["collisions"][0]["reason"]
        assert _lane(w.tl) == []
    finally:
        w.restore()


def test_place_sfx_puts_the_editors_bin_back_or_says_so():
    """[Codex r1] The bin restore was skipped by an exception and its answer
    ignored. Three cells: the import raises (restored anyway), the restore is
    refused (reported, nothing laid), the open bin cannot be read (refused
    before any import)."""
    class Raises(FakeMediaPool):
        def ImportMedia(self, paths):
            raise RuntimeError("disk went away")

    class Stuck(FakeMediaPool):
        def SetCurrentFolder(self, folder):
            if folder is self._root:
                return False
            return super().SetCurrentFolder(folder)

    class Unread(FakeMediaPool):
        def GetCurrentFolder(self):
            return None
    w = _sfx_world(SFX_FILES, pool_cls=Raises)
    try:
        r = server.place_sfx(_plan(w, SFX_ROWS[:1]))
        assert not r["ok"] and "disk went away" in r["error"], r
        assert w.pool.GetCurrentFolder() is w.pool.GetRootFolder() and _lane(w.tl) == []
    finally:
        w.restore()
    w = _sfx_world(SFX_FILES, pool_cls=Stuck)
    try:
        r = server.place_sfx(_plan(w, SFX_ROWS[:1]))
        assert not r["ok"] and r["outcome"] == "failed" and r["binRestored"] is False, r
        assert "could not reselect" in r["failure"]["reason"] and r["imported"] and _lane(w.tl) == []
    finally:
        w.restore()
    w = _sfx_world(SFX_FILES, pool_cls=Unread)
    try:
        r = server.place_sfx(_plan(w, SFX_ROWS[:1]))
        assert not r["ok"] and r["outcome"] == "refused" and "which bin is open" in r["failure"]["reason"], r
        assert w.pool.imports == [] and _lane(w.tl) == []
    finally:
        w.restore()


def test_place_sfx_fails_closed_on_a_track_it_cannot_read():
    """[Codex r1] A track that did not answer read as an EMPTY track, and an
    item without a frame was skipped: both judge collisions blind."""
    for broken in ("none", "item"):
        w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), lane=[("theirs.wav", 600, 40, 0)])
        try:
            real = w.tl.GetItemListInTrack
            if broken == "none":
                w.tl.GetItemListInTrack = lambda kind, idx: None if (kind, idx) == ("audio", 3) else real(kind, idx)
            else:
                w.tl._tracks[("audio", 3)][0].GetStart = lambda: None
            r = server.place_sfx(_plan(w, SFX_ROWS))
            assert not r["ok"] and "refusing to judge collisions blind" in r["error"], (broken, r)
            assert len(w.tl._tracks[("audio", 3)]) == 1 and w.pool.imports == []
        finally:
            w.restore()


def test_place_sfx_read_back_needs_the_file_and_the_trim_it_can_read():
    """[Codex r1] An unreadable left offset passed the read-back, and the file
    was never checked: a namesake from another library certified the row."""
    class NoOffset(FakeMediaPool):
        def AppendToTimeline(self, items):
            out = super().AppendToTimeline(items)
            out[0].GetLeftOffset = lambda *a: None
            return out

    class Namesake(FakeMediaPool):
        def AppendToTimeline(self, items):
            decoy = FakeClip("check.wav", 98, fps=60, path="/other/library/check.wav")
            return super().AppendToTimeline([dict(items[0], mediaPoolItem=decoy)])
    for cls in (NoOffset, Namesake):
        w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), pool_cls=cls)
        try:
            r = server.place_sfx(_plan(w, [SFX_ROWS[1]]))
            assert not r["ok"] and r["outcome"] == "failed" and r["placed"] == [], (cls.__name__, r)
            assert "landed somewhere other" in r["failure"]["reason"], (cls.__name__, r["failure"])
        finally:
            w.restore()


def test_place_sfx_read_back_sees_a_neighbour_change_under_the_append():
    class Nudge(FakeMediaPool):
        def AppendToTimeline(self, items):
            out = super().AppendToTimeline(items)
            theirs = next(x for x in self._project.GetCurrentTimeline()._tracks[("audio", 3)]
                          if x.GetName() == "theirs.wav")
            theirs._left += 2
            return out
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), pool_cls=Nudge, lane=[("theirs.wav", 3000, 40, 0)])
    try:
        r = server.place_sfx(_plan(w, SFX_ROWS[:1]))
        assert not r["ok"] and r["failure"]["reason"] == "an item already on the track changed under the append", r
        assert not {c["check"]: c for c in r["diff"]["checks"]}["targetTrackIsBeforePlusPlaced"]["pass"]
    finally:
        w.restore()


def test_place_sfx_refuses_when_the_track_and_its_export_disagree():
    """The item list judged the collisions; the export is what the diff will
    compare against. An occupant only one of them shows means neither can be
    trusted, so nothing is laid."""
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES))
    try:
        hidden = FakeItem("hidden.wav", 0, 75)
        w.tl._tracks[("audio", 3)].append(hidden)
        real = w.tl.GetItemListInTrack
        w.tl.GetItemListInTrack = lambda kind, idx: [x for x in real(kind, idx) if x is not hidden]
        r = server.place_sfx(_plan(w, [("check.wav", 0, 82 / 60, 30)]))
        assert not r["ok"] and r["outcome"] == "refused" and "disagree" in r["failure"]["reason"], r
        assert real("audio", 3) == [hidden]
    finally:
        w.restore()


def test_place_sfx_timeline_end_check_can_go_red():
    """[Codex r1] The fake's end was fixed, so timelineEndUnchanged could never
    fail. A write that also lays something past the end must trip it."""
    class Stretch(FakeMediaPool):
        def AppendToTimeline(self, items):
            out = super().AppendToTimeline(items)
            tl = self._project.GetCurrentTimeline()
            tl._tracks[("video", 1)].append(FakeItem("tail.mp4", tl._end, 50))
            return out
    w = _sfx_world(SFX_FILES, pooled=tuple(SFX_FILES), pool_cls=Stretch)
    try:
        r = server.place_sfx(_plan(w, SFX_ROWS[:1]))
        row = {c["check"]: c for c in r["diff"]["checks"]}["timelineEndUnchanged"]
        assert not r["ok"] and not row["pass"] and row["after"] == row["before"] + 50, r["diff"]
    finally:
        w.restore()


def test_export_timeline_otio_refuses_a_zero_byte_file():
    """[Codex r1] The redproof for this went red on wording alone; drive the
    export function itself."""
    w = _sfx_world(SFX_FILES, export_bytes=0)
    try:
        path = os.path.join(w.tmp, "empty.otio")
        try:
            rapi.export_timeline_otio(w.resolve, w.tl, path)
        except rapi.ResolveError as e:
            assert "0 bytes" in str(e), e
        else:
            raise AssertionError("a zero-byte export was accepted")
    finally:
        w.restore()


def test_items_by_path_matches_file_path_not_name():
    """Two libraries, one filename. The pool item in another bin with the same
    NAME but a different path must not be used for this file."""
    w = _sfx_world(SFX_FILES, pooled=())
    try:
        decoy = FakeClip("pop.wav", 13, fps=60, path="/other/library/pop.wav")
        w.pool.GetRootFolder()._subs[0]._clips.append(decoy)
        found = rapi.items_by_path(w.pool, {w.paths["pop.wav"]}, prefer=w.bin)
        assert found == {}
        r = server.place_sfx(_plan(w, SFX_ROWS[:1]))
        assert r["ok"] and r["imported"] == [w.paths["pop.wav"]], r
        assert w.tl.GetItemListInTrack("audio", 3)[0].GetMediaPoolItem() is not decoy
    finally:
        w.restore()


def test_snapshot_timeline_writes_a_new_file_and_never_overwrites():
    w = _sfx_world(SFX_FILES)
    try:
        out = os.path.join(w.tmp, "yt.otio")
        r = server.snapshot_timeline(out)
        assert r["ok"] and r["bytes"] > 0 and r["timeline"] == "YT" and r["timelineFps"] == 60.0, r
        assert [(t["kind"], t["index"], t["name"], t["clips"]) for t in r["tracks"]] == [
            ("Video", 1, "Video 1", 1), ("Audio", 1, "Audio 1", 0), ("Audio", 2, "SFX", 1), ("Audio", 3, "SFX 3", 0)]
        again = server.snapshot_timeline(out)
        assert not again["ok"] and "never overwrites" in again["error"]
        other = server.snapshot_timeline(os.path.join(w.tmp, "shorts.otio"), "YT Shorts")
        assert other["ok"] and other["timeline"] == "YT Shorts"
        assert not server.snapshot_timeline(os.path.join(w.tmp, "x.otio"), "NOPE")["ok"]
    finally:
        w.restore()


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    unknown = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Unknown as e:
            unknown += 1
            print(f"UNKNOWN  {name} — {e}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {name}")
            traceback.print_exc()
    passed = len(tests) - failed - unknown
    # never "N/N" over unknowns: the line says what ran, what could not, what broke
    print(f"\n{passed} passed, {unknown} unknown (not passes), {failed} failed — of {len(tests)}")
    sys.exit(1 if failed else 0)
