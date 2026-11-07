"""운영자 CLI의 명령 표와 제어 평면의 LiteLLM 접근 범위.

모든 명령은 운영자 토큰으로 인증되며 행위자는 토큰에 결속된 운영자 이름이다. 제어 평면은
LiteLLM에 쓰지 않는다. 확인에 필요한 값은 관리 API로 읽는다.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from mint_litellm.types.events import EventKind


class OperatorRole(StrEnum):
    ADMIN = "admin"
    APPROVER = "approver"
    VIEWER = "viewer"


# 제어 평면 LiteLLM 키(proxy_admin)에 허용하는 경로 최소 목록. 정확 일치 또는 ``경로/`` 접두사.
CONTROL_PLANE_ALLOWED_ROUTES: tuple[str, ...] = (
    "/v1/mcp/server",
    "/key/info",
    "/team/info",
    "/user/info",
    "/key/list",
    "/team/list",
    "/guardrails/list",
)

# 관리 API 응답에서 수신 즉시 버리고 저장·기록·표시하지 않는 자격 열.
LITELLM_CREDENTIAL_FIELDS: tuple[str, ...] = (
    "credentials",
    "static_headers",
    "env_vars",
    "credential_b64",
    "mcp_user_credentials",
)

ALL_ROLES = frozenset(OperatorRole)
APPROVER_UP = frozenset({OperatorRole.ADMIN, OperatorRole.APPROVER})
ADMIN_ONLY = frozenset({OperatorRole.ADMIN})


class CliCommand(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: tuple[str, ...]
    summary: str
    roles: frozenset[OperatorRole]
    # 토큰 없이 실행되는 명령(최초 설정)
    unauthenticated: bool = False
    litellm_reads: tuple[str, ...] = ()
    events: tuple[EventKind, ...] = ()

    @property
    def name(self) -> str:
        return " ".join(self.path)


_SERVER_GET = "/v1/mcp/server/{server_id}"

COMMANDS: tuple[CliCommand, ...] = (
    CliCommand(path=("contract", "show"), summary="승인·후보 계약을 조회한다.", roles=ALL_ROLES),
    CliCommand(path=("contract", "diff"), summary="승인본과 관측본을 비교한다.", roles=ALL_ROLES),
    CliCommand(
        path=("contract", "reapprove"),
        summary="재검사 증거를 확인하고 재승인 계획과 동기화 작업을 등록한다.",
        roles=APPROVER_UP,
        events=(EventKind.REAPPROVAL_REQUESTED,),
    ),
    CliCommand(
        path=("init",),
        summary="LiteLLM 설정 조각(권장 설정 포함)을 만들고 기본 조직을 준비한다. "
        "`--no-spend-logs`를 주면 `disable_spend_logs: true`를 포함한다.",
        roles=frozenset(),
        unauthenticated=True,
    ),
    CliCommand(
        path=("check",),
        summary="필수 설정 위반이 있으면 종료 코드 0이 아닌 값으로 끝나고, 권장 설정은 경고하며, "
        "관측 항목 4가지를 출력한다.",
        roles=ALL_ROLES,
        litellm_reads=("/v1/mcp/server", "/key/list", "/team/list", "/guardrails/list"),
    ),
    CliCommand(
        path=("status",),
        summary="모드, 정책 판본, 경보, 관측 항목을 표시한다.",
        roles=ALL_ROLES,
        litellm_reads=("/v1/mcp/server", "/key/list", "/team/list"),
    ),
    CliCommand(
        path=("operator", "add"),
        summary="운영자를 등록하고 토큰을 발급한다. `admin`·`approver`는 LiteLLM 사용자 식별자 "
        "(`--user-id`) 결속이 필수이다.",
        roles=ADMIN_ONLY,
        litellm_reads=("/user/info",),
        events=(EventKind.OPERATOR_ADDED,),
    ),
    CliCommand(
        path=("operator", "bootstrap"),
        summary="`operators` 표가 비어 있을 때만 토큰 없이 첫 `admin` 운영자를 만들고 토큰을 "
        "발급한다. `--name`과 `--user-id`가 필요하며, 사용자가 LiteLLM의 사람 사용자로 "
        "존재하는지 확인한다. 행위자는 `bootstrap`으로 기록하고, 운영자가 이미 있으면 "
        "거부한다.",
        roles=frozenset(),
        unauthenticated=True,
        litellm_reads=("/user/info",),
        events=(EventKind.OPERATOR_ADDED,),
    ),
    CliCommand(
        path=("server", "register"),
        summary="LiteLLM에 등록된 서버를 승인 대기 상태로 등록한다.",
        roles=ADMIN_ONLY,
        litellm_reads=(_SERVER_GET,),
        events=(EventKind.SERVER_REGISTERED,),
    ),
    CliCommand(
        path=("server", "suspend"),
        summary="서버를 중지한다. 중지된 서버의 호출은 차단된다.",
        roles=ADMIN_ONLY,
        events=(EventKind.SERVER_SUSPENDED,),
    ),
    CliCommand(
        path=("server", "resume"),
        summary="중지한 서버를 다시 승인 상태로 되돌린다.",
        roles=ADMIN_ONLY,
        events=(EventKind.SERVER_RESUMED,),
    ),
    CliCommand(
        path=("relation", "add"),
        summary="조직·팀·목적·서버를 묶는 이용 관계를 만든다.",
        roles=ADMIN_ONLY,
        events=(EventKind.RELATION_CREATED,),
    ),
    CliCommand(
        path=("relation", "terminate"),
        summary="이용 관계를 종료 중으로 바꾼다. 그 관계에 속한 승인과 매칭되는 호출은 "
        "`RELATION_TERMINATED`로 차단된다.",
        roles=ADMIN_ONLY,
        events=(EventKind.RELATION_TERMINATING,),
    ),
    CliCommand(
        path=("approve",),
        summary="이용 관계에 속한 승인을 등록한다. 근거와 유효 기간이 필수이며, 대상 권한이 "
        "LiteLLM에 서버 식별자 형식으로 있는지 읽어 확인한다.",
        roles=APPROVER_UP,
        litellm_reads=("/key/info", "/team/info"),
        events=(EventKind.APPROVAL_GRANTED, EventKind.SERVER_APPROVED),
    ),
    CliCommand(
        path=("deny",),
        summary="주체 또는 서버에 대한 거부를 만든다. 거부는 모든 승인보다 우선한다.",
        roles=APPROVER_UP,
        events=(EventKind.DENIAL_CREATED,),
    ),
    CliCommand(
        path=("denial", "lift"),
        summary="거부를 해제한다.",
        roles=APPROVER_UP,
        events=(EventKind.DENIAL_LIFTED,),
    ),
    CliCommand(
        path=("revoke",),
        summary="승인을 철회한다. `--deny`를 주면 같은 범위의 거부를 함께 만든다.",
        roles=APPROVER_UP,
        events=(EventKind.APPROVAL_REVOKED, EventKind.DENIAL_CREATED),
    ),
    CliCommand(
        path=("call-approval", "request"),
        summary="특정 주체·서버·도구·인자에 대한 호출별 승인을 요청한다.",
        roles=APPROVER_UP,
        events=(EventKind.CALL_APPROVAL_REQUESTED,),
    ),
    CliCommand(
        path=("call-approval", "approve"),
        summary="호출별 승인을 승인한다. 호출 주체와 같은 사용자의 운영자는 승인할 수 없다.",
        roles=APPROVER_UP,
        events=(EventKind.CALL_APPROVAL_APPROVED,),
    ),
    CliCommand(
        path=("call-approval", "reject"),
        summary="호출별 승인 요청을 거절한다.",
        roles=APPROVER_UP,
        events=(EventKind.CALL_APPROVAL_REJECTED,),
    ),
    CliCommand(
        path=("call-approval", "cancel"),
        summary="요청 중이거나 승인된 호출별 승인을 취소한다.",
        roles=APPROVER_UP,
        events=(EventKind.CALL_APPROVAL_CANCELLED,),
    ),
    CliCommand(
        path=("consent", "add"),
        summary="사용자 동의를 등록한다. 운영자 증명은 증거 참조와 증거 해시가 필수이다.",
        roles=ADMIN_ONLY,
        events=(EventKind.CONSENT_GRANTED,),
    ),
    CliCommand(
        path=("consent", "revoke"),
        summary="등록된 동의를 철회한다.",
        roles=ADMIN_ONLY,
        events=(EventKind.CONSENT_REVOKED,),
    ),
    CliCommand(
        path=("allowlist", "add"),
        summary="허용 목록 항목을 등록한다. 종료일이 필수이다.",
        roles=ADMIN_ONLY,
        events=(EventKind.ALLOWLIST_ADDED,),
    ),
    CliCommand(
        path=("allowlist", "lift"),
        summary="허용 목록 항목을 해제한다.",
        roles=ADMIN_ONLY,
        events=(EventKind.ALLOWLIST_LIFTED,),
    ),
    CliCommand(
        path=("org", "add"),
        summary="조직을 만든다.",
        roles=ADMIN_ONLY,
        events=(EventKind.ORG_CREATED,),
    ),
    CliCommand(
        path=("org", "close"),
        summary="조직을 닫는다. 닫힌 조직에 속한 주체의 호출은 차단된다.",
        roles=ADMIN_ONLY,
        events=(EventKind.ORG_CLOSED,),
    ),
    CliCommand(
        path=("org", "bind"),
        summary="팀 또는 사용자를 조직에 묶는다. 한 팀·한 사용자는 한 조직에만 묶인다.",
        roles=ADMIN_ONLY,
        events=(EventKind.ORG_BINDING_CREATED,),
    ),
    CliCommand(
        path=("org", "unbind"),
        summary="팀 또는 사용자의 조직 바인딩을 제거한다.",
        roles=ADMIN_ONLY,
        events=(EventKind.ORG_BINDING_REMOVED,),
    ),
    CliCommand(
        path=("settings", "default-org"),
        summary="바인딩이 없는 주체에 적용할 기본 조직을 지정하거나 해제한다.",
        roles=ADMIN_ONLY,
        events=(EventKind.SETTINGS_CHANGED,),
    ),
    CliCommand(
        path=("settings", "mode"),
        summary="집행 모드를 enforce 또는 observe로 정한다. 환경 변수의 상한이 enforce이면 "
        "observe로 바꿀 수 없다.",
        roles=ADMIN_ONLY,
        events=(EventKind.SETTINGS_CHANGED,),
    ),
    CliCommand(
        path=("settings", "observable"),
        summary="관측 모드로 전환할 수 있는 사유 계열과 정책을 정한다.",
        roles=ADMIN_ONLY,
        events=(EventKind.SETTINGS_CHANGED,),
    ),
    CliCommand(
        path=("policy", "activate"),
        summary="정책 번들을 검증하고 정책 엔진에 적재한 뒤 활성 판본으로 전환한다.",
        roles=ADMIN_ONLY,
        events=(EventKind.POLICY_ACTIVATED,),
    ),
    CliCommand(
        path=("ledger", "list"),
        summary="원장 사건을 종류·기간·호출 식별자로 조회한다.",
        roles=ALL_ROLES,
    ),
)
