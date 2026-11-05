"""LiteLLM 1.106.6의 실제 기반 클래스와 객체로 로거·가드레일 훅을 시험한다."""

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("litellm")
pytest.importorskip("fastapi")

from fastapi import HTTPException  # noqa: E402
from litellm.integrations.custom_guardrail import CustomGuardrail  # noqa: E402
from litellm.integrations.custom_logger import CustomLogger  # noqa: E402
from litellm.proxy._experimental.mcp_server.mcp_server_manager import (  # noqa: E402
    global_mcp_server_manager,
)
from litellm.proxy._types import UserAPIKeyAuth  # noqa: E402
from litellm.types.guardrails import GuardrailEventHooks  # noqa: E402
from litellm.types.mcp import MCPTransport  # noqa: E402
from litellm.types.mcp_server.mcp_server_manager import MCPServer  # noqa: E402
from plugin_helpers import (  # noqa: E402
    FakeSnapshots,
    FakeStore,
    approval,
    config,
    evaluator,
    snapshot,
)

from mint_litellm.plugin import logger as logger_module  # noqa: E402
from mint_litellm.plugin.buffer import EvidenceRecorder, LocalBuffer  # noqa: E402
from mint_litellm.plugin.guardrail import MintGuardrail  # noqa: E402
from mint_litellm.plugin.logger import MintLogger  # noqa: E402
from mint_litellm.plugin.selfcheck import PluginRuntime, worker_id  # noqa: E402
from mint_litellm.types.decision import (  # noqa: E402
    MINT_CTX_KEY,
    MINT_DECISION_REF_KEY,
    url_sha256,
)
from mint_litellm.types.errors import MintError  # noqa: E402

RAW_KEY = "sk-" + "t" * 40
URL = "https://mcp.example/api"


@pytest.fixture(autouse=True)
def isolated_callbacks(monkeypatch):
    """가드레일을 만들면 로거가 전역 콜백 목록에 올라가므로 시험마다 비운다."""
    import litellm

    monkeypatch.setattr(litellm, "callbacks", [])


@pytest.fixture
def registry(monkeypatch):
    """전역 서버 관리자에 서버 하나와 도구 경로를 임시로 올린다."""
    manager = global_mcp_server_manager
    server = MCPServer(
        server_id="srv-1", name="mslearn", alias="mslearn", url=URL, transport=MCPTransport.http
    )
    monkeypatch.setitem(manager.registry, "srv-1", server)
    monkeypatch.setitem(manager.tool_name_to_mcp_server_name_mapping, "search", "mslearn")
    return server


def build_runtime(tmp_path, snap=None, *, evaluate="allow", verified=True):
    store = FakeStore()
    recorder = EvidenceRecorder(
        store,
        LocalBuffer(tmp_path / "buf", 1024 * 1024),
        budget_seconds=1.0,
        instance_id="inst",
        stderr=io.StringIO(),
    )
    snapshots = FakeSnapshots(snap if snap is not None else snapshot())

    class Database:
        read_snapshot = staticmethod(snapshots.read_snapshot)

        async def read_epoch(self, instance_id):
            return 7

        async def has_negative_state(self, query):
            return False

    runtime = PluginRuntime(
        config=config(tmp_path),
        database=Database(),
        recorder=recorder,
        evaluate=evaluator() if evaluate == "allow" else evaluate,
    )
    runtime.verified = verified
    # 로거는 등록된 MINT 가드레일 인스턴스가 이 호출에서 실행되는지 묻는다.
    MintGuardrail(
        runtime=runtime,
        guardrail_name="mint",
        event_hook=["pre_mcp_call", "post_mcp_call"],
        default_on=True,
    )
    return runtime, store, snapshots


def server_snapshot(**override):
    args = {"server": snapshot().server}
    args.update(override)
    return snapshot(**args)


def logging_obj():
    return SimpleNamespace(litellm_call_id="call-1", model_call_details={})


def synthesized(obj, **override):
    data = {
        "litellm_logging_obj": obj,
        "mcp_arguments": {"q": "x"},
        "mcp_server_name": "mslearn",
        "mcp_tool_name": "search",
        "messages": [],
        "metadata": {},
        "model": "mcp-tool-call",
        "user_api_key_end_user_id": None,
        "user_api_key_hash": None,
        "user_api_key_request_route": None,
        "user_api_key_team_id": "t1",
        "user_api_key_user_id": "u1",
    }
    data.update(override)
    return data


def auth(**override):
    args = {"api_key": RAW_KEY, "user_id": "u1", "team_id": "t1"}
    args.update(override)
    return UserAPIKeyAuth(**args)


def run(coro):
    return asyncio.run(coro)


def make_logger(runtime):
    return MintLogger(runtime=runtime)


def pre(hook, data, call_type="call_mcp_tool", user=None):
    return run(
        hook.async_pre_call_hook(
            user_api_key_dict=user if user is not None else auth(),
            cache=None,
            data=data,
            call_type=call_type,
        )
    )


def blocked(call):
    with pytest.raises(HTTPException) as raised:
        call()
    assert raised.value.status_code == 403
    return MintError.from_detail(raised.value.detail)


def matching_snapshot():
    return snapshot(server=server_snapshot().server)


@pytest.fixture
def contract_snapshot(registry):
    server = registry
    return snapshot(
        server=snapshot().server.__class__(
            id="row-1",
            server_id="srv-1",
            status="approved",
            url_sha256=url_sha256(server.url),
            transport="http",
            auth_type="none",
            basis_allowlist_ids=(),
        ),
        approvals=(approval(),),
    )


