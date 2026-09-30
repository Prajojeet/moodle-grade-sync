"""Push TA marking-sheet results into Moodle assignment grades.

Usage (run these in order the first time):

    python moodle_grade.py login              # you log in yourself, once
    python moodle_grade.py discover           # find the assignment ids in a course
    python moodle_grade.py roster             # build roll-number -> Moodle user map
    python moodle_grade.py plan               # dry run: show exactly what would change
    python moodle_grade.py apply --limit 2    # pilot on the first 2 students
    python moodle_grade.py apply              # the rest

Design rules that are deliberately not configurable:
  * a mark is written only after the roll number on screen matches the sheet;
  * every save is read back from a fresh page load before it counts as done;
  * a blank / '-' cell is never turned into a zero;
  * an existing different grade is reported as a conflict, not overwritten,
    unless you pass --overwrite.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

import moodle
from moodle import MoodleProblem
from sheet import SheetRow, load_sheet, normalise_roll

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"         # overridden by --config
PROFILE = HERE / ".browser-profile"   # holds YOUR Moodle session, never a password
SESSION = HERE / "session.json"       # ditto: cookies only, no credentials
RUNS = HERE / "runs"


def set_config_path(path: str | None) -> None:
    """Point every later load_config()/save_config() at this file."""
    global CONFIG
    if path:
        p = Path(path)
        CONFIG = p if p.is_absolute() else (HERE / p)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG.exists():
        sys.exit(f"Missing {CONFIG}. Copy config.example.json to config.json first.")
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def save_config(cfg: dict) -> None:
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")


def lab_tag(cfg: dict) -> str:
    """Short slug identifying this lab, used to keep rosters and logs separate."""
    return re.sub(r"[^A-Za-z0-9]+", "-", cfg.get("lab", CONFIG.stem)).strip("-")


def roster_path(cfg: dict, key: str) -> Path:
    return HERE / "rosters" / f"{cfg['course']['shortname']}_{lab_tag(cfg)}_{key}.json"


def browser(pw, headless: bool = False):
    PROFILE.mkdir(exist_ok=True)
    # Chrome cannot open the same user-data-dir twice; a second concurrent run
    # dies with an unhelpful TargetClosedError, so say what is actually wrong.
    lock = PROFILE / "SingletonLock"
    if (PROFILE / "lockfile").exists() or lock.exists():
        sys.exit("Another moodle_grade run is already using the browser profile. "
                 "Wait for it to finish, or delete "
                 f"{PROFILE / 'SingletonLock'} if no run is active.")
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        channel="chrome",
        headless=headless,
        viewport={"width": 1500, "height": 950},
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx.set_default_timeout(moodle.ACT_TIMEOUT)
    # Moodle's MoodleSession is a session cookie, which Chrome discards when the
    # window closes, so the profile alone is not enough to stay logged in.
    if SESSION.exists():
        try:
            saved = json.loads(SESSION.read_text(encoding="utf-8")).get("cookies", [])
            if saved:
                ctx.add_cookies(saved)
        except Exception as e:  # noqa: BLE001
            print(f"! could not restore saved session ({e}); you may need to log in again")
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def save_session(ctx) -> None:
    """Persist cookies (including session cookies) so the next command reuses them."""
    try:
        ctx.storage_state(path=str(SESSION))
    except Exception:
        pass


def build_feedback(row: SheetRow, cfg: dict, target_key: str) -> str | None:
    """Return the feedback text for this target, or None to leave it alone."""
    tgt = cfg["targets"][target_key]
    if not tgt.get("write_feedback"):
        return None
    tpl = tgt.get("feedback_template", "{comment}")
    text = tpl.format(
        comment=row.comment,
        name=row.name,
        roll=row.roll,
        **{k: ("" if v is None else moodle.fmt_grade(v))
           for k, v in row.marks.items()},
    ).strip()
    if not text and not tgt.get("write_empty_feedback"):
        return None
    return text


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

def match_rows(rows: list[SheetRow], participants: list[dict]) -> tuple[list[dict], list[dict]]:
    """Match sheet rows to Moodle users by roll number only (never by order)."""
    by_roll: dict[str, list[dict]] = {}
    for p in participants:
        if p["roll"]:
            by_roll.setdefault(normalise_roll(p["roll"]), []).append(p)

    matched, blocked = [], []
    for r in rows:
        if r.problems:
            blocked.append({"row": r, "why": "; ".join(r.problems)})
            continue
        hits = by_roll.get(r.roll, [])
        if not hits:
            blocked.append({"row": r, "why": f"roll {r.roll} is not enrolled / not in "
                                             f"the Moodle submissions list"})
        elif len(hits) > 1:
            blocked.append({"row": r, "why": f"roll {r.roll} matches {len(hits)} "
                                             f"Moodle users -- ambiguous"})
        else:
            p = hits[0]
            sheet_last = set(re.findall(r"[a-z]+", r.name.lower()))
            moodle_last = set(re.findall(r"[a-z]+", p["name"].lower()))
            note = ""
            if sheet_last and moodle_last and not (sheet_last & moodle_last):
                note = (f"name differs (sheet {r.name!r} vs Moodle {p['name']!r}) "
                        f"but roll numbers match")
            matched.append({"row": r, "user": p, "note": note})
    return matched, blocked


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_login(args) -> None:
    cfg = load_config()
    base = cfg["base_url"].rstrip("/")
    with sync_playwright() as pw:
        ctx, page = browser(pw)
        page.goto(f"{base}/login/index.php", wait_until="domcontentloaded",
                  timeout=moodle.NAV_TIMEOUT)
        print("\nA Chrome window has opened on your desktop.")
        print("Log in to Moodle there yourself -- this tool never sees your password.")
        print("Any tab of that window is fine; so is going through the SSO page.")
        print(f"Waiting up to {args.wait} seconds for your session to become active ...")
        sys.stdout.flush()
        deadline = time.time() + args.wait
        who = None
        last = ""
        try:
            while time.time() < deadline:
                # Probe over HTTP using the window's own cookies, so we never
                # navigate the tab you are typing into.
                try:
                    who = moodle.probe_session(ctx, base)
                    if who:
                        break
                except Exception as e:
                    if "closed" in str(e).lower():
                        sys.exit("The Chrome window was closed before login "
                                 "completed. Re-run 'login'.")
                left = int(deadline - time.time())
                if left // 30 != last:
                    last = left // 30
                    print(f"  ... still waiting ({left}s left)")
                    sys.stdout.flush()
                time.sleep(4)
            if who:
                print(f"OK - session saved for: {who}")
                print("You can close that Chrome window; the session is stored in "
                      ".browser-profile/")
            else:
                sys.exit("TIMED OUT - no active Moodle session detected. "
                         "Re-run 'login' and finish signing in.")
        finally:
            save_session(ctx)
            ctx.close()


def _find_course(page, base: str, cfg: dict) -> str:
    """Locate the course id from the user's own course list."""
    want = [w for w in (cfg["course"].get("shortname"),
                        cfg["course"].get("fullname")) if w]
    for url in (f"{base}/my/courses.php", f"{base}/my/"):
        page.goto(url, wait_until="domcontentloaded", timeout=moodle.NAV_TIMEOUT)
        page.wait_for_timeout(2500)
        links = page.locator('a[href*="/course/view.php?id="]')
        hits: dict[str, str] = {}
        for i in range(links.count()):
            a = links.nth(i)
            href = a.get_attribute("href") or ""
            m = re.search(r"[?&]id=(\d+)", href)
            text = " ".join((a.inner_text() or "").split())
            if not m or not text:
                continue
            if any(w.lower() in text.lower() for w in want):
                hits[m.group(1)] = text
        if len(hits) == 1:
            cid, name = next(iter(hits.items()))
            print(f"Matched course id={cid}  {name}")
            return cid
        if len(hits) > 1:
            for cid, name in hits.items():
                print(f"  id={cid}  {name}")
            sys.exit("More than one course matched -- re-run with --courseid N")
    sys.exit(f"Could not find a course matching {want} on your dashboard. "
             "Open the course in a browser and re-run with --courseid N "
             "(the id= in the course URL).")


