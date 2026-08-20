#!/usr/bin/env python3
"""
pvol_derotate.py  —  IAU-2018 / Horizons-based planetary de-rotation

De-rotates and co-adds planetary stacks for Jupiter (CM II), Mars (CM I)
and Saturn (CM III).

Algorithm:
  1. Parse UTC from each filename → query JPL Horizons for the
     planet→observer ICRF vector at each time.
  2. Compute the sub-observer CM longitude, sub-Earth latitude and pole
     position angle from the IAU-2018 pole orientation and prime meridian.
  3. Fit a wireframe to each frame for its center, scale, and the pole's
     orientation on the camera sensor.
  4. Warp every frame into the reference frame's geometry in a single
     interpolation pass: each line of sight is intersected with the planet's
     oblate ellipsoid and the surface point rotated about the polar axis
     by ΔCM.
  5. Accumulate a quality-weighted mean.
"""

import os
import re
import json
import math
import threading
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional

import numpy as np
from PIL import Image
from scipy.ndimage import map_coordinates

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ── Config / paths ────────────────────────────────────────────────────────────

CONFIG_DIR  = Path(os.path.expanduser("~")) / ".kepler_ephem"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_SITE = {
    "lat":  0.0,
    "lon":  0.0,
    "elev": 0.0,
}

PLANET_CM_SYSTEM = {
    "JUPITER": "CM2",
    "MARS":    "CM1",
    "SATURN":  "CM3",
}

# Horizons body codes
PLANET_CODE = {
    "JUPITER": "599",
    "MARS":    "499",
    "SATURN":  "699",
}

LON_MIN_DEG, LON_MAX_DEG = 0.0, 360.0
LAT_MIN_DEG, LAT_MAX_DEG = -90.0, 90.0

FILENAME_TIME_FORMAT = "%Y-%m-%d-%H%M"

HORIZONS_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"


# ── Config I/O ────────────────────────────────────────────────────────────────

