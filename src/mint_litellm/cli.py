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
from dataclasses import dataclass, field
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
    policy_check,
    policy_versions,
    relations,
    secrets_file,
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
from mint_litellm.types import fingerprint
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
    """명령 한 건의 실행 맥락과 출력. 텍스트와 JSON은 같은 결과에서 만든다."""

    args: argparse.Namespace
    cfg: ControlConfig
    environ: dict[str, str]
    conn: psycopg.Connection
    ctx: Context | None
    emitted: list = field(default_factory=list)
    extra_events: list = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    failure: str | None = None
    failure_code: int = 1

    def context(self) -> Context:
        assert self.ctx is not None
        return self.ctx

    def litellm(self) -> LiteLLMAdmin:
        return self.cfg.litellm()

    def say(self, line: str) -> None:
        self.text.append(line)

    def events(self) -> list:
        return self.ctx.emitted if self.ctx is not None else self.emitted


Handler = Callable[[Run], None]


def _when(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"날짜·시각 형식이 올바르지 않습니다: {text}") from None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


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
    ctx = run.context()
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
        # 처음 만든 설정에서만 기본 조직을 정한다. 이후 재실행은 운영자가 바꾼 값을 건드리지 않는다.
        if not fetchone(
            run.conn,
            "SELECT 1 AS x FROM mint.orgs WHERE instance_id = %s AND org_id = %s",
            (cfg.instance_id, args.org_id),
        ):
            orgs.add(ctx, args.org_id, args.org_name)
        settings_store.set_default_org(ctx, args.org_id)
    text = litellm_config.snippet(servers_arg, no_spend_logs=args.no_spend_logs)
    secrets_path = Path(args.secrets_file or run.environ.get("MINT_SECRETS_FILE")
                        or secrets_file.DEFAULT_PATH).expanduser()
    try:
        key_created = secrets_file.ensure_fingerprint_key(secrets_path)
    except OSError as error:
        raise ControlError(f"비밀 파일을 쓸 수 없습니다({secrets_path}): {error}") from None
    run.data.update(
        migrations=applied,
        login_roles=configured_roles,
        fingerprint_key_file=secrets_path.as_posix(),
        fingerprint_key_created=key_created,
    )
    if args.out == "-":
        run.say(text.rstrip("\n"))
        run.data["snippet"] = text
    else:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        run.data["snippet_path"] = path.as_posix()
    log = sys.stderr
    print(f"마이그레이션 적용: {', '.join(applied) or '없음(이미 최신)'}", file=log)
    if args.out != "-":
        print(f"LiteLLM 설정 조각: {args.out}", file=log)
    print(
        f"인자 지문 키: {secrets_path}" + (" (새로 생성)" if key_created else " (기존 키 유지)"),
        file=log,
    )
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
        policy_state=policy_check.verify_active_policy(
            run.conn, ctx.instance_id, run.environ.get("MINT_OPA_URL")
        ),
    )
    run.data.update(
        findings=[
            {"item": f.item, "level": f.level.name, "message": f.message} for f in report.findings
        ],
        observations=report.observations,
    )
    run.say(report.render())
    run.data["incomplete"] = report.incomplete
    if report.failed:
        run.failure = "필수 설정 점검에 실패했습니다."
    elif report.incomplete:
        run.failure = "필수 검증을 모두 수행하지 못했습니다: " + ", ".join(report.incomplete)
        run.failure_code = 3


def _status(run: Run) -> None:
    info = status.collect(
        run.context(),
        run.cfg,
        _optional_litellm(run),
        litellm_config.load_config(_config_path(run)),
        _gather_env(run),
        policy_check.verify_active_policy(
            run.conn, run.context().instance_id, run.environ.get("MINT_OPA_URL")
        ),
    )
    run.data.update(info)
    run.say(status.render(info))


def _issued(run: Run, token: str) -> None:
    run.data["token"] = token
    run.say(f"발급된 토큰(다시 표시되지 않습니다): {token}")


