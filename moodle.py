"""Thin, defensive wrapper around the Moodle assignment grading screens.

Everything here is written so that a surprising page makes the run STOP rather
than write a mark into the wrong record.  Nothing is ever saved without first
confirming that the student shown on screen is the student we intended.
"""
from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

ROLL_RE = re.compile(r"\b\d{2}[A-Z]{2}\d{5}\b")
NAV_TIMEOUT = 90_000
ACT_TIMEOUT = 45_000

GRADE_INPUT = 'input[name="grade"]'
GRADE_PANEL = '[data-region="grade-panel"]'
PANEL_IFRAME = '[data-region="grade-panel"] iframe, iframe[id*="gradingpanel"]'
FEEDBACK_IFRAME = 'iframe[id*="assignfeedbackcomments"]'


class MoodleProblem(RuntimeError):
    """Raised whenever the page is not in the state we require."""


@dataclass
class Participant:
    userid: str
    name: str
    roll: str
    identity: str


def grader_url(base: str, cmid: str, userid: str | int) -> str:
    return f"{base}/mod/assign/view.php?id={cmid}&action=grader&userid={userid}"


def grading_table_url(base: str, cmid: str, page_no: int = 0) -> str:
    return f"{base}/mod/assign/view.php?id={cmid}&action=grading&page={page_no}"


def _text(loc: Locator) -> str:
    try:
        return (loc.inner_text(timeout=5_000) or "").strip()
    except Exception:
        return ""


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------

def probe_session(ctx, base: str) -> str | None:
    """Check whether the browser context holds a live Moodle session.

    Uses the context's HTTP client, which shares cookies with every tab, so it
    works no matter which tab you logged in from and never disturbs the page
    you are typing into.  Returns the logged-in name, or None.
    """
    resp = ctx.request.get(f"{base}/my/", timeout=30_000)
    if not resp.ok:
        return None
    if "login" in resp.url.lower():
        return None
    body = resp.text()
    if "loginpage" in body or "You are not logged in" in body:
        return None
    if "/login/logout.php" not in body:
        return None
    m = re.search(r'class="usertext[^"]*"[^>]*>\s*([^<]{2,80}?)\s*<', body)
    return (m.group(1).strip() if m else "(logged in)")


