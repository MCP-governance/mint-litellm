"""도구 목록 발견 판정.

도구 목록을 읽을 때 LiteLLM이 도구마다 가드레일 사전 훅을 부른다. 도구 하나마다 스냅숏을 읽고
상태 집행과 거부·승인 단계만 적용하며, 숨길 도구는 훅이 예외를 던져 목록에서 빠진다. 숨김은
목록에만 영향이 있고 직접 호출은 호출 판정이 막는다.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mint_litellm.compat.litellm_1_106 import HookData
from mint_litellm.plugin.pipeline import PipelineDeps, build_request, evaluate_request
from mint_litellm.types.decision import OperationType, ResolvedServer
from mint_litellm.types.errors import MintError
from mint_litellm.types.identity import Principal


@dataclass(frozen=True)
class DiscoveryResult:
    show: bool
    error: MintError | None


async def evaluate_listed_tool(
    data: Mapping[str, Any],
    user_api_key_dict: Any,
    deps: PipelineDeps,
    *,
    hook_parser: Callable[[Mapping[str, Any]], HookData],
    resolver: Callable[[HookData], ResolvedServer],
    principal_factory: Callable[..., Principal],
) -> DiscoveryResult:
    request = build_request(
        data,
        user_api_key_dict,
        operation=OperationType.RUNTIME_DISCOVERY,
        instance_id=deps.config.instance_id,
        hook_parser=hook_parser,
        resolver=resolver,
        principal_factory=principal_factory,
    )
    outcome = await evaluate_request(request, deps)
    return DiscoveryResult(show=outcome.allowed, error=outcome.error)
