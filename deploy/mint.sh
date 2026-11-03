#!/usr/bin/env bash
# MINT 배포 보조 스크립트: up | status | backup | upgrade | cli
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE=(docker compose --env-file .env -f compose.yaml)
BUNDLE_FILE=/bundles/mint.tar.gz

fail() { echo "$*" >&2; exit 1; }

env_value() {
    grep -E "^$1=" .env | head -n1 | cut -d= -f2-
}

random_hex() {
    openssl rand -hex 32 2>/dev/null || od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
}

# 호출별 승인의 인자 지문 키는 배포마다 한 번 생성해 .env에 둔다. 바꾸면 이미 요청한 호출별
# 승인이 맞지 않게 되므로 이미 있으면 건드리지 않는다.
ensure_secrets() {
    if [ -z "$(env_value MINT_FINGERPRINT_KEY)" ]; then
        sed -i '/^MINT_FINGERPRINT_KEY=/d' .env
        printf 'MINT_FINGERPRINT_KEY=%s\n' "$(random_hex)" >> .env
    fi
}

require_env() {
    [ -f .env ] || fail ".env가 없습니다. .env.example을 복사해 값을 채우십시오."
    ensure_secrets
}

# 관리자 권한(마이그레이션·역할 생성)이 필요한 명령은 슈퍼유저 접속으로 실행한다.
admin_cli() {
    "${COMPOSE[@]}" run --rm --no-deps \
        -e "MINT_DATABASE_URL=postgresql://postgres:$(env_value POSTGRES_PASSWORD)@postgres:5432/mint" \
        -e "MINT_PLUGIN_DB_PASSWORD=$(env_value MINT_PLUGIN_DB_PASSWORD)" \
        -e "MINT_CONTROL_DB_PASSWORD=$(env_value MINT_CONTROL_DB_PASSWORD)" \
        control "$@"
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
"
}

cmd_up() {
    require_env
    mkdir -p config
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

# 백업은 데이터베이스 두 개, 정책 번들, LiteLLM 설정을 한 폴더에 모은다. 하나라도 비면 폴더를 지운다.
cmd_backup() {
    require_env
    local dir="backups/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$dir"
    for database in mint litellm; do
        "${COMPOSE[@]}" exec -T postgres pg_dump -U postgres -d "$database" > "$dir/$database.sql"
    done
    "${COMPOSE[@]}" run --rm --no-deps -T --entrypoint cat control "$BUNDLE_FILE" > "$dir/mint.tar.gz"
    cp config/litellm.config.yaml "$dir/litellm.config.yaml"
    for file in mint.sql litellm.sql mint.tar.gz litellm.config.yaml; do
        [ -s "$dir/$file" ] || { rm -rf "$dir"; fail "백업에 $file이 비어 있어 폴더를 지웠습니다."; }
    done
    echo "백업: $dir (mint.sql, litellm.sql, mint.tar.gz, litellm.config.yaml; 비밀값이 담긴 .env는 포함하지 않습니다)"
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
    upgrade) cmd_upgrade ;;
    cli) shift; require_env; cli "$@" ;;
    *) fail "사용법: ./mint.sh up | status | backup | upgrade | cli <명령...>" ;;
esac
