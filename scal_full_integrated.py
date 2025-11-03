# ======= EMBEDDED BLOCKS (auto-managed by Telegram commands) ===============
# ==== EMBEDDED_CONFIG (JSON) START
EMBEDDED_CONFIG = r"""{
  "server": {
    "port": 5320
  },
  "frame": {
    "tz": "Asia/Seoul",
    "ical_url": "https://calendar.google.com/calendar/ical/bob.gondrae%40gmail.com/private-00822d9dbbe3140b9253bf2e0bda95c6/basic.ics"
  },
  "weather": {
    "provider": "openweathermap",
    "api_key": "9809664c22a3501382380f2781e1a9da",
    "location": "Seoul, South Korea",
    "units": "metric"
  },
  "telegram": {
    "bot_token": "7523443246:AAF-fHGcw4NLgDQDRbDz7j1xOTEFYfeZPQ0",
    "allowed_user_ids": [5517670242],
    "mode": "polling",
    "webhook_base": "",
    "path_secret": ""
  },
  "google": {
    "scopes": [
      "https://www.googleapis.com/auth/calendar"
    ],
    "calendar": {
      "id": "bob.gondrae@gmail.com"
    }
  },
  "home_assistant": {
    "base_url": "http://ttms03.duckdns.org:8123",
    "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJkZDVlMWRkY2U2ZjY0Njk1ODUyMjc0OGIyOTg3OTgwOSIsImlhdCI6MTc2MTc5MzI5MywiZXhwIjoyMDc3MTUzMjkzfQ.R2ZZEPSOwyXFL6gNrVhfCmJxtcP-qFvT5EZsn5bZdVI",
    "verify_ssl": true,
    "timeout": 5,
    "include_domains": [
      "light",
      "switch"
    ],
    "include_entities": []
  },
  "todoist": {
    "api_token": "0aa4d2a4f95e952a1f635c14d6c6ba7e3b26bc2b",
    "max_items": 20
  },
  "bus": {
    "city_code": "",
    "node_id": "",
    "key": "3d3d725df7c8daa3445ada3ceb7778d94328541e6eb616f02c0b82cb11ff182f"
  }
}"""
# ==== EMBEDDED_CONFIG (JSON) END

