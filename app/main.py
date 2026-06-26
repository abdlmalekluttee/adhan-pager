"""FastAPI application: REST API + serves the single-page admin UI."""
from __future__ import annotations

import datetime
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile, File, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import audio, config, ntp, prayer, scheduler, sip_engine, timetable, logbuf, auth

logbuf.setup(logging.INFO)
log = logging.getLogger("main")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
APP_VERSION = "1.7.0"   # single source of truth — also surfaced via /api/version
_ntp_stop = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.load()
    log.info("Adhan Pager starting")
    _se = auth.store_error()
    if _se:
        log.error("AUTH STORE ERROR: %s", _se)
    global _ntp_stop
    _ntp_stop = ntp.start()
    sip_engine.get_engine().start()
    scheduler.start()
    yield
    if _ntp_stop:
        _ntp_stop.set()
    sip_engine.get_engine().shutdown()


app = FastAPI(title="Adhan Pager", version=APP_VERSION, lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Authentication layer
# --------------------------------------------------------------------------- #
_PUBLIC_EXACT = {"/", "/favicon.ico", "/api/auth/login", "/api/auth/me", "/api/version"}
# While must_change_password is true, ONLY these endpoints are reachable.
_FORCED_ALLOW = {"/api/auth/me", "/api/auth/change-password", "/api/auth/logout"}


def _set_session_cookies(resp: Response, user: dict, remember: bool):
    max_age = auth.REMEMBER_MAX_AGE if remember else auth.DEFAULT_MAX_AGE
    resp.set_cookie(auth.SESSION_COOKIE, auth.issue_token(user), max_age=max_age,
                    httponly=True, samesite="lax", secure=auth.COOKIE_SECURE, path="/")
    resp.set_cookie(auth.CSRF_COOKIE, auth.new_csrf(), max_age=max_age,
                    httponly=False, samesite="lax", secure=auth.COOKIE_SECURE, path="/")


def _clear_session_cookies(resp: Response):
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    resp.delete_cookie(auth.CSRF_COOKIE, path="/")


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    request.state.user = auth.verify_token(request.cookies.get(auth.SESSION_COOKIE, ""))
    is_api = path.startswith("/api/")
    public = (path in _PUBLIC_EXACT) or path.startswith("/static/")

    if not public:
        user = request.state.user
        if not user:
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        # Forced password change: only session-check, password-change, logout allowed
        if user.get("must_change_password") and path not in _FORCED_ALLOW:
            return JSONResponse({"detail": "Password change required"}, status_code=403)
        # CSRF (double-submit) for state-changing requests
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            cok = request.cookies.get(auth.CSRF_COOKIE, "")
            if not cok or request.headers.get("x-csrf-token", "") != cok:
                return JSONResponse({"detail": "CSRF token missing or invalid"}, status_code=403)

    resp = await call_next(request)
    if is_api:
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/auth/login")
async def auth_login(request: Request, payload: dict):
    store_err = auth.store_error()
    if store_err:
        log.error("auth: %s", store_err)
        return JSONResponse(
            {"ok": False, "error": "Authentication store is unavailable. Check server logs."},
            status_code=503)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    remember = bool(payload.get("remember"))
    ip = request.client.host if request.client else "-"

    locked = auth.is_locked(username, ip)
    if locked:
        return JSONResponse(
            {"ok": False, "error": "Too many attempts. Try again later.",
             "locked_seconds": locked}, status_code=429)

    user = auth.authenticate(username, password)
    if not user:
        auth.record_fail(username, ip)
        log.info("auth: failed login for '%s' from %s", username[:32], ip)
        return JSONResponse({"ok": False, "error": "Invalid username or password"},
                            status_code=401)

    auth.reset_fails(username, ip)
    auth.mark_login(user)
    log.info("auth: login OK for '%s'", user["username"])
    body = {"ok": True, **auth.public_profile(user)}
    resp = JSONResponse(body)
    _set_session_cookies(resp, user, remember)
    return resp


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    user = request.state.user
    if user:
        auth.logout_user(user)  # bump token_version -> any copied cookie is now dead
        log.info("auth: logout for '%s'", user["username"])
    resp = JSONResponse({"ok": True})
    _clear_session_cookies(resp)
    return resp


@app.get("/api/auth/me")
async def auth_me(request: Request):
    user = request.state.user
    if not user:
        return {"authenticated": False}
    return {"authenticated": True, **auth.public_profile(user)}


@app.get("/api/version")
async def app_version():
    return {"version": APP_VERSION}


@app.post("/api/auth/change-password")
async def auth_change_password(request: Request, payload: dict):
    user = request.state.user
    err = auth.change_password(user, payload.get("current_password") or "",
                               payload.get("new_password") or "")
    if err:
        raise HTTPException(400, err)
    log.info("auth: password changed for '%s'", user["username"])
    resp = JSONResponse({"ok": True, **auth.public_profile(user)})
    _set_session_cookies(resp, user, False)  # re-issue (token_version bumped)
    return resp


@app.post("/api/auth/update-account")
async def auth_update_account(request: Request, payload: dict):
    user = request.state.user
    new_username = payload.get("username")
    prefs = payload.get("preferences") or {}
    renamed = bool(new_username and new_username != user["username"])
    err = auth.update_account(user, new_username, prefs)
    if err:
        raise HTTPException(400, err)
    resp = JSONResponse({"ok": True, **auth.public_profile(user)})
    if renamed:
        _set_session_cookies(resp, user, False)  # token_version bumped on rename
    return resp


@app.post("/api/auth/preferences")
async def auth_preferences(request: Request, payload: dict):
    user = request.state.user
    auth.save_preferences(user, payload or {})
    return {"ok": True, **auth.public_profile(user)}


# --------------------------------------------------------------------------- #
# Status & dashboard
# --------------------------------------------------------------------------- #
@app.get("/api/status")
def get_status():
    cfg = config.get()
    now = ntp.corrected_now(cfg.location.timezone)
    return {
        "now": now.isoformat(),
        "timezone": cfg.location.timezone,
        "sip": sip_engine.get_engine().status(),
        "active_call": sip_engine.get_engine().active_call(),
        "ntp": ntp.status(),
        "upcoming": scheduler.upcoming_jobs(),
        "last_page": scheduler.last_page(),
        "sip_available": sip_engine.SIP_AVAILABLE,
        "ffmpeg": audio.have_ffmpeg(),
    }


@app.get("/api/prayer-times")
def prayer_times(date: str | None = None):
    cfg = config.get()
    try:
        d = datetime.date.fromisoformat(date) if date else \
            ntp.corrected_now(cfg.location.timezone).date()
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")
    times = prayer.times_for_date(d)
    return {
        "date": d.isoformat(),
        "times": {k: (v.isoformat() if v is not None else None)
                  for k, v in times.items()},
        "source": cfg.prayer.source,
        "enabled": cfg.prayer.enabled_prayers,
        "iqama": cfg.prayer.iqama.model_dump(),
        "iqama_prayers": cfg.prayer.iqama_prayers,
    }


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@app.get("/api/config")
def get_config():
    return config.get().model_dump()


_NEEDS_SIP_RESTART = {"sip", "codecs"}
_NEEDS_RESCHEDULE = {"prayer", "location", "destinations", "audio"}


@app.post("/api/config/{section}")
async def update_config(section: str, payload: dict):
    valid = set(config.AppConfig.model_fields.keys())
    if section not in valid:
        raise HTTPException(404, f"unknown section '{section}'")
    data = payload.get("data", payload)
    # Location integrity for "by city" mode:
    #  - unknown country/city  -> fall back to manual coordinates
    #  - known country/city     -> overwrite coords/timezone with the DB record so a
    #                              direct API call can't store a real city with spoofed
    #                              coordinates.
    if section == "location" and isinstance(data, dict) and data.get("use_manual") is False:
        from . import geo
        rec = geo.city_record(data.get("country_code", ""), data.get("city", ""),
                              city_id=data.get("city_id", ""), region=data.get("region", ""))
        if not (geo.country_exists(data.get("country_code", "")) and rec):
            data["use_manual"] = True
            log.info("location: unknown country/city for city-mode — normalised to manual coordinates")
        else:
            data["city_id"] = rec["id"]
            data["latitude"] = rec["lat"]
            data["longitude"] = rec["lng"]
            data["timezone"] = rec["tz"]
            data["city"] = rec["name"]
            data["region"] = rec["region"]
            if rec["local_name"]:
                data["city_local_name"] = rec["local_name"]
    try:
        cfg = config.update_section(section, data)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        # surface a clean message for pydantic validation errors
        errs = getattr(e, "errors", None)
        if callable(errs):
            try:
                first = e.errors()[0]
                msg = first.get("msg", msg).replace("Value error, ", "")
            except Exception:  # noqa: BLE001
                pass
        raise HTTPException(400, msg)
    if section in _NEEDS_SIP_RESTART:
        sip_engine.get_engine().restart()
    if section in _NEEDS_RESCHEDULE or section in _NEEDS_SIP_RESTART:
        scheduler.reschedule()
    return {"ok": True, "config": cfg.model_dump()}


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
@app.post("/api/page-test")
def page_test(payload: dict | None = None):
    payload = payload or {}
    prayer_name = payload.get("prayer", "dhuhr")
    kind = payload.get("kind", "adhan")
    scheduler.page_now(prayer_name, kind)
    return {"ok": True, "message": f"Test page started for {prayer_name}"}


@app.post("/api/sip/reconnect")
def sip_reconnect():
    sip_engine.get_engine().restart()
    return {"ok": True, "sip": sip_engine.get_engine().status()}


@app.post("/api/ntp/sync")
def ntp_sync():
    return {"ok": True, "ntp": ntp.sync_once()}


# --------------------------------------------------------------------------- #
# Manual timetable (uploaded prayer-time file)
# --------------------------------------------------------------------------- #
@app.get("/api/schedule/manual")
def manual_status():
    return timetable.status()


@app.post("/api/schedule/manual/import")
async def manual_import(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(413, "file too large (max 5 MB)")
    try:
        text = data.decode("utf-8", errors="replace")
        summary = timetable.import_text(text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"could not parse file: {e}")
    # Switch to manual mode and rebuild today's jobs.
    config.update_section("prayer", {"source": "manual"})
    scheduler.reschedule()
    return {"ok": True, **summary}


@app.post("/api/schedule/test-play")
def schedule_test_play(payload: dict | None = None):
    """Test-play a single file to the enabled destinations right now."""
    payload = payload or {}
    file_name = payload.get("file")
    if not file_name:
        raise HTTPException(400, "missing 'file'")
    scheduler.play_file_now(file_name)
    return {"ok": True, "message": f"Test play started: {file_name}"}


@app.post("/api/page/stop")
def stop_page():
    """Immediately stop/drop any in-progress page or test call."""
    res = sip_engine.get_engine().stop_active()
    return {"ok": True, **res}


# --------------------------------------------------------------------------- #
# Geo (offline country/city picker)
# --------------------------------------------------------------------------- #
@app.get("/api/geo/countries")
def geo_countries():
    from . import geo
    return {"countries": geo.countries()}


@app.get("/api/geo/cities")
def geo_cities(country: str, q: str = "", limit: int = 50):
    from . import geo
    return {
        "cities": geo.cities(country, q, min(max(limit, 1), 100)),
        "recommended_method": geo.recommended_method(country),
    }


# --------------------------------------------------------------------------- #
# Logs
# --------------------------------------------------------------------------- #
@app.get("/api/logs")
def get_logs(limit: int = 300):
    return {"lines": logbuf.recent(limit)}


@app.get("/api/logs/export")
def export_logs():
    text = logbuf.export_text()
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return PlainTextResponse(
        text,
        headers={"Content-Disposition": f'attachment; filename="adhan-pager-{ts}.log"'},
    )


# --------------------------------------------------------------------------- #
# Audio / files
# --------------------------------------------------------------------------- #
@app.get("/api/audio")
def list_audio():
    return {"files": audio.list_files(),
            "assignments": config.get().audio.model_dump()}


@app.post("/api/audio/upload")
async def upload_audio(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(413, "file too large (max 50 MB)")
    try:
        result = audio.save_upload(file.filename, data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, str(e))
    return {"ok": True, **result}


@app.delete("/api/audio/{name}")
def delete_audio(name: str):
    audio.delete_file(name)
    return {"ok": True}


@app.get("/api/audio/file/{name}")
def get_audio(name: str):
    path = audio.path_for(name)
    if not path:
        raise HTTPException(404, "not found")
    return FileResponse(path)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
