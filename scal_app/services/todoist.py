"""Todoist REST API helpers for Smart Frame."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import logging
import time

import requests

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - fallback when zoneinfo unavailable
    ZoneInfo = None  # type: ignore

LOGGER = logging.getLogger(__name__)

API_URL = "https://api.todoist.com/rest/v2/tasks"
CACHE_TTL = 120  # seconds


class TodoistAPIError(RuntimeError):
    """Raised when Todoist API requests fail."""


@dataclass
class _CacheEntry:
    ts: float
    items: List[Dict[str, object]]


_CACHE: Dict[Tuple[str, str], _CacheEntry] = {}


def _normalize_token(token: str) -> str:
    return token.strip()


def _normalize_project(project_id: Optional[str]) -> str:
    return (project_id or "").strip()


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _apply_timezone(dt: datetime, tzname: Optional[str]) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    if tzname and ZoneInfo is not None:
        try:
            zone = ZoneInfo(tzname)
        except Exception:  # pragma: no cover - invalid tz name
            LOGGER.debug("Todoist timezone '%s' not found", tzname)
        else:
            return dt.astimezone(zone)
    return dt


def _format_due_label(due_dt: Optional[datetime], all_day_date: Optional[datetime], tz) -> Tuple[str, bool]:
    now = datetime.now(tz)
    overdue = False
    if due_dt:
        local = due_dt.astimezone(tz)
        overdue = local < now
        delta = (local.date() - now.date()).days
        if delta == 0:
            return f"오늘 {local.strftime('%H:%M')}", overdue
        if delta == 1:
            return f"내일 {local.strftime('%H:%M')}", overdue
        if -1 <= delta <= 6:
            weekday = ["월", "화", "수", "목", "금", "토", "일"][local.weekday()]
            return f"{local.strftime('%m/%d')}({weekday}) {local.strftime('%H:%M')}", overdue
        return f"{local.strftime('%Y/%m/%d %H:%M')}", overdue
    if all_day_date:
        local = all_day_date.astimezone(tz)
        overdue = local.date() < now.date()
        delta = (local.date() - now.date()).days
        if delta == 0:
            return "오늘", overdue
        if delta == 1:
            return "내일", overdue
        if -1 <= delta <= 6:
            weekday = ["월", "화", "수", "목", "금", "토", "일"][local.weekday()]
            return f"{local.strftime('%m/%d')}({weekday})", overdue
        return f"{local.strftime('%Y/%m/%d')}", overdue
    return "—", overdue


def fetch_tasks(
    token: str,
    *,
    project_id: Optional[str] = None,
    limit: int = 12,
    tz=timezone.utc,
) -> List[Dict[str, object]]:
    """Fetch active Todoist tasks for the configured account."""

    token = _normalize_token(token)
    project_id = _normalize_project(project_id)
    if not token:
        return []

    cache_key = (token, project_id)
    now = time.time()
    cached = _CACHE.get(cache_key)
    if cached and now - cached.ts < CACHE_TTL:
        return cached.items

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    params = {}
    if project_id:
        params["project_id"] = project_id

    try:
        response = requests.get(API_URL, headers=headers, params=params or None, timeout=10)
    except requests.RequestException as exc:  # pragma: no cover - network
        raise TodoistAPIError(f"Todoist 호출 실패: {exc}") from exc

    if response.status_code == 401:
        raise TodoistAPIError("Todoist API 토큰이 올바르지 않습니다.")

    if not response.ok:
        raise TodoistAPIError(f"Todoist 오류: HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise TodoistAPIError("Todoist 응답을 파싱하지 못했습니다.") from exc

    items: List[Dict[str, object]] = []
    now_dt = datetime.now(tz)
    for task in payload:
        content = (task.get("content") or "").strip()
        if not content:
            continue

        due = task.get("due") or {}
        due_dt = None
        all_day_dt = None
        due_iso = None
        if isinstance(due, dict):
            due_dt_raw = due.get("datetime")
            tzname = due.get("timezone") or None
            parsed = _parse_iso_datetime(due_dt_raw) if due_dt_raw else None
            if parsed:
                due_dt = _apply_timezone(parsed, tzname)
                due_iso = due_dt.astimezone(timezone.utc).isoformat()
            elif due.get("date"):
                date_str = due.get("date")
                try:
                    all_day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    all_day = None
                if all_day:
                    if tzname and ZoneInfo is not None:
                        try:
                            zone = ZoneInfo(tzname)
                            all_day = all_day.astimezone(zone)
                        except Exception:  # pragma: no cover - invalid tz name
                            pass
                    all_day_dt = all_day
                    due_iso = all_day.astimezone(timezone.utc).isoformat()

        due_label, overdue = _format_due_label(due_dt, all_day_dt, tz)

        ordering_key = due_dt or all_day_dt or (now_dt + timedelta(days=365))

        items.append(
            {
                "id": str(task.get("id")),
                "title": content,
                "due_label": due_label,
                "overdue": overdue,
                "url": task.get("url"),
                "due_iso": due_iso,
                "order": ordering_key,
            }
        )

    items.sort(key=lambda x: (x["order"], x["title"]))
    trimmed = items[:limit]
    for item in trimmed:
        item.pop("order", None)

    _CACHE[cache_key] = _CacheEntry(ts=now, items=trimmed)
    return trimmed
