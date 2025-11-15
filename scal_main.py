#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Smart Frame application powering the smart dashboard experience."""

from __future__ import annotations

# Code below is organized with clearly marked sections.
# Search for lines like `# === [SECTION: ...] ===` to navigate.

# === [SECTION: Imports / Standard & Third-party] ==============================
import os, time, secrets, re, xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
import html
import logging
from flask import Flask, request, jsonify, render_template_string, abort, send_from_directory
from PIL import Image, ImageOps
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bus")
from scal_app.config import (
    CFG,
    TZ,
    TZ_NAME,
    PHOTOS_DIR,
    get_verse,
    set_verse,
    save_config_to_source,
    FRAME_LAYOUT_DEFAULTS,
    frame_layout_snapshot,
    get_layout_for_orientation,
    normalize_orientation,
    update_layout_config,
    load_todos,
    save_todos,
)
from scal_app.services.weather import fetch_weather, fetch_air_quality
from scal_app.services.bus import get_bus_arrivals, render_bus_box, pick_text
from scal_app.templates import load_board_html, load_settings_html, load_main_html

# === [SECTION: Photo processing helpers] =====================================

def _pil_resample_lanczos():
    """Return the best available LANCZOS resampling filter."""
    resampling = getattr(Image, "Resampling", None)
    if resampling is not None and hasattr(resampling, "LANCZOS"):
        return resampling.LANCZOS
    return getattr(Image, "LANCZOS")


def _normalize_format(fmt: str) -> str:
    fmt = (fmt or "").strip().upper()
    if fmt == "JPG":
        return "JPEG"
    return fmt


def _save_pil_image(img: Image.Image, dest: Path, img_format: str) -> None:
    fmt = _normalize_format(img_format or dest.suffix.lstrip("."))
    save_kwargs = {}
    if fmt:
        save_kwargs["format"] = fmt
        if fmt == "JPEG" and img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
    img.save(dest, **save_kwargs)


def _resize_to_frame_height(img: Image.Image) -> Image.Image:
    width, height = img.size
    if height <= 0:
        return img
    if height > 1080:
        scale = 1080.0 / float(height)
        new_width = max(1, int(round(width * scale)))
        new_size = (new_width, 1080)
        if (width, height) != new_size:
            img = img.resize(new_size, _pil_resample_lanczos())
    return img


def process_uploaded_photo(dest: Path) -> None:
    """Rotate & resize landscape photos for the vertical frame layout."""
    if not dest.exists():
        return
    with Image.open(dest) as img:
        original_format = img.format or dest.suffix.lstrip(".")
        img = ImageOps.exif_transpose(img)
        width, height = img.size
        is_landscape = width > height
        if is_landscape:
            img = img.rotate(90, expand=True)
        img = _resize_to_frame_height(img)
        _save_pil_image(img, dest, original_format)


def rotate_photo_file(dest: Path, angle: int) -> int:
    """Rotate photo file by multiples of 90 degrees and keep frame sizing."""
    if not dest.exists():
        raise FileNotFoundError(dest)
    normalized = angle % 360
    with Image.open(dest) as img:
        original_format = img.format or dest.suffix.lstrip(".")
        img = ImageOps.exif_transpose(img)
        if normalized:
            img = img.rotate(normalized, expand=True)
        img = _resize_to_frame_height(img)
        _save_pil_image(img, dest, original_format)
    return normalized

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
        "verse": {"text": get_verse()},
    }


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
TODO_DATE_FMT = "%Y-%m-%d"


def _generate_todo_id() -> str:
    return secrets.token_urlsafe(8)


