

# ======= EMBEDDED BLOCKS (auto-managed by Telegram commands) ===============
# ==== EMBEDDED_CONFIG (YAML) START
EMBEDDED_CONFIG = r"""server:
  port: 5320

frame:
  tz: Asia/Seoul
  ical_url: https://calendar.google.com/calendar/ical/bob.gondrae%40gmail.com/private-00822d9dbbe3140b9253bf2e0bda95c6/basic.ics

weather:
  provider: openweathermap
  api_key: 9809664c22a3501382380f2781e1a9da
  location: Seoul, South Korea
  units: metric

telegram:
  bot_token: 7523443246:AAF-fHGcw4NLgDQDRbDz7j1xOTEFYfeZPQ0
  allowed_user_ids:
    - 5517670242
  mode: polling
  webhook_base: ''
  path_secret: ''

google:
  scopes:
    - https://www.googleapis.com/auth/calendar
  calendar:
    id: bob.gondrae@gmail.com

todoist:
  api_token: "0aa4d2a4f95e952a1f635c14d6c6ba7e3b26bc2b"

# ===== BusInfo (단일 프로필, 텔레그램에서 station_id 입력) =====
bus:
  region: seoul            # seoul | gyeonggi
  seoul:
    api_key: "95b2c4966eeb698ed2db22bf5eb6d753c8e106e6cfbd171fa94306d46287a265"
    ars_id: "39516"        # 서울은 arsId
  gyeonggi:
    api_key: "95b2c4966eeb698ed2db22bf5eb6d753c8e106e6cfbd171fa94306d46287a265"
    station_id: "200000078"
  max_items: 8
  routes_whitelist: ["14-1","47","62","532","1302"]     # ["7016","M7106"] 처럼 문자열로!
"""
# ==== EMBEDDED_CONFIG (YAML) END

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

