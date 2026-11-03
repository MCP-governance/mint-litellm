"""환경 변수에서 읽는 제어 평면 설정."""

import os
from dataclasses import dataclass
from pathlib import Path

from mint_litellm.control.errors import ControlError
from mint_litellm.control.litellm_admin import LiteLLMAdmin
from mint_litellm.types.settings import Mode


@dataclass(frozen=True)
class ControlConfig:
    database_url: str | None
    instance_id: str
    litellm_url: str | None
    litellm_key: str | None
    litellm_config: Path | None
    mode_ceiling: Mode
    replicas: int

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ControlConfig":
        env = dict(os.environ) if env is None else env
        raw_mode = env.get("MINT_MODE", "enforce")
        try:
            ceiling = Mode(raw_mode)
        except ValueError:
            raise ControlError(f"MINT_MODE는 enforce 또는 observe여야 합니다: {raw_mode}") from None
        config_path = env.get("MINT_LITELLM_CONFIG")
        return cls(
            database_url=env.get("MINT_DATABASE_URL"),
            instance_id=env.get("MINT_INSTANCE_ID", "default"),
            litellm_url=env.get("MINT_LITELLM_URL"),
            litellm_key=env.get("MINT_LITELLM_KEY"),
            litellm_config=Path(config_path) if config_path else None,
            mode_ceiling=ceiling,
            replicas=int(env.get("MINT_REPLICAS", "1")),
        )

    def require_database(self) -> str:
        if not self.database_url:
            raise ControlError("MINT_DATABASE_URL이 설정되어 있지 않습니다.")
        return self.database_url

    def litellm(self) -> LiteLLMAdmin:
        if not self.litellm_url or not self.litellm_key:
            raise ControlError("MINT_LITELLM_URL과 MINT_LITELLM_KEY가 필요합니다.")
        return LiteLLMAdmin(self.litellm_url, self.litellm_key)
