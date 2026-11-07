#!/usr/bin/env bash
# 계수 역프록시의 시험용 CA와 서버 인증서를 만든다(이미 있으면 그대로 둔다). 사용: certs.sh <디렉터리>
# 최신 파이썬(3.13)의 엄격 검증을 통과하도록 CA에 basicConstraints·keyUsage를, 서버 인증서에 SAN·serverAuth를 넣는다.
set -euo pipefail
D=${1:?디렉터리}
[ -f "$D/ca.pem" ] && exit 0
mkdir -p "$D" && chmod 755 "$D"
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=acc-ca"   -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"   -keyout "$D/ca.key" -out "$D/ca.pem" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=cnt" -keyout "$D/cnt.key" -out "$D/cnt.csr" 2>/dev/null
printf 'subjectAltName=DNS:cnt,DNS:localhost
extendedKeyUsage=serverAuth
keyUsage=digitalSignature,keyEncipherment
basicConstraints=CA:FALSE
' > "$D/ext.cnf"
openssl x509 -req -in "$D/cnt.csr" -CA "$D/ca.pem" -CAkey "$D/ca.key" -CAcreateserial -days 30   -extfile "$D/ext.cnf" -out "$D/cnt.pem" 2>/dev/null
chmod 644 "$D"/*.pem "$D/cnt.key"
