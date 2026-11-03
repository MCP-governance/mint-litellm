"""단계 결과, 종결자, 판정 스냅숏.

단계는 ``finding``(관측 모드로 전환될 수 있는 사유)을 누적하며 끝까지 진행하고, 전환
불가 사유만 ``stop``으로 이후 단계를 건너뛴다. 최종 집행은 ``conclude``가 정한다.
"""

import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from mint_litellm.types.identity import Principal
from mint_litellm.types.reason_codes import (
    OBSERVATION_CANDIDATES,
    Reason,
    make_reason_code,
    parse_reason_code,
)
from mint_litellm.types.settings import Mode, ObservationSettings

MINT_CTX_KEY = "mint_ctx"
"""``litellm_logging_obj.model_call_details``에서 호출 컨텍스트를 싣는 키."""

MINT_DECISION_REF_KEY = "mint_decision_ref"
"""사전 훅 ``data``에서 판정이 끝났음을 가드레일에 알리는 표식 키."""


class PolicyDecision(StrEnum):
    ALLOW = "Allow"
    ALERT = "Alert"
    BLOCK = "Block"
    REQUIRE_APPROVAL = "RequireApproval"


_DECISION_RANK = {
    PolicyDecision.ALLOW: 0,
    PolicyDecision.ALERT: 1,
    PolicyDecision.REQUIRE_APPROVAL: 2,
    PolicyDecision.BLOCK: 3,
}


def strongest_decision(decisions: Iterable[PolicyDecision]) -> PolicyDecision:
    """Block > RequireApproval > Alert > Allow. 비어 있으면 Allow."""
    return max(decisions, key=_DECISION_RANK.__getitem__, default=PolicyDecision.ALLOW)


class ObligationType(StrEnum):
    ALERT = "alert"
    REQUIRE_CALL_APPROVAL = "require_call_approval"
    REQUIRE_CONSENT = "require_consent"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class OperationType(StrEnum):
    TOOL_CALL = "tool_call"
    RUNTIME_DISCOVERY = "runtime_discovery"


class Action(StrEnum):
    ALLOW = "allow"
    BLOCK = "block"


class StageStatus(StrEnum):
    PASS = "pass"
    FINDING = "finding"
    STOP = "stop"
    SKIPPED = "skipped"


class Stage(StrEnum):
    CONTEXT = "context"
    SNAPSHOT = "snapshot"
    STATE = "state"
    DENIAL_APPROVAL = "denial_approval"
    CONTRACT = "contract"
    POLICY = "policy"
    OBLIGATIONS = "obligations"


STAGE_ORDER: tuple[Stage, ...] = tuple(Stage)


class StageResult(BaseModel):
    """한 단계의 결과.

    ``finding``은 전환 후보 계열의 사유만, ``stop``은 전환 불가 사유만 가질 수 있다.
    ``detail``에는 인자·결과·의무 값을 넣지 않는다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: StageStatus
    reason_code: str | None = None
    detail: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.status is StageStatus.SKIPPED:
            raise ValueError("skipped is recorded per stage, not returned by a stage")
        if self.status is StageStatus.PASS:
            if self.reason_code is not None:
                raise ValueError("pass carries no reason_code")
            return self
        if self.reason_code is None:
            raise ValueError(f"{self.status.value} requires reason_code")
        family, _ = parse_reason_code(self.reason_code)
        candidate = family in OBSERVATION_CANDIDATES
        if self.status is StageStatus.FINDING and not candidate:
            raise ValueError(f"{self.reason_code} cannot be a finding; use stop")
        if self.status is StageStatus.STOP and candidate:
            raise ValueError(f"{self.reason_code} cannot be a stop; use finding")
        return self

    @classmethod
    def passed(cls) -> Self:
        return cls(status=StageStatus.PASS)

    @classmethod
    def finding(
        cls, reason: Reason, qualifier: str | None = None, detail: str | None = None
    ) -> Self:
        return cls(
            status=StageStatus.FINDING,
            reason_code=make_reason_code(reason, qualifier),
            detail=detail,
        )

    @classmethod
    def stop(cls, reason: Reason, qualifier: str | None = None, detail: str | None = None) -> Self:
        return cls(
            status=StageStatus.STOP,
            reason_code=make_reason_code(reason, qualifier),
            detail=detail,
        )


class StageEntry(BaseModel):
    """판정 스냅숏에 남기는 단계별 기록. 건너뛴 단계도 ``skipped``로 남는다."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Stage
    status: StageStatus
    reason_code: str | None = None
    detail: str | None = Field(default=None, max_length=256)
    missing_reason: str | None = None

    @model_validator(mode="after")
    def _skipped_explains(self) -> Self:
        if self.status is StageStatus.SKIPPED and self.missing_reason is None:
            raise ValueError("skipped stage requires missing_reason")
        return self


