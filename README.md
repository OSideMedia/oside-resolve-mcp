[![version](https://img.shields.io/badge/version-0.7.0-blue)](CHANGELOG.md)
[![license](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![DaVinci Resolve](https://img.shields.io/badge/DaVinci%20Resolve-Studio%2021.1%20(measured)-e8552d)](https://www.blackmagicdesign.com/products/davinciresolve)
[![platform](https://img.shields.io/badge/platform-MCP%20%7C%20Claude%20Code-8a3ffc)](https://modelcontextprotocol.io)

# OSIDE Resolve MCP

An MCP server that turns a folder of clips and a JSON manifest into a finished
**DaVinci Resolve** project — and then checks its own work.

Point it at a package and you get: a new project from your template, the media
imported into its bins, every clip on V1 **in shot order**, a named marker on
each shot, narration laid on its own audio lanes at the right frames, an
optional colour balance on every clip — and an **acceptance gate** that reports
`PASS` or `FAIL` per check rather than assuming any of it worked.

## What it does

- **Builds the project** from your own `.drp` template, so settings and the bin
  tree are yours, not this tool's.
- **Imports the package** into the right bins, by name.
- **Lays the timeline** — clips in shot order on V1, one marker per shot
  carrying its number and description.
- **Places narration on its own lanes** — board-wide takes on `VO`, takes pinned
  to a shot on `VO PINS`, at that shot's exact frame. Never on A1, which holds
  the clips' own audio.
- **Applies a starting colour balance** (ASC CDL on node 1) when the package
  carries a look — a matched starting point for the colourist, never a grade.
- **Verifies the result and can fail.** Clip order, stray clips, every pinned
  take on its frame at zero tolerance, a marker per shot naming its own shot.
  A build that went wrong is reported as `FAIL`, not as success.

It **never deletes, never overwrites, and never renders.** An existing project
or timeline name is a hard refusal.

## Do I need OSIDE to use this?

**No.** The input is a plain folder plus a JSON manifest — nothing proprietary,
no service to sign up for, no network calls. OSIDE Studio happens to write that
manifest, but anything can: a script, another editor, your own tool.

A minimal `oside-davinci/v1` package looks like this:

```
my-package/
  manifest.json
  videos/shot1.mp4
  videos/shot2.mp4
  vo/narration.mp3
```

```json
{
  "format": "oside-davinci/v1",
  "kind": "cinematic",
  "project": { "name": "MY FILM" },
  "videos": [
    { "file": "videos/shot1.mp4", "bin": "VIDEOS", "sceneIndex": 1, "shotIndex": 1,
      "shotNumber": "1A", "description": "Ada enters the diner." },
    { "file": "videos/shot2.mp4", "bin": "VIDEOS", "sceneIndex": 1, "shotIndex": 2,
      "shotNumber": "2",  "description": "Two-shot at the counter." }
  ],
  "audio": [
    { "file": "vo/narration.mp3", "bin": "VO", "label": "Narrator",
      "durationSeconds": 12.0, "placements": [] }
  ]
}
```

`placements: []` means a board-wide take (laid at the head of `VO`). To pin a
take under one shot, give it the shot it belongs to and it rides `VO PINS` on
that shot's first frame:

```json
"placements": [{ "sceneIndex": 1, "shotIndex": 2, "shotNumber": "2",
                 "videoFile": "videos/shot2.mp4" }]
```

Everything else — dialogue cues, in-frame text worklists, the look block — is
optional and described under [The tools](#the-tools).

## Requirements

- **DaVinci Resolve Studio**, running. The free edition has no external
  scripting and will never answer.
- Preferences → System → General → **External scripting using: Local**.
- Python 3.10+.
- `ffprobe` on `PATH` is optional — it makes offline dry runs more accurate.

## Install

```bash
git clone https://github.com/OSideMedia/oside-resolve-mcp
cd oside-resolve-mcp
python3 -m venv .venv && .venv/bin/python -m pip install -e .
```

Register it with your MCP client — for Claude Code, in `.mcp.json`:

```json
{
  "mcpServers": {
    "oside-resolve": {
      "command": "/absolute/path/to/oside-resolve-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/oside-resolve-mcp/server.py"]
    }
  }
}
```

**Then snapshot your templates once**, with Resolve open. This server builds
projects from *your* `.drp`, so it needs one per `kind` in
`templates/templates.json`:

```
export_template("cinematic")     # or, in Resolve: right-click the template
export_template("explainer")     # project → Export Project → save to templates/
```

Verify the install with `resolve_status()` — it answers even when Resolve is
unreachable, and says why.

## Quick start

```
resolve_status()                                   # preconditions + capabilities
build_timeline("…/manifest.json", dry_run=True,    # the plan, before anything exists
               project="MY FILM")                  # touches nothing
create_project("cinematic", "MY FILM")
import_package("…/manifest.json")
build_timeline("…/manifest.json")                  # V1 + VO lanes + markers
save_project()                                     # the verification boundary
verify_import("…/manifest.json")                   # PASS | FAIL, per check
```

**Always dry-run first.** `dry_run=True` returns the whole plan — clip order,
what is on disk, where every VO take would land, every marker — and writes
nothing. It works with Resolve closed.

**Save before you verify.** An errored append's placements survive every
in-session read and are discarded by the save, so a gate run before the save can
pass over work that was never written.

Or run the whole thing as one command:

```bash
.venv/bin/python pipeline.py path/to/manifest.json --name "MY FILM"
.venv/bin/python pipeline.py path/to/manifest.json --dry-run    # touches nothing
```

It prints a JSON report and exits non-zero unless the gate passes.

## Working alongside Blackmagic's own MCP

This is deliberately NOT general Resolve remote control. **Blackmagic ships its
own MCP server** with Studio 21.1, inside the app bundle at
`DaVinci Resolve.app/Contents/Applications/ResolveMCP`: arbitrary `run_script`
against the scripting API, plus `search_scripting_api` / `get_scripting_api` /
`get_whats_new` — version-matched to the Resolve you are running, and so the
owning source for call SHAPES.

The two do different jobs. Reach for Blackmagic's for API lookup, the changelog
past your training cutoff, and ad-hoc read-only probing. Reach for THIS one for
the handoff — a fixed pipeline gets fixed tools so it runs identically every
time, and the acceptance gate is the part a general tool cannot give you. The
first-party stubs document call shapes but none of the BEHAVIOURAL lies this
server is built around (`AppendToTimeline` still promises "the list of appended
timelineItems" over a silent drop), so ANTI-PATTERNS in `CLAUDE.md` still earns
its keep. While a build is running, treat the general server as read-only: two
writers, one Resolve.

[samuelgursky/davinci-resolve-mcp](https://github.com/samuelgursky/davinci-resolve-mcp)
(MIT) predates both, and its connection pattern informed `resolve_api.py`.

## The tools

The full surface, with the behaviour each one guarantees.

1. `create_project(kind, name)` — new project from the right template snapshot
   (`cinematic` → YT-4K 23.976fps, `explainer` → YT-4K 60fps). Settings and the
   whole bin tree (O-Side Media / VIDEOS / VO / …) come from the `.drp`.
2. `import_package(manifest_path)` — videos into the VIDEOS bin, narration
   mp3s into the VO bin.
3. `build_timeline(manifest_path, timeline_name="EDIT 01", dry_run=False,
   cues=True, project=None)` — clips in shot order on V1, one Blue point
   marker per shot carrying its number + description (+ VO line for
   explainers), and narration on **its own audio lanes — never A1**, which
   carries the shot clips' OWN audio: Seedance generates audio and video in one
   pass and OSIDE directs it, so A1 holds the dialogue, SFX and ambience the
   director specified. Narration is separate work and gets its own lanes, one
   per DOOR: a board-wide take (`placements: []` — the recorded spine, or a
   board narration) rides **`VO`** at the head, and a take **pinned** to a shot
   rides **`VO PINS`** on that shot's own frame. The two overlap in time — a
   spine is often as long as the finished piece — so a single track cannot hold
   both: laid together the spine went down first and every pin was refused to
   the tail, which failed verify on any package using both doors [measured
   2026-09-08]. Pins that collide with EACH OTHER (a long take pinned to a
   short shot spilling into the next pin) checkerboard onto `VO PINS 2`,
   `VO PINS 3`… — never a slide to the tail. And — when the
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
  Since 0.5.3+ (step e) it also writes `verify.json` beside the manifest — one row per clip by `generationId`
  (`placed`, `start`, `duration`), `overall`, `verifiedAt`; OSIDE's build door reads it back onto the generation row.
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

5. `snapshot_timeline(out_path, timeline_name=None)` (0.7.0) — export a
   timeline (the current one by default) as **OTIO** to a NEW file, and return
   a per-track summary (`kind`, `index`, `name`, clip count) with the rate and
   the start/end frames. Read-only on the project; refuses an existing file.
   This is what a spotting pass reads: which piece of picture sits where, what
   covers it, which sound lanes are already busy.
6. `place_sfx(plan_path, dry_run=False, resume=False)` (0.7.0) — lay a spotted sound-effects
   pass onto **one named audio track of an existing timeline**, from an
   `oside-sfx-plan/v1` file:

   ```json
   {"schema": "oside-sfx-plan/v1", "timeline": "YT", "track": "SFX 3", "bin": "SFX",
    "placements": [{"file": "/abs/path/check.wav", "sourceIn": 0, "sourceOut": 1.633,
                    "record": 609, "label": "part 1 — check", "kind": "CHECK"}]}
   ```

   `record` is a timeline frame relative to the timeline's start; trims are
   **seconds**, because the frame unit is the pool item's own rate and Resolve
   picks that at import (a WAV can arrive tagged 25 fps on a 60 fps project).
   The guarantees, in order: refuses unless the plan's timeline is the
   **current** one (it never switches your editor), the track exists exactly
   once and the bin exists — it never creates tracks or bins; judges every
   collision — with the track, with other rows, past the timeline's end, past
   the end of the file, a trim shorter than a frame — from **one read before
   any write**, so a plan refused there writes nothing; imports what the pool
   lacks (matched by `File Path`, never by clip name), puts your current bin
   back even when the import fails — a failed import is a `failed` outcome
   with `binRestored`, never an exception that hides the bin — and
   judges the plan again at the rate the pool gave each file — a refusal at
   that point **has imported** into the bin, and the report lists `imported`;
   exports the timeline and refuses unless the export and the track read
   agree; then appends **one clip at a time and re-reads the track** after
   each — the new item must be the right file at its frame with its length and
   its trim, every item already there must be unchanged, and the first
   mismatch stops the run (no retry, no delete: the stray is reported); then
   exports again and proves **every other track is identical**, every clip and
   transition the target had is still there unchanged with nothing new beside
   what was laid, and the timeline end did not move (`diff.checks`). A track or
   item that does not answer is an error, never an empty lane. A clip already
   at its frame with its file, length and trim reads `alreadyPlaced`, so a
   re-run is a no-op. Every run that lays a clip — or finds the plan on the
   track with no record of it there — is recorded beside the plan in
   **`<plan>.placed.json`** (project, timeline, track, when). A **partly**
   placed plan is refused unless `resume=True`, and so is a plan that ledger
   says was laid **anywhere** with rows now missing here — which catches a
   pass the editor deleted *whole*, where nothing on the track says it ever
   ran, and the same pass after a project, timeline or track rename, which
   reads like somewhere new. Rows missing from a track the editor has worked
   on were most likely deleted on purpose, and re-running would put them
   back — `resume` is for finishing a run that stopped, or for laying the plan
   on a second timeline on purpose; moving the ledger aside makes the plan
   new again. A ledger entry that is not a whole record is an error, never
   "no history".
   `dry_run=True` touches nothing and returns `toPlace`, `alreadyPlaced`,
   `partial`, `laidBefore`, `laidElsewhere`, `wouldImport` and `collisions`.
   **It never saves** — `save_project`, then the same plan with
   `dry_run=True`: every row should come back `alreadyPlaced`.
   Any producer that writes the schema can use it; a spotting pass that reads
   the motion-graphics source and a `snapshot_timeline` export is the intended one.

Plus `resolve_status` (with a `capabilities` block — `{"manifest":
"oside-davinci/v1", "features": ["placements","cues","dry_run","verify_v2",
"vo_track","look","text_tasks", …, "sfx_plan"], "sfxPlan": "oside-sfx-plan/v1", "version"}` — returned even when Resolve is unreachable, so OSIDE can compare
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

## The full recipe, end to end

The quick start above is the short path. This is everything, including the look
step and the Depth Converter hand-off, as the studio's **Build in Resolve**
button runs it.


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
| `manifest.project must be an object with a `name`` | `project` is a string or a number — a hand edit, or a producer that is not the studio | re-export the package; the studio writes `project: {id, name, …}` |
| `Unknown kind 'X' — templates.json knows …` | the manifest's `kind` has no template; before 2026-09-06 a dry run blessed it and only `create_project` refused | use a listed kind, or add a template (`export_template`) and its `templates.json` row |
| `manifest.videos must be a list of entries` (also `audio`) | the key holds an object or a string instead of a list | `videos` and `audio` are lists of `{file, bin, …}` entries |
| `manifest.videos[i] must be an entry object` / `has no `file`` | an entry is a bare string, or lost its `file` key | every entry is `{file, bin, …}` with a relative `file` |
| `manifest.cues must be the worklist's filename` (also `textTasks`) | the key holds a list or object instead of a CSV filename — before 2026-09-06 that dropped the whole worklist silently and the gate expected zero markers | point the key at the CSV the studio wrote beside the manifest |
| `manifest.look must be an object` | the look block is a string or a list — a hand edit | the studio writes `look: {schema, hex, shares, targets, …}`; drop the key or re-export |
| `manifest.look.schema 'X' is not oside-look/1` | the package carries a newer look block than this MCP reads | update oside-resolve-mcp, or re-export from a studio build that writes v1 |
| `pipeline crashed before a report could be built: …` | an exception past the manifest load — the report still prints, with the Python error in `error` | read the error; if it names a manifest key, it is a shape this table does not know yet — file it |
| `Refusing to save 'Untitled Project'` | `save_project` on the default project — it has no location, the call cannot succeed, and headless it blocks indefinitely | `create_project` first; the pipeline always names its project |
| `The current timeline is 'X', not 'Y'.` | `place_sfx` lays into the CURRENT timeline only, and will not switch your editor | open the plan's timeline in Resolve, then run the plan again |
| `Expected exactly one audio track named 'X' …, found N.` | the plan names a track that is missing, or two tracks share the name | add or rename the track in Resolve — `place_sfx` never creates or renames tracks |
| `No bin named 'X'. place_sfx imports into an EXISTING bin only.` | the plan's `bin` is not in the media pool (often a typo) | fix the plan, or create the bin in Resolve |
| `outcome: refused` with `collisions` | a row overlaps a clip already on the track, another row, the timeline's end, or the end of its file, or trims to under a frame — judged before any write, so nothing was placed | move or trim the rows it names; nothing to undo |
| `… (the same file at the same frame, with a different trim or length)` | that row's sound is on the track at its frame but cut differently — an editor's trim, or a different plan | leave it if the editor trimmed it; otherwise remove it in Resolve and re-run |
| `refused AFTER importing N file(s) into 'BIN'` | a row only collides at the rate the pool gave its file, which no read can know before the import | the files stay in the bin (harmless); fix the rows the collisions name and re-run |
| `outcome: refused` — `… rows are already on the track and … are not` | the plan is partly on the track: a run that stopped, or an editor who deleted some of the cues | if the editor cut them, leave it — that is the edit. To finish a run that stopped, pass `resume=True` |
| `outcome: refused` — `This plan was laid here on … and k of its n rows are not on the track now` | the ledger `<plan>.placed.json` says this plan ran on this project, timeline and track, and rows are gone — often the editor deleted the whole pass | leave it if that was the edit. To lay it again on purpose, pass `resume=True`, or move the ledger aside |
| `outcome: refused` — `This plan was laid on '…', timeline …, track … on … — not here` | the ledger has this plan on another project, timeline or track. A rename since reads exactly like this; so does a second timeline | after a rename, the pass was probably deleted on purpose — leave it. To lay it here as well, pass `resume=True` (`laidElsewhere` in the dry run lists where it went) |
| `<plan>.placed.json does not read` / `is not an oside-sfx-ledger/v1 ledger` / `entry N is not a whole record` | the ledger was hand-edited or truncated; read as "never laid" it could re-lay a deleted pass | move it aside only if you mean to lay the plan as new |
| `ledgerWritten: false` (with `ledgerError`) | the clips were laid, but the plan's folder would not take the ledger | re-run the plan once the folder takes files, before anyone edits that track: a run that finds the rows already on the track records them. Fixing the permissions alone records nothing |
| `outcome: failed` — `the import failed (…) — nothing was laid` / `Resolve did not import …` | the import raised or left files out; `binRestored` says whether your open bin came back | check the files exist and the bin is writable; if `binRestored` is false, reselect your bin in the media pool |
| `could not read which bin is open` / `could not reselect the bin you had open` | the media pool would not report, or would not restore, your current bin — nothing was laid | reselect your bin in the media pool and re-run |
| `… did not answer with its items` / `did not report its name, start or length` / `the track's item list and its export disagree` | before any append: a read of the target track could not be trusted, so collisions cannot be judged — nothing was laid | close any modal in Resolve and re-run; if it persists, `snapshot_timeline` and look at the track |
| `outcome: failed` — `the track could not be read back: …` | the same unreadable track, but AFTER an append — `placed` lists what was laid before it | check that row in Resolve, then re-run with `resume=True`; laid rows read `alreadyPlaced` |
| `outcome: failed` with `failure.landed` | Resolve laid a clip somewhere other than its frame, length or trim (or not at all); the run stopped there | remove the item `landed` names in Resolve, then re-run — re-run with `resume=True` — the rows already laid read `alreadyPlaced` |
| `no before-snapshot, so nothing was laid: Timeline.Export (OTIO) answered … and wrote 0 bytes` | no before-snapshot to judge with | check the disk has room and no modal is open in Resolve; retry |
| `laid N clip(s) but the after-snapshot failed, so nothing is proven` | the clips ARE on the track, but the after-export failed, so nothing proves the other tracks are untouched | check the other tracks by eye or with `snapshot_timeline`; the laid rows read `alreadyPlaced` on a re-run |
| `Manifest not found` / `Not an oside-davinci/v1 manifest` | wrong path, or not a package this server understands | point at the package's `manifest.json`; compare `resolve_status().capabilities.manifest` |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Two things worth knowing before you
open a PR: the twelve tools are deliberately few (a fixed pipeline gets fixed
tools), and the ANTI-PATTERNS in `CLAUDE.md` are load-bearing — each was bought
with a live walk, and each looks wrong until you have hit it.

## Licence

MIT — see [LICENSE](LICENSE).

DaVinci Resolve is a trademark of Blackmagic Design Pty Ltd. This project is an
independent tool and is **not affiliated with, endorsed by, or supported by
Blackmagic Design**. It drives Resolve through the public scripting API that
Resolve Studio exposes.
