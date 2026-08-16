# oside-resolve-mcp

The OSIDE studio → DaVinci Resolve bridge (OSIDE-PLAN-2 Phase 4). A small,
deterministic MCP server that consumes an `oside-davinci/v1` package — written
by the studio's **Export for DaVinci** action — and turns it into a Resolve
project:

1. `create_project(kind, name)` — new project from the right template snapshot
   (`cinematic` → YT-4K 23.976fps, `explainer` → YT-4K 60fps). Settings and the
   whole bin tree (O-Side Media / VIDEOS / VO / …) come from the `.drp`.
2. `import_package(manifest_path)` — videos into the VIDEOS bin, narration
   mp3s into the VO bin.
3. `build_timeline(manifest_path, timeline_name="EDIT 01", dry_run=False,
   cues=True)` — clips in shot order on V1, one Blue point marker per shot
   carrying its number + description (+ VO line for explainers), each pinned
   VO take on A1 under its own shot (unpinned VO at the head), and — when the
   manifest names a `dialogue-cues.csv` — one **range marker per scripted
   line** under its shot: name = speaker, note = the line, length = the line's
   estimate (the shot's length when a cue has none), one colour per speaker
   (deterministic per project, never Blue). `cues=False` skips them.
   **`dry_run=True` touches nothing** and returns the full plan — ordered clip
   list with on-disk / in-bin presence, VO placement per take (`underShot` /
   `atHead`, target shot, start frame), shot + cue markers, a `missing` list
   and `wouldBuild` — in the same shape the real call reports (`plan` on
   both), so the two can be diffed. It works with Resolve closed (bin presence
   is then `"unknown — Resolve not connected"`; clip lengths come from
   `ffprobe` when it is on PATH, or an explainer's beat timing).
4. `verify_import(manifest_path, timeline_name=None, cues=True)` — the
   acceptance gate: a per-check table `checks: [{check, expected, found,
   pass, …}]` and `overall: PASS|FAIL`. Checks: every package file in its bin;
   V1 clip count; V1 clip order == manifest order (by clip name); no stray V1
   clips; every **pinned** VO take on its shot's first frame (tolerance 0,
   `delta` reported; unpinned takes pass on presence, since two head takes
   cannot share frame 0 on A1); one shot marker per shot, each on its shot's
   first frame; cue markers == scripted lines (0 when `cues=False`). The
   pre-v2 `clean` + `report` fields stay (`clean` now means overall PASS).

Plus `resolve_status` (with a `capabilities` block — `{"manifest":
"oside-davinci/v1", "features": ["placements","cues","dry_run","verify_v2"],
"version"}` — returned even when Resolve is unreachable, so OSIDE can compare
before it relies on a feature), `launch_resolve`, `list_templates`, and
`export_template(kind)` (snapshot a live template project to `templates/`).
One MCP prompt, `handoff`, carries the recipe below. Every tool declares MCP
annotations (`readOnlyHint` / `destructiveHint` / `idempotentHint` /
`openWorldHint`).

Markers are tagged through Resolve's `customData` (`oside:shot`, `oside:cue`)
so the gate tells them apart by more than colour; a bare Blue marker from a
pre-0.2 timeline still counts as a shot marker.

`pipeline.py <manifest.json> [--name] [--timeline] [--dry-run] [--no-cues]`
runs the whole thing as one command (what the studio's **Build in Resolve**
button spawns); `--dry-run` prints the plan and never launches Resolve.

## Self-test

```bash
.venv/bin/python tests/test_handoff.py      # no Resolve, no pytest needed
.venv/bin/python -m pytest tests            # if pytest is installed
```

Drives the dry run against `tests/fixtures/pkg` (a fixture package with empty
media files) and the whole build → verify path against a fake Resolve.

This is deliberately NOT general Resolve remote control —
[samuelgursky/davinci-resolve-mcp](https://github.com/samuelgursky/davinci-resolve-mcp)
(MIT) exists for that, and its connection pattern informed `resolve_api.py`.
A fixed pipeline gets fixed tools so it runs identically every time.

## Requirements

- DaVinci Resolve **Studio** (free edition has no scripting), running.
- Preferences → System → General → **External scripting using: Local**.
- Python 3.10+ with the `mcp` package (`.venv` in this repo).

## Setup

```bash
uv venv && uv pip install mcp        # once
# then register (already done for OSIDE AI FILMMAKER via .mcp.json):
#   command: <repo>/.venv/bin/python   args: [<repo>/server.py]
```

Snapshot the templates once (Resolve open): call `export_template("cinematic")`
and `export_template("explainer")`, or in Resolve right-click the template
project → Export Project → save into `templates/` with the names in
`templates/templates.json`. Re-export whenever a template changes.

## The recipe

```
Studio: board → DaVinci → attach VO → Export package (to the working drive)
Claude: resolve_status()                          ← preconditions + capabilities
        create_project("cinematic", "MY FILM")   ← or explainer
        import_package("/Volumes/.../manifest.json")
        build_timeline("/Volumes/.../manifest.json", dry_run=True)   ← read the plan
        build_timeline("/Volumes/.../manifest.json")
        verify_import("/Volumes/.../manifest.json")                 ← overall PASS|FAIL
```

## Guardrails

- Creates projects/timelines, **never deletes or overwrites** — an existing
  project or timeline name is a hard refusal.
- Template projects are read (exported), never modified.
- Renders are user-triggered in Resolve, not tool-triggered.
- stdio transport only; nothing listens on the network.

## Known errors

Every message the server raises, and its fix. Messages are quoted as they
appear in the tool's `error` field.

| error | cause | fix |
| --- | --- | --- |
| `DaVinciResolveScript module not found: …` | Resolve's scripting modules are not at `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules` — Resolve is not installed, or installed somewhere else | install DaVinci Resolve **Studio** at the standard path (or fix `RESOLVE_MODULES` in `resolve_api.py`) |
| `Cannot reach DaVinci Resolve. Is it running, and is Preferences → System → General → 'External scripting using' set to Local?` | Resolve is closed, or scripting is set to None / Network, or this is the **free** edition (no external scripting) | open Resolve (`launch_resolve` does it), set External scripting = **Local**, and use Studio — the free edition never answers |
| `Resolve did not answer scripting within 60s of launch.` | Resolve is still starting (first launch, database migration, a modal dialog on screen) | dismiss any dialog, wait, call `resolve_status` again |
| `GetProjectManager returned nothing — Resolve may still be starting.` | scripting answered before the Project Manager was up | retry in a few seconds |
| `No project named 'X' in the current project library folder.` | `export_template`: the template project named in `templates/templates.json` is not in the current library folder — renamed, moved, or a different library is open | open the right project library / folder in Resolve, or fix the name in `templates.json` |
| `ExportProject failed for 'X' → …` | Resolve refused to write the `.drp` (path not writable, project open elsewhere) | check `templates/` is writable; close the template project; retry |
| `Template file missing: …/templates/<kind>.drp` | no snapshot for that kind yet | run `export_template(kind)` once with the template project available, and commit the `.drp` |
| `A project named 'X' already exists — pick another name.` | the **create-only guardrail**: this server never overwrites a project | choose a new name (in the studio: rename in the field and build again); delete the old one yourself in Resolve if that is what you want |
| `ImportProject failed (… as 'X').` | the `.drp` is corrupt or from a newer Resolve, or the name is not accepted | re-snapshot the template with `export_template`; use a plain name |
| `Imported but could not open 'X'.` | the project imported but `LoadProject` failed — usually a modal in Resolve | dismiss the dialog, then open the project by hand or rerun with a new name |
| `A timeline named 'X' already exists — pick another name.` | same guardrail, timeline level | pass a different `timeline_name` |
| `No project is open — run create_project first.` | `import_package` / `build_timeline` / `verify_import` need the target project open | `create_project` first, or open it in Resolve |
| `Clips not in the media pool (run import_package first): …` | the timeline build reads clips from the VIDEOS bin and they are not there | run `import_package`; a `build_timeline(dry_run=True)` shows exactly which are missing |
| `Package file missing on disk: …` | a file the manifest names is gone (drive not mounted, package moved) | mount the drive / restore the package; export again from the studio |
| `Manifest not found` / `Not an oside-davinci/v1 manifest` | wrong path, or not a package this server understands | point at the package's `manifest.json`; compare `resolve_status().capabilities.manifest` |
