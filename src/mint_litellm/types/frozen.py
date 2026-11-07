"""기록에 담기는 컬렉션의 불변 표현과 JSON 값 검사.

``frozen=True`` 모델도 필드 안의 ``dict``·``list``는 바꿀 수 있어, 기록 직전에 내용이
달라질 수 있다. 사건 본문 같은 값은 생성할 때 복사해 변경할 수 없는 컬렉션으로 바꾼다.
이 컬렉션은 ``dict``·``list``의 하위 클래스라 JSON 직렬화와 동등 비교는 그대로 된다.
"""

import math
from typing import Any, NoReturn

MAX_JSON_DEPTH = 64


def _immutable(self: object, *args: object, **kwargs: object) -> NoReturn:
    raise TypeError(f"{type(self).__name__} is immutable")


class FrozenDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenDict, (dict(self),))


class FrozenList(list):
    __setitem__ = __delitem__ = __iadd__ = __imul__ = _immutable
    append = extend = insert = remove = pop = clear = sort = reverse = _immutable

    def __reduce__(self) -> tuple[Any, ...]:
        return (FrozenList, (list(self),))


def no_floats(value: object) -> None:
    """실수 값이 하나라도 있으면 ``ValueError``. 봉인·서명의 정규 JSON은 실수를 받지 않는다."""
    if isinstance(value, float):
        raise ValueError("JSON numbers must be integers (floats are not allowed)")
    if isinstance(value, dict):
        for item in value.values():
            no_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            no_floats(item)


def json_value(value: object, _depth: int = 0) -> Any:
    """JSON 값(문자열·정수·실수·불리언·None·리스트·문자열 키 딕셔너리)만 받아 동결 사본을 만든다.

    튜플·집합·UUID·시각처럼 JSON 왕복에서 형태가 바뀌는 값과 유한하지 않은 실수는 거부한다.
    """
    if _depth > MAX_JSON_DEPTH:
        raise ValueError("JSON value is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, list):
        return FrozenList(json_value(item, _depth + 1) for item in value)
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
        return FrozenDict({key: json_value(item, _depth + 1) for key, item in value.items()})
    raise ValueError(f"{type(value).__name__} is not a JSON value")