def cmd_discover(args) -> None:
    """List every assignment in the course with its cmid, so you can fill config."""
    cfg = load_config()
    base = cfg["base_url"].rstrip("/")
    courseid = args.courseid or cfg["course"].get("id")
    with sync_playwright() as pw:
        ctx, page = browser(pw)
        try:
            moodle.ensure_logged_in(page, base)
            if not courseid:
                courseid = _find_course(page, base, cfg)
            page.goto(f"{base}/course/view.php?id={courseid}",
                      wait_until="domcontentloaded", timeout=moodle.NAV_TIMEOUT)
            title = page.title()
            print(f"\nCourse: {title}\n")
            links = page.locator('a[href*="/mod/assign/view.php?id="]')
            found: dict[str, str] = {}
            for i in range(links.count()):
                a = links.nth(i)
                href = a.get_attribute("href") or ""
                m = re.search(r"[?&]id=(\d+)", href)
                if not m:
                    continue
                cmid = m.group(1)
                name = " ".join((a.inner_text() or "").split())
                name = re.sub(r"\s*(Assignment|Opened:|Due:).*$", "", name).strip() or name
                if cmid in found or not name:
                    continue
                found[cmid] = name
                print(f"  cmid={cmid:<8} {name}")
        finally:
            save_session(ctx)
            ctx.close()

    cfg["course"]["id"] = str(courseid)
    for key, tgt in cfg["targets"].items():
        want = tgt["name"].strip().lower()
        hits = [c for c, n in found.items() if n.strip().lower().startswith(want)]
        if len(hits) == 1:
            tgt["cmid"] = hits[0]
            print(f"\nconfig: targets.{key}.cmid = {hits[0]}  ({tgt['name']})")
        elif len(hits) > 1:
            print(f"\n! targets.{key}: {tgt['name']!r} matched cmids {hits} "
                  f"-- set it by hand in config.json")
        else:
            print(f"\n! targets.{key}: no activity named {tgt['name']!r} found "
                  f"-- set the cmid by hand in config.json")
    save_config(cfg)
    print("\nconfig.json updated.")


