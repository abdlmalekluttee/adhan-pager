"""Offline country/city geolocation backed by the bundled geonamescache data.

geonamescache ships the GeoNames *cities15000* dataset (~32k cities worldwide,
each with latitude/longitude/timezone/country/region) plus country names — all
on disk, so this works on a LAN with no internet access.

Exposes:
  countries()                 -> [{code, name, flag}]
  cities(cc, q, limit)        -> [{name, region, lat, lng, tz, population}]
  recommended_method(cc)      -> method string for the calculation dropdown
"""
from __future__ import annotations

import functools
import logging

log = logging.getLogger("geo")

# Country -> recommended adhanpy calculation method. Anything not listed falls
# back to MUSLIM_WORLD_LEAGUE. Method strings match the UI's METHODS list.
_METHOD_BY_COUNTRY = {
    "SA": "UMM_AL_QURA", "YE": "UMM_AL_QURA",
    "AE": "DUBAI",
    "KW": "KUWAIT",
    "QA": "QATAR",
    "EG": "EGYPTIAN", "LY": "EGYPTIAN", "SD": "EGYPTIAN", "SY": "EGYPTIAN",
    "LB": "EGYPTIAN", "JO": "EGYPTIAN", "IQ": "EGYPTIAN", "PS": "EGYPTIAN",
    "DZ": "EGYPTIAN", "TN": "EGYPTIAN", "MA": "EGYPTIAN",
    "PK": "KARACHI", "IN": "KARACHI", "BD": "KARACHI", "AF": "KARACHI",
    "SG": "SINGAPORE", "MY": "SINGAPORE", "ID": "SINGAPORE", "BN": "SINGAPORE",
    "US": "NORTH_AMERICA", "CA": "NORTH_AMERICA",
    "FR": "UOIF",
}

# Arabic country names for the most relevant countries (fallback = English name).
_COUNTRY_AR = {
    "LY": "ليبيا", "EG": "مصر", "SA": "السعودية", "AE": "الإمارات", "KW": "الكويت",
    "QA": "قطر", "BH": "البحرين", "OM": "عُمان", "YE": "اليمن", "IQ": "العراق",
    "SY": "سوريا", "JO": "الأردن", "LB": "لبنان", "PS": "فلسطين", "SD": "السودان",
    "TN": "تونس", "DZ": "الجزائر", "MA": "المغرب", "MR": "موريتانيا", "SO": "الصومال",
    "DJ": "جيبوتي", "KM": "جزر القمر", "TR": "تركيا", "IR": "إيران", "PK": "باكستان",
    "AF": "أفغانستان", "IN": "الهند", "BD": "بنغلاديش", "ID": "إندونيسيا", "MY": "ماليزيا",
    "NG": "نيجيريا", "ML": "مالي", "NE": "النيجر", "TD": "تشاد", "SN": "السنغال",
    "US": "الولايات المتحدة", "GB": "المملكة المتحدة", "FR": "فرنسا", "DE": "ألمانيا",
    "IT": "إيطاليا", "ES": "إسبانيا", "CN": "الصين", "RU": "روسيا", "BR": "البرازيل",
    "CA": "كندا", "AU": "أستراليا", "ZA": "جنوب أفريقيا", "KE": "كينيا", "ET": "إثيوبيا",
}


def _arabic_alt(alternatenames) -> str:
    """First alternate name written in Arabic script, if any."""
    if not alternatenames:
        return ""
    if isinstance(alternatenames, str):
        alternatenames = alternatenames.split(",")
    for nm in alternatenames:
        if any("\u0600" <= ch <= "\u06FF" for ch in nm):
            return nm.strip()
    return ""


# Curated Arabic names for common Muslim-world cities where GeoNames alternate
# names are missing or use a less-standard spelling. Keyed by (country, lowercase name).
_CITY_AR = {
    ("SA", "mecca"): "مكة المكرمة", ("SA", "makkah"): "مكة المكرمة",
    ("SA", "medina"): "المدينة المنورة", ("SA", "al madinah"): "المدينة المنورة",
    ("SA", "riyadh"): "الرياض", ("SA", "jeddah"): "جدة", ("SA", "jiddah"): "جدة",
    ("EG", "cairo"): "القاهرة", ("EG", "alexandria"): "الإسكندرية", ("EG", "giza"): "الجيزة",
    ("LY", "tripoli"): "طرابلس", ("LY", "benghazi"): "بنغازي", ("LY", "misratah"): "مصراتة",
    ("TR", "istanbul"): "إسطنبول", ("TR", "ankara"): "أنقرة",
    ("AE", "dubai"): "دبي", ("AE", "abu dhabi"): "أبو ظبي", ("AE", "sharjah"): "الشارقة",
    ("QA", "doha"): "الدوحة", ("KW", "kuwait city"): "مدينة الكويت", ("KW", "al kuwayt"): "مدينة الكويت",
    ("BH", "manama"): "المنامة", ("OM", "muscat"): "مسقط", ("JO", "amman"): "عمّان",
    ("IQ", "baghdad"): "بغداد", ("SY", "damascus"): "دمشق", ("LB", "beirut"): "بيروت",
    ("PS", "jerusalem"): "القدس", ("PS", "gaza"): "غزة", ("TN", "tunis"): "تونس",
    ("DZ", "algiers"): "الجزائر", ("MA", "casablanca"): "الدار البيضاء", ("MA", "rabat"): "الرباط",
}


