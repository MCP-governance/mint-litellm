"""조직과 팀·사용자 바인딩 서비스."""

from mint_litellm.control import orgs as domain
from mint_litellm.control.context import Context
from mint_litellm.control.services.base import lock_version, service
from mint_litellm.types.identity import SubjectType


@service("org add")
def add(ctx: Context, org_id: str, name: str) -> None:
    domain.add(ctx, org_id, name)


@service("org close")
def close(ctx: Context, org_id: str, *, expected_version: int | None = None) -> None:
    lock_version(ctx, "orgs", org_id, expected_version)
    domain.close(ctx, org_id)


@service("org bind")
def bind(ctx: Context, org_id: str, subject_type: SubjectType, subject_id: str) -> None:
    domain.bind(ctx, org_id, subject_type, subject_id)


@service("org unbind")
def unbind(ctx: Context, subject_type: SubjectType, subject_id: str) -> None:
    domain.unbind(ctx, subject_type, subject_id)
