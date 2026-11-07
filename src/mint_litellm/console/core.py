"""콘솔 공통: 설정, 요청마다 여는 DB 연결, 인증(세션 쿠키 또는 운영자 토큰), 오류 변환."""

import hmac
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import psycopg
from fastapi import Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from mint_litellm.control import operators
from mint_litellm.control.config import ControlConfig
from mint_litellm.control.context import Context, Operator
from mint_litellm.control.db.connection import connect
from mint_litellm.control.errors import AdminCode, AdminError, AuthError, ControlError
from mint_litellm.control.litellm_admin import LiteLLMAdmin
from mint_litellm.control.services import console_sessions
from mint_litellm.control.stage2_config import Stage2Settings

COOKIE = "__Host-mint_session"
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})

STATUS = {
    AdminCode.FORBIDDEN: 403,
    AdminCode.SEPARATION_OF_DUTIES: 403,
    AdminCode.NOT_FOUND: 404,
    AdminCode.CONFLICT: 409,
    AdminCode.STALE_EVIDENCE: 409,
}
INVALID = "MINT-INVALID-INPUT"


class Unauthenticated(AdminError):
    """로그인하지 않았거나 세션·토큰이 유효하지 않다(HTTP 401)."""

    def __init__(self, message: str = "로그인이 필요합니다.") -> None:
        super().__init__(AdminCode.FORBIDDEN, message)


@dataclass(frozen=True)
class ConsoleConfig:
    control: ControlConfig
    stage2: Stage2Settings
    litellm_url: str
    # 승인·운영자 등록처럼 LiteLLM 읽기가 필요한 변경에만 쓴다. 없으면 그 변경은 거부한다.
    litellm_admin: LiteLLMAdmin | None
    database_url: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ConsoleConfig":
        control = ControlConfig.from_env(dict(env))
        stage2 = Stage2Settings.from_env(env)
        if not control.litellm_url:
            raise ControlError("MINT_LITELLM_URL이 설정되어 있지 않습니다.")
        admin = control.litellm() if control.litellm_key else None
        return cls(control, stage2, control.litellm_url, admin, control.require_database())

    @property
    def instance_id(self) -> str:
        return self.control.instance_id


@dataclass
class Actor:
    ctx: Context
    session: console_sessions.SessionInfo | None
    session_id: str | None
    config: ConsoleConfig
    request_id: str

    @property
    def conn(self) -> psycopg.Connection:
        return self.ctx.conn

    @property
    def instance(self) -> str:
        return self.ctx.instance_id

    @property
    def role(self) -> str:
        return self.ctx.operator.role

    @property
    def csrf(self) -> str:
        return console_sessions.csrf_token(self.session_id) if self.session_id else ""

    def litellm(self) -> LiteLLMAdmin:
        if self.config.litellm_admin is None:
            raise AdminError(
                AdminCode.FORBIDDEN,
                "이 콘솔에는 제어 평면 키가 설정되어 있지 않아 LiteLLM을 읽는 변경은 "
                "명령줄에서 하십시오.",
            )
        return self.config.litellm_admin


def config_of(request: Request) -> ConsoleConfig:
    return request.app.state.config


def db(request: Request) -> Iterator[psycopg.Connection]:
    """요청마다 연결 하나. 요청이 끝나면 확정하고, 오류면 되돌린다."""
    with connect(config_of(request).database_url) as conn:
        yield conn


async def require_csrf(request: Request) -> None:
    """세션 쿠키로 인증한 변경 요청은 CSRF 토큰(헤더 또는 폼 필드)과 출처가 맞아야 한다."""
    if request.method not in UNSAFE:
        return
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/") != f"{request.url.scheme}://{request.url.netloc}":
        raise AdminError(AdminCode.FORBIDDEN, "다른 출처에서 보낸 요청입니다.")
    cookie = request.cookies.get(COOKIE)
    if not cookie:
        return  # 세션이 없으면 인증 단계가 거부한다(로그인 요청은 출처만 확인한다)
    sent = request.headers.get("x-csrf-token")
    if sent is None and "form" in request.headers.get("content-type", ""):
        sent = (await request.form()).get("csrf")
    if not isinstance(sent, str) or not hmac.compare_digest(
        sent, console_sessions.csrf_token(cookie)
    ):
        raise AdminError(
            AdminCode.FORBIDDEN, "요청을 확인할 수 없습니다. 화면을 새로 고친 뒤 다시 시도하십시오."
        )


def actor_dep(*, allow_token: bool):
    """세션 쿠키(또는 ``allow_token``이면 운영자 토큰)로 행위자를 만든다."""

    def dependency(request: Request, conn: psycopg.Connection = Depends(db)) -> Actor:
        config = config_of(request)
        request_id = request.state.request_id
        header = request.headers.get("authorization", "")
        if allow_token and header.lower().startswith("bearer "):
            try:
                operator = operators.authenticate(
                    conn, config.instance_id, header[7:].strip()
                )
            except AuthError as error:
                raise Unauthenticated(str(error)) from None
            ctx = Context(conn, config.instance_id, operator)
            return Actor(ctx, None, None, config, request_id)
        session_id = request.cookies.get(COOKIE)
        info = (
            console_sessions.lookup(conn, config.instance_id, config.stage2, session_id)
            if session_id
            else None
        )
        if info is None:
            raise Unauthenticated()
        operator = Operator(info.operator_name, info.role, info.user_id)
        ctx = Context(conn, config.instance_id, operator)
        return Actor(ctx, info, session_id, config, request_id)

    return dependency


api_actor = actor_dep(allow_token=True)
page_actor = actor_dep(allow_token=False)


def error_response(error: ControlError, request_id: str) -> JSONResponse:
    if isinstance(error, AdminError):
        status = 401 if isinstance(error, Unauthenticated) else STATUS[error.code]
        body = error.body()
        if ":" not in body["reference"]:  # 사건 키(source_key)가 없으면 요청 식별자이다
            body["reference"] = request_id
    else:
        status = 400
        body = {"code": INVALID, "message": str(error), "reference": request_id}
    return JSONResponse(body, status_code=status, headers={"X-Request-Id": request_id})


def login_redirect(request: Request) -> Response:
    if request.headers.get("hx-request"):
        return Response(status_code=200, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


def public(value: Any) -> Any:
    from mint_litellm.control.services.reads import jsonable

    return jsonable(value)
