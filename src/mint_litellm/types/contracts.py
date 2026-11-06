"""도구 계약의 위험 등급 값과 점수 규칙.

등급 점수 = max(자료 점수, 행위 점수) + (되돌릴 수 없으면 1). 0 low, 1 medium, 2 high,
3 이상 critical. high 이상 도구는 호출별 승인 대상이다.
"""

from enum import StrEnum


class DataGrade(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class ActionType(StrEnum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    EXTERNAL_SEND = "external_send"
    CREDENTIAL_ACCESS = "credential_access"


class RiskGrade(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


DATA_SCORE: dict[DataGrade, int] = {
    DataGrade.PUBLIC: 0,
    DataGrade.INTERNAL: 1,
    DataGrade.CONFIDENTIAL: 2,
    DataGrade.RESTRICTED: 3,
}

ACTION_SCORE: dict[ActionType, int] = {
    ActionType.READ: 0,
    ActionType.WRITE: 1,
    ActionType.EXTERNAL_SEND: 2,
    ActionType.DELETE: 2,
    ActionType.CREDENTIAL_ACCESS: 3,
}

_BY_SCORE = (RiskGrade.LOW, RiskGrade.MEDIUM, RiskGrade.HIGH, RiskGrade.CRITICAL)

HIGH_RISK_GRADES: frozenset[RiskGrade] = frozenset({RiskGrade.HIGH, RiskGrade.CRITICAL})


def risk_grade(data: DataGrade, action: ActionType, *, irreversible: bool = False) -> RiskGrade:
    score = max(DATA_SCORE[data], ACTION_SCORE[action]) + (1 if irreversible else 0)
    return _BY_SCORE[min(score, 3)]