class TestLogger:
    def test_it_is_a_custom_logger_whose_pre_call_hook_is_defined_on_the_class(self):
        assert issubclass(MintLogger, CustomLogger)
        assert "async_pre_call_hook" in vars(MintLogger)

    def test_an_allowed_call_gets_the_marker_and_a_context(self, tmp_path, contract_snapshot):
        runtime, store, _ = build_runtime(tmp_path, contract_snapshot)
        hook = make_logger(runtime)
        obj = logging_obj()
        data = synthesized(obj)
        result = pre(hook, data)
        assert result is data
        ref = data[MINT_DECISION_REF_KEY]
        ctx = obj.model_call_details[MINT_CTX_KEY]
        assert ctx["decision_ref"] == ref and ctx["allowed"] is True
        assert json.loads(json.dumps(ctx)) == ctx
        (event,) = store.of_kind("decision_evaluated")
        assert event.payload["enforced_action"] == "allow"
        assert runtime.logger is hook

    def test_a_blocked_call_raises_403_with_the_error_contract_and_a_denied_context(
        self, tmp_path, contract_snapshot
    ):
        runtime, store, _ = build_runtime(
            tmp_path, snapshot(server=contract_snapshot.server, approvals=())
        )
        hook = make_logger(runtime)
        obj = logging_obj()
        error = blocked(lambda: pre(hook, synthesized(obj)))
        assert (
            error.code == "MINT-APPROVAL-ABSENT" and error.dispatch_state.value == "not_dispatched"
        )
        assert error.reference == obj.model_call_details[MINT_CTX_KEY]["decision_ref"]
        assert obj.model_call_details[MINT_CTX_KEY]["allowed"] is False
        assert len(store.of_kind("decision_evaluated")) == 1

    def test_the_error_detail_is_a_json_string(self, tmp_path, contract_snapshot):
        runtime, _, _ = build_runtime(
            tmp_path, snapshot(server=contract_snapshot.server, approvals=())
        )
        with pytest.raises(HTTPException) as raised:
            pre(make_logger(runtime), synthesized(logging_obj()))
        detail = raised.value.detail
        assert isinstance(detail, str)
        assert set(json.loads(detail)) == {"code", "dispatch_state", "retry_safe", "reference"}

    def test_an_internal_block_uses_the_same_reference_form(self):
        from mint_litellm.plugin.logger import internal_block

        invocation, attempt = json.loads(internal_block().detail)["reference"].rsplit(":", 1)
        assert attempt == "1" and len(invocation) == 36

    def test_a_marker_cannot_be_set_by_the_caller_data(self, tmp_path, contract_snapshot):
        runtime, _, _ = build_runtime(
            tmp_path, snapshot(server=contract_snapshot.server, approvals=())
        )
        data = synthesized(logging_obj(), mint_decision_ref="forged")
        blocked(lambda: pre(make_logger(runtime), data))
        assert data[MINT_DECISION_REF_KEY] == "forged"

    def test_the_rest_request_body_stage_is_ignored(self, tmp_path):
        runtime, store, snapshots = build_runtime(tmp_path)
        body_stage = {"arguments": {}, "litellm_logging_obj": logging_obj(), "server_id": "x"}
        assert pre(make_logger(runtime), body_stage) is None
        assert store.events == {} and snapshots.queries == []

    @pytest.mark.parametrize("call_type", ["completion", "list_mcp_tools", "acompletion"])
    def test_other_call_types_are_ignored(self, tmp_path, call_type):
        runtime, store, snapshots = build_runtime(tmp_path)
        assert pre(make_logger(runtime), synthesized(logging_obj()), call_type) is None
        assert store.events == {} and snapshots.queries == []

    def test_an_unverified_runtime_blocks_every_call(self, tmp_path, contract_snapshot):
        runtime, store, snapshots = build_runtime(tmp_path, contract_snapshot, verified=False)
        error = blocked(lambda: pre(make_logger(runtime), synthesized(logging_obj())))
        assert error.code == "MINT-ENFORCEMENT-UNVERIFIED" and snapshots.queries == []
        assert len(store.of_kind("decision_evaluated")) == 1

    def test_the_master_key_is_forbidden(self, tmp_path, contract_snapshot):
        runtime, _, snapshots = build_runtime(tmp_path, contract_snapshot)
        master = UserAPIKeyAuth(api_key="litellm_proxy_master_key", user_id="default_user_id")
        error = blocked(lambda: pre(make_logger(runtime), synthesized(logging_obj()), user=master))
        assert error.code == "MINT-MASTER-KEY-FORBIDDEN" and snapshots.queries == []

    def test_an_unresolvable_tool_is_blocked_before_any_state_is_read(self, tmp_path, registry):
        runtime, _, snapshots = build_runtime(tmp_path)
        data = synthesized(logging_obj(), mcp_tool_name="unknown_tool")
        assert blocked(lambda: pre(make_logger(runtime), data)).code == "MINT-SERVER-UNRESOLVED"
        assert snapshots.queries == []

    def test_a_changed_server_url_is_an_identity_mismatch(
        self, tmp_path, contract_snapshot, registry
    ):
        registry.url = "https://moved.example/api"
        runtime, _, _ = build_runtime(tmp_path, contract_snapshot)
        error = blocked(lambda: pre(make_logger(runtime), synthesized(logging_obj())))
        assert error.code == "MINT-SERVER-IDENTITY-MISMATCH"

    def test_a_judgment_bug_blocks_instead_of_letting_the_call_through(
        self, tmp_path, contract_snapshot
    ):
        runtime, _, _ = build_runtime(tmp_path, contract_snapshot)

        async def explode(*args, **kwargs):
            raise AssertionError("bug")

        runtime.judge_call = explode
        assert blocked(lambda: pre(make_logger(runtime), synthesized(logging_obj()))).code == (
            "MINT-SCHEMA-ERROR"
        )

    def test_data_without_a_logging_object_is_still_judged(self, tmp_path, contract_snapshot):
        runtime, store, _ = build_runtime(tmp_path, contract_snapshot)
        data = synthesized(None)
        assert pre(make_logger(runtime), data) is data
        assert len(store.of_kind("decision_evaluated")) == 1


