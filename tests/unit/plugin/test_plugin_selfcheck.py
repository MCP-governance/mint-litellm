import asyncio
import io
from dataclasses import replace
from types import SimpleNamespace

import pytest
from plugin_helpers import FakeStore, config

from mint_litellm.compat.litellm_1_106 import RegistryEntry
from mint_litellm.plugin.buffer import EvidenceRecorder, LocalBuffer
from mint_litellm.plugin.selfcheck import (
    LiteLLMView,
    PluginRuntime,
    check_items,
    collect_view,
    contract_mismatches,
)
from mint_litellm.plugin.snapshot import RegisteredServer
from mint_litellm.types.decision import url_sha256
from mint_litellm.types.settings import SELF_CHECK_DESCRIPTIONS, SelfCheckItem

URL = "https://mcp.example/api"
HASH = url_sha256(URL)


def entry(server_id="srv-1", names=("mslearn",), **override):
    args = {
        "server_id": server_id,
        "names": names,
        "url_sha256": HASH,
        "transport": "http",
        "auth_type": "none",
        "allow_all_keys": False,
    }
    args.update(override)
    return RegistryEntry(**args)


def guardrail(**override):
    args = {
        "guardrail_name": "mint",
        "event_hook": ["pre_mcp_call", "post_mcp_call"],
        "default_on": True,
        "run_in_parallel": False,
    }
    args.update(override)
    return SimpleNamespace(**args)


LOGGER = SimpleNamespace(name="logger")
GUARDRAIL = guardrail()


def view(**override):
    args = {
        "callbacks": [LOGGER, GUARDRAIL],
        "general_settings": {"require_key_mcp_access_defined": True},
        "strict_guardrail_modes": True,
        "store_prompts_in_spend_logs": False,
        "user_url_validation": True,
        "registry": [entry()],
        "missing_symbols": [],
    }
    args.update(override)
    return LiteLLMView(**args)


def registered(**override):
    args = {
        "server_id": "srv-1",
        "status": "approved",
        "url_sha256": HASH,
        "transport": "http",
        "auth_type": "none",
    }
    args.update(override)
    return RegisteredServer(**args)


def run_checks(v=None, *, logger=LOGGER, guard=GUARDRAIL, controlled="default"):
    return check_items(
        v or view(),
        logger=logger,
        guardrail=guard,
        controlled=[registered()] if controlled == "default" else controlled,
    )


def failed(results):
    return {r.item for r in results if not r.ok}


def test_all_eight_items_are_reported_and_a_good_setup_passes():
    results = run_checks()
    assert [r.item for r in results] == list(SelfCheckItem)
    assert failed(results) == set()


def test_item_1_logger_must_be_registered():
    assert failed(run_checks(view(callbacks=[GUARDRAIL]))) >= {SelfCheckItem.CALLBACK_REGISTERED}
    assert SelfCheckItem.CALLBACK_REGISTERED in failed(run_checks(logger=None))


def test_item_2_guardrail_modes_and_default_on():
    other = SimpleNamespace(value="pre_mcp_call"), SimpleNamespace(value="post_mcp_call")
    enum_style = guardrail(event_hook=list(other))
    assert failed(run_checks(view(callbacks=[LOGGER, enum_style]), guard=enum_style)) == set()
    for bad in (
        guardrail(event_hook=["pre_mcp_call"]),
        guardrail(event_hook=["post_mcp_call"]),
        guardrail(event_hook="pre_mcp_call"),
        guardrail(event_hook=None),
        guardrail(default_on=False),
    ):
        results = run_checks(view(callbacks=[LOGGER, bad]), guard=bad)
        assert SelfCheckItem.GUARDRAIL_MODES in failed(results)
    results = run_checks(view(callbacks=[LOGGER]))
    assert SelfCheckItem.GUARDRAIL_MODES in failed(results)
    assert SelfCheckItem.GUARDRAIL_MODES in failed(run_checks(guard=None))


def test_item_3_order():
    later = guardrail(guardrail_name="pii", event_hook=["pre_mcp_call"])
    assert SelfCheckItem.GUARDRAIL_ORDER in failed(
        run_checks(view(callbacks=[LOGGER, GUARDRAIL, later]))
    )
    assert SelfCheckItem.GUARDRAIL_ORDER in failed(run_checks(view(callbacks=[GUARDRAIL, LOGGER])))
    parallel = guardrail(run_in_parallel=True)
    assert SelfCheckItem.GUARDRAIL_ORDER in failed(
        run_checks(view(callbacks=[LOGGER, parallel]), guard=parallel)
    )
    unrelated_mode = guardrail(guardrail_name="pii", event_hook=["pre_call"])
    always = guardrail(guardrail_name="all", event_hook=None)
    assert SelfCheckItem.GUARDRAIL_ORDER not in failed(
        run_checks(view(callbacks=[LOGGER, GUARDRAIL, unrelated_mode]))
    )
    assert SelfCheckItem.GUARDRAIL_ORDER in failed(
        run_checks(view(callbacks=[LOGGER, GUARDRAIL, always]))
    )
    earlier = guardrail(guardrail_name="pii", event_hook=["pre_mcp_call"])
    assert SelfCheckItem.GUARDRAIL_ORDER not in failed(
        run_checks(view(callbacks=[earlier, LOGGER, GUARDRAIL]))
    )


