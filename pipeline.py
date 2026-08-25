# ============================================================================
# pipeline.py — the whole handoff as ONE command (no agent required):
#
#   .venv/bin/python pipeline.py <manifest.json> [--name NAME] [--timeline NAME]
#                                                [--dry-run] [--no-cues]
#
# launch Resolve (if closed) → create project from the manifest's kind
# template → import into bins → build the timeline → verify. Prints a JSON
# report to stdout; exit 0 only when every step succeeded AND verify PASSES.
#
# --dry-run touches nothing (and never launches Resolve): it prints the
# build plan for the package — clip order, VO placement, markers, what is
# missing — and exits 0 only when the plan says it would build. The dry run
# happens BEFORE create/import, so it is planned for the TARGET project by
# name: until that project exists and is open, bin presence is "unknown — not
# imported yet" (never missing), wouldBuild is judged on disk presence and the
# fps comes from the kind's template — not from whatever project happens to
# be open in Resolve.
#
# This is what the studio's "Build in Resolve" button spawns; the MCP tools in
# server.py expose the same steps individually for agent-driven sessions.
# ============================================================================

import argparse
import json
import sys

import server


def main() -> int:
    ap = argparse.ArgumentParser(description="oside-davinci/v1 package → Resolve project")
    ap.add_argument("manifest", help="path to the package's manifest.json")
    ap.add_argument("--name", help="Resolve project name (default: the board name)")
    ap.add_argument("--timeline", default="EDIT 01", help="timeline name (default: EDIT 01)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing")
    ap.add_argument("--no-cues", action="store_true", help="skip dialogue-cue range markers")
    args = ap.parse_args()
    cues = not args.no_cues

    # The capability block rides at the top level too: OSIDE's Build-in-Resolve
    # dialog reads report.features to warn about an older MCP (v0.2.0 handshake).
    caps = server.capabilities()
    report = {"ok": False, "steps": {}, "capabilities": caps, "features": caps["features"]}

    def step(key: str, result: dict) -> bool:
        report["steps"][key] = result
        if not result.get("ok"):
            report["error"] = f"{key}: {result.get('error', 'failed')}"
        return bool(result.get("ok"))

    try:
        manifest, _base = server._load_manifest(args.manifest)
    except Exception as e:  # noqa: BLE001
        report["error"] = str(e)
        print(json.dumps(report))
        return 1

    kind = manifest.get("kind", "cinematic")
    name = args.name or manifest.get("project", {}).get("name") or "OSIDE IMPORT"
    report["project"] = name
    report["kind"] = kind

    if args.dry_run:
        plan = server.build_timeline(args.manifest, args.timeline, dry_run=True, cues=cues, project=name)
        report["dryRun"] = True
        report["ok"] = bool(step("plan", plan) and plan.get("wouldBuild"))
        if plan.get("ok") and not plan.get("wouldBuild"):
            report["error"] = f"plan: {len(plan.get('missing') or [])} package file(s) missing — see steps.plan.missing"
        print(json.dumps(report))
        return 0 if report["ok"] else 1

    def look_step() -> bool:
        """The look, ADVISORY. `apply_look` refuses cleanly when the manifest has
        no look block or Depth Converter has not measured a look-cdl.json beside
        it — a refusal is a legitimate outcome and must not fail the build (the
        handoff prompt says exactly this). What is NOT acceptable is what this
        pipeline did before: skip the step entirely and say nothing, so the
        grade silently failed to travel on the one-click path while the
        agent-driven path applied it [audit 2026-08-24]. A look that was
        attempted and came back incomplete IS a failure."""
        if not manifest.get("look"):
            report["steps"]["look"] = {"ok": True, "skipped": "manifest carries no look block"}
            return True
        result = server.apply_look(args.manifest, args.timeline)
        report["steps"]["look"] = result
        if result.get("refused"):
            return True
        if not result.get("ok") or not result.get("complete"):
            report["error"] = (f"look: applied {result.get('applied')} of {result.get('of')}"
                               f" — {result.get('error') or 'not every clip took the CDL'}")
            return False
        return True

    def save_step() -> bool:
        """THE SAVE IS THE VERIFICATION BOUNDARY, not the end of the job.

        Placements from an append whose response errored are not durable: they
        appear in the timeline, every in-session read agrees they are there, and
        the save discards them — measured elsewhere on a real turnover at
        573 items before the save and 500 after. Our whole re-read doctrine is a
        pre-save witness derived from the same unsaved state as the thing it
        checks, so it cannot contradict this class of defect; only a post-save
        read can. Verify AFTER this step.

        Guarded on a named project: on the default 'Untitled Project' SaveProject
        returns False in the GUI and BLOCKS FOREVER headless. This pipeline
        always creates a named project, so the guard is belt-and-braces."""
        result = server.save_project()
        report["steps"]["save"] = result
        if not result.get("ok"):
            report["error"] = f"save: {result.get('error', 'failed')}"
        return bool(result.get("ok"))

    done = (
        step("launch", server.launch_resolve())
        and step("create", server.create_project(kind, name))
        and step("import", server.import_package(args.manifest))
        and step("timeline", server.build_timeline(args.manifest, args.timeline, cues=cues))
        and look_step()
        and save_step()
        and step("verify", server.verify_import(args.manifest, args.timeline, cues=cues))
    )
    verify = report["steps"].get("verify", {})
    clean = verify.get("overall") == "PASS"
    if done and not clean:
        failed = [c["check"] for c in verify.get("checks", []) if not c.get("pass")]
        report["error"] = "verify FAIL: " + (", ".join(failed) or "package and project do not match")
    report["ok"] = done and clean
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
