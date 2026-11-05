"""진입점 스크립트 시험용 설정 로더 자리. 실제 LiteLLM의 병합 규칙(목록은 이어 붙이고 나머지는
덮어쓰기, 포함 파일은 선언한 파일 옆에서 찾기)만 흉내 낸다. 실제 로더와의 일치는 파생 이미지에서
따로 확인한다."""

import os
from pathlib import Path

import yaml


class ProxyConfig:
    async def _get_config_from_file(self, config_file_path=None):
        return self._load(Path(config_file_path), seen=set())

    def _load(self, path, seen):
        seen.add(path.resolve())
        if not path.is_file():
            raise Exception(f"Config file not found: {path}")
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for include in config.pop("include", []) or []:
            target = (path.parent / include).resolve()
            if target in seen:
                continue
            for key, value in self._load(target, seen).items():
                if isinstance(value, list) and isinstance(config.get(key), list):
                    config[key] = [*config[key], *value]
                else:
                    config[key] = value
        return config


os.environ.get("LITELLM_MODE")
