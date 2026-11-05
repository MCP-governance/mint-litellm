"""기동 자가 점검과 플러그인 실행 상태.

플러그인은 ``unverified`` 상태로 시작해 모든 MCP 호출을 차단하고, LiteLLM 앱이 기동을 마친 뒤
워커마다 8개 항목을 프로세스 안에서 확인한다. 모두 통과해야 ``verified``가 된다. 점검이
실패하면 사건을 남기고(``MINT_REQUIRED``가 참이면) 프로세스를 멈춘다. LiteLLM의 키나 관리
API는 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import multiprocessing
import os
import signal
import time
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mint_litellm.compat import litellm_1_106 as compat
from mint_litellm.compat.litellm_1_106 import RegistryEntry
from mint_litellm.plugin.buffer import EvidenceRecorder, LocalBuffer, SystemEventKeys
from mint_litellm.plugin.config import PluginConfig
from mint_litellm.plugin.discovery import DiscoveryResult, evaluate_listed_tool
from mint_litellm.plugin.pipeline import (
    Outcome,
    PipelineDeps,
    PolicyEvaluate,
    build_request,
    evaluate_request,
)
from mint_litellm.plugin.policy.opa_client import Evaluation, OPAClient
from mint_litellm.plugin.result import ResultDeps
from mint_litellm.plugin.snapshot import PluginDatabase, RegisteredServer
from mint_litellm.types.decision import OperationType, StageResult
from mint_litellm.types.events import Event, EventKind
from mint_litellm.types.settings import SELF_CHECK_DESCRIPTIONS, SelfCheckItem

log = logging.getLogger("mint_litellm.selfcheck")

REQUIRED_MODES = ("pre_mcp_call", "post_mcp_call")
# 통제 대상 서버 상태. 그 밖의 상태 서버는 LiteLLM에 없어도 기동을 막지 않는다.
CONTROLLED_SERVER_STATUSES = frozenset({"approved", "suspended"})
CONTRACT_FIELDS = ("url_sha256", "transport", "auth_type")


@dataclass(frozen=True)
class CheckResult:
    item: SelfCheckItem
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class LiteLLMView:
    """점검 시점에 프로세스 안에서 읽은 LiteLLM 상태."""

    callbacks: Sequence[Any]
    general_settings: Mapping[str, Any]
    strict_guardrail_modes: bool
    store_prompts_in_spend_logs: bool
    user_url_validation: bool
    registry: Sequence[RegistryEntry]
    missing_symbols: Sequence[str]


def collect_view(env: Mapping[str, str] | None = None) -> LiteLLMView:
    missing = compat.missing_symbols()
    if missing:
        return LiteLLMView((), {}, True, False, True, (), missing)
    return LiteLLMView(
        callbacks=compat.litellm_callbacks(),
        general_settings=compat.general_settings(),
        strict_guardrail_modes=compat.strict_guardrail_modes_enabled(
            os.environ if env is None else env
        ),
        store_prompts_in_spend_logs=compat.spend_logs_store_prompts(),
        user_url_validation=compat.litellm_attribute("user_url_validation", True) is not False,
        registry=compat.registry_entries(),
        missing_symbols=(),
    )


def _hook_names(event_hook: Any) -> set[str]:
    if event_hook is None:
        return set()
    items = event_hook if isinstance(event_hook, (list, tuple, set)) else [event_hook]
    return {str(getattr(i, "value", i)) for i in items}


def _is_guardrail(callback: Any) -> bool:
    return hasattr(callback, "guardrail_name") and hasattr(callback, "event_hook")


def _runs_before_tool_call(callback: Any) -> bool:
    event_hook = getattr(callback, "event_hook", None)
    return event_hook is None or "pre_mcp_call" in _hook_names(event_hook)


def check_items(
    view: LiteLLMView,
    *,
    logger: Any,
    guardrail: Any,
    controlled: Sequence[RegisteredServer] | None,
    fingerprint_key_set: bool = True,
) -> list[CheckResult]:
    """8개 항목을 확인한다. ``controlled``가 ``None``이면 MINT DB를 읽지 못한 것이다."""
    results: list[CheckResult] = []

    def add(item: SelfCheckItem, ok: bool, detail: str = "") -> None:
        results.append(CheckResult(item, ok, "" if ok else detail))

    callbacks = list(view.callbacks)
    positions = {id(cb): i for i, cb in enumerate(callbacks)}

    add(
        SelfCheckItem.CALLBACK_REGISTERED,
        logger is not None and id(logger) in positions,
        "logger is not registered in litellm_settings.callbacks",
    )

    modes = _hook_names(getattr(guardrail, "event_hook", None)) if guardrail else set()
    add(
        SelfCheckItem.GUARDRAIL_MODES,
        guardrail is not None
        and id(guardrail) in positions
        and all(m in modes for m in REQUIRED_MODES)
        and getattr(guardrail, "default_on", False) is True,
        "guardrail is missing, lacks a mode, or is not default_on",
    )

    order_ok = False
    if guardrail is not None and logger is not None:
        g_pos, l_pos = positions.get(id(guardrail)), positions.get(id(logger))
        later = [
            cb
            for i, cb in enumerate(callbacks)
            if g_pos is not None and i > g_pos and _is_guardrail(cb) and _runs_before_tool_call(cb)
        ]
        order_ok = (
            g_pos is not None
            and l_pos is not None
            and l_pos < g_pos
            and not later
            and getattr(guardrail, "run_in_parallel", False) is False
        )
    add(
        SelfCheckItem.GUARDRAIL_ORDER,
        order_ok,
        "MINT guardrail must follow the logger and be the last pre-call guardrail",
    )

    by_id = {entry.server_id: entry for entry in view.registry}
    if controlled is None:
        add(SelfCheckItem.KEY_ACCESS_STRICT, False, "MINT database unavailable")
        add(SelfCheckItem.SERVER_REGISTRY_MATCH, False, "MINT database unavailable")
    else:
        active = [s for s in controlled if s.status in CONTROLLED_SERVER_STATUSES]
        open_servers = [
            s.server_id for s in active if getattr(by_id.get(s.server_id), "allow_all_keys", False)
        ]
        strict = view.general_settings.get("require_key_mcp_access_defined") is True
        add(
            SelfCheckItem.KEY_ACCESS_STRICT,
            strict and not open_servers,
            "require_key_mcp_access_defined must be true"
            if not strict
            else f"allow_all_keys is set on {', '.join(open_servers[:5])}",
        )
        absent = [s.server_id for s in active if s.server_id not in by_id]
        add(
            SelfCheckItem.SERVER_REGISTRY_MATCH,
            not absent,
            f"not in the registry: {', '.join(absent[:5])}",
        )

    owners: dict[str, set[str]] = {}
    for entry in view.registry:
        for name in entry.names:
            owners.setdefault(name.replace(" ", "_"), set()).add(entry.server_id)
    clashes = sorted(name for name, ids in owners.items() if len(ids) > 1)
    add(
        SelfCheckItem.NAME_ALIAS_UNIQUE,
        not clashes,
        f"names shared by several servers: {', '.join(clashes[:5])}",
    )

    add(
        SelfCheckItem.COMPAT_SYMBOLS,
        not view.missing_symbols,
        f"missing: {', '.join(view.missing_symbols[:5])}",
    )

    problems = []
    if not fingerprint_key_set:
        problems.append("MINT_FINGERPRINT_KEY is not set")
    if not view.strict_guardrail_modes:
        problems.append("LITELLM_STRICT_GUARDRAIL_MODES=false")
    if view.store_prompts_in_spend_logs:
        problems.append("store_prompts_in_spend_logs=true")
    if not view.user_url_validation:
        problems.append("user_url_validation=false")
    supported = view.general_settings.get("supported_db_objects")
    if isinstance(supported, list) and "mcp" not in [str(s) for s in supported]:
        problems.append("supported_db_objects lacks mcp")
    add(SelfCheckItem.REQUIRED_SETTINGS, not problems, "; ".join(problems))
    return results


def contract_mismatches(
    view: LiteLLMView, controlled: Sequence[RegisteredServer]
) -> list[tuple[str, str]]:
    """레지스트리에 있지만 승인된 계약 값과 다른 통제 서버의 (서버, 항목). 기동을 막지 않는다."""
    by_id = {entry.server_id: entry for entry in view.registry}
    found = []
    for server in controlled:
        entry = by_id.get(server.server_id)
        if server.status not in CONTROLLED_SERVER_STATUSES or entry is None:
            continue
        found.extend(
            (server.server_id, name)
            for name in CONTRACT_FIELDS
            if getattr(entry, name) != getattr(server, name)
        )
    return found


def terminate_process() -> None:
    """프록시를 멈춘다. 워커이면 감독 프로세스에도 종료를 알려 컨테이너 재시작 정책이 받게 한다."""
    parent = os.getppid()
    if multiprocessing.parent_process() is not None or parent > 1:
        with contextlib.suppress(OSError):
            os.kill(parent, signal.SIGTERM)
    os._exit(3)


async def wait_for_readiness(url: str, timeout: float) -> bool:
    def probe() -> bool:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                return response.status == 200
        except Exception:
            return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await asyncio.to_thread(probe):
            return True
        await asyncio.sleep(0.25)
    return False


@dataclass
class PluginRuntime:
    """한 워커 프로세스의 플러그인 상태. 로거와 가드레일이 공유한다."""

    config: PluginConfig
    database: PluginDatabase
    recorder: EvidenceRecorder
    evaluate: PolicyEvaluate | None
    view_collector: Callable[[], LiteLLMView] = collect_view
    terminate: Callable[[], None] = terminate_process
    readiness: Callable[[str, float], Awaitable[bool]] = wait_for_readiness
    retry_seconds: float = 1.0
    recheck_seconds: float = 30.0
    verified: bool = False
    logger: Any = None
    guardrail: Any = None
    last_results: list[CheckResult] = field(default_factory=list)
    last_mismatches: list[tuple[str, str]] = field(default_factory=list)
    keys: SystemEventKeys = field(default_factory=SystemEventKeys, repr=False)
    _reported: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def deps(self) -> PipelineDeps:
        return PipelineDeps(
            config=self.config,
            snapshots=self.database,
            recorder=self.recorder,
            evaluate=self.evaluate,
            verified=lambda: self.verified,
        )

    @property
    def result_deps(self) -> ResultDeps:
        return ResultDeps(config=self.config, database=self.database, recorder=self.recorder)

    async def judge_call(
        self,
        data: Mapping[str, Any],
        auth: Any,
        *,
        unverified: bool = False,
        stop: StageResult | None = None,
        attempt: tuple[UUID, int] | None = None,
    ):
        deps = self.deps if not unverified else _unverified(self.deps)
        request = build_request(
            data,
            auth,
            operation=OperationType.TOOL_CALL,
            instance_id=self.config.instance_id,
            hook_parser=compat.parse_hook_data,
            resolver=compat.resolve_server,
            principal_factory=compat.principal_from_auth,
        )
        if stop is not None:
            request = replace(request, forced_stop=stop)
        if attempt is not None:
            request = replace(request, invocation_id=attempt[0], attempt_id=attempt[1])
        outcome: Outcome = await evaluate_request(request, deps)
        return outcome

    async def judge_listed_tool(self, data: Mapping[str, Any], auth: Any) -> DiscoveryResult:
        return await evaluate_listed_tool(
            data,
            auth,
            self.deps,
            hook_parser=compat.parse_hook_data,
            resolver=compat.resolve_listed_server,
            principal_factory=compat.principal_from_auth,
        )

    async def run_checks(self) -> list[CheckResult]:
        try:
            controlled: Sequence[RegisteredServer] | None = await asyncio.wait_for(
                self.database.registered_servers(self.config.instance_id),
                self.config.decision_budget_seconds * 10,
            )
        except Exception:
            controlled = None
        view = self.view_collector()
        results = check_items(
            view,
            logger=self.logger,
            guardrail=self.guardrail,
            controlled=controlled,
            fingerprint_key_set=self.config.fingerprint_key is not None,
        )
        self.last_results = results
        self.last_mismatches = contract_mismatches(view, controlled or [])
        return results

    async def verify(self) -> bool:
        results = await self.run_checks()
        self.verified = all(r.ok for r in results)
        return self.verified

    def ensure_started(self) -> None:
        """실행 중인 이벤트 루프가 있으면 기동 후 점검을 예약한다(워커마다 한 번)."""
        if self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._startup())

    async def _startup(self) -> None:
        self.keys = SystemEventKeys()
        grace = self.config.selfcheck_grace_seconds
        url = self.config.readiness_url or (
            f"http://localhost:{os.environ.get('PORT', '4000')}/health/readiness"
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace
        try:
            error: str | None = "readiness_timeout"
            ready = False
            # 준비 신호와 8항목은 하나의 외부 시간 제한 안에서 모두 통과해야 한다. 다른 워커가 낸
            # 준비 신호로 시작했다면 이 워커의 레지스트리가 아직 비어 있을 수 있어 다시 확인한다.
            try:
                async with asyncio.timeout_at(deadline):
                    ready = await self.readiness(url, grace)
                    if ready:
                        error = None
                        while not await self.verify():
                            await asyncio.sleep(self.retry_seconds)
            except TimeoutError:
                pass
            if ready:
                if self.verified and loop.time() < deadline:
                    log.info("MINT 자가 점검 통과")
                    await self._report_mismatches()
                    return
                # 시간 제한이 지난 뒤에 끝난 점검은 통과로 치지 않는다.
                self.verified = False
                if not any(not r.ok for r in self.last_results):
                    error = "startup_timeout"
                await self._report_mismatches()
            await self._fail(error=error)
            if self.config.required:
                self.terminate()
                return
            while not await self.verify():
                await asyncio.sleep(self.recheck_seconds)
        except Exception as exc:
            log.error("자가 점검 실행 오류: %s", type(exc).__name__)
            await self._fail(error=type(exc).__name__)
            if self.config.required:
                self.terminate()

    async def _report_mismatches(self) -> None:
        """승인된 계약과 다른 서버마다 사건을 한 건 남긴다. 그 서버의 호출은 판정에서 차단된다."""
        by_server: dict[str, list[str]] = {}
        for server_id, name in self.last_mismatches:
            by_server.setdefault(server_id, []).append(name)
        for server_id, names in by_server.items():
            fields = tuple(names)
            if self._reported.get(server_id) == fields:
                continue
            self._reported[server_id] = fields
            kind = EventKind.SERVER_IDENTITY_MISMATCH_OBSERVED
            await self.recorder.record(
                Event(
                    instance_id=self.config.instance_id,
                    inserted_at=datetime.now(UTC),
                    kind=kind,
                    source_key=self.keys.next(kind),
                    payload={"server_id": server_id, "fields": list(fields)},
                )
            )

    async def _fail(self, error: str | None = None) -> None:
        failed = [
            {"item": int(r.item), "name": r.item.name, "detail": r.detail}
            for r in self.last_results
            if not r.ok
        ]
        payload: dict[str, Any] = {"failed": failed}
        if error is not None:
            payload["error"] = error
        event = Event(
            instance_id=self.config.instance_id,
            inserted_at=datetime.now(UTC),
            kind=EventKind.ENFORCEMENT_UNVERIFIED,
            source_key=self.keys.next(EventKind.ENFORCEMENT_UNVERIFIED),
            payload=payload,
        )
        await self.recorder.record(event)
        log.error(
            "MINT 자가 점검 실패: %s",
            ", ".join(f"{SELF_CHECK_DESCRIPTIONS[r.item]}" for r in self.last_results if not r.ok)
            or (error or "unknown"),
        )


def _unverified(deps: PipelineDeps) -> PipelineDeps:
    return PipelineDeps(
        config=deps.config,
        snapshots=deps.snapshots,
        recorder=deps.recorder,
        evaluate=deps.evaluate,
        verified=lambda: False,
        clock=deps.clock,
    )


class PolicyEngine:
    """OPA 클라이언트를 이벤트 루프마다 하나씩 만들어 쓰고, 그 루프가 끝나면 닫는다.

    클라이언트는 만든 프로세스·루프 밖에서 쓰면 ``RuntimeError``를 내므로, 프로세스나 루프가
    바뀌면 새로 만든다. 클라이언트를 닫는 일은 그 루프에 남겨 둔 작업이 맡는다. 루프가 끝나며
    남은 작업을 취소할 때 그 작업이 연결을 닫는다.
    """

    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self._base_url = base_url
        self._timeout = timeout_seconds
        self._client: OPAClient | None = None
        self._owner: tuple[int, asyncio.AbstractEventLoop] | None = None
        self._guard: asyncio.Task[None] | None = None

    async def evaluate(
        self,
        policy_input: dict[str, Any],
        *,
        policy_version_id: str,
        bundle_sha256: str,
        budget_seconds: float | None = None,
    ) -> Evaluation:
        loop = asyncio.get_running_loop()
        owner = (os.getpid(), loop)
        if self._client is None or self._owner != owner:
            client = OPAClient(self._base_url, self._timeout)
            self._client, self._owner = client, owner
            self._guard = loop.create_task(self._close_with_loop(client))
        return await self._client.evaluate(
            policy_input,
            policy_version_id=policy_version_id,
            bundle_sha256=bundle_sha256,
            budget_seconds=budget_seconds,
        )

    @staticmethod
    async def _close_with_loop(client: OPAClient) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            await client.aclose()

    async def aclose(self) -> None:
        client, guard = self._client, self._guard
        self._client = self._owner = self._guard = None
        if guard is not None:
            guard.cancel()
        if client is not None:
            await client.aclose()


_runtime: PluginRuntime | None = None


def get_runtime(env: Mapping[str, str] | None = None) -> PluginRuntime:
    """프로세스 전역 실행 상태. 처음 부를 때 환경 변수에서 설정을 읽는다."""
    global _runtime
    if _runtime is None:
        config = PluginConfig.from_env(os.environ if env is None else env)
        database = PluginDatabase(
            config.database_url,
            max_size=config.db_pool_size,
            acquire_timeout=config.decision_budget_seconds,
        )
        recorder = EvidenceRecorder(
            database,
            LocalBuffer(config.buffer_dir, config.buffer_max_bytes),
            budget_seconds=config.decision_budget_seconds,
            instance_id=config.instance_id,
        )
        _runtime = PluginRuntime(
            config=config,
            database=database,
            recorder=recorder,
            evaluate=PolicyEngine(config.opa_url, config.decision_budget_seconds).evaluate,
        )
    return _runtime
