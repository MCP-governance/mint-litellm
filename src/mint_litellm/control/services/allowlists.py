"""허용 목록 서비스."""

from datetime import datetime

from mint_litellm.control import allowlists as domain
from mint_litellm.control.context import Context
from mint_litellm.control.services.base import lock_version, service


@service("allowlist add")
def add(
    ctx: Context,
    kind: str,
    value: str,
    valid_from: datetime,
    valid_until: datetime,
    org_id: str | None,
    note: str | None,
) -> str:
    return domain.add(ctx, kind, value, valid_from, valid_until, org_id, note)


@service("allowlist lift")
def lift(ctx: Context, allowlist_id: str, *, expected_version: int | None = None) -> None:
    lock_version(ctx, "allowlists", allowlist_id, expected_version)
    domain.lift(ctx, allowlist_id)
