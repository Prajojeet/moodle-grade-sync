"""Read a TA marking sheet (.xlsx) into normalised rows.

Expected columns (case-insensitive, configurable in config.json):
    Roll No | Student Name | Code | Viva | Comments

A cell that is blank, '-', 'NA', 'A' or 'absent' is treated as MISSING, never as 0.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import openpyxl

MISSING_TOKENS = {"", "-", "--", "na", "n/a", "nil", "absent", "a", "none"}
ROLL_RE = re.compile(r"\b\d{2}[A-Z]{2}\d{5}\b")


def _norm(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _is_missing(v: Any) -> bool:
    return _norm(v).lower() in MISSING_TOKENS


def _num(v: Any) -> float | None:
    if _is_missing(v):
        return None
    try:
        return float(str(v).strip())
    except ValueError:
        return None


@dataclass
class SheetRow:
    excel_row: int
    roll: str
    name: str
    marks: dict[str, float | None] = field(default_factory=dict)
    raw_marks: dict[str, str] = field(default_factory=dict)
    comment: str = ""
    problems: list[str] = field(default_factory=list)


def normalise_roll(raw: str) -> str:
    """Uppercase, strip spaces/punctuation so 24ab 10001 == 24AB10001."""
    return re.sub(r"[^A-Z0-9]", "", _norm(raw).upper())


def load_sheet(path: str, columns: dict[str, str]) -> list[SheetRow]:
    """columns maps logical name -> header text, e.g.
    {"roll": "Roll No", "name": "Student Name", "comment": "Comments",
     "code": "Code", "viva": "Viva"}
    Any key other than roll/name/comment is treated as a marks column.
    """
    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise SystemExit(f"{path}: sheet is empty")

    header = [_norm(c).lower() for c in rows[0]]

    def col_index(header_text: str) -> int:
        want = header_text.strip().lower()
        for i, h in enumerate(header):
            if h == want:
                return i
        raise SystemExit(
            f"{path}: column {header_text!r} not found. Headers present: {rows[0]}"
        )

    idx = {key: col_index(text) for key, text in columns.items()}
    mark_keys = [k for k in columns if k not in ("roll", "name", "comment")]

    out: list[SheetRow] = []
    seen: dict[str, int] = {}
    for n, raw in enumerate(rows[1:], start=2):
        def cell(key: str) -> Any:
            i = idx[key]
            return raw[i] if i < len(raw) else None

        roll = normalise_roll(_norm(cell("roll")))
        name = _norm(cell("name"))
        if not roll and not name:
            continue  # blank spacer row

        row = SheetRow(excel_row=n, roll=roll, name=name)
        if not roll:
            row.problems.append("no roll number in sheet")
        elif not ROLL_RE.fullmatch(roll):
            row.problems.append(f"roll {roll!r} does not look like a KGP roll number")
        if roll in seen:
            row.problems.append(f"duplicate roll, also on excel row {seen[roll]}")
        elif roll:
            seen[roll] = n

        for key in mark_keys:
            v = cell(key)
            row.raw_marks[key] = _norm(v)
            if _is_missing(v):
                row.marks[key] = None
            else:
                num = _num(v)
                if num is None:
                    row.problems.append(f"{key}: cannot read {v!r} as a number")
                row.marks[key] = num

        if "comment" in idx:
            row.comment = _norm(cell("comment"))
        out.append(row)
    return out
