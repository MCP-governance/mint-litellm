#!/usr/bin/env bash
# 솔루션 기기에서 실행하는 합격 시험 스택 기동(처음부터). 공개 MCP 스택(tests/oss/stack.sh up)이 먼저 떠 있어야 한다.
# 비밀은 이 스크립트가 만들어 deploy/.env와 ~/mint-s2/acc.env(0600)에만 둔다.
set -euo pipefail
export DOCKER_CONFIG=${DOCKER_CONFIG:-$HOME/.docker-acc}
export COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-mints2}
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../../.." && pwd)"
STATE=${ACC_HOME:-$HOME/mint-s2}
cd "$REPO/deploy"
OVERLAY="$HERE/acc.yaml"
C=(docker compose --env-file .env -f compose.yaml -f "$OVERLAY")
rand() { head -c 24 /dev/urandom | base64 | tr -d '/+=\n' | head -c "${1:-32}"; }

docker network inspect mint-g10_mcp >/dev/null
mkdir -p "$STATE"
if [ ! -f .env ]; then
  cp .env.example .env
  for pair in POSTGRES_PASSWORD=$(rand 24) MINT_PLUGIN_DB_PASSWORD=$(rand 24) MINT_CONTROL_DB_PASSWORD=$(rand 24) \
              LITELLM_MASTER_KEY=sk-acc-$(rand 32) MINT_INSTANCE_ID=acceptance MINT_LITELLM_PORT=4300 \
              MINT_CONSOLE_PORT=4330 MINT_REPLICAS=2 MINT_LOG_LEVEL=DEBUG MINT_SELFCHECK_GRACE_SECONDS=30 MINT_SECRETS_DIR=$STATE/secrets; do
    k=${pair%%=*}; v=${pair#*=}
    sed -i "/^$k=/d" .env; printf '%s=%s\n' "$k" "$v" >> .env
  done
  chmod 600 .env
fi
mkdir -p config
cp "$HERE/litellm.config.yaml" config/litellm.config.yaml
cp "$HERE/litellm.plain.yaml" config/litellm.plain.yaml
# 내보내기 볼륨은 호스트 디렉터리에 바인드한다(어댑터가 파일을 직접 읽고 쓴다). 컨테이너 사용자가 쓰도록 777.
mkdir -p "$STATE/export" "$STATE/dumps" && chmod 777 "$STATE/export" "$STATE/dumps"
export ACC_DUMP_DIR="$STATE/dumps"
docker volume inspect "${COMPOSE_PROJECT_NAME}_export" >/dev/null 2>&1 ||   docker volume create --driver local --opt type=none --opt o=bind --opt "device=$STATE/export" "${COMPOSE_PROJECT_NAME}_export" >/dev/null
./mint.sh up
"${C[@]}" up -d --wait postgres
"${C[@]}" up -d cnt fixture
set -a; . ./.env; set +a
"${C[@]}" exec -T postgres psql -U postgres -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='litellm_plain'" | grep -q 1 \
  || "${C[@]}" exec -T postgres psql -U postgres -d postgres -c "CREATE DATABASE litellm_plain"
"${C[@]}" up -d litellm litellm2 litellm-plain
echo up-done