# ==== EMBEDDED_VERSES START
EMBEDDED_VERSES = r"""테스트"""
# ==== EMBEDDED_VERSES END
# ===========================================================================
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fully-integrated Smart Frame + Telegram bot with Google OAuth routes
+ Telegram Calendar: view / edit-title / edit-time / delete
( /cal menu is merged into /set -> '6) Manage Events' )
(UI restores sframe's older 'Monthly Calendar + Photo Fade' layout)

+ Todoist: fetch tasks and render in 2 columns (10 items each, total 20)
+ Verse: /set -> verse input that shows on board
+ Bot duplication guard (file lock) to avoid double polling
+ Bus: nationwide arrival info via TAGO API with Telegram configuration
"""

# Code below is organized with clearly marked sections.
# Search for lines like `# === [SECTION: ...] ===` to navigate.

# === [SECTION: Imports / Standard & Third-party] ==============================
import os, json, time, secrets, threading, collections, re, socket, fcntl, xml.etree.ElementTree as ET, subprocess, sys
from pathlib import Path
from datetime import datetime, timezone, timedelta, date
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote

import requests
import html
import logging
from flask import Flask, request, jsonify, render_template_string, abort, send_from_directory, redirect, url_for, make_response
from werkzeug.middleware.proxy_fix import ProxyFix
import telebot

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bus")
# ======= Embedded-block helpers (final) ======================================
import tempfile, os, json
CFG_START = "# ==== EMBEDDED_CONFIG (JSON) START"
CFG_END   = "# ==== EMBEDDED_CONFIG (JSON) END"
VER_START = "# ==== EMBEDDED_VERSES START"
VER_END   = "# ==== EMBEDDED_VERSES END"

def _extract_block(src_text: str, start_tag: str, end_tag: str):
    s = src_text.find(start_tag); e = src_text.find(end_tag)
    if s == -1 or e == -1 or e <= s:
        raise RuntimeError(f"Marker not found: {start_tag}..{end_tag}")
    s_body = src_text.find("\n", s) + 1
    e_body = e
    return s_body, e_body, src_text[s_body:e_body]

def _replace_block_in_text(src_text: str, start_tag: str, end_tag: str, new_body: str) -> str:
    s_body, e_body, _old = _extract_block(src_text, start_tag, end_tag)
    if not new_body.endswith("\n"): new_body += "\n"
    return src_text[:s_body] + new_body + src_text[e_body:]

def _atomic_write(path: str, data: str):
    d = os.path.dirname(os.path.abspath(path)) or "."
    with tempfile.NamedTemporaryFile("w", delete=False, dir=d, encoding="utf-8") as tmp:
        tmp.write(data); tmp.flush(); os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)

def _read_block(start_tag: str, end_tag: str, file_path: str = __file__) -> str:
    with open(file_path, "r", encoding="utf-8") as f:
        src = f.read()
    _, _, body = _extract_block(src, start_tag, end_tag)
    # If wrapped as VAR = r"""...""", return only the inner text
    import re as _re
    m = _re.search(r'r?"""\s*([\s\S]*?)\s*"""', body)
    return (m.group(1) if m else body)

def _write_block(new_text: str, start_tag: str, end_tag: str, file_path: str = __file__):
    with open(file_path, "r", encoding="utf-8") as f:
        src = f.read()
    varname = "EMBEDDED_CONFIG" if "CONFIG" in start_tag else "EMBEDDED_VERSES"
    wrapped = varname + ' = r"""' + new_text + '"""' 
    _atomic_write(file_path, _replace_block_in_text(src, start_tag, end_tag, wrapped))

def load_config_from_embedded(defaults: dict):
    data = json.loads(_read_block(CFG_START, CFG_END) or "{}")
    def deep_fill(dst, src):
        for k, v in src.items():
            if k not in dst:
                dst[k] = v
            elif isinstance(v, dict):
                dst[k] = deep_fill(dst.get(k, {}) or {}, v)
        return dst
    return deep_fill(data, defaults)

def save_config_to_source(new_data: dict, file_path: str = __file__):
    json_text = json.dumps(new_data, ensure_ascii=False, indent=2)
    _write_block(json_text, CFG_START, CFG_END, file_path=file_path)

def get_verse() -> str:
    return _read_block(VER_START, VER_END).strip()

def set_verse(text: str):
    _write_block((text or "").strip(), VER_START, VER_END)
# ===========================================================================

# === [SECTION: Optional Google libraries (lazy check)] =======================
# - 구글 라이브러리가 없을 수 있으므로 임포트 시도 후 플래그만 세팅
try:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google_auth_oauthlib.flow import Flow
    GOOGLE_OK = True
except Exception:
    GOOGLE_OK = False

# === [SECTION: Paths / Base config file locations] ===========================
BASE = Path("/root/scal")
STATE_PATH = BASE / "sframe_state.json"
PHOTOS_DIR = BASE / "frame_photos"
GCLIENT_PATH = BASE / "google_client_secret.json"
GTOKEN_PATH = BASE / "google_token.json"
BASE.mkdir(parents=True, exist_ok=True)
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

# === [SECTION: Default configuration structure] ==============================
DEFAULT_CFG = {
    "server": {"port": 5320},
    "frame": {"tz": "Asia/Seoul", "ical_url": ""},
    "weather": {
        "provider": "openweathermap",
        "api_key": "",
        "location": "Seoul, South Korea",
        "units": "metric"
    },
    "telegram": {
        "bot_token": "",
        "allowed_user_ids": [],            # e.g. [5517670242]
        "mode": "polling",                 # polling | webhook
        "webhook_base": "",
        "path_secret": ""
    },
    "google": {
        "scopes": ["https://www.googleapis.com/auth/calendar.events"],
        "calendar": {"id": "primary"}

},
    "home_assistant": {
        "base_url": "http://localhost:8123",
        "token": "",
        "verify_ssl": True,
        "timeout": 5,
        "include_domains": ["light", "switch"],
        "include_entities": [],
        "dashboard": {
            "url_path": "",
            "title": ""
        }
    },
    # Todoist (config에서 설정) — 여기 값은 기본값
    "todoist": {
        "api_token": "",                   # 설정에 넣은 토큰 사용; 비어있으면 비활성
        "filter": "today | overdue",       # Todoist filter query
        "project_id": "",                  # optional: limit to project
        "max_items": 20                    # UI는 좌10/우10
    },
    # Bus 설정
    "bus": {
        "city_code": "",
        "node_id": "",
        "key": "",
    }
}
CFG = load_config_from_embedded(DEFAULT_CFG)

# === [SECTION: Timezone utilities] ===========================================
TZ = timezone(timedelta(hours=9)) if CFG["frame"]["tz"] == "Asia/Seoul" else timezone.utc
TZ_NAME = "Asia/Seoul" if CFG["frame"]["tz"] == "Asia/Seoul" else "UTC"

# === [SECTION: iCal loader (with basic fallback parser)] =====================
_ical_cache = {"url": None, "ts": 0.0, "events": []}

def _fmt_ics_date(v: str) -> str:
    if not v:
        return ""
    v = v.strip()
    if len(v) >= 8 and v[:8].isdigit():
        return f"{v[0:4]}-{v[4:6]}-{v[6:8]}"
    return v

def _parse_ics_basic(text: str):
    """Very basic ICS event parser without external libs."""
    evs, cur = [], {}
    for raw in text.splitlines():
        line = raw.strip()
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line.startswith("SUMMARY:"):
            cur["title"] = line[8:].strip()
        elif line.startswith("DTSTART"):
            cur["start"] = _fmt_ics_date(line.split(":", 1)[1])
        elif line.startswith("DTEND"):
            cur["end"] = _fmt_ics_date(line.split(":", 1)[1])
        elif line == "END:VEVENT":
            if "start" in cur:
                cur.setdefault("end", cur["start"])
                cur.setdefault("title", "(untitled)")
                evs.append(cur)
    return evs

def fetch_ical(url: str):
    """Fetch ICS; use python-ics if available else fallback parser."""
    global _ical_cache
    now = time.time()
    if not url:
        return []
    if _ical_cache["url"] == url and now - _ical_cache["ts"] < 300:
        return _ical_cache["events"]
    r = requests.get(url, timeout=10); r.raise_for_status()
    text = r.text
    try:
        from ics import Calendar
        cal = Calendar(text)
        evs = []
        for ev in cal.events:
            start = ev.begin.date().isoformat() if getattr(ev, "begin", None) else ""
            end = ev.end.date().isoformat() if getattr(ev, "end", None) else start
            title = (ev.name or "").strip() or "(untitled)"
            evs.append({"title": title, "start": start, "end": end})
    except Exception:
        evs = _parse_ics_basic(text)
    evs.sort(key=lambda x: (x.get("start", ""), x.get("title", "")))
    _ical_cache = {"url": url, "ts": now, "events": evs}
    return evs

def month_filter(items, y, m):
    mm = f"{y:04d}-{m:02d}"
    return [e for e in items if (e.get("start", "").startswith(mm) or e.get("end", "").startswith(mm))]

# === [SECTION: Weather (OpenWeatherMap API)] =================================
_weather_cache = {"key": "", "loc": "", "ts": 0.0, "data": None}
_air_cache = {"key": "", "loc": "", "ts": 0.0, "data": None}

def _owm_geocode(q, key):
    url = "https://api.openweathermap.org/geo/1.0/direct"
    r = requests.get(url, params={"q": q, "limit": 1, "appid": key}, timeout=10)
    r.raise_for_status()
    arr = r.json()
    if not arr:
        raise RuntimeError("Location not found")
    return float(arr[0]["lat"]), float(arr[0]["lon"])

def _owm_fetch_onecall(lat, lon, key, units):
    r = requests.get(
        "https://api.openweathermap.org/data/3.0/onecall",
        params={"lat": lat, "lon": lon, "appid": key, "units": units, "exclude": "minutely,hourly,alerts"},
        timeout=10,
    )
    r.raise_for_status()
    js = r.json()
    def icon_url(code): return f"https://openweathermap.org/img/wn/{code}@2x.png"
    cur = js.get("current", {})
    dailies = (js.get("daily") or [])[:5]
    def _safe_round(value):
        return round(value) if isinstance(value, (int, float)) else None

    cur_data = {
        "temp": _safe_round(cur.get("temp", 0)),
        "icon": icon_url(cur.get("weather", [{}])[0].get("icon", "01d")),
        "feels_like": _safe_round(cur.get("feels_like")),
        "humidity": _safe_round(cur.get("humidity")),
        "dew_point": _safe_round(cur.get("dew_point")),
    }
    days = []
    for d in dailies:
        dt = datetime.fromtimestamp(int(d.get("dt", 0)), tz=timezone.utc).astimezone(TZ).date()
        t = d.get("temp", {})
        icon = (d.get("weather", [{}])[0] or {}).get("icon", "01d")
        days.append({"date": dt.isoformat(), "min": round(t.get("min", 0)), "max": round(t.get("max", 0)), "icon": icon_url(icon)})
    return {"current": cur_data, "days": days}

def _owm_fetch_fiveday(lat, lon, key, units):
    cur = requests.get("https://api.openweathermap.org/data/2.5/weather",
                       params={"lat": lat, "lon": lon, "appid": key, "units": units}, timeout=10).json()
    fc = requests.get("https://api.openweathermap.org/data/2.5/forecast",
                      params={"lat": lat, "lon": lon, "appid": key, "units": units}, timeout=10).json()
    def icon_url(code): return f"https://openweathermap.org/img/wn/{code}@2x.png"
    def _safe_round(value):
        return round(value) if isinstance(value, (int, float)) else None

    main = cur.get("main", {})
    cur_data = {
        "temp": _safe_round(main.get("temp", 0)),
        "icon": icon_url(cur.get("weather", [{}])[0].get("icon", "01d")),
        "feels_like": _safe_round(main.get("feels_like")),
        "humidity": _safe_round(main.get("humidity")),
        "dew_point": _safe_round(main.get("dew_point")),
    }
    by_day = collections.defaultdict(list)
    for it in fc.get("list", []):
        ts = int(it.get("dt", 0))
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ).date()
        by_day[dt].append(it)
    days = []
    for d in sorted(by_day.keys())[:5]:
        arr = by_day[d]
        tmins, tmaxs, icons = [], [], []
        for it in arr:
            m = it.get("main", {})
            tmins.append(m.get("temp_min"))
            tmaxs.append(m.get("temp_max"))
            icons.append(it.get("weather", [{}])[0].get("icon", "01d"))
        pick = None
        for it in arr:
            hour = datetime.fromtimestamp(int(it["dt"]), tz=timezone.utc).astimezone(TZ).hour
            if 9 <= hour <= 15:
                pick = it.get("weather", [{}])[0].get("icon", "01d"); break
        if not pick:
            pick = max(set(icons), key=icons.count)
        days.append({"date": d.isoformat(), "min": round(min(tmins)), "max": round(max(tmaxs)), "icon": icon_url(pick)})
    return {"current": cur_data, "days": days}

def fetch_weather():
    cfgw = CFG.get("weather", {})
    key = cfgw.get("api_key", "").strip()
    loc = cfgw.get("location", "").strip()
    units = cfgw.get("units", "metric")
    if not key or not loc:
        return None
    now = time.time()
    cache_ok = (_weather_cache["data"] is not None and
                _weather_cache["key"] == key and
                _weather_cache["loc"] == loc and
                now - _weather_cache["ts"] < 600)
    if cache_ok:
        return _weather_cache["data"]
    lat, lon = _owm_geocode(loc, key)
    try:
        data = _owm_fetch_onecall(lat, lon, key, units)
    except Exception:
        data = _owm_fetch_fiveday(lat, lon, key, units)
    _weather_cache.update({"key": key, "loc": loc, "ts": now, "data": data})
    return data

# --- Air quality ------------------------------------------------------------
def fetch_air_quality():
    cfgw = CFG.get("weather", {})
    key = cfgw.get("api_key", "").strip()
    loc = cfgw.get("location", "").strip()
    if not key or not loc:
        return None
    now = time.time()
    cache_ok = (
        _air_cache["data"] is not None
        and _air_cache["key"] == key
        and _air_cache["loc"] == loc
        and now - _air_cache["ts"] < 600
    )
    if cache_ok:
        return _air_cache["data"]
    lat, lon = _owm_geocode(loc, key)
    url = "https://api.openweathermap.org/data/2.5/air_pollution"
    r = requests.get(url, params={"lat": lat, "lon": lon, "appid": key}, timeout=10)
    r.raise_for_status()
    js = r.json()
    first = (js.get("list") or [{}])[0]
    aqi = (first.get("main") or {}).get("aqi")
    comps = first.get("components") or {}
    labels = {1: "Good", 2: "Fair", 3: "Moderate", 4: "Poor", 5: "Very Poor"}
    colors = {1: "#009966", 2: "#ffde33", 3: "#ff9933", 4: "#cc0033", 5: "#660099"}
    data = {"aqi": aqi, "label": labels.get(aqi, "?"), "color": colors.get(aqi, "#fff")}
    for k in ("pm2_5", "pm10", "no2", "o3", "so2", "co", "nh3"):
        v = comps.get(k)
        if v is not None:
            data[k] = v
    _air_cache.update({"key": key, "loc": loc, "ts": now, "data": data})
    return data

# === [SECTION: Bus utilities] ================================================
def _pick_text(elem: Optional[ET.Element], *names: str) -> str:
    """주어진 태그 목록 중 첫 번째로 존재하는 텍스트를 반환."""
    if elem is None:
        return ""
    for n in names:
        child = elem.find(n)
        if child is not None and child.text and child.text.strip():
            return html.unescape(child.text.strip())
    return ""


def _extract_eta_minutes(msg: str) -> int:
    """도착 안내 문자열에서 분 단위 ETA 추출."""
    if not msg:
        return 99999
    if "곧" in msg:
        return 0
    m = re.search(r"(\d+)\s*분", msg)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    s = re.search(r"(\d+)\s*초", msg)
    if s:
        try:
            sec = int(s.group(1))
            return 0 if sec <= 60 else max(1, sec // 60)
        except Exception:
            pass
    only_num = re.search(r"^\s*(\d+)\s*$", msg)
    if only_num:
        try:
            return int(only_num.group(1))
        except Exception:
            pass
    return 99999


def _eta_display(minutes: int) -> str:
    return "곧 도착" if minutes == 0 else f"{minutes}분"


def get_bus_arrivals(
    city_code: str,
    node_id: str,
    service_key_encoded: str,
    *,
    dedup_by_route: bool = True,
    limit: int = 5,
    timeout: int = 7,
) -> Dict[str, Any]:
    """TAGO 버스도착정보 호출."""
    if not (city_code and node_id and service_key_encoded):
        return {"stop_name": "", "items": [], "need_config": True}

    url = (
        "http://apis.data.go.kr/1613000/BusArrivalService/getBusArrivalList"
        f"?serviceKey={quote(service_key_encoded)}&cityCode={quote(str(city_code))}&nodeId={quote(str(node_id))}"
    )

    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(r.text)

    stop_name = ""
    records: List[Tuple[int, Dict[str, Any]]] = []

    for it in root.iter("item"):
        if not stop_name:
            stop_name = _pick_text(it, "nodenm", "nodeNm")

        route = _pick_text(it, "routeno", "routeNo")
        if not route:
            continue

        arr_sec = _pick_text(it, "arrtime")
        arr_min = _pick_text(it, "predictTime1")
        raw_msg = ""

        minutes = 99999
        if arr_sec:
            try:
                sec = int(str(arr_sec).strip())
                minutes = 0 if sec <= 60 else max(1, sec // 60)
                raw_msg = "곧 도착" if minutes == 0 else f"{minutes}분"
            except Exception:
                pass
        elif arr_min:
            try:
                minutes = int(str(arr_min).strip())
                raw_msg = f"{minutes}분"
            except Exception:
                pass
        else:
            raw_msg = _pick_text(it, "arrmsg1", "arrmsg") or ""
            minutes = _extract_eta_minutes(raw_msg)

        hops = _pick_text(it, "arrprevstationcnt", "arrprevStationCnt")
        if hops.isdigit():
            hops = f"{hops}정거장"
        if not hops or hops == "0정거장":
            hops = "1정거장"

        display = _eta_display(minutes)

        rec = {
            "route": route,
            "eta_min": minutes,
            "eta_text": display,
            "hops": hops,
            "raw_msg": raw_msg or display,
        }
        records.append((minutes, rec))

    records = [r for r in records if r[1]["route"] and r[1]["eta_min"] < 99999]
    records.sort(key=lambda x: x[0])

    items: List[Dict[str, Any]] = []
    seen = set()
    for _, rec in records:
        if dedup_by_route:
            if rec["route"] in seen:
                continue
            seen.add(rec["route"])
        items.append(rec)
        if len(items) >= limit:
            break

    return {"stop_name": stop_name, "items": items}


def render_bus_box():
    cfg = CFG.get("bus", {}) or {}
    city = (cfg.get("city_code") or "").strip()
    node = (cfg.get("node_id") or "").strip()
    key = (cfg.get("key") or "").strip()
    data = get_bus_arrivals(city, node, key, dedup_by_route=True, limit=5, timeout=7)

    if data.get("need_config"):
        return {
            "title": "버스도착",
            "stop": "설정 필요",
            "rows": [{"text": "도시/정류장/키를 설정해주세요"}],
        }

    rows = []
    for it in data.get("items", []):
        rows.append(
            {
                "route": it["route"],
                "eta": it["eta_text"],
                "hops": it["hops"],
                "text": f'{it["route"]} · {it["eta_text"]} · {it["hops"]}',
            }
        )

    stop_name = data.get("stop_name", "")
    title = "버스도착"
    if stop_name:
        title += f" · {stop_name}"

    return {"title": title, "stop": stop_name, "rows": rows}


# === [SECTION: Home Assistant 연동 헬퍼] =====================================

HA_DOMAIN_ICONS = {
    "light": "💡",
    "switch": "🔌",
    "fan": "🌀",
    "media_player": "🔊",
    "climate": "🌡️",
    "humidifier": "💧",
    "cover": "🪟",
    "lock": "🔐",
    "vacuum": "🤖",
    "scene": "🎨",
    "script": "⚙️",
    "automation": "⚡",
    "input_boolean": "🔘",
}

HA_SERVICE_MAP: Dict[str, Tuple[Optional[str], Optional[str]]] = {
    "light": ("turn_on", "turn_off"),
    "switch": ("turn_on", "turn_off"),
    "fan": ("turn_on", "turn_off"),
    "media_player": ("turn_on", "turn_off"),
    "climate": ("turn_on", "turn_off"),
    "humidifier": ("turn_on", "turn_off"),
    "input_boolean": ("turn_on", "turn_off"),
    "automation": ("turn_on", "turn_off"),
    "vacuum": ("start", "return_to_base"),
    "cover": ("open_cover", "close_cover"),
    "lock": ("unlock", "lock"),
    "scene": ("turn_on", None),
    "script": ("turn_on", None),
}


HA_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$", re.IGNORECASE)


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "on", "yes", "y"}:
            return True
        if lowered in {"0", "false", "off", "no", "n"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    raise ValueError("불리언으로 변환할 수 없는 값입니다.")


class HomeAssistantError(RuntimeError):
    """Base exception for Home Assistant helper errors."""


class HomeAssistantConfigError(HomeAssistantError):
    """Raised when configuration is incomplete."""


class HomeAssistantAPIError(HomeAssistantError):
    """Raised when the Home Assistant API responds with an error."""


def _home_assistant_cfg() -> Dict[str, Any]:
    return CFG.get("home_assistant", {}) or {}


def _normalize_base_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    return url.rstrip("/")


def _mask_secret(value: str, *, head: int = 4, tail: int = 4) -> str:
    value = (value or "").strip()
    if not value:
        return "설정안됨"
    if len(value) <= 2:
        return value[0] + "*" * (len(value) - 1) if len(value) == 2 else "*"
    if len(value) <= head + tail:
        return value[0] + "*" * (len(value) - 2) + value[-1]
    return value[:head] + "*" * (len(value) - head - tail) + value[-tail:]


def _home_assistant_session() -> Tuple[requests.Session, str, float, Dict[str, Any]]:
    cfg = _home_assistant_cfg()
    base_url = _normalize_base_url(cfg.get("base_url", ""))
    token = (cfg.get("token") or "").strip()
    if not base_url:
        raise HomeAssistantConfigError("home_assistant.base_url 설정이 필요합니다.")
    if not token:
        raise HomeAssistantConfigError("home_assistant.token 설정이 필요합니다.")

    verify_raw = cfg.get("verify_ssl", True)
    if isinstance(verify_raw, str):
        try:
            verify = _coerce_bool(verify_raw)
        except ValueError:
            verify = True
    else:
        verify = bool(verify_raw) if isinstance(verify_raw, bool) else True

    timeout_raw = cfg.get("timeout", 5)
    try:
        timeout = max(1.0, float(timeout_raw))
    except (TypeError, ValueError):
        timeout = 5.0

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    session.verify = verify
    return session, base_url, timeout, cfg


def _ha_request(session: requests.Session, method: str, url: str, *, timeout: float, json_payload: Optional[Dict[str, Any]] = None) -> Any:
    try:
        resp = session.request(method, url, json=json_payload, timeout=timeout)
    except Exception as e:
        raise HomeAssistantAPIError(f"Home Assistant 요청 실패: {e}")

    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("error") or detail
        else:
            message = detail
        raise HomeAssistantAPIError(f"HTTP {resp.status_code}: {message}")

    if resp.content:
        try:
            return resp.json()
        except Exception:
            raise HomeAssistantAPIError("응답 JSON 파싱 실패")
    return None


def _ha_fetch_states(session: requests.Session, base_url: str, timeout: float) -> List[Dict[str, Any]]:
    data = _ha_request(session, "GET", f"{base_url}/api/states", timeout=timeout)
    if not isinstance(data, list):
        raise HomeAssistantAPIError("/api/states 응답 형식이 올바르지 않습니다.")
    return data


def _ha_fetch_areas(session: requests.Session, base_url: str, timeout: float) -> Dict[str, str]:
    try:
        data = _ha_request(session, "GET", f"{base_url}/api/areas", timeout=timeout)
    except HomeAssistantAPIError:
        return {}
    if not isinstance(data, list):
        return {}
    areas = {}
    for item in data:
        if isinstance(item, dict):
            area_id = item.get("area_id")
            name = item.get("name") or item.get("id")
            if area_id and isinstance(name, str):
                areas[area_id] = name
    return areas


def _ha_fetch_device_area(session: requests.Session, base_url: str, timeout: float) -> Dict[str, str]:
    try:
        data = _ha_request(session, "GET", f"{base_url}/api/devices", timeout=timeout)
    except HomeAssistantAPIError:
        return {}
    if not isinstance(data, list):
        return {}
    device_area: Dict[str, str] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        device_id = item.get("id")
        area_id = item.get("area_id")
        if isinstance(device_id, str) and isinstance(area_id, str) and area_id:
            device_area[device_id] = area_id
    return device_area


def _ha_collect_entities(node: Any, acc: Set[str]) -> None:
    if isinstance(node, dict):
        for value in node.values():
            _ha_collect_entities(value, acc)
    elif isinstance(node, (list, tuple, set)):
        for item in node:
            _ha_collect_entities(item, acc)
    elif isinstance(node, str):
        candidate = node.strip()
        if candidate and HA_ENTITY_ID_RE.match(candidate):
            acc.add(candidate)


def _ha_should_include(entity_id: str, cfg: Dict[str, Any]) -> bool:
    include_domains = cfg.get("include_domains") or []
    include_entities = cfg.get("include_entities") or []
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""

    include_entities_set = {e for e in include_entities if isinstance(e, str)}
    include_domains_set = {d for d in include_domains if isinstance(d, str)}

    if include_entities_set and entity_id in include_entities_set:
        return True
    if include_domains_set and domain in include_domains_set:
        return True
    if include_entities_set or include_domains_set:
        return False
    # 기본값: 토글 가능한 대표 도메인만 노출
    return domain in {
        "light",
        "switch",
        "fan",
        "media_player",
        "humidifier",
        "input_boolean",
        "automation",
        "cover",
        "lock",
        "vacuum",
        "scene",
        "script",
    }


def _ha_pick_icon(domain: str) -> str:
    return HA_DOMAIN_ICONS.get(domain, "🔘")


def _ha_interpret_on(domain: str, state_value: str) -> Optional[bool]:
    if not state_value:
        return None
    s = state_value.lower()
    if s in {"unavailable", "unknown"}:
        return None
    if domain == "cover":
        if s in {"opening"}:
            return True
        if s in {"closing"}:
            return False
        return s in {"open"}
    if domain == "lock":
        if s in {"locking", "unlocking"}:
            return None
        return s not in {"locked"}
    if domain == "media_player":
        if s in {"playing", "on"}:
            return True
        if s in {"off", "standby"}:
            return False
        if s in {"paused"}:
            return None
    if domain == "vacuum":
        if s in {"docked"}:
            return False
        if s in {"cleaning", "returning"}:
            return True
    if domain in {"scene", "script"}:
        return None
    return s not in {"off", "closed", "closing", "idle", "standby", "paused", "locked"}


def _ha_state_label(domain: str, state_value: str, raw_display: str, online: bool, is_on: Optional[bool]) -> str:
    if not online:
        return "오프라인"
    if domain == "cover":
        if state_value in {"opening"}:
            return "열리는 중…"
        if state_value in {"closing"}:
            return "닫히는 중…"
        if is_on is True:
            return "열림"
        if is_on is False:
            return "닫힘"
    if domain == "lock":
        if state_value == "locking":
            return "잠그는 중…"
        if state_value == "unlocking":
            return "잠금 해제 중…"
        return "잠금 해제" if is_on else "잠김"
    if domain == "media_player":
        if state_value == "playing":
            return "재생 중"
        if state_value == "paused":
            return "일시정지"
        if is_on is False:
            return "꺼짐"
    if domain == "vacuum":
        if state_value == "cleaning":
            return "청소 중"
        if state_value == "returning":
            return "복귀 중"
        if state_value == "docked":
            return "대기"
    if domain in {"scene", "script"}:
        return raw_display or ""
    if is_on is True:
        return "켜짐"
    if is_on is False:
        return "꺼짐"
    return raw_display or "상태 확인 불가"


def _format_ha_device(state: Dict[str, Any], area_lookup: Dict[str, str], device_area: Dict[str, str]) -> Optional[Dict[str, Any]]:
    entity_id = state.get("entity_id")
    if not isinstance(entity_id, str):
        return None
    cfg = _home_assistant_cfg()
    if not _ha_should_include(entity_id, cfg):
        return None

    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    attributes = state.get("attributes") or {}
    if not isinstance(attributes, dict):
        attributes = {}

    friendly_name = attributes.get("friendly_name") or entity_id
    area_id = attributes.get("area_id")
    if isinstance(area_id, str) and area_id:
        room = area_lookup.get(area_id, "")
    else:
        device_id = attributes.get("device_id")
        room = area_lookup.get(device_area.get(device_id, ""), "") if isinstance(device_id, str) else ""

    raw_state = state.get("state") or ""
    if not isinstance(raw_state, str):
        raw_state = str(raw_state)
    raw_display = raw_state.strip()
    normalized = raw_display.lower()
    online = normalized not in {"unavailable", "unknown"}
    is_on = _ha_interpret_on(domain, normalized)

    services = HA_SERVICE_MAP.get(domain, (None, None))
    can_toggle = bool(services[0] and services[1])

    icon = _ha_pick_icon(domain)
    label = _ha_state_label(domain, normalized, raw_display, online, is_on)

    return {
        "id": entity_id,
        "name": friendly_name,
        "room": room,
        "type": domain,
        "icon": icon,
        "online": online,
        "can_toggle": can_toggle,
        "traits": [],
        "state": {"on": is_on if isinstance(is_on, bool) else None},
        "state_label": label,
    }


def home_assistant_list_devices() -> List[Dict[str, Any]]:
    session, base_url, timeout, _cfg = _home_assistant_session()
    try:
        states = _ha_fetch_states(session, base_url, timeout)
        areas = _ha_fetch_areas(session, base_url, timeout)
        device_area = _ha_fetch_device_area(session, base_url, timeout)
        devices: List[Dict[str, Any]] = []
        for state in states:
            if not isinstance(state, dict):
                continue
            formatted = _format_ha_device(state, areas, device_area)
            if formatted:
                devices.append(formatted)
        devices.sort(key=lambda d: ((d.get("room") or ""), d.get("name") or d.get("id") or ""))
        return devices
    finally:
        try:
            session.close()
        except Exception:
            pass


def home_assistant_list_dashboards() -> List[Dict[str, Any]]:
    session, base_url, timeout, _cfg = _home_assistant_session()
    try:
        data = _ha_request(session, "GET", f"{base_url}/api/lovelace/dashboards", timeout=timeout)
        if not isinstance(data, list):
            raise HomeAssistantAPIError("/api/lovelace/dashboards 응답 형식이 올바르지 않습니다.")
        dashboards: List[Dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            url_path = (item.get("url_path") or item.get("id") or "").strip()
            title = (item.get("title") or url_path or item.get("id") or "").strip()
            dashboards.append(
                {
                    "id": item.get("id") or url_path or title,
                    "title": title or "(이름 없음)",
                    "url_path": url_path,
                    "mode": item.get("mode") or "",
                    "require_admin": bool(item.get("require_admin")),
                }
            )
        dashboards.sort(key=lambda d: (d.get("title") or "").lower())
        return dashboards
    finally:
        try:
            session.close()
        except Exception:
            pass


def home_assistant_fetch_dashboard_entities(url_path: str) -> Tuple[str, List[str]]:
    url_path = (url_path or "").strip()
    if not url_path:
        raise HomeAssistantAPIError("대시보드 식별자가 필요합니다.")

    session, base_url, timeout, _cfg = _home_assistant_session()
    try:
        safe_path = quote(url_path, safe="")
        data = _ha_request(
            session,
            "GET",
            f"{base_url}/api/lovelace/dashboards/{safe_path}",
            timeout=timeout,
        )
        if not isinstance(data, dict):
            raise HomeAssistantAPIError("대시보드 상세 응답 형식이 올바르지 않습니다.")

        title = (data.get("title") or data.get("id") or url_path).strip()
        views: List[Any] = []
        config = data.get("config")
        if isinstance(config, dict) and isinstance(config.get("views"), list):
            views = config.get("views") or []
        elif isinstance(data.get("views"), list):
            views = data.get("views") or []

        found: Set[str] = set()
        for view in views:
            _ha_collect_entities(view, found)

        if not found:
            raise HomeAssistantAPIError("선택한 대시보드에서 제어할 엔티티를 찾지 못했습니다.")

        return title, sorted(found)
    finally:
        try:
            session.close()
        except Exception:
            pass


def home_assistant_apply_dashboard(url_path: str) -> Dict[str, Any]:
    title, entities = home_assistant_fetch_dashboard_entities(url_path)
    cfg = CFG.setdefault("home_assistant", {})
    cfg["include_entities"] = entities
    dashboard_cfg = cfg.setdefault("dashboard", {})
    dashboard_cfg["url_path"] = url_path
    dashboard_cfg["title"] = title
    dashboard_cfg["entity_count"] = len(entities)
    save_config_to_source(CFG)
    return {"title": title, "count": len(entities), "entities": entities}


def home_assistant_execute(entity_id: str, turn_on: bool) -> Any:
    if not entity_id or "." not in entity_id:
        raise HomeAssistantAPIError("유효한 Home Assistant 엔티티 ID가 필요합니다.")

    session, base_url, timeout, _cfg = _home_assistant_session()
    try:
        domain = entity_id.split(".", 1)[0]
        on_service, off_service = HA_SERVICE_MAP.get(domain, (None, None))
        service = on_service if turn_on else off_service
        if not service:
            action = "켜기" if turn_on else "끄기"
            raise HomeAssistantAPIError(f"{domain} 엔티티는 '{action}' 명령을 지원하지 않습니다.")
        url = f"{base_url}/api/services/{domain}/{service}"
        payload = {"entity_id": entity_id}
        return _ha_request(session, "POST", url, timeout=timeout, json_payload=payload)
    finally:
        try:
            session.close()
        except Exception:
            pass


# === [SECTION: Standalone Bus Arrival Telegram Bot (PTB)] ====================
try:
    from telegram import (
        Update,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        ForceReply,
    )
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
except ModuleNotFoundError:  # pragma: no cover - 테스트 환경용 더미
    Update = InlineKeyboardButton = InlineKeyboardMarkup = ForceReply = object  # type: ignore
    Application = CallbackQueryHandler = CommandHandler = ContextTypes = MessageHandler = object  # type: ignore

    class filters:  # type: ignore
        TEXT = COMMAND = None

BB_BOT_TOKEN = CFG["telegram"]["bot_token"]
BB_ALLOWED_USER_IDS = set(CFG["telegram"].get("allowed_user_ids", []))
BB_TAGO_SERVICE_KEY = os.environ.get("TAGO_API_KEY", CFG.get("bus", {}).get("key", "")).strip()
BB_DEFAULT_CITY = CFG.get("bus", {}).get("city_code", "")
BB_DEFAULT_NODE = CFG.get("bus", {}).get("node_id", "")

BB_USER_STATE: Dict[int, Dict[str, Any]] = {}
BB_CITY_CACHE: List[Tuple[str, str]] = []
bb_log = logging.getLogger("busbot")


def bb_ensure_user_state(uid: int) -> Dict[str, Any]:
    if uid not in BB_USER_STATE:
        BB_USER_STATE[uid] = {
            "city_code": BB_DEFAULT_CITY,
            "node_id": BB_DEFAULT_NODE,
            "key": BB_TAGO_SERVICE_KEY,
            "awaiting": None,
            "stop_results": [],
        }
    return BB_USER_STATE[uid]


def bb_check_auth(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in BB_ALLOWED_USER_IDS)


def bb_extract_arg(text: str) -> str:
    parts = text.strip().split(maxsplit=2)
    if len(parts) == 2:
        return parts[1]
    if len(parts) >= 3:
        return parts[2]
    return ""


def bb_extract_arg2(text: str) -> List[str]:
    parts = text.strip().split()
    return parts[2:] if len(parts) >= 3 else []


def bb_is_tago_node_id(text: str) -> bool:
    """ICB164000104 형태의 TAGO nodeId인지 검사"""
    return bool(re.fullmatch(r"[A-Z]{3}\d{7,}", text.strip()))


def bb_paginate(items: List[Any], page: int, per_page: int = 10) -> List[Any]:
    start = page * per_page
    end = start + per_page
    return items[start:end]


def bb_tago_get_city_list(service_key: str) -> List[Tuple[str, str]]:
    """(cityName, cityCode) 리스트 반환"""
    if BB_CITY_CACHE:
        return BB_CITY_CACHE
    url = (
        "http://apis.data.go.kr/1613000/BusRouteInfoInqireService/getCtyCodeList"
        f"?serviceKey={quote(service_key)}"
    )
    try:
        r = requests.get(url, timeout=7)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    except Exception as e:
        log.warning("City list fetch failed: %s", e)
        return []
    for it in root.iter("item"):
        name = _pick_text(it, "cityname") or _pick_text(it, "cityName")
        code = _pick_text(it, "citycode") or _pick_text(it, "cityCode")
        if name and code:
            BB_CITY_CACHE.append((name, code))
    return BB_CITY_CACHE


def bb_tago_search_stops(city_code: str, keyword: str, service_key: str) -> List[Tuple[str, str, str]]:
    """(정류소명, 정류소번호, nodeId) 리스트 반환"""
    url = (
        "http://apis.data.go.kr/1613000/BusSttnInfoInqireService/getSttnList"
        f"?serviceKey={quote(service_key)}&cityCode={quote(str(city_code))}&nodeNm={quote(keyword)}"
    )
    r = requests.get(url, timeout=7)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    stops: List[Tuple[str, str, str]] = []
    for it in root.iter("item"):
        name = _pick_text(it, "nodenm") or _pick_text(it, "nodeNm")
        ars = _pick_text(it, "arsno") or _pick_text(it, "arsNo")
        node = _pick_text(it, "nodeid") or _pick_text(it, "nodeId")
        if name and node:
            stops.append((name, ars, node))
    return stops


def bb_build_city_keyboard(cities: List[Tuple[str, str]], page: int = 0) -> InlineKeyboardMarkup:
    per = 10
    items = bb_paginate(cities, page, per)
    buttons = [
        [InlineKeyboardButton(f"{name}", callback_data=f"city:{code}")]
        for name, code in items
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"citypage:{page-1}"))
    if (page + 1) * per < len(cities):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"citypage:{page+1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


def bb_build_stop_keyboard(stops: List[Tuple[str, str, str]], page: int = 0) -> InlineKeyboardMarkup:
    per = 10
    items = bb_paginate(stops, page, per)
    buttons = [
        [InlineKeyboardButton(f"{name} {ars}", callback_data=f"stop:{node}")]
        for name, ars, node in items
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"stoppage:{page-1}"))
    if (page + 1) * per < len(stops):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"stoppage:{page+1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


async def bb_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bb_check_auth(update):
        return
    st = bb_ensure_user_state(update.effective_user.id)
    await update.message.reply_text(
        "🚌 버스 도착정보 봇\n"
        "- /bus\n"
        "- /stop (정류소 변경)\n"
        "- /set key <서비스키>\n\n"
        f"현재설정: city={st['city_code']} / node={st['node_id']} / key={'등록됨' if st['key'] else '미등록'}"
    )


async def bb_cmd_bus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bb_check_auth(update):
        return
    st = bb_ensure_user_state(update.effective_user.id)
    city, node, key = st["city_code"], st["node_id"], st["key"]
    await update.message.reply_text(f"⏳ 조회 중… (city={city}, node={node})")
    data = get_bus_arrivals(city, node, key, dedup_by_route=False, limit=20, timeout=7)
    lines = []
    stop = data.get("stop_name")
    if stop:
        lines.append(stop)
    for it in data.get("items", []):
        lines.append(f"{it['route']} {it['eta_text']} {it['hops']}")
    await update.message.reply_text("\n".join(lines) if lines else "정보가 없습니다.")


async def bb_cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bb_check_auth(update):
        return
    st = bb_ensure_user_state(update.effective_user.id)
    text = (update.message.text or "").strip()
    if text.lower().startswith("/set key"):
        arg = bb_extract_arg(text)
        if not arg:
            await update.message.reply_text("사용법: /set key <서비스키>")
            return
        st["key"] = arg.strip()
        await update.message.reply_text("✔ 서비스키 등록 완료")
    else:
        await update.message.reply_text("사용법: /set key <서비스키>")


async def bb_cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bb_check_auth(update):
        return
    st = bb_ensure_user_state(update.effective_user.id)
    cities = bb_tago_get_city_list(st["key"])
    if not cities:
        st["awaiting"] = "keyword"
        await update.message.reply_text(
            "도시 목록을 가져오지 못했습니다. 서비스 키를 확인하거나 직접 도시 코드를 입력하세요."
        )
        return
    await update.message.reply_text(
        "도시를 선택하세요", reply_markup=bb_build_city_keyboard(cities, 0)
    )


async def bb_on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bb_check_auth(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    uid = update.effective_user.id
    st = bb_ensure_user_state(uid)

    if data.startswith("citypage:"):
        page = int(data.split(":", 1)[1])
        cities = bb_tago_get_city_list(st["key"])
        await query.edit_message_reply_markup(bb_build_city_keyboard(cities, page))
        return
    if data.startswith("city:"):
        code = data.split(":", 1)[1]
        st["city_code"] = code
        st["awaiting"] = "keyword"
        await query.message.reply_text(
            "정류소명을 입력하세요", reply_markup=ForceReply(selective=True)
        )
        return
    if data.startswith("stoppage:"):
        page = int(data.split(":", 1)[1])
        stops = st.get("stop_results", [])
        await query.edit_message_reply_markup(bb_build_stop_keyboard(stops, page))
        return
    if data.startswith("stop:"):
        node = data.split(":", 1)[1]
        st["node_id"] = node
        st["awaiting"] = None
        await query.message.reply_text(f"✔ 정류소 등록 완료: {node}")
        return


async def bb_on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bb_check_auth(update):
        return
    uid = update.effective_user.id
    st = bb_ensure_user_state(uid)
    if st.get("awaiting") == "keyword":
        kw = (update.message.text or "").strip()
        if not st.get("city_code"):
            if bb_is_tago_node_id(kw):
                st["node_id"] = kw
                st["awaiting"] = None
                await update.message.reply_text(f"✔ 정류소 등록 완료: {kw}")
                return
            if kw.isdigit():
                st["city_code"] = kw
                await update.message.reply_text(
                    "정류소명을 입력하세요", reply_markup=ForceReply(selective=True)
                )
                return
            await update.message.reply_text("도시 코드를 먼저 입력하세요.")
            return
        if bb_is_tago_node_id(kw):
            st["node_id"] = kw
            st["awaiting"] = None
            await update.message.reply_text(f"✔ 정류소 등록 완료: {kw}")
            return
        stops = bb_tago_search_stops(st["city_code"], kw, st["key"])
        st["stop_results"] = stops
        st["awaiting"] = "stop"
        await update.message.reply_text(
            "정류소를 선택하세요", reply_markup=bb_build_stop_keyboard(stops, 0)
        )


def run_bus_bot():
    app = Application.builder().token(BB_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", bb_start))
    app.add_handler(CommandHandler("bus", bb_cmd_bus))
    app.add_handler(CommandHandler("set", bb_cmd_set))
    app.add_handler(CommandHandler("stop", bb_cmd_stop))
    app.add_handler(CallbackQueryHandler(bb_on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bb_on_message))
    bb_log.info("Bus bot started.")
    app.run_polling()


# === [SECTION: Waydroid KakaoBus automation] ================================
KBUS_PKG = "com.kakao.bus"
KBUS_LAUNCH_TRY = 1
KBUS_OUT = Path("bus.json")
KBUS_INTERVAL = 10  # seconds
KBUS_KST = timezone(timedelta(hours=9))
KBUS_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
KBUS_ALLOWED_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}


def kbus_sh(*args: str, check: bool = True, text: bool = True):
    return subprocess.run(args, check=check, text=text, capture_output=True)


def kbus_adb(*args: str, check: bool = True):
    return kbus_sh("adb", *args, check=check)


def kbus_set_adb_ime() -> None:
    kbus_adb("shell", "ime", "enable", "com.android.adbkeyboard/.AdbIME")
    kbus_adb("shell", "ime", "set", "com.android.adbkeyboard/.AdbIME")


def kbus_launch_app() -> None:
    kbus_adb("shell", "input", "keyevent", "3")
    time.sleep(0.6)
    for _ in range(KBUS_LAUNCH_TRY):
        kbus_adb(
            "shell",
            "monkey",
            "-p",
            KBUS_PKG,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        )
        time.sleep(1.2)


def kbus_enter_search(query: str) -> None:
    kbus_adb("shell", "input", "keyevent", "84")
    time.sleep(0.8)
    kbus_adb(
        "shell",
        "am",
        "broadcast",
        "-a",
        "ADB_INPUT_TEXT",
        "--es",
        "msg",
        query,
    )
    time.sleep(0.4)
    kbus_adb("shell", "input", "keyevent", "66")
    time.sleep(1.2)


def kbus_apply_search(query: str) -> None:
    kbus_adb("wait-for-device")
    kbus_set_adb_ime()
    kbus_launch_app()
    kbus_enter_search(query)
    print(f"OK: '{query}' 검색어 적용 완료 (카카오버스 화면 고정 시도)")


def kbus_dump_xml(local: Path) -> Path:
    kbus_adb("shell", "uiautomator", "dump", "/sdcard/view.xml")
    kbus_adb("pull", "/sdcard/view.xml", str(local))
    return local


def kbus_texts_from(xml_path: Path) -> List[str]:
    root = ET.parse(xml_path).getroot()
    res: List[str] = []
    for node in root.iter():
        t = node.attrib.get("text")
        if t:
            t = " ".join(t.split())
            if t:
                res.append(t)
    return res


def kbus_parse_heuristic(texts: List[str]) -> dict:
    routes, arrivals, meta = [], [], []
    for t in texts:
        if ("번" in t and any(c.isdigit() for c in t)) or t.isdigit():
            routes.append(t)
        elif any(k in t for k in ("분", "초", "도착", "전", "곧")):
            arrivals.append(t)
        elif any(k in t for k in ("정류장", "남음", "방면", "행")):
            meta.append(t)
    return {
        "timestamp": datetime.now(KBUS_KST).isoformat(),
        "routes": routes[:20],
        "arrivals": arrivals[:20],
        "meta": meta[:30],
        "raw": texts[:200],
    }


def kbus_scrape_loop() -> None:
    out_dir = KBUS_OUT.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            xml = kbus_dump_xml(out_dir / "view.xml")
            texts = kbus_texts_from(xml)
            data = kbus_parse_heuristic(texts)
            KBUS_OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            print(f"[{data['timestamp']}] {len(texts)} nodes → {KBUS_OUT}")
        except Exception as e:
            print("error:", e)
        time.sleep(KBUS_INTERVAL)


def kbus_allowed(user_id: int) -> bool:
    return not KBUS_ALLOWED_IDS or user_id in KBUS_ALLOWED_IDS


async def kbus_deny(update: Update) -> None:
    await update.message.reply_text("허용된 사용자만 사용할 수 있습니다.")


async def kbus_start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not kbus_allowed(update.effective_user.id):
        return await kbus_deny(update)
    await update.message.reply_text("버스봇입니다. /bus <정류소명 또는 번호> 로 설정하세요.")


async def kbus_bus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not kbus_allowed(update.effective_user.id):
        return await kbus_deny(update)
    if not context.args:
        return await update.message.reply_text("사용법: /bus <정류소명 또는 번호>")
    query = " ".join(context.args)
    try:
        kbus_apply_search(query)
        await update.message.reply_text(f"검색 적용: {query}")
    except subprocess.CalledProcessError as e:
        await update.message.reply_text(f"실패: {e.output}")
    except Exception as e:
        await update.message.reply_text(f"오류: {e}")


async def kbus_echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not kbus_allowed(update.effective_user.id):
        return await kbus_deny(update)
    text = (update.message.text or "").strip()
    if not text:
        return
    try:
        kbus_apply_search(text)
        await update.message.reply_text(f"검색 적용: {text}")
    except subprocess.CalledProcessError as e:
        await update.message.reply_text(f"실패: {e.output}")
    except Exception as e:
        await update.message.reply_text(f"오류: {e}")


def kbus_run_bot() -> None:
    if not KBUS_BOT_TOKEN:
        print("TELEGRAM_BOT_TOKEN not set; bot disabled")
        return
    app = Application.builder().token(KBUS_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", kbus_start_cmd))
    app.add_handler(CommandHandler("bus", kbus_bus_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, kbus_echo))
    app.run_polling()


# Telebot helper keyboards for bus configuration
def tb_build_city_keyboard(cities: List[Tuple[str, str]], page: int = 0) -> telebot.types.InlineKeyboardMarkup:
    per = 10
    items = bb_paginate(cities, page, per)
    kb = telebot.types.InlineKeyboardMarkup()
    for name, code in items:
        kb.add(telebot.types.InlineKeyboardButton(f"{name}", callback_data=f"bus_city:{code}"))
    nav = []
    if page > 0:
        nav.append(telebot.types.InlineKeyboardButton("⬅️", callback_data=f"bus_citypage:{page-1}"))
    if (page + 1) * per < len(cities):
        nav.append(telebot.types.InlineKeyboardButton("➡️", callback_data=f"bus_citypage:{page+1}"))
    if nav:
        kb.row(*nav)
    return kb


def tb_build_stop_keyboard(
    stops: List[Tuple[str, str, str]], page: int = 0
) -> telebot.types.InlineKeyboardMarkup:
    per = 10
    items = bb_paginate(stops, page, per)
    kb = telebot.types.InlineKeyboardMarkup()
    for name, ars, node in items:
        label = f"{name} {ars}" if ars else name
        kb.add(telebot.types.InlineKeyboardButton(label, callback_data=f"bus_stop:{node}"))
    nav = []
    if page > 0:
        nav.append(telebot.types.InlineKeyboardButton("⬅️", callback_data=f"bus_stoppage:{page-1}"))
    if (page + 1) * per < len(stops):
        nav.append(telebot.types.InlineKeyboardButton("➡️", callback_data=f"bus_stoppage:{page+1}"))
    if nav:
        kb.row(*nav)
    return kb


# === [SECTION: Photo file listing for board background] ======================
def list_local_images():
    exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    files = []
    for p in sorted(PHOTOS_DIR.glob("**/*")):
        if p.is_file() and p.suffix.lower() in exts:
            files.append(str(p.relative_to(PHOTOS_DIR)))
    return files

# === [SECTION: Flask app / session / proxy headers] ==========================
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SFRAME_SESSION_SECRET", "CHANGE_ME_32CHARS")
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="None")

# === [SECTION: Verse helpers + API endpoints] ================================
def get_verse() -> str:
    # 위에서 선언한 공용 헬퍼 사용
    return _read_block(VER_START, VER_END).strip()

def set_verse(text: str):
    # 소스의 EMBEDDED_VERSES 블록을 즉시 갱신
    _write_block((text or "").strip(), VER_START, VER_END)
    # 선택: 텍스트 파일도 함께 갱신(원하셨던 verse txt 파일)
    try:
        (BASE / "verse.txt").write_text((text or "").strip() + "\n", encoding="utf-8")
    except Exception:
        pass

@app.get("/api/verse")
def api_verse():
    return jsonify({"text": get_verse()})

# === [SECTION: Todoist helpers + API endpoint] ===============================
def todoist_headers():
    tok = (CFG.get("todoist", {}) or {}).get("api_token", "").strip() or os.environ.get("SFRAME_TODOIST_TOKEN", "").strip()
    if not tok:
        # 토큰 없으면 need_config 표기
        raise RuntimeError("Todoist API token missing")
    return {"Authorization": f"Bearer {tok}"}

def todoist_list_tasks():
    """
    Fetch open tasks via REST v2.
    Respects filter/project_id; returns trimmed fields up to max_items (default 20).
    """
    base = "https://api.todoist.com/rest/v2/tasks"
    cfg = CFG.get("todoist", {}) or {}
    params = {}
    if cfg.get("project_id"):
        params["project_id"] = cfg["project_id"]
    if cfg.get("filter"):
        params["filter"] = cfg["filter"]
    # request
    r = requests.get(base, headers=todoist_headers(), params=params, timeout=10)
    r.raise_for_status()
    items = r.json()
    out = []
    max_items = int(cfg.get("max_items", 20))
    for t in items[:max_items]:
        out.append({
            "id": t.get("id"),
            "title": t.get("content"),
            "due": (t.get("due") or {}).get("date"),  # YYYY-MM-DD or RFC3339
            "priority": t.get("priority"),
            "project_id": t.get("project_id"),
            "url": t.get("url"),
        })
    return out

@app.get("/api/todo")
def api_todo():
    try:
        return jsonify(todoist_list_tasks())
    except Exception as e:
        return jsonify({"error": str(e), "need_config": True}), 200

# === [SECTION: Google OAuth helpers / Calendar service] ======================
def have_google_libs():
    return GOOGLE_OK

def load_google_creds():
    if not GTOKEN_PATH.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(GTOKEN_PATH), scopes=CFG["google"]["scopes"])
    except Exception:
        return None

def save_google_creds(creds: "Credentials"):
    GTOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

def get_google_service():
    if not have_google_libs():
        raise RuntimeError("Google libraries not installed. pip install google-auth google-auth-oauthlib google-api-python-client")
    creds = load_google_creds()
    if not creds:
        raise RuntimeError("No Google token. Visit /oauth/start to authorize.")
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request as GRequest
        creds.refresh(GRequest()); save_google_creds(creds)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

# === [SECTION: Google Calendar helpers (view/edit/delete)] ===================
def _cal_id():
    return CFG["google"]["calendar"].get("id", "primary")

def _fmt_start_end(ev):
    def pick(obj):
        if "dateTime" in obj:
            dt = datetime.fromisoformat(obj["dateTime"].replace("Z", "+00:00")).astimezone(TZ)
            return dt.strftime("%Y-%m-%d %H:%M")
        return obj.get("date", "")
    s = pick(ev["start"]); e = pick(ev["end"])
    return f"{s} ~ {e}"

def list_upcoming_events(max_results=10):
    svc = get_google_service()
    now_iso = datetime.now(timezone.utc).isoformat()
    res = svc.events().list(
        calendarId=_cal_id(),
        timeMin=now_iso,
        singleEvents=True,
        orderBy="startTime",
        maxResults=max_results
    ).execute()
    return res.get("items", [])

def send_event_picker(chat_id, action_prefix, max_results=10):
    """Send inline keyboard to pick an upcoming event."""
    try:
        items = list_upcoming_events(max_results=max_results)
    except Exception as e:
        TB.send_message(chat_id, f"Failed to fetch events: {e}")
        return
    if not items:
        TB.send_message(chat_id, "No upcoming events.")
        return
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    for ev in items:
        title = ev.get("summary") or "(untitled)"
        txt = f"{title}  {_fmt_start_end(ev)}"
        kb.add(telebot.types.InlineKeyboardButton(txt[:64], callback_data=f"pick_{action_prefix}:{ev['id']}"))
    TB.send_message(chat_id, "Select an event:", reply_markup=kb)

def load_event(ev_id):
    svc = get_google_service()
    return svc.events().get(calendarId=_cal_id(), eventId=ev_id).execute()

def patch_event(ev_id, **fields):
    svc = get_google_service()
    return svc.events().patch(calendarId=_cal_id(), eventId=ev_id, body=fields).execute()

def delete_event(ev_id):
    svc = get_google_service()
    svc.events().delete(calendarId=_cal_id(), eventId=ev_id).execute()

# === [SECTION: Natural-language date/time parsing (ASCII-safe)] ==============
def _rel_date_en(word):
    """Simple relative date helper."""
    today = datetime.now(TZ).date()
    w = word.lower()
    if "today" in w:
        return today
    if "tomorrow" in w:
        return today + timedelta(days=1)
    if "day after" in w:
        return today + timedelta(days=2)
    return None

def _parse_date_token(tok, default_year=None):
    """Parse date token formats: YYYY-MM-DD / YYYY.MM.DD / MM-DD / MM/DD / YYYYMMDD."""
    tok = tok.strip()
    d = _rel_date_en(tok)
    if d: return d
    m = re.search(r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})", tok)
    if m:
        y, mo, da = map(int, m.groups()); return date(y, mo, da)
    m = re.search(r"(\d{1,2})[./-](\d{1,2})", tok)  # MM/DD or MM-DD
    if m:
        mo, da = map(int, m.groups())
        y = default_year or datetime.now(TZ).year
        return date(y, mo, da)
    m = re.search(r"(\d{8})", tok)  # YYYYMMDD
    if m:
        s = m.group(1); return date(int(s[0:4]), int(s[4:6]), int(s[6:8]))
    return None

def _parse_time_one(tok):
    """Parse time like '14', '14:30', '2pm', '2:15 pm'."""
    s = tok.strip().lower().replace(" ", "")
    ampm = None
    if s.endswith("am"):
        ampm = "am"; s = s[:-2]
    elif s.endswith("pm"):
        ampm = "pm"; s = s[:-2]
    m = re.match(r"^(\d{1,2})(?::?(\d{2}))?$", s)
    if not m: return None
    h = int(m.group(1)); mnt = int(m.group(2) or 0)
    if ampm == "pm" and 1 <= h < 12: h += 12
    if ampm == "am" and h == 12: h = 0
    if not (0 <= h <= 23 and 0 <= mnt <= 59): return None
    return h, mnt

def parse_when_range(text):
    """
    Parse a human text like:
      '2025-09-02 14:00~16:00'
      '8/30 9~11'
      'today 15:00~16:00'
      '9/1~9/3 (all-day)'
    """
    txt = text.strip()
    y_default = datetime.now(TZ).year

    d_candidates = re.findall(r"(\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}|today|tomorrow|day after|\d{8})", txt, flags=re.IGNORECASE)
    dates = []
    for t in d_candidates:
        dd = _parse_date_token(t, default_year=y_default)
        if dd: dates.append(dd)
    dates = dates[:2]

    # time tokens around ~ or - ranges
    t_left = t_right = None
    if re.search(r"[~\-]", txt):
        lr = re.split(r"[~\-]", txt, maxsplit=1)
        tl = re.findall(r"(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", lr[0], flags=re.IGNORECASE)
        tr = re.findall(r"(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", lr[1], flags=re.IGNORECASE)
        if tl: t_left  = _parse_time_one(tl[-1])
        if tr: t_right = _parse_time_one(tr[0])
    if not t_left:
        m = re.findall(r"(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", txt, flags=re.IGNORECASE)
        if m:
            t_left = _parse_time_one(m[0])
            if len(m) >= 2: t_right = _parse_time_one(m[1])

    if dates:
        start_d = dates[0]
        end_d = dates[1] if len(dates) >= 2 else dates[0]
    else:
        d = _rel_date_en(txt) or datetime.now(TZ).date()
        start_d = end_d = d

    if t_left:
        sh, sm = t_left
        if t_right:
            eh, em = t_right
        else:
            dt2 = (datetime.combine(start_d, datetime.min.time()).replace(tzinfo=TZ) +
                   timedelta(hours=sh, minutes=sm) + timedelta(hours=1))
            eh, em = dt2.hour, dt2.minute
        start_dt = datetime(start_d.year, start_d.month, start_d.day, sh, sm, tzinfo=TZ)
        end_dt   = datetime(end_d.year, end_d.month, end_d.day, eh, em, tzinfo=TZ)
        if end_dt <= start_dt: end_dt += timedelta(days=1)
        return {"kind": "timed", "start_dt": start_dt, "end_dt": end_dt}
    else:
        return {"kind": "all_day", "start_date": start_d, "end_date": end_d}

# === [SECTION: REST API endpoints used by the board HTML] ====================
@app.get("/api/events")
def api_events():
    url = CFG["frame"]["ical_url"]
    if not url:
        return jsonify([])
    try:
        y = int(request.args.get("year")) if request.args.get("year") else None
        m = int(request.args.get("month")) if request.args.get("month") else None
    except Exception:
        y = m = None
    now_kst = datetime.now(TZ)
    y = y or now_kst.year
    m = m or now_kst.month
    items = month_filter(fetch_ical(url), y, m)
    return jsonify(items)

@app.get("/api/weather")
def api_weather():
    try:
        data = fetch_weather()
        return jsonify(data or {"need_config": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/api/air")
def api_air():
    try:
        data = fetch_air_quality()
        return jsonify(data or {"need_config": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.get("/api/photos")
def api_photos():
    return jsonify(list_local_images())

@app.get("/photos/<path:fname>")
def serve_photo(fname):
    return send_from_directory(str(PHOTOS_DIR), fname)

@app.get("/api/bus")
def api_bus():
    try:
        data = render_bus_box()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/home-devices")
def api_home_devices():
    try:
        devices = home_assistant_list_devices()
        resp: Dict[str, Any] = {"devices": devices}
        dash_cfg = _home_assistant_cfg().get("dashboard") or {}
        if isinstance(dash_cfg, dict) and (
            dash_cfg.get("title") or dash_cfg.get("url_path")
        ):
            resp["dashboard"] = {
                "title": dash_cfg.get("title") or "",
                "url_path": dash_cfg.get("url_path") or "",
                "entity_count": dash_cfg.get("entity_count"),
            }
        if not devices:
            resp["message"] = "Home Assistant에서 표시할 기기를 찾지 못했습니다."
        return jsonify(resp)
    except HomeAssistantConfigError as e:
        return jsonify({"need_config": True, "message": str(e)})
    except HomeAssistantAPIError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/home-devices/<device_id>/execute")
def api_home_devices_execute(device_id: str):
    payload = request.get_json(silent=True) or {}
    if "on" not in payload:
        return jsonify({"error": "'on' 값을 전달해야 합니다."}), 400
    try:
        desired = _coerce_bool(payload.get("on"))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        home_assistant_execute(device_id, desired)
        return jsonify({"success": True})
    except HomeAssistantConfigError as e:
        return jsonify({"error": str(e)}), 400
    except HomeAssistantAPIError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# === [SECTION: Board HTML (legacy UI; monthly calendar + photo fade)] ========
BOARD_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no"/>
<title>Smart Frame</title>
<style>
  :root { --W:1080px; --H:1828px; --top:70px; --cal:910px;
          --weather:280px; --layout-left:420px; --section-gap:20px;
          --section-card-gap:calc(var(--section-gap) - 4px); }

  /* Global layout */
  html,body { margin:0; padding:0; background:transparent; color:#fff; font-family:system-ui,-apple-system,Roboto,'Noto Sans KR',sans-serif; }
  .frame { width:var(--W); min-height:var(--H); margin:0 auto; display:flex; flex-direction:column; position:relative; }

  /* Background photo crossfade */
  .bg, .bg2 {
    position: fixed; inset: 0; z-index: -1;
    background-size: cover; background-position: center center; background-repeat: no-repeat;
    transition: opacity 1s ease;
  }
  .bg2 { opacity: 0; }

  .top { height:var(--top); display:flex; align-items:center; justify-content:space-between; padding:0 24px; box-sizing:border-box; }
  .time { font-size:38px; font-weight:700; letter-spacing:1px; text-shadow:0 0 6px rgba(0,0,0,.65);}
  .date { font-size:22px; opacity:.95; text-shadow:0 0 6px rgba(0,0,0,.65);}

  .cal { height:auto; min-height:var(--cal); padding:8px 20px calc(var(--section-gap)/2); box-sizing:border-box; display:flex; flex-direction:column; }
  .cal h2 { margin:0 0 8px 0; font-size:22px; opacity:.95; display:flex; align-items:center; gap:8px; text-shadow:0 0 6px rgba(0,0,0,.65);}

  .grid { flex:1 1 auto; display:grid; grid-template-columns: repeat(7, 1fr); grid-auto-rows: 1fr; gap:6px; }
  .dow { display:grid; grid-template-columns: repeat(7, 1fr); margin-bottom:6px; opacity:.95; font-size:14px; text-shadow:0 0 6px rgba(0,0,0,.65);}
  .dow div { text-align:center; }

  /* Calendar cells */
  .cell { border:1px solid rgba(255,255,255,.12); border-radius:10px; padding:6px;
          background:rgba(0,0,0,.35); display:flex; flex-direction:column; overflow:hidden;}
  .cell.dim { opacity:.45; }
  .dnum { font-size:14px; opacity:.95; margin-bottom:4px; text-shadow:0 0 6px rgba(0,0,0,.65);}
  .ev { font-size:12px; line-height:1.25; margin:2px 0;
        background:rgba(0,0,0,.45); border-radius:6px; padding:2px 6px;
        white-space:nowrap; overflow:hidden; text-overflow:ellipsis; text-shadow:0 0 6px rgba(0,0,0,.65);}

  .section {
    position:relative;
    min-height:auto;
    height:auto;
    padding:calc(var(--section-gap)/2) 24px var(--section-gap);
    box-sizing:border-box;
    display:flex;
    flex-direction:column;
    gap:var(--section-gap);
  }

  .section-upper {
    display:grid;
    grid-template-columns:minmax(0, var(--layout-left)) minmax(0, 1fr);
    gap:var(--section-card-gap);
    align-items:stretch;
  }

  .section-left {
    display:flex;
    flex-direction:column;
    gap:var(--section-card-gap);
  }

  .section-right {
    display:flex;
    flex-direction:column;
    gap:var(--section-card-gap);
    border-left:none;
    padding-left:0;
  }

  .blk { background:rgba(0,0,0,.35); border:1px solid rgba(255,255,255,.08); border-radius:12px; padding:16px 18px; }
  .blk h3 { margin:0 0 6px 0; font-size:16px; opacity:.95; text-shadow:0 0 6px rgba(0,0,0,.65);}

.todo{ display:flex; flex-direction:column; height:100%; min-height:170px; }
  .todo .rows { display:grid; grid-template-columns: 1fr 1fr; gap:8px; flex:1 1 auto; }
  .todo .col { display:flex; flex-direction:column; gap:6px; min-width:0; }
  .todo .item { display:flex; justify-content:flex-start; gap:10px; font-size:14px; }
  .todo .title { flex:1 1 auto; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .todo .due { opacity:.9; min-width:50px; margin-right:12px; }

  .bus{display:flex; flex-direction:column; gap:10px; background:rgba(0,0,0,.28); border:1px solid rgba(255,255,255,.12); border-radius:12px; padding:16px 18px; height:100%; min-height:190px;}
  .bus .arrivals{display:flex; flex-direction:column; gap:8px; min-width:0; flex:1 1 auto;}
  .bus .arrivals h3{margin-bottom:0;}
  .bus .stop{font-size:14px; margin-bottom:4px;}
  .bus .rows{display:flex; gap:10px; overflow:hidden;}
  .bus .col{flex:1 1 50%; display:flex; flex-direction:column; gap:6px;}
  .bus .item{display:flex; font-size:14px; white-space:nowrap;}
  .bus .item .rt{font-weight:700; width:8ch; white-space:nowrap;}
  .bus .item .hops{width:6ch; text-align:right; margin-right:4px; white-space:nowrap;}
  .bus .item .msg{flex:0 0 6ch; text-align:right; opacity:.9; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;}

  .home{display:flex; flex-direction:column; background:rgba(0,0,0,.28); border:1px solid rgba(255,255,255,.12); border-radius:12px; padding:20px 22px; min-height:360px; height:100%;}
  .home h3{margin-bottom:6px;}
  .home .ha-grid{display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:14px; flex:1 1 auto; align-content:start; overflow:auto; padding-bottom:6px;}
  .home .ha-device{display:flex; align-items:center; gap:14px; padding:18px 16px; border-radius:16px; background:rgba(0,0,0,.32); border:1px solid rgba(255,255,255,.12); transition:background .2s, border-color .2s, box-shadow .2s, transform .2s; cursor:pointer; user-select:none; min-height:82px;}
  .home .ha-device .icon{font-size:30px;}
  .home .ha-device .meta{display:flex; flex-direction:column; gap:6px; align-items:flex-start; min-width:0;}
  .home .ha-device .name{font-size:16px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:100%;}
  .home .ha-device .state{font-size:14px; opacity:.9;}
  .home .ha-device .state.on{color:#96f7c5;}
  .home .ha-device .state.off{color:rgba(255,255,255,.82);}
  .home .ha-device.on{background:rgba(255,255,255,.12); border-color:rgba(150,247,197,.55); box-shadow:0 0 22px rgba(150,247,197,.35);}
  .home .ha-device.offline{opacity:.55; border-style:dashed; border-color:rgba(255,255,255,.25); cursor:default;}
  .home .ha-device.disabled{cursor:default; opacity:.7;}
  .home .ha-device.pending{pointer-events:none; opacity:.65;}
  .home .ha-device.offline .state{color:#ffb4b4;}
  .home .ha-status{grid-column:1 / -1; padding:12px 16px; border-radius:12px; background:rgba(0,0,0,.32); font-size:14px; text-align:center; line-height:1.4;}
  .home .ha-device:active{transform:scale(.97);}

  /* Verse block */
  .verse { flex:0 0 100px; display:flex; flex-direction:column; align-items:flex-start; }
  .verse .text { white-space:pre-wrap; line-height:1.4; font-size:16px; text-shadow:0 0 6px rgba(0,0,0,.65); }

/* Weather layout (card style 5-day forecast) */
  .weather {
    display:flex;
    gap:16px;
    align-items:stretch;
    flex-wrap:wrap;
    padding:14px 16px;
  }
  .weather .w-now {
    display:flex;
    align-items:center;
    gap:12px;
    min-width:220px;
    flex:0 0 auto;
  }
  .weather .w-now .info { display:flex; flex-direction:column; gap:6px; }
  .weather .w-now .temp { font-size:38px; font-weight:800; line-height:1; }
  .weather .w-now .meta { display:flex; flex-direction:column; gap:4px; font-size:14px; opacity:.9; }
  .weather .w-now .meta span { display:flex; align-items:center; gap:6px; }

  .weather .w-days {
    display:grid;
    grid-template-columns:repeat(5, minmax(0, 105px));
    gap:16px;
    justify-content:flex-start;
    flex:0 0 auto;
    margin-right:auto;
  }
  .weather .w-day {
    text-align:center;
    background:rgba(0,0,0,.25);
    border:1px solid rgba(255,255,255,.08);
    border-radius:12px;
    padding:6px 4px;
    width:105px;
  }
.weather .w-day.today { outline:2px solid rgba(255,255,255,.35); outline-offset:-2px; }
.weather .w-day img { width:52px; height:52px; display:block; margin:2px auto; }
.weather .w-day .temps { display:flex; justify-content:center; gap:6px; font-size:14px; margin-top:2px; }
.weather .w-day .hi { font-weight:800; font-size:16px; }
.weather .w-day .lo { opacity:.75; font-size:14px; }

/* ▼ AQI card on the right */
  .weather .w-aqi {
    width:105px;
    text-align:center;
    background:rgba(0,0,0,.25);
    border:1px solid rgba(255,255,255,.08);
    border-radius:12px;
    padding:10px 6px;
    display:flex;
    flex-direction:column;
    justify-content:center;
    gap:4px;
    flex:0 0 auto;
  }
.weather .w-aqi .ttl { font-size:12px; letter-spacing:.5px; opacity:.9; }
.weather .w-aqi .idx { font-size:24px; font-weight:800; line-height:1; }
.weather .w-aqi .lbl { font-size:14px; opacity:.9; }
.weather .w-aqi .pm { font-size:12px; opacity:.85; }

/* Background must stay behind content */
.bg, .bg2 { z-index:-1; }
.frame { position:relative; z-index:1; }

</style>
</head>
<body>
<div class="bg" id="bg1"></div>
<div class="bg2" id="bg2"></div>

<div class="frame">
  <div class="top">
    <div class="time" id="clock">--:--</div>
    <div class="date" id="datetxt">----</div>
  </div>

  <div class="cal">
    <h2 id="cal-title">Calendar</h2>
    <div class="dow"><div>Sun</div><div>Mon</div><div>Tue</div><div>Wed</div><div>Thu</div><div>Fri</div><div>Sat</div></div>
    <div class="grid" id="grid"></div>
  </div>

  <div class="section">
    <div class="section-upper">
      <div class="section-left">
        <div class="verse blk"><h3>Today's Verse</h3><div id="verse" class="text"></div></div>
        <div class="todo blk">
          <h3>Todo</h3>
          <div class="rows">
            <div class="col" id="todo-col-1"></div>
            <div class="col" id="todo-col-2"></div>
          </div>
        </div>
        <div class="bus blk">
          <div class="arrivals">
            <h3 id="bus-title">BUS Info</h3>
            <div class="stop" id="bus-stop"></div>
            <div class="rows" id="businfo">
              <div class="col" id="bus-left"></div>
              <div class="col" id="bus-right"></div>
            </div>
          </div>
        </div>
      </div>
      <div class="section-right">
        <div class="home blk">
          <h3>Home Control</h3>
          <div class="ha-grid" id="ha-grid"></div>
        </div>
      </div>
    </div>
    <div class="weather blk" id="weather"></div>
  </div>
</div>

<script>
function z(n){return n<10?'0'+n:n}
function tick(){
  const d=new Date();
  const days=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  document.getElementById('clock').textContent = z(d.getHours())+":"+z(d.getMinutes());
  document.getElementById('datetxt').textContent = d.getFullYear()+"."+z(d.getMonth()+1)+"."+z(d.getDate())+" ("+days[d.getDay()]+")";
}
setInterval(tick, 1000); tick();

function startOfWeek(d){ const day=d.getDay(); const s=new Date(d); s.setDate(d.getDate()-day); s.setHours(0,0,0,0); return s; }

async function loadEvents(){
  const d=new Date();
  const y=d.getFullYear(), m=d.getMonth()+1;
  document.getElementById('cal-title').textContent = `Calendar  ${y}-${z(m)}`;
  const r = await fetch(`/api/events?year=${y}&month=${m}`);
  const items = await r.json();

  const byDay = {};
  for(const ev of items){
    const k = (ev.start||'').substring(0,10);
    (byDay[k]=byDay[k]||[]).push(ev);
  }

  const first = new Date(y, m-1, 1);
  let cur = startOfWeek(first);
  const grid = document.getElementById('grid'); grid.innerHTML='';
  let count = 0;
  while(count < 42){
    const cell = document.createElement('div');
    cell.className = 'cell' + ((cur.getMonth()+1!==m)?' dim':'');
    const key = `${cur.getFullYear()}-${z(cur.getMonth()+1)}-${z(cur.getDate())}`;
    const dn  = document.createElement('div'); dn.className='dnum'; dn.textContent = cur.getDate();
    cell.appendChild(dn);
    const arr = (byDay[key]||[]).slice(0,3);
    for(const ev of arr){
      const e=document.createElement('div'); e.className='ev'; e.textContent = ev.title || '(untitled)';
      cell.appendChild(e);
    }
    grid.appendChild(cell);
    cur.setDate(cur.getDate()+1);
    count++;
  }
}
loadEvents(); setInterval(loadEvents, 5*60*1000);




// ===== Weather block (final: card-style 5-day forecast) =====
async function loadWeather() {
  const box = document.getElementById('weather');
  try {
    // 날씨 + AQI 동시 요청
    const [wr, ar] = await Promise.all([
      fetch('/api/weather'),
      fetch('/api/air')
    ]);

    const data = await wr.json();
    const air  = await ar.json().catch(()=>null);

    box.innerHTML = '';

    if (data && data.need_config) { box.textContent = 'OWM API Key required'; return; }
    if (!data || data.error)     { box.textContent = 'Weather error';       return; }

    // 현재(좌측)
    const now = document.createElement('div');
    now.className = 'w-now';
    const current = data.current || {};
    const i = document.createElement('img');
    i.src = current.icon || ''; i.alt = ''; i.style.width='70px'; i.style.height='70px';
    const info = document.createElement('div'); info.className = 'info';
    const t = document.createElement('div');
    t.className = 'temp';
    const tempValue = (current.temp !== null && current.temp !== undefined) ? current.temp + '°' : '–';
    t.textContent = tempValue;
    info.appendChild(t);

    const meta = document.createElement('div');
    meta.className = 'meta';
    const feels = current.feels_like;
    const humidity = current.humidity;
    const dew = current.dew_point;
    if (feels !== null && feels !== undefined) {
      const row = document.createElement('span');
      row.textContent = `체감온도 ${feels}°`;
      meta.appendChild(row);
    }
    if (humidity !== null && humidity !== undefined) {
      const row = document.createElement('span');
      row.textContent = `습도 ${humidity}%`;
      meta.appendChild(row);
    }
    if (dew !== null && dew !== undefined) {
      const row = document.createElement('span');
      row.textContent = `이슬점 ${dew}°`;
      meta.appendChild(row);
    }
    if (meta.childElementCount > 0) info.appendChild(meta);

    now.appendChild(i);
    now.appendChild(info);

    // 5일 카드(중앙) — 데이터가 7일 와도 5개만 사용
    const days = document.createElement('div');
    days.className = 'w-days';
    const names = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    const todayIso = new Date().toISOString().slice(0,10);

    const fiveDays = (Array.isArray(data.days) ? data.days : []).slice(0, 5);
    for (const d of fiveDays) {
      const dt = new Date(d.date);
      const item = document.createElement('div');
      item.className = 'w-day';
      if (d.date === todayIso) item.classList.add('today');

      const nm = document.createElement('div'); nm.className='nm'; nm.textContent = names[dt.getDay()];
      const im = document.createElement('img'); im.src = d.icon; im.alt = '';
      const temps = document.createElement('div'); temps.className='temps';
      const hi = document.createElement('div'); hi.className='hi'; hi.textContent = d.max + '°';
      const lo = document.createElement('div'); lo.className='lo'; lo.textContent = d.min + '°';
      temps.appendChild(hi); temps.appendChild(lo);

      item.appendChild(nm); item.appendChild(im); item.appendChild(temps);
      days.appendChild(item);
    }

    // AQI 카드(우측 끝)
    const aqiCard = document.createElement('div');
    aqiCard.className = 'w-aqi';
    const ttl = document.createElement('div'); ttl.className='ttl'; ttl.textContent = 'AQI';
    const idx = document.createElement('div'); idx.className='idx';
    const lbl = document.createElement('div'); lbl.className='lbl';
    const pm25 = document.createElement('div'); pm25.className='pm pm25';
    const pm10 = document.createElement('div'); pm10.className='pm pm10';

    if (air && !air.error && !air.need_config) {
      idx.textContent = air.aqi != null ? String(air.aqi) : '?';
      lbl.textContent = air.label || '';
      if (air.color) {
        aqiCard.style.boxShadow = `inset 0 0 0 2px ${air.color}`;
        aqiCard.style.color = '#fff';
      }
      if (air.pm2_5 != null) pm25.textContent = 'PM2.5 ' + Math.round(air.pm2_5);
      if (air.pm10  != null) pm10.textContent  = 'PM10 '  + Math.round(air.pm10);
    } else {
      idx.textContent = '–';
      lbl.textContent = 'n/a';
    }
    aqiCard.appendChild(ttl); aqiCard.appendChild(idx); aqiCard.appendChild(lbl);
    if (pm25.textContent) aqiCard.appendChild(pm25);
    if (pm10.textContent) aqiCard.appendChild(pm10);

    // 조립
    box.appendChild(now);
    box.appendChild(days);
    box.appendChild(aqiCard);

  } catch (e) {
    if (box) box.textContent = 'Failed to load weather';
  }
}
loadWeather();
setInterval(loadWeather, 10 * 60 * 1000);

// ===== Verse block =====
async function loadVerse(){
  try{
    const r = await fetch('/api/verse');
    const js = await r.json();
    document.getElementById('verse').textContent = js.text || '';
  }catch(e){
    document.getElementById('verse').textContent = '';
  }
}
loadVerse(); setInterval(loadVerse, 10*1000);

// ===== Todo block (Todoist, 2 columns, 10 each) =====
function fmtDue(v){
  if(!v) return '';
  const d = new Date(v);
  if (isNaN(d.getTime())) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(v);
    if (m) return m[2] + '/' + m[3];
    return v;
  }
  return (d.getMonth()+1) + '/' + d.getDate();
}

async function loadTodo(){
  try{
    const r = await fetch('/api/todo');
    const data = await r.json();
    const c1 = document.getElementById('todo-col-1');
    const c2 = document.getElementById('todo-col-2');
    c1.innerHTML = ''; c2.innerHTML = '';

    if (data.need_config){
      const msg = document.createElement('div'); msg.textContent = 'Todoist API token required';
      c1.appendChild(msg);
      return;
    }
    if (!Array.isArray(data) || data.length === 0){
      const msg = document.createElement('div'); msg.textContent = 'No pending tasks.';
      c1.appendChild(msg);
      return;
    }

    const first10 = data.slice(0,10);
    const next10  = data.slice(10,20);

    for (const t of first10){
      const row = document.createElement('div'); row.className='item';
      const date = document.createElement('div'); date.className='due'; date.textContent = fmtDue(t.due) || '';
      const title = document.createElement('div'); title.className='title'; title.textContent = t.title || '(untitled)';
      row.appendChild(date); row.appendChild(title); c1.appendChild(row);
    }
    for (const t of next10){
      const row = document.createElement('div'); row.className='item';
      const date = document.createElement('div'); date.className='due'; date.textContent = fmtDue(t.due) || '';
      const title = document.createElement('div'); title.className='title'; title.textContent = t.title || '(untitled)';
      row.appendChild(date); row.appendChild(title); c2.appendChild(row);
    }
  }catch(e){
    // ignore
  }
}
loadTodo(); setInterval(loadTodo, 20*1000);

// ===== Home Assistant 제어 패널 =====
let haDevices = [];
const haDevicesState = { loading:false, needConfig:false, message:'', fetchError:'', commandError:'', dashboardTitle:'' };

function renderHomeControls(){
  const grid = document.getElementById('ha-grid');
  if(!grid) return;
  grid.innerHTML = '';

  const heading = document.querySelector('.home h3');
  if(heading){
    heading.textContent = haDevicesState.dashboardTitle ? `Home Control · ${haDevicesState.dashboardTitle}` : 'Home Control';
  }

  const statuses = [];
  if(haDevicesState.needConfig){
    statuses.push(haDevicesState.message || 'Home Assistant 연동 설정이 필요합니다.');
  }
  if(haDevicesState.fetchError){
    statuses.push('불러오기 오류: ' + haDevicesState.fetchError);
  }
  if(haDevicesState.commandError){
    statuses.push('명령 실패: ' + haDevicesState.commandError);
  }
  if(!haDevicesState.needConfig && !haDevicesState.fetchError && haDevicesState.message){
    statuses.push(haDevicesState.message);
  }
  if(!haDevicesState.loading && !haDevicesState.needConfig && !haDevicesState.fetchError && haDevices.length === 0){
    statuses.push('표시할 기기가 없습니다.');
  }

  statuses.filter(Boolean).forEach(text => {
    const msg = document.createElement('div');
    msg.className = 'ha-status';
    msg.textContent = text;
    grid.appendChild(msg);
  });

  if(haDevicesState.needConfig){
    return;
  }
  if(haDevicesState.fetchError && haDevices.length === 0){
    return;
  }

  haDevices.forEach(dev => {
    const item = document.createElement('div');
    const isOn = dev.state && dev.state.on === true;
    const isOnline = dev.online !== false;
    const canToggle = dev.can_toggle === true && isOnline;
    const isPending = dev.pending === true;
    item.className = 'ha-device' + (isOn ? ' on' : '');
    if(!isOnline) item.className += ' offline';
    if(!canToggle) item.className += ' disabled';
    if(isPending) item.className += ' pending';
    item.dataset.id = dev.id || '';
    item.setAttribute('role', 'button');
    item.setAttribute('aria-pressed', String(!!isOn));
    const tooltip = [dev.name || dev.id || '', dev.room || '', dev.state_label || ''].filter(Boolean).join(' · ');
    if(tooltip) item.title = tooltip;

    const icon = document.createElement('div');
    icon.className = 'icon';
    icon.textContent = dev.icon || '🔘';

    const meta = document.createElement('div');
    meta.className = 'meta';

    const name = document.createElement('div');
    name.className = 'name';
    const room = dev.room ? ` · ${dev.room}` : '';
    name.textContent = (dev.name || dev.id || '기기') + room;

    const state = document.createElement('div');
    state.className = 'state';
    if(!isPending && isOnline){
      state.classList.add(isOn ? 'on' : 'off');
    }
    state.textContent = isPending ? '동작 중…' : (dev.state_label || (isOn ? '켜짐' : '꺼짐'));

    meta.appendChild(name);
    meta.appendChild(state);

    item.appendChild(icon);
    item.appendChild(meta);

    if(canToggle){
      item.addEventListener('click', () => toggleHADevice(dev));
    }

    grid.appendChild(item);
  });
}

async function loadHomeDevices(){
  haDevicesState.loading = true;
  haDevicesState.fetchError = '';
  haDevicesState.message = '';
  renderHomeControls();
  try{
    const r = await fetch('/api/home-devices');
    if(!r.ok){
      throw new Error('HTTP ' + r.status);
    }
    const data = await r.json();
    haDevicesState.dashboardTitle = data.dashboard && data.dashboard.title ? data.dashboard.title : '';
    if(data.need_config){
      haDevices = [];
      haDevicesState.needConfig = true;
      haDevicesState.message = data.message || 'Home Assistant 연동을 설정하세요.';
      haDevicesState.dashboardTitle = '';
      return;
    }
    haDevicesState.needConfig = false;
    haDevicesState.commandError = '';
    if(data.error){
      throw new Error(data.error);
    }
    haDevices = Array.isArray(data.devices) ? data.devices : [];
    if(data.message){
      haDevicesState.message = data.message;
    }
  }catch(e){
    haDevices = [];
    haDevicesState.fetchError = (e && e.message) ? e.message : '알 수 없는 오류';
    haDevicesState.dashboardTitle = '';
  }finally{
    haDevicesState.loading = false;
    renderHomeControls();
  }
}

async function toggleHADevice(dev){
  if(!dev || dev.pending || dev.can_toggle !== true || dev.online === false){
    return;
  }
  const desired = !(dev.state && dev.state.on === true);
  dev.pending = true;
  dev.state = Object.assign({}, dev.state, { on: desired });
  dev.state_label = desired ? '켜짐' : '꺼짐';
  renderHomeControls();
  try{
    const r = await fetch(`/api/home-devices/${encodeURIComponent(dev.id)}/execute`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ on: desired })
    });
    if(!r.ok){
      const err = await r.json().catch(()=>({}));
      throw new Error(err.error || ('HTTP ' + r.status));
    }
    haDevicesState.commandError = '';
  }catch(e){
    haDevicesState.commandError = (e && e.message) ? e.message : '실행 실패';
  }finally{
    dev.pending = false;
    await loadHomeDevices();
  }
}

loadHomeDevices();
setInterval(loadHomeDevices, 30*1000);

async function refreshBus(){
  try{
    const r = await fetch('/api/bus');
    if(!r.ok) return;
    const data = await r.json();
    const titleEl = document.getElementById('bus-title');
    if(titleEl) titleEl.textContent = data.title || '버스도착';
    const stopEl = document.getElementById('bus-stop');
    if(stopEl) stopEl.textContent = data.stop || '';

    const left = document.getElementById('bus-left');
    const right = document.getElementById('bus-right');
    if(!left || !right) return;

    left.innerHTML='';
    right.innerHTML='';

    const rows = data.rows || [];
    if(!rows.length){
      left.textContent = '정보 없음';
      return;
    }
    if(rows.length===1 && rows[0].text && !rows[0].route){
      left.textContent = rows[0].text;
      return;
    }
    const mid = Math.ceil(rows.length/2);
    rows.slice(0, mid).forEach(it=>{
      const row=document.createElement('div');
      row.className='item';
      row.innerHTML=`<div class="rt">${it.route}</div><div class="hops">${it.hops}</div><div class="msg">${it.eta}</div>`;
      left.appendChild(row);
    });
    rows.slice(mid).forEach(it=>{
      const row=document.createElement('div');
      row.className='item';
      row.innerHTML=`<div class="rt">${it.route}</div><div class="hops">${it.hops}</div><div class="msg">${it.eta}</div>`;
      right.appendChild(row);
    });
  }catch(e){}
}
refreshBus();
setInterval(refreshBus,60000);

// ===== Background photo crossfade (delay-optimized & path-safe) =====
// - /api/photos 목록 셔플
// - 세그먼트별 URL 인코딩(하위 폴더 유지)
// - Image().decode()로 미리 디코드 후 전환
// - 초기 한 장은 화면에 바로 세팅하고 큐에서 소비 → 첫 전환 즉시 다른 사진
// - 탭 비활성화 시 타이머 일시중지

let photoList = [];
let pi = 0;           // 사진 인덱스
let front = 1;        // 현재 보이는 레이어: 1=bg1, 2=bg2

const DISPLAY_INTERVAL_MS = 5000;
const PRELOAD_MIN_COUNT   = 2;
const PRELOAD_COOLDOWN_MS = 250;

let preloadQueue = [];     // [{ url, readyAt }]
let isPreloading = false;
let nextSwitchAt = 0;
let slideTimer = null;
let refillTimer = null;

// --- 유틸: 세그먼트별 인코딩(하위 폴더 유지) -------------------------------
function buildPhotoUrl(name){
  // "a/b c.jpg" -> "/photos/a/b%20c.jpg"
  return '/photos/' + String(name).split('/').map(encodeURIComponent).join('/');
}

// --- 유틸: 배열 셔플 -------------------------------------------------------
function shuffle(arr){
  for (let i = arr.length - 1; i > 0; i--){
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
}

// --- 목록 로드 --------------------------------------------------------------
async function loadPhotos(){
  try{
    const r = await fetch('/api/photos');
    photoList = await r.json();
    shuffle(photoList);
  }catch(e){
    console.error('[photos] load failed:', e);
    photoList = [];
  }
}

// --- 이미지 1장 프리로드(+decode) -----------------------------------------
function preloadOne(url){
  return new Promise((resolve)=>{
    const img = new Image();
    let done = false;
    const finish = ok => { if (!done){ done = true; resolve(ok ? img : null); } };
    img.onload = ()=>{
      if (img.decode){
        img.decode().then(()=>finish(true)).catch(()=>finish(true));
      }else{
        finish(true);
      }
    };
    img.onerror = ()=> finish(null);
    img.src = url;
  });
}

// --- 프리로드 큐 보충 ------------------------------------------------------
async function ensurePreloaded(){
  if (isPreloading) return;
  isPreloading = true;
  try{
    while (preloadQueue.length < PRELOAD_MIN_COUNT && photoList.length){
      const name = photoList[pi % photoList.length]; pi++;
      const url  = buildPhotoUrl(name);
      const ok   = await preloadOne(url);
      if (ok){
        preloadQueue.push({ url, readyAt: Date.now() + PRELOAD_COOLDOWN_MS });
      }
    }
  }finally{
    isPreloading = false;
  }
}

// --- 실제 전환 --------------------------------------------------------------
function swapBackground(nextUrl){
  const incoming = document.getElementById(front === 1 ? 'bg2' : 'bg1'); // 들어올 레이어(현재 투명)
  incoming.style.backgroundImage = `url("${nextUrl}")`;
  // reflow
  incoming.offsetHeight;
  incoming.style.opacity = 1;

  const outgoing = document.getElementById(front === 1 ? 'bg1' : 'bg2'); // 나갈 레이어(현재 보임)
  outgoing.style.opacity = 0;

  front = 3 - front;
}

// --- 한 스텝 전환 ----------------------------------------------------------
async function showNextPhoto(){
  if (!photoList.length) return;

  await ensurePreloaded();
  if (!preloadQueue.length) return;

  const now = Date.now();
  if (now < nextSwitchAt) return;

  const { url, readyAt } = preloadQueue[0];
  if (now < readyAt) return;

  preloadQueue.shift();
  swapBackground(url);
  nextSwitchAt = now + DISPLAY_INTERVAL_MS;

  // 백그라운드 프리로드
  ensurePreloaded();
}

// --- 타이머 컨트롤/가시성 대응 --------------------------------------------
function stopPhotoTimers(){
  if (slideTimer){ clearInterval(slideTimer); slideTimer = null; }
  if (refillTimer){ clearInterval(refillTimer); refillTimer = null; }
}
function startPhotoTimers(){
  if (!slideTimer){
    slideTimer = setInterval(showNextPhoto, DISPLAY_INTERVAL_MS);
  }
  if (!refillTimer){
    refillTimer = setInterval(async ()=>{
      if (!photoList.length){
        await loadPhotos();
      }
      ensurePreloaded();
    }, 60 * 1000);
  }
}
document.addEventListener('visibilitychange', ()=>{
  if (document.hidden){
    stopPhotoTimers();
  }else{
    nextSwitchAt = Date.now();
    startPhotoTimers();
  }
});

// --- 초기화(IIFE) -----------------------------------------------------------
(async ()=>{
  stopPhotoTimers();

  await loadPhotos();
  if (!photoList.length){
    return;
  }

  await ensurePreloaded();

  const b1 = document.getElementById('bg1');
  const b2 = document.getElementById('bg2');

  // 초기 1장 화면 세팅(큐에서 소비)
  if (preloadQueue[0]){
    const first = preloadQueue.shift();
    b1.style.backgroundImage = `url("${first.url}")`;
    b1.style.opacity = 1;
    b2.style.opacity = 0;
    front = 1;
    nextSwitchAt = Date.now() + DISPLAY_INTERVAL_MS;
  }

  // 다음 전환용으로 숨김 레이어 미리 세팅(있다면)
  if (preloadQueue[0]){
    b2.style.backgroundImage = `url("${preloadQueue[0].url}")`;
  }

  startPhotoTimers();
  showNextPhoto(); // 준비됐으면 바로 1회 시도
})();
</script>
</body>
</html>
"""

@app.get("/board")
def board():
    return render_template_string(BOARD_HTML)

# === [SECTION: Bot state helpers (persist to json file)] ===

def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_state(d):
    STATE_PATH.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

# === [SECTION: Telegram bot initialization / ACL] ============================
TB = telebot.TeleBot(CFG["telegram"]["bot_token"]) if CFG["telegram"]["bot_token"] else None
ALLOWED = set(CFG["telegram"]["allowed_user_ids"])
def allowed(uid): return uid in ALLOWED if ALLOWED else True
KBUS_WAIT = set()

# === [SECTION: Inline calendar add flow UI helpers] ==========================
def month_days(year: int, month: int) -> int:
    if month == 12:
        return (date(year + 1, 1, 1) - date(year, 12, 1)).days
    return (date(year, month + 1, 1) - date(year, month, 1)).days

def ask_year(chat_id, uid, next_cb):
    now = datetime.now(TZ)
    start = now.year
    kb = telebot.types.InlineKeyboardMarkup(row_width=3)
    for y in range(start, start + 11):
        kb.add(telebot.types.InlineKeyboardButton(str(y), callback_data=f"{next_cb}:{y}"))
    TB.send_message(chat_id, "Select a year.", reply_markup=kb)

def ask_month(chat_id, uid, year: int, next_cb):
    kb = telebot.types.InlineKeyboardMarkup(row_width=4)
    for m in range(1, 13):
        kb.add(telebot.types.InlineKeyboardButton(f"{m:02d}", callback_data=f"{next_cb}:{year},{m}"))
    TB.send_message(chat_id, f"Select a month of {year}.", reply_markup=kb)

def ask_day(chat_id, uid, year: int, month: int, next_cb):
    days = month_days(year, month)
    kb = telebot.types.InlineKeyboardMarkup(row_width=7)
    row = []
    for d in range(1, days + 1):
        row.append(telebot.types.InlineKeyboardButton(f"{d:02d}", callback_data=f"{next_cb}:{year},{month},{d}"))
        if len(row) == 7:
            kb.row(*row); row = []
    if row: kb.row(*row)
    TB.send_message(chat_id, f"Select a day: {year}-{month:02d}.", reply_markup=kb)

def ask_end_same(chat_id):
    kb = telebot.types.InlineKeyboardMarkup()
    kb.add(telebot.types.InlineKeyboardButton("Single day", callback_data="end_same_yes"))
    kb.add(telebot.types.InlineKeyboardButton("Multi-day", callback_data="end_same_no"))
    TB.send_message(chat_id, "Is the event single-day?", reply_markup=kb)

def ask_content(chat_id):
    TB.send_message(chat_id, "Please enter the event title.")

def create_event_via_google(chat_id, uid):
    """Insert all-day event by selected start/end, then clear state."""
    st = load_state().get(str(uid), {})
    try:
        y, mo, d = st["year"], st["month"], st["day"]
        ey, em, ed = st["end_year"], st["end_month"], st["end_day"]
    except KeyError:
        TB.send_message(chat_id, "Flow state missing. Start again with /set."); return
    title = st.get("title", "(untitled)")
    try:
        svc = get_google_service()
    except Exception as e:
        TB.send_message(chat_id, f"Google error: {e}"); return
    cal_id = CFG["google"]["calendar"].get("id", "primary")
    body = {
        "summary": title,
        "start": {"date": date(y, mo, d).isoformat()},
        "end":   {"date": (date(ey, em, ed) + timedelta(days=1)).isoformat()},
    }
    try:
        svc.events().insert(calendarId=cal_id, body=body).execute()
        TB.send_message(chat_id, "Event created.")
    except Exception as e:
        TB.send_message(chat_id, f"Insert failed: {e}")
    finally:
        st_all = load_state(); st_all.pop(str(uid), None); save_state(st_all)

def kb_inline(rows):
    kb = telebot.types.InlineKeyboardMarkup(row_width=1)
    for r in rows: kb.add(*r)
    return kb

# === [SECTION: Telegram handlers (commands, callbacks, text)] ================
if TB:
    @TB.message_handler(commands=["start"])
    def start_cmd(m):
        if not allowed(m.from_user.id):
            return TB.reply_to(m, "Not authorized.")
        lines = [
            "Welcome!",
            "Use /frame or /set to configure the frame.",
            "For Google Calendar, authorize at /oauth/start in the web UI."
        ]
        TB.reply_to(m, "\n".join(lines))

    @TB.message_handler(commands=["frame"])
    def frame_cmd(m):
        if not allowed(m.from_user.id):
            return TB.reply_to(m, "Not authorized.")
        kb = kb_inline([
            [telebot.types.InlineKeyboardButton("1) iCal URL View/Change", callback_data="cfg_ical")],
            [telebot.types.InlineKeyboardButton("2) Todo (later)", callback_data="noop")],
            [telebot.types.InlineKeyboardButton("3) Photos (later)", callback_data="noop")],
            [telebot.types.InlineKeyboardButton("4) Home Assistant", callback_data="cfg_ha")],
            [telebot.types.InlineKeyboardButton("5) 버스정보", callback_data="cfg_bus")],
        ])
        TB.send_message(m.chat.id, "Smart Frame Settings", reply_markup=kb)

    @TB.message_handler(commands=["set"])
    def set_cmd(m):
        if not allowed(m.from_user.id):
            return TB.reply_to(m, "Not authorized.")
        kb = kb_inline([
            [telebot.types.InlineKeyboardButton("1) calendar", callback_data="cfg_ical")],
            [telebot.types.InlineKeyboardButton("2) google oauth status", callback_data="cfg_ghow")],
            [telebot.types.InlineKeyboardButton("3) Home Assistant", callback_data="cfg_ha")],
            [telebot.types.InlineKeyboardButton("4) 버스정보", callback_data="cfg_bus")],
            [telebot.types.InlineKeyboardButton("5) photo (later)", callback_data="noop")],
            [telebot.types.InlineKeyboardButton("6) weather (later)", callback_data="noop")],
            [telebot.types.InlineKeyboardButton("7) manage events", callback_data="cal_manage")],
            [telebot.types.InlineKeyboardButton("8) verse", callback_data="set_verse")],
            [telebot.types.InlineKeyboardButton("9) 카카오버스 검색", callback_data="kbus_search")],
        ])
        TB.send_message(m.chat.id, "Select category:", reply_markup=kb)

    @TB.message_handler(commands=["bus"])
    def tb_kbus_cmd(m):
        if not allowed(m.from_user.id):
            return TB.reply_to(m, "Not authorized.")
        parts = m.text.split(maxsplit=1)
        if len(parts) < 2:
            return TB.reply_to(m, "사용법: /bus <정류소명 또는 번호>")
        query = parts[1].strip()
        try:
            kbus_apply_search(query)
            TB.reply_to(m, f"검색 적용: {query}")
        except subprocess.CalledProcessError as e:
            TB.reply_to(m, f"실패: {e.output}")
        except Exception as e:
            TB.reply_to(m, f"오류: {e}")

    # /cal merged into /set option 6
    @TB.callback_query_handler(func=lambda c: c.data == "cal_manage")
    def open_cal_manage(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        kb = telebot.types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            telebot.types.InlineKeyboardButton("Add", callback_data="cal_add"),
            telebot.types.InlineKeyboardButton("Edit/Delete", callback_data="cal_edit_delete"),
        )
        kb.add(telebot.types.InlineKeyboardButton("View", callback_data="cal_view"))
        TB.send_message(c.message.chat.id, "Calendar menu:", reply_markup=kb)

    @TB.callback_query_handler(func=lambda c: c.data in ("cfg_ical", "cfg_ghow", "cfg_ha", "noop", "set_verse", "cfg_bus"))
    def on_cb(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        if c.data == "cfg_ical":
            url = CFG["frame"].get("ical_url", "(not set)")
            st = load_state(); st[str(c.from_user.id)] = {"wait": "ical"}; save_state(st)
            TB.answer_callback_query(c.id)
            TB.send_message(c.message.chat.id, f"Current iCal URL:\n{url}\n\nSend a new URL, or /cancel to abort.")
        elif c.data == "cfg_ghow":
            TB.answer_callback_query(c.id)
            if not have_google_libs():
                TB.send_message(c.message.chat.id, "google-* libs missing. pip install google-auth google-auth-oauthlib google-api-python-client"); return
            have_token = GTOKEN_PATH.exists()
            msg = f"Google OAuth: {'connected' if have_token else 'not connected'}\nOpen /oauth/start in the web UI."
            TB.send_message(c.message.chat.id, msg)
        elif c.data == "cfg_ha":
            TB.answer_callback_query(c.id)
            cfg = CFG.setdefault("home_assistant", {})
            base_url = _normalize_base_url(cfg.get("base_url", "")) or "설정안됨"
            token_status = "등록됨" if cfg.get("token") else "미등록"
            kb = telebot.types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                telebot.types.InlineKeyboardButton("베이스 URL 설정", callback_data="ha_set_base_url"),
                telebot.types.InlineKeyboardButton("토큰 설정", callback_data="ha_set_token"),
                telebot.types.InlineKeyboardButton("대시보드 선택", callback_data="ha_choose_dashboard"),
                telebot.types.InlineKeyboardButton("대시보드 해제", callback_data="ha_clear_dashboard"),
                telebot.types.InlineKeyboardButton("현재 설정 보기", callback_data="ha_show_config"),
            )
            msg = (
                "Home Assistant 연동 메뉴입니다.\n"
                f"현재 베이스 URL: {base_url}\n"
                f"토큰: {token_status}"
            )
            TB.send_message(c.message.chat.id, msg, reply_markup=kb)
        elif c.data == "set_verse":
            TB.answer_callback_query(c.id)
            st = load_state(); st[str(c.from_user.id)] = {"mode": "await_verse"}; save_state(st)
            TB.send_message(c.message.chat.id, "input text")
        elif c.data == "cfg_bus":
            TB.answer_callback_query(c.id)
            kb = telebot.types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                telebot.types.InlineKeyboardButton("정류소 변경", callback_data="bus_set_stop"),
                telebot.types.InlineKeyboardButton("서비스키 변경", callback_data="bus_set_key"),
                telebot.types.InlineKeyboardButton("현재설정 조회", callback_data="bus_show_config"),
                telebot.types.InlineKeyboardButton("지정정류소 현황조회", callback_data="bus_test"),
            )
            TB.send_message(c.message.chat.id, "버스 정보 메뉴를 선택하세요:", reply_markup=kb)
        elif c.data == "noop":
            TB.answer_callback_query(c.id, "Coming soon")

    @TB.callback_query_handler(func=lambda c: c.data in ("ha_set_base_url", "ha_set_token", "ha_show_config"))
    def on_cb_home_assistant(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        cfg = CFG.setdefault("home_assistant", {})
        if c.data == "ha_set_base_url":
            current = _normalize_base_url(cfg.get("base_url", "")) or "설정안됨"
            st = load_state(); st[str(c.from_user.id)] = {"mode": "await_ha_base_url"}; save_state(st)
            TB.send_message(
                c.message.chat.id,
                f"현재 베이스 URL: {current}\n새 Home Assistant URL을 입력하세요 (예: https://example.duckdns.org).\n/cancel 로 취소합니다.",
            )
        elif c.data == "ha_set_token":
            masked = _mask_secret(cfg.get("token"))
            st = load_state(); st[str(c.from_user.id)] = {"mode": "await_ha_token"}; save_state(st)
            TB.send_message(
                c.message.chat.id,
                f"현재 토큰: {masked}\nHome Assistant Long-Lived Access Token을 입력하세요.\n/cancel 로 취소합니다.",
            )
        else:
            base_url = _normalize_base_url(cfg.get("base_url", "")) or "설정안됨"
            token_masked = _mask_secret(cfg.get("token"))
            verify = cfg.get("verify_ssl", True)
            timeout = cfg.get("timeout", 5)
            domains = ", ".join(cfg.get("include_domains") or []) or "(없음)"
            entities = ", ".join(cfg.get("include_entities") or []) or "(없음)"
            lines = [
                f"베이스 URL: {base_url}",
                f"토큰: {token_masked}",
                f"SSL 검증: {verify}",
                f"요청 타임아웃: {timeout}",
                f"도메인 필터: {domains}",
                f"엔티티 필터: {entities}",
            ]
            dash = cfg.get("dashboard") or {}
            if dash.get("title") or dash.get("url_path"):
                lines.append(
                    f"대시보드: {dash.get('title') or '(제목 없음)'} ({dash.get('url_path') or '미지정'})"
                )
                if dash.get("entity_count"):
                    lines.append(f"대시보드 연동 엔티티 수: {dash.get('entity_count')}")
            else:
                lines.append("대시보드: (연동 안함)")
            TB.send_message(c.message.chat.id, "\n".join(lines))

    @TB.callback_query_handler(func=lambda c: c.data == "ha_clear_dashboard")
    def on_cb_ha_clear_dashboard(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        cfg = CFG.setdefault("home_assistant", {})
        cfg["include_entities"] = []
        dash_cfg = cfg.setdefault("dashboard", {})
        dash_cfg.clear()
        dash_cfg.update({"url_path": "", "title": ""})
        save_config_to_source(CFG)
        TB.send_message(c.message.chat.id, "대시보드 선택을 해제하고 엔티티 필터를 초기화했습니다.")

    @TB.callback_query_handler(func=lambda c: c.data == "ha_choose_dashboard")
    def on_cb_ha_choose_dashboard(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        try:
            dashboards = home_assistant_list_dashboards()
        except HomeAssistantError as e:
            TB.send_message(c.message.chat.id, f"대시보드 목록 조회 실패: {e}")
            return
        if not dashboards:
            TB.send_message(c.message.chat.id, "사용 가능한 대시보드를 찾지 못했습니다.")
            return
        kb = telebot.types.InlineKeyboardMarkup(row_width=1)
        added = False
        for dash in dashboards:
            url_path = (dash.get("url_path") or "").strip()
            title = dash.get("title") or url_path or dash.get("id") or "(제목 없음)"
            if not url_path:
                continue
            label = f"{title} ({url_path})"
            kb.add(telebot.types.InlineKeyboardButton(label, callback_data=f"ha_dashsel:{url_path}"))
            added = True
        if not added:
            TB.send_message(c.message.chat.id, "선택 가능한 대시보드가 없습니다.")
            return
        TB.send_message(c.message.chat.id, "연동할 대시보드를 선택하세요:", reply_markup=kb)

    @TB.callback_query_handler(func=lambda c: c.data.startswith("ha_dashsel:"))
    def on_cb_ha_dashsel(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        url_path = c.data.split(":", 1)[1]
        try:
            result = home_assistant_apply_dashboard(url_path)
        except HomeAssistantError as e:
            TB.send_message(c.message.chat.id, f"대시보드 적용 실패: {e}")
            return
        title = result.get("title") or url_path
        count = result.get("count")
        msg = f"대시보드 적용 완료: {title}"
        if count is not None:
            msg += f" (엔티티 {count}개)"
        TB.send_message(
            c.message.chat.id,
            msg + "\nHome Control 패널이 새로운 엔티티 목록으로 곧 갱신됩니다.",
        )

    @TB.callback_query_handler(func=lambda c: c.data == "kbus_search")
    def on_cb_kbus_search(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        KBUS_WAIT.add(c.from_user.id)
        TB.send_message(c.message.chat.id, "카카오버스 검색어를 입력하세요.")

    @TB.message_handler(func=lambda m: m.from_user.id in KBUS_WAIT, content_types=["text"])
    def on_kbus_wait(m):
        if not allowed(m.from_user.id):
            return
        query = (m.text or "").strip()
        if not query:
            return
        try:
            kbus_apply_search(query)
            TB.reply_to(m, f"검색 적용: {query}")
        except subprocess.CalledProcessError as e:
            TB.reply_to(m, f"실패: {e.output}")
        except Exception as e:
            TB.reply_to(m, f"오류: {e}")
        finally:
            KBUS_WAIT.discard(m.from_user.id)

    # ---- Bus settings flow
    @TB.callback_query_handler(func=lambda c: c.data in ("bus_set_stop", "bus_set_key", "bus_show_config", "bus_test"))
    def on_cb_bus(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        uid = c.from_user.id
        if c.data == "bus_set_stop":
            key = CFG.get("bus", {}).get("key", "").strip()
            if not key:
                TB.send_message(c.message.chat.id, "서비스키가 설정되지 않았습니다.")
                return
            cities = bb_tago_get_city_list(key)
            st = load_state(); st[str(uid)] = {"mode": "bus_city"}; save_state(st)
            TB.send_message(
                c.message.chat.id,
                "도시를 선택하세요",
                reply_markup=tb_build_city_keyboard(cities, 0),
            )
        elif c.data == "bus_set_key":
            st = load_state(); st[str(uid)] = {"mode": "await_bus_key"}; save_state(st)
            TB.send_message(c.message.chat.id, "서비스키를 입력하세요.")
        elif c.data == "bus_show_config":
            bus = CFG.get("bus", {})
            city = bus.get("city_code", "설정안됨")
            node = bus.get("node_id", "설정안됨")
            key_status = "등록" if bus.get("key") else "미등록"
            msg = [
                f"도시코드: {city}",
                f"노드ID: {node}",
                f"서비스키: {key_status}",
            ]
            TB.send_message(c.message.chat.id, "\n".join(msg))
        elif c.data == "bus_test":
            try:
                box = render_bus_box()
                rows = box.get("rows", [])
                if not rows:
                    TB.send_message(c.message.chat.id, "(정보 없음)")
                    return
                if len(rows) == 1 and rows[0].get("text") and not rows[0].get("route"):
                    TB.send_message(c.message.chat.id, rows[0]["text"])
                    return
                lines = [box.get("title", "버스도착")]
                for it in rows[:10]:
                    lines.append(it.get("text") or f"{it.get('route')} {it.get('eta')} {it.get('hops')}")
                TB.send_message(c.message.chat.id, "\n".join(lines))
            except Exception as e:
                TB.send_message(c.message.chat.id, f"검색 실패: {e}")

    @TB.callback_query_handler(
        func=lambda c: c.data.startswith(
            ("bus_city", "bus_citypage", "bus_stop", "bus_stoppage")
        )
    )
    def on_cb_bus_flow(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        uid = c.from_user.id
        st_all = load_state()
        st = st_all.get(str(uid), {})
        if c.data.startswith("bus_citypage:"):
            page = int(c.data.split(":", 1)[1])
            key = CFG.get("bus", {}).get("key", "")
            cities = bb_tago_get_city_list(key)
            TB.edit_message_reply_markup(
                c.message.chat.id,
                c.message.message_id,
                reply_markup=tb_build_city_keyboard(cities, page),
            )
            return
        if c.data.startswith("bus_city:"):
            code = c.data.split(":", 1)[1]
            st_all[str(uid)] = {"mode": "bus_keyword", "city_code": code}
            save_state(st_all)
            TB.send_message(
                c.message.chat.id,
                "정류소명을 입력하세요",
                reply_markup=telebot.types.ForceReply(selective=False),
            )
            return
        if c.data.startswith("bus_stoppage:"):
            page = int(c.data.split(":", 1)[1])
            stops = st.get("stop_results", [])
            TB.edit_message_reply_markup(
                c.message.chat.id,
                c.message.message_id,
                reply_markup=tb_build_stop_keyboard(stops, page),
            )
            return
        if c.data.startswith("bus_stop:"):
            node = c.data.split(":", 1)[1]
            CFG["bus"]["city_code"] = st.get("city_code", "")
            CFG["bus"]["node_id"] = node
            save_config_to_source(CFG)
            st_all.pop(str(uid), None); save_state(st_all)
            TB.send_message(c.message.chat.id, f"정류소 등록 완료: {node}")
            return

    # ---- Add flow
    @TB.callback_query_handler(func=lambda c: c.data.startswith(("cal_add","add_year","add_month","add_day","end_same_","add_eyear","add_emonth","add_eday")))
    def on_cb_add(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        uid = c.from_user.id
        data = c.data
        if data == "cal_add":
            st = load_state(); st[str(uid)] = {"mode": "add"}; save_state(st)
            ask_year(c.message.chat.id, uid, next_cb="add_year"); TB.answer_callback_query(c.id); return
        if data.startswith("add_year:"):
            y = int(data.split(":")[1])
            st = load_state().get(str(uid), {}); st.update({"year": y}); save_state({**load_state(), str(uid): st})
            ask_month(c.message.chat.id, uid, y, next_cb="add_month"); TB.answer_callback_query(c.id); return
        if data.startswith("add_month:"):
            y, mo = map(int, data.split(":")[1].split(","))
            st = load_state().get(str(uid), {}); st.update({"month": mo}); save_state({**load_state(), str(uid): st})
            ask_day(c.message.chat.id, uid, y, mo, next_cb="add_day"); TB.answer_callback_query(c.id); return
        if data.startswith("add_day:"):
            y, mo, d_ = map(int, data.split(":")[1].split(","))
            st = load_state().get(str(uid), {}); st.update({"day": d_}); save_state({**load_state(), str(uid): st})
            ask_end_same(c.message.chat.id); TB.answer_callback_query(c.id); return
        if data == "end_same_yes":
            st = load_state().get(str(uid), {})
            st["end_year"], st["end_month"], st["end_day"] = st["year"], st["month"], st["day"]
            st["mode"] = "await_content"
            save_state({**load_state(), str(uid): st})
            ask_content(c.message.chat.id); TB.answer_callback_query(c.id); return
        if data == "end_same_no":
            st = load_state().get(str(uid), {}); st["mode"]="add_end_date"; save_state({**load_state(), str(uid): st})
            ask_year(c.message.chat.id, uid, next_cb="add_eyear"); TB.answer_callback_query(c.id); return
        if data.startswith("add_eyear:"):
            y = int(data.split(":")[1])
            st = load_state().get(str(uid), {}); st.update({"end_year": y}); save_state({**load_state(), str(uid): st})
            ask_month(c.message.chat.id, uid, y, next_cb="add_emonth"); TB.answer_callback_query(c.id); return
        if data.startswith("add_emonth:"):
            y, mo = map(int, data.split(":")[1].split(","))
            st = load_state().get(str(uid), {}); st.update({"end_month": mo}); save_state({**load_state(), str(uid): st})
            ask_day(c.message.chat.id, uid, y, mo, next_cb="add_eday"); TB.answer_callback_query(c.id); return
        if data.startswith("add_eday:"):
            y, mo, d_ = map(int, data.split(":")[1].split(","))
            st = load_state().get(str(uid), {}); st.update({"end_day": d_, "mode":"await_content"}); save_state({**load_state(), str(uid): st})
            ask_content(c.message.chat.id); TB.answer_callback_query(c.id); return

    # ---- View/Edit/Delete branches
    @TB.callback_query_handler(func=lambda c: c.data in ("cal_view","cal_edit_delete"))
    def cal_pick_mode(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        chat_id = c.message.chat.id
        if c.data == "cal_view":
            send_event_picker(chat_id, action_prefix="view", max_results=10)
        else:
            kb = telebot.types.InlineKeyboardMarkup(row_width=2)
            kb.add(
                telebot.types.InlineKeyboardButton("Edit title", callback_data="edit_title_start"),
                telebot.types.InlineKeyboardButton("Edit time", callback_data="edit_time_start"),
            )
            kb.add(telebot.types.InlineKeyboardButton("Delete", callback_data="del_start"))
            TB.send_message(chat_id, "Choose action:", reply_markup=kb)

    @TB.callback_query_handler(func=lambda c: c.data in ("edit_title_start","edit_time_start","del_start"))
    def cal_choose_item(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        chat_id = c.message.chat.id
        if c.data == "edit_title_start":
            send_event_picker(chat_id, action_prefix="edit", max_results=10)
        elif c.data == "edit_time_start":
            send_event_picker(chat_id, action_prefix="etime", max_results=10)
        else:
            send_event_picker(chat_id, action_prefix="del", max_results=10)

    # === View picked event
    @TB.callback_query_handler(func=lambda c: c.data.startswith("pick_view:"))
    def on_pick_view(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        ev_id = c.data.split(":",1)[1]
        try:
            ev = load_event(ev_id)
        except Exception as e:
            TB.send_message(c.message.chat.id, f"Load failed: {e}"); return
        title = ev.get("summary") or "(untitled)"
        TB.send_message(c.message.chat.id, f"Title: {title}\n{_fmt_start_end(ev)}\nLocation: {ev.get('location','-')}\nDesc: {ev.get('description','-')}")

    # === Edit title: pick event -> ask new title
    @TB.callback_query_handler(func=lambda c: c.data.startswith("pick_edit:"))
    def on_pick_edit(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        ev_id = c.data.split(":",1)[1]
        st = load_state(); st[str(c.from_user.id)] = {"mode":"await_new_title", "ev_id": ev_id}; save_state(st)
        TB.send_message(c.message.chat.id, "Enter new title. /cancel to abort.")

    # === Edit time: pick event -> ask new times
    @TB.callback_query_handler(func=lambda c: c.data.startswith("pick_etime:"))
    def on_pick_etime(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        ev_id = c.data.split(":",1)[1]
        st = load_state(); st[str(c.from_user.id)] = {"mode":"await_time_edit", "ev_id": ev_id}; save_state(st)
        examples = "e.g. 2025-09-02 14:00~16:00 / 8/30 9~11 / today 15:00~16:00 / 9/1~9/3 (all-day)"
        TB.send_message(c.message.chat.id, f"Enter new time range.\n{examples}\n/cancel to abort.")

    # === Delete: confirm
    @TB.callback_query_handler(func=lambda c: c.data.startswith("pick_del:"))
    def on_pick_del(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        ev_id = c.data.split(":",1)[1]
        kb = telebot.types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            telebot.types.InlineKeyboardButton("Confirm delete", callback_data=f"del_confirm:{ev_id}"),
            telebot.types.InlineKeyboardButton("Cancel", callback_data="noop"),
        )
        TB.send_message(c.message.chat.id, "Delete this event?", reply_markup=kb)

    @TB.callback_query_handler(func=lambda c: c.data.startswith("del_confirm:"))
    def on_del_confirm(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        ev_id = c.data.split(":",1)[1]
        try:
            delete_event(ev_id)
            TB.send_message(c.message.chat.id, "Deleted.")
        except Exception as e:
            TB.send_message(c.message.chat.id, f"Delete failed: {e}")

    @TB.message_handler(commands=["cancel"])
    def cancel(m):
        st = load_state(); st.pop(str(m.from_user.id), None); save_state(st)
        TB.reply_to(m, "Canceled.")

    @TB.message_handler(func=lambda m: True)
    def on_text(m):
        st = load_state().get(str(m.from_user.id))
        if not st: return

        # update iCal url
        if st.get("wait") == "ical":
            new_url = m.text.strip()
            if not (new_url.startswith("http://") or new_url.startswith("https://")):
                TB.reply_to(m, "Invalid URL. Please send http/https URL."); return
            CFG["frame"]["ical_url"] = new_url
            save_config_to_source(CFG)
            global _ical_cache
            _ical_cache = {"url": None, "ts": 0.0, "events": []}
            TB.reply_to(m, "iCal URL updated. Board will auto-refresh in ~1-2 min.")
            allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

        # set verse text
        if st.get("mode") == "await_verse":
            txt = (m.text or "").strip()
            set_verse(txt)
            TB.reply_to(m, "Verse updated.")
            allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

        # bus: keyword -> search stops or accept nodeId directly
        if st.get("mode") in ("bus_keyword", "bus_stop"):
            kw = (m.text or "").strip()
            city = st.get("city_code", "")
            if bb_is_tago_node_id(kw):
                CFG["bus"]["city_code"] = city
                CFG["bus"]["node_id"] = kw
                save_config_to_source(CFG)
                TB.reply_to(m, "변경완료")
                allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
                return
            key = CFG.get("bus", {}).get("key", "")
            stops = bb_tago_search_stops(city, kw, key)
            if not stops:
                TB.reply_to(m, "검색 결과가 없습니다. 다시 입력해주세요.")
                return
            st.update({"mode": "bus_stop", "stop_results": stops})
            save_state({**load_state(), str(m.from_user.id): st})
            TB.send_message(
                m.chat.id,
                "정류소를 선택하세요",
                reply_markup=tb_build_stop_keyboard(stops, 0),
            )
            return

        # bus: api key
        if st.get("mode") == "await_bus_key":
            val = (m.text or "").strip()
            CFG["bus"]["key"] = val
            save_config_to_source(CFG)
            TB.reply_to(m, "변경완료")
            allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

        # home assistant: base url
        if st.get("mode") == "await_ha_base_url":
            new_url = (m.text or "").strip()
            if not new_url:
                TB.reply_to(m, "빈 값은 저장할 수 없습니다. 다시 입력하거나 /cancel 로 취소하세요."); return
            if not re.match(r"^https?://", new_url, re.IGNORECASE):
                TB.reply_to(m, "http:// 또는 https:// 로 시작하는 URL을 입력하세요."); return
            normalized = _normalize_base_url(new_url)
            if not normalized:
                TB.reply_to(m, "유효한 URL을 입력해주세요."); return
            cfg = CFG.setdefault("home_assistant", {})
            cfg["base_url"] = normalized
            save_config_to_source(CFG)
            TB.reply_to(m, f"Home Assistant URL 저장 완료: {normalized}")
            allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

        # home assistant: token
        if st.get("mode") == "await_ha_token":
            token = (m.text or "").strip()
            if not token:
                TB.reply_to(m, "빈 값은 저장할 수 없습니다. 다시 입력하거나 /cancel 로 취소하세요."); return
            cfg = CFG.setdefault("home_assistant", {})
            cfg["token"] = token
            save_config_to_source(CFG)
            TB.reply_to(m, f"토큰 저장 완료: {_mask_secret(token)}")
            allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

        # add flow: get title
        if st.get("mode") == "await_content":
            st["title"] = m.text.strip()
            save_state({**load_state(), str(m.from_user.id): st})
            create_event_via_google(m.chat.id, m.from_user.id)
            return

        # edit title
        if st.get("mode") == "await_new_title":
            new_title = (m.text or "").strip()
            if not new_title:
                TB.reply_to(m, "Empty title. Please enter again or /cancel"); return
            ev_id = st.get("ev_id")
            try:
                patch_event(ev_id, summary=new_title)
                TB.reply_to(m, "Title updated.")
            except Exception as e:
                TB.reply_to(m, f"Update failed: {e}")
            finally:
                allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

        # edit time
        if st.get("mode") == "await_time_edit":
            ev_id = st.get("ev_id")
            try:
                parsed = parse_when_range(m.text)
            except Exception as e:
                TB.reply_to(m, f"Parse error: {e}\nExample: 2025-09-02 14:00~16:00 / 8/30 9~11 / today 15:00~16:00"); return
            try:
                if parsed["kind"] == "timed":
                    sd = parsed["start_dt"]; ed = parsed["end_dt"]
                    body = {
                        "start": {"dateTime": sd.isoformat(), "timeZone": TZ_NAME},
                        "end":   {"dateTime": ed.isoformat(), "timeZone": TZ_NAME},
                    }
                else:
                    sd = parsed["start_date"]; ed_incl = parsed["end_date"]
                    body = {
                        "start": {"date": sd.isoformat()},
                        "end":   {"date": (ed_incl + timedelta(days=1)).isoformat()},
                    }
                patch_event(ev_id, **body)
                TB.reply_to(m, "Time updated.")
            except Exception as e:
                TB.reply_to(m, f"Update failed: {e}")
            finally:
                allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

# === [SECTION: Telegram start (webhook or polling) + duplication guard] ======
# - 파일락(/tmp/scal_bot.lock)으로 중복 폴링 방지 (다중 토큰/다중 인스턴스 보호)
_lock_file = None
def start_telegram():
    """Start telegram in single-instance mode using a file lock."""
    global _lock_file
    if not TB:
        print("[TG] Telegram not configured (no bot token).")
        return
    # acquire lock file to avoid double polling
    try:
        _lock_file = open("/tmp/scal_bot.lock", "w")
        fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_file.write(str(os.getpid()))
        _lock_file.flush()
    except Exception:
        print("[TG] Another instance is already running. Skipping telegram start.")
        return

    mode = CFG["telegram"].get("mode", "polling")
    if mode == "webhook":
        base = CFG["telegram"].get("webhook_base", "").rstrip("/")
        if not base:
            print("[TG] webhook mode, but webhook_base missing; fallback to polling")
            return start_polling()
        secret = CFG["telegram"].get("path_secret") or secrets.token_urlsafe(24)
        CFG["telegram"]["path_secret"] = secret
        save_config_to_source(CFG)
        hook_url = f"{base}/tg/{secret}"
        TB.remove_webhook()
        TB.set_webhook(url=hook_url, drop_pending_updates=True)
        print(f"[TG] Telegram webhook set: {hook_url}")

        @app.post(f"/tg/{secret}")
        def tg_webhook():
            if request.headers.get("content-type") != "application/json":
                abort(403)
            update = telebot.types.Update.de_json(request.get_data().decode("utf-8"))
            TB.process_new_updates([update])
            return "OK"
    else:
        start_polling()

def start_polling():
    TB.remove_webhook()
    print("[TG] Telegram polling started")
    TB.infinity_polling(timeout=60, long_polling_timeout=60, allowed_updates=["message", "callback_query"])

# === [SECTION: Google OAuth routes (home/start/callback/test)] ===============
HOME_HTML = r"""
<!doctype html><meta charset="utf-8">
<title>SCAL Home</title>
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,'Noto Sans KR',sans-serif;padding:24px;line-height:1.6}</style>
<h2>SCAL  Smart Calendar</h2>
<ul>
  <li><a href="/board" target="_blank">Open Board (/board)</a></li>
  <li><a href="/oauth/start">Start Google OAuth</a>  Calendar features</li>
  <li>Home Assistant 연동: <code>scal_full_integrated.py</code> 상단 EMBEDDED_CONFIG에서 <code>home_assistant.base_url</code>·<code>token</code>을 입력하고 서비스를 재시작하세요.</li>
</ul>
<hr>
<p>Status:
  <b>Google libs</b> : {{ 'OK' if google_ok else 'install required' }}<br>
  <b>Google token</b> : {{ 'connected' if token_ok else 'not connected' }}</p>
<p>Files:
  <code>{{ base }}/google_client_secret.json</code> (manual),
  <code>{{ base }}/google_token.json</code> (auto)
</p>
"""

@app.get("/")
def home():
    return render_template_string(
        HOME_HTML,
        google_ok=have_google_libs(),
        token_ok=GTOKEN_PATH.exists(),
        base=str(BASE),
    )

@app.get("/oauth/start")
def oauth_start():
    if not have_google_libs():
        return "google-* libs missing. pip install google-auth google-auth-oauthlib google-api-python-client", 500
    if not GCLIENT_PATH.exists():
        return f"Client secret file missing: {GCLIENT_PATH}", 500
    redirect_uri = request.url_root.rstrip("/") + "/oauth/callback"
    flow = Flow.from_client_secrets_file(str(GCLIENT_PATH), scopes=CFG["google"]["scopes"], redirect_uri=redirect_uri)
    auth_url, state = flow.authorization_url(access_type="offline", include_granted_scopes="true", prompt="consent")
    resp = make_response(redirect(auth_url))
    resp.set_cookie("oauth_state", state, max_age=600, httponly=True, samesite="None", secure=True)
    return resp

@app.get("/oauth/callback")
def oauth_callback():
    if not have_google_libs():
        return "google-* libs missing.", 500
    if not GCLIENT_PATH.exists():
        return f"Client secret file missing: {GCLIENT_PATH}", 500
    state_cookie = request.cookies.get("oauth_state")
    state_param = request.args.get("state")
    if not state_cookie or state_cookie != state_param:
        return "OAuth state mismatch.", 400
    redirect_uri = request.url_root.rstrip("/") + "/oauth/callback"
    flow = Flow.from_client_secrets_file(str(GCLIENT_PATH), scopes=CFG["google"]["scopes"], redirect_uri=redirect_uri)
    try:
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        save_google_creds(creds)
    except Exception as e:
        return f"Token exchange failed: {e}", 400
    return redirect(url_for("home"))

@app.post("/oauth/test-insert")
def oauth_test_insert():
    try:
        svc = get_google_service()
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    js = request.get_json(force=True, silent=True) or {}
    summary = js.get("summary") or "Test Event"
    start = js.get("start") or datetime.now(TZ).date().isoformat()
    end   = js.get("end") or start
    body = {"summary": summary, "start": {"date": start}, "end": {"date": end}}
    try:
        cal_id = CFG["google"]["calendar"]["id"]
        ev = svc.events().insert(calendarId=cal_id, body=body).execute()
        return jsonify({"ok": True, "id": ev.get("id")})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

# === [SECTION: App entrypoints (web thread + telegram)] ======================
def run_web():
    # debug=False, use_reloader=False to prevent reloader double-start
    try:
        app.run(host="0.0.0.0", port=int(CFG["server"]["port"]), debug=False, use_reloader=False)
    except OSError:
        print("Address already in use")
        raise

def main():
    threading.Thread(target=kbus_scrape_loop, daemon=True).start()
    t = threading.Thread(target=run_web, daemon=True); t.start()
    print(f"[WEB] started on :{CFG['server']['port']}  -> /board")
    start_telegram()


def kbus_entry(argv: List[str]) -> None:
    if argv and argv[0] == "search":
        if len(argv) < 2:
            print("Usage: kbus search '<정류소명 또는 번호>'")
            return
        kbus_apply_search(argv[1])
        return
    kbus_run_bot()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "kbus":
        kbus_entry(sys.argv[2:])
    else:
        main()
