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
   cues=True, project=None)` — clips in shot order on V1, one Blue point
   marker per shot carrying its number + description (+ VO line for
   explainers), each pinned VO take under its own shot **on its own audio
   track named `VO`** (added to the timeline — never A1, which the shot clips'
   embedded audio fills; unpinned VO at the head of that track), and — when the
   manifest names a `dialogue-cues.csv` — one **range marker per scripted
   line** under its shot: name = speaker, note = the line, length = the line's
   estimate (the shot's length when a cue has none), one colour per speaker
   (deterministic per project, never Blue). `cues=False` skips them.
   When the manifest also names a `text-tasks.csv`, one **point marker per run
   of in-frame lettering** lands on the shot that asked for it: name and note
   carry the exact wording, and the note says the generation deliberately did
   not render it — video models re-spell text between takes, so a sign belongs
   over the clip, not inside it. Placed AFTER that shot's cues (Resolve keeps
   one marker per frame), and a task with no free frame inside its own clip is
   REPORTED rather than nudged onto the next shot. Rides the same `cues`
   switch.
   **`dry_run=True` touches nothing** and returns the full plan — ordered clip
   list with on-disk / in-bin presence, VO placement per take (`underShot` /
   `atHead`, target shot, start frame, `track: "VO"`), shot + cue markers, a
   `missing` list and `wouldBuild` — in the same shape the real call reports
   (`plan` on both), so the two can be diffed. It works with Resolve closed
   (bin presence is then `"unknown — Resolve not connected"`; clip lengths
   come from `ffprobe` when it is on PATH, or an explainer's beat timing).
   `project=NAME` makes the plan **project-aware**: when NAME is not the open
   project (the usual case — the dry run comes before `create_project`), bin
   presence reads `"unknown — not imported yet"` (never `missing`),
   `wouldBuild` is judged on disk presence, and `fps` comes from the kind's
   template (`templates.json`), not from whatever project happens to be open.
   Only when NAME is the open project do the bins count. Every VO placement in
   a real build is confirmed by re-reading the VO track — Resolve's
   `AppendToTimeline` answers a truthy list on a silent drop.
3b. `apply_look(manifest_path, timeline_name=None, dry_run=False)` — **"look
   travels"** (0.3.0). When the manifest carries a `look` block (the studio
   project had a Style Constant with colour-law hexes), reads the
   `look-cdl.json` Depth Converter measured beside it (`depthc look-compare
   --manifest <manifest.json> --emit-cdl`) and sets each clip's ASC CDL on
   **node 1** of its V1 item — key, temperature, saturation only. A matched
   starting balance for the colourist; **never a palette fix, never a creative
   grade** (measured live 2026-08-16: 8 clips went from ΔL* −7…+9.5 to ±0.4
   against the look, palette distance unchanged). Idempotent (absolute
   values). Refuses — applies nothing — without a look block or the CDL file,
   and says why. Returns per-clip rows `{clip, shot, applied, identity, cdl,
   measured}`, `missing`, `extra` (V1 items the manifest does not know stay
   untouched). Resolve exposes no `GetCDL`, so `applied` is only SetCDL's answer
   about itself — **`verify=True` (0.5.0) READS THE GRADE BACK**: it exports the
   timeline as an EDL carrying ASC CDL and compares what returns against what it
   set, per clip (`verified`, plus `readBackDiffs` when it does not match) with a
   `readback` summary. `complete` then requires the read-back to match, so a look
   that did not take can no longer report success. The EDL has no clip names —
   every event's reel is `AX` — so events match V1 items BY POSITION. A false
   `applied` carries a diagnosis against the item's node count. Echoes Depth
   Converter's own `derivation` line (the gains are a fitted HEURISTIC, and the
   file says so). `dry_run=True` returns the plan without touching Resolve.
   **Film stock (0.3.1):** when the look block carries `stockIntent`, one
   marker goes on the head of the timeline — `Stock intent: <label>
   (<balance>) — colourist's call, nothing applied`. No node, no LUT, no
   Film Look Creator: a stock IS a creative grade and this tool sells a
   starting balance. It nudges past shot 1's marker and reports the frame
   it used; no room means a reported skip, never a silent drop.
4. `verify_import(manifest_path, timeline_name=None, cues=True)` — the
   acceptance gate: a per-check table `checks: [{check, expected, found,
   pass, …}]` and `overall: PASS|FAIL`. Checks: every package file in its bin;
   V1 clip count; V1 clip order == manifest order (by clip name); no stray V1
   clips; every **pinned** VO take on its shot's first frame (tolerance 0,
   `delta` reported; looked for on **every** audio track, and each VO row
   names the `track` it sits on, e.g. `"A2 VO"`; unpinned takes pass on
   presence, since two head takes cannot share frame 0); one shot marker per
   shot, each on its shot's
   first frame; cue markers == scripted lines (0 when `cues=False`);
   text-task markers == rows in the worklist, a check that appears ONLY when
   the package carries one, so a package exported before the studio wrote them
   still passes. The
   pre-v2 `clean` + `report` fields stay (`clean` now means overall PASS).
   **v3 (0.5.0):** each shot marker must NAME its own shot (the old row compared
   sorted frame lists, so rotating every marker onto a different clip passed);
   cue and text markers are scored against what the build INTENDED to place, so
   a deliberately reported skip is no longer a FAIL; narration must be OFF A1;
   a stock marker is required when the manifest names a `stockIntent`; and a
   worklist the manifest names but which is not on disk is reported rather than
   silently passing.

