#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "[scal-refresh] 이 스크립트는 루트 권한으로 실행해야 합니다." >&2
    exit 1
fi

REPO_URL="https://github.com/cw-isl/Scal.git"
APP_DIR="/root/scal"
SERVICE_NAME="scal.service"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_ARCHIVE="/root/scal_backup_${TIMESTAMP}.tar.gz"
RENAMED_DIR="/root/scal_${TIMESTAMP}_old"

log() {
    echo "[scal-refresh] $*"
}

stop_service() {
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            log "서비스(${SERVICE_NAME}) 중지 중"
            systemctl stop "$SERVICE_NAME"
        else
            log "서비스(${SERVICE_NAME})가 실행 중이 아닙니다."
        fi
    else
        log "systemctl을 찾을 수 없어 서비스 중지를 건너뜁니다."
    fi
}

backup_existing_directory() {
    if [[ -d "$APP_DIR" ]]; then
        log "기존 디렉터리를 ${BACKUP_ARCHIVE} 로 백업"
        tar -czf "$BACKUP_ARCHIVE" -C "$(dirname "$APP_DIR")" "$(basename "$APP_DIR")"
        log "기존 디렉터리를 ${RENAMED_DIR} 로 이름 변경"
        mv "$APP_DIR" "$RENAMED_DIR"
    else
        log "백업할 기존 디렉터리가 없습니다."
    fi
}

clone_repository() {
    log "GitHub 최신 저장소를 ${APP_DIR} 에 클론"
    git clone "$REPO_URL" "$APP_DIR"
}

apply_permissions() {
    log "디렉터리와 파일 퍼미션 설정"
    find "$APP_DIR" -type d -exec chmod 755 {} +
    find "$APP_DIR" -type f -exec chmod 644 {} +
    find "$APP_DIR" -type f -name '*.sh' -exec chmod 755 {} +
}

restart_service() {
    if command -v systemctl >/dev/null 2>&1; then
        log "서비스(${SERVICE_NAME}) 시작"
        systemctl daemon-reload || true
        if systemctl start "$SERVICE_NAME"; then
            log "서비스 상태 확인"
            systemctl status "$SERVICE_NAME" --no-pager
        else
            log "서비스 시작에 실패했습니다. 상태 정보를 확인합니다."
            systemctl status "$SERVICE_NAME" --no-pager || true
            log "서비스 시작 실패로 인해 재부팅을 중단합니다." >&2
            exit 1
        fi
    else
        log "systemctl을 찾을 수 없어 서비스 상태 확인을 건너뜁니다."
    fi
}

reboot_system() {
    log "시스템을 재부팅합니다."
    reboot
}

main() {
    stop_service
    backup_existing_directory
    clone_repository
    apply_permissions
    restart_service
    reboot_system
}

main "$@"
