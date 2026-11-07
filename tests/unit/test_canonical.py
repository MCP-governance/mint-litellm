import math

import pytest

from mint_litellm.types.canonical import canonical_text, pin_set_sha256, pin_sha256, sha256


def test_jcs_vectors():
    assert canonical_text({"b": 1.0, "a": -0.0}) == '{"a":0,"b":1}'
    assert canonical_text([333333333.33333329, 1e30, 4.50, 2e-3, 1e-27]) == (
        "[333333333.3333333,1e+30,4.5,0.002,1e-27]"
    )
    assert canonical_text([1e-6, 1e-7, 1e20, 1e21]) == (
        "[0.000001,1e-7,100000000000000000000,1e+21]"
    )
    assert canonical_text({"\ue000": 1, "😀": 2}) == '{"😀":2,"\ue000":1}'
    assert canonical_text({"한글": '\n\\"'}) == '{"한글":"\\n\\\\\\""}'
    assert sha256({"a": 1}) == "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862"


@pytest.mark.parametrize("value", [math.nan, math.inf, 2**53, {1: 2}, "\ud800", object()])
def test_jcs_rejects_invalid_values(value):
    with pytest.raises((ValueError, UnicodeError)):
        canonical_text(value)


def test_pin_domain_and_set():
    tool = {"description": "read", "input_schema": {"type": "object"}}
    changed = {**tool, "output_schema": {"type": "string"}, "annotations": {"readOnly": True}}
    assert pin_sha256(tool) == pin_sha256(changed)
    assert sha256(tool) != sha256(changed)
    assert pin_set_sha256({"b": tool, "a": tool}) == pin_set_sha256({"a": tool, "b": tool})
