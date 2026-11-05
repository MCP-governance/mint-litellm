#!/bin/sh
# 최초 기동 때 MINT 전용 데이터베이스를 만든다. LiteLLM 데이터베이스와 분리한다.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -c "CREATE DATABASE mint"
