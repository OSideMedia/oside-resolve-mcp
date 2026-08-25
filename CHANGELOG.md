# Changelog

## 0.5.0 — 2026-08-24

**The plan is not the witness.** A three-seat council audit (Codex, Opus,
Gemini) plus a cherry-pick pass over `samuelgursky/davinci-resolve-mcp` found
one defect shape running through the whole bridge: the server reported on its
INTENTIONS rather than on what happened, and the test fake was written to match
the code rather than the API, so none of it could go red. Every fix below ships
with a test that fails against 0.4.0.

### The blocker

- **`SetCurrentTimeline` was called with a string; the API takes a Timeline
  object** (Blackmagic's own reference: `SetCurrentTimeline(timeline) --> Bool`).
  On a string the call is a no-op, and the build then appended into WHATEVER
  TIMELINE WAS CURRENT — and both template snapshots ship with four timelines of
  their own, so this could silently edit a template, the one thing the
  create-only guardrail exists to forbid. Live walks passed only because Resolve
  21.0.4.5 happens to make a newly created timeline current, which is documented
  nowhere. We now pass the object we already hold and refuse the build if the
  switch does not take. **The test fake accepted a string too** — it has been
  corrected to the vendor contract, which is what makes this assertion capable
  of failing at all.

### The gate was wrong in both directions

- **It never bound a marker to the shot it names.** `shot marker positions`
  compared two sorted frame lists, so rotating every shot marker onto a
  different clip returned `overall: PASS`. New row: `shot markers name their own
  shot`. Untagged pre-0.2 Blue markers stay exempt, as the README promises.
- **It failed builds that behaved correctly.** `expected` was the raw worklist
  row count, so a cue or text task the planner deliberately refused (no room
  inside its own shot) scored as a failure and `pipeline.py` exited 1 over a
  correct project. `expected` is now what the build INTENDED to place, with
  `skippedByDesign` alongside it.
- **Three shipped features had no gate row at all**: the film-stock marker, the
  look, and the VO track itself. Narration back on A1 — the exact 2026-08-16
  defect `vo_track` was built to prevent — used to PASS. The canonical
  `_good_timeline()` fixture put VO on A1 and asserted PASS, which is why nobody
  noticed; it now models a correct build.
- **A named-but-missing worklist is surfaced.** `load_cues` returning
  `([], reason)` must not block a build; reporting PASS without mentioning it
  turned a half-copied package into a clean handoff.

### Markers on the wrong picture

- **Dialogue cues cascade with a clip-boundary guard**, the one
  `text_task_rows` has enforced since 0.4.0 and whose reasoning this changelog
  already recorded: *a marker on the wrong picture is worse than no marker*. Two
  ordinary lines under a five-second generated shot used to put cue 2 on the
  next shot and cue 3 past the end of the timeline, with `reason: None`.
- **The placement nudges no longer cross the boundary.** The guard lived in
  `handoff.py` and the transform in `resolve_api.py`: the 4- and 12-frame nudge
  windows knew nothing about the clip and walked correctly-planned markers onto
  the next shot anyway, reporting them `placed`. Rows now carry a `limit`.
- **A cue with no `Est s` reserves a nominal second**, not the whole shot —
  claiming the shot cascaded every following cue off the end of it and swallowed
  every text task on it.

### Honest returns

- `build_timeline` reconciles against the timeline it observes: a clip-count
  mismatch is refused rather than reported as success with marker counts taken
  from the plan, and `AddMarker`'s return is now checked (it was the only marker
  write in the repo that discarded it).
- `import_package` compares `imported` against `expected` instead of printing
  both under an unconditional `ok: true`.
- `apply_look` reports `complete`, and diagnoses a false `SetCDL` against
  `GetNodeGraph().GetNumNodes()` instead of returning a bare `applied: false`.
- VO rows for takes that never imported say so, instead of being stamped
  `track: "VO"` alongside the ones that were actually laid.
- `append_audio` accepts a placement Resolve honoured at a shifted frame rather
  than appending a second copy and then reporting that nothing was placed.

### Cross-repo and hardening

- **Mixed-rate sources are conformed.** Every OSIDE render is 24 fps and the
  explainer template is 60, so the CONNECTED dry run read 121 source frames
  where the clip occupies 303 — every explainer plan was short by 182 frames per
  clip, and every cue frame derived from those starts was wrong with it. The
  offline path was always correct, which made the connected plan the less
  accurate of the two.
- **The CDL envelope is enforced**, not just promised in prose: slope/power
  0.5–2.0, |offset| ≤ 0.25, saturation 0.5–1.5, each vector exactly three finite
  numbers. A clip outside it is refused BY NAME instead of applied as if it were
  a starting balance, and a malformed entry no longer raises `KeyError` and
  takes the whole run down. `apply_look` now also echoes Depth Converter's own
  `derivation` line — the file said "HEURISTIC v1.1, gains fitted once" and this
  tool was quietly dropping that caveat.
