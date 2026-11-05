"""저장소 밖 비밀 파일의 인자 지문 키."""

import base64
import os
import stat
import threading

import pytest

from mint_litellm.control import secrets_file
from mint_litellm.control.errors import ControlError

POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX 파일 권한 시험")


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
    assert [p.name for p in path.parent.iterdir()] == ["secrets.env"]  # 임시 파일이 남지 않는다


@POSIX
def test_secret_file_is_created_private(tmp_path):
    path = tmp_path / "secrets.env"
    secrets_file.ensure_fingerprint_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@POSIX
def test_an_existing_file_with_loose_permissions_is_reported_not_repaired(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("MINT_FINGERPRINT_KEY=existing-key-value\n", encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(ControlError, match="0600"):
        secrets_file.ensure_fingerprint_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o644  # 조용히 고치지 않는다
    assert path.read_text(encoding="utf-8") == "MINT_FINGERPRINT_KEY=existing-key-value\n"


@POSIX
def test_an_existing_private_file_is_never_overwritten(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("MINT_FINGERPRINT_KEY=kept\nOTHER=1\n", encoding="utf-8")
    path.chmod(0o600)
    assert secrets_file.ensure_fingerprint_key(path) is False
    assert path.read_text(encoding="utf-8") == "MINT_FINGERPRINT_KEY=kept\nOTHER=1\n"


@POSIX
def test_an_existing_file_without_a_key_fails_instead_of_being_overwritten(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("OTHER=1\n", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(ControlError, match="MINT_FINGERPRINT_KEY가 없습니다"):
        secrets_file.ensure_fingerprint_key(path)
    assert path.read_text(encoding="utf-8") == "OTHER=1\n"


def test_concurrent_initializations_leave_exactly_one_key(tmp_path):
    path = tmp_path / "mint" / "secrets.env"
    results: list[object] = []
    start = threading.Barrier(8)

    def run():
        start.wait()
        try:
            created = secrets_file.ensure_fingerprint_key(path)
            results.append((created, read(path)["MINT_FINGERPRINT_KEY"]))
        except ControlError as error:
            results.append(error)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert all(isinstance(r, tuple) for r in results), results
    assert sum(1 for created, _ in results if created) == 1
    assert len({key for _, key in results}) == 1  # 모두 같은 키를 읽었다
    assert [p.name for p in path.parent.iterdir()] == ["secrets.env"]


def test_two_deployments_get_different_keys(tmp_path):
    first, second = tmp_path / "a.env", tmp_path / "b.env"
    secrets_file.ensure_fingerprint_key(first)
    secrets_file.ensure_fingerprint_key(second)
    assert read(first)["MINT_FINGERPRINT_KEY"] != read(second)["MINT_FINGERPRINT_KEY"]
