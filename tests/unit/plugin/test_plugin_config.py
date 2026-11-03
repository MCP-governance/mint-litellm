from pathlib import Path

import pytest

from mint_litellm.plugin.config import PluginConfig
from mint_litellm.types.settings import Mode

BASE = {"MINT_INSTANCE_ID": "prod-1", "MINT_DATABASE_URL": "postgresql://mint@db/mint"}


def test_defaults():
    config = PluginConfig.from_env(BASE)
    assert config.instance_id == "prod-1"
    assert config.mode_ceiling is Mode.ENFORCE and config.required is True
    assert config.high_risk_tools == frozenset()
    assert config.buffer_dir == Path("/var/lib/mint/buffer")
    assert config.buffer_max_bytes == 50 * 1024 * 1024
    assert config.decision_budget_seconds == 0.3
    assert config.fingerprint_key is None and config.opa_url is None


def test_every_setting_can_be_given():
    env = {
        **BASE,
        "MINT_MODE": "Observe",
        "MINT_REQUIRED": "false",
        "MINT_HIGH_RISK_TOOLS": " delete_repo, , drop_table ",
        "MINT_BUFFER_DIR": "/data/buf",
        "MINT_BUFFER_MAX_BYTES": "2048",
        "MINT_DB_POOL_SIZE": "4",
        "MINT_DECISION_BUDGET_MS": "500",
        "MINT_FINGERPRINT_KEY": "k" * 32,
        "MINT_FINGERPRINT_KEY_VERSION": "3",
        "MINT_OPA_URL": "http://opa:8181",
        "MINT_READINESS_URL": "http://localhost:4000/health/readiness",
        "MINT_SELFCHECK_GRACE_SECONDS": "5",
    }
    config = PluginConfig.from_env(env)
    assert config.mode_ceiling is Mode.OBSERVE and config.required is False
    assert config.high_risk_tools == {"delete_repo", "drop_table"}
    assert config.buffer_dir == Path("/data/buf") and config.buffer_max_bytes == 2048
    assert config.db_pool_size == 4 and config.decision_budget_seconds == 0.5
    assert config.fingerprint_key == b"k" * 32 and config.fingerprint_key_version == 3
    assert config.opa_url == "http://opa:8181" and config.selfcheck_grace_seconds == 5.0


@pytest.mark.parametrize(
    "override",
    [
        {"MINT_INSTANCE_ID": ""},
        {"MINT_INSTANCE_ID": "has space"},
        {"MINT_INSTANCE_ID": "a:b"},
        {"MINT_DATABASE_URL": " "},
        {"MINT_MODE": "audit"},
        {"MINT_REQUIRED": "maybe"},
        {"MINT_BUFFER_MAX_BYTES": "ten"},
        {"MINT_BUFFER_MAX_BYTES": "10"},
        {"MINT_DB_POOL_SIZE": "0"},
        {"MINT_DECISION_BUDGET_MS": "1"},
        {"MINT_FINGERPRINT_KEY": "short"},
        {"MINT_FINGERPRINT_KEY_VERSION": "0"},
    ],
)
def test_invalid_values_raise_runtime_error_only(override):
    # LiteLLM keeps starting without the guardrail when the constructor raises ValueError
    # or TypeError, so configuration problems must surface as RuntimeError.
    with pytest.raises(RuntimeError):
        PluginConfig.from_env({**BASE, **override})


@pytest.mark.parametrize("missing", ["MINT_INSTANCE_ID", "MINT_DATABASE_URL"])
def test_required_settings(missing):
    env = {k: v for k, v in BASE.items() if k != missing}
    with pytest.raises(RuntimeError, match=missing):
        PluginConfig.from_env(env)


@pytest.mark.parametrize("text", ["1", "TRUE", "yes", "on"])
def test_truthy_required_values(text):
    assert PluginConfig.from_env({**BASE, "MINT_REQUIRED": text}).required is True