Plus `resolve_status` (with a `capabilities` block — `{"manifest":
"oside-davinci/v1", "features": ["placements","cues","dry_run","verify_v2",
"vo_track","look","text_tasks"], "version"}` — returned even when Resolve is unreachable, so OSIDE can compare
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
button spawns); `--dry-run` prints the plan and never launches Resolve. The
dry run is planned for the target project **by name** (`--name`, or the
manifest's project name): before that project exists, bins read `unknown —
not imported yet`, `wouldBuild` is judged on disk and fps comes from the
kind's template — Resolve being open on some other project no longer turns
every file into `missing where:"bin"`.

## Self-test

```bash
.venv/bin/python tests/test_handoff.py      # no Resolve, no pytest needed
.venv/bin/python -m pytest tests            # if pytest is installed
```

Drives the dry run against `tests/fixtures/pkg` (a fixture package with empty
media files) and the whole build → verify path against a fake Resolve. The
fake reproduces two behaviours measured on Resolve 21.0.4.5: a video clip
with embedded audio also fills A1, and a clip-info append aimed at an
occupied frame answers a truthy `[<PyRemoteObject>]` while placing nothing.
`OSIDE_RESOLVE_OFFLINE=1` (set by the test module) keeps the offline cases
offline even with a real Resolve running.

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
        build_timeline("/Volumes/.../manifest.json", dry_run=True, project="MY FILM")
                                                  ← the plan BEFORE anything exists:
                                                    bins "unknown — not imported yet",
                                                    wouldBuild on disk, fps from template
        create_project("cinematic", "MY FILM")   ← or explainer
        import_package("/Volumes/.../manifest.json")
        build_timeline("/Volumes/.../manifest.json")   ← V1 + VO track + markers
        (depthc look-compare --manifest /Volumes/.../manifest.json --emit-cdl)   ← Depth Converter, when the manifest has a look
        apply_look("/Volumes/.../manifest.json", verify=True)   ← node-1 CDL per clip: starting balance,
                                                         key/temp/sat only — and READ BACK out of an
                                                         EDL+CDL export, not just SetCDL's own answer
        save_project()                                 ← THE VERIFICATION BOUNDARY: an errored append's
                                                         placements survive every in-session read and are
                                                         discarded by the save. Verify on the far side of it.
        verify_import("/Volumes/.../manifest.json")    ← overall PASS|FAIL, VO rows name their track
```

## Guardrails

- Creates projects/timelines, **never deletes or overwrites** — an existing
  project or timeline name is a hard refusal.
- Template projects are read (exported), never modified.
- Renders are user-triggered in Resolve, not tool-triggered.
- `save_project` refuses the default `Untitled Project`: the call cannot succeed
  there and headless it blocks forever rather than returning.
- Manifest entries may not be absolute, may not resolve outside the package, and
  may not share a clip name (Resolve matches clips by basename alone).
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
| `AddTrack('audio') failed — could not create the VO track.` / `… the track count did not grow.` | Resolve refused to add the narration's own audio track to the new timeline | check the timeline is not locked / a modal is not open; retry with a new `timeline_name` |
| `Could not add VO clip 'X' to the timeline (track N): Resolve placed nothing at the pinned frame or at the track tail.` | the VO track was re-read after both attempts and the take is not there — this server never counts a placement it cannot see | check the VO clip opens in Resolve; retry with a new `timeline_name` |
| `Target project 'X' is not the open project (…).` | a **real** `build_timeline(…, project=X)` while some other project is open — the build is refused rather than laid into the wrong project | open X (or `create_project` it), or drop `project` to build into the open one |
| `Clips not in the media pool (run import_package first): …` | the timeline build reads clips from the VIDEOS bin and they are not there | run `import_package`; a `build_timeline(dry_run=True)` shows exactly which are missing |
| `Package file missing on disk: …` | a file the manifest names is gone (drive not mounted, package moved) | mount the drive / restore the package; export again from the studio |
| `Resolve laid N clip(s) on V1 but M were sent — timeline 'X' is incomplete` | Resolve dropped a clip during the append (an unreadable or offline source is the usual cause). The build stops rather than stamping shot markers onto the wrong pictures and reporting success | check every file opens in Resolve, then rebuild with a new `timeline_name` |
| `Could not make 'X' the current timeline — refusing to build into whichever timeline is open instead` | Resolve refused the timeline switch. The build refuses rather than appending into whatever was current — a template project ships with timelines of its own | close any modal, retry with a new `timeline_name` |
| `Resolve imported fewer files than the package names (...)` | `ImportMedia` returned fewer items than paths given | check the media opens in Resolve (codec, drive mounted), then re-run `import_package` |
| `Two package files share the clip name 'X'` | two manifest entries have the same basename — Resolve matches clips by name alone, so one would silently stand in for the other | re-export from the studio (its exporter numbers colliding names) |
| `Manifest entry 'X' is an absolute path` / `resolves outside the package directory` | a hand-built or malformed manifest points outside its own package | make every `file` relative to the manifest |
| `Refusing to save 'Untitled Project'` | `save_project` on the default project — it has no location, the call cannot succeed, and headless it blocks indefinitely | `create_project` first; the pipeline always names its project |
| `Manifest not found` / `Not an oside-davinci/v1 manifest` | wrong path, or not a package this server understands | point at the package's `manifest.json`; compare `resolve_status().capabilities.manifest` |
