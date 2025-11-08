"""Home Assistant 설정을 점검하기 위한 간단한 CLI 도구."""

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


def _parse_args(argv: List[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Home Assistant 연동 설정과 API 호출을 빠르게 점검합니다.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="사용할 config.yaml 경로 (미지정 시 기본 경로 사용)",
    )
    parser.add_argument(
        "--list-entities",
        action="store_true",
        help="Home Assistant에서 표시 가능한 엔티티 목록을 출력합니다.",
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
            _home_assistant_cfg,
            _home_assistant_session,
            _home_assistant_request,
            _home_assistant_should_include,
            _format_home_assistant_entity,
            HomeAssistantAPIError,
            HomeAssistantConfigError,
        )
    except ModuleNotFoundError as exc:  # pragma: no cover - 환경 문제 방지
        print("[오류] scal_main 모듈을 불러올 수 없습니다:", exc, file=sys.stderr)
        return 2

    cfg = _home_assistant_cfg()
    base_url = (cfg.get("base_url") or "").strip()
    token = (cfg.get("token") or "").strip()
    domains = cfg.get("include_domains") or []
    entities = cfg.get("include_entities") or []

    _print_header("Home Assistant 기본 설정")
    print(f"기본 URL            : {base_url or '(미설정)'}")
    print(f"장기 액세스 토큰    : {_mask(token)}")
    print(
        "허용 도메인         : "
        + (", ".join(domains) if domains else "기본(light, switch)")
    )
    print(
        "허용 엔티티         : "
        + (", ".join(entities) if entities else "도메인 기준")
    )
    timeout_display = cfg.get("timeout")
    if args.timeout is not None:
        timeout_display = args.timeout
    print(f"요청 타임아웃       : {timeout_display}초")

    try:
        session, timeout, cfg, base_url = _home_assistant_session()
    except HomeAssistantConfigError as exc:
        print("\n[실패] 설정 오류로 세션 생성에 실패했습니다:")
        print(f"  - {exc}")
        return 1
    except HomeAssistantAPIError as exc:
        print("\n[실패] Home Assistant 인증에 실패했습니다:")
        print(f"  - {exc}")
        return 1

    if args.timeout is not None:
        timeout = max(float(args.timeout), 1.0)

    print("\n[성공] Home Assistant 세션을 생성했습니다.")
    print(f"  - base_url : {base_url}")
    print(f"  - timeout  : {timeout}초")

    if not args.list_entities:
        print("\nℹ️  --list-entities 옵션을 사용하면 현재 표시 가능한 엔티티 목록을 확인할 수 있습니다.")
        return 0

    try:
        data = _home_assistant_request(
            session,
            "GET",
            "/api/states",
            timeout=timeout,
            base_url=base_url,
        )
    except HomeAssistantAPIError as exc:
        print("\n[실패] /api/states 호출이 오류를 반환했습니다:")
        print(f"  - {exc}")
        return 1
    finally:
        try:
            session.close()
        except Exception:  # pragma: no cover - 방어적
            pass

    if not isinstance(data, list):
        print("\n[실패] /api/states 응답 형식이 올바르지 않습니다.")
        return 1

    filtered: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        entity_id = str(item.get("entity_id") or "")
        if not entity_id or "." not in entity_id:
            continue
        attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        if not _home_assistant_should_include(entity_id, attributes, cfg):
            continue
        try:
            filtered.append(_format_home_assistant_entity(item))
        except HomeAssistantAPIError:
            continue

    _print_header("표시 대상 엔티티 목록")
    if not filtered:
        print("표시할 엔티티가 없습니다. 설정 값을 확인하세요.")
        return 0

    for idx, entity in enumerate(filtered, start=1):
        status = entity.get("state_label") or "상태 미확인"
        room = entity.get("room") or "-"
        print(f"{idx:2d}. {entity.get('name') or entity.get('id')} [{room}] -> {status}")

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 진입점
    sys.exit(main(sys.argv[1:]))
