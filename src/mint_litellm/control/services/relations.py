"""이용 관계 서비스."""

from mint_litellm.control import relations as domain
from mint_litellm.control.context import Context
from mint_litellm.control.services.base import lock_version, service


@service("relation add")
def add(
    ctx: Context, org_id: str, server_id: str, purpose: str, team_id: str | None
) -> str:
    return domain.add(ctx, org_id, server_id, purpose, team_id)


@service("relation terminate")
def terminate(
    ctx: Context, relation_id: str, *, expected_version: int | None = None
) -> None:
    lock_version(ctx, "usage_relations", relation_id, expected_version)
    domain.terminate(ctx, relation_id)
