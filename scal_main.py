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
from typing import Any, Dict, List, Optional, Set, Tuple
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
from scal_app.templates import load_board_html, load_settings_html

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
    allowed_ids = tg_cfg.get("allowed_user_ids") or []
    allowed_text = ", ".join(str(x) for x in allowed_ids)
    return {
        "frame": {"ical_url": frame_cfg.get("ical_url", "")},
        "home_assistant": {
            "base_url": ha_cfg.get("base_url", ""),
            "token": ha_cfg.get("token", ""),
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
    """Return an empty Todo list placeholder for the board UI."""

    return jsonify({"items": []})


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
            ical = (payload["frame"].get("ical_url") or "").strip()
            if ical and not re.match(r"^https?://", ical, re.IGNORECASE):
                errors.append("iCal URL은 http:// 또는 https:// 로 시작해야 합니다.")
            else:
                CFG.setdefault("frame", {})["ical_url"] = ical
                updated = True

        if "home_assistant" in payload:
            section = payload["home_assistant"] or {}
            base = (section.get("base_url") or "").strip()
            token = (section.get("token") or "").strip()
            if base and not re.match(r"^https?://", base, re.IGNORECASE):
                errors.append("Home Assistant URL은 http:// 또는 https:// 로 시작해야 합니다.")
            else:
                CFG.setdefault("home_assistant", {})["base_url"] = _normalize_base_url(base)
                CFG.setdefault("home_assistant", {})["token"] = token
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
            current = CFG.get("frame", {}).get("ical_url") or "(미설정)"
            _set_state(uid, {"mode": "await_ical"})
            TB.send_message(
                chat_id,
                f"현재 iCal URL:\n{current}\n새 URL을 입력하거나 /cancel 로 취소하세요.",
            )
        elif c.data == "cfg_ha":
            kb = telebot.types.InlineKeyboardMarkup(row_width=1)
            kb.add(
                telebot.types.InlineKeyboardButton("베이스 URL 입력", callback_data="ha_set_base"),
                telebot.types.InlineKeyboardButton("토큰 입력", callback_data="ha_set_token"),
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


    @TB.callback_query_handler(func=lambda c: c.data in {"ha_set_base", "ha_set_token", "ha_show_config"})
    def on_home_assistant_callbacks(c):
        if not allowed(c.from_user.id):
            TB.answer_callback_query(c.id, "권한이 없습니다.")
            return
        chat_id = c.message.chat.id
        uid = c.from_user.id
        TB.answer_callback_query(c.id)

        if c.data == "ha_set_base":
            _set_state(uid, {"mode": "await_ha_base"})
            TB.send_message(chat_id, "Home Assistant 베이스 URL을 입력하세요 (http/https). /cancel 로 취소")
        elif c.data == "ha_set_token":
            _set_state(uid, {"mode": "await_ha_token"})
            TB.send_message(chat_id, "Home Assistant 장기 토큰을 입력하세요. /cancel 로 취소")
        elif c.data == "ha_show_config":
            cfg = _home_assistant_cfg()
            base = cfg.get("base_url") or "설정안됨"
            token = _mask_secret(cfg.get("token", ""))
            dash = cfg.get("dashboard", {}) or {}
            dash_txt = "설정안됨"
            if dash.get("title") or dash.get("url_path"):
                dash_txt = f"{dash.get('title') or dash.get('url_path')} ({dash.get('entity_count', 0)}개)"
            lines = [
                f"베이스 URL: {base}",
                f"토큰: {token}",
                f"대시보드: {dash_txt}",
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
            CFG.setdefault("frame", {})["ical_url"] = text
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "iCal URL이 저장되었습니다. 보드는 잠시 후 갱신됩니다.")
        elif mode == "await_verse":
            set_verse(text)
            _clear_state(uid)
            TB.reply_to(m, "오늘의 한마디가 저장되었습니다.")
        elif mode == "await_ha_base":
            if not re.match(r"^https?://", text, re.IGNORECASE):
                TB.reply_to(m, "http:// 또는 https:// 로 시작하는 주소를 입력하세요.")
                return
            normalized = _normalize_base_url(text)
            if not normalized:
                TB.reply_to(m, "유효한 URL을 입력해주세요.")
                return
            CFG.setdefault("home_assistant", {})["base_url"] = normalized
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, f"Home Assistant URL 저장 완료: {normalized}")
        elif mode == "await_ha_token":
            CFG.setdefault("home_assistant", {})["token"] = text
            save_config_to_source(CFG)
            _clear_state(uid)
            TB.reply_to(m, "토큰이 저장되었습니다.")
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
