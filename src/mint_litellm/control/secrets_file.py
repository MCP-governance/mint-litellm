"""저장소 밖 비밀 파일. 호출별 승인의 인자 지문 키를 한 번 생성해 둔다."""

import os
import secrets
from pathlib import Path

KEY_NAME = "MINT_FINGERPRINT_KEY"
VERSION_NAME = "MINT_FINGERPRINT_KEY_VERSION"
DEFAULT_PATH = "~/.config/mint/secrets.env"


def _values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            found[name.strip()] = value.strip()
    return found


def ensure_fingerprint_key(path: Path) -> bool:
    """키가 없으면 무작위 32바이트(base64url)를 만들어 0600 파일에 기록하고 True를 돌려준다.

    이미 있는 키는 바꾸지 않는다. 키를 바꾸면 이미 요청된 호출별 승인의 지문이 맞지 않게 된다.
    파일을 마운트한 디렉터리의 소유자에게 파일을 넘겨, 컨테이너 밖의 사용자가 읽을 수 있게 한다.
    """
    path = path.expanduser()
    if _values(path).get(KEY_NAME):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    kept = [
        line
        for line in (path.read_text(encoding="utf-8").splitlines() if path.exists() else [])
        if line.partition("=")[0].strip() not in (KEY_NAME, VERSION_NAME)
    ]
    version = _values(path).get(VERSION_NAME) or "1"
    lines = [*kept, f"{KEY_NAME}={secrets.token_urlsafe(32)}", f"{VERSION_NAME}={version}"]
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    try:
        owner = path.parent.stat()
        os.chown(path, owner.st_uid, owner.st_gid)
    except (AttributeError, OSError):
        pass  # 소유자를 넘길 수 없는 환경(권한 부족, Windows)에서는 그대로 둔다.
    return True
