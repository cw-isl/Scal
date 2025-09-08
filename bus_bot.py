#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Simple Telegram bot to control KakaoBus via ``kbus_search.py``."""
import os
import subprocess
import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}
KST = timezone(timedelta(hours=9))


def allowed(user_id: int) -> bool:
    return not ALLOWED_IDS or user_id in ALLOWED_IDS


async def _deny(update: Update) -> None:
    await update.message.reply_text("허용된 사용자만 사용할 수 있습니다.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return await _deny(update)
    await update.message.reply_text("버스봇입니다. /bus <정류소명 또는 번호> 로 설정하세요.")


async def bus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return await _deny(update)
    if not context.args:
        return await update.message.reply_text("사용법: /bus <정류소명 또는 번호>")

    query = " ".join(context.args)
    try:
        out = subprocess.check_output(
            ["python3", "kbus_search.py", query],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        await update.message.reply_text(f"검색 적용: {query}\n{out.strip()}")
    except subprocess.CalledProcessError as e:  # pragma: no cover - runtime
        await update.message.reply_text(f"실패: {e.output}")
    except Exception as e:  # pragma: no cover - runtime
        await update.message.reply_text(f"오류: {e}")


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update.effective_user.id):
        return await _deny(update)
    text = (update.message.text or "").strip()
    if not text:
        return
    try:
        out = subprocess.check_output(
            ["python3", "kbus_search.py", text],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        await update.message.reply_text(f"검색 적용: {text}\n{out.strip()}")
    except subprocess.CalledProcessError as e:  # pragma: no cover
        await update.message.reply_text(f"실패: {e.output}")
    except Exception as e:  # pragma: no cover
        await update.message.reply_text(f"오류: {e}")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("bus", bus))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.run_polling()


if __name__ == "__main__":
    main()
