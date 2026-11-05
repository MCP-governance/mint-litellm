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


def test_concurrent_initializations_leave_exactly_one_key(tmp_path, monkeypatch):
    """모든 실행이 ``파일 없음``을 본 상태에서 키를 만드는 지점에 함께 멈췄다가 동시에 진행한다."""
    path = tmp_path / "mint" / "secrets.env"
    count = 8
    gate = threading.Barrier(count, timeout=10)
    real_new_key = secrets_file.new_key

    def new_key_after_everyone_is_ready():
        gate.wait()  # 구현이 어떻든 키를 만들려면 이 지점을 지난다
        return real_new_key()

    monkeypatch.setattr(secrets_file, "new_key", new_key_after_everyone_is_ready)
    results: list[object] = []

    def run():
        try:
            created = secrets_file.ensure_fingerprint_key(path)
            results.append((created, read(path)["MINT_FINGERPRINT_KEY"]))
        except Exception as error:  # noqa: BLE001
            results.append(error)

    threads = [threading.Thread(target=run) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not any(t.is_alive() for t in threads)
    assert len(results) == count and all(isinstance(r, tuple) for r in results), results
    assert sum(1 for created, _ in results if created) == 1
    assert len({key for _, key in results}) == 1  # 모두 같은 키를 읽었다
    assert [p.name for p in path.parent.iterdir()] == ["secrets.env"]


def test_two_deployments_get_different_keys(tmp_path):
    first, second = tmp_path / "a.env", tmp_path / "b.env"
    secrets_file.ensure_fingerprint_key(first)
    secrets_file.ensure_fingerprint_key(second)
    assert read(first)["MINT_FINGERPRINT_KEY"] != read(second)["MINT_FINGERPRINT_KEY"]


@POSIX
def test_loading_a_key_from_a_file_applies_the_same_permission_check(tmp_path):
    path = tmp_path / "secrets.env"
    secrets_file.ensure_fingerprint_key(path)
    key, version = secrets_file.load_fingerprint_key({}, path)
    assert key and version == 1
    path.chmod(0o644)
    with pytest.raises(ControlError, match="0600"):
        secrets_file.load_fingerprint_key({}, path)
    assert secrets_file.load_fingerprint_key({"MINT_FINGERPRINT_KEY": "from-env"}, path)[0] == (
        "from-env")  # 환경 변수로 받은 키는 파일을 읽지 않는다


@POSIX
def test_symbolic_links_are_refused_for_the_file_and_its_directory(tmp_path):
    real = tmp_path / "real.env"
    secrets_file.ensure_fingerprint_key(real)
    link = tmp_path / "link.env"
    link.symlink_to(real)
    with pytest.raises(ControlError, match="심볼릭 링크"):
        secrets_file.load_fingerprint_key({}, link)
    with pytest.raises(ControlError, match="심볼릭 링크"):
        secrets_file.ensure_fingerprint_key(link)
    directory_link = tmp_path / "dir-link"
    directory_link.symlink_to(tmp_path)
    with pytest.raises(ControlError, match="심볼릭 링크"):
        secrets_file.ensure_fingerprint_key(directory_link / "new.env")


@POSIX
def test_the_directory_is_created_private_and_shared_directories_are_refused(tmp_path):
    created = tmp_path / "a" / "b"
    secrets_file.ensure_fingerprint_key(created / "secrets.env")
    assert stat.S_IMODE(created.stat().st_mode) == 0o700
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(ControlError, match="다른 사용자가 쓸 수"):
        secrets_file.ensure_fingerprint_key(shared / "secrets.env")
    assert not (shared / "secrets.env").exists()


@POSIX
def test_an_existing_file_is_refused_under_a_linked_or_shared_directory(tmp_path):
    real = tmp_path / "real"
    secrets_file.ensure_fingerprint_key(real / "secrets.env")
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    with pytest.raises(ControlError, match="심볼릭 링크"):
        secrets_file.load_fingerprint_key({}, alias / "secrets.env")
    with pytest.raises(ControlError, match="심볼릭 링크"):
        secrets_file.ensure_fingerprint_key(alias / "secrets.env")
    real.chmod(0o777)  # 파일은 0600이어도 디렉터리를 다른 사용자가 바꿀 수 있다
    with pytest.raises(ControlError, match="다른 사용자가 쓸 수"):
        secrets_file.load_fingerprint_key({}, real / "secrets.env")
    with pytest.raises(ControlError, match="다른 사용자가 쓸 수"):
        secrets_file.ensure_fingerprint_key(real / "secrets.env")
    real.chmod(0o700)
    assert secrets_file.load_fingerprint_key({}, real / "secrets.env")[0]


@POSIX
def test_invalid_utf8_and_unreadable_files_become_control_errors(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_bytes(b"MINT_FINGERPRINT_KEY=\xff\xfe\n")
    path.chmod(0o600)
    with pytest.raises(ControlError, match="읽을 수 없습니다"):
        secrets_file.load_fingerprint_key({}, path)
    with pytest.raises(ControlError, match="읽을 수 없습니다"):
        secrets_file.ensure_fingerprint_key(path)
