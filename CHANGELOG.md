# Changelog

## Unreleased

### Changed — the README is a front door, not the design document

- `Install` was at **line 178 of 280** — a reader scrolled 63% of the document
  to learn how to run it, past a title that was the repo slug and an opening
  line reading "(OSIDE-PLAN-2 Phase 4)". It now sits at 22%.
- The README had grown out of the design doc, so it opened with the exact
  semantics of tool #3. **Nothing was deleted** — the tool reference moved below
  the front door, where a reader who is already committed will find it.
- **New: "Do I need OSIDE to use this?"** The old text said the input was
  "written by the studio's Export for DaVinci action", so a stranger concluded
  they could not use this at all — even though the format is a plain documented
  JSON manifest anything can emit. That section now says so and pastes a minimal
  working package inline.
- Readable title, linked badges above it, one sentence about what LANDS in
  Resolve, and six outcome bullets instead of a numbered list of signatures.
- The standard this follows is `docs/README-STANDARD.md` in claude-commands.

### Added — public pre-flight: licence, contributing guide, badges

- **MIT LICENSE.** A public repo without one is "all rights reserved" — readable
  but not legally usable, which defeats publishing it. Declared in `pyproject`
  and asserted by a test, so the three surfaces cannot drift apart.
- **CONTRIBUTING.md**, covering the two things that would otherwise produce a
  rejected patch: the ten tools are deliberately few (a PR adding a tool "while
  we're here" will be declined — Blackmagic's own MCP is the right home for
  general control), and the ANTI-PATTERNS are load-bearing, so a "fix" to one
  needs a live measurement rather than a reading. Also states the house rules
  every existing test follows: gates must be shown failing, unknown is never a
  pass, and an assertion needs a named non-empty subject set.
- **README badges** (version, licence, the Resolve build actually measured
  against, platform), a findable `## Working alongside Blackmagic's own MCP`
  section, and a footer noting DaVinci Resolve is a Blackmagic Design trademark
  and this project is **not affiliated with or endorsed by** them.
- **The version badge is gated.** A static badge is a claim that rots: the first
  release after adding one would have shipped a README advertising the previous
  version, invisibly — the same class as the skill/prompt drift. A test ties the
  badge to `pyproject`, the one place the version lives, and was red-proofed by
  desyncing them.

### Added — a gate so the templates can live in a public repo

- `export_template` snapshots the LIVE Resolve template project straight into
  `templates/*.drp`, so whatever those projects hold on the day it runs is what
  lands in the repo. Audited by hand 2026-09-08 and clean (bin and track names
  only) — but **an audit is a moment and a gate is a guarantee**, and the next
  snapshot is one command away. The day a bin is named after a client, or a
  template picks up a render path, the old answer stops being true.
- `test_no_template_publishes_a_path_an_email_or_a_hostname` scans every
  `templates/*.drp` for home paths, mounted volumes, Windows paths, email
  addresses, bonjour hostnames and credential assignments.
- **It extracts the archive first.** A `.drp` is a ZIP: every path and address
  inside it is compressed, so a scanner pointed at the file reads nothing and
  passes clean. The archive is the transform that would blind the guard, so the
  MEMBERS are scanned, and the test asserts the compressed bytes do NOT contain
  the planted string — proving the extraction step is load-bearing rather than
  decorative.
- **It carries its own counterexample.** A `.drp`-shaped archive holding a real
  home path and an address runs through the SAME predicate first and must be
  caught, so "no leaks found" can never be confused with a scanner that cannot
  read a zip.
- **Unknown is never a pass.** A `.drp` the scanner cannot open is reported as a
  leak, not skipped — a future format that defeats the reader must fail loudly.
  An empty `templates/` fails too, and every `drp` declared in `templates.json`
  must be on disk, so the subject set cannot quietly shrink to nothing.
- Red-proofed four ways: a leak planted among the REAL templates (`/Volumes/…`),
  a corrupt archive, a declared-but-absent template, and the built-in control.

## 0.6.0 — 2026-09-08

### Changed — "what this is NOT" points at Blackmagic's own MCP

