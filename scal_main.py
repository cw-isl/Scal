#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smart Frame application with Telegram assistant and simplified features."""

from __future__ import annotations

# Code below is organized with clearly marked sections.
# Search for lines like `# === [SECTION: ...] ===` to navigate.

# === [SECTION: Imports / Standard & Third-party] ==============================
import os, time, secrets, threading, re, fcntl, xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
import html
import logging
from flask import Flask, request, jsonify, render_template_string, abort, send_from_directory
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
try:  # Telegram bot integration is optional
    import telebot
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    telebot = None  # type: ignore[assignment]

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bus")
from scal_app.config import (
    CFG,
    TZ,
    TZ_NAME,
    BASE,
    PHOTOS_DIR,
    get_verse,
    set_verse,
    save_config_to_source,
    load_state,
    save_state,
)
from scal_app.services.weather import fetch_weather, fetch_air_quality
from scal_app.services.bus import get_bus_arrivals, render_bus_box, pick_text
from scal_app.services.todoist import fetch_tasks as fetch_todoist_tasks, TodoistAPIError
from scal_app.templates import load_board_html, load_settings_html

# === [SECTION: iCal loader (with basic fallback parser)] =====================
_ical_cache: Dict[str, Dict[str, Any]] = {}
DEFAULT_CAL_COLOR = "#4b6bff"

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
    now = time.time()
    if not url:
        return []

    cached = _ical_cache.get(url)
    if cached and now - cached.get("ts", 0.0) < 300:
        return cached.get("events", [])

    r = requests.get(url, timeout=10)
    r.raise_for_status()
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
    _ical_cache[url] = {"ts": now, "events": evs}
    # Keep cache small
    if len(_ical_cache) > 6:
        # Drop oldest entry
        oldest_url = min(_ical_cache.items(), key=lambda item: item[1].get("ts", 0.0))[0]
        if oldest_url != url:
            _ical_cache.pop(oldest_url, None)
    return evs

def month_filter(items, y, m):
    mm = f"{y:04d}-{m:02d}"
    return [e for e in items if (e.get("start", "").startswith(mm) or e.get("end", "").startswith(mm))]


_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _normalize_color(value: str) -> str:
    value = (value or "").strip()
    if _COLOR_RE.match(value):
        return value.lower()
    return DEFAULT_CAL_COLOR


def _calendar_entries() -> List[Dict[str, str]]:
    frame_cfg = CFG.get("frame", {}) or {}
    calendars = frame_cfg.get("calendars")
    result: List[Dict[str, str]] = []
    if isinstance(calendars, list):
        for entry in calendars:
            if not isinstance(entry, dict):
                continue
            url = (entry.get("url") or "").strip()
            if not url:
                continue
            color = _normalize_color(entry.get("color") or DEFAULT_CAL_COLOR)
            result.append({"url": url, "color": color})
    if not result:
        url = (frame_cfg.get("ical_url") or "").strip()
        if url:
            result.append({"url": url, "color": DEFAULT_CAL_COLOR})
    return result[:3]


def _primary_calendar_url() -> str:
    calendars = _calendar_entries()
    return calendars[0]["url"] if calendars else ""


def _set_primary_calendar(url: str, *, color: Optional[str] = None) -> None:
    frame_cfg = CFG.setdefault("frame", {})
    normalized_color = _normalize_color(color or DEFAULT_CAL_COLOR)
    if url:
        frame_cfg["ical_url"] = url
        frame_cfg["calendars"] = [{"url": url, "color": normalized_color}]
    else:
        frame_cfg["ical_url"] = ""
        frame_cfg["calendars"] = []

# Weather and air-quality helpers live in scal_app.services.weather

# Bus utilities are implemented in scal_app.services.bus

# === [SECTION: Bus configuration helpers for Telegram] =======================
def bus_search_stops(city_code: str, keyword: str, service_key: str, *, limit: int = 10) -> List[Tuple[str, str, str]]:
    city_code = (city_code or "").strip()
    keyword = (keyword or "").strip()
    service_key = (service_key or "").strip()
    if not (city_code and keyword and service_key):
        return []

    url = (
        "http://apis.data.go.kr/1613000/BusSttnInfoInqireService/getSttnList"
        f"?serviceKey={quote(service_key)}&cityCode={quote(city_code)}&nodeNm={quote(keyword)}"
    )
    try:
        response = requests.get(url, timeout=7)
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(f"정류소 검색 실패: {exc}")

    try:
        root = ET.fromstring(response.text)
    except Exception as exc:
        raise RuntimeError(f"TAGO 응답을 파싱하지 못했습니다: {exc}")

    stops: List[Tuple[str, str, str]] = []
    for item in root.iter("item"):
        name = pick_text(item, "nodenm", "nodeNm")
        ars = pick_text(item, "arsno", "arsNo")
        node = pick_text(item, "nodeid", "nodeId")
        if name and node:
            stops.append((name, ars, node))
            if len(stops) >= limit:
                break
    return stops

# === [SECTION: Google Home 연동 헬퍼] =======================================

# Optional dependency loaded via requirements.txt
try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request as GoogleAuthRequest
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    service_account = None  # type: ignore[assignment]
    GoogleAuthRequest = None  # type: ignore[assignment]


GOOGLE_HOME_BASE_URL = "https://homegraph.googleapis.com/v1"
GOOGLE_HOME_SCOPES = ("https://www.googleapis.com/auth/homegraph",)

GOOGLE_DEVICE_ICONS = {
    "action.devices.types.LIGHT": "💡",
    "action.devices.types.SWITCH": "🔌",
    "action.devices.types.OUTLET": "🔌",
    "action.devices.types.SENSOR": "📟",
    "action.devices.types.FAN": "🌀",
    "action.devices.types.AC_UNIT": "🌬️",
    "action.devices.types.THERMOSTAT": "🌡️",
    "action.devices.types.AIRPURIFIER": "💧",
    "action.devices.types.DISPLAY": "🖥️",
    "action.devices.types.SPEAKER": "🔊",
    "action.devices.types.TV": "📺",
    "action.devices.types.VACUUM": "🤖",
    "action.devices.types.SCENE": "🎨",
    "action.devices.types.LOCK": "🔐",
}


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


