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
    ("left offset not compared", "sfx.py",
     "            and abs(item[\"leftOffset\"] - row[\"leftOffset\"]) <= tol)", "            and True)",
     ["test_place_sfx_catches_a_clip_laid_with_the_wrong_trim", "test_sfx_check_spans_will_not_vouch_for_a_different_sound"]),
    ("unreadable left offset passes", "sfx.py",
     "            and item.get(\"leftOffset\") is not None\n            and abs(item[\"leftOffset\"] - row[\"leftOffset\"]) <= tol)",
     "            and (item.get(\"leftOffset\") is None or abs(item[\"leftOffset\"] - row[\"leftOffset\"]) <= tol))",
     ["test_place_sfx_read_back_needs_the_file_and_the_trim_it_can_read",
      "test_sfx_check_spans_will_not_vouch_for_a_different_sound"]),
    ("file path not compared", "sfx.py",
     "    return (item.get(\"path\") is not None and item[\"path\"] == row[\"file\"]\n            and item[\"start\"]",
     "    return (True\n            and item[\"start\"]",
     ["test_place_sfx_read_back_needs_the_file_and_the_trim_it_can_read",
      "test_sfx_check_spans_will_not_vouch_for_a_different_sound"]),
    ("conformed length held exact", "sfx.py",
     "            and abs(item[\"duration\"] - row[\"length\"]) <= tol", "            and item[\"duration\"] == row[\"length\"]",
     ["test_place_sfx_rerun_holds_on_a_conformed_lay", "test_sfx_check_spans_will_not_vouch_for_a_different_sound"]),
    ("one item vouches for many rows", "sfx.py", "if i not in used and same_clip(it, r)", "if same_clip(it, r)",
     ["test_sfx_check_spans_will_not_vouch_for_a_different_sound"]),
    ("other tracks not compared", "sfx.py", "        if b[\"json\"] != a[\"json\"]:", "        if False:",
     ["test_place_sfx_diff_catches_a_change_on_another_track", "test_sfx_otio_diff_goes_red_on_each_kind_of_change"]),
    ("target compared by name, frame and length only", "sfx.py",
     "    had, has = otio_items(target_b[\"json\"]), otio_items(target_a[\"json\"])",
     "    had, has = ([(q, {\"name\": i.get(\"name\"), \"OTIO_SCHEMA\": i.get(\"OTIO_SCHEMA\"), "
     "\"source_range\": {\"duration\": (i.get(\"source_range\") or {}).get(\"duration\")}}) "
     "for q, i in otio_items(t[\"json\"])] for t in (target_b, target_a))",
     ["test_sfx_otio_diff_sees_a_changed_clip_on_the_target_track"]),
    ("export size not checked", "resolve_api.py", "    if not ok or size == 0:\n        raise ResolveError(\n            f\"Timeline.Export (OTIO)",
     "    if not ok:\n        raise ResolveError(\n            f\"Timeline.Export (OTIO)",
     ["test_export_timeline_otio_refuses_a_zero_byte_file"]),
    ("timeline end not compared", "resolve_api.py", "\"pass\": end_after == end_frame,", "\"pass\": True,",
     ["test_place_sfx_timeline_end_check_can_go_red"]),
    ("no intra-plan overlap check", "sfx.py", "        if b[\"record\"] < a[\"record\"] + a[\"length\"]:", "        if False:",
     ["test_sfx_check_spans_judges_from_one_read"]),
    ("no end-of-timeline check", "sfx.py", "        if end_limit is not None and span[1] > end_limit:", "        if False:",
     ["test_sfx_check_spans_judges_from_one_read"]),
    ("sub-frame row laid anyway", "sfx.py", "        if r[\"length\"] < 1:", "        if False:",
     ["test_sfx_a_trim_shorter_than_a_frame_is_refused_not_rounded_up"]),
    ("already-placed treated as new", "sfx.py",
     "        if match is not None:\n            used.add(match)\n            already.append(r)\n            continue",
     "        if False:\n            pass",
     ["test_sfx_check_spans_judges_from_one_read", "test_place_sfx_rerun_is_a_no_op"]),
    ("pool matched by name", "resolve_api.py", "                    p = clip.GetClipProperty(\"File Path\")",
     "                    p = next((q for q in paths if os.path.basename(q) == clip.GetName()), None)",
     ["test_items_by_path_matches_file_path_not_name"]),
    ("judged only after import", "resolve_api.py",
     "    if verdict[\"collisions\"]:\n        return finish(\"refused\", toPlace=[_public(r) for r in verdict[\"toPlace\"]])\n    if dry_run:",
     "    if dry_run:",
     ["test_place_sfx_refuses_a_collision_before_writing_anything"]),
    ("refusal after import reads like a clean refusal", "resolve_api.py",
     "        if verdict[\"collisions\"]:\n            # an import cannot be judged exactly",
     "        if verdict[\"collisions\"]:\n            return finish(\"refused\", toPlace=[_public(r) for r in verdict[\"toPlace\"]])\n"
     "            # an import cannot be judged exactly",
     ["test_place_sfx_says_so_when_it_refuses_after_importing"]),
    ("current bin not restored", "resolve_api.py",
     "            try:\n                restored = bool(media_pool.SetCurrentFolder(previous))",
     "            try:\n                restored = True",
     ["test_place_sfx_lays_reads_back_and_proves_nothing_else_moved", "test_place_sfx_puts_the_editors_bin_back_or_says_so"]),
    ("bin restored only when the import works", "resolve_api.py",
     "        finally:\n            try:\n                restored = bool(media_pool.SetCurrentFolder(previous))",
     "        else:\n            try:\n                restored = bool(media_pool.SetCurrentFolder(previous))",
     ["test_place_sfx_puts_the_editors_bin_back_or_says_so"]),
    ("failed restore not reported", "resolve_api.py", "        if not restored:\n            return finish(\"failed\"",
     "        if False:\n            return finish(\"failed\"",
     ["test_place_sfx_puts_the_editors_bin_back_or_says_so"]),
    ("unreadable open bin imported over", "resolve_api.py", "        if previous is None:\n            # importing selects",
     "        if False:\n            # importing selects",
     ["test_place_sfx_puts_the_editors_bin_back_or_says_so"]),
    ("no conform on mismatched rates", "sfx.py", "    if _same_rate(item_fps, timeline_fps):", "    if True:",
     ["test_sfx_expected_lay_conforms_by_duration", "test_place_sfx_lays_reads_back_and_proves_nothing_else_moved"]),
    ("no trim-past-end check", "resolve_api.py", "        verdict[\"collisions\"] += _trim_problems(rows)", "        pass",
     ["test_place_sfx_refuses_a_trim_past_the_end_of_the_file"]),
    ("current timeline not checked", "resolve_api.py", "    if current != plan[\"timeline\"]:", "    if False:",
     ["test_place_sfx_refuses_the_wrong_timeline_track_or_bin"]),
    ("timeline start ignored", "resolve_api.py", "            \"trackIndex\": idx, \"recordFrame\": start + r[\"record\"], \"mediaType\": 2,",
     "            \"trackIndex\": idx, \"recordFrame\": r[\"record\"], \"mediaType\": 2,",
     ["test_place_sfx_frames_are_relative_to_the_timeline_start"]),
    ("partly placed plan re-laid", "resolve_api.py", "    if (partial or laid_before or elsewhere) and verdict[\"toPlace\"] and not resume:",
     "    if (laid_before or elsewhere) and verdict[\"toPlace\"] and not resume:",
     ["test_place_sfx_will_not_relay_what_an_editor_deleted"]),
    ("ledger ignored", "resolve_api.py", "    if (partial or laid_before or elsewhere) and verdict[\"toPlace\"] and not resume:",
     "    if (partial or elsewhere) and verdict[\"toPlace\"] and not resume:",
     ["test_place_sfx_will_not_relay_a_pass_deleted_whole"]),
    ("ledger entries not validated", "sfx.py", "        if not ok:\n            raise PlanError(f\"{path}: entry",
     "        if False:\n            raise PlanError(f\"{path}: entry",
     ["test_place_sfx_refuses_a_ledger_that_only_half_reads"]),
    ("a ledger run that is not a record passes", "sfx.py",
     "and isinstance(e.get(\"runs\"), list) and all(isinstance(r, dict) for r in e[\"runs\"]))",
     "and isinstance(e.get(\"runs\"), list))",
     ["test_place_sfx_refuses_a_ledger_that_only_half_reads"]),
    ("ledger matched by name only", "resolve_api.py", "    if (partial or laid_before or elsewhere) and verdict[\"toPlace\"] and not resume:",
     "    if (partial or laid_before) and verdict[\"toPlace\"] and not resume:",
     ["test_place_sfx_a_renamed_project_does_not_make_a_deleted_pass_new"]),
    ("no ledger recovery on a clean re-run", "server.py",
     "(report.get(\"placed\") or (report.get(\"alreadyPlaced\") and here is None))", "report.get(\"placed\")",
     ["test_place_sfx_ledger_that_failed_to_write_is_written_by_the_next_run"]),
    ("an odd ledger failure eats the report", "server.py",
     "            except Exception as e:  # noqa: BLE001 — the placement report must survive",
     "            except OSError as e:  # noqa: BLE001 — the placement report must survive",
     ["test_place_sfx_ledger_that_failed_to_write_is_written_by_the_next_run"]),
    ("import failure raised past the bin report", "resolve_api.py",
     "        except Exception as e:  # noqa: BLE001 — reported with the bin's state, not raised past it",
     "        except ZeroDivisionError as e:  # noqa: BLE001 — reported with the bin's state, not raised past it",
     ["test_place_sfx_puts_the_editors_bin_back_or_says_so"]),
    ("an import that brings nothing in passes", "resolve_api.py",
     "        if import_error is not None or still:", "        if import_error is not None:",
     ["test_place_sfx_puts_the_editors_bin_back_or_says_so"]),
    ("a pool read after import raises past the bin report", "resolve_api.py",
     "        except Exception as e:  # noqa: BLE001 — the bin's state is reported, not raised past",
     "        except ZeroDivisionError as e:  # noqa: BLE001 — the bin's state is reported, not raised past",
     ["test_place_sfx_puts_the_editors_bin_back_or_says_so"]),
    ("an unread import reported as none", "resolve_api.py",
     "            report[\"imported\"] = None    # unknown", "            pass    # unknown",
     ["test_place_sfx_puts_the_editors_bin_back_or_says_so"]),
    ("unreadable ledger read as none", "sfx.py", "    except (OSError, ValueError) as e:\n        raise PlanError(f\"{path} does not read",
     "    except (OSError, ValueError) as e:\n        return None\n        raise PlanError(f\"{path} does not read",
     ["test_place_sfx_refuses_on_a_ledger_it_cannot_read_and_reports_one_it_cannot_write"]),
    ("unreadable track read as empty", "resolve_api.py", "    if not isinstance(items, (list, tuple)):\n        raise ResolveError(",
     "    items = items or []\n    if False:\n        raise ResolveError(",
     ["test_place_sfx_fails_closed_on_a_track_it_cannot_read"]),
    ("unreadable item skipped", "resolve_api.py", "            raise ResolveError(f\"An item on audio track {index} did not report",
     "            continue\n            raise ResolveError(f\"An item on audio track {index} did not report",
     ["test_place_sfx_fails_closed_on_a_track_it_cannot_read"]),
    ("track read and export not reconciled", "resolve_api.py", "    if seen != read:", "    if False:",
     ["test_place_sfx_refuses_when_the_track_and_its_export_disagree"]),
    ("snapshot overwrites", "server.py", "        if os.path.exists(path):\n            raise rapi.ResolveError(f\"{path!r} already exists",
     "        if False:\n            raise rapi.ResolveError(f\"{path!r} already exists",
     ["test_snapshot_timeline_writes_a_new_file_and_never_overwrites"]),
    ("snapshot unstamped", "server.py", "        meta[sfx.SNAPSHOT_STAMP] = stamp\n", "",
     ["test_snapshot_timeline_writes_a_new_file_and_never_overwrites"]),
    ("nameless timeline snapshotted", "server.py", "        if not isinstance(name, str) or not name:\n            raise",
     "        if False:\n            raise", ["test_snapshot_timeline_writes_a_new_file_and_never_overwrites"]),
    ("a non-string name stamped", "server.py", "        if not isinstance(name, str) or not name:\n            raise",
     "        if not name:\n            raise", ["test_snapshot_timeline_writes_a_new_file_and_never_overwrites"]),
    ("snapshot written over a file that appeared mid-export", "server.py", "with open(path, \"x\", encoding=\"utf-8\")",
     "with open(path, \"w\", encoding=\"utf-8\")", ["test_snapshot_timeline_writes_a_new_file_and_never_overwrites"]),
    ("the plan's project not compared", "resolve_api.py", "    if project_name != plan[\"project\"]:",
     "    if False:",
     ["test_place_sfx_refuses_a_plan_for_another_project",
      "test_place_sfx_a_renamed_project_does_not_make_a_deleted_pass_new"]),
    ("a plan read without a project", "sfx.py",
     "    if not isinstance(plan.get(\"project\"), str) or not plan[\"project\"].strip():", "    if False:",
     ["test_sfx_plan_refuses_each_bad_row_with_a_sentence"]),
    ("the project trimmed before the guard", "sfx.py", "    return {\"project\": plan[\"project\"], ",
     "    return {\"project\": plan[\"project\"].strip(), ",
     ["test_place_sfx_compares_the_project_and_timeline_exactly"]),
    ("the timeline trimmed before the guard", "sfx.py", "\"timeline\": plan[\"timeline\"], \"track\"",
     "\"timeline\": plan[\"timeline\"].strip(), \"track\"",
     ["test_place_sfx_compares_the_project_and_timeline_exactly"]),
    ("a blank project accepted", "sfx.py",
     "    if not isinstance(plan.get(\"project\"), str) or not plan[\"project\"].strip():",
     "    if not isinstance(plan.get(\"project\"), str):",
     ["test_sfx_plan_refuses_each_bad_row_with_a_sentence"]),
    # Not mutated: the metadata guard. Without it a stampless export still
    # refuses (assigning into None raises before any file opens), so removing
    # it changes the message, not the outcome; the test pins the message only.
]


