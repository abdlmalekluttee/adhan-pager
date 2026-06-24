"""Audio file management.

Adhan recordings are uploaded here, normalised to the format PJSIP plays back
cleanly (16-bit signed PCM WAV, mono, at the configured sample rate), and listed
for assignment to individual prayers. Conversion uses ffmpeg.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import wave

from . import config

ALLOWED_UPLOAD = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".opus"}


def _audio_dir() -> str:
    os.makedirs(config.AUDIO_DIR, exist_ok=True)
    return config.AUDIO_DIR


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def list_files() -> list[dict]:
    d = _audio_dir()
    out = []
    for name in sorted(os.listdir(d)):
        if name.startswith("."):
            continue
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            continue
        info = {"name": name, "size": os.path.getsize(path), "duration": None,
                "rate": None, "channels": None, "playable": name.lower().endswith(".wav")}
        if info["playable"]:
            try:
                with wave.open(path, "rb") as w:
                    info["rate"] = w.getframerate()
                    info["channels"] = w.getnchannels()
                    frames = w.getnframes()
                    info["duration"] = round(frames / float(w.getframerate()), 1)
            except Exception:  # noqa: BLE001
                info["playable"] = False
        out.append(info)
    return out


def path_for(name: str) -> str | None:
    if not name:
        return None
    p = os.path.join(_audio_dir(), os.path.basename(name))
    return p if os.path.isfile(p) else None


def save_upload(filename: str, data: bytes) -> dict:
    """Store an uploaded file and convert it to a normalised .wav.

    The upload is written to a temporary source path that is always distinct
    from the final .wav, so ffmpeg never reads and writes the same file (which
    fails) — this matters when the upload is itself a .wav.
    """
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_UPLOAD:
        raise ValueError(f"Unsupported file type: {ext}")
    d = _audio_dir()
    base = os.path.splitext(os.path.basename(filename))[0]
    wav_name = f"{base}.wav"
    wav_path = os.path.join(d, wav_name)

    # Hidden temp source (kept out of the library listing, removed after convert).
    tmp_src = os.path.join(d, f".{base}.upload{ext}")
    with open(tmp_src, "wb") as f:
        f.write(data)
    try:
        _convert_to_wav(tmp_src, wav_path)
    finally:
        if os.path.exists(tmp_src) and os.path.abspath(tmp_src) != os.path.abspath(wav_path):
            os.remove(tmp_src)
    return {"name": wav_name}


def _convert_to_wav(src: str, dst: str) -> None:
    cfg = config.get().audio
    rate = cfg.target_rate if cfg.target_rate in (8000, 16000, 32000, 44100, 48000) else 16000
    if not have_ffmpeg():
        # No ffmpeg: only works if the source is already a usable wav.
        if src != dst:
            shutil.copyfile(src, dst)
        return
    gain = cfg.gain_db
    af = f"volume={gain}dB" if gain else "anull"
    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-ac", "1", "-ar", str(rate),
        "-af", af,
        "-acodec", "pcm_s16le",
        dst,
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=120)


def delete_file(name: str) -> None:
    p = path_for(name)
    if p:
        os.remove(p)


def resolve_for_prayer(prayer: str) -> str | None:
    """Pick the adhan wav to play for a prayer.

    If 'use one file for all adhans' is set, every prayer uses default_file.
    Otherwise use the prayer's assigned file, falling back to default_file.
    """
    cfg = config.get().audio
    if cfg.use_global_for_all:
        chosen = cfg.default_file
    else:
        chosen = cfg.per_prayer.get(prayer) or cfg.default_file
    return path_for(chosen)


def resolve_iqama_for_prayer(prayer: str) -> str | None:
    """Pick the iqama wav for a prayer: per-prayer iqama file, else global iqama."""
    cfg = config.get().audio
    chosen = cfg.iqama_per_prayer.get(prayer) or cfg.iqama_file
    return path_for(chosen)


def duration_of(path: str | None) -> float | None:
    """Length in seconds of a WAV file, or None if unknown."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with wave.open(path, "rb") as w:
            return round(w.getnframes() / float(w.getframerate()), 2)
    except Exception:  # noqa: BLE001
        return None
