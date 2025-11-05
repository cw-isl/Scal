"""Weather and air-quality helpers."""
from __future__ import annotations

import collections
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

from ..config import CFG, TZ

_weather_cache: Dict[str, Any] = {"key": "", "loc": "", "ts": 0.0, "data": None}
_air_cache: Dict[str, Any] = {"key": "", "loc": "", "ts": 0.0, "data": None}


def _owm_geocode(query: str, api_key: str) -> tuple[float, float]:
    url = "https://api.openweathermap.org/geo/1.0/direct"
    response = requests.get(
        url,
        params={"q": query, "limit": 1, "appid": api_key},
        timeout=10,
    )
    response.raise_for_status()
    items = response.json()
    if not items:
        raise RuntimeError("Location not found")
    return float(items[0]["lat"]), float(items[0]["lon"])


def _owm_fetch_onecall(lat: float, lon: float, key: str, units: str) -> Dict[str, Any]:
    response = requests.get(
        "https://api.openweathermap.org/data/3.0/onecall",
        params={
            "lat": lat,
            "lon": lon,
            "appid": key,
            "units": units,
            "exclude": "minutely,hourly,alerts",
        },
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()

    def icon_url(code: str) -> str:
        return f"https://openweathermap.org/img/wn/{code}@2x.png"

    current = data.get("current", {})
    dailies = (data.get("daily") or [])[:5]

    def _safe_round(value: Any):
        return round(value) if isinstance(value, (int, float)) else None

    current_data = {
        "temp": _safe_round(current.get("temp", 0)),
        "icon": icon_url(current.get("weather", [{}])[0].get("icon", "01d")),
        "feels_like": _safe_round(current.get("feels_like")),
        "humidity": _safe_round(current.get("humidity")),
        "dew_point": _safe_round(current.get("dew_point")),
    }

    days = []
    for item in dailies:
        dt = datetime.fromtimestamp(int(item.get("dt", 0)), tz=timezone.utc).astimezone(TZ).date()
        temps = item.get("temp", {})
        icon = (item.get("weather", [{}])[0] or {}).get("icon", "01d")
        days.append(
            {
                "date": dt.isoformat(),
                "min": round(temps.get("min", 0)),
                "max": round(temps.get("max", 0)),
                "icon": icon_url(icon),
            }
        )
    return {"current": current_data, "days": days}


def _owm_fetch_fiveday(lat: float, lon: float, key: str, units: str) -> Dict[str, Any]:
    current = requests.get(
        "https://api.openweathermap.org/data/2.5/weather",
        params={"lat": lat, "lon": lon, "appid": key, "units": units},
        timeout=10,
    ).json()
    forecast = requests.get(
        "https://api.openweathermap.org/data/2.5/forecast",
        params={"lat": lat, "lon": lon, "appid": key, "units": units},
        timeout=10,
    ).json()

    def icon_url(code: str) -> str:
        return f"https://openweathermap.org/img/wn/{code}@2x.png"

    def _safe_round(value: Any):
        return round(value) if isinstance(value, (int, float)) else None

    main = current.get("main", {})
    current_data = {
        "temp": _safe_round(main.get("temp", 0)),
        "icon": icon_url(current.get("weather", [{}])[0].get("icon", "01d")),
        "feels_like": _safe_round(main.get("feels_like")),
        "humidity": _safe_round(main.get("humidity")),
        "dew_point": _safe_round(main.get("dew_point")),
    }

    grouped = collections.defaultdict(list)
    for item in forecast.get("list", []):
        ts = int(item.get("dt", 0))
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ).date()
        grouped[dt].append(item)

    days = []
    for day in sorted(grouped.keys())[:5]:
        entries = grouped[day]
        tmins, tmaxs, icons = [], [], []
        for entry in entries:
            main_entry = entry.get("main", {})
            tmins.append(main_entry.get("temp_min"))
            tmaxs.append(main_entry.get("temp_max"))
            icons.append(entry.get("weather", [{}])[0].get("icon", "01d"))
        pick = None
        for entry in entries:
            hour = datetime.fromtimestamp(int(entry["dt"]), tz=timezone.utc).astimezone(TZ).hour
            if 9 <= hour <= 15:
                pick = entry.get("weather", [{}])[0].get("icon", "01d")
                break
        if not pick:
            pick = max(set(icons), key=icons.count)
        days.append(
            {
                "date": day.isoformat(),
                "min": round(min(tmins)),
                "max": round(max(tmaxs)),
                "icon": icon_url(pick),
            }
        )
    return {"current": current_data, "days": days}


def fetch_weather() -> Optional[Dict[str, Any]]:
    config = CFG.get("weather", {})
    key = config.get("api_key", "").strip()
    location = config.get("location", "").strip()
    units = config.get("units", "metric")
    if not key or not location:
        return None

    now = time.time()
    cache_ok = (
        _weather_cache["data"] is not None
        and _weather_cache["key"] == key
        and _weather_cache["loc"] == location
        and now - _weather_cache["ts"] < 600
    )
    if cache_ok:
        return _weather_cache["data"]

    lat, lon = _owm_geocode(location, key)
    try:
        data = _owm_fetch_onecall(lat, lon, key, units)
    except Exception:
        data = _owm_fetch_fiveday(lat, lon, key, units)
    _weather_cache.update({"key": key, "loc": location, "ts": now, "data": data})
    return data


def fetch_air_quality() -> Optional[Dict[str, Any]]:
    config = CFG.get("weather", {})
    key = config.get("api_key", "").strip()
    location = config.get("location", "").strip()
    if not key or not location:
        return None

    now = time.time()
    cache_ok = (
        _air_cache["data"] is not None
        and _air_cache["key"] == key
        and _air_cache["loc"] == location
        and now - _air_cache["ts"] < 600
    )
    if cache_ok:
        return _air_cache["data"]

    lat, lon = _owm_geocode(location, key)
    url = "https://api.openweathermap.org/data/2.5/air_pollution"
    response = requests.get(url, params={"lat": lat, "lon": lon, "appid": key}, timeout=10)
    response.raise_for_status()
    data = response.json()
    first = (data.get("list") or [{}])[0]
    aqi = (first.get("main") or {}).get("aqi")
    components = first.get("components") or {}
    labels = {1: "Good", 2: "Fair", 3: "Moderate", 4: "Poor", 5: "Very Poor"}
    colors = {1: "#009966", 2: "#ffde33", 3: "#ff9933", 4: "#cc0033", 5: "#660099"}
    result: Dict[str, Any] = {
        "aqi": aqi,
        "label": labels.get(aqi, "?"),
        "color": colors.get(aqi, "#fff"),
    }
    for key_name in ("pm2_5", "pm10", "no2", "o3", "so2", "co", "nh3"):
        value = components.get(key_name)
        if value is not None:
            result[key_name] = value
    _air_cache.update({"key": key, "loc": location, "ts": now, "data": result})
    return result