- **Manifest entries are checked at the door**: no absolute paths, nothing
  resolving outside the package, no two files sharing a clip name. Resolve
  matches clips by basename alone, and we depended on an invariant only OSIDE's
  exporter enforced.
- `create_project` reconciles the project's real frame rate against the one
  `templates.json` declares (they agree today; nothing was checking).

### From the cherry-pick pass

- **`save_project` — the save is the verification boundary.** Placements from an
  errored append are visible to every in-session read and are discarded by the
  save (measured elsewhere at 573 items before, 500 after). Our re-read doctrine
  is a pre-save witness derived from the same unsaved state as the thing it
  checks, so it cannot see this class at all. `pipeline.py` now saves between
  build and verify. Guarded on a named project: on `Untitled Project`
  `SaveProject` returns False in the GUI and blocks forever headless.
- **`pipeline.py` applies the look.** The one-click Build-in-Resolve path never
  called `apply_look`, so the grade travelled when an agent drove the handoff
  and silently did not when the director pressed the button. A refusal (no look
  block, no measured CDL) is still not a failure; a partial application is.
- `resolve_status` reports `GetProductName()` (free vs Studio) and
  `GetCurrentDatabase()` — the only honest liveness check, since a wedged
  Resolve answers every cheaper probe normally while `LoadProject` fails forever.
- **Doc correction:** "Resolve has no CDL getter" is true of `GetCDL` and was
  being read as "no read-back is possible". `Timeline.Export` with
  `EXPORT_EDL`+`EXPORT_CDL` (or `EXPORT_ALE_CDL`) carries applied CDLs out of
  Resolve. Reworded; no code change until that path is walked live.

### Removed

- `video_start_frames` — dead (grep found only its definition) and it returned
  ABSOLUTE frames where every sibling returns timeline-relative ones, so the
  next caller to reach for it would have laid everything an hour into the
  timeline.

### Measured on the live walk (Resolve 21.0.4.5, 2026-08-24)

Four walks against a real Resolve with a real 24 fps package. Facts, not
inferences — each one settles something the fake could not.

- **`SetCurrentTimeline("name")` returns False. With the Timeline object it
  returns True.** The blocker was real, not theoretical. It never fired only
  because `CreateEmptyTimeline` DOES make the new timeline current on its own
  (also measured) — and the template ships with `YT` and `YT Shorts`, with `YT`
  current, so that undocumented behaviour was the only thing standing between a
  build and a template timeline.
- **The fps conform is confirmed.** On the 60 fps explainer template a
  5.041667 s / 24 fps source lands at frames 0, 302, 604 with duration 302 —
  Resolve conforms by DURATION. The pre-0.5.0 arithmetic would have planned
  121, off by 181 frames per clip.
- **Clip frames FLOOR.** 120 on 23.976 and 302 on 60, where rounding gave
  121/303. `seconds_to_clip_frames` now floors, so the dry run predicts exactly
  what gets built on both templates (verified against both walks). Cue
  durations still round.
- **The CDL read-back WORKS.** `Timeline.Export(path, EXPORT_EDL, EXPORT_CDL)`
  returned our exact applied values — `*ASC_SOP (1.0 1.0 1.0)(-0.025 -0.02
  -0.015)(1.0 1.0 1.0)` / `*ASC_SAT 0.940000`. So there IS a read-back path for
  the look; the README's caveat is now scoped to `GetCDL` alone rather than
  implying no read-back exists. Wiring it into `apply_look` is a follow-up.
- **`GetStartFrame()` is 0 on both templates**, so the absolute-vs-relative
  marker question is moot for our packages — the subtraction is a no-op in
  practice, and the fake's 86400 start is the more conservative case.
- **The save discarded nothing** on these append shapes (3 clips in, 3 after
  the save, on every walk). `save_project` stays: it is cheap, and the class it
  guards against is one no in-session read can see.
- **`AddMarker`'s return value is reliable** — False on an occupied frame, True
  on a free one, including past the end of the timeline. An earlier draft of
  this release recorded a "measured false-negative"; that was a bug in my own
  walk harness (a dict comprehension over `.get()` called `apply_look` eight
  times), not a Resolve defect, and the false claim has been removed from the
  source rather than left to mislead the next reader.

### Found by the walk

- **`apply_look` was not idempotent.** The CDL half was — absolute values, a
  re-run resets node 1 — but the film-stock marker was added AGAIN on every
  run, so re-applying a look three times left three `Stock intent` markers a few
  frames apart. It now returns the existing marker's frame with
  `alreadyPresent: true` and adds nothing. The new `film-stock marker` gate row
  is what caught it, on its first live run.
