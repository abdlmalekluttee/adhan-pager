"""Scheduler.

Each day it computes the day's prayer times (using NTP-corrected time) and
schedules one paging job per enabled prayer, plus an optional iqama page. A
daily refresh job at 00:01 recomputes for the new day. Reschedules immediately
whenever prayer/location/destination/schedule config changes.
"""
from __future__ import annotations

import datetime
import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from . import audio, config, ntp, prayer, sip_engine

log = logging.getLogger("scheduler")

_scheduler: BackgroundScheduler | None = None
_last_page: dict | None = None
_page_lock = threading.Lock()


def _enabled_destinations() -> list[str]:
    return [d.uri for d in config.get().destinations if d.enabled]


def _do_page(prayer_name: str, kind: str = "adhan"):
    """Perform a page. Serialised so two prayers never overlap on the line."""
    global _last_page
    with _page_lock:
        cfg = config.get()
        if kind == "iqama":
            wav = audio.resolve_iqama_for_prayer(prayer_name) or audio.resolve_for_prayer(prayer_name)
        else:
            wav = audio.resolve_for_prayer(prayer_name)
        dests = _enabled_destinations()
        started = ntp.corrected_now(cfg.location.timezone)
        record = {"prayer": prayer_name, "kind": kind,
                  "time": started.isoformat(), "destinations": dests,
                  "audio": wav, "ok": False, "error": None}
        if not wav:
            record["error"] = "No audio file assigned"
            log.error("%s page skipped: no audio", prayer_name)
            _last_page = record
            return
        if not dests:
            record["error"] = "No enabled destinations"
            log.error("%s page skipped: no destinations", prayer_name)
            _last_page = record
            return
        try:
            log.info("PAGING %s (%s) -> %s", prayer_name, kind, dests)
            label = f"{prayer_name.capitalize()} {kind}"
            sip_engine.get_engine().page(wav, dests, cfg.call.mode, label=label, kind=kind)
            record["ok"] = True
        except Exception as e:  # noqa: BLE001
            record["error"] = str(e)
            log.exception("page failed for %s", prayer_name)
        _last_page = record


def page_now(prayer_name: str = "dhuhr", kind: str = "adhan"):
    """Trigger a page immediately (test button / manual)."""
    threading.Thread(target=_do_page, args=(prayer_name, kind),
                     daemon=True, name="manual-page").start()


def _do_page_file(file_name: str):
    """Page a specific audio file right now (test-play of one file)."""
    global _last_page
    with _page_lock:
        cfg = config.get()
        wav = audio.path_for(file_name)
        dests = _enabled_destinations()
        started = ntp.corrected_now(cfg.location.timezone)
        record = {"prayer": "(test)", "kind": "test", "file": file_name,
                  "time": started.isoformat(), "destinations": dests,
                  "audio": wav, "ok": False, "error": None}
        if not wav:
            record["error"] = f"File not found: {file_name}"
            log.error("test-play skipped: %s not found", file_name)
            _last_page = record
            return
        if not dests:
            record["error"] = "No enabled destinations"
            log.error("test-play skipped: no destinations")
            _last_page = record
            return
        try:
            log.info("TEST-PLAY %s -> %s", file_name, dests)
            sip_engine.get_engine().page(wav, dests, cfg.call.mode,
                                         label=f"Test · {file_name}", kind="test")
            record["ok"] = True
        except Exception as e:  # noqa: BLE001
            record["error"] = str(e)
            log.exception("test-play failed for %s", file_name)
        _last_page = record


def play_file_now(file_name: str):
    """Trigger a test-play of one file immediately (per-file test button)."""
    threading.Thread(target=_do_page_file, args=(file_name,),
                     daemon=True, name="test-play").start()


def _schedule_day(day_date, now, tag: str) -> int:
    """Add jobs for the given date whose times are still in the future. Returns count."""
    cfg = config.get()
    # today_schedule computes for now.date(); for tomorrow we pass a noon anchor.
    anchor = datetime.datetime.combine(
        day_date, datetime.time(0, 1), tzinfo=now.tzinfo)
    rows = prayer.today_schedule(anchor)
    count = 0
    for row in rows:
        if not row["enabled"] or row["time"] is None:
            continue
        name = row["prayer"]
        run_at = row["time"]
        if run_at > now:
            _scheduler.add_job(_do_page, "date", run_date=run_at,
                               args=[name, "adhan"], id=f"prayer:{tag}:{name}",
                               misfire_grace_time=120, replace_existing=True)
            count += 1
        if row.get("iqama_enabled") and row["iqama_time"] and row["iqama_time"] > now:
            _scheduler.add_job(_do_page, "date", run_date=row["iqama_time"],
                               args=[name, "iqama"], id=f"prayer:{tag}:{name}:iqama",
                               misfire_grace_time=120, replace_existing=True)
    return count


def reschedule():
    """Rebuild jobs for today's remaining prayers and tomorrow, from NTP time.

    Scheduling tomorrow as well means the dashboard's "next page" always has a
    value, even late at night after the last prayer has already fired.
    """
    if _scheduler is None:
        return
    for job in list(_scheduler.get_jobs()):
        if job.id.startswith("prayer:"):
            job.remove()

    cfg = config.get()
    now = ntp.corrected_now(cfg.location.timezone)
    today = _schedule_day(now.date(), now, "today")
    tomorrow = _schedule_day(now.date() + datetime.timedelta(days=1), now, "tomorrow")
    log.info("scheduled %d today + %d tomorrow prayer page(s) (as of %s)",
             today, tomorrow, now.strftime("%Y-%m-%d %H:%M"))


def _daily_refresh():
    log.info("daily refresh")
    reschedule()


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    tz = config.get().location.timezone
    _scheduler = BackgroundScheduler(timezone=tz)
    _scheduler.add_job(_daily_refresh, "cron", hour=0, minute=1,
                       id="daily-refresh", replace_existing=True)
    _scheduler.start()
    reschedule()
    return _scheduler


def upcoming_jobs() -> list[dict]:
    if _scheduler is None:
        return []
    out = []
    for job in _scheduler.get_jobs():
        if job.id.startswith("prayer:") and job.next_run_time:
            parts = job.id.split(":")  # prayer:<day>:<name>[:iqama]
            name = parts[2] if len(parts) > 2 else parts[-1]
            out.append({
                "prayer": name,
                "kind": "iqama" if job.id.endswith(":iqama") else "adhan",
                "run_at": job.next_run_time.isoformat(),
            })
    return sorted(out, key=lambda x: x["run_at"])


def last_page() -> dict | None:
    return _last_page