def test_item_4_key_access_is_strict_for_controlled_servers():
    assert SelfCheckItem.KEY_ACCESS_STRICT in failed(run_checks(view(general_settings={})))
    assert SelfCheckItem.KEY_ACCESS_STRICT in failed(
        run_checks(view(general_settings={"require_key_mcp_access_defined": "true"}))
    )
    open_server = view(registry=[entry(allow_all_keys=True)])
    assert SelfCheckItem.KEY_ACCESS_STRICT in failed(run_checks(open_server))
    uncontrolled_open = view(registry=[entry(), entry("srv-9", ("other",), allow_all_keys=True)])
    assert SelfCheckItem.KEY_ACCESS_STRICT not in failed(run_checks(uncontrolled_open))


@pytest.mark.parametrize(
    "mutation",
    [
        {"url_sha256": url_sha256("https://changed/")},
        {"transport": "sse"},
        {"auth_type": "bearer_token"},
    ],
    ids=["url", "transport", "auth"],
)
def test_item_5_does_not_fail_when_a_contract_value_differs(mutation):
    changed = view(registry=[entry(**mutation)])
    assert SelfCheckItem.SERVER_REGISTRY_MATCH not in failed(run_checks(changed))
    (field,) = mutation
    assert contract_mismatches(changed, [registered()]) == [("srv-1", field)]


def test_contract_mismatches_cover_only_controlled_servers_present_in_the_registry():
    changed = view(registry=[entry(transport="sse", auth_type="oauth2")])
    assert contract_mismatches(changed, [registered()]) == [
        ("srv-1", "transport"),
        ("srv-1", "auth_type"),
    ]
    assert contract_mismatches(changed, [registered(status="pending")]) == []
    assert contract_mismatches(view(registry=[]), [registered()]) == []
    assert contract_mismatches(view(), [registered()]) == []


def test_item_5_registry_must_contain_every_controlled_server():
    assert SelfCheckItem.SERVER_REGISTRY_MATCH in failed(run_checks(view(registry=[])))
    for status in ("approved", "suspended"):
        results = run_checks(view(registry=[]), controlled=[registered(status=status)])
        assert SelfCheckItem.SERVER_REGISTRY_MATCH in failed(results)
    other_states = (
        "pending",
        "validating",
        "awaiting_approval",
        "rejected",
        "terminating",
        "terminated",
    )
    for status in other_states:
        results = run_checks(view(registry=[]), controlled=[registered(status=status)])
        assert SelfCheckItem.SERVER_REGISTRY_MATCH not in failed(results)


def test_items_4_and_5_fail_when_the_mint_database_cannot_be_read():
    assert {SelfCheckItem.KEY_ACCESS_STRICT, SelfCheckItem.SERVER_REGISTRY_MATCH} <= failed(
        run_checks(controlled=None)
    )


def test_item_6_names_and_aliases_are_unique_across_servers():
    clash = view(registry=[entry(), entry("srv-2", ("mslearn", "other"))])
    assert failed(run_checks(clash)) >= {SelfCheckItem.NAME_ALIAS_UNIQUE}
    spaced = view(registry=[entry(names=("my server",)), entry("srv-2", ("my_server",))])
    assert SelfCheckItem.NAME_ALIAS_UNIQUE in failed(run_checks(spaced))
    same_server = view(registry=[entry(names=("mslearn", "mslearn", "ms"))])
    assert SelfCheckItem.NAME_ALIAS_UNIQUE not in failed(run_checks(same_server))


def test_item_7_symbols():
    results = run_checks(view(missing_symbols=["litellm.x:y"]))
    assert SelfCheckItem.COMPAT_SYMBOLS in failed(results)
    assert (
        "litellm.x:y" in next(r for r in results if r.item is SelfCheckItem.COMPAT_SYMBOLS).detail
    )


@pytest.mark.parametrize(
    "override",
    [
        {"strict_guardrail_modes": False},
        {"store_prompts_in_spend_logs": True},
        {"user_url_validation": False},
        {
            "general_settings": {
                "require_key_mcp_access_defined": True,
                "supported_db_objects": ["models"],
            }
        },
    ],
)
def test_item_8_required_settings(override):
    assert failed(run_checks(view(**override))) == {SelfCheckItem.REQUIRED_SETTINGS}


