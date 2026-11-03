import base64
import hashlib
import hmac
import re

import pytest

from mint_litellm.types.fingerprint import (
    MIN_KEY_BYTES,
    argument_fingerprint,
    canonical_arguments,
    decode_fingerprint_key,
)

KEY = bytes(range(32))


def test_fingerprint_is_hmac_of_canonical_json():
    arguments = {"b": 1, "a": {"y": [1, 2.5, None, True], "x": "한글"}}
    expected = hmac.new(
        KEY, '{"a":{"x":"한글","y":[1,2.5,null,true]},"b":1}'.encode(), hashlib.sha256
    ).hexdigest()
    assert argument_fingerprint(arguments, KEY) == expected
    assert re.fullmatch(r"[0-9a-f]{64}", expected)


def test_key_order_does_not_change_fingerprint():
    first = argument_fingerprint({"a": 1, "b": 2}, KEY)
    assert first == argument_fingerprint({"b": 2, "a": 1}, KEY)


def test_missing_arguments_are_an_empty_object():
    assert canonical_arguments(None) == b"{}"
    assert argument_fingerprint(None, KEY) == argument_fingerprint({}, KEY)


@pytest.mark.parametrize("value", [[1, 2], "text", 3])
def test_non_object_arguments_are_rejected(value):
    with pytest.raises(ValueError):
        canonical_arguments(value)


def test_non_finite_numbers_are_rejected():
    with pytest.raises(ValueError):
        canonical_arguments({"a": float("nan")})


def test_different_keys_give_different_fingerprints():
    other = bytes(reversed(range(32)))
    assert argument_fingerprint({"a": 1}, KEY) != argument_fingerprint({"a": 1}, other)


def test_short_key_is_rejected():
    with pytest.raises(ValueError):
        argument_fingerprint({}, b"x" * (MIN_KEY_BYTES - 1))


def test_decode_key_accepts_unpadded_base64url():
    raw = bytes(range(40))
    text = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert decode_fingerprint_key(text) == raw
    assert decode_fingerprint_key(text + "==" if len(text) % 4 == 2 else text) == raw


@pytest.mark.parametrize("text", ["", "not base64!", base64.urlsafe_b64encode(b"short").decode()])
def test_decode_key_rejects_invalid_or_short_values(text):
    with pytest.raises(ValueError):
        decode_fingerprint_key(text)
