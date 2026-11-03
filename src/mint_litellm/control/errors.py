"""제어 평면 오류. 메시지는 운영자에게 그대로 출력된다."""


class ControlError(Exception):
    """명령을 수행할 수 없는 상태. 트랜잭션은 롤백되고 종료 코드는 ``exit_code``이다."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class AuthError(ControlError):
    """토큰이 없거나 유효하지 않거나 역할이 부족하다."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=4)
