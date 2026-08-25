# oside-resolve-mcp

The OSIDE studio → DaVinci Resolve bridge. A small, deterministic MCP server
(Python, stdio, local only) that turns an `oside-davinci/v1` export package into
a Resolve project. Four modules: `resolve_api.py` (the Resolve surface),
`handoff.py` (the pure half — plan and gate, no Resolve imports), `server.py`
(the MCP tools), `pipeline.py` (the whole handoff as one command).

Product detail lives in `README.md`; what changed and why in `CHANGELOG.md`.

## Commands

```bash
.venv/bin/python tests/test_handoff.py      # the self-test — no Resolve, no pytest
.venv/bin/python pipeline.py <manifest.json> --dry-run --name NAME   # touches nothing
```

## What this is NOT

Not general Resolve remote control — [samuelgursky/davinci-resolve-mcp][sg]
(MIT) exists for that and is credited in the README. **A fixed pipeline gets
fixed tools so it runs identically every time.** Ten tools, one recipe. Every
proposal to add a tool "while we're here" is answered by that sentence.

Never deletes. Never overwrites a project or timeline name. Never renders —
renders are user-triggered in Resolve. Nothing listens on a network.

[sg]: https://github.com/samuelgursky/davinci-resolve-mcp

## ANTI-PATTERNS — each of these was paid for

Every line below was bought with a council audit or a live walk against Resolve
Studio 21.0.4.5. They are here because each one looks *wrong* to a reader coming
in fresh, and the obvious-looking "fix" is the bug. **Do not quietly correct any
of them. If one looks wrong, say so and leave it.**

1. **`SetCurrentTimeline` takes a Timeline OBJECT, never a name.** The vendor
   reference is explicit and a string returns `False` (measured). Passing a name
   is a no-op, and the build then appends into whatever timeline was current —
   the templates ship with `YT` and `YT Shorts`, so that is a template timeline.
   Do not "simplify" it back to a string because a name reads better.

2. **Never trust `AppendToTimeline`'s return.** It answers a truthy
   `[<PyRemoteObject>]` over a silent drop. `append_audio` re-reads the track
   after every append and judges placement by what appeared. Do not replace the
   re-read with the return value, however redundant it looks.

3. **Resolve collisions from ONE read; never retry writes.** `first_free_frame`
   searches a marker set read once. A loop that calls `AddMarker` until one
   "succeeds" is the shape that turns a repeated call into a pile of markers.

4. **`AddMarker`'s return IS reliable** — False on an occupied frame, True on a
   free one, including past the end of the timeline. An earlier comment here
   claimed a measured false-negative; that was a harness bug (a dict
   comprehension calling `apply_look` once per key), not Resolve. Do not
   re-introduce a workaround for a defect that does not exist.

5. **The save is the verification boundary.** Placements from an errored append
   are visible to every in-session read and are discarded by the save. A witness
   read from the same unsaved state as the thing it checks cannot contradict it.
   `pipeline.py` saves between build and verify. Do not move `verify_import`
   before `save_project` to "fail faster".

6. **`SaveProject` on `Untitled Project` blocks FOREVER headless.** The guard on
   the project name is not defensive noise.

7. **Clip frames FLOOR, and a mismatched source rate conforms by DURATION.**
   A 5.041667s/24fps source is 120 frames on 23.976 and 302 on 60 — rounding
   gives 121/303 and drifts a frame per clip. `seconds_to_clip_frames` floors on
   purpose; `seconds_to_frames` still rounds, and that asymmetry is deliberate
   (shaving a frame off every spoken line is the wrong direction for a marker).

8. **A marker on the wrong picture is worse than no marker.** Both cue and
   text-task rows carry a `limit` (the first frame past their own clip) and are
   REPORTED as skipped rather than nudged onto the next shot. The acceptance
   gate scores them against what the build *intended* to place, not against the
   raw CSV row count — a deliberate skip is a correct outcome, not a failure.

9. **There is no `GetCDL`.** `apply_look(verify=True)` reads the grade back out
   of a `Timeline.Export` EDL+CDL. That export carries no clip names — every
   event's reel is `AX` — so events match V1 items BY POSITION. `EXPORT_ALE_CDL`
   does not help: measured, it returns `True` and writes a zero-byte file.
   Check export SIZE, not existence.

10. **Narration is MONO by decision; stereo is for SFX and music.**
    `AddTrack("audio")` defaults to mono *silently*, so the subtype is requested
    explicitly and read back. Do not drop the argument because the default
    already matches — that is the accident, not the intent.

11. **The test fake implements the VENDOR contract, not our code.** It once
    accepted a string for `SetCurrentTimeline` because our code passed one,
    which is precisely why that bug survived every green run. When a fake and
    the vendor reference disagree, the fake is wrong.

## How to change this repo safely

- **Prove it red.** Every fix carries a test that FAILS against the pre-fix
  module — restore the old file, watch it fail, restore the fix. An assertion
  nobody has seen fail is a claim, not a check.
- **A guard is only as good as what it is allowed to see.** Before adding a
  check, ask whether its subject exists in the harness. The 0.5.0 import check
  was written against a fake with no `ImportMedia` at all.
- **Facts about Resolve get a date and a version.** "Measured 2026-08-25 on
  21.0.4.5" is the format. A vendor claim you have not reproduced is not a fact
  — say whose claim it is. A fabricated quirk in a comment outlives the session
  that invented it.
- The authority on the API is on this machine:
  `/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/README.txt`
  (~1150 lines). Probe it before guessing, and prefer it to any third-party
  docstring.

## Open product calls

- **Embedded audio on A1.** Measured 2026-08-25: `mediaType: 1` on the V1 append
  keeps A1 empty; the bare list we ship lays the shot clips' embedded audio
  there. The whole `vo_track` feature works *around* that fill — so it is opt-out,
  not a fact of nature. Whether OSIDE renders' embedded audio should reach the
  editor at all is undecided.
- **Clip-level markers.** `TimelineItem.AddMarker` is clip-relative and travels
  with the clip through trims; ours sit at absolute timeline frames and go stale
  the moment an editor ripples. Decided 2026-08-24 to keep timeline markers;
  revisit if drift after trimming actually bites.
