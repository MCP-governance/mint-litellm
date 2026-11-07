"""계약의 JCS(RFC 8785) 직렬화와 두 해시 정의역."""

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("JCS requires finite numbers")
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    mantissa, _, exponent = repr(abs(value)).lower().partition("e")
    whole, _, fraction = mantissa.partition(".")
    digits = (whole + fraction).lstrip("0")
    point = len(whole) + int(exponent or 0)
    # repr uses a leading zero only for fixed-point fractions.
    if whole == "0":
        leading = len(fraction) - len(fraction.lstrip("0"))
        point = -leading
    digits = digits.rstrip("0")
    if 0 < point <= 21:
        body = digits[:point] + ("." + digits[point:] if point < len(digits) else "")
        body += "0" * max(0, point - len(digits))
    elif -6 < point <= 0:
        body = "0." + "0" * -point + digits
    else:
        power = point - 1
        body = digits[0] + ("." + digits[1:] if len(digits) > 1 else "")
        body += "e" + ("+" if power >= 0 else "-") + str(abs(power))
    return sign + body


def canonical_text(value: Any) -> str:
    """JSON 값만 허용한다. 키는 UTF-16 순서, 숫자는 binary64 표기이다."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        if abs(value) > 2**53 - 1:
            raise ValueError("JCS integer exceeds the interoperable exact range")
        return str(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, list):
        return "[" + ",".join(canonical_text(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JCS object keys must be strings")
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", errors="strict"))
        return (
            "{" + ",".join(canonical_text(k) + ":" + canonical_text(value[k]) for k in keys) + "}"
        )
    raise ValueError("JCS accepts JSON values only")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_text(value).encode("utf-8")).hexdigest()


def pin_sha256(definition: Mapping[str, Any]) -> str:
    return sha256(
        {"description": definition.get("description"), "input_schema": definition["input_schema"]}
    )


def pin_set_sha256(definitions: Mapping[str, Mapping[str, Any]]) -> str:
    return sha256({name: pin_sha256(value) for name, value in definitions.items()})


def credential_scope(auth_type: str) -> str:
    return "per_user" if auth_type in {"oauth_delegate", "true_passthrough"} else "server"
