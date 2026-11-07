"""approve의 --confirm-* 옵션이 승인 조건 require_user_confirmation으로 저장되는지 본다."""

from types import SimpleNamespace

import pytest

from mint_litellm import cli
from mint_litellm.control import approvals
from mint_litellm.control.errors import ControlError

BASE = [
    "approve", "--relation", "r", "--scope", "key", "--scope-id", "k", "--tools", "echo,read",
    "--valid-until", "20" + "99-01-01", "--basis", "b", "--requested-by", "x",
]


def conditions(args):
    a = cli.build_parser().parse_args([*BASE, *args])
    return approvals.build_conditions(
        high_risk_tools=a.high_risk_tools, business_hours_only=False, business_hours=None,
        require_consent=None, confirm_tools=a.confirm_tool, confirm_purpose=a.confirm_purpose,
        confirm_fields=a.confirm_field,
    )


def test_flags_repeat_and_become_a_sorted_condition():
    saved = conditions([
        "--confirm-tool", "read", "--confirm-tool", "echo", "--confirm-purpose", "전송",
        "--confirm-field", "to", "--confirm-field", "body", "--confirm-field", "to",
    ])
    assert saved == {"require_user_confirmation": {
        "tools": ["echo", "read"], "purpose": "전송", "display_fields": ["body", "to"]}}


def test_fields_are_optional_and_absent_flags_add_nothing():
    saved = conditions(["--confirm-tool", "echo", "--confirm-purpose", "p"])
    assert saved["require_user_confirmation"]["display_fields"] == []
    assert conditions([]) == {}


@pytest.mark.parametrize("flags", [
    ["--confirm-tool", "echo"],
    ["--confirm-tool", "echo", "--confirm-purpose", " "],
    ["--confirm-purpose", "p"],
    ["--confirm-field", "q"],
])
def test_incomplete_confirmation_flags_are_rejected(flags):
    with pytest.raises(ControlError):
        conditions(flags)


def test_confirm_tool_must_be_one_of_the_approved_tools():
    a = cli.build_parser().parse_args([*BASE, "--confirm-tool", "write", "--confirm-purpose", "p"])
    with pytest.raises(ControlError, match="write"):
        cli._approve(SimpleNamespace(args=a))
