"""명령줄 도구: 명령 표와의 일치, 인증·역할, 성공 경로, 종료 코드."""

import json

import pytest
from litellm_stub import CONTROL_KEY, GOOD_KEY

psycopg = pytest.importorskip("psycopg")

from mint_litellm import cli  # noqa: E402
from mint_litellm.types.cli_commands import COMMANDS, OperatorRole  # noqa: E402


def test_every_command_in_the_table_has_a_handler_and_a_parser():
    names = {c.name for c in COMMANDS}
    assert names == set(cli.HANDLERS)
    assert set(cli.ARGUMENTS) <= names
    parser = cli.build_parser()
    for command in COMMANDS:
        with pytest.raises(SystemExit) as raised:
            parser.parse_args([*command.path, "--help"])
        assert raised.value.code == 0


def test_help_summaries_come_from_the_command_table(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--help"])
    out = capsys.readouterr().out
    for top in {c.path[0] for c in COMMANDS}:
        assert top in out


class Cli:
    def __init__(self, db_url, litellm_url, capsys, tmp_path):
        self.env = {
            "MINT_DATABASE_URL": db_url,
            "MINT_INSTANCE_ID": "clitest",
            "MINT_LITELLM_URL": litellm_url,
            "MINT_LITELLM_KEY": CONTROL_KEY,
        }
        self.capsys = capsys
        self.tmp = tmp_path

    def run(self, *argv, token=None):
        env = dict(self.env)
        if token:
            env["MINT_OPERATOR_TOKEN"] = token
        code = cli.main(list(argv), env)
        captured = self.capsys.readouterr()
        return code, captured.out, captured.err

    @staticmethod
    def token(out: str) -> str:
        return out.rsplit(": ", 1)[1].strip()


@pytest.fixture
def mint(fresh_db, stub, capsys, tmp_path):
    return Cli(fresh_db, stub.url, capsys, tmp_path)


def bootstrap(mint) -> str:
    code, out, _ = mint.run("init")
    assert code == 0
    code, out, err = mint.run("operator", "bootstrap", "--name", "boss", "--user-id", "human-2")
    assert code == 0, err
    return mint.token(out)


def test_init_creates_the_default_org_settings_and_prints_the_snippet(mint):
    path = mint.tmp / "snippet.yaml"
    code, out, err = mint.run("init", "--mcp-server", "docs=http://u.test/mcp",
                              "--output", str(path))
    assert code == 0 and "마이그레이션 적용" in err and "allowed_routes" in err
    assert "server_id: docs" in path.read_text(encoding="utf-8")
    again, _, err = mint.run("init")
    assert again == 0 and "이미 최신" in err
    with psycopg.connect(mint.env["MINT_DATABASE_URL"]) as conn:
        settings = conn.execute(
            "SELECT default_org_id, observable_policy_ids FROM mint.settings"
        ).fetchone()
        assert settings == ("default", ["R1", "R3"])
        kinds = [r[0] for r in conn.execute("SELECT kind FROM mint.events ORDER BY inserted_at")]
        assert kinds == ["settings_changed", "org_created", "settings_changed"]


def test_init_no_spend_logs_and_bad_server_argument(mint):
    code, out, _ = mint.run("init", "--no-spend-logs")
    assert code == 0 and "disable_spend_logs: true" in out
    code, _, err = mint.run("init", "--mcp-server", "missing-url")
    assert code == 1 and "이름=URL" in err


def test_init_creates_login_roles_when_passwords_are_given(mint):
    mint.env["MINT_PLUGIN_DB_PASSWORD"] = "plugin-pass"
    mint.env["MINT_CONTROL_DB_PASSWORD"] = "control-pass"
    code, _, err = mint.run("init")
    assert code == 0 and "mint_plugin_app" in err and "mint_control_app" in err
    with psycopg.connect(mint.env["MINT_DATABASE_URL"]) as conn:
        members = {r[0] for r in conn.execute(
            "SELECT m.rolname FROM pg_auth_members a JOIN pg_roles m ON m.oid = a.member"
            " JOIN pg_roles g ON g.oid = a.roleid WHERE g.rolname IN ('mint_plugin','mint_control')"
        )}
    assert {"mint_plugin_app", "mint_control_app"} <= members


def test_bootstrap_once_then_every_command_needs_a_valid_token(mint):
    token = bootstrap(mint)
    code, _, err = mint.run("operator", "bootstrap", "--name", "b2", "--user-id", "human-1")
    assert code == 1 and "이미 있어" in err
    code, _, err = mint.run("org", "add", "x", "--name", "X")
    assert code == 4 and "필요" in err
    code, _, err = mint.run("org", "add", "x", "--name", "X", token="mint_wrong")
    assert code == 4 and "올바르지" in err
    code, _, _ = mint.run("org", "add", "x", "--name", "X", token=token)
    assert code == 0


def test_role_checks_follow_the_command_table(mint):
    boss = bootstrap(mint)
    viewer = mint.token(mint.run("operator", "add", "eye", "--role", "viewer", token=boss)[1])
    approver = mint.token(mint.run("operator", "add", "ap", "--role", "approver",
                                   "--user-id", "human-1", token=boss)[1])
    for command in COMMANDS:
        if command.unauthenticated:
            continue
        for role, token in ((OperatorRole.VIEWER, viewer), (OperatorRole.APPROVER, approver)):
            if role in command.roles:
                continue
            argv = _minimal_argv(command.path)
            code, _, err = mint.run(*argv, token=token)
            assert code == 4, (command.name, role, err)
            assert "역할로 실행할 수 없습니다" in err
    assert mint.run("ledger", "list", token=viewer)[0] == 0
    assert mint.run("org", "add", "o", "--name", "O", token=viewer)[0] == 4
    assert mint.run("org", "add", "o", "--name", "O", token=approver)[0] == 4


def _minimal_argv(path):
    """권한 검사가 인자 검사보다 앞서므로 인자는 형식만 맞으면 된다."""
    parser = cli.build_parser()
    required = {
        "--role": "viewer", "--relation": "r", "--scope": "key", "--scope-id": "s",
        "--tools": "t", "--valid-until": "2099-01-01", "--basis": "b", "--requested-by": "u",
        "--reason": "r", "--org": "o", "--server": "s", "--purpose": "p",
        "--principal-type": "user",
        "--principal-id": "p", "--tool": "t", "--args-fp": "0" * 64, "--user": "u",
        "--data-scope": "d", "--text-version": "v", "--evidence-ref": "e",
        "--evidence-sha256": "0" * 64, "--kind": "provider", "--value": "v", "--name": "n",
        "--team": "t", "--catalog-sha256": "0" * 64, "--bundle-sha256": "0" * 64,
    }
    argv = list(path)
    leaf = parser
    for part in path:
        leaf = next(a for a in leaf._actions if hasattr(a, "choices") and a.choices).choices[part]
    for action in leaf._actions:
        if action.option_strings and action.required and action.option_strings[0] in required:
            argv += [action.option_strings[0], required[action.option_strings[0]]]
        elif not action.option_strings and action.dest not in ("help",):
            argv.append(list(action.choices)[0] if action.choices else "x")
    for group in leaf._mutually_exclusive_groups:
        if group.required:
            argv += [group._group_actions[0].option_strings[0], "x"]
    return argv


def test_expired_and_unauthorized_tokens_exit_with_four(mint):
    boss = bootstrap(mint)
    with psycopg.connect(mint.env["MINT_DATABASE_URL"]) as conn:
        conn.execute("UPDATE mint.operators SET expires_at = now() - interval '1 second'")
        conn.commit()
    assert mint.run("ledger", "list", token=boss)[0] == 4


def test_token_file_option(mint):
    boss = bootstrap(mint)
    path = mint.tmp / "token"
    path.write_text(boss + "\n", encoding="utf-8")
    code, _, _ = mint.run("--token-file", str(path), "ledger", "list")
    assert code == 0
    code, _, err = mint.run("--token-file", str(mint.tmp / "none"), "ledger", "list")
    assert code == 4 and "토큰 파일" in err


def test_full_activation_path_through_the_cli(mint):
    boss = bootstrap(mint)
    appr = mint.token(mint.run("operator", "add", "ap", "--role", "approver", "--user-id",
                               "human-1", token=boss)[1])
    assert mint.run("org", "add", "acme", "--name", "ACME", token=boss)[0] == 0
    code, _, err = mint.run("server", "register", "ctl-everything", token=boss)
    assert code == 1 and "--url" in err
    assert mint.run("server", "register", "ctl-everything", "--url", "http://u.test/mcp",
                    token=boss)[0] == 0
    code, out, _ = mint.run("relation", "add", "--org", "acme", "--server", "ctl-everything",
                            "--purpose", "ops", token=boss)
    relation = out.strip()
    # 신청자와 승인자가 같은 사용자이면 거부, --second-approver로 다른 사용자가 확인하면 허용
    args = ["approve", "--relation", relation, "--scope", "key", "--scope-id", GOOD_KEY,
            "--tools", "echo", "--valid-until", "2099-01-01", "--basis", "근거",
            "--requested-by", "human-1"]
    code, _, err = mint.run(*args, token=appr)
    assert code == 1 and "--second-approver" in err
    boss_file = mint.tmp / "boss"
    boss_file.write_text(boss, encoding="utf-8")
    code, out, _ = mint.run(*args, "--second-approver", f"@{boss_file}", token=appr)
    assert code == 0, out
    code, out, _ = mint.run("ledger", "list", "--json", "--kind", "server_approved", token=boss)
    assert code == 0 and json.loads(out.strip())["kind"] == "server_approved"
    code, out, _ = mint.run("revoke", "not-a-uuid", "--reason", "r", token=appr)
    assert code == 1  # 잘못된 식별자는 데이터베이스 오류로 거부된다


def test_settings_and_org_commands(mint):
    boss = bootstrap(mint)
    run = lambda *a: mint.run(*a, token=boss)  # noqa: E731
    assert run("org", "add", "o1", "--name", "O1")[0] == 0
    assert run("org", "bind", "--org", "o1", "--team", "t1")[0] == 0
    code, _, err = run("org", "bind", "--org", "default", "--team", "t1")
    assert code == 1 and "이미 조직 o1" in err
    assert run("org", "unbind", "--team", "t1")[0] == 0
    assert run("settings", "default-org", "--org", "o1")[0] == 0
    assert run("settings", "default-org", "--clear")[0] == 0
    mint.env["MINT_MODE"] = "observe"
    assert run("settings", "mode", "observe")[0] == 0
    observable = run("settings", "observable", "--reasons", "APPROVAL_ABSENT", "--policy-ids", "R1")
    assert observable[0] == 0
    code, _, err = run("settings", "observable", "--reasons", "DENIED")
    assert code == 1 and "전환할 수 없는" in err
    mint.env["MINT_MODE"] = "enforce"
    code, _, err = run("settings", "mode", "observe")
    assert code == 1 and "MINT_MODE" in err
    code, out, _ = run("status")
    assert code == 0 and "모드: enforce (저장 observe, 환경 상한 enforce)" in out
    code, out, _ = run("status", "--json")
    info = json.loads(out)
    assert info["mode"]["effective"] == "enforce" and info["policy_version"] is None


def test_policy_activate_without_the_verifier_module_is_refused(mint, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "mint_litellm.plugin.policy.bundle", None)
    boss = bootstrap(mint)
    code, _, err = mint.run("policy", "activate", "v1", "--catalog-sha256", "a" * 64,
                            "--bundle-sha256", "b" * 64, token=boss)
    assert code == 1 and "검증 모듈" in err


def test_check_exits_nonzero_on_required_violations_and_prints_levels(mint):
    boss = bootstrap(mint)
    config = mint.tmp / "config.yaml"
    mint.run("init", "--output", str(config))
    code, out, err = mint.run("check", "--config", str(config), token=boss)
    assert code == 1 and "필수 설정 점검에 실패" in err
    assert "[실패] object_permission 서버 식별자 형식" in out
    assert "[경고] PROXY_BASE_URL" in out
    assert "  unbound_keys: 1" in out
    bad = mint.tmp / "bad.yaml"
    bad.write_text("general_settings: {}\n", encoding="utf-8")
    code, out, _ = mint.run("check", "--config", str(bad), "--json", token=boss)
    result = json.loads(out)
    assert code == 1 and result["ok"] is False
    assert {f["level"] for f in result["findings"]} >= {"FAIL"}
    env_file = mint.tmp / "litellm.env"
    env_file.write_text("PROXY_BASE_URL=https://g.test\nMCP_TRUSTED_REDIRECT_ORIGINS=https://a.test\n",
                        encoding="utf-8")
    code, out, _ = mint.run("check", "--config", str(config), "--env-file", str(env_file),
                            token=boss)
    assert "[경고] PROXY_BASE_URL" not in out


def test_check_without_a_config_file_fails(mint):
    boss = bootstrap(mint)
    code, out, _ = mint.run("check", token=boss)
    assert code == 1 and "[실패] LiteLLM 설정 파일" in out


def test_database_errors_are_reported_without_a_traceback(mint):
    boss = bootstrap(mint)
    code, _, err = mint.run("denial", "lift", "not-a-uuid", token=boss)
    assert code == 1 and "데이터베이스 오류" in err
