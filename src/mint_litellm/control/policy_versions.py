"""정책 판본 활성화의 데이터베이스 단계.

번들 검증과 정책 엔진 적재는 ``verify``가 맡는다. ``verify``는 실제로 적재된 번들의 해시를
확인하지 못하면 예외를 던져야 하며, 그 경우 판본은 만들어지지 않는다.
"""

from collections.abc import Callable

from mint_litellm.control.context import Context
from mint_litellm.control.db.connection import fetchone
from mint_litellm.control.errors import ControlError
from mint_litellm.types.events import EventKind

Verifier = Callable[[str, str, str], None]
"""``verify(version_id, catalog_sha256, bundle_sha256)``"""


def activate(
    ctx: Context, version_id: str, catalog_sha256: str, bundle_sha256: str, verify: Verifier
) -> None:
    existing = fetchone(
        ctx.conn,
        "SELECT status, catalog_sha256, bundle_sha256 FROM mint.policy_versions"
        " WHERE instance_id = %s AND id = %s FOR UPDATE",
        (ctx.instance_id, version_id),
    )
    if existing and (existing["catalog_sha256"], existing["bundle_sha256"]) != (
        catalog_sha256,
        bundle_sha256,
    ):
        raise ControlError(f"판본 {version_id}은(는) 다른 해시로 이미 등록되어 있습니다.")
    if existing and existing["status"] in ("active", "superseded"):
        raise ControlError(
            f"판본 {version_id}은(는) {existing['status']} 상태입니다. 새 판본으로 등록하십시오."
        )
    verify(version_id, catalog_sha256, bundle_sha256)
    if existing is None:
        ctx.conn.execute(
            "INSERT INTO mint.policy_versions (instance_id, id, catalog_sha256, bundle_sha256)"
            " VALUES (%s, %s, %s, %s)",
            (ctx.instance_id, version_id, catalog_sha256, bundle_sha256),
        )
    ctx.conn.execute(
        "UPDATE mint.policy_versions SET status = 'staged'"
        " WHERE instance_id = %s AND id = %s AND status = 'validated'",
        (ctx.instance_id, version_id),
    )
    previous = fetchone(
        ctx.conn,
        "UPDATE mint.policy_versions SET status = 'superseded'"
        " WHERE instance_id = %s AND status = 'active' RETURNING id",
        (ctx.instance_id,),
    )
    row = fetchone(
        ctx.conn,
        "UPDATE mint.policy_versions SET status = 'active', activated_at = now(),"
        " activated_by = %s WHERE instance_id = %s AND id = %s RETURNING state_version",
        (ctx.operator.name, ctx.instance_id, version_id),
    )
    ctx.emit(
        EventKind.POLICY_ACTIVATED,
        version_id,
        row["state_version"],
        {
            "policy_version_id": version_id,
            "catalog_sha256": catalog_sha256,
            "bundle_sha256": bundle_sha256,
            "superseded": None if previous is None else previous["id"],
        },
    )


def load_verifier() -> Verifier:
    """정책 레인의 번들 검증 모듈을 불러온다. 없으면 활성화할 수 없다."""
    try:
        from mint_litellm.plugin.policy.bundle import verify_and_load  # type: ignore[import-not-found]
    except ImportError:
        raise ControlError(
            "정책 번들 검증 모듈이 이 설치본에 없어 정책을 활성화할 수 없습니다."
        ) from None
    return verify_and_load
