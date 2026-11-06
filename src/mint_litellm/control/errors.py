"""제어 평면 오류. 메시지는 운영자에게 그대로 출력된다."""

from mint_litellm.types.ids import new_uuid7


class ControlError(Exception):
    """명령을 수행할 수 없는 상태. 트랜잭션은 롤백되고 종료 코드는 ``exit_code``이다."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class AuthError(ControlError):
    """토큰이 없거나 유효하지 않거나 역할이 부족하다."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=4)


class AdminCode:
    """관리 오류 코드. API 오류 본문의 ``code``이다."""

    SEPARATION_OF_DUTIES = "MINT-ADMIN-SEPARATION-OF-DUTIES"
    CONFLICT = "MINT-ADMIN-CONFLICT"
    STALE_EVIDENCE = "MINT-ADMIN-STALE-EVIDENCE"
    NOT_FOUND = "MINT-ADMIN-NOT-FOUND"
    FORBIDDEN = "MINT-ADMIN-FORBIDDEN"
    ALL = frozenset({SEPARATION_OF_DUTIES, CONFLICT, STALE_EVIDENCE, NOT_FOUND, FORBIDDEN})


class AdminError(ControlError):
    """관리 오류. ``body()``가 API 오류 본문 ``{code, message, reference}``이다.

    ``reference``는 사건이 있으면 그 ``source_key``, 없으면 요청 식별자(uuid7)이다.
    """

    def __init__(
        self, code: str, message: str, *, reference: str | None = None, exit_code: int = 1
    ) -> None:
        super().__init__(message, exit_code)
        if code not in AdminCode.ALL:
            raise ValueError(f"unknown admin error code {code!r}")
        self.code = code
        self.message = message
        self.reference = reference or str(new_uuid7())

    def body(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "reference": self.reference}


def not_found(message: str) -> AdminError:
    return AdminError(AdminCode.NOT_FOUND, message)


def conflict(message: str) -> AdminError:
    return AdminError(AdminCode.CONFLICT, message)


def separation_of_duties(message: str) -> AdminError:
    return AdminError(AdminCode.SEPARATION_OF_DUTIES, message)


def forbidden(message: str, exit_code: int = 1) -> AdminError:
    return AdminError(AdminCode.FORBIDDEN, message, exit_code=exit_code)


def stale_evidence(message: str) -> AdminError:
    return AdminError(AdminCode.STALE_EVIDENCE, message)
