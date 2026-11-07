import pytest

from mint_litellm.control.contract_observe import drift_kind, normalize_tools
from mint_litellm.control.contract_pin import verify_pin
from mint_litellm.control.errors import AdminError

TOOL = {"description": "read", "input_schema": {"type": "object"}}


def test_drift_classification():
    assert drift_kind(TOOL, None) == "removed"
    assert drift_kind(TOOL, {**TOOL, "input_schema": {}}) == "input_schema"
    assert drift_kind(TOOL, {**TOOL, "description": "changed"}) == "description"
    assert drift_kind(TOOL, {**TOOL, "annotations": {"readOnlyHint": True}}) == "description"
    assert drift_kind({**TOOL, "output_schema": {}}, TOOL) == "output_schema"
    assert drift_kind(TOOL, {**TOOL, "output_schema": {}}) == "none"


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"tools": [], "error": True},
        {"tools": [{"name": "x"}]},
        {"tools": [{"name": "x", "inputSchema": {}}] * 2},
        {"tools": [{"name": "x", "inputSchema": {}}] * 501},
    ],
)
def test_listing_failure_is_not_empty_catalog(body):
    with pytest.raises(ValueError):
        normalize_tools(body)


def test_listing_normalization_and_empty_catalog():
    assert normalize_tools({"tools": []}) == {}
    assert normalize_tools(
        {"tools": [{"name": "read", "description": "read", "inputSchema": {"type": "object"}}]}
    ) == {"read": TOOL}


def test_pin_checks_full_set_and_hash_domain():
    assert verify_pin({"read": TOOL}, {"read": TOOL})
    for response in (
        {},
        {"other": TOOL},
        {"read": TOOL, "extra": TOOL},
        {"read": {**TOOL, "input_schema": {}}},
        {"read": {}},
    ):
        with pytest.raises(AdminError):
            verify_pin({"read": TOOL}, response)