class TestResponseAndFailureEvents:
    def context(self, **override):
        ctx = {
            "invocation_id": "0192f7a0-7c1e-7a10-8000-0123456789ab",
            "attempt_id": 1,
            "decision_ref": "0192f7a0-7c1e-7a10-8000-0123456789ab:1",
            "allowed": True,
            "state_epoch": 7,
            "server_id": "srv-1",
            "tool": "search",
            "approval_id": "a1",
            "relation_id": "r1",
            "key_id": "k" * 64,
            "user_id": "u1",
            "team_id": "t1",
            "org_id": "org1",
        }
        ctx.update(override)
        return ctx

    def hook(self, tmp_path):
        runtime, store, _ = build_runtime(tmp_path)
        return make_logger(runtime), store

    def test_a_successful_result_is_recorded_with_its_size_only(self, tmp_path):
        hook, store = self.hook(tmp_path)
        result = SimpleNamespace(content=[SimpleNamespace(text="secret result text")])
        run(hook.async_log_success_event({MINT_CTX_KEY: self.context()}, result, None, None))
        (event,) = store.of_kind("call_response_received")
        assert event.payload["is_error"] is False
        assert event.payload["size_bytes"] == len("secret result text")
        assert "secret" not in json.dumps(event.payload)

    def test_a_tool_error_result_is_recorded_as_an_error_response(self, tmp_path):
        class MCPToolResultError(Exception):
            pass

        hook, store = self.hook(tmp_path)
        kwargs = {MINT_CTX_KEY: self.context(), "exception": MCPToolResultError("boom")}
        run(hook.async_log_failure_event(kwargs, None, None, None))
        assert store.of_kind("call_response_received")[0].payload["is_error"] is True
        assert store.of_kind("call_outcome_unknown") == []

    def test_a_timeout_failure_is_an_unknown_outcome(self, tmp_path):
        hook, store = self.hook(tmp_path)
        kwargs = {
            MINT_CTX_KEY: self.context(),
            "exception": HTTPException(504, {"error": "timeout"}),
        }
        run(hook.async_log_failure_event(kwargs, None, None, None))
        (event,) = store.of_kind("call_outcome_unknown")
        assert event.payload["cause"] == "timeout" and event.payload["http_status"] == 504
        assert event.payload["exception_type"] == "HTTPException"

    def test_the_failure_event_of_a_blocked_call_is_ignored(self, tmp_path):
        hook, store = self.hook(tmp_path)
        kwargs = {MINT_CTX_KEY: self.context(allowed=False), "exception": HTTPException(403, "x")}
        run(hook.async_log_failure_event(kwargs, None, None, None))
        assert store.events == {}

    def test_events_without_a_context_are_ignored(self, tmp_path):
        hook, store = self.hook(tmp_path)
        run(hook.async_log_success_event({}, None, None, None))
        run(hook.async_log_failure_event({"exception": ValueError()}, None, None, None))
        assert store.events == {}

    def test_an_observation_error_never_reaches_litellm(self, tmp_path):
        hook, store = self.hook(tmp_path)
        run(hook.async_log_success_event({MINT_CTX_KEY: {"allowed": True}}, None, None, None))
        assert store.events == {}


