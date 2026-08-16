# Changelog

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
