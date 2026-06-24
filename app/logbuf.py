"""Application logging: console + persistent rotating file + in-memory buffer.

* The rotating file lives next to the config (so it survives restarts via the
  mounted /config volume) and can be downloaded from the UI for debugging.
* The in-memory ring buffer powers the live "Logs" view without re-reading the
  file on every poll.
"""
from __future__ import annotations

import collections
import logging
import logging.handlers
import os

from . import config

LOG_PATH = os.environ.get("LOG_PATH", "/config/adhan-pager.log")
_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_ring: "collections.deque[str]" = collections.deque(maxlen=2000)
_configured = False


class _RingHandler(logging.Handler):
    def emit(self, record):
        try:
            _ring.append(self.format(record))
        except Exception:  # noqa: BLE001
            pass


def setup(level: int = logging.INFO) -> None:
    """Install console + file + ring handlers on the root logger (idempotent)."""
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(_FMT)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        fileh = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except Exception as e:  # noqa: BLE001
        root.warning("could not open log file %s: %s", LOG_PATH, e)

    ring = _RingHandler()
    ring.setFormatter(fmt)
    root.addHandler(ring)
    _configured = True


def recent(limit: int = 300) -> list[str]:
    if limit <= 0:
        return list(_ring)
    return list(_ring)[-limit:]


def export_text() -> str:
    """Full on-disk log (all rotated parts, oldest first) for download."""
    parts = []
    for path in (f"{LOG_PATH}.3", f"{LOG_PATH}.2", f"{LOG_PATH}.1", LOG_PATH):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    parts.append(f.read())
            except Exception:  # noqa: BLE001
                pass
    if not parts:
        return "\n".join(_ring)
    return "".join(parts)
