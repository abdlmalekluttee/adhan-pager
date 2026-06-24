"""Manual prayer timetable: parse an uploaded file, store it, look times up.

The expected upload format is repeating day blocks::

    Date : 2024-01-01
    Fajr :
    Dhuhr : 13:14:00
    Asr : 15:52:00
    Maghrib : 18:15:00
    Isha : 19:41:00

    Date : 2024-01-02
    ...

Rules
-----
* A line is ``Key : Value`` (spaces around the colon optional).
* A blank value (e.g. ``Fajr :``) means "no time for this prayer that day" ->
  that prayer is simply skipped on that date.
* We key every entry by ``MM-DD`` (month-day), ignoring the year, so a single
  year's file repeats every year. Feb-29 is kept and only used in leap years.
* Time may be ``HH:MM`` or ``HH:MM:SS``.

The parsed result is stored as JSON: ``{"MM-DD": {"dhuhr": "13:14:00", ...}}``.
Only the five canonical prayers are kept; unknown keys are ignored.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date as _date
from typing import Dict, Optional

from . import config

PRAYERS = config.PRAYERS  # ["fajr","dhuhr","asr","maghrib","isha"]

_DATE_RE = re.compile(r"^\s*date\s*:\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$", re.I)
_TIME_RE = re.compile(r"^\s*([A-Za-z']+)\s*:\s*(.*)$")
_HHMMSS = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")

# Accept a few common spellings/aliases for prayer names.
_ALIASES = {
    "fajr": "fajr", "fajer": "fajr", "subh": "fajr", "sobh": "fajr", "fjr": "fajr",
    "dhuhr": "dhuhr", "duhr": "dhuhr", "zuhr": "dhuhr", "dhuhur": "dhuhr",
    "dohr": "dhuhr", "zuhur": "dhuhr",
    "asr": "asr", "aser": "asr",
    "maghrib": "maghrib", "magrib": "maghrib", "maghreb": "maghrib", "mghrb": "maghrib",
    "isha": "isha", "ishaa": "isha", "esha": "isha", "isha'a": "isha", "icha": "isha",
}


def _norm_time(val: str) -> Optional[str]:
    val = val.strip()
    if not val:
        return None
    m = _HHMMSS.match(val)
    if not m:
        return None
    hh = int(m.group(1)); mm = int(m.group(2)); ss = int(m.group(3) or 0)
    if not (0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60):
        return None
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def parse(text: str) -> Dict[str, Dict[str, str]]:
    """Parse timetable text into ``{"MM-DD": {prayer: "HH:MM:SS"}}``."""
    table: Dict[str, Dict[str, str]] = {}
    cur_key: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        dm = _DATE_RE.match(line)
        if dm:
            month = int(dm.group(2)); day = int(dm.group(3))
            if 1 <= month <= 12 and 1 <= day <= 31:
                cur_key = f"{month:02d}-{day:02d}"
                table.setdefault(cur_key, {})
            else:
                cur_key = None
            continue
        if cur_key is None:
            continue
        tm = _TIME_RE.match(line)
        if not tm:
            continue
        name = _ALIASES.get(tm.group(1).strip().lower())
        if not name:
            continue
        t = _norm_time(tm.group(2))
        if t:
            table[cur_key][name] = t
    # Drop dates that ended up with no usable times.
    return {k: v for k, v in table.items() if v}


def save(table: Dict[str, Dict[str, str]]) -> int:
    path = config.MANUAL_TIMETABLE_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(table, f)
    os.replace(tmp, path)
    return len(table)


def load() -> Dict[str, Dict[str, str]]:
    path = config.MANUAL_TIMETABLE_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def import_text(text: str) -> dict:
    """Parse + persist. Returns a small summary for the API/UI."""
    table = parse(text)
    if not table:
        raise ValueError(
            "No valid day entries found. Expected lines like 'Date : 2024-01-01' "
            "followed by 'Dhuhr : 13:14:00'."
        )
    count = save(table)
    sample_keys = sorted(table.keys())[:1]
    return {
        "days": count,
        "sample_date": sample_keys[0] if sample_keys else None,
        "sample_times": table.get(sample_keys[0], {}) if sample_keys else {},
    }


def status() -> dict:
    table = load()
    return {"loaded": bool(table), "days": len(table)}


def times_for(d: _date) -> Dict[str, str]:
    """Return ``{prayer: "HH:MM:SS"}`` for the given date (by MM-DD)."""
    table = load()
    key = f"{d.month:02d}-{d.day:02d}"
    if key in table:
        return dict(table[key])
    # Feb-29 fallback in non-leap years -> use Feb-28.
    if key == "02-29" and "02-28" in table:
        return dict(table["02-28"])
    return {}