class TestGuardrail:
    def make(self, runtime):
        return MintGuardrail(
            runtime=runtime,
            guardrail_name="mint",
            event_hook=["pre_mcp_call", "post_mcp_call"],
            default_on=True,
            unreachable_fallback="fail_closed",
            version=None,
        )

    def test_it_is_a_custom_guardrail_registered_for_both_modes(self, tmp_path):
        runtime, _, _ = build_runtime(tmp_path)
        guardrail = self.make(runtime)
        assert isinstance(guardrail, CustomGuardrail)
        assert guardrail.get_supported_event_hooks() == [
            GuardrailEventHooks.pre_mcp_call,
            GuardrailEventHooks.post_mcp_call,
        ]
        assert guardrail.default_on is True and runtime.guardrail is guardrail
        assert "async_pre_call_hook" in vars(MintGuardrail)

    def test_a_call_without_the_marker_is_blocked_and_recorded(self, tmp_path, contract_snapshot):
        runtime, store, _ = build_runtime(tmp_path, contract_snapshot)
        guardrail = self.make(runtime)
        error = blocked(lambda: pre(guardrail, synthesized(logging_obj())))
        assert error.code == "MINT-ENFORCEMENT-UNVERIFIED"
        (event,) = store.of_kind("decision_evaluated")
        assert event.payload["stop"]["reason_code"] == "ENFORCEMENT_UNVERIFIED"

    def test_a_call_judged_by_the_logger_passes_the_guardrail(self, tmp_path, contract_snapshot):
        runtime, store, _ = build_runtime(tmp_path, contract_snapshot)
        guardrail, hook = self.make(runtime), make_logger(runtime)
        data = synthesized(logging_obj())
        pre(hook, data)
        assert pre(guardrail, data) is None
        assert len(store.of_kind("decision_evaluated")) == 1

    def test_a_marker_that_does_not_match_the_context_is_blocked(self, tmp_path, contract_snapshot):
        runtime, _, _ = build_runtime(tmp_path, contract_snapshot)
        guardrail = self.make(runtime)
        obj = logging_obj()
        data = synthesized(obj, mint_decision_ref="forged")
        obj.model_call_details[MINT_CTX_KEY] = {"decision_ref": "other", "allowed": True}
        assert blocked(lambda: pre(guardrail, data)).code == "MINT-ENFORCEMENT-UNVERIFIED"
        obj.model_call_details[MINT_CTX_KEY] = {"decision_ref": "forged", "allowed": False}
        assert blocked(lambda: pre(guardrail, data)).code == "MINT-ENFORCEMENT-UNVERIFIED"

    @pytest.mark.parametrize(
        "logging",
        [None, SimpleNamespace(litellm_call_id="c", model_call_details=None)],
        ids=["no-logging-object", "unreadable-details"],
    )
    def test_a_marker_without_a_readable_context_is_blocked(self, tmp_path, logging):
        runtime, _, _ = build_runtime(tmp_path)
        data = synthesized(logging, mint_decision_ref="x")
        assert blocked(lambda: pre(self.make(runtime), data)).code == "MINT-ENFORCEMENT-UNVERIFIED"

    def test_a_listed_tool_with_a_grant_stays_visible(self, tmp_path, contract_snapshot):
        runtime, store, _ = build_runtime(tmp_path, contract_snapshot)
        data = synthesized(None)
        assert pre(self.make(runtime), data, "list_mcp_tools") is None
        (event,) = store.of_kind("discovery_evaluated")
        assert (
            event.payload["operation"] == "runtime_discovery" and event.payload["attempt_id"] == 1
        )

    def test_a_listed_tool_without_a_grant_is_hidden_by_an_exception(self, tmp_path, registry):
        runtime, store, _ = build_runtime(tmp_path, snapshot(approvals=()))
        error = blocked(lambda: pre(self.make(runtime), synthesized(None), "list_mcp_tools"))
        assert error.code == "MINT-APPROVAL-ABSENT"
        assert len(store.of_kind("discovery_evaluated")) == 1

    def test_listing_resolves_servers_before_the_tool_table_exists(
        self, tmp_path, contract_snapshot
    ):
        global_mcp_server_manager.tool_name_to_mcp_server_name_mapping.pop("search", None)
        runtime, _, _ = build_runtime(tmp_path, contract_snapshot)
        assert pre(self.make(runtime), synthesized(None), "list_mcp_tools") is None

    def test_a_listing_is_hidden_while_unverified(self, tmp_path, contract_snapshot):
        runtime, _, _ = build_runtime(tmp_path, contract_snapshot, verified=False)
        error = blocked(lambda: pre(self.make(runtime), synthesized(None), "list_mcp_tools"))
        assert error.code == "MINT-ENFORCEMENT-UNVERIFIED"

    def test_two_registry_entries_with_the_same_name_hide_the_tool(
        self, tmp_path, contract_snapshot
    ):
        twin = MCPServer(
            server_id="srv-2", name="other", alias="mslearn", url=URL, transport=MCPTransport.http
        )
        global_mcp_server_manager.registry["srv-2"] = twin
        try:
            runtime, _, _ = build_runtime(tmp_path, contract_snapshot)
            error = blocked(lambda: pre(self.make(runtime), synthesized(None), "list_mcp_tools"))
        finally:
            global_mcp_server_manager.registry.pop("srv-2", None)
        assert error.code == "MINT-SERVER-IDENTITY-AMBIGUOUS"

    def test_other_call_types_pass_untouched(self, tmp_path):
        runtime, store, _ = build_runtime(tmp_path)
        assert pre(self.make(runtime), {"messages": []}, "completion") is None
        assert store.events == {}

    def test_a_judgment_bug_in_listing_hides_the_tool(self, tmp_path):
        runtime, _, _ = build_runtime(tmp_path)

        async def explode(*args, **kwargs):
            raise AssertionError("bug")

        runtime.judge_listed_tool = explode
        assert blocked(
            lambda: pre(self.make(runtime), synthesized(None), "list_mcp_tools")
        ).code == ("MINT-SCHEMA-ERROR")


class ArgumentsRewriter(CustomGuardrail):
    """도구 호출 인자를 바꿔 돌려주는 가드레일. 켜면 가드레일 해제 metadata도 함께 돌려준다."""

    def __init__(self, replacement, disable_guardrails=False, **kwargs):
        super().__init__(**kwargs)
        self.replacement = replacement
        self.disable_guardrails = disable_guardrails

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        changed = {**data, "modified_arguments": self.replacement}
        if self.disable_guardrails:
            metadata = dict(data.get("metadata") or {})
            key_metadata = dict(metadata.get("user_api_key_metadata") or {})
            key_metadata["disable_global_guardrails"] = True
            changed["metadata"] = {**metadata, "user_api_key_metadata": key_metadata}
        return changed


class Disabling(dict):
    """``ChainRunner.run_chain``의 순서 항목: 인자 교체와 함께 가드레일 해제 metadata를 돌려준다."""