def _read_config() -> Dict:
    """The whole config file; {} when it is missing or unreadable."""
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_config(data: Dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_config() -> Dict[str, float]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = _read_config()
    if data:
        try:
            return {k: float(data.get(k, DEFAULT_SITE[k])) for k in DEFAULT_SITE}
        except Exception:
            pass
    save_config(DEFAULT_SITE)
    return DEFAULT_SITE.copy()


def save_config(site: Dict[str, float]) -> None:
    # Merge instead of rewriting the file: it also carries saved window
    # positions, and a plain overwrite would silently drop them every time
    # the observer location was saved.
    data = _read_config()
    data.update({k: float(site[k]) for k in DEFAULT_SITE})
    _write_config(data)


def load_window_pos(key: str) -> Optional[Tuple[int, int]]:
    """Remembered top-left of a named window, or None if never saved."""
    try:
        x, y = _read_config()["window_pos"][key]
        return int(x), int(y)
    except Exception:
        return None


def save_window_pos(key: str, x: int, y: int) -> None:
    data = _read_config()
    slot = data.get("window_pos")
    if not isinstance(slot, dict):
        slot = {}
    slot[key] = [int(x), int(y)]
    data["window_pos"] = slot
    _write_config(data)


# LD preview settings — remembered between sessions.  off by default; value is
# the edge-boost strength, angle is the % radius from the center where the boost
# starts.  This is a viewing aid only and never touches the de-rotated stack.
_LD_DEFAULTS = {"on": False, "value": 1.0, "angle": 65}


def load_ld_settings() -> Dict:
    d = _read_config().get("ld_preview")
    if not isinstance(d, dict):
        return dict(_LD_DEFAULTS)
    return {
        "on":    bool(d.get("on", _LD_DEFAULTS["on"])),
        "value": float(d.get("value", _LD_DEFAULTS["value"])),
        "angle": float(d.get("angle", _LD_DEFAULTS["angle"])),
    }


def save_ld_settings(on: bool, value: float, angle: float) -> None:
    data = _read_config()
    data["ld_preview"] = {"on": bool(on), "value": float(value),
                          "angle": float(angle)}
    _write_config(data)


def desktop_rect(widget) -> Tuple[int, int, int, int]:
    """(x, y, w, h) spanning every monitor, not just the primary one.

    Tk's winfo_screenwidth/height only ever describe the primary display, so
    a window legitimately parked on a second monitor would look off-screen.
    """
    if os.name == "nt":
        try:
            import ctypes
            metric = ctypes.windll.user32.GetSystemMetrics
            # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CX.., SM_CY..
            w, h = metric(78), metric(79)
            if w > 0 and h > 0:
                return metric(76), metric(77), w, h
        except Exception:
            pass
    # X11 with Xinerama reports the full desktop here; elsewhere this is the
    # primary monitor, which is the best answer available.
    return (widget.winfo_vrootx(), widget.winfo_vrooty(),
            max(widget.winfo_vrootwidth(), widget.winfo_screenwidth()),
            max(widget.winfo_vrootheight(), widget.winfo_screenheight()))


def position_is_reachable(widget, x: int, y: int, w: int, h: int,
                          margin: int = 90) -> bool:
    """True if enough of the title bar would land on a monitor to grab it.

    Catches a position saved on a display that has since been unplugged,
    which would otherwise reopen the window somewhere invisible.
    """
    vx, vy, vw, vh = desktop_rect(widget)
    return (x + w > vx + margin and x < vx + vw - margin
            and y >= vy - 4 and y < vy + vh - 40)


# ── Filename → UTC ────────────────────────────────────────────────────────────

def parse_ut_from_filename(path: Path) -> datetime:
    """
    Extract UTC timestamp from filenames like:
      2026-03-31-0335_8-U-RGB-...
    Matches YYYY-MM-DD-HHMM (15 chars).
    """
    stem = path.stem
    fmt  = "%Y-%m-%d-%H%M"   # 15 chars
    n    = len(fmt.replace("%Y","0000").replace("%m","00").replace("%d","00")
               .replace("%H","00").replace("%M","00"))   # = 15
    for i in range(len(stem) - n + 1):
        candidate = stem[i:i+n]
        try:
            dt = datetime.strptime(candidate, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse UT from filename: {path.name}")


# ── datetime → JD ─────────────────────────────────────────────────────────────

def datetime_to_jd(dt: datetime) -> float:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (UTC).")
    dt_utc = dt.astimezone(timezone.utc)
    year, month, day = dt_utc.year, dt_utc.month, dt_utc.day
    hour   = dt_utc.hour
    minute = dt_utc.minute
    second = dt_utc.second + dt_utc.microsecond / 1e6
    if month <= 2:
        year -= 1; month += 12
    A = year // 100
    B = 2 - A + A // 4
    day_frac = (hour + (minute + second / 60.0) / 60.0) / 24.0
    return (int(365.25 * (year + 4716))
            + int(30.6001 * (month + 1))
            + day + day_frac + B - 1524.5)


# ── Image I/O ─────────────────────────────────────────────────────────────────

def load_image(path: Path) -> np.ndarray:
    """Load PNG/TIFF as float32 H×W or H×W×C, range 0–1."""
    img = Image.open(path)
    arr = np.array(img)
    if arr.dtype == np.uint8:
        return arr.astype(np.float32) / 255.0
    return arr.astype(np.float32) / 65535.0


def save_16bit_png(arr: np.ndarray, path: Path) -> None:
    arr = np.clip(arr, 0.0, 1.0)
    arr16 = (arr * 65535.0 + 0.5).astype(np.uint16)
    Image.fromarray(arr16).save(path)


def save_16bit_tiff(arr: np.ndarray, path: Path) -> None:
    try:
        import tifffile
        arr16 = np.clip(arr * 65535.0, 0, 65535).astype(np.uint16)
        tifffile.imwrite(str(path), arr16)
    except ImportError:
        save_16bit_png(arr, path.with_suffix(".png"))


# ── Horizons VECTORS REST client ──────────────────────────────────────────────

@dataclass
class HorizonsVectorRow:
    jd: float
    r_vec_au: np.ndarray   # planet→observer vector in ICRF, AU, shape (3,)


HORIZONS_FILE_URL = "https://ssd.jpl.nasa.gov/api/horizons_file.api"


def fetch_horizons_vectors(
    planet: str,
    times: List[datetime],
    site: Dict[str, float],
    log=None,
) -> List[HorizonsVectorRow]:
    """
    Query JPL Horizons using the File API (POST) for the planet→observer
    ICRF position vector at each timestamp.

    Uses the horizons_file.api endpoint which accepts a batch input file
    as a POST request — avoids all URL encoding ambiguities with the
    GET endpoint.

    Returns List[HorizonsVectorRow] in the same order as input times.
    """
    def _log(msg):
        if log: log(msg)

    code    = PLANET_CODE[planet.upper()]
    results = []

    for i, dt in enumerate(times):
        t0 = dt
        t1 = dt + timedelta(minutes=2)
        t0_str = t0.strftime("%Y-%b-%d %H:%M")
        t1_str = t1.strftime("%Y-%b-%d %H:%M")
        site_coord = f"{site['lon']:.6f},{site['lat']:.6f},{site['elev']:.1f}"

        # Build Horizons batch input file content.
        # Single quotes are required by Horizons for string values.
        # The !$$SOF marker is required.
        batch_input = (
            "!$$SOF\n"
            f"COMMAND='{code}'\n"
            f"EPHEM_TYPE='VECTORS'\n"
            f"CENTER='coord@399'\n"
            f"SITE_COORD='{site_coord}'\n"
            f"START_TIME='{t0_str}'\n"
            f"STOP_TIME='{t1_str}'\n"
            f"STEP_SIZE='1m'\n"
            f"VEC_TABLE='2'\n"
            f"OUT_UNITS='AU-D'\n"
            f"REF_FRAME='ICRF'\n"
            # REF_PLANE defaults to ECLIPTIC for VECTORS.  The IAU pole
            # constants below are equatorial, so without this the two frames
            # disagree by the 23.4° obliquity and every pole-derived quantity
            # (position angle, sub-Earth latitude) comes out wrong.
            f"REF_PLANE='FRAME'\n"
            f"REF_SYSTEM='J2000'\n"
            f"VEC_CORR='LT+S'\n"
            f"CSV_FORMAT='YES'\n"
            f"OBJ_DATA='NO'\n"
            f"MAKE_EPHEM='YES'\n"
        )

        _log(f"  Horizons query {i+1}/{len(times)}: {t0_str}")

        # POST multipart/form-data with format=text and input=<batch file>
        # Manually construct the multipart body
        boundary = "----HorizonsKepler1234"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="format"\r\n\r\n'
            f"text\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="input"; filename="horizons.txt"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
            f"{batch_input}\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        try:
            req = urllib.request.Request(
                HORIZONS_FILE_URL,
                data=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "User-Agent": "Kepler-v1.3.5/pvol-derotate",
                },
                method="POST")
            # Use certifi SSL context if available (macOS python.org builds)
            try:
                import ssl as _ssl, certifi as _certifi
                _ctx = _ssl.create_default_context(cafile=_certifi.where())
            except ImportError:
                import ssl as _ssl, os as _os
                _cafile = _os.environ.get("SSL_CERT_FILE")
                _ctx = _ssl.create_default_context(cafile=_cafile) if _cafile else None
            with urllib.request.urlopen(req, timeout=30,
                                        **({"context": _ctx} if _ctx else {})) as resp:
                result_text = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            try:
                body_err = e.read().decode("utf-8")
            except Exception:
                body_err = str(e)
            raise RuntimeError(
                f"Horizons File API HTTP {e.code} for {dt}.\n"
                f"Response: {body_err[:400]}")
        except Exception as e:
            raise RuntimeError(f"Horizons request failed for {dt}: {e}")

        if "error" in result_text.lower() and "$$SOE" not in result_text:
            raise RuntimeError(
                f"Horizons returned error for {dt}:\n{result_text[:400]}")

        row = _parse_horizons_vectors(result_text, dt)
        results.append(row)
        _log(f"    JD={row.jd:.5f}  R=({row.r_vec_au[0]:.4f}, "
             f"{row.r_vec_au[1]:.4f}, {row.r_vec_au[2]:.4f}) AU")

    return results


def _parse_horizons_vectors(text: str, dt: datetime) -> HorizonsVectorRow:
    """
    Parse the $$SOE...$$EOE block from a Horizons VECTORS CSV response.
    Returns HorizonsVectorRow with the first data line.

    Horizons reports the target relative to the center, i.e. observer→planet.
    Everything downstream wants planet→observer, so the vector is negated here.
    Verified against Horizons' own sub-observer quantities: with REF_PLANE=FRAME
    and this negation, the derived pole angle and sub-Earth latitude match to
    better than 0.01°.
    """
    lines = text.split("\n")

    in_data = False
    for line in lines:
        if "$$SOE" in line:
            in_data = True
            continue
        if "$$EOE" in line:
            break
        if not in_data:
            continue

        line = line.strip()
        if not line:
            continue

        # CSV format: JD, CalendarDate, X, Y, Z, VX, VY, VZ, ...
        # or: JD, CalendarDate, X, Y, Z (VEC_TABLE=2 gives fewer columns)
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            jd = float(parts[0])
            x  = float(parts[2])
            y  = float(parts[3])
            z  = float(parts[4])
            return HorizonsVectorRow(jd=jd, r_vec_au=-np.array([x, y, z]))
        except (ValueError, IndexError):
            continue

    # Fallback: try non-CSV format (space-separated after $$SOE)
    in_data = False
    for line in lines:
        if "$$SOE" in line:
            in_data = True
            continue
        if "$$EOE" in line:
            break
        if not in_data:
            continue
        # Non-CSV: "JD_TDB = XXXXXXX.X" then X/Y/Z on subsequent lines
        m = re.match(r"\s*(\d+\.\d+)\s*=\s*A\.D\.", line)
        if m:
            jd_str = m.group(1)
            # Find X Y Z line
            continue
        # Look for X = ... Y = ... Z = ...
        mx = re.search(r"X\s*=\s*([-+Ee0-9.]+)", line)
        my = re.search(r"Y\s*=\s*([-+Ee0-9.]+)", line)
        mz = re.search(r"Z\s*=\s*([-+Ee0-9.]+)", line)
        if mx and my and mz:
            jd = datetime_to_jd(dt)  # approximate fallback
            x  = float(mx.group(1))
            y  = float(my.group(1))
            z  = float(mz.group(1))
            return HorizonsVectorRow(jd=jd, r_vec_au=-np.array([x, y, z]))

    raise RuntimeError(
        f"Could not parse Horizons VECTORS response for {dt}.\n"
        f"Response excerpt: {text[:500]}")


# ── IAU-2018 constants ────────────────────────────────────────────────────────
# Source: Archinal et al. 2018, "Report of the IAU Working Group on
#         Cartographic Coordinates and Rotational Elements: 2015"
#         Celestial Mechanics and Dynamical Astronomy 130:22
#         https://doi.org/10.1007/s10569-017-9805-5
#
# Jupiter CM II uses System II rotation rate (equatorial atmosphere):
#   W_II = 43.3 + 870.2700000 * d  (rate from Rogers 1995)
# Saturn CM III uses System III (radio rotation):
#   W_III = 38.90 + 810.7939024 * d
# Mars CM I uses IAU-2018 prime meridian directly.

IAU2018_CONSTANTS = {
    "JUPITER": {
        # Pole orientation (IAU 2015)
        "alpha0_deg":               268.056595,
        "alpha1_deg_per_century":    -0.006499,
        "delta0_deg":                64.495303,
        "delta1_deg_per_century":     0.002413,
        # System II prime meridian (equatorial atmosphere)
        #   W_II  =  43.3   + 870.2700000 * d     (d = days from J2000.0)
        # The rate here previously read 870.5360000, which is System III's
        # rate paired with System II's epoch constant — an internally
        # inconsistent system.  It barely affected de-rotation (only ΔCM is
        # used, and the two rates differ by 0.0055° over half an hour) but the
        # reported CM drifted ~150° from true System II.
        # System III, for reference, is  W_III = 284.95 + 870.5360000 * d;
        # substituting those two values here reproduces Horizons' ObsSub-LON
        # to 0.7°, which is how the CM formula was validated.
        "W0_deg":    43.3,
        "Wdot_deg_per_day": 870.2700000,
    },
    "MARS": {
        # Pole orientation (IAU 2015)
        "alpha0_deg":               317.68143,
        "alpha1_deg_per_century":    -0.1061,
        "delta0_deg":                52.88650,
        "delta1_deg_per_century":    -0.0609,
        # Prime meridian (IAU 2015)
        "W0_deg":    176.630,
        "Wdot_deg_per_day": 350.89198226,
        # Flattening for planetographic latitude conversion
        "flattening": 0.00589,   # (Re - Rp) / Re  =  (3396.2 - 3376.2) / 3396.2
    },
    "SATURN": {
        # Pole orientation (IAU 2015)
        "alpha0_deg":                40.589,
        "alpha1_deg_per_century":    -0.036,
        "delta0_deg":                83.537,
        "delta1_deg_per_century":    -0.004,
        # System III prime meridian (radio emissions)
        # W_III = 38.90 + 810.7939024 * d
        "W0_deg":    38.90,
        "Wdot_deg_per_day": 810.7939024,
    },
}

JD_J2000 = 2451545.0


def compute_cm_iau2018(
    planet: str,
    jd: float,
    r_vec_au: np.ndarray,
) -> float:
    """
    Compute the sub-observer Central Meridian longitude using
    IAU-2018 pole orientation and prime meridian evolution.

    planet    : 'JUPITER', 'MARS', or 'SATURN'
    jd        : Julian Date (TT/TDB, consistent with Horizons)
    r_vec_au  : planet→observer vector in ICRF, AU, shape (3,)

    Returns CM in degrees [0, 360):
        JUPITER → CM II  (equatorial atmosphere, System II)
        MARS    → CM I   (planetographic)
        SATURN  → CM III (System III, radio rotation)
    """
    C = IAU2018_CONSTANTS[planet.upper()]

    # Step 1: centuries since J2000
    T = (jd - JD_J2000) / 36525.0
    d = jd - JD_J2000  # days since J2000

    # Step 2: pole RA/Dec at epoch
    alpha_deg = C["alpha0_deg"] + C["alpha1_deg_per_century"] * T
    delta_deg = C["delta0_deg"] + C["delta1_deg_per_century"] * T

    a = math.radians(alpha_deg)
    dd = math.radians(delta_deg)

    # Step 3: pole unit vector P in ICRF
    P = np.array([
        math.cos(dd) * math.cos(a),
        math.cos(dd) * math.sin(a),
        math.sin(dd),
    ])

    # Step 4: normalize planet→observer vector
    R = r_vec_au / np.linalg.norm(r_vec_au)

    # Step 5: sub-observer planetocentric latitude (unused in CM calc
    # but computed for completeness / Mars planetographic conversion)
    lat_c = math.asin(float(np.dot(R, P)))

    if planet.upper() == "MARS":
        f = C["flattening"]
        # planetographic latitude
        _lat_g = math.atan(math.tan(lat_c) / (1.0 - f) ** 2)
        # (not used in CM calculation, but would be used for latitude display)

    # Step 6: sub-observer longitude in the rotating body frame.
    #
    # The observer direction is resolved against the ICRF node of the body
    # equator.  Q0 = Z x P is the ascending node (the body-equator direction
    # from which W is measured); Q90 completes the right-handed pair.  The
    # angle from Q0 to the equatorial projection of R is the sub-observer
    # longitude measured eastward from the node.
    #
    # NOTE: the previous implementation used E = normalize(P x R) and then
    # atan2(dot(E, R), ...).  E is perpendicular to R by construction, so
    # dot(E, R) was identically zero and this term always collapsed to 0 —
    # CM degenerated into the prime-meridian angle W(t) alone, ignoring the
    # observer entirely.
    R_eq = R - float(np.dot(R, P)) * P          # R projected into body equator
    n_eq = np.linalg.norm(R_eq)
    if n_eq < 1e-10:
        # Observer exactly over the pole — longitude undefined
        return (C["W0_deg"] + C["Wdot_deg_per_day"] * d) % 360.0
    R_eq = R_eq / n_eq

    Q0 = np.cross(np.array([0.0, 0.0, 1.0]), P)  # ICRF node of the body equator
    n0 = np.linalg.norm(Q0)
    if n0 < 1e-10:
        Q0 = np.array([1.0, 0.0, 0.0])
    else:
        Q0 = Q0 / n0
    Q90 = np.cross(P, Q0)                        # 90° east of the node

    lon_deg = math.degrees(math.atan2(float(np.dot(R_eq, Q90)),
                                      float(np.dot(R_eq, Q0)))) % 360.0

    # Step 7: prime meridian angle W, evaluated at the RETARDED epoch.
    # We see the planet as it was one light-time ago, so W must be evaluated
    # then.  Without this the reported CM is out by the rotation accumulated
    # during light travel — 21° for Jupiter, 41° for Saturn.
    lt_days = float(np.linalg.norm(r_vec_au)) * LIGHT_DAY_PER_AU
    W_deg = (C["W0_deg"] + C["Wdot_deg_per_day"] * (d - lt_days)) % 360.0

    # Step 8: Central meridian.  IAU longitudes for these planets increase
    # westward, so the sub-observer longitude is subtracted from W.
    # Validated against Horizons quantity 14 (ObsSub-LON): agrees to <0.7°,
    # the residual being a small difference between Horizons' internal
    # rotation kernel and the published IAU-2018 constants.
    CM = (W_deg - lon_deg) % 360.0

    return CM


def compute_sub_earth_lat(
    planet: str,
    jd: float,
    r_vec_au: np.ndarray,
) -> float:
    """
    Planetocentric sub-Earth latitude D_E in degrees.

    This is the latitude of the point facing the observer — i.e. how far the
    planet's axis is tipped toward or away from us.  It is what makes the
    apparent disc an ellipse of a different flattening than the true one, and
    it sets the axis the planet appears to rotate about.

    Ranges: Jupiter ±3.3°, Saturn ±27°, Mars ±25°.

    Returns the PLANETOCENTRIC value, which is what the warp geometry needs.
    Horizons quantity 14 reports the planetographic value; the two differ by
    tan(phi_c) = (1-f)^2 tan(phi_g)  (3.15° vs 2.76° for Jupiter).
    """
    C = IAU2018_CONSTANTS[planet.upper()]
    T = (jd - JD_J2000) / 36525.0
    alpha = math.radians(C["alpha0_deg"] + C["alpha1_deg_per_century"] * T)
    delta = math.radians(C["delta0_deg"] + C["delta1_deg_per_century"] * T)
    pole = np.array([
        math.cos(delta) * math.cos(alpha),
        math.cos(delta) * math.sin(alpha),
        math.sin(delta),
    ])
    R = r_vec_au / np.linalg.norm(r_vec_au)
    return math.degrees(math.asin(float(np.clip(np.dot(pole, R), -1.0, 1.0))))


def compute_pole_position_angle(
    planet: str,
    jd: float,
    r_vec_au: np.ndarray,
) -> float:
    """
    Compute the Position Angle of the planet's north pole as seen by the
    observer, measured eastward from celestial north in the image plane.

    This is the angle by which the planet's rotation axis is tilted relative
    to the up direction of an equatorially-mounted telescope pointing at the
    planet.  It is zero when the north pole is directly above the disc center
    (celestial north = planet north), and increases counter-clockwise.

    planet    : 'JUPITER', 'MARS', or 'SATURN'
    jd        : Julian Date (TT/TDB)
    r_vec_au  : planet→observer ICRF vector, AU, shape (3,)

    Returns pole position angle in degrees, range (−180, +180].
    Positive = pole tilted east of celestial north (counter-clockwise in
    standard astronomical orientation, i.e. north-up east-left).
    """
    C = IAU2018_CONSTANTS[planet.upper()]
    T = (jd - JD_J2000) / 36525.0

    # Planet pole unit vector in ICRF
    alpha = math.radians(C["alpha0_deg"] + C["alpha1_deg_per_century"] * T)
    delta = math.radians(C["delta0_deg"] + C["delta1_deg_per_century"] * T)
    pole = np.array([
        math.cos(delta) * math.cos(alpha),
        math.cos(delta) * math.sin(alpha),
        math.sin(delta),
    ])

    # Observer direction (planet→observer, normalized)
    obs = r_vec_au / np.linalg.norm(r_vec_au)

    # Celestial north pole unit vector in ICRF
    cel_north = np.array([0.0, 0.0, 1.0])

    # Project both pole and celestial-north onto the image plane
    # (plane perpendicular to the observer direction)
    def proj(v, line_of_sight):
        """Project v onto the plane perpendicular to line_of_sight."""
        return v - float(np.dot(v, line_of_sight)) * line_of_sight

    pole_proj  = proj(pole,      obs)
    north_proj = proj(cel_north, obs)

    n1 = np.linalg.norm(pole_proj)
    n2 = np.linalg.norm(north_proj)
    if n1 < 1e-10 or n2 < 1e-10:
        return 0.0   # observer at pole, or pole on line of sight

    pole_proj  = pole_proj  / n1
    north_proj = north_proj / n2

    # Angle from celestial-north projection to pole projection,
    # measured with the sign convention: positive = eastward = CCW in
    # north-up east-left astronomical image orientation.
    cos_pa = float(np.dot(north_proj, pole_proj))
    cos_pa = max(-1.0, min(1.0, cos_pa))

    # Sign: use the component along obs × north_proj (i.e. eastward)
    east_proj = np.cross(obs, north_proj)
    sin_pa = float(np.dot(east_proj, pole_proj))

    pa_deg = math.degrees(math.atan2(sin_pa, cos_pa))
    return pa_deg



LIGHT_DAY_PER_AU = 499.004784 / 86400.0   # one-way light time per AU, in days

PLANET_OBLATENESS = {
    "JUPITER": 0.0649,
    "MARS":    0.0059,
    "SATURN":  0.0980,
}

# ── Wireframe measurement model ──────────────────────────────────────────────
#
# The user fits a wireframe to each frame; the fit yields everything the warp
# needs — center, scale and the true orientation of the pole on the sensor.
# Measuring per frame also absorbs drift between captures, which a single
# reference measurement cannot.

# Ring radii in equatorial radii of Saturn
SATURN_RINGS = [
    ("C inner", 1.239), ("B inner", 1.525),
    ("B outer", 1.950), ("A inner", 2.027), ("A outer", 2.269),
]


@dataclass
class DiscMeasurement:
    """A fitted wireframe for one frame."""
    cx: float          # disc center, pixels
    cy: float
    r_eq: float        # equatorial radius, pixels (isotropic plate scale)
    tilt_deg: float    # pole position angle IN THE IMAGE, clockwise from up
    # False when the disc is too round for the limb to fix the rotation
    # (Mars); the tilt then comes from the hint, not the image.
    tilt_determined: bool = True

    def as_tuple(self):
        return (self.cx, self.cy, self.r_eq, self.tilt_deg)


def apparent_polar_ratio(flattening: float, sub_earth_lat_deg: float) -> float:
    """b_app / a — the silhouette's flattening as seen at this sub-Earth latitude.

    At D_E = 0 this is just (1-f); as the pole tips toward us the disc looks
    rounder, reaching 1.0 if we could look straight down the axis.
    """
    D = math.radians(sub_earth_lat_deg)
    return math.sqrt(math.sin(D) ** 2 + (1.0 - flattening) ** 2 * math.cos(D) ** 2)


def _project_body(px, py, pz, m: DiscMeasurement, sub_earth_lat_deg: float):
    """Body-frame points (in units of r_eq) → image pixels.

    Same convention as warp_source_to_reference: z is the polar axis, the
    observer sits at planetocentric latitude D on the x meridian.
    """
    D = math.radians(sub_earth_lat_deg)
    sD, cD = math.sin(D), math.cos(D)
    t = math.radians(m.tilt_deg)
    ct, st = math.cos(t), math.sin(t)
    u = py
    v = -px * sD + pz * cD
    dx = ct * u - st * v
    dy = st * u + ct * v
    return m.cx + dx * m.r_eq, m.cy - dy * m.r_eq


def _visible(px, py, pz, flattening, sub_earth_lat_deg):
    """Outward normal facing the observer?"""
    D = math.radians(sub_earth_lat_deg)
    b = 1.0 - flattening
    return (px * math.cos(D) + (pz / (b * b)) * math.sin(D)) > 0


def wireframe_paths(m: DiscMeasurement, planet: str,
                    sub_earth_lat_deg: float,
                    n: int = 241) -> Dict[str, List[np.ndarray]]:
    """
    Build the overlay geometry for one frame, in image pixel coordinates.

    Returns a dict of named lists of (N,2) polylines:
        limb, equator, meridians, parallels, axis, rings
    Hidden portions are dropped, so the far half of the equator and the far
    ring arc do not draw over the globe.
    """
    planet = planet.upper()
    f = PLANET_OBLATENESS.get(planet, 0.0649)
    b = 1.0 - f
    out: Dict[str, List[np.ndarray]] = {
        "limb": [], "equator": [], "meridians": [], "parallels": [],
        "axis": [], "rings": [],
    }

    # Limb — the silhouette ellipse, drawn directly
    bapp = apparent_polar_ratio(f, sub_earth_lat_deg)
    th = np.linspace(0, 2 * np.pi, n)
    t = math.radians(m.tilt_deg)
    ct, st = math.cos(t), math.sin(t)
    u, v = np.cos(th), bapp * np.sin(th)
    dx, dy = ct * u - st * v, st * u + ct * v
    out["limb"].append(np.stack([m.cx + dx * m.r_eq, m.cy - dy * m.r_eq], -1))

    def add(key, px, py, pz):
        vis = _visible(px, py, pz, f, sub_earth_lat_deg)
        if not vis.any():
            return
        X, Y = _project_body(px, py, pz, m, sub_earth_lat_deg)
        # split into runs of visible points so hidden arcs leave a gap
        idx = np.nonzero(vis)[0]
        for grp in np.split(idx, np.nonzero(np.diff(idx) > 1)[0] + 1):
            if len(grp) > 1:
                out[key].append(np.stack([X[grp], Y[grp]], -1))

    lam = np.linspace(-np.pi, np.pi, n)
    add("equator", np.cos(lam), np.sin(lam), np.zeros_like(lam))

    for lat in (-60, -30, 30, 60):                      # parametric latitudes
        bl = math.radians(lat)
        add("parallels", math.cos(bl) * np.cos(lam),
                         math.cos(bl) * np.sin(lam),
                         np.full_like(lam, b * math.sin(bl)))

    beta = np.linspace(-np.pi / 2, np.pi / 2, n)
    for lon in (-90, -45, 0, 45, 90):                   # meridians from the CM
        ll = math.radians(lon)
        add("meridians", np.cos(beta) * math.cos(ll),
                         np.cos(beta) * math.sin(ll),
                         b * np.sin(beta))

    # Polar axis stub, drawn slightly beyond the poles so it is visible
    for s in (-1.0, 1.0):
        pz = np.array([s * b, s * b * 1.15])
        X, Y = _project_body(np.zeros(2), np.zeros(2), pz, m, sub_earth_lat_deg)
        out["axis"].append(np.stack([X, Y], -1))

    if planet == "SATURN":
        for _, R in SATURN_RINGS:
            px, py, pz = R * np.cos(lam), R * np.sin(lam), np.zeros_like(lam)
            X, Y = _project_body(px, py, pz, m, sub_earth_lat_deg)
            out["rings"].append(np.stack([X, Y], -1))    # rings drawn whole

    return out


def _limb_points(image: np.ndarray, thresh_frac: float = 0.22) -> np.ndarray:
    """Sub-pixel limb points: for each row and column, the 50% crossing of the
    bright region's edge.  Returns (N,2) array of (x, y)."""
    lum = image.mean(axis=2) if image.ndim == 3 else image
    lo = float(lum.max()) * thresh_frac
    pts = []

    def crossings(profile, fixed, axis):
        idx = np.nonzero(profile > lo)[0]
        if len(idx) < 3:
            return
        for edge, nb in ((idx[0], idx[0] - 1), (idx[-1], idx[-1] + 1)):
            if nb < 0 or nb >= len(profile):
                continue
            a_, b_ = profile[nb], profile[edge]
            if b_ == a_:
                continue
            frac = (lo - a_) / (b_ - a_)          # linear sub-pixel crossing
            pos = nb + frac * (edge - nb)
            pts.append((pos, fixed) if axis == 0 else (fixed, pos))

    for y in range(lum.shape[0]):
        crossings(lum[y, :], y, 0)
    for x in range(lum.shape[1]):
        crossings(lum[:, x], x, 1)
    return np.array(pts, dtype=np.float64) if pts else np.zeros((0, 2))


def tilt_is_determinable(r_eq: float, flattening: float,
                         sub_earth_lat_deg: float,
                         resid_px: float = 0.0) -> bool:
    """
    Can the limb actually pin down the pole's rotation angle?

    Only the silhouette's ellipticity carries that information: a circle looks
    the same at every rotation.  The signal is the difference between the two
    semi-axes, r_eq·(1−b/a); the noise is the limb-fit residual.

    Mars is the case that matters — f = 0.0059 gives b/a = 0.994, so a 64 px
    disc departs from a circle by 0.37 px, far below a typical ~0.6 px residual.
    Jupiter (0.065) and Saturn (0.098) are comfortably determinable.
    """
    signal = r_eq * (1.0 - apparent_polar_ratio(flattening, sub_earth_lat_deg))
    return signal > 2.0 * max(resid_px, 0.35)


def _seed_from_min_extent(image: np.ndarray, polar_ratio: float,
                          frac: float = 0.22) -> "DiscMeasurement":
    """
    Ring-immune starting estimate.

    A ring system lies in the equatorial plane, so it can only ever extend the
    bright region along the equator — never along the pole.  The direction in
    which the bright region is NARROWEST is therefore the polar axis, and that
    width is the globe's polar diameter, whatever the rings are doing.

    Gives r_eq to about 1% on Jupiter, Saturn (open or edge-on) and Mars.
    """
    lum = image.mean(axis=2) if image.ndim == 3 else image
    m = lum > float(lum.max()) * frac
    ys, xs = np.nonzero(m)
    if len(xs) < 20:
        H, W = lum.shape
        return DiscMeasurement(W / 2.0, H / 2.0, min(H, W) * 0.2, 0.0)
    cx, cy = float(xs.mean()), float(ys.mean())
    dx, dy = xs - cx, ys - cy
    best = None
    for deg in np.arange(0.0, 180.0, 0.5):
        t = math.radians(deg)
        proj = dx * math.cos(t) + dy * math.sin(t)
        ext = float(proj.max() - proj.min())
        if best is None or ext < best[0]:
            best = (ext, deg)
    ext, deg = best
    t = math.radians(deg)
    # the narrow axis is the pole; express it clockwise from image-up
    pa = (math.degrees(math.atan2(math.cos(t), -math.sin(t))) + 90) % 180 - 90
    return DiscMeasurement(cx=cx, cy=cy,
                           r_eq=(ext / 2.0) / max(polar_ratio, 1e-6),
                           tilt_deg=pa)


def autofit_disc(image: np.ndarray, planet: str, sub_earth_lat_deg: float,
                 init: Optional[DiscMeasurement] = None,
                 tilt_hint: Optional[float] = None) -> DiscMeasurement:
    """
    Snap the wireframe to the planet's limb (the F11 auto-fit).

    The silhouette's SHAPE is fixed by the ephemeris — only where it sits, how
    big it is and how it is rotated are unknown.  Fitting those four numbers
    instead of a free ellipse keeps the result stable on partial or noisy limbs,
    and on Saturn, where the rings would otherwise drag a free fit off the globe.

    A soft-L1 loss keeps ring pixels and stray moons from dominating.
    """
    from scipy.optimize import least_squares

    f = PLANET_OBLATENESS.get(planet.upper(), 0.0649)
    k = apparent_polar_ratio(f, sub_earth_lat_deg)
    pts = _limb_points(image)
    if len(pts) < 20:
        raise RuntimeError("Could not find enough limb points to fit.")

    if init is None:
        init = _seed_from_min_extent(image, k)

    # Reject limb points that cannot belong to the globe.  On Saturn the ring
    # ansae reach 2.3 equatorial radii and dominate the raw point set — with
    # the rings open they outnumber the globe's own limb, which is enough to
    # send an unseeded fit to infinity.
    def _trim(m, lo=0.75, hi=1.30):
        t = math.radians(m.tilt_deg)
        ct, st = math.cos(t), math.sin(t)
        X = pts[:, 0] - m.cx
        Y = m.cy - pts[:, 1]
        u =  ct * X + st * Y
        v = -st * X + ct * Y
        rad = np.hypot(u / max(m.r_eq, 1e-6), v / max(m.r_eq * k, 1e-6))
        return (rad > lo) & (rad < hi)

    keep = _trim(init)
    if keep.sum() >= 20:
        pts = pts[keep]

    def residual(p):
        cx, cy, r, tilt = p
        t = math.radians(tilt)
        ct, st = math.cos(t), math.sin(t)
        X = pts[:, 0] - cx
        Y = cy - pts[:, 1]
        # rotate into the pole-up frame, then the ellipse becomes axis-aligned
        u =  ct * X + st * Y
        v = -st * X + ct * Y
        return np.hypot(u / max(r, 1e-6), v / max(r * k, 1e-6)) - 1.0

    sol = least_squares(residual,
                        [init.cx, init.cy, init.r_eq, init.tilt_deg],
                        loss="soft_l1", f_scale=0.02, max_nfev=200)
    cx, cy, r, tilt = sol.x
    r = abs(float(r))
    # Robust scatter, not RMS: Saturn's ring tips survive into the limb point
    # set and would inflate an RMS residual enough to make a perfectly
    # determinable fit look degenerate.
    resid_px = float(np.median(np.abs(sol.fun))) * r

    # If the disc is too round for its ellipticity to fix the rotation, the
    # fitted tilt is noise.  Hold it at the supplied hint (normally the
    # ephemeris pole angle plus the camera rotation) and re-fit the rest, so
    # center and scale stay honest.
    if not tilt_is_determinable(r, f, sub_earth_lat_deg, resid_px):
        held = float(tilt_hint) if tilt_hint is not None else float(tilt)

        def residual_fixed(p):
            cx_, cy_, r_ = p
            t = math.radians(held)
            ct, st = math.cos(t), math.sin(t)
            X = pts[:, 0] - cx_
            Y = cy_ - pts[:, 1]
            u =  ct * X + st * Y
            v = -st * X + ct * Y
            return np.hypot(u / max(r_, 1e-6), v / max(r_ * k, 1e-6)) - 1.0

        sol2 = least_squares(residual_fixed, [cx, cy, r],
                             loss="soft_l1", f_scale=0.02, max_nfev=200)
        cx, cy, r = sol2.x
        m = DiscMeasurement(cx=float(cx), cy=float(cy), r_eq=float(abs(r)),
                            tilt_deg=held)
        m.tilt_determined = False
        return m

    m = DiscMeasurement(cx=float(cx), cy=float(cy), r_eq=r,
                        tilt_deg=float((tilt + 90) % 180 - 90))
    m.tilt_determined = True
    return m


# ── Disc geometry estimation ──────────────────────────────────────────────────

def estimate_disk_geometry(
    image: np.ndarray,
    planet: str = "JUPITER",
    sub_earth_lat_deg: float = 0.0,
    pole_tilt_deg: float = 0.0,
) -> Tuple[float, float, float, float]:
    """
    Estimate planet globe center (cx, cy), equatorial radius (r_eq) and
    polar radius (r_pol) from an image.

    Algorithm:
      - cy, r_pol: from the row brightness profile (works for all planets;
        ring brightness is spread horizontally so doesn't inflate row maxima)
      - cx: from the polar-cap columns only (top+bottom 20% of globe rows)
        to avoid ring contamination at the equatorial plane
      - r_eq: computed from r_pol and the planet's IAU oblateness, so rings
        never contaminate the equatorial radius estimate

    Returns (cx, cy, r_eq, flattening).
    """
    img = image.mean(axis=2) if image.ndim == 3 else image
    planet = planet.upper()
    oblateness = PLANET_OBLATENESS.get(planet, 0.0649)

    # Row profile → vertical globe extent
    row_max      = np.max(img, axis=1)
    thresh_globe = float(img.max()) * 0.20
    globe_rows   = np.where(row_max > thresh_globe)[0]

    if len(globe_rows) < 10:
        H, W = img.shape
        return W / 2.0, H / 2.0, min(H, W) * 0.17 / (1.0 - oblateness), oblateness

    v_half = (globe_rows.max() - globe_rows.min()) / 2.0   # measured half-height
    cy     = float(globe_rows.min() + globe_rows.max()) / 2.0

    # cx from polar-cap rows only (top+bottom 20% of globe rows)
    q           = max(1, len(globe_rows) // 5)
    polar_rows  = np.concatenate([globe_rows[:q], globe_rows[-q:]])
    polar_lum   = img[polar_rows, :]
    col_polar   = np.max(polar_lum, axis=0)
    thresh_cx   = float(col_polar.max()) * 0.20
    globe_cols  = np.where(col_polar > thresh_cx)[0]
    cx = float(globe_cols.mean()) if len(globe_cols) > 0 else img.shape[1] / 2.0

    # Invert the measured half-height to the equatorial radius.
    #
    # The image's vertical extent is NOT the polar radius unless the pole is
    # upright and the sub-Earth latitude is zero.  The projected silhouette is
    # an ellipse with semi-axis a across the pole and
    #     b_app = a·sqrt(sin²D + (1-f)²cos²D)
    # along it, rotated by the pole position angle; its half-height is
    #     sqrt(a² sin²PA + b_app² cos²PA).
    # Solving that for a removes both errors (Saturn at D=27°, PA=25° was
    # over-estimating r_eq by ~4%).
    D  = math.radians(sub_earth_lat_deg)
    PA = math.radians(pole_tilt_deg)
    k2 = math.sin(D) ** 2 + (1.0 - oblateness) ** 2 * math.cos(D) ** 2
    denom = math.sqrt(math.sin(PA) ** 2 + k2 * math.cos(PA) ** 2)
    r_eq  = v_half / max(denom, 1e-6)

    return cx, cy, r_eq, oblateness


# ── Single-pass direct disc warp (no intermediate map) ───────────────────────

# Steepness of the limb-darkening edge taper.  Exponent = (1 − LD)·_LD_SCALE, so
# LD 0.90 → μ^0.4 (gentle), 0.80 → μ^0.8, 0.65 → μ^1.4 (firm).  LD 1.0 → μ^0 = 1,
# i.e. no edge treatment — the default.  Lower LD fades the limb harder, which
# is what smooths over the bright-fringe / dark-halo de-rotation artifacts.
_LD_SCALE = 4.0


def _ld_taper(mu, ld, ld_angle):
    """Per-pixel limb-fade factor (≤1) from the emission-angle cosine μ.

    Fades toward the limb by μ**((1−LD)·k), but only past the ld_angle radius:
    the projected radius r = sqrt(1−μ²) runs 0 (center) → 1 (limb), and a ramp
    from ld_angle to the limb blends the disc from untouched (factor 1) to the
    full fade.  Inside ld_angle the factor is exactly 1.
    """
    mu = np.clip(mu, 0.0, 1.0)
    fade = mu ** ((1.0 - ld) * _LD_SCALE)
    r = np.sqrt(np.maximum(0.0, 1.0 - mu * mu))
    w = np.clip((r - ld_angle) / max(1.0 - ld_angle, 1e-6), 0.0, 1.0)
    return 1.0 - w * (1.0 - fade)


def emission_taper(cx, cy, r_eq, flattening, disc_tilt_deg,
                   sub_earth_lat_deg, out_h, out_w, ld, ld_angle=0.65):
    """Limb-darkening edge-taper map for a disc at this geometry.

    Returns the _ld_taper factor inside the disc and 1.0 elsewhere; LD ≥ 1.0
    gives an all-ones (no-op) map.  Used for the reference frame (which is not
    warped) and for the measurement-window preview.
    """
    if ld >= 1.0:
        return np.ones((out_h, out_w))
    y, x = np.indices((out_h, out_w), dtype=np.float64)
    tr = math.radians(disc_tilt_deg)
    ct, st = math.cos(tr), math.sin(tr)
    dx = x - cx
    dy = cy - y
    u =  ct * dx + st * dy
    v = -st * dx + ct * dy
    D = math.radians(sub_earth_lat_deg)
    sD, cD = math.sin(D), math.cos(D)
    a = float(r_eq)
    b = a * (1.0 - float(flattening))
    e = (a / b) ** 2
    A = cD * cD + e * sD * sD
    B = 2.0 * v * sD * cD * (1.0 - e)
    Cq = u * u + v * v * (sD * sD + e * cD * cD) - a * a
    disc = B * B - 4.0 * A * Cq
    on_disc = disc >= 0.0
    t = (-B - np.sqrt(np.maximum(disc, 0.0))) / (2.0 * A)
    px = -(v * sD + t * cD)
    py = u
    pz =  v * cD - t * sD
    inv_a2, inv_b2 = 1.0 / (a * a), 1.0 / (b * b)
    nrx, nry, nrz = px * inv_a2, py * inv_a2, pz * inv_b2
    mu = ((nrx * cD + nrz * sD)
          / np.sqrt(nrx * nrx + nry * nry + nrz * nrz + 1e-20))
    return np.where(on_disc, _ld_taper(mu, ld, ld_angle), 1.0)


def warp_source_to_reference(
    src_img: np.ndarray,
    cm_src: float,
    cm_ref: float,
    cx: float, cy: float,
    r_eq: float, flattening: float,
    out_h: int, out_w: int,
    disc_tilt_deg: float = 0.0,
    sub_earth_lat_deg: float = 0.0,
    ld: float = 1.0,
    ld_angle: float = 0.65,
) -> np.ndarray:
    """
    Warp src_img (captured at cm_src) into the reference frame (at cm_ref) in
    a SINGLE interpolation pass — no intermediate cylindrical map.

    ld / ld_angle apply the limb-darkening edge treatment: ld 1.0 leaves the
    limb sharp (default), lower ld fades it past the ld_angle radius to smooth
    over the bright-fringe / dark-halo artifacts that de-rotation can leave.

    The planet is treated as a true oblate ellipsoid viewed from an arbitrary
    sub-Earth latitude: each output pixel's line of sight is intersected with
    the ellipsoid, the resulting body-frame point is rotated about the polar
    axis by ΔCM, and projected back.  No small-angle or spherical
    approximation is involved.

    r_eq              : equatorial radius in pixels (isotropic plate scale —
                        the apparent polar radius follows from the geometry)
    flattening        : (r_eq − r_pol)/r_eq for the planet
    disc_tilt_deg     : tilt of the planet's north pole in the image, measured
                        clockwise from image-up.  0 = pole straight up.
    sub_earth_lat_deg : planetocentric sub-Earth latitude D_E.  Ignoring this
                        rotates frames about the image vertical instead of the
                        true polar axis — worth ~14 px on Saturn at D_E = 27°.
    """
    if src_img.ndim == 3:
        return np.stack([
            warp_source_to_reference(
                src_img[..., c], cm_src, cm_ref,
                cx, cy, r_eq, flattening, out_h, out_w,
                disc_tilt_deg, sub_earth_lat_deg, ld, ld_angle)
            for c in range(src_img.shape[2])], axis=-1)

    y, x = np.indices((out_h, out_w), dtype=np.float64)

    # ── Step 1: pixel → image-plane offsets, pole rotated upright ──────────
    # The plate scale is isotropic, so both axes use r_eq.  The apparent
    # flattening is produced by the ellipsoid geometry below rather than by
    # scaling the y axis, which is what lets sub-Earth latitude work.
    tilt_rad = math.radians(disc_tilt_deg)
    cos_t, sin_t = math.cos(tilt_rad), math.sin(tilt_rad)
    dx = x - cx
    dy = cy - y                                   # up positive
    u =  cos_t * dx + sin_t * dy                  # image right ("east")
    v = -sin_t * dx + cos_t * dy                  # toward the projected pole

    # ── Step 2: intersect the line of sight with the ellipsoid ────────────
    # Body frame: z = polar axis, observer at planetocentric latitude D on the
    # x meridian, so   R = (cosD,0,sinD)  N = (-sinD,0,cosD)  E = (0,1,0).
    # A pixel maps to  p = u·E + v·N − t·R,  and t solves
    #     (px² + py²)/a² + pz²/b² = 1.
    D = math.radians(sub_earth_lat_deg)
    sD, cD = math.sin(D), math.cos(D)
    a = float(r_eq)
    b = a * (1.0 - float(flattening))
    e = (a / b) ** 2

    A = cD * cD + e * sD * sD
    B = 2.0 * v * sD * cD * (1.0 - e)
    Cq = u * u + v * v * (sD * sD + e * cD * cD) - a * a
    disc = B * B - 4.0 * A * Cq
    on_disc = disc >= 0.0
    t = (-B - np.sqrt(np.maximum(disc, 0.0))) / (2.0 * A)   # near surface

    px = -(v * sD + t * cD)
    py = u
    pz =  v * cD - t * sD

    # ── Step 3: de-rotate — a plain rotation about the polar axis ─────────
    delta = math.radians(cm_src - cm_ref)
    cd, sd = math.cos(delta), math.sin(delta)
    qx = px * cd - py * sd
    qy = px * sd + py * cd
    qz = pz

    # ── Step 4: project back, and keep only points still facing us ────────
    u2 = qy
    v2 = -qx * sD + qz * cD
    # outward normal at q must point toward the observer
    facing = (qx / (a * a)) * cD + (qz / (b * b)) * sD > 0.0

    dx2 =  cos_t * u2 - sin_t * v2
    dy2 =  sin_t * u2 + cos_t * v2
    x_src = cx + dx2
    y_src = cy - dy2

    coords  = np.vstack([y_src.ravel(), x_src.ravel()])
    sampled = map_coordinates(src_img, coords, order=3, mode="nearest")
    out = sampled.reshape(out_h, out_w)

    # ── Limb-darkening edge treatment ─────────────────────────────────────
    # Fade the limb (past ld_angle) so stacking mismatches there don't beat
    # into bright fringes or dark halos.  ld 1.0 = no change.
    if ld < 1.0:
        inv_a2, inv_b2 = 1.0 / (a * a), 1.0 / (b * b)
        nrx, nry, nrz = px * inv_a2, py * inv_a2, pz * inv_b2
        mu = ((nrx * cD + nrz * sD)
              / np.sqrt(nrx * nrx + nry * nry + nrz * nrz + 1e-20))
        out = out * _ld_taper(mu, ld, ld_angle)

    # Mask: on the reference disc, still visible after rotation, and inset by
    # ~1 px so cubic overshoot at the limb cannot leak in.
    a_in = a - 1.0
    disc_in = (B * B) - 4.0 * A * (u * u + v * v * (sD * sD + e * cD * cD) - a_in * a_in)
    out[~(on_disc & facing & (disc_in >= 0.0))] = 0.0
    return out


def resolve_pole_direction(
    ref_img: np.ndarray, src_img: np.ndarray,
    cm_ref: float, cm_src: float,
    m_ref: "DiscMeasurement", m_src: "DiscMeasurement",
    flattening: float, sub_earth_lat_deg: float,
) -> bool:
    """
    Decide which end of the fitted axis is NORTH  (step 4: cardinal directions).

    A limb fit gives the pole's line but not its direction, and getting it
    backward reverses the de-rotation: features move the wrong way and the
    stack smears instead of sharpening.

    Resolve it by warping one frame onto the reference both ways and keeping
    whichever correlates better over the overlap.  Returns True if the axis
    should be flipped by 180°.
    """
    H, W = (ref_img.shape[0], ref_img.shape[1])
    ref_lum = ref_img.mean(-1) if ref_img.ndim == 3 else ref_img

    def score(flip: bool) -> float:
        w = warp_source_to_reference(
            src_img, cm_src=cm_src, cm_ref=cm_ref,
            cx=m_src.cx, cy=m_src.cy, r_eq=m_src.r_eq, flattening=flattening,
            out_h=H, out_w=W,
            disc_tilt_deg=m_src.tilt_deg + (180.0 if flip else 0.0),
            sub_earth_lat_deg=sub_earth_lat_deg)
        wl = w.mean(-1) if w.ndim == 3 else w
        m = (wl > 0.02) & (ref_lum > 0.02)
        if m.sum() < 200:
            return -1.0
        a = wl[m] - wl[m].mean()
        b = ref_lum[m] - ref_lum[m].mean()
        den = math.sqrt(float((a * a).sum()) * float((b * b).sum()))
        return float((a * b).sum()) / den if den > 0 else -1.0

    return score(True) > score(False)


# ── Core de-rotation engine ───────────────────────────────────────────────────

def run_derotation(
    input_files: List[Path],
    output_dir: Optional[Path],
    planet: str,
    site: Dict[str, float],
    log_callback=None,
    measurements: Optional[List["DiscMeasurement"]] = None,
    camera_rotation_deg: float = 0.0,
    ld_value: float = 1.0,
    ld_angle: float = 65.0,
) -> None:
    """
    Full de-rotation pipeline using single-pass direct warp.
    output_dir : if None, auto-creates <input_folder>/de-rotated/
    Images must be oriented with the planet's north pole at the top.
    Southern hemisphere observers should rotate images 180° first.
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)

    planet = planet.upper()
    if planet not in PLANET_CM_SYSTEM:
        raise RuntimeError(f"Unsupported planet: {planet}")
    if not input_files:
        raise RuntimeError("No input files provided.")

    log(f"Planet: {planet}  ({PLANET_CM_SYSTEM[planet]})")
    log(f"Frames: {len(input_files)}")
    log(f"Observer: lat={site['lat']:.5f}°  lon={site['lon']:.5f}°  "
        f"elev={site['elev']:.0f} m")
    if output_dir is None:
        output_dir = input_files[0].parent / "de-rotated"
    output_dir.mkdir(parents=True, exist_ok=True)
    log(f"Output → {output_dir}")
    log("─" * 60)
    times = [parse_ut_from_filename(f) for f in input_files]

    try:
        from ephem_cache import get_vector
        use_cache = True
    except ImportError:
        use_cache = False

    vec_rows = []
    any_live = False
    for i, (f, dt) in enumerate(zip(input_files, times)):
        row = None
        if use_cache:
            cached = get_vector(planet, dt, site)
            if cached:
                row = HorizonsVectorRow(
                    jd=cached["jd"],
                    r_vec_au=np.array([cached["x"], cached["y"], cached["z"]]))

        if row is None:
            # Live query fallback
            if not any_live:
                log("  Some timestamps not in cache — querying JPL Horizons…")
            any_live = True
            live = fetch_horizons_vectors(planet, [dt], site, log=None)
            row  = live[0]
            # Save to cache for next time
            if use_cache:
                try:
                    from ephem_cache import _cache_key, _load_day_cache, _save_day_cache
                    date  = dt.replace(hour=0, minute=0, second=0,
                                       microsecond=0, tzinfo=timezone.utc)
                    cache = _load_day_cache(planet, date)
                    cache[_cache_key(dt)] = {
                        "jd": row.jd,
                        "x": float(row.r_vec_au[0]),
                        "y": float(row.r_vec_au[1]),
                        "z": float(row.r_vec_au[2])}
                    _save_day_cache(planet, date, cache)
                except Exception:
                    pass

        vec_rows.append(row)
        log(f"  [{i+1}/{len(times)}] JD={row.jd:.5f}  "
            f"R=({row.r_vec_au[0]:.4f}, {row.r_vec_au[1]:.4f}, {row.r_vec_au[2]:.4f}) AU"
            + (" [live]" if any_live else " [cache]"))

    if len(vec_rows) != len(times):
        raise RuntimeError("Vector lookup length mismatch.")

    idx_ref = len(input_files) // 2   # middle frame is the reference

    # Step 2: disc geometry — auto-estimated, north pole assumed at top
    ref_img  = load_image(input_files[idx_ref])
    ref_gray = ref_img.mean(axis=2) if ref_img.ndim == 3 else ref_img
    H, W     = ref_gray.shape

    # Step 3: CM per frame — pole position angle from ephemeris,
    # disc_tilt = PA only (no camera rotation term; north-up assumed)
    jds        = [datetime_to_jd(t) for t in times]
    cm_values  = []
    pole_angles = []
    sub_lats    = []
    for f, jd, row in zip(input_files, jds, vec_rows):
        cm = compute_cm_iau2018(planet, jd, row.r_vec_au)
        pa = compute_pole_position_angle(planet, jd, row.r_vec_au)
        de = compute_sub_earth_lat(planet, jd, row.r_vec_au)
        cm_values.append(cm)
        pole_angles.append(pa)
        sub_lats.append(de)
        log(f"  {f.name[:50]}  CM={cm:.2f}°  PA={pa:+.1f}°  D_E={de:+.2f}°")

    # Disc geometry.  Per-frame wireframe measurements are strongly preferred:
    # they carry the true pole orientation on the sensor (which the ephemeris
    # alone cannot know) and absorb drift between captures.  Falling back to a
    # single auto-estimate assumes the image is celestial-north-up.
    de_ref = sub_lats[idx_ref]
    pa_ref = pole_angles[idx_ref]
    flattening = PLANET_OBLATENESS.get(planet, 0.0649)

    if measurements is None:
        log("\nNo wireframe measurements supplied — falling back to an "
            "auto-estimate that assumes north is up.")
        try:
            fit = autofit_disc(ref_img, planet, de_ref)
            measurements = [DiscMeasurement(fit.cx, fit.cy, fit.r_eq, fit.tilt_deg)
                            for _ in input_files]
            log(f"  auto-fit: cx={fit.cx:.1f} cy={fit.cy:.1f} "
                f"r_eq={fit.r_eq:.2f}px tilt={fit.tilt_deg:+.2f}°")
        except Exception as exc:
            log(f"  auto-fit failed ({exc}); using threshold estimate")
            tilt0 = pa_ref + camera_rotation_deg
            cx, cy, r_eq, flattening = estimate_disk_geometry(
                ref_gray, planet=planet,
                sub_earth_lat_deg=de_ref, pole_tilt_deg=tilt0)
            measurements = [DiscMeasurement(cx, cy, r_eq, tilt0)
                            for _ in input_files]
            if camera_rotation_deg:
                log(f"  camera rotation {camera_rotation_deg:+.2f}° applied")
    else:
        log(f"\nUsing {len(measurements)} wireframe measurements.")

    # De-rotation LD edge treatment (an algorithmic parameter passed in from
    # the De-rotate tab — SEPARATE from the measurement-window visual aid).
    # ld_value < 1 fades the limb to smooth over de-rotation fringes/halos.
    ld_val    = float(ld_value)
    ld_angle_ = float(ld_angle) / 100.0            # given as 0–95, use 0–1
    if ld_val < 1.0:
        log(f"De-rotation LD: value={ld_val:.2f}  angle={ld_angle:.0f}")

    m_ref = measurements[idx_ref]
    log(f"Reference geometry: cx={m_ref.cx:.1f}  cy={m_ref.cy:.1f}  "
        f"r_eq={m_ref.r_eq:.2f} px  tilt={m_ref.tilt_deg:+.2f}°  "
        f"f={flattening:.4f}  (D_E={de_ref:+.2f}°, PA_eph={pa_ref:+.1f}°)")

    # Reference = middle frame
    cm_ref = cm_values[idx_ref]
    log(f"\nReference: {input_files[idx_ref].name}  CM_ref={cm_ref:.2f}°")
    log("─" * 60)

    # Steps 4–6: single-pass warp each source frame directly into reference
    # frame coordinates, then accumulate weighted mean.
    # The reference frame itself contributes with weight 1 (no warp needed).
    log("\nWarping frames to reference geometry (single-pass)…")

    # Crop covers all bright content in the reference frame (globe + rings)
    lum_ref = ref_gray
    bright  = lum_ref > (float(lum_ref.max()) * 0.04)
    ys_b, xs_b = np.where(bright)
    pad  = 12
    y0   = max(0, int(ys_b.min()) - pad)
    y1   = min(H, int(ys_b.max()) + pad + 1)
    x0   = max(0, int(xs_b.min()) - pad)
    x1   = min(W, int(xs_b.max()) + pad + 1)
    oh, ow = y1 - y0, x1 - x0

    # Quality-weighted accumulation.
    # Each frame is weighted by its disc sharpness (Laplacian variance),
    # measured on the original image before warping (warp distorts the metric).
    # This downweights soft frames from poor seeing automatically.

    def disc_sharpness(img):
        """Laplacian variance on the disc region — higher = sharper."""
        from scipy.ndimage import laplace as _lap
        lum = (img[...,0]*0.299 + img[...,1]*0.587 + img[...,2]*0.114
               if img.ndim == 3 else img)
        disc = lum > 0.04
        if not disc.any():
            return 1.0
        return float(_lap(lum)[disc].var())

    # Score all frames first so we can log relative quality
    scores = {}
    for f, img_pre in zip(input_files,
                          [load_image(ff) for ff in input_files]):
        scores[f] = disc_sharpness(img_pre)
    max_score = max(scores.values()) or 1.0
    log("\nFrame quality scores (Laplacian variance, higher = sharper):")
    for f in input_files:
        pct = scores[f] / max_score * 100
        log(f"  {f.name[:50]}  score={scores[f]:.6f}  ({pct:.0f}%)")

    # Reference frame weight.  The reference is not warped, but it still gets
    # the same limb fade or its sharp edge would ring against the tapered ones.
    ref_w    = scores[input_files[idx_ref]]
    ref_crop = ref_img[y0:y1, x0:x1].astype(np.float64)
    if ld_val < 1.0:
        rt = emission_taper(m_ref.cx, m_ref.cy, m_ref.r_eq, flattening,
                            m_ref.tilt_deg, de_ref, H, W,
                            ld_val, ld_angle_)[y0:y1, x0:x1]
        ref_crop = (ref_crop * rt[..., np.newaxis] if ref_crop.ndim == 3
                    else ref_crop * rt)
    acc = ref_crop * ref_w
    wt_map = np.full((oh, ow), ref_w, dtype=np.float64)

    for i_f, (f, cm, tilt, de) in enumerate(
            zip(input_files, cm_values, pole_angles, sub_lats)):
        if i_f == idx_ref:
            continue
        img = load_image(f)
        q   = scores[f]
        mi  = measurements[i_f]
        log(f"  {f.name[:50]}  ΔCM={cm - cm_ref:+.2f}°  weight={q/max_score:.2f}")
        warped = warp_source_to_reference(
            img, cm_src=cm, cm_ref=cm_ref,
            cx=mi.cx, cy=mi.cy, r_eq=mi.r_eq, flattening=flattening,
            out_h=H, out_w=W,
            disc_tilt_deg=mi.tilt_deg, sub_earth_lat_deg=de,
            ld=ld_val, ld_angle=ld_angle_)
        warped_crop = warped[y0:y1, x0:x1].astype(np.float64)
        lum_w = (warped_crop[...,0]*0.299 + warped_crop[...,1]*0.587
                 + warped_crop[...,2]*0.114) if warped_crop.ndim==3 else warped_crop
        has_data = (lum_w > 0.001).astype(np.float64)
        if warped_crop.ndim == 3:
            acc += warped_crop * (q * has_data)[..., np.newaxis]
        else:
            acc += warped_crop * q * has_data
        wt_map += q * has_data

    # Quality-weighted mean
    wt_safe = np.where(wt_map > 0, wt_map, 1.0)
    if acc.ndim == 3:
        final_disk = np.clip(acc / wt_safe[..., np.newaxis], 0.0, None).astype(np.float32)
    else:
        final_disk = np.clip(acc / wt_safe, 0.0, None).astype(np.float32)

    log(f"Stacked {len(input_files)} frames → {ow}×{oh} px")

    # Save outputs
    output_dir.mkdir(parents=True, exist_ok=True)
    ref_stem  = input_files[idx_ref].stem
    disk_path = output_dir / f"{ref_stem}_derotated.tif"

    save_16bit_tiff(final_disk, disk_path)

    log("─" * 60)
    log(f"✔  Disc:  {disk_path.name}")
    log("Done.")


# ── Image Measurement window ─────────────────────────────────────────────────

_NL = chr(10)
_ROUND_DISC_WARNING = "".join([
    _NL, _NL,
    "!! disc too round for the limb to", _NL,
    "   fix the rotation. tilt came from", _NL,
    "   the camera rotation, not the", _NL,
    "   image. Set it by hand, or copy", _NL,
    "   it from a Jupiter/Saturn fit.",
])

WIRE_COLORS = {
    "limb":      "#00ff50",
    "equator":   "#ffdc00",
    "meridians": "#00beff",
    "parallels": "#00beff",
    "axis":      "#ff3c3c",
    "rings":     "#ff8c00",
}


# Shown verbatim in the measurement window's side panel.  Keep it here rather
# than scraping the class docstring: the docstring's own source indentation
# leaks into the label, and Consolas only lines the columns up if nothing is
# re-wrapped, so every line has to stay comfortably inside the panel width.
_KEYS_HELP = """arrows      move outline
+  -        zoom image
PgUp/PgDn   resize outline
[  ]        rotate
N           flip north
F11         auto-fit frame
Shift+F11   auto-fit all
Enter       accept

hold Shift for fine steps"""

# Key under which the measurement window's last position is stored.
_MEASURE_WIN_KEY = "measure"

# Match the main Kepler app's monospace UI font (Consolas on Windows/Linux,
# Menlo on macOS) so the measurement window's buttons don't fall back to Tk's
# default proportional face, which looks foreign next to the rest of Kepler.
import sys as _sys
_UI_FAMILY = "Menlo" if _sys.platform == "darwin" else "Consolas"
_BTN_FONT  = (_UI_FAMILY, 11)

# Colors — mirror the main Kepler app (app.py) so the measurement dialog reads
# as part of the same program rather than a foreign dark-themed window.  The
# image canvas itself stays black (it shows the planet against sky).
_C_PANEL  = "#e2e8f0"   # window / side-panel background  (app BG_PANEL)
_C_CARD   = "#ffffff"   # entry / spinbox interior        (app BG_CARD)
_C_FG     = "#0a0e18"   # primary text                    (app FG_MID)
_C_FG_DIM = "#475569"   # secondary / hint text (readable slate on light)
_C_BTN    = "#b8c5d6"   # button face                     (app BTN_BG)
_C_BTN_AC = "#93a8c0"   # button hover / press            (app BTN_ACTIVE)
_C_TRG    = "#b0bec5"   # slider trough                   (app SLIDER_TRG)


class MeasurementWindow(tk.Toplevel):
    """
    Image Measurement.

    A wireframe built from the ephemeris for each frame's own timestamp is laid
    over the image; you snap it to the limb with F11 or nudge it by hand.  The
    fit supplies the disc center, scale and — the part that cannot be guessed —
    the true orientation of the pole on the camera sensor.

    Keyboard shortcuts are listed in _KEYS_HELP above.
    """

    def __init__(self, parent, files: List[Path], planet: str,
                 site: Dict[str, float], ephem: List[Dict], on_done=None,
                 camera_rotation_deg: float = 0.0):
        super().__init__(parent)
        self.title(f"Image Measurement — {planet.title()}")
        self.configure(bg=_C_PANEL)
        self.files, self.planet, self.ephem = files, planet.upper(), ephem
        self.on_done = on_done
        self.flattening = PLANET_OBLATENESS.get(self.planet, 0.0649)
        self.camera_rotation_deg = float(camera_rotation_deg)
        self.idx = len(files) // 2          # start on the reference frame
        self.measurements: List[Optional[DiscMeasurement]] = [None] * len(files)
        self._imgs: Dict[int, np.ndarray] = {}
        self._photo = None
        self.view_zoom = 1.0        # image magnification; 1.0 = fit to canvas
        self.show_outline = True    # wireframe drawn over the image?
        # LD preview settings are global (a viewing aid, not per-frame data)
        # and remembered between sessions.
        _ld = load_ld_settings()
        self._ld_on    = _ld["on"]
        self._ld_value = _ld["value"]
        self._ld_angle = _ld["angle"]
        self._ld_updating = True       # guard while building; released below

        wrap = tk.Frame(self, bg=_C_PANEL); wrap.pack(fill="both", expand=True)

        # Left column: the image on top, a limb-darkening bar underneath it.
        # The canvas keeps a black background — it shows the planet against sky.
        left = tk.Frame(wrap, bg=_C_PANEL); left.pack(side="left",
                                                      fill="both", expand=True)
        self.canvas = tk.Canvas(left, bg="black", highlightthickness=0,
                                width=760, height=620)
        self.canvas.pack(side="top", fill="both", expand=True)
        self._build_ld_bar(left)

        side = tk.Frame(wrap, bg=_C_PANEL, padx=12, pady=10)
        side.pack(side="right", fill="y")
        self.info = tk.Label(side, justify="left", anchor="nw", bg=_C_PANEL,
                             fg=_C_FG, font=(_UI_FAMILY, 9))
        self.info.pack(fill="x")
        tk.Label(side, text=_KEYS_HELP,
                 justify="left", anchor="nw", bg=_C_PANEL, fg=_C_FG_DIM,
                 font=(_UI_FAMILY, 8)).pack(fill="x", pady=(10, 0))

        btns = tk.Frame(side, bg=_C_PANEL); btns.pack(fill="x", pady=12)

        def _btn(txt, cmd):
            b = tk.Button(btns, text=txt, command=cmd, width=20, font=_BTN_FONT,
                          bg=_C_BTN, fg=_C_FG, activebackground=_C_BTN_AC,
                          activeforeground=_C_FG, relief="groove", bd=2,
                          cursor="hand2")
            b.pack(pady=2, fill="x")
            return b

        _btn("Auto-fit (F11)", self.autofit)
        _btn("Auto-fit all", self.autofit_all)
        _btn("Flip north (N)", self.flip_north)
        _btn("Center outline", self.center_outline)
        _btn("Center image + outline", self.center_all)
        self._outline_btn = _btn("Hide outline", self.toggle_outline)
        _btn("Prev frame", lambda: self.step(-1))
        _btn("Next frame", lambda: self.step(+1))
        _btn("Accept", self.accept)

        for seq, fn in (
            ("<Left>",  lambda e: self.nudge(-1, 0, e)),
            ("<Right>", lambda e: self.nudge(+1, 0, e)),
            ("<Up>",    lambda e: self.nudge(0, -1, e)),
            ("<Down>",  lambda e: self.nudge(0, +1, e)),
            ("<plus>",  lambda e: self.zoom_in(e)),
            ("<equal>", lambda e: self.zoom_in(e)),
            ("<KP_Add>", lambda e: self.zoom_in(e)),
            ("<minus>", lambda e: self.zoom_out(e)),
            ("<KP_Subtract>", lambda e: self.zoom_out(e)),
            ("<Prior>", lambda e: self.resize(+1, e)),   # PgUp — outline out
            ("<Next>",  lambda e: self.resize(-1, e)),   # PgDn — outline in
            ("<bracketleft>",  lambda e: self.rotate(-1, e)),
            ("<bracketright>", lambda e: self.rotate(+1, e)),
            ("<n>", lambda e: self.flip_north()),
            ("<N>", lambda e: self.flip_north()),
            ("<F11>", lambda e: self.autofit()),
            ("<Shift-F11>", lambda e: self.autofit_all()),
            ("<Return>", lambda e: self.accept()),
        ):
            self.bind(seq, fn)

        # Reopen where it was last closed.  Failing that, open over the main
        # window instead of wherever the window manager would default to
        # (top-left of the primary monitor).
        self.transient(parent)
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        pos = load_window_pos(_MEASURE_WIN_KEY)
        if pos is None or not position_is_reachable(self, pos[0], pos[1], w, h):
            # Centered on the parent, measured against the parent alone and
            # never against the screen, so a parent on a second monitor keeps
            # the dialog on that same monitor.  max(0, ...): when the dialog
            # is larger than the parent, sit on the parent's top-left instead
            # of centring to a negative offset, which would put the title bar
            # off the top of the screen out of reach.
            pos = (parent.winfo_rootx() + max(0, (parent.winfo_width() - w) // 2),
                   parent.winfo_rooty() + max(0, (parent.winfo_height() - h) // 2))
        self.geometry(f"+{pos[0]}+{pos[1]}")
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._ld_updating = False    # UI built — slider is live now
        self.focus_set()
        self.after(50, self._first_draw)

    # ── data ──────────────────────────────────────────────────────────────
    def img(self, i=None):
        i = self.idx if i is None else i
        if i not in self._imgs:
            self._imgs[i] = load_image(self.files[i])
        return self._imgs[i]

    def meas(self) -> DiscMeasurement:
        if self.measurements[self.idx] is None:
            self.measurements[self.idx] = self._fit(self.idx)
        return self.measurements[self.idx]

    def _fit(self, i):
        """Auto-fit frame i, hinting the rotation from the ephemeris pole angle
        plus the camera rotation — the only usable source when the disc is too
        round for its limb to fix the angle (Mars)."""
        m = autofit_disc(self.img(i), self.planet, self.ephem[i]["de"],
                         tilt_hint=self.ephem[i]["pa"] + self.camera_rotation_deg)
        return m

    def _first_draw(self):
        self.autofit_all()

    # ── edits ─────────────────────────────────────────────────────────────
    def _fine(self, e):
        return bool(getattr(e, "state", 0) & 0x0001)      # Shift held

    def nudge(self, dx, dy, e=None):
        s = 0.2 if (e is not None and self._fine(e)) else 1.0
        m = self.meas(); m.cx += dx * s; m.cy += dy * s; self.draw()

    def resize(self, d, e=None):
        s = 0.1 if (e is not None and self._fine(e)) else 0.5
        m = self.meas(); m.r_eq = max(4.0, m.r_eq + d * s); self.draw()

    def rotate(self, d, e=None):
        s = 0.1 if (e is not None and self._fine(e)) else 0.5
        m = self.meas(); m.tilt_deg += d * s; self.draw()

    def flip_north(self):
        m = self.meas(); m.tilt_deg += 180.0; self.draw()

    def autofit(self):
        try:
            self.measurements[self.idx] = self._fit(self.idx)
        except Exception as exc:
            messagebox.showwarning("Auto-fit", f"Could not fit this frame:\n{exc}")
        self.draw()

    def autofit_all(self):
        for i in range(len(self.files)):
            try:
                self.measurements[i] = self._fit(i)
            except Exception:
                self.measurements[i] = None
        self._resolve_north()
        self.draw()

    def _resolve_north(self):
        """Step 4 — settle which end of the axis is north, once, for the set."""
        r = self.idx
        other = next((i for i in range(len(self.files))
                      if i != r and self.measurements[i] is not None), None)
        if other is None or self.measurements[r] is None:
            return
        try:
            flip = resolve_pole_direction(
                self.img(r), self.img(other),
                self.ephem[r]["cm"], self.ephem[other]["cm"],
                self.measurements[r], self.measurements[other],
                self.flattening, self.ephem[r]["de"])
        except Exception:
            return
        if flip:
            for m in self.measurements:
                if m is not None:
                    m.tilt_deg += 180.0

    def step(self, d):
        self.idx = max(0, min(len(self.files) - 1, self.idx + d)); self.draw()

    # ── view (image zoom / centring / outline visibility) ──────────────────
    def _set_zoom(self, z):
        self.view_zoom = max(0.25, min(8.0, z))
        self.draw()

    def zoom_in(self, e=None):
        self._set_zoom(self.view_zoom * (1.1 if (e and self._fine(e)) else 1.25))

    def zoom_out(self, e=None):
        self._set_zoom(self.view_zoom / (1.1 if (e and self._fine(e)) else 1.25))

    def center_outline(self):
        """Move the wireframe to the center of the image (its position, not the
        image view).  A quick reset when the outline has been nudged off-disc."""
        H, W = self.img().shape[:2]
        m = self.meas(); m.cx, m.cy = W / 2.0, H / 2.0
        self.draw()

    def center_all(self):
        """Reset the image view to fit and recenter the outline in one action."""
        self.view_zoom = 1.0
        self.center_outline()

    def toggle_outline(self):
        self.show_outline = not self.show_outline
        self._outline_btn.config(
            text="Hide outline" if self.show_outline else "Show outline")
        self.draw()

    # ── LD compensation (measurement visual aid) ───────────────────────────
    def _build_ld_bar(self, parent):
        """LD compensation controls, sitting under the planet image.

        A VISUAL AID for the measurement stage only: it brightens the limb
        (limb-darkening compensation) so the disc edge is easy to see and place
        the wireframe against.  Higher value = more dramatic.  It does NOT touch
        the de-rotated stack — the De-rotate tab has a separate De-rotation LD
        for the edge-artifact cleanup.
        """
        # pady leaves a margin above (from the image) and below (from the
        # window edge) so the row is not crammed against the bottom.
        bar = tk.Frame(parent, bg=_C_PANEL)
        bar.pack(side="bottom", fill="x", pady=(6, 8))

        self.ld_on_var    = tk.BooleanVar(value=self._ld_on)
        self.ld_var       = tk.StringVar(value=f"{self._ld_value:.2f}")
        self.ld_angle_var = tk.StringVar(value=f"{self._ld_angle:.0f}")

        tk.Checkbutton(bar, text="LD compensation", variable=self.ld_on_var,
                       command=self._on_ld_toggle, bg=_C_PANEL, fg=_C_FG,
                       activebackground=_C_PANEL, activeforeground=_C_FG,
                       selectcolor=_C_CARD, font=(_UI_FAMILY, 10)
                       ).pack(side="left", padx=(12, 14), pady=8)

        tk.Label(bar, text="LD value", bg=_C_PANEL, fg=_C_FG,
                 font=(_UI_FAMILY, 10)).pack(side="left", padx=(0, 4), pady=8)
        self._ld_value_spin = tk.Spinbox(
            bar, from_=0.00, to=3.00, increment=0.05, width=5,
            textvariable=self.ld_var, format="%.2f", justify="right",
            command=self._on_ld_change, bg=_C_CARD, fg=_C_FG,
            buttonbackground=_C_BTN, relief="groove", bd=2,
            font=(_UI_FAMILY, 10))
        self._ld_value_spin.pack(side="left", padx=(0, 14), pady=8)

        tk.Label(bar, text="LD angle", bg=_C_PANEL, fg=_C_FG,
                 font=(_UI_FAMILY, 10)).pack(side="left", padx=(0, 4), pady=8)
        self._ld_angle_spin = tk.Spinbox(
            bar, from_=0, to=95, increment=5, width=5,
            textvariable=self.ld_angle_var, justify="right",
            command=self._on_ld_change, bg=_C_CARD, fg=_C_FG,
            buttonbackground=_C_BTN, relief="groove", bd=2,
            font=(_UI_FAMILY, 10))
        self._ld_angle_spin.pack(side="left", padx=(0, 14), pady=8)

        tk.Label(bar, text="viewing aid — brightens the limb so the edge is "
                           "easy to fit (higher = more); does not affect the stack",
                 bg=_C_PANEL, fg=_C_FG_DIM,
                 font=(_UI_FAMILY, 9)).pack(side="left", pady=8)

        # Typing in a spinbox updates its var directly (not via command); trace
        # it so hand-typed values register too.
        self.ld_var.trace_add("write", lambda *a: self._on_ld_change())
        self.ld_angle_var.trace_add("write", lambda *a: self._on_ld_change())

    def _on_ld_change(self):
        """A spinbox changed: update the live preview."""
        if self._ld_updating:
            return
        try:
            self._ld_value = float(self.ld_var.get())
        except Exception:
            pass
        try:
            self._ld_angle = float(self.ld_angle_var.get())
        except Exception:
            pass
        self.draw()

    def _on_ld_toggle(self):
        """Master enable checkbox toggled — refresh the preview."""
        if self._ld_updating:
            return
        self._ld_on = bool(self.ld_on_var.get())
        self.draw()

    def _ld_preview(self, disp, cxd, cyd, rd):
        """LD preview — serves both jobs of LD compensation:

        1. Refining the outline: the disc is brightened and filled to the fitted
           silhouette so the limb is obvious and easy to place the wireframe on.
        2. De-rotation cleanup: the same edge taper de-rotation will apply is
           layered on, so as you drop the LD value you see the limb soften — a
           preview of the blend that removes bright-fringe / dark-halo artifacts.

        Display only.
        """
        k = float(np.clip(self._ld_value, 0.0, 3.0))
        if rd <= 1e-6 or k <= 0.0:
            return disp
        m = self.meas()
        de = self.ephem[self.idx]["de"]

        # elliptical radius (0 center → 1 limb) and emission cosine μ
        pr = apparent_polar_ratio(self.flattening, de)
        tr = math.radians(m.tilt_deg)
        ct, st = math.cos(tr), math.sin(tr)
        yy, xx = np.indices(disp.shape[:2])
        A = (xx - cxd) / rd
        B = (yy - cyd) / rd
        er = np.sqrt((ct * A - st * B) ** 2
                     + ((-st * A - ct * B) / max(pr, 1e-6)) ** 2)
        mu = np.sqrt(np.clip(1.0 - er * er, 0.0, 1.0))
        mask = np.clip((1.0 - er) / 0.03, 0.0, 1.0)   # confine to the disc
        disc = mask > 0.5
        if not disc.any():
            return disp

        # Limb-darkening COMPENSATION: divide the disc by μ^k to lift the dark
        # limb, then normalize to the disc's brightest so the limb whites out
        # and the center darkens as k grows — the WinJUPOS behavior (LD 0.5
        # gentle → LD 1.3 bright rim, black center).
        lum = disp[..., :3].mean(-1) if disp.ndim == 3 else disp
        comp = lum / np.clip(mu, 0.12, 1.0) ** k
        scale = np.percentile(comp[disc], 99.0)
        newlum = np.clip(comp / max(scale, 1e-6), 0.0, 1.0)
        gain = 1.0 + mask * (newlum / np.maximum(lum, 1e-4) - 1.0)
        if disp.ndim == 3:
            gain = gain[..., np.newaxis]
        return np.clip(disp * gain, 0.0, 1.0)

    def _save_pos(self):
        """Remember where the window sits, so it reopens in the same place."""
        try:
            # "WxH+X+Y", but the offsets are signed — a window on a monitor
            # left of or above the primary one has negative coordinates.
            m = re.match(r"\d+x\d+([+-]\d+)([+-]\d+)$", self.geometry())
            if m:
                save_window_pos(_MEASURE_WIN_KEY, int(m.group(1)), int(m.group(2)))
        except Exception:
            pass          # a config that cannot be written must never block closing

    def _save_ld(self):
        """Persist the LD preview settings so they carry to the next session."""
        try:
            save_ld_settings(self._ld_on, self._ld_value, self._ld_angle)
        except Exception:
            pass

    def _on_close(self):
        self._save_pos()
        self._save_ld()
        self.destroy()

    def accept(self):
        if any(m is None for m in self.measurements):
            messagebox.showwarning("Measurement",
                                   "Some frames are not measured yet.")
            return
        if self.on_done:
            self.on_done(list(self.measurements))
        self._save_pos()
        self._save_ld()
        self.destroy()

    # ── drawing ───────────────────────────────────────────────────────────
    def draw(self):
        from PIL import ImageTk
        c = self.canvas; c.delete("all")
        arr = self.img()
        lum = arr.mean(-1) if arr.ndim == 3 else arr
        m = self.meas()               # disc geometry (also drives the LD preview)
        de = self.ephem[self.idx]["de"]
        cw = max(c.winfo_width(), 400); ch = max(c.winfo_height(), 400)
        H, W = lum.shape
        sc = min(cw / W, ch / H) * self.view_zoom     # image px → canvas px
        # image is centered in the canvas; its top-left sits at (ox, oy)
        ox = (cw - W * sc) / 2.0
        oy = (ch - H * sc) / 2.0

        # Render only the part of the frame actually on-screen, so a deep zoom
        # never builds a PhotoImage larger than the canvas.
        ix0 = max(0, int(np.floor((0 - ox) / sc)))
        iy0 = max(0, int(np.floor((0 - oy) / sc)))
        ix1 = min(W, int(np.ceil((cw - ox) / sc)))
        iy1 = min(H, int(np.ceil((ch - oy) / sc)))
        self._photo = None
        if ix1 > ix0 and iy1 > iy0:
            # Display the frame in color when it is a color image (grayscale
            # otherwise).  The white point is set from luminance so the color
            # balance is preserved; headroom 1.7 keeps the globe — ~90 % of max
            # — a natural mid tone instead of blown to white.
            crop = arr[iy0:iy1, ix0:ix1]
            if crop.ndim == 3:
                crop = crop[..., :3]
            white = max(float(lum.max()) * 1.7, 1e-6)
            disp = np.clip(crop / white, 0, 1) ** 0.8
            if self._ld_on:           # exaggerate the limb (viewing aid only)
                disp = self._ld_preview(disp, m.cx - ix0, m.cy - iy0, m.r_eq)
            dw = max(1, int(round((ix1 - ix0) * sc)))
            dh = max(1, int(round((iy1 - iy0) * sc)))
            arr8 = (np.clip(disp, 0, 1) * 255).astype(np.uint8)
            mode = "RGB" if arr8.ndim == 3 else "L"
            pil = Image.fromarray(arr8, mode).resize((dw, dh), Image.BILINEAR)
            self._photo = ImageTk.PhotoImage(pil)
            c.create_image(ox + ix0 * sc, oy + iy0 * sc, anchor="nw",
                           image=self._photo)

        if self.show_outline:
            for key, polys in wireframe_paths(m, self.planet, de).items():
                for poly in polys:
                    pts = []
                    for x, y in poly:
                        pts.extend([ox + x * sc, oy + y * sc])
                    if len(pts) >= 4:
                        c.create_line(pts, fill=WIRE_COLORS[key],
                                      width=2 if key in ("limb", "axis") else 1)
            # mark north
            nx, ny = _project_body(np.array([0.0]), np.array([0.0]),
                                   np.array([(1 - self.flattening) * 1.25]), m, de)
            c.create_text(ox + nx[0] * sc, oy + ny[0] * sc, text="N",
                          fill="#ff3c3c", font=("Consolas", 11, "bold"))

        e = self.ephem[self.idx]
        self.info.configure(text=(
            f"Frame {self.idx+1} / {len(self.files)}\n"
            f"{self.files[self.idx].name[:30]}\n\n"
            f"UT     {e['ut']:%Y-%m-%d %H:%M}\n"
            f"CM     {e['cm']:8.2f}°\n"
            f"D_E    {e['de']:+8.2f}°\n"
            f"PA eph {e['pa']:+8.2f}°\n\n"
            f"center {m.cx:8.1f}, {m.cy:.1f}\n"
            f"r_eq   {m.r_eq:8.2f} px\n"
            f"tilt   {m.tilt_deg:+8.2f}°\n"
            f"camera {(m.tilt_deg - e['pa'] + 180) % 360 - 180:+8.2f}°\n\n"
            f"measured {sum(x is not None for x in self.measurements)}"
            f"/{len(self.files)}"
            + ("" if getattr(m, "tilt_determined", True) else
               _ROUND_DISC_WARNING)))


# ── GUI ───────────────────────────────────────────────────────────────────────

class DerotationGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("pvol_derotate v1.3.5 — IAU-2018 Planetary De-Rotation")
        self.geometry("860x680")
        self.resizable(True, True)

        self.input_files: List[Path] = []
        self.measurements: Optional[List[DiscMeasurement]] = None
        self.output_dir  = tk.StringVar()
        self.planet      = tk.StringVar(value="JUPITER")

        site = load_config()
        self.site_lat  = tk.StringVar(value=f"{site['lat']:.8f}")
        self.site_lon  = tk.StringVar(value=f"{site['lon']:.8f}")
        self.site_elev = tk.StringVar(value=f"{site['elev']:.2f}")

        self._build()

    def _build(self):
        p = {"padx": 8, "pady": 3}
        frm = ttk.Frame(self); frm.pack(fill="both", expand=True, padx=10, pady=8)

        # ── Input files ───────────────────────────────────────────────────────
        row = 0
        ttk.Label(frm, text="Input images (PNG/TIFF):").grid(
            row=row, column=0, sticky="w", **p)
        ttk.Button(frm, text="Add…",            command=self._add   ).grid(row=row, column=1, sticky="w", **p)
        ttk.Button(frm, text="Remove selected", command=self._remove).grid(row=row, column=2, sticky="w", **p)
        ttk.Button(frm, text="Clear",           command=self._clear ).grid(row=row, column=3, sticky="w", **p)

        row += 1
        self._lb = tk.Listbox(frm, height=7, width=90, selectmode=tk.MULTIPLE)
        sb = ttk.Scrollbar(frm, orient="vertical", command=self._lb.yview)
        self._lb.configure(yscrollcommand=sb.set)
        self._lb.grid(row=row, column=0, columnspan=4, sticky="nsew", **p)
        sb.grid(row=row, column=4, sticky="ns")

        # ── Output folder ─────────────────────────────────────────────────────
        row += 1
        ttk.Label(frm, text="Output folder:").grid(row=row, column=0, sticky="w", **p)
        ttk.Entry(frm, textvariable=self.output_dir, width=44).grid(
            row=row, column=1, columnspan=2, sticky="we", **p)
        ttk.Button(frm, text="Browse…", command=self._browse_out).grid(row=row, column=3, **p)
        ttk.Label(frm, text="(leave blank to auto-create 'de-rotated' subfolder)",
                  foreground="gray").grid(row=row+1, column=1, columnspan=3, sticky="w",
                  padx=8, pady=0)
        row += 1

        # ── Planet ────────────────────────────────────────────────────────────
        row += 1
        ttk.Label(frm, text="Planet:").grid(row=row, column=0, sticky="w", **p)
        ttk.Combobox(frm, textvariable=self.planet,
                     values=["JUPITER", "MARS", "SATURN"],
                     state="readonly", width=12).grid(row=row, column=1, sticky="w", **p)

        # ── Camera rotation ───────────────────────────────────────────────────
        row += 1
        ttk.Label(frm, text="Camera rotation (°):").grid(row=row, column=0, sticky="w", **p)
        self.cam_rot = tk.StringVar(value="0.0")
        ttk.Entry(frm, textvariable=self.cam_rot, width=8).grid(
            row=row, column=1, sticky="w", **p)
        ttk.Label(frm, text="fallback only — superseded by Measure images…",
                  foreground="gray").grid(row=row, column=2, columnspan=2,
                  sticky="w", padx=4)

        # ── Image measurement (wireframe fit) ─────────────────────────────────
        row += 1
        ttk.Button(frm, text="Measure images…",
                   command=self._measure).grid(row=row, column=0, sticky="w", **p)
        self.meas_status = tk.StringVar(value="not measured — Run will auto-fit")
        ttk.Label(frm, textvariable=self.meas_status, foreground="gray").grid(
            row=row, column=1, columnspan=3, sticky="w", padx=4)

        # ── Observer location ─────────────────────────────────────────────────
        row += 1
        ttk.Separator(frm, orient="horizontal").grid(
            row=row, column=0, columnspan=5, sticky="ew", pady=6)
        row += 1
        ttk.Label(frm, text="Observer location  "
                  "(stored in ~/.kepler_ephem/config.json):").grid(
            row=row, column=0, columnspan=4, sticky="w", **p)

        for label, var in [("Latitude (°):",  self.site_lat),
                            ("Longitude (°):", self.site_lon),
                            ("Elevation (m):", self.site_elev)]:
            row += 1
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", **p)
            ttk.Entry(frm, textvariable=var, width=18).grid(
                row=row, column=1, sticky="w", **p)

        row += 1
        ttk.Button(frm, text="Save location", command=self._save_loc).grid(
            row=row, column=1, sticky="w", **p)

        # ── Run ───────────────────────────────────────────────────────────────
        row += 1
        ttk.Separator(frm, orient="horizontal").grid(
            row=row, column=0, columnspan=5, sticky="ew", pady=6)
        row += 1
        ttk.Button(frm, text="▶  Run De-Rotation", command=self._run).grid(
            row=row, column=0, columnspan=2, pady=8)

        # ── Log ───────────────────────────────────────────────────────────────
        row += 1
        ttk.Label(frm, text="Log:").grid(row=row, column=0, sticky="nw", **p)
        self._log_txt = tk.Text(frm, height=12, width=90, font=("Consolas", 9))
        log_sb = ttk.Scrollbar(frm, orient="vertical", command=self._log_txt.yview)
        self._log_txt.configure(yscrollcommand=log_sb.set)
        self._log_txt.grid(row=row, column=1, columnspan=3, sticky="nsew", **p)
        log_sb.grid(row=row, column=4, sticky="ns")

        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(row, weight=1)

    # ── GUI helpers ───────────────────────────────────────────────────────────

    def _add(self):
        paths = filedialog.askopenfilenames(
            title="Select image files",
            filetypes=[("Images", "*.png *.tif *.tiff"), ("All files", "*.*")])
        for p in sorted(paths):
            path = Path(p)
            if path not in self.input_files:
                self.input_files.append(path)
                self._lb.insert("end", path.name)
        self._invalidate_measurements()
        # Auto-set output dir hint
        if self.input_files and not self.output_dir.get():
            self.output_dir.set(
                str(self.input_files[0].parent / "de-rotated"))

    def _remove(self):
        for idx in reversed(self._lb.curselection()):
            self._lb.delete(idx)
            del self.input_files[idx]
        self._invalidate_measurements()

    def _clear(self):
        self._lb.delete(0, "end")
        self.input_files.clear()
        self._invalidate_measurements()
        self.output_dir.set("")

    def _browse_out(self):
        d = filedialog.askdirectory()
        if d:
            self.output_dir.set(d)

    def _save_loc(self):
        try:
            site = {"lat":  float(self.site_lat.get()),
                    "lon":  float(self.site_lon.get()),
                    "elev": float(self.site_elev.get())}
        except ValueError:
            messagebox.showerror("Error", "Lat/lon/elev must be numeric.")
            return
        save_config(site)
        self._log(f"Observer location saved: lat={site['lat']:.6f}  "
                  f"lon={site['lon']:.6f}  elev={site['elev']:.0f} m")

    def _log(self, msg: str):
        self._log_txt.insert("end", msg + "\n")
        self._log_txt.see("end")
        self.update_idletasks()

    def _invalidate_measurements(self):
        self.measurements = None
        if hasattr(self, "meas_status"):
            self.meas_status.set("not measured — Run will auto-fit")

    def _measure(self):
        """Step 1-4: fetch the ephemeris for each frame's own UT (parsed from
        the filename), then open the wireframe editor."""
        if not self.input_files:
            messagebox.showerror("Measure", "No input files selected.")
            return
        try:
            site = {"lat":  float(self.site_lat.get()),
                    "lon":  float(self.site_lon.get()),
                    "elev": float(self.site_elev.get())}
        except Exception as e:
            messagebox.showerror("Measure", f"Invalid observer location: {e}")
            return
        planet = self.planet.get().upper()
        files  = list(self.input_files)

        self.meas_status.set("fetching ephemeris…")
        self.update_idletasks()
        try:
            eph = []
            for f in files:
                dt  = parse_ut_from_filename(f)      # UT comes from the filename
                row = fetch_horizons_vectors(planet, [dt], site, log=None)[0]
                eph.append(dict(
                    ut=dt,
                    cm=compute_cm_iau2018(planet, row.jd, row.r_vec_au),
                    de=compute_sub_earth_lat(planet, row.jd, row.r_vec_au),
                    pa=compute_pole_position_angle(planet, row.jd, row.r_vec_au)))
        except Exception as e:
            self.meas_status.set("ephemeris lookup failed")
            messagebox.showerror("Measure", f"Ephemeris lookup failed:\n{e}")
            return

        def done(ms):
            self.measurements = ms
            self.meas_status.set(
                f"measured {len(ms)}/{len(files)} — tilt "
                f"{ms[len(ms)//2].tilt_deg:+.2f}° on the reference frame")

        self.meas_status.set("measuring…")
        try:
            cam = float(self.cam_rot.get())
        except Exception:
            cam = 0.0
        MeasurementWindow(self, files, planet, site, eph, on_done=done,
                          camera_rotation_deg=cam)

    def _run(self):
        if not self.input_files:
            messagebox.showerror("Error", "No input files selected.")
            return
        try:
            out_str = self.output_dir.get().strip()
            out_dir = Path(out_str) if out_str else None
            planet  = self.planet.get().upper()
            cam_rot = float(self.cam_rot.get())
            site    = {"lat":  float(self.site_lat.get()),
                       "lon":  float(self.site_lon.get()),
                       "elev": float(self.site_elev.get())}
        except Exception as e:
            messagebox.showerror("Error", f"Invalid parameter: {e}")
            return

        save_config(site)
        self._log_txt.delete("1.0", "end")
        self._log("Starting de-rotation…")

        def _worker():
            try:
                run_derotation(
                    input_files=list(self.input_files),
                    output_dir=out_dir,
                    planet=planet,
                    site=site,
                    camera_rotation_deg=cam_rot,
                    measurements=self.measurements,
                    log_callback=self._log)
            except Exception as e:
                import traceback
                self._log(f"\nError: {e}\n{traceback.format_exc()}")
                messagebox.showerror("Error", str(e))

        threading.Thread(target=_worker, daemon=True).start()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = DerotationGUI()
    app.mainloop()