def _mask_secret(value: str, *, head: int = 4, tail: int = 4) -> str:
    value = (value or "").strip()
    if not value:
        return "설정안됨"
    if len(value) <= 2:
        return value[0] + "*" * (len(value) - 1) if len(value) == 2 else "*"
    if len(value) <= head + tail:
        return value[0] + "*" * (len(value) - 2) + value[-1]
    return value[:head] + "*" * (len(value) - head - tail) + value[-tail:]


class GoogleHomeError(RuntimeError):
    """Google Home 통신과 관련된 기본 예외."""


class GoogleHomeConfigError(GoogleHomeError):
    """설정이 누락되었거나 잘못되었을 때 발생."""


class GoogleHomeAPIError(GoogleHomeError):
    """Google Home Graph API 호출이 실패했을 때 발생."""


def _google_home_cfg() -> Dict[str, Any]:
    return CFG.get("google_home", {}) or {}


def _google_home_timeout(cfg: Dict[str, Any]) -> float:
    raw = cfg.get("timeout", 10)
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = 10.0
    return max(5.0, timeout)


def _load_google_credentials(cfg: Dict[str, Any]):
    if service_account is None or GoogleAuthRequest is None:
        raise GoogleHomeConfigError("google-auth 패키지가 필요합니다. requirements.txt를 확인하세요.")

    sa_file = (cfg.get("service_account_file") or "").strip()
    if not sa_file:
        raise GoogleHomeConfigError("google_home.service_account_file 설정이 필요합니다.")

    path = Path(sa_file).expanduser()
    if not path.exists():
        raise GoogleHomeConfigError(f"서비스 계정 키 파일을 찾을 수 없습니다: {path}")

    try:
        credentials = service_account.Credentials.from_service_account_file(
            str(path), scopes=GOOGLE_HOME_SCOPES
        )
    except Exception as exc:  # pragma: no cover - defensive
        raise GoogleHomeConfigError(f"서비스 계정 자격 증명 로드 실패: {exc}") from exc

    request = GoogleAuthRequest()
    try:
        credentials.refresh(request)
    except Exception as exc:
        raise GoogleHomeAPIError(f"Google OAuth 토큰 갱신 실패: {exc}") from exc

    if not credentials.token:
        raise GoogleHomeAPIError("Google OAuth 토큰을 받지 못했습니다.")

    return credentials


def _google_home_session() -> Tuple[requests.Session, float, Dict[str, Any], str]:
    cfg = _google_home_cfg()
    agent_user_id = (cfg.get("agent_user_id") or "").strip()
    if not agent_user_id:
        raise GoogleHomeConfigError("google_home.agent_user_id 설정이 필요합니다.")

    credentials = _load_google_credentials(cfg)
    timeout = _google_home_timeout(cfg)

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {credentials.token}",
            "Content-Type": "application/json",
        }
    )
    return session, timeout, cfg, agent_user_id


def _google_home_request(
    session: requests.Session,
    method: str,
    path: str,
    *,
    timeout: float,
    json_payload: Optional[Dict[str, Any]] = None,
) -> Any:
    url = f"{GOOGLE_HOME_BASE_URL}{path}"
    try:
        resp = session.request(method, url, json=json_payload, timeout=timeout)
    except Exception as exc:
        raise GoogleHomeAPIError(f"Google Home 요청 실패: {exc}") from exc

    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        if isinstance(detail, dict):
            message = detail.get("error") or detail.get("message") or detail
        else:
            message = detail
        raise GoogleHomeAPIError(f"HTTP {resp.status_code}: {message}")

    if resp.content:
        try:
            return resp.json()
        except Exception as exc:
            raise GoogleHomeAPIError("응답 JSON 파싱 실패") from exc
    return None