class ChainRunner:
    SENT = {"q": "x"}
    OTHER = {"q": "y"}

    @staticmethod
    def rewriter(item, index):
        return ArgumentsRewriter(
            item,
            disable_guardrails=isinstance(item, Disabling),
            guardrail_name=f"rewriter{index}",
            event_hook="pre_mcp_call",
            default_on=True,
        )

    def build_rig(
        self,
        tmp_path,
        monkeypatch,
        snap,
        *,
        order=None,
        before=(),
        after=(),
        evaluate="allow",
        guardrail_instance="registered",
    ):
        """호출 사슬을 세운다.

        ``order``가 있으면 그 순서 그대로이다(문자열은 MINT 로거·가드레일, 그 밖의 값은 인자를
        바꾸는 가드레일). 없으면 LiteLLM이 하는 대로 ``before``의 콜백을 올린 뒤 가드레일을 만들어
        로거를 등록시키고 가드레일을 목록 끝에 붙이고 ``after``의 콜백을 붙인다.
        """
        import litellm
        from litellm.caching.caching import DualCache
        from litellm.proxy.utils import ProxyLogging

        runtime, store, _ = build_runtime(tmp_path, snap, evaluate=evaluate)
        logger = make_logger(runtime)
        monkeypatch.setattr(litellm, "callbacks", [])
        if order is None:
            litellm.callbacks.extend(self.rewriter(item, i) for i, item in enumerate(before))
        guardrail = MintGuardrail(
            runtime=runtime,
            guardrail_name="mint",
            event_hook=["pre_mcp_call", "post_mcp_call"],
            default_on=True,
        )
        if guardrail_instance == "missing":
            runtime.guardrail = None
        elif guardrail_instance == "failing":

            def explode(**kwargs):
                raise RuntimeError("boom")

            guardrail.should_run_guardrail = explode
        if order is None:
            litellm.callbacks.append(guardrail)
            litellm.callbacks.extend(
                self.rewriter(item, 100 + i) if not isinstance(item, CustomLogger) else item
                for i, item in enumerate(after)
            )
        else:
            chain = []
            for index, item in enumerate(order):
                if item == "logger":
                    chain.append(logger)
                elif item == "guardrail":
                    chain.append(guardrail)
                else:
                    chain.append(self.rewriter(item, index))
            litellm.callbacks[:] = chain
        proxy = ProxyLogging(user_api_key_cache=DualCache())
        return SimpleNamespace(
            runtime=runtime, store=store, logger=logger, guardrail=guardrail, proxy=proxy
        )

    def call(self, rig, *, empty_arguments=False, user=None):
        """사슬에 도구 호출 하나를 보낸다. 도구 호출 데이터는 LiteLLM이 키 객체에서 직접 만든다."""
        obj = logging_obj()
        start = {} if empty_arguments else dict(self.SENT)
        user = user if user is not None else auth()
        kwargs = {
            "name": "search",
            "arguments": start,
            "server_name": "mslearn",
            "mcp_rate_limit_server_name": "mslearn",
            "user_api_key_auth": user,
            "user_api_key_user_id": user.user_id,
            "user_api_key_team_id": user.team_id,
        }
        proxy = rig.proxy
        data = proxy.convert_mcp_to_llm_format(
            proxy.create_mcp_request_object_from_kwargs(kwargs), kwargs
        )
        data["litellm_logging_obj"] = obj
        result = SimpleNamespace(
            error=None, sent=None, obj=obj, store=rig.store, logger=rig.logger, runtime=rig.runtime
        )
        try:
            data = run(
                proxy.pre_call_hook(user_api_key_dict=user, data=data, call_type="call_mcp_tool")
            )
        except HTTPException as exc:
            result.error = MintError.from_detail(exc.detail)
            return result
        converted = proxy.convert_mcp_hook_response_to_kwargs(data, {"arguments": start})
        result.sent = converted["arguments"]
        return result

    def run_chain(
        self,
        tmp_path,
        monkeypatch,
        order,
        snap,
        evaluate="allow",
        empty_arguments=False,
        user=None,
        guardrail_instance="registered",
    ):
        """``order``대로 세운 사슬에 호출 하나를 보낸다."""
        rig = self.build_rig(
            tmp_path,
            monkeypatch,
            snap,
            order=order,
            evaluate=evaluate,
            guardrail_instance=guardrail_instance,
        )
        return self.call(rig, empty_arguments=empty_arguments, user=user)


