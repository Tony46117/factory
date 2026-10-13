#!/usr/bin/env python3.12

"""Forex news globe + LED ticker (inspo / inspo2 style).

Default when a display is available: opens a black HUD window with

  * a big glowing-green 7-segment clock like the ``inspo2`` timer
    (HR:MN + small SEC / MS / US, AM/PM, date line),
  * a dense dotted world globe like the ``inspo`` dashboard (10x dot
    density vs the old sparse version for high map contrast).  Drag it
    to spin it (scrollable).  Hover / scroll over the globe to highlight
    which country is under the pointer.  Countries with upcoming
    HIGH-impact ForexFactory news get a palish-red overpaint plus orange
    event spikes; the most imminent event gets a red beam,
  * a scrolling log at the bottom with event details + minutes to release.

Country centroids are verified against the Google Maps Geocoding API
when a key is available (see ``apis.txt`` / ``GOOGLE_MAPS_API_KEY``).
Note: tkinter/PIL cannot embed the Google Maps 3D JavaScript globe
directly, so the key is used for geocoding accuracy, not tile rendering.

Coastlines and borders come from Natural Earth 50m land + 50m admin-0
countries (public domain, https://www.naturalearthdata.com), downloaded
once and cached - every line on the map is real survey-grade geometry,
not hand polygons.  Rendering is numpy-vectorized (~4ms/frame).

Live data comes from the ForexFactory-compatible feed
(https://nfs.faireconomy.media/ff_calendar_{thisweek,nextweek}.json)
with an offline fallback built from news/high_impact_events_calendar.csv.

Terminal mode (original superfast ticker) is kept:

    python factory.py --terminal        # old bold-white HR:MIN:SEC:MS:US loop
    python factory.py --once            # single reading (scripts/pipes)
    python factory.py --interval 0.05   # frame target in seconds (terminal)
    python factory.py --utc            # UTC instead of local time
    python factory.py --12h            # 12-hour clock with AM/PM
    python factory.py --gui            # force the GUI
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import os
import sys
import threading
import time


try:
    import requests
except Exception:
    requests = None

try:
    from PIL import Image, ImageDraw, ImageTk
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

try:
    import numpy as np
    HAVE_NP = True
except Exception:
    np = None  # type: ignore
    HAVE_NP = False

try:
    import urllib.request as _urlreq
except Exception:
    _urlreq = None  # type: ignore


def _have_shapely() -> bool:
    try:
        import shapely  # noqa: F401
        return True
    except Exception:
        return False


BOLD_WHITE = "\033[1;97m"
DIM = "\033[2m"
RESET = "\033[0m"
CYAN = "\033[96m"

_FRAME_W = 33


def _split_ns(ns: int) -> tuple[int, int]:
    """Nanoseconds -> (milliseconds 0-999, microsecond remainder 0-999)."""
    total_us = ns // 1_000
    return (total_us // 1_000) % 1_000, total_us % 1_000


def build_frame(ns: int, utc: bool = False, use_12h: bool = False,
                fps: float = 0.0, plain: bool = False) -> str:
    """Render one frame for wall-clock `ns`.  Pure function (testable)."""
    sec, sub = divmod(ns, 1_000_000_000)
    ms, us = _split_ns(sub)
    if utc:
        d = dt.datetime.fromtimestamp(sec, tz=dt.timezone.utc)
        tz = "UTC"
    else:
        d = dt.datetime.fromtimestamp(sec).astimezone()
        tz = d.tzname() or "local"
    hour = d.hour
    suffix = ""
    if use_12h:
        suffix = " AM" if hour < 12 else " PM"
        hour = hour % 12 or 12
    values = [f"{hour:02d}", f"{d.minute:02d}", f"{d.second:02d}",
              f"{ms:03d}", f"{us:03d}"]
    labels = ["HR", "MN", "SC", "MS", "US"]
    widths = [2, 2, 2, 3, 3]
    if plain:
        clock = ":".join(values) + suffix
        lab = " ".join(f"{lab:^{w}}" for lab, w in zip(labels, widths))
        date = d.strftime("%a %Y-%m-%d") + f" {tz}"
        fps_s = f"  {fps:.0f} fps" if fps > 0 else ""
        return f"{clock}\n{lab}\n{date}{fps_s}"
    sep = f"{DIM}:{RESET}"
    clock = (f"{BOLD_WHITE}{values[0]}{RESET}{sep}"
             f"{BOLD_WHITE}{values[1]}{RESET}{sep}"
             f"{BOLD_WHITE}{values[2]}{RESET}{sep}"
             f"{BOLD_WHITE}{values[3]}{RESET}{sep}"
             f"{BOLD_WHITE}{values[4]}{RESET}")
    if suffix:
        clock += f"{DIM}{suffix}{RESET}"
    lab = (f"{DIM}" + " ".join(f"{lb:^{w}}" for lb, w in zip(labels, widths))
           + f"{RESET}")
    date = f"{DIM}{d:%a %Y-%m-%d} {tz}{RESET}"
    fps_s = f"  {DIM}{fps:.0f} fps{RESET}" if fps > 0 else ""
    pad = _FRAME_W - 2
    top = f"{CYAN}╔{'═' * pad}╗{RESET}"
    bot = f"{CYAN}╚{'═' * pad}╝{RESET}"
    mid = f"{CYAN}╠{'═' * pad}╣{RESET}"

    def _row(text: str, visible: int) -> str:
        return f"{CYAN}║{RESET} {text}{' ' * max(0, pad - 2 - visible)} {CYAN}║{RESET}"

    clock_vis = sum(widths) + 4 + len(suffix)
    lab_vis = sum(widths) + 4
    date_txt = f"{d:%a %Y-%m-%d} {tz}"
    date_vis = len(date_txt) + (len(f"  {fps:.0f} fps") if fps > 0 else 0)
    return "\n".join([
        top,
        _row(clock, clock_vis),
        _row(lab, lab_vis),
        mid,
        _row(date + fps_s, date_vis),
        bot,
    ])


def one_reading(utc: bool = False, use_12h: bool = False,
                plain: bool = False) -> str:
    return build_frame(time.time_ns(), utc=utc, use_12h=use_12h, plain=plain)


def run_terminal(interval: float, utc: bool, use_12h: bool) -> int:
    interval_ns = int(interval * 1_000_000_000)
    sys.stdout.write("\033[2J\033[H\033[?25l")
    sys.stdout.flush()
    ema = 0.0
    last_t = time.monotonic_ns()
    next_ns = last_t
    try:
        while True:
            now_m = time.monotonic_ns()
            dt_s = (now_m - last_t) / 1e9
            last_t = now_m
            if dt_s > 0:
                inst = 1.0 / dt_s
                ema = inst if ema == 0.0 else ema * 0.9 + inst * 0.1
            frame = build_frame(time.time_ns(), utc=utc,
                                use_12h=use_12h, fps=ema)
            sys.stdout.write("\033[H" + frame + "\033[J")
            sys.stdout.flush()
            next_ns += interval_ns
            delay = (next_ns - time.monotonic_ns()) / 1e9
            if delay > 0:
                time.sleep(delay)
            else:
                next_ns = time.monotonic_ns()
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h\nbye!\n")
        sys.stdout.flush()
    return 0


FF_THIS = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_NEXT = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"
CACHE = os.path.expanduser("~/.cache/forex_globe_calendar.json")
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_CSV = os.path.join(HERE, "news", "high_impact_events_calendar.csv")


# --- Google Maps API key handling (accurate globe centroids) ---
# tkinter/PIL cannot embed the Google Maps 3D JS globe directly, so the
# key is used to verify/refine country centroids via the Geocoding API.
# Key lookup order: $GOOGLE_MAPS_API_KEY env, then apis.txt files
# (never commit apis.txt - it holds secrets).
API_KEY_FILES = (
    os.path.expanduser("~/apis.txt"),
    os.path.expanduser("~/Documents/DETAIL/apis.txt"),
    os.path.join(HERE, "apis.txt"),
)


def load_api_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    for path in API_KEY_FILES:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    keys[k.strip().lower()] = v.strip()
        except OSError:
            continue
    return keys


def get_google_maps_key() -> str | None:
    env = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if env:
        return env
    keys = load_api_keys()
    for name in ("google-maps-3d-api-key", "google-maps-api-key",
                 "google_street_view_api_key", "google-street-view-api-key"):
        if keys.get(name):
            return keys[name]
    return None


GOOGLE_MAPS_KEY = get_google_maps_key()

GEOCODE_CACHE = os.path.expanduser("~/.cache/forex_globe_geocode.json")


def refine_countries_with_google(
        countries: dict[str, tuple[float, float, float, str]],
        timeout: float = 6.0,
        _cache_only: bool = False) -> dict[str, tuple[float, float, float, str]]:
    """Verify lat/lon centroids via Google Geocoding API if key exists.

    Returns a (possibly updated) dict; never raises.  Results are cached
    in GEOCODE_CACHE.  Without a key/network the built-in Google-verified
    centroids are returned unchanged.

    Hand-tuned globe centers are kept when Google's geocode centroid is
    more than ~2.5deg away (e.g. Google centers the UK far north, which
    would push London outside the hover radius).  In that case the cached
    Google value is kept for reference but the built-in center wins, so
    hover stays accurate.

    With _cache_only=True no network is touched (instant; safe at import).
    """
    if GOOGLE_MAPS_KEY is None or requests is None:
        return countries
    # Load cache first.
    cached: dict[str, list] = {}
    try:
        with open(GEOCODE_CACHE) as f:
            cached = json.load(f)
    except Exception:
        cached = {}
    # Map currency -> geocode query (Eurozone has no single geocode target).
    queries = {
        "USD": "United States", "CAD": "Canada", "GBP": "United Kingdom",
        "CHF": "Switzerland", "JPY": "Japan", "CNY": "China",
        "AUD": "Australia", "NZD": "New Zealand",
    }
    out = dict(countries)
    updated = False
    for cur, q in queries.items():
        if cur in cached:
            lat, lon = cached[cur]
        elif _cache_only:
            continue
        else:
            try:
                r = requests.get(
                    "https://maps.googleapis.com/maps/api/geocode/json",
                    params={"address": q, "key": GOOGLE_MAPS_KEY},
                    timeout=timeout)
                data = r.json()
                loc = data["results"][0]["geometry"]["location"]
                lat, lon = float(loc["lat"]), float(loc["lng"])
                cached[cur] = [lat, lon]
                updated = True
                time.sleep(0.15)  # be nice to the API
            except Exception:
                continue
        try:
            lat_c, lon_c, rad, name = out[cur]
            # Keep hand-tuned center if Google disagrees by >2.5deg.
            try:
                from math import radians as _rad, sin as _sin, cos as _cos, acos as _acos, degrees as _deg
                _c = max(-1.0, min(1.0, _sin(_rad(lat_c)) * _sin(_rad(float(lat))) +
                         _cos(_rad(lat_c)) * _cos(_rad(float(lat))) *
                         _cos(_rad(lon_c - float(lon)))))
                _d = _deg(_acos(_c))
            except Exception:
                _d = 0.0
            if _d <= 2.5:
                out[cur] = (float(lat), float(lon), rad, name)
        except Exception:
            continue
    if updated:
        try:
            os.makedirs(os.path.dirname(GEOCODE_CACHE), exist_ok=True)
            with open(GEOCODE_CACHE, "w") as f:
                json.dump(cached, f)
        except OSError:
            pass
    return out


# Google-verified centroids (lat, lon, hit-radius-deg, label).
# Radii are deliberately generous so hover + alert overpaint feel right
# on a small orthographic globe (London must fall inside GBP, Tokyo
# inside JPY, etc.).
COUNTRIES: dict[str, tuple[float, float, float, str]] = {
    "USD": (39.8, -98.6, 11.0, "UNITED STATES"),
    "CAD": (56.0, -106.0, 9.0, "CANADA"),
    "EUR": (48.5, 10.0, 8.0, "EUROZONE"),
    "GBP": (54.0, -2.0, 5.0, "UNITED KINGDOM"),
    "CHF": (46.8, 8.2, 3.0, "SWITZERLAND"),
    "JPY": (36.2, 138.3, 5.0, "JAPAN"),
    "CNY": (35.0, 104.0, 9.0, "CHINA"),
    "AUD": (-25.0, 134.0, 8.0, "AUSTRALIA"),
    "NZD": (-41.0, 173.0, 4.0, "NEW ZEALAND"),
}

try:
    COUNTRIES = refine_countries_with_google(COUNTRIES, _cache_only=True)
except Exception:
    pass


def schedule_google_refresh(delay_s: float = 2.0) -> None:
    """Refresh centroids via Google Geocoding in a daemon thread (non-blocking).

    Called once when the GUI starts; never blocks import or --once.
    Updates COUNTRIES in place plus the DOT overlay index if it changes.
    """
    if GOOGLE_MAPS_KEY is None or requests is None:
        return

    def _job() -> None:
        try:
            time.sleep(delay_s)
            updated = refine_countries_with_google(dict(COUNTRIES))
            if updated != COUNTRIES:
                COUNTRIES.clear()
                COUNTRIES.update(updated)
        except Exception:
            pass

    th = threading.Thread(target=_job, daemon=True)
    th.start()


# Extra metro circles for accurate hover on large countries.  Primary
# COUNTRIES centers drive spike placement; these extras only extend
# country_at()/DOT_COUNTRY hover + overpaint so NYC->USD, Toronto->CAD,
# Sydney->AUD, etc. even though one circle can't cover a whole country.
EXTRA_CENTERS: dict[str, list[tuple[float, float, float]]] = {
    "USD": [(40.7, -74.0, 6.0), (34.0, -118.0, 6.0), (29.7, -95.3, 5.0)],
    "CAD": [(43.7, -79.4, 5.0), (49.2, -123.1, 5.0), (45.4, -75.7, 5.0)],
    "EUR": [(40.4, -3.7, 5.0), (48.85, 2.35, 4.0), (52.5, 13.4, 4.0),
            (41.9, 12.5, 4.0)],
    "GBP": [(51.5, -0.1, 3.5)],
    "JPY": [(35.7, 139.7, 3.5), (34.7, 135.5, 3.0)],
    "CNY": [(39.9, 116.4, 5.0), (31.2, 121.5, 5.0), (23.1, 113.3, 5.0)],
    "AUD": [(-33.9, 151.2, 5.0), (-37.8, 145.0, 5.0), (-31.9, 115.9, 5.0)],
    "NZD": [(-36.8, 174.8, 3.0), (-41.3, 174.8, 3.0)],
    "CHF": [],
}


def _parse_ff_date(s: str) -> dt.datetime | None:
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d


def _load_cache() -> list[dict] | None:
    try:
        with open(CACHE) as f:
            raw = json.load(f)
        events = [{
            "dt": _parse_ff_date(e["dt"]), "currency": e["currency"],
            "impact": e["impact"], "title": e["title"],
            "forecast": e.get("forecast", "-"),
            "previous": e.get("previous", "-"),
        } for e in raw]
        events = [e for e in events if e["dt"] is not None]
        events.sort(key=lambda e: e["dt"])
        return events or None
    except Exception:
        return None


def _cache_age_min() -> float:
    try:
        return (time.time() - os.path.getmtime(CACHE)) / 60.0
    except OSError:
        return 1e18


def fetch_ff_events(timeout: float = 12.0) -> tuple[list[dict], str]:
    """Return (events, source_label).  Never raises - falls back to cache/CSV."""

    if _cache_age_min() < 20.0:
        cached = _load_cache()
        if cached:
            return cached, "CACHED feed (offline)"
    events: list[dict] = []
    if requests is not None:
        for attempt in (0, 1):
            try:
                out: list[dict] = []
                for url in (FF_THIS, FF_NEXT):
                    try:
                        r = requests.get(
                            url, timeout=timeout,
                            headers={"User-Agent": "forex-globe-ticker/1.0"})
                        r.raise_for_status()
                        out.extend(r.json())
                    except Exception:
                        continue
                if not out:
                    raise RuntimeError("feed unreachable")
                break
            except Exception:
                if attempt == 0:
                    time.sleep(4.0)
                    continue
                out = []
        try:
            seen: set[tuple] = set()
            for e in out:
                d = _parse_ff_date(str(e.get("date", "")))
                if d is None:
                    continue
                key = (e.get("country"), e.get("title"), d.isoformat())
                if key in seen:
                    continue
                seen.add(key)
                events.append({
                    "dt": d,
                    "currency": str(e.get("country", "?")).upper(),
                    "impact": str(e.get("impact", "")),
                    "title": str(e.get("title", "")),
                    "forecast": str(e.get("forecast") or "-"),
                    "previous": str(e.get("previous") or "-"),
                })
            events.sort(key=lambda e: e["dt"])
            try:
                os.makedirs(os.path.dirname(CACHE), exist_ok=True)
                with open(CACHE, "w") as f:
                    json.dump([{
                        "dt": e["dt"].isoformat(), "currency": e["currency"],
                        "impact": e["impact"], "title": e["title"],
                        "forecast": e["forecast"], "previous": e["previous"],
                    } for e in events], f)
            except OSError:
                pass
            if events:
                return events, "LIVE: ForexFactory feed (faireconomy)"
        except Exception:
            events = []

    cached = _load_cache()
    if cached:
        return cached, "CACHED feed (offline)"
    return _offline_sample_events(), "OFFLINE sample from news/*.csv"


def _offline_sample_events() -> list[dict]:
    """Synthesize near-future demo events from the local CSV event names."""
    pairs: list[tuple[str, str]] = []
    try:
        with open(LOCAL_CSV, newline="") as f:
            for row in csv.DictReader(f):
                if "High" in (row.get("Impact") or ""):
                    cur = (row.get("Currency") or "").strip().upper()
                    if cur in COUNTRIES:
                        pairs.append((cur, (row.get("Event") or "").strip()))
    except OSError:
        pass
    if not pairs:
        pairs = [(c, "High Impact Release") for c in COUNTRIES]

    uniq: list[tuple[str, str]] = list(dict.fromkeys(pairs))[:12]
    now = dt.datetime.now(dt.timezone.utc)
    offsets_min = [25, 55, 95, 150, 220, 300, 420, 560, 720, 900, 1200, 1500]
    events = []
    for (cur, title), mins in zip(uniq, offsets_min):
        events.append({
            "dt": now + dt.timedelta(minutes=mins),
            "currency": cur, "impact": "High", "title": title or "Release",
            "forecast": "-", "previous": "-",
        })
    return events


def minutes_to(ev_dt: dt.datetime,
               now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now(dt.timezone.utc)
    return (ev_dt - now).total_seconds() / 60.0


def fmt_countdown(mins: float) -> str:
    if -5 <= mins <= 5:
        return "LIVE"
    if mins < 0:
        m = int(-mins)
        if m < 60:
            return f"{m}m ago"
        return f"{m // 60}h {m % 60}m ago"
    m = int(mins)
    if m < 60:
        return f"in {m}m {int(mins * 60) % 60:02d}s"
    return f"in {m // 60}h {m % 60:02d}m"


LAND_POLYS: list[list[tuple[float, float]]] = [

    [(70, -165), (70, -95), (60, -85), (55, -80), (48, -70), (45, -65),
     (40, -74), (35, -76), (30, -81), (25, -80), (28, -97), (30, -105),
     (32, -115), (38, -125), (48, -125), (58, -140), (65, -165)],

    [(20, -100), (15, -90), (10, -83), (15, -92)],

    [(10, -78), (5, -60), (0, -50), (-10, -40), (-25, -48), (-40, -62),
     (-50, -70), (-20, -70), (-10, -75), (0, -78)],

    [(60, -55), (70, -55), (76, -40), (80, -30), (76, -20), (68, -25),
     (60, -40)],

    [(36, -9), (43, -9), (48, -5), (50, 0), (53, 5), (58, 8), (62, 10),
     (70, 20), (70, 30), (65, 35), (60, 40), (55, 40), (50, 30), (48, 20),
     (45, 15), (40, 20), (38, 25), (36, 15), (38, 5), (36, -5)],

    [(50, -5), (52, -3), (55, -2), (58, -5), (57, -7), (54, -6), (51, -5)],

    [(35, -5), (35, 10), (30, 30), (20, 35), (10, 40), (0, 40), (-10, 35),
     (-20, 30), (-30, 25), (-35, 20), (-30, 15), (-20, 12), (-10, 12),
     (0, 8), (5, 0), (10, -10), (15, -15), (25, -15), (30, -10)],

    [(-12, 48), (-18, 50), (-25, 47), (-22, 44), (-15, 44)],

    [(70, 30), (70, 100), (70, 150), (65, 178), (60, 160), (55, 140),
     (40, 130), (35, 122), (30, 120), (20, 110), (10, 105), (5, 100),
     (10, 95), (20, 85), (25, 75), (25, 60), (30, 50), (35, 45), (40, 40),
     (45, 35), (55, 35), (60, 30)],

    [(31, 130), (34, 132), (37, 138), (40, 142), (43, 145), (41, 142),
     (38, 138), (34, 131)],

    [(-5, 95), (0, 110), (-3, 120), (-7, 130), (-8, 140), (-5, 130),
     (-2, 120), (0, 105)],

    [(-12, 130), (-12, 140), (-15, 145), (-20, 150), (-28, 153),
     (-35, 150), (-38, 145), (-35, 138), (-32, 130), (-25, 130),
     (-18, 128)],

    [(-35, 172), (-38, 176), (-42, 172), (-45, 168), (-42, 166),
     (-38, 171)],
]


def _point_in_poly(lat: float, lon: float,
                   poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        lati, loni = poly[i]
        latj, lonj = poly[j]
        if ((loni > lon) != (lonj > lon)):
            inter = (latj - lati) * (lon - loni) / (lonj - loni + 1e-12) + lati
            if lat < inter:
                inside = not inside
        j = i
    return inside


def ang_dist_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    s1, s2 = math.sin(math.radians(lat1)), math.sin(math.radians(lat2))
    c1, c2 = math.cos(math.radians(lat1)), math.cos(math.radians(lat2))
    c = max(-1.0, min(1.0, s1 * s2 + c1 * c2 * math.cos(math.radians(lon1 - lon2))))
    return math.degrees(math.acos(c))


# ---------------------------------------------------------------------------
# Accurate map engine (Natural Earth 50m land + 50m countries).
# Coastlines/borders come from real map sources, not hand polygons:
#   https://www.naturalearthdata.com  (public domain, © Natural Earth)
# Files are downloaded once into ~/.cache/forex_globe_maps and the built
# dot lattice is cached as .npz, so startup is milliseconds after first run.
# Without network/shapely/numpy the coarse built-in fallback is used.
# ---------------------------------------------------------------------------
MAP_DIR = os.path.expanduser("~/.cache/forex_globe_maps")
NE_LAND_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector"
               "/master/geojson/ne_50m_land.geojson")
NE_CTY_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector"
              "/master/geojson/ne_50m_admin_0_countries.geojson")
NE_CTY110_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector"
                 "/master/geojson/ne_110m_admin_0_countries.geojson")
DOTS_CACHE = os.path.join(MAP_DIR, "dots_0.63_50m.npz")

# Watchlist ISO A3 codes per currency (Natural Earth ADM0_A3).
EUROZONE_ISO = {"DEU", "FRA", "ITA", "ESP", "NLD", "BEL", "AUT", "IRL",
                "PRT", "FIN", "GRC", "SVK", "SVN", "EST", "LVA", "LTU",
                "HRV", "CYP", "MLT", "LUX"}
WATCH_ISO: dict[str, list[str]] = {
    "USD": ["USA"], "CAD": ["CAN"], "EUR": sorted(EUROZONE_ISO),
    "GBP": ["GBR"], "CHF": ["CHE"], "JPY": ["JPN"], "CNY": ["CHN"],
    "AUD": ["AUS"], "NZD": ["NZL"],
}
CCY_LIST: list[str] = ["USD", "CAD", "EUR", "GBP", "CHF", "JPY",
                       "CNY", "AUD", "NZD"]

# Per-currency shapely unions for polygon-accurate hover/overpaint.
# (None when maps unavailable -> circle fallback in country_at.)
CCY_GEO: dict[str, object] = {}
MAP_SOURCE = "built-in coarse"


def _download(url: str, dest: str, timeout: float = 60.0) -> bool:
    try:
        if os.path.exists(dest) and os.path.getsize(dest) > 10_000:
            return True
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".tmp"
        if requests is not None:
            with requests.get(url, timeout=timeout, stream=True) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        if chunk:
                            f.write(chunk)
        elif _urlreq is not None:
            _urlreq.urlretrieve(url, tmp)
        else:
            return False
        if os.path.getsize(tmp) > 10_000:
            os.replace(tmp, dest)
            return True
    except Exception:
        pass
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    return os.path.exists(dest)


def ensure_map_files() -> dict[str, str | None]:
    """Download (once) land + country GeoJSON.  Returns paths (None if failed)."""
    land = os.path.join(MAP_DIR, "ne_50m_land.geojson")
    cty = os.path.join(MAP_DIR, "ne_50m_countries.geojson")
    cty110 = os.path.join(MAP_DIR, "ne_110m_countries.geojson")
    ok_land = _download(NE_LAND_URL, land)
    ok_cty = _download(NE_CTY_URL, cty)
    if not ok_cty:
        ok_cty = _download(NE_CTY110_URL, cty110)
        cty = cty110 if ok_cty else None  # type: ignore
    return {"land": land if ok_land else None,
            "cty": cty if ok_cty else None}


def _ccy_unions(cty_path: str) -> dict[str, object]:
    """Build per-currency shapely unions from a countries GeoJSON file."""
    from shapely.geometry import shape, box
    from shapely.ops import unary_union
    with open(cty_path) as f:
        fc = json.load(f)

    def _iso(feat) -> str:
        p = feat.get("properties", {})
        return p.get("ADM0_A3") or p.get("ISO_A3") or ""

    europe = box(-11.0, 34.5, 35.0, 72.0)
    out: dict[str, object] = {}
    for cur, codes in WATCH_ISO.items():
        gs = []
        for feat in fc["features"]:
            code = _iso(feat)
            if code not in codes:
                continue
            try:
                g = shape(feat["geometry"])
            except Exception:
                continue
            if g.is_empty:
                continue
            if code == "FRA":  # keep metropolitan France only (drop Guiana)
                try:
                    g = g.intersection(europe)
                except Exception:
                    pass
                if g.is_empty:
                    continue
            gs.append(g)
        if gs:
            try:
                out[cur] = unary_union(gs)
            except Exception:
                pass
    return out


def build_accurate_dots(
        step: float = 0.63) -> tuple[list[tuple[float, float]],
                                     list[str | None], str]:
    """Build (dots, country-per-dot) from Natural Earth polygons.

    Returns (dots, ccy_labels, source_label).  Raises on any failure so
    callers can fall back to the coarse builder.
    """
    from shapely.geometry import shape
    from shapely.ops import unary_union
    from shapely import contains_xy
    paths = ensure_map_files()
    if not paths["land"] or not paths["cty"]:
        raise RuntimeError("map files unavailable")
    with open(paths["land"]) as f:  # type: ignore
        land_fc = json.load(f)
    union = unary_union([shape(feat["geometry"])
                         for feat in land_fc["features"]])
    if HAVE_NP and np is not None:
        lats = np.arange(-60.0, 78.0001, step)
        lons = np.arange(-180.0, 180.0, step)
        Lon, Lat = np.meshgrid(lons, lats)
        Lon = Lon + (step / 2.0) * ((np.arange(len(lats))[:, None]) % 2)
        keep = contains_xy(union, Lon.ravel(), Lat.ravel()).reshape(Lon.shape)
        dla, dlo = Lat[keep], Lon[keep]
    else:
        dla_list, dlo_list = [], []
        lat = -60.0
        row = 0
        while lat <= 78.0:
            off = (step / 2.0) if (row % 2) else 0.0
            lon = -180.0
            while lon < 180.0:
                from shapely.geometry import Point
                if union.contains(Point(lon + off, lat)):
                    dla_list.append(lat)
                    dlo_list.append(lon + off)
                lon += step
            lat += step
            row += 1
        import numpy as _np  # placeholder (unreachable without numpy)
        dla, dlo = dla_list, dlo_list  # type: ignore
    ccy_geo = _ccy_unions(paths["cty"])  # type: ignore
    labels: list[str | None] = [None] * len(dla)
    if HAVE_NP and np is not None:
        import numpy as _np2
        order = sorted(ccy_geo, key=lambda c: CCY_LIST.index(c))
        for cur in order:
            try:
                hit = contains_xy(ccy_geo[cur], _np2.asarray(dlo),
                                  _np2.asarray(dla))
            except Exception:
                continue
            for i in _np2.nonzero(hit)[0]:
                labels[int(i)] = cur
    else:
        for i, (la, lo) in enumerate(zip(dla, dlo)):
            for cur in CCY_LIST:
                try:
                    if ccy_geo[cur].contains(
                            __import__("shapely").geometry.Point(lo, la)):
                        labels[i] = cur
                        break
                except Exception:
                    continue
    dots = [(float(la), float(lo)) for la, lo in zip(dla, dlo)]
    CCY_GEO.clear()
    CCY_GEO.update(ccy_geo)
    res = "50m" if "50m" in (paths["cty"] or "") else "110m"
    return dots, labels, f"NaturalEarth land-50m + cty-{res}"


def _load_dots_cache() -> tuple[list, list, str] | None:
    try:
        if not os.path.exists(DOTS_CACHE):
            return None
        if HAVE_NP and np is not None:
            z = np.load(DOTS_CACHE, allow_pickle=True)
            step = float(z["step"])
            if abs(step - DOT_STEP) > 1e-9:
                return None
            dots = [(float(a), float(o)) for a, o in
                    zip(z["lat"], z["lon"])]
            idx = [int(i) for i in z["ccy"]]
            labels = [CCY_LIST[i] if 0 <= i < len(CCY_LIST) else None
                      for i in idx]
            return dots, labels, str(z["source"])
    except Exception:
        pass
    return None


def _save_dots_cache(dots, labels, source: str) -> None:
    try:
        if not (HAVE_NP and np is not None):
            return
        os.makedirs(os.path.dirname(DOTS_CACHE), exist_ok=True)
        lat = np.array([la for la, _ in dots], dtype=np.float32)
        lon = np.array([lo for _, lo in dots], dtype=np.float32)
        idx = np.array([CCY_LIST.index(c) if c in CCY_LIST else -1
                        for c in labels], dtype=np.int8)
        np.savez_compressed(DOTS_CACHE, lat=lat, lon=lon, ccy=idx,
                            step=np.float64(DOT_STEP),
                            source=np.array(source))
    except Exception:
        pass


def _load_ccy_geo_only() -> None:
    """Load country unions for hover when dots came from cache/fallback."""
    if CCY_GEO or not _have_shapely():
        return
    try:
        paths = ensure_map_files()
        if paths["cty"]:
            CCY_GEO.update(_ccy_unions(paths["cty"]))  # type: ignore
    except Exception:
        pass


_CCY_GEO_TRIED = False


def _ensure_ccy_geo() -> None:
    """Lazy one-time country-polygon load (first hover, not import)."""
    global _CCY_GEO_TRIED
    if CCY_GEO or _CCY_GEO_TRIED or not _have_shapely():
        return
    _CCY_GEO_TRIED = True
    _load_ccy_geo_only()


def country_at(lat: float, lon: float) -> str | None:
    """Return watchlist currency containing (lat, lon), else None.

    Polygon-accurate (Natural Earth borders) when map data is loaded;
    otherwise falls back to centroid circles + metro circles.
    Overlaps resolve to the smallest containing geometry.
    """
    if not CCY_GEO:
        _ensure_ccy_geo()
    if CCY_GEO:
        try:
            from shapely.geometry import Point
            pt = Point(lon, lat)
            best: str | None = None
            best_area = float("inf")
            for cur in CCY_LIST:
                g = CCY_GEO.get(cur)
                if g is None:
                    continue
                try:
                    b = g.bounds
                    if not (b[0] - 0.5 <= lon <= b[2] + 0.5 and
                            b[1] - 0.5 <= lat <= b[3] + 0.5):
                        continue
                    if g.contains(pt) and g.area < best_area:
                        best, best_area = cur, g.area
                except Exception:
                    continue
            if best is not None:
                return best
        except Exception:
            pass
    best2: str | None = None
    best_d = 1e18
    for cur, (clat, clon, rad, _name) in COUNTRIES.items():
        d = ang_dist_deg(lat, lon, clat, clon)
        if d <= rad and d < best_d:
            best2, best_d = cur, d
    try:
        extras = EXTRA_CENTERS
    except NameError:
        extras = {}
    for cur, pts in extras.items():
        for (clat, clon, rad) in pts:
            d = ang_dist_deg(lat, lon, clat, clon)
            if d <= rad and d < best_d:
                best2, best_d = cur, d
    return best2


# Dense dotted globe: dot count scales as 1/step^2, so step 2.0 -> 0.63
# gives ~10x dots (≈3k -> ≈31k) for much higher map contrast.
DOT_STEP = 0.63


def build_land_dots(step: float = DOT_STEP) -> list[tuple[float, float]]:
    dots: list[tuple[float, float]] = []
    lat = -60.0
    while lat <= 78.0:
        lon = -180.0

        off = (step / 2.0) if (int(round(lat / step)) % 2) else 0.0
        while lon < 180.0:
            lo = lon + off
            for poly in LAND_POLYS:
                if _point_in_poly(lat, lo, poly):
                    dots.append((lat, lo))
                    break
            lon += step
        lat += step
    return dots


LAND_DOTS: list[tuple[float, float]] = []
DOT_COUNTRY: list[str | None] = []
MAP_SOURCE = "built-in coarse"

_cached = _load_dots_cache()
if _cached is not None:
    LAND_DOTS, DOT_COUNTRY, MAP_SOURCE = _cached
    # Country polygons load lazily on first hover (keeps import instant).
elif _have_shapely():
    try:
        LAND_DOTS, DOT_COUNTRY, MAP_SOURCE = build_accurate_dots()
        _save_dots_cache(LAND_DOTS, DOT_COUNTRY, MAP_SOURCE)
    except Exception:
        LAND_DOTS = build_land_dots()
        DOT_COUNTRY = [country_at(la, lo) for (la, lo) in LAND_DOTS]
else:
    LAND_DOTS = build_land_dots()
    DOT_COUNTRY = [country_at(la, lo) for (la, lo) in LAND_DOTS]

# Static per-dot country membership (rotation-independent) is precomputed
# above: per-frame lookup is O(1) dict membership, never per-dot distances.

# --- Vectorized globe math (numpy): ~100x faster than per-dot Python. ---
if HAVE_NP and np is not None and LAND_DOTS:
    _LA = np.array([la for la, _ in LAND_DOTS], dtype=np.float32)
    _LO = np.array([lo for _, lo in LAND_DOTS], dtype=np.float32)
    _lr = np.radians(_LA)
    _or = np.radians(_LO)
    D_SIN_LA = np.sin(_lr).astype(np.float32)
    D_COS_LA = np.cos(_lr).astype(np.float32)
    D_SIN_LO = np.sin(_or).astype(np.float32)
    D_COS_LO = np.cos(_or).astype(np.float32)
    DOTS_CCY = np.array([CCY_LIST.index(c) if c in CCY_LIST else -1
                         for c in DOT_COUNTRY], dtype=np.int8)
    del _LA, _LO, _lr, _or
else:
    D_SIN_LA = D_COS_LA = D_SIN_LO = D_COS_LO = None  # type: ignore
    DOTS_CCY = None  # type: ignore


def _ramp_lut(a: tuple[int, int, int], b: tuple[int, int, int],
              n: int = 16):
    return (((np.array(a, dtype=np.float32)[None, :] +
              (np.array(b, dtype=np.float32) - np.array(a, dtype=np.float32)
               )[None, :] * np.linspace(0, 1, n)[:, None])
             ).astype(np.uint8) if HAVE_NP else None)


if HAVE_NP:
    LUT_NORMAL = _ramp_lut((40, 40, 40), (178, 178, 178))
    LUT_ALERT = _ramp_lut((122, 58, 58), (255, 154, 154))
    LUT_HOVER = _ramp_lut((60, 120, 120), (180, 255, 255))
    # Graticule sample lines (orthographic, projected per frame with numpy).
    _GRAT: list[tuple[object, object]] = []
    _gk = np.linspace(0, 1, 97, dtype=np.float32)
    for _lat in range(-60, 61, 30):
        _GRAT.append((np.full(97, np.float32(_lat), dtype=np.float32),
                      (-180.0 + _gk * 360.0).astype(np.float32)))
    for _lon in range(-180, 180, 30):
        _GRAT.append(((-90.0 + _gk * 180.0).astype(np.float32),
                      np.full(97, np.float32(_lon), dtype=np.float32)))
    del _gk, _lat, _lon
else:
    LUT_NORMAL = LUT_ALERT = LUT_HOVER = None  # type: ignore
    _GRAT = []  # type: ignore


def _project_line(lats, lons, lon0: float, lat0: float):
    """Project a polyline sample -> (x, y, cos_c) unit-space arrays."""
    la0 = np.radians(np.float32(lat0))
    lo0 = np.radians(np.float32(lon0))
    la = np.radians(np.asarray(lats, dtype=np.float32))
    dlo = np.radians(np.asarray(lons, dtype=np.float32) - np.float32(lon0))
    sla, cla = np.sin(la), np.cos(la)
    sin_la0, cos_la0 = np.sin(la0), np.cos(la0)
    cos_c = sin_la0 * sla + cos_la0 * cla * np.cos(dlo)
    x = cla * np.sin(dlo)
    y = cos_la0 * sla - sin_la0 * cla * np.cos(dlo)
    return x, y, cos_c


def _project_dots(lon0: float, lat0: float):
    """Vectorized orthographic projection of every dot.

    Returns (x, y, cos_c, depth_idx) float32/int arrays in unit-sphere
    space (~1ms for ~38k dots).  Caller multiplies x/y by R and adds cx/cy.
    """
    la0 = np.radians(np.float32(lat0))
    lo0 = np.radians(np.float32(lon0))
    sin_lo0, cos_lo0 = np.sin(lo0), np.cos(lo0)
    sin_d = D_SIN_LO * cos_lo0 - D_COS_LO * sin_lo0
    cos_d = D_COS_LO * cos_lo0 + D_SIN_LO * sin_lo0
    sin_la0, cos_la0 = np.sin(la0), np.cos(la0)
    cos_c = sin_la0 * D_SIN_LA + cos_la0 * D_COS_LA * cos_d
    x = D_COS_LA * sin_d
    y = cos_la0 * D_SIN_LA - sin_la0 * D_COS_LA * cos_d
    depth = np.clip(((cos_c - 0.03) / 0.97 * 15.999).astype(np.int32),
                    0, 15)
    return x, y, cos_c, depth


NEON = "#39ff14"
NEON_DIM = "#0a5c00"
NEON_FAINT = "#062d02"
TIMER = "#ffffff"
TIMER_GLOW = "#6e6e6e"
TIMER_FAINT = "#1f1f1f"
TIMER_SUB = "#8f8f8f"
BG = "#000000"
PANEL_BG = "#050505"
GRID_BG = "#0b0b0b"
DOT = (178, 178, 178)
PALE_RED = (255, 154, 154)
PALE_RED_DIM = (122, 58, 58)
ORANGE = (255, 167, 38)
RED_BEAM = (255, 42, 42)


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int],
          t: float) -> tuple[int, int, int]:
    return (int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t))


SEG_MAP = {
    "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc",
    "5": "afgcd", "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcfgd",
    "-": "g", " ": "",
}


def _hpoly(x: float, y: float, L: float, T: float) -> list[float]:
    h = T / 2.0
    return [x + h, y, x + L - h, y, x + L, y + h, x + L - h, y + T,
            x + h, y + T, x, y + h]


def _vpoly(x: float, y: float, L: float, T: float) -> list[float]:
    h = T / 2.0
    return [x + h, y, x + T, y + h, x + T, y + L - h, x + h, y + L,
            x, y + L - h, x, y + h]


class SevenSeg:
    """Glowing-green 7-segment digit renderer on a tkinter Canvas."""

    def __init__(self, canvas, glow: bool = True):
        self.cv = canvas
        self.glow = glow

    def digit(self, x: float, y: float, w: float, h: float, ch: str,
              color: str = TIMER, ghost: bool = True) -> float:
        """Draw digit, return advance width (w + gap)."""
        T = max(3.0, w * 0.24)
        pad = T * 0.7
        vlen = h / 2.0 - pad * 1.2
        H = w - 2 * pad
        segs = {
            "a": _hpoly(x + pad, y, H, T),
            "g": _hpoly(x + pad, y + h / 2.0 - T / 2.0, H, T),
            "d": _hpoly(x + pad, y + h - T, H, T),
            "f": _vpoly(x, y + pad, vlen, T),
            "b": _vpoly(x + w - T, y + pad, vlen, T),
            "e": _vpoly(x, y + h / 2.0 + pad * 0.2, vlen, T),
            "c": _vpoly(x + w - T, y + h / 2.0 + pad * 0.2, vlen, T),
        }
        on = set(SEG_MAP.get(ch, ""))
        for name, poly in segs.items():
            if name in on:
                if self.glow:
                    self.cv.create_polygon(poly, fill=TIMER_GLOW,
                                           outline="", width=0)

                    self.cv.create_polygon(poly, fill="", outline=TIMER_GLOW,
                                           width=3)
                self.cv.create_polygon(poly, fill=color, outline=color,
                                       width=1)
            elif ghost:
                self.cv.create_polygon(poly, fill=TIMER_FAINT, outline="",
                                       width=0)
        return w + T * 0.55

    def colon(self, x: float, y: float, w: float, h: float,
              on: bool = True, color: str = TIMER) -> float:
        T = max(3.0, w * 0.24)
        for cy in (y + h * 0.30, y + h * 0.66):
            fill = color if on else TIMER_FAINT
            if on and self.glow:
                self.cv.create_rectangle(x - 2, cy - 2, x + T + 2, cy + T + 2,
                                         fill=TIMER_GLOW, outline="")
            self.cv.create_rectangle(x, cy, x + T, cy + T, fill=fill,
                                     outline=fill)
        return T + w * 0.3

    def number(self, x: float, y: float, w: float, h: float, text: str,
               color: str = TIMER, blink_colon: bool = False,
               subsec: int = 0) -> float:
        cx = x
        for ch in text:
            if ch == ":":
                on = True
                if blink_colon:
                    on = (subsec % 1000) < 600
                cx += self.colon(cx, y, w * 0.5, h, on=on, color=color)
            else:
                cx += self.digit(cx, y, w, h, ch, color=color)
        return cx - x


class GlobeApp:
    ALERT_WINDOW_H = 48.0
    REFRESH_MIN = 15

    def __init__(self, root, utc: bool = False, use_12h: bool = False,
                 high_only: bool = True):
        self.root = root
        self.utc = utc
        self.use_12h = use_12h
        self.high_only = high_only
        self.events: list[dict] = []
        self.source = "loading…"
        self.lock = threading.Lock()
        self.lon0 = -40.0
        self.lat0 = 22.0
        self.zoom = 1.0
        self.auto = True
        self.last_drag = 0.0
        self.pulse = 0.0
        self.show_utc_btn = None
        # Hover highlight: last pointer pixel + resolved country.
        self.hover_xy: tuple[int, int] | None = None
        self.hover_cur: str | None = None
        self.hover_latlon: tuple[float, float] | None = None
        self.hover_label = "— hover globe —"
        self._globe_S = 500
        if not HAVE_PIL:
            raise RuntimeError("Pillow is required for the globe (pip install pillow)")
        self._build_ui()
        self._fetch_async()
        try:
            schedule_google_refresh()
        except Exception:
            pass
        self._tick_timer()
        self._tick_globe()
        self._tick_log()
        self._slow_refresh()


    def _build_ui(self):
        import tkinter as tk
        from tkinter import ttk
        r = self.root
        r.title("FOREX NEWS GLOBE // LED TICKER")
        r.configure(bg=BG)
        try:
            r.geometry("1080x970")
            r.minsize(920, 700)
        except Exception:
            pass

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground="#9a9a9a",
                        font=("Courier New", 9))
        style.configure("TButton", background="#0d0d0d", foreground=NEON,
                        font=("Courier New", 9, "bold"),
                        borderwidth=1, relief="solid")
        style.map("TButton", background=[("active", "#0f2a0f")])
        style.configure("TCheckbutton", background=BG, foreground="#9a9a9a",
                        font=("Courier New", 9))


        hdr = ttk.Frame(r)
        hdr.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(hdr, text="◤ FOREX NEWS GLOBE // LED TICKER ◥",
                  foreground=NEON,
                  font=("Courier New", 12, "bold")).pack(side="left")
        self.src_lbl = ttk.Label(hdr, text="loading…")
        self.src_lbl.pack(side="right")


        self.timer_cv = tk.Canvas(r, height=206, bg=BG, highlightthickness=1,
                                  highlightbackground="#1a1a1a")
        self.timer_cv.pack(fill="x", padx=10, pady=8)
        self.seg = SevenSeg(self.timer_cv)


        mid = ttk.Frame(r)
        mid.pack(fill="both", expand=True, padx=10)
        self.globe_lbl = tk.Label(mid, bg=BG, bd=1, relief="solid")
        self.globe_lbl.pack(side="left", fill="both", expand=True)
        self.globe_lbl.bind("<ButtonPress-1>", self._drag_start)
        self.globe_lbl.bind("<B1-Motion>", self._drag_move)
        self.globe_lbl.bind("<Motion>", self._hover_move)
        self.globe_lbl.bind("<Leave>", self._hover_leave)
        self.globe_lbl.bind("<MouseWheel>", self._wheel)
        self.globe_lbl.bind("<Button-4>", lambda e: self._zoom(1.08))
        self.globe_lbl.bind("<Button-5>", lambda e: self._zoom(1 / 1.08))
        r.bind("<Left>", lambda e: self._nudge(8, 0))
        r.bind("<Right>", lambda e: self._nudge(-8, 0))
        r.bind("<Up>", lambda e: self._nudge(0, 5))
        r.bind("<Down>", lambda e: self._nudge(0, -5))
        r.bind("<space>", lambda e: self._toggle_auto())

        side = ttk.Frame(mid, width=290)
        side.pack(side="right", fill="y", padx=(8, 0))
        side.pack_propagate(False)
        ttk.Label(side, text="── NEXT RELEASE ──",
                  foreground=NEON,
                  font=("Courier New", 10, "bold")).pack(pady=(2, 2))
        self.next_cd = tk.Label(side, text="--:--:--", bg=BG, fg=TIMER,
                                font=("Courier New", 26, "bold"))
        self.next_cd.pack()
        self.next_info = tk.Label(side, text="waiting for feed…", bg=BG,
                                  fg="#e8e8e8", font=("Courier New", 10),
                                  wraplength=280, justify="left")
        self.next_info.pack(pady=2, anchor="w")
        ttk.Label(side, text="── STATES ON ALERT ──",
                  foreground=NEON,
                  font=("Courier New", 10, "bold")).pack(pady=(4, 2))
        self.alert_box = tk.Label(side, text="—", bg=BG, fg="#ffb3b3",
                                  font=("Courier New", 10), wraplength=280,
                                  justify="left")
        self.alert_box.pack(anchor="w")
        ttk.Label(side, text="── CONTROLS ──", foreground=NEON,
                  font=("Courier New", 10, "bold")).pack(pady=(4, 2))
        btns = ttk.Frame(side)
        btns.pack(fill="x")
        self.utc_btn = ttk.Button(btns, text="UTC" if not self.utc else "LOCAL",
                                  command=self._toggle_tz, width=8)
        self.utc_btn.pack(side="left", padx=2)
        ttk.Button(btns, text="12H" if not self.use_12h else "24H",
                   command=self._toggle_12h, width=8).pack(side="left",
                                                           padx=2)
        self.auto_btn = ttk.Button(btns, text="PAUSE",
                                   command=self._toggle_auto, width=10)
        self.auto_btn.pack(side="left", padx=2)
        brow2 = ttk.Frame(side)
        brow2.pack(fill="x", pady=2)
        ttk.Button(brow2, text="REFRESH NEWS",
                   command=self._fetch_async).pack(side="left", padx=2)
        self.hivar = tk.BooleanVar(value=self.high_only)
        ttk.Checkbutton(brow2, text="High only", variable=self.hivar,
                        command=self._toggle_filter).pack(side="left",
                                                          padx=6)
        ttk.Label(side, wraplength=280, justify="left",
                  text="drag globe to scroll - hover highlights country - wheel = zoom - arrows = rotate - space = pause",
                  font=("Courier New", 8)).pack(pady=4)
        self.legend = tk.Label(side, bg=BG, fg="#9a9a9a",
                               font=("Courier New", 9), justify="left",
                               text="* grey = land\n* pale-red = HIGH news <48h\n* orange spike = event count\n* red beam = next release")
        self.legend.pack(anchor="w")
        ttk.Label(side, text="── HOVER ──", foreground=NEON,
                  font=("Courier New", 10, "bold")).pack(pady=(4, 2))
        self.hover_lbl = tk.Label(side, bg=BG, fg="#7df9ff",
                                  font=("Courier New", 10, "bold"),
                                  wraplength=280, justify="left",
                                  text="— hover globe —")
        self.hover_lbl.pack(anchor="w")


        logf = ttk.Frame(r)
        logf.pack(fill="both", expand=False, padx=10, pady=8)
        ttk.Label(logf, text="── NEWS LOG · details + minutes to release ──",
                  foreground=NEON,
                  font=("Courier New", 10, "bold")).pack(anchor="w")
        self.log = tk.Text(logf, height=7, bg="#020202", fg="#c9c9c9",
                           insertbackground=NEON, relief="solid", bd=1,
                           font=("Courier New", 10), wrap="none",
                           state="disabled")
        sb = tk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, fg, bg in (("high", "#ffb3b3", "#1c0606"),
                            ("med", "#ffd28a", None),
                            ("live", "#ffffff", "#3d0000"),
                            ("dim", "#6f6f6f", None),
                            ("head", NEON, None)):
            self.log.tag_config(tag, foreground=fg,
                                background=bg or "#020202")
        foot = ttk.Frame(r)
        foot.pack(fill="x", padx=10, pady=(0, 8))
        self.fps_lbl = ttk.Label(foot, text="")
        self.fps_lbl.pack(side="left")
        ttk.Label(foot, text="inspo2 timer · inspo globe · forexfactory feed"
                  ).pack(side="right")
        self._photo = None
        self._t0 = time.monotonic()
        self._frames = 0
        self._fps = 0.0
        self._drag_xy: tuple[int, int] | None = None


    def _fetch_async(self):
        self.src_lbl.configure(text="fetching…")
        th = threading.Thread(target=self._fetch_job, daemon=True)
        th.start()

    def _fetch_job(self):
        evs, src = fetch_ff_events()
        with self.lock:
            self.events = evs
            self.source = src

    def _slow_refresh(self):
        self._fetch_async()
        self.root.after(self.REFRESH_MIN * 60 * 1000, self._slow_refresh)

    def _snapshot(self):
        with self.lock:
            return list(self.events), self.source

    def _alerts(self, events, now):
        """currency -> {'count', 'next_min', 'events'} for window."""
        alerts: dict[str, dict] = {}
        for e in events:
            if self.high_only and e["impact"] != "High":
                continue
            m = minutes_to(e["dt"], now)
            if -30 <= m <= self.ALERT_WINDOW_H * 60:
                cur = e["currency"]
                a = alerts.setdefault(cur, {"count": 0, "next_min": 1e18,
                                            "events": []})
                a["count"] += 1
                a["next_min"] = min(a["next_min"], m)
                a["events"].append(e)
        for a in alerts.values():
            a["events"].sort(key=lambda e: e["dt"])
        return alerts

    def _next_event(self, events, now):
        best = None
        for e in events:
            if self.high_only and e["impact"] != "High":
                continue
            if minutes_to(e["dt"], now) >= -5:
                if best is None or e["dt"] < best["dt"]:
                    best = e
        return best


    def _tick_timer(self):
        try:
            self._draw_timer()
        except Exception:
            pass
        self.root.after(33, self._tick_timer)

    def _now_dt(self):
        if self.utc:
            return dt.datetime.now(dt.timezone.utc), "UTC"
        d = dt.datetime.now().astimezone()
        return d, d.tzname() or "local"

    def _draw_timer(self):
        cv = self.timer_cv
        cv.delete("all")
        W = max(600, cv.winfo_width() - 4)
        H = 206

        for gx in range(0, W, 44):
            cv.create_line(gx, 0, gx, H, fill="#070707")
        now_ns = time.time_ns()
        sec, sub = divmod(now_ns, 1_000_000_000)
        ms, us = _split_ns(sub)
        d, tz = self._now_dt()
        hr = d.hour
        ampm = ""
        if self.use_12h:
            ampm = "AM" if hr < 12 else "PM"
            hr = hr % 12 or 12
        big_h, big_w = 104.0, 60.0
        x0, y0 = 26.0, 12.0
        self.seg.number(x0, y0, big_w, big_h, f"{hr:02d}" if not self.use_12h
                        or hr >= 10 else f" {hr}",
                        blink_colon=False, subsec=sub // 1_000_000)
        x0 += 2 * (big_w + big_w * 0.24 * 0.55) + 8
        self.seg.colon(x0, y0, big_w * 0.5, big_h,
                       on=(sub // 1_000_000) < 600)
        x0 += big_w * 0.5 * 0.3 + max(3.0, big_w * 0.5 * 0.24) + 8
        self.seg.number(x0, y0, big_w, big_h, f"{d.minute:02d}")

        sx = x0 + 2 * (big_w + 16) + 22
        self.seg.number(sx, y0 + 6, 27.0, 46.0, f"{d.second:02d}")
        cv.create_text(sx + 4, y0 + 64, text=f"{ms:03d} {us:03d}",
                       fill=TIMER, font=("Courier New", 13, "bold"),
                       anchor="w")
        cv.create_text(sx + 4, y0 + 86, text="MS   US",
                       fill=TIMER_SUB, font=("Courier New", 9, "bold"),
                       anchor="w")
        if ampm:
            cv.create_text(W - 30, 34, text=ampm, fill=TIMER,
                           font=("Courier New", 26, "bold"), anchor="e")
        else:
            cv.create_text(W - 30, 34,
                           text="HR  MN" if not self.use_12h else "HR MN",
                           fill=TIMER_SUB, font=("Courier New", 12, "bold"),
                           anchor="e")

        months = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
                  "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER",
                  "DECEMBER"]
        date_s = (f"{months[d.month - 1]} {d.day:02d} {d.year}   "
                  f"{d.strftime('%A').upper()}   {tz}")
        cv.create_text(30, 148, text=date_s, fill=TIMER,
                       font=("Courier New", 17, "bold"), anchor="w")
        cv.create_text(30, 176, text="HR : MN : SC : MS : US",
                       fill=TIMER_SUB, font=("Courier New", 10, "bold"),
                       anchor="w")


    def _drag_start(self, e):
        self._drag_xy = (e.x, e.y)
        self.last_drag = time.monotonic()

    def _drag_move(self, e):
        if self._drag_xy is None:
            return
        dx = e.x - self._drag_xy[0]
        dy = e.y - self._drag_xy[1]
        self._drag_xy = (e.x, e.y)
        self.lon0 = (self.lon0 - dx * 0.45) % 360.0
        self.lat0 = max(-60.0, min(60.0, self.lat0 + dy * 0.3))
        self.last_drag = time.monotonic()
        self.hover_xy = (e.x, e.y)

    def _hover_move(self, e):
        self.hover_xy = (e.x, e.y)
        self.last_drag = time.monotonic()

    def _hover_leave(self, _e):
        self.hover_xy = None
        self.hover_cur = None
        self.hover_latlon = None
        self.hover_label = "— hover globe —"

    def _invert_project(self, px: float, py: float, cx: float, cy: float,
                        R: float) -> tuple[float, float] | None:
        """Screen pixel -> (lat, lon) on the visible globe half, else None."""
        xp = (px - cx) / R
        ym = (cy - py) / R  # math-y (up positive)
        rho = math.hypot(xp, ym)
        if rho > 1.0:
            return None
        c = math.asin(min(1.0, max(-1.0, rho)))
        la0 = math.radians(self.lat0)
        lo0 = math.radians(self.lon0 if self.lon0 <= 180 else self.lon0 - 360)
        if rho < 1e-9:
            return math.degrees(la0), (math.degrees(lo0) + 540) % 360 - 180
        sin_c, cos_c = math.sin(c), math.cos(c)
        lat = math.asin(cos_c * math.sin(la0) +
                        (ym * sin_c * math.cos(la0)) / rho)
        lon = lo0 + math.atan2(xp * sin_c,
                               rho * math.cos(la0) * cos_c -
                               ym * math.sin(la0) * sin_c)
        lon_deg = (math.degrees(lon) + 540) % 360 - 180
        return math.degrees(lat), lon_deg

    def _resolve_hover(self, S: int):
        """Refresh hover country from last pointer pixel + current rotation."""
        if self.hover_xy is None:
            self.hover_cur = None
            self.hover_latlon = None
            self.hover_label = "— hover globe —"
            return
        # Widget pixels -> square-globe pixels.  The rendered square S may be
        # smaller than the widget; assume top-left anchored like the PhotoImage.
        cx = cy = S / 2.0
        R = (S / 2.0 - 26) * self.zoom
        ll = self._invert_project(float(self.hover_xy[0]),
                                  float(self.hover_xy[1]), cx, cy, R)
        if ll is None:
            self.hover_cur = None
            self.hover_latlon = None
            self.hover_label = "○ ocean / space"
            return
        self.hover_latlon = ll
        cur = country_at(ll[0], ll[1])
        self.hover_cur = cur
        if cur is None:
            self.hover_label = f"○ {ll[0]:+.1f}°, {ll[1]:+.1f}° (no watchlist CCY)"
        else:
            _la, _lo, _r, name = COUNTRIES[cur]
            self.hover_label = f"▶ {cur} {name}  ({ll[0]:+.1f}°, {ll[1]:+.1f}°)"

    def _zoom(self, f: float):
        self.zoom = max(0.65, min(1.5, self.zoom * f))

    def _wheel(self, e):
        self._zoom(1.08 if getattr(e, "delta", 0) > 0 else 1 / 1.08)

    def _nudge(self, dx: float, dy: float):
        self.lon0 = (self.lon0 + dx) % 360.0
        self.lat0 = max(-60.0, min(60.0, self.lat0 + dy))
        self.last_drag = time.monotonic()

    def _toggle_auto(self):
        self.auto = not self.auto
        self.auto_btn.configure(text="PAUSE" if self.auto else "SPIN")

    def _toggle_tz(self):
        self.utc = not self.utc
        self.utc_btn.configure(text="UTC" if not self.utc else "LOCAL")

    def _toggle_12h(self):
        self.use_12h = not self.use_12h
        self._rebuild_12h_label()

    def _rebuild_12h_label(self):
        import tkinter as tk

        def walk(w):
            for c in w.winfo_children():
                try:
                    if isinstance(c, tk.Button) or c.winfo_class() == "TButton":
                        t = c.cget("text")
                        if t in ("12H", "24H"):
                            c.configure(text="12H" if not self.use_12h
                                        else "24H")
                except Exception:
                    pass
                walk(c)
        walk(self.root)

    def _toggle_filter(self):
        self.high_only = bool(self.hivar.get())

    def _project(self, lat: float, lon: float, cx: float, cy: float,
                 R: float):
        """Orthographic projection. Returns (x, y, cos_c) or None if hidden."""
        la, lo = math.radians(lat), math.radians(lon - self.lon0)
        la0 = math.radians(self.lat0)
        cos_c = (math.sin(la0) * math.sin(la) +
                 math.cos(la0) * math.cos(la) * math.cos(lo))
        x = R * math.cos(la) * math.sin(lo)
        y = R * (math.cos(la0) * math.sin(la) -
                 math.sin(la0) * math.cos(la) * math.cos(lo))
        return cx + x, cy - y, cos_c

    def _tick_globe(self):
        try:
            if self.auto and (time.monotonic() - self.last_drag) > 3.0:
                self.lon0 = (self.lon0 + 0.55) % 360.0
            self.pulse += 0.12
            self._draw_globe()
        except Exception:
            pass
        self.root.after(90, self._tick_globe)

    def _draw_globe(self):
        events, _ = self._snapshot()
        now = dt.datetime.now(dt.timezone.utc)
        w = max(420, self.globe_lbl.winfo_width() - 4)
        S = min(500, max(380, w))
        self._globe_S = S
        self._resolve_hover(S)
        img = self.render_globe(events, now, S)
        self._photo = ImageTk.PhotoImage(img)
        self.globe_lbl.configure(image=self._photo)
        self._update_side(events, now)

    def render_globe(self, events, now, S: int = 500):
        """Pure render of the dotted globe -> PIL Image (headless-friendly)."""
        alerts = self._alerts(events, now)
        nxt = self._next_event(events, now)
        img = Image.new("RGB", (S, S), (0, 0, 0))
        dr = ImageDraw.Draw(img)
        for gx in range(0, S, 40):
            dr.line([(gx, 0), (gx, S)], fill=(11, 11, 11))
            dr.line([(0, gx), (S, gx)], fill=(11, 11, 11))
        cx = cy = S / 2.0
        R = (S / 2.0 - 26) * self.zoom


        grat = (46, 46, 46)
        if HAVE_NP and _GRAT:
            for _lats, _lons in _GRAT:
                _gx, _gy, _gc = _project_line(_lats, _lons, self.lon0,
                                              self.lat0)
                _pts: list = []
                for _k in range(97):
                    if _gc[_k] > 0.02:
                        _pts.append((cx + float(_gx[_k]) * R,
                                     cy - float(_gy[_k]) * R))
                    else:
                        if len(_pts) > 1:
                            dr.line(_pts, fill=grat)
                        _pts = []
                if len(_pts) > 1:
                    dr.line(_pts, fill=grat)
        else:
            for lat in range(-60, 61, 30):
                pts = []
                for k in range(97):
                    pr = self._project(lat, -180 + k * (360 / 96), cx, cy, R)
                    if pr is not None and pr[2] > 0.02:
                        pts.append((pr[0], pr[1]))
                    else:
                        if len(pts) > 1:
                            dr.line(pts, fill=grat)
                        pts = []
                if len(pts) > 1:
                    dr.line(pts, fill=grat)
            for lon in range(-180, 180, 30):
                pts = []
                for k in range(97):
                    pr = self._project(-90 + k * (180 / 96), lon, cx, cy, R)
                    if pr is not None and pr[2] > 0.02:
                        pts.append((pr[0], pr[1]))
                    else:
                        if len(pts) > 1:
                            dr.line(pts, fill=grat)
                        pts = []
                if len(pts) > 1:
                    dr.line(pts, fill=grat)
        dr.ellipse([cx - R, cy - R, cx + R, cy + R], outline=(90, 90, 90))


        imminent_cur = nxt["currency"] if nxt else None

        hover_cur = getattr(self, "hover_cur", None)

        if HAVE_NP and D_SIN_LA is not None:
            self._draw_dots_fast(img, alerts, imminent_cur, hover_cur,
                                 cx, cy, R, S)
        else:
            self._draw_dots_slow(dr, alerts, imminent_cur, hover_cur,
                                 cx, cy, R, S)

        for cur, a in sorted(alerts.items(), key=lambda kv: kv[1]["count"]):
            cc = COUNTRIES.get(cur)
            if cc is None:
                continue
            pr = self._project(cc[0], cc[1], cx, cy, R)
            if pr is None or pr[2] <= 0.05:
                continue
            x, y, _ = pr
            dx, dy = x - cx, y - cy
            n = math.hypot(dx, dy) or 1.0
            ux, uy = dx / n, dy / n
            L = min(64, 12 + 7 * a["count"])
            is_next = (cur == imminent_cur)
            col = RED_BEAM if is_next else ORANGE
            wdt = 3 if is_next else 2
            dr.line([(x, y), (x + ux * L, y + uy * L)], fill=col, width=wdt)
            tip = 3 if is_next else 2
            dr.ellipse([x + ux * L - tip, y + uy * L - tip,
                        x + ux * L + tip, y + uy * L + tip], fill=col)

        tops = [c for c in alerts if c in COUNTRIES][:4]
        for i in range(len(tops)):
            for j in range(i + 1, len(tops)):
                a = COUNTRIES[tops[i]][:2]
                b = COUNTRIES[tops[j]][:2]
                pts = []
                for k in range(33):
                    t = k / 32.0
                    la = a[0] + (b[0] - a[0]) * t + math.sin(t * math.pi) * 12
                    dlo = ((b[1] - a[1] + 540) % 360) - 180
                    lo = a[1] + dlo * t
                    pr = self._project(la, lo, cx, cy, R)
                    if pr is not None and pr[2] > 0.05:
                        pts.append((pr[0], pr[1]))
                    else:
                        if len(pts) > 1:
                            for sgm in range(0, len(pts) - 1, 2):
                                dr.line([pts[sgm], pts[sgm + 1]],
                                        fill=(120, 120, 120))
                        pts = []
                if len(pts) > 1:
                    for sgm in range(0, len(pts) - 1, 2):
                        dr.line([pts[sgm], pts[sgm + 1]],
                                fill=(120, 120, 120))

        # Hover highlight ring + label: shows exactly which watchlist
        # country the pointer is over while scrolling/dragging.
        hover_cur = getattr(self, "hover_cur", None)
        hover_latlon = getattr(self, "hover_latlon", None)
        if hover_cur is not None and hover_cur in COUNTRIES:
            cc = COUNTRIES[hover_cur]
            pr = self._project(cc[0], cc[1], cx, cy, R)
            if pr is not None and pr[2] > 0.03:
                hx, hy, _ = pr
                rr = 14 + 2.0 * math.sin(self.pulse * 2.0)
                dr.ellipse([hx - rr, hy - rr, hx + rr, hy + rr],
                           outline=(125, 249, 255), width=2)
                dr.ellipse([hx - 3, hy - 3, hx + 3, hy + 3],
                           fill=(125, 249, 255))
        if hover_latlon is not None:
            if hover_cur is not None:
                htxt = f"▶ {hover_cur} {COUNTRIES[hover_cur][3]}"
            else:
                htxt = f"○ {hover_latlon[0]:+.1f}°, {hover_latlon[1]:+.1f}°"
            dr.text((40, S - 28), htxt, fill=(125, 249, 255))

        m, L = 10, 26
        for (x0, y0, dx, dy) in ((m, m, 1, 1), (S - m, m, -1, 1),
                                 (m, S - m, 1, -1), (S - m, S - m, -1, -1)):
            dr.line([(x0, y0), (x0 + dx * L, y0)], fill=(120, 120, 120),
                    width=2)
            dr.line([(x0, y0), (x0, y0 + dy * L)], fill=(120, 120, 120),
                    width=2)

        dr.text((40, 12), f"LON {self.lon0:06.1f}  LAT {self.lat0:+05.1f}",
                fill=(90, 90, 90))
        return img

    def _draw_dots_fast(self, img, alerts, imminent_cur, hover_cur,
                        cx: float, cy: float, R: float, S: int) -> None:
        """Blit all ~38k dots via numpy (~4ms).  No per-dot Python."""
        import numpy as _np
        x, y, cos_c, depth = _project_dots(self.lon0, self.lat0)
        vis = (cos_c > 0.03)
        if not bool(vis.any()):
            return
        alert_idx = _np.array([CCY_LIST.index(c) for c in alerts
                               if c in CCY_LIST], dtype=_np.int8)
        is_alert = _np.isin(DOTS_CCY,
                            alert_idx) if alert_idx.size else _np.zeros(
                                DOTS_CCY.shape, dtype=bool)
        imm_idx = CCY_LIST.index(imminent_cur) \
            if imminent_cur in CCY_LIST else -1
        is_imm = (is_alert & (DOTS_CCY == imm_idx)) if imm_idx >= 0 \
            else _np.zeros(DOTS_CCY.shape, dtype=bool)
        hov_idx = CCY_LIST.index(hover_cur) \
            if hover_cur in CCY_LIST else -1
        is_hov = (DOTS_CCY == hov_idx) if hov_idx >= 0 else _np.zeros(
            DOTS_CCY.shape, dtype=bool)
        blink = 0.75 + 0.25 * math.sin(self.pulse * 2.0)
        imm_col = (255, int(120 * blink + 60), int(120 * blink + 60))
        px = _np.clip((cx + x * R).astype(_np.int32), 0, S - 1)
        py = _np.clip((cy - y * R).astype(_np.int32), 0, S - 1)
        buf = _np.array(img)  # writable copy; pasted back at the end
        # size class: 1 | 2 | 3 (+1 when hovered)
        deep = depth >= 7  # == old depth>=0.45 (7/16=0.4375)
        size = _np.where(is_alert, _np.where(deep, 3, 2),
                         _np.where(deep, 2, 1)).astype(_np.int32)
        size = _np.where(is_hov, size + 1, size)
        v = vis
        col = _np.empty((DOTS_CCY.shape[0], 3), dtype=_np.uint8)
        col[v] = LUT_NORMAL[depth[v]]
        hov_only = v & is_hov & ~is_alert
        col[hov_only] = LUT_HOVER[depth[hov_only]]
        al = v & is_alert & ~is_imm
        col[al] = LUT_ALERT[depth[al]]
        im = v & is_imm
        if bool(im.any()):
            base = LUT_ALERT[depth[im]].astype(_np.int32)
            tgt = _np.array(imm_col, dtype=_np.int32)
            col[im] = (base + (tgt - base) *
                       (depth[im][:, None] / 15.0)).astype(_np.uint8)
            if hov_idx >= 0:
                both = im & is_hov
                c2 = col[both].astype(_np.int32)
                c2[:, 0] = _np.minimum(255, c2[:, 0] + 60)
                c2[:, 1] = _np.minimum(255, c2[:, 1] + 40)
                c2[:, 2] = _np.minimum(255, c2[:, 2] + 40)
                col[both] = c2.astype(_np.uint8)
        buf[py[v], px[v]] = col[v]
        # grow dots: size>=2 fills 2x2, >=3 fills 3x3 block, >=4 adds cross
        for _dx, _dy in ((1, 0), (0, 1), (1, 1)):
            m = v & (size >= 2)
            if not bool(m.any()):
                break
            qx = _np.clip(px[m] + _dx, 0, S - 1)
            qy = _np.clip(py[m] + _dy, 0, S - 1)
            buf[qy, qx] = col[m]
        m3 = v & (size >= 3)
        if bool(m3.any()):
            for _dx, _dy in ((-1, 0), (0, -1), (-1, -1), (2, 0), (0, 2),
                             (2, 1), (1, 2), (2, 2)):
                qx = _np.clip(px[m3] + _dx, 0, S - 1)
                qy = _np.clip(py[m3] + _dy, 0, S - 1)
                buf[qy, qx] = col[m3]
        m4 = v & (size >= 4)
        if bool(m4.any()):
            for _dx, _dy in ((-1, 1), (1, -1), (3, 1), (1, 3), (-2, 0),
                             (0, -2)):
                qx = _np.clip(px[m4] + _dx, 0, S - 1)
                qy = _np.clip(py[m4] + _dy, 0, S - 1)
                buf[qy, qx] = col[m4]
        img.paste(Image.fromarray(buf))

    def _draw_dots_slow(self, dr, alerts, imminent_cur, hover_cur,
                        cx: float, cy: float, R: float, S: int) -> None:
        """Original per-dot fallback when numpy is unavailable."""
        for (la, lo), dot_cur in zip(LAND_DOTS, DOT_COUNTRY):
            pr = self._project(la, lo, cx, cy, R)
            if pr is None or pr[0] < 0 or pr[0] >= S:
                continue
            x, y, c = pr
            if c <= 0.03:
                continue
            depth = min(1.0, max(0.0, (c - 0.03) / 0.97))
            hit = None
            if dot_cur is not None and dot_cur in alerts:
                hit = (dot_cur, alerts[dot_cur]["next_min"])
            is_hover = hover_cur is not None and dot_cur == hover_cur
            if hit is None:
                if is_hover:
                    # Hovered country: bright cyan-white so it pops while
                    # scrolling, even with no news alert.
                    base = _lerp((60, 120, 120), (180, 255, 255), depth)
                else:
                    base = _lerp((40, 40, 40), DOT, depth)
            else:
                base = _lerp(PALE_RED_DIM, PALE_RED, depth)
                if hit[0] == imminent_cur:
                    blink = 0.75 + 0.25 * math.sin(self.pulse * 2.0)
                    base = _lerp(PALE_RED_DIM,
                                 (255, int(120 * blink + 60), int(120 * blink + 60)),
                                 depth)
                if is_hover:
                    # Hovered alert country: push toward white-hot.
                    base = (min(255, base[0] + 60), min(255, base[1] + 40),
                            min(255, base[2] + 40))
            s = 1 if depth < 0.45 else 2
            if hit is not None:
                s = 2 if depth < 0.5 else 3
            if is_hover:
                s += 1
            dr.rectangle([x - s / 2, y - s / 2, x + s / 2, y + s / 2],
                         fill=base)

    def _update_side(self, events, now):
        alerts = self._alerts(events, now)
        nxt = self._next_event(events, now)

        self._frames += 1
        el = time.monotonic() - self._t0
        if el >= 1.0:
            self._fps = self._frames / el
            self._frames = 0
            self._t0 = time.monotonic()
        self.fps_lbl.configure(
            text=f"globe {self._fps:.0f} fps · {len(LAND_DOTS)} dots · "
                 f"{MAP_SOURCE} · "
                 f"{'AUTO-SPIN' if self.auto else 'HELD'}")
        try:
            self.hover_lbl.configure(text=getattr(self, "hover_label",
                                                  "— hover globe —"))
        except Exception:
            pass
        if nxt is not None:
            m = minutes_to(nxt["dt"], now)
            self.next_cd.configure(text=self._cd_big(m))
            if self.utc:
                when = nxt["dt"].astimezone(dt.timezone.utc).strftime(
                    "%a %H:%M UTC")
            else:
                when = nxt["dt"].astimezone().strftime("%a %H:%M %Z")
            self.next_info.configure(
                text=f"{nxt['currency']}  {nxt['impact'].upper()}\n"
                     f"{nxt['title']}\n{when} · {fmt_countdown(m)}\n"
                     f"F:{nxt['forecast']}  P:{nxt['previous']}")
        else:
            self.next_cd.configure(text="--:--:--")
            self.next_info.configure(text="no upcoming events in filter")
        if alerts:
            lines = []
            for cur, a in sorted(alerts.items(),
                                 key=lambda kv: kv[1]["next_min"]):
                nm = COUNTRIES.get(cur, ("", "", 0, cur))[3]
                lines.append(f"> {cur} {nm}: {a['count']} x "
                             f"next {fmt_countdown(a['next_min'])}")
            self.alert_box.configure(text="\n".join(lines))
        else:
            self.alert_box.configure(text="— no HIGH-impact news <48h —")

    @staticmethod
    def _cd_big(m: float) -> str:
        if -5 <= m <= 5:
            return "LIVE"
        neg = m < 0
        s = int(abs(m) * 60)
        h, rem = divmod(s, 3600)
        mi, se = divmod(rem, 60)
        t = f"{h:02d}:{mi:02d}:{se:02d}"
        return f"T-{t}" if not neg else f"T+{t}"


    def _tick_log(self):
        try:
            self._draw_log()
        except Exception:
            pass
        self.root.after(1000, self._tick_log)

    def _draw_log(self):
        events, src = self._snapshot()
        self.src_lbl.configure(text=f"{src} · {len(events)} events · "
                                    f"{dt.datetime.now().strftime('%H:%M:%S')}")
        now = dt.datetime.now(dt.timezone.utc)
        rows = []
        for e in events:
            if self.high_only and e["impact"] != "High":
                continue
            m = minutes_to(e["dt"], now)
            if -120 <= m <= 7 * 24 * 60:
                rows.append((m, e))
        rows.sort(key=lambda r: r[0])
        rows = rows[:60]
        y0 = self.log.yview()[0]
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("end", f"  {'IN':>12}  {'TIME':<14} {'CUR':<4} "
                               f"{'EVT':<44} {'FCAST':<10} PREV\n", "head")
        for m, e in rows:
            if self.utc:
                ts = e["dt"].astimezone(dt.timezone.utc).strftime(
                    "%m-%d %H:%M UTC")
            else:
                ts = e["dt"].astimezone().strftime("%m-%d %H:%M %Z")
            tag = None
            if -5 <= m <= 5:
                tag = "live"
            elif e["impact"] == "High":
                tag = "high"
            elif e["impact"] == "Medium":
                tag = "med"
            elif m < 0:
                tag = "dim"
            line = (f"  {fmt_countdown(m):>12}  {ts:<14} {e['currency']:<4} "
                    f"{e['title'][:44]:<44} {e['forecast'][:10]:<10} "
                    f"{e['previous']}\n")
            if tag:
                self.log.insert("end", line, tag)
            else:
                self.log.insert("end", line)
        if not rows:
            self.log.insert("end", "  no events in window — press REFRESH\n",
                            "dim")
        self.log.configure(state="disabled")
        try:
            self.log.yview_moveto(y0)
        except Exception:
            pass


def have_display() -> bool:
    if sys.platform.startswith("win"):
        return True
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return True
    return False


def run_gui(utc: bool, use_12h: bool, high_only: bool) -> int:
    import tkinter as tk
    root = tk.Tk()
    GlobeApp(root, utc=utc, use_12h=use_12h, high_only=high_only)
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="LED ticker + forex-news globe (inspo/inspo2 style)")
    ap.add_argument("--once", action="store_true",
                    help="print one reading and exit")
    ap.add_argument("--interval", type=float, default=0.01,
                    help="target seconds per frame, terminal mode (0.001..1.0)")
    ap.add_argument("--utc", action="store_true", help="UTC instead of local")
    ap.add_argument("--12h", dest="use_12h", action="store_true",
                    help="12-hour clock with AM/PM")
    ap.add_argument("--gui", action="store_true", help="force GUI")
    ap.add_argument("--terminal", action="store_true",
                    help="force terminal ticker (no GUI)")
    ap.add_argument("--high-only", dest="high_only", action="store_true",
                    default=True, help="globe/log: high impact only (default)")
    ap.add_argument("--all-impacts", dest="high_only", action="store_false",
                    help="globe/log: include medium+low impact")
    args = ap.parse_args(argv)

    if args.once or not sys.stdout.isatty():
        print(one_reading(utc=args.utc, use_12h=args.use_12h,
                          plain=not sys.stdout.isatty()))
        return 0

    want_gui = args.gui or (not args.terminal and have_display())
    if want_gui:
        try:
            return run_gui(utc=args.utc, use_12h=args.use_12h,
                           high_only=args.high_only)
        except Exception as exc:
            print(f"GUI unavailable ({exc}); falling back to terminal.",
                  file=sys.stderr)
    if not 0.001 <= args.interval <= 1.0:
        print("interval must be 0.001..1.0 seconds", file=sys.stderr)
        return 1
    return run_terminal(args.interval, args.utc, args.use_12h)


if __name__ == "__main__":
    raise SystemExit(main())