def cmd_roster(args) -> None:
    cfg = load_config()
    base = cfg["base_url"].rstrip("/")
    (HERE / "rosters").mkdir(exist_ok=True)
    keys = [args.target] if args.target else list(cfg["targets"])
    with sync_playwright() as pw:
        ctx, page = browser(pw)
        try:
            print(f"Logged in as: {moodle.ensure_logged_in(page, base)}")
            for key in keys:
                tgt = cfg["targets"][key]
                if not tgt.get("cmid"):
                    print(f"  ! {key}: no cmid in config.json, skipping")
                    continue
                print(f"\nReading submissions table for {key} "
                      f"({tgt['name']}, cmid={tgt['cmid']}) ...")
                ppl = moodle.scrape_participants(page, base, str(tgt["cmid"]))
                data = [p.__dict__ for p in ppl]
                no_roll = [p for p in data if not p["roll"]]
                out = roster_path(cfg, key)
                out.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                               encoding="utf-8")
                print(f"  {len(data)} participants -> {out.name}")
                if no_roll:
                    print(f"  ! {len(no_roll)} rows had no roll number visible:")
                    for p in no_roll[:10]:
                        print(f"      userid={p['userid']} {p['name']}")
                    print("    Turn on the ID-number column in the submissions table "
                          "(Moodle shows the columns your site admin configured).")
        finally:
            save_session(ctx)
            ctx.close()


def _load_roster(cfg: dict, key: str) -> list[dict]:
    p = roster_path(cfg, key)
    if not p.exists():
        sys.exit(f"No roster for {key}. Run:  python moodle_grade.py roster")
    return json.loads(p.read_text(encoding="utf-8"))


def _plan_for(cfg: dict, key: str, rows: list[SheetRow]) -> tuple[list[dict], list[dict]]:
    tgt = cfg["targets"][key]
    col = tgt["column"]
    participants = _load_roster(cfg, key)
    matched, blocked = match_rows(rows, participants)

    # A sheet value may need converting to the activity's own scale, e.g. a
    # sheet marked out of 20 feeding an activity marked out of 25.
    scale = float(tgt.get("scale", 1))
    # "absent_zero": a blank cell means the student did not attend, so write 0.
    # Anyone Moodle has already graded is still protected by the CONFLICT check.
    absent_zero = bool(tgt.get("absent_zero", cfg.get("absent_zero", False)))

    todo, skipped = [], []
    for m in matched:
        r: SheetRow = m["row"]
        val = r.marks.get(col)
        absent = val is None
        if absent:
            if not absent_zero:
                skipped.append({"roll": r.roll, "name": r.name,
                                "why": f"{col} cell is {r.raw_marks.get(col, '')!r} "
                                       f"-- left untouched"})
                continue
            val = 0.0
        else:
            val = val * scale
            if (rt := tgt.get("round_to")):
                val = round(val / float(rt)) * float(rt)
        mx = tgt.get("max_grade")
        if mx is not None and (val < 0 or val > float(mx)):
            skipped.append({"roll": r.roll, "name": r.name,
                            "why": f"{col}={val} is outside 0..{mx}"})
            continue
        todo.append({
            "roll": r.roll, "name": r.name, "userid": m["user"]["userid"],
            "moodle_name": m["user"]["name"], "grade": val, "absent": absent,
            # an absent student has no comment to write
            "feedback": None if absent else build_feedback(r, cfg, key),
            "note": m["note"], "excel_row": r.excel_row,
        })
    for b in blocked:
        skipped.append({"roll": b["row"].roll, "name": b["row"].name, "why": b["why"]})
    return todo, skipped


