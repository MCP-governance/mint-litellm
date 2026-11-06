"""운영자 등록 서비스."""

import psycopg

from mint_litellm.control import operators as domain
from mint_litellm.control.context import Context
from mint_litellm.control.litellm_admin import LiteLLMAdmin
from mint_litellm.control.services.base import Prefetched, require_role, transact
from mint_litellm.types.cli_commands import OperatorRole


def add(
    ctx: Context,
    litellm: LiteLLMAdmin,
    name: str,
    role: OperatorRole,
    user_id: str | None,
    expires_in_days: int | None,
) -> str:
    """운영자를 등록하고 토큰(한 번만 표시)을 돌려준다."""
    require_role(ctx, "operator add")
    facts = Prefetched(litellm)
    if user_id:
        facts.prefetch("get_user", user_id)
    with transact(ctx):
        return domain.add(ctx, facts, name, role, user_id, expires_in_days)  # type: ignore[arg-type]


def bootstrap(
    conn: psycopg.Connection,
    instance_id: str,
    litellm: LiteLLMAdmin,
    name: str,
    user_id: str,
    emitted: list | None = None,
) -> str:
    """운영자가 하나도 없을 때만 토큰 없이 첫 admin을 만든다(토큰 없는 명령)."""
    facts = Prefetched(litellm).prefetch("get_user", user_id)
    mark = len(emitted) if emitted is not None else 0
    try:
        with conn.transaction():
            return domain.bootstrap(conn, instance_id, facts, name, user_id, emitted)  # type: ignore[arg-type]
    except BaseException:
        if emitted is not None:
            del emitted[mark:]
        raise
