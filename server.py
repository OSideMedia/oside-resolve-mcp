# ============================================================================
# server.py — oside-resolve-mcp: the OSIDE studio → DaVinci Resolve bridge.
#
# A deliberately SMALL, deterministic tool surface (not general Resolve
# control — samuelgursky/davinci-resolve-mcp exists for that): consume an
# oside-davinci/v1 package (manifest.json written by the studio's
# "Export for DaVinci") and turn it into a Resolve project built from the
# right template, with media filed into its bins and a timeline in shot
# order carrying one marker per shot.
#
# stdio transport, local machine only. Guardrails: creates, never deletes;
# refuses to reuse an existing project or timeline name.
# ============================================================================

import json
import os
import re

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

import handoff
import resolve_api as rapi

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(HERE, "templates")
TEMPLATES_CONFIG = os.path.join(TEMPLATES_DIR, "templates.json")
PYPROJECT = os.path.join(HERE, "pyproject.toml")

mcp = FastMCP("oside-resolve")


def _version() -> str:
    """The package version, read off pyproject.toml so it lives in ONE place."""
    try:
        with open(PYPROJECT, encoding="utf-8") as f:
            m = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else "0.0.0"
    except OSError:
        return "0.0.0"


def capabilities() -> dict:
    """What this server understands — OSIDE compares against it before it
    relies on a feature (placements, cues, dry_run, verify_v2)."""
    return {"manifest": handoff.MANIFEST_FORMAT, "features": list(handoff.FEATURES), "version": _version()}


# Tool annotations (MCP spec hints). Every tool declares them; openWorldHint
# is False throughout — this server talks to one local Resolve, nothing else.
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
CREATES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
IDEMPOTENT_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
OVERWRITES_SNAPSHOT = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)


def _templates() -> dict:
    with open(TEMPLATES_CONFIG, encoding="utf-8") as f:
        return json.load(f)


def _load_manifest(manifest_path: str) -> tuple[dict, str]:
    if not os.path.isfile(manifest_path):
        raise rapi.ResolveError(f"Manifest not found: {manifest_path}")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    if manifest.get("format") != "oside-davinci/v1":
        raise rapi.ResolveError(f"Not an oside-davinci/v1 manifest: {manifest_path}")
    return manifest, os.path.dirname(os.path.abspath(manifest_path))


def _abs_files(base: str, entries: list[dict]) -> list[str]:
    paths = []
    for e in entries:
        p = os.path.join(base, e["file"])
        if not os.path.isfile(p):
            raise rapi.ResolveError(f"Package file missing on disk: {p}")
        paths.append(p)
    return paths


def _ok(**kw) -> dict:
    return {"ok": True, **kw}


def _err(e: Exception, **kw) -> dict:
    return {"ok": False, "error": str(e), **kw}


def _open_project(resolve):
    pm = rapi.project_manager(resolve)
    project = pm.GetCurrentProject()
    if project is None:
        raise rapi.ResolveError("No project is open — run create_project first.")
    return pm, project


def _bin_names(media_pool, entries: list, default: str) -> set | None:
    """Filenames present in the bin the entries name (empty when the bin does
    not exist yet — nothing imported)."""
    if not entries:
        return set()
    folder = rapi.find_bin(media_pool, entries[0].get("bin") or default)
    return set(rapi.clips_by_filename(folder).keys()) if folder else set()


@mcp.tool(annotations=READ_ONLY)
def resolve_status() -> dict:
    """Is Resolve reachable? Returns version, current project and its projects
    list, plus this server's `capabilities` block (manifest format, feature
    list, version) — returned even when Resolve is not reachable."""
    caps = capabilities()
    try:
        resolve = rapi.connect()
        pm = rapi.project_manager(resolve)
        current = pm.GetCurrentProject()
        return _ok(
            version=resolve.GetVersionString(),
            currentProject=current.GetName() if current else None,
            projects=rapi.project_names(pm),
            capabilities=caps,
        )
    except Exception as e:  # noqa: BLE001
        return _err(e, capabilities=caps)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def launch_resolve() -> dict:
    """Open DaVinci Resolve (if closed) and wait until scripting answers."""
    try:
        resolve = rapi.launch_and_connect()
        return _ok(version=resolve.GetVersionString())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=READ_ONLY)
def list_templates() -> dict:
    """The kind → Resolve-template mapping, and whether each .drp snapshot exists."""
    try:
        cfg = _templates()
        out = {}
        for kind, spec in cfg.items():
            drp = os.path.join(TEMPLATES_DIR, spec["drp"])
            out[kind] = {
                "resolveProject": spec["resolveProject"],
                "drp": drp,
                "snapshotExists": os.path.isfile(drp),
            }
        return _ok(templates=out)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=OVERWRITES_SNAPSHOT)
