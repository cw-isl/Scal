"""Configuration, state persistence, and embedded block helpers."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any, Dict

LOGGER = logging.getLogger(__name__)

CFG_START = "# ==== EMBEDDED_CONFIG (JSON) START"
CFG_END = "# ==== EMBEDDED_CONFIG (JSON) END"
VER_START = "# ==== EMBEDDED_VERSES START"
VER_END = "# ==== EMBEDDED_VERSES END"

BASE = Path(os.environ.get("SCAL_DATA_DIR", "/root/scal")).expanduser()
STATE_PATH = BASE / "sframe_state.json"
PHOTOS_DIR = BASE / "frame_photos"
GCLIENT_PATH = BASE / "google_client_secret.json"
GTOKEN_PATH = BASE / "google_token.json"

BASE.mkdir(parents=True, exist_ok=True)
PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_SOURCE = Path(
    os.environ.get("SCAL_CONFIG_SOURCE", "Scal/scal_full_integrated.py")
).resolve()


def set_config_source(path: Path) -> None:
    """Set the python source file that stores embedded configuration blocks."""
    global CONFIG_SOURCE
    CONFIG_SOURCE = Path(path).resolve()


def _extract_block(src_text: str, start_tag: str, end_tag: str):
    s = src_text.find(start_tag)
    e = src_text.find(end_tag)
    if s == -1 or e == -1 or e <= s:
        raise RuntimeError(f"Marker not found: {start_tag}..{end_tag}")
    s_body = src_text.find("\n", s) + 1
    e_body = e
    return s_body, e_body, src_text[s_body:e_body]


def _replace_block_in_text(src_text: str, start_tag: str, end_tag: str, new_body: str) -> str:
    s_body, e_body, _old = _extract_block(src_text, start_tag, end_tag)
    if not new_body.endswith("\n"):
        new_body += "\n"
    return src_text[:s_body] + new_body + src_text[e_body:]


def _atomic_write(path: Path, data: str) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=directory, encoding="utf-8") as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _read_block(start_tag: str, end_tag: str, file_path: Path | None = None) -> str:
    file_path = file_path or CONFIG_SOURCE
    with open(file_path, "r", encoding="utf-8") as handle:
        src = handle.read()
    _, _, body = _extract_block(src, start_tag, end_tag)
    import re as _re

    match = _re.search(r'r?"""\s*([\s\S]*?)\s*"""', body)
    return match.group(1) if match else body


def _write_block(new_text: str, start_tag: str, end_tag: str, file_path: Path | None = None):
    file_path = file_path or CONFIG_SOURCE
    with open(file_path, "r", encoding="utf-8") as handle:
        src = handle.read()
    varname = "EMBEDDED_CONFIG" if "CONFIG" in start_tag else "EMBEDDED_VERSES"
    wrapped = varname + ' = r"""' + new_text + '"""'
    _atomic_write(file_path, _replace_block_in_text(src, start_tag, end_tag, wrapped))


def load_config_from_embedded(defaults: Dict[str, Any]) -> Dict[str, Any]:
    data = json.loads(_read_block(CFG_START, CFG_END) or "{}")

    def deep_fill(dst: Dict[str, Any], src: Dict[str, Any]):
        for key, value in src.items():
            if key not in dst:
                dst[key] = value
            elif isinstance(value, dict):
                dst[key] = deep_fill(dst.get(key, {}) or {}, value)
        return dst

    return deep_fill(data, defaults)


def save_config_to_source(new_data: Dict[str, Any], file_path: Path | None = None):
    file_path = file_path or CONFIG_SOURCE
    json_text = json.dumps(new_data, ensure_ascii=False, indent=2)
    _write_block(json_text, CFG_START, CFG_END, file_path=file_path)


DEFAULT_CFG: Dict[str, Any] = {
    "server": {"port": 5320},
    "frame": {"tz": "Asia/Seoul", "ical_url": ""},
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
    "google": {
        "scopes": ["https://www.googleapis.com/auth/calendar.events"],
        "calendar": {"id": "primary"},
    },
    "home_assistant": {
        "base_url": "http://localhost:8123",
        "token": "",
        "verify_ssl": True,
        "timeout": 5,
        "include_domains": ["light", "switch"],
        "include_entities": [],
        "dashboard": {"url_path": "", "title": ""},
    },
    "todoist": {
        "api_token": "",
        "filter": "today | overdue",
        "project_id": "",
        "max_items": 20,
    },
    "bus": {"city_code": "", "node_id": "", "key": ""},
}

CFG: Dict[str, Any] = load_config_from_embedded(DEFAULT_CFG)

TZ = timezone(timedelta(hours=9)) if CFG["frame"]["tz"] == "Asia/Seoul" else timezone.utc
TZ_NAME = "Asia/Seoul" if CFG["frame"]["tz"] == "Asia/Seoul" else "UTC"


def get_verse() -> str:
    return _read_block(VER_START, VER_END).strip()


def set_verse(text: str) -> None:
    _write_block((text or "").strip(), VER_START, VER_END)
    try:
        (BASE / "verse.txt").write_text((text or "").strip() + "\n", encoding="utf-8")
    except Exception as exc:  # pragma: no cover - best effort persistence
        LOGGER.debug("Failed to mirror verse.txt: %s", exc)


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(data: Dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