def _google_home_should_include(device: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    include_devices = cfg.get("include_devices")
    include_types = cfg.get("include_types")
    device_id = str(device.get("id") or "")
    device_type = device.get("type") or ""

    if isinstance(include_devices, list) and include_devices:
        normalized = {str(x).strip() for x in include_devices if x}
        return device_id in normalized

    if isinstance(include_types, list) and include_types:
        normalized_types = {str(x).strip() for x in include_types if x}
        return device_type in normalized_types or not normalized_types

    return True


def _google_home_pick_icon(device_type: str) -> str:
    return GOOGLE_DEVICE_ICONS.get(device_type, "🔘")


def _google_home_state_label(can_toggle: bool, online: bool, state: Dict[str, Any]) -> str:
    if not online:
        return "오프라인"

    status = (state.get("status") or "").upper()
    if status == "ERROR":
        error = state.get("errorCode") or state.get("error_code") or "알 수 없는 오류"
        return f"오류: {error}"

    on_state = state.get("on")
    if can_toggle and isinstance(on_state, bool):
        return "켜짐" if on_state else "꺼짐"

    if isinstance(state.get("brightness"), (int, float)):
        return f"밝기 {int(state['brightness'])}%"

    if isinstance(state.get("humidity"), (int, float)):
        return f"습도 {int(state['humidity'])}%"

    if isinstance(state.get("temperatureSetpoint"), (int, float)):
        return f"설정 {state['temperatureSetpoint']}°"

    return "상태 확인 필요"


def _format_google_home_device(device: Dict[str, Any], state: Dict[str, Any]) -> Dict[str, Any]:
    device_id = str(device.get("id") or "")
    if not device_id:
        raise GoogleHomeAPIError("동기화 응답에 기기 ID가 없습니다.")

    name_block = device.get("name") if isinstance(device.get("name"), dict) else {}
    display_name: Optional[str] = None
    if isinstance(name_block, dict):
        display_name = name_block.get("name")
        if not display_name:
            defaults = name_block.get("defaultNames")
            if isinstance(defaults, list) and defaults:
                display_name = str(defaults[0])
            else:
                nick = name_block.get("nicknames")
                if isinstance(nick, list) and nick:
                    display_name = str(nick[0])

    display_name = str(display_name or device_id)

    room = device.get("roomHint") or ""
    traits = device.get("traits") if isinstance(device.get("traits"), list) else []
    can_toggle = "action.devices.traits.OnOff" in traits

    online = bool(state.get("online", True))
    status = (state.get("status") or "").upper()
    error_code = (state.get("errorCode") or state.get("error_code") or "").lower()
    if status == "ERROR" and "offline" in error_code:
        online = False

    icon = _google_home_pick_icon(str(device.get("type") or ""))
    state_label = _google_home_state_label(can_toggle, online, state)
    on_state = state.get("on") if isinstance(state.get("on"), bool) else None

    return {
        "id": device_id,
        "name": display_name,
        "room": room if isinstance(room, str) else "",
        "type": str(device.get("type") or ""),
        "icon": icon,
        "online": online,
        "can_toggle": can_toggle,
        "traits": traits,
        "state": {"on": on_state},
        "state_label": state_label,
    }


def google_home_list_devices() -> List[Dict[str, Any]]:
    session, timeout, cfg, agent_user_id = _google_home_session()
    try:
        sync_payload = {"agentUserId": agent_user_id}
        sync_data = _google_home_request(
            session, "POST", "/devices:sync", timeout=timeout, json_payload=sync_payload
        )
        if not isinstance(sync_data, dict):
            raise GoogleHomeAPIError("devices:sync 응답 형식이 올바르지 않습니다.")

        raw_devices = sync_data.get("devices")
        devices_list: List[Dict[str, Any]] = []
        if isinstance(raw_devices, list):
            for device in raw_devices:
                if isinstance(device, dict) and _google_home_should_include(device, cfg):
                    devices_list.append(device)

        if not devices_list:
            return []

        query_payload = {
            "requestId": secrets.token_hex(8),
            "agentUserId": agent_user_id,
            "inputs": [
                {
                    "intent": "action.devices.QUERY",
                    "payload": {
                        "devices": [
                            {"id": str(dev.get("id"))}
                            for dev in devices_list
                            if dev.get("id")
                        ]
                    },
                }
            ],
        }

        query_data = _google_home_request(
            session, "POST", "/devices:query", timeout=timeout, json_payload=query_payload
        )

        states: Dict[str, Dict[str, Any]] = {}
        if isinstance(query_data, dict):
            payload = query_data.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("devices"), dict):
                devices_payload = payload.get("devices") or {}
                if isinstance(devices_payload, dict):
                    for key, val in devices_payload.items():
                        if isinstance(val, dict):
                            states[str(key)] = val

        formatted: List[Dict[str, Any]] = []
        for device in devices_list:
            dev_id = str(device.get("id") or "")
            state = states.get(dev_id, {})
            formatted.append(_format_google_home_device(device, state))

        formatted.sort(key=lambda d: ((d.get("room") or ""), d.get("name") or d.get("id") or ""))
        return formatted
    finally:
        try:
            session.close()
        except Exception:  # pragma: no cover - defensive
            pass


def google_home_execute(device_id: str, turn_on: bool) -> Any:
    device_id = (device_id or "").strip()
    if not device_id:
        raise GoogleHomeAPIError("유효한 Google Home 기기 ID가 필요합니다.")

    session, timeout, _cfg, agent_user_id = _google_home_session()
    try:
        payload = {
            "requestId": secrets.token_hex(8),
            "agentUserId": agent_user_id,
            "commands": [
                {
                    "devices": [{"id": device_id}],
                    "execution": [
                        {
                            "command": "action.devices.commands.OnOff",
                            "params": {"on": bool(turn_on)},
                        }
                    ],
                }
            ],
        }
        return _google_home_request(
            session, "POST", "/devices:executeCommand", timeout=timeout, json_payload=payload
        )
    finally:
        try:
            session.close()
        except Exception:  # pragma: no cover - defensive
            pass

# === [SECTION: Photo file listing for board background] ======================
def list_local_images():
    exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    files = []
    for p in sorted(PHOTOS_DIR.glob("**/*")):
        if p.is_file() and p.suffix.lower() in exts:
            files.append(str(p.relative_to(PHOTOS_DIR)))
    return files


def _settings_snapshot() -> Dict[str, Any]:
    frame_cfg = CFG.get("frame", {}) or {}
    gh_cfg = CFG.get("google_home", {}) or {}
    bus_cfg = CFG.get("bus", {}) or {}
    weather_cfg = CFG.get("weather", {}) or {}
    tg_cfg = CFG.get("telegram", {}) or {}
    todo_cfg = CFG.get("todoist", {}) or {}
    allowed_ids = tg_cfg.get("allowed_user_ids") or []
    allowed_text = ", ".join(str(x) for x in allowed_ids)
    return {
        "frame": {
            "ical_url": frame_cfg.get("ical_url", ""),
            "calendars": _calendar_entries(),
        },
        "google_home": {
            "service_account_file": gh_cfg.get("service_account_file", ""),
            "agent_user_id": gh_cfg.get("agent_user_id", ""),
            "include_types": gh_cfg.get("include_types", []),
            "include_devices": gh_cfg.get("include_devices", []),
        },
        "bus": {
            "key": bus_cfg.get("key", ""),
            "city_code": bus_cfg.get("city_code", ""),
            "node_id": bus_cfg.get("node_id", ""),
        },
        "weather": {
            "api_key": weather_cfg.get("api_key", ""),
            "location": weather_cfg.get("location", ""),
        },
        "telegram": {
            "bot_token": tg_cfg.get("bot_token", ""),
            "allowed_user_ids": allowed_ids,
            "allowed_user_ids_text": allowed_text,
        },
        "todoist": {
            "api_token": todo_cfg.get("api_token", ""),
            "project_id": todo_cfg.get("project_id", ""),
        },
        "verse": {"text": get_verse()},
    }


