#!/bin/sh
# LiteLLM을 시작하기 전에 MINT 필수 설정을 검사한다. 설정 파일을 읽지 못하거나 검사 모듈이 없거나
# 필수 설정이 어긋나면 LiteLLM을 시작하지 않는다. 검사하는 파일과 LiteLLM이 읽는 파일이 달라질 수
# 없도록 설정은 --config 하나로만 받는다: 다른 출처(환경 변수)가 정의되어 있으면 거부하고, 설정
# 인자(--config, --config=, -c, -c<값>)는 정확히 하나여야 하며 해석할 수 없는 형태도 거부한다.
set -u

fail() {
    echo "$1 LiteLLM을 시작하지 않습니다." >&2
    exit 1
}

# LiteLLM은 설정 파일을 환경 변수로도 받는다(CONFIG_FILE_PATH, LITELLM_CONFIG_BUCKET_*). 환경 변수가
# 인자보다 먼저 쓰이므로, 정의되어 있으면 빈 문자열이어도 검사한 파일과 다른 설정으로 시작할 수 있다.
for name in CONFIG_FILE_PATH LITELLM_CONFIG_BUCKET_NAME LITELLM_CONFIG_BUCKET_OBJECT_KEY     LITELLM_CONFIG_BUCKET_TYPE; do
    if [ -n "$(env | grep "^$name=" || true)" ]; then
        fail "다른 설정 출처($name)가 정의되어 있습니다. 설정은 --config 하나로만 받습니다."
    fi
done

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