+ Todoist: fetch tasks and render in 2 columns (7 items each)
+ Verse: /set -> verse input that shows on board
+ Bot duplication guard (file lock) to avoid double polling
+ Bus (Seoul/Gyeonggi): real APIs + stop change via Telegram + board display
"""

# Code below is organized with clearly marked sections.
# Search for lines like `# === [SECTION: ...] ===` to navigate.

# === [SECTION: Imports / Standard & Third-party] ==============================
import os, json, time, secrets, threading, collections, re, socket, fcntl, xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta, date

import yaml
import requests
from flask import Flask, request, jsonify, render_template_string, abort, send_from_directory, redirect, url_for, make_response
from werkzeug.middleware.proxy_fix import ProxyFix
import telebot
# ======= Embedded-block helpers (final) ======================================
import io, tempfile, os, yaml

CFG_START = "# ==== EMBEDDED_CONFIG (YAML) START"
CFG_END   = "# ==== EMBEDDED_CONFIG (YAML) END"
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
    data = yaml.safe_load(_read_block(CFG_START, CFG_END)) or {}
    def deep_fill(dst, src):
        for k, v in src.items():
            if k not in dst:
                dst[k] = v
            elif isinstance(v, dict):
                dst[k] = deep_fill(dst.get(k, {}) or {}, v)
        return dst
    return deep_fill(data, defaults)

def save_config_to_source(new_yaml_text: str, file_path: str = __file__):
    _write_block(new_yaml_text, CFG_START, CFG_END, file_path=file_path)

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
CFG_PATH = BASE / "sframe.yaml"
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
    # Todoist (yaml에서 설정) — 여기 값은 기본값
    "todoist": {
        "api_token": "",                   # yaml에 넣은 토큰 사용; 비어있으면 비활성
        "filter": "today | overdue",       # Todoist filter query
        "project_id": "",                  # optional: limit to project
        "max_items": 20                    # UI는 좌10/우10
    },
    # Bus (서울/경기) 설정
    "bus": {
        "region": "seoul",                 # seoul | gyeonggi
        "seoul":   {"api_key": "", "ars_id": ""},        # ars_id 예: "02139"
        "gyeonggi":{"api_key": "", "station_id": ""},    # station_id 예: "200000078"
        "max_items": 8,
        "routes_whitelist": []             # 예: ["7016","M7106"] 비워두면 전체
    }
}
CFG = load_config_from_embedded(DEFAULT_CFG)

# === [SECTION: YAML loader + default writer] ===============================
def load_yaml(p: Path, defaults: dict):
    """Load YAML with defaults; write default file if missing."""
    if not p.exists():
        p.write_text(yaml.safe_dump(defaults, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print(f"[CFG] Default config created: {p}  (edit as needed)")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    def deep_fill(dst, src):
        for k, v in src.items():
            if k not in dst:
                dst[k] = v
            elif isinstance(v, dict):
                dst[k] = deep_fill(dst.get(k, {}) or {}, v)
        return dst
    return deep_fill(data, defaults)

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
    cur_data = {"temp": round(cur.get("temp", 0)), "icon": icon_url(cur.get("weather", [{}])[0].get("icon", "01d"))}
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
    cur_data = {"temp": round(cur.get("main", {}).get("temp", 0)), "icon": icon_url(cur.get("weather", [{}])[0].get("icon", "01d"))}
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
    aqi = ((js.get("list") or [{}])[0].get("main") or {}).get("aqi")
    labels = {1: "Good", 2: "Fair", 3: "Moderate", 4: "Poor", 5: "Very Poor"}
    colors = {1: "#009966", 2: "#ffde33", 3: "#ff9933", 4: "#cc0033", 5: "#660099"}
    data = {"aqi": aqi, "label": labels.get(aqi, "?"), "color": colors.get(aqi, "#fff")}
    _air_cache.update({"key": key, "loc": loc, "ts": now, "data": data})
    return data

# === [SECTION: Bus (Seoul/Gyeonggi) API adapters] ============================
def _as_int(x, default=None):
    try:
        return int(x)
    except Exception:
        return default

def _extract_min_from_msg(msg: str):
    """한국어 도착메시지에서 '분' 앞의 숫자 추출."""
    if not msg: return None
    m = re.search(r"(\d+)\s*분", msg)
    return _as_int(m.group(1)) if m else None

def fetch_bus_seoul(api_key: str, ars_id: str, whitelist=None, max_items=8):
    """
    서울시 버스 정류소(arsId) 도착정보
    API: http://ws.bus.go.kr/api/rest/stationinfo/getStationByUid?serviceKey=...&arsId=...&_type=json
    Fallback: XML 파싱
    """
    base = "http://ws.bus.go.kr/api/rest/stationinfo/getStationByUid"
    params = {"serviceKey": api_key, "arsId": ars_id, "_type": "json"}
    stop_name = ""
    items = []
    try:
        r = requests.get(base, params=params, timeout=10); r.raise_for_status()
        js = r.json()
        body = (js.get("msgBody") or
                (js.get("ServiceResult", {}).get("msgBody") if isinstance(js.get("ServiceResult"), dict) else None))
        arr = (body or {}).get("itemList") or []
        if isinstance(arr, dict): arr = [arr]
        for it in arr:
            rt = it.get("rtNm") or it.get("busRouteNm") or ""
            if whitelist and rt not in whitelist: continue
            msg1 = it.get("arrmsg1") or ""
            msg2 = it.get("arrmsg2") or ""
            dest = it.get("adirection") or it.get("dir") or ""
            stop_name = it.get("stNm") or stop_name
            items.append({
                "route": rt,
                "to": dest,
                "msg1": msg1,
                "msg2": msg2,
                "min1": _extract_min_from_msg(msg1),
                "min2": _extract_min_from_msg(msg2),
            })
    except ValueError:
        # JSON 파싱 실패 -> XML 시도
        rx = requests.get(base, params={"serviceKey": api_key, "arsId": ars_id}, timeout=10); rx.raise_for_status()
        root = ET.fromstring(rx.text)
        for it in root.iterfind(".//itemList"):
            rt = (it.findtext("rtNm") or it.findtext("busRouteNm") or "")
            if whitelist and rt not in whitelist: continue
            msg1 = it.findtext("arrmsg1") or ""
            msg2 = it.findtext("arrmsg2") or ""
            dest = it.findtext("adirection") or it.findtext("dir") or ""
            stnm = it.findtext("stNm") or ""
            if stnm: stop_name = stnm
            items.append({
                "route": rt, "to": dest, "msg1": msg1, "msg2": msg2,
                "min1": _extract_min_from_msg(msg1), "min2": _extract_min_from_msg(msg2),
            })
    # 정렬: 예상 도착 분(min1) 오름차순, None은 뒤로
    items.sort(key=lambda x: (9999 if x["min1"] is None else x["min1"], x["route"]))
    return {"region": "seoul", "stop_name": stop_name or f"arsId {ars_id}", "items": items[:max_items]}

def fetch_bus_gyeonggi(api_key: str, station_id: str, whitelist=None, max_items=8):
    """
    경기도 정류소(stationId) 도착정보
    API: https://apis.data.go.kr/6410000/busarrivalservice/getBusArrivalList?serviceKey=...&stationId=...&resultType=json
    Fallback: XML 파싱
    """
    base = "https://apis.data.go.kr/6410000/busarrivalservice/getBusArrivalList"
    params = {"serviceKey": api_key, "stationId": station_id, "resultType": "json"}
    stop_name = ""
    items = []
    try:
        r = requests.get(base, params=params, timeout=10); r.raise_for_status()
        js = r.json()
        body = (js.get("response") or {}).get("msgBody") or {}
        # 일부 엔드포인트는 배열을 바로 내보내기도 함
        arr = body.get("busArrivalList") or body.get("busArrivalResult") or body.get("itemList") or body
        if isinstance(arr, dict):
            arr = [arr]
        for it in arr:
            # 노선 명칭
            rt = it.get("routeName") or it.get("routeNo") or it.get("routeNumber") or ""
            if not rt and it.get("routeId"):
                rt = str(it.get("routeId"))
            if whitelist and rt not in whitelist: continue
            # 예측 분
            p1 = _as_int(it.get("predictTime1")) if it.get("predictTime1") is not None else None
            p2 = _as_int(it.get("predictTime2")) if it.get("predictTime2") is not None else None
            # 행선지
            dest = it.get("staOrder") or it.get("direction") or ""
            # 메시지 구성
            msg1 = "곧 도착" if p1 == 0 else (f"{p1}분" if p1 is not None else "")
            msg2 = "" if p2 is None else (f"{p2}분" if p2 > 0 else "곧 도착")
            # 정류소명은 이 API에서 직접 안줄 수 있어 공백 유지
            items.append({"route": rt, "to": dest, "msg1": msg1, "msg2": msg2, "min1": p1, "min2": p2})
    except ValueError:
        # JSON 파싱 실패 -> XML 시도
        rx = requests.get(base, params={"serviceKey": api_key, "stationId": station_id}, timeout=10); rx.raise_for_status()
        root = ET.fromstring(rx.text)
        for it in root.iterfind(".//busArrivalList"):
            rt = it.findtext("routeName") or it.findtext("routeNo") or ""
            if not rt:
                rid = it.findtext("routeId")
                if rid: rt = rid
            if whitelist and rt not in whitelist: continue
            p1 = _as_int(it.findtext("predictTime1"))
            p2 = _as_int(it.findtext("predictTime2"))
            dest = it.findtext("direction") or it.findtext("staOrder") or ""
            msg1 = "곧 도착" if p1 == 0 else (f"{p1}분" if p1 is not None else "")
            msg2 = "" if p2 is None else (f"{p2}분" if p2 > 0 else "곧 도착")
            items.append({"route": rt, "to": dest, "msg1": msg1, "msg2": msg2, "min1": p1, "min2": p2})
    items.sort(key=lambda x: (9999 if x["min1"] is None else x["min1"], x["route"]))
    return {"region": "gyeonggi", "stop_name": stop_name or f"stationId {station_id}", "items": items[:max_items]}

def fetch_bus():
    cfg = CFG.get("bus", {}) or {}
    region = (cfg.get("region") or "seoul").lower()
    wl = cfg.get("routes_whitelist") or []
    max_items = int(cfg.get("max_items", 8))
    if region == "seoul":
        key = (cfg.get("seoul") or {}).get("api_key", "").strip()
        ars = (cfg.get("seoul") or {}).get("ars_id", "").strip()
        if not key or not ars:
            return {"need_config": True, "region": "seoul"}
        return fetch_bus_seoul(key, ars, whitelist=wl, max_items=max_items)
    else:
        key = (cfg.get("gyeonggi") or {}).get("api_key", "").strip()
        sid = (cfg.get("gyeonggi") or {}).get("station_id", "").strip()
        if not key or not sid:
            return {"need_config": True, "region": "gyeonggi"}
        return fetch_bus_gyeonggi(key, sid, whitelist=wl, max_items=max_items)

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
    Respects filter/project_id; returns trimmed fields up to max_items (default 14).
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
        data = fetch_bus()
        return jsonify(data)
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
  :root { --W:1080px; --H:1920px; --top:90px; --cal:910px;
          --bus:280px; --weather:280px; --todo:360px; } /* todo -100px */

  /* Global layout */
  html,body { margin:0; padding:0; background:transparent; color:#fff; font-family:system-ui,-apple-system,Roboto,'Noto Sans KR',sans-serif; }
  .frame { width:var(--W); height:var(--H); margin:0 auto; display:flex; flex-direction:column; position:relative; }

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

  .cal { height:var(--cal); padding:8px 20px; box-sizing:border-box; display:flex; flex-direction:column; }
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

  .section { height: calc(var(--H) - var(--top) - var(--cal)); padding:10px 24px; box-sizing:border-box; display:flex; flex-direction:column; gap:10px; }

  .blk { background:rgba(0,0,0,.35); border:1px solid rgba(255,255,255,.08); border-radius:12px; padding:10px 12px; }
  .blk h3 { margin:0 0 6px 0; font-size:16px; opacity:.95; text-shadow:0 0 6px rgba(0,0,0,.65);}

.todo{ flex:1 1 var(--todo); display:flex; flex-direction:column;}
  .todo .rows { display:grid; grid-template-columns: 1fr 1fr; gap:8px; }
  .todo .col { display:flex; flex-direction:column; gap:6px; min-width:0; }
  .todo .item { display:flex; justify-content:flex-start; gap:10px; font-size:14px; }
  .todo .title { flex:1 1 auto; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .todo .due { opacity:.9; min-width:50px; margin-right:12px; }

  .bus{flex:0 0 var(--bus);}
  .bus .rows { display:grid; grid-template-columns: repeat(2, 1fr); gap:8px; }
  .bus .item { display:flex; justify-content:space-between; gap:10px; font-size:14px; background:rgba(0,0,0,.35); border-radius:8px; padding:6px 8px; }
  .bus .route { font-weight:700; min-width:48px; }
  .bus .msgs { display:flex; gap:12px; opacity:.95; }
  .bus .meta { opacity:.9; font-size:13px; }

  /* Verse block */
  .verse { flex:0 0 100px; display:flex; align-items:flex-start; gap:12px; }
  .verse .text { white-space:pre-wrap; line-height:1.4; font-size:16px; text-shadow:0 0 6px rgba(0,0,0,.65); }

/* Weather layout (card style 5-day forecast) */
.weather {
  display:flex;
  gap:16px;
  align-items:stretch;
}
.weather .w-now {
  display:flex;
  align-items:center;
  gap:12px;
  min-width:180px;
}
.weather .w-now .temp { font-size:44px; font-weight:800; line-height:1; }

.weather .w-days {
  display:grid;
  grid-template-columns:repeat(5,1fr);
  gap:12px;
  width:100%;
  align-items:stretch;
  flex:1 1 auto;
}
.weather .w-day {
  text-align:center;
  background:rgba(0,0,0,.25);
  border:1px solid rgba(255,255,255,.08);
  border-radius:12px;
  padding:10px 6px;
  min-width:0;
}
.weather .w-day.today { outline:2px solid rgba(255,255,255,.35); outline-offset:-2px; }
.weather .w-day img { width:72px; height:72px; display:block; margin:6px auto; }
.weather .w-day .temps { display:flex; justify-content:center; gap:8px; font-size:14px; margin-top:4px; }
.weather .w-day .hi { font-weight:800; font-size:16px; }
.weather .w-day .lo { opacity:.75; font-size:14px; }


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
    <div class="verse blk"><h3 style="margin-right:8px">Today's Verse</h3><div id="verse" class="text"> </div></div>
    <div class="todo blk">
      <h3>Todo</h3>
      <div class="rows">
        <div class="col" id="todo-col-1"></div>
        <div class="col" id="todo-col-2"></div>
      </div>
    </div>
    <div class="bus blk">
      <h3>Bus</h3>
      <div class="meta" id="bus-meta"></div>
      <div class="rows" id="bus-rows"></div>
    </div>
    <div class="weather blk" id="weather"></div>
    <div class="aqi blk" id="aqi"></div>
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
    const i = document.createElement('img');
    i.src = data.current.icon; i.alt = ''; i.style.width='70px'; i.style.height='70px';
    const t = document.createElement('div');
    t.className = 'temp';
    t.textContent = data.current.temp + '°';
    now.appendChild(i); now.appendChild(t);

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

    if (air && !air.error && !air.need_config) {
      idx.textContent = air.aqi != null ? String(air.aqi) : '?';
      lbl.textContent = air.label || '';
      if (air.color) {
        aqiCard.style.boxShadow = `inset 0 0 0 2px ${air.color}`;
        aqiCard.style.color = '#fff';
      }
    } else {
      idx.textContent = '–';
      lbl.textContent = 'n/a';
    }
    aqiCard.appendChild(ttl); aqiCard.appendChild(idx); aqiCard.appendChild(lbl);

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

// ===== Todo block (Todoist, 2 columns, 7 each) =====
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

// ===== Bus block =====
async function loadBus(){
  const meta = document.getElementById('bus-meta');
  const rows = document.getElementById('bus-rows');
  meta.textContent = ''; rows.innerHTML = '';
  try{
    const r = await fetch('/api/bus');
    const data = await r.json();
    if (data.need_config){
      meta.textContent = 'Bus config required';
      return;
    }
    if (data.error){
      meta.textContent = 'Bus error';
      return;
    }
    const region = data.region==='seoul'?'Seoul':'Gyeonggi';
    meta.textContent = `${region} · ${data.stop_name||''}`;

    const arr = data.items || [];
    if (!arr.length){
      const d = document.createElement('div'); d.textContent='No bus data.';
      rows.appendChild(d);
      return;
    }
    for(const it of arr){
      const row = document.createElement('div'); row.className='item';
      const left = document.createElement('div'); left.className='route'; left.textContent = it.route || '-';
      const right = document.createElement('div'); right.className='msgs';
      const m1 = document.createElement('div'); m1.textContent = it.msg1 || '';
      const m2 = document.createElement('div'); m2.textContent = it.msg2 || '';
      right.appendChild(m1); if (it.msg2) right.appendChild(m2);
      row.appendChild(left); row.appendChild(right);
      rows.appendChild(row);
    }
  }catch(e){
    meta.textContent = 'Bus load failed';
  }
}
loadBus(); setInterval(loadBus, 15*1000);

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
            [telebot.types.InlineKeyboardButton("4) Bus (view/change)", callback_data="cfg_bus")],
        ])
        TB.send_message(m.chat.id, "Smart Frame Settings", reply_markup=kb)

    @TB.message_handler(commands=["set"])
    def set_cmd(m):
        if not allowed(m.from_user.id):
            return TB.reply_to(m, "Not authorized.")
        kb = kb_inline([
            [telebot.types.InlineKeyboardButton("1) calendar", callback_data="cfg_ical")],
            [telebot.types.InlineKeyboardButton("2) google oauth status", callback_data="cfg_ghow")],
            [telebot.types.InlineKeyboardButton("3) businfo (view/change)", callback_data="cfg_bus")],
            [telebot.types.InlineKeyboardButton("4) photo (later)", callback_data="noop")],
            [telebot.types.InlineKeyboardButton("5) weather (later)", callback_data="noop")],
            [telebot.types.InlineKeyboardButton("6) manage events", callback_data="cal_manage")],
            [telebot.types.InlineKeyboardButton("7) verse", callback_data="set_verse")],
        ])
        TB.send_message(m.chat.id, "Select category:", reply_markup=kb)

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

    @TB.callback_query_handler(func=lambda c: c.data in ("cfg_ical", "cfg_ghow", "noop", "set_verse", "cfg_bus"))
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
        elif c.data == "set_verse":
            TB.answer_callback_query(c.id)
            st = load_state(); st[str(c.from_user.id)] = {"mode": "await_verse"}; save_state(st)
            TB.send_message(c.message.chat.id, "input text")
        elif c.data == "cfg_bus":
            TB.answer_callback_query(c.id)
            bus = CFG.get("bus", {})
            rgn = bus.get("region", "seoul")
            s_se = bus.get("seoul", {}) or {}
            s_gg = bus.get("gyeonggi", {}) or {}
            msg = [
                "* Bus config *",
                f"- region : {rgn}",
                f"- seoul.ars_id : {s_se.get('ars_id','')}",
                f"- gyeonggi.station_id : {s_gg.get('station_id','')}",
                f"- routes_whitelist : {', '.join(bus.get('routes_whitelist', [])) or '(all)'}",
                "",
                "Choose an action:",
            ]
            kb = telebot.types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                telebot.types.InlineKeyboardButton("Change region (seoul/gyeonggi)", callback_data="bus_set_region"),
                telebot.types.InlineKeyboardButton("Change stop id (arsId / stationId)", callback_data="bus_set_stop"),
                telebot.types.InlineKeyboardButton("Set routes filter", callback_data="bus_set_routes"),
                telebot.types.InlineKeyboardButton("Test fetch", callback_data="bus_test"),
            )
            TB.send_message(c.message.chat.id, "\n".join(msg), reply_markup=kb, parse_mode="Markdown")
        elif c.data == "noop":
            TB.answer_callback_query(c.id, "Coming soon")

    # ---- Bus settings flow
    @TB.callback_query_handler(func=lambda c: c.data in ("bus_set_region","bus_set_stop","bus_set_routes","bus_test"))
    def on_cb_bus(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "Not authorized."); return
        TB.answer_callback_query(c.id)
        uid = c.from_user.id
        if c.data == "bus_set_region":
            st = load_state(); st[str(uid)] = {"mode":"await_bus_region"}; save_state(st)
            TB.send_message(c.message.chat.id, "Type region: seoul or gyeonggi. (/cancel to abort)")
        elif c.data == "bus_set_stop":
            rgn = (CFG.get("bus", {}) or {}).get("region", "seoul")
            st = load_state(); st[str(uid)] = {"mode":"await_bus_stop"}; save_state(st)
            if rgn == "seoul":
                TB.send_message(c.message.chat.id, "Enter Seoul arsId (e.g., 02139). (/cancel to abort)")
            else:
                TB.send_message(c.message.chat.id, "Enter Gyeonggi stationId (e.g., 200000078). (/cancel to abort)")
        elif c.data == "bus_set_routes":
            st = load_state(); st[str(uid)] = {"mode":"await_bus_routes"}; save_state(st)
            TB.send_message(c.message.chat.id, "Enter route numbers separated by spaces or commas (e.g., 7016 M7106). Send * to clear. (/cancel to abort)")
        elif c.data == "bus_test":
            try:
                data = fetch_bus()
                if data.get("need_config"):
                    TB.send_message(c.message.chat.id, "Bus config incomplete.")
                    return
                lines = [f"[{data['region']}] {data.get('stop_name','')}"]
                for it in data.get("items", [])[:10]:
                    lines.append(f"{it['route']}: {it.get('msg1','')} {('/ '+it['msg2']) if it.get('msg2') else ''}")
                if len(lines)==1: lines.append("(no items)")
                TB.send_message(c.message.chat.id, "\n".join(lines))
            except Exception as e:
                TB.send_message(c.message.chat.id, f"Fetch failed: {e}")

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
            save_config_to_source(yaml.safe_dump(CFG, allow_unicode=True, sort_keys=False))
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

        # bus: region
        if st.get("mode") == "await_bus_region":
            val = (m.text or "").strip().lower()
            if val not in ("seoul","gyeonggi"):
                TB.reply_to(m, "Invalid. Type seoul or gyeonggi."); return
            CFG["bus"]["region"] = val
            save_config_to_source(yaml.safe_dump(CFG, allow_unicode=True, sort_keys=False))
            TB.reply_to(m, f"Bus region set to {val}.")
            allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

        # bus: stop id
        if st.get("mode") == "await_bus_stop":
            val = (m.text or "").strip()
            rgn = CFG.get("bus",{}).get("region","seoul")
            if rgn == "seoul":
                CFG["bus"]["seoul"]["ars_id"] = val
            else:
                CFG["bus"]["gyeonggi"]["station_id"] = val
            save_config_to_source(yaml.safe_dump(CFG, allow_unicode=True, sort_keys=False))
            TB.reply_to(m, f"Bus stop updated ({rgn}).")
            allst = load_state(); allst.pop(str(m.from_user.id), None); save_state(allst)
            return

        # bus: routes whitelist
        if st.get("mode") == "await_bus_routes":
            txt = (m.text or "").strip()
            if txt == "*":
                CFG["bus"]["routes_whitelist"] = []
            else:
                parts = [x.strip() for x in re.split(r"[,\s]+", txt) if x.strip()]
                CFG["bus"]["routes_whitelist"] = parts
            save_config_to_source(yaml.safe_dump(CFG, allow_unicode=True, sort_keys=False))
            TB.reply_to(m, "Routes filter updated.")
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
        save_config_to_source(yaml.safe_dump(CFG, allow_unicode=True, sort_keys=False))
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
    t = threading.Thread(target=run_web, daemon=True); t.start()
    print(f"[WEB] started on :{CFG['server']['port']}  -> /board")
    start_telegram()

if __name__ == "__main__":
    main()
