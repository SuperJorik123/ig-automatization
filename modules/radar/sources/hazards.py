"""Free hazard feeds — "something big just hit X, go look for video there".

No keys, no accounts:
  USGS   earthquakes  earthquake.usgs.gov  (summary/2.5_day.geojson)
  NWS    US warnings  api.weather.gov      (only events that produce video)
  GDACS  world disasters, Orange/Red only   (gdacs.org/xml/rss.xml)
  NHC    Atlantic/Pacific hurricanes        (nhc.noaa.gov/CurrentStorms.json)

Each `fetch_*` returns Items with `kind="hazard"` and `metrics["alert"]` saying
whether it clears the bar NOW — a quake's "felt" reports keep coming in, so one
that is quiet at first can qualify on a later poll; the pipeline alerts an item
once, the first time it qualifies. Every hazard alert carries a Snap Map link
to its coordinates, which is where the eyewitness clips are.
"""
import logging
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests

from modules.radar import settings as S
from modules.radar.items import Item

log = logging.getLogger(__name__)

US_STATES = {
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "CA", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
    "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland",
    "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana",
    "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
    "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
    "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
    "Puerto Rico",
}


def snap_map(lat, lon, zoom: float = 11) -> str:
    if lat is None or lon is None:
        return ""
    return f"https://map.snapchat.com/@{lat:.4f},{lon:.4f},{zoom:.2f}z"


def _get(url: str, **kw):
    r = requests.get(url, headers={"User-Agent": S.USER_AGENT}, timeout=30, **kw)
    r.raise_for_status()
    return r


# --- USGS -----------------------------------------------------------------
def is_us_place(place: str) -> bool:
    tail = (place or "").rsplit(",", 1)[-1].strip()
    return tail in US_STATES


def quake_alert(p: dict) -> bool:
    """Worth an alert = PEOPLE felt it, not just a seismograph.

    A M6.1 on a mid-ocean ridge or a M5 off the Aleutians makes no video; a
    M4.9 under Los Angeles makes hundreds. USGS "felt" reports (Did You Feel
    It?) come in within minutes of a quake near people, so they are the test:
      US:    100+ felt, or M >= QUAKE_MIN_US with 10+ felt, or M6.5+
      world: M >= QUAKE_MIN_WORLD with a yellow+ PAGER alert, a tsunami flag
             or 50+ felt — or M7+ anywhere."""
    mag = float(p.get("mag") or 0)
    felt = int(p.get("felt") or 0)
    if is_us_place(p.get("place", "")):
        return felt >= 100 or (mag >= S.QUAKE_MIN_US and felt >= 10) or mag >= 6.5
    if mag >= 7.0:
        return True
    if mag < S.QUAKE_MIN_WORLD:
        return False
    return (p.get("alert") in ("yellow", "orange", "red") or bool(p.get("tsunami"))
            or felt >= 50)


def parse_usgs(data: dict) -> list:
    out = []
    for f in data.get("features", []):
        p = f.get("properties") or {}
        lon, lat = (f.get("geometry") or {}).get("coordinates", [None, None])[:2]
        place = p.get("place") or ""
        mag = float(p.get("mag") or 0)
        out.append(Item(
            source="usgs", kind="hazard", ext_id=str(f.get("id")),
            time=(p.get("time") or 0) / 1000,
            text=f"M{mag:.1f} earthquake — {place}",
            link=p.get("url") or "", place=place,
            country="United States" if is_us_place(place) else "",
            lat=lat, lon=lon,
            metrics={"alert": quake_alert(p), "mag": mag, "felt": int(p.get("felt") or 0),
                     "tsunami": bool(p.get("tsunami")), "category": "earthquake"},
        ))
    return out


