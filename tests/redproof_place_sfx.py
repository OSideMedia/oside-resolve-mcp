# ============================================================================
# tests/redproof_place_sfx.py — proves each place_sfx guard has a test that
# goes RED without it. Not part of CI (it copies the repo once per mutation).
#
#   .venv/bin/python tests/redproof_place_sfx.py
#
# A positive control first: the unmutated copy must be all green. Then each
# mutation removes ONE guard and the named tests must fail. A mutation whose
# anchor is missing counts as a miss, so a refactor cannot silently retire it.
# ============================================================================

import os
import re
import shutil
import subprocess
import sys
import tempfile

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

MUTS = [
    ("floor drops the epsilon", "sfx.py", "return int(math.floor(x + _EPS))", "return int(math.floor(x))",
     ["test_sfx_trim_floors_through_float_noise"]),
    ("append trusted, read-back skipped", "resolve_api.py",
     "        if not good:\n            failure =", "        good = bool(new) or True\n        if not good:\n            failure =",
     ["test_place_sfx_stops_at_the_first_clip_that_lands_wrong", "test_place_sfx_catches_the_plain_track_tail_mode",
      "test_place_sfx_catches_a_silent_drop", "test_place_sfx_catches_a_clip_laid_with_the_wrong_trim"]),
    ("left offset not compared", "resolve_api.py",
     "                and (new[0][\"leftOffset\"] is None or abs(new[0][\"leftOffset\"] - r[\"leftOffset\"]) <= tol))",
     "                and True)",
     ["test_place_sfx_catches_a_clip_laid_with_the_wrong_trim"]),
    ("other tracks not compared", "sfx.py", "        if b[\"json\"] != a[\"json\"]:", "        if False:",
     ["test_place_sfx_diff_catches_a_change_on_another_track", "test_sfx_otio_diff_goes_red_on_each_kind_of_change"]),
    ("export size not checked", "resolve_api.py", "    if not ok or size == 0:\n        raise ResolveError(\n            f\"Timeline.Export (OTIO)",
     "    if not ok:\n        raise ResolveError(\n            f\"Timeline.Export (OTIO)",
     ["test_place_sfx_refuses_on_an_empty_snapshot"]),
    ("no intra-plan overlap check", "sfx.py", "        if b[\"record\"] < a[\"record\"] + a[\"length\"]:", "        if False:",
     ["test_sfx_check_spans_judges_from_one_read"]),
    ("no end-of-timeline check", "sfx.py", "        if end_limit is not None and span[1] > end_limit:", "        if False:",
     ["test_sfx_check_spans_judges_from_one_read"]),
    ("already-placed treated as new", "sfx.py", "        if same:\n            already.append(r)\n            continue", "        if False:\n            pass",
     ["test_sfx_check_spans_judges_from_one_read", "test_place_sfx_rerun_is_a_no_op"]),
    ("pool matched by name", "resolve_api.py", "                    p = clip.GetClipProperty(\"File Path\")",
     "                    p = next((q for q in paths if os.path.basename(q) == clip.GetName()), None)",
     ["test_items_by_path_matches_file_path_not_name"]),
    ("judged only after import", "resolve_api.py",
     "    if verdict[\"collisions\"]:\n        return finish(\"refused\", toPlace=[_public(r) for r in verdict[\"toPlace\"]])\n    if dry_run:",
     "    if dry_run:",
     ["test_place_sfx_refuses_a_collision_before_writing_anything"]),
    ("current bin not restored", "resolve_api.py", "        if previous is not None:\n            media_pool.SetCurrentFolder(previous)", "        pass",
     ["test_place_sfx_lays_reads_back_and_proves_nothing_else_moved"]),
    ("no conform on mismatched rates", "sfx.py", "    if _same_rate(item_fps, timeline_fps):", "    if True:",
     ["test_sfx_expected_lay_conforms_by_duration", "test_place_sfx_lays_reads_back_and_proves_nothing_else_moved"]),
    ("no trim-past-end check", "resolve_api.py", "        verdict[\"collisions\"] += _trim_problems(rows)", "        pass",
     ["test_place_sfx_refuses_a_trim_past_the_end_of_the_file"]),
    ("current timeline not checked", "resolve_api.py", "    if current != plan[\"timeline\"]:", "    if False:",
     ["test_place_sfx_refuses_the_wrong_timeline_track_or_bin"]),
    ("timeline start ignored", "resolve_api.py", "            \"trackIndex\": idx, \"recordFrame\": start + r[\"record\"], \"mediaType\": 2,",
     "            \"trackIndex\": idx, \"recordFrame\": r[\"record\"], \"mediaType\": 2,",
     ["test_place_sfx_frames_are_relative_to_the_timeline_start"]),
    ("partly placed plan re-laid", "resolve_api.py", "    if partial and not resume:", "    if False:",
     ["test_place_sfx_will_not_relay_what_an_editor_deleted"]),
    ("snapshot overwrites", "server.py", "        if os.path.exists(path):\n            raise rapi.ResolveError(f\"{path!r} already exists",
     "        if False:\n            raise rapi.ResolveError(f\"{path!r} already exists",
     ["test_snapshot_timeline_writes_a_new_file_and_never_overwrites"]),
]


WORK = os.path.join(tempfile.mkdtemp(prefix="redproof-"), "repo")


def run():
    r = subprocess.run([PY, "tests/test_handoff.py"], cwd=WORK, capture_output=True, text=True)
    return {m.group(1) for m in re.finditer(r"^FAIL  (\S+)", r.stdout, re.M)}, r.stdout.strip().splitlines()[-1]


def fresh():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    shutil.copytree(SRC, WORK, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))


fresh()
failed, tail = run()
print("CONTROL (unmutated):", tail)
assert not failed, failed
bad = 0
for name, f, old, new, expect in MUTS:
    fresh()
    p = os.path.join(WORK, f)
    s = open(p).read()
    if s.count(old) != 1:
        print(f"MISS {name}: anchor found {s.count(old)}x")
        bad += 1
        continue
    open(p, "w").write(s.replace(old, new))
    failed, tail = run()
    missing = [t for t in expect if t not in failed]
    print(f"{'RED ' if not missing else 'MISS'} {name}: failed={sorted(failed)}" + (f" — NOT red: {missing}" if missing else ""))
    bad += bool(missing)
shutil.rmtree(os.path.dirname(WORK))
print("mutations not caught:", bad)
sys.exit(1 if bad else 0)