WORK = os.path.join(tempfile.mkdtemp(prefix="redproof-"), "repo")


def run():
    r = subprocess.run([PY, "tests/test_handoff.py"], cwd=WORK, capture_output=True, text=True)
    return ({m.group(1) for m in re.finditer(r"^FAIL  (\S+)", r.stdout, re.M)}, r.stdout.strip().splitlines()[-1],
            r.returncode)


def fresh():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    shutil.copytree(SRC, WORK, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))


fresh()
failed, tail, rc = run()
print("CONTROL (unmutated):", tail)
# a suite that dies before its first case prints no FAIL line either: require its clean exit and a
# tally in which every case passed
tally = re.fullmatch(r"(\d+) passed, 0 unknown \(not passes\), 0 failed — of (\d+)", tail)
assert rc == 0 and not failed and tally and int(tally[1]) > 0 and tally[1] == tally[2], (rc, failed, tail)
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
    failed, tail, _ = run()
    missing = [t for t in expect if t not in failed]
    print(f"{'RED ' if not missing else 'MISS'} {name}: failed={sorted(failed)}" + (f" — NOT red: {missing}" if missing else ""))
    bad += bool(missing)
shutil.rmtree(os.path.dirname(WORK))
print("mutations not caught:", bad)
sys.exit(1 if bad else 0)