def cmd_plan(args) -> None:
    cfg = load_config()
    rows = load_sheet(cfg["sheet"]["path"], cfg["sheet"]["columns"])
    print(f"Sheet: {cfg['sheet']['path']}  ({len(rows)} data rows)\n")
    keys = [args.target] if args.target else list(cfg["targets"])
    RUNS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for key in keys:
        tgt = cfg["targets"][key]
        todo, skipped = _plan_for(cfg, key, rows)
        print(f"=== {key}: {tgt['name']} (cmid={tgt.get('cmid')}, "
              f"column {tgt['column']!r}, max {tgt.get('max_grade')}) ===")
        n_absent = sum(1 for t in todo if t.get("absent"))
        n_fb = sum(1 for t in todo if t.get("feedback"))
        print(f"  would write : {len(todo)}  "
              f"({len(todo) - n_absent} marked, {n_absent} absent->0, "
              f"{n_fb} with feedback)")
        if scale_note := tgt.get("scale"):
            print(f"  scale       : sheet value x {scale_note}")
        print(f"  would skip  : {len(skipped)}")
        for s in skipped:
            print(f"    - {s['roll'] or '(no roll)':<10} {s['name'][:32]:<34} {s['why']}")
        notes = [t for t in todo if t["note"]]
        for t in notes:
            print(f"    ? {t['roll']:<10} {t['note']}")
        out = RUNS / f"plan_{lab_tag(cfg)}_{key}_{stamp}.csv"
        with out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["roll", "sheet_name", "moodle_name", "userid", "grade",
                        "feedback", "note"])
            for t in todo:
                w.writerow([t["roll"], t["name"], t["moodle_name"], t["userid"],
                            moodle.fmt_grade(t["grade"]), t["feedback"] or "",
                            t["note"]])
        print(f"  full plan -> runs/{out.name}\n")


def cmd_preflight(args) -> None:
    """Read-only: open one student per activity and report what Moodle says.

    Confirms the maximum grade, that the numeric grade box and feedback editor
    exist, and how many students already have a grade -- all without writing.
    """
    cfg = load_config()
    base = cfg["base_url"].rstrip("/")
    rows = load_sheet(cfg["sheet"]["path"], cfg["sheet"]["columns"])
    keys = [args.target] if args.target else list(cfg["targets"])
    ok = True
    with sync_playwright() as pw:
        ctx, page = browser(pw, headless=not args.show)
        try:
            print(f"Logged in as: {moodle.ensure_logged_in(page, base)}\n")
            for key in keys:
                tgt = cfg["targets"][key]
                todo, _ = _plan_for(cfg, key, rows)
                if not todo:
                    print(f"{key}: nothing planned, skipping preflight")
                    continue
                t = todo[0]
                print(f"=== {key}: {tgt['name']} (cmid={tgt['cmid']}) ===")
                info = moodle.open_student(page, base, str(tgt["cmid"]),
                                           t["userid"], t["roll"], t["name"])
                print(f"  opened   : {info['header'][:90]}")
                _, current, maxgrade = moodle.read_grade_field(page)
                print(f"  Moodle max grade : {maxgrade}   (config says "
                      f"{tgt.get('max_grade')})")
                print(f"  current grade    : {current or '(empty)'}")
                if maxgrade is not None and tgt.get("max_grade") is not None and \
                        abs(float(maxgrade) - float(tgt["max_grade"])) > 1e-6:
                    ok = False
                    print(f"  ** MISMATCH: apply would abort. Either fix the "
                          f"activity's Maximum grade in Moodle, or change "
                          f"max_grade in config.json.")
                over = [x for x in todo
                        if maxgrade is not None and x["grade"] > maxgrade]
                if over:
                    ok = False
                    print(f"  ** {len(over)} student(s) have a mark above Moodle's "
                          f"maximum, e.g. {over[0]['roll']} = "
                          f"{moodle.fmt_grade(over[0]['grade'])}")
                if tgt.get("write_feedback"):
                    frame = moodle._grade_frame(page)
                    ed_id = moodle._feedback_editor_id(frame)
                    print(f"  feedback editor  : {ed_id or 'NOT FOUND'}")
                    if not ed_id:
                        ok = False
                print()
        finally:
            save_session(ctx)
            ctx.close()
    print("Preflight OK -- safe to apply." if ok else
          "Preflight found problems (above). Fix them before applying.")
    if not ok:
        sys.exit(2)


