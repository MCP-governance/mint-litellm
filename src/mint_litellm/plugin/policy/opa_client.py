"""OPA 판정 요청과 활성 판본 확인."""

import asyncio
import os
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
    """OPA 판정 클라이언트. 연결을 재사용하고 시간 제한은 호출마다 받는다.

    연결이 끊기거나 OPA가 재시작되어 요청이 실패하면 그 호출은 ``DEPENDENCY_FAILURE``로
    차단되며, 끊어진 연결은 풀에서 제거되어 다음 호출이 새로 연결한다.

    수명 계약: 인스턴스는 워커 프로세스의 이벤트 루프마다 하나씩 만들고, 그 루프가 끝나기 전
    종료 훅에서 ``aclose()``를 호출한다. 연결을 만든 뒤 다른 이벤트 루프나 fork된 자식
    프로세스에서 쓰면 ``RuntimeError``가 난다.
    """

    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._url = base_url.rstrip("/") + "/v1/data/mint/decision"
        self._timeout = timeout_seconds
        self._client: httpx.AsyncClient | None = None
        self._owner: tuple[int, asyncio.AbstractEventLoop] | None = None

    async def aclose(self) -> None:
        client, self._client, self._owner = self._client, None, None
        if client is not None:
            await client.aclose()

    def _http(self) -> httpx.AsyncClient:
        owner = (os.getpid(), asyncio.get_running_loop())
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
            self._owner = owner
        elif self._owner != owner:
            raise RuntimeError("OPAClient is bound to the process and event loop that created it")
        return self._client

    async def evaluate(
        self,
        policy_input: dict[str, Any],
        *,
        policy_version_id: str,
        bundle_sha256: str,
        budget_seconds: float | None = None,
    ) -> Evaluation:
        """``budget_seconds``는 이 호출에 남은 판정 예산이다(없으면 생성 시 값)."""
        if not has_only_policy_fields(policy_input):
            return Evaluation(failure=StageResult.stop(Reason.SCHEMA_ERROR))
        budget = self._timeout if budget_seconds is None else budget_seconds
        if budget <= 0:
            return Evaluation(failure=StageResult.stop(Reason.DEPENDENCY_FAILURE))
        deadline = asyncio.get_running_loop().time() + budget
        try:
            async with asyncio.timeout_at(deadline):
                client = self._http()
                for _ in range(2):
                    remaining = deadline - asyncio.get_running_loop().time()
                    response = await client.post(
                        self._url, json={"input": policy_input}, timeout=max(remaining, 0.001)
                    )
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