- Both `CLAUDE.md` and the README sent readers to third-party
  `samuelgursky/davinci-resolve-mcp` for general Resolve control. **Blackmagic
  ships its own since Studio 21.1**, inside the app bundle at
  `DaVinci Resolve.app/Contents/Applications/ResolveMCP` — version-matched to
  the running Resolve, so its `search_scripting_api` / `get_scripting_api` are
  the OWNING source for call shapes and retire hand-grepping `README.txt`.
- Both notes now also say what it does NOT replace, because "there is a
  first-party general server" invites exactly the wrong conclusion: its
  `run_script` makes every anti-pattern available fresh, its stubs document
  none of the behavioural lies (`AppendToTimeline`'s still promises "the list
  of appended timelineItems" over a silent drop — a type signature cannot
  express a lie), and it has no gate. This session's VO-lane defect was caught
  by `verify_import` refusing to call the build a success.
- samuelgursky's is still credited: it predates both and its connection pattern
  informed `resolve_api.py`.

### Fixed — a narration spine no longer swallows every pinned take (2026-09-08)

- **A real explainer package could not pass verify.** OSIDE's exporter
  documents VO arriving through TWO doors and ships both: takes attached to the
  whole board (`placements: []` — the recorded spine, or a board narration) and
  takes pinned to a single shot. `build_timeline` laid them on one track in
  manifest order, so the spine went down first — and a spine is often as long
  as the finished piece — after which **every pinned take hit an occupied frame
  and was refused to the tail**. Measured live on 21.1.0.14: pins expected at
  119/214 landed at 288/363, `verify` FAIL, exit 1.
- **One lane per door.** A board-wide take keeps **`VO`** (unchanged for the
  many packages that only ever use that door); a pinned take rides **`VO
  PINS`**. Pins can still collide with EACH OTHER — a long take pinned to a
  short shot spills into the next shot's pin — so a pin whose span is occupied
  opens **`VO PINS 2`**, and so on: a checkerboard, which is what a dialogue
  editor does by hand. **Never a slide to the tail** — that is the behaviour
  this removes, and re-introducing it as the overflow strategy would put the
  take back where nobody expects it. Capped at 8 lanes.
- The split is **inferred from `placements` being empty**. No manifest field was
  added: `placements: []` already carries exactly this information, and a new
  `track` hint would mean a coordinated release across two repos to transmit
  something we can already read.
- The spine lane is created **up front and only when a board-wide take needs
  it**, so `VO` always precedes `VO PINS` in the track order. Lazy creation
  would seat whichever door the manifest happens to list first at A2, and an
  editor should not have to guess which lane is which per package.
- **Each plan row now names the lane it actually landed on**, plus `placedAt`
  and `laidAs`. The old code stamped the spine's name onto every row, so a
  pinned row claimed `track: "VO"` while the take sat elsewhere — an honest
  aggregate over a lying detail, the same defect as audit 2026-08-24, and
  `placedAt` was `None` on every row.
- **`verify` gains one row**: a pinned take must ride a pins lane. The frame row
  already caught the slide; this one names the CAUSE, so a regression reads as
  "back on the spine" rather than as an unexplained 169-frame delta.
- Verified end to end on live Resolve 21.1.0.14 and read back independently
  through Blackmagic's own MCP: `A2 VO` holds the spine at 0, `A3 VO PINS` holds
  both pins at 119 and 214 at full length. `voUnderShot 2, voLoose 0`, PASS.
- Declared as the capability **`vo_lanes`**, the way every other behaviour
  change in this server is: an MCP without it lays both doors on one track,
  which is precisely the build OSIDE must be able to detect.
- The `build_timeline` docstring, the `handoff` prompt and the discovery skill
  all said "its OWN audio track named VO" and are corrected. **The skill/prompt
  drift gate stayed GREEN over all three** — it compares the skill to the
  prompt, so two surfaces agreeing with each other while both disagree with the
  code is exactly the shape it cannot see. Its class is tool names and order;
  this was prose. Noted rather than widened, since prose equivalence is not a
  thing that gate can mechanically decide.
- Three tests, each red-proofed against the pre-fix source: the end-to-end
  regression (which asserts the PRECONDITION that the spine really does cover
  the pinned frame, or it proves nothing), the lane gate's red-proof, and the
  checkerboard. The canonical good fixture now carries both lanes — it encoded
  the single-track layout, which is exactly how the previous single-lane defect
  survived.