class TestCallbackChain(ChainRunner):
    """LiteLLM의 사전 훅 순회를 그대로 거쳐, 판정한 인자와 전송될 인자를 대조한다."""

    def test_the_unchanged_arguments_pass_the_whole_chain(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        result = self.run_chain(tmp_path, monkeypatch, ["logger", "guardrail"], contract_snapshot)
        assert result.error is None and result.sent == self.SENT

    def test_arguments_changed_after_the_judgment_are_not_sent(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        order = ["logger", self.OTHER, "guardrail"]
        result = self.run_chain(tmp_path, monkeypatch, order, contract_snapshot)
        assert result.sent is None
        assert result.error.code == "MINT-ARGUMENTS-CHANGED"
        assert result.error.dispatch_state.value == "not_dispatched"
        assert result.error.retry_safe is False
        assert result.obj.model_call_details[MINT_CTX_KEY]["allowed"] is False

    def test_the_block_leaves_one_new_event_that_points_back_to_the_decision(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        order = ["logger", self.OTHER, "guardrail"]
        result = self.run_chain(tmp_path, monkeypatch, order, contract_snapshot)
        original = result.obj.model_call_details[MINT_CTX_KEY]["decision_ref"]
        decisions = result.store.of_kind("decision_evaluated")
        assert sorted(e.payload["enforced_action"] for e in decisions) == ["allow", "block"]
        (blocked_event,) = [e for e in decisions if e.payload["enforced_action"] == "block"]
        assert blocked_event.payload["stop"]["reason_code"] == "ARGUMENTS_CHANGED"
        assert blocked_event.payload["stop"]["detail"] == f"decision_ref={original}"
        assert len(result.store.events) == 2
        ctx = result.obj.model_call_details[MINT_CTX_KEY]
        assert str(blocked_event.invocation_id) == ctx["invocation_id"]
        assert blocked_event.attempt_id == ctx["attempt_id"] + 1
        assert result.error.reference == f"{blocked_event.invocation_id}:{blocked_event.attempt_id}"

    def test_a_change_that_was_blocked_leaves_no_unknown_outcome_event(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        order = ["logger", self.OTHER, "guardrail"]
        result = self.run_chain(tmp_path, monkeypatch, order, contract_snapshot)
        ctx = result.obj.model_call_details[MINT_CTX_KEY]
        failure = {MINT_CTX_KEY: ctx, "exception": ValueError()}
        run(result.logger.async_log_failure_event(failure, None, None, None))
        assert result.store.of_kind("call_outcome_unknown") == []

    @pytest.mark.parametrize("replacement", [{"q": "x"}, {}], ids=["same", "empty"])
    def test_a_replacement_that_changes_nothing_is_not_a_change(
        self, tmp_path, monkeypatch, contract_snapshot, replacement
    ):
        order = ["logger", replacement, "guardrail"]
        result = self.run_chain(tmp_path, monkeypatch, order, contract_snapshot)
        assert result.error is None and result.sent == self.SENT

    @pytest.mark.parametrize("replacement", [["x"], "text", 7], ids=["list", "text", "number"])
    def test_arguments_replaced_by_something_that_is_not_an_object_are_not_sent(
        self, tmp_path, monkeypatch, contract_snapshot, replacement
    ):
        snap = contract_snapshot
        order = ["logger", replacement, "guardrail"]
        result = self.run_chain(tmp_path, monkeypatch, order, snap, empty_arguments=True)
        assert result.sent is None and result.error.code == "MINT-ARGUMENTS-CHANGED"

    @pytest.mark.parametrize(
        ("approved", "expected"),
        [({"q": "x"}, "MINT-CALL-APPROVAL-REQUIRED"), ({"q": "y"}, None)],
        ids=["old-arguments-approved", "new-arguments-approved"],
    )
    def test_a_change_before_the_logger_is_what_gets_judged_and_sent(
        self, tmp_path, monkeypatch, contract_snapshot, approved, expected
    ):
        from plugin_helpers import FP_KEY, call_approval

        from mint_litellm.types.fingerprint import argument_fingerprint

        snap = snapshot(
            server=contract_snapshot.server,
            approvals=contract_snapshot.approvals,
            call_approvals=(call_approval(args_fp=argument_fingerprint(approved, FP_KEY)),),
        )
        evaluate = evaluator("RequireApproval", policy_ids=("R2",))
        order = [self.OTHER, "logger", "guardrail"]
        result = self.run_chain(tmp_path, monkeypatch, order, snap, evaluate)
        assert (result.error.code if result.error else None) == expected
        assert result.sent == (self.OTHER if expected is None else None)

    def test_an_approval_for_the_old_arguments_does_not_cover_the_new_ones(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        from plugin_helpers import FP_KEY, call_approval

        from mint_litellm.types.fingerprint import argument_fingerprint

        snap = snapshot(
            server=contract_snapshot.server,
            approvals=contract_snapshot.approvals,
            call_approvals=(call_approval(args_fp=argument_fingerprint(self.SENT, FP_KEY)),),
        )
        evaluate = evaluator("RequireApproval", policy_ids=("R2",))
        order = ["logger", self.OTHER, "guardrail"]
        result = self.run_chain(tmp_path, monkeypatch, order, snap, evaluate)
        assert result.sent is None and result.error.code == "MINT-ARGUMENTS-CHANGED"
        assert len(result.store.consumed) == 1

    def test_a_consumed_call_approval_is_recorded_as_consumed_without_dispatch(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        from plugin_helpers import FP_KEY, call_approval

        from mint_litellm.types.fingerprint import argument_fingerprint

        approval = call_approval(args_fp=argument_fingerprint(self.SENT, FP_KEY))
        snap = snapshot(
            server=contract_snapshot.server,
            approvals=contract_snapshot.approvals,
            call_approvals=(approval,),
        )
        evaluate = evaluator("RequireApproval", policy_ids=("R2",))
        order = ["logger", self.OTHER, "guardrail"]
        result = self.run_chain(tmp_path, monkeypatch, order, snap, evaluate)
        assert result.error.code == "MINT-ARGUMENTS-CHANGED" and result.sent is None
        (event,) = result.store.of_kind("call_approval_consumed_without_dispatch")
        decision_ref = result.obj.model_call_details[MINT_CTX_KEY]["decision_ref"]
        assert event.payload == {
            "reason": "ARGUMENTS_CHANGED",
            "decision_ref": decision_ref,
            "call_approval_id": str(approval.id),
        }
        assert event.source_key == (
            f"call_approval:{approval.id}:{approval.state_version}"
            ":call_approval_consumed_without_dispatch"
        )
        assert event.actor == "mint-plugin"
        ctx = result.obj.model_call_details[MINT_CTX_KEY]
        assert str(event.invocation_id) == ctx["invocation_id"]
        decisions = result.store.of_kind("decision_evaluated")
        assert {str(e.invocation_id) for e in decisions} == {ctx["invocation_id"]}
        text = event.to_json_line()
        assert approval.args_fp not in text and "arguments_digest" not in text
        assert len(result.store.events) == 3

    def test_without_a_consumed_call_approval_no_consumption_event_is_written(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        order = ["logger", self.OTHER, "guardrail"]
        result = self.run_chain(tmp_path, monkeypatch, order, contract_snapshot)
        assert result.store.of_kind("call_approval_consumed_without_dispatch") == []


class TestGuardrailOptOut(ChainRunner):
    """키·팀 설정으로 MINT 가드레일이 꺼진 호출은 로거가 판정 단계에서 막는다."""

    def test_the_data_the_logger_receives_carries_the_key_and_team_metadata(self):
        from litellm.caching.caching import DualCache
        from litellm.proxy.utils import ProxyLogging

        proxy = ProxyLogging(user_api_key_cache=DualCache())
        user = auth(
            metadata={"opted_out_global_guardrails": ["mint"]},
            team_metadata={"disable_global_guardrails": True},
        )
        kwargs = {"name": "search", "arguments": {}, "server_name": "mslearn"}
        kwargs["user_api_key_auth"] = user
        data = proxy.convert_mcp_to_llm_format(
            proxy.create_mcp_request_object_from_kwargs(kwargs), kwargs
        )
        assert data["metadata"]["user_api_key_metadata"] == {
            "opted_out_global_guardrails": ["mint"]
        }
        assert data["metadata"]["user_api_key_team_metadata"] == {"disable_global_guardrails": True}

    def test_a_key_that_opts_out_of_the_mint_guardrail_cannot_send_changed_arguments(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        user = auth(metadata={"opted_out_global_guardrails": ["mint"]})
        order = ["logger", self.OTHER, "guardrail"]
        result = self.run_chain(tmp_path, monkeypatch, order, contract_snapshot, user=user)
        assert result.sent is None
        assert result.error.code == "MINT-GUARDRAIL-OPTED-OUT"
        assert result.error.dispatch_state.value == "not_dispatched"
        assert result.error.retry_safe is False
        (event,) = result.store.events.values()
        assert event.kind.value == "decision_evaluated"
        assert event.payload["stop"]["reason_code"] == "GUARDRAIL_OPTED_OUT"
        assert result.store.consumed == []

    def test_opting_out_blocks_every_call_of_the_key(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        user = auth(metadata={"opted_out_global_guardrails": ["mint"]})
        result = self.run_chain(
            tmp_path, monkeypatch, ["logger", "guardrail"], contract_snapshot, user=user
        )
        assert result.error.code == "MINT-GUARDRAIL-OPTED-OUT" and result.sent is None

    def test_opting_out_of_another_guardrail_leaves_the_call_judged_normally(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        user = auth(metadata={"opted_out_global_guardrails": ["pii"]})
        order = ["logger", "guardrail"]
        result = self.run_chain(tmp_path, monkeypatch, order, contract_snapshot, user=user)
        assert result.error is None and result.sent == self.SENT
        order = ["logger", self.OTHER, "guardrail"]
        result = self.run_chain(
            tmp_path / "again", monkeypatch, order, contract_snapshot, user=user
        )
        assert result.error.code == "MINT-ARGUMENTS-CHANGED"

    def test_a_team_that_disables_global_guardrails_is_blocked(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        user = auth(team_metadata={"disable_global_guardrails": True})
        order = ["logger", "guardrail"]
        result = self.run_chain(tmp_path, monkeypatch, order, contract_snapshot, user=user)
        assert result.error.code == "MINT-GUARDRAIL-OPTED-OUT"

    @pytest.mark.parametrize("instance", ["missing", "failing"])
    def test_a_guardrail_that_cannot_be_asked_blocks_the_call(
        self, tmp_path, monkeypatch, contract_snapshot, instance
    ):
        order = ["logger", "guardrail"]
        result = self.run_chain(
            tmp_path, monkeypatch, order, contract_snapshot, guardrail_instance=instance
        )
        assert result.error.code == "MINT-GUARDRAIL-OPTED-OUT" and result.sent is None
        (event,) = result.store.events.values()
        assert event.payload["stop"]["detail"] in {
            "guardrail not registered",
            "guardrail check failed",
        }


class TestHooksThatAlsoSwitchTheGuardrailOff(ChainRunner):
    """인자와 함께 가드레일 해제 metadata를 돌려주는 콜백은 로거 앞에서만 설 수 있다."""

    def test_before_the_pair_the_final_input_is_what_gets_judged(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        rig = self.build_rig(
            tmp_path, monkeypatch, contract_snapshot, before=[Disabling(self.OTHER)]
        )
        result = self.call(rig)
        assert result.sent is None and result.error.code == "MINT-GUARDRAIL-OPTED-OUT"
        assert result.store.consumed == []

    def test_the_registration_puts_the_logger_directly_before_the_guardrail(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        import litellm

        self.build_rig(tmp_path, monkeypatch, contract_snapshot, before=[Disabling(self.OTHER)])
        names = [type(cb).__name__ for cb in litellm.callbacks]
        assert names == ["ArgumentsRewriter", "MintLogger", "MintGuardrail"]

    def test_a_hook_after_the_guardrail_cannot_pass_the_order_check(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        import litellm

        from mint_litellm.compat import litellm_1_106 as compat
        from mint_litellm.plugin.selfcheck import order_check

        rig = self.build_rig(
            tmp_path, monkeypatch, contract_snapshot, after=[Disabling(self.OTHER)]
        )
        check = order_check(
            litellm.callbacks,
            compat.is_pre_call_hook,
            logger=rig.logger,
            guardrail=rig.guardrail,
        )
        assert not check.ok and check.offenders == ("ArgumentsRewriter",)
        allowed = order_check(
            litellm.callbacks,
            compat.is_pre_call_hook,
            logger=rig.logger,
            guardrail=rig.guardrail,
            allowed=frozenset({"ArgumentsRewriter"}),
        )
        assert allowed.ok


class TestLoggerRegistration:
    def test_the_guardrail_registers_the_logger_right_before_itself(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        import litellm

        rig = ChainRunner().build_rig(tmp_path, monkeypatch, contract_snapshot)
        assert litellm.callbacks == [rig.logger, rig.guardrail]
        assert rig.logger.registered_by_guardrail is True

    def test_a_logger_listed_in_the_configuration_makes_the_startup_fail(
        self, tmp_path, monkeypatch
    ):
        import litellm

        runtime, _, _ = build_runtime(tmp_path)
        monkeypatch.setattr(litellm, "callbacks", [MintLogger(runtime=runtime)])
        with pytest.raises(RuntimeError, match="litellm_settings.callbacks"):
            MintGuardrail(
                runtime=runtime,
                guardrail_name="mint",
                event_hook=["pre_mcp_call", "post_mcp_call"],
                default_on=True,
            )

    def test_creating_the_guardrail_again_keeps_one_logger_directly_before_it(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        import litellm

        rig = ChainRunner().build_rig(tmp_path, monkeypatch, contract_snapshot)
        litellm.callbacks.remove(rig.guardrail)
        again = MintGuardrail(
            runtime=rig.runtime,
            guardrail_name="mint",
            event_hook=["pre_mcp_call", "post_mcp_call"],
            default_on=True,
        )
        litellm.callbacks.append(again)
        assert litellm.callbacks == [rig.logger, again]

    def test_the_default_configuration_with_only_the_internal_hooks_passes(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        import litellm

        from mint_litellm.compat import litellm_1_106 as compat
        from mint_litellm.plugin.selfcheck import order_check

        rig = ChainRunner().build_rig(tmp_path, monkeypatch, contract_snapshot)
        rig.proxy._init_litellm_callbacks(llm_router=None)  # 시작 때 LiteLLM이 내부 훅을 붙인다
        hooks = {
            type(cb).__name__
            for cb in litellm.callbacks
            if compat.is_pre_call_hook(cb) and cb not in (rig.logger, rig.guardrail)
        }
        assert hooks == compat.LITELLM_INTERNAL_PRE_HOOKS
        check = order_check(
            litellm.callbacks, compat.is_pre_call_hook, logger=rig.logger, guardrail=rig.guardrail
        )
        assert check.ok, check

    def test_a_lookalike_internal_hook_or_a_string_callback_after_the_guardrail_fails(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        import litellm
        from litellm.integrations.custom_logger import CustomLogger

        from mint_litellm.compat import litellm_1_106 as compat
        from mint_litellm.plugin.selfcheck import order_check

        class ResponsesIDSecurity(CustomLogger):
            async def async_pre_call_hook(self, *args, **kwargs):
                return None

        rig = ChainRunner().build_rig(tmp_path, monkeypatch, contract_snapshot)
        for extra in (ResponsesIDSecurity(), "banned_keywords"):
            litellm.callbacks.append(extra)
            check = order_check(
                litellm.callbacks,
                compat.is_pre_call_hook,
                logger=rig.logger,
                guardrail=rig.guardrail,
            )
            assert not check.ok and check.offenders == (compat.class_name(extra),)
            litellm.callbacks.remove(extra)


class TestCallbacksAddedAfterStartup(ChainRunner):
    def test_a_guardrail_added_later_blocks_the_next_call_with_one_event(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        import litellm

        from mint_litellm.plugin.selfcheck import callbacks_snapshot

        rig = self.build_rig(tmp_path, monkeypatch, contract_snapshot)
        assert self.call(rig).error is None
        rig.runtime.order_snapshot = callbacks_snapshot(litellm.callbacks)
        litellm.callbacks.append(self.rewriter(self.OTHER, 9))
        first = self.call(rig)
        second = self.call(rig)
        assert first.error.code == second.error.code == "MINT-ENFORCEMENT-UNVERIFIED"
        assert first.sent is None and second.sent is None
        (event,) = rig.store.of_kind("enforcement_unverified")
        assert event.payload == {
            "worker_id": worker_id(),
            "cause": "callbacks_changed",
            "hooks": ["ArgumentsRewriter"],
        }


class TestOrderRestoredAfterStartup(ChainRunner):
    def test_removing_the_late_guardrail_makes_the_next_call_pass_and_records_it_once(
        self, tmp_path, monkeypatch, contract_snapshot
    ):
        import litellm

        from mint_litellm.plugin.selfcheck import callbacks_snapshot

        rig = self.build_rig(tmp_path, monkeypatch, contract_snapshot)
        rig.runtime.order_snapshot = callbacks_snapshot(litellm.callbacks)
        late = self.rewriter(self.OTHER, 9)
        litellm.callbacks.append(late)
        assert self.call(rig).error.code == "MINT-ENFORCEMENT-UNVERIFIED"
        litellm.callbacks.remove(late)
        for _ in range(3):
            result = self.call(rig)
            assert result.error is None and result.sent == self.SENT
        assert len(rig.store.of_kind("enforcement_unverified")) == 1
        (event,) = rig.store.of_kind("enforcement_verified")
        assert event.payload["allowed_hooks_used"] == []
        assert event.payload["worker_id"] == worker_id()


class TestConstructors:
    def test_a_missing_configuration_raises_runtime_error_only(self, monkeypatch):
        for name in ("MINT_INSTANCE_ID", "MINT_DATABASE_URL"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr("mint_litellm.plugin.selfcheck._runtime", None)
        with pytest.raises(RuntimeError):
            MintLogger()
        with pytest.raises(RuntimeError):
            MintGuardrail(guardrail_name="mint", event_hook=["pre_mcp_call"], default_on=True)

    def test_unexpected_errors_are_converted_to_runtime_error(self):
        class Broken:
            def __setattr__(self, name, value):
                raise ValueError("no")

            def ensure_started(self):
                pass

        with pytest.raises(RuntimeError) as raised:
            MintLogger(runtime=Broken())
        assert not isinstance(raised.value, ValueError)
        with pytest.raises(RuntimeError):
            MintGuardrail(runtime=Broken(), guardrail_name="mint", default_on=True)

    def test_a_guardrail_with_an_unsupported_hook_list_does_not_raise_value_error(self, tmp_path):
        runtime, _, _ = build_runtime(tmp_path)
        MintGuardrail(
            runtime=runtime, guardrail_name="mint", event_hook=["pre_call"], default_on=True
        )

    def test_the_module_level_logger_is_created_once_on_first_use(self, tmp_path, monkeypatch):
        runtime, _, _ = build_runtime(tmp_path)
        monkeypatch.setattr(logger_module, "_instance", None)
        monkeypatch.setattr(logger_module, "get_runtime", lambda: runtime)
        first = logger_module.mint_logger
        assert isinstance(first, MintLogger) and logger_module.mint_logger is first
        with pytest.raises(AttributeError):
            _ = logger_module.something_else
        assert runtime.logger is first
