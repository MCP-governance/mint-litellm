"""저장소 밖 비밀 파일의 인자 지문 키."""

import base64
import os
import stat

import pytest

from mint_litellm.control import secrets_file


def read(path):
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())


def test_key_is_generated_once_with_enough_randomness(tmp_path):
    path = tmp_path / "mint" / "secrets.env"
    assert secrets_file.ensure_fingerprint_key(path) is True
    values = read(path)
    key = values["MINT_FINGERPRINT_KEY"]
    assert len(base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))) >= 32
    assert values["MINT_FINGERPRINT_KEY_VERSION"] == "1"
    assert secrets_file.ensure_fingerprint_key(path) is False
    assert read(path)["MINT_FINGERPRINT_KEY"] == key


@pytest.mark.skipif(os.name == "nt", reason="POSIX 파일 권한 시험")
def test_secret_file_is_private(tmp_path):
    path = tmp_path / "secrets.env"
    secrets_file.ensure_fingerprint_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_other_entries_and_the_version_survive_when_the_key_is_missing(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("OTHER=1\nMINT_FINGERPRINT_KEY=\nMINT_FINGERPRINT_KEY_VERSION=4\n",
                    encoding="utf-8")
    assert secrets_file.ensure_fingerprint_key(path) is True
    values = read(path)
    assert values["OTHER"] == "1" and values["MINT_FINGERPRINT_KEY_VERSION"] == "4"
    assert values["MINT_FINGERPRINT_KEY"]


def test_two_deployments_get_different_keys(tmp_path):
    first, second = tmp_path / "a.env", tmp_path / "b.env"
    secrets_file.ensure_fingerprint_key(first)
    secrets_file.ensure_fingerprint_key(second)
    assert read(first)["MINT_FINGERPRINT_KEY"] != read(second)["MINT_FINGERPRINT_KEY"]