def _parse_allowed_ids(raw: str) -> List[int]:
    raw = (raw or "").strip()
    if not raw:
        return []
    parts = re.split(r"[\s,]+", raw)
    result: List[int] = []
    for part in parts:
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError as exc:
            raise ValueError(f"숫자 ID만 입력하세요: '{part}'") from exc
    return result


def _is_safe_photo_path(path: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(PHOTOS_DIR.resolve())  # type: ignore[attr-defined]
    except AttributeError:
        resolved_dir = PHOTOS_DIR.resolve()
        resolved_path = Path(path).resolve()
        return str(resolved_path).startswith(str(resolved_dir))

# === [SECTION: Flask app / session / proxy headers] ==========================
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SFRAME_SESSION_SECRET", "CHANGE_ME_32CHARS")
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="None")

# === [SECTION: Verse helpers + API endpoints] ================================
@app.get("/api/verse")
def api_verse():
    return jsonify({"text": get_verse()})


@app.post("/api/verse")
def api_set_verse():
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    try:
        set_verse(text)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"success": True, "verse": {"text": get_verse()}})

# === [SECTION: REST API endpoints used by the board HTML] ====================
@app.get("/api/todo")
def api_todo():
    cfg = CFG.get("todoist", {}) or {}
    token = (cfg.get("api_token") or "").strip()
    project = (cfg.get("project_id") or "").strip() or None
    if not token:
        return jsonify({"items": [], "need_config": True})
    try:
        tasks = fetch_todoist_tasks(token, project_id=project, limit=10, tz=TZ)
    except TodoistAPIError as exc:
        return jsonify({"items": [], "error": str(exc)})
    except Exception as exc:
        logging.getLogger(__name__).warning("Todoist fetch failed: %s", exc, exc_info=True)
        return jsonify({"items": [], "error": "할 일을 불러오지 못했습니다."}), 502
    return jsonify({"items": tasks})


# === [SECTION: Settings management endpoints] ================================
@app.get("/api/settings")
def api_get_settings():
    return jsonify(_settings_snapshot())


@app.post("/api/settings")
def api_update_settings():
    payload = request.get_json(silent=True) or {}
    errors: List[str] = []
    updated = False

    try:
        if "frame" in payload:
            section = payload["frame"] or {}
            frame_cfg = CFG.setdefault("frame", {})
            calendars_payload = section.get("calendars")
            calendars: List[Dict[str, str]] = []
            if isinstance(calendars_payload, list):
                for entry in calendars_payload[:3]:
                    if not isinstance(entry, dict):
                        continue
                    url = (entry.get("url") or "").strip()
                    if not url:
                        continue
                    color = _normalize_color(entry.get("color") or DEFAULT_CAL_COLOR)
                    calendars.append({"url": url, "color": color})

            if calendars:
                invalid = [c for c in calendars if not re.match(r"^https?://", c["url"], re.IGNORECASE)]
                if invalid:
                    errors.append("캘린더 URL은 http:// 또는 https:// 로 시작해야 합니다.")
                else:
                    frame_cfg["calendars"] = calendars
                    frame_cfg["ical_url"] = calendars[0]["url"]
                    updated = True
            else:
                ical = (section.get("ical_url") or "").strip()
                if ical:
                    if not re.match(r"^https?://", ical, re.IGNORECASE):
                        errors.append("iCal URL은 http:// 또는 https:// 로 시작해야 합니다.")
                    else:
                        _set_primary_calendar(ical)
                        updated = True
                else:
                    if frame_cfg.get("ical_url") or frame_cfg.get("calendars"):
                        _set_primary_calendar("")
                        updated = True

        if "google_home" in payload:
            section = payload["google_home"] or {}
            cfg = CFG.setdefault("google_home", {})
            sa_file = (section.get("service_account_file") or "").strip()
            agent_user_id = (section.get("agent_user_id") or "").strip()
            include_types = section.get("include_types")
            include_devices = section.get("include_devices")

            if sa_file:
                cfg["service_account_file"] = sa_file
            elif "service_account_file" in section:
                cfg["service_account_file"] = sa_file

            if agent_user_id or "agent_user_id" in section:
                cfg["agent_user_id"] = agent_user_id

            if isinstance(include_types, list):
                cfg["include_types"] = [
                    str(x).strip() for x in include_types if str(x).strip()
                ]

            if isinstance(include_devices, list):
                cfg["include_devices"] = [
                    str(x).strip() for x in include_devices if str(x).strip()
                ]

            updated = True

        if "bus" in payload:
            section = payload["bus"] or {}
            CFG.setdefault("bus", {})["key"] = (section.get("key") or "").strip()
            CFG.setdefault("bus", {})["city_code"] = (section.get("city_code") or "").strip()
            CFG.setdefault("bus", {})["node_id"] = (section.get("node_id") or "").strip()
            updated = True

        if "weather" in payload:
            section = payload["weather"] or {}
            CFG.setdefault("weather", {})["api_key"] = (section.get("api_key") or "").strip()
            CFG.setdefault("weather", {})["location"] = (section.get("location") or "").strip()
            updated = True

        if "todoist" in payload:
            section = payload["todoist"] or {}
            CFG.setdefault("todoist", {})["api_token"] = (section.get("api_token") or "").strip()
            CFG.setdefault("todoist", {})["project_id"] = (section.get("project_id") or "").strip()
            updated = True

        if "telegram" in payload:
            section = payload["telegram"] or {}
            bot_token = (section.get("bot_token") or "").strip()
            try:
                allowed = _parse_allowed_ids(section.get("allowed_user_ids", ""))
            except ValueError as exc:
                errors.append(str(exc))
            else:
                cfg = CFG.setdefault("telegram", {})
                cfg["bot_token"] = bot_token
                cfg["allowed_user_ids"] = allowed
                global ALLOWED
                ALLOWED = set(allowed)
                updated = True
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    if errors:
        return jsonify({"error": "\n".join(errors)}), 400

    if updated:
        save_config_to_source(CFG)

    return jsonify({"success": True, "config": _settings_snapshot()})


