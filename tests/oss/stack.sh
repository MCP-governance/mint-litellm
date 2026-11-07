#!/usr/bin/env bash
# 공개 MCP 서버 도입 시험 스택 제어. 사용: stack.sh up | swap <서버> <판본> | status | env | down
# 서버: playwright | context7 | grafana. 비밀은 실행 때 만들어 .runtime/env(권한 600, 저장소 제외)에만 둔다.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="$HERE/.runtime"
ENV_FILE="$RUNTIME/env"
COMPOSE=(docker compose -p mint-g10 -f "$HERE/compose.yaml")

# 판본 표: 전부 실제 레지스트리에서 확인한 값이다. npm은 integrity, 이미지는 index digest.
declare -A PLAYWRIGHT_INTEGRITY=(
  [0.0.83]='sha512-oNcl+Ae2/IAjhfPeP46BfIkSakfmprY+aOtkv5MjrQ4lPav4/yNtPhL0iq8SlIM90oApWgBDUxaNKvktazUKOg=='
  [0.0.84]='sha512-weqLrA7/D8MJzNV9/t3Hnjp100tS2sxFXKeb2gH6Nhktqmdfvx5qRA9AZ5Mhjnre1jY0wltsrRK5VZS5NBgcvw=='
)
declare -A CONTEXT7_INTEGRITY=(
  [2.2.1]='sha512-0Uu7GekKYaP/sgSCeoClYFqCm00rqehMFfHhj14krs0cVkVVz9vrChhZdV5kmkYDNNnQ7Vd6F5b57evina2+5w=='
  [2.2.2]='sha512-4vPhvMNA7CDuh4pITBWmeshdxi8Ew4LMtXihetZxKD62e3IAdwL5ZAYwcQXj9ExG//BjsHlybsbizjdxNNtnZA=='
)
declare -A GRAFANA_MCP_IMAGE=(
  [1.4.0]='grafana/mcp-grafana:1.4.0@sha256:13cf62271d456750e3371051a7e3927339ad956f3ac5e7b998a943472b48be1c'
  [1.4.1]='grafana/mcp-grafana:1.4.1@sha256:cfcf06863c23b2db64c41b0379d1efe95703aca4ba5434d90674f72ec0b7d810'
)

rand() { head -c 24 /dev/urandom | base64 | tr -d '/+=\n' | head -c "${1:-32}"; }

load() { set -a; . "$ENV_FILE"; set +a; }

save_version() { # 서버 판본: 환경 파일의 선택 판본과 그 판본의 고정 값을 갱신한다.
  local server="$1" version="$2"
  case "$server" in
    playwright) [[ -n "${PLAYWRIGHT_INTEGRITY[$version]:-}" ]] || { echo "알 수 없는 판본: $version" >&2; exit 2; }
      set_var G10_PLAYWRIGHT_VERSION "$version"; set_var G10_PLAYWRIGHT_INTEGRITY "${PLAYWRIGHT_INTEGRITY[$version]}" ;;
    context7) [[ -n "${CONTEXT7_INTEGRITY[$version]:-}" ]] || { echo "알 수 없는 판본: $version" >&2; exit 2; }
      set_var G10_CONTEXT7_VERSION "$version"; set_var G10_CONTEXT7_INTEGRITY "${CONTEXT7_INTEGRITY[$version]}" ;;
    grafana) [[ -n "${GRAFANA_MCP_IMAGE[$version]:-}" ]] || { echo "알 수 없는 판본: $version" >&2; exit 2; }
      set_var G10_GRAFANA_MCP_VERSION "$version"; set_var G10_GRAFANA_MCP_IMAGE "${GRAFANA_MCP_IMAGE[$version]}" ;;
    *) echo "알 수 없는 서버: $server" >&2; exit 2 ;;
  esac
}

set_var() { # 이름 값
  local tmp="$ENV_FILE.tmp"
  { grep -v "^$1=" "$ENV_FILE" 2>/dev/null || true; printf '%s=%q\n' "$1" "$2"; } >"$tmp"
  chmod 600 "$tmp"; mv "$tmp" "$ENV_FILE"
}

wait_http() { # url 시도횟수
  for _ in $(seq 1 "${2:-60}"); do curl -fsS -o /dev/null "$1" 2>/dev/null && return 0; sleep 2; done
  echo "응답 없음: $1" >&2; return 1
}

grafana_token() { # Grafana 관리자 자격으로 서비스 계정과 토큰을 만들어 토큰만 표준 출력에 낸다.
  local auth="admin:$G10_GRAFANA_ADMIN_PASSWORD" sa
  sa=$(curl -fsS -u "$auth" -H 'Content-Type: application/json' \
        -d '{"name":"mcp","role":"Admin"}' http://localhost:4742/api/serviceaccounts \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
  curl -fsS -u "$auth" -H 'Content-Type: application/json' -d '{"name":"mcp-token"}' \
        "http://localhost:4742/api/serviceaccounts/$sa/tokens" \
        | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])'
}

cmd_up() {
  mkdir -p "$RUNTIME"; chmod 700 "$RUNTIME"
  if [[ -f "$ENV_FILE" ]]; then load; "${COMPOSE[@]}" --profile grafana-mcp down -v --remove-orphans >/dev/null 2>&1 || true; fi
  : >"$ENV_FILE"; chmod 600 "$ENV_FILE"
  set_var G10_DB_PASSWORD "$(rand 24)"
  set_var G10_MASTER_KEY "sk-g10-$(rand 32)"
  set_var G10_SALT_KEY "sk-g10-$(rand 32)"
  set_var G10_GRAFANA_ADMIN_PASSWORD "$(rand 24)"
  save_version playwright "${G10_PLAYWRIGHT:-0.0.83}"
  save_version context7 "${G10_CONTEXT7:-2.2.1}"
  save_version grafana "${G10_GRAFANA_MCP:-1.4.0}"
  load
  "${COMPOSE[@]}" up -d --build --wait postgres litellm grafana mcp-playwright mcp-context7
  set_var G10_GRAFANA_TOKEN "$(grafana_token)"
  load
  "${COMPOSE[@]}" --profile grafana-mcp up -d mcp-grafana
  echo "stack up (env: $ENV_FILE)"
}

cmd_swap() { # 한 서버의 판본을 바꿔 그 서버만 다시 올린다.
  [[ -f "$ENV_FILE" ]] || { echo "먼저 up" >&2; exit 2; }
  local server="$1" version="$2" service
  save_version "$server" "$version"; load
  case "$server" in
    playwright) service=mcp-playwright ;; context7) service=mcp-context7 ;; grafana) service=mcp-grafana ;;
  esac
  "${COMPOSE[@]}" --profile grafana-mcp up -d --build --force-recreate --no-deps "$service"
  echo "$server -> $version"
}

case "${1:-}" in
  up) cmd_up ;;
  swap) shift; cmd_swap "$@" ;;
  status) load; "${COMPOSE[@]}" --profile grafana-mcp ps ;;
  env) echo "$ENV_FILE" ;;
  down) if [[ -f "$ENV_FILE" ]]; then load; "${COMPOSE[@]}" --profile grafana-mcp down -v --remove-orphans; rm -rf "$RUNTIME"; fi ;;
  *) echo "사용: $0 up | swap <playwright|context7|grafana> <판본> | status | env | down" >&2; exit 2 ;;
esac
