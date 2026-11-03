"""배포 점검의 등급별 출력. 관리 API 응답은 v1.106.6의 실제 응답 형태 고정값이다."""

import copy

import pytest
import yaml
from litellm_stub import GOOD_KEY, fixture, key_entry

from mint_litellm.control import checks, litellm_config
from mint_litellm.control.checks import Level
from mint_litellm.types.settings import OBSERVATION_ITEMS, SETTINGS_TABLE

ENV = {"PROXY_BASE_URL": "https://gateway.test", "MCP_TRUSTED_REDIRECT_ORIGINS": "https://app.test"}


def good_config() -> dict:
    text = litellm_config.snippet({"everything": ("ctl-everything", "http://u.test/mcp")},
                                  no_spend_logs=False)
    return yaml.safe_load(text)


def registered_guardrails() -> dict:
    body = copy.deepcopy(fixture("guardrails_registered"))
    body["guardrails"][0]["litellm_params"]["guardrail"] = litellm_config.GUARDRAIL_CLASS
    return body


@pytest.fixture
def healthy(stub, litellm):
    """등록 서버와 같은 서버만 있고 결속되지 않은 키·제외 키가 없는 LiteLLM."""
    keys = fixture("key_list_full")
    keys["keys"] = [key_entry("k-good")]
    stub.set("/key/list", keys)
    stub.set("/team/list", [])
    stub.set("/guardrails/list", registered_guardrails())
    stub.set("/v1/mcp/server", [fixture("server_get")])
    return litellm


def run(config, litellm, env=ENV, **kwargs):
    return checks.run_checks(config, env, litellm, compat_probe=lambda: [], **kwargs)


def levels(report) -> dict[str, list[Level]]:
    found: dict[str, list[Level]] = {}
    for f in report.findings:
        found.setdefault(f.item, []).append(f.level)
    return found


def test_a_generated_snippet_with_full_environment_passes_cleanly(healthy):
    report = run(good_config(), healthy)
    bad = [f for f in report.findings if f.level in (Level.FAIL, Level.WARN)]
    assert bad == []
    assert not report.failed


def test_every_settings_row_is_reported(healthy):
    report = run(good_config(), healthy)
    reported = {f.item for f in report.findings}
    expected = {r.key for r in SETTINGS_TABLE if r.observation_item is None
                or r.key == "enable_redis_auth_cache" or r.key == "사용자·팀에 결속되지 않은 키"}
    assert expected <= reported
    assert set(report.observations) == set(OBSERVATION_ITEMS)


@pytest.mark.parametrize(
    ("mutate", "item"),
    [
        (lambda c: c.pop("guardrails"), "guardrails[MINT]"),
        (lambda c: c["guardrails"][0]["litellm_params"].update(mode=["pre_mcp_call"]),
         "guardrails[MINT]"),
        (lambda c: c["guardrails"][0]["litellm_params"].update(default_on=False),
         "guardrails[MINT]"),
        (lambda c: c["guardrails"].append({"guardrail_name": "z", "litellm_params": {
            "guardrail": "other"}}), "guardrails[MINT]"),
        (lambda c: c["litellm_settings"].pop("callbacks"), "callbacks"),
        (lambda c: c["mcp_servers"]["everything"].update(allow_all_keys=True),
         "mcp_servers.<이름>.allow_all_keys"),
        (lambda c: c["mcp_servers"]["everything"].pop("server_id"),
         "mcp_servers.<이름>.server_id"),
        (lambda c: c["general_settings"].pop("require_key_mcp_access_defined"),
         "require_key_mcp_access_defined"),
        (lambda c: c["general_settings"].update(store_prompts_in_spend_logs=True),
         "store_prompts_in_spend_logs"),
        (lambda c: c["litellm_settings"].update(user_url_validation=False),
         "user_url_validation"),
        (lambda c: c["general_settings"].update(supported_db_objects=["guardrails"]),
         "supported_db_objects"),
    ],
)
def test_required_violations_fail(healthy, mutate, item):
    config = good_config()
    mutate(config)
    report = run(config, healthy)
    assert report.failed
    assert Level.FAIL in levels(report)[item]


