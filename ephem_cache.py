"""
ephem_cache.py — Ephemeris cache for Kepler planetary de-rotation.

Caches JPL Horizons VECTORS query results to disk so each unique
timestamp is only queried once.  Subsequent runs with the same input
files load from the local cache and work fully offline.

Cache location : ~/.kepler_ephem/cache/<PLANET>/<YYYY-MM-DD>.json
Cache format   : JSON, one entry per queried UTC minute
Cache lifetime : unlimited (Horizons vectors for past dates never change)

Pre-population (called by the installer):
    populate_cache(planet, year, site, log_callback)
    Downloads one year of ephemeris data for a planet at 2-minute
    resolution — enough for any de-rotation session.  ~350 KB per
    planet-year.
"""

import os
import json
import math
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

# ── Paths ─────────────────────────────────────────────────────────────────────

CACHE_ROOT = Path(os.path.expanduser("~")) / ".kepler_ephem" / "cache"

HORIZONS_FILE_URL = "https://ssd.jpl.nasa.gov/api/horizons_file.api"

PLANET_CODE = {
    "JUPITER": "599",
    "MARS":    "499",
    "SATURN":  "699",
}


# ── Cache I/O ─────────────────────────────────────────────────────────────────

def _cache_path(planet: str, date: datetime) -> Path:
    """Return the cache file path for a planet on a given UTC date."""
    # "v2" = equatorial (REF_PLANE=FRAME) planet->observer vectors.  v1 held
    # ecliptic observer->planet vectors; mixing them silently corrupts the
    # pole geometry, so the generation lives in the path.
    return (CACHE_ROOT / planet.upper() / "v2"
            / f"{date.strftime('%Y-%m-%d')}.json")


def _load_day_cache(planet: str, date: datetime) -> Dict:
    """Load the cache for a planet+date. Returns {} if not cached."""
    p = _cache_path(planet, date)
    if p.exists():
        try:
            with p.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_day_cache(planet: str, date: datetime, data: Dict) -> None:
    """Persist cache data for a planet+date."""
    p = _cache_path(planet, date)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f)


def _cache_key(dt: datetime) -> str:
    """Cache key = UTC time rounded to the nearest minute: 'HH:MM'."""
    return dt.strftime("%H:%M")


# ── Horizons query (single window) ────────────────────────────────────────────

def _query_horizons(planet: str, t0: datetime, t1: datetime,
                    site: Dict[str, float],
                    step: str = "1m") -> List[Dict]:
    """
    Query Horizons for planet→observer ICRF vectors from t0 to t1.
    Returns list of {jd, x, y, z} dicts.
    """
    code       = PLANET_CODE[planet.upper()]
    t0_str     = t0.strftime("%Y-%b-%d %H:%M")
    t1_str     = t1.strftime("%Y-%b-%d %H:%M")
    site_coord = f"{site['lon']:.6f},{site['lat']:.6f},{site['elev']:.1f}"

    batch = (
        "!$$SOF\n"
        f"COMMAND='{code}'\n"
        f"EPHEM_TYPE='VECTORS'\n"
        f"CENTER='coord@399'\n"
        f"SITE_COORD='{site_coord}'\n"
        f"START_TIME='{t0_str}'\n"
        f"STOP_TIME='{t1_str}'\n"
        f"STEP_SIZE='{step}'\n"
        f"VEC_TABLE='2'\n"
        f"OUT_UNITS='AU-D'\n"
        f"REF_FRAME='ICRF'\n"
        # REF_PLANE defaults to ECLIPTIC for VECTORS, but the IAU pole
        # constants are equatorial — without this the two frames disagree by
        # the 23.4° obliquity and every pole-derived quantity is wrong.
        f"REF_PLANE='FRAME'\n"
        f"REF_SYSTEM='J2000'\n"
        f"VEC_CORR='LT+S'\n"
        f"CSV_FORMAT='YES'\n"
        f"OBJ_DATA='NO'\n"
        f"MAKE_EPHEM='YES'\n"
    )

    boundary = "----KeplerEphem1234"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="format"\r\n\r\n'
        f"text\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="input"; filename="horizons.txt"\r\n'
        f"Content-Type: text/plain\r\n\r\n"
        f"{batch}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")

    req = urllib.request.Request(
        HORIZONS_FILE_URL,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent":   "Kepler-v1.3.5/ephem-cache",
        },
        method="POST")

    # Use certifi SSL context if available (needed for macOS python.org builds)
    try:
        import ssl, certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        import ssl, os
        cafile = os.environ.get("SSL_CERT_FILE")
        ctx = ssl.create_default_context(cafile=cafile) if cafile else None

    with urllib.request.urlopen(req, timeout=60,
                                **({"context": ctx} if ctx else {})) as resp:
        text = resp.read().decode("utf-8")

    return _parse_vectors(text)


