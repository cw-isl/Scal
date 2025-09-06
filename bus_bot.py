#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Seoul Bus Arrival (BIS-style) Telegram Bot — PTB v21 compatible (Ubuntu 25.04)

명령:
  /start
  /bus                      : 현재 설정으로 도착정보 조회
  /set id <도시코드> <노드ID>
  /set key <TAGO서비스키>

특징:
- 서울 TOPIS: getStationByUid(ARS) 우선 → 빈결과/오류 시 getLowArrInfoByStIdList 보조 시도
- 탭 구분 출력(예)
    47\t곧 도착
    14-1\t2정거장\t4분
    1302\t11정거장\t32분
"""

from __future__ import annotations

import html
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
import requests
import xml.etree.ElementTree as ET

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ===== 사용자 제공 정보 =====
# 프레임 통합 스크립트(scal_full_integrated.py)와 동일한 토큰을 사용합니다.
BOT_TOKEN = "7523443246:AAF-fHGcw4NLgDQDRbDz7j1xOTEFYfeZPQ0"
ALLOWED_USER_IDS = {5517670242}

# TAGO 서비스키 (환경변수 TAGO_API_KEY 우선)
TAGO_SERVICE_KEY = os.environ.get(
    "TAGO_API_KEY",
    "3d3d725df7c8daa3445ada3ceb7778d94328541e6eb616f02c0b82cb11ff182f",
).strip()

DEFAULT_CITY = ""
DEFAULT_NODE = ""

# 간단 세션(유저별 상태)
USER_STATE: Dict[int, Dict[str, Any]] = {}

# ===== Logger =====
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("busbot")


# ===== 유틸 =====
def ensure_user_state(uid: int) -> Dict[str, Any]:
    if uid not in USER_STATE:
        USER_STATE[uid] = {
            "city_code": DEFAULT_CITY,
            "node_id": DEFAULT_NODE,
            "key": TAGO_SERVICE_KEY,
        }
    return USER_STATE[uid]


def check_auth(update: Update) -> bool:
    u = update.effective_user
    return bool(u and u.id in ALLOWED_USER_IDS)


def extract_arg(text: str) -> str:
    parts = text.strip().split(maxsplit=2)
    if len(parts) == 2:
        return parts[1]
    if len(parts) >= 3:
        return parts[2]
    return ""


def extract_arg2(text: str) -> List[str]:
    parts = text.strip().split()
    return parts[2:] if len(parts) >= 3 else []


def _pick_text(elem: Optional[ET.Element], tag: str) -> str:
    if elem is None:
        return ""
    child = elem.find(tag)
    return html.unescape(child.text) if (child is not None and child.text) else ""


def _normalize_arrmsg(msg: str, fallback_seconds: Optional[int]) -> Tuple[str, str]:
    """
    msg: '3분 후[2번째 전]' -> ('3분', '2정거장')
    fallback_seconds: traTime(초). 120초 미만 → '곧 도착'
    """
    if not msg and fallback_seconds is None:
        return ("", "")

    # traTime 기반 우선
    if fallback_seconds is not None:
        if fallback_seconds < 120:
            return ("곧 도착", "1정거장")
        minutes = fallback_seconds // 60
        return (f"{minutes}분", "1정거장")

    # 메시지에서 'N분', 'N번째 전' 추출
    m_min = re.search(r"(\d+)\s*분", msg or "")
    m_hops = re.search(r"(\d+)\s*번째\s*전", msg or "")
    if m_min and int(m_min.group(1)) < 2:
        t = "곧 도착"
    else:
        t = f"{m_min.group(1)}분" if m_min else ("곧 도착" if "곧 도착" in (msg or "") else (msg or ""))
    hops = f"{m_hops.group(1)}정거장" if m_hops else ""
    if not hops or hops == "0정거장":
        hops = "1정거장"
    return (t, hops)


# ===== TAGO API 호출 =====
def tago_get_arrivals(city_code: str, node_id: str, service_key: str) -> Tuple[str, List[str]]:
    url = (
        "http://apis.data.go.kr/1613000/BusArrivalService/getBusArrivalList"
        f"?serviceKey={quote(service_key)}&cityCode={quote(str(city_code))}&nodeId={quote(str(node_id))}"
    )
    r = requests.get(url, timeout=7)
    r.raise_for_status()
    root = ET.fromstring(r.text)

    records: List[Tuple[int, str]] = []
    stop_name = ""
    for it in root.iter("item"):
        if not stop_name:
            stop_name = _pick_text(it, "nodenm") or _pick_text(it, "nodeNm")
        rtNm = _pick_text(it, "routeno") or _pick_text(it, "routeNo")
        arr = _pick_text(it, "arrtime") or _pick_text(it, "predictTime1")
        hops_raw = _pick_text(it, "arrsttnm") or _pick_text(it, "arriveRemainSeatCnt")
        seconds = int(arr) if arr and arr.isdigit() else None
        t1, hops = _normalize_arrmsg("", seconds)
        if hops_raw and hops_raw.isdigit():
            hops = f"{hops_raw}정거장"
        if not rtNm:
            continue
        line = "\t".join(filter(None, [rtNm, hops, t1]))
        m = re.search(r"(\d+)", t1)
        minutes = 0 if t1 == "곧 도착" else (int(m.group(1)) if m else 99999)
        records.append((minutes, line))

    records = [r for r in records if r[1].strip()]
    records.sort(key=lambda x: x[0])
    lines = [r[1] for r in records]
    return stop_name, lines


# ===== 핸들러 =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    st = ensure_user_state(update.effective_user.id)
    await update.message.reply_text(
        "🚌 버스 도착정보 봇\n"
        "- /bus\n"
        "- /set id <도시코드> <노드ID>\n"
        "- /set key <서비스키>\n\n"
        f"현재설정: city={st['city_code']} / node={st['node_id']} / key={'등록됨' if st['key'] else '미등록'}"
    )


async def cmd_bus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    st = ensure_user_state(update.effective_user.id)
    city, node, key = st["city_code"], st["node_id"], st["key"]

    await update.message.reply_text(f"⏳ 조회 중… (city={city}, node={node})")
    stop_name, lines = tago_get_arrivals(city, node, key)
    await update.message.reply_text("\n".join(lines) if lines else "정보가 없습니다.")


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    st = ensure_user_state(update.effective_user.id)
    text = (update.message.text or "").strip()

    if text.lower().startswith("/set id"):
        args = extract_arg2(text)
        if len(args) < 2:
            await update.message.reply_text("사용법: /set id <도시코드> <노드ID>")
            return
        st["city_code"], st["node_id"] = args[0], args[1]
        await update.message.reply_text(f"✔ id = {args[0]} {args[1]}")

    elif text.lower().startswith("/set key"):
        arg = extract_arg(text)
        if not arg:
            await update.message.reply_text("사용법: /set key <서비스키>")
            return
        st["key"] = arg.strip()
        await update.message.reply_text("✔ 서비스키 등록 완료")

    else:
        await update.message.reply_text("사용법: /set id|key ...")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bus", cmd_bus))
    app.add_handler(CommandHandler("set", cmd_set))
    log.info("Bus bot started.")
    app.run_polling()  # v21: close_loop 인자 없음


if __name__ == "__main__":
    main()