def cmd_apply(args) -> None:
    cfg = load_config()
    base = cfg["base_url"].rstrip("/")
    rows = load_sheet(cfg["sheet"]["path"], cfg["sheet"]["columns"])
    keys = [args.target] if args.target else list(cfg["targets"])
    RUNS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = RUNS / f"apply_{lab_tag(cfg)}_{stamp}.csv"
    done_before = _already_done(lab_tag(cfg))

    notify = bool(cfg.get("notify_students", False))
    print(f"Notify students on save: {notify}")
    print(f"Log: runs/{log_path.name}\n")

    with log_path.open("w", newline="", encoding="utf-8") as fh:
        log = csv.writer(fh)
        log.writerow(["time", "target", "roll", "name", "userid", "grade",
                      "status", "detail"])
        fh.flush()

        with sync_playwright() as pw:
            ctx, page = browser(pw, headless=args.headless)
            failures = 0
            try:
                print(f"Logged in as: {moodle.ensure_logged_in(page, base)}\n")
                for key in keys:
                    tgt = cfg["targets"][key]
                    todo, skipped = _plan_for(cfg, key, rows)
                    for s in skipped:
                        log.writerow([datetime.now().isoformat(timespec="seconds"),
                                      key, s["roll"], s["name"], "", "",
                                      "SKIPPED", s["why"]])
                    fh.flush()

                    if getattr(args, "only", None):
                        want = {normalise_roll(x) for x in args.only.split(",") if x.strip()}
                        missing = want - {t["roll"] for t in todo}
                        if missing:
                            sys.exit(f"--only: {sorted(missing)} are not in the "
                                     f"writable plan for {key} (check 'plan' output)")
                        todo = [t for t in todo if t["roll"] in want]
                    if args.limit:
                        todo = todo[:args.limit]
                    print(f"=== {key}: {tgt['name']} -- {len(todo)} student(s) ===")

                    for i, t in enumerate(todo, 1):
                        rec_key = (key, t["roll"])
                        if args.resume and rec_key in done_before:
                            print(f"  [{i}/{len(todo)}] {t['roll']} already done, skip")
                            continue
                        status, detail = _apply_one(page, base, cfg, key, t,
                                                    notify, args.overwrite)
                        print(f"  [{i}/{len(todo)}] {t['roll']:<10} "
                              f"{t['name'][:28]:<30} {status}  {detail}")
                        log.writerow([datetime.now().isoformat(timespec="seconds"),
                                      key, t["roll"], t["name"], t["userid"],
                                      moodle.fmt_grade(t["grade"]), status,
                                      ("absent->0; " if t.get("absent") else "") + detail])
                        fh.flush()
                        if status not in ("SAVED", "ALREADY_SET", "SKIPPED"):
                            failures += 1
                            if failures >= args.max_failures:
                                print(f"\nSTOPPING: {failures} failures "
                                      f"(--max-failures {args.max_failures}).")
                                return
            finally:
                save_session(ctx)
                ctx.close()
    print(f"\nDone. Full record in runs/{log_path.name}")