def export_template(kind: str) -> dict:
    """One-time setup (and re-run after template tweaks): snapshot the live
    Resolve template project for `kind` ('cinematic' | 'explainer') to
    templates/<kind>.drp. Reads the template, never modifies it."""
    try:
        spec = _templates().get(kind)
        if not spec:
            raise rapi.ResolveError(f"Unknown kind {kind!r} — see list_templates.")
        resolve = rapi.connect()
        pm = rapi.project_manager(resolve)
        drp = os.path.join(TEMPLATES_DIR, spec["drp"])
        rapi.export_project(pm, spec["resolveProject"], drp)
        return _ok(kind=kind, drp=drp, sizeBytes=os.path.getsize(drp))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=CREATES)
def create_project(kind: str, name: str) -> dict:
    """Create and open a NEW Resolve project named `name` from the `kind`
    template snapshot ('cinematic' = 23.976fps, 'explainer' = 60fps). All
    project settings and the bin tree come from the template. Refuses to
    reuse an existing project name."""
    try:
        spec = _templates().get(kind)
        if not spec:
            raise rapi.ResolveError(f"Unknown kind {kind!r} — see list_templates.")
        drp = os.path.join(TEMPLATES_DIR, spec["drp"])
        resolve = rapi.connect()
        pm = rapi.project_manager(resolve)
        project = rapi.create_from_template(pm, drp, name)
        fps = project.GetSetting("timelineFrameRate")
        return _ok(project=name, template=spec["resolveProject"], timelineFrameRate=fps)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=CREATES)
def import_package(manifest_path: str) -> dict:
    """Import an oside-davinci/v1 package into the CURRENTLY OPEN project:
    videos land in the manifest's video bin (VIDEOS), narration mp3s in the
    VO bin — bins are found anywhere in the tree, or created under the root.
    Run create_project first."""
    try:
        manifest, base = _load_manifest(manifest_path)
        resolve = rapi.connect()
        _pm, project = _open_project(resolve)
        media_pool = project.GetMediaPool()

        imported = {"videos": 0, "audio": 0}
        for key in ("videos", "audio"):
            entries = manifest.get(key) or []
            if not entries:
                continue
            bin_name = entries[0].get("bin") or ("VIDEOS" if key == "videos" else "VO")
            folder = rapi.ensure_bin(media_pool, bin_name)
            items = rapi.import_into_bin(media_pool, folder, _abs_files(base, entries))
            imported[key] = len(items)

        expected = {"videos": len(manifest.get("videos") or []), "audio": len(manifest.get("audio") or [])}
        return _ok(project=project.GetName(), imported=imported, expected=expected)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _plan(manifest: dict, base: str, timeline_name: str, cues_on: bool) -> tuple[dict, list, dict]:
    """The plan for this package: from the media pool when Resolve answers,
    from the manifest + disk alone when it does not. Returns (plan, cues, ctx)
    where ctx carries the live objects for the real build (empty offline)."""
    cues, cue_warning = handoff.load_cues(manifest, base) if cues_on else ([], None)
    ctx: dict = {}
    try:
        resolve = rapi.connect()
        _pm, project = _open_project(resolve)
        media_pool = project.GetMediaPool()
        videos = manifest.get("videos") or []
        audio = manifest.get("audio") or []
        video_bin = rapi.find_bin(media_pool, (videos[0].get("bin") if videos else None) or "VIDEOS")
        vo_bin = rapi.find_bin(media_pool, (audio[0].get("bin") if audio else None) or "VO")
        by_name = {
            "videos": rapi.clips_by_filename(video_bin) if video_bin else {},
            "audio": rapi.clips_by_filename(vo_bin) if vo_bin else {},
        }
        try:
            fps = float(project.GetSetting("timelineFrameRate"))
            fps_source = "resolve"
        except (TypeError, ValueError):
            fps, fps_source = handoff.kind_fps(manifest), "kind-default"
        ctx = {"resolve": resolve, "project": project, "media_pool": media_pool, "by_name": by_name}

        def bin_lookup(kind, fname):
            return fname in by_name[kind]

        def duration_lookup(_v, abs_path):
            item = by_name["videos"].get(os.path.basename(abs_path))
            frames = rapi.clip_duration_frames(item) if item is not None else None
            if frames is not None:
                return frames, "resolve"
            secs = handoff.probe_duration_seconds(abs_path)
            return handoff.seconds_to_frames(secs, fps), ("ffprobe" if secs is not None else "unknown")

        connected = True
    except rapi.ResolveError as e:
        fps, fps_source = handoff.kind_fps(manifest), "kind-default"
        connected = False
        ctx = {"error": str(e)}

        def bin_lookup(_kind, _fname):
            return None

        def duration_lookup(v, abs_path):
            secs = handoff.probe_duration_seconds(abs_path)
            if secs is not None:
                return handoff.seconds_to_frames(secs, fps), "ffprobe"
            if v.get("tcStart") is not None and v.get("tcEnd") is not None:
                return handoff.seconds_to_frames(float(v["tcEnd"]) - float(v["tcStart"]), fps), "beat"
            return None, "unknown"

    plan = handoff.plan_timeline(
        manifest, base, timeline_name, cues, fps, fps_source, connected, bin_lookup, duration_lookup
    )
    plan["cuesEnabled"] = cues_on
    plan["cuesFile"] = manifest.get("cues") if cues_on else None
    if cue_warning:
        plan["cueWarning"] = cue_warning
    if not connected:
        plan["resolveError"] = ctx.get("error")
    return plan, cues, ctx


