"""저장소 밖 비밀 파일. 호출별 승인의 인자 지문 키를 한 번만 생성해 둔다."""

import os
import secrets
import stat
from pathlib import Path

from mint_litellm.control.errors import ControlError

KEY_NAME = "MINT_FINGERPRINT_KEY"
VERSION_NAME = "MINT_FINGERPRINT_KEY_VERSION"
DEFAULT_PATH = "~/.config/mint/secrets.env"


def _values(path: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            found[name.strip()] = value.strip()
    return found


def _check_existing(path: Path) -> bool:
    """이미 있는 파일을 검사한다. 키가 있으면 True, 권한이 0600이 아니거나 키가 없으면 실패한다."""
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ControlError(
            f"비밀 파일 {path}의 권한이 0600이 아닙니다. 키가 노출되었을 수 있으니 권한을 "
            "0600으로 고치고 키 노출 여부를 확인하십시오(자동으로 고치지 않습니다)."
        )
    if not _values(path).get(KEY_NAME):
        raise ControlError(
            f"비밀 파일 {path}에 {KEY_NAME}가 없습니다. 파일을 덮어쓰지 않으므로 키를 직접 "
            "추가하거나 파일을 지운 뒤 다시 실행하십시오."
        )
    return True


def ensure_fingerprint_key(path: Path) -> bool:
    """파일이 없을 때만 무작위 32바이트(base64url) 키를 만들어 0600 파일로 게시하고 True를 돌려준다.

    내용을 임시 파일에 다 쓴 뒤 하드 링크로 게시하므로 동시에 실행된 쪽 가운데 한 곳만 만들고,
    나머지는 완성된 파일을 읽기만 한다. 이미 있는 파일은 덮어쓰지 않는다. 파일을 마운트한
    디렉터리의 소유자에게 소유권을 넘겨 컨테이너 밖의 사용자가 읽을 수 있게 한다.
    """
    path = path.expanduser()
    if path.exists():
        _check_existing(path)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(
                f"{KEY_NAME}={secrets.token_urlsafe(32)}\n{VERSION_NAME}=1\n"
            )
            output.flush()
            os.fsync(output.fileno())
        try:
            owner = path.parent.stat()
            os.chown(temp, owner.st_uid, owner.st_gid)
        except (AttributeError, OSError):
            pass  # 소유자를 넘길 수 없는 환경(권한 부족, Windows)에서는 그대로 둔다.
        try:
            os.link(temp, path)
        except FileExistsError:
            _check_existing(path)
            return False
    finally:
        temp.unlink(missing_ok=True)
    return True
