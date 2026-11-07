"""봉인·내보내기·복원 명령은 1단계 명령 표 밖에 있고 같은 파서·실행 경로에 붙는다."""

import pytest

from mint_litellm import cli, cli_restore
from mint_litellm.types.cli_commands import COMMANDS, OperatorRole


def test_the_stage_two_commands_are_outside_the_first_stage_table_but_parse():
    names = {c.name for c in cli_restore.COMMANDS}
    assert names == {"seal verify", "export status", "restore check", "restore attest",
                     "restore accept-stale"}
    assert not names & {c.name for c in COMMANDS}
    assert names == set(cli_restore.HANDLERS) == names & set(cli.COMMAND_INDEX)
    parser = cli.build_parser()
    args = parser.parse_args(["seal", "verify", "--from", "3", "--to", "9"])
    assert (args.command_name, args.from_seq, args.to_seq) == ("seal verify", 3, 9)
    args = parser.parse_args(
        ["restore", "accept-stale", "--reason", "r", "--second-approver", "@t"]
    )
    assert args.command_name == "restore accept-stale" and args.second_approver == "@t"
    with pytest.raises(SystemExit):
        parser.parse_args(["restore", "attest"])  # 사유가 필수다


def test_roles_follow_the_design_table():
    by = {c.name: c for c in cli_restore.COMMANDS}
    for name in ("seal verify", "export status"):
        assert by[name].roles == frozenset(OperatorRole)
    for name in ("restore check", "restore attest", "restore accept-stale"):
        assert by[name].roles == frozenset({OperatorRole.ADMIN})