def _apply_one(page, base, cfg, key, t, notify, overwrite) -> tuple[str, str]:
    tgt = cfg["targets"][key]
    cmid = str(tgt["cmid"])
    try:
        moodle.open_student(page, base, cmid, t["userid"], t["roll"], t["name"])
        _, current, maxgrade = moodle.read_grade_field(page)
        if maxgrade is not None and tgt.get("max_grade") is not None:
            if abs(float(maxgrade) - float(tgt["max_grade"])) > 1e-6:
                return "ABORT", (f"Moodle says 'Grade out of {maxgrade}' but "
                                 f"config says max_grade={tgt['max_grade']}")
        if maxgrade is not None and t["grade"] > maxgrade:
            return "ABORT", f"grade {t['grade']} exceeds Moodle max {maxgrade}"
        if current:
            try:
                same = abs(float(current) - float(t["grade"])) < 1e-6
            except ValueError:
                same = False
            if same:
                if not t["feedback"]:
                    return "ALREADY_SET", f"already {current}"
                existing = moodle.read_feedback(page)
                want = " ".join(re.sub(r"<[^>]+>", " ", t["feedback"]).split())
                if want and want in existing:
                    return "ALREADY_SET", f"already {current}, feedback already set"
                # grade is right, feedback is missing: write feedback only
                moodle.write_grade(page, t["grade"], t["feedback"], notify)
                ok, detail = moodle.verify_saved(page, base, cmid, t["userid"],
                                                 t["roll"], t["name"], t["grade"],
                                                 t["feedback"])
                return (("SAVED", "grade unchanged, " + detail) if ok
                        else ("VERIFY_FAILED", detail))
            if not overwrite:
                return "CONFLICT", (f"Moodle already has {current}, sheet says "
                                    f"{moodle.fmt_grade(t['grade'])} "
                                    f"(re-run with --overwrite to replace)")
        moodle.write_grade(page, t["grade"], t["feedback"], notify)
        ok, detail = moodle.verify_saved(page, base, cmid, t["userid"], t["roll"],
                                         t["name"], t["grade"], t["feedback"])
        return ("SAVED", detail) if ok else ("VERIFY_FAILED", detail)
    except MoodleProblem as e:
        return "ERROR", str(e)
    except Exception as e:  # noqa: BLE001 - surface anything unexpected, loudly
        return "ERROR", f"{type(e).__name__}: {e}"


def _already_done(tag: str) -> set[tuple[str, str]]:
    """Students already saved FOR THIS LAB. Other labs' logs are ignored."""
    done: set[tuple[str, str]] = set()
    if not RUNS.exists():
        return done
    for p in sorted(RUNS.glob(f"apply_{tag}_*.csv")):
        with p.open(encoding="utf-8") as fh:
            for rec in csv.DictReader(fh):
                if rec.get("status") in ("SAVED", "ALREADY_SET"):
                    done.add((rec["target"], rec["roll"]))
    return done


def cmd_verify(args) -> None:
    """Independent audit: read every grade back out of Moodle and diff the sheet.

    Deliberately does not consult the run logs -- it re-reads the submissions
    table, so it catches anything the apply run thought it did but did not.
    """
    cfg = load_config()
    base = cfg["base_url"].rstrip("/")
    rows = load_sheet(cfg["sheet"]["path"], cfg["sheet"]["columns"])
    keys = [args.target] if args.target else list(cfg["targets"])
    grade_re = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)")
    all_ok = True

    with sync_playwright() as pw:
        ctx, page = browser(pw, headless=True)
        try:
            print(f"Logged in as: {moodle.ensure_logged_in(page, base)}\n")
            for key in keys:
                tgt = cfg["targets"][key]
                if not tgt.get("cmid"):
                    continue
                todo, skipped = _plan_for(cfg, key, rows)
                want = {t["roll"]: t["grade"] for t in todo}
                skip_rolls = {s["roll"] for s in skipped}

                found: dict[str, float] = {}
                for pn in range(60):
                    page.goto(moodle.grading_table_url(base, str(tgt["cmid"]), pn),
                              wait_until="domcontentloaded", timeout=moodle.NAV_TIMEOUT)
                    try:
                        page.wait_for_selector("table.generaltable tbody tr",
                                               timeout=moodle.ACT_TIMEOUT)
                    except Exception:
                        break
                    trs = page.locator("table.generaltable tbody tr")
                    before = len(found)
                    for i in range(trs.count()):
                        blob = " ".join((trs.nth(i).inner_text() or "").split())
                        rolls = set(moodle.ROLL_RE.findall(blob.upper()))
                        if len(rolls) != 1:
                            continue
                        roll_here = rolls.pop()
                        g = grade_re.search(blob)
                        if roll_here not in found:
                            found[roll_here] = float(g.group(1)) if g else None
                    if len(found) == before:
                        break

                good = bad = missing = 0
                problems = []
                for roll, grade in want.items():
                    got = found.get(roll, "ABSENT")
                    if got == "ABSENT":
                        missing += 1
                        problems.append(f"{roll}: not found in Moodle's table")
                    elif got is None:
                        bad += 1
                        problems.append(f"{roll}: sheet {moodle.fmt_grade(grade)}, "
                                        f"Moodle has NO grade")
                    elif abs(got - float(grade)) > 1e-6:
                        bad += 1
                        problems.append(f"{roll}: sheet {moodle.fmt_grade(grade)}, "
                                        f"Moodle has {got}")
                    else:
                        good += 1
                others = [r for r in skip_rolls
                          if found.get(r) not in (None, "ABSENT")]
                stray = []   # nothing here is this run's responsibility
                print(f"=== {key}: {tgt['name']} ===")
                print(f"  matching sheet   : {good}/{len(want)}")
                print(f"  wrong or missing : {bad + missing}")
                print(f"  not in your sheet: {len(others)} already graded by "
                      f"someone else, {len(skip_rolls) - len(others)} still blank")
                for p in problems[:20]:
                    print(f"    ! {p}")

                if bad or missing or stray:
                    all_ok = False
                print()
        finally:
            save_session(ctx)
            ctx.close()
    print("VERIFIED - Moodle matches the sheet." if all_ok else
          "MISMATCHES FOUND - see above.")
    if not all_ok:
        sys.exit(2)


