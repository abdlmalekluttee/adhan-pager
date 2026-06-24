"""Configuration: typed models, YAML persistence, sane defaults.

The whole running configuration lives in one YAML file (CONFIG_PATH). The web UI
reads and writes sections of it; the rest of the app reads the in-memory model.
"""
from __future__ import annotations

import os
import threading
from typing import Dict, List, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/audio")
# Parsed manual prayer timetable (keyed by MM-DD), written by the importer.
MANUAL_TIMETABLE_PATH = os.environ.get(
    "MANUAL_TIMETABLE_PATH", "/config/manual_timetable.json"
)

PRAYERS = ["fajr", "dhuhr", "asr", "maghrib", "isha"]


class SipConfig(BaseModel):
    enabled: bool = True
    registrar: str = "sip.example.net"        # SIP domain / registrar host
    proxy: str = ""                            # optional outbound proxy host[:port]
    port: int = 5060
    transport: Literal["udp", "tcp", "tls"] = "udp"
    username: str = "9000"
    auth_user: str = "9000"                    # auth/realm username (often == username)
    password: str = ""
    display_name: str = "Adhan System"
    register_expires: int = 300
    local_port: int = 5060                     # local UDP/TCP port the UA binds


class Destination(BaseModel):
    name: str = "Paging"
    uri: str = "601"                           # extension number or full sip: URI
    enabled: bool = True


class CallConfig(BaseModel):
    mode: Literal["sequential", "parallel"] = "sequential"
    ring_timeout: int = 30                     # seconds to wait for answer
    max_call_seconds: int = 600                # hard safety cap on a single page
    answer_delay_ms: int = 1500                # pause after answer before audio starts
    hangup_after_eof_ms: int = 800             # pause after audio ends before hangup


class CodecConfig(BaseModel):
    # priority 1..254 (higher = preferred); 0 disables the codec entirely.
    priorities: Dict[str, int] = Field(
        default_factory=lambda: {
            "PCMA/8000/1": 240,   # G.711 a-law
            "PCMU/8000/1": 230,   # G.711 u-law
            "G722/16000/1": 250,  # wideband, best for Adhan if PBX supports it
            "opus/48000/2": 0,
            "speex/16000/1": 0,
            "iLBC/8000/1": 0,
            "GSM/8000/1": 0,
        }
    )


class LocationConfig(BaseModel):
    latitude: float = 0.0                      # neutral example default
    longitude: float = 0.0
    timezone: str = "Etc/UTC"
    # City-picker selection (purely informational + lets the UI restore the choice)
    country_code: str = ""
    country_name: str = ""
    country_local_name: str = ""               # e.g. ليبيا (restores Arabic display on reload)
    city: str = ""
    city_id: str = ""                          # stable GeoNames id (disambiguates duplicates)
    city_local_name: str = ""                  # e.g. طرابلس
    region: str = ""
    use_manual: bool = False                   # True = manual coords override the city

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        v = (v or "").strip()
        try:
            ZoneInfo(v)
        except Exception:
            raise ValueError(
                f"Invalid timezone '{v}'. Use a valid IANA timezone such as Etc/UTC."
            )
        return v

    @model_validator(mode="after")
    def _normalise_mode(self):
        # If "by city" is implied but there's no real country/city selection (old
        # coordinate-only configs, or a direct API payload), fall back to manual so
        # the UI never opens city-mode with nothing selected. Coordinates still work.
        if not self.use_manual and (not self.country_code or not self.city):
            self.use_manual = True
        return self


class IqamaConfig(BaseModel):
    enabled: bool = False
    minutes: int = 15                          # second page N minutes after adhan


class PrayerConfig(BaseModel):
    source: Literal["calculated", "manual"] = "calculated"  # astronomical vs uploaded timetable
    method: str = "UMM_AL_QURA"                # adhanpy CalculationMethod name
    madhab: Literal["SHAFI", "HANAFI"] = "SHAFI"
    high_latitude_rule: Literal[
        "MIDDLE_OF_THE_NIGHT", "SEVENTH_OF_THE_NIGHT", "TWILIGHT_ANGLE"
    ] = "MIDDLE_OF_THE_NIGHT"
    adjustments: Dict[str, int] = Field(
        default_factory=lambda: {p: 0 for p in PRAYERS}
    )
    enabled_prayers: Dict[str, bool] = Field(
        default_factory=lambda: {p: True for p in PRAYERS}
    )
    iqama: IqamaConfig = Field(default_factory=IqamaConfig)
    # Per-prayer iqama on/off (only consulted when iqama.enabled is True).
    iqama_prayers: Dict[str, bool] = Field(
        default_factory=lambda: {p: True for p in PRAYERS}
    )


class AudioConfig(BaseModel):
    default_file: str = ""                     # filename inside AUDIO_DIR
    use_global_for_all: bool = False           # if True, every adhan uses default_file
    per_prayer: Dict[str, str] = Field(
        default_factory=lambda: {p: "" for p in PRAYERS}
    )
    iqama_file: str = ""                        # global iqama tone/announcement (fallback)
    iqama_per_prayer: Dict[str, str] = Field(  # per-prayer iqama file (overrides iqama_file)
        default_factory=lambda: {p: "" for p in PRAYERS}
    )
    target_rate: int = 16000                   # 16000 for G722, 8000 for G711-only
    gain_db: float = 0.0


class NtpConfig(BaseModel):
    enabled: bool = True
    server: str = "pool.ntp.org"
    sync_interval_min: int = 60
    set_system_clock: bool = False             # needs privileged container + CAP_SYS_TIME


class WebConfig(BaseModel):
    bind: str = "0.0.0.0"
    port: int = 8080


class AppConfig(BaseModel):
    sip: SipConfig = Field(default_factory=SipConfig)
    destinations: List[Destination] = Field(
        default_factory=lambda: [Destination(name="Paging Group", uri="601")]
    )
    call: CallConfig = Field(default_factory=CallConfig)
    codecs: CodecConfig = Field(default_factory=CodecConfig)
    location: LocationConfig = Field(default_factory=LocationConfig)
    prayer: PrayerConfig = Field(default_factory=PrayerConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    ntp: NtpConfig = Field(default_factory=NtpConfig)
    web: WebConfig = Field(default_factory=WebConfig)


_lock = threading.RLock()
_config: AppConfig | None = None


def load() -> AppConfig:
    """Load config from disk (creating a default file on first run)."""
    global _config
    with _lock:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            _config = AppConfig(**raw)
        else:
            _config = AppConfig()
            _write(_config)
        return _config


def get() -> AppConfig:
    return _config if _config is not None else load()


def _write(cfg: AppConfig) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg.model_dump(), f, sort_keys=False, allow_unicode=True)
    os.replace(tmp, CONFIG_PATH)


def save(cfg: AppConfig) -> AppConfig:
    global _config
    with _lock:
        _config = cfg
        _write(cfg)
        return cfg


def update_section(section: str, data: dict) -> AppConfig:
    """Merge `data` into one top-level section and persist. Returns new config."""
    with _lock:
        cfg = get()
        current = cfg.model_dump()
        if section == "destinations":
            current["destinations"] = data  # list replace
        elif section in current and isinstance(current[section], dict):
            current[section].update(data)
        else:
            current[section] = data
        new_cfg = AppConfig(**current)
        return save(new_cfg)
