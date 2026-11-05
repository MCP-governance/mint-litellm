"""LiteLLM 기동 전 설정 검사와 진입점."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import mint_litellm
from mint_litellm.control import litellm_config, preflight

ROOT = Path(mint_litellm.__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "deploy" / "mint-entrypoint.sh"


def good_text() -> str:
    return litellm_config.snippet({"docs": ("docs", "http://u.test/mcp")}, no_spend_logs=False)


def write(tmp_path, data) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")
    return path


def test_a_complete_snippet_passes(tmp_path):
    assert preflight.preflight(write(tmp_path, good_text()), {}) == []
    assert preflight.main([str(write(tmp_path, good_text()))], {}) == 0


@pytest.mark.parametrize(
    ("mutate", "item"),
    [
        (lambda c: c.pop("guardrails"), "guardrails[MINT]"),
        (lambda c: c["guardrails"][0]["litellm_params"].update(default_on=False),
         "guardrails[MINT]"),
        (lambda c: c["guardrails"][0]["litellm_params"].update(mode=["pre_mcp_call"]),
         "guardrails[MINT]"),
        (lambda c: c["litellm_settings"].pop("callbacks"), "callbacks"),
        (lambda c: c["general_settings"].pop("require_key_mcp_access_defined"),
         "require_key_mcp_access_defined"),
        (lambda c: c["general_settings"].update(store_prompts_in_spend_logs=True),
         "store_prompts_in_spend_logs"),
        (lambda c: c["mcp_servers"]["docs"].update(allow_all_keys=True), "allow_all_keys"),
        (lambda c: c["mcp_servers"]["docs"].pop("server_id"), "server_id"),
    ],
)
def test_missing_or_wrong_required_rows_are_refused(tmp_path, capsys, mutate, item):
    config = yaml.safe_load(good_text())
    mutate(config)
    path = write(tmp_path, config)
    assert any(item in line for line in preflight.preflight(path, {}))
    assert preflight.main([str(path)], {}) == 1
    assert item in capsys.readouterr().err


def test_required_environment_is_checked(tmp_path):
    path = write(tmp_path, good_text())
    assert preflight.main([str(path)], {"LITELLM_STRICT_GUARDRAIL_MODES": "false"}) == 1


def test_an_unreadable_or_invalid_config_is_refused(tmp_path, capsys):
    assert preflight.main([str(tmp_path / "missing.yaml")], {}) == 1
    assert "검사를 할 수 없습니다" in capsys.readouterr().err
    assert preflight.main([str(write(tmp_path, "- a\n- b\n"))], {}) == 1
    assert preflight.main([], {}) == 2


def test_recommended_rows_do_not_block_startup(tmp_path):
    config = yaml.safe_load(good_text())
    config["litellm_settings"].pop("key_generation_settings")
    assert preflight.main([str(write(tmp_path, config))], {}) == 0


# --- 진입점 -----------------------------------------------------------------------------

bash = pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="POSIX bash 필요")


def entrypoint_env(tmp_path, python_ok=True):
    """진입점이 부르는 python과 원래 진입점을 대역으로 바꾼 작업 디렉터리."""
    work = tmp_path / "app"
    (work / "docker").mkdir(parents=True, exist_ok=True)
    original = work / "docker" / "prod_entrypoint.sh"
    original.write_text('#!/bin/sh\necho "STARTED $@" >> "$START_LOG"\n', encoding="utf-8")
    original.chmod(0o755)
    shim = tmp_path / "bin"
    shim.mkdir(exist_ok=True)
    python = shim / "python"
    if python_ok:
        python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    else:
        python.write_text('#!/bin/sh\necho "No module named mint_litellm" >&2\nexit 1\n',
                          encoding="utf-8")
    python.chmod(0o755)
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}",
           "START_LOG": str(tmp_path / "start.log"), "PYTHONPATH": str(ROOT / "src")}
    return work, env


def start(tmp_path, args, python_ok=True):
    work, env = entrypoint_env(tmp_path, python_ok)
    result = subprocess.run(["sh", str(ENTRYPOINT), *args], cwd=work, env=env,
                            capture_output=True, text=True, encoding="utf-8")
    started = (tmp_path / "start.log").exists()
    return result, started


@bash
def test_the_entrypoint_starts_litellm_only_after_a_passing_check(tmp_path):
    path = write(tmp_path, good_text())
    result, started = start(tmp_path, ["--config", str(path), "--port", "4000"])
    assert result.returncode == 0, result.stderr
    assert started and "STARTED --config" in (tmp_path / "start.log").read_text(encoding="utf-8")


@bash
def test_the_entrypoint_refuses_a_config_without_the_mint_entries(tmp_path):
    config = yaml.safe_load(good_text())
    config.pop("guardrails")
    config["litellm_settings"].pop("callbacks")
    result, started = start(tmp_path, [f"--config={write(tmp_path, config)}"])
    assert result.returncode != 0 and not started
    assert "guardrails[MINT]" in result.stderr


@bash
def test_the_entrypoint_refuses_when_the_config_is_missing_or_unspecified(tmp_path):
    result, started = start(tmp_path, ["--config", str(tmp_path / "none.yaml")])
    assert result.returncode != 0 and not started
    result, started = start(tmp_path, ["--port", "4000"])
    assert result.returncode != 0 and not started and "--config" in result.stderr


@bash
def test_the_entrypoint_refuses_when_the_check_module_is_absent(tmp_path):
    path = write(tmp_path, good_text())
    result, started = start(tmp_path, ["--config", str(path)], python_ok=False)
    assert result.returncode != 0 and not started


def bad_config(tmp_path) -> Path:
    config = yaml.safe_load(good_text())
    config.pop("guardrails")
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


@bash
@pytest.mark.parametrize("order", ["good-first", "bad-first"])
@pytest.mark.parametrize(
    "flags", [("--config", "-c"), ("-c", "--config"), ("--config", "--config")]
)
def test_two_configuration_arguments_are_refused_whatever_their_form(tmp_path, order, flags):
    good, bad = write(tmp_path, good_text()), bad_config(tmp_path)
    first, second = (good, bad) if order == "good-first" else (bad, good)
    result, started = start(tmp_path, [flags[0], str(first), flags[1], str(second)])
    assert result.returncode != 0 and not started
    assert "둘 이상" in result.stderr


@bash
def test_every_single_configuration_form_checks_the_file_it_starts_with(tmp_path):
    good, bad = write(tmp_path, good_text()), bad_config(tmp_path)
    for index, form in enumerate((["-c", "{}"], ["--config", "{}"], ["--config={}"], ["-c{}"])):
        for path, should_start in ((good, True), (bad, False)):
            args = [part.format(path) for part in form] + ["--port", "4000", "-v"]
            sub = tmp_path / f"run{index}-{should_start}"
            sub.mkdir()
            result, started = start(sub, args)
            assert started is should_start, (form, result.stderr)
            assert (result.returncode == 0) is should_start


@bash
def test_unparseable_or_incomplete_configuration_arguments_are_refused(tmp_path):
    good = write(tmp_path, good_text())
    for args in (["--port", "4000", "-c"], ["-vc", str(good)], [f"--config={good}", "-ec"]):
        result, started = start(tmp_path, args)
        assert result.returncode != 0 and not started, args
        (tmp_path / "start.log").unlink(missing_ok=True)