def _parse_vectors(text: str) -> List[Dict]:
    """Parse $$SOE...$$EOE CSV block from Horizons VECTORS response."""
    results = []
    in_data = False
    for line in text.split("\n"):
        if "$$SOE" in line:
            in_data = True; continue
        if "$$EOE" in line:
            break
        if not in_data:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            # Horizons returns target-relative-to-center (observer→planet);
            # everything downstream wants planet→observer, so negate.
            results.append({
                "jd": float(parts[0]),
                "x":  -float(parts[2]),
                "y":  -float(parts[3]),
                "z":  -float(parts[4]),
            })
        except (ValueError, IndexError):
            continue
    return results


# ── Public API ────────────────────────────────────────────────────────────────

def get_vector(planet: str, dt: datetime,
               site: Dict[str, float]) -> Optional[Dict]:
    """
    Return the cached planet→observer ICRF vector for a given UTC time.
    Queries Horizons and caches the result if not already cached.

    Returns {'jd': float, 'x': float, 'y': float, 'z': float} or None.
    """
    planet = planet.upper()
    date   = dt.replace(hour=0, minute=0, second=0, microsecond=0,
                        tzinfo=timezone.utc)
    key    = _cache_key(dt)

    cache = _load_day_cache(planet, date)
    if key in cache:
        return cache[key]

    # Not cached — query a 3-minute window around the requested time
    t0  = dt - timedelta(minutes=1)
    t1  = dt + timedelta(minutes=2)
    rows = _query_horizons(planet, t0, t1, site, step="1m")

    if not rows:
        return None

    # Store all returned rows into the day cache
    for row in rows:
        # Reconstruct the UTC minute from the JD
        jd   = row["jd"]
        d    = jd - 2451545.0
        # Approximate UTC from JD (good to ~1 second for caching purposes)
        dt_r = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc) + timedelta(days=d)
        k    = _cache_key(dt_r)
        cache[k] = row

    _save_day_cache(planet, date, cache)
    return cache.get(key) or rows[0]


def is_day_cached(planet: str, date: datetime) -> bool:
    """Return True if a full day's ephemeris is cached for this planet+date."""
    p = _cache_path(planet.upper(), date)
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text())
        # A full day at 2-min resolution = ~720 entries
        return len(data) >= 600
    except Exception:
        return False


def populate_cache(planet: str, year: int, site: Dict[str, float],
                   log_callback=None, progress_callback=None,
                   should_stop=None) -> bool:
    """
    Pre-download one full year of ephemeris data for a planet.
    Called by the Kepler installer and by the De-rotate tab's
    "Download this year's ephemeris" button.

    Downloads in 10-day chunks to stay within Horizons request limits.
    Stores results in the local cache.

    should_stop, if given, is polled before each chunk; returning True aborts
    the download cleanly (returns False).

    Returns True on success, False on failure or cancellation.
    """
    def _log(msg):
        if log_callback: log_callback(msg)

    def _prog(pct):
        if progress_callback: progress_callback(pct)

    planet = planet.upper()
    _log(f"  Downloading {planet} ephemeris for {year}…")

    # Full year in 10-day chunks
    start = datetime(year, 1, 1, 0, 0, tzinfo=timezone.utc)
    end   = datetime(year, 12, 31, 23, 59, tzinfo=timezone.utc)

    chunk_days = 10
    total_days = (end - start).days + 1
    chunks     = math.ceil(total_days / chunk_days)
    done       = 0

    t = start
    while t < end:
        if should_stop is not None and should_stop():
            _log("  Canceled.")
            return False
        t1 = min(t + timedelta(days=chunk_days), end)

        # Check if all days in this chunk are already cached
        all_cached = all(
            is_day_cached(planet, t + timedelta(days=d))
            for d in range((t1 - t).days + 1))
        if all_cached:
            t = t1 + timedelta(minutes=1)
            done += chunk_days
            _prog(int(done / total_days * 100))
            continue

        try:
            rows = _query_horizons(planet, t, t1, site, step="2m")
        except Exception as e:
            _log(f"    ⚠ Query failed for {t.strftime('%Y-%m-%d')}: {e}")
            return False

        # Bin rows into day caches
        day_buckets: Dict[str, Dict] = {}
        for row in rows:
            jd   = row["jd"]
            d    = jd - 2451545.0
            dt_r = (datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
                    + timedelta(days=d))
            date_key = dt_r.strftime("%Y-%m-%d")
            hm_key   = _cache_key(dt_r)
            if date_key not in day_buckets:
                day_buckets[date_key] = {}
            day_buckets[date_key][hm_key] = row

        for date_str, data in day_buckets.items():
            date_obj = datetime.strptime(date_str, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
            # Merge with any existing data
            existing = _load_day_cache(planet, date_obj)
            existing.update(data)
            _save_day_cache(planet, date_obj, existing)

        done += chunk_days
        _prog(min(99, int(done / total_days * 100)))
        t = t1 + timedelta(minutes=1)

    _prog(100)
    _log(f"  ✔ {planet} ephemeris cached for {year} "
         f"({CACHE_ROOT / planet})")
    return True


def cache_size_mb(planet: Optional[str] = None) -> float:
    """Return total cache size in MB, optionally filtered by planet."""
    root = CACHE_ROOT / planet.upper() if planet else CACHE_ROOT
    if not root.exists():
        return 0.0
    total = sum(f.stat().st_size for f in root.rglob("*.json"))
    return total / 1_048_576
