"""인수시험 시나리오 도구: 운영자 CLI 호출, 시험 주체 만들기, 호출 결과와 원장 사건 조회.

CLI는 스택의 ``control`` 컨테이너 안에서 실행한다(정책 번들 볼륨과 데이터베이스 역할이 배포와 같다).
"""

import contextlib
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import support
from psycopg.rows import dict_row

sys.path.insert(0, str(support.HERE.parents[1] / "src"))

from mint_litellm.types.fingerprint import argument_fingerprint, decode_fingerprint_key  # noqa: E402

FINGERPRINT_KEY = "BwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyAhIiMkJSY"


def iso(delta: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) + delta).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def fingerprint(arguments: dict) -> str:
    return argument_fingerprint(arguments, decode_fingerprint_key(FINGERPRINT_KEY))


@dataclass
class Outcome:
    status: int
    code: str | None
    mark: str
    body: str = ""
    reference: str | None = None

    @property
    def allowed(self) -> bool:
        return self.status == 200


@dataclass
class Subject:
    """시험 주체: LiteLLM 사용자(와 선택적으로 팀)에 결속된 키."""

    user_id: str
    key: str
    key_id: str
    team_id: str | None = None
    org: str | None = None
    approvals: list[str] = field(default_factory=list)


class Mint:
    """``control`` 컨테이너에서 ``mint-litellm``을 실행한다. 출력은 ``--json`` 한 개."""

    def __init__(self, token: str | None = None):
        self.token = token
        self.env: dict[str, str] = {}

    def run(
        self, *args: str, token: str | None = None, check: bool = True, env: dict | None = None
    ):
        variables = {**self.env, **(env or {})}
        chosen = token or self.token
        if chosen:
            variables["MINT_OPERATOR_TOKEN"] = chosen
        flags = [x for k, v in variables.items() for x in ("-e", f"{k}={v}")]
        done = support.compose(
            "exec", "-T", *flags, "control", "mint-litellm", *args, "--json", check=False
        )
        if check and done.returncode != 0:
            raise AssertionError(f"{' '.join(args)} failed: {done.stdout}{done.stderr}")
        return done

    def data(self, *args: str, **kw) -> dict:
        return json.loads(self.run(*args, **kw).stdout)


