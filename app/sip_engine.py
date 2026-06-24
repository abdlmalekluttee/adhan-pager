"""SIP engine (PJSIP / pjsua2).

Responsibilities
----------------
* Maintain one registered SIP account against the PBX/registrar.
* Apply codec priorities from config.
* "Page": dial one or more destinations, play an Adhan WAV into the call, and
  hang up automatically the moment the file reaches end-of-file.

pjsua2 delivers callbacks on its own worker threads. Any of *our* threads that
call into the library (scheduler, web request handlers) must first register
themselves with `ep.libRegisterThread()` — handled by `_register_thread()`.

The module imports pjsua2 lazily so the rest of the app (and the web UI) still
runs in environments where the native library isn't built — useful for config
editing and testing. `SIP_AVAILABLE` reports which mode we're in.
"""
from __future__ import annotations

import logging
import threading
import time

from . import audio, config

log = logging.getLogger("sip")

try:
    import pjsua2 as pj
    SIP_AVAILABLE = True
except Exception as _e:  # noqa: BLE001
    pj = None
    SIP_AVAILABLE = False
    _IMPORT_ERROR = str(_e)

_thread_local = threading.local()
_endpoint_gen = 0  # bumped every time a new pjsua2 endpoint is created


def _register_thread() -> None:
    """Ensure the current OS thread is known to pjsua2 before calling into it.

    Registration is per-endpoint-instance: after a restart() (libDestroy +
    libCreate) every prior registration is void, so we re-register whenever the
    endpoint "generation" has advanced since this thread last registered.
    """
    if not SIP_AVAILABLE:
        return
    if getattr(_thread_local, "registered_gen", None) == _endpoint_gen:
        return
    try:
        ep = Endpoint.instance
        if ep and not ep.libIsThreadRegistered():
            ep.libRegisterThread(threading.current_thread().name)
        _thread_local.registered_gen = _endpoint_gen
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# pjsua2 subclasses
# --------------------------------------------------------------------------- #
if SIP_AVAILABLE:

    class _Player(pj.AudioMediaPlayer):
        """Audio player that flags when playback finishes (no loop)."""

        def __init__(self, on_eof):
            super().__init__()
            self._on_eof = on_eof

        def onEof2(self):  # noqa: N802 (pjsua2 naming)
            try:
                self._on_eof()
            except Exception:  # noqa: BLE001
                pass

    class _Call(pj.Call):
        """A single outbound paging call: play file on answer, hang up on EOF."""

        def __init__(self, acc, wav_path: str, done_evt: threading.Event, engine=None, label: str = ""):
            super().__init__(acc)
            self.wav_path = wav_path
            self.done = done_evt
            self.engine = engine
            self.label = label
            self.player: _Player | None = None
            self._playing = False
            self.connected = False

        def onCallState(self, prm):  # noqa: N802
            try:
                ci = self.getInfo()
            except Exception:  # noqa: BLE001
                return
            log.info("call to %s: %s", ci.remoteUri, ci.stateText)
            if ci.state == pj.PJSIP_INV_STATE_CONFIRMED:
                self.connected = True
                if self.engine:
                    self.engine._on_call_connected(self.remote_label(ci))
            elif ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                self._cleanup()
                self.done.set()

        def remote_label(self, ci) -> str:
            try:
                return ci.remoteUri
            except Exception:  # noqa: BLE001
                return self.label

        def onCallMediaState(self, prm):  # noqa: N802
            ci = self.getInfo()
            for i, mi in enumerate(ci.media):
                if (mi.type == pj.PJMEDIA_TYPE_AUDIO and
                        mi.status == pj.PJSUA_CALL_MEDIA_ACTIVE and
                        not self._playing):
                    if self.engine:
                        self.engine._on_call_codec(self._codec_name())
                    self._start_playback(i)

        def _codec_name(self) -> str:
            try:
                si = self.getStreamInfo(0)
                name = getattr(si, "codecName", "") or ""
                clock = getattr(si, "codecClockRate", 0) or 0
                return f"{name}/{clock}" if clock else name
            except Exception:  # noqa: BLE001
                return ""

        def _start_playback(self, media_index: int):
            cfg = config.get().call
            try:
                call_media = self.getAudioMedia(media_index)
            except Exception as e:  # noqa: BLE001
                log.error("no call audio media: %s", e)
                return
            if cfg.answer_delay_ms:
                time.sleep(cfg.answer_delay_ms / 1000.0)
            self.player = _Player(self._on_audio_finished)
            try:
                self.player.createPlayer(self.wav_path, pj.PJMEDIA_FILE_NO_LOOP)
                self.player.startTransmit(call_media)
                self._playing = True
                log.info("playing %s into call", self.wav_path)
            except Exception as e:  # noqa: BLE001
                log.error("playback failed: %s", e)
                self._hangup()

        def _on_audio_finished(self):
            cfg = config.get().call
            log.info("audio finished -> hanging up")
            # Defer the hangup off the media callback thread.
            def _later():
                _register_thread()
                if cfg.hangup_after_eof_ms:
                    time.sleep(cfg.hangup_after_eof_ms / 1000.0)
                self._hangup()
            threading.Thread(target=_later, daemon=True).start()

        def _hangup(self):
            try:
                op = pj.CallOpParam()
                op.statusCode = pj.PJSIP_SC_OK
                self.hangup(op)
            except Exception:  # noqa: BLE001
                pass

        def _cleanup(self):
            try:
                if self.player:
                    self.player = None
            except Exception:  # noqa: BLE001
                pass

    class _Account(pj.Account):
        def __init__(self):
            super().__init__()
            self.reg_active = False
            self.reg_code = 0
            self.reg_reason = ""

        def onRegState(self, prm):  # noqa: N802
            ai = self.getInfo()
            self.reg_active = ai.regIsActive
            self.reg_code = prm.code
            self.reg_reason = prm.reason
            log.info("registration: active=%s code=%s %s",
                     ai.regIsActive, prm.code, prm.reason)