@app.get("/api/bus/search")
def api_bus_search():
    keyword = (request.args.get("keyword") or "").strip()
    if not keyword:
        return jsonify({"results": []})
    city = (request.args.get("city") or CFG.get("bus", {}).get("city_code") or "").strip()
    service_key = (request.args.get("service_key") or CFG.get("bus", {}).get("key") or "").strip()
    if not city:
        return jsonify({"error": "도시 코드를 먼저 입력하세요."}), 400
    if not service_key:
        return jsonify({"error": "TAGO 서비스키가 필요합니다."}), 400
    try:
        results = bus_search_stops(city, keyword, service_key, limit=12)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
    payload = [
        {"name": name, "ars": ars, "node_id": node}
        for name, ars, node in results
    ]
    return jsonify({"results": payload})

@app.get("/api/events")
def api_events():
    calendars = _calendar_entries()
    if not calendars:
        return jsonify([])
    try:
        y = int(request.args.get("year")) if request.args.get("year") else None
        m = int(request.args.get("month")) if request.args.get("month") else None
    except Exception:
        y = m = None
    now_kst = datetime.now(TZ)
    y = y or now_kst.year
    m = m or now_kst.month
    aggregated: List[Dict[str, Any]] = []
    for idx, cal in enumerate(calendars):
        url = cal["url"]
        try:
            events = fetch_ical(url)
        except Exception as exc:
            logging.getLogger(__name__).warning("Failed to fetch calendar %s: %s", url, exc)
            continue
        for ev in month_filter(events, y, m):
            item = dict(ev)
            item.setdefault("title", "(untitled)")
            item.setdefault("start", "")
            item.setdefault("end", item.get("start", ""))
            item["color"] = cal["color"]
            item["calendar_index"] = idx
            aggregated.append(item)
    aggregated.sort(key=lambda x: (x.get("start", ""), x.get("title", "")))
    return jsonify(aggregated)

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


@app.post("/api/photos/upload")
def api_photos_upload():
    if "photo" not in request.files:
        return jsonify({"error": "사진 파일을 선택해주세요."}), 400
    file = request.files["photo"]
    if not file or not file.filename:
        return jsonify({"error": "파일 이름을 확인해주세요."}), 400
    filename = secure_filename(file.filename)
    ext = Path(filename).suffix.lower()
    allowed_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    if ext not in allowed_exts:
        return jsonify({"error": "지원하지 않는 파일 형식입니다."}), 400
    ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    new_name = f"web_{ts}_{secrets.token_hex(3)}{ext}"
    dest = PHOTOS_DIR / new_name
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        file.save(dest)
    except Exception as exc:
        return jsonify({"error": f"업로드 실패: {exc}"}), 500
    return jsonify({"success": True, "filename": new_name})


@app.delete("/api/photos/<path:fname>")
def api_delete_photo(fname: str):
    target = PHOTOS_DIR / fname
    if not _is_safe_photo_path(target):
        return jsonify({"error": "잘못된 경로입니다."}), 400
    try:
        if not target.exists():
            return jsonify({"error": "파일을 찾을 수 없습니다."}), 404
        target.unlink()
    except Exception as exc:
        return jsonify({"error": f"삭제 실패: {exc}"}), 500
    return jsonify({"success": True})


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
        devices = google_home_list_devices()
        resp: Dict[str, Any] = {
            "devices": devices,
            "dashboard": {"title": "Google Home", "entity_count": len(devices)},
        }
        if not devices:
            resp["message"] = "Google Home에서 표시할 기기를 찾지 못했습니다."
        return jsonify(resp)
    except GoogleHomeConfigError as e:
        return jsonify({"need_config": True, "message": str(e)})
    except GoogleHomeAPIError as e:
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
        google_home_execute(device_id, desired)
        return jsonify({"success": True})
    except GoogleHomeConfigError as e:
        return jsonify({"error": str(e)}), 400
    except GoogleHomeAPIError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# === [SECTION: Board HTML (legacy UI; monthly calendar + photo fade)] ========
# Board HTML moved to scal_app.templates.board.html



@app.get("/board")
def board():
    return render_template_string(load_board_html())


@app.get("/settings")
def settings_page():
    return render_template_string(load_settings_html())

# Bot state helpers are provided by scal_app.config.load_state/save_state

# === [SECTION: Telegram bot initialization / ACL] ============================
if telebot and CFG["telegram"].get("bot_token"):
    TB = telebot.TeleBot(CFG["telegram"]["bot_token"])
else:
    if CFG["telegram"].get("bot_token") and not telebot:
        print("[TG] pyTelegramBotAPI 미설치로 텔레그램을 비활성화합니다.")
    TB = None
ALLOWED = set(CFG["telegram"]["allowed_user_ids"])


def allowed(uid: int) -> bool:
    return uid in ALLOWED if ALLOWED else True


