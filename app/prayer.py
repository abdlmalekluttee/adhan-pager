"""Prayer-time computation (fully offline, via adhanpy).

Maps the YAML config onto adhanpy's calculation model and returns timezone-aware
datetimes for a given local date.
"""
from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from adhanpy.PrayerTimes import PrayerTimes
from adhanpy.calculation.CalculationMethod import CalculationMethod
from adhanpy.calculation.CalculationParameters import CalculationParameters
from adhanpy.calculation.HighLatitudeRule import HighLatitudeRule
from adhanpy.calculation.Madhab import Madhab
from adhanpy.calculation.PrayerAdjustments import PrayerAdjustments

from . import config, timetable

PRAYERS = ["fajr", "dhuhr", "asr", "maghrib", "isha"]


def _params(p) -> CalculationParameters:
    method = getattr(CalculationMethod, p.method, CalculationMethod.UMM_AL_QURA)
    adj = PrayerAdjustments(
        fajr=p.adjustments.get("fajr", 0),
        dhuhr=p.adjustments.get("dhuhr", 0),
        asr=p.adjustments.get("asr", 0),
        maghrib=p.adjustments.get("maghrib", 0),
        isha=p.adjustments.get("isha", 0),
    )
    params = CalculationParameters(method=method, adjustments=adj)
    params.madhab = getattr(Madhab, p.madhab, Madhab.SHAFI)
    params.high_latitude_rule = getattr(
        HighLatitudeRule, p.high_latitude_rule, HighLatitudeRule.MIDDLE_OF_THE_NIGHT
    )
    return params


def _manual_times_for_date(date: datetime.date) -> dict[str, datetime.datetime | None]:
    """Build tz-aware datetimes from the uploaded timetable for this date.

    Prayers with no time in the timetable (e.g. a blank Fajr column) come back as
    None and are simply not paged. Per-prayer minute adjustments still apply.
    """
    cfg = config.get()
    tz = ZoneInfo(cfg.location.timezone)
    raw = timetable.times_for(date)        # {prayer: "HH:MM:SS"}
    out: dict[str, datetime.datetime | None] = {}
    for name in PRAYERS:
        hhmmss = raw.get(name)
        if not hhmmss:
            out[name] = None
            continue
        h, m, s = (int(x) for x in hhmmss.split(":"))
        # Manual timetable times are used exactly as given in the file.
        dt = datetime.datetime(date.year, date.month, date.day, h, m, s, tzinfo=tz)
        out[name] = dt
    return out


def times_for_date(date: datetime.date) -> dict[str, datetime.datetime | None]:
    """Return {prayer: tz-aware datetime (or None)} for the given local date."""
    cfg = config.get()
    if cfg.prayer.source == "manual":
        return _manual_times_for_date(date)
    tz = ZoneInfo(cfg.location.timezone)
    when = datetime.datetime(date.year, date.month, date.day, 12, 0, tzinfo=tz)
    pt = PrayerTimes(
        (cfg.location.latitude, cfg.location.longitude),
        when,
        calculation_parameters=_params(cfg.prayer),
        time_zone=tz,
    )
    out = {}
    for name in PRAYERS:
        t = getattr(pt, name)
        if t.tzinfo is None:
            t = t.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        out[name] = t
    out["sunrise"] = pt.sunrise
    return out


def today_schedule(now: datetime.datetime) -> list[dict]:
    """Today's prayers as {prayer, time, iqama_time, enabled, iqama_enabled}."""
    cfg = config.get()
    times = times_for_date(now.date())
    iqama = cfg.prayer.iqama
    rows = []
    for name in PRAYERS:
        t = times.get(name)
        prayer_on = cfg.prayer.enabled_prayers.get(name, True) and (t is not None)
        iqama_on = (
            prayer_on
            and iqama.enabled
            and cfg.prayer.iqama_prayers.get(name, True)
        )
        rows.append({
            "prayer": name,
            "time": t,
            "iqama_time": (t + datetime.timedelta(minutes=iqama.minutes))
                          if (iqama_on and t is not None) else None,
            "enabled": prayer_on,
            "iqama_enabled": iqama_on,
        })
    return rows
