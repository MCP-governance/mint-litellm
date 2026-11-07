"""화면에 보이는 한국어 표시명. 영어 열거값은 화면에 내지 않고 이 표를 거친다.

상태 표시는 ``(표시명, 심각도)``이고 심각도는 색·아이콘을 정한다: ok(정상), wait(대기), error(오류),
block(차단), off(비활성). 겹치면 차단 > 오류 > 대기 > 정상이다.
"""

from typing import NamedTuple


class Status(NamedTuple):
    text: str
    level: str


SEVERITY_ORDER = ("block", "error", "wait", "ok", "off")

SERVER = {
    "pending": Status("접수", "wait"),
    "validating": Status("검사 중", "wait"),
    "awaiting_approval": Status("승인 대기", "wait"),
    "rejected": Status("거절됨", "block"),
    "approved": Status("승인됨", "ok"),
    "suspended": Status("중지됨", "block"),
    "terminating": Status("종료 중", "wait"),
    "terminated": Status("종료됨", "off"),
}
APPROVAL = {
    "pending": Status("대기", "wait"),
    "active": Status("유효", "ok"),
    "expired": Status("만료", "off"),
    "revoked": Status("철회됨", "block"),
}
CALL_APPROVAL = {
    "pending": Status("응답 대기", "wait"),
    "approved": Status("승인됨", "ok"),
    "rejected": Status("거부됨", "block"),
    "cancelled": Status("취소됨", "off"),
    "expired": Status("만료", "off"),
    "consumed": Status("사용됨", "off"),
}
RELATION = {
    "active": Status("이용 중", "ok"),
    "terminating": Status("종료 중", "wait"),
    "terminated": Status("종료됨", "off"),
}
ORG = {"active": Status("운영 중", "ok"), "closed": Status("닫힘", "off")}
ALLOWLIST = {
    "active": Status("유효", "ok"),
    "expired": Status("만료", "off"),
    "lifted": Status("해제됨", "off"),
}
POLICY_VERSION = {
    "validated": Status("검증됨", "wait"),
    "staged": Status("준비됨", "wait"),
    "active": Status("적용 중", "ok"),
    "superseded": Status("교체됨", "off"),
}
JOB = {
    "pending": Status("대기", "wait"),
    "running": Status("실행 중", "wait"),
    "done": Status("완료", "ok"),
    "failed": Status("실패", "error"),
}
ALERT_SEVERITY = {
    "low": Status("낮음", "wait"),
    "medium": Status("보통", "wait"),
    "high": Status("높음", "error"),
    "critical": Status("심각", "block"),
}
ENFORCEMENT = {
    "enforcement_verified": Status("정상", "ok"),
    "enforcement_unverified": Status("확인 불가", "error"),
}

ROLE = {"admin": "관리자", "approver": "승인자", "viewer": "조회자", "staff": "직원"}
MODE = {"enforce": "집행", "observe": "관측"}
SCOPE = {"key": "키", "user": "사용자", "team": "팀", "org": "조직", "server": "서버"}
APPROVAL_CLASS = {"standard": "일반", "exception": "예외"}
PRINCIPAL = {"user": "사용자", "team_key": "팀 키", "service_account": "서비스 계정"}
ALLOWLIST_KIND = {
    "provider": "제공자",
    "auth_server": "인증 서버",
    "egress_target": "외부 전송 대상",
    "registry_source": "레지스트리 출처",
}
TRANSPORT = {"http": "HTTP", "sse": "SSE", "streamable_http": "스트리밍 HTTP"}
AUTH_TYPE = {
    "none": "없음",
    "api_key": "API 키",
    "bearer_token": "베어러 토큰",
    "basic": "기본 인증",
    "oauth2": "OAuth",
    "token": "토큰",
}
RESULT = {
    "allow": "허용",
    "deny": "차단",
    "stop": "차단",
    "transform": "변환",
    "observe": "관측",
}
OBSERVATION = {
    "spend_log_arguments_stored": "지출 기록에 인자 저장",
    "guardrail_optout_subjects": "가드레일 제외 대상",
    "auth_cache_shared": "인증 캐시 공유",
    "unbound_keys": "조직에 묶이지 않은 키",
}
REASON = {
    "SERVER_UNREGISTERED": "등록되지 않은 서버",
    "SERVER_NOT_APPROVED": "승인되지 않은 서버",
    "APPROVAL_ABSENT": "승인 없음",
    "TOOL_NOT_IN_APPROVAL": "승인 범위 밖 도구",
    "POLICY_DETECT": "정책 탐지",
    "CONTRACT_DRIFT": "계약 변경",
    "OUTPUT_SCAN": "결과 검사",
}

