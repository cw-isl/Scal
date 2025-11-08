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


DEFAULT_CFG: Dict[str, Any] = {
    "server": {"port": 5320},
    "frame": {
        "tz": "Asia/Seoul",
        "ical_url": "",
        "calendars": [],
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
    "google_home": {
        "service_account_file": str(BASE / "google-home-service-account.json"),
        "agent_user_id": "",
        "timeout": 10,
        "include_types": [
            "action.devices.types.LIGHT",
            "action.devices.types.SWITCH",
        ],
        "include_devices": [],
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

if not frame_cfg["calendars"] and frame_cfg.get("ical_url"):
    frame_cfg["calendars"] = [
        {"url": frame_cfg.get("ical_url", ""), "color": "#4b6bff"}
    ]

google_cfg = CFG.setdefault("google_home", {})
if not isinstance(google_cfg.get("include_types"), list):
    google_cfg["include_types"] = []
if not isinstance(google_cfg.get("include_devices"), list):
    google_cfg["include_devices"] = []


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
