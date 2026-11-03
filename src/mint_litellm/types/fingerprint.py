"""호출별 승인에 쓰는 인자 지문.

플러그인과 운영자 명령이 같은 함수로 지문을 계산해야 승인과 실제 호출이 일치한다.
"""

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any

MIN_KEY_BYTES = 32


def decode_fingerprint_key(value: str) -> bytes:
    """base64url 문자열을 지문 키 바이트로 해독한다. 덧붙임 문자는 있어도 없어도 된다."""
    text = value.strip()
    try:
        key = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("fingerprint key is not base64url") from exc
    if len(key) < MIN_KEY_BYTES:
        raise ValueError(f"fingerprint key must decode to at least {MIN_KEY_BYTES} bytes")
    return key


def canonical_arguments(arguments: Mapping[str, Any] | None) -> bytes:
    """키 정렬·구분자 고정·비ASCII 보존 JSON의 UTF-8 바이트. 인자가 없으면 빈 객체로 본다."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ValueError("arguments must be a JSON object")
    text = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return text.encode("utf-8")


def argument_fingerprint(arguments: Mapping[str, Any] | None, key: bytes) -> str:
    """인자의 HMAC-SHA256 지문(소문자 16진 64자)."""
    if len(key) < MIN_KEY_BYTES:
        raise ValueError(f"fingerprint key must be at least {MIN_KEY_BYTES} bytes")
    return hmac.new(key, canonical_arguments(arguments), hashlib.sha256).hexdigest()