EVENT_KIND = {
    "decision_evaluated": "호출 판정",
    "discovery_evaluated": "도구 목록 판정",
    "call_response_received": "호출 응답",
    "call_outcome_unknown": "호출 결과 불명",
    "enforcement_unverified": "집행 확인 불가",
    "enforcement_verified": "집행 확인",
    "server_identity_mismatch_observed": "서버 신원 불일치 관측",
    "buffer_overflow": "버퍼 초과",
    "buffer_replayed": "버퍼 재전송",
    "server_registered": "서버 등록",
    "server_approved": "서버 승인",
    "server_suspended": "서버 중지",
    "server_resumed": "서버 재개",
    "relation_created": "이용 관계 생성",
    "relation_terminating": "이용 관계 종료",
    "approval_granted": "승인",
    "approval_revoked": "승인 철회",
    "denial_created": "거부 등록",
    "denial_lifted": "거부 해제",
    "call_approval_requested": "호출별 승인 요청",
    "call_approval_approved": "호출별 승인",
    "call_approval_rejected": "호출별 거부",
    "call_approval_cancelled": "호출별 승인 취소",
    "consent_granted": "동의 부여",
    "consent_revoked": "동의 철회",
    "allowlist_added": "허용 목록 추가",
    "allowlist_lifted": "허용 목록 해제",
    "org_created": "조직 생성",
    "org_closed": "조직 닫기",
    "org_binding_created": "조직 연결",
    "org_binding_removed": "조직 연결 해제",
    "policy_activated": "정책 적용",
    "settings_changed": "설정 변경",
    "operator_added": "운영자 등록",
    "intake_received": "신청 접수",
    "intake_requester_assigned": "신청자 지정",
    "intake_checked": "수용검증",
    "intake_decided": "신청 결정",
    "contract_approved": "계약 승인",
    "contract_drift_detected": "계약 변경 탐지",
    "contract_superseded": "계약 교체",
    "reapproval_requested": "재승인 요청",
    "sync_applied": "동기화 적용",
    "sync_held": "동기화 보류",
    "export_written": "내보내기 완료",
    "export_failed": "내보내기 실패",
    "restore_blocked": "되돌림 차단",
    "restore_attested": "되돌림 확인",
    "restore_stale_accepted": "오래된 상태 수용",
    "alert_opened": "경보 발생",
    "alert_acknowledged": "경보 확인",
    "alert_resolved": "경보 해결",
    "console_session_started": "콘솔 로그인",
    "console_session_ended": "콘솔 로그아웃",
    "deployment_contract_approved": "배포 계약 승인",
    "deployment_drift_detected": "배포 변경 탐지",
    "deployment_contract_superseded": "배포 계약 교체",
}

SESSION_END_REASON = {
    "logout": "로그아웃",
    "key_gone": "키 삭제",
    "key_blocked": "키 차단",
    "key_expired": "키 만료",
    "key_owner_changed": "키 소유자 변경",
    "user_not_human": "사람 사용자 아님",
    "operator_changed": "운영자 변경",
    "unverified": "재확인 실패",
}

NOTICES = {
    "suspended": "서버를 중지했습니다.",
    "resumed": "서버를 재개했습니다.",
    "approved": "승인했습니다.",
    "revoked": "승인을 철회했습니다.",
    "revoked_denied": "승인을 철회하고 거부를 등록했습니다.",
    "denied": "거부를 등록했습니다.",
    "lifted": "해제했습니다.",
    "call_approved": "호출을 승인했습니다.",
    "call_rejected": "호출을 거부했습니다.",
    "call_cancelled": "승인 요청을 취소했습니다.",
    "org_added": "조직을 생성했습니다.",
    "org_closed": "조직을 닫았습니다.",
    "allowlist_added": "허용 목록에 추가했습니다.",
    "mode_set": "모드를 변경했습니다.",
}


def status(table: dict[str, Status], value: str | None) -> Status:
    if value is None:
        return Status("—", "off")
    return table.get(value, Status(value, "off"))


def pick(mapping: dict[str, str], value: str | None) -> str:
    return "—" if value in (None, "") else mapping.get(value, value)


def worst(levels: list[str]) -> str:
    for level in SEVERITY_ORDER:
        if level in levels:
            return level
    return "off"
