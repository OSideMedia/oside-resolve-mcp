# Contributing

Thanks for looking. A few things about this repo are unusual, and knowing them
first will save you a rejected patch.

## This is a fixed pipeline, not a Resolve remote control

Ten tools, one recipe: package → project → import → timeline → look → verify.
The tools are deliberately few so the handoff runs identically every time.

**If you want general Resolve control, you already have it** — DaVinci Resolve
Studio 21.1 ships its own MCP server inside the app bundle, with `run_script`
and the scripting-API stubs. That is the right tool for arbitrary automation,
and this server does not try to be it. See "Working alongside Blackmagic's own
MCP" in the README.

So: **a PR that adds a tool "while we're here" will be declined.** A PR that
makes one of the ten do its job better is very welcome.

## The ANTI-PATTERNS in CLAUDE.md are load-bearing

Every entry there was bought with a live walk against Resolve Studio, and each
one looks *wrong* to a reader coming in fresh — the obvious-looking "fix" is
the bug. Examples: `SetCurrentTimeline` takes a Timeline **object**, never a
name. `AppendToTimeline`'s truthiness is worthless — it returns a one-element
list over a silent drop.

**Do not quietly correct them.** If one looks wrong, say so in an issue and
leave it. If you can show it is wrong *on a live Resolve*, that is a great PR —
bring the measurement.

## Every gate must be able to fail

This suite is mostly gates, and a gate that has never gone red is a claim, not
a check. If you add one, show it failing on the code it is meant to catch:

```bash
git show main:handoff.py > handoff.py   # the pre-fix version
python tests/test_handoff.py            # expect RED
git checkout handoff.py                 # restore
```

Two related rules the existing tests follow:

- **Unknown is never a pass.** A check that cannot reach its subject reports
  UNKNOWN and fails; it never silently succeeds. (`CI=1` declares the
  handoff-skill case absent so the runner counts it on its own line.)
- **Name the subject set, and make it non-empty.** An assertion over an empty
  set is vacuously true. See the template-leak gate, which fails if
  `templates/` is empty or if a declared `.drp` is missing.

## Running the suite

```bash
python -m venv .venv && .venv/bin/python -m pip install -e .
.venv/bin/python tests/test_handoff.py          # the whole suite, no pytest needed
CI=1 .venv/bin/python tests/test_handoff.py     # exactly what CI runs
```

The suite is **hermetic**: it never needs Resolve, never touches the network,
and stubs the Resolve API. A test that requires a live Resolve will not be
accepted — the live walks happen by hand, and their findings land in
`CLAUDE.md` as anti-patterns.

## The templates

`templates/*.drp` are Resolve project snapshots and are **published bytes**.
`export_template` overwrites them from a live Resolve project, so a gate scans
them for filesystem paths, email addresses and hostnames before they can ship.
If you re-snapshot, read the diff.

## Style

Match the surrounding code. Comments here explain *why*, usually citing the
walk or audit that produced the rule — that history is the point, so please
keep it when you touch those lines.