def test_required_environment_violation_fails(healthy):
    report = run(good_config(), healthy, env={**ENV, "LITELLM_STRICT_GUARDRAIL_MODES": "false"})
    assert Level.FAIL in levels(report)["LITELLM_STRICT_GUARDRAIL_MODES"]
    env_spend = {**ENV, "STORE_PROMPTS_IN_SPEND_LOGS": "true"}
    assert Level.FAIL in levels(run(good_config(), healthy, env=env_spend))[
        "store_prompts_in_spend_logs"]


@pytest.mark.parametrize(
    ("mutate", "item"),
    [
        (lambda c: c["litellm_settings"].pop("key_generation_settings"),
         "key_generation_settings.personal_key_generation.allowed_user_roles"),
        (lambda c: c["general_settings"].update(mcp_allow_all_keys_respects_mcp_scope=False),
         "mcp_allow_all_keys_respects_mcp_scope"),
        (lambda c: c["general_settings"].update(use_x_forwarded_for=True),
         "use_x_forwarded_for, mcp_trusted_proxy_ranges"),
        (lambda c: c["mcp_servers"]["everything"].update(auth_type="oauth2"),
         "mcp_servers.<이름>.issuer"),
        (lambda c: c["mcp_servers"]["everything"].update(auth_type="oauth2", issuer="https://i.test"),
         "mcp_servers.<이름>.oauth_identity_binding.mode"),
    ],
)
def test_recommended_violations_warn_without_failing(healthy, mutate, item):
    config = good_config()
    mutate(config)
    report = run(config, healthy)
    assert Level.WARN in levels(report)[item]
    assert not report.failed


def test_missing_environment_recommendations_warn(healthy):
    report = run(good_config(), healthy, env={"LEGACY_MULTI_INSTANCE_RATE_LIMITING": "true"})
    found = levels(report)
    for item in ("PROXY_BASE_URL", "MCP_TRUSTED_REDIRECT_ORIGINS",
                 "LEGACY_MULTI_INSTANCE_RATE_LIMITING"):
        assert Level.WARN in found[item]
    assert not report.failed


def test_second_replica_needs_a_shared_auth_cache(healthy):
    assert Level.WARN in levels(run(good_config(), healthy, replicas=2))["enable_redis_auth_cache"]
    config = good_config()
    config["litellm_settings"]["enable_redis_auth_cache"] = True
    assert Level.OK in levels(run(config, healthy, replicas=2))["enable_redis_auth_cache"]
    assert run(config, healthy, replicas=2).observations["auth_cache_shared"] == "true"


def test_observation_items_report_values_and_do_not_fail(stub, litellm):
    stub.set("/guardrails/list", registered_guardrails())
    report = run(good_config(), litellm)  # 실제 키 목록: 결속 없는 키 1, 별칭 형식 권한 1
    assert report.observations["unbound_keys"] == 1
    assert report.observations["guardrail_optout_subjects"] == 0
    assert report.observations["spend_log_arguments_stored"] == "true"
    assert Level.WARN in levels(report)["사용자·팀에 결속되지 않은 키"]
    config = good_config()
    config["general_settings"]["disable_spend_logs"] = True
    assert run(config, litellm).observations["spend_log_arguments_stored"] == "false"


def test_optout_metadata_on_keys_and_teams_is_counted_and_warned(healthy, stub):
    keys = fixture("key_list_full")
    keys["keys"] = [
        {**key_entry("k-good"), "metadata": {"opted_out_global_guardrails": ["mint"]}},
        {**key_entry("k-unbound"), "user_id": "u", "metadata": {"disable_global_guardrails": True}},
    ]
    stub.set("/key/list", keys)
    teams = fixture("team_list")
    teams[0]["metadata"] = {"opted_out_global_guardrails": ["mint"]}
    stub.set("/team/list", teams)
    report = run(good_config(), healthy)
    assert report.observations["guardrail_optout_subjects"] == 3
    assert Level.WARN in levels(report)["가드레일 제외 대상"]
    assert not report.failed


