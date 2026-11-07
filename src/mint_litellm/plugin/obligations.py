"""정책 출력의 정규화와 의무 합성.

지원하는 의무는 알림·호출별 승인·동의·본인 확인 네 가지다.
그 밖의 의무(인자 변환 등)나 서로 모순되는 의무는 호출을 차단하는 ``stop``이 된다.
원장에는 의무의 종류와 정책 식별자만 남기고 의무 값(심각도·목적)은 남기지 않는다.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from mint_litellm.types.decision import (
    ObligationType,
    PolicyDecision,
    Severity,
    StageResult,
)
from mint_litellm.types.reason_codes import Reason

SUPPORTED_OBLIGATIONS: frozenset[ObligationType] = frozenset({
    ObligationType.ALERT, ObligationType.REQUIRE_CALL_APPROVAL,
    ObligationType.REQUIRE_CONSENT, ObligationType.REQUIRE_USER_CONFIRMATION,
})

_SEVERITY_RANK = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


class AlertObligation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal[ObligationType.ALERT] = ObligationType.ALERT
    severity: Severity


class RequireCallApproval(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal[ObligationType.REQUIRE_CALL_APPROVAL] = ObligationType.REQUIRE_CALL_APPROVAL


class RequireConsent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal[ObligationType.REQUIRE_CONSENT] = ObligationType.REQUIRE_CONSENT
    purpose: str = Field(min_length=1, max_length=128, pattern=r"\S")


class RequireUserConfirmation(BaseModel):
    """본인 확인 의무의 형식. ``display_fields``는 확인 화면에 보일 인자 이름이다."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal[ObligationType.REQUIRE_USER_CONFIRMATION] = (
        ObligationType.REQUIRE_USER_CONFIRMATION
    )
    purpose: str = Field(min_length=1, max_length=128, pattern=r"\S")
    display_fields: tuple[str, ...] = ()


Obligation = Annotated[
    AlertObligation | RequireCallApproval | RequireConsent | RequireUserConfirmation,
    Field(discriminator="type"),
]
ParsedObligation = AlertObligation | RequireCallApproval | RequireConsent | RequireUserConfirmation
_OBLIGATION = TypeAdapter(Obligation)


class PolicyNormalization(BaseModel):
    """정규화 결과. ``failure``가 있으면 호출은 차단되며 다른 필드는 비어 있다.

    ``failure``의 사유가 ``SCHEMA_ERROR``이면 정책 단계, ``UNSUPPORTED``이면 의무 확인
    단계의 stop이다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: PolicyDecision | None = None
    obligations: tuple[ParsedObligation, ...] = ()
    findings: tuple[StageResult, ...] = ()
    failure: StageResult | None = None

    @property
    def obligation_types(self) -> tuple[ObligationType, ...]:
        return tuple(o.type for o in self.obligations)


def _failed(reason: Reason, detail: str) -> PolicyNormalization:
    return PolicyNormalization(failure=StageResult.stop(reason, detail=detail))


def _parse(item: Mapping[str, Any]) -> ParsedObligation:
    return _OBLIGATION.validate_python(dict(item))


def compose_obligations(
    raw: Iterable[Mapping[str, Any]], *, require_call_approval: bool = False
) -> PolicyNormalization:
    """의무 목록을 지원 집합 안에서 합성한다.

    같은 종류는 하나로 합친다: ``alert``는 가장 높은 심각도, ``require_call_approval``은 하나,
    ``require_consent``는 같은 목적일 때만 하나로 합치고 목적이 다르면 충돌로 차단한다.
    """
    parsed: list[ParsedObligation] = []
    for item in raw:
        kind = item.get("type") if isinstance(item, Mapping) else None
        if not isinstance(kind, str):
            return _failed(Reason.SCHEMA_ERROR, "obligation without type")
        if kind not in {t.value for t in SUPPORTED_OBLIGATIONS}:
            return _failed(Reason.UNSUPPORTED, f"obligation type {kind[:64]}")
        try:
            parsed.append(_parse(item))
        except ValidationError:
            return _failed(Reason.SCHEMA_ERROR, f"invalid {kind}")
    if require_call_approval:
        parsed.append(RequireCallApproval())

    alerts = [o for o in parsed if isinstance(o, AlertObligation)]
    purposes = {o.purpose for o in parsed if isinstance(o, RequireConsent)}
    if len(purposes) > 1:
        return _failed(Reason.UNSUPPORTED, "conflicting consent purposes")

    confirmations = [o for o in parsed if isinstance(o, RequireUserConfirmation)]
    if len({o.purpose for o in confirmations}) > 1:
        return _failed(Reason.UNSUPPORTED, "conflicting confirmation purposes")

    composed: list[ParsedObligation] = []
    if alerts:
        composed.append(max(alerts, key=lambda a: _SEVERITY_RANK[a.severity]))
    if any(isinstance(o, RequireCallApproval) for o in parsed):
        composed.append(RequireCallApproval())
    if purposes:
        composed.append(RequireConsent(purpose=next(iter(purposes))))
    if confirmations:
        composed.append(RequireUserConfirmation(
            purpose=confirmations[0].purpose,
            display_fields=tuple(sorted({f for o in confirmations for f in o.display_fields})),
        ))
    return PolicyNormalization(obligations=tuple(composed))


def normalize_policy_output(
    decision: object,
    policy_ids: Sequence[str],
    obligations: Iterable[Mapping[str, Any]],
) -> PolicyNormalization:
    """정책 엔진 출력을 파이프라인이 쓰는 형태로 바꾼다.

    - 정의 밖 결정 값, 또는 정책 식별자가 없는 Block은 ``SCHEMA_ERROR``.
    - ``RequireApproval``은 ``require_call_approval`` 의무로 바뀐다.
    - ``Block``은 정책마다 ``POLICY_DETECT_<id>`` finding이 된다. 관측 모드에서 허용으로
      전환할지는 종결자가 정책의 관측 가능 선언과 저장된 설정으로 정한다.
    """
    try:
        parsed = PolicyDecision(decision)
    except ValueError:
        return _failed(Reason.SCHEMA_ERROR, "unknown policy decision")
    findings: tuple[StageResult, ...] = ()
    if parsed is PolicyDecision.BLOCK:
        if not policy_ids:
            return _failed(Reason.SCHEMA_ERROR, "block without policy ids")
        try:
            findings = tuple(
                StageResult.finding(Reason.POLICY_DETECT, policy_id) for policy_id in policy_ids
            )
        except ValueError:
            return _failed(Reason.SCHEMA_ERROR, "invalid policy id")
    composed = compose_obligations(
        obligations, require_call_approval=parsed is PolicyDecision.REQUIRE_APPROVAL
    )
    if composed.failure is not None:
        return composed
    return composed.model_copy(update={"decision": parsed, "findings": findings})
