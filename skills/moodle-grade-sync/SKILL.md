---
name: moodle-grade-sync
description: Upload marks and feedback from a TA marking spreadsheet into Moodle assignment grades, safely and verifiably, using the bundled Python/Playwright CLI. Use this skill whenever someone needs to get grades out of a spreadsheet and into Moodle - including phrasings like "upload the lab marks", "enter these grades into Moodle", "put the viva scores in", "add the feedback comments to Moodle", "the TA marking sheet needs to go into the LMS", or when they mention a grading spreadsheet alongside Moodle, an assignment grader page, or a "Change user" dropdown. Also use it when asked to check, audit, or fix grades already uploaded to Moodle, or to rescale marks to a different total. Prefer this over ad-hoc browser automation: entering marks against the wrong student is an academic-records error, and this workflow is built to make that impossible rather than unlikely.
---

# Moodle grade sync

Marks in a spreadsheet, grades in Moodle, one student at a time through a slow web
UI. The mechanical part is easy. The part that matters is not putting one student's
marks onto another student's record, and knowing afterwards that you didn't.

This skill drives a CLI that does the writing. Your job is to run the steps in
order, read what each one tells you, and stop when something looks wrong instead of
pushing through.

## The one rule that matters

**Never write a mark you cannot justify from the sheet, to a student whose identity
you have not just confirmed on screen.**

Everything below follows from that. The CLI enforces much of it, but it can only
refuse what it can detect - you are the part that notices when the *plan* is wrong,
not just the execution.

## Workflow

Run these in order. Each one earns the right to run the next.

```bash
python moodle_grade.py --config config.<lab>.json <command>
```

| # | Command | What it establishes |
|---|---------|---------------------|
| 1 | `login` | A session exists. Opens Chrome; **the user logs in themselves** |
| 2 | `discover` | The activity IDs (`cmid`) for this lab, written into the config |
| 3 | `roster` | roll number → Moodle user ID, scraped from the submissions table |
| 4 | `preflight` | The activity's real maximum grade, and that the fields exist |
| 5 | `plan` | Exactly who gets what. Writes nothing |
| 6 | `apply --limit 2` | A pilot. Two students, fully verified |
| 7 | `apply --resume` | The rest |
| 8 | `verify` | Independent audit: re-reads Moodle, diffs the sheet |

Skipping steps is how people get hurt. `preflight` exists because an activity set to
"out of 20" while the sheet marks out of 25 will reject every write - better to learn
that from one page than from fifty failures. `plan` exists because seeing "would write
52, would skip 3" is the last cheap moment to notice the skip list is wrong.

### About the pilot

Pick students who exercise the code paths you actually care about. If you are writing
feedback, **pilot on someone whose comment cell is non-empty** - use
`apply --only <roll>,<roll>`. The first rows of a sheet often have blank comments, so a
naive `--limit 2` can report complete success while never once touching the feedback
editor. That exact hole shipped in this project's first run.

After the pilot, stop and let the user look at Moodle before continuing.

## Safety rules

These are worth understanding rather than just obeying, because each one exists to
catch a specific way this goes wrong.

**Match on roll number, never row order.** The sheet and Moodle disagree about
ordering, always. The CLI matches on roll number alone and refuses an ambiguous match
rather than picking one.

**Verify identity on the page before writing.** Every write is preceded by loading
that student's grader page and confirming the roll number in the header matches the
one being written. If it doesn't, the run stops. This is the check that makes the
whole thing trustworthy.

**A blank cell is not a zero.** Blank means "no information" - the student may be
absent, ungraded, or another TA's responsibility. Writing 0 is a real accusation about
a real person. The default is to skip. Only set `absent_zero: true` when the user has
said explicitly that blanks mean absent, and even then Moodle's existing grade wins if
there is one.

**Never overwrite silently.** If Moodle already holds a different grade, that is a
`CONFLICT`: the CLI leaves it alone and reports it. Someone entered that number on
purpose. Surface it and ask; only pass `--overwrite` when the user has said so about
those specific students.

Note the consequence: a conflicted student is skipped *entirely*, so their feedback
is not written either. Say so, rather than letting the user assume the comment landed.

**Verify by reading back, not by clicking.** Moodle is slow and a save can appear to
succeed without sticking. Every write is confirmed by reloading the page and reading
the value out. Then `verify` re-reads everything from the submissions tables,
independently of any log, because a run log only records what the tool *believed*.

**One run at a time.** All runs share one browser profile; a second concurrent run
cannot start.