def entries_from_results(results: Iterable[tuple[Stage, StageResult]]) -> tuple[StageEntry, ...]:
    """단계 결과 목록을 스냅숏 기록으로 바꾼다. ``stop`` 뒤의 단계는 ``skipped``로 채운다."""
    entries: list[StageEntry] = []
    stopped = False
    done: set[Stage] = set()
    for stage, result in results:
        if stopped:
            break
        entries.append(
            StageEntry(
                stage=stage,
                status=result.status,
                reason_code=result.reason_code,
                detail=result.detail,
            )
        )
        done.add(stage)
        stopped = result.status is StageStatus.STOP
    if stopped:
        entries.extend(
            StageEntry(stage=s, status=StageStatus.SKIPPED, missing_reason="after_stop")
            for s in STAGE_ORDER
            if s not in done
        )
    return tuple(entries)


class Conclusion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluated_decision: Action
    enforced_action: Action
    findings: tuple[StageResult, ...] = ()
    stop: StageResult | None = None


def _is_converted(
    finding: StageResult,
    *,
    mode: Mode,
    observation: ObservationSettings,
    policy_observable: bool,
) -> bool:
    """finding이 관측 모드에서 허용으로 전환되는지."""
    if mode is not Mode.OBSERVE:
        return False
    assert finding.reason_code is not None
    family, qualifier = parse_reason_code(finding.reason_code)
    if family not in observation.observable_reasons:
        return False
    if family is Reason.POLICY_DETECT:
        return policy_observable and qualifier in observation.observable_policy_ids
    return True


def conclude(
    results: Iterable[StageResult],
    *,
    mode: Mode,
    observation: ObservationSettings,
    policy_observable: bool = False,
) -> Conclusion:
    """종결자.

    - stop이 있으면 모드와 무관하게 block.
    - 아니면 finding이 하나라도 전환되지 않으면 block. enforce에서는 finding이 곧 block이다.
    - finding이 모두 전환되는 observe, 또는 finding이 없으면 allow.

    ``evaluated_decision``은 전환을 적용하기 전의 평가 결과(stop이나 finding이 있으면
    block)이고 ``enforced_action``은 실제 집행이다. ``mode``는 환경 상한을 반영한 유효
    모드이며 ``policy_observable``은 정책 결정이 관측 가능으로 선언되었는지를 뜻한다.
    """
    findings: list[StageResult] = []
    stop: StageResult | None = None
    for result in results:
        if result.status is StageStatus.STOP:
            if stop is not None:
                raise ValueError("a pipeline has at most one stop")
            stop = result
        elif result.status is StageStatus.FINDING:
            findings.append(result)
    evaluated = Action.BLOCK if (stop or findings) else Action.ALLOW
    if stop is not None:
        enforced = Action.BLOCK
    elif all(
        _is_converted(f, mode=mode, observation=observation, policy_observable=policy_observable)
        for f in findings
    ):
        enforced = Action.ALLOW
    else:
        enforced = Action.BLOCK
    return Conclusion(
        evaluated_decision=evaluated,
        enforced_action=enforced,
        findings=tuple(findings),
        stop=stop,
    )


def apply_record_failure(conclusion: Conclusion) -> Conclusion:
    """선기록 실패의 반영. 허용 경로는 감사 불가로 차단하고, 차단 경로는 결과를 바꾸지 않는다."""
    if conclusion.enforced_action is Action.BLOCK:
        return conclusion
    return Conclusion(
        evaluated_decision=Action.BLOCK,
        enforced_action=Action.BLOCK,
        findings=conclusion.findings,
        stop=StageResult.stop(Reason.AUDIT_FAILURE),
    )


class RevocationCheck(StrEnum):
    UNCHANGED = "unchanged"
    CHANGED = "changed"
    UNAVAILABLE = "unavailable"


