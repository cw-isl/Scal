#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dump KakaoBus UI via uiautomator and convert to JSON.

This script periodically pulls the current UI hierarchy from the connected
Android device, extracts text nodes and writes a JSON summary to
``bus.json``.  The JSON file can be consumed by a smart frame or other
applications.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

OUT = Path("bus.json")
INTERVAL = 10  # seconds
KST = timezone(timedelta(hours=9))


def adb(*args: str) -> str:
    """Run an adb command and return its standard output."""
    return subprocess.check_output(["adb", *args], text=True)


def dump_xml(local: Path) -> Path:
    """Dump the current UI hierarchy to ``local`` and return the path."""
    adb("shell", "uiautomator", "dump", "/sdcard/view.xml")
    adb("pull", "/sdcard/view.xml", str(local))
    return local


def texts_from(xml_path: Path) -> List[str]:
    root = ET.parse(xml_path).getroot()
    res: List[str] = []
    for node in root.iter():
        t = node.attrib.get("text")
        if t:
            t = " ".join(t.split())
            if t:
                res.append(t)
    return res


def parse_heuristic(texts: List[str]) -> dict:
    routes, arrivals, meta = [], [], []
    for t in texts:
        if ("번" in t and any(c.isdigit() for c in t)) or t.isdigit():
            routes.append(t)
        elif any(k in t for k in ("분", "초", "도착", "전", "곧")):
            arrivals.append(t)
        elif any(k in t for k in ("정류장", "남음", "방면", "행")):
            meta.append(t)
    return {
        "timestamp": datetime.now(KST).isoformat(),
        "routes": routes[:20],
        "arrivals": arrivals[:20],
        "meta": meta[:30],
        "raw": texts[:200],
    }


def main() -> None:
    out_dir = OUT.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            xml = dump_xml(out_dir / "view.xml")
            texts = texts_from(xml)
            data = parse_heuristic(texts)
            OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
            print(f"[{data['timestamp']}] {len(texts)} nodes → {OUT}")
        except Exception as e:  # pragma: no cover - runtime logging
            print("error:", e)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