# --------------------------------------------------------------------------- #
# Endpoint wrapper
# --------------------------------------------------------------------------- #
class Endpoint:
    instance: "pj.Endpoint | None" = None  # raw pjsua2 endpoint
    _wrapper: "Endpoint | None" = None

    def __init__(self):
        self.ep = None
        self.account = None
        self._lock = threading.RLock()
        self._active: dict | None = None          # current page in progress
        self._active_lock = threading.Lock()
        self._active_calls = []                   # live _Call objects (for manual stop)
        self._stop_requested = False
        self.last_error = "" if SIP_AVAILABLE else f"pjsua2 not available: {_IMPORT_ERROR}"

    # -- active-call bookkeeping ------------------------------------------- #
    def _set_active(self, info: dict | None):
        with self._active_lock:
            self._active = info

    def _register_call(self, call):
        with self._active_lock:
            self._active_calls.append(call)

    def _unregister_call(self, call):
        with self._active_lock:
            if call in self._active_calls:
                self._active_calls.remove(call)

    def stop_active(self) -> dict:
        """Immediately hang up any in-progress page/call (manual operator stop)."""
        if not SIP_AVAILABLE:
            return {"stopped": 0, "available": False}
        _register_thread()
        self._stop_requested = True
        with self._active_lock:
            if self._active is not None:
                self._active["stopping"] = True
            calls = list(self._active_calls)
        n = 0
        for c in calls:
            try:
                c.hangup(pj.CallOpParam())
                n += 1
            except Exception as e:  # noqa: BLE001
                log.warning("manual stop: hangup failed: %s", e)
        log.info("manual stop requested by operator — hung up %d active call(s)", n)
        return {"stopped": n, "available": True}

    def _on_call_connected(self, remote: str):
        with self._active_lock:
            if self._active is not None:
                self._active["connected"] = True
                self._active["connected_at"] = time.time()
                if remote:
                    self._active["remote"] = remote

    def _on_call_codec(self, codec: str):
        with self._active_lock:
            if self._active is not None and codec:
                self._active["codec"] = codec

    def active_call(self) -> dict | None:
        """Snapshot of the page currently on the line, with time remaining."""
        with self._active_lock:
            a = self._active
            if not a:
                return None
            out = dict(a)
        dur = out.get("duration")
        if out.get("connected") and out.get("connected_at") and dur:
            elapsed = time.time() - out["connected_at"]
            out["elapsed_s"] = round(max(0.0, elapsed), 1)
            out["remaining_s"] = round(max(0.0, dur - elapsed), 1)
        else:
            out["elapsed_s"] = (round(time.time() - out["started_at"], 1)
                                if out.get("started_at") else None)
            out["remaining_s"] = None
        out.pop("connected_at", None)
        out["stopping"] = bool(a.get("stopping"))
        return out

    # -- lifecycle ---------------------------------------------------------- #
    def start(self):
        if not SIP_AVAILABLE:
            log.warning("SIP engine disabled: %s", self.last_error)
            return
        with self._lock:
            if self.ep is not None:
                return
            ep = pj.Endpoint()
            ep.libCreate()
            ep_cfg = pj.EpConfig()
            ep_cfg.uaConfig.threadCnt = 1
            ep_cfg.uaConfig.userAgent = "AdhanPager/1.0"
            ep_cfg.logConfig.level = 3
            ep.libInit(ep_cfg)

            self._create_transport(ep)
            ep.libStart()
            # No physical sound card in a container: route through a null device
            # so the conference bridge / file player works headless.
            try:
                ep.audDevManager().setNullDev()
            except Exception as e:  # noqa: BLE001
                log.warning("could not set null audio device: %s", e)
            self.ep = ep
            Endpoint.instance = ep
            global _endpoint_gen
            _endpoint_gen += 1
            self._apply_codecs()
            self._create_account()
            log.info("SIP endpoint started")

    def _create_transport(self, ep):
        cfg = config.get().sip
        tcfg = pj.TransportConfig()
        tcfg.port = cfg.local_port
        ttype = {
            "udp": pj.PJSIP_TRANSPORT_UDP,
            "tcp": pj.PJSIP_TRANSPORT_TCP,
            "tls": pj.PJSIP_TRANSPORT_TLS,
        }.get(cfg.transport, pj.PJSIP_TRANSPORT_UDP)
        ep.transportCreate(ttype, tcfg)

    def _apply_codecs(self):
        cfg = config.get().codecs
        try:
            available = {c.codecId: c for c in self.ep.codecEnum2()}
        except Exception:  # noqa: BLE001
            available = {}
        for codec_id, prio in cfg.priorities.items():
            match = None
            for avail_id in available:
                if avail_id.lower().startswith(codec_id.split("/")[0].lower()):
                    if avail_id == codec_id or codec_id in avail_id:
                        match = avail_id
                        break
                    match = match or avail_id
            target = match or codec_id
            try:
                self.ep.codecSetPriority(target, max(0, min(254, int(prio))))
            except Exception as e:  # noqa: BLE001
                log.debug("codec %s not set: %s", target, e)

    def _create_account(self):
        cfg = config.get().sip
        if not cfg.enabled:
            return
        acfg = pj.AccountConfig()
        acfg.idUri = f"sip:{cfg.username}@{cfg.registrar}"
        acfg.regConfig.registrarUri = f"sip:{cfg.registrar}:{cfg.port}"
        acfg.regConfig.timeoutSec = cfg.register_expires
        if cfg.display_name:
            acfg.idUri = f'"{cfg.display_name}" <sip:{cfg.username}@{cfg.registrar}>'
        if cfg.proxy:
            proxy = cfg.proxy if cfg.proxy.startswith("sip:") else f"sip:{cfg.proxy}"
            acfg.sipConfig.proxies.append(f"{proxy};lr")
        cred = pj.AuthCredInfo("digest", "*", cfg.auth_user or cfg.username,
                               0, cfg.password)
        acfg.sipConfig.authCreds.append(cred)

        acc = _Account()
        acc.create(acfg)
        self.account = acc

    def restart(self):
        """Tear down and rebuild the endpoint (after SIP/codec config changes)."""
        _register_thread()
        with self._lock:
            self.shutdown()
            self.start()

    def shutdown(self):
        _register_thread()
        with self._lock:
            if self.ep is None:
                return
            try:
                self.account = None
                self.ep.libDestroy()
            except Exception as e:  # noqa: BLE001
                log.warning("shutdown error: %s", e)
            finally:
                self.ep = None
                Endpoint.instance = None

    # -- status ------------------------------------------------------------- #
    def status(self) -> dict:
        if not SIP_AVAILABLE:
            return {"available": False, "registered": False,
                    "detail": self.last_error}
        with self._lock:
            if self.ep is None:
                return {"available": True, "running": False, "registered": False}
            # IMPORTANT: never call into pjsua2 (e.g. account.getInfo()) from here.
            # This runs on a uvicorn worker thread that pjlib doesn't know about,
            # and any pjsua2 call from an unregistered thread aborts the process
            # ("Calling pjlib from unknown/external thread"). We instead read the
            # plain-Python attributes that onRegState() caches on the PJSIP thread.
            reg = bool(getattr(self.account, "reg_active", False)) if self.account else False
            code = getattr(self.account, "reg_code", 0) if self.account else 0
            reason = getattr(self.account, "reg_reason", "") if self.account else ""
            cfg = config.get().sip
            return {
                "available": True,
                "running": True,
                "registered": reg,
                "reg_code": code,
                "reg_reason": reason,
                "identity": f"{cfg.username}@{cfg.registrar}",
                "transport": cfg.transport,
            }

    # -- paging ------------------------------------------------------------- #
    def page(self, wav_path: str, destinations: list[str], mode: str = "sequential",
             label: str = "", kind: str = "adhan"):
        """Dial destinations and play `wav_path`. Blocks until all calls end."""
        if not SIP_AVAILABLE:
            raise RuntimeError("SIP engine not available (pjsua2 not built)")
        with self._lock:
            if self.ep is None or self.account is None:
                raise RuntimeError("SIP not started / no account")
        _register_thread()
        from . import audio as _audio
        self._stop_requested = False
        self._set_active({
            "label": label or kind,
            "kind": kind,
            "destinations": destinations,
            "mode": mode,
            "audio": wav_path,
            "duration": _audio.duration_of(wav_path),
            "started_at": time.time(),
            "connected": False,
            "codec": "",
            "remote": "",
        })
        try:
            if mode == "parallel":
                self._page_parallel(wav_path, destinations, label, kind)
            else:
                for dst in destinations:
                    if self._stop_requested:
                        break
                    self._dial_one(wav_path, dst, label, kind)
        finally:
            self._set_active(None)
            self._stop_requested = False

    def _dest_uri(self, dst: str) -> str:
        cfg = config.get().sip
        dst = dst.strip()
        if dst.startswith("sip:"):
            return dst
        if "@" in dst:
            return f"sip:{dst}"
        return f"sip:{dst}@{cfg.registrar}"

    def _dial_one(self, wav_path: str, dst: str, label: str = "", kind: str = "adhan"):
        cfg = config.get().call
        done = threading.Event()
        call = _Call(self.account, wav_path, done, engine=self, label=label)
        op = pj.CallOpParam(True)
        uri = self._dest_uri(dst)
        log.info("paging %s", uri)
        try:
            call.makeCall(uri, op)
        except Exception as e:  # noqa: BLE001
            log.error("makeCall failed for %s: %s", uri, e)
            return
        self._register_call(call)
        # Cap the call at the audio length (+ margin) or the safety max, whichever
        # is larger, so it never hangs the line if EOF/BYE is somehow missed.
        from . import audio as _audio
        dur = _audio.duration_of(wav_path) or 0
        hard = max(cfg.max_call_seconds, int(dur) + 10)
        timeout = cfg.ring_timeout + hard
        try:
            if not done.wait(timeout=timeout):
                log.warning("call to %s exceeded %ss, forcing hangup", uri, timeout)
                try:
                    call.hangup(pj.CallOpParam())
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._unregister_call(call)

    def _page_parallel(self, wav_path: str, destinations: list[str],
                       label: str = "", kind: str = "adhan"):
        threads = []
        for dst in destinations:
            t = threading.Thread(
                target=lambda d=dst: (_register_thread(),
                                      self._dial_one(wav_path, d, label, kind)),
                daemon=True, name=f"page-{dst}")
            t.start()
            threads.append(t)
        for t in threads:
            t.join()


def get_engine() -> Endpoint:
    if Endpoint._wrapper is None:
        Endpoint._wrapper = Endpoint()
    return Endpoint._wrapper
