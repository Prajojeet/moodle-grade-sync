# moodle-grade-sync

Upload marks and feedback from a TA marking spreadsheet into Moodle assignment
grades — one student at a time, verifying each student's identity before writing
and reading every save back afterwards.

Built for a university ML lab where ~55 students × 2 activities × 6 labs had to be
typed into Moodle by hand. Nothing in it is course-specific: everything lives in a
config file.

Works two ways:

- **As a CLI** — clone, configure, run. No AI involved.
- **As a Claude Code plugin** — installs a skill so Claude drives the workflow for
  you, including knowing when to stop and ask.

---

## Why it's built this way

Putting one student's marks on another student's record is an academic-records
error, not a bug you patch next sprint. So the tool is deliberately paranoid:

| Guarantee | How |
|---|---|
| Right student | Students are matched by **roll number only**, never row order. Before every write, the grader page is loaded and the roll number in the header must match — plus the header's profile link must point at the requested user ID |
| It actually saved | Every write is confirmed by reloading the page and reading the value back. Moodle is slow; a click is not proof |
| Nothing silently overwritten | A grade Moodle already holds that differs from the sheet is a `CONFLICT` — reported, not replaced, unless you pass `--overwrite` |
| Blank ≠ zero | An empty cell is skipped. Writing 0 is a claim about a student, so it happens only if you opt in with `absent_zero` |
| Wrong scale caught early | `preflight` reads the activity's real maximum and refuses to run if it disagrees with your config |
| No silent continuation | Runs stop after `--max-failures` (default 3) |
| Independently checkable | `verify` re-reads every grade from Moodle and diffs the sheet, ignoring the run logs entirely |

Every run appends a CSV to `runs/` — one line per student, with what was written and
why anything was skipped.

---

## Setup

Requires Python 3.10+ and Google Chrome.

```bash
git clone https://github.com/Prajojeet/moodle-grade-sync.git
cd moodle-grade-sync
pip install -r requirements.txt
cp config.example.json config.mylab.json
```

Edit `config.mylab.json`: set `base_url` (include any path prefix — many Moodle
installs live at `https://host/moodle`, not `https://host`), your sheet path, and
the exact activity names as they appear in the course.

Your spreadsheet needs a roll-number column, a name column, one column per mark, and
optionally a comments column. Header names are configurable; nothing else about the
layout matters.

---

## Usage

```bash
python moodle_grade.py --config config.mylab.json login       # you log in yourself
python moodle_grade.py --config config.mylab.json discover    # finds activity IDs
python moodle_grade.py --config config.mylab.json roster      # roll -> Moodle user id
python moodle_grade.py --config config.mylab.json preflight   # reads the real max grade
python moodle_grade.py --config config.mylab.json plan        # dry run, writes nothing
python moodle_grade.py --config config.mylab.json apply --limit 2   # pilot
python moodle_grade.py --config config.mylab.json apply --resume    # the rest
python moodle_grade.py --config config.mylab.json verify      # independent audit
```

Run them in that order. `preflight` and `plan` are the cheap moments to notice a
problem; skipping them just moves the discovery later.

**Pilot on a student who has a comment** (`--only 24AB10004`) if you're writing
feedback. The first rows of a sheet often have empty comment cells, so a plain
`--limit 2` can report total success without ever touching the feedback editor.

### Useful flags

| Flag | Effect |
|---|---|
| `--only 24AB10001,24AB10007` | Just these roll numbers |
| `--limit N` | First N students |
| `--resume` | Skip anyone already recorded `SAVED` |
| `--overwrite` | Replace grades Moodle already has — deliberate, per-run |
| `--headless` | Hide the browser |
| `--max-failures N` | Stop after N failures (default 3) |

### Statuses

`SAVED` and `ALREADY_SET` are success. `SKIPPED` means no usable mark in the sheet.
`CONFLICT` means Moodle disagrees and a human should decide. `VERIFY_FAILED`,
`ABORT` and `ERROR` all mean look at it.

