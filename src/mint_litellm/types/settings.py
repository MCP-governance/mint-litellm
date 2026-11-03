"""설정 우선순위와 LiteLLM 필수·권장·관측 설정 표.

``SETTINGS_TABLE``은 ``init``의 설정 조각 생성, ``check``의 검사, 플러그인 자가 점검이
공유하는 단일 기준이다. 등급이 ``REQUIRED``인 행은 자가 점검 실패(기동 중단)이자 ``check``
실패이고, ``RECOMMENDED``는 ``check`` 경고, ``OBSERVATION``은 ``status`` 표시만 한다.
"""

from enum import IntEnum, StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

from mint_litellm.types.reason_codes import OBSERVATION_CANDIDATES, Reason


class Mode(StrEnum):
    ENFORCE = "enforce"
    OBSERVE = "observe"


def effective_mode(ceiling: Mode, stored: Mode) -> Mode:
    """환경 변수의 모드 상한이 enforce이면 저장된 모드가 observe여도 enforce로 집행한다."""
    return Mode.ENFORCE if ceiling is Mode.ENFORCE else stored


class ObservationSettings(BaseModel):
    """저장된 설정 중 관측 모드 전환 조건에 쓰이는 부분."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observable_reasons: frozenset[Reason] = OBSERVATION_CANDIDATES
    observable_policy_ids: frozenset[str] = frozenset()

    @field_validator("observable_reasons")
    @classmethod
    def _within_candidates(cls, value: frozenset[Reason]) -> frozenset[Reason]:
        extra = value - OBSERVATION_CANDIDATES
        if extra:
            names = ", ".join(sorted(r.value for r in extra))
            raise ValueError(f"not eligible for observation: {names}")
        return value


class SettingSource(StrEnum):
    ENV = "env"
    DB = "db"


class MintSettingRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    source: SettingSource
    rule: str


MINT_SETTINGS: tuple[MintSettingRow, ...] = (
    MintSettingRow(
        key="MINT_MODE",
        source=SettingSource.ENV,
        rule="모드 상한. enforce이면 저장된 설정이 observe로 바꿀 수 없다.",
    ),
    MintSettingRow(
        key="settings.mode",
        source=SettingSource.DB,
        rule="상한 이하에서만 설정한다. 변경은 사건을 남기고 세대를 올린다.",
    ),
    MintSettingRow(
        key="settings.observable_reasons",
        source=SettingSource.DB,
        rule="관측 모드로 전환할 수 있는 사유 계열의 부분집합. 기본값은 전환 후보 전체.",
    ),
    MintSettingRow(
        key="settings.observable_policy_ids",
        source=SettingSource.DB,
        rule="관측 가능으로 선언된 정책 중 관측에 쓸 정책 식별자.",
    ),
    MintSettingRow(
        key="settings.default_org_id",
        source=SettingSource.DB,
        rule="팀·사용자 바인딩이 없는 주체에 적용할 기본 조직. 해제할 수 있다.",
    ),
    MintSettingRow(
        key="MINT_REQUIRED",
        source=SettingSource.ENV,
        rule="기본 true. 자가 점검 실패 시 프록시 기동을 중단한다.",
    ),
    MintSettingRow(
        key="MINT_HIGH_RISK_TOOLS",
        source=SettingSource.ENV,
        rule="호출별 승인을 요구할 고위험 도구 목록. 저장된 설정으로 덮지 않는다.",
    ),
)


class SelfCheckItem(IntEnum):
    CALLBACK_REGISTERED = 1
    GUARDRAIL_MODES = 2
    GUARDRAIL_ORDER = 3
    KEY_ACCESS_STRICT = 4
    SERVER_REGISTRY_MATCH = 5
    NAME_ALIAS_UNIQUE = 6
    COMPAT_SYMBOLS = 7
    REQUIRED_SETTINGS = 8


SELF_CHECK_DESCRIPTIONS: dict[SelfCheckItem, str] = {
    SelfCheckItem.CALLBACK_REGISTERED: "MINT 로거가 LiteLLM 콜백에 등록되어 있다.",
    SelfCheckItem.GUARDRAIL_MODES: "MINT 가드레일이 두 모드로 등록되어 있고 default_on이다.",
    SelfCheckItem.GUARDRAIL_ORDER: "MINT 가드레일이 사전 훅 순서의 마지막이다.",
    SelfCheckItem.KEY_ACCESS_STRICT: (
        "통제 서버가 allow_all_keys: false이고 require_key_mcp_access_defined가 true이다."
    ),
    SelfCheckItem.SERVER_REGISTRY_MATCH: (
        "통제 서버의 server_id가 레지스트리에 있고 계약 값(URL 해시·전송 방식·인증 방식)과 같다."
    ),
    SelfCheckItem.NAME_ALIAS_UNIQUE: "설정과 DB를 합친 서버의 이름·별칭이 전역에서 유일하다.",
    SelfCheckItem.COMPAT_SYMBOLS: "호환 계층이 쓰는 LiteLLM 비공개 심볼이 모두 존재한다.",
    SelfCheckItem.REQUIRED_SETTINGS: "필수 설정 행이 모두 표의 값과 일치한다.",
}


class SettingGrade(StrEnum):
    REQUIRED = "required"
    RECOMMENDED = "recommended"
    OBSERVATION = "observation"


class SettingLocation(StrEnum):
    GUARDRAILS = "guardrails"
    LITELLM_SETTINGS = "litellm_settings"
    GENERAL_SETTINGS = "general_settings"
    MCP_SERVERS = "mcp_servers"
    ENVIRONMENT = "environment"
    PROCESS = "process"
    MANAGEMENT_API = "management_api"


class SettingRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    location: SettingLocation
    value: str
    grade: SettingGrade
    self_checks: tuple[SelfCheckItem, ...] = ()
    # 프로세스 안에서 원본 설정을 읽지 않아 init·check의 설정 파일 검사로만 확인하는 행
    file_check_only: bool = False
    observation_item: str | None = None
    reason: str


SETTINGS_TABLE: tuple[SettingRow, ...] = (
    SettingRow(
        key="guardrails[MINT]",
        location=SettingLocation.GUARDRAILS,
        value="mode: [pre_mcp_call, post_mcp_call], default_on: true, 사전 훅 순서 마지막",
        grade=SettingGrade.REQUIRED,
        self_checks=(SelfCheckItem.GUARDRAIL_MODES, SelfCheckItem.GUARDRAIL_ORDER),
        reason="가드레일이 없거나 순서가 뒤바뀌면 목록 필터와 인자 변경 보장이 사라진다.",
    ),
    SettingRow(
        key="callbacks",
        location=SettingLocation.LITELLM_SETTINGS,
        value="MINT 로거 등록",
        grade=SettingGrade.REQUIRED,
        self_checks=(SelfCheckItem.CALLBACK_REGISTERED,),
        reason="도구 호출 판정은 로거의 사전 훅이 수행한다. 키·팀 설정으로 끌 수 없는 지점이다.",
    ),
    SettingRow(
        key="mcp_servers.<이름>.allow_all_keys",
        location=SettingLocation.MCP_SERVERS,
        value="false",
        grade=SettingGrade.REQUIRED,
        self_checks=(SelfCheckItem.KEY_ACCESS_STRICT,),
        reason="모든 키에 열린 서버는 통제 대상에서 제외된다.",
    ),
    SettingRow(
        key="mcp_servers.<이름>.server_id",
        location=SettingLocation.MCP_SERVERS,
        value="명시",
        grade=SettingGrade.REQUIRED,
        self_checks=(),
        file_check_only=True,
        reason=(
            "명시하지 않으면 URL이 바뀔 때 식별자가 바뀌어 기존 권한이 어긋난다. "
            "설정 파일 검사로 확인하며, 프로세스 안에서는 레지스트리 값 점검이 대신한다."
        ),
    ),
    SettingRow(
        key="서버 이름·별칭 유일성",
        location=SettingLocation.MANAGEMENT_API,
        value="전역 유일",
        grade=SettingGrade.REQUIRED,
        self_checks=(SelfCheckItem.NAME_ALIAS_UNIQUE,),
        reason="설정 서버와 같은 별칭의 DB 서버가 있으면 판정 대상과 실행 대상이 갈린다.",
    ),
    SettingRow(
        key="호환 계층 비공개 심볼",
        location=SettingLocation.PROCESS,
        value="존재",
        grade=SettingGrade.REQUIRED,
        self_checks=(SelfCheckItem.COMPAT_SYMBOLS,),
        reason="지원 판본이 아니면 심볼이 사라져 서버 식별이 불가능해진다.",
    ),
    SettingRow(
        key="require_key_mcp_access_defined",
        location=SettingLocation.GENERAL_SETTINGS,
        value="true",
        grade=SettingGrade.REQUIRED,
        self_checks=(SelfCheckItem.KEY_ACCESS_STRICT,),
        reason="MCP 접근 범위가 정의되지 않은 키가 서버를 호출하지 못하게 한다.",
    ),
    SettingRow(
        key="LITELLM_STRICT_GUARDRAIL_MODES",
        location=SettingLocation.ENVIRONMENT,
        value="true (기본값, false 금지)",
        grade=SettingGrade.REQUIRED,
        self_checks=(SelfCheckItem.REQUIRED_SETTINGS,),
        reason="가드레일 모드 불일치를 경고만 하고 넘기지 않도록 한다.",
    ),
    SettingRow(
        key="store_prompts_in_spend_logs",
        location=SettingLocation.GENERAL_SETTINGS,
        value="false (기본값, true 금지)",
        grade=SettingGrade.REQUIRED,
        self_checks=(SelfCheckItem.REQUIRED_SETTINGS,),
        reason="true이면 도구 결과 본문이 LiteLLM 지출 로그에 저장된다.",
    ),
    SettingRow(
        key="user_url_validation",
        location=SettingLocation.LITELLM_SETTINGS,
        value="true (기본값)",
        grade=SettingGrade.REQUIRED,
        self_checks=(SelfCheckItem.REQUIRED_SETTINGS,),
        reason="사용자가 지정한 메타데이터 주소의 내부망 접근을 막는다.",
    ),
    SettingRow(
        key="supported_db_objects",
        location=SettingLocation.GENERAL_SETTINGS,
        value="설정한 경우 mcp 포함",
        grade=SettingGrade.REQUIRED,
        self_checks=(SelfCheckItem.REQUIRED_SETTINGS,),
        reason="mcp가 빠지면 DB에 등록한 서버가 복제본에 반영되지 않는다.",
    ),
    SettingRow(
        key="disable_spend_logs",
        location=SettingLocation.GENERAL_SETTINGS,
        value="true는 init --no-spend-logs를 선택한 경우만",
        grade=SettingGrade.OBSERVATION,
        observation_item="spend_log_arguments_stored",
        reason="도구 호출 인자가 LiteLLM 지출 로그에 저장되는지 표시한다.",
    ),
    SettingRow(
        key="key_generation_settings.personal_key_generation.allowed_user_roles",
        location=SettingLocation.LITELLM_SETTINGS,
        value='["proxy_admin"]',
        grade=SettingGrade.RECOMMENDED,
        reason="일반 사용자가 가드레일 제외 메타데이터를 담은 개인 키를 만들지 못하게 한다.",
    ),
    SettingRow(
        key="mcp_allow_all_keys_respects_mcp_scope",
        location=SettingLocation.GENERAL_SETTINGS,
        value="true",
        grade=SettingGrade.RECOMMENDED,
        reason="접근 범위를 명시한 키에서 개방 서버를 제외한다.",
    ),
    SettingRow(
        key="PROXY_BASE_URL",
        location=SettingLocation.ENVIRONMENT,
        value="설정",
        grade=SettingGrade.RECOMMENDED,
        reason="없으면 OAuth 메타데이터와 콜백의 기준 주소가 요청의 Host 헤더를 따른다.",
    ),
    SettingRow(
        key="MCP_TRUSTED_REDIRECT_ORIGINS, use_x_forwarded_for, mcp_trusted_proxy_ranges",
        location=SettingLocation.ENVIRONMENT,
        value="배치에 맞게 설정",
        grade=SettingGrade.RECOMMENDED,
        reason="리다이렉트 수신처와 역프록시 뒤의 클라이언트 주소 판별을 제한한다.",
    ),
    SettingRow(
        key="mcp_servers.<이름>.issuer",
        location=SettingLocation.MCP_SERVERS,
        value="발견형 인증 서버는 명시",
        grade=SettingGrade.RECOMMENDED,
        reason="고정하지 않으면 첫 응답의 인가 서버를 그대로 신뢰한다.",
    ),
    SettingRow(
        key="mcp_servers.<이름>.oauth_identity_binding.mode",
        location=SettingLocation.MCP_SERVERS,
        value="enforce",
        grade=SettingGrade.RECOMMENDED,
        reason="nonce 대조와 주체 결속, PKCE 강제를 켠다.",
    ),
    SettingRow(
        key="LEGACY_MULTI_INSTANCE_RATE_LIMITING",
        location=SettingLocation.ENVIRONMENT,
        value="미설정",
        grade=SettingGrade.RECOMMENDED,
        reason="true이면 MCP 분당 호출 한도가 집행되지 않는다.",
    ),
    SettingRow(
        key="enable_redis_auth_cache",
        location=SettingLocation.LITELLM_SETTINGS,
        value="복제본이 둘 이상이면 설정 (공유 Redis 필요)",
        grade=SettingGrade.RECOMMENDED,
        observation_item="auth_cache_shared",
        reason="Redis가 없으면 키·팀 권한 캐시 무효화가 복제본 사이에 전파되지 않는다.",
    ),
)

# check·status가 출력하는 관측 항목. 실패 조건이 아니다.
# guardrail_optout_subjects는 가드레일 제외 메타데이터를 가진 키·팀의 수이다.
OBSERVATION_ITEMS: tuple[str, ...] = (
    "spend_log_arguments_stored",
    "guardrail_optout_subjects",
    "auth_cache_shared",
)
