#!/usr/bin/env python3
"""Google Home 설정을 독립적으로 점검하기 위한 간단한 CLI 도구."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


def _parse_args(argv: List[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Google Home 연동 설정과 API 호출을 빠르게 점검합니다.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="사용할 config.yaml 경로 (미지정 시 기본 경로 사용)",
    )
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="devices:sync API를 호출해 기기 목록을 출력합니다.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="임시로 사용할 요청 타임아웃 값(초). 지정 시 설정 파일 값을 덮어씁니다.",
    )
    return parser.parse_args(argv)


def _ensure_config_env(path: str | None) -> None:
    if not path:
        return
    config_path = Path(path).expanduser()
    if not config_path.exists():
        raise SystemExit(f"[오류] 지정한 설정 파일을 찾을 수 없습니다: {config_path}")
    os.environ.setdefault("SCAL_CONFIG_FILE", str(config_path))


def _mask(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "(비어 있음)"
    if len(value) <= 4:
        return value[0] + "*" * (len(value) - 1)
    return f"{value[:3]}***{value[-2:]}"


def _print_header(title: str) -> None:
    print("\n" + title)
    print("-" * len(title))


def main(argv: List[str] | None = None) -> int:
    args = _parse_args(argv)

    try:
        _ensure_config_env(args.config)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    try:
        from scal_main import (
            _google_home_cfg,
            _google_home_session,
            _google_home_request,
            GoogleHomeAPIError,
            GoogleHomeConfigError,
        )
    except ModuleNotFoundError as exc:  # pragma: no cover - 환경 문제 방지
        print("[오류] scal_main 모듈을 불러올 수 없습니다:", exc, file=sys.stderr)
        return 2

    cfg = _google_home_cfg()
    sa_path = (cfg.get("service_account_file") or "").strip()
    agent_user_id = (cfg.get("agent_user_id") or "").strip()

    _print_header("Google Home 기본 설정")
    print(f"서비스 계정 키 경로 : {sa_path or '(미설정)'}")
    print(f"agentUserId        : {_mask(agent_user_id)}")
    timeout_display = cfg.get("timeout")
    if args.timeout:
        timeout_display = args.timeout
    print(f"요청 타임아웃       : {timeout_display}초")

    try:
        session, timeout, _, agent_user_id = _google_home_session()
    except GoogleHomeConfigError as exc:
        print("\n[실패] 설정 오류로 세션 생성에 실패했습니다:")
        print(f"  - {exc}")
        return 1
    except GoogleHomeAPIError as exc:
        print("\n[실패] Google OAuth 토큰 발급에 실패했습니다:")
        print(f"  - {exc}")
        return 1

    if args.timeout:
        timeout = max(float(args.timeout), 1.0)

    print("\n[성공] Google Home 세션을 생성했습니다.")
    print(f"  - agentUserId : {agent_user_id}")
    print(f"  - timeout     : {timeout}초")

    if not args.list_devices:
        print("\nℹ️  --list-devices 옵션을 사용하면 devices:sync 호출 결과를 확인할 수 있습니다.")
        return 0

    try:
        data = _google_home_request(
            session,
            "POST",
            "/devices:sync",
            timeout=timeout,
            json_payload={"agentUserId": agent_user_id},
        )
    except GoogleHomeAPIError as exc:
        print("\n[실패] devices:sync 호출이 오류를 반환했습니다:")
        print(f"  - {exc}")
        return 1

    devices: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        devices = data.get("devices") or []

    _print_header("devices:sync 결과")
    if not devices:
        print("등록된 기기가 없습니다. Google Home 앱에서 연결 상태를 확인하세요.")
        return 0

    for idx, device in enumerate(devices, start=1):
        dev_id = str(device.get("id") or "(id 없음)")
        dev_type = device.get("type") or "(type 없음)"
        names = []
        name_info = device.get("name")
        if isinstance(name_info, dict):
            if isinstance(name_info.get("name"), list):
                names.extend(str(n) for n in name_info["name"] if n)
            if isinstance(name_info.get("nicknames"), list):
                names.extend(str(n) for n in name_info["nicknames"] if n)
        pretty_name = names[0] if names else "(이름 없음)"
        print(f"{idx:2d}. {pretty_name} [{dev_type}] -> ID: {dev_id}")

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 진입점
    sys.exit(main(sys.argv[1:]))