## Scale conversions

Sheets and activities often use different totals - a sheet marked out of 20 feeding an
activity out of 100. Set `scale` (and `round_to` when the activity uses whole numbers).

**Validate the conversion against grades already in Moodle before writing any.** If
other students are already graded on that activity, your rule must reproduce their
values exactly. In this project the sheets were /20 and the activities /25, and
`round(sheet × 1.25)` reproduced all 55 existing entries - which is what made it safe
to use for the four that were missing. Had even one disagreed, the right move was to
stop and ask.

If nothing is graded yet, there is nothing to validate against. Say that plainly
rather than implying the rule is confirmed.

## Before you start writing

Check whether the marks are already there. Spreadsheets get uploaded by other people.
Run `plan` and read the `ALREADY_SET` count: a lab that looks unstarted may be fully
graded already, with only the *comments* missing. Discovering that after a long run is
worse than discovering it before one.

Relatedly: many courses split students across TAs, so a sheet that is only half filled
is normal, and the other students already have marks from someone else. Those are not
your gaps to fill.

## Statuses

| Status | Meaning |
|---|---|
| `SAVED` | Written and confirmed by re-reading the page |
| `ALREADY_SET` | Moodle already had exactly this value |
| `SKIPPED` | No mark in the sheet, out of range, or roll not matched |
| `CONFLICT` | Moodle has a *different* value - needs a human |
| `VERIFY_FAILED` | Saved but did not stick - check by hand |
| `ABORT` | Max-grade mismatch, or a grade above the activity maximum |
| `ERROR` | Identity mismatch, timeout, or an unexpected page |

`SAVED` and `ALREADY_SET` are fine. Everything else deserves a sentence in your report
naming the students involved.

## Config

One file per lab, so runs, rosters and logs never mix.

```jsonc
{
  "base_url": "https://moodle.example.edu",   // include any path prefix, e.g. /moodle
  "course": { "id": "12345", "shortname": "CS101" },
  "lab": "svm",                                // names this lab's roster and logs
  "notify_students": false,                    // emails students on save - keep off
  "sheet": {
    "path": "C:\\path\\to\\SVM_Lab.xlsx",
    "columns": { "roll": "Roll No", "name": "Name", "comment": "Comments",
                 "Code": "Code", "Viva": "Viva" }
  },
  "targets": {
    "code": { "name": "Assignment 5 Submission Link", "cmid": null,
              "column": "Code", "max_grade": 100, "scale": 5,
              "write_feedback": false },
    "viva": { "name": "In Lab Viva - SVM", "cmid": null,
              "column": "Viva", "max_grade": 100, "scale": 10,
              "write_feedback": true, "feedback_template": "{comment}" }
  }
}
```

Set `cmid: null` and let `discover` fill it. Leave `max_grade: null` on a first
`preflight` so it *reports* the real maximum instead of asserting your guess, then set
it to lock the guard in.

`notify_students` emails every student on save. Keep it `false` unless asked -
a mistake that has been emailed cannot be quietly fixed.

## Things that will waste your time otherwise

- **The Moodle base URL may carry a path prefix** (`/moodle`). Get it from a real
  course URL rather than assuming the bare domain.
- **The session cookie is session-scoped**, so it dies when Chrome closes. The CLI
  saves and re-injects cookies; if commands report "not logged in" right after a
  successful login, that mechanism is what to look at.
- **The submissions table paginates** and ignores a `perpage` parameter. Page through
  it. Counting rows before the table finishes rendering silently loses students.
- **A table row can contain the same roll number twice** when the submitted file is
  named after it (`24AB10001_Assignment4.zip`). Deduplicate with a set - not doing so
  once made the auditor report saved grades as missing.
- **The feedback editor may be legacy TinyMCE**, which needs its JS API rather than
  typing into the DOM.
- **A TA account usually cannot change activity settings.** If the maximum grade is
  wrong, that needs an editing teacher. Don't burn time automating around it.

## Reporting back

Say what changed, what didn't, and what needs a human. Give the `verify` counts rather
than the run-log counts - the audit is the number worth trusting. Name the students
behind every `CONFLICT`, `ERROR` and `VERIFY_FAILED`, and state plainly when a
conflict means a comment was not written.

If a check contradicts an earlier claim of success, believe the check and say so. In
this project the auditor twice reported problems that turned out to be bugs in the
auditor - both times the right move was to investigate the contradiction rather than
pick whichever answer was more convenient.
