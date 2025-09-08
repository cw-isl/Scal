#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Automate search in KakaoBus via ADB.

This script launches the KakaoBus app on a connected Waydroid/Android device
and injects a search query using the ADB Keyboard IME.  It is intended to be
invoked from a Telegram bot or other automation.

Usage:
    python3 kbus_search.py "<정류소명 또는 번호>"
"""
import subprocess
import time
import sys

PKG = "com.kakao.bus"
LAUNCH_TRY = 1


def sh(*args, check=True, text=True):
    """Run a subprocess and return the CompletedProcess."""
    return subprocess.run(args, check=check, text=text, capture_output=True)


def adb(*args, check=True):
    """Run an adb command."""
    return sh("adb", *args, check=check)


def set_adb_ime() -> None:
    """Ensure the ADB Keyboard IME is active."""
    adb("shell", "ime", "enable", "com.android.adbkeyboard/.AdbIME")
    adb("shell", "ime", "set", "com.android.adbkeyboard/.AdbIME")


def launch_app() -> None:
    """Return to the home screen and launch KakaoBus."""
    adb("shell", "input", "keyevent", "3")  # HOME
    time.sleep(0.6)
    for _ in range(LAUNCH_TRY):
        adb(
            "shell",
            "monkey",
            "-p",
            PKG,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        )
        time.sleep(1.2)


def enter_search(query: str) -> None:
    """Enter a search query into the KakaoBus search UI."""
    adb("shell", "input", "keyevent", "84")  # SEARCH
    time.sleep(0.8)
    adb(
        "shell",
        "am",
        "broadcast",
        "-a",
        "ADB_INPUT_TEXT",
        "--es",
        "msg",
        query,
    )
    time.sleep(0.4)
    adb("shell", "input", "keyevent", "66")  # ENTER
    time.sleep(1.2)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: kbus_search.py '<정류소명 또는 번호>'")
        sys.exit(1)
    query = sys.argv[1]
    adb("wait-for-device")
    set_adb_ime()
    launch_app()
    enter_search(query)
    print(f"OK: '{query}' 검색어 적용 완료 (카카오버스 화면 고정 시도)")


if __name__ == "__main__":
    main()