### Measured — the append trap re-verified on Studio 21.1.0.14 (2026-09-08)

- **Anti-pattern 2 was UNDERSTATED, and is rewritten.** A three-cell live walk
  against 21.1.0.14 (positive control on an empty track, negative control on a
  free frame of the target track, then the trap) confirms `AppendToTimeline`
  still answers a truthy one-element list over a silent drop. It also found a
  SECOND mode and a way to tell them apart:
  - occupied by the shot clips' embedded audio → places NOTHING, and the
    returned proxy is NULL (`GetName()`, `GetStart()`, `GetDuration()`,
    `GetUniqueId()` all `None`).
  - occupied on a plain audio track → places at the track TAIL **truncated** to
    about `min(recordFrame, clipLength)`. Replicated 6× (asking 10/30/50/74 on
    a track holding 0..75 landed at 75 with duration 10/30/50/74). The proxy is
    live and honest here — it reports the real, wrong start and duration.
  - free frame → correct.
  So `bool(r)`/`len(r)` are worthless but `r[0].GetStart()` is a real oracle.
  **No code changed to rely on it**: the re-read holds on both 21.0.4.5 and
  21.1, the proxy behaviour is measured on 21.1 only, and swapping a working
  defence for a newer one buys nothing. The finding is recorded, not adopted.
- Cell ORDER contaminates these experiments — a "silent drop" measured on a
  track already appended to twice did not reproduce on a fresh timeline. Every
  append cell needs its own timeline and seed. (Caught by an adversarial review
  pass, not by the first read.)

### Added — the gate asserts VO LENGTH, not just the start frame

- Every VO row compared a START frame, so a take sitting on its own frame while
  carrying a fraction of its audio PASSED. Resolve's truncation mode always
  moves the clip too, so the existing rows do red on it — the uncovered shape
  is a take that landed exactly where it was asked and is short anyway. That,
  and only that, is now asserted: **pinned takes, exact frame, one-sided
  (short), 2 frames of slack** (`VO_LENGTH_SLACK`), because `durationSeconds`
  is the exporter's DB value while the placed length is the conformed file and
  the two disagree honestly by a frame or so. Every VO row now also carries
  `lengthFrames`, so a FAIL is diagnosable as relocation vs truncation.
- Shipped with a red-proof (`test_the_vo_length_gate_can_actually_fail`): it
  asserts the row EXISTS on a good build, goes RED on a truncated take while
  the start row stays green, and pins both sides of the slack. Against the
  pre-fix `handoff.py` it fails with "the length row never ran — the gate has
  no subject". The canonical good fixture now carries real VO durations, since
  a gate whose subject is absent from the fixture is a gate that cannot fail.


### Fixed — RM-10 and RM-11 (audit 2026-09-06, ow-9415e0)

- **An unapproved take is named on the timeline and in the gate** (RM-10).
  OSIDE deliberately exports `pending`/`rejected` takes and warns; the MCP laid
  them on V1 with nothing saying so. The shot marker keeps its tag and colour
  (identity is the tag) and carries the outcome in its NAME (`shot 2 · PENDING
  TAKE`) and at the head of its note; `verify_import` adds an advisory row
  `unapproved takes on V1` naming them without failing the build. The naming
  contract compares the base name, so a pre-0.6 timeline still passes.
- **A missing frame says why** (RM-11). `no frame (shot has no clip on V1)`
  fired whenever a start could not be computed — including a planned clip on
  disk whose predecessor's duration was unmeasurable (no ffprobe on PATH), so
  an offline dry run blamed a clip that was there. `no_frame_reason` tells the
  two apart; shot-marker rows without a frame carry the reason too.

### Added — `verify_import` writes `verify.json` (PLAN-SHARED-GENERATION-ID step e, 2026-09-06)

- The gate's verdict used to be RETURNED and written nowhere: the manifest's
  per-clip `generationId` was emitted by OSIDE and read by nothing, so the first
  end-of-pipeline truth about a render never reached the row that paid for it.
  `verify_import` now writes `verify.json` beside the manifest — `format:
  oside-verify/1`, `verifiedAt`, the timeline name, `overall`, and `clips`
  keyed by `generationId` with `placed` (the clip sits on V1) and its
  `start`/`duration` in frames; a video without a generationId is listed under
  `unkeyed`, never dropped. The result carries `sidecar` (the path) and a
  `sidecarWarning` when the write failed — a sidecar that cannot be written is
  never a failed verify. Capability `verify_json`. OSIDE's Build-in-Resolve
  door reads it back onto `generations.request_payload.resolve`.