def _operator_add(run: Run) -> None:
    a = run.args
    token = operators.add(
        run.context(), run.litellm(), a.name, OperatorRole(a.role), a.user_id, a.expires_in_days
    )
    _issued(run, token)


def _operator_bootstrap(run: Run) -> None:
    token = operators.bootstrap(
        run.conn, run.cfg.instance_id, run.litellm(), run.args.name, run.args.user_id, run.emitted
    )
    _issued(run, token)


def _second(run: Run) -> Operator | None:
    value = getattr(run.args, "second_approver", None)
    if not value:
        return None
    if value.startswith("@"):
        value = Path(value[1:]).read_text(encoding="utf-8").strip()
    return operators.authenticate(run.conn, run.cfg.instance_id, value)


def _created(run: Run, object_id: str) -> None:
    run.data["id"] = object_id
    run.say(object_id)


def _approve(run: Run) -> None:
    a = run.args
    approval_id, _ = approvals.approve(
        run.context(),
        run.litellm(),
        relation_id=a.relation,
        scope_type=a.scope,
        scope_id=a.scope_id,
        tools=a.tools,
        valid_from=a.valid_from or datetime.now(UTC),
        valid_until=a.valid_until,
        approval_class=a.approval_class,
        basis_text=a.basis,
        requested_by=a.requested_by,
        allowlist_ids=a.allowlist_ref or [],
        second=_second(run),
        conditions=approvals.build_conditions(
            high_risk_tools=a.high_risk_tools,
            business_hours_only=a.business_hours_only,
            business_hours=a.business_hours,
            require_consent=a.require_consent,
        ),
    )
    _created(run, approval_id)


def _subject(a: argparse.Namespace) -> tuple[SubjectType, str]:
    return (SubjectType.TEAM, a.team) if a.team else (SubjectType.USER, a.user)


def _decision_ref(text: str) -> tuple[UUID, int]:
    """오류 본문의 ``reference``(``{invocation_id}:{attempt_id}``)를 나눈다."""
    invocation, sep, attempt = text.rpartition(":")
    try:
        parsed = (UUID(invocation), int(attempt))
    except ValueError:
        parsed = None
    if not sep or parsed is None or parsed[1] < 1:
        raise ControlError("--decision-ref는 <호출 식별자>:<시도 번호> 형식이어야 합니다.")
    return parsed


def _ledger_list(run: Run) -> None:
    a = run.args
    invocation_id, attempt_id = a.invocation_id, None
    if a.decision_ref:
        if a.invocation_id:
            raise ControlError("--decision-ref와 --invocation-id는 함께 쓸 수 없습니다.")
        invocation_id, attempt_id = _decision_ref(a.decision_ref)
    rows = ledger.list_events(
        run.conn,
        run.cfg.instance_id,
        kind=a.kind,
        since=a.since,
        until=a.until,
        invocation_id=invocation_id,
        attempt_id=attempt_id,
        limit=a.limit,
    )
    run.data["events"] = [json.loads(json.dumps(row, default=str)) for row in rows]
    for row in rows:
        run.say(
            f"{row['inserted_at'].isoformat()}\t{row['kind']}\t{row['actor'] or '-'}\t"
            f"{row['source_key']}"
        )


def _policy_activate(run: Run) -> None:
    a, ctx = run.args, run.context()
    source = policy_versions.PolicySource(
        root=Path(a.policy_root),
        opa=a.opa,
        bundle_path=Path(a.bundle_path),
        opa_url=a.opa_url or run.environ.get("MINT_OPA_URL", ""),
        timeout_seconds=a.timeout,
    )
    if not source.opa_url:
        raise ControlError("--opa-url 또는 MINT_OPA_URL이 필요합니다.")
    policy_versions.activate(
        run.conn, run.cfg.require_database(), ctx.instance_id, ctx.operator.name, a.version, source
    )
    run.conn.commit()
    row = fetchone(
        run.conn,
        "SELECT event_id, kind, source_key FROM mint.events WHERE instance_id = %s"
        " AND kind = 'policy_activated' AND source_key LIKE %s ORDER BY inserted_at DESC LIMIT 1",
        (ctx.instance_id, f"policy_version:{a.version}:%"),
    )
    if row:
        run.extra_events.append(row)
    run.data["id"] = a.version


