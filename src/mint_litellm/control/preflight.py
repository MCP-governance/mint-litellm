"""LiteLLM 기동 전 설정 검사. 파생 이미지의 진입점이 LiteLLM을 시작하기 전에 실행한다.

``python -m mint_litellm.control.preflight <설정 파일>``. 검사 대상은 주 파일이 아니라 LiteLLM의
설정 로더가 ``include``까지 병합한 유효 설정이다. 설정 파일을 읽지 못하거나, 로더를 쓸 수 없거나,
기동 뒤에 설정 출처를 바꿀 수 있는 입력이 있거나, 필수 행이 하나라도 어긋나면 0이 아닌 값으로
끝나고, 진입점은 그 경우 LiteLLM을 시작하지 않는다.
"""

import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from mint_litellm.control import checks, litellm_config
from mint_litellm.control.checks import Level
from mint_litellm.control.errors import ControlError

# LiteLLM 1.106.6이 시작할 때 설정 파일 인자보다 먼저 쓰는 출처(환경 변수).
CONFIG_SOURCE_VARIABLES = (
    "CONFIG_FILE_PATH",
    "LITELLM_CONFIG_BUCKET_NAME",
    "LITELLM_CONFIG_BUCKET_OBJECT_KEY",
    "LITELLM_CONFIG_BUCKET_TYPE",
)
_KMS_PREFIX = "litellm_secret_aws_kms"
_KMS_ALIAS = re.compile("litellm_secret_aws_kms_", re.IGNORECASE)


def source_problems(env: Mapping[str, str]) -> list[str]:
    """설정 출처를 인자 밖으로 옮길 수 있는 환경 변수. ``.env``가 적재된 뒤의 환경을 받아야 한다."""
    problems = [
        f"다른 설정 출처({name})가 정의되어 있습니다."
        for name in CONFIG_SOURCE_VARIABLES
        if name in env
    ]
    if env.get("USE_AWS_KMS") == "True":
        # LiteLLM(decrypt_env_var)은 이름이 litellm_secret_aws_kms로 시작하거나 값이 aws_kms/로
        # 시작하는 변수를 풀어, 이름에서 접두사(대소문자 무시, 모든 위치)를 지운 이름의 환경 변수로
        # 만든다. 그 이름이 설정 출처 이름이면 기동 중에 설정이 바뀐다. 값은 이 검사가 볼 수 없다.
        sources = {name.upper() for name in CONFIG_SOURCE_VARIABLES}
        for name, value in env.items():
            targeted = name.lower().startswith(_KMS_PREFIX) or str(value).startswith("aws_kms/")
            if targeted and _KMS_ALIAS.sub("", name).upper() in sources:
                problems.append(
                    f"USE_AWS_KMS와 설정 경로 별칭({name})이 함께 있어 기동 중에 설정 출처가 "
                    "바뀔 수 있습니다."
                )
    return problems


def preflight(path: Path, env: Mapping[str, str], *, require_loader: bool = True) -> list[str]:
    """필수 설정 위반 목록. 비어 있으면 통과이다."""
    config = litellm_config.load_effective(path, require_loader=require_loader)
    problems = source_problems(env)  # 로더를 불러온 뒤에 본다: .env가 환경에 적재된다.
    report = checks.run_checks(config, env, None, compat_probe=lambda: [])
    problems += [f"{f.item}: {f.message}" for f in report.findings if f.level is Level.FAIL]
    return problems


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    *,
    require_loader: bool = True,
) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("사용법: python -m mint_litellm.control.preflight <LiteLLM 설정 파일>",
              file=sys.stderr)
        return 2
    try:
        problems = preflight(
            Path(args[0]), os.environ if env is None else env, require_loader=require_loader
        )
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