def test_item_8_accepts_a_supported_db_objects_list_with_mcp():
    settings = {"require_key_mcp_access_defined": True, "supported_db_objects": ["models", "mcp"]}
    assert failed(run_checks(view(general_settings=settings))) == set()


def test_failure_details_never_contain_credentials_or_urls():
    results = run_checks(view(registry=[entry(transport="sse")]))
    text = " ".join(r.detail for r in results)
    assert "mcp.example" not in text


def make_runtime(tmp_path, *, v=None, controlled=None, required=True, retry=0.01, ready=True):
    store = FakeStore()
    recorder = EvidenceRecorder(
        store,
        LocalBuffer(tmp_path / "buf", 1024 * 1024),
        budget_seconds=1.0,
        instance_id="inst",
        stderr=io.StringIO(),
    )

    class Database:
        async def registered_servers(self, instance_id):
            if controlled is Exception:
                raise ConnectionError("down")
            return [registered()] if controlled is None else controlled

    stopped: list[int] = []
    state = {"view": v or view()}

    waited: list[float] = []

    async def readiness(url, timeout):
        waited.append(timeout)
        return ready

    runtime = PluginRuntime(
        config=config(tmp_path, required=required, selfcheck_grace_seconds=0.05),
        database=Database(),
        recorder=recorder,
        evaluate=None,
        view_collector=lambda: state["view"],
        terminate=lambda: stopped.append(1),
        readiness=readiness,
        retry_seconds=retry,
        recheck_seconds=retry,
    )
    runtime.logger, runtime.guardrail = LOGGER, GUARDRAIL
    runtime.readiness_waits = waited
    return runtime, store, stopped, state


def test_a_runtime_is_unverified_until_every_item_passes(tmp_path):
    runtime, _, _, state = make_runtime(tmp_path)
    assert runtime.verified is False
    assert asyncio.run(runtime.verify()) is True and runtime.verified is True
    state["view"] = view(strict_guardrail_modes=False)
    assert asyncio.run(runtime.verify()) is False and runtime.verified is False


def test_a_passing_startup_verifies_without_stopping(tmp_path):
    runtime, store, stopped, _ = make_runtime(tmp_path)
    asyncio.run(runtime._startup())
    assert runtime.verified and stopped == [] and store.events == {}


def test_a_failing_startup_records_the_failure_and_stops_the_process(tmp_path):
    runtime, store, stopped, _ = make_runtime(tmp_path, v=view(callbacks=[GUARDRAIL]))
    asyncio.run(runtime._startup())
    assert not runtime.verified and stopped == [1]
    (event,) = store.of_kind("enforcement_unverified")
    assert event.source_key.startswith("selfcheck:") and event.source_key.endswith(
        ":1:enforcement_unverified"
    )
    assert event.payload["failed"][0]["item"] == 1
    assert event.payload["failed"][0]["name"] == SelfCheckItem.CALLBACK_REGISTERED.name


def test_a_failure_that_resolves_within_the_grace_period_does_not_stop(tmp_path):
    runtime, store, stopped, state = make_runtime(tmp_path, v=view(strict_guardrail_modes=False))
    runtime.config = replace(runtime.config, selfcheck_grace_seconds=5.0)

    async def scenario():
        task = asyncio.create_task(runtime._startup())
        await asyncio.sleep(0.05)
        state["view"] = view()
        await task

    asyncio.run(scenario())
    assert runtime.verified and stopped == [] and store.events == {}


def test_a_non_required_runtime_stays_blocked_and_recovers_later(tmp_path):
    runtime, store, stopped, state = make_runtime(
        tmp_path, v=view(strict_guardrail_modes=False), required=False
    )

    async def scenario():
        task = asyncio.create_task(runtime._startup())
        await asyncio.sleep(0.3)
        assert not runtime.verified
        state["view"] = view()
        await task

    asyncio.run(scenario())
    assert runtime.verified and stopped == []
    assert len(store.of_kind("enforcement_unverified")) == 1


def test_an_unreadable_mint_database_fails_the_startup_checks(tmp_path):
    runtime, store, stopped, _ = make_runtime(tmp_path, controlled=Exception)
    asyncio.run(runtime._startup())
    assert stopped == [1]
    names = {f["name"] for f in store.of_kind("enforcement_unverified")[0].payload["failed"]}
    assert {"KEY_ACCESS_STRICT", "SERVER_REGISTRY_MATCH"} <= names


def test_an_error_while_checking_also_stops_the_process(tmp_path):
    runtime, store, stopped, _ = make_runtime(tmp_path)

    def broken():
        raise KeyError("x")

    runtime.view_collector = broken
    asyncio.run(runtime._startup())
    assert stopped == [1]
    assert store.of_kind("enforcement_unverified")[0].payload["error"] == "KeyError"


