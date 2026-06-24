"""NTP synchronisation.

Accurate time is essential — prayer times are computed to the minute. In a
container the host clock is usually fine, but this module lets the operator point
at a specific NTP server (e.g. a LAN time source) and keeps a software offset so
scheduling stays correct even when the container can't set the system clock.

If `set_system_clock` is enabled AND the container is privileged (CAP_SYS_TIME),
it also steps the OS clock; otherwise it applies the offset in software only.
"""
from __future__ import annotations

import datetime
import logging
import subprocess
import threading
import time
from zoneinfo import ZoneInfo

import ntplib

from . import config

log = logging.getLogger("ntp")

_offset_seconds: float = 0.0
_last_sync: datetime.datetime | None = None
_last_error: str = ""
_detail: dict = {}
_lock = threading.Lock()


def _ref_id_str(resp) -> str:
    """Human-readable reference identifier (server/source the peer syncs to)."""
    try:
        return ntplib.ref_id_to_text(resp.ref_id, resp.stratum)
    except Exception:  # noqa: BLE001
        return str(getattr(resp, "ref_id", ""))


def sync_once() -> dict:
    """Query the configured NTP server once, store the offset, optionally step clock."""
    global _offset_seconds, _last_sync, _last_error, _detail
    cfg = config.get().ntp
    if not cfg.enabled:
        return status()
    try:
        client = ntplib.NTPClient()
        resp = client.request(cfg.server, version=3, timeout=5)
        with _lock:
            _offset_seconds = resp.offset
            _last_sync = datetime.datetime.now(datetime.timezone.utc)
            _last_error = ""
            _detail = {
                "stratum": resp.stratum,
                "stratum_text": ntplib.stratum_to_text(resp.stratum)
                                if hasattr(ntplib, "stratum_to_text") else str(resp.stratum),
                "ref_id": _ref_id_str(resp),
                "root_delay_ms": round(resp.root_delay * 1000, 2),
                "root_dispersion_ms": round(resp.root_dispersion * 1000, 2),
                "round_trip_ms": round(resp.delay * 1000, 2),
                "precision": resp.precision,
                "leap": ntplib.leap_to_text(resp.leap) if hasattr(ntplib, "leap_to_text") else resp.leap,
                "ntp_version": resp.version,
                "tx_time": datetime.datetime.fromtimestamp(
                    resp.tx_time, datetime.timezone.utc).isoformat(),
            }
        log.info("NTP sync via %s offset=%.3fs stratum=%s ref=%s",
                 cfg.server, resp.offset, resp.stratum, _detail["ref_id"])

        if cfg.set_system_clock and abs(resp.offset) > 0.5:
            _try_step_system_clock(resp.tx_time)
    except Exception as e:  # noqa: BLE001
        with _lock:
            _last_error = str(e)
        log.warning("NTP sync failed (%s): %s", cfg.server, e)
    return status()


def _try_step_system_clock(unix_ts: float) -> None:
    """Best-effort: set the OS clock. Silently no-ops without privileges."""
    try:
        new = datetime.datetime.utcfromtimestamp(unix_ts).strftime("%Y-%m-%d %H:%M:%S")
        subprocess.run(["date", "-u", "-s", new], check=True,
                       capture_output=True, timeout=5)
        with _lock:
            global _offset_seconds
            _offset_seconds = 0.0
        log.info("Stepped system clock to %s UTC", new)
    except Exception as e:  # noqa: BLE001
        log.info("Could not set system clock (needs privileged container): %s", e)


def corrected_now(tz: str | None = None) -> datetime.datetime:
    """Current time corrected by the NTP offset, in the requested timezone."""
    with _lock:
        off = _offset_seconds
    zone = ZoneInfo(tz) if tz else datetime.timezone.utc
    return datetime.datetime.now(zone) + datetime.timedelta(seconds=off)


def status() -> dict:
    cfg = config.get().ntp
    with _lock:
        next_sync = None
        if _last_sync:
            next_sync = (_last_sync + datetime.timedelta(
                minutes=max(5, cfg.sync_interval_min))).isoformat()
        out = {
            "enabled": cfg.enabled,
            "server": cfg.server,
            "offset_ms": round(_offset_seconds * 1000, 1),
            "last_sync": _last_sync.isoformat() if _last_sync else None,
            "next_sync": next_sync,
            "sync_interval_min": cfg.sync_interval_min,
            "set_system_clock": cfg.set_system_clock,
            "error": _last_error or None,
            "system_time": datetime.datetime.now().astimezone().isoformat(),
            "synced": _last_sync is not None and not _last_error,
        }
        out.update(_detail)
        return out


def run_loop(stop: threading.Event) -> None:
    """Background thread: sync now, then every sync_interval_min."""
    while not stop.is_set():
        cfg = config.get().ntp
        if cfg.enabled:
            sync_once()
        interval = max(5, config.get().ntp.sync_interval_min) * 60
        stop.wait(interval)


def start() -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=run_loop, args=(stop,), daemon=True,
                     name="ntp-loop").start()
    return stop