class World:
    def __init__(self, mint: Mint):
        self.mint = mint
        self.registered: set[str] = set()
        self.control_key: str | None = None

    def litellm_user(self, role: str = "internal_user") -> str:
        user_id = "it-user-" + uuid.uuid4().hex[:8]
        reply = httpx.post(
            support.url(support.LITELLM_PORTS[0], "/user/new"),
            headers=support.master_headers(),
            json={"user_id": user_id, "user_role": role},
            timeout=30,
        )
        reply.raise_for_status()
        return user_id

    def subject(self, servers=("a",), *, team: bool = False, **key_extra) -> Subject:
        user_id = self.litellm_user()
        extra = {"user_id": user_id, **key_extra}
        team_id = None
        if team:
            reply = httpx.post(
                support.url(support.LITELLM_PORTS[0], "/team/new"),
                headers=support.master_headers(),
                json={"team_alias": "it-team-" + uuid.uuid4().hex[:8]},
                timeout=30,
            )
            reply.raise_for_status()
            team_id = reply.json()["team_id"]
            extra["team_id"] = team_id
        key = support.new_key(servers, **extra)
        return Subject(user_id, key, hashlib.sha256(key.encode()).hexdigest(), team_id)

    def register(self, server: str = "a") -> None:
        if server not in self.registered:
            self.mint.run("server", "register", support.SERVER_ID[server])
            self.registered.add(server)

    def prepare(self, subject: Subject, server: str = "a", org: str | None = None) -> str:
        """조직 생성·바인딩·서버 등록·이용 관계까지 만들고 관계 식별자를 돌려준다."""
        org = org or "org-" + uuid.uuid4().hex[:8]
        self.register(server)
        self.mint.run("org", "add", org, "--name", org)
        target = ("--team", subject.team_id) if subject.team_id else ("--user", subject.user_id)
        self.mint.run("org", "bind", "--org", org, *target)
        subject.org = org
        reply = self.mint.data(
            "relation",
            "add",
            "--org",
            org,
            "--server",
            support.SERVER_ID[server],
            "--purpose",
            "it",
        )
        return reply["id"]

    def approve(
        self,
        subject: Subject,
        relation: str,
        *,
        tools: str = "echo,slow,mutable",
        valid: timedelta = timedelta(days=1),
        valid_from: timedelta | None = None,
        scope: str | None = None,
        extra: tuple[str, ...] = (),
    ) -> str:
        scope = scope or ("team" if subject.team_id else "key")
        scope_id = {"key": subject.key_id, "team": subject.team_id, "user": subject.user_id}[scope]
        args = [
            "approve",
            "--relation",
            relation,
            "--scope",
            scope,
            "--scope-id",
            scope_id,
            "--tools",
            tools,
            "--valid-until",
            iso(valid),
            "--basis",
            "integration-evidence",
            "--requested-by",
            subject.user_id,
            *extra,
        ]
        if valid_from is not None:
            args += ["--valid-from", iso(valid_from)]
        reply = self.mint.data(*args)
        subject.approvals.append(reply["id"])
        return reply["id"]

    def call(
        self,
        subject: Subject,
        server: str = "a",
        tool: str = "echo",
        *,
        port: int | None = None,
        mark: str | None = None,
        arguments: dict | None = None,
        key: str | None = None,
    ) -> Outcome:
        """REST 경로로 호출한다. 호출 식별자 헤더를 시험 표식과 같게 둬 사건과 대조한다."""
        mark = mark or support.marker()
        reply = support.rest_call(
            key or subject.key,
            server,
            tool,
            arguments if arguments is not None else {"text": "x", "marker": mark},
            port=port or support.LITELLM_PORTS[0],
            call_id=mark,
        )
        return Outcome(reply.status_code, *_error(reply), mark, reply.text)

    def events(self, mark: str, kind: str = "decision_evaluated") -> list[dict]:
        with psycopg.connect(support.mint_database_url(), row_factory=dict_row) as conn:
            return conn.execute(
                "select kind, payload, source_key, invocation_id, attempt_id from mint.events "
                "where kind = %s and payload->>'litellm_call_id' = %s order by inserted_at",
                (kind, mark),
            ).fetchall()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with psycopg.connect(support.mint_database_url(), row_factory=dict_row) as conn:
            return conn.execute(sql, params).fetchall()

    def now(self):
        return self.query("select clock_timestamp() as t")[0]["t"]

    def count_since(self, since, kind: str) -> int:
        rows = self.query(
            "select count(*) as n from mint.events where kind = %s and inserted_at >= %s",
            (kind, since),
        )
        return rows[0]["n"]

    def rows_for(self, source_key_prefix: str) -> int:
        rows = self.query(
            "select count(*) as n from mint.events where source_key like %s",
            (source_key_prefix + "%",),
        )
        return rows[0]["n"]


@contextlib.contextmanager
def litellm_config(path: str):
    """두 복제본을 다른 설정 파일로 다시 띄웠다가 기본 설정으로 되돌린다."""
    done = support.compose(
        "up",
        "-d",
        "--wait",
        "--force-recreate",
        "litellm1",
        "litellm2",
        env={"MINT_E_LITELLM_CONFIG": path},
        check=False,
    )
    try:
        yield done
    finally:
        support.compose("up", "-d", "--wait", "--force-recreate", "litellm1", "litellm2")


def _error(reply: httpx.Response) -> tuple[str | None, str | None]:
    """(차단 오류 코드, 판정 참조). 허용이면 (None, None)."""
    if reply.status_code == 200:
        return None, None
    try:
        detail = reply.json().get("detail")
        parsed = json.loads(detail) if isinstance(detail, str) else detail
        return parsed.get("code"), parsed.get("reference")
    except (ValueError, KeyError, AttributeError):
        return None, None


def decision(event: dict) -> tuple[str, str, str | None]:
    """(evaluated_decision, enforced_action, 차단 사유 코드) 한 사건에서 읽는다."""
    payload = event["payload"]
    stop = payload.get("stop") or {}
    findings = payload.get("findings") or [{}]
    reason = stop.get("reason_code") or findings[0].get("reason_code")
    return payload["evaluated_decision"], payload["enforced_action"], reason


def project_root() -> Path:
    return support.HERE.parents[1]
