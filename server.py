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

import datetime
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


def _template_fps(manifest: dict) -> float:
    """The timeline rate the manifest's kind will get — from templates.json
    (`fps` per template), the kind default when the file cannot be read. This
    is what a dry run reports when the target project is not the open one:
    the CURRENT project's rate says nothing about a project not built yet."""
    kind = manifest.get("kind") or "cinematic"
    try:
        fps = _templates().get(kind, {}).get("fps")
        return float(fps) if fps else handoff.kind_fps(manifest)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return handoff.kind_fps(manifest)


def _check_entries(manifest: dict, base: str) -> None:
    """Two invariants this server RELIES ON but never used to check.

    1. Every entry path stays inside the package. `os.path.join(base, e["file"])`
       silently returns an ABSOLUTE path when `file` is absolute, and `../..`
       walks straight out — so a hand-built or malformed manifest could point
       imports at anything on disk.
    2. No two entries share a basename. Resolve matches clips by name alone, so
       colliding basenames mean the wrong media is laid twice and the right
       media never — and `apply_look` then grades whichever instance won the
       dict. OSIDE's exporter already guarantees uniqueness (lib/export/
       basenames.ts, `claimUniqueFile`), which is exactly why this went unseen:
       we depend on an invariant only the PRODUCER enforces. A second producer,
       or a hand-edited package, breaks it silently [audit 2026-08-24].
    """
    root = os.path.realpath(base)
    seen: dict = {}
    for key in ("videos", "audio"):
        for e in manifest.get(key) or []:
            rel = e.get("file") or ""
            if os.path.isabs(rel):
                raise rapi.ResolveError(
                    f"Manifest entry {rel!r} is an absolute path — package files must be "
                    "relative to the manifest."
                )
            full = os.path.realpath(os.path.join(base, rel))
            if full != root and not full.startswith(root + os.sep):
                raise rapi.ResolveError(
                    f"Manifest entry {rel!r} resolves outside the package directory."
                )
            name = os.path.basename(rel).lower()
            if name in seen:
                raise rapi.ResolveError(
                    f"Two package files share the clip name {os.path.basename(rel)!r} "
                    f"({seen[name]} and {rel}) — Resolve matches clips by name alone, so "
                    "one would silently stand in for the other. Re-export with distinct names."
                )
            seen[name] = rel


def _load_manifest(manifest_path: str) -> tuple[dict, str]:
    if not os.path.isfile(manifest_path):
        raise rapi.ResolveError(f"Manifest not found: {manifest_path}")
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    if manifest.get("format") != "oside-davinci/v1":
        raise rapi.ResolveError(f"Not an oside-davinci/v1 manifest: {manifest_path}")
    _check_shape(manifest)
    base = os.path.dirname(os.path.abspath(manifest_path))
    _check_entries(manifest, base)
    return manifest, base


