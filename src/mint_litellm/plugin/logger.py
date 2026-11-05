"""도구 호출 판정의 1차 집행점인 ``CustomLogger``.

``litellm_settings.callbacks``에는 모듈 수준 인스턴스를 적는다.
``mint_litellm.plugin.logger.mint_logger``는 처음 읽을 때 만들어진다.

로거의 사전 훅은 키·팀 메타데이터의 가드레일 제외 설정에 영향받지 않으므로 호출 판정은 여기서
한다. 가드레일은 같은 호출에 이 훅의 판정 표식이 있고 인자가 바뀌지 않았는지 확인하므로, MINT
가드레일이 이 호출에서 실행되지 않으면 판정 단계에서 호출을 막는다.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from mint_litellm.compat import litellm_1_106 as compat
from mint_litellm.plugin.result import observe_response, observe_unknown_outcome
from mint_litellm.plugin.selfcheck import PluginRuntime, get_runtime
from mint_litellm.types.decision import (
    MINT_CTX_KEY,
    MINT_DECISION_REF_KEY,
    MintContext,
    StageResult,
)
from mint_litellm.types.errors import error_for_reason
from mint_litellm.types.ids import new_uuid7
from mint_litellm.types.reason_codes import Reason

log = logging.getLogger("mint_litellm.logger")


def internal_block() -> HTTPException:
    """판정 코드 자체의 오류. 판정할 수 없으므로 차단한다."""
    reference = MintContext(invocation_id=new_uuid7(), attempt_id=1).decision_ref
    error = error_for_reason(Reason.SCHEMA_ERROR.value, reference)
    return HTTPException(status_code=403, detail=error.detail())


class MintLogger(CustomLogger):
    def __init__(self, runtime: PluginRuntime | None = None, **kwargs: Any) -> None:
        # LiteLLM은 생성자가 RuntimeError 밖의 예외를 던지면 기동을 계속하므로 모두 바꾼다.
        try:
            super().__init__(**kwargs)
            self._runtime = runtime if runtime is not None else get_runtime()
            self._runtime.logger = self
            self._runtime.ensure_started()
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"MintLogger could not start: {type(exc).__name__}") from exc

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type != compat.CALL_TOOL or not isinstance(data, dict):
            return None
        hook = compat.parse_hook_data(data)
        if not hook.is_synthesized:
            return None
        try:
            outcome = await self._runtime.judge_call(
                data, user_api_key_dict, stop=self._opted_out_stop(data)
            )
        except Exception as exc:
            log.error("도구 호출 판정 오류: %s", type(exc).__name__)
            raise internal_block() from exc
        details = compat.logging_details(hook.logging_obj)
        if details is not None:
            details[MINT_CTX_KEY] = outcome.context
        if not outcome.allowed:
            assert outcome.error is not None
            raise HTTPException(status_code=403, detail=outcome.error.detail())
        data[MINT_DECISION_REF_KEY] = outcome.snapshot.decision_ref
        return data

    def _opted_out_stop(self, data: dict) -> StageResult | None:
        """MINT 가드레일이 이 호출에서 실행되지 않으면 호출을 막는 정지. 확인하지 못해도 막는다.

        가드레일이 건너뛰어지면 판정 뒤에 바뀐 인자를 대조할 곳이 없다.
        """
        guardrail = self._runtime.guardrail
        if guardrail is None:
            return StageResult.stop(Reason.GUARDRAIL_OPTED_OUT, detail="guardrail not registered")
        try:
            if compat.guardrail_runs(guardrail, data):
                return None
        except Exception as exc:
            log.error("가드레일 실행 여부 확인 오류: %s", type(exc).__name__)
            return StageResult.stop(Reason.GUARDRAIL_OPTED_OUT, detail="guardrail check failed")
        return StageResult.stop(Reason.GUARDRAIL_OPTED_OUT)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        ctx = kwargs.get(MINT_CTX_KEY)
        if not isinstance(ctx, dict):
            return
        try:
            await observe_response(
                ctx,
                is_error=False,
                size_bytes=compat.tool_result_size(response_obj),
                deps=self._runtime.result_deps,
            )
        except Exception as exc:
            log.error("응답 관측 오류: %s", type(exc).__name__)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        ctx = kwargs.get(MINT_CTX_KEY)
        if not isinstance(ctx, dict):
            return
        exc = compat.failure_exception(kwargs)
        try:
            if compat.is_tool_result_error(exc):
                await observe_response(
                    ctx, is_error=True, size_bytes=None, deps=self._runtime.result_deps
                )
            else:
                await observe_unknown_outcome(
                    ctx,
                    exception_type=type(exc).__name__ if exc is not None else "unknown",
                    http_status=compat.exception_status(exc),
                    deps=self._runtime.result_deps,
                )
        except Exception as error:
            log.error("실패 관측 오류: %s", type(error).__name__)


_instance: MintLogger | None = None


def __getattr__(name: str) -> MintLogger:
    global _instance
    if name != "mint_logger":
        raise AttributeError(name)
    if _instance is None:
        _instance = MintLogger()
    return _instance
