"""LiteLLM 기동 전 설정 검사. 파생 이미지의 진입점이 LiteLLM을 시작하기 전에 실행한다.

``python -m mint_litellm.control.preflight <설정 파일>``. 설정 파일을 읽지 못하거나 필수 행이
하나라도 어긋나면 0이 아닌 값으로 끝나고, 진입점은 그 경우 LiteLLM을 시작하지 않는다.
"""

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from mint_litellm.control import checks, litellm_config
from mint_litellm.control.checks import Level
from mint_litellm.control.errors import ControlError


def preflight(path: Path, env: Mapping[str, str]) -> list[str]:
    """필수 설정 위반 목록. 비어 있으면 통과이다."""
    config = litellm_config.load_config(path)
    report = checks.run_checks(config, env, None, compat_probe=lambda: [])
    return [f"{f.item}: {f.message}" for f in report.findings if f.level is Level.FAIL]


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("사용법: python -m mint_litellm.control.preflight <LiteLLM 설정 파일>",
              file=sys.stderr)
        return 2
    try:
        problems = preflight(Path(args[0]), os.environ if env is None else env)
    except ControlError as error:
        print(f"기동 전 설정 검사를 할 수 없습니다: {error}", file=sys.stderr)
        return 1
    if problems:
        print("MINT 필수 설정이 어긋나 LiteLLM을 시작하지 않습니다:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
