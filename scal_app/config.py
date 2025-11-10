"""Configuration, state persistence, and configuration file helpers."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Dict

LOGGER = logging.getLogger(__name__)

BASE = Path(os.environ.get("SCAL_DATA_DIR", "/root/scal")).expanduser()
STATE_PATH = BASE / "sframe_state.json"
PHOTOS_DIR = BASE / "frame_photos"
GCLIENT_PATH = BASE / "google_client_secret.json"
GTOKEN_PATH = BASE / "google_token.json"
CONFIG_PATH = Path(os.environ.get("SCAL_CONFIG_FILE", BASE / "config.yaml")).expanduser()
VERSE_PATH = Path(os.environ.get("SCAL_VERSE_FILE", BASE / "verse.txt")).expanduser()

BASE.mkdir(parents=True, exist_ok=True)
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)


def set_config_source(path: Path) -> None:
    """Set the configuration file location (kept for backward compatibility)."""

    global CONFIG_PATH
    CONFIG_PATH = Path(path).expanduser()


def _atomic_write(path: Path, data: str) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=directory, encoding="utf-8") as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _load_structured(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        LOGGER.warning("Failed to read config file: %%s", path, exc_info=True)
        return {}
    if not text.strip():
        return {}
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text)  # type: ignore[attr-defined]
        if loaded is None:
            return {}
        if not isinstance(loaded, dict):
            raise TypeError("Configuration file must contain a mapping at the top level")
        return loaded
    except ModuleNotFoundError:
        LOGGER.debug("PyYAML not available, falling back to JSON parsing for config file")
    except Exception:
        LOGGER.warning("Failed to parse config file as YAML; attempting JSON", exc_info=True)
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
        raise TypeError("Configuration JSON must be an object")
    except Exception:
        LOGGER.error("Failed to parse configuration file; using defaults", exc_info=True)
        return {}


def _deep_update(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in updates.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            base[key] = _deep_update(base[key], value)
        else:
            base[key] = value
    return base


def load_config(defaults: Dict[str, Any]) -> Dict[str, Any]:
    loaded = _load_structured(CONFIG_PATH)
    if not loaded:
        return defaults.copy()
    merged = defaults.copy()
    return _deep_update(merged, loaded)


def save_config_to_source(new_data: Dict[str, Any], file_path: Path | None = None):
    file_path = Path(file_path) if file_path else CONFIG_PATH
    try:
        import yaml  # type: ignore

        text = yaml.safe_dump(  # type: ignore[attr-defined]
            new_data,
            allow_unicode=True,
            sort_keys=True,
        )
    except ModuleNotFoundError:
        LOGGER.debug("PyYAML not available; writing config as JSON")
        text = json.dumps(new_data, ensure_ascii=False, indent=2)
    except Exception:
        LOGGER.warning("Failed to dump config as YAML; falling back to JSON", exc_info=True)
        text = json.dumps(new_data, ensure_ascii=False, indent=2)
    _atomic_write(file_path, text)


FRAME_LAYOUT_KEYS = (
    "width",
    "height",
    "top",
    "calendar",
    "weather",
    "layout_left",
    "section_gap",
)

FRAME_LAYOUT_DEFAULTS: Dict[str, Dict[str, int]] = {
    "portrait": {
        "width": 1080,
        "height": 1920,
        "top": 90,
        "calendar": 1020,
        "weather": 320,
        "layout_left": 460,
        "section_gap": 26,
    },
    "landscape_right": {
        "width": 1080,
        "height": 1920,
        "top": 90,
        "calendar": 1020,
        "weather": 320,
        "layout_left": 460,
        "section_gap": 26,
    },
    "landscape_left": {
        "width": 1080,
        "height": 1920,
        "top": 90,
        "calendar": 1020,
        "weather": 320,
        "layout_left": 460,
        "section_gap": 26,
    },
}

FRAME_ORIENTATION_ALIASES = {
    "portrait": "portrait",
    "default": "portrait",
    "normal": "portrait",
    "vertical": "portrait",
    "landscape_right": "landscape_right",
    "right": "landscape_right",
    "rotate_90": "landscape_right",
    "rotate90": "landscape_right",
    "rotate-90": "landscape_left",
    "cw": "landscape_right",
    "clockwise": "landscape_right",
    "landscape_left": "landscape_left",
    "left": "landscape_left",
    "rotate_-90": "landscape_left",
    "rotate_neg_90": "landscape_left",
    "rotate--90": "landscape_left",
    "ccw": "landscape_left",
    "counterclockwise": "landscape_left",
}


DEFAULT_CFG: Dict[str, Any] = {
    "server": {"port": 5320},
    "frame": {
        "tz": "Asia/Seoul",
        "ical_url": "",
        "calendars": [],
        "layout": {k: v.copy() for k, v in FRAME_LAYOUT_DEFAULTS.items()},
    },
    "weather": {
        "provider": "openweathermap",
        "api_key": "",
        "location": "Seoul, South Korea",
        "units": "metric",
    },
    "telegram": {
        "bot_token": "",
        "allowed_user_ids": [],
        "mode": "polling",
        "webhook_base": "",
        "path_secret": "",
    },
    "home_assistant": {
        "base_url": "http://homeassistant.local:8123",
        "token": "",
        "timeout": 10,
        "include_domains": [
            "light",
            "switch",
        ],
        "include_entities": [],
    },
    "bus": {"city_code": "", "node_id": "", "key": ""},
    "photos": {"album": "default"},
    "todoist": {"api_token": "", "project_id": ""},
}

CFG: Dict[str, Any] = load_config(DEFAULT_CFG)

TZ = timezone(timedelta(hours=9)) if CFG["frame"].get("tz") == "Asia/Seoul" else timezone.utc
TZ_NAME = "Asia/Seoul" if CFG["frame"].get("tz") == "Asia/Seoul" else "UTC"


frame_cfg = CFG.setdefault("frame", {})
if not isinstance(frame_cfg.get("calendars"), list):
    frame_cfg["calendars"] = []

layout_cfg = frame_cfg.setdefault("layout", {})
for orient, defaults in FRAME_LAYOUT_DEFAULTS.items():
    orient_cfg = layout_cfg.setdefault(orient, {})
    for key, value in defaults.items():
        orient_cfg.setdefault(key, value)


def normalize_orientation(value: str | None) -> str:
    key = (value or "").strip().lower()
    key = key.replace(" ", "_").replace("-", "_")
    while "__" in key:
        key = key.replace("__", "_")
    key = key.strip("_")
    return FRAME_ORIENTATION_ALIASES.get(key, "portrait")


def get_layout_for_orientation(orientation: str) -> Dict[str, int]:
    key = normalize_orientation(orientation)
    layout_cfg = CFG.setdefault("frame", {}).setdefault("layout", {})
    result = FRAME_LAYOUT_DEFAULTS.get(key, {}).copy()
    stored = layout_cfg.get(key, {})
    if isinstance(stored, dict):
        for field in FRAME_LAYOUT_KEYS:
            value = stored.get(field)
            if isinstance(value, (int, float)):
                result[field] = int(value)
            else:
                try:
                    if isinstance(value, str) and value.strip():
                        result[field] = int(float(value))
                except Exception:  # pragma: no cover - defensive
                    continue
    return result


def update_layout_config(updates: Dict[str, Any]) -> bool:
    if not isinstance(updates, dict):
        return False
    frame_cfg = CFG.setdefault("frame", {})
    layout_cfg = frame_cfg.setdefault("layout", {})
    changed = False
    for name, payload in updates.items():
        key = normalize_orientation(name)
        if not isinstance(payload, dict):
            continue
        dest = layout_cfg.setdefault(key, {})
        for field in FRAME_LAYOUT_KEYS:
            if field not in payload:
                continue
            raw = payload[field]
            try:
                if raw is None or raw == "":
                    continue
                value = int(float(raw))
            except Exception:
                continue
            if value <= 0:
                continue
            if dest.get(field) != value:
                dest[field] = value
                changed = True
    return changed


def frame_layout_snapshot() -> Dict[str, Dict[str, int]]:
    layout_cfg = CFG.setdefault("frame", {}).setdefault("layout", {})
    snapshot: Dict[str, Dict[str, int]] = {}
    for orient in FRAME_LAYOUT_DEFAULTS.keys():
        snapshot[orient] = get_layout_for_orientation(orient)
    for orient, data in layout_cfg.items():
        key = normalize_orientation(orient)
        if key not in snapshot:
            snapshot[key] = get_layout_for_orientation(key)
    return snapshot

if not frame_cfg["calendars"] and frame_cfg.get("ical_url"):
    frame_cfg["calendars"] = [
        {"url": frame_cfg.get("ical_url", ""), "color": "#4b6bff"}
    ]

ha_cfg = CFG.setdefault("home_assistant", {})
if not isinstance(ha_cfg.get("include_domains"), list):
    ha_cfg["include_domains"] = []
if not isinstance(ha_cfg.get("include_entities"), list):
    ha_cfg["include_entities"] = []


def get_verse() -> str:
    if VERSE_PATH.exists():
        try:
            return VERSE_PATH.read_text(encoding="utf-8").strip()
        except Exception:
            LOGGER.warning("Failed to read verse file", exc_info=True)
    return ""


def set_verse(text: str) -> None:
    value = (text or "").strip()
    try:
        _atomic_write(VERSE_PATH, value + ("\n" if value else ""))
    except Exception:
        LOGGER.warning("Failed to store verse", exc_info=True)


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(data: Dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
