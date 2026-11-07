import pytest

from mint_litellm import cli
from mint_litellm.control import approvals
from mint_litellm.control.errors import ControlError

FAR = "20" + "99-01-01"
BASE = [
    "approve",
    "--relation",
    "r",
    "--scope",
    "key",
    "--scope-id",
    "k",
    "--tools",
    "echo",
    "--valid-until",
    FAR,
    "--basis",
    "b",
    "--requested-by",
    "x",
]


def test_allowed_flags_repeat_and_reach_the_conditions():
    a = cli.build_parser().parse_args(
        [
            *BASE,
            "--allowed-host",
            "b.test",
            "--allowed-host",
            "a.test",
            "--allowed-path",
            "/srv/docs",
            "--allowed-arg-key",
            "repo",
            "--allowed-arg-key",
            "repo",
        ]
    )
    conditions = approvals.build_conditions(
        high_risk_tools=None,
        business_hours_only=False,
        business_hours=None,
        require_consent=None,
        allowed_hosts=a.allowed_host,
        allowed_paths=a.allowed_path,
        allowed_arg_keys=a.allowed_arg_key,
    )
    assert conditions == {
        "allowed_hosts": ["a.test", "b.test"],
        "allowed_paths": ["/srv/docs"],
        "allowed_arg_keys": ["repo"],
    }


def test_conditions_without_the_flags_are_unchanged():
    a = cli.build_parser().parse_args(BASE)
    assert (a.allowed_host, a.allowed_path, a.allowed_arg_key) == (None, None, None)
    assert (
        approvals.build_conditions(
            high_risk_tools=None,
            business_hours_only=False,
            business_hours=None,
            require_consent=None,
        )
        == {}
    )


def test_blank_allowed_values_are_rejected():
    with pytest.raises(ControlError, match="allowed-host"):
        approvals.build_conditions(
            high_risk_tools=None,
            business_hours_only=False,
            business_hours=None,
            require_consent=None,
            allowed_hosts=[" "],
        )