class ResolvedServer(BaseModel):
    """호환 계층이 실행 단계와 같은 해석 함수로 얻은 서버.

    ``candidates_with_same_name``이 2 이상이면 같은 이름·별칭의 서버가 여럿이라는 뜻이다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    server_id: str
    url_sha256: str
    transport: str
    auth_type: str
    candidates_with_same_name: int = Field(ge=1)


class MintContext(BaseModel):
    """호출 컨텍스트. ``MINT_CTX_KEY``로 응답·실패 훅까지 전달되며 업스트림 헤더에는 싣지 않는다."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: UUID
    attempt_id: int = Field(ge=1)

    @property
    def decision_ref(self) -> str:
        return f"{self.invocation_id}:{self.attempt_id}"


# 값이 없으면 사유를 함께 남겨야 하는 필드. 가짜 값으로 채우지 않는다.
MISSING_REASON_FIELDS: tuple[str, ...] = (
    "litellm_call_id",
    "requested_server_name",
    "tool",
    "state_epoch",
    "settings_version",
    "policy_version_id",
    "bundle_sha256",
    "policy_decision",
    "resolved_server_id",
    "resolved_url_sha256",
    "resolved_transport",
    "resolved_auth_type",
    "candidates_with_same_name",
    "selected_approval_id",
    "consumed_call_approval_id",
    "revoked_after_snapshot",
    "revocation_check",
)

_MISSING_REASON = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class DecisionSnapshot(BaseModel):
    """``decision_evaluated``·``discovery_evaluated`` 사건의 본문.

    인자·결과 본문과 의무 값은 담지 않는다. 정책 식별자와 의무 종류만 남긴다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: UUID
    attempt_id: int = Field(ge=1)
    decision_ref: str
    litellm_call_id: str | None = None
    operation: OperationType
    evaluated_at: AwareDatetime
    principal: Principal
    requested_server_name: str | None = None
    tool: str | None = None
    resolved_server_id: str | None = None
    resolved_url_sha256: str | None = None
    resolved_transport: str | None = None
    resolved_auth_type: str | None = None
    candidates_with_same_name: int | None = Field(default=None, ge=1)
    mode: Mode
    state_epoch: int | None = Field(default=None, ge=0)
    settings_version: int | None = Field(default=None, ge=0)
    policy_version_id: str | None = None
    bundle_sha256: str | None = None
    policy_decision: PolicyDecision | None = None
    policy_ids: tuple[str, ...] = ()
    obligation_types: tuple[ObligationType, ...] = ()
    stages: tuple[StageEntry, ...] = ()
    findings: tuple[StageResult, ...] = ()
    stop: StageResult | None = None
    evaluated_decision: Action
    enforced_action: Action
    selected_approval_id: str | None = None
    consumed_call_approval_id: str | None = None
    revoked_after_snapshot: bool | None = None
    revocation_check: RevocationCheck | None = None
    missing_reasons: dict[str, str] = {}

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.decision_ref != MintContext(
            invocation_id=self.invocation_id, attempt_id=self.attempt_id
        ).decision_ref:
            raise ValueError("decision_ref does not match invocation_id and attempt_id")

        blocked = self.stop is not None or bool(self.findings)
        if (self.evaluated_decision is Action.BLOCK) != blocked:
            raise ValueError("evaluated_decision disagrees with findings and stop")
        if self.stop is not None and self.enforced_action is Action.ALLOW:
            raise ValueError("a stop cannot be enforced as allow")
        if (
            self.findings
            and self.enforced_action is Action.ALLOW
            and self.mode is Mode.ENFORCE
        ):
            raise ValueError("findings cannot be enforced as allow in enforce mode")

        for name in MISSING_REASON_FIELDS:
            empty = getattr(self, name) is None
            has_reason = name in self.missing_reasons
            if empty and not has_reason:
                raise ValueError(f"{name} is null without missing_reason")
            if not empty and has_reason:
                raise ValueError(f"{name} has a value but also a missing_reason")
        for name, reason in self.missing_reasons.items():
            if name not in MISSING_REASON_FIELDS:
                raise ValueError(f"missing_reason for unknown field {name}")
            if not _MISSING_REASON.match(reason):
                raise ValueError(f"invalid missing_reason for {name}")
        return self