# === [SECTION: Telegram command handlers (simplified menu)] ===================
if TB:
    def _get_state(uid: int) -> Dict[str, Any]:
        return load_state().get(str(uid), {})


    def _set_state(uid: int, data: Dict[str, Any]) -> None:
        st = load_state()
        st[str(uid)] = data
        save_state(st)


    def _update_state(uid: int, **updates: Any) -> Dict[str, Any]:
        st = load_state()
        cur = st.get(str(uid), {})
        cur.update(updates)
        st[str(uid)] = cur
        save_state(st)
        return cur


    def _clear_state(uid: int) -> None:
        st = load_state()
        if str(uid) in st:
            st.pop(str(uid), None)
            save_state(st)


    def _send_main_menu(chat_id: int) -> None:
        kb = telebot.types.InlineKeyboardMarkup(row_width=1)
        options = [
            ("1) 캘린더 iCal 주소", "cfg_ical"),
            ("2) Google Home 설정", "cfg_gh"),
            ("3) 버스 정보", "cfg_bus"),
            ("4) 사진 등록", "cfg_photo"),
            ("5) 날씨 API 설정", "cfg_weather"),
            ("6) 오늘의 한마디", "cfg_verse"),
        ]
        for text_label, data in options:
            kb.add(telebot.types.InlineKeyboardButton(text_label, callback_data=data))
        TB.send_message(chat_id, "원하는 항목을 선택하세요:", reply_markup=kb)


    def _build_bus_stop_keyboard(stops: List[Tuple[str, str, str]]) -> telebot.types.InlineKeyboardMarkup:
        kb = telebot.types.InlineKeyboardMarkup(row_width=1)
        for idx, (name, ars, _node) in enumerate(stops[:10]):
            label = f"{name} ({ars})" if ars else name
            kb.add(
                telebot.types.InlineKeyboardButton(label[:64], callback_data=f"bus_stop:{idx}")
            )
        kb.add(telebot.types.InlineKeyboardButton("취소", callback_data="bus_stop_cancel"))
        return kb


    @TB.message_handler(commands=["start"])
    def tg_start(m):
        if not allowed(m.from_user.id):
            return TB.reply_to(m, "권한이 없습니다.")
        _clear_state(m.from_user.id)
        lines = ["스마트 프레임 설정 봇입니다.", "메뉴에서 원하는 항목을 선택하세요."]
        TB.send_message(m.chat.id, "\n".join(lines))
        _send_main_menu(m.chat.id)


    @TB.message_handler(commands=["set", "frame"])
    def tg_set_menu(m):
        if not allowed(m.from_user.id):
            return TB.reply_to(m, "권한이 없습니다.")
        _clear_state(m.from_user.id)
        _send_main_menu(m.chat.id)


    @TB.callback_query_handler(
        func=lambda c: c.data in {"cfg_ical", "cfg_gh", "cfg_bus", "cfg_photo", "cfg_weather", "cfg_verse"}
    )
    def on_main_callbacks(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "권한이 없습니다.")
            return
        chat_id = c.message.chat.id
        uid = c.from_user.id
        TB.answer_callback_query(c.id)

        if c.data == "cfg_ical":
            current = _primary_calendar_url() or "(미설정)"
            _set_state(uid, {"mode": "await_ical"})
            TB.send_message(
                chat_id,
                f"현재 iCal URL:\n{current}\n새 URL을 입력하거나 /cancel 로 취소하세요.",
            )
        elif c.data == "cfg_gh":
            kb = telebot.types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                telebot.types.InlineKeyboardButton("서비스 계정 키 경로", callback_data="gh_set_sa"),
                telebot.types.InlineKeyboardButton("Agent User ID", callback_data="gh_set_agent"),
                telebot.types.InlineKeyboardButton("허용 기기 타입", callback_data="gh_set_types"),
                telebot.types.InlineKeyboardButton("허용 기기 ID", callback_data="gh_set_devices"),
                telebot.types.InlineKeyboardButton("현재 설정 보기", callback_data="gh_show_config"),
            )
            TB.send_message(chat_id, "Google Home 설정을 선택하세요.", reply_markup=kb)
        elif c.data == "cfg_bus":
            kb = telebot.types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                telebot.types.InlineKeyboardButton("서비스키 입력", callback_data="bus_set_key"),
                telebot.types.InlineKeyboardButton("도시 코드 입력", callback_data="bus_set_city"),
                telebot.types.InlineKeyboardButton("정류소 검색", callback_data="bus_set_stop"),
                telebot.types.InlineKeyboardButton("현재 설정 보기", callback_data="bus_show_config"),
                telebot.types.InlineKeyboardButton("도착 정보 테스트", callback_data="bus_test"),
            )
            TB.send_message(chat_id, "버스 정보 설정을 선택하세요.", reply_markup=kb)
        elif c.data == "cfg_photo":
            _set_state(uid, {"mode": "await_photo"})
            TB.send_message(
                chat_id,
                "등록할 사진을 전송해주세요.\n원하지 않으면 /cancel 을 입력하세요.",
            )
        elif c.data == "cfg_weather":
            kb = telebot.types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                telebot.types.InlineKeyboardButton("API 키 입력", callback_data="weather_set_key"),
                telebot.types.InlineKeyboardButton("위치 입력", callback_data="weather_set_location"),
                telebot.types.InlineKeyboardButton("현재 설정 보기", callback_data="weather_show_config"),
            )
            TB.send_message(chat_id, "날씨 설정을 선택하세요.", reply_markup=kb)
        elif c.data == "cfg_verse":
            _set_state(uid, {"mode": "await_verse"})
            TB.send_message(chat_id, "오늘의 한마디를 입력해주세요. /cancel 로 취소할 수 있습니다.")


    @TB.callback_query_handler(
        func=lambda c: c.data
        in {"gh_set_sa", "gh_set_agent", "gh_set_types", "gh_set_devices", "gh_show_config"}
    )
    def on_google_home_callbacks(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "권한이 없습니다.")
            return
        chat_id = c.message.chat.id
        uid = c.from_user.id
        TB.answer_callback_query(c.id)

        if c.data == "gh_set_sa":
            _set_state(uid, {"mode": "await_gh_sa"})
            TB.send_message(
                chat_id,
                "Google Home 서비스 계정 JSON 파일 경로를 입력하세요. /cancel 로 취소",
            )
        elif c.data == "gh_set_agent":
            _set_state(uid, {"mode": "await_gh_agent"})
            TB.send_message(chat_id, "Google Home agentUserId 값을 입력하세요. /cancel 로 취소")
        elif c.data == "gh_set_types":
            _set_state(uid, {"mode": "await_gh_types"})
            TB.send_message(
                chat_id,
                "허용할 기기 타입을 콤마로 구분해 입력하세요. 비우면 전체 허용. /cancel 로 취소",
            )
        elif c.data == "gh_set_devices":
            _set_state(uid, {"mode": "await_gh_devices"})
            TB.send_message(
                chat_id,
                "허용할 기기 ID를 콤마로 구분해 입력하세요. 비우면 전체 허용. /cancel 로 취소",
            )
        elif c.data == "gh_show_config":
            cfg = CFG.get("google_home", {}) or {}
            sa_file = cfg.get("service_account_file") or "설정안됨"
            agent = cfg.get("agent_user_id") or "설정안됨"
            types = cfg.get("include_types") or []
            devices = cfg.get("include_devices") or []
            type_txt = ", ".join(types) if types else "전체 허용"
            dev_txt = ", ".join(devices) if devices else "전체 허용"
            lines = [
                f"서비스 계정 파일: {sa_file}",
                f"agentUserId: {agent}",
                f"허용 타입: {type_txt}",
                f"허용 기기 ID: {dev_txt}",
            ]
            TB.send_message(chat_id, "\n".join(lines))


    @TB.callback_query_handler(
        func=lambda c: c.data in {"bus_set_key", "bus_set_city", "bus_set_stop", "bus_show_config", "bus_test"}
    )
    def on_bus_callbacks(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "권한이 없습니다.")
            return
        chat_id = c.message.chat.id
        uid = c.from_user.id
        TB.answer_callback_query(c.id)
        bus_cfg = CFG.setdefault("bus", {})

        if c.data == "bus_set_key":
            _set_state(uid, {"mode": "await_bus_key"})
            TB.send_message(chat_id, "TAGO 서비스키를 입력하세요. /cancel 로 취소")
        elif c.data == "bus_set_city":
            _set_state(uid, {"mode": "await_bus_city"})
            TB.send_message(chat_id, "도시 코드를 입력하세요 (예: 25). /cancel 로 취소")
        elif c.data == "bus_set_stop":
            if not (bus_cfg.get("key") and bus_cfg.get("city_code")):
                TB.send_message(chat_id, "먼저 서비스키와 도시 코드를 입력해주세요.")
                return
            _set_state(uid, {"mode": "await_bus_stop_keyword"})
            TB.send_message(chat_id, "정류소명을 입력하세요. /cancel 로 취소")
        elif c.data == "bus_show_config":
            key_state = "등록" if bus_cfg.get("key") else "미등록"
            lines = [
                f"도시 코드: {bus_cfg.get('city_code') or '설정안됨'}",
                f"정류소 nodeId: {bus_cfg.get('node_id') or '설정안됨'}",
                f"서비스 키: {key_state}",
            ]
            TB.send_message(chat_id, "\n".join(lines))
        elif c.data == "bus_test":
            try:
                box = render_bus_box()
            except Exception as exc:
                TB.send_message(chat_id, f"버스 정보 조회 실패: {exc}")
                return
            rows = box.get("rows", [])
            if not rows:
                TB.send_message(chat_id, "도착 정보를 가져오지 못했습니다.")
                return
            lines = [box.get("title", "버스도착")]
            for row in rows[:10]:
                text = row.get("text")
                if text:
                    lines.append(text)
                else:
                    lines.append(f"{row.get('route')} · {row.get('eta')} · {row.get('hops')}")
            TB.send_message(chat_id, "\n".join(lines))


    @TB.callback_query_handler(func=lambda c: c.data in {"weather_set_key", "weather_set_location", "weather_show_config"})
    def on_weather_callbacks(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "권한이 없습니다.")
            return
        chat_id = c.message.chat.id
        uid = c.from_user.id
        TB.answer_callback_query(c.id)
        weather_cfg = CFG.setdefault("weather", {})

        if c.data == "weather_set_key":
            _set_state(uid, {"mode": "await_weather_key"})
            TB.send_message(chat_id, "OpenWeather API 키를 입력하세요. /cancel 로 취소")
        elif c.data == "weather_set_location":
            _set_state(uid, {"mode": "await_weather_location"})
            TB.send_message(chat_id, "날씨를 조회할 위치를 입력하세요 (예: Seoul, KR). /cancel 로 취소")
        elif c.data == "weather_show_config":
            provider = weather_cfg.get("provider") or "openweathermap"
            location = weather_cfg.get("location") or "설정안됨"
            api_key = _mask_secret(weather_cfg.get("api_key", ""))
            lines = [
                f"제공자: {provider}",
                f"위치: {location}",
                f"API 키: {api_key}",
            ]
            TB.send_message(chat_id, "\n".join(lines))


    @TB.callback_query_handler(func=lambda c: c.data.startswith("bus_stop:"))
    def on_bus_stop_select(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "권한이 없습니다.")
            return
        uid = c.from_user.id
        st = _get_state(uid)
        if st.get("mode") != "await_bus_stop_select":
            TB.answer_callback_query(c.id)
            return
        stops = st.get("stop_results") or []
        try:
            index = int(c.data.split(":", 1)[1])
        except (ValueError, IndexError):
            TB.answer_callback_query(c.id, "선택 오류")
            return
        if not (0 <= index < len(stops)):
            TB.answer_callback_query(c.id, "선택 범위를 벗어났습니다.")
            return
        name, _ars, node = stops[index]
        CFG.setdefault("bus", {})["node_id"] = node
        save_config_to_source(CFG)
        _clear_state(uid)
        TB.answer_callback_query(c.id, "정류소 저장 완료")
        TB.send_message(c.message.chat.id, f"정류소가 저장되었습니다: {name} ({node})")


    @TB.callback_query_handler(func=lambda c: c.data == "bus_stop_cancel")
    def on_bus_stop_cancel(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "권한이 없습니다.")
            return
        _clear_state(c.from_user.id)
        TB.answer_callback_query(c.id, "취소했습니다.")
        TB.send_message(c.message.chat.id, "정류소 선택을 취소했습니다.")


    @TB.message_handler(commands=["cancel"])
    def tg_cancel(m):
        if not allowed(m.from_user.id):
            return TB.reply_to(m, "권한이 없습니다.")
        _clear_state(m.from_user.id)
        TB.reply_to(m, "진행 중인 작업을 취소했습니다.")


    @TB.message_handler(content_types=["photo"])
    def on_photo(m):
        if not allowed(m.from_user.id):
            return
        st = _get_state(m.from_user.id)
        if st.get("mode") != "await_photo":
            return
        try:
            largest = max(m.photo, key=lambda p: p.file_size or 0)
            file_info = TB.get_file(largest.file_id)
            data = TB.download_file(file_info.file_path)
            suffix = Path(file_info.file_path).suffix or ".jpg"
            ts = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
            fname = f"tg_{ts}_{secrets.token_hex(3)}{suffix}"
            dest = PHOTOS_DIR / fname
            dest.write_bytes(data)
            _clear_state(m.from_user.id)
            TB.reply_to(m, f"사진 저장 완료: {fname}")
        except Exception as exc:
            TB.reply_to(m, f"사진 저장 실패: {exc}")


    @TB.message_handler(func=lambda m: True, content_types=["text"])
    def on_text(m):
        if not allowed(m.from_user.id):
            return
        text = (m.text or "").strip()
        if not text:
            return
        st = _get_state(m.from_user.id)
        if not st:
            return
        mode = st.get("mode")
        uid = m.from_user.id

        if mode == "await_ical":
            if not (text.startswith("http://") or text.startswith("https://")):
                TB.reply_to(m, "http:// 또는 https:// 로 시작하는 URL을 입력하세요.")
                return
            _set_primary_calendar(text)
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "iCal URL이 저장되었습니다. 보드는 잠시 후 갱신됩니다.")
        elif mode == "await_verse":
            set_verse(text)
            _clear_state(uid)
            TB.reply_to(m, "오늘의 한마디가 저장되었습니다.")
        elif mode == "await_gh_sa":
            CFG.setdefault("google_home", {})["service_account_file"] = text
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "서비스 계정 경로가 저장되었습니다.")
        elif mode == "await_gh_agent":
            CFG.setdefault("google_home", {})["agent_user_id"] = text
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "agentUserId가 저장되었습니다.")
        elif mode == "await_gh_types":
            items = [seg.strip() for seg in re.split(r"[\s,]+", text) if seg.strip()]
            CFG.setdefault("google_home", {})["include_types"] = items
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "허용 타입 목록이 저장되었습니다.")
        elif mode == "await_gh_devices":
            items = [seg.strip() for seg in re.split(r"[\s,]+", text) if seg.strip()]
            CFG.setdefault("google_home", {})["include_devices"] = items
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "허용 기기 ID 목록이 저장되었습니다.")
        elif mode == "await_bus_key":
            CFG.setdefault("bus", {})["key"] = text
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "서비스키가 저장되었습니다.")
        elif mode == "await_bus_city":
            CFG.setdefault("bus", {})["city_code"] = text
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "도시 코드가 저장되었습니다.")
        elif mode == "await_bus_stop_keyword":
            bus_cfg = CFG.setdefault("bus", {})
            key = bus_cfg.get("key", "")
            city = bus_cfg.get("city_code", "")
            try:
                stops = bus_search_stops(city, text, key)
            except Exception as exc:
                TB.reply_to(m, f"정류소 검색 실패: {exc}")
                return
            if not stops:
                TB.reply_to(m, "검색 결과가 없습니다. 다른 키워드를 입력해주세요.")
                return
            _set_state(uid, {"mode": "await_bus_stop_select", "stop_results": stops})
            TB.send_message(
                m.chat.id,
                "정류소를 선택하세요.",
                reply_markup=_build_bus_stop_keyboard(stops),
            )
        elif mode == "await_weather_key":
            CFG.setdefault("weather", {})["api_key"] = text
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "날씨 API 키가 저장되었습니다.")
        elif mode == "await_weather_location":
            CFG.setdefault("weather", {})["location"] = text
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "날씨 위치가 저장되었습니다.")

