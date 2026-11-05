#!/bin/sh
# LiteLLM을 시작하기 전에 MINT 필수 설정을 검사한다. 설정 파일을 읽지 못하거나 검사 모듈이 없거나
# 필수 설정이 어긋나면 LiteLLM을 시작하지 않는다.
set -u

config=""
previous=""
for argument in "$@"; do
    if [ "$previous" = "--config" ]; then
        config="$argument"
    fi
    case "$argument" in
        --config=*) config="${argument#--config=}" ;;
    esac
    previous="$argument"
done

if [ -z "$config" ]; then
    echo "--config가 없어 MINT 기동 전 검사를 할 수 없습니다. LiteLLM을 시작하지 않습니다." >&2
    exit 1
fi

if ! python -m mint_litellm.control.preflight "$config"; then
    exit 1
fi

exec docker/prod_entrypoint.sh "$@"
