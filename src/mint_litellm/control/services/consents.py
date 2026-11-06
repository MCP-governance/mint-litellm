"""운영자가 증명한 동의 서비스."""

from datetime import datetime

from mint_litellm.control import consents as domain
from mint_litellm.control.context import Context
from mint_litellm.control.litellm_admin import LiteLLMAdmin
from mint_litellm.control.services.base import (
    Prefetched,
    lock_version,
    require_role,
    service,
    transact,
)


def add(
    ctx: Context,
    litellm: LiteLLMAdmin,
    org_id: str,
    human_user_id: str,
    server_id: str,
    purpose: str,
    data_scope: str,
    text_version: str,
    granted_at: datetime,
    valid_until: datetime,
    evidence_ref: str,
    evidence_sha256: str,
) -> str:
    require_role(ctx, "consent add")
    facts = Prefetched(litellm).prefetch("get_user", human_user_id)
    with transact(ctx):
        return domain.add(
            ctx,
            facts,  # type: ignore[arg-type]
            org_id,
            human_user_id,
            server_id,
            purpose,
            data_scope,
            text_version,
            granted_at,
            valid_until,
            evidence_ref,
            evidence_sha256,
        )


@service("consent revoke")
def revoke(ctx: Context, consent_id: str, *, expected_version: int | None = None) -> None:
    lock_version(ctx, "consents", consent_id, expected_version)
    domain.revoke(ctx, consent_id)
