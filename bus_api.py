# -*- coding: utf-8 -*-
"""TAGO 버스도착정보 간단 호출 모듈."""
from typing import List, Dict, Any, Tuple
import re
import requests
import xml.etree.ElementTree as ET
from urllib.parse import quote


def _pick_text(elem: ET.Element, *names: str) -> str:
    for n in names:
        v = elem.findtext(n)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def _extract_eta_minutes(msg: str) -> int:
    """한국어 또는 숫자 혼용 도착메시지에서 '분' 추출."""
    if not msg:
        return 99999
    if "곧" in msg:
        return 0
    m = re.search(r"(\d+)\s*분", msg)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    s = re.search(r"(\d+)\s*초", msg)
    if s:
        try:
            sec = int(s.group(1))
            return 0 if sec <= 60 else max(1, sec // 60)
        except Exception:
            pass
    only_num = re.search(r"^\s*(\d+)\s*$", msg)
    if only_num:
        try:
            return int(only_num.group(1))
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
    """TAGO 버스도착정보 가져오기."""
    if not (city_code and node_id and service_key_encoded):
        return {"stop_name": "", "items": [], "need_config": True}

    url = (
        "http://apis.data.go.kr/1613000/BusArrivalService/getBusArrivalList"
        f"?serviceKey={quote(service_key_encoded)}&cityCode={quote(str(city_code))}&nodeId={quote(str(node_id))}"
    )

    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    root = ET.fromstring(r.text)

    stop_name = ""
    records: List[Tuple[int, Dict[str, Any]]] = []

    for it in root.iter("item"):
        if not stop_name:
            stop_name = _pick_text(it, "nodenm", "nodeNm")

        route = _pick_text(it, "routeno", "routeNo")
        if not route:
            continue

        arr_sec = _pick_text(it, "arrtime")
        arr_min = _pick_text(it, "predictTime1")
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
            raw_msg = _pick_text(it, "arrmsg1", "arrmsg") or ""
            minutes = _extract_eta_minutes(raw_msg)

        hops = _pick_text(it, "arrprevstationcnt", "arrprevStationCnt")
        if hops.isdigit():
            hops = f"{hops}정거장"
        if not hops or hops == "0정거장":
            hops = "1정거장"

        display = _eta_display(minutes)

        rec = {
            "route": route,
            "eta_min": minutes,
            "eta_text": display,
            "hops": hops,
            "raw_msg": raw_msg or display,
        }
        records.append((minutes, rec))

    records = [r for r in records if r[1]["route"] and r[1]["eta_min"] < 99999]
    records.sort(key=lambda x: x[0])

    items: List[Dict[str, Any]] = []
    seen = set()
    for _, rec in records:
        if dedup_by_route:
            if rec["route"] in seen:
                continue
            seen.add(rec["route"])
        items.append(rec)
        if len(items) >= limit:
            break

    return {"stop_name": stop_name, "items": items}