def test_alias_formatted_object_permission_fails_with_guidance(stub, litellm):
    stub.set("/guardrails/list", registered_guardrails())
    report = run(good_config(), litellm)
    assert report.failed
    message = next(f.message for f in report.findings
                   if f.item == "object_permission 서버 식별자 형식")
    assert "k-alias" in message and "everything" in message and "server_id" in message


def test_duplicate_server_names_fail(healthy, stub):
    entry = fixture("server_get")
    twin = {**entry, "server_id": "uuid-2", "server_name": "other", "alias": "everything"}
    stub.set("/v1/mcp/server", [entry, twin])
    report = run(good_config(), healthy)
    assert Level.FAIL in levels(report)["서버 이름·별칭 유일성"]
    assert "everything" in next(f.message for f in report.findings
                                if f.item == "서버 이름·별칭 유일성")


def test_one_server_with_equal_name_and_alias_is_not_a_duplicate(stub, litellm):
    stub.set("/v1/mcp/server", fixture("server_list"))  # 하나의 서버가 이름과 별칭을 같게 가진다
    report = run(good_config(), litellm)
    assert Level.OK in levels(report)["서버 이름·별칭 유일성"]


def test_registered_servers_must_match_the_registry(healthy):
    from mint_litellm.types.decision import url_sha256

    good = {"server_id": "ctl-everything", "url_sha256": url_sha256("http://u.test/mcp"),
            "transport": "http", "auth_type": "none"}
    assert Level.OK in levels(run(good_config(), healthy, registered_servers=[good]))[
        "등록 서버의 계약 값"]
    for bad in ({**good, "transport": "sse"}, {**good, "auth_type": "bearer_token"},
                {**good, "server_id": "ghost"}):
        assert Level.FAIL in levels(run(good_config(), healthy, registered_servers=[bad]))[
            "등록 서버의 계약 값"]


def test_registered_guardrail_must_match_the_running_one(healthy, stub):
    stub.set("/guardrails/list", fixture("guardrails_empty"))
    assert Level.FAIL in levels(run(good_config(), healthy))["guardrails[MINT]"]
    body = registered_guardrails()
    body["guardrails"][0]["litellm_params"]["default_on"] = False
    stub.set("/guardrails/list", body)
    assert any("default_on" in f.message for f in run(good_config(), healthy).findings)
    stub.set("/guardrails/list", fixture("guardrails_registered"))  # 다른 클래스
    findings = run(good_config(), healthy).findings
    assert any("MINT 가드레일이 아닙니다" in f.message for f in findings)


def test_compat_symbols_are_required_when_litellm_is_installed(healthy):
    report = checks.run_checks(good_config(), ENV, healthy, compat_probe=lambda: ["x.y"])
    assert Level.FAIL in levels(report)["호환 계층 비공개 심볼"]
    absent = checks.run_checks(good_config(), ENV, healthy, compat_probe=lambda: None)
    assert Level.SKIP in levels(absent)["호환 계층 비공개 심볼"] and not absent.failed


def test_missing_config_file_and_management_api(litellm):
    report = checks.run_checks(None, ENV, None, compat_probe=lambda: [])
    assert report.failed and Level.FAIL in levels(report)["LiteLLM 설정 파일"]
    assert Level.SKIP in levels(report)["서버 이름·별칭 유일성"]
    assert report.observations.get("spend_log_arguments_stored") == "확인 불가"


def test_render_lists_levels_and_the_four_observation_items(healthy):
    text = run(good_config(), healthy).render()
    assert "[통과] callbacks" in text
    for name in OBSERVATION_ITEMS:
        assert f"  {name}: " in text


def test_key_list_uses_full_objects_and_follows_pages(stub, litellm):
    page1 = {"keys": [key_entry("k-good")], "total_count": 2, "current_page": 1, "total_pages": 2}
    page2 = {"keys": [key_entry("k-alias")], "total_count": 2, "current_page": 2, "total_pages": 2}
    stub.set("/key/list", page1, return_full_object="true", size="100", page="1")
    stub.set("/key/list", page2, return_full_object="true", size="100", page="2")
    keys = litellm.list_keys()
    assert [k["key_alias"] for k in keys] == ["k-good", "k-alias"]
    assert all("metadata" in k for k in keys)
    assert GOOD_KEY == keys[0]["token"]
