"""통합 시험 전용 순차 가드레일. MINT 가드레일 앞에서 MCP 호출 인자를 바꾸는 두 방식을 재현한다.

LiteLLM 사용자가 직접 쓰는 사용자 정의 가드레일과 같은 형태이며 배포 이미지에는 들어가지 않는다.
"""

from litellm.integrations.custom_guardrail import CustomGuardrail


class RewriteModified(CustomGuardrail):
    """``text``가 ``orig``이면 ``modified_arguments``로 ``rewritten``을 돌려준다(공식 방식)."""

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        arguments = data.get("mcp_arguments") or {}
        if arguments.get("text") == "orig":
            data["modified_arguments"] = {**arguments, "text": "rewritten"}
        return data


class ReplaceArgumentsOnly(CustomGuardrail):
    """``mcp_arguments``만 바꾸고 ``modified_arguments``는 만들지 않는다."""

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        arguments = data.get("mcp_arguments") or {}
        if arguments.get("text") == "orig":
            data["mcp_arguments"] = {**arguments, "text": "replaced"}
        return data