def test_startup_is_scheduled_once_and_only_inside_a_running_loop(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    runtime.ensure_started()
    assert runtime._task is None

    async def scenario():
        runtime.ensure_started()
        first = runtime._task
        runtime.ensure_started()
        assert runtime._task is first
        await first

    asyncio.run(scenario())
    assert runtime.verified


def test_descriptions_exist_for_every_item():
    assert set(SELF_CHECK_DESCRIPTIONS) == set(SelfCheckItem)


def test_view_reports_missing_symbols_when_litellm_is_absent_or_other(monkeypatch):
    from mint_litellm.compat import litellm_1_106 as compat

    monkeypatch.setattr(compat, "missing_symbols", lambda: ["litellm: not installed"])
    collected = collect_view({})
    assert collected.missing_symbols == ["litellm: not installed"]
    assert SelfCheckItem.COMPAT_SYMBOLS in failed(
        check_items(collected, logger=LOGGER, guardrail=GUARDRAIL, controlled=[])
    )


def test_the_runtime_is_built_from_the_environment_with_the_policy_client(tmp_path, monkeypatch):
    from mint_litellm.plugin import selfcheck
    from mint_litellm.plugin.policy.opa_client import OPAClient

    monkeypatch.setattr(selfcheck, "_runtime", None)
    env = {
        "MINT_INSTANCE_ID": "prod",
        "MINT_DATABASE_URL": "postgresql://mint@db/mint",
        "MINT_OPA_URL": "http://opa:8181",
        "MINT_BUFFER_DIR": str(tmp_path / "buffer"),
        "MINT_DECISION_BUDGET_MS": "250",
    }
    runtime = selfcheck.get_runtime(env)
    assert runtime is selfcheck.get_runtime(env)
    assert runtime.config.decision_budget_seconds == 0.25
    assert isinstance(runtime.evaluate.__self__, OPAClient)
    assert runtime.database.acquire_timeout == 0.25 and runtime.verified is False
    monkeypatch.setattr(selfcheck, "_runtime", None)


def test_a_runtime_without_the_required_environment_raises_runtime_error(monkeypatch):
    from mint_litellm.plugin import selfcheck

    monkeypatch.setattr(selfcheck, "_runtime", None)
    with pytest.raises(RuntimeError):
        selfcheck.get_runtime({})


def test_a_contract_mismatch_does_not_stop_startup_and_is_recorded_once_per_field(tmp_path):
    changed = view(registry=[entry(transport="sse", url_sha256=url_sha256("https://moved/"))])
    runtime, store, stopped, _ = make_runtime(tmp_path, v=changed)
    asyncio.run(runtime._startup())
    assert runtime.verified and stopped == []
    events = store.of_kind("server_identity_mismatch_observed")
    assert sorted(e.payload["field"] for e in events) == ["transport", "url_sha256"]
    assert {e.payload["server_id"] for e in events} == {"srv-1"}
    for event in events:
        assert event.actor is None and event.invocation_id is None
        assert event.source_key.startswith("selfcheck:")
        assert event.source_key.endswith(":1:server_identity_mismatch_observed")
    assert len({e.source_key for e in events}) == 2
    asyncio.run(runtime._report_mismatches())
    assert len(store.of_kind("server_identity_mismatch_observed")) == 2


def test_a_mismatch_is_also_recorded_when_another_item_stops_the_startup(tmp_path):
    changed = view(registry=[entry(transport="sse")], strict_guardrail_modes=False)
    runtime, store, stopped, _ = make_runtime(tmp_path, v=changed)
    asyncio.run(runtime._startup())
    assert stopped == [1]
    assert len(store.of_kind("server_identity_mismatch_observed")) == 1
    assert len(store.of_kind("enforcement_unverified")) == 1


def test_readiness_is_awaited_for_at_most_the_grace_period(tmp_path):
    runtime, _, _, _ = make_runtime(tmp_path)
    asyncio.run(runtime._startup())
    assert runtime.readiness_waits == [runtime.config.selfcheck_grace_seconds]


def test_without_readiness_in_time_the_startup_is_recorded_and_stopped(tmp_path):
    runtime, store, stopped, _ = make_runtime(tmp_path, ready=False)
    runtime.view_collector = lambda: pytest.fail("no check runs before readiness")
    asyncio.run(runtime._startup())
    assert stopped == [1] and not runtime.verified
    (event,) = store.of_kind("enforcement_unverified")
    assert event.payload == {"failed": [], "error": "readiness_timeout"}


def test_without_readiness_a_non_required_runtime_stays_blocked_and_recovers(tmp_path):
    runtime, store, stopped, _ = make_runtime(tmp_path, ready=False, required=False)

    async def scenario():
        await runtime._startup()

    asyncio.run(scenario())
    assert stopped == [] and runtime.verified
    assert store.of_kind("enforcement_unverified")[0].payload["error"] == "readiness_timeout"
