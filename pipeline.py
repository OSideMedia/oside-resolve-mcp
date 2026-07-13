# ============================================================================
# pipeline.py — the whole handoff as ONE command (no agent required):
#
#   .venv/bin/python pipeline.py <manifest.json> [--name NAME] [--timeline NAME]
#
# launch Resolve (if closed) → create project from the manifest's kind
# template → import into bins → build the timeline → verify. Prints a JSON
# report to stdout; exit 0 only when every step succeeded AND verify is clean.
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
    args = ap.parse_args()

    report = {"ok": False, "steps": {}}

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

    done = (
        step("launch", server.launch_resolve())
        and step("create", server.create_project(kind, name))
        and step("import", server.import_package(args.manifest))
        and step("timeline", server.build_timeline(args.manifest, args.timeline))
        and step("verify", server.verify_import(args.manifest))
    )
    clean = bool(report["steps"].get("verify", {}).get("clean"))
    if done and not clean:
        report["error"] = "verify: package and project do not match"
    report["ok"] = done and clean
    print(json.dumps(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
