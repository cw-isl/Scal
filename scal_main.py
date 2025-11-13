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
    FRAME_LAYOUT_DEFAULTS,
    frame_layout_snapshot,
    get_layout_for_orientation,
    normalize_orientation,
    update_layout_config,
)
from scal_app.services.weather import fetch_weather, fetch_air_quality
from scal_app.services.bus import get_bus_arrivals, render_bus_box, pick_text
from scal_app.services.todoist import fetch_tasks as fetch_todoist_tasks, TodoistAPIError
from scal_app.templates import load_board_html, load_settings_html, load_main_html

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

# === [SECTION: Home Assistant 연동 헬퍼] ====================================

HOME_ASSISTANT_DOMAIN_ICONS = {
    "light": "💡",
    "switch": "🔌",
    "outlet": "🔌",
    "fan": "🌀",
    "climate": "🌡️",
    "humidifier": "💧",
    "air_purifier": "💧",
    "media_player": "📺",
    "sensor": "📟",
    "binary_sensor": "📟",
    "vacuum": "🤖",
    "scene": "🎨",
    "lock": "🔐",
    "cover": "🪟",
}

HOME_ASSISTANT_DEFAULT_DOMAINS = {"light", "switch"}
HOME_ASSISTANT_TOGGLE_DOMAINS = {
    "light",
    "switch",
    "fan",
    "media_player",
    "climate",
    "cover",
    "humidifier",
    "air_purifier",
    "input_boolean",
    "scene",
    "script",
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


class HomeAssistantError(RuntimeError):
    """Home Assistant 통신과 관련된 기본 예외."""


class HomeAssistantConfigError(HomeAssistantError):
    """설정이 누락되었거나 잘못되었을 때 발생."""


class HomeAssistantAPIError(HomeAssistantError):
    """Home Assistant REST API 호출이 실패했을 때 발생."""


def _home_assistant_cfg() -> Dict[str, Any]:
    return CFG.get("home_assistant", {}) or {}


def _home_assistant_timeout(cfg: Dict[str, Any]) -> float:
    raw = cfg.get("timeout", 10)
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = 10.0
    return max(5.0, timeout)


def _home_assistant_session() -> Tuple[requests.Session, float, Dict[str, Any], str]:
    cfg = _home_assistant_cfg()
    base_url = (cfg.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise HomeAssistantConfigError("home_assistant.base_url 설정이 필요합니다.")
    if not re.match(r"^https?://", base_url, re.IGNORECASE):
        raise HomeAssistantConfigError(
            "home_assistant.base_url 은 http:// 또는 https:// 로 시작해야 합니다."
        )

    token = (cfg.get("token") or "").strip()
    if not token:
        raise HomeAssistantConfigError("home_assistant.token 설정이 필요합니다.")

    timeout = _home_assistant_timeout(cfg)

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
    )
    return session, timeout, cfg, base_url


def _home_assistant_request(
    session: requests.Session,
    method: str,
    path: str,
    *,
    timeout: float,
    base_url: str,
    json_payload: Optional[Dict[str, Any]] = None,
) -> Any:
    normalized_path = path if path.startswith("/") else f"/{path}"
    url = f"{base_url}{normalized_path}"
    try:
        resp = session.request(method, url, json=json_payload, timeout=timeout)
    except Exception as exc:
        raise HomeAssistantAPIError(f"Home Assistant 요청 실패: {exc}") from exc

    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        if isinstance(detail, dict):
            message = (
                detail.get("message")
                or detail.get("error")
                or detail.get("code")
                or detail
            )
        else:
            message = detail
        raise HomeAssistantAPIError(f"HTTP {resp.status_code}: {message}")

    if resp.content:
        try:
            return resp.json()
        except Exception as exc:
            raise HomeAssistantAPIError("응답 JSON 파싱 실패") from exc
    return None


def _home_assistant_should_include(
    entity_id: str, attributes: Dict[str, Any], cfg: Dict[str, Any]
) -> bool:
    include_entities = cfg.get("include_entities")
    include_domains = cfg.get("include_domains")
    normalized_entity = entity_id.lower()
    domain = normalized_entity.split(".", 1)[0] if "." in normalized_entity else ""

    if isinstance(include_entities, list) and include_entities:
        normalized = {str(x).strip().lower() for x in include_entities if x}
        return normalized_entity in normalized

    if isinstance(include_domains, list) and include_domains:
        normalized_types = {str(x).strip().lower() for x in include_domains if x}
        if normalized_types:
            return domain in normalized_types

    return domain in HOME_ASSISTANT_DEFAULT_DOMAINS


def _home_assistant_pick_icon(domain: str) -> str:
    return HOME_ASSISTANT_DOMAIN_ICONS.get(domain.lower(), "🏠")


def _home_assistant_state_label(
    state: str, attributes: Dict[str, Any], can_toggle: bool, online: bool
) -> str:
    if not online:
        return "오프라인"

    normalized_state = (state or "").strip().lower()
    if can_toggle and normalized_state in {"on", "off"}:
        return "켜짐" if normalized_state == "on" else "꺼짐"

    brightness = attributes.get("brightness")
    if isinstance(brightness, (int, float)):
        value = float(brightness)
        if value > 1:
            percent = int(round(max(0.0, min(value, 255.0)) / 255.0 * 100))
        else:
            percent = int(round(max(0.0, min(value, 1.0)) * 100))
        return f"밝기 {percent}%"

    percentage = attributes.get("percentage")
    if isinstance(percentage, (int, float)):
        return f"동작 {int(round(max(0.0, min(float(percentage), 100.0))))}%"

    humidity = attributes.get("humidity") or attributes.get("current_humidity")
    if isinstance(humidity, (int, float)):
        return f"습도 {int(round(float(humidity)))}%"

    temperature = (
        attributes.get("temperature")
        or attributes.get("current_temperature")
        or attributes.get("temperature_setpoint")
    )
    if isinstance(temperature, (int, float)):
        return f"온도 {float(temperature):.1f}°"

    if normalized_state:
        if normalized_state == "unavailable":
            return "오프라인"
        if normalized_state == "unknown":
            return "상태 미확인"
        return state

    return "상태 확인 필요"


def _format_home_assistant_entity(raw: Dict[str, Any]) -> Dict[str, Any]:
    entity_id = str(raw.get("entity_id") or "").strip()
    if not entity_id:
        raise HomeAssistantAPIError("엔티티 ID가 비어 있습니다.")

    state_text = str(raw.get("state") or "")
    attributes = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
    domain = entity_id.split(".", 1)[0].lower() if "." in entity_id else ""

    friendly_name = attributes.get("friendly_name")
    if not isinstance(friendly_name, str) or not friendly_name.strip():
        fallback = entity_id.split(".", 1)[-1]
        friendly_name = fallback.replace("_", " ") if fallback else entity_id
    else:
        friendly_name = friendly_name.strip()

    room = ""
    for key in ("room_name", "area", "area_name", "area_id", "floor", "floor_name"):
        value = attributes.get(key)
        if isinstance(value, str) and value.strip():
            room = value.strip()
            break

    can_toggle = domain in HOME_ASSISTANT_TOGGLE_DOMAINS
    online = state_text.lower() not in {"unavailable", "unknown"}
    icon = _home_assistant_pick_icon(domain)
    state_label = _home_assistant_state_label(state_text, attributes, can_toggle, online)
    on_state = None
    if state_text.lower() in {"on", "off"}:
        on_state = state_text.lower() == "on"

    return {
        "id": entity_id,
        "name": friendly_name,
        "room": room,
        "type": domain,
        "icon": icon,
        "online": online,
        "can_toggle": can_toggle,
        "traits": list(attributes.keys()),
        "state": {"on": on_state},
        "state_label": state_label,
    }


def home_assistant_list_devices() -> List[Dict[str, Any]]:
    session, timeout, cfg, base_url = _home_assistant_session()
    try:
        data = _home_assistant_request(
            session, "GET", "/api/states", timeout=timeout, base_url=base_url
        )
        if not isinstance(data, list):
            raise HomeAssistantAPIError("엔티티 목록을 받아오지 못했습니다.")

        formatted: List[Dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            entity_id = str(item.get("entity_id") or "")
            if not entity_id or "." not in entity_id:
                continue
            attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            if not _home_assistant_should_include(entity_id, attributes, cfg):
                continue
            try:
                formatted.append(_format_home_assistant_entity(item))
            except HomeAssistantError:
                continue

        formatted.sort(key=lambda d: ((d.get("room") or ""), d.get("name") or d.get("id") or ""))
        return formatted
    finally:
        try:
            session.close()
        except Exception:  # pragma: no cover - defensive
            pass


def home_assistant_execute(entity_id: str, turn_on: bool) -> Any:
    entity_id = (entity_id or "").strip()
    if not entity_id:
        raise HomeAssistantAPIError("유효한 Home Assistant 엔티티 ID가 필요합니다.")

    session, timeout, _cfg, base_url = _home_assistant_session()
    try:
        service = "turn_on" if bool(turn_on) else "turn_off"
        payload = {"entity_id": entity_id}
        return _home_assistant_request(
            session,
            "POST",
            f"/api/services/homeassistant/{service}",
            timeout=timeout,
            base_url=base_url,
            json_payload=payload,
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
    ha_cfg = CFG.get("home_assistant", {}) or {}
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
            "layout": frame_layout_snapshot(),
            "layout_defaults": {name: vals.copy() for name, vals in FRAME_LAYOUT_DEFAULTS.items()},
        },
        "home_assistant": {
            "base_url": ha_cfg.get("base_url", ""),
            "token": ha_cfg.get("token", ""),
            "include_domains": ha_cfg.get("include_domains", []),
            "include_entities": ha_cfg.get("include_entities", []),
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


@app.get("/api/frame-layout")
def api_frame_layout():
    orientation = request.args.get("orientation", "portrait")
    key = normalize_orientation(orientation)
    layout = get_layout_for_orientation(key)
    defaults = FRAME_LAYOUT_DEFAULTS.get(key, {})
    return jsonify(
        {
            "orientation": key,
            "layout": layout,
            "defaults": defaults,
            "all": {name: vals for name, vals in FRAME_LAYOUT_DEFAULTS.items()},
        }
    )


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

            layout_payload = section.get("layout")
            if isinstance(layout_payload, dict):
                if update_layout_config(layout_payload):
                    updated = True

        if "home_assistant" in payload:
            section = payload["home_assistant"] or {}
            cfg = CFG.setdefault("home_assistant", {})

            base_url = (section.get("base_url") or "").strip()
            token = (section.get("token") or "").strip()
            include_domains = section.get("include_domains")
            include_entities = section.get("include_entities")

            if base_url or "base_url" in section:
                cfg["base_url"] = base_url

            if token or "token" in section:
                cfg["token"] = token

            if isinstance(include_domains, list):
                cfg["include_domains"] = [
                    str(x).strip() for x in include_domains if str(x).strip()
                ]

            if isinstance(include_entities, list):
                cfg["include_entities"] = [
                    str(x).strip() for x in include_entities if str(x).strip()
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
        devices = home_assistant_list_devices()
        resp: Dict[str, Any] = {
            "devices": devices,
            "dashboard": {"title": "Home Assistant", "entity_count": len(devices)},
        }
        if not devices:
            resp["message"] = "Home Assistant에서 표시할 엔티티를 찾지 못했습니다."
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
# Board HTML moved to scal_app.templates.board.html



@app.get("/main")
def main_page():
    return render_template_string(load_main_html())


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
            ("2) Home Assistant 설정", "cfg_ha"),
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
        func=lambda c: c.data in {"cfg_ical", "cfg_ha", "cfg_bus", "cfg_photo", "cfg_weather", "cfg_verse"}
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
        elif c.data == "cfg_ha":
            kb = telebot.types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                telebot.types.InlineKeyboardButton("기본 URL", callback_data="ha_set_url"),
                telebot.types.InlineKeyboardButton("토큰", callback_data="ha_set_token"),
                telebot.types.InlineKeyboardButton("허용 도메인", callback_data="ha_set_domains"),
                telebot.types.InlineKeyboardButton("허용 엔티티", callback_data="ha_set_entities"),
                telebot.types.InlineKeyboardButton("현재 설정 보기", callback_data="ha_show_config"),
            )
            TB.send_message(chat_id, "Home Assistant 설정을 선택하세요.", reply_markup=kb)
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
        in {"ha_set_url", "ha_set_token", "ha_set_domains", "ha_set_entities", "ha_show_config"}
    )
    def on_home_assistant_callbacks(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "권한이 없습니다.")
            return
        chat_id = c.message.chat.id
        uid = c.from_user.id
        TB.answer_callback_query(c.id)

        if c.data == "ha_set_url":
            _set_state(uid, {"mode": "await_ha_url"})
            TB.send_message(
                chat_id,
                "Home Assistant 기본 URL을 입력하세요. 예: https://homeassistant.local:8123",
            )
        elif c.data == "ha_set_token":
            _set_state(uid, {"mode": "await_ha_token"})
            TB.send_message(chat_id, "Home Assistant 장기 액세스 토큰을 입력하세요. /cancel 로 취소")
        elif c.data == "ha_set_domains":
            _set_state(uid, {"mode": "await_ha_domains"})
            TB.send_message(
                chat_id,
                "표시할 도메인을 콤마로 구분해 입력하세요. 비우면 기본값(light,switch). /cancel",
            )
        elif c.data == "ha_set_entities":
            _set_state(uid, {"mode": "await_ha_entities"})
            TB.send_message(
                chat_id,
                "표시할 엔티티 ID를 콤마로 구분해 입력하세요. 비우면 도메인 기준. /cancel",
            )
        elif c.data == "ha_show_config":
            cfg = CFG.get("home_assistant", {}) or {}
            base_url = cfg.get("base_url") or "설정안됨"
            token = _mask_secret(cfg.get("token", ""))
            domains = cfg.get("include_domains") or []
            entities = cfg.get("include_entities") or []
            dom_txt = ", ".join(domains) if domains else "기본값"
            ent_txt = ", ".join(entities) if entities else "도메인 기준"
            lines = [
                f"기본 URL: {base_url}",
                f"토큰: {token}",
                f"허용 도메인: {dom_txt}",
                f"허용 엔티티: {ent_txt}",
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
        elif mode == "await_ha_url":
            CFG.setdefault("home_assistant", {})["base_url"] = text
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "기본 URL이 저장되었습니다.")
        elif mode == "await_ha_token":
            CFG.setdefault("home_assistant", {})["token"] = text
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "토큰이 저장되었습니다.")
        elif mode == "await_ha_domains":
            items = [seg.strip() for seg in re.split(r"[\s,]+", text) if seg.strip()]
            CFG.setdefault("home_assistant", {})["include_domains"] = items
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "허용 도메인 목록이 저장되었습니다.")
        elif mode == "await_ha_entities":
            items = [seg.strip() for seg in re.split(r"[\s,]+", text) if seg.strip()]
            CFG.setdefault("home_assistant", {})["include_entities"] = items
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "허용 엔티티 목록이 저장되었습니다.")
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
