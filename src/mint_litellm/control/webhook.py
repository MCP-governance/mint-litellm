"""경보 웹훅: 주소 검사, 서명, 본문, 전송.

요청 헤더 ``X-MINT-Signature``는 ``t=<unix 초>,v1=<HMAC-SHA256 16진수>``이고, 서명 대상은
``<t>.`` 뒤에 본문 바이트를 붙인 값이다. 수신 쪽은 ``verify``로 시각 차이(5분)와 서명을 함께 본다.
``X-MINT-Delivery``는 전송 식별자로, 수신 쪽이 중복을 거를 때 쓴다. 응답 본문은 읽어서 버린다.
"""

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx

from mint_litellm.control.errors import ControlError

log = logging.getLogger("mint_litellm.webhook")

SIGNATURE_HEADER = "X-MINT-Signature"
DELIVERY_HEADER = "X-MINT-Delivery"
TOLERANCE_SECONDS = 300
ATTEMPTS = 3
TIMEOUT_SECONDS = 5.0


def check_url(url: str, allowed_hosts: Sequence[str]) -> None:
    """``https`` 주소이고 호스트가 허용 목록에 있어야 한다. 아니면 ``ControlError``."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ControlError("웹훅 주소는 https만 받습니다.")
    if parts.hostname not in allowed_hosts:
        raise ControlError("웹훅 주소의 호스트가 허용 목록에 없습니다.")


def sign(key: bytes, body: bytes, timestamp: int) -> str:
    mac = hmac.new(key, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify(
    key: bytes,
    header: str,
    body: bytes,
    *,
    now: int | None = None,
    tolerance: int = TOLERANCE_SECONDS,
) -> bool:
    """수신 쪽 검증. 형식이 틀리거나, 시각 차이가 ``tolerance``를 넘거나, 서명이 다르면 거짓이다."""
    fields = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    try:
        timestamp = int(fields["t"])
    except (KeyError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance:
        return False
    expected = sign(key, body, timestamp).rsplit("v1=", 1)[1]
    return hmac.compare_digest(expected, fields.get("v1", ""))


def build_body(row: Mapping[str, Any]) -> bytes:
    payload = {
        "kind": row["kind"],
        "severity": row["severity"],
        "subject": row["subject"],
        "count": row["count"],
        "first_at": row["first_at"].isoformat(),
        "last_at": row["last_at"].isoformat(),
        "console_url": None,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def post(
    client: httpx.Client, url: str, key: bytes, delivery_id: str, body: bytes
) -> tuple[bool, int]:
    """최대 ``ATTEMPTS``번 보낸다. 2xx면 성공. (성공 여부, 보낸 횟수)를 돌려준다."""
    for attempt in range(1, ATTEMPTS + 1):
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign(key, body, int(time.time())),
            DELIVERY_HEADER: delivery_id,
        }
        try:
            status = client.post(url, content=body, headers=headers).status_code
        except httpx.HTTPError as error:
            log.warning("웹훅 전송 %d회째 실패: %s", attempt, type(error).__name__)
            continue
        if 200 <= status < 300:
            return True, attempt
        log.warning("웹훅 전송 %d회째 응답 상태 %d", attempt, status)
    return False, ATTEMPTS


def client(timeout: float = TIMEOUT_SECONDS) -> httpx.Client:
    """리다이렉트를 따르지 않고 요청마다 같은 제한 시간을 쓰는 클라이언트."""
    return httpx.Client(timeout=timeout, follow_redirects=False)