- **Marker collisions are resolved from ONE read** (`first_free_frame` over a
  set) instead of by trying writes until one sticks. Retrying writes is the
  shape that turns a repeated call into a pile of markers.

`capabilities.features` gains `verify_v3`, `post_save` and `cdl_envelope`.

## 0.4.0 — 2026-08-17

**In-frame text arrives as a worklist on the timeline.**

- New manifest key `textTasks` names a `text-tasks.csv` (Scene, Shot, Text,
  Where it lands, File, Outcome) written by OSIDE v0.160.0. Each row becomes
  ONE point marker on the shot that asked for the lettering, carrying the exact
  wording and the reason it is a task rather than a rendered sign: video models
  re-spell text between takes, so a sign that changes across two shots of one
  scene is a continuity break no re-roll fixes. The studio's answer is to
  describe the surface, leave the words out of the generation, and lay the real
  text over the clip here.
- **Placed after that shot's cues**, computed from the cue rows already built
  rather than a fixed offset. Resolve keeps one marker per frame; shot 1's Blue
  marker owns frame 0 and the dialogue cues cascade from frame 1, so a fixed
  offset would fight them on any shot carrying dialogue. Three tests go red
  against the naive version.
- **A task that cannot fit inside its own clip is REPORTED, not nudged onto the
  next shot** — a marker on the wrong picture would tell the editor to title the
  wrong shot, which is worse than no marker.
- `verify_import` counts them by tag (`oside:text`), and the check appears ONLY
  when the package carries a worklist — a package exported before OSIDE wrote
  them still passes rather than gaining a row asserting zero.
- Rides the existing `cues` switch: both are marker worklists on one timeline,
  and a director who turned markers off meant all of them.
- `capabilities.features` gains `text_tasks`, so OSIDE can check before relying
  on it. An older server ignores the manifest key exactly as it always did.

## 0.3.1 — 2026-08-16

**Film stock travels as INTENT, never as a grade.**

- `apply_look` now reads the manifest look block's optional `stockIntent`
  (additive on `oside-look/1`; older packages simply do not have it) and writes
  ONE timeline marker at the head — `Stock intent: <label> (<balance>) —
  colourist's call, nothing applied`, plus what OSIDE actually measured that
  stock doing. It adds **no node, no LUT and no Film Look Creator preset**: a
  film stock IS a creative grade, and this tool sells a starting balance.
- The marker nudges past shot 1's Blue marker (Resolve keeps one marker per
  frame) and REPORTS the frame it landed on; a head-dense timeline that leaves
  no room is reported as skipped, never silently dropped. `dry_run` shows the
  note before Resolve is touched. Tagged `oside:stock` in customData — the
  colour palette is fully spoken for, so the tag is the discriminator.
- A malformed or half-filled `stockIntent` reads as "no stock" rather than
  putting a broken marker on a colourist's timeline. 4 new tests (28 total).

Earned by OSIDE's two film-stock A/Bs (64 cells, $5.76,
`DOCS/livefire/LIVEFIRE-2026-08-16-FILM-STOCK-AB{,-CONFIRM}`): naming a stock
measurably moves the IMAGE MODEL, which is why the words ship — and it is
still the colourist's call what happens in Resolve.

## 0.3.0 — 2026-08-16

**"Look travels"** — the film's starting balance rides into Resolve.

- New tool `apply_look(manifest_path, timeline_name=None, dry_run=False)`:
  when the manifest carries a `look` block (OSIDE writes one from the project's
  Style Constant — colour-law hexes + words + derived L*/b*/C* targets), it
  reads the `look-cdl.json` Depth Converter measured beside the manifest
  (`depthc look-compare --manifest --emit-cdl`) and sets each clip's ASC CDL on
  node 1 of its V1 item. Key, temperature, saturation only — never palette
  content, never a creative grade. Idempotent; refuses (and says why) without a
  look block or CDL file; per-clip rows + `missing` + `extra`; `dry_run` plan.
- Capability feature `look` in `resolve_status().capabilities.features`; recipe
  step 3b in the `handoff` prompt (never blocks the gate).
- Live-proved 2026-08-16 (Resolve Studio, throwaway project deleted after):
  8 clips, 8/8 applied, re-run idempotent; the render re-scored by Depth
  Converter went from ΔL* −7…+9.5 to ±0.4 and Δb* to ±1.7 against the look with
  palette distance unchanged (9.9 → 9.6) — which is the whole promise.
- Pure half (`handoff.load_look_cdl`, `plan_look`, `cdl_payload`) offline-
  tested; 24 tests.

## 0.2.1 — 2026-08-16

**VO on its own track** — Resolve's `AppendToTimeline` returns truthy on a
silent drop; found by the v0.2.0 gate on the first live walk (Resolve Studio
21.0.4.5, project RAID-WALK-2026-08-16).