def fetch_usgs() -> list:
    return parse_usgs(_get(
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson").json())


# --- NWS ------------------------------------------------------------------
NWS_EVENTS = [
    "Tornado Warning", "Flash Flood Warning", "Extreme Wind Warning",
    "Hurricane Warning", "Tsunami Warning", "Storm Surge Warning",
    "Dust Storm Warning", "Snow Squall Warning",
]
_NWS_CATEGORY = {"Tornado Warning": "weather", "Flash Flood Warning": "flood",
                 "Tsunami Warning": "disaster", "Storm Surge Warning": "flood"}


def nws_alert(p: dict) -> bool:
    """Only warnings that come with eyewitness video: a tornado CONFIRMED on the
    ground (not radar-indicated — there are dozens of those on a stormy day), a
    flash flood with considerable/catastrophic damage threat, and the rare big
    ones (extreme wind, hurricane, tsunami, storm surge, dust storm, snow squall
    — the last two cause the highway pile-ups)."""
    ev = p.get("event", "")
    params = p.get("parameters") or {}
    if ev == "Tornado Warning":
        return "OBSERVED" in [s.upper() for s in params.get("tornadoDetection", [])]
    if ev == "Flash Flood Warning":
        threat = [s.upper() for s in params.get("flashFloodDamageThreat", [])]
        return bool({"CONSIDERABLE", "CATASTROPHIC"} & set(threat))
    return ev in NWS_EVENTS


def _state(p: dict) -> str:
    ugc = ((p.get("geocode") or {}).get("UGC") or [""])[0]
    return ugc[:2]


def parse_nws(data: dict) -> list:
    out = []
    for f in data.get("features", []):
        p = f.get("properties") or {}
        state = _state(p)
        try:
            sent = parsedate_to_datetime(p.get("sent", "")).timestamp()
        except (TypeError, ValueError):
            from datetime import datetime
            try:
                sent = datetime.fromisoformat(p.get("sent", "")).timestamp()
            except ValueError:
                sent = time.time()
        # One alert per event + state per 3 hours: a tornado outbreak issues
        # dozens of warnings for the same storm.
        bucket = int(sent // (3 * 3600))
        lat = lon = None
        geom = f.get("geometry") or {}
        if geom.get("type") == "Polygon":
            ring = geom["coordinates"][0]
            lon = sum(c[0] for c in ring) / len(ring)
            lat = sum(c[1] for c in ring) / len(ring)
        out.append(Item(
            source="nws", kind="hazard",
            ext_id=f"{p.get('event')}|{state}|{bucket}",
            time=sent,
            text=f"{p.get('event')} — {p.get('areaDesc', '')[:120]}"
                 + (f"\n{p.get('headline')}" if p.get("headline") else ""),
            link=f"https://alerts.weather.gov/search?area={state}" if state else "",
            place=(p.get("areaDesc") or "")[:80], country="United States",
            lat=lat, lon=lon,
            metrics={"alert": nws_alert(p), "event": p.get("event"),
                     "category": _NWS_CATEGORY.get(p.get("event"), "weather")},
        ))
    return out


def fetch_nws() -> list:
    return parse_nws(_get("https://api.weather.gov/alerts/active",
                          params={"event": ",".join(NWS_EVENTS)}).json())


# --- GDACS ----------------------------------------------------------------
_NS = {"gdacs": "http://www.gdacs.org", "geo": "http://www.w3.org/2003/01/geo/wgs84_pos#"}
_GDACS_CATEGORY = {"EQ": "earthquake", "TC": "weather", "FL": "flood",
                   "VO": "disaster", "WF": "fire", "DR": "disaster"}


def parse_gdacs(xml_text: str) -> list:
    out = []
    root = ET.fromstring(xml_text.lstrip("﻿").encode())
    for it in root.iter("item"):
        level = (it.findtext("gdacs:alertlevel", "", _NS) or "").strip()
        etype = (it.findtext("gdacs:eventtype", "", _NS) or "").strip()
        guid = (it.findtext("guid") or "").strip()
        try:
            when = parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
        except (TypeError, ValueError):
            when = time.time()
        try:
            lat = float(it.findtext("geo:Point/geo:lat", "", _NS))
            lon = float(it.findtext("geo:Point/geo:long", "", _NS))
        except ValueError:
            lat = lon = None
        out.append(Item(
            source="gdacs", kind="hazard", ext_id=f"{guid}|{level}", time=when,
            text=(it.findtext("title") or "").strip(),
            link=(it.findtext("link") or "").strip(),
            place=(it.findtext("gdacs:country", "", _NS) or "").strip(),
            country=(it.findtext("gdacs:country", "", _NS) or "").strip(),
            lat=lat, lon=lon,
            # Droughts are "Orange" for months and never make a clip.
            metrics={"alert": level in ("Orange", "Red") and etype != "DR",
                     "level": level,
                     "category": _GDACS_CATEGORY.get(etype, "disaster")},
        ))
    return out


def fetch_gdacs() -> list:
    return parse_gdacs(_get("https://www.gdacs.org/xml/rss.xml").text)


# --- NHC ------------------------------------------------------------------
_CLASS = {"HU": "Hurricane", "TS": "Tropical Storm", "TD": "Tropical Depression",
          "STS": "Subtropical Storm", "PTC": "Potential Tropical Cyclone"}


def parse_nhc(data: dict, now: float) -> list:
    """An alert when a storm becomes a hurricane, and again at major (100 kt+)."""
    out = []
    for s in data.get("activeStorms", []):
        cls = s.get("classification", "")
        try:
            kt = int(s.get("intensity") or 0)
        except (TypeError, ValueError):
            kt = 0
        stage = "major" if cls == "HU" and kt >= 100 else cls
        try:
            lat, lon = float(s.get("latitudeNumeric")), float(s.get("longitudeNumeric"))
        except (TypeError, ValueError):
            lat = lon = None
        name = f"{_CLASS.get(cls, cls)} {s.get('name', '')}".strip()
        out.append(Item(
            source="nhc", kind="hazard", ext_id=f"{s.get('id')}|{stage}", time=now,
            text=f"{name} — {kt} kt" + (" (MAJOR)" if stage == "major" else ""),
            link="https://www.nhc.noaa.gov/", lat=lat, lon=lon,
            metrics={"alert": cls == "HU", "kt": kt, "category": "weather"},
        ))
    return out


def fetch_nhc() -> list:
    return parse_nhc(_get("https://www.nhc.noaa.gov/CurrentStorms.json").json(), time.time())


FETCHERS = {"usgs": fetch_usgs, "nws": fetch_nws, "gdacs": fetch_gdacs, "nhc": fetch_nhc}
