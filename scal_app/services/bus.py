"""Bus arrival utilities and presentation helpers."""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from urllib.parse import quote

from ..config import CFG


def pick_text(elem: Optional[ET.Element], *names: str) -> str:
    """Return the first non-empty text for the provided tag names."""
    if elem is None:
        return ""
    for name in names:
        child = elem.find(name)
        if child is not None and child.text and child.text.strip():
            return html.unescape(child.text.strip())
    return ""


def _extract_eta_minutes(message: str) -> int:
    """Parse textual ETA into minute integers with heuristics."""
    if not message:
        return 99999
    if "곧" in message:
        return 0
    match = re.search(r"(\d+)\s*분", message)
    if match:
        try:
            return int(match.group(1))
        except Exception:
            pass
    seconds = re.search(r"(\d+)\s*초", message)
    if seconds:
        try:
            sec = int(seconds.group(1))
            return 0 if sec <= 60 else max(1, sec // 60)
        except Exception:
            pass
    numeric = re.search(r"^\s*(\d+)\s*$", message)
    if numeric:
        try:
            return int(numeric.group(1))
        except Exception:
            pass
    return 99999


def _eta_display(minutes: int) -> str:
    return "곧 도착" if minutes == 0 else f"{minutes}분"


def get_bus_arrivals(
    city_code: str,
    node_id: str,
    service_key_encoded: str,
    *,
    dedup_by_route: bool = True,
    limit: int = 5,
    timeout: int = 7,
) -> Dict[str, Any]:
    """Fetch arrival information from the TAGO open API."""
    if not (city_code and node_id and service_key_encoded):
        return {"stop_name": "", "items": [], "need_config": True}

    url = (
        "http://apis.data.go.kr/1613000/BusArrivalService/getBusArrivalList"
        f"?serviceKey={quote(service_key_encoded)}&cityCode={quote(str(city_code))}&nodeId={quote(str(node_id))}"
    )

    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    root = ET.fromstring(response.text)

    stop_name = ""
    records: List[Tuple[int, Dict[str, Any]]] = []

    for item in root.iter("item"):
        if not stop_name:
            stop_name = pick_text(item, "nodenm", "nodeNm")

        route = pick_text(item, "routeno", "routeNo")
        if not route:
            continue

        arr_sec = pick_text(item, "arrtime")
        arr_min = pick_text(item, "predictTime1")
        raw_msg = ""

        minutes = 99999
        if arr_sec:
            try:
                sec = int(str(arr_sec).strip())
                minutes = 0 if sec <= 60 else max(1, sec // 60)
                raw_msg = "곧 도착" if minutes == 0 else f"{minutes}분"
            except Exception:
                pass
        elif arr_min:
            try:
                minutes = int(str(arr_min).strip())
                raw_msg = f"{minutes}분"
            except Exception:
                pass
        else:
            raw_msg = pick_text(item, "arrmsg1", "arrmsg") or ""
            minutes = _extract_eta_minutes(raw_msg)

        hops = pick_text(item, "arrprevstationcnt", "arrprevStationCnt")
        if hops.isdigit():
            hops = f"{hops}정거장"
        if not hops or hops == "0정거장":
            hops = "1정거장"

        display = _eta_display(minutes)

        record = {
            "route": route,
            "eta_min": minutes,
            "eta_text": display,
            "hops": hops,
            "raw_msg": raw_msg or display,
        }
        records.append((minutes, record))

    records = [entry for entry in records if entry[1]["route"] and entry[1]["eta_min"] < 99999]
    records.sort(key=lambda entry: entry[0])

    items: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for _, record in records:
        if dedup_by_route:
            if record["route"] in seen:
                continue
            seen.add(record["route"])
        items.append(record)
        if len(items) >= limit:
            break

    return {"stop_name": stop_name, "items": items}


def render_bus_box() -> Dict[str, Any]:
    config = CFG.get("bus", {}) or {}
    city = (config.get("city_code") or "").strip()
    node = (config.get("node_id") or "").strip()
    key = (config.get("key") or "").strip()
    data = get_bus_arrivals(city, node, key, dedup_by_route=True, limit=5, timeout=7)

    if data.get("need_config"):
        return {
            "title": "버스도착",
            "stop": "설정 필요",
            "rows": [{"text": "도시/정류장/키를 설정해주세요"}],
        }

    rows = []
    for entry in data.get("items", []):
        rows.append(
            {
                "route": entry["route"],
                "eta": entry["eta_text"],
                "hops": entry["hops"],
                "text": f'{entry["route"]} · {entry["eta_text"]} · {entry["hops"]}',
            }
        )

    stop_name = data.get("stop_name", "")
    title = "버스도착"
    if stop_name:
        title += f" · {stop_name}"

    return {"title": title, "stop": stop_name, "rows": rows}
