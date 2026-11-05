"""배포 스크립트의 환경 파일 처리. docker는 호출 인자만 기록하는 대역으로 바꾼다."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import mint_litellm

SCRIPT = Path(mint_litellm.__file__).resolve().parents[2] / "deploy" / "mint.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash가 필요하다")


def run(tmp_path: Path, env_text: str, *args: str):
    deploy = tmp_path / "deploy"
    deploy.mkdir(exist_ok=True)
    shutil.copy(SCRIPT, deploy / "mint.sh")
    (deploy / ".env").write_text(env_text, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "docker"
    fake.write_text('#!/bin/sh\necho "$@" >> "$FAKE_DOCKER_LOG"\n', encoding="utf-8")
    fake.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
    }
    result = subprocess.run(
        [BASH, "mint.sh", *args], cwd=deploy, env=env, capture_output=True, text=True, encoding="utf-8"
    )
    return result, deploy / ".env", home


def test_an_env_file_without_the_secrets_directory_key_still_works(tmp_path):
    result, env_file, home = run(tmp_path, "POSTGRES_PASSWORD=x\n", "cli", "status")
    assert result.returncode == 0, result.stderr
    lines = env_file.read_text(encoding="utf-8").splitlines()
    configured = [ln for ln in lines if ln.startswith("MINT_SECRETS_DIR=")]
    assert len(configured) == 1 and configured[0].endswith("/home/.config/mint")
    assert (home / ".config" / "mint").is_dir()
    if os.name != "nt":
        assert stat.S_IMODE((home / ".config" / "mint").stat().st_mode) == 0o700
    again, _, _ = run(tmp_path, env_file.read_text(encoding="utf-8"), "cli", "status")
    assert again.returncode == 0
    assert env_file.read_text(encoding="utf-8").count("MINT_SECRETS_DIR=") == 1


def test_an_empty_secrets_directory_setting_is_replaced_not_duplicated(tmp_path):
    result, env_file, _ = run(tmp_path, "MINT_SECRETS_DIR=\nOTHER=1\n", "cli", "status")
    assert result.returncode == 0, result.stderr
    text = env_file.read_text(encoding="utf-8")
    assert text.count("MINT_SECRETS_DIR=") == 1 and "OTHER=1" in text


def test_a_configured_secrets_directory_is_used_and_no_secret_file_is_created(tmp_path):
    target = tmp_path / "outside"
    result, _, _ = run(tmp_path, f"MINT_SECRETS_DIR={target}\n", "cli", "status")
    assert result.returncode == 0, result.stderr
    assert target.is_dir() and not (target / "secrets.env").exists()


def test_missing_env_file_and_unknown_commands_fail_clearly(tmp_path):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    shutil.copy(SCRIPT, deploy / "mint.sh")
    missing = subprocess.run([BASH, "mint.sh", "status"], cwd=deploy, capture_output=True,
                             text=True, encoding="utf-8")
    assert missing.returncode != 0 and ".env" in missing.stderr
    unknown = subprocess.run([BASH, "mint.sh", "restore"], cwd=deploy, capture_output=True,
                             text=True, encoding="utf-8")
    assert unknown.returncode != 0 and "restore" not in unknown.stdout
