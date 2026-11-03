"""식별자 생성. 호출 식별자와 사건 식별자는 시간순 정렬이 되는 UUID 7을 쓴다."""

import os
import time
import uuid


def new_uuid7() -> uuid.UUID:
    """RFC 9562 UUID 7: 48비트 밀리초 타임스탬프 + 무작위 74비트."""
    millis = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (millis & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & ((1 << 62) - 1)
    return uuid.UUID(int=value)
