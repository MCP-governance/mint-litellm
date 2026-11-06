"""정책 판본 활성화 서비스."""

from mint_litellm.control import policy_versions
from mint_litellm.control.context import Context
from mint_litellm.control.db.connection import fetchone
from mint_litellm.control.services.base import require_role


def activate(
    ctx: Context, database_url: str, version_id: str, source: policy_versions.PolicySource
) -> dict | None:
    """판본을 활성화하고 남은 ``policy_activated`` 사건 행을 돌려준다.

    번들 게시와 정책 엔진 확인은 외부 호출이라 이 연결의 트랜잭션 밖에서 하며, 판본 전환과 사건은
    정책 모듈이 자기 연결의 트랜잭션에서 기록한다. 실패하면 ``ActivationError``이다.
    """
    require_role(ctx, "policy activate")
    policy_versions.activate(
        ctx.conn, database_url, ctx.instance_id, ctx.operator.name, version_id, source
    )
    return fetchone(
        ctx.conn,
        "SELECT event_id, kind, source_key FROM mint.events WHERE instance_id = %s"
        " AND kind = 'policy_activated' AND source_key LIKE %s ORDER BY inserted_at DESC LIMIT 1",
        (ctx.instance_id, f"policy_version:{version_id}:%"),
    )
