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

from mcp.server.fastmcp import FastMCP

import resolve_api as rapi

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(HERE, "templates")
TEMPLATES_CONFIG = os.path.join(TEMPLATES_DIR, "templates.json")

mcp = FastMCP("oside-resolve")


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


def _err(e: Exception) -> dict:
    return {"ok": False, "error": str(e)}


@mcp.tool()
def resolve_status() -> dict:
    """Is Resolve reachable? Returns version, current project and its projects list."""
    try:
        resolve = rapi.connect()
        pm = rapi.project_manager(resolve)
        current = pm.GetCurrentProject()
        return _ok(
            version=resolve.GetVersionString(),
            currentProject=current.GetName() if current else None,
            projects=rapi.project_names(pm),
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def launch_resolve() -> dict:
    """Open DaVinci Resolve (if closed) and wait until scripting answers."""
    try:
        resolve = rapi.launch_and_connect()
        return _ok(version=resolve.GetVersionString())
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def import_package(manifest_path: str) -> dict:
    """Import an oside-davinci/v1 package into the CURRENTLY OPEN project:
    videos land in the manifest's video bin (VIDEOS), narration mp3s in the
    VO bin — bins are found anywhere in the tree, or created under the root.
    Run create_project first."""
    try:
        manifest, base = _load_manifest(manifest_path)
        resolve = rapi.connect()
        pm = rapi.project_manager(resolve)
        project = pm.GetCurrentProject()
        if project is None:
            raise rapi.ResolveError("No project is open — run create_project first.")
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


@mcp.tool()
def build_timeline(manifest_path: str, timeline_name: str = "EDIT 01") -> dict:
    """Build a NEW timeline in the currently open project: the package's clips
    in manifest (shot) order on V1, one marker per shot carrying its number +
    description, and each VO clip on A1 UNDER THE SHOT it is pinned to (a
    board-wide VO, or a pin whose shot has no clip here, still goes to the
    head). Run import_package first."""
    try:
        manifest, _base = _load_manifest(manifest_path)
        resolve = rapi.connect()
        pm = rapi.project_manager(resolve)
        project = pm.GetCurrentProject()
        if project is None:
            raise rapi.ResolveError("No project is open — run create_project first.")
        media_pool = project.GetMediaPool()

        videos = manifest.get("videos") or []
        video_bin = rapi.find_bin(media_pool, (videos[0].get("bin") if videos else None) or "VIDEOS")
        by_name = rapi.clips_by_filename(video_bin) if video_bin else {}
        items, markers, missing = [], [], []
        shot_word = "block" if manifest.get("kind") == "explainer" else "shot"
        for v in videos:
            fname = os.path.basename(v["file"])
            clip = by_name.get(fname)
            if clip is None:
                missing.append(fname)
                continue
            items.append(clip)
            note = v.get("description") or ""
            if v.get("vo"):
                note = f"{note}\nVO: {v['vo']}".strip()
            markers.append({"name": f"{shot_word} {v.get('shotNumber', '?')}", "note": note})
        if missing:
            raise rapi.ResolveError(
                f"Clips not in the media pool (run import_package first): {', '.join(missing)}"
            )

        timeline = rapi.build_timeline(project, media_pool, timeline_name, items, markers)

        # WHERE EACH VO CLIP GOES. OSIDE pins narration takes to individual
        # shots and writes `audio[].placements` naming the owning scene, shot
        # and the videos[] file it sits against. Resolve it to a real timeline
        # frame so the take lands UNDER its shot; anything unplaceable (a
        # board-wide VO with no placements, or a pin whose shot has no clip in
        # this package) still goes to the head, as before [council 2026-08-07].
        audio_entries = manifest.get("audio") or []
        frames_by_clip = rapi.video_start_frames(timeline, items)
        vo_report = {"underShot": 0, "atHead": 0, "loose": 0, "looseLabels": []}
        vo_count = 0
        if audio_entries:
            vo_bin = rapi.find_bin(media_pool, audio_entries[0].get("bin") or "VO")
            placements = []
            if vo_bin:
                by_vo_name = rapi.clips_by_filename(vo_bin)
                for a in audio_entries:
                    item = by_vo_name.get(os.path.basename(a["file"]))
                    if item is None:
                        continue
                    # One narration can be pinned to SEVERAL shots. The file is
                    # imported once; lay it once per shot it belongs to.
                    targets = [
                        frames_by_clip.get(os.path.basename(p["videoFile"]))
                        for p in (a.get("placements") or [])
                        if p.get("videoFile")
                    ]
                    targets = [f for f in targets if f is not None]
                    if targets:
                        for f in targets:
                            placements.append(
                                {"item": item, "recordFrame": f, "label": a.get("label")}
                            )
                    else:
                        placements.append(
                            {"item": item, "recordFrame": None, "label": a.get("label")}
                        )
            vo_count = len(placements)
            if placements:
                vo_report = rapi.append_audio(media_pool, timeline, placements)

        return _ok(
            timeline=timeline_name,
            clips=len(items),
            markers=len(markers),
            voClips=vo_count,
            voUnderShot=vo_report["underShot"],
            voAtHead=vo_report["atHead"],
            voLoose=vo_report["loose"],
            voLooseLabels=vo_report["looseLabels"],
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool()
def verify_import(manifest_path: str) -> dict:
    """Check the open project against the manifest: every package file present
    in its bin, and the timeline clip count. The Phase-4 gate check."""
    try:
        manifest, _base = _load_manifest(manifest_path)
        resolve = rapi.connect()
        pm = rapi.project_manager(resolve)
        project = pm.GetCurrentProject()
        if project is None:
            raise rapi.ResolveError("No project is open.")
        media_pool = project.GetMediaPool()

        report = {}
        for key in ("videos", "audio"):
            entries = manifest.get(key) or []
            if not entries:
                report[key] = {"expected": 0, "found": 0, "missing": []}
                continue
            folder = rapi.find_bin(media_pool, entries[0].get("bin") or "VIDEOS")
            names = set(rapi.clips_by_filename(folder).keys()) if folder else set()
            missing = [os.path.basename(e["file"]) for e in entries if os.path.basename(e["file"]) not in names]
            report[key] = {"expected": len(entries), "found": len(entries) - len(missing), "missing": missing}

        timeline = project.GetCurrentTimeline()
        report["timeline"] = (
            {
                "name": timeline.GetName(),
                "videoClips": len(timeline.GetItemListInTrack("video", 1) or []),
                "markers": len(timeline.GetMarkers() or {}),
            }
            if timeline
            else None
        )
        clean = all(not r["missing"] for r in (report["videos"], report["audio"]))
        return _ok(clean=clean, report=report)
    except Exception as e:  # noqa: BLE001
        return _err(e)


if __name__ == "__main__":
    mcp.run()
