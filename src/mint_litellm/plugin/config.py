"""플러그인 설정. 환경 변수에서만 읽고, 잘못된 값은 ``RuntimeError``로 알린다.

LiteLLM은 가드레일 생성자가 ``ValueError``·``TypeError``를 던지면 가드레일 없이 기동하므로
이 모듈의 모든 오류는 ``RuntimeError``이다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from mint_litellm.types.fingerprint import decode_fingerprint_key
from mint_litellm.types.settings import Mode

_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

DEFAULT_BUFFER_DIR = "/var/lib/mint/buffer"
DEFAULT_BUFFER_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_DECISION_BUDGET_MS = 300
DEFAULT_SELFCHECK_GRACE_SECONDS = 120.0
SELFCHECK_GRACE_RANGE = (10, 600)


@dataclass(frozen=True)
class PluginConfig:
    instance_id: str
    database_url: str
    opa_url: str
    mode_ceiling: Mode = Mode.ENFORCE
    required: bool = True
    high_risk_tools: frozenset[str] = frozenset()
    buffer_dir: Path = Path(DEFAULT_BUFFER_DIR)
    buffer_max_bytes: int = DEFAULT_BUFFER_MAX_BYTES
    db_pool_size: int = 8
    decision_budget_seconds: float = DEFAULT_DECISION_BUDGET_MS / 1000
    fingerprint_key: bytes | None = None
    fingerprint_key_version: int = 1
    readiness_url: str | None = None
    selfcheck_grace_seconds: float = DEFAULT_SELFCHECK_GRACE_SECONDS
    allowed_pre_hooks: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> PluginConfig:
        instance_id = _required(env, "MINT_INSTANCE_ID")
        if not _IDENT.fullmatch(instance_id):
            raise RuntimeError("MINT_INSTANCE_ID must be an identifier of letters, digits, . _ -")
        mode_text = env.get("MINT_MODE", Mode.ENFORCE.value).strip().lower()
        try:
            ceiling = Mode(mode_text)
        except ValueError:
            raise RuntimeError("MINT_MODE must be enforce or observe") from None
        key = _fingerprint_key(env.get("MINT_FINGERPRINT_KEY"))
        return cls(
            instance_id=instance_id,
            database_url=_required(env, "MINT_DATABASE_URL"),
            opa_url=_required(env, "MINT_OPA_URL"),
            mode_ceiling=ceiling,
            required=_boolean(env, "MINT_REQUIRED", True),
            high_risk_tools=frozenset(
                t.strip() for t in env.get("MINT_HIGH_RISK_TOOLS", "").split(",") if t.strip()
            ),
            buffer_dir=Path(env.get("MINT_BUFFER_DIR", DEFAULT_BUFFER_DIR)),
            buffer_max_bytes=_integer(env, "MINT_BUFFER_MAX_BYTES", DEFAULT_BUFFER_MAX_BYTES, 1024),
            db_pool_size=_integer(env, "MINT_DB_POOL_SIZE", 8, 1),
            decision_budget_seconds=_integer(
                env, "MINT_DECISION_BUDGET_MS", DEFAULT_DECISION_BUDGET_MS, 10
            )
            / 1000,
            fingerprint_key=key,
            fingerprint_key_version=_integer(env, "MINT_FINGERPRINT_KEY_VERSION", 1, 1),
            readiness_url=env.get("MINT_READINESS_URL") or None,
            allowed_pre_hooks=_class_names(env.get("MINT_ALLOWED_PRE_HOOKS", "")),
            selfcheck_grace_seconds=float(
                _integer(
                    env,
                    "MINT_SELFCHECK_GRACE_SECONDS",
                    int(DEFAULT_SELFCHECK_GRACE_SECONDS),
                    SELFCHECK_GRACE_RANGE[0],
                    SELFCHECK_GRACE_RANGE[1],
                )
            ),
        )


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _fingerprint_key(raw: str | None) -> bytes | None:
    if raw is None:
        return None
    try:
        return decode_fingerprint_key(raw)
    except ValueError as exc:
        raise RuntimeError(f"MINT_FINGERPRINT_KEY is invalid: {exc}") from None


def _class_names(raw: str) -> frozenset[str]:
    names = [item.strip() for item in raw.split(",") if item.strip()]
    for name in names:
        if not _CLASS_NAME.fullmatch(name):
            raise RuntimeError("MINT_ALLOWED_PRE_HOOKS must list class names separated by commas")
    return frozenset(names)


def _boolean(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    text = raw.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _integer(
    env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int | None = None
) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer") from None
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise RuntimeError(f"{name} must be at most {maximum}")
    return value
