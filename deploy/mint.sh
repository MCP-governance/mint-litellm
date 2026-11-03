#!/usr/bin/env bash
# MINT 배포 보조 스크립트: up | status | backup | restore | upgrade | cli
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE=(docker compose --env-file .env -f compose.yaml)
BUNDLE_FILE=/bundles/mint.tar.gz

fail() { echo "$*" >&2; exit 1; }

require_env() {
    [ -f .env ] || fail ".env가 없습니다. .env.example을 복사해 값을 채우십시오."
}

# 관리자 권한(마이그레이션·역할 생성)이 필요한 명령은 슈퍼유저 접속으로 실행한다.
admin_cli() {
    "${COMPOSE[@]}" run --rm --no-deps \
        -e "MINT_DATABASE_URL=postgresql://postgres:$(env_value POSTGRES_PASSWORD)@postgres:5432/mint" \
        -e "MINT_PLUGIN_DB_PASSWORD=$(env_value MINT_PLUGIN_DB_PASSWORD)" \
        -e "MINT_CONTROL_DB_PASSWORD=$(env_value MINT_CONTROL_DB_PASSWORD)" \
        control "$@"
}

env_value() {
    grep -E "^$1=" .env | head -n1 | cut -d= -f2-
}

cli() {
    "${COMPOSE[@]}" run --rm control "$@"
}

# 정책 엔진은 번들 파일이 있어야 기동한다. 파일이 없으면 저장소의 기본 정책으로 첫 번들을 게시하고,
# 판본 등록과 활성화는 policy activate가 수행한다.
ensure_bundle() {
    "${COMPOSE[@]}" run --rm --no-deps -T --entrypoint python control -c "
from pathlib import Path
from mint_litellm.plugin.policy.bundle import prepare_bundle, publish_bundle
target = Path('${BUNDLE_FILE}')
if not target.exists():
    publish_bundle(target, prepare_bundle(Path('/opt/mint'), 'initial').archive)
    target.chmod(0o644)
"
}

cmd_up() {
    require_env
    [ -f config/litellm.config.yaml ] || cp litellm.config.example.yaml config/litellm.config.yaml
    "${COMPOSE[@]}" build litellm
    "${COMPOSE[@]}" up -d --wait postgres
    admin_cli init --out /app/config/litellm.mint.yaml >/dev/null
    ensure_bundle
    "${COMPOSE[@]}" up -d opa litellm
    echo "기동했습니다. 상태: ./mint.sh status"
}

cmd_status() {
    require_env
    "${COMPOSE[@]}" ps
    cli status
}

cmd_backup() {
    require_env
    local dir="backups/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$dir"
    for database in mint litellm; do
        "${COMPOSE[@]}" exec -T postgres pg_dump -U postgres --clean --if-exists -d "$database" \
            > "$dir/$database.sql"
    done
    cp config/litellm.config.yaml "$dir/litellm.config.yaml"
    echo "백업: $dir (비밀값이 담긴 .env는 포함하지 않습니다)"
}

cmd_restore() {
    require_env
    local dir="${1:-}" confirm="${2:-}"
    [ -d "$dir" ] || fail "사용법: ./mint.sh restore <백업 폴더> --yes"
    [ "$confirm" = "--yes" ] || fail "현재 데이터를 백업 내용으로 덮어씁니다. 계속하려면 --yes를 붙이십시오."
    "${COMPOSE[@]}" stop litellm
    for database in mint litellm; do
        [ -f "$dir/$database.sql" ] || fail "$dir/$database.sql이 없습니다."
        "${COMPOSE[@]}" exec -T postgres psql -v ON_ERROR_STOP=1 -U postgres -d "$database" \
            < "$dir/$database.sql" > /dev/null
    done
    "${COMPOSE[@]}" up -d litellm
    echo "복원했습니다. 복원 직후 ./mint.sh status와 check로 상태를 확인하십시오."
}

cmd_upgrade() {
    require_env
    "${COMPOSE[@]}" build litellm
    "${COMPOSE[@]}" up -d --wait postgres
    admin_cli init --out /app/config/litellm.mint.yaml >/dev/null
    "${COMPOSE[@]}" up -d litellm
    echo "업그레이드했습니다."
}

case "${1:-}" in
    up) cmd_up ;;
    status) cmd_status ;;
    backup) cmd_backup ;;
    restore) shift; cmd_restore "$@" ;;
    upgrade) cmd_upgrade ;;
    cli) shift; require_env; cli "$@" ;;
    *) fail "사용법: ./mint.sh up | status | backup | restore <폴더> --yes | upgrade | cli <명령...>" ;;
esac