@mcp.tool(annotations=CREATES)
def build_timeline(manifest_path: str, timeline_name: str = "EDIT 01",
                   dry_run: bool = False, cues: bool = True) -> dict:
    """Build a NEW timeline in the currently open project: the package's clips
    in manifest (shot) order on V1, one Blue marker per shot carrying its
    number + description, each VO clip on A1 UNDER THE SHOT it is pinned to
    (a board-wide VO, or a pin whose shot has no clip here, still goes to the
    head), and — when the manifest names a dialogue-cues file and `cues` is
    on — one RANGE marker per scripted line under its shot (name = speaker,
    note = the line, colour per speaker). Run import_package first.

    dry_run=True touches nothing: it returns the full plan (clip order with
    on-disk / in-bin presence, VO placement per take, shot + cue markers, a
    `missing` list and `wouldBuild`) in the same shape the real build reports,
    so the two can be diffed. Works without Resolve running (bin presence is
    then reported as unknown)."""
    try:
        manifest, base = _load_manifest(manifest_path)
        plan, cue_list, ctx = _plan(manifest, base, timeline_name, cues)
        if dry_run:
            return _ok(dryRun=True, **handoff.summarize(plan), wouldBuild=plan["wouldBuild"],
                       missing=plan["missing"], plan=plan)

        if "project" not in ctx:
            raise rapi.ResolveError(plan.get("resolveError") or "Cannot reach DaVinci Resolve.")
        project, media_pool, by_name = ctx["project"], ctx["media_pool"], ctx["by_name"]

        not_in_bin = [os.path.basename(m["file"]) for m in plan["missing"] if m["where"] == "bin"
                      and m["file"] in {v["file"] for v in manifest.get("videos") or []}]
        if not_in_bin:
            raise rapi.ResolveError(
                f"Clips not in the media pool (run import_package first): {', '.join(not_in_bin)}"
            )
        # every clip is in the bin past this point, so items and markers are the
        # same list in the same order (rapi.build_timeline zips them)
        items = [by_name["videos"][m["file"]] for m in plan["markers"]]
        markers = [{"name": m["name"], "note": m["note"]} for m in plan["markers"]]
        timeline = rapi.build_timeline(project, media_pool, timeline_name, items, markers)

        # WHERE EACH VO CLIP GOES. OSIDE pins narration takes to individual
        # shots and writes `audio[].placements` naming the owning scene, shot
        # and the videos[] file it sits against. Resolve it to a real timeline
        # frame so the take lands UNDER its shot; anything unplaceable (a
        # board-wide VO with no placements, or a pin whose shot has no clip in
        # this package) still goes to the head, as before [council 2026-08-07].
        # The plan's frames were estimates; re-lay everything from where the
        # clips ACTUALLY landed on V1.
        observed = rapi.observe_timeline(timeline)
        tl_start = int(timeline.GetStartFrame())
        landed = {it["name"]: it["start"] for it in observed["v1"]}
        lengths = {it["name"]: it["duration"] for it in observed["v1"]}
        for c in plan["clips"]:
            fname = os.path.basename(c["file"])
            c["startFrame"] = landed.get(fname)
            c["durationFrames"] = lengths.get(fname, c["durationFrames"])
            c["durationSource"] = "timeline"
        for m in plan["markers"]:
            m["frame"] = landed.get(m["file"], m["frame"])
        vo_on_disk = {os.path.basename(r["file"]): r["onDisk"] for r in plan["vo"]}
        vo_in_bin = {os.path.basename(r["file"]): r["inBin"] for r in plan["vo"]}
        plan["vo"] = handoff.vo_rows(manifest, landed, set(landed), vo_on_disk, vo_in_bin)
        vo_report = {"underShot": 0, "atHead": 0, "loose": 0, "looseLabels": []}
        placements = []
        for r in plan["vo"]:
            item = by_name["audio"].get(os.path.basename(r["file"]))
            if item is None:
                continue
            record = None if r["mode"] == "atHead" else tl_start + int(r["startFrame"])
            placements.append({"item": item, "recordFrame": record, "label": r.get("label")})
        if placements:
            vo_report = rapi.append_audio(media_pool, timeline, placements)

        plan["cueMarkers"] = handoff.cue_rows(manifest, cue_list, landed, lengths, plan["fps"])
        cue_report = rapi.add_range_markers(timeline, plan["cueMarkers"]) if plan["cueMarkers"] else {"placed": 0, "skipped": []}

        summary = handoff.summarize(plan)
        summary.update(
            voClips=len(placements),
            voUnderShot=vo_report["underShot"],
            voAtHead=vo_report["atHead"],
            voLoose=vo_report["loose"],
            voLooseLabels=vo_report["looseLabels"],
            cueMarkers=cue_report["placed"],
            cueMarkersSkipped=cue_report["skipped"],
        )
        return _ok(dryRun=False, **summary, wouldBuild=True,
                   missing=[m for m in plan["missing"] if m["where"] == "disk"], plan=plan)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=READ_ONLY)
