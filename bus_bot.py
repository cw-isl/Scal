#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Seoul Bus Arrival (BIS-style) Telegram Bot — PTB v21 compatible (Ubuntu 25.04)

명령:
  /start
  /bus                      : 현재(region/api/id)로 도착정보 조회
  /set region <서울|경기|인천>
  /set api <seoul|tago>     : 현재 seoul 실동작, tago는 안내
  /set id <정류장ARS번호>   : 예) /set id 17102
  /set key <키>             : data.go.kr 발급 인증키(인코딩/디코딩 그대로 OK)

특징:
- 서울 TOPIS: getStationByUid(ARS) 우선 → 빈결과/오류 시 getLowArrInfoByStIdList 보조 시도
- 탭 구분 출력(예)
    47\t곧 도착
    14-1\t4분\t2정거장
    1302\t32분\t11정거장
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

# 환경변수로 키를 미리 줄 수도 있음
SEOUL_SERVICE_KEY = os.environ.get("SEOUL_API_KEY", "").strip()
TAGO_SERVICE_KEY = os.environ.get("TAGO_API_KEY", "").strip()

DEFAULT_REGION = "서울"
DEFAULT_API = "seoul"
DEFAULT_STOP_ID = "17102"   # 서울 ARS 번호

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
            "region": DEFAULT_REGION,
            "api": DEFAULT_API,
            "stop_id": DEFAULT_STOP_ID,
            "keys": {"seoul": SEOUL_SERVICE_KEY, "tago": TAGO_SERVICE_KEY},
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


def _normalize_arrmsg(msg: str, fallback_minutes: Optional[int]) -> Tuple[str, str]:
    """
    msg: '3분 후[2번째 전]' -> ('3분', '2정거장')
    fallback_minutes: traTime(초)로 얻은 분(반올림). 0이하 → '곧 도착'
    """
    if not msg and fallback_minutes is None:
        return ("", "")

    # traTime 기반 우선
    if fallback_minutes is not None:
        return ("곧 도착", "") if fallback_minutes <= 0 else (f"{fallback_minutes}분", "")

    # 메시지에서 'N분', 'N번째 전' 추출
    m_min = re.search(r"(\d+)\s*분", msg or "")
    m_hops = re.search(r"(\d+)\s*번째\s*전", msg or "")
    t = f"{m_min.group(1)}분" if m_min else ("곧 도착" if "곧 도착" in (msg or "") else (msg or ""))
    hops = f"{m_hops.group(1)}정거장" if m_hops else ""
    return (t, hops)


# ===== 서울 API 호출 =====
def _seoul_station_by_uid(ars_id: str, service_key: str) -> List[str]:
    """TOPIS: 정류장 ARS번호 기반 getStationByUid"""
    url = (
        "http://ws.bus.go.kr/api/rest/stationinfo/getStationByUid"
        f"?serviceKey={quote(service_key)}&arsId={quote(str(ars_id))}"
    )
    r = requests.get(url, timeout=7)
    r.raise_for_status()
    root = ET.fromstring(r.text)

    lines: List[str] = []
    for it in root.iter("itemList"):
        rtNm = _pick_text(it, "rtNm")
        arrmsg1 = _pick_text(it, "arrmsg1")
        traTime1 = _pick_text(it, "traTime1")
        fallback = round(int(traTime1) / 60) if traTime1.isdigit() else None
        t1, hops = _normalize_arrmsg(arrmsg1, fallback)
        if not rtNm:
            continue
        lines.append(f"{rtNm}\t{t1}\t{hops}".rstrip())

    lines = [ln for ln in lines if ln.strip()]
    if lines:
        def keyf(s: str):
            first = s.split("\t", 1)[0]
            return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", first)]
        lines.sort(key=keyf)
    return lines


def _seoul_low_by_stid(ars_id_as_stid: str, service_key: str) -> List[str]:
    """보조: getLowArrInfoByStIdList (계정/오퍼레이션에 따라 응답 형식 다름)"""
    url = (
        "http://ws.bus.go.kr/api/rest/arrive/getLowArrInfoByStIdList"
        f"?serviceKey={quote(service_key)}&stId={quote(str(ars_id_as_stid))}"
    )
    r = requests.get(url, timeout=7)
    r.raise_for_status()
    root = ET.fromstring(r.text)

    lines: List[str] = []
    for it in root.iter("itemList"):
        rtNm = _pick_text(it, "rtNm") or _pick_text(it, "busRouteNm")
        arrmsg = _pick_text(it, "arrmsg1") or _pick_text(it, "arrmsg")
        traTime = _pick_text(it, "traTime1") or _pick_text(it, "traTime")
        fallback = round(int(traTime) / 60) if traTime.isdigit() else None
        t1, hops = _normalize_arrmsg(arrmsg, fallback)
        if not rtNm:
            continue
        lines.append(f"{rtNm}\t{t1}\t{hops}".rstrip())

    lines = [ln for ln in lines if ln.strip()]
    if lines:
        def keyf(s: str):
            first = s.split("\t", 1)[0]
            return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", first)]
        lines.sort(key=keyf)
    return lines