def _check_shape(manifest: dict) -> None:
    """The manifest's SHAPE, refused here with an operator sentence instead of
    a Python traceback somewhere downstream (audit 2026-09-06 RM-1/3/4/5/6).
    Fourteen corrupt variants were run: `project` as a string killed the
    pipeline with zero bytes of report; a string entry in `videos` died with
    `'str' object has no attribute 'get'`; an entry with no `file` died at
    handoff.py:405; a wrong-TYPE `cues` value silently lost the whole worklist
    while a missing file warned loudly; `kind: documentary` rehearsed green in
    a dry run and was refused only by create_project; `oside-look/2` was
    consumed as v1 without a word. Every sentence here has a row in README's
    error table."""
    project = manifest.get("project")
    if project is not None and not isinstance(project, dict):
        raise rapi.ResolveError(
            f"manifest.project must be an object with a `name` (got {type(project).__name__}) — "
            "re-export the package from the studio."
        )
    kind = manifest.get("kind") or "cinematic"
    try:
        known = sorted(_templates().keys())
    except (OSError, ValueError, json.JSONDecodeError):
        known = []
    if known and kind not in known:
        raise rapi.ResolveError(
            f"Unknown kind {kind!r} — templates.json knows {', '.join(known)}. See list_templates."
        )
    for key in ("videos", "audio"):
        entries = manifest.get(key)
        if entries is None:
            continue
        if not isinstance(entries, list):
            raise rapi.ResolveError(f"manifest.{key} must be a list of entries (got {type(entries).__name__}).")
        for i, e in enumerate(entries):
            if not isinstance(e, dict):
                raise rapi.ResolveError(
                    f"manifest.{key}[{i}] must be an entry object with a `file` (got {type(e).__name__})."
                )
            if not isinstance(e.get("file"), str) or not e.get("file"):
                raise rapi.ResolveError(f"manifest.{key}[{i}] has no `file` — every entry names its package file.")
    for key in ("cues", "textTasks"):
        v = manifest.get(key)
        if v is not None and not isinstance(v, str):
            raise rapi.ResolveError(
                f"manifest.{key} must be the worklist's filename (got {type(v).__name__}) — "
                "a wrong-typed value would drop the whole worklist silently."
            )
    look = manifest.get("look")
    if look is not None:
        if not isinstance(look, dict):
            raise rapi.ResolveError(f"manifest.look must be an object (got {type(look).__name__}).")
        schema = look.get("schema")
        if schema is not None and schema != handoff.LOOK_SCHEMA:
            raise rapi.ResolveError(
                f"manifest.look.schema {schema!r} is not {handoff.LOOK_SCHEMA} — this MCP reads "
                f"{handoff.LOOK_SCHEMA} only; update oside-resolve-mcp or re-export."
            )


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
        # PRODUCT NAME, not just the version: it reads "DaVinci Resolve" on the
        # free edition and "DaVinci Resolve Studio" on Studio. This server needs
        # Studio (the free edition has no external scripting), and our error
        # message used to offer three causes at once — closed / scripting off /
        # free edition — where one call distinguishes them.
        product = None
        try:
            product = resolve.GetProductName()
        except (AttributeError, TypeError):
            pass
        # GetCurrentDatabase is the only HONEST liveness check: after an unclean
        # shutdown Resolve still answers GetProductName, GetVersionString and
        # GetCurrentProject normally while LoadProject/CreateProject fail
        # indefinitely — i.e. every cheap probe passes in the wedged state.
        database = None
        try:
            database = pm.GetCurrentDatabase()
        except (AttributeError, TypeError):
            database = None
        return _ok(
            version=resolve.GetVersionString(),
            product=product,
            studio=(product is None or "studio" in str(product).lower()),
            database=database,
            live=bool(database) if database is not None else None,
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
        # RECONCILE the rate the project actually got against the one
        # templates.json declares. The rate is authored in three places —
        # handoff.KIND_FPS, templates.json, and the .drp itself — and only the
        # .drp is true. Nothing compared them, and export_template overwrites the
        # snapshot without touching templates.json, so a template re-cut at a
        # different rate would leave every seconds-to-frames conversion in the
        # dry run silently wrong [audit 2026-08-24]. Checked today: they agree.
        declared = _template_fps({"kind": kind})
        mismatch = None
        try:
            if fps is not None and abs(float(fps) - float(declared)) > 0.01:
                mismatch = (f"project opened at {fps} fps but templates.json declares "
                            f"{declared} for {kind!r} — re-run export_template, or fix the "
                            "declared fps; the dry run plans at the declared rate")
        except (TypeError, ValueError):
            pass
        return _ok(project=name, template=spec["resolveProject"], timelineFrameRate=fps,
                   declaredFrameRate=declared, frameRateWarning=mismatch)
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
        # The two numbers used to sit side by side under an unconditional ok:true,
        # so a wholly failed ImportMedia reported success with imported.videos: 0
        # and pipeline.py's step() — which reads only `ok` — walked straight past
        # it [audit 2026-08-24]. Compare them.
        if imported != expected:
            short = ", ".join(f"{k}: {imported[k]} of {expected[k]}"
                              for k in expected if imported[k] != expected[k])
            raise rapi.ResolveError(
                f"Resolve imported fewer files than the package names ({short}). "
                "Check the media opens in Resolve, then re-run import_package."
            )
        return _ok(project=project.GetName(), imported=imported, expected=expected)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _plan(manifest: dict, base: str, timeline_name: str, cues_on: bool,
          project_name: str | None = None) -> tuple[dict, list, dict]:
    """The plan for this package: from the media pool when Resolve answers
    AND the open project is the one being planned for, from the manifest +
    disk alone otherwise. Returns (plan, cues, ctx) where ctx carries the
    live objects for the real build (empty offline / when the target project
    is not the open one).

    project_name: the project the plan is FOR. When it is not the open project
    (typically: not created yet — pipeline.py's dry run happens before
    create_project) bin presence is reported unknown, never missing, and the
    fps comes from the kind's template rather than whatever project happens
    to be open."""
    cues, cue_warning = handoff.load_cues(manifest, base) if cues_on else ([], None)
    # The in-frame-text worklist rides the SAME switch as the cues: both are
    # marker worklists laid on the same timeline, and a director who turned the
    # markers off meant all of them.
    text_tasks, text_warning = handoff.load_text_tasks(manifest, base) if cues_on else ([], None)
    ctx: dict = {}
    target: dict | None = None
    bin_unknown = handoff.BIN_UNKNOWN_OFFLINE

    def offline_duration_lookup(fps):
        def duration_lookup(v, abs_path):
            secs = handoff.probe_duration_seconds(abs_path)
            if secs is not None:
                return handoff.seconds_to_clip_frames(secs, fps), "ffprobe"
            if v.get("tcStart") is not None and v.get("tcEnd") is not None:
                return handoff.seconds_to_clip_frames(float(v["tcEnd"]) - float(v["tcStart"]), fps), "beat"
            return None, "unknown"
        return duration_lookup

    def no_bin_lookup(_kind, _fname):
        return None

    try:
        resolve = rapi.connect()
        pm = rapi.project_manager(resolve)
        project = pm.GetCurrentProject()
        current_name = project.GetName() if project is not None else None
        connected = True
        if project_name is not None and current_name != project_name:
            exists = project_name in rapi.project_names(pm)
            target = {"name": project_name, "exists": exists, "current": False}
            bin_unknown = handoff.BIN_UNKNOWN_NOT_OPEN if exists else handoff.BIN_UNKNOWN_NOT_IMPORTED
            fps, fps_source = _template_fps(manifest), "template"
            bin_lookup, duration_lookup = no_bin_lookup, offline_duration_lookup(fps)
            ctx = {"error": f"Target project {project_name!r} is not the open project "
                            f"({'exists but not open' if exists else 'not created yet'})."}
        else:
            if project is None:
                raise rapi.ResolveError("No project is open — run create_project first.")
            if project_name is not None:
                target = {"name": project_name, "exists": True, "current": True}
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
                fps, fps_source = _template_fps(manifest), "template"
            ctx = {"resolve": resolve, "project": project, "media_pool": media_pool, "by_name": by_name}

            def bin_lookup(kind, fname):
                return fname in by_name[kind]

            def duration_lookup(_v, abs_path):
                item = by_name["videos"].get(os.path.basename(abs_path))
                # `fps` is the TIMELINE rate — pass it so a source at a different
                # rate is conformed. Every OSIDE render is 24 fps and the
                # explainer template is 60, so without this the connected plan
                # was short by 182 frames per clip on every explainer board, and
                # every cue frame derived from those starts was wrong with it
                # [audit 2026-08-24]. The offline path below was always correct,
                # which made the CONNECTED dry run the less accurate of the two.
                frames = rapi.clip_duration_frames(item, fps) if item is not None else None
                if frames is not None:
                    return frames, "resolve"
                secs = handoff.probe_duration_seconds(abs_path)
                return handoff.seconds_to_clip_frames(secs, fps), ("ffprobe" if secs is not None else "unknown")

    except rapi.ResolveError as e:
        fps, fps_source = _template_fps(manifest), "template"
        connected = False
        ctx = {"error": str(e)}
        bin_lookup, duration_lookup = no_bin_lookup, offline_duration_lookup(fps)

    plan = handoff.plan_timeline(
        manifest, base, timeline_name, cues, fps, fps_source, connected, bin_lookup, duration_lookup,
        bin_unknown=bin_unknown, target_project=target, text_tasks=text_tasks,
    )
    plan["cuesEnabled"] = cues_on
    plan["cuesFile"] = manifest.get("cues") if cues_on else None
    if cue_warning:
        plan["cueWarning"] = cue_warning
    plan["textTasksFile"] = manifest.get("textTasks") if cues_on else None
    if text_warning:
        plan["textTaskWarning"] = text_warning
    # Collected so the GATE can surface them too. A manifest that names a
    # worklist which is not on disk must not block a build — but reporting PASS
    # without mentioning it turns a half-copied package into a clean handoff.
    plan["sidecarWarnings"] = [w for w in (cue_warning, text_warning) if w]
    if "project" not in ctx:
        plan["resolveError"] = ctx.get("error")
    return plan, cues, ctx, text_tasks


@mcp.tool(annotations=CREATES)
def build_timeline(manifest_path: str, timeline_name: str = "EDIT 01",
                   dry_run: bool = False, cues: bool = True,
                   project: str | None = None) -> dict:
    """Build a NEW timeline in the currently open project: the package's clips
    in manifest (shot) order on V1, one Blue marker per shot carrying its
    number + description, each VO clip on ITS OWN audio track named "VO" (added
    to the timeline; never A1, which the shot clips' embedded audio fills)
    UNDER THE SHOT it is pinned to (a board-wide VO, or a pin whose shot has no
    clip here, still goes to the head), and — when the manifest names a
    dialogue-cues file and `cues` is on — one RANGE marker per scripted line
    under its shot (name = speaker, note = the line, colour per speaker). Every
    VO placement is confirmed by re-reading the VO track, not by Resolve's
    return value. Run import_package first.

    dry_run=True touches nothing: it returns the full plan (clip order with
    on-disk / in-bin presence, VO placement per take with `track: "VO"`, shot +
    cue markers, a `missing` list and `wouldBuild`) in the same shape the real
    build reports, so the two can be diffed. Works without Resolve running (bin
    presence is then reported as unknown). `project` names the project the plan
    is FOR: when it is not the open one (not created yet), bin presence is
    "unknown — not imported yet" (never missing), `wouldBuild` is judged on
    disk presence and fps comes from the kind's template."""
    try:
        manifest, base = _load_manifest(manifest_path)
        plan, cue_list, ctx, text_tasks = _plan(manifest, base, timeline_name, cues, project)
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
        vo_report = {"underShot": 0, "atHead": 0, "loose": 0, "looseLabels": [], "track": None, "trackName": None}
        placements = []
        placed_rows = []
        for r in plan["vo"]:
            item = by_name["audio"].get(os.path.basename(r["file"]))
            if item is None:
                # MARK IT, don't skip it silently. This used to `continue`, and
                # the loop below then stamped `track: "VO"` on every row anyway —
                # so a narration file that never imported produced a row claiming
                # it sat under its shot on the VO track. The aggregate count was
                # honest; the per-row detail was not, and the row is what an agent
                # quotes back [audit 2026-08-24].
                r["placed"] = False
                r["track"] = None
                r["reason"] = "not in the VO bin — never appended"
                continue
            r["placed"] = True
            record = None if r["mode"] == "atHead" else tl_start + int(r["startFrame"])
            placements.append({"item": item, "recordFrame": record, "label": r.get("label")})
            placed_rows.append(r)
        if placements:
            # Narration never rides A1 — it is already full of the shot clips'
            # embedded audio, and Resolve answers truthy on a silent drop. The
            # SPINE lane is created up front (and only when a board-wide take
            # actually needs it) so that VO always precedes VO PINS in the track
            # order: lazy creation would put whichever door the manifest happens
            # to list first at A2, and an editor should not have to guess which
            # lane is which from package to package.
            has_head = any(p["recordFrame"] is None for p in placements)
            vo_track = rapi.ensure_vo_track(timeline) if has_head else None
            vo_report = rapi.append_audio(media_pool, timeline, placements, vo_track)
            # EACH ROW CARRIES THE LANE IT ACTUALLY LANDED ON. Stamping the
            # spine's name onto every row (what this did before the lanes) made
            # every pinned row claim `track: "VO"` while the take sat on VO PINS
            # — the aggregate was right and the per-row detail was a lie, and the
            # row is what an agent quotes back [same defect as audit 2026-08-24].
            for r, lay in zip(placed_rows, vo_report.get("lays") or []):
                r["track"] = lay["trackName"]
                r["trackIndex"] = lay["track"]
                r["placedAt"] = lay.get("placedAt")
                r["laidAs"] = lay.get("mode")

        plan["cueMarkers"] = handoff.cue_rows(manifest, cue_list, landed, lengths, plan["fps"])
        cue_report = rapi.add_range_markers(timeline, plan["cueMarkers"]) if plan["cueMarkers"] else {"placed": 0, "skipped": []}

        # in-frame text, AFTER the cues so the frames they occupy are already
        # taken and the nudge in add_text_task_markers has honest information
        plan["textTaskMarkers"] = handoff.text_task_rows(
            manifest, text_tasks, plan["cueMarkers"], landed, lengths,
        )
        text_report = (rapi.add_text_task_markers(timeline, plan["textTaskMarkers"])
                       if plan["textTaskMarkers"] else {"placed": 0, "skipped": []})

        # RECONCILE BEFORE REPORTING. `summarize` counts the PLAN; the timeline
        # was observed above. rapi.build_timeline now refuses a clip-count
        # mismatch outright, so this is the belt to that braces — and it keeps
        # the reported numbers sourced from the timeline rather than from what
        # we meant to do [audit 2026-08-24].
        summary = handoff.summarize(plan)
        landed_count = len(observed["v1"])
        if landed_count != len(items):
            raise rapi.ResolveError(
                f"Resolve laid {landed_count} clip(s) on V1 but {len(items)} were sent — "
                f"timeline {timeline_name!r} is incomplete."
            )
        summary["clips"] = landed_count
        summary["markers"] = sum(1 for m in (observed.get("markers") or {}).values()
                                 if (m.get("customData") or "") == rapi.SHOT_TAG)
        summary.update(
            voClips=len(placements),
            voUnderShot=vo_report["underShot"],
            voAtHead=vo_report["atHead"],
            voLoose=vo_report["loose"],
            voLooseLabels=vo_report["looseLabels"],
            voTrack=vo_report["trackName"],
            voTrackIndex=vo_report["track"],
            cueMarkers=cue_report["placed"],
            cueMarkersSkipped=cue_report["skipped"],
            textTaskMarkers=text_report["placed"],
            textTaskMarkersSkipped=text_report["skipped"],
        )
        return _ok(dryRun=False, **summary, wouldBuild=True,
                   missing=[m for m in plan["missing"] if m["where"] == "disk"], plan=plan)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def save_project() -> dict:
    """Save the open project, and report what V1 holds AFTER the save.

    The save is the verification boundary. Placements from an append whose
    response errored are not durable — they appear in the timeline, every
    in-session read agrees they are there, and the save discards them. A witness
    read from the same unsaved state as the thing it is checking cannot
    contradict that; only a post-save read can. Run this between build_timeline
    and verify_import.

    Refuses on the default 'Untitled Project': SaveProject cannot succeed there
    (no location, no SaveProjectAs) and headless it blocks forever rather than
    returning."""
    try:
        resolve = rapi.connect()
        pm, project = _open_project(resolve)
        name = project.GetName()
        if name == "Untitled Project":
            raise rapi.ResolveError(
                "Refusing to save 'Untitled Project' — it has no location, the call cannot "
                "succeed, and headless it blocks indefinitely. Create a named project first."
            )
        if not pm.SaveProject():
            raise rapi.ResolveError(f"SaveProject returned false for {name!r}.")
        # what survived the save, per timeline — the whole point of the call
        after = {}
        for i in range(project.GetTimelineCount()):
            tl = project.GetTimelineByIndex(i + 1)
            if tl is not None:
                after[tl.GetName()] = len(tl.GetItemListInTrack("video", 1) or [])
        return _ok(project=name, saved=True, v1CountsAfterSave=after)
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=READ_ONLY)
def verify_import(manifest_path: str, timeline_name: str | None = None, cues: bool = True) -> dict:
    """The acceptance gate. Checks the open project against the manifest and
    returns a per-check table `checks: [{check, expected, found, pass, ...}]`
    plus `overall: PASS|FAIL`: every package file in its bin; V1 clip count,
    order (by clip name) and no strays; every pinned VO take on its shot's
    first frame (0-frame tolerance, delta reported; looked for on EVERY audio
    track, the row names the track it sits on) and unpinned VO at the
    head; one shot marker per shot, each on its shot's first frame; cue
    markers == scripted lines (0 when `cues` is off). `timeline_name` picks a
    timeline without switching to it (default: the current one). The old
    `clean` + `report` fields stay; `clean` now means overall PASS."""
    try:
        manifest, base = _load_manifest(manifest_path)
        resolve = rapi.connect()
        _pm, project = _open_project(resolve)
        media_pool = project.GetMediaPool()
        cue_list, cue_warn = handoff.load_cues(manifest, base) if cues else ([], None)
        task_list, task_warn = handoff.load_text_tasks(manifest, base) if cues else ([], None)

        timeline = rapi.timeline_by_name(project, timeline_name)
        tl_observed = rapi.observe_timeline(timeline) if timeline else None
        observed = {
            "binVideos": _bin_names(media_pool, manifest.get("videos") or [], "VIDEOS"),
            "binAudio": _bin_names(media_pool, manifest.get("audio") or [], "VO"),
            "timeline": tl_observed,
        }
        # RE-DERIVE THE PLAN FROM THE TIMELINE ITSELF, so `expected` means "what
        # the build intended to place" rather than "how many lines the CSV holds".
        # Scoring against the raw worklist made the gate FAIL builds that had
        # correctly refused to put a marker on the wrong shot [audit 2026-08-24].
        planned_cues = planned_tasks = None
        if tl_observed:
            landed = {it["name"]: it["start"] for it in tl_observed["v1"]}
            lengths = {it["name"]: it["duration"] for it in tl_observed["v1"]}
            try:
                fps = float(project.GetSetting("timelineFrameRate"))
            except (TypeError, ValueError):
                fps = _template_fps(manifest)
            planned_cues = handoff.cue_rows(manifest, cue_list, landed, lengths, fps)
            planned_tasks = handoff.text_task_rows(manifest, task_list, planned_cues,
                                                  landed, lengths)
        result = handoff.evaluate_verify(
            manifest, cue_list, observed, cues_expected=cues, text_tasks=task_list,
            planned_cues=planned_cues, planned_text_tasks=planned_tasks,
            stock_intent=handoff.stock_intent_note(manifest),
            sidecar_warnings=[w for w in (cue_warn, task_warn) if w],
        )
        # step (e): the verdict is WRITTEN beside the manifest, per clip by
        # generationId, so OSIDE's build door can read it back onto the row.
        # A sidecar that cannot be written is a warning on the result, never a
        # failed verify — the gate's verdict is the timeline, not the file.
        sidecar_path = None
        sidecar_warning = None
        try:
            sidecar = handoff.verify_sidecar(
                manifest, observed, result,
                datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            sidecar_path = handoff.write_verify_sidecar(base, sidecar)
        except Exception as e:  # noqa: BLE001
            sidecar_warning = f"verify.json not written: {e}"
        return _ok(overall=result["overall"], clean=result["overall"] == "PASS",
                   checks=result["checks"], report=result["report"],
                   sidecar=sidecar_path,
                   **({"sidecarWarning": sidecar_warning} if sidecar_warning else {}))
    except Exception as e:  # noqa: BLE001
        return _err(e)


@mcp.tool(annotations=IDEMPOTENT_WRITE)
def apply_look(manifest_path: str, timeline_name: str | None = None, dry_run: bool = False,
               verify: bool = False) -> dict:
    """"Look travels": put the film's starting balance on every clip. Reads the
    manifest's `look` block and the `look-cdl.json` Depth Converter measured
    beside it (`depthc look-compare --manifest --emit-cdl`), and sets that
    clip's ASC CDL on NODE 1 of each V1 item (slope/offset/power/saturation —
    key, temperature, saturation only; never palette content, never a
    creative grade). Idempotent: absolute values, a re-run resets node 1 to
    the same CDL. Refuses — applies nothing — when the manifest has no look
    block or the CDL file is missing/unreadable. `dry_run=True` returns the
    plan without touching Resolve. Returns per-clip rows {clip, shot, applied,
    identity, cdl}, `missing` (manifest clips it could not grade), `extra` (V1
    items the manifest does not know — untouched).

    The CDL lands in a colour version named `OSIDE base`, created per clip and
    left ACTIVE — so the film opens showing the balance as before, while the
    colourist keeps a clean `Version 1` underneath and an unambiguous name for
    which grade is the tool's. A re-run overwrites `OSIDE base` (ours, by name),
    not their work — provided they grade in their own version.

    Resolve exposes no `GetCDL`, so without `verify` the `applied` flag is only
    what SetCDL said about itself. **`verify=True` READS THE GRADE BACK** out of
    a `Timeline.Export` EDL+CDL: each row gains `verified` (and `readBackDiffs`
    when it does not match), the result gains a `readback` block, and `complete`
    then requires the read-back to agree. The EDL carries no clip names — every
    event's reel is `AX` — so events match V1 items BY POSITION."""
    try:
        manifest, base = _load_manifest(manifest_path)
        cdl_doc, why = handoff.load_look_cdl(manifest, base)
        if cdl_doc is None:
            return _ok(applied=0, refused=True, reason=why, rows=[], missing=[], extra=[])
        stock_note = handoff.stock_intent_note(manifest)
        if dry_run:
            names = [{"name": _basename_of(v["file"])} for v in manifest.get("videos") or []]
            plan = handoff.plan_look(manifest, cdl_doc, names)
            return _ok(dryRun=True, look=manifest["look"].get("name"), rows=plan["rows"], missing=plan["missing"], extra=plan["extra"],
                       stockIntent=stock_note)
        resolve = rapi.connect()
        _pm, project = _open_project(resolve)
        timeline = rapi.timeline_by_name(project, timeline_name)
        if timeline is None:
            raise rapi.ResolveError("No timeline — run build_timeline first.")
        items = timeline.GetItemListInTrack("video", 1) or []
        v1 = [{"name": it.GetName()} for it in items]
        plan = handoff.plan_look(manifest, cdl_doc, v1)
        by_name = {it.GetName(): it for it in items}
        rows = []
        for r in plan["rows"]:
            it = by_name[r["clip"]]
            # THE LOOK GETS ITS OWN COLOUR VERSION. SetCDL writes node 1 of
            # whatever version is ACTIVE, so writing the bare active version put
            # our starting balance in the same slot a colourist grades in — the
            # one place this server overwrote a human's work. `look_version`
            # creates `OSIDE base` and leaves it active, so the film still opens
            # showing the balance while a clean Version 1 survives underneath.
            # It never raises: a Resolve that will not give us a version falls
            # back to today's behaviour and says so on the row.
            ver = rapi.look_version(it)
            ok = bool(it.SetCDL(r["set"]))
            row = {"clip": r["clip"], "shot": r["shot"], "applied": ok,
                   "version": ver.get("version"), "versionCreated": ver.get("created"),
                   "identity": r["identity"], "cdl": r["cdl"], "measured": r["measured"]}
            if ver.get("why"):
                row["versionNote"] = ver["why"]
            if not ok:
                # SetCDL returns False with NO reason. The diagnosable cause is
                # the node count: NodeIndex is 1-based and must not exceed
                # GetNodeGraph().GetNumNodes() (TimelineItem.GetNumNodes is
                # deprecated). Attach it rather than reporting a bare false.
                nodes = None
                try:
                    graph = it.GetNodeGraph()
                    nodes = int(graph.GetNumNodes()) if graph is not None else None
                except (AttributeError, TypeError, ValueError):
                    nodes = None
                row["nodes"] = nodes
                row["why"] = ("node 1 does not exist on this item" if nodes == 0
                              else "SetCDL rejected the values" if nodes
                              else "SetCDL returned false and the node count could not be read")
            rows.append(row)
        applied = sum(1 for r in rows if r["applied"])

        # READ BACK. `applied` above is SetCDL's answer about itself; this is the
        # only witness Resolve offers that is not the writer's own return value.
        # There is no GetCDL, but EXPORT_EDL+EXPORT_CDL carries the applied
        # numbers out (proven on the 2026-08-24 walk). The EDL has no clip names
        # — the reel is `AX` on every event — so events match V1 items BY
        # POSITION, which is why the mapping goes through `v1` and not the
        # manifest order.
        readback = None
        if verify:
            text = rapi.export_timeline_cdl(resolve, timeline)
            if text is None:
                readback = {"ok": False, "reason": "Timeline.Export returned no EDL — cannot read back"}
            else:
                found = handoff.parse_cdl_edl(text)
                by_clip = {}
                for i, it in enumerate(v1):
                    if i < len(found):
                        by_clip[it["name"]] = found[i]
                checked = 0
                for r in rows:
                    if not r["applied"]:
                        continue
                    verdict = handoff.compare_cdl(r["cdl"], by_clip.get(r["clip"]))
                    r["verified"] = verdict["match"]
                    if verdict["diffs"]:
                        r["readBackDiffs"] = verdict["diffs"]
                    checked += 1
                matched = sum(1 for r in rows if r.get("verified"))
                readback = {"ok": True, "events": len(found), "checked": checked,
                            "verified": matched,
                            "allMatch": checked > 0 and matched == checked,
                            "version": rapi.LOOK_VERSION_NAME,
                            # The export reads each item's ACTIVE version
                            # (measured 2026-08-25). `look_version` left ours
                            # active a few lines above, which is the only reason
                            # this read-back is about OUR grade — verifying with
                            # the colourist's version active would have confirmed
                            # THEIR values and reported success.
                            "how": "Timeline.Export(EXPORT_EDL, EXPORT_CDL) with "
                                   f"{rapi.LOOK_VERSION_NAME!r} active, matched by V1 position"}

        # the stock is INTENT: one marker, nothing graded for it.
        stock_marker = rapi.add_stock_marker(timeline, stock_note) if stock_note else None
        # A run where NOTHING applied used to return ok:true. The look is the
        # whole point of the call; a total failure is a failure [audit 2026-08-24].
        complete = (applied == len(rows)) and not plan["missing"]
        if readback is not None:
            # a run that could not be read back, or read back wrong, is not complete
            complete = complete and bool(readback.get("allMatch"))
        return _ok(look=manifest["look"].get("name"), applied=applied, of=len(rows), rows=rows,
                   missing=plan["missing"], extra=plan["extra"],
                   complete=complete,
                   readback=readback,
                   stockMarker=stock_marker,
                   # The CDL file carries its own honesty — Depth Converter stamps
                   # a `derivation` line saying the gains are a fitted HEURISTIC.
                   # Repeat it rather than substituting our own summary: the file
                   # was honest about what it is and this tool was not.
                   derivation=cdl_doc.get("derivation"),
                   note="node 1 CDL = starting balance (key/temperature/saturation). Not a read-back: Resolve exposes no GetCDL "
                        "(applied values can only be read back out of band, e.g. a Timeline.Export EDL+CDL). "
                        "A film stock, when the manifest names one, travels as a marker only — never a node or a LUT.")
    except Exception as e:  # noqa: BLE001
        return _err(e)


def _basename_of(rel: str) -> str:
    return os.path.basename(rel)


@mcp.prompt(name="handoff", description="The OSIDE → Resolve handoff recipe, step by step.")
def handoff_prompt(manifest_path: str = "<package>/manifest.json") -> str:
    """The four-step handoff, in the order the tools expect it."""
    return (
        "Drive an oside-davinci/v1 package into DaVinci Resolve with the oside-resolve tools.\n\n"
        "0. Preconditions: call resolve_status. Resolve STUDIO must be running with "
        "Preferences → System → General → External scripting = Local (launch_resolve opens it). "
        "Compare resolve_status.capabilities.features with what the package needs.\n"
        f"1. build_timeline({manifest_path!r}, dry_run=True, project=name) — the plan BEFORE anything is "
        "created: clip order, VO placement, markers, missing. With the target project not created yet, "
        "bin presence reads 'unknown — not imported yet' (that is not missing) and wouldBuild is judged "
        "on disk presence; fps comes from the kind's template.\n"
        f"2. create_project(kind, name) — kind is manifest.kind ('cinematic' → 23.976 fps template, "
        f"'explainer' → 60 fps). An existing project name is refused: choose another, never overwrite.\n"
        f"3. import_package({manifest_path!r}) — clips into the VIDEOS bin, narration into VO. Then "
        f"build_timeline({manifest_path!r}) for real. A NEW timeline: clips in shot order on V1, one Blue "
        "marker per shot, each pinned VO under its shot on its OWN audio track named VO (never A1 — the "
        "shot clips' embedded audio fills it), dialogue cues as range markers.\n"
        f"3b. (when the manifest carries a `look` block) apply_look({manifest_path!r}, verify=True) — the film's starting "
        "balance on node 1 of every V1 clip, from the look-cdl.json Depth Converter measured beside the "
        "manifest (`depthc look-compare --manifest --emit-cdl`). Key, temperature, saturation only — never "
        "a palette fix, never a creative grade. `verify=True` reads the grade BACK out of an EDL+CDL export "
        "(Resolve has no GetCDL) — quote `readback.verified of checked`, not just `applied`. "
        "If it `refused`, say why and move on; it never blocks the gate.\n"
        "3c. save_project() — SAVE BEFORE YOU VERIFY. The save is the verification boundary: "
        "placements from an errored append are visible to every in-session read and are discarded "
        "by the save, so a gate run before it cannot contradict them. It reports V1 counts AFTER "
        "the save; compare them with what build_timeline said it laid.\n"
        f"4. verify_import({manifest_path!r}) — the gate. Report `overall` first (PASS or FAIL) and quote "
        "the failing rows of `checks` verbatim; a FAIL is a FAIL, name the delta. The gate binds each "
        "marker to the shot it names, scores cue/text markers against what the build INTENDED to place "
        "(a reported skip is not a failure), requires the stock marker when the manifest names a stock, "
        "and requires narration to be off A1.\n\n"
        "Never render, never delete, never overwrite; name any partial project left behind."
    )


if __name__ == "__main__":
    mcp.run()