def _normalize_due_date(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        datetime.strptime(text, TODO_DATE_FMT)
    except ValueError as exc:  # pragma: no cover - defensive validation
        raise ValueError("날짜는 YYYY-MM-DD 형식으로 입력하세요.") from exc
    return text


def _normalize_loaded_todo(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Invalid todo entry")
    title = str(raw.get("title") or raw.get("text") or "").strip()
    if not title:
        raise ValueError("할 일 제목이 비어 있습니다.")
    due_date = _normalize_due_date(raw.get("due_date") or raw.get("due"))
    todo_id = str(raw.get("id") or raw.get("todo_id") or "").strip() or _generate_todo_id()
    completed = bool(raw.get("completed"))
    created = str(raw.get("created_at") or "").strip()
    if not created:
        created = datetime.now(TZ).isoformat()
    updated = str(raw.get("updated_at") or "").strip() or created
    return {
        "id": todo_id,
        "title": title,
        "due_date": due_date,
        "completed": completed,
        "created_at": created,
        "updated_at": updated,
    }


def _todo_sort_key(item: Dict[str, Any]) -> Tuple[int, str, str, str]:
    completed = 1 if item.get("completed") else 0
    due = item.get("due_date") or "9999-12-31"
    created = item.get("created_at") or ""
    title = str(item.get("title") or "")
    return (completed, due, created, title.lower())


def _load_todo_items() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for raw in load_todos():
        try:
            items.append(_normalize_loaded_todo(raw))
        except Exception:
            logging.getLogger(__name__).debug("Ignoring invalid todo entry", exc_info=True)
    items.sort(key=_todo_sort_key)
    return items


def _persist_todo_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items.sort(key=_todo_sort_key)
    save_todos(items)
    return items


def _serialize_todos(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(item) for item in items]


@app.get("/api/todo")
def api_todo():
    items = _load_todo_items()
    return jsonify({"items": _serialize_todos(items)})


@app.post("/api/todo")
def api_todo_create():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title") or "").strip()
    if not title:
        return jsonify({"error": "할 일 내용을 입력하세요."}), 400
    try:
        due_date = _normalize_due_date(payload.get("due_date"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    completed = bool(payload.get("completed"))
    now_iso = datetime.now(TZ).isoformat()
    item = {
        "id": _generate_todo_id(),
        "title": title,
        "due_date": due_date,
        "completed": completed,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    items = _load_todo_items()
    items.append(item)
    persisted = _persist_todo_items(items)
    return jsonify({"item": item, "items": _serialize_todos(persisted)}), 201


@app.route("/api/todo/<todo_id>", methods=["PUT", "PATCH"])
def api_todo_update(todo_id: str):
    payload = request.get_json(silent=True) or {}
    items = _load_todo_items()
    for item in items:
        if item["id"] == todo_id:
            break
    else:
        return jsonify({"error": "할 일을 찾을 수 없습니다."}), 404

    if "title" in payload:
        title = str(payload.get("title") or "").strip()
        if not title:
            return jsonify({"error": "할 일 내용을 입력하세요."}), 400
        item["title"] = title

    if "due_date" in payload:
        try:
            item["due_date"] = _normalize_due_date(payload.get("due_date"))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    if "completed" in payload:
        item["completed"] = bool(payload.get("completed"))

    item["updated_at"] = datetime.now(TZ).isoformat()
    persisted = _persist_todo_items(items)
    return jsonify({"item": item, "items": _serialize_todos(persisted)})


@app.delete("/api/todo/<todo_id>")
def api_todo_delete(todo_id: str):
    items = _load_todo_items()
    new_items = [item for item in items if item.get("id") != todo_id]
    if len(new_items) == len(items):
        return jsonify({"error": "할 일을 찾을 수 없습니다."}), 404
    persisted = _persist_todo_items(new_items)
    return jsonify({"items": _serialize_todos(persisted), "success": True})


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
        process_uploaded_photo(dest)
    except Exception as exc:
        try:
            dest.unlink()
        except Exception:
            pass
        return jsonify({"error": f"업로드 실패: {exc}"}), 500
    return jsonify({"success": True, "filename": new_name})


@app.post("/api/photos/<path:fname>/rotate")
def api_rotate_photo(fname: str):
    target = PHOTOS_DIR / fname
    if not _is_safe_photo_path(target):
        return jsonify({"error": "잘못된 경로입니다."}), 400
    if not target.exists():
        return jsonify({"error": "파일을 찾을 수 없습니다."}), 404

    payload = request.get_json(silent=True) or {}
    direction = str(payload.get("direction") or payload.get("dir") or "").strip().lower()
    angle_value = payload.get("angle")
    steps_value = payload.get("steps")

    angle: Optional[int] = None
    if angle_value is not None:
        try:
            angle = int(angle_value)
        except (TypeError, ValueError):
            return jsonify({"error": "회전 각도는 숫자여야 합니다."}), 400
    elif steps_value is not None:
        try:
            angle = int(steps_value) * 90
        except (TypeError, ValueError):
            return jsonify({"error": "회전 단계는 숫자여야 합니다."}), 400
    elif direction:
        if direction in {"clockwise", "cw", "right"}:
            angle = -90
        elif direction in {"counterclockwise", "ccw", "left"}:
            angle = 90
        elif direction in {"flip", "half", "180"}:
            angle = 180
    if angle is None:
        angle = -90  # default: rotate clockwise

    if angle % 90 != 0:
        return jsonify({"error": "회전은 90도 단위로만 가능합니다."}), 400

    try:
        normalized = rotate_photo_file(target, angle)
    except FileNotFoundError:
        return jsonify({"error": "파일을 찾을 수 없습니다."}), 404
    except Exception as exc:
        return jsonify({"error": f"회전에 실패했습니다: {exc}"}), 500

    return jsonify({"success": True, "angle": angle, "normalized_angle": normalized})


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

# === [SECTION: App entrypoint (web only)] ===================================
def run_web():
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
    print(f"[WEB] starting on :{CFG['server']['port']}  -> /board")
    run_web()


if __name__ == "__main__":
    main()