def _server_register(run: Run) -> None:
    servers.register(
        run.context(),
        run.litellm(),
        run.args.server_id,
        run.args.url,
        litellm_config.load_config(_config_path(run)),
    )


def _done(call: Callable[..., Any], *names: str) -> Handler:
    def handler(run: Run) -> None:
        call(run.context(), *[getattr(run.args, n) for n in names])

    return handler


def _denial(run: Run) -> None:
    a = run.args
    _created(
        run,
        approvals.create_denial(
            run.context(),
            scope_type=a.scope,
            scope_id=a.scope_id,
            server_id=a.server,
            tools=a.tools,
            reason=a.reason,
            org_id=a.org,
        ),
    )


def _fingerprint(run: Run) -> tuple[str, int]:
    """``--args-fp``는 그대로, 인자 원문은 플러그인과 같은 함수로 지문을 계산한다."""
    a = run.args
    if a.args_fp:
        return a.args_fp, a.fp_key_version or 1
    text = a.arguments if a.arguments is not None else Path(a.args_file).read_text("utf-8")
    try:
        arguments = json.loads(text)
        key_text, version = secrets_file.load_fingerprint_key(run.environ)
        key = fingerprint.decode_fingerprint_key(key_text)
        return fingerprint.argument_fingerprint(arguments, key), a.fp_key_version or version
    except (ValueError, OSError) as error:
        raise ControlError(f"인자 지문을 계산할 수 없습니다: {error}") from None


def _call_request(run: Run) -> None:
    a, ctx = run.args, run.context()
    args_fp, fp_key_version = _fingerprint(run)
    _created(
        run,
        approvals.call_request(
            ctx,
            org_id=a.org,
            principal_type=a.principal_type,
            principal_id=a.principal_id,
            server_id=a.server,
            tool=a.tool,
            args_fp=args_fp,
            fp_key_version=fp_key_version,
            policy_version_id=a.policy_version,
            expires_in_minutes=a.expires_in_minutes,
            requested_by=a.requested_by or ctx.operator.user_id or ctx.operator.name,
        ),
    )


def _consent_add(run: Run) -> None:
    a = run.args
    _created(
        run,
        consents.add(
            run.context(),
            run.litellm(),
            a.org,
            a.user,
            a.server,
            a.purpose,
            a.data_scope,
            a.text_version,
            a.granted_at or datetime.now(UTC),
            a.valid_until,
            a.evidence_ref,
            a.evidence_sha256,
        ),
    )


def _allowlist_add(run: Run) -> None:
    a = run.args
    _created(
        run,
        allowlists.add(
            run.context(),
            a.kind,
            a.value,
            a.valid_from or datetime.now(UTC),
            a.valid_until,
            a.org,
            a.note,
        ),
    )