## 0.5.3 — 2026-09-06

**A malformed manifest is refused with a sentence, never a traceback — and the
pipeline always prints its report.** From the six-repo ecosystem audit of
2026-09-06 (OSIDE `DOCS/audits/AUDIT-2026-09-06-ECOSYSTEM.md`, RM-1..RM-6):

- **RM-1 (P0)** `pipeline.py` died with zero bytes on stdout when
  `manifest.project` was a string (the try/except wrapped only the load), so
  the studio's one-click door could only say "Pipeline returned no report".
  The shape is refused at load, and a catch-all past the load still prints the
  report with the exception in `error`. Test proven red on 0.5.2.
- **RM-3/4/5/6** `_check_shape()` at load: `project` must be an object; `kind`
  must be a template (`documentary` used to rehearse green in a dry run and
  fail only at `create_project`); `videos`/`audio` entries must be objects
  with a `file` (a string entry died with `'str' object has no attribute
  'get'`, a missing `file` at handoff.py:405); a wrong-TYPE `cues`/`textTasks`
  is refused instead of silently dropping the worklist while the gate expected
  zero markers; `look.schema` other than `oside-look/1` is refused (v2 was
  consumed as v1). Every sentence has a row in README's error table, and the
  test requires it.
- **RM-2** the selftest counted an UNKNOWN as a PASS (`pytest.skip` with no
  pytest fell through to a print); UNKNOWN is its own tally line and, without
  `CI=1`, a FAIL naming the missing skill.

Not changed (owed): `verify_import` still counts unmeasurable clip durations
as "no frame" without ffprobe (RM-11); the failure path in OSIDE discards
`checks[]` (RM-9, studio side); OSIDE's capability gate wants 5 of 11
features and never asks for `post_save` (RM-7/E-4, studio side);
`DOCS/DAVINCI/DAVINCI-HANDOFF.md` A1 copy (RM-8, studio side).

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

### The grade is now READ BACK, not asserted

`apply_look(..., verify=True)` exports the timeline as an EDL carrying ASC CDL
and compares what comes back against what it set — per clip, with `verified` and
`readBackDiffs` on each row and a `readback` summary. `complete` now requires the
read-back to match, so a look that did not take can no longer report success.
`pipeline.py` uses it.

This is the only witness Resolve offers that is not the writer's own return
value: there is no `GetCDL`, but `Timeline.Export(path, EXPORT_EDL, EXPORT_CDL)`
carries the applied numbers out. The EDL has no clip names — every event's reel
is `AX` — so events match V1 items BY POSITION, which is what the mapping does.
Measured end to end: 3 events, 3 checked, 3 verified.

### The A1 collision, closed live at last

The 2026-08-16 defect `vo_track` exists for — shot clips carrying embedded audio
fill A1, so an A1 append at an occupied frame answers truthy and places nothing —
had only ever been covered by the fake. A walk with a real video+audio package
now reproduces the condition and shows the fix holding: **A1 carried
`shot01/02/03` at 0/120/240 and every VO take landed on `A2 VO`, `voLoose: 0`.**

`capabilities.features` gains `verify_v3`, `post_save`, `cdl_envelope` and
`cdl_readback`.

## 0.5.2 — 2026-08-25

**The look gets its own colour version, so a re-run cannot overwrite a colourist.**

`SetCDL` writes node 1 of whatever colour VERSION is active, so `apply_look` wrote
into the same slot a colourist grades in — the one place this server overwrote a
human's work, against the create-only guardrail everything else obeys.

The CDL now lands in a version named `OSIDE base`, created per clip and left
ACTIVE. The film still opens showing the balance exactly as before; the colourist
gains a clean `Version 1` underneath and an unambiguous name for which grade is
the tool's.

**Scope, stated honestly:** this does NOT stop a re-run overwriting the version it
owns — that is intended, the version carries our name. It stops a re-run
overwriting THEIRS, provided they grade in their own version, which is standard
practice and which the named version now makes obvious.

