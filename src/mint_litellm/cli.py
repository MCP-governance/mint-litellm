"""운영자 명령줄 도구 ``mint-litellm``.

명령 표의 정본은 ``types/cli_commands.py``의 ``COMMANDS``이다. 이 모듈은 표의 모든 명령에
인자와 처리 함수를 붙인다. 인증이 필요한 명령은 토큰으로 운영자를 확인하고, 변경은 한 트랜잭션에서
상태와 사건을 함께 기록한다.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg

from mint_litellm.control import (
    allowlists,
    approvals,
    checks,
    consents,
    ledger,
    litellm_config,
    operators,
    orgs,
    policy_versions,
    relations,
    servers,
    settings_store,
    status,
)
from mint_litellm.control.config import ControlConfig
from mint_litellm.control.context import Context, Operator
from mint_litellm.control.db.connection import connect, fetchall, fetchone
from mint_litellm.control.db.migrate import apply_migrations
from mint_litellm.control.db.roles import LOGIN_ROLES, ensure_login_roles
from mint_litellm.control.errors import AuthError, ControlError
from mint_litellm.control.litellm_admin import LiteLLMAdmin
from mint_litellm.types.cli_commands import (
    COMMANDS,
    CONTROL_PLANE_ALLOWED_ROUTES,
    CliCommand,
    OperatorRole,
)
from mint_litellm.types.events import EventKind
from mint_litellm.types.identity import SubjectType
from mint_litellm.types.settings import Mode

PROGRAM = "mint-litellm"


@dataclass
class Run:
    args: argparse.Namespace
    cfg: ControlConfig
    environ: dict[str, str]
    conn: psycopg.Connection
    ctx: Context | None

    def context(self) -> Context:
        assert self.ctx is not None
        return self.ctx

    def litellm(self) -> LiteLLMAdmin:
        return self.cfg.litellm()


Handler = Callable[[Run], None]


def _when(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"날짜·시각 형식이 올바르지 않습니다: {text}") from None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def _out(text: str = "") -> None:
    print(text)


def _issued(token: str) -> None:
    _out(f"발급된 토큰(다시 표시되지 않습니다): {token}")


# ---------------------------------------------------------------------------
# 처리 함수
# ---------------------------------------------------------------------------


def _init(run: Run) -> None:
    args, cfg = run.args, run.cfg
    servers_arg: dict[str, tuple[str, str]] = {}
    for item in args.mcp_server or []:
        name, sep, url = item.partition("=")
        if not sep or not name or not url:
            raise ControlError(f"--mcp-server는 이름=URL 형식이어야 합니다: {item}")
        servers_arg[name] = (name, url)
    applied = apply_migrations(run.conn)
    passwords = {
        group: run.environ[var]
        for group, var in (
            ("mint_plugin", "MINT_PLUGIN_DB_PASSWORD"),
            ("mint_control", "MINT_CONTROL_DB_PASSWORD"),
        )
        if run.environ.get(var)
    }
    configured_roles = ensure_login_roles(run.conn, passwords)
    ctx = Context(run.conn, cfg.instance_id, Operator("init", "admin", None))
    version = settings_store.ensure(run.conn, cfg.instance_id)
    if version is not None:
        ctx.emit(
            EventKind.SETTINGS_CHANGED,
            cfg.instance_id,
            version,
            {
                "created": True,
                "observable_policy_ids": list(settings_store.DEFAULT_OBSERVABLE_POLICY_IDS),
            },
        )
    if not fetchone(
        run.conn,
        "SELECT 1 AS x FROM mint.orgs WHERE instance_id = %s AND org_id = %s",
        (cfg.instance_id, args.org_id),
    ):
        orgs.add(ctx, args.org_id, args.org_name)
    if settings_store.load(run.conn, cfg.instance_id)["default_org_id"] is None:
        settings_store.set_default_org(ctx, args.org_id)
    text = litellm_config.snippet(servers_arg, no_spend_logs=args.no_spend_logs)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8", newline="\n")
    else:
        _out(text.rstrip("\n"))
    log = sys.stderr
    print(f"마이그레이션 적용: {', '.join(applied) or '없음(이미 최신)'}", file=log)
    if configured_roles:
        print(f"로그인 역할 설정: {', '.join(configured_roles)}", file=log)
    elif not passwords:
        print(
            "로그인 역할은 만들지 않았습니다. 플러그인 접속에는 "
            f"{LOGIN_ROLES['mint_plugin']}, 제어 평면에는 {LOGIN_ROLES['mint_control']} "
            "역할을 쓰도록 MINT_PLUGIN_DB_PASSWORD·MINT_CONTROL_DB_PASSWORD를 지정하십시오.",
            file=log,
        )
    print(
        "제어 평면 LiteLLM 키는 proxy_admin 사용자 키로 발급하고 allowed_routes를 다음으로 "
        "제한하십시오:\n  "
        + ", ".join(CONTROL_PLANE_ALLOWED_ROUTES)
        + "\n그 값을 MINT_LITELLM_KEY로 설정합니다.",
        file=log,
    )


def _gather_env(run: Run) -> dict[str, str]:
    env = dict(run.environ)
    if getattr(run.args, "env_file", None):
        env.update(litellm_config.load_env_file(Path(run.args.env_file)))
    return env


def _config_path(run: Run) -> Path | None:
    return Path(run.args.config) if getattr(run.args, "config", None) else run.cfg.litellm_config


def _optional_litellm(run: Run) -> LiteLLMAdmin | None:
    cfg = run.cfg
    return cfg.litellm() if cfg.litellm_url and cfg.litellm_key else None


def _check(run: Run) -> None:
    ctx = run.context()
    registered = fetchall(
        run.conn,
        "SELECT server_id, url_sha256, transport, auth_type FROM mint.servers"
        " WHERE instance_id = %s",
        (ctx.instance_id,),
    )
    report = checks.run_checks(
        litellm_config.load_config(_config_path(run)),
        _gather_env(run),
        _optional_litellm(run),
        replicas=run.cfg.replicas,
        registered_servers=registered,
    )
    if run.args.json:
        _out(json.dumps({
            "ok": not report.failed,
            "findings": [{"item": f.item, "level": f.level.name, "message": f.message}
                         for f in report.findings],
            "observations": report.observations,
        }, ensure_ascii=False))
    else:
        _out(report.render())
    if report.failed:
        raise ControlError("필수 설정 점검에 실패했습니다.")


def _status(run: Run) -> None:
    info = status.collect(
        run.context(), run.cfg, _optional_litellm(run),
        litellm_config.load_config(_config_path(run)), _gather_env(run),
    )
    _out(json.dumps(info, ensure_ascii=False) if run.args.json else status.render(info))


def _operator_add(run: Run) -> None:
    a = run.args
    _issued(operators.add(run.context(), run.litellm(), a.name, OperatorRole(a.role), a.user_id,
                          a.expires_in_days))


def _operator_bootstrap(run: Run) -> None:
    token = operators.bootstrap(run.conn, run.cfg.instance_id, run.litellm(), run.args.name,
                                run.args.user_id)
    _issued(token)


def _second(run: Run) -> Operator | None:
    value = getattr(run.args, "second_approver", None)
    if not value:
        return None
    if value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="utf-8").strip()
    return operators.authenticate(run.conn, run.cfg.instance_id, value)


def _approve(run: Run) -> None:
    a = run.args
    approval_id, notes = approvals.approve(
        run.context(), run.litellm(),
        relation_id=a.relation, scope_type=a.scope, scope_id=a.scope_id, tools=a.tools,
        valid_from=a.valid_from or datetime.now(UTC), valid_until=a.valid_until,
        approval_class=a.approval_class, basis_text=a.basis, requested_by=a.requested_by,
        allowlist_ids=a.allowlist_ref or [], second=_second(run),
    )
    for note in notes:
        print(note, file=sys.stderr)
    _out(f"승인 {approval_id}")


def _subject(a: argparse.Namespace) -> tuple[SubjectType, str]:
    return (SubjectType.TEAM, a.team) if a.team else (SubjectType.USER, a.user)


def _ledger_list(run: Run) -> None:
    a = run.args
    rows = ledger.list_events(
        run.conn, run.cfg.instance_id, kind=a.kind, since=a.since, until=a.until,
        invocation_id=a.invocation_id, limit=a.limit,
    )
    for row in rows:
        if a.json:
            _out(json.dumps(row, ensure_ascii=False, default=str))
        else:
            _out(f"{row['inserted_at'].isoformat()}\t{row['kind']}\t{row['actor'] or '-'}\t"
                 f"{row['source_key']}")


def _policy_activate(run: Run) -> None:
    a = run.args
    policy_versions.activate(run.context(), a.version, a.catalog_sha256, a.bundle_sha256,
                             policy_versions.load_verifier())


def _printing(call: Callable[..., Any], *names: str):
    def handler(run: Run) -> None:
        result = call(run.context(), *[getattr(run.args, n) for n in names])
        if isinstance(result, str):
            _out(result)

    return handler


HANDLERS: dict[str, Handler] = {
    "init": _init,
    "check": _check,
    "status": _status,
    "operator add": _operator_add,
    "operator bootstrap": _operator_bootstrap,
    "server register": lambda r: servers.register(r.context(), r.litellm(), r.args.server_id,
                                                  r.args.url),
    "server suspend": _printing(servers.suspend, "server_id"),
    "server resume": _printing(servers.resume, "server_id"),
    "relation add": lambda r: _out(relations.add(
        r.context(), r.args.org, r.args.server, r.args.purpose, r.args.team)),
    "relation terminate": _printing(relations.terminate, "relation"),
    "approve": _approve,
    "deny": lambda r: _out(approvals.create_denial(
        r.context(), scope_type=r.args.scope, scope_id=r.args.scope_id, server_id=r.args.server,
        tools=r.args.tools, reason=r.args.reason, org_id=r.args.org)),
    "denial lift": _printing(approvals.lift_denial, "denial_id"),
    "revoke": lambda r: approvals.revoke(r.context(), r.args.approval_id, r.args.reason,
                                         r.args.deny),
    "call-approval request": lambda r: _out(approvals.call_request(
        r.context(), org_id=r.args.org, principal_type=r.args.principal_type,
        principal_id=r.args.principal_id, server_id=r.args.server, tool=r.args.tool,
        args_fp=r.args.args_fp, fp_key_version=r.args.fp_key_version,
        policy_version_id=r.args.policy_version, expires_in_minutes=r.args.expires_in_minutes,
        requested_by=r.args.requested_by or r.context().operator.user_id
        or r.context().operator.name)),
    "call-approval approve": lambda r: approvals.call_approve(
        r.context(), r.args.call_approval_id, _second(r)),
    "call-approval reject": _printing(approvals.call_reject, "call_approval_id"),
    "call-approval cancel": _printing(approvals.call_cancel, "call_approval_id"),
    "consent add": lambda r: _out(consents.add(
        r.context(), r.litellm(), r.args.org, r.args.user, r.args.server, r.args.purpose,
        r.args.data_scope, r.args.text_version, r.args.granted_at or datetime.now(UTC),
        r.args.valid_until, r.args.evidence_ref, r.args.evidence_sha256)),
    "consent revoke": _printing(consents.revoke, "consent_id"),
    "allowlist add": lambda r: _out(allowlists.add(
        r.context(), r.args.kind, r.args.value, r.args.valid_from or datetime.now(UTC),
        r.args.valid_until, r.args.org, r.args.note)),
    "allowlist lift": _printing(allowlists.lift, "allowlist_id"),
    "org add": lambda r: orgs.add(r.context(), r.args.org_id, r.args.name),
    "org close": _printing(orgs.close, "org_id"),
    "org bind": lambda r: orgs.bind(r.context(), r.args.org, *_subject(r.args)),
    "org unbind": lambda r: orgs.unbind(r.context(), *_subject(r.args)),
    "settings default-org": lambda r: settings_store.set_default_org(
        r.context(), None if r.args.clear else r.args.org),
    "settings mode": lambda r: settings_store.set_mode(
        r.context(), Mode(r.args.mode), r.cfg.mode_ceiling),
    "settings observable": lambda r: settings_store.set_observable(
        r.context(), r.args.reasons, r.args.policy_ids),
    "policy activate": _policy_activate,
    "ledger list": _ledger_list,
}


# ---------------------------------------------------------------------------
# 인자 정의
# ---------------------------------------------------------------------------


def _a(*flags: str, **kwargs: Any) -> tuple[tuple[str, ...], dict[str, Any]]:
    return flags, kwargs


_JSON = _a("--json", action="store_true", help="JSON으로 출력")
_CONFIG = _a("--config", help="LiteLLM config.yaml 경로(기본: MINT_LITELLM_CONFIG)")
_ENV_FILE = _a("--env-file", help="LiteLLM 환경 파일(KEY=VALUE)")
_SECOND = _a("--second-approver", help="요청자와 다른 사용자의 운영자 토큰(또는 @파일)")
_SERVER_ID = _a("server_id", help="LiteLLM 서버 식별자")


ARGUMENTS: dict[str, list[Any]] = {
    "init": [
        _a("--org-id", default="default", help="기본 조직 식별자"),
        _a("--org-name", default="기본 조직", help="기본 조직 이름"),
        _a("--mcp-server", action="append", help="통제할 설정 서버(이름=URL, 반복 가능)"),
        _a("--output", help="설정 조각을 쓸 파일(기본: 표준 출력)"),
        _a("--no-spend-logs", action="store_true",
           help="disable_spend_logs: true를 설정 조각에 포함"),
    ],
    "check": [_CONFIG, _ENV_FILE, _JSON],
    "status": [_CONFIG, _ENV_FILE, _JSON],
    "operator add": [
        _a("name"),
        _a("--role", required=True, choices=[r.value for r in OperatorRole]),
        _a("--user-id", help="LiteLLM 사용자 식별자(admin·approver 필수)"),
        _a("--expires-in-days", type=int),
    ],
    "operator bootstrap": [_a("--name", required=True), _a("--user-id", required=True)],
    "server register": [
        _SERVER_ID,
        _a("--url", help="레지스트리가 URL을 응답하지 않을 때 계약 값에 쓸 서버 URL"),
    ],
    "server suspend": [_SERVER_ID],
    "server resume": [_SERVER_ID],
    "relation add": [
        _a("--org", required=True),
        _a("--server", required=True),
        _a("--purpose", required=True),
        _a("--team"),
    ],
    "relation terminate": [_a("relation", help="이용 관계 식별자")],
    "approve": [
        _a("--relation", required=True),
        _a("--scope", required=True, choices=approvals.SCOPE_TYPES),
        _a("--scope-id", required=True),
        _a("--tools", required=True, type=_csv, help="쉼표로 구분한 도구 이름"),
        _a("--valid-from", type=_when),
        _a("--valid-until", required=True, type=_when),
        _a("--class", dest="approval_class", default="standard",
           choices=["standard", "exception"]),
        _a("--basis", required=True, help="승인 근거"),
        _a("--requested-by", required=True, help="신청자의 LiteLLM 사용자 식별자"),
        _a("--allowlist-ref", action="append", help="근거 허용 목록 항목 식별자(첫 승인에서만)"),
        _SECOND,
    ],
    "deny": [
        _a("--scope", required=True, choices=[*approvals.SCOPE_TYPES, "server"]),
        _a("--scope-id", required=True),
        _a("--server"),
        _a("--tools", type=_csv),
        _a("--reason", required=True),
        _a("--org"),
    ],
    "denial lift": [_a("denial_id")],
    "revoke": [
        _a("approval_id"),
        _a("--reason", required=True),
        _a("--deny", action="store_true", help="같은 범위의 거부를 함께 만든다"),
    ],
    "call-approval request": [
        _a("--org", required=True),
        _a("--principal-type", required=True, choices=list(approvals.PRINCIPAL_TYPES)),
        _a("--principal-id", required=True),
        _a("--server", required=True),
        _a("--tool", required=True),
        _a("--args-fp", required=True, help="차단 사건에 기록된 인자 지문"),
        _a("--fp-key-version", type=int, default=1),
        _a("--policy-version", help="기본: 활성 정책 판본"),
        _a("--expires-in-minutes", type=int, default=30),
        _a("--requested-by", help="기본: 운영자의 사용자 식별자"),
    ],
    "call-approval approve": [_a("call_approval_id"), _SECOND],
    "call-approval reject": [_a("call_approval_id")],
    "call-approval cancel": [_a("call_approval_id")],
    "consent add": [
        _a("--org", required=True),
        _a("--user", required=True, help="동의한 사람의 LiteLLM 사용자 식별자"),
        _a("--server", required=True),
        _a("--purpose", required=True),
        _a("--data-scope", required=True),
        _a("--text-version", required=True),
        _a("--granted-at", type=_when),
        _a("--valid-until", required=True, type=_when),
        _a("--evidence-ref", required=True),
        _a("--evidence-sha256", required=True),
    ],
    "consent revoke": [_a("consent_id")],
    "allowlist add": [
        _a("--kind", required=True, choices=list(allowlists.KINDS)),
        _a("--value", required=True),
        _a("--valid-from", type=_when),
        _a("--valid-until", required=True, type=_when),
        _a("--org"),
        _a("--note"),
    ],
    "allowlist lift": [_a("allowlist_id")],
    "org add": [_a("org_id"), _a("--name", required=True)],
    "org close": [_a("org_id")],
    "org bind": [_a("--org", required=True), "SUBJECT"],
    "org unbind": ["SUBJECT"],
    "settings default-org": ["ORG_OR_CLEAR"],
    "settings mode": [_a("mode", choices=[m.value for m in Mode])],
    "settings observable": [
        _a("--reasons", type=_csv, help="관측으로 전환할 수 있는 사유 계열(쉼표 구분)"),
        _a("--policy-ids", type=_csv, help="관측에 쓸 정책 식별자(쉼표 구분)"),
    ],
    "policy activate": [
        _a("version", help="정책 판본 식별자"),
        _a("--catalog-sha256", required=True),
        _a("--bundle-sha256", required=True),
    ],
    "ledger list": [
        _a("--kind"),
        _a("--since", type=_when),
        _a("--until", type=_when),
        _a("--invocation-id", type=UUID),
        _a("--limit", type=int, default=100),
        _JSON,
    ],
}


def _add_arguments(parser: argparse.ArgumentParser, name: str) -> None:
    for spec in ARGUMENTS.get(name, []):
        if spec == "SUBJECT":
            group = parser.add_mutually_exclusive_group(required=True)
            group.add_argument("--team")
            group.add_argument("--user")
        elif spec == "ORG_OR_CLEAR":
            group = parser.add_mutually_exclusive_group(required=True)
            group.add_argument("--org")
            group.add_argument("--clear", action="store_true", help="기본 조직 해제")
        else:
            flags, kwargs = spec
            parser.add_argument(*flags, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog=PROGRAM, description="MINT 운영자 명령줄 도구")
    root.add_argument("--token-file", help="운영자 토큰 파일(기본: MINT_OPERATOR_TOKEN)")
    tree: dict[tuple[str, ...], argparse._SubParsersAction] = {(): root.add_subparsers(
        dest="group0", metavar="명령", required=True)}
    for command in COMMANDS:
        for depth in range(1, len(command.path)):
            prefix = command.path[:depth]
            if prefix not in tree:
                parent = tree[prefix[:-1]]
                group = parent.add_parser(prefix[-1], help=prefix[-1])
                tree[prefix] = group.add_subparsers(
                    dest=f"group{depth}", metavar="명령", required=True)
        leaf = tree[command.path[:-1]].add_parser(command.path[-1], help=command.summary)
        leaf.set_defaults(command_name=command.name)
        _add_arguments(leaf, command.name)
    return root


COMMAND_INDEX: dict[str, CliCommand] = {c.name: c for c in COMMANDS}


def _token(args: argparse.Namespace, environ: dict[str, str]) -> str | None:
    if args.token_file:
        try:
            return Path(args.token_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise AuthError(f"토큰 파일을 읽을 수 없습니다: {error}") from None
    return environ.get("MINT_OPERATOR_TOKEN")


def run_command(args: argparse.Namespace, environ: dict[str, str]) -> None:
    command = COMMAND_INDEX[args.command_name]
    cfg = ControlConfig.from_env(environ)
    with connect(cfg.require_database()) as conn:
        ctx = None
        if not command.unauthenticated:
            operator = operators.authenticate(conn, cfg.instance_id, _token(args, environ))
            if OperatorRole(operator.role) not in command.roles:
                raise AuthError(f"{command.name}은(는) {operator.role} 역할로 실행할 수 없습니다.")
            ctx = Context(conn, cfg.instance_id, operator)
        HANDLERS[command.name](Run(args, cfg, environ, conn, ctx))


def main(argv: Sequence[str] | None = None, environ: dict[str, str] | None = None) -> int:
    environ = dict(os.environ) if environ is None else environ
    args = build_parser().parse_args(argv)
    try:
        run_command(args, environ)
    except ControlError as error:
        print(str(error), file=sys.stderr)
        return error.exit_code
    except psycopg.Error as error:
        print(f"데이터베이스 오류: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
