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
3. `build_timeline(manifest_path)` — clips in shot order on V1, one marker per
   shot carrying its number + description (+ VO line for explainers), VO at the
   head of A1.
4. `verify_import(manifest_path)` — the gate check: package vs media pool vs
   timeline.

Plus `resolve_status`, `launch_resolve`, `list_templates`, and
`export_template(kind)` (snapshot a live template project to `templates/`).

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
Claude: create_project("cinematic", "MY FILM")   ← or explainer
        import_package("/Volumes/.../manifest.json")
        build_timeline("/Volumes/.../manifest.json")
        verify_import("/Volumes/.../manifest.json")
```

## Guardrails

- Creates projects/timelines, **never deletes or overwrites** — an existing
  project or timeline name is a hard refusal.
- Template projects are read (exported), never modified.
- Renders are user-triggered in Resolve, not tool-triggered.
- stdio transport only; nothing listens on the network.