**And a correction to how this was first framed:** the trigger is NOT the
director's rebuild. `create_project` refuses an existing project name and runs
first, so pressing *Build in Resolve* again either fails at step one or makes a
new project — the create-only guardrail already blocked that path. The real
exposure is a direct `apply_look` call on a timeline a colourist has since
graded, which is the agent path, not the button.

### Measured before any of it was written (21.0.4.5, 2026-08-25)

One answer decided the design, so it was probed rather than guessed:

- `AddVersion(name, 0)` works and is **not Studio-gated**.
- It **creates AND switches** — `GetCurrentVersion` reports the new one at once,
  so a `SetCDL` straight after lands in ours with no extra call.
- `SetCDL` writes to the **active** version (`Version 1` kept its own values).
- `Timeline.Export` EDL+CDL **reads back the active version**.

That last one is why `verify=True` had to move with the write. Building this
blind and leaving the export where it was would have had the read-back confirm
the COLOURIST'S grade and report success — a check that always passes, which is
worse than no check.

### Walked live

A clip graded to `saturation 0.777` first, then `apply_look(verify=True)`:
applied 3/3, `complete`, read-back verified 3/3 against `OSIDE base`. Afterwards
every clip carried both versions with ours active, and reading each back through
the EDL gave `Version 1 = [0.777, 0.777, 0.777]` and
`OSIDE base = [0.94, 1.05, 0.99]`. Both survived.

Never fails a build: a Resolve that will not give us a version falls back to
today's behaviour and says so on the row. The fake now models colour versions and
`SetCDL` at all — before this, `apply_look`'s impure half had never been
exercised by anything. Tests 59 → 62, all three new ones proven red.

## 0.5.1 — 2026-08-25

Follow-ups from evaluating eight more Resolve MCP repos (barckley75, hiteshK03,
apvlv, allwavemedia, hoyt-harness, lordhoell, Tooflex, wassermanproductions).
Most of that field is general remote control with no measured API knowledge —
seven of the eight contain no evidence language at all, and **not one verifies a
write by re-reading state**. The value was in three facts and one refutation.

- **Narration is MONO by decision, and now says so.** `AddTrack("audio")`
  defaults to mono *silently*, so every VO track OSIDE ever built was mono by an
  undocumented default nobody chose — measured 2026-08-25: our track came back
  `subType='mono'` while the template's A1 is `'stereo'`. The subtype is now
  requested explicitly and READ BACK, and a track that comes back in another
  format is refused rather than filled with narration. Stereo is reserved for
  sound effects and music, which this bridge does not lay today. An existing VO
  track is still reused whatever its format — the check guards what we create.
- **`Timeline.Export` can answer True over a ZERO-BYTE file.** Measured:
  `EXPORT_ALE_CDL` on a populated timeline returned True and wrote nothing. The
  documented trap is that a STRING enum is silently rejected with no file
  written; this is a second, different lie. The read-back checked existence, and
  a 0-byte file exists — it now checks size. (Same probe settles a standing
  question: ALE_CDL carries no clip names, so it cannot retire the EDL's
  match-by-position.)
- **`apply_look`'s docstring was stale** — it still ended "Resolve exposes no CDL
  getter, so `applied` is what SetCDL returned, not a read-back", the opposite of
  what 0.5.0 shipped one commit earlier.
- **A `CLAUDE.md` with an ANTI-PATTERNS block.** Eleven invariants that each look
  wrong to a fresh reader and whose obvious-looking "fix" is the bug, every one
  bought with a council audit or a live walk. They were buried in CHANGELOG prose
  that nothing loads.
- **A trigger skill** (`~/.claude/skills/oside-resolve-handoff`). The `handoff`
  MCP prompt stays the source of truth — it lives inside the server and cannot
  drift from the tools — but a prompt only reaches an agent that already knows to
  ask for it, and by then the first tool call is usually made.

**Refuted, and worth recording so it is not re-raised:** a claim repeated in
another repo's prose AND its test fake says `ImportMedia` returns only *newly*
imported items, which would make our 0.5.0 import check fail on any re-run.
Measured on 21.0.4.5: importing the same package twice returned the full count
both times and did not duplicate the bins. `import_package` IS idempotent. The
claim holds only WITHIN one call — the same path listed twice returned one item —
which our duplicate-basename door check makes unreachable.

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