def seoul_get_by_ars(ars_id: str, service_key: str) -> List[str]:
    """서울 도착정보: 우선 getStationByUid → 없으면 보조 API 시도"""
    if not service_key:
        return ["❗️서울 API 서비스키가 설정되지 않았습니다. /set key <키값>"]

    try:
        primary = _seoul_station_by_uid(ars_id, service_key)
        if primary:
            return primary
    except requests.RequestException as e:
        log.warning(f"getStationByUid error: {e}")
    except ET.ParseError as e:
        log.warning(f"XML parse error (primary): {e}")

    # 보조 시도
    try:
        backup = _seoul_low_by_stid(ars_id, service_key)
        if backup:
            return backup
    except requests.RequestException as e:
        log.warning(f"getLowArrInfoByStIdList error: {e}")
    except ET.ParseError as e:
        log.warning(f"XML parse error (backup): {e}")

    return ["해당 정류장의 도착정보가 없습니다. (ARS/오퍼레이션 확인 필요)"]


# ===== TAGO (안내) =====
def tago_stub(stop_id: str, key: str, region: str) -> List[str]:
    return [
        "TAGO(국토부) API는 도시코드/노드ID가 추가로 필요합니다.",
        "서울은 seoul API 사용을 권장합니다. (원하면 TAGO 실구현 붙여드릴게요)",
    ]


# ===== 핸들러 =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    st = ensure_user_state(update.effective_user.id)
    await update.message.reply_text(
        "🚌 버스 도착정보 봇\n"
        "- /bus\n"
        "- /set region <서울|경기|인천>\n"
        "- /set api <seoul|tago>\n"
        "- /set id <정류장ARS>\n"
        "- /set key <서비스키>\n\n"
        f"현재설정: region={st['region']} / api={st['api']} / id={st['stop_id']} / "
        f"key={'등록됨' if st['keys'].get(st['api']) else '미등록'}"
    )


async def cmd_bus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    st = ensure_user_state(update.effective_user.id)
    region, api, stop_id = st["region"], st["api"], st["stop_id"]
    key = st["keys"].get(api, "")

    await update.message.reply_text(f"⏳ 조회 중… (region={region}, api={api}, id={stop_id})")
    lines = seoul_get_by_ars(stop_id, key) if api == "seoul" else tago_stub(stop_id, key, region)
    await update.message.reply_text("\n".join(lines))


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_auth(update):
        return
    st = ensure_user_state(update.effective_user.id)
    text = (update.message.text or "").strip()

    if text.lower().startswith("/set region"):
        arg = extract_arg(text)
        if not arg:
            await update.message.reply_text("사용법: /set region <서울|경기|인천>")
            return
        st["region"] = arg
        await update.message.reply_text(f"✔ region = {arg}")

    elif text.lower().startswith("/set api"):
        arg = extract_arg(text).lower()
        if arg not in {"seoul", "tago"}:
            await update.message.reply_text("사용법: /set api <seoul|tago>")
            return
        st["api"] = arg
        await update.message.reply_text(f"✔ api = {arg}  (key: {'등록됨' if st['keys'].get(arg) else '미등록'})")

    elif text.lower().startswith("/set id"):
        arg = extract_arg(text)
        if not arg or not re.fullmatch(r"\d+", arg):
            await update.message.reply_text("사용법: /set id <정류장ARS번호(숫자)>  예) /set id 17102")
            return
        st["stop_id"] = arg
        await update.message.reply_text(f"✔ id = {arg}")

    elif text.lower().startswith("/set key"):
        args = extract_arg2(text)
        if len(args) == 1:
            the_api, the_key = st["api"], args[0]
        elif len(args) >= 2:
            the_api, the_key = args[0].lower(), " ".join(args[1:])
            if the_api not in {"seoul", "tago"}:
                await update.message.reply_text("사용법: /set key <키>  또는  /set key <seoul|tago> <키>")
                return
        else:
            await update.message.reply_text("사용법: /set key <키>  또는  /set key <seoul|tago> <키>")
            return
        st["keys"][the_api] = the_key.strip()
        await update.message.reply_text(f"✔ {the_api} 서비스키 등록 완료")

    else:
        await update.message.reply_text("사용법: /set region|api|id|key ...")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bus", cmd_bus))
    app.add_handler(CommandHandler("set", cmd_set))
    log.info("Bus bot started.")
    app.run_polling()  # v21: close_loop 인자 없음


if __name__ == "__main__":
    main()