def verify_import(manifest_path: str, timeline_name: str | None = None, cues: bool = True) -> dict:
    """The acceptance gate. Checks the open project against the manifest and
    returns a per-check table `checks: [{check, expected, found, pass, ...}]`
    plus `overall: PASS|FAIL`: every package file in its bin; V1 clip count,
    order (by clip name) and no strays; every pinned VO take on its shot's
    first frame (0-frame tolerance, delta reported) and unpinned VO at the
    head; one shot marker per shot, each on its shot's first frame; cue
    markers == scripted lines (0 when `cues` is off). `timeline_name` picks a
    timeline without switching to it (default: the current one). The old
    `clean` + `report` fields stay; `clean` now means overall PASS."""
    try:
        manifest, base = _load_manifest(manifest_path)
        resolve = rapi.connect()
        _pm, project = _open_project(resolve)
        media_pool = project.GetMediaPool()
        cue_list, _warn = handoff.load_cues(manifest, base) if cues else ([], None)

        timeline = rapi.timeline_by_name(project, timeline_name)
        observed = {
            "binVideos": _bin_names(media_pool, manifest.get("videos") or [], "VIDEOS"),
            "binAudio": _bin_names(media_pool, manifest.get("audio") or [], "VO"),
            "timeline": rapi.observe_timeline(timeline) if timeline else None,
        }
        result = handoff.evaluate_verify(manifest, cue_list, observed, cues_expected=cues)
        return _ok(overall=result["overall"], clean=result["overall"] == "PASS",
                   checks=result["checks"], report=result["report"])
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.prompt(name="handoff", description="The OSIDE → Resolve handoff recipe, step by step.")
def handoff_prompt(manifest_path: str = "<package>/manifest.json") -> str:
    """The four-step handoff, in the order the tools expect it."""
    return (
        "Drive an oside-davinci/v1 package into DaVinci Resolve with the oside-resolve tools.\n\n"
        "0. Preconditions: call resolve_status. Resolve STUDIO must be running with "
        "Preferences → System → General → External scripting = Local (launch_resolve opens it). "
        "Compare resolve_status.capabilities.features with what the package needs.\n"
        f"1. create_project(kind, name) — kind is manifest.kind ('cinematic' → 23.976 fps template, "
        f"'explainer' → 60 fps). An existing project name is refused: choose another, never overwrite.\n"
        f"2. import_package({manifest_path!r}) — clips into the VIDEOS bin, narration into VO.\n"
        f"3. build_timeline({manifest_path!r}, dry_run=True) first and read the plan (clip order, VO "
        "placement, markers, missing); then build_timeline(...) for real. A NEW timeline: clips in shot "
        "order on V1, one Blue marker per shot, each pinned VO under its shot on A1, dialogue cues as "
        "range markers.\n"
        f"4. verify_import({manifest_path!r}) — the gate. Report `overall` first (PASS or FAIL) and quote "
        "the failing rows of `checks` verbatim; a FAIL is a FAIL, name the delta.\n\n"
        "Never render, never delete, never overwrite; name any partial project left behind."
    )


if __name__ == "__main__":
    mcp.run()