Note that a `CONFLICT` skips the student **entirely** — so their feedback isn't
written either.

---

## Configuration

```jsonc
{
  "base_url": "https://moodle.example.edu",
  "course":   { "id": null, "shortname": "COURSE101" },
  "lab":      "lab1",              // names this lab's roster and run logs
  "notify_students": false,        // emails students on save — leave off
  "absent_zero": false,            // blank cell -> 0 (opt in, see below)
  "sheet": {
    "path": "C:/path/to/Lab.xlsx",
    "columns": { "roll": "Roll No", "name": "Name", "comment": "Comments",
                 "Code": "Code", "Viva": "Viva" }
  },
  "targets": {
    "code": { "name": "Assignment 1 Submission Link", "cmid": null,
              "column": "Code", "max_grade": 25, "scale": 1.25, "round_to": 1,
              "write_feedback": false },
    "viva": { "name": "In Lab Viva - Topic", "cmid": null,
              "column": "Viva", "max_grade": 10,
              "write_feedback": true, "feedback_template": "{comment}" }
  }
}
```

One config per lab keeps rosters and logs separate, so `--resume` never mixes them.

| Key | Notes |
|---|---|
| `cmid` | Leave `null`; `discover` fills it by matching the activity name |
| `max_grade` | Leave `null` for the first `preflight` so it *reports* the real value, then set it to lock the guard in |
| `scale` | Multiply the sheet value — for a /20 sheet feeding a /100 activity, use `5` |
| `round_to` | Round the scaled value to this step, if the activity uses whole numbers |
| `absent_zero` | Blank cells become 0. Students Moodle already graded are still protected |
| `feedback_template` | Accepts `{comment}`, `{name}`, `{roll}` and any marks column |

### Scale conversions — validate before you trust

If other students are already graded on that activity, your conversion must
reproduce their existing values **exactly** before you use it. In the course this was
built for, the sheets were marked /20 while the activities were /25, and
`round(sheet × 1.25)` reproduced all 55 existing entries — which is what made it safe
for the handful that were missing. If a rule doesn't reproduce them, stop and find
out why.

---

## Claude Code plugin

```
/plugin install Prajojeet/moodle-grade-sync
```

This installs the `moodle-grade-sync` skill, which teaches Claude the workflow order,
the safety rules, and — importantly — when to stop and ask you rather than guess.
Then just say what you want:

> upload the SVM lab marks to Moodle, code is out of 20 but the activity is out of 100

Claude still can't log in as you, and won't overwrite existing grades without
checking first.

---

## Security and privacy

**Never commit these** — `.gitignore` covers them, but know what they are:

- `session.json` and `.browser-profile/` hold your **live Moodle session cookie**.
  Anyone with them can act as you in Moodle. Delete both to log out.
- `runs/`, `rosters/` and any spreadsheets contain **student names, roll numbers and
  marks**.

The tool never sees or stores your password — you log in yourself in a real Chrome
window, and only the resulting cookie is kept, locally.

`notify_students` is `false` by default because a mistaken mark that has been emailed
to a student cannot be quietly corrected.

---

## Limitations

- Simple numeric grades only — **rubrics and marking guides are not supported**, and
  the tool stops rather than guessing if it can't find a plain grade box.
- A TA account typically **cannot change activity settings**. If an activity's maximum
  grade is wrong, an editing teacher has to fix it.
- One run at a time — all runs share a single browser profile.
- Built against Moodle 4.x assignment grading. Other versions may need selector
  changes in `moodle.py`.

---

## Files

| File | Purpose |
|---|---|
| `moodle_grade.py` | CLI: login / discover / roster / preflight / plan / apply / verify / report |
| `moodle.py` | Moodle page interaction, identity checks, save verification |
| `sheet.py` | Spreadsheet reading; decides what counts as a missing mark |
| `skills/moodle-grade-sync/SKILL.md` | The workflow knowledge, for Claude Code |
| `config.example.json` | Template — copy per lab |

MIT licensed. Issues and PRs welcome.