HANDLERS: dict[str, Handler] = {
    "init": _init,
    "check": _check,
    "status": _status,
    "operator add": _operator_add,
    "operator bootstrap": _operator_bootstrap,
    "server register": _server_register,
    "server suspend": _done(servers.suspend, "server_id"),
    "server resume": _done(servers.resume, "server_id"),
    "relation add": lambda r: _created(
        r, relations.add(r.context(), r.args.org, r.args.server, r.args.purpose, r.args.team)
    ),
    "relation terminate": _done(relations.terminate, "relation"),
    "approve": _approve,
    "deny": _denial,
    "denial lift": _done(approvals.lift_denial, "denial_id"),
    "revoke": lambda r: approvals.revoke(
        r.context(), r.args.approval_id, r.args.reason, r.args.deny
    ),
    "call-approval request": _call_request,
    "call-approval approve": lambda r: approvals.call_approve(
        r.context(), r.args.call_approval_id, _second(r)
    ),
    "call-approval reject": _done(approvals.call_reject, "call_approval_id"),
    "call-approval cancel": _done(approvals.call_cancel, "call_approval_id"),
    "consent add": _consent_add,
    "consent revoke": _done(consents.revoke, "consent_id"),
    "allowlist add": _allowlist_add,
    "allowlist lift": _done(allowlists.lift, "allowlist_id"),
    "org add": lambda r: orgs.add(r.context(), r.args.org_id, r.args.name),
    "org close": _done(orgs.close, "org_id"),
    "org bind": lambda r: orgs.bind(r.context(), r.args.org, *_subject(r.args)),
    "org unbind": lambda r: orgs.unbind(r.context(), *_subject(r.args)),
    "settings default-org": lambda r: settings_store.set_default_org(
        r.context(), None if r.args.clear else r.args.org
    ),
    "settings mode": lambda r: settings_store.set_mode(
        r.context(), Mode(r.args.mode), r.cfg.mode_ceiling
    ),
    "settings observable": lambda r: settings_store.set_observable(
        r.context(), r.args.reasons, r.args.policy_ids
    ),
    "policy activate": _policy_activate,
    "ledger list": _ledger_list,
}


# ---------------------------------------------------------------------------
# 인자 정의
# ---------------------------------------------------------------------------


def _a(*flags: str, **kwargs: Any) -> tuple[tuple[str, ...], dict[str, Any]]:
    return flags, kwargs


_CONFIG = _a("--config", help="LiteLLM config.yaml 경로(기본: MINT_LITELLM_CONFIG)")
_ENV_FILE = _a("--env-file", help="LiteLLM 환경 파일(KEY=VALUE)")
_SECOND = _a("--second-approver", help="요청자와 다른 사용자의 운영자 토큰(또는 @파일)")
_SERVER_ID = _a("server_id", help="LiteLLM 서버 식별자")


ARGUMENTS: dict[str, list[Any]] = {
    "init": [
        _a("--org-id", default="default", help="기본 조직 식별자"),
        _a("--org-name", default="기본 조직", help="기본 조직 이름"),
        _a("--mcp-server", action="append", help="통제할 설정 서버(이름=URL, 반복 가능)"),
        _a("--out", default="config/litellm.mint.yaml",
           help="설정 조각을 쓸 파일(기본: config/litellm.mint.yaml, -는 표준 출력)"),
        _a("--secrets-file",
           help="인자 지문 키를 둘 저장소 밖 비밀 파일"
                "(기본: MINT_SECRETS_FILE 또는 ~/.config/mint/secrets.env)"),
        _a("--no-spend-logs", action="store_true",
           help="disable_spend_logs: true를 설정 조각에 포함"),
    ],
    "check": [_CONFIG, _ENV_FILE],
    "status": [_CONFIG, _ENV_FILE],
    "operator add": [
        _a("name"),
        _a("--role", required=True, choices=[r.value for r in OperatorRole]),
        _a("--user-id", help="LiteLLM 사용자 식별자(admin·approver 필수)"),
        _a("--expires-in-days", type=int),
    ],
    "operator bootstrap": [_a("--name", required=True), _a("--user-id", required=True)],
    "server register": [
        _SERVER_ID,
        _a("--url", help="계약 값에 쓸 서버 URL(제어 평면 키는 레지스트리 URL을 읽을 수 없다)"),
        _CONFIG,
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
        _a("--high-risk-tools", type=_csv, help="이 승인에서 호출별 승인을 요구할 도구(쉼표 구분)"),
        _a("--business-hours-only", action="store_true", help="업무 시간 밖의 호출을 차단"),
        _a("--business-hours",
           help="업무 시간 재정의: 시간대,요일,시작,끝 (예: Asia/Seoul,1-5,09:00,18:00)"),
        _a("--require-consent", help="이 목적의 동의가 있어야 호출 허용"),
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
        "FINGERPRINT",
        _a("--fp-key-version", type=int,
           help="지문 키 판본(기본: MINT_FINGERPRINT_KEY_VERSION 또는 1)"),
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
        _a("--policy-root", default=os.environ.get("MINT_POLICY_ROOT", "/opt/mint"),
           help="policy/ 디렉터리를 담은 루트(기본: MINT_POLICY_ROOT 또는 /opt/mint)"),
        _a("--opa", default="opa", help="정책 시험에 쓸 opa 실행 파일"),
        _a("--bundle-path",
           default=os.environ.get("MINT_OPA_BUNDLE_PATH", "/bundles/mint.tar.gz"),
           help="정책 엔진이 감시하는 번들 파일(공유 볼륨)"),
        _a("--opa-url", help="정책 엔진 주소(기본: MINT_OPA_URL)"),
        _a("--timeout", type=float, default=30.0, help="엔진 적재 확인 제한 시간(초)"),
    ],
    "ledger list": [
        _a("--kind"),
        _a("--since", type=_when),
        _a("--until", type=_when),
        _a("--invocation-id", type=UUID),
        _a("--decision-ref",
           help="오류 본문의 reference(<호출 식별자>:<시도 번호>)로 판정 사건을 찾는다"),
        _a("--limit", type=int, default=100),
    ],
}


