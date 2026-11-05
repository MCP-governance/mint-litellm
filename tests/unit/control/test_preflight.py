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
STUB_LOADER = Path(__file__).resolve().parents[1] / "stub_litellm"


def good_text() -> str:
    return litellm_config.snippet({"docs": ("docs", "http://u.test/mcp")}, no_spend_logs=False)


def write(tmp_path, data) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data), encoding="utf-8")
    return path


def test_a_complete_snippet_passes(tmp_path):
    assert preflight.preflight(write(tmp_path, good_text()), {}, require_loader=False) == []
    assert preflight.main([str(write(tmp_path, good_text()))], {}, require_loader=False) == 0


@pytest.mark.parametrize(
    ("mutate", "item"),
    [
        (lambda c: c.pop("guardrails"), "guardrails[MINT]"),
        (lambda c: c["guardrails"][0]["litellm_params"].update(default_on=False),
         "guardrails[MINT]"),
        (lambda c: c["guardrails"][0]["litellm_params"].update(mode=["pre_mcp_call"]),
         "guardrails[MINT]"),
        (lambda c: c["litellm_settings"].update(callbacks=["x.plugin.MintLogger"]), "callbacks"),
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
    assert any(item in line for line in preflight.preflight(path, {}, require_loader=False))
    assert preflight.main([str(path)], {}, require_loader=False) == 1
    assert item in capsys.readouterr().err


def test_required_environment_is_checked(tmp_path):
    path = write(tmp_path, good_text())
    env = {"LITELLM_STRICT_GUARDRAIL_MODES": "false"}
    assert preflight.main([str(path)], env, require_loader=False) == 1


def test_an_unreadable_or_invalid_config_is_refused(tmp_path, capsys):
    assert preflight.main([str(tmp_path / "missing.yaml")], {}, require_loader=False) == 1
    assert "검사를 할 수 없습니다" in capsys.readouterr().err
    assert preflight.main([str(write(tmp_path, "- a\n- b\n"))], {}) == 1
    assert preflight.main([], {}, require_loader=False) == 2


def test_recommended_rows_do_not_block_startup(tmp_path):
    config = yaml.safe_load(good_text())
    config["litellm_settings"].pop("key_generation_settings")
    assert preflight.main([str(write(tmp_path, config))], {}, require_loader=False) == 0


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
           "START_LOG": str(tmp_path / "start.log"),
           "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(STUB_LOADER)])}
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


def litellm_stand_in(tmp_path, body):
    """진입점이 시작하는 LiteLLM의 대역과, 종료 코드 표식 파일을 정한 환경."""
    good = write(tmp_path, good_text())
    work, env = entrypoint_env(tmp_path)
    script = work / "docker" / "prod_entrypoint.sh"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    marker = tmp_path / "exit-code"
    return work, {**env, "MINT_EXIT_CODE_FILE": str(marker)}, good, marker


@bash
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("exit 0\n", 0),
        ("exit 5\n", 5),
        ('echo 3 > "$MINT_EXIT_CODE_FILE"\nexit 0\n', 3),
        ('echo 3 > "$MINT_EXIT_CODE_FILE"\nexit 1\n', 3),
    ],
    ids=["clean", "own-code", "worker-failure-supervisor-clean", "worker-failure-supervisor-error"],
)
def test_the_entrypoint_exits_3_when_a_worker_left_the_failure_marker(tmp_path, body, expected):
    work, env, good, _ = litellm_stand_in(tmp_path, body)
    result = subprocess.run(["sh", str(ENTRYPOINT), "--config", str(good)], cwd=work, env=env,
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == expected, result.stderr


@bash
def test_a_marker_from_an_earlier_run_does_not_change_the_exit_code(tmp_path):
    work, env, good, marker = litellm_stand_in(tmp_path, "exit 0\n")
    marker.write_text("3", encoding="utf-8")
    result = subprocess.run(["sh", str(ENTRYPOINT), "--config", str(good)], cwd=work, env=env,
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0 and not marker.exists()


@bash
@pytest.mark.parametrize("sent", ["SIGTERM", "SIGINT"])
def test_stop_signals_reach_litellm_as_term_and_its_own_exit_code_is_kept(tmp_path, sent):
    import signal
    import time

    body = (
        """trap 'echo GOT-TERM >> "$START_LOG"; exit 0' TERM\n"""
        'echo READY >> "$START_LOG"\nwhile :; do sleep 0.1; done\n'
    )
    work, env, good, _ = litellm_stand_in(tmp_path, body)
    log = tmp_path / "start.log"
    proc = subprocess.Popen(["sh", str(ENTRYPOINT), "--config", str(good)], cwd=work, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (log.exists() and "READY" in log.read_text()):
            time.sleep(0.05)
        assert "READY" in log.read_text(encoding="utf-8")
        proc.send_signal(getattr(signal, sent))
        assert proc.wait(timeout=15) == 0
    finally:
        proc.kill()
    assert "GOT-TERM" in log.read_text(encoding="utf-8")


@bash
def test_a_litellm_that_dies_of_the_term_we_forwarded_is_a_clean_stop(tmp_path):
    import signal
    import time

    body = 'echo READY >> "$START_LOG"\ntrap - TERM\nwhile :; do sleep 0.1; done\n'
    work, env, good, _ = litellm_stand_in(tmp_path, body)
    log = tmp_path / "start.log"
    proc = subprocess.Popen(["sh", str(ENTRYPOINT), "--config", str(good)], cwd=work, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not (log.exists() and "READY" in log.read_text()):
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=15) == 0
    finally:
        proc.kill()


@bash
def test_a_litellm_that_dies_of_term_on_its_own_keeps_its_status(tmp_path):
    work, env, good, _ = litellm_stand_in(tmp_path, "kill -TERM $$\n")
    result = subprocess.run(["sh", str(ENTRYPOINT), "--config", str(good)], cwd=work, env=env,
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 143


@bash
def test_the_entrypoint_refuses_a_config_without_the_mint_entries(tmp_path):
    config = yaml.safe_load(good_text())
    config.pop("guardrails")
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
            args = [part.format(path) for part in form] + ["--port", "4000", "--detailed_debug"]
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


@bash
@pytest.mark.parametrize(
    "variable",
    ["CONFIG_FILE_PATH", "LITELLM_CONFIG_BUCKET_NAME", "LITELLM_CONFIG_BUCKET_OBJECT_KEY",
     "LITELLM_CONFIG_BUCKET_TYPE"],
)
@pytest.mark.parametrize("value", ["", "/app/other.yaml", "x"])
def test_another_configuration_source_is_refused_even_when_empty(tmp_path, variable, value):
    """LiteLLM은 환경 변수의 설정을 인자보다 먼저 쓴다. 정의만 되어 있어도 시작하지 않는다."""
    good = write(tmp_path, good_text())
    work, env = entrypoint_env(tmp_path)
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "--config", str(good)], cwd=work, env={**env, variable: value},
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode != 0 and not (tmp_path / "start.log").exists()
    assert variable in result.stderr


@bash
def test_a_path_with_spaces_is_checked_and_started_as_one_argument(tmp_path):
    folder = tmp_path / "with space"
    folder.mkdir()
    good = folder / "config file.yaml"
    good.write_text(good_text(), encoding="utf-8")
    result, started = start(tmp_path, ["--config", str(good), "--port", "4000"])
    assert result.returncode == 0 and started, result.stderr
    assert f"STARTED --config {good}" in (tmp_path / "start.log").read_text(encoding="utf-8")


@bash
def test_unrelated_environment_variables_do_not_block_startup(tmp_path):
    good = write(tmp_path, good_text())
    work, env = entrypoint_env(tmp_path)
    result = subprocess.run(
        ["sh", str(ENTRYPOINT), "--config", str(good)], cwd=work,
        env={**env, "CONFIG_FILE_PATHS": "x", "MY_CONFIG_FILE_PATH": "x"},
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


# --- 유효 설정(include 병합)과 기동 뒤 설정 출처 ---------------------------------------------


@pytest.fixture
def stub_loader(monkeypatch):
    """LiteLLM이 설치되지 않은 환경에서 설정 로더 자리를 대신한다(병합 규칙만 흉내 낸다)."""
    for name in [n for n in sys.modules if n == "litellm" or n.startswith("litellm.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(STUB_LOADER))
    yield
    for name in [n for n in sys.modules if n == "litellm" or n.startswith("litellm.")]:
        sys.modules.pop(name, None)


def test_the_merged_effective_configuration_is_what_gets_checked(tmp_path, stub_loader):
    main = yaml.safe_load(good_text())
    main["include"] = ["extra.yaml"]
    (tmp_path / "extra.yaml").write_text(
        yaml.safe_dump({"guardrails": None, "litellm_settings": {"user_url_validation": True}}),
        encoding="utf-8",
    )
    path = write(tmp_path, main)
    assert any("guardrails[MINT]" in line for line in preflight.preflight(path, {}))
    assert preflight.main([str(path)], {}) == 1


def test_an_include_that_appends_a_mint_logger_callback_is_refused(tmp_path, stub_loader):
    main = yaml.safe_load(good_text())
    main["include"] = ["extra.yaml"]
    (tmp_path / "extra.yaml").write_text(
        yaml.safe_dump({"litellm_settings": {"callbacks": ["x.mint_logger"]}}), encoding="utf-8"
    )
    problems = preflight.preflight(write(tmp_path, main), {})
    assert any(line.startswith("callbacks") for line in problems)


def test_a_clean_include_passes(tmp_path, stub_loader):
    main = yaml.safe_load(good_text())
    main["include"] = ["models.yaml"]
    (tmp_path / "models.yaml").write_text(yaml.safe_dump({"model_list": []}), encoding="utf-8")
    assert preflight.preflight(write(tmp_path, main), {}) == []


def test_without_the_loader_an_include_is_refused_and_so_is_startup(tmp_path, monkeypatch):
    for name in [n for n in sys.modules if n == "litellm" or n.startswith("litellm.")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "litellm", None)  # 불러올 수 없는 상태
    main = yaml.safe_load(good_text())
    main["include"] = ["models.yaml"]
    path = write(tmp_path, main)
    assert preflight.main([str(path)], {}, require_loader=False) == 1  # include를 해석할 수 없다
    clean = write(tmp_path, good_text())
    assert preflight.main([str(clean)], {}, require_loader=False) == 0
    assert preflight.main([str(clean)], {}) == 1  # 기동 전 검사는 로더가 없으면 통과시키지 않는다


@pytest.mark.parametrize(
    "env",
    [
        {"CONFIG_FILE_PATH": ""},
        {"LITELLM_CONFIG_BUCKET_NAME": "b"},
        {"LITELLM_CONFIG_BUCKET_OBJECT_KEY": "k"},
        {"LITELLM_CONFIG_BUCKET_TYPE": "s3"},
        {"USE_AWS_KMS": "True", "LITELLM_SECRET_AWS_KMS_CONFIG_FILE_PATH": "x"},
        {"USE_AWS_KMS": "True", "litellm_secret_aws_kms_litellm_config_bucket_name": "x"},
    ],
)
def test_inputs_that_can_move_the_configuration_source_after_startup_are_refused(tmp_path, env):
    assert preflight.source_problems(env)
    path = write(tmp_path, good_text())
    assert preflight.main([str(path)], env, require_loader=False) == 1


def test_kms_without_a_path_alias_and_aliases_without_kms_are_not_refused():
    assert preflight.source_problems({"USE_AWS_KMS": "True"}) == []
    other = {"USE_AWS_KMS": "True", "LITELLM_SECRET_AWS_KMS_OTHER": "x"}
    assert preflight.source_problems(other) == []
    assert preflight.source_problems({"LITELLM_SECRET_AWS_KMS_CONFIG_FILE_PATH": "x"}) == []
    assert preflight.source_problems({"USE_AWS_KMS": "False",
                                      "LITELLM_SECRET_AWS_KMS_CONFIG_FILE_PATH": "x"}) == []


def test_a_dotenv_loaded_by_the_loader_is_seen_after_loading(tmp_path, monkeypatch):
    """로더를 불러오면 .env가 환경에 적재되므로, 환경은 불러온 뒤에 본다."""
    import types

    path = write(tmp_path, good_text())
    environ: dict[str, str] = {}

    class Loader:
        def __call__(self, config_file_path):
            environ["CONFIG_FILE_PATH"] = "/from/dotenv.yaml"  # .env 적재를 흉내 낸다

            async def result():
                return yaml.safe_load(good_text())

            return result()

    module = types.SimpleNamespace(
        ProxyConfig=lambda: types.SimpleNamespace(_get_config_from_file=Loader())
    )
    monkeypatch.setitem(sys.modules, "litellm", types.ModuleType("litellm"))
    monkeypatch.setitem(sys.modules, "litellm.proxy", types.ModuleType("litellm.proxy"))
    monkeypatch.setitem(sys.modules, "litellm.proxy.proxy_server", module)
    problems = preflight.preflight(path, environ)
    assert any("CONFIG_FILE_PATH" in line for line in problems)


@pytest.mark.parametrize(
    "env",
    [
        # 접두사를 두 번 쓴 이름: LiteLLM은 이름 전체에서 접두사를 지운다
        {"USE_AWS_KMS": "True",
         "LITELLM_SECRET_AWS_KMS_LITELLM_SECRET_AWS_KMS_CONFIG_FILE_PATH": "x"},
        # 이름 가운데 접두사가 들어간 이름
        {"USE_AWS_KMS": "True",
         "LITELLM_SECRET_AWS_KMS_CONFIG_litellm_secret_aws_kms_FILE_PATH": "x"},
        # 이름은 접두사로 시작하지 않고 값만 aws_kms/ 인 경우
        {"USE_AWS_KMS": "True", "CONFIG_litellm_secret_aws_kms_FILE_PATH": "aws_kms/abc"},
        {"USE_AWS_KMS": "True", "litellm_secret_aws_kms_litellm_config_bucket_type": "x"},
    ],
)
def test_the_kms_name_normalization_matches_litellms(env):
    assert preflight.source_problems(env)


@pytest.mark.parametrize(
    "env",
    [
        {"USE_AWS_KMS": "True", "CONFIG_litellm_secret_aws_kms_FILE_PATH": "plain-value"},
        {"USE_AWS_KMS": "True", "OTHER": "aws_kms/abc"},
        {"USE_AWS_KMS": "True", "LITELLM_SECRET_AWS_KMS_LITELLM_CONFIG": "x"},
        {"CONFIG_litellm_secret_aws_kms_FILE_PATH": "aws_kms/abc"},
    ],
)
def test_names_that_do_not_become_a_configuration_source_are_not_refused(env):
    assert preflight.source_problems(env) == []