# === [SECTION: Telegram start (webhook or polling) + duplication guard] ======
# - 파일락(/tmp/scal_bot.lock)으로 중복 폴링 방지 (다중 토큰/다중 인스턴스 보호)
_lock_file = None
def start_telegram():
    """Start telegram in single-instance mode using a file lock."""
    global _lock_file
    if not TB:
        if CFG["telegram"].get("bot_token") and not telebot:
            print("[TG] pyTelegramBotAPI 미설치로 텔레그램을 비활성화합니다.")
        else:
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

# === [SECTION: App entrypoints (web thread + telegram)] ======================
def run_web():
    # debug=False, use_reloader=False to prevent reloader double-start
    try:
        app.run(
            host="0.0.0.0",
            port=int(CFG["server"]["port"]),
            debug=False,
            use_reloader=False,
        )
    except OSError:
        print("Address already in use")
        raise


def main():
    # 웹 서버 스레드는 daemon 이 아니어야 프로세스가 안 죽음
    web_thread = threading.Thread(target=run_web, name="scal-web")
    web_thread.start()

    print(f"[WEB] started on :{CFG['server']['port']}  -> /board")

    # 텔레그램은 옵션: 설정이 없으면 그냥 경고만 찍고 계속 진행
    try:
        start_telegram()
    except Exception as e:
        # 여기서 토큰 없음 등으로 에러 나도 Flask 만으로 계속 서비스
        print(f"[TG] Telegram not configured or failed to start: {e}")

    # start_telegram() 이 바로 리턴해도, 웹 스레드가 끝날 때까지 프로세스를 유지
    web_thread.join()


if __name__ == "__main__":
    main()
