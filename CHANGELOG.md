# Changelog

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
