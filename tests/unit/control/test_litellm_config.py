"""init이 만드는 설정 조각과 설정·환경 파일 읽기."""

import pytest
import yaml

from mint_litellm.control import litellm_config
from mint_litellm.control.errors import ControlError


def test_snippet_has_the_required_guardrail_callback_and_server_entries():
    data = yaml.safe_load(
        litellm_config.snippet({"docs": ("docs-id", "http://u.test/mcp")}, no_spend_logs=False)
    )
    guardrail = data["guardrails"][0]["litellm_params"]
    assert guardrail["mode"] == ["pre_mcp_call", "post_mcp_call"] and guardrail["default_on"]
    assert litellm_config.LOGGER_REF in data["litellm_settings"]["callbacks"]
    assert data["general_settings"]["require_key_mcp_access_defined"] is True
    assert data["general_settings"]["store_prompts_in_spend_logs"] is False
    assert "disable_spend_logs" not in data["general_settings"]
    roles = data["litellm_settings"]["key_generation_settings"]["personal_key_generation"]
    assert roles["allowed_user_roles"] == ["proxy_admin"]
    assert data["mcp_servers"]["docs"] == {
        "server_id": "docs-id", "url": "http://u.test/mcp", "transport": "http",
        "allow_all_keys": False,
    }


def test_disable_spend_logs_only_with_the_option_and_servers_are_optional():
    on = yaml.safe_load(litellm_config.snippet({}, no_spend_logs=True))
    assert on["general_settings"]["disable_spend_logs"] is True
    assert "mcp_servers" not in on


def test_load_config_and_env_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("general_settings:\n  a: 1\n", encoding="utf-8")
    assert litellm_config.load_config(path) == {"general_settings": {"a": 1}}
    assert litellm_config.load_config(None) is None
    path.write_text("- list\n", encoding="utf-8")
    with pytest.raises(ControlError, match="매핑"):
        litellm_config.load_config(path)
    with pytest.raises(ControlError, match="읽을 수 없습니다"):
        litellm_config.load_config(tmp_path / "missing.yaml")
    env = tmp_path / ".env"
    env.write_text("# 주석\nA=1\nB = \"two\"\n\nbroken\n", encoding="utf-8")
    assert litellm_config.load_env_file(env) == {"A": "1", "B": "two"}
