"""호출별 승인에 쓰는 인자 지문.

플러그인과 운영자 명령이 같은 함수로 지문을 계산해야 승인과 실제 호출이 일치한다.
"""

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from typing import Any

MIN_KEY_BYTES = 32
_BASE64URL = re.compile(r"[A-Za-z0-9_-]+={0,2}")


def decode_fingerprint_key(value: str) -> bytes:
    """base64url 문자열을 지문 키 바이트로 해독한다. 덧붙임 문자는 있어도 없어도 된다.

    표준 해독기는 알파벳 밖 문자를 건너뛰므로 먼저 문자 집합을 확인한다.
    """
    text = value.strip()
    if not _BASE64URL.fullmatch(text):
        raise ValueError("fingerprint key is not base64url")
    body = text.rstrip("=")
    try:
        key = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("fingerprint key is not base64url") from exc
    if len(key) < MIN_KEY_BYTES:
        raise ValueError(f"fingerprint key must decode to at least {MIN_KEY_BYTES} bytes")
    return key


def canonical_arguments(arguments: Mapping[str, Any] | None) -> bytes:
    """키 정렬·구분자 고정·비ASCII 보존 JSON의 UTF-8 바이트. 인자가 없으면 빈 객체로 본다.

    입력은 JSON 문서를 해독한 값이어야 한다. 숫자 표기(1과 1.0)와 유니코드 정규화는 손대지 않으므로
    표기가 다르면 지문도 다르다.
    """
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ValueError("arguments must be a JSON object")
    try:
        text = json.dumps(
            arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except TypeError as exc:
        raise ValueError("arguments must contain only JSON values") from exc
    return text.encode("utf-8")


def argument_fingerprint(arguments: Mapping[str, Any] | None, key: bytes) -> str:
    """인자의 HMAC-SHA256 지문(소문자 16진 64자)."""
    if len(key) < MIN_KEY_BYTES:
        raise ValueError(f"fingerprint key must be at least {MIN_KEY_BYTES} bytes")
    return hmac.new(key, canonical_arguments(arguments), hashlib.sha256).hexdigest()
