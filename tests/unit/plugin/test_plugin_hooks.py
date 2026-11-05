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
from mint_litellm.plugin.selfcheck import PluginRuntime  # noqa: E402
from mint_litellm.types.decision import (  # noqa: E402
    MINT_CTX_KEY,
    MINT_DECISION_REF_KEY,
    url_sha256,
)
from mint_litellm.types.errors import MintError  # noqa: E402

RAW_KEY = "sk-" + "t" * 40
URL = "https://mcp.example/api"


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

    def test_a_marker_without_a_logging_object_is_accepted(self, tmp_path):
        runtime, _, _ = build_runtime(tmp_path)
        assert pre(self.make(runtime), synthesized(None, mint_decision_ref="x")) is None

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
    """도구 호출 인자를 바꿔 돌려주는 가드레일."""

    def __init__(self, replacement, **kwargs):
        super().__init__(**kwargs)
        self.replacement = replacement

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        return {**data, "modified_arguments": self.replacement}


class TestCallbackChain:
    """LiteLLM의 사전 훅 순회를 그대로 거쳐, 판정한 인자와 전송될 인자를 대조한다."""

    SENT = {"q": "x"}
    OTHER = {"q": "y"}

    def run_chain(self, tmp_path, monkeypatch, order, snap, evaluate="allow"):
        """``order``의 문자열은 MINT 로거·가드레일, 딕셔너리는 인자를 그 값으로 바꾸는 가드레일."""
        import litellm
        from litellm.caching.caching import DualCache
        from litellm.proxy.utils import ProxyLogging

        runtime, store, _ = build_runtime(tmp_path, snap, evaluate=evaluate)
        logger = make_logger(runtime)
        guardrail = MintGuardrail(
            runtime=runtime,
            guardrail_name="mint",
            event_hook=["pre_mcp_call", "post_mcp_call"],
            default_on=True,
        )
        chain = []
        for index, item in enumerate(order):
            if item == "logger":
                chain.append(logger)
            elif item == "guardrail":
                chain.append(guardrail)
            else:
                chain.append(
                    ArgumentsRewriter(
                        item,
                        guardrail_name=f"rewriter{index}",
                        event_hook="pre_mcp_call",
                        default_on=True,
                    )
                )
        monkeypatch.setattr(litellm, "callbacks", chain)
        proxy = ProxyLogging(user_api_key_cache=DualCache())
        obj = logging_obj()
        data = synthesized(obj, mcp_arguments=dict(self.SENT))
        result = SimpleNamespace(error=None, sent=None, obj=obj, store=store, logger=logger)
        try:
            data = run(
                proxy.pre_call_hook(user_api_key_dict=auth(), data=data, call_type="call_mcp_tool")
            )
        except HTTPException as exc:
            result.error = MintError.from_detail(exc.detail)
            return result
        kwargs = proxy.convert_mcp_hook_response_to_kwargs(data, {"arguments": dict(self.SENT)})
        result.sent = kwargs["arguments"]
        return result

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
        assert result.error.code == "MINT-ENFORCEMENT-UNVERIFIED"
        assert result.error.dispatch_state.value == "not_dispatched"
        assert result.obj.model_call_details[MINT_CTX_KEY]["allowed"] is False
        actions = [e.payload["enforced_action"] for e in result.store.of_kind("decision_evaluated")]
        assert sorted(actions) == ["allow", "block"]

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
        assert result.sent is None and result.error.code == "MINT-ENFORCEMENT-UNVERIFIED"
        assert (
            len(result.store.consumed) == 1 and len(result.store.of_kind("decision_evaluated")) == 2
        )


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