def _add_arguments(parser: argparse.ArgumentParser, name: str) -> None:
    for spec in ARGUMENTS.get(name, []):
        if spec == "SUBJECT":
            group = parser.add_mutually_exclusive_group(required=True)
            group.add_argument("--team")
            group.add_argument("--user")
        elif spec == "FINGERPRINT":
            group = parser.add_mutually_exclusive_group(required=True)
            group.add_argument("--args-fp", help="차단 사건에 기록된 인자 지문(소문자 16진 64자)")
            group.add_argument("--arguments", help="호출 인자(JSON 객체), 지문 키로 지문을 계산")
            group.add_argument("--args-file", help="호출 인자를 담은 JSON 파일")
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
    root.add_argument("--json", action="store_true", help="결과를 JSON 한 개로 출력")
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
        leaf.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                          help="결과를 JSON 한 개로 출력")
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


def _event_entry(event: Any) -> dict[str, Any]:
    """사건(객체 또는 행)을 JSON 출력용으로 줄인다. 식별자와 상태 버전은 source_key에서 읽는다."""
    get = event.get if isinstance(event, dict) else lambda key: getattr(event, key)
    key = str(get("source_key"))
    entity, entity_id, version, _ = key.split(":", 3)
    return {
        "event_id": str(get("event_id")),
        "kind": str(getattr(get("kind"), "value", get("kind"))),
        "source_key": key,
        "entity": entity,
        "entity_id": entity_id,
        "state_version": int(version),
    }


def _result(run: Run, command: CliCommand) -> dict[str, Any]:
    entries = [_event_entry(e) for e in [*run.events(), *run.extra_events]]
    result: dict[str, Any] = {"ok": run.failure is None, "command": command.name}
    result.update(run.data)
    if entries:
        result.setdefault("id", entries[0]["entity_id"])
        result["state_version"] = entries[0]["state_version"]
        result["events"] = entries
    return result


def run_command(args: argparse.Namespace, environ: dict[str, str]) -> None:
    command = COMMAND_INDEX[args.command_name]
    cfg = ControlConfig.from_env(environ)
    with connect(cfg.require_database()) as conn:
        ctx = None
        if command.name == "init":
            ctx = Context(conn, cfg.instance_id, Operator("init", "admin", None))
        elif not command.unauthenticated:
            operator = operators.authenticate(conn, cfg.instance_id, _token(args, environ))
            if OperatorRole(operator.role) not in command.roles:
                raise AuthError(f"{command.name}은(는) {operator.role} 역할로 실행할 수 없습니다.")
            ctx = Context(conn, cfg.instance_id, operator)
        run = Run(args, cfg, environ, conn, ctx)
        HANDLERS[command.name](run)
    if args.json:
        print(json.dumps(_result(run, command), ensure_ascii=False, default=str))
    else:
        for line in run.text:
            print(line)
    if run.failure:
        raise ControlError(run.failure, run.failure_code)


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
