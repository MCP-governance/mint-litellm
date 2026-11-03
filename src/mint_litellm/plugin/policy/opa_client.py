"""OPA 판정 요청과 활성 판본 확인."""

import asyncio
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mint_litellm.plugin.obligations import normalize_policy_output
from mint_litellm.plugin.policy.input import has_only_policy_fields
from mint_litellm.types.decision import PolicyDecision, StageResult
from mint_litellm.types.reason_codes import Reason


class PolicyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    decision: str
    policy_ids: list[str]
    policy_version_id: str = Field(min_length=1)
    bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    obligations: list[dict[str, Any]]
    observable: bool
    missing_inputs: list[str]


@dataclass(frozen=True)
class Evaluation:
    output: PolicyOutput | None = None
    failure: StageResult | None = None


class OPAClient:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._url = base_url.rstrip("/") + "/v1/data/mint/decision"
        self._timeout = timeout_seconds

    async def evaluate(
        self, policy_input: dict[str, Any], *, policy_version_id: str, bundle_sha256: str
    ) -> Evaluation:
        if not has_only_policy_fields(policy_input):
            return Evaluation(failure=StageResult.stop(Reason.SCHEMA_ERROR))
        try:
            async with asyncio.timeout(self._timeout):
                async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
                    for _ in range(2):
                        response = await client.post(self._url, json={"input": policy_input})
                        response.raise_for_status()
                        try:
                            result = response.json()["result"]
                            output = PolicyOutput.model_validate(result)
                        except (KeyError, TypeError, ValueError, ValidationError):
                            return Evaluation(failure=StageResult.stop(Reason.SCHEMA_ERROR))
                        if (output.policy_version_id, output.bundle_sha256) == (
                            policy_version_id,
                            bundle_sha256,
                        ):
                            if output.missing_inputs:
                                return Evaluation(failure=StageResult.stop(Reason.SCHEMA_ERROR))
                            try:
                                PolicyDecision(output.decision)
                            except ValueError:
                                return Evaluation(failure=StageResult.stop(Reason.SCHEMA_ERROR))
                            normalized = normalize_policy_output(
                                output.decision, output.policy_ids, output.obligations
                            )
                            if normalized.failure is not None:
                                return Evaluation(failure=normalized.failure)
                            return Evaluation(output=output)
                    return Evaluation(failure=StageResult.stop(Reason.POLICY_VERSION_MISMATCH))
        except (TimeoutError, httpx.HTTPError):
            return Evaluation(failure=StageResult.stop(Reason.DEPENDENCY_FAILURE))
