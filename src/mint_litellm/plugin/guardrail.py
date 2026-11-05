"""도구 목록 발견 처리와 호출 판정의 안전망인 ``CustomGuardrail``.

``guardrails`` 설정에서 ``pre_mcp_call``과 ``post_mcp_call`` 두 모드, ``default_on: true``로
등록한다. 도구 호출 판정은 로거가 하며, 가드레일은 같은 호출에 로거의 판정 표식이 없을 때만
호출을 차단한다. 도구 목록은 도구마다 이 훅이 불리고, 예외를 던진 도구는 목록에서 빠진다.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.types.guardrails import GuardrailEventHooks

from mint_litellm.compat import litellm_1_106 as compat
from mint_litellm.plugin.logger import internal_block
from mint_litellm.plugin.pipeline import arguments_digest
from mint_litellm.plugin.result import record_consumed_without_dispatch
from mint_litellm.plugin.selfcheck import PluginRuntime, get_runtime
from mint_litellm.types.decision import MINT_CTX_KEY, MINT_DECISION_REF_KEY, StageResult
from mint_litellm.types.reason_codes import Reason

log = logging.getLogger("mint_litellm.guardrail")


class MintGuardrail(CustomGuardrail):
    def __init__(self, runtime: PluginRuntime | None = None, **kwargs: Any) -> None:
        # LiteLLM은 생성자가 RuntimeError 밖의 예외를 던지면 가드레일 없이 기동하므로 모두 바꾼다.
        try:
            super().__init__(**kwargs)
            self._runtime = runtime if runtime is not None else get_runtime()
            self._runtime.guardrail = self
            self._runtime.ensure_started()
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"MintGuardrail could not start: {type(exc).__name__}") from exc

    def get_supported_event_hooks(self):
        return [GuardrailEventHooks.pre_mcp_call, GuardrailEventHooks.post_mcp_call]

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type == compat.LIST_TOOLS:
            return await self._discover(user_api_key_dict, data)
        if call_type == compat.CALL_TOOL and isinstance(data, dict):
            return await self._backstop(user_api_key_dict, data)
        return None

    async def _discover(self, user_api_key_dict, data):
        try:
            result = await self._runtime.judge_listed_tool(data, user_api_key_dict)
        except Exception as exc:
            log.error("도구 목록 판정 오류: %s", type(exc).__name__)
            raise internal_block() from exc
        if not result.show:
            assert result.error is not None
            raise HTTPException(status_code=403, detail=result.error.detail())
        return None

    async def _backstop(self, user_api_key_dict, data):
        ref = data.get(MINT_DECISION_REF_KEY)
        state, ctx = self._judged_state(data, ref)
        if state == "current":
            return None
        stop = None
        if state == "changed":
            stop = self._changed_stop(ctx)
        try:
            outcome = await self._runtime.judge_call(
                data, user_api_key_dict, unverified=stop is None, stop=stop
            )
        except Exception as exc:
            log.error("안전망 판정 오류: %s", type(exc).__name__)
            raise internal_block() from exc
        if state == "changed":
            await self._record_consumption(ctx)
        assert outcome.error is not None
        raise HTTPException(status_code=403, detail=outcome.error.detail())

    @staticmethod
    def _changed_stop(ctx: dict) -> StageResult:
        detail = f"decision_ref={ctx.get('decision_ref')}"
        consumed = ctx.get("consumed_call_approval")
        if isinstance(consumed, dict):
            detail += f"; call_approval_id={consumed.get('id')}"
        return StageResult.stop(Reason.ARGUMENTS_CHANGED, detail=detail[:200])

    async def _record_consumption(self, ctx: dict) -> None:
        try:
            await record_consumed_without_dispatch(ctx, self._runtime.result_deps)
        except Exception as exc:
            log.error("승인 소비 기록 오류: %s", type(exc).__name__)

    def _judged_state(self, data: dict, ref: Any) -> tuple[str, dict]:
        """표식이 이 호출의 로거 컨텍스트와 같은 판정을 가리키는지, 그 뒤 인자가 바뀌었는지.

        ``current``는 판정과 같은 인자, ``unjudged``는 판정 표식이 없거나 다른 판정,
        ``changed``는 판정한 뒤 인자가 바뀐 경우이다.
        """
        if not isinstance(ref, str):
            return "unjudged", {}
        details = compat.logging_details(data.get("litellm_logging_obj"))
        if details is None:
            return "unjudged", {}
        ctx = details.get(MINT_CTX_KEY)
        if not (
            isinstance(ctx, dict) and ctx.get("decision_ref") == ref and ctx.get("allowed") is True
        ):
            return "unjudged", {}
        # 로거가 판정한 뒤 앞선 훅이 인자를 바꿨으면 그 판정과 승인은 전송될 인자의 것이 아니다.
        digest = ctx.get("arguments_digest")
        # 인자를 객체로 읽지 못하면 판정한 인자와 같다고 할 수 없다.
        arguments = compat.effective_arguments(data)
        key = self._runtime.config.fingerprint_key
        current = arguments_digest(arguments, key) if arguments is not None else None
        if isinstance(digest, str) and digest == current:
            return "current", ctx
        details[MINT_CTX_KEY] = {**ctx, "allowed": False}
        return "changed", ctx
