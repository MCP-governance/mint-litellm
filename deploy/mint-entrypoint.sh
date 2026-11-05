#!/bin/sh
# LiteLLM을 시작하기 전에 MINT 필수 설정을 검사한다. 설정 파일을 읽지 못하거나 검사 모듈이 없거나
# 필수 설정이 어긋나면 LiteLLM을 시작하지 않는다. 검사하는 파일과 LiteLLM이 읽는 파일이 달라질 수
# 없도록 설정 인자는 정확히 하나여야 하고(--config, --config=, -c, -c<값>), 해석할 수 없는 형태는
# 거부한다.
set -u

fail() {
    echo "$1 LiteLLM을 시작하지 않습니다." >&2
    exit 1
}

config=""
count=0
expect=0
for argument in "$@"; do
    if [ "$expect" = 1 ]; then
        config="$argument"
        count=$((count + 1))
        expect=0
        continue
    fi
    case "$argument" in
        --config | -c) expect=1 ;;
        --config=*)
            config="${argument#--config=}"
            count=$((count + 1))
            ;;
        --*) ;;
        -c?*)
            config="${argument#-c}"
            count=$((count + 1))
            ;;
        -[!-]*c*) fail "설정 인자를 해석할 수 없는 형태입니다($argument)." ;;
    esac
done

[ "$expect" = 0 ] || fail "--config에 값이 없습니다."
[ "$count" -ge 1 ] || fail "--config가 없어 MINT 기동 전 검사를 할 수 없습니다."
[ "$count" -eq 1 ] || fail "설정 인자가 둘 이상입니다. 검사한 파일과 기동하는 파일이 달라질 수 있습니다."

if ! python -m mint_litellm.control.preflight "$config"; then
    exit 1
fi

exec docker/prod_entrypoint.sh "$@"