def cmd_report(args) -> None:
    """Summarise the most recent apply run."""
    runs = sorted(RUNS.glob("apply_*.csv"))
    if not runs:
        sys.exit("No apply runs yet.")
    p = Path(args.file) if args.file else runs[-1]
    counts: dict[str, int] = {}
    bad = []
    with p.open(encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
            if rec["status"] not in ("SAVED", "ALREADY_SET"):
                bad.append(rec)
    print(f"{p.name}\n")
    for k, v in sorted(counts.items()):
        print(f"  {k:<14} {v}")
    if bad:
        print("\nNeeds your attention:")
        for rec in bad:
            print(f"  {rec['target']:<6} {rec['roll'] or '(no roll)':<10} "
                  f"{rec['name'][:28]:<30} {rec['status']}: {rec['detail']}")


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="config file to use (default config.json); "
                                     "keep one per lab, e.g. config.knn.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lg = sub.add_parser("login", help="open Chrome so you can log in; saves the session")
    lg.add_argument("--wait", type=int, default=300,
                    help="seconds to wait for you to finish signing in")

    d = sub.add_parser("discover", help="list assignment cmids in the course")
    d.add_argument("--courseid")

    r = sub.add_parser("roster", help="scrape roll-number -> Moodle userid map")
    r.add_argument("--target")

    p = sub.add_parser("plan", help="dry run; writes nothing")
    p.add_argument("--target")

    a = sub.add_parser("apply", help="write grades into Moodle")
    a.add_argument("--target")
    a.add_argument("--limit", type=int, default=0,
                   help="only the first N students (use for a pilot run)")
    a.add_argument("--only",
                   help="comma-separated roll numbers to write, e.g. 24AB10001,24AB10007")
    a.add_argument("--overwrite", action="store_true",
                   help="replace a grade that Moodle already has")
    a.add_argument("--resume", action="store_true",
                   help="skip students already recorded SAVED in runs/")
    a.add_argument("--headless", action="store_true")
    a.add_argument("--max-failures", type=int, default=3)

    pf = sub.add_parser("preflight", help="read-only check of the grading screens")
    pf.add_argument("--target")
    pf.add_argument("--show", action="store_true", help="show the browser")

    v = sub.add_parser("verify", help="re-read Moodle and diff against the sheet")
    v.add_argument("--target")

    rep = sub.add_parser("report", help="summarise a run log")
    rep.add_argument("--file")

    args = ap.parse_args()
    set_config_path(args.config)
    {"login": cmd_login, "discover": cmd_discover, "roster": cmd_roster,
     "plan": cmd_plan, "preflight": cmd_preflight, "apply": cmd_apply,
     "verify": cmd_verify, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    os.chdir(HERE)
    main()