def _local_city(cc: str, name: str, alternatenames) -> str:
    return _CITY_AR.get((cc.upper(), name.strip().lower()), "") or _arabic_alt(alternatenames)


@functools.lru_cache(maxsize=1)
def _cache():
    import geonamescache
    gc = geonamescache.GeonamesCache()
    countries = gc.get_countries()
    cities = gc.get_cities()
    # Pre-index cities by country code (for filtering) and by geonameid (stable lookup).
    by_country: dict[str, list] = {}
    by_id: dict[str, dict] = {}
    for c in cities.values():
        by_country.setdefault(c["countrycode"], []).append(c)
        by_id[str(c["geonameid"])] = c
    for lst in by_country.values():
        lst.sort(key=lambda c: c.get("population", 0), reverse=True)
    return countries, by_country, by_id


def _flag(cc: str) -> str:
    """Emoji flag from an ISO 3166-1 alpha-2 code (e.g. 'LY' -> 🇱🇾)."""
    cc = (cc or "").upper()
    if len(cc) != 2 or not cc.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in cc)


def countries() -> list[dict]:
    try:
        countries, by_country, _by_id = _cache()
    except Exception as e:  # noqa: BLE001
        log.warning("geo dataset unavailable: %s", e)
        return []
    out = []
    for code, info in countries.items():
        if code not in by_country:      # skip countries with no cities in the set
            continue
        out.append({"code": code, "name": info["name"],
                    "local_name": _COUNTRY_AR.get(code, ""), "flag": _flag(code)})
    out.sort(key=lambda x: x["name"])
    return out


def recommended_method(country_code: str) -> str:
    return _METHOD_BY_COUNTRY.get((country_code or "").upper(), "MUSLIM_WORLD_LEAGUE")


def country_exists(country_code: str) -> bool:
    try:
        countries, by_country, _by_id = _cache()
    except Exception:  # noqa: BLE001
        return False
    cc = (country_code or "").upper()
    return cc in by_country and cc in countries


def city_exists(country_code: str, city: str) -> bool:
    try:
        _, by_country, _by_id = _cache()
    except Exception:  # noqa: BLE001
        return False
    cc = (country_code or "").upper()
    name = (city or "").strip().lower()
    if not name:
        return False
    return any(c["name"].lower() == name for c in by_country.get(cc, []))


def _format_record(cc: str, c: dict) -> dict:
    return {
        "id": str(c["geonameid"]),
        "name": c["name"],
        "local_name": _local_city(cc, c["name"], c.get("alternatenames")),
        "region": c.get("admin1code", "") or "",
        "lat": round(float(c["latitude"]), 5),
        "lng": round(float(c["longitude"]), 5),
        "tz": c.get("timezone", "") or "",
    }


def city_record(country_code: str, city: str = "", city_id: str = "",
                region: str = "") -> dict | None:
    """Canonical DB record for a saved selection.

    Resolution order (so duplicate city names never pick the wrong place):
      1. stable geonameid (city_id) — must belong to the given country
      2. country + name + region (admin1) exact match
      3. country + name, highest population (legacy fallback)
    Returns None if nothing safe matches.
    """
    try:
        _, by_country, by_id = _cache()
    except Exception:  # noqa: BLE001
        return None
    cc = (country_code or "").upper()

    # 1. by stable id
    if city_id:
        c = by_id.get(str(city_id))
        if c and c.get("countrycode", "").upper() == cc:
            return _format_record(cc, c)

    name = (city or "").strip().lower()
    if not name:
        return None
    matches = [c for c in by_country.get(cc, []) if c["name"].lower() == name]
    if not matches:
        return None
    # 2. disambiguate by region when provided
    if region:
        reg = str(region).strip().lower()
        regional = [c for c in matches if str(c.get("admin1code", "")).lower() == reg]
        if regional:
            matches = regional
    # 3. highest population
    best = max(matches, key=lambda c: c.get("population", 0))
    return _format_record(cc, best)


def cities(country_code: str, q: str = "", limit: int = 50) -> list[dict]:
    try:
        countries, by_country, _by_id = _cache()
    except Exception as e:  # noqa: BLE001
        log.warning("geo dataset unavailable: %s", e)
        return []
    cc = (country_code or "").upper()
    rows = by_country.get(cc, [])
    q = (q or "").strip().lower()
    out = []
    for c in rows:
        if q:
            name = c["name"].lower()
            alt = c.get("alternatenames") or ""
            if isinstance(alt, list):
                alt = " ".join(alt)
            if not (q in name or q in alt.lower()):
                continue
        out.append({
            "id": str(c["geonameid"]),
            "name": c["name"],
            "local_name": _local_city(cc, c["name"], c.get("alternatenames")),
            "region": c.get("admin1code", "") or "",
            "lat": round(float(c["latitude"]), 5),
            "lng": round(float(c["longitude"]), 5),
            "tz": c.get("timezone", "") or "",
            "population": c.get("population", 0),
        })
        if len(out) >= limit:
            break
    return out