def ensure_logged_in(page: Page, base: str) -> str:
    """Return the logged-in user's name; raise if the session is not active."""
    page.goto(f"{base}/my/", timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
    if "login" in page.url.lower():
        raise MoodleProblem("Not logged in to Moodle. Run:  python moodle_grade.py login")
    who = _text(page.locator(".usertext").first)
    return who or "(logged in)"


# --------------------------------------------------------------------------
# roster
# --------------------------------------------------------------------------

def scrape_participants(page: Page, base: str, cmid: str) -> list[Participant]:
    """Read the assignment's 'View all submissions' table.

    Identity is taken from the row itself, so it does not matter which columns
    the site admin has configured or what order students appear in.
    """
    seen: dict[str, Participant] = {}
    max_pages = 60
    for page_no in range(max_pages):
        page.goto(grading_table_url(base, cmid, page_no), timeout=NAV_TIMEOUT,
                  wait_until="domcontentloaded")
        if "login" in page.url.lower():
            raise MoodleProblem("Session expired while opening the submissions table.")

        # The table renders after domcontentloaded; counting too early silently
        # loses students, so wait for it to settle before reading any row.
        try:
            page.wait_for_selector("table.generaltable tbody tr",
                                   timeout=ACT_TIMEOUT)
        except PWTimeout:
            if page_no == 0:
                raise MoodleProblem(
                    "No rows in the submissions table -- check the assignment id "
                    "(cmid) and that you have grading rights on it.")
            break
        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except PWTimeout:
            pass

        rows = page.locator("table.generaltable tbody tr")
        before = len(seen)
        for i in range(rows.count()):
            row = rows.nth(i)
            link = row.locator('a[href*="/user/view.php?id="]').first
            if link.count() == 0:
                continue
            href = link.get_attribute("href") or ""
            m = re.search(r"[?&]id=(\d+)", href)
            if not m:
                continue
            userid = m.group(1)
            if userid in seen:
                continue
            name = _text(link)
            blob = " ".join(_text(row).split())
            rolls = set(ROLL_RE.findall(blob.upper()))
            if len(rolls) > 1:
                raise MoodleProblem(
                    f"Row for {name!r} (userid {userid}) contains more than one "
                    f"roll-number-like string {sorted(rolls)}; refusing to guess.")
            roll = rolls.pop() if rolls else ""
            seen[userid] = Participant(userid=userid, name=name, roll=roll,
                                       identity=blob[:200])
        if len(seen) == before:
            break   # this page added nobody new: we are past the last page
    else:
        raise MoodleProblem(f"Stopped after {max_pages} pages of the submissions "
                            f"table; that is more than expected.")
    return list(seen.values())


# --------------------------------------------------------------------------
# grading panel
# --------------------------------------------------------------------------

def open_student(page: Page, base: str, cmid: str, userid: str,
                 expect_roll: str, expect_name: str) -> dict:
    """Navigate to one student's grading screen and verify their identity.

    Raises MoodleProblem unless the roll number printed in the page header is
    exactly the roll number we are about to grade.
    """
    if not expect_roll:
        raise MoodleProblem(
            f"No roll number to verify against for userid {userid}; refusing.")

    page.goto(grader_url(base, cmid, userid), timeout=NAV_TIMEOUT,
              wait_until="domcontentloaded")
    if "login" in page.url.lower():
        raise MoodleProblem("Session expired -- re-run the login step.")

    header = page.locator('[data-region="user-info"]').first
    try:
        header.wait_for(state="visible", timeout=ACT_TIMEOUT)
    except PWTimeout:
        raise MoodleProblem("The grading screen did not load a user-info header in time.")

    # Moodle fills the header asynchronously, and while the grading app is
    # re-rendering, inner_text() can keep returning "" even though the markup is
    # already there -- so fall back to the header's HTML rather than giving up.
    # We still read only the user-info region, never the whole page, so this
    # cannot pick up a roll number belonging to some other student.
    deadline = time.time() + 30
    blob = ""
    while time.time() < deadline:
        blob = " ".join(_text(header).split())
        if not blob:
            try:
                html_blob = header.inner_html(timeout=5_000) or ""
            except Exception:
                html_blob = ""
            blob = " ".join(re.sub(r"<[^>]+>", " ", html_blob).split())
        if blob:
            break
        page.wait_for_timeout(500)
    if not blob:
        raise MoodleProblem("The student header stayed empty; not safe to grade.")

    # Independent second check: the profile link in the header must point at the
    # very user id we asked for.
    link = header.locator('a[href*="/user/view.php?id="]').first
    if link.count():
        href = link.get_attribute("href") or ""
        m = re.search(r"[?&]id=(\d+)", href)
        if m and m.group(1) != str(userid):
            raise MoodleProblem(
                f"IDENTITY MISMATCH: asked for userid {userid} but the header "
                f"links to userid {m.group(1)}")

    rolls = set(ROLL_RE.findall(blob.upper()))
    if expect_roll not in rolls:
        raise MoodleProblem(
            f"IDENTITY MISMATCH: expected roll {expect_roll} for {expect_name!r} "
            f"(userid {userid}) but the page header shows "
            f"{sorted(rolls) or 'no roll number'} -- {blob[:160]!r}")
    return {"header": blob, "url": page.url}


def _grade_frame(page: Page, timeout_ms: int = ACT_TIMEOUT):
    """Return the frame that actually holds the grading form.

    The form is normally in the main frame, but Moodle has shipped iframe-based
    grading panels too, so we look for whichever frame owns the grade box rather
    than assuming a container element.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for f in page.frames:
            try:
                if f.locator(GRADE_INPUT).count() > 0:
                    return f
            except Exception:
                continue
        page.wait_for_timeout(500)
    raise MoodleProblem(
        "Could not find the numeric grade box. If this assignment uses a rubric "
        "or marking guide, this tool cannot fill it -- stopping.")


def read_grade_field(page: Page) -> tuple[Locator, str, float | None]:
    """Return (locator, current text, max grade) for the numeric grade box."""
    frame = _grade_frame(page)
    boxes = frame.locator(GRADE_INPUT)
    if boxes.count() != 1:
        raise MoodleProblem(
            f"Expected exactly one grade box on the page, found {boxes.count()}; "
            f"refusing to guess which one to fill.")
    box = boxes.first
    box.wait_for(state="visible", timeout=ACT_TIMEOUT)
    current = (box.input_value() or "").strip()

    maxgrade = None
    body = " ".join(_text(frame.locator("body")).split())
    m = re.search(r"Grade out of\s+([0-9]+(?:\.[0-9]+)?)", body, re.I)
    if m:
        maxgrade = float(m.group(1))
    return box, current, maxgrade


_TINYMCE_JS = """
(args) => {
  const [id, html, mode] = args;
  const tm = window.tinyMCE || window.tinymce;
  const ed = tm && tm.get ? tm.get(id) : null;
  if (ed) {
    if (mode === 'get') return ed.getContent({format: 'text'});
    ed.setContent(html);
    ed.save();                       // push content back into the textarea
    return 'ok';
  }
  const ta = document.getElementById(id);
  if (ta) {
    if (mode === 'get') return ta.value;
    ta.value = html;
    ta.dispatchEvent(new Event('input', {bubbles: true}));
    ta.dispatchEvent(new Event('change', {bubbles: true}));
    return 'ok';
  }
  return null;
}
"""


def _feedback_editor_id(frame) -> str | None:
    ta = frame.locator('textarea[name*="assignfeedbackcomments"]').first
    if ta.count() == 0:
        return None
    return ta.get_attribute("id")


def read_feedback(page: Page) -> str:
    frame = _grade_frame(page)
    ed_id = _feedback_editor_id(frame)
    if not ed_id:
        return ""
    got = frame.evaluate(_TINYMCE_JS, [ed_id, "", "get"])
    if got is None:
        return ""
    return " ".join(re.sub(r"<[^>]+>", " ", str(got)).split())


def _set_feedback(page: Page, text: str) -> bool:
    """Fill the 'Feedback comments' editor. Returns False if it is absent."""
    frame = _grade_frame(page)
    ed_id = _feedback_editor_id(frame)
    if not ed_id:
        # Atto renders a contenteditable div rather than a plain textarea.
        ed = frame.locator('[id*="assignfeedbackcomments"][contenteditable="true"], '
                           'div.editor_atto_content[contenteditable="true"]').first
        if ed.count() == 0:
            return False
        ed.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        if text:
            page.keyboard.type(re.sub(r"<[^>]+>", "", text))
        return True

    body = f"<p>{escape_feedback(text)}</p>" if text else ""
    res = frame.evaluate(_TINYMCE_JS, [ed_id, body, "set"])
    return res is not None


def _set_notify(page: Page, notify: bool) -> None:
    """Tick/untick 'Notify students'.

    Moodle renders a hidden default field alongside the visible checkbox, so we
    must target the checkbox itself rather than the name alone.
    """
    frame = _grade_frame(page)
    cb = frame.locator('input[type="checkbox"][name="sendstudentnotifications"]').first
    if cb.count() == 0:
        return
    try:
        if cb.is_checked() != notify:
            cb.set_checked(notify, timeout=10_000)
    except Exception:
        pass


def write_grade(page: Page, grade: float, feedback: str | None,
                notify: bool) -> None:
    box, _, _ = read_grade_field(page)
    box.fill("")
    box.type(fmt_grade(grade))
    if feedback is not None:
        if not _set_feedback(page, feedback) and feedback.strip():
            raise MoodleProblem(
                "Feedback text was supplied but no 'Feedback comments' editor "
                "exists on this assignment.")
    _set_notify(page, notify)

    frame = _grade_frame(page)
    save = frame.locator('button[name="savechanges"], input[name="savechanges"], '
                         'button:has-text("Save changes")').first
    if save.count() == 0:
        raise MoodleProblem("Could not find the 'Save changes' button.")
    save.click(timeout=ACT_TIMEOUT)
    page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT)
    page.wait_for_timeout(1500)


def verify_saved(page: Page, base: str, cmid: str, userid: str,
                 expect_roll: str, expect_name: str,
                 grade: float, feedback: str | None) -> tuple[bool, str]:
    """Reload the student's screen and confirm the grade really stuck."""
    open_student(page, base, cmid, userid, expect_roll, expect_name)
    _, current, _ = read_grade_field(page)
    if current == "":
        return False, "grade box is empty after save"
    try:
        ok = abs(float(current) - float(grade)) < 1e-6
    except ValueError:
        return False, f"unreadable grade after save: {current!r}"
    note = f"read back grade={current}"
    if ok and feedback:
        got = read_feedback(page)
        want = " ".join(re.sub(r"<[^>]+>", "", feedback).split())
        if want and want not in got:
            return False, f"grade saved but feedback did not stick (found {got[:80]!r})"
        note += ", feedback ok"
    return ok, note


def fmt_grade(g: float) -> str:
    return str(int(g)) if float(g).is_integer() else str(g)


def escape_feedback(text: str) -> str:
    return html.escape(text or "", quote=False)
