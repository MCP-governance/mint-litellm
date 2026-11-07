#!/usr/bin/env bash
# 장비에서 처음부터: 스택 기동 → 사용자·키·운영자 → 계수 서비스 → site.json → 기준 스냅숏. 깨끗한 셸에서 실행한다.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../../.." && pwd)"
export ACC_HOME=${ACC_HOME:-$HOME/mint-s2}
PY=$ACC_HOME/venv/bin/python
cd "$REPO"
bash "$HERE/up.sh"
for _ in $(seq 1 60); do
  curl -fs http://127.0.0.1:4300/health/readiness >/dev/null && curl -fs http://127.0.0.1:4301/health/readiness >/dev/null && break
  sleep 5
done
"$PY" "$HERE/bootstrap.py"
export DOCKER_CONFIG=$HOME/.docker-acc COMPOSE_PROJECT_NAME=mints2 ACC_DUMP_DIR=$ACC_HOME/dumps
C=(docker compose --env-file deploy/.env -f deploy/compose.yaml -f tests/acceptance/stage2/device/acc.yaml)
"${C[@]}" --profile worker --profile console up -d worker console
"$PY" "$HERE/bootstrap.py" operators
pkill -f counter_service.py || true
setsid nohup "$PY" "$HERE/counter_service.py" http://127.0.0.1:4320/__log 4350 > "$ACC_HOME/counter.log" 2>&1 < /dev/null &
source "$HERE/env.sh"
mkdir -p tests/acceptance/stage2/.runtime
"$PY" "$HERE/make_site.py" > tests/acceptance/stage2/.runtime/site.json
sleep 20
"$PY" -m tests.acceptance.stage2.adapters snapshot apply
echo all-done
