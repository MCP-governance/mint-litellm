"""인수시험 시나리오 도구: 운영자 CLI 호출, 시험 주체 만들기, 호출 결과와 원장 사건 조회.

CLI 인자 이름과 출력 형식은 이 파일 한 곳에만 둔다. 구성요소가 병합되면 여기만 맞춘다.
"""

import contextlib
import json
import os
import shlex
import subprocess
import uuid
from dataclasses import dataclass, field

import httpx
import psycopg
import support
from psycopg.rows import dict_row

CLI = shlex.split(os.environ.get("MINT_E_CLI", "mint-litellm"))


@dataclass
class Outcome:
    status: int
    code: str | None
    mark: str
    body: str = ""

    @property
    def allowed(self) -> bool:
        return self.status == 200


@dataclass
class Subject:
    """시험 주체: LiteLLM 사용자(와 선택적으로 팀)에 결속된 키."""

    user_id: str
    key: str
    team_id: str | None = None
    org: str | None = None
    approvals: list[str] = field(default_factory=list)


class Mint:
    """``mint-litellm`` 명령 호출. 출력은 JSON 한 개로 가정한다."""

    def __init__(self, token: str | None = None):
        self.token = token
        self.env = {
            **os.environ,
            "MINT_DATABASE_URL": support.mint_database_url(),
            "MINT_LITELLM_URL": support.url(support.LITELLM_PORTS[0]),
        }

    def run(self, *args: str, token: str | None = None, check: bool = True):
        env = dict(self.env)
        chosen = token or self.token
        if chosen:
            env["MINT_OPERATOR_TOKEN"] = chosen
        done = subprocess.run(
            [*CLI, *args, "--json"],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        if check and done.returncode != 0:
            raise AssertionError(f"{' '.join(args)} failed: {done.stdout}{done.stderr}")
        return done

    def data(self, *args: str, **kw) -> dict:
        return json.loads(self.run(*args, **kw).stdout)


class World:
    def __init__(self, mint: Mint):
        self.mint = mint
        self.relation: dict[tuple[str, str], str] = {}
        self.registered: set[str] = set()
        self.control_key: str | None = None

    def litellm_user(self) -> str:
        user_id = "it-user-" + uuid.uuid4().hex[:8]
        reply = httpx.post(
            support.url(support.LITELLM_PORTS[0], "/user/new"),
            headers=support.master_headers(),
            json={"user_id": user_id, "user_role": "internal_user"},
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
        return Subject(user_id, support.new_key(servers, **extra), team_id)

    def register(self, server: str = "a") -> None:
        if server not in self.registered:
            self.mint.run("server", "register", support.SERVER_ID[server])
            self.registered.add(server)

    def prepare(self, subject: Subject, server: str = "a", org: str | None = None) -> str:
        """조직 생성·바인딩·서버 등록·이용 관계까지 만들고 관계 식별자를 돌려준다."""
        org = org or "org-" + uuid.uuid4().hex[:8]
        self.register(server)
        self.mint.run("org", "add", org)
        target = ("--team", subject.team_id) if subject.team_id else ("--user", subject.user_id)
        self.mint.run("org", "bind", org, *target)
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
        return reply["relation_id"]

    def approve(
        self,
        subject: Subject,
        relation: str,
        *,
        tools: str = "*",
        valid_days: int = 1,
        key_scope: bool = False,
    ) -> str:
        target = (
            ("--key", subject.key)
            if key_scope
            else (("--team", subject.team_id) if subject.team_id else ("--user", subject.user_id))
        )
        reply = self.mint.data(
            "approve",
            "--relation",
            relation,
            *target,
            "--tools",
            tools,
            "--basis",
            "integration-evidence",
            "--valid-days",
            str(valid_days),
        )
        subject.approvals.append(reply["approval_id"])
        return reply["approval_id"]

    def call(
        self,
        subject: Subject,
        server: str = "a",
        tool: str = "echo",
        *,
        port: int | None = None,
        mark: str | None = None,
        headers: dict | None = None,
    ) -> Outcome:
        """REST 경로로 호출한다. 호출 식별자 헤더를 시험 표식과 같게 둬 사건과 대조한다."""
        mark = mark or support.marker()
        reply = support.rest_call(
            subject.key,
            server,
            tool,
            {"text": "x", "marker": mark},
            port=port or support.LITELLM_PORTS[0],
            call_id=mark,
        )
        return Outcome(reply.status_code, _error_code(reply), mark, reply.text)

    def events(self, mark: str, kind: str = "decision_evaluated") -> list[dict]:
        with psycopg.connect(support.mint_database_url(), row_factory=dict_row) as conn:
            return conn.execute(
                "select kind, payload, source_key from mint.events "
                "where kind = %s and payload->>'litellm_call_id' = %s order by inserted_at",
                (kind, mark),
            ).fetchall()

    def now(self):
        with psycopg.connect(support.mint_database_url()) as conn:
            return conn.execute("select clock_timestamp()").fetchone()[0]

    def count_since(self, since, kind: str) -> int:
        with psycopg.connect(support.mint_database_url()) as conn:
            return conn.execute(
                "select count(*) from mint.events where kind = %s and inserted_at >= %s",
                (kind, since),
            ).fetchone()[0]

    def rows_for(self, source_key_prefix: str) -> int:
        with psycopg.connect(support.mint_database_url()) as conn:
            return conn.execute(
                "select count(*) from mint.events where source_key like %s",
                (source_key_prefix + "%",),
            ).fetchone()[0]


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
    )
    assert done.returncode == 0, done.stderr
    try:
        yield
    finally:
        support.compose("up", "-d", "--wait", "--force-recreate", "litellm1", "litellm2")


def _error_code(reply: httpx.Response) -> str | None:
    if reply.status_code == 200:
        return None
    try:
        detail = reply.json().get("detail")
        return json.loads(detail)["code"] if isinstance(detail, str) else detail.get("code")
    except (ValueError, KeyError, AttributeError):
        return None


def decision(event: dict) -> tuple[str, str, str | None]:
    """(evaluated_decision, enforced_action, 차단 사유 코드) 한 사건에서 읽는다."""
    payload = event["payload"]
    stop = payload.get("stop") or {}
    findings = payload.get("findings") or [{}]
    reason = stop.get("reason_code") or findings[0].get("reason_code")
    return payload["evaluated_decision"], payload["enforced_action"], reason
