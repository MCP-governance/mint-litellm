"""LiteLLM이 설정 파일을 읽을 때 쓰는 로더의 호출. 1.106.6의 비공개 심볼에 의존하므로 여기에 둔다.

``ProxyConfig._get_config_from_file``은 YAML을 읽고 ``include``를 같은 규칙(목록은 이어 붙이고
나머지는 덮어쓰기)으로 병합한다. 이 모듈이 LiteLLM을 불러오면 시작할 때처럼 ``.env``가 환경에
적재된다.
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

import yaml

from mint_litellm.control.errors import ControlError


def _loader() -> Any | None:
    try:
        from litellm.proxy.proxy_server import ProxyConfig
    except Exception:  # LiteLLM이 없거나 불러오지 못하면 로더가 없는 것이다.
        return None
    return getattr(ProxyConfig(), "_get_config_from_file", None)


def _utf8_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
    if "b" not in mode:
        kwargs.setdefault("encoding", "utf-8")
    return open(file, mode, *args, **kwargs)  # noqa: SIM115


def _run_loader(loader: Any, path: Path) -> Any:
    """LiteLLM의 로더는 인코딩 없이 파일을 열어 기본 인코딩이 UTF-8이 아닌 환경(Windows)에서
    한국어 설정을 읽지 못한다. 로더가 속한 모듈에서만 ``open``을 UTF-8로 고정해 호출한다."""
    module = sys.modules.get(getattr(loader, "__module__", ""))
    if module is None or "open" in vars(module):
        return asyncio.run(loader(config_file_path=str(path)))
    module.open = _utf8_open  # type: ignore[attr-defined]
    try:
        return asyncio.run(loader(config_file_path=str(path)))
    finally:
        del module.open  # type: ignore[attr-defined]


def load_effective_config(path: Path, *, require_loader: bool) -> dict[str, Any]:
    """LiteLLM이 적재할 병합된 설정.

    로더를 쓸 수 없으면 ``require_loader``일 때는 거부하고, 아니면 주 파일을 직접 읽되 ``include``가
    있으면 해석할 수 없으므로 거부한다.
    """
    loader = _loader()
    if loader is not None:
        try:
            config = _run_loader(loader, path)
        except Exception as error:
            raise ControlError(f"LiteLLM 설정 로더가 설정을 읽지 못했습니다: {error}") from None
        if not isinstance(config, dict):
            raise ControlError("LiteLLM 설정 로더가 매핑이 아닌 값을 돌려주었습니다.")
        return config
    if require_loader:
        raise ControlError(
            "LiteLLM 설정 로더를 쓸 수 없어 유효 설정을 확인할 수 없습니다(이 환경에서는 기동 전 "
            "검사를 통과시키지 않습니다)."
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ControlError(f"LiteLLM 설정 파일을 읽을 수 없습니다: {error}") from None
    if not isinstance(data, dict):
        raise ControlError("LiteLLM 설정 파일의 최상위가 매핑이 아닙니다.")
    if "include" in data:
        raise ControlError(
            "설정에 include가 있으나 LiteLLM 설정 로더를 쓸 수 없어 병합 결과를 확인하지 못합니다."
        )
    return data