- Every shot clip carried embedded audio (Seedance 2.x default), so appending
  the video filled A1; `append_audio` then asked for `trackIndex: 1` at an
  occupied frame, got `[<PyRemoteObject>]` back — truthy — while Resolve placed
  NOTHING. `build_timeline` reported `voUnderShot 1 / voAtHead 1` over a
  timeline with no VO; `verify_import` (v0.2.0) caught it (`VO … found: null`).
- Fix: narration is laid on ITS OWN audio track named `VO` (`AddTrack` +
  `SetTrackName`; an existing `VO` track is reused; never A1). Every placement
  is judged by RE-READING the track after the append (a new item, by name, at
  the frame asked for) — the return value is never trusted; the "loose"
  fallback lands at the VO track's tail, and a take that still is not there is
  an error, never a counted clip. `build_timeline` reports `voTrack` /
  `voTrackIndex`; every plan/dry-run VO row says `track: "VO"`.
- `observe_timeline` reads EVERY audio track (`audioTracks: [{index, name,
  items}]`, `a1` kept); `verify_import`'s VO rows look on all tracks and name
  the `track` each take sits on (`"A2 VO"`).
- `clip_duration_frames` reads audio items: their `Frames` is `''`; the length
  now comes from the `Duration` timecode at the item's `FPS`
  (`00:00:05:01` @ 23.976 = 121), else ffprobe of `File Path`. `endFrame` is
  EXCLUSIVE (measured: a 47-frame clip with `endFrame 47` lands as 47 frames,
  46 as 46) — the old `end - 1` shaved a frame.
- **Project-aware dry run.** `pipeline.py --dry-run` plans BEFORE
  create/import, so with Resolve open on another project every file read
  `missing where:"bin"`, `wouldBuild false`, exit 1, and fps came off the
  wrong project. `build_timeline(…, dry_run=True, project=NAME)` (pipeline
  passes `--name`): when NAME is not the open project, bin presence is
  `"unknown — not imported yet"` (or `"… exists but is not open"`), never
  missing; `wouldBuild` is judged on disk; fps comes from the kind's template
  (`templates.json` now carries `fps`); the plan carries `targetProject`.
  A REAL build with a `project` that is not the open one is refused.
- `capabilities.features` gains `vo_track`. `handoff` prompt + README recipe:
  dry run first (before `create_project`), VO on its own track.
- Self-test: 20 checks (was 14) — the fake Resolve now fills A1 with embedded
  audio and answers truthy-but-empty on an occupied frame (red on 0.2.0,
  green on the fix), VO-track placement + verify, silent-drop = error, dry
  run before create → unknown not missing, audio timecode lengths.
  `OSIDE_RESOLVE_OFFLINE=1` keeps offline cases offline beside a live Resolve.

## 0.2.0 — 2026-08-16

Four cherry-picks — pattern from OpenChatCut (AGPL), restated in our own
words; no code or prose copied.

- **`build_timeline(…, dry_run=True)`** — plan before touching Resolve: ordered
  clip list (on disk / in bin), VO placement per take (`underShot` / `atHead`,
  target shot, start frame), shot + cue markers, `missing`, `wouldBuild`. Same
  shape as the real call (`plan` on both). Works with Resolve closed (bin
  presence reported unknown; lengths via ffprobe or beat timing). Also
  `pipeline.py --dry-run`.
- **`verify_import` is a real acceptance gate** — per-check table
  `checks: [{check, expected, found, pass}]` + `overall: PASS|FAIL`: bins, V1
  count, V1 order == manifest order, no strays, pinned VO on its shot's first
  frame (0-frame tolerance, delta reported), shot markers count + positions,
  cue markers count. Old `clean` / `report` fields kept; `clean` = PASS.
  Optional `timeline_name` (no switching) and `cues` flag.
- **Dialogue cues → range markers** — `manifest.cues` names the studio's
  `dialogue-cues.csv`; each scripted line becomes a range marker under its shot
  (name = speaker, note = line, length = estimate or the shot, colour per
  speaker, never Blue). Opt-out `cues=False`. Markers carry `customData`
  tags (`oside:shot`, `oside:cue`); a skipped cue is reported, never dropped.
- **MCP hygiene** — annotations on every tool; the `handoff` prompt;
  `resolve_status().capabilities` (`manifest`, `features`, `version`);
  README "Known errors" table.
- `handoff.py` holds the pure planner + gate; `tests/test_handoff.py` self-test
  (14 checks, fixture package + fake Resolve, no pytest needed).

## 0.1.0 — 2026-07-12 … 2026-08-07

Initial bridge: template copy, bin imports, shot-order timeline with markers,
VO under its pinned shot (2026-08-07), `pipeline.py` one-command handoff.
